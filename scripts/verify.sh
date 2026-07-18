#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${MAPP_ENV_FILE:-${ROOT_DIR}/.env}"

dotenv_value() {
  local key="$1"
  local value
  value="$(sed -n "s/^${key}=//p" "${ENV_FILE}" | tail -n 1)"
  value="${value%$'\r'}"
  if [[ "${value}" == \"*\" && "${value}" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "${value}" == \'*\' && "${value}" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "${value}"
}

environment_value() {
  local key="$1"
  if [[ -v "${key}" ]]; then
    printf '%s' "${!key}"
  else
    dotenv_value "${key}"
  fi
}

reject_database_environment_overrides() {
  local key configured
  for key in \
    DBS_MAPP ETL_DATABASE_URL POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD \
    ETL_DB_USER ETL_DB_PASSWORD XYZ_DB_USER XYZ_DB_PASSWORD
  do
    if [[ -v "${key}" ]]; then
      configured="$(dotenv_value "${key}")"
      if [[ "${!key}" != "${configured}" ]]; then
        printf 'Exported %s conflicts with the authoritative value in %s; unset it or update the env file deliberately.\n' \
          "${key}" "${ENV_FILE}" >&2
        exit 2
      fi
    fi
  done
}

if [[ ! -f "${ENV_FILE}" ]]; then
  printf 'Missing %s. Run ./bin/mapp init first.\n' "${ENV_FILE}" >&2
  exit 2
fi
reject_database_environment_overrides

compose=(
  docker compose
  --project-directory "${ROOT_DIR}"
  --env-file "${ENV_FILE}"
  --file "${ROOT_DIR}/compose.yaml"
)
database_mode="$(dotenv_value MAPP_DATABASE_MODE)"
if [[ -v MAPP_DATABASE_MODE && "${MAPP_DATABASE_MODE}" != "${database_mode}" ]]; then
  printf 'Exported MAPP_DATABASE_MODE conflicts with the authoritative value in %s; unset it or update the env file deliberately.\n' \
    "${ENV_FILE}" >&2
  exit 2
fi
case "${database_mode}" in
  bundled)
    compose+=(--file "${ROOT_DIR}/compose.bundled-db.yaml")
    required_services=(db xyz xyz-preview config-ui browser-runner caddy)
    ;;
  external)
    required_services=(xyz xyz-preview config-ui browser-runner caddy)
    ;;
  *)
    printf 'MAPP_DATABASE_MODE must be bundled or external.\n' >&2
    exit 2
    ;;
esac
deployment_environment="$(dotenv_value MAPP_ENVIRONMENT)"
if [[ -v MAPP_ENVIRONMENT && "${MAPP_ENVIRONMENT}" != "${deployment_environment}" ]]; then
  printf 'Exported MAPP_ENVIRONMENT conflicts with the authoritative value in %s; unset it or update the env file deliberately.\n' \
    "${ENV_FILE}" >&2
  exit 2
fi
case "${deployment_environment}" in
  development)
    production=false
    ;;
  production)
    production=true
    python3 "${ROOT_DIR}/scripts/validate_production_env.py" \
      --environment "${ENV_FILE}"
    compose+=(--file "${ROOT_DIR}/compose.production.yaml")
    ;;
  *)
    printf 'MAPP_ENVIRONMENT must be development or production.\n' >&2
    exit 2
    ;;
esac

"${compose[@]}" config --quiet
resolved_dbs="$(
  "${compose[@]}" config --format json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["xyz"]["environment"]["DBS_MAPP"], end="")'
)"
if [[ "${database_mode}" == "external" ]]; then
  printf '%s' "${resolved_dbs}" \
    | python3 "${ROOT_DIR}/scripts/validate_database_url.py"
fi

