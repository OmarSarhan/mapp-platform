#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${MAPP_ENV_FILE:-${ROOT_DIR}/.env}"

compose=(
  docker compose
  --project-directory "${ROOT_DIR}"
  --env-file "${ENV_FILE}"
  --file "${ROOT_DIR}/compose.yaml"
)
case "${MAPP_ENVIRONMENT:-development}" in
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
    printf 'Unsupported MAPP_ENVIRONMENT: expected development or production.\n' >&2
    exit 2
    ;;
esac

"${compose[@]}" config --quiet

for service in db xyz config-ui browser-runner caddy; do
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
    ('planning_applications_recent', 'application_object_id', 'integer'),
    ('planning_applications_recent', 'proposal', 'text'),
    ('planning_applications_recent', 'housing_density', 'double precision'),
    ('planning_applications_recent', 'validated_at', 'timestamp with time zone')
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
    'leeds.planning_applications_recent'
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

"${compose[@]}" exec -T xyz node --input-type=module -e '
  import pg from "pg";
  const pool = new pg.Pool({connectionString: process.env.DBS_MAPP});
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

mvt_file="$(mktemp)"
trap 'rm -f "${mvt_file}"' EXIT
curl --fail --silent --show-error "${map_headers[@]}" --get "${map_url}/api/query" \
  --data-urlencode "template=mvt" \
  --data-urlencode "locale=locale" \
  --data-urlencode "layer=Bus Stops" \
  --data-urlencode "table=leeds.bus_stops" \
  --data-urlencode "geom=geom_3857" \
  --data-urlencode "x=1015" \
  --data-urlencode "y=659" \
  --data-urlencode "z=11" \
  --output "${mvt_file}"
if [[ ! -s "${mvt_file}" ]]; then
  printf 'XYZ returned an empty MVT response for the Leeds smoke tile.\n' >&2
  exit 1
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

printf 'PASS: Compose, service health, PostGIS, ETL tables, public config identity, browser-runner health, shared SVG icons, SRIDs, XYZ, and Caddy guards.\n'