for service in "${required_services[@]}"; do
  deadline=$((SECONDS + 60))
  health="missing"
  while ((SECONDS < deadline)); do
    container_id="$("${compose[@]}" ps --quiet "${service}")"
    if [[ -n "${container_id}" ]]; then
      health="$(
        docker inspect \
          --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
          "${container_id}" 2>/dev/null || true
      )"
      if [[ "${health}" == "healthy" ]]; then
        break
      fi
      if [[ "${health}" == "exited" || "${health}" == "dead" ]]; then
        break
      fi
    fi
    sleep 2
  done
  if [[ "${health}" != "healthy" ]]; then
    printf '%s health is %s after waiting, expected healthy.\n' \
      "${service}" "${health}" >&2
    exit 1
  fi
done

for service in xyz config-ui; do
  running_dbs="$(
    "${compose[@]}" exec -T "${service}" sh -c 'printf %s "$DBS_MAPP"'
  )"
  if [[ "${running_dbs}" != "${resolved_dbs}" ]]; then
    printf '%s is not running with the DBS_MAPP value resolved from the current environment. Recreate the platform services before verification.\n' \
      "${service}" >&2
    exit 1
  fi
done

if ! "${compose[@]}" exec -T browser-runner \
  curl --fail --silent --show-error http://xyz-preview:3000/ >/dev/null; then
  printf 'Isolated XYZ preview could not render its private workspace.\n' >&2
  exit 1
fi

if [[ "${database_mode}" == "bundled" ]]; then
  "${compose[@]}" exec -T db sh -c \
    'exec psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1' <<'SQL'
SELECT postgis_full_version();

DO $$
DECLARE
  mismatch_count integer;
BEGIN
  SELECT count(*) INTO mismatch_count
  FROM (VALUES
    ('bus_stops', 'created_at', 'timestamp with time zone'),
    ('definitive_paths', 'length_metres', 'double precision'),
    ('smoke_control_orders', 'source_id', 'integer'),
    ('smoke_control_orders', 'description', 'text'),
    ('smoke_control_orders', 'area_square_metres', 'double precision'),
    ('smoke_control_orders', 'registered_at', 'timestamp with time zone')
  ) AS expected(table_name, column_name, data_type)
  LEFT JOIN information_schema.columns actual
    ON actual.table_schema = 'leeds'
   AND actual.table_name = expected.table_name
   AND actual.column_name = expected.column_name
   AND actual.data_type = expected.data_type
  WHERE actual.column_name IS NULL;

  IF mismatch_count <> 0 THEN
    RAISE EXCEPTION '% representative typed ETL columns are missing or mismatched', mismatch_count;
  END IF;
END
$$;

DO $$
DECLARE
  relation text;
  row_total bigint;
  bad_geom bigint;
BEGIN
  FOREACH relation IN ARRAY ARRAY[
    'leeds.bus_stops',
    'leeds.definitive_paths',
    'leeds.smoke_control_orders'
  ] LOOP
    IF to_regclass(relation) IS NULL THEN
      RAISE EXCEPTION 'Missing ETL relation: %', relation;
    END IF;

    EXECUTE format('SELECT count(*) FROM %s', relation) INTO row_total;
    IF row_total = 0 THEN
      RAISE EXCEPTION 'ETL relation is empty: %', relation;
    END IF;

    EXECUTE format(
      'SELECT count(*) FROM %s WHERE geom IS NOT NULL AND (ST_SRID(geom) <> 4326 OR ST_SRID(geom_3857) <> 3857 OR NOT ST_IsValid(geom))',
      relation
    ) INTO bad_geom;
    IF bad_geom <> 0 THEN
      RAISE EXCEPTION 'Invalid geometry/SRID rows in %: %', relation, bad_geom;
    END IF;

    RAISE NOTICE '%: % rows verified', relation, row_total;
  END LOOP;
END
$$;

SELECT
  layers.layer_key,
  layers.target_table,
  layers.geometry_type,
  layers.source_srid,
  runs.expected_count,
  runs.rows_seen,
  runs.rows_deleted,
  runs.finished_at
FROM leeds._etl_layers AS layers
JOIN leeds._etl_runs AS runs
  ON runs.run_id = layers.last_successful_run_id
WHERE runs.status = 'succeeded'
ORDER BY layers.layer_key;
SQL
fi

"${compose[@]}" exec -T xyz node --input-type=module -e '
  import pg from "pg";
  const pool = new pg.Pool({
    connectionString: process.env.DBS_MAPP,
    connectionTimeoutMillis: 10000,
    query_timeout: 15000,
    statement_timeout: 15000,
  });
  const result = await pool.query("SELECT current_database() AS db, PostGIS_Version() AS postgis");
  if (!result.rows[0]?.postgis) process.exitCode = 1;
  await pool.end();
'

published_http="$("${compose[@]}" port caddy 80 | tail -n 1)"
published_http="${published_http/#0.0.0.0:/127.0.0.1:}"
published_http="${published_http/#\[::\]:/[::1]:}"
if [[ "${production}" == true ]]; then
  map_url="$("${compose[@]}" exec -T caddy sh -c 'printf %s "$MAP_SITE"')"
  config_url="$("${compose[@]}" exec -T caddy sh -c 'printf %s "$CONFIG_SITE"')"
  published_https="$("${compose[@]}" port caddy 443 | tail -n 1)"
  published_https="${published_https/#0.0.0.0:/127.0.0.1:}"
  published_https="${published_https/#\[::\]:/[::1]:}"
  https_port="${published_https##*:}"
  https_address="${published_https%:*}"
  map_headers=()
  config_headers=()
else
  map_url="http://${published_http}"
  config_url="${map_url}"
  map_headers=(--header 'Host: localhost')
  config_headers=(--header 'Host: config.localhost')
fi
map_url="${map_url%/}"
config_url="${config_url%/}"
if [[ "${production}" == true ]]; then
  map_host="${map_url#https://}"
  config_host="${config_url#https://}"
  map_host="${map_host%:443}"
  config_host="${config_host%:443}"
  map_headers=(--resolve "${map_host}:${https_port}:${https_address}")
  config_headers=(--resolve "${config_host}:${https_port}:${https_address}")
  check_https_redirect() {
    local hostname="$1"
    local expected_url="$2"
    local response status location
    response="$(
      curl --silent --output /dev/null \
        --write-out $'%{http_code}\n%{redirect_url}' \
        --header "Host: ${hostname}" \
        "http://${published_http}/"
    )"
    status="${response%%$'\n'*}"
    location="${response#*$'\n'}"
    if [[ "${status}" != "301" && "${status}" != "308" ]]; then
      printf 'Caddy HTTP redirect for %s returned %s, expected 301 or 308.\n' \
        "${hostname}" "${status}" >&2
      exit 1
    fi
    if [[ "${location}" != "${expected_url}/" ]]; then
      printf 'Caddy HTTP redirect for %s did not target its exact HTTPS origin.\n' \
        "${hostname}" >&2
      exit 1
    fi
  }
  check_https_redirect "${map_host}" "https://${map_host}"
  check_https_redirect "${config_host}" "https://${config_host}"
  if ! curl --fail --silent --show-error --dump-header - --output /dev/null \
    "${map_headers[@]}" "${map_url}/" \
    | grep --ignore-case '^strict-transport-security: max-age=31536000' >/dev/null; then
    printf 'Map HTTPS response is missing the required HSTS policy.\n' >&2
    exit 1
  fi
  if ! curl --fail --silent --show-error --dump-header - --output /dev/null \
    "${config_headers[@]}" "${config_url}/" \
    | grep --ignore-case '^strict-transport-security: max-age=31536000' >/dev/null; then
    printf 'Configuration HTTPS response is missing the required HSTS policy.\n' >&2
    exit 1
  fi
fi
curl --fail --silent --show-error "${map_headers[@]}" "${map_url}/" >/dev/null
curl --fail --silent --show-error "${map_headers[@]}" "${map_url}/api/workspace/locales" >/dev/null

curl --fail --silent --show-error "${config_headers[@]}" "${config_url}/healthz" >/dev/null
curl --fail --silent --show-error "${config_headers[@]}" "${config_url}/api/public/identity" >/dev/null
traversal_status="$(
  curl --path-as-is --silent "${config_headers[@]}" \
    --output /dev/null --write-out '%{http_code}' \
    "${config_url}/../../app.py"
)"
if [[ "${traversal_status}" != "404" ]]; then
  printf 'Configuration static-file traversal guard returned %s, expected 404.\n' \
    "${traversal_status}" >&2
  exit 1
fi
catalog_count="$("${compose[@]}" exec -T config-ui python -c 'import app; print(len(app.discover()))')"
if ((catalog_count < 1)); then
  printf 'Config UI did not discover any renderable database tables.\n' >&2
  exit 1
fi
icon_count="$("${compose[@]}" exec -T config-ui python -c 'import app; print(len(app.discover_icons()))')"
if ((icon_count < 1)); then
  printf 'Config UI did not discover any shared SVG icons.\n' >&2
  exit 1
fi
curl --fail --silent --show-error "${map_headers[@]}" "${map_url}/instance/svg/bus.svg" >/dev/null

if [[ "${database_mode}" == "bundled" ]]; then
  mvt_query="$(
    "${compose[@]}" exec -T config-ui python - <<'PY'
from urllib.parse import urlencode

from app import read_workspace
from control_api import effective_locales

_, workspace, _ = read_workspace()
selected = None
for locale_key, locale in effective_locales(workspace).items():
    layers = locale.get("layers")
    if not isinstance(layers, dict):
        continue
    for layer_key, layer in layers.items():
        if not isinstance(layer, dict) or layer.get("format") != "mvt":
            continue
        table = layer.get("table")
        geom = layer.get("geom")
        if isinstance(table, str) and table and isinstance(geom, str) and geom:
            selected = {
                "template": "mvt",
                "locale": locale_key,
                "layer": layer_key,
                "table": table,
                "geom": geom,
                "x": 1015,
                "y": 659,
                "z": 11,
            }
            break
    if selected:
        break

if not selected:
    raise SystemExit("The current workspace has no database-backed MVT layer to verify.")
print(urlencode(selected), end="")
PY
  )"
  mvt_file="$(mktemp)"
  trap 'rm -f "${mvt_file}"' EXIT
  curl --fail --silent --show-error "${map_headers[@]}" \
    "${map_url}/api/query?${mvt_query}" \
    --output "${mvt_file}"
  if [[ ! -s "${mvt_file}" ]]; then
    printf 'XYZ returned an empty MVT response for a current workspace layer.\n' >&2
    exit 1
  fi
fi

blocked_status="$(curl --silent "${map_headers[@]}" --output /dev/null --write-out '%{http_code}' "${map_url}/api/provider/file?url=../../proc/self/environ")"
if [[ "${blocked_status}" != "404" ]]; then
  printf 'Gateway file-provider guard returned %s, expected 404.\n' "${blocked_status}" >&2
  exit 1
fi
published_automation_status="$(
  curl --silent --header 'Host: caddy' \
    --output /dev/null --write-out '%{http_code}' \
    "http://${published_http}/api/provider/file?url=../../proc/self/environ"
)"
if [[ "${published_automation_status}" != "404" ]]; then
  printf 'Published automation-host guard returned %s, expected 404.\n' \
    "${published_automation_status}" >&2
  exit 1
fi
internal_automation_status="$(
  "${compose[@]}" exec -T config-ui python -c \
    'import urllib.error, urllib.request
try:
    urllib.request.urlopen("http://caddy:8081/api/provider/file?url=../../proc/self/environ")
except urllib.error.HTTPError as error:
    print(error.code)
else:
    print(200)'
)"
if [[ "${internal_automation_status}" != "404" ]]; then
  printf 'Internal automation file-provider guard returned %s, expected 404.\n' \
    "${internal_automation_status}" >&2
  exit 1
fi

if [[ "${database_mode}" == "bundled" ]]; then
  printf 'PASS: bundled PostGIS and sample data, service health, public config identity, browser-runner health, shared SVG icons, XYZ, and Caddy guards.\n'
else
  printf 'PASS: external PostGIS connectivity, service health, catalog discovery, public config identity, browser-runner health, shared SVG icons, XYZ, and Caddy guards. Run layer-specific visual tests for the external workspace.\n'
fi
