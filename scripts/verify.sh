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
    "${!DBS_@}" "${!FEDERATION_DBS_@}" \
    ETL_DATABASE_URL POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD \
    ETL_DB_USER ETL_DB_PASSWORD XYZ_DB_USER XYZ_DB_PASSWORD \
    DERIVED_DB_USER DERIVED_DB_PASSWORD DERIVED_DATABASE_URL \
    DERIVED_OWNER_ROLE DERIVED_READER_ROLE \
    FEDERATION_DB_USER FEDERATION_DB_PASSWORD FEDERATION_DATABASE_URL \
    SOURCE_POSTGRES_DB SOURCE_POSTGRES_USER SOURCE_POSTGRES_PASSWORD \
    SOURCE_READER_USER SOURCE_READER_PASSWORD
  do
    if [[ -v "${key}" ]]; then
      configured="$(dotenv_value "${key}")"
      if [[ "${!key}" != "${configured}" ]]; then
        printf 'Exported %s conflicts with the authoritative value in %s; unset it or update the env file deliberately.\n' \
          "${key}" "${ENV_FILE}" >&2
        exit 2
      fi
      # Shell values outrank --env-file interpolation in Compose. Remove an
      # exact duplicate so nested values such as DBS_MAPP resolve from .env.
      unset "${key}"
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
compose+=(--file "${ROOT_DIR}/compose.bundled-db.yaml")
required_services=(db semantic-service xyz xyz-preview config-ui browser-runner egress-proxy caddy)
# An overlay that carries FEDERATION_DBS_<REF> entries must be applied whenever
# an alias using them could be registered, or recreating config-ui silently
# strips the reference and the periodic verifier withdraws that source. One
# rule, applied to both opt-in overlays.
#
# The demo overlay also starts its two databases, because turning the demo on
# means running it. The federation-test rig does not: its source-db is started
# by ./bin/mapp federation-test alone, so this only carries its reference
# through to config-ui.
#
# Deliberately NOT added to the exported-value conflict guard that
# MAPP_ENVIRONMENT uses: no compose file interpolates MAPP_DEMO_SOURCES, so an
# exported value cannot reach the resolved model.
demo_sources="$(dotenv_value MAPP_DEMO_SOURCES)"
if [[ -n "${demo_sources}" ]]; then
  compose+=(--file "${ROOT_DIR}/compose.federated-demo.yaml")
  required_services+=(census-db ops-db)
fi
if [[ -n "$(dotenv_value FEDERATION_DBS_LEEDS_EXT)" ]]; then
  compose+=(--file "${ROOT_DIR}/compose.federation-test.yaml")
fi
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
census_manifest_contract="$(
  PYTHONPATH="${ROOT_DIR}/etl/src" python3 - \
    "${ROOT_DIR}/instance/etl/census.json" <<'PY'
import json
import sys

from leeds_arcgis_etl.census_config import load_census_config


config = load_census_config(sys.argv[1])
topic_hashes = {topic.id: topic.sha256 for topic in config.topics}
print(
    config.geometry_sha256
    + "\t"
    + json.dumps(topic_hashes, sort_keys=True, separators=(",", ":"))
)
PY
)"
IFS=$'\t' read -r census_geometry_sha256 census_topic_hashes_json \
  <<<"${census_manifest_contract}"
if [[ -z "${census_geometry_sha256}" || -z "${census_topic_hashes_json}" ]]; then
  printf 'Could not read the pinned Census source hashes from instance/etl/census.json.\n' >&2
  exit 2
fi
PLUGIN_DIR="${ROOT_DIR}/instance/public/plugins" \
  PYTHONPATH="${ROOT_DIR}/config-ui" \
  python3 -c 'from plugin_registry import catalogue; import sys; result = catalogue(); print("Plugin catalogue fingerprint: " + result["fingerprint"]); sys.exit(0 if result["valid"] else 1)'
resolved_dbs="$(
  "${compose[@]}" config --format json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["xyz"]["environment"]["DBS_MAPP"], end="")'
)"
resolved_derived_dbs="$(
  "${compose[@]}" config --format json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["config-ui"]["environment"]["DERIVED_DATABASE_URL"], end="")'
)"
resolved_derived_owner="$(
  "${compose[@]}" config --format json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["config-ui"]["environment"]["DERIVED_OWNER_ROLE"], end="")'
)"
resolved_derived_reader="$(
  "${compose[@]}" config --format json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["config-ui"]["environment"]["DERIVED_READER_ROLE"], end="")'
)"
resolved_federation_dbs="$(
  "${compose[@]}" config --format json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["config-ui"]["environment"].get("FEDERATION_DATABASE_URL", ""), end="")'
)"
resolved_federation_role="$(
  "${compose[@]}" config --format json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["config-ui"]["environment"].get("FEDERATION_DB_USER", ""), end="")'
)"
if [[ -z "${resolved_derived_dbs}" \
      && ( -n "${resolved_derived_owner}" || -n "${resolved_derived_reader}" ) ]] \
  || [[ -n "${resolved_derived_dbs}" \
      && ( -z "${resolved_derived_owner}" || -z "${resolved_derived_reader}" ) ]]; then
  printf 'DERIVED_DATABASE_URL, DERIVED_OWNER_ROLE, and DERIVED_READER_ROLE must either all be configured or all be empty.\n' >&2
  exit 2
fi
if [[ -z "${resolved_federation_dbs}" && -n "${resolved_federation_role}" ]] \
  || [[ -n "${resolved_federation_dbs}" && -z "${resolved_federation_role}" ]]; then
  printf 'FEDERATION_DATABASE_URL and FEDERATION_DB_USER must either both be configured or both be empty.\n' >&2
  exit 2
fi
if [[ -z "${resolved_federation_dbs}" ]]; then
  printf 'FEDERATION_DATABASE_URL and FEDERATION_DB_USER are required with a local database.\n' >&2
  exit 2
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

"${compose[@]}" exec -T config-ui python -c '
import json
import os
import urllib.request

request = urllib.request.Request(
    os.environ["SEMANTIC_SERVICE_URL"].rstrip("/") + "/v1/status",
    headers={
        "Authorization": "Bearer " + os.environ["SEMANTIC_INTERNAL_TOKEN"],
        "X-MAPP-Actor": "system:verify",
        "X-MAPP-Scopes": "semantic:inspect",
    },
)
with urllib.request.urlopen(request, timeout=5) as response:
    payload = json.load(response)
if response.status != 200 or not isinstance(payload.get("catalogRevision"), int):
    raise SystemExit("Semantic service status contract failed.")
print("Private semantic service authentication and status contract verified.")
'

for service in xyz xyz-preview config-ui; do
  running_dbs="$(
    "${compose[@]}" exec -T "${service}" sh -c 'printf %s "$DBS_MAPP"'
  )"
  if [[ "${running_dbs}" != "${resolved_dbs}" ]]; then
    if [[ "${running_dbs}" == *'${'* ]]; then
      printf '%s is running with unresolved placeholders in DBS_MAPP. Start services through ./bin/mapp so the private environment is resolved before container creation.\n' \
        "${service}" >&2
    else
      printf '%s is not running with the DBS_MAPP value resolved from the current environment.\n' \
        "${service}" >&2
    fi
    printf 'Run ./bin/mapp up --force-recreate to replace the stale containers, then run ./bin/mapp verify again.\n' >&2
    exit 1
  fi
done

running_derived_dbs="$(
  "${compose[@]}" exec -T config-ui sh -c \
    'printf %s "$DERIVED_DATABASE_URL"'
)"
if [[ "${running_derived_dbs}" != "${resolved_derived_dbs}" ]]; then
  printf 'config-ui is not running with the DERIVED_DATABASE_URL value resolved from the current environment. Recreate the service before verification.\n' >&2
  exit 1
fi
running_derived_owner="$(
  "${compose[@]}" exec -T config-ui sh -c \
    'printf %s "$DERIVED_OWNER_ROLE"'
)"
if [[ "${running_derived_owner}" != "${resolved_derived_owner}" ]]; then
  printf 'config-ui is not running with the DERIVED_OWNER_ROLE value resolved from the current environment. Recreate the service before verification.\n' >&2
  exit 1
fi
running_derived_reader="$(
  "${compose[@]}" exec -T config-ui sh -c \
    'printf %s "$DERIVED_READER_ROLE"'
)"
if [[ "${running_derived_reader}" != "${resolved_derived_reader}" ]]; then
  printf 'config-ui is not running with the DERIVED_READER_ROLE value resolved from the current environment. Recreate the service before verification.\n' >&2
  exit 1
fi
running_federation_dbs="$(
  "${compose[@]}" exec -T config-ui sh -c \
    'printf %s "$FEDERATION_DATABASE_URL"'
)"
if [[ "${running_federation_dbs}" != "${resolved_federation_dbs}" ]]; then
  printf 'config-ui is not running with the FEDERATION_DATABASE_URL value resolved from the current environment. Recreate the service before verification.\n' >&2
  exit 1
fi
running_federation_role="$(
  "${compose[@]}" exec -T config-ui sh -c \
    'printf %s "$FEDERATION_DB_USER"'
)"
if [[ "${running_federation_role}" != "${resolved_federation_role}" ]]; then
  printf 'config-ui is not running with the FEDERATION_DB_USER value resolved from the current environment. Recreate the service before verification.\n' >&2
  exit 1
fi

plugin_hashes() {
  local service="$1" root="$2"
  "${compose[@]}" exec -T "${service}" sh -c \
    "cd '${root}' && find . -type f -not -type l -print0 | sort -z | xargs -0 sha256sum"
}
if ! diff -u \
  <(plugin_hashes xyz /app/xyz/public/instance/plugins) \
  <(plugin_hashes config-ui /instance-public/plugins); then
  printf 'Live XYZ and configuration service plugin mounts differ.\n' >&2
  exit 1
fi
if ! diff -u \
  <(plugin_hashes xyz /app/xyz/public/instance/plugins) \
  <(plugin_hashes xyz-preview /app/xyz/public/instance/plugins); then
  printf 'Live and preview XYZ plugin mounts differ.\n' >&2
  exit 1
fi

if ! "${compose[@]}" exec -T browser-runner \
  curl --fail --silent --show-error http://xyz-preview:3000/ >/dev/null; then
  printf 'Isolated XYZ preview could not render its private workspace.\n' >&2
  exit 1
fi

"${compose[@]}" exec -T db \
  sh /usr/local/bin/mapp-prepare-spatial-indexes check
"${compose[@]}" exec -T db sh -c \
  'exec psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set ON_ERROR_STOP=1 \
    --set=etl_db_user="$ETL_DB_USER" \
    --set=xyz_db_user="$XYZ_DB_USER" \
    --set=derived_db_user="$DERIVED_DB_USER"' <<'SQL'
SELECT postgis_full_version();
SELECT set_config('mapp.verify.etl_db_user', :'etl_db_user', false);
SELECT set_config('mapp.verify.xyz_db_user', :'xyz_db_user', false);
SELECT set_config(
'mapp.verify.derived_db_user',
:'derived_db_user',
false
);


DO $$
DECLARE
table_present integer;
guard_event_trigger_count integer;
BEGIN
SELECT count(*) INTO table_present
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS ns
  ON ns.oid = relation.relnamespace
WHERE ns.nspname = 'public'
  AND relation.relname = 'mapp_platform_layer_dependencies';
IF table_present = 0 THEN
  RAISE EXCEPTION 'Layer dependency guard table public.mapp_platform_layer_dependencies is missing.';
END IF;

IF NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_proc AS proc
  JOIN pg_catalog.pg_namespace AS ns
    ON ns.oid = proc.pronamespace
  WHERE proc.proname = 'mapp_sync_platform_layer_dependencies'
    AND ns.nspname = 'public'
) THEN
  RAISE EXCEPTION 'Layer dependency sync function public.mapp_sync_platform_layer_dependencies is missing.';
END IF;

IF to_regprocedure('public.mapp_sync_platform_layer_dependencies(text, jsonb)') IS NULL THEN
  RAISE EXCEPTION 'Layer dependency sync function public.mapp_sync_platform_layer_dependencies(text, jsonb) is missing.';
END IF;

IF NOT has_function_privilege(
  'public',
  'public.mapp_sync_platform_layer_dependencies(text, jsonb)',
  'execute'
) THEN
  RAISE EXCEPTION 'PUBLIC does not have execute permission on public.mapp_sync_platform_layer_dependencies(text, jsonb).';
END IF;

SELECT count(*) INTO guard_event_trigger_count
FROM pg_catalog.pg_event_trigger AS trigger
JOIN pg_catalog.pg_proc AS proc
  ON proc.oid = trigger.evtfoid
JOIN pg_catalog.pg_namespace AS ns
  ON ns.oid = proc.pronamespace
WHERE trigger.evtname = 'mapp_block_platform_layer_drops'
  AND proc.proname = 'mapp_block_platform_layer_drops'
  AND ns.nspname = 'public'
;
IF guard_event_trigger_count <> 1 THEN
  RAISE EXCEPTION 'Layer drop guard event trigger mapp_block_platform_layer_drops is missing.';
END IF;

IF NOT EXISTS (
  SELECT 1
  FROM pg_catalog.pg_proc AS proc
  JOIN pg_catalog.pg_namespace AS ns
    ON ns.oid = proc.pronamespace
  WHERE proc.proname = 'mapp_block_platform_layer_drops'
    AND ns.nspname = 'public'
) THEN
  RAISE EXCEPTION 'Layer drop guard function public.mapp_block_platform_layer_drops is missing.';
END IF;
END
$$;


DO $$
DECLARE
checked_relation regclass;
etl_role name := current_setting('mapp.verify.etl_db_user');
xyz_role name := current_setting('mapp.verify.xyz_db_user');
derived_role name := current_setting('mapp.verify.derived_db_user');
row_total bigint;
distinct_code_count bigint;
invalid_code_count bigint;
invalid_geometry_count bigint;
unsafe_table_default boolean;
unsafe_sequence_default boolean;
public_connect boolean;
public_temporary boolean;
BEGIN
IF xyz_role = derived_role THEN
  RAISE EXCEPTION
    'Bundled runtime reader and derived owner must be separate roles';
END IF;

SELECT
  EXISTS (
    SELECT 1
    FROM pg_catalog.pg_default_acl AS defaults
    JOIN pg_catalog.pg_roles AS owner
      ON owner.oid = defaults.defaclrole
    LEFT JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = defaults.defaclnamespace
    CROSS JOIN LATERAL
      pg_catalog.aclexplode(defaults.defaclacl) AS privilege
    WHERE owner.rolname = etl_role
      AND defaults.defaclnamespace = 0
      AND defaults.defaclobjtype = 'r'
      AND CASE
        WHEN privilege.grantee = 0 THEN true
        ELSE
          pg_has_role(xyz_role, privilege.grantee, 'MEMBER')
          OR pg_has_role(
            derived_role,
            privilege.grantee,
            'MEMBER'
          )
      END
      AND (
        privilege.privilege_type <> 'SELECT'
        OR privilege.is_grantable
      )
  ),
  EXISTS (
    SELECT 1
    FROM pg_catalog.pg_default_acl AS defaults
    JOIN pg_catalog.pg_roles AS owner
      ON owner.oid = defaults.defaclrole
    LEFT JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = defaults.defaclnamespace
    CROSS JOIN LATERAL
      pg_catalog.aclexplode(defaults.defaclacl) AS privilege
    WHERE owner.rolname = etl_role
      AND defaults.defaclnamespace = 0
      AND defaults.defaclobjtype = 'S'
      AND CASE
        WHEN privilege.grantee = 0 THEN true
        ELSE
          pg_has_role(xyz_role, privilege.grantee, 'MEMBER')
          OR pg_has_role(
            derived_role,
            privilege.grantee,
            'MEMBER'
          )
      END
      AND privilege.privilege_type IN ('USAGE', 'UPDATE')
  )
INTO
  unsafe_table_default,
  unsafe_sequence_default;

IF unsafe_table_default THEN
  RAISE EXCEPTION
    'Runtime reader and derived owner default privileges must be non-grantable SELECT only';
END IF;
IF unsafe_sequence_default THEN
  RAISE EXCEPTION
    'Runtime reader and derived owner defaults must not permit sequence mutation';
END IF;

SELECT
  EXISTS (
    SELECT 1
    FROM pg_catalog.pg_database AS database
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(
        database.datacl,
        pg_catalog.acldefault('d', database.datdba)
      )
    ) AS privilege
    WHERE database.datname = current_database()
      AND privilege.grantee = 0
      AND privilege.privilege_type = 'CONNECT'
  ),
  EXISTS (
    SELECT 1
    FROM pg_catalog.pg_database AS database
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(
        database.datacl,
        pg_catalog.acldefault('d', database.datdba)
      )
    ) AS privilege
    WHERE database.datname = current_database()
      AND privilege.grantee = 0
      AND privilege.privilege_type = 'TEMPORARY'
  )
INTO public_connect, public_temporary;

IF public_connect OR public_temporary THEN
  RAISE EXCEPTION
    'Bundled database CONNECT and TEMPORARY must be revoked from PUBLIC';
END IF;
IF has_database_privilege(
     etl_role,
     current_database(),
     'CONNECT'
   ) IS NOT TRUE
   OR has_database_privilege(
     etl_role,
     current_database(),
     'TEMPORARY'
   ) IS NOT TRUE
   OR has_database_privilege(
     xyz_role,
     current_database(),
     'CONNECT'
   ) IS NOT TRUE
   OR has_database_privilege(
     derived_role,
     current_database(),
     'CONNECT'
   ) IS NOT TRUE
   OR has_database_privilege(
     xyz_role,
     current_database(),
     'TEMPORARY'
   )
   OR has_database_privilege(
     derived_role,
     current_database(),
     'TEMPORARY'
   ) THEN
  RAISE EXCEPTION
    'Bundled database must grant CONNECT+TEMP only to ETL and CONNECT only to runtime/derived roles';
END IF;

END
$$;

SQL

# The census content assertions run against the database that holds the data.
# census_config.py pins TARGET_SCHEMA to leeds and rejects any other value, so
# the schema name is identical on both sides and every assertion below is the
# same SQL it was when it ran against the packaged database -- only the
# container changed. The MAPP-role ownership and grant checks that used to wrap
# these did not move: source relations are owned by the source admin, and the
# roles they named do not exist here.
if [[ -n "${demo_sources}" ]]; then
  "${compose[@]}" exec -T \
    -e "MAPP_VERIFY_CENSUS_GEOMETRY_SHA256=${census_geometry_sha256}" \
    -e "MAPP_VERIFY_CENSUS_TOPIC_HASHES_JSON=${census_topic_hashes_json}" \
    census-db sh -c \
    'exec psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      --set ON_ERROR_STOP=1 \
      --set=census_geometry_sha256="$MAPP_VERIFY_CENSUS_GEOMETRY_SHA256" \
      --set=census_topic_hashes_json="$MAPP_VERIFY_CENSUS_TOPIC_HASHES_JSON"' <<'CENSUS_SQL'
SELECT set_config(
'mapp.verify.census_geometry_sha256',
:'census_geometry_sha256',
false
);
SELECT set_config(
'mapp.verify.census_topic_hashes_json',
:'census_topic_hashes_json',
false
);

DO $$
DECLARE
census_relation regclass := to_regclass(
  'leeds.census_2021_england_oa'
);
row_total bigint;
distinct_code_count bigint;
invalid_code_count bigint;
invalid_geometry_count bigint;
statistic_column_count integer;
invalid_statistic_column_count integer;
variable_metadata_count integer;
variable_topic_count integer;
unmatched_variable_count integer;
dataset_metadata_count integer;
dataset_topic_hash_count integer;
dataset_topic_hash_mismatch_count integer;
matching_dataset_metadata_count integer;
matching_last_run_count integer;
generated_kind text;
expected_geometry_sha256 text :=
  current_setting('mapp.verify.census_geometry_sha256');
expected_topic_hashes jsonb :=
  current_setting('mapp.verify.census_topic_hashes_json')::jsonb;
expected_topic_hash_count integer;
variable_topic_hash_mismatch_count integer;
BEGIN
IF census_relation IS NULL THEN
  RAISE NOTICE 'Optional Census relation leeds.census_2021_england_oa is absent; skipped Census verification';
  RETURN;
END IF;


SELECT
  count(*)::bigint,
  count(DISTINCT oa21cd)::bigint,
  count(*) FILTER (
    WHERE oa21cd IS NULL OR oa21cd !~ '^E[0-9]{8}$'
  )::bigint,
  count(*) FILTER (
    WHERE geom IS NULL
       OR ST_SRID(geom) <> 4326
       OR ST_IsEmpty(geom)
       OR NOT ST_IsValid(geom)
       OR geom_3857 IS NULL
       OR ST_SRID(geom_3857) <> 3857
       OR ST_IsEmpty(geom_3857)
       OR NOT ST_IsValid(geom_3857)
  )::bigint
INTO
  row_total,
  distinct_code_count,
  invalid_code_count,
  invalid_geometry_count
FROM leeds.census_2021_england_oa;

IF row_total <> 178605 THEN
  RAISE EXCEPTION
    'Census relation has % rows, expected 178605',
    row_total;
END IF;
IF invalid_code_count <> 0 THEN
  RAISE EXCEPTION
    'Census relation has % invalid England OA codes',
    invalid_code_count;
END IF;
IF distinct_code_count <> row_total THEN
  RAISE EXCEPTION
    'Census relation has duplicate England OA codes';
END IF;
IF invalid_geometry_count <> 0 THEN
  RAISE EXCEPTION
    'Census relation has % invalid, empty, or incorrectly projected geometries',
    invalid_geometry_count;
END IF;

SELECT attribute.attgenerated::text
INTO generated_kind
FROM pg_catalog.pg_attribute AS attribute
WHERE attribute.attrelid = census_relation
  AND attribute.attname = 'geom_3857'
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped;
IF generated_kind IS DISTINCT FROM 's' THEN
  RAISE EXCEPTION
    'Census geom_3857 must be a stored generated column';
END IF;

SELECT
  count(*)::integer,
  count(*) FILTER (
    WHERE data_type <> 'double precision'
       OR column_name !~ '^ts[0-9]{3}a?_[0-9]{4}$'
  )::integer
INTO statistic_column_count, invalid_statistic_column_count
FROM information_schema.columns
WHERE table_schema = 'leeds'
  AND table_name = 'census_2021_england_oa'
  AND column_name LIKE 'ts%';

IF statistic_column_count <> 467
   OR invalid_statistic_column_count <> 0 THEN
  RAISE EXCEPTION
    'Census statistic column contract failed (columns=%, invalid=%; expected 467 double-precision reviewed names)',
    statistic_column_count,
    invalid_statistic_column_count;
END IF;

SELECT
  count(*)::integer,
  count(DISTINCT topic_id)::integer
INTO variable_metadata_count, variable_topic_count
FROM leeds.census_variables
WHERE dataset_key = 'census_2021_england_oa';

SELECT count(*)::integer
INTO unmatched_variable_count
FROM (
  SELECT column_name
  FROM information_schema.columns
  WHERE table_schema = 'leeds'
    AND table_name = 'census_2021_england_oa'
    AND column_name LIKE 'ts%'
) AS statistic
FULL OUTER JOIN (
  SELECT column_name
  FROM leeds.census_variables
  WHERE dataset_key = 'census_2021_england_oa'
) AS variable
  ON variable.column_name = statistic.column_name
WHERE statistic.column_name IS NULL
   OR variable.column_name IS NULL;

IF variable_metadata_count <> 467
   OR variable_topic_count <> 47
   OR unmatched_variable_count <> 0 THEN
  RAISE EXCEPTION
    'Census variable metadata contract failed (variables=%, topics=%, unmatched=%; expected 467/47/0)',
    variable_metadata_count,
    variable_topic_count,
    unmatched_variable_count;
END IF;

SELECT
  count(*)::integer,
  count(*) FILTER (
    WHERE target_table = 'census_2021_england_oa'
      AND oa_count = 178605
      AND variable_count = 467
      AND geometry_repairs BETWEEN 0 AND 64
      AND geometry_source_url =
        'https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Output_Areas_2021_EW_BGC_V2/FeatureServer/0'
      AND geometry_source_sha256 = expected_geometry_sha256
      AND source_metadata #>> '{geometry,sha256}' =
        expected_geometry_sha256
      AND jsonb_typeof(
        source_metadata #> '{geometry,repairs}'
      ) = 'number'
      AND source_metadata #>> '{geometry,repairs}' =
        geometry_repairs::text
  )::integer
INTO dataset_metadata_count, matching_dataset_metadata_count
FROM leeds.census_datasets
WHERE dataset_key = 'census_2021_england_oa';

IF dataset_metadata_count <> 1
   OR matching_dataset_metadata_count <> 1 THEN
  RAISE EXCEPTION
    'Census dataset metadata contract failed (rows=%, matching=%; expected 1/1)',
    dataset_metadata_count,
    matching_dataset_metadata_count;
END IF;

WITH expected AS (
  SELECT key AS topic_id, value AS source_sha256
  FROM jsonb_each_text(expected_topic_hashes)
),
loaded AS (
  SELECT
    topic_id,
    min(source_sha256) AS source_sha256,
    count(DISTINCT source_sha256) AS distinct_hash_count
  FROM leeds.census_variables
  WHERE dataset_key = 'census_2021_england_oa'
  GROUP BY topic_id
)
SELECT
  (SELECT count(*)::integer FROM expected),
  count(*) FILTER (
    WHERE expected.topic_id IS NULL
       OR loaded.topic_id IS NULL
       OR loaded.distinct_hash_count <> 1
       OR loaded.source_sha256 IS DISTINCT FROM expected.source_sha256
  )::integer
INTO
  expected_topic_hash_count,
  variable_topic_hash_mismatch_count
FROM expected
FULL OUTER JOIN loaded USING (topic_id);

IF expected_topic_hash_count <> 47
   OR variable_topic_hash_mismatch_count <> 0 THEN
  RAISE EXCEPTION
    'Census variable source hashes do not match the 47 pinned topic archives (expected_topics=%, mismatches=%)',
    expected_topic_hash_count,
    variable_topic_hash_mismatch_count;
END IF;

WITH expected AS (
  SELECT key AS topic_id, value AS source_sha256
  FROM jsonb_each_text(expected_topic_hashes)
),
loaded AS (
  SELECT
    topic.value ->> 'topic_id' AS topic_id,
    min(topic.value ->> 'archive_sha256') AS source_sha256,
    count(*) AS entry_count,
    count(
      DISTINCT topic.value ->> 'archive_sha256'
    ) AS distinct_hash_count
  FROM leeds.census_datasets AS dataset
  CROSS JOIN LATERAL jsonb_array_elements(
    dataset.source_metadata -> 'topics'
  ) WITH ORDINALITY AS topic(value, ordinal)
  WHERE dataset.dataset_key = 'census_2021_england_oa'
  GROUP BY topic.value ->> 'topic_id'
)
SELECT
  (SELECT sum(entry_count)::integer FROM loaded),
  count(*) FILTER (
    WHERE expected.topic_id IS NULL
       OR loaded.topic_id IS NULL
       OR loaded.entry_count <> 1
       OR loaded.distinct_hash_count <> 1
       OR loaded.source_sha256 IS DISTINCT FROM expected.source_sha256
  )::integer
INTO dataset_topic_hash_count, dataset_topic_hash_mismatch_count
FROM expected
FULL OUTER JOIN loaded USING (topic_id);

IF dataset_topic_hash_count <> 47
   OR dataset_topic_hash_mismatch_count <> 0 THEN
  RAISE EXCEPTION
    'Census dataset source metadata does not match the 47 pinned topic archive hashes (topics=%, mismatches=%)',
    dataset_topic_hash_count,
    dataset_topic_hash_mismatch_count;
END IF;

SELECT count(*)::integer
INTO matching_last_run_count
FROM leeds.census_datasets AS dataset
JOIN leeds._census_etl_runs AS run
  ON run.run_id = dataset.last_successful_run_id
WHERE dataset.dataset_key = 'census_2021_england_oa'
  AND run.dataset_key = dataset.dataset_key
  AND run.target_table = dataset.target_table
  AND run.status = 'succeeded'
  AND run.finished_at IS NOT NULL
  AND run.geometry_rows = 178605
  AND run.geometry_repairs = dataset.geometry_repairs
  AND run.topics_loaded = 47
  AND run.error IS NULL;

IF matching_last_run_count <> 1 THEN
  RAISE EXCEPTION
    'Census last-successful-run contract failed; expected one succeeded 178605-row, 47-topic run';
END IF;

RAISE NOTICE
  'leeds.census_2021_england_oa: 178605 rows, 467 variables, and 47 topics verified';
END
$$;
CENSUS_SQL
fi

# The sample-layer assertions run against the database that holds those layers.
# Both blocks are portable as they stand: they name only leeds relations and
# information_schema, never a MAPP role, so nothing had to be dropped to move
# them.
if [[ -n "${demo_sources}" ]]; then
  "${compose[@]}" exec -T ops-db sh -c \
    'exec psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      --set ON_ERROR_STOP=1' <<'OPS_SQL'
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
OPS_SQL
fi

"${compose[@]}" exec -T xyz node --input-type=module -e '
  import pg from "pg";

  const fail = (message) => {
    throw new Error(message);
  };
  let uriUser;
  try {
    const parsed = new URL(process.env.DBS_MAPP);
    if (!["postgres:", "postgresql:"].includes(parsed.protocol)) {
      fail("DBS_MAPP is not a PostgreSQL URI.");
    }
    uriUser = decodeURIComponent(parsed.username);
  } catch {
    fail("DBS_MAPP does not contain a valid encoded login identity.");
  }
  if (!uriUser) {
    fail("DBS_MAPP must contain an explicit login identity.");
  }

  const pool = new pg.Pool({
    connectionString: process.env.DBS_MAPP,
    connectionTimeoutMillis: 10000,
    query_timeout: 15000,
    statement_timeout: 15000,
  });

  try {
    const result = await pool.query(`
      SELECT
        current_user::text AS "currentUser",
        session_user::text AS "sessionUser",
        PostGIS_Version() AS postgis,
        login_role.rolcanlogin AS "canLogin",
        login_role.rolsuper AS superuser,
        login_role.rolcreatedb AS "canCreateDatabase",
        login_role.rolcreaterole AS "canCreateRole",
        login_role.rolreplication AS replication,
        login_role.rolbypassrls AS "bypassesRls",
        (
          SELECT database.datdba = login_role.oid
          FROM pg_catalog.pg_database AS database
          WHERE database.datname = current_database()
        ) AS "ownsDatabase",
        has_database_privilege(
          current_database(),
          $$TEMPORARY$$
        ) AS "hasTemporary",
        has_database_privilege(
          current_database(),
          $$CREATE$$
        ) AS "canCreateDatabaseObject",
        EXISTS (
          SELECT 1
          FROM pg_catalog.pg_foreign_data_wrapper AS wrapper
          WHERE wrapper.fdwname = $$postgres_fdw$$
            AND has_foreign_data_wrapper_privilege(
              current_user, wrapper.oid, $$USAGE$$
            )
        ) AS "canUseFdw",
        EXISTS (
          SELECT 1
          FROM pg_catalog.pg_database AS database
          CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
              database.datacl,
              pg_catalog.acldefault($$d$$, database.datdba)
            )
          ) AS privilege
          WHERE database.datname = current_database()
            AND privilege.grantee = 0
            AND privilege.privilege_type IN (
              $$CONNECT$$,
              $$TEMPORARY$$
            )
        ) AS "hasPublicDatabasePrivilege",
        EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          WHERE namespace.nspname !~ $$^pg_$$
            AND namespace.nspname <> $$information_schema$$
            AND has_schema_privilege(namespace.oid, $$CREATE$$)
        ) AS "canCreateSchema",
        EXISTS (
          SELECT 1
          FROM pg_catalog.pg_class AS relation
          JOIN pg_catalog.pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          WHERE namespace.nspname !~ $$^pg_$$
            AND namespace.nspname <> $$information_schema$$
            AND relation.relkind IN ($$r$$, $$p$$, $$v$$, $$m$$, $$f$$)
            AND (
              has_table_privilege(relation.oid, $$INSERT$$)
              OR has_table_privilege(relation.oid, $$UPDATE$$)
              OR has_table_privilege(relation.oid, $$DELETE$$)
              OR has_table_privilege(relation.oid, $$TRUNCATE$$)
              OR has_table_privilege(relation.oid, $$REFERENCES$$)
              OR has_table_privilege(relation.oid, $$TRIGGER$$)
              OR has_any_column_privilege(relation.oid, $$INSERT$$)
              OR has_any_column_privilege(relation.oid, $$UPDATE$$)
              OR has_any_column_privilege(relation.oid, $$REFERENCES$$)
            )
        ) AS "hasUnsafeRelationPrivilege",
        EXISTS (
          SELECT 1
          FROM pg_catalog.pg_class AS relation
          JOIN pg_catalog.pg_namespace AS namespace
            ON namespace.oid = relation.relnamespace
          WHERE namespace.nspname !~ $$^pg_$$
            AND namespace.nspname <> $$information_schema$$
            AND relation.relkind = $$S$$
            AND (
              has_sequence_privilege(relation.oid, $$USAGE$$)
              OR has_sequence_privilege(relation.oid, $$UPDATE$$)
            )
        ) AS "hasUnsafeSequencePrivilege",
        EXISTS (
          SELECT 1
          FROM pg_catalog.pg_roles AS reachable_role
          WHERE reachable_role.oid <> login_role.oid
            AND pg_has_role(
              current_user,
              reachable_role.oid,
              $$MEMBER$$
            )
            AND (
              reachable_role.rolsuper
              OR reachable_role.rolcreatedb
              OR reachable_role.rolcreaterole
              OR reachable_role.rolreplication
              OR reachable_role.rolbypassrls
              OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_database AS database
                WHERE database.datname = current_database()
                  AND database.datdba = reachable_role.oid
              )
              OR has_database_privilege(
                reachable_role.oid,
                current_database(),
                $$TEMPORARY$$
              )
              OR has_database_privilege(
                reachable_role.oid,
                current_database(),
                $$CREATE$$
              )
              OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_foreign_data_wrapper AS wrapper
                WHERE wrapper.fdwname = $$postgres_fdw$$
                  AND has_foreign_data_wrapper_privilege(
                    reachable_role.oid, wrapper.oid, $$USAGE$$
                  )
              )
              OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname !~ $$^pg_$$
                  AND namespace.nspname <> $$information_schema$$
                  AND has_schema_privilege(
                    reachable_role.oid,
                    namespace.oid,
                    $$CREATE$$
                  )
              )
              OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname !~ $$^pg_$$
                  AND namespace.nspname <> $$information_schema$$
                  AND relation.relkind IN (
                    $$r$$,
                    $$p$$,
                    $$v$$,
                    $$m$$,
                    $$f$$
                  )
                  AND (
                    has_table_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$INSERT$$
                    )
                    OR has_table_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$UPDATE$$
                    )
                    OR has_table_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$DELETE$$
                    )
                    OR has_table_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$TRUNCATE$$
                    )
                    OR has_table_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$REFERENCES$$
                    )
                    OR has_table_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$TRIGGER$$
                    )
                    OR has_any_column_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$INSERT$$
                    )
                    OR has_any_column_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$UPDATE$$
                    )
                    OR has_any_column_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$REFERENCES$$
                    )
                  )
              )
              OR EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname !~ $$^pg_$$
                  AND namespace.nspname <> $$information_schema$$
                  AND relation.relkind = $$S$$
                  AND (
                    has_sequence_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$USAGE$$
                    )
                    OR has_sequence_privilege(
                      reachable_role.oid,
                      relation.oid,
                      $$UPDATE$$
                    )
                  )
              )
            )
        ) AS "hasUnsafeMembership"
      FROM pg_catalog.pg_roles AS login_role
      WHERE login_role.rolname = current_user
    `);
    const audit = result.rows[0];
    const declaredReader = process.argv[1];
    const bundledReader = process.argv[2];
    if (
      !audit
      || !audit.postgis
      || audit.currentUser !== audit.sessionUser
      || audit.currentUser !== uriUser
      || (declaredReader && audit.currentUser !== declaredReader)
      || audit.currentUser !== bundledReader
      || !audit.canLogin
      || audit.superuser
      || audit.canCreateDatabase
      || audit.canCreateRole
      || audit.replication
      || audit.bypassesRls
      || audit.ownsDatabase
      || audit.hasTemporary
      || audit.canCreateDatabaseObject
      || audit.canUseFdw
      || audit.hasPublicDatabasePrivilege
      || audit.canCreateSchema
      || audit.hasUnsafeRelationPrivilege
      || audit.hasUnsafeSequencePrivilege
      || audit.hasUnsafeMembership
    ) {
      fail("The active DBS_MAPP session is not the required read-only runtime identity.");
    }

  } finally {
    await pool.end();
  }
' "${resolved_derived_reader}" "$(dotenv_value XYZ_DB_USER)"

"${compose[@]}" exec -T config-ui python -c '
import os
import sys
from urllib.parse import unquote_to_bytes, urlsplit

import psycopg
from psycopg.rows import dict_row


def fail(message):
    raise SystemExit(message)


def resource_policy_valid(session, limits):
    if not session:
        return False
    for key, (minimum, maximum) in limits.items():
        if key == "transactionTimeoutMs" and session["serverVersionNum"] < 170000:
            continue
        value = session.get(key)
        if value is None or value < minimum or value > maximum:
            return False
    if session["serverVersionNum"] >= 170000:
        transaction_timeout = session.get("transactionTimeoutMs")
        if (
            transaction_timeout is None
            or transaction_timeout < 1
            or transaction_timeout > limits["transactionTimeoutMs"][1]
        ):
            return False
    return True


reader_resource_limits = {
    "connectionLimit": (1, 32),
    "workMemKb": (1, 8 * 1024),
    "hashMemMultiplier": (1, 1),
    "maintenanceWorkMemKb": (1, 32 * 1024),
    "maxParallelWorkers": (0, 1),
    "tempFileLimitKb": (0, 256 * 1024),
    "statementTimeoutMs": (1, 15 * 1000),
    "transactionTimeoutMs": (1, 30 * 1000),
    "lockTimeoutMs": (1, 5 * 1000),
    "idleTransactionTimeoutMs": (1, 30 * 1000),
}
derived_resource_limits = {
    "connectionLimit": (1, 4),
    "workMemKb": (1, 16 * 1024),
    "hashMemMultiplier": (1, 1),
    "maintenanceWorkMemKb": (1, 64 * 1024),
    "maxParallelWorkers": (0, 2),
    "tempFileLimitKb": (0, 1024 * 1024),
    "statementTimeoutMs": (1, 30 * 60 * 1000),
    "transactionTimeoutMs": (1, 35 * 60 * 1000),
    "lockTimeoutMs": (1, 5 * 1000),
    "idleTransactionTimeoutMs": (1, 60 * 1000),
}


bundled_derived_role = sys.argv[1]
database_url = os.environ.get("DERIVED_DATABASE_URL", "")
derived_role = os.environ.get("DERIVED_OWNER_ROLE", "")
reader_role = os.environ.get("DERIVED_READER_ROLE", "")
federation_database_url = os.environ.get("FEDERATION_DATABASE_URL", "")
federation_role = os.environ.get("FEDERATION_DB_USER", "")

if not database_url:
    if derived_role or reader_role:
        fail(
            "DERIVED_OWNER_ROLE and DERIVED_READER_ROLE must be empty when "
            "derived database management is disabled."
        )
    print("Derived database management is disabled and internally consistent.")
    raise SystemExit(0)
if not derived_role or not reader_role:
    fail(
        "DERIVED_OWNER_ROLE and DERIVED_READER_ROLE are required with "
        "DERIVED_DATABASE_URL."
    )
if bool(federation_database_url) != bool(federation_role):
    fail(
        "FEDERATION_DATABASE_URL and FEDERATION_DB_USER must either both be "
        "configured or both be empty."
    )
if not federation_database_url:
    fail("A local database requires FEDERATION_DATABASE_URL.")

try:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.username is None:
        fail("DERIVED_DATABASE_URL must contain an explicit PostgreSQL login.")
    uri_user = unquote_to_bytes(parsed.username).decode("utf-8")
except (UnicodeDecodeError, ValueError):
    fail("DERIVED_DATABASE_URL contains an invalid encoded login identity.")
if uri_user != derived_role:
    fail("DERIVED_DATABASE_URL login must match DERIVED_OWNER_ROLE.")
federation_uri_user = None
if federation_database_url:
    try:
        parsed_federation = urlsplit(federation_database_url)
        if (
            parsed_federation.scheme not in {"postgres", "postgresql"}
            or parsed_federation.username is None
        ):
            fail(
                "FEDERATION_DATABASE_URL must contain an explicit "
                "PostgreSQL login."
            )
        federation_uri_user = unquote_to_bytes(
            parsed_federation.username
        ).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        fail(
            "FEDERATION_DATABASE_URL contains an invalid encoded login "
            "identity."
        )
    if federation_uri_user != federation_role:
        fail("FEDERATION_DATABASE_URL login must match FEDERATION_DB_USER.")

with psycopg.connect(
    os.environ["DBS_MAPP"],
    connect_timeout=10,
    row_factory=dict_row,
) as reader_connection:
    with reader_connection.cursor() as reader_cursor:
        reader_cursor.execute("""
            SELECT
              current_database() AS database_name,
              current_user::text AS current_user,
              session_user::text AS session_user,
              reader_role.rolconnlimit AS "connectionLimit",
              current_setting($$server_version_num$$)::integer
                AS "serverVersionNum",
              settings.work_mem_kb AS "workMemKb",
              settings.hash_mem_multiplier AS "hashMemMultiplier",
              settings.maintenance_work_mem_kb AS "maintenanceWorkMemKb",
              settings.max_parallel_workers AS "maxParallelWorkers",
              settings.temp_file_limit_kb AS "tempFileLimitKb",
              settings.statement_timeout_ms AS "statementTimeoutMs",
              settings.transaction_timeout_ms AS "transactionTimeoutMs",
              settings.lock_timeout_ms AS "lockTimeoutMs",
              settings.idle_transaction_timeout_ms
                AS "idleTransactionTimeoutMs"
            FROM pg_catalog.pg_roles AS reader_role
            CROSS JOIN LATERAL (
              SELECT
                max(setting::numeric) FILTER (WHERE name = $$work_mem$$)
                  AS work_mem_kb,
                max(setting::numeric) FILTER (
                  WHERE name = $$hash_mem_multiplier$$
                ) AS hash_mem_multiplier,
                max(setting::numeric) FILTER (
                  WHERE name = $$maintenance_work_mem$$
                ) AS maintenance_work_mem_kb,
                max(setting::numeric) FILTER (
                  WHERE name = $$max_parallel_workers_per_gather$$
                ) AS max_parallel_workers,
                max(setting::numeric) FILTER (
                  WHERE name = $$temp_file_limit$$
                ) AS temp_file_limit_kb,
                max(setting::numeric) FILTER (
                  WHERE name = $$statement_timeout$$
                ) AS statement_timeout_ms,
                max(setting::numeric) FILTER (
                  WHERE name = $$transaction_timeout$$
                ) AS transaction_timeout_ms,
                max(setting::numeric) FILTER (WHERE name = $$lock_timeout$$)
                  AS lock_timeout_ms,
                max(setting::numeric) FILTER (
                  WHERE name = $$idle_in_transaction_session_timeout$$
                ) AS idle_transaction_timeout_ms
              FROM pg_catalog.pg_settings
              WHERE name IN (
                $$work_mem$$, $$hash_mem_multiplier$$,
                $$maintenance_work_mem$$,
                $$max_parallel_workers_per_gather$$, $$temp_file_limit$$,
                $$statement_timeout$$, $$transaction_timeout$$,
                $$lock_timeout$$, $$idle_in_transaction_session_timeout$$
              )
            ) AS settings
            WHERE reader_role.rolname = current_user
        """)
        reader_session = reader_cursor.fetchone()
if (
    not reader_session
    or reader_session["current_user"] != reader_session["session_user"]
    or reader_session["current_user"] != reader_role
):
    fail(
        "The configuration service DBS_MAPP session does not match "
        "DERIVED_READER_ROLE."
    )
if not resource_policy_valid(reader_session, reader_resource_limits):
    fail(
        "The active DBS_MAPP runtime reader does not enforce the required "
        "connection, memory, temporary-file, parallelism, and timeout limits."
    )

with psycopg.connect(
    database_url,
    connect_timeout=10,
    row_factory=dict_row,
) as connection:
    with connection.cursor() as cursor:
        audit_sql = """
            SELECT
              current_database() AS "databaseName",
              current_user::text AS "currentUser",
              session_user::text AS "sessionUser",
              PostGIS_Version() AS postgis,
              login_role.rolcanlogin AS "canLogin",
              login_role.rolsuper AS superuser,
              login_role.rolcreatedb AS "canCreateDatabase",
              login_role.rolcreaterole AS "canCreateRole",
              login_role.rolreplication AS replication,
              login_role.rolbypassrls AS "bypassesRls",
              login_role.rolconnlimit AS "connectionLimit",
              current_setting($$search_path$$) AS "searchPath",
              current_setting($$server_version_num$$)::integer
                AS "serverVersionNum",
              settings.work_mem_kb AS "workMemKb",
              settings.hash_mem_multiplier AS "hashMemMultiplier",
              settings.maintenance_work_mem_kb AS "maintenanceWorkMemKb",
              settings.max_parallel_workers AS "maxParallelWorkers",
              settings.temp_file_limit_kb AS "tempFileLimitKb",
              settings.statement_timeout_ms AS "statementTimeoutMs",
              settings.transaction_timeout_ms AS "transactionTimeoutMs",
              settings.lock_timeout_ms AS "lockTimeoutMs",
              settings.idle_transaction_timeout_ms
                AS "idleTransactionTimeoutMs",
              (
                SELECT database.datdba = login_role.oid
                FROM pg_catalog.pg_database AS database
                WHERE database.datname = current_database()
              ) AS "ownsDatabase",
              has_database_privilege(
                current_database(),
                $$TEMPORARY$$
              ) AS "hasTemporary",
              has_database_privilege(
                current_database(),
                $$CREATE$$
              ) AS "canCreateSchema",
              EXISTS (
                SELECT 1
                FROM pg_catalog.pg_foreign_data_wrapper AS wrapper
                WHERE wrapper.fdwname = $$postgres_fdw$$
                  AND has_foreign_data_wrapper_privilege(
                    current_user, wrapper.oid, $$USAGE$$
                  )
              ) AS "canUseFdw",
              EXISTS (
                SELECT 1
                FROM pg_catalog.pg_database AS database
                CROSS JOIN LATERAL pg_catalog.aclexplode(
                  COALESCE(
                    database.datacl,
                    pg_catalog.acldefault(
                      $$d$$,
                      database.datdba
                    )
                  )
                ) AS privilege
                WHERE database.datname = current_database()
                  AND privilege.grantee = 0
                  AND privilege.privilege_type IN (
                    $$CONNECT$$,
                    $$TEMPORARY$$
                  )
              ) AS "hasPublicDatabasePrivilege",
              (
                SELECT namespace.nspowner = login_role.oid
                FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname = $$derived_layers$$
              ) AS "ownsDerivedSchema",
              has_schema_privilege(
                $$derived_layers$$,
                $$CREATE$$
              ) AS "canCreateDerived",
              -- The derived owner may create objects only in its own schema.
              -- Federation control/source objects have a separate owner.
              EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname !~ $$^pg_$$
                  AND namespace.nspname <> $$information_schema$$
                  AND NOT (
                    namespace.nspowner = login_role.oid
                    AND namespace.nspname = $$derived_layers$$
                  )
                  AND has_schema_privilege(namespace.oid, $$CREATE$$)
              ) AS "canCreateBaseSchema",
              EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname !~ $$^pg_$$
                  AND namespace.nspname <> $$information_schema$$
                  AND NOT (
                    namespace.nspowner = login_role.oid
                    AND namespace.nspname = $$derived_layers$$
                  )
                  AND relation.relkind IN (
                    $$r$$,
                    $$p$$,
                    $$v$$,
                    $$m$$,
                    $$f$$
                  )
                  AND (
                    has_table_privilege(relation.oid, $$INSERT$$)
                    OR has_table_privilege(relation.oid, $$UPDATE$$)
                    OR has_table_privilege(relation.oid, $$DELETE$$)
                    OR has_table_privilege(relation.oid, $$TRUNCATE$$)
                    OR has_table_privilege(relation.oid, $$REFERENCES$$)
                    OR has_table_privilege(relation.oid, $$TRIGGER$$)
                    OR has_any_column_privilege(
                      relation.oid,
                      $$INSERT$$
                    )
                    OR has_any_column_privilege(
                      relation.oid,
                      $$UPDATE$$
                    )
                    OR has_any_column_privilege(
                      relation.oid,
                      $$REFERENCES$$
                    )
                  )
              ) AS "hasUnsafeBaseRelationPrivilege",
              EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname !~ $$^pg_$$
                  AND namespace.nspname <> $$information_schema$$
                  AND NOT (
                    namespace.nspowner = login_role.oid
                    AND namespace.nspname = $$derived_layers$$
                  )
                  AND relation.relkind = $$S$$
                  AND (
                    has_sequence_privilege(relation.oid, $$USAGE$$)
                    OR has_sequence_privilege(relation.oid, $$UPDATE$$)
                  )
              ) AS "hasUnsafeBaseSequencePrivilege",
              EXISTS (
                SELECT 1
                FROM pg_catalog.pg_roles AS reachable_role
                WHERE reachable_role.oid <> login_role.oid
                  AND pg_has_role(
                    current_user,
                    reachable_role.oid,
                    $$MEMBER$$
                  )
                  AND (
                    reachable_role.rolsuper
                    OR reachable_role.rolcreatedb
                    OR reachable_role.rolcreaterole
                    OR reachable_role.rolreplication
                    OR reachable_role.rolbypassrls
                    OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_database AS database
                      WHERE database.datname = current_database()
                        AND database.datdba = reachable_role.oid
                    )
                    OR has_database_privilege(
                      reachable_role.oid,
                      current_database(),
                      $$TEMPORARY$$
                    )
                    OR has_database_privilege(
                      reachable_role.oid,
                      current_database(),
                      $$CREATE$$
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_foreign_data_wrapper AS wrapper
                      WHERE wrapper.fdwname = $$postgres_fdw$$
                        AND has_foreign_data_wrapper_privilege(
                          reachable_role.oid, wrapper.oid, $$USAGE$$
                        )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_namespace AS namespace
                      WHERE namespace.nspname !~ $$^pg_$$
                        AND namespace.nspname <> $$information_schema$$
                        AND NOT (
                          namespace.nspowner = reachable_role.oid
                          AND namespace.nspname = $$derived_layers$$
                        )
                        AND has_schema_privilege(
                          reachable_role.oid,
                          namespace.oid,
                          $$CREATE$$
                        )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_class AS relation
                      JOIN pg_catalog.pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                      WHERE namespace.nspname !~ $$^pg_$$
                        AND namespace.nspname <> $$information_schema$$
                        AND NOT (
                          namespace.nspowner = reachable_role.oid
                          AND namespace.nspname = $$derived_layers$$
                        )
                        AND relation.relkind IN (
                          $$r$$,
                          $$p$$,
                          $$v$$,
                          $$m$$,
                          $$f$$
                        )
                        AND (
                          has_table_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$INSERT$$
                          )
                          OR has_table_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$UPDATE$$
                          )
                          OR has_table_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$DELETE$$
                          )
                          OR has_table_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$TRUNCATE$$
                          )
                          OR has_table_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$REFERENCES$$
                          )
                          OR has_table_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$TRIGGER$$
                          )
                          OR has_any_column_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$INSERT$$
                          )
                          OR has_any_column_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$UPDATE$$
                          )
                          OR has_any_column_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$REFERENCES$$
                          )
                        )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_class AS relation
                      JOIN pg_catalog.pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                      WHERE namespace.nspname !~ $$^pg_$$
                        AND namespace.nspname <> $$information_schema$$
                        AND NOT (
                          namespace.nspowner = reachable_role.oid
                          AND namespace.nspname = $$derived_layers$$
                        )
                        AND relation.relkind = $$S$$
                        AND (
                          has_sequence_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$USAGE$$
                          )
                          OR has_sequence_privilege(
                            reachable_role.oid,
                            relation.oid,
                            $$UPDATE$$
                          )
                        )
                    )
                  )
              ) AS "hasUnsafeMembership"
            FROM pg_catalog.pg_roles AS login_role
            CROSS JOIN LATERAL (
              SELECT
                max(setting::numeric) FILTER (WHERE name = $$work_mem$$)
                  AS work_mem_kb,
                max(setting::numeric) FILTER (
                  WHERE name = $$hash_mem_multiplier$$
                ) AS hash_mem_multiplier,
                max(setting::numeric) FILTER (
                  WHERE name = $$maintenance_work_mem$$
                ) AS maintenance_work_mem_kb,
                max(setting::numeric) FILTER (
                  WHERE name = $$max_parallel_workers_per_gather$$
                ) AS max_parallel_workers,
                max(setting::numeric) FILTER (
                  WHERE name = $$temp_file_limit$$
                ) AS temp_file_limit_kb,
                max(setting::numeric) FILTER (
                  WHERE name = $$statement_timeout$$
                ) AS statement_timeout_ms,
                max(setting::numeric) FILTER (
                  WHERE name = $$transaction_timeout$$
                ) AS transaction_timeout_ms,
                max(setting::numeric) FILTER (WHERE name = $$lock_timeout$$)
                  AS lock_timeout_ms,
                max(setting::numeric) FILTER (
                  WHERE name = $$idle_in_transaction_session_timeout$$
                ) AS idle_transaction_timeout_ms
              FROM pg_catalog.pg_settings
              WHERE name IN (
                $$work_mem$$, $$hash_mem_multiplier$$,
                $$maintenance_work_mem$$,
                $$max_parallel_workers_per_gather$$, $$temp_file_limit$$,
                $$statement_timeout$$, $$transaction_timeout$$,
                $$lock_timeout$$, $$idle_in_transaction_session_timeout$$
              )
            ) AS settings
            WHERE login_role.rolname = current_user
        """
        cursor.execute(audit_sql)
        audit = cursor.fetchone()
        if audit and audit["searchPath"] != "pg_catalog, public":
            fail(
                "The active DERIVED_DATABASE_URL owner search_path must be "
                "exactly pg_catalog, public."
            )
        # The databaseName comparison below is the generalized invariant from
        # docs/federation-architecture-waypoint.md, "Relationship to the
        # current single-database contract": the derived owner must live in
        # the database the effective dbs alias of the workspace resolves to.
        # compose.yaml forwards DBS_MAPP by explicit enumeration, so that
        # alias can only resolve to this reader connection, and federated mode
        # repoints reader and derived owner in the same step by design. Do not
        # relax it when federated mode gains its own database.
        if (
            not audit
            or not audit["postgis"]
            or audit["databaseName"] != reader_session["database_name"]
            or audit["currentUser"] != audit["sessionUser"]
            or audit["currentUser"] != uri_user
            or audit["currentUser"] != derived_role
            or audit["currentUser"] == reader_role
            or audit["currentUser"] != bundled_derived_role
            or not audit["canLogin"]
            or audit["superuser"]
            or audit["canCreateDatabase"]
            or audit["canCreateRole"]
            or audit["replication"]
            or audit["bypassesRls"]
            or audit["ownsDatabase"]
            or audit["hasTemporary"]
            or audit["canCreateSchema"]
            or audit["canUseFdw"]
            or audit["hasPublicDatabasePrivilege"]
            or not audit["ownsDerivedSchema"]
            or not audit["canCreateDerived"]
            or audit["canCreateBaseSchema"]
            or audit["hasUnsafeBaseRelationPrivilege"]
            or audit["hasUnsafeBaseSequencePrivilege"]
            or audit["hasUnsafeMembership"]
        ):
            fail(
                "The active DERIVED_DATABASE_URL session is not the required "
                "least-privilege derived owner."
            )
        if not resource_policy_valid(audit, derived_resource_limits):
            fail(
                "The active DERIVED_DATABASE_URL owner does not enforce the "
                "required connection, memory, temporary-file, parallelism, "
                "and timeout limits."
            )

if federation_database_url:
    with psycopg.connect(
        federation_database_url,
        connect_timeout=10,
        row_factory=dict_row,
    ) as federation_connection:
        with federation_connection.cursor() as cursor:
            cursor.execute("""
                SELECT
                  current_database() AS "databaseName",
                  current_user::text AS "currentUser",
                  session_user::text AS "sessionUser",
                  role.rolcanlogin AS "canLogin",
                  role.rolsuper AS superuser,
                  role.rolcreatedb AS "canCreateDatabase",
                  role.rolcreaterole AS "canCreateRole",
                  role.rolreplication AS replication,
                  role.rolbypassrls AS "bypassesRls",
                  role.rolconnlimit AS "connectionLimit",
                  current_setting($$search_path$$) AS "searchPath",
                  current_setting($$server_version_num$$)::integer
                    AS "serverVersionNum",
                  settings.work_mem_kb AS "workMemKb",
                  settings.hash_mem_multiplier AS "hashMemMultiplier",
                  settings.maintenance_work_mem_kb AS "maintenanceWorkMemKb",
                  settings.max_parallel_workers AS "maxParallelWorkers",
                  settings.temp_file_limit_kb AS "tempFileLimitKb",
                  settings.statement_timeout_ms AS "statementTimeoutMs",
                  settings.transaction_timeout_ms AS "transactionTimeoutMs",
                  settings.lock_timeout_ms AS "lockTimeoutMs",
                  settings.idle_transaction_timeout_ms
                    AS "idleTransactionTimeoutMs",
                  has_database_privilege(current_database(), $$CREATE$$)
                    AS "canCreateSchema",
                  has_database_privilege(current_database(), $$TEMPORARY$$)
                    AS "hasTemporary",
                  has_foreign_data_wrapper_privilege(
                    current_user, $$postgres_fdw$$, $$USAGE$$
                  ) AS "canUseFdw",
                  (
                    SELECT namespace.nspowner = role.oid
                    FROM pg_catalog.pg_namespace AS namespace
                    WHERE namespace.nspname = $$federation$$
                  ) AS "ownsFederationSchema",
                  -- to_regnamespace yields NULL rather than raising for an
                  -- absent schema. The bundled database creates federation at
                  -- init; an external host federating for the first time may
                  -- legitimately not have it yet, and a bare
                  -- has_schema_privilege($$federation$$, ...) would abort the
                  -- whole audit with invalid_schema_name rather than report it.
                  (pg_catalog.to_regnamespace($$federation$$) IS NOT NULL)
                    AS "federationSchemaPresent",
                  COALESCE(
                    has_schema_privilege(
                      pg_catalog.to_regnamespace($$federation$$), $$CREATE$$
                    ),
                    false
                  ) AS "canCreateFederation",
                  has_schema_privilege(
                    $$derived_layers$$, $$USAGE,CREATE$$
                  ) AS "hasDerivedSchemaPrivilege",
                  EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS derived_object
                    JOIN pg_catalog.pg_namespace AS derived_namespace
                      ON derived_namespace.oid = derived_object.relnamespace
                    WHERE derived_namespace.nspname = $$derived_layers$$
                      AND CASE
                        WHEN derived_object.relkind = $$S$$
                        THEN has_sequence_privilege(
                          current_user,
                          derived_object.oid,
                          $$USAGE,SELECT,UPDATE$$
                        )
                        WHEN derived_object.relkind IN (
                          $$r$$, $$p$$, $$v$$, $$m$$, $$f$$
                        ) THEN
                          has_table_privilege(
                            current_user,
                            derived_object.oid,
                            $$SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER$$
                          )
                          OR has_any_column_privilege(
                            current_user,
                            derived_object.oid,
                            $$SELECT,INSERT,UPDATE,REFERENCES$$
                          )
                        ELSE false
                      END
                  ) AS "hasDerivedObjectPrivilege",
                  EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS relation
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = $$federation$$
                      AND relation.relowner <> role.oid
                  ) AS "hasUnownedRegistryObject",
                  EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_auth_members AS membership
                    WHERE membership.member = role.oid
                       OR membership.roleid = role.oid
                  ) AS "hasMembership"
                FROM pg_catalog.pg_roles AS role
                CROSS JOIN LATERAL (
                  SELECT
                    max(setting::numeric) FILTER (WHERE name = $$work_mem$$)
                      AS work_mem_kb,
                    max(setting::numeric) FILTER (
                      WHERE name = $$hash_mem_multiplier$$
                    ) AS hash_mem_multiplier,
                    max(setting::numeric) FILTER (
                      WHERE name = $$maintenance_work_mem$$
                    ) AS maintenance_work_mem_kb,
                    max(setting::numeric) FILTER (
                      WHERE name = $$max_parallel_workers_per_gather$$
                    ) AS max_parallel_workers,
                    max(setting::numeric) FILTER (
                      WHERE name = $$temp_file_limit$$
                    ) AS temp_file_limit_kb,
                    max(setting::numeric) FILTER (
                      WHERE name = $$statement_timeout$$
                    ) AS statement_timeout_ms,
                    max(setting::numeric) FILTER (
                      WHERE name = $$transaction_timeout$$
                    ) AS transaction_timeout_ms,
                    max(setting::numeric) FILTER (WHERE name = $$lock_timeout$$)
                      AS lock_timeout_ms,
                    max(setting::numeric) FILTER (
                      WHERE name = $$idle_in_transaction_session_timeout$$
                    ) AS idle_transaction_timeout_ms
                  FROM pg_catalog.pg_settings
                  WHERE name IN (
                    $$work_mem$$, $$hash_mem_multiplier$$,
                    $$maintenance_work_mem$$,
                    $$max_parallel_workers_per_gather$$, $$temp_file_limit$$,
                    $$statement_timeout$$, $$transaction_timeout$$,
                    $$lock_timeout$$, $$idle_in_transaction_session_timeout$$
                  )
                ) AS settings
                WHERE role.rolname = current_user
            """)
            federation_audit = cursor.fetchone()
            # Reported separately from the privilege check below because it is
            # a different instruction: create the schema, not fix a grant.
            if federation_audit and not federation_audit["federationSchemaPresent"]:
                fail(
                    "The federation registry schema does not exist on the "
                    "host database. The bundled database creates it at init; "
                    "docker/postgis/init/10-roles.sh creates it, so a "
                    "database predating that script needs "
                    "CREATE SCHEMA federation AUTHORIZATION <FEDERATION_DB_USER>."
                )
            # Same invariant as the derived owner above, for the same reason.
            if (
                not federation_audit
                or federation_audit["databaseName"]
                != reader_session["database_name"]
                or federation_audit["currentUser"]
                != federation_audit["sessionUser"]
                or federation_audit["currentUser"] != federation_uri_user
                or federation_audit["currentUser"] != federation_role
                or federation_audit["currentUser"] in {derived_role, reader_role}
                or not federation_audit["canLogin"]
                or federation_audit["superuser"]
                or federation_audit["canCreateDatabase"]
                or federation_audit["canCreateRole"]
                or federation_audit["replication"]
                or federation_audit["bypassesRls"]
                or federation_audit["hasTemporary"]
                or not federation_audit["canCreateSchema"]
                or not federation_audit["canUseFdw"]
                or not federation_audit["ownsFederationSchema"]
                or not federation_audit["canCreateFederation"]
                or federation_audit["hasDerivedSchemaPrivilege"]
                or federation_audit["hasDerivedObjectPrivilege"]
                or federation_audit["hasUnownedRegistryObject"]
                or federation_audit["hasMembership"]
                or federation_audit["searchPath"] != "pg_catalog, public"
                or not resource_policy_valid(
                    federation_audit, derived_resource_limits
                )
            ):
                fail(
                    "FEDERATION_DATABASE_URL is not the required isolated "
                    "FDW provisioner."
                )


            cursor.execute("""
                WITH blocked_roles(role_name) AS (
                  VALUES (%s::name), (%s::name)
                )
                SELECT
                  bool_or(
                    has_schema_privilege(
                      role_name, $$federation$$, $$USAGE,CREATE$$
                    )
                  ) AS "hasRegistrySchemaAccess",
                  bool_or(EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class AS registry_object
                    JOIN pg_catalog.pg_namespace AS namespace
                      ON namespace.oid = registry_object.relnamespace
                    WHERE namespace.nspname = $$federation$$
                      AND CASE
                        WHEN registry_object.relkind = $$S$$
                        THEN has_sequence_privilege(
                          role_name,
                          registry_object.oid,
                          $$USAGE,SELECT,UPDATE$$
                        )
                        WHEN registry_object.relkind IN (
                          $$r$$, $$p$$, $$v$$, $$m$$, $$f$$
                        ) THEN
                          has_table_privilege(
                            role_name,
                            registry_object.oid,
                            $$SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER$$
                          )
                          OR has_any_column_privilege(
                            role_name,
                            registry_object.oid,
                            $$SELECT,INSERT,UPDATE,REFERENCES$$
                          )
                        ELSE false
                      END
                  )) AS "hasRegistryObjectAccess"
                FROM blocked_roles
            """, (derived_role, reader_role))
            registry_access = cursor.fetchone()
            if any(registry_access.values()):
                fail(
                    "Runtime reader and derived owner must not access the "
                    "federation control registry."
                )

            cursor.execute("""
                SELECT EXISTS (
                  SELECT 1
                  FROM pg_catalog.pg_auth_members AS membership
                  JOIN pg_catalog.pg_roles AS consumer_role
                    ON consumer_role.oid = membership.roleid
                  WHERE consumer_role.rolname IN (%s, %s)
                ) AS "hasConsumerRoleMember"
            """, (derived_role, reader_role))
            if cursor.fetchone()["hasConsumerRoleMember"]:
                fail(
                    "Runtime reader and derived owner roles must not have "
                    "members."
                )

            cursor.execute(
                "SELECT to_regclass($$federation._aliases$$) AS oid"
            )
            if cursor.fetchone()["oid"] is not None:
                # archived_schema is added by the alias store the first time it
                # connects, which on an upgraded deployment may not have
                # happened yet. Selecting it unconditionally would abort this
                # whole audit with UndefinedColumn. Its absence also means no
                # alias can have been retired, so the retired branch below is
                # unreachable and NULL is the correct stand-in.
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = $$federation$$ "
                    "AND table_name = $$_aliases$$ "
                    "AND column_name IN "
                    "($$archived_schema$$, $$archived_server$$)"
                )
                present = {row["column_name"] for row in cursor.fetchall()}
                archived_column = (
                    "archived_schema"
                    if "archived_schema" in present
                    else "NULL::text AS archived_schema"
                ) + ", " + (
                    "archived_server"
                    if "archived_server" in present
                    else "NULL::text AS archived_server"
                )
                cursor.execute(
                    "SELECT public.PostGIS_Lib_Version() AS postgis, "
                    "(SELECT extversion FROM pg_catalog.pg_extension "
                    " WHERE extname = $$postgis$$) AS postgis_extversion, "
                    "public.PostGIS_PROJ_Version() AS proj, "
                    "public.PostGIS_GEOS_Version() AS geos"
                )
                local_extension_versions = dict(cursor.fetchone())
                cursor.execute(
                    "SELECT alias, connection_ref, allowed_relations, status, "
                    "last_observation, accepted_schema_fingerprint, "
                    "accepted_physical_identity, "
                    "accepted_connection_identity, last_observation_id, "
                    + archived_column + " "
                    "FROM federation._aliases "
                    "WHERE provisioned_at IS NOT NULL"
                )
                for alias_row in cursor.fetchall():
                    alias_value = alias_row["alias"]
                    if alias_row["status"] == "retired":
                        # These three hold however the alias was retired.
                        # retire() records no archived schema when the local
                        # one had already been removed by hand, but it still
                        # archives the server and drops the mappings
                        # independently. Skipping the live-name audits on that
                        # path left the credential-retention case unchecked --
                        # precisely the state retirement exists to prevent, and
                        # precisely the fix an earlier commit made to the
                        # store, unverified by the thing that audits it.
                        archived = alias_row["archived_schema"]
                        cursor.execute(
                            "SELECT 1 FROM pg_catalog.pg_namespace "
                            "WHERE nspname = %s",
                            (f"source_{alias_value}",),
                        )
                        if cursor.fetchone():
                            fail(
                                f"Federation alias {alias_value!r} is retired "
                                "but its live source schema still exists."
                            )
                        cursor.execute(
                            "SELECT 1 FROM pg_catalog.pg_foreign_server "
                            "WHERE srvname = %s",
                            (f"{alias_value}_srv",),
                        )
                        if cursor.fetchone():
                            fail(
                                f"Federation alias {alias_value!r} is retired "
                                "but its live foreign server still exists."
                            )
                        cursor.execute(
                            "SELECT count(*) AS mappings "
                            "FROM pg_catalog.pg_user_mappings "
                            "WHERE srvname = %s",
                            (f"{alias_value}_srv",),
                        )
                        if cursor.fetchone()["mappings"]:
                            fail(
                                f"Federation alias {alias_value!r} is retired "
                                "but its foreign server still holds user "
                                "mappings."
                            )
                        # retire() records the archived server name, so the
                        # audit never derives or pattern-matches it. Deriving
                        # it from archived_schema is wrong -- the alias is
                        # truncated four characters further for the server --
                        # and a "retired_" prefix does not identify an archive
                        # either, since ALIAS_RE permits an alias literally
                        # named retired_sites whose live server would then be
                        # read as one. Asserting existence is the point: the
                        # contract is archive rather than drop, and an audit
                        # that cannot notice a dropped archive does not verify
                        # it.
                        archived_srv = alias_row["archived_server"]
                        if archived_srv:
                            cursor.execute(
                                "SELECT pg_get_userbyid(srvowner) AS owner, "
                                "(SELECT count(*) "
                                "   FROM pg_catalog.pg_user_mappings AS m "
                                "  WHERE m.srvname = s.srvname) AS mappings "
                                "FROM pg_catalog.pg_foreign_server AS s "
                                "WHERE srvname = %s",
                                (archived_srv,),
                            )
                            archived_row = cursor.fetchone()
                            if not archived_row:
                                fail(
                                    f"Federation alias {alias_value!r} records "
                                    f"archived server {archived_srv!r}, which "
                                    "no longer exists."
                                )
                            archived_owner = archived_row["owner"]
                            if archived_owner != federation_role:
                                fail(
                                    f"Archived server {archived_srv!r} is "
                                    f"owned by {archived_owner!r}, not the "
                                    "federation provisioner."
                                )
                            if archived_row["mappings"]:
                                fail(
                                    f"Archived server {archived_srv!r} still "
                                    "holds user mappings."
                                )
                        if archived:
                            # Retirement archives rather than drops, so the
                            # schema must still exist under its archived name,
                            # owned by the provisioner and unreachable by
                            # either consumer role.
                            cursor.execute(
                                "SELECT pg_get_userbyid(nspowner) AS owner, "
                                "has_schema_privilege(%s, oid, $$USAGE$$) "
                                "  AS derived_use, "
                                "has_schema_privilege(%s, oid, $$USAGE$$) "
                                "  AS reader_use "
                                "FROM pg_catalog.pg_namespace "
                                "WHERE nspname = %s",
                                (derived_role, reader_role, archived),
                            )
                            archive = cursor.fetchone()
                            if (
                                not archive
                                or archive["owner"] != federation_role
                                or archive["derived_use"]
                                or archive["reader_use"]
                            ):
                                fail(
                                    f"Federation alias {alias_value!r} "
                                    "archived schema is missing, misowned, or "
                                    "still readable by a consumer role."
                                )
                        continue
                    if (
                        alias_row["status"] == "active"
                        and any(
                            alias_row[field] is None
                            for field in (
                                "accepted_schema_fingerprint",
                                "accepted_physical_identity",
                                "accepted_connection_identity",
                                "last_observation_id",
                            )
                        )
                    ):
                        fail(
                            f"Federation alias {alias_value!r} is active "
                            "without complete accepted evidence."
                        )
                    connection_ref = alias_row["connection_ref"]
                    connection_url = os.environ.get(
                        f"FEDERATION_DBS_{connection_ref}"
                    )
                    if not connection_url:
                        fail(
                            f"Federation alias {alias_value!r} connectionRef "
                            f"{connection_ref!r} is not configured."
                        )
                    params = psycopg.conninfo.conninfo_to_dict(connection_url)
                    server_name = f"{alias_value}_srv"
                    cursor.execute(
                        "SELECT pg_get_userbyid(srvowner) AS owner, srvoptions, "
                        "has_server_privilege(%s, oid, $$USAGE$$) AS derived_use, "
                        "has_server_privilege(%s, oid, $$USAGE$$) AS reader_use "
                        "FROM pg_catalog.pg_foreign_server WHERE srvname = %s",
                        (derived_role, reader_role, server_name),
                    )
                    server = cursor.fetchone()
                    if (
                        not server
                        or server["owner"] != federation_role
                        or server["derived_use"]
                        or server["reader_use"]
                    ):
                        fail(
                            f"Federation alias {alias_value!r} server is not "
                            "isolated to the FDW provisioner."
                        )
                    server_options = dict(
                        option.split("=", 1)
                        for option in server["srvoptions"]
                    )
                    connection_option_names = (
                        "host", "hostaddr", "port", "dbname", "sslmode",
                        "sslrootcert", "gssencmode",
                    )
                    expected_options = {
                        "host": str(params.get("host", "")),
                        "port": str(params.get("port", "5432")),
                        "dbname": str(params.get("dbname", "")),
                    }
                    expected_options.update({
                        name: str(params[name])
                        for name in connection_option_names
                        if name not in expected_options and params.get(name)
                    })
                    actual_options = {
                        name: server_options[name]
                        for name in connection_option_names
                        if name in server_options
                    }
                    unexpected_options = set(server_options) - (
                        set(connection_option_names)
                        | {"use_remote_estimate", "extensions"}
                    )
                    remote_extension_versions = (
                        alias_row["last_observation"] or {}
                    ).get("extensionVersions", {})
                    pushdown_safe = all(
                        local_extension_versions.get(local_key)
                        and local_extension_versions[local_key]
                            == remote_extension_versions.get(remote_key)
                        for local_key, remote_key in (
                            ("postgis", "postgis"),
                            ("postgis_extversion", "postgisExtversion"),
                            ("proj", "proj"),
                            ("geos", "geos"),
                        )
                    )
                    if (
                        actual_options != expected_options
                        or unexpected_options
                        or server_options.get("use_remote_estimate") != "true"
                        or server_options.get("extensions")
                            not in {None, "postgis"}
                        or (
                            alias_row["status"] == "active"
                            and server_options.get("extensions") == "postgis"
                            and not pushdown_safe
                        )
                    ):
                        fail(
                            f"Federation alias {alias_value!r} server options "
                            "do not match its connectionRef."
                        )
                    schema_name = f"source_{alias_value}"
                    expected_usage = alias_row["status"] == "active"
                    cursor.execute(
                        "SELECT pg_get_userbyid(nspowner) AS owner, "
                        "has_schema_privilege(%s, oid, $$USAGE$$) AS derived_use, "
                        "has_schema_privilege(%s, oid, $$USAGE$$) AS reader_use, "
                        "EXISTS ("
                        "  SELECT 1 FROM pg_catalog.aclexplode(COALESCE("
                        "    nspacl, pg_catalog.acldefault($$n$$, nspowner)"
                        "  )) AS privilege "
                        "  WHERE NOT ("
                        "    privilege.grantee = nspowner "
                        "    OR (%s AND privilege.grantor = nspowner "
                        "      AND privilege.grantee IN ("
                        "        pg_catalog.to_regrole(%s)::oid, "
                        "        pg_catalog.to_regrole(%s)::oid"
                        "      ) "
                        "      AND privilege.privilege_type = $$USAGE$$ "
                        "      AND NOT privilege.is_grantable)"
                        "    )"
                        ") AS \"hasUnexpectedAcl\" "
                        "FROM pg_catalog.pg_namespace WHERE nspname = %s",
                        (
                            derived_role,
                            reader_role,
                            expected_usage,
                            derived_role,
                            reader_role,
                            schema_name,
                        ),
                    )
                    schema = cursor.fetchone()
                    if (
                        not schema
                        or schema["owner"] != federation_role
                        or schema["derived_use"] != expected_usage
                        or schema["reader_use"] != expected_usage
                        or schema["hasUnexpectedAcl"]
                    ):
                        fail(
                            f"Federation alias {alias_value!r} source schema "
                            "ownership or access does not match its status."
                        )

                    cursor.execute("""
                        SELECT relation.relname, relation.relkind,
                               pg_get_userbyid(relation.relowner) AS owner,
                               server.srvname, foreign_table.ftoptions,
                               has_table_privilege(%s, relation.oid, $$SELECT$$)
                                 AS derived_select,
                               has_table_privilege(%s, relation.oid, $$SELECT$$)
                                 AS reader_select,
                               EXISTS (
                                 SELECT 1
                                 FROM pg_catalog.aclexplode(COALESCE(
                                   relation.relacl,
                                   pg_catalog.acldefault(
                                     $$r$$, relation.relowner
                                   )
                                 )) AS privilege
                                 WHERE NOT (
                                   privilege.grantee = relation.relowner
                                   OR (%s
                                     AND privilege.grantor = relation.relowner
                                     AND privilege.grantee IN (
                                       pg_catalog.to_regrole(%s)::oid,
                                       pg_catalog.to_regrole(%s)::oid
                                     )
                                     AND privilege.privilege_type = $$SELECT$$
                                     AND NOT privilege.is_grantable)
                                   )
                               ) AS "hasUnexpectedAcl",
                               EXISTS (
                                 SELECT 1
                                 FROM pg_catalog.pg_attribute AS attribute
                                 WHERE attribute.attrelid = relation.oid
                                   AND attribute.attnum > 0
                                   AND NOT attribute.attisdropped
                                   AND attribute.attacl IS NOT NULL
                               ) AS "hasColumnAcl"
                        FROM pg_catalog.pg_class AS relation
                        JOIN pg_catalog.pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        LEFT JOIN pg_catalog.pg_foreign_table AS foreign_table
                          ON foreign_table.ftrelid = relation.oid
                        LEFT JOIN pg_catalog.pg_foreign_server AS server
                          ON server.oid = foreign_table.ftserver
                        WHERE namespace.nspname = %s
                          AND relation.relkind IN (
                            $$r$$, $$p$$, $$v$$, $$m$$, $$f$$, $$S$$
                          )
                    """, (
                        derived_role,
                        reader_role,
                        expected_usage,
                        derived_role,
                        reader_role,
                        schema_name,
                    ))
                    local_relations = cursor.fetchall()
                    expected_relations = {
                        relation.split(".", 1)[1]: relation
                        for relation in alias_row["allowed_relations"]
                    }
                    if {row["relname"] for row in local_relations} != set(
                        expected_relations
                    ):
                        fail(
                            f"Federation alias {alias_value!r} local relation "
                            "set does not match its allowlist."
                        )
                    for relation in local_relations:
                        remote_options = dict(
                            option.split("=", 1)
                            for option in (relation["ftoptions"] or [])
                        )
                        remote_schema = remote_options.get("schema_name", "")
                        remote_table = remote_options.get("table_name", "")
                        remote_relation = f"{remote_schema}.{remote_table}"
                        if (
                            relation["relkind"] != "f"
                            or relation["owner"] != federation_role
                            or relation["srvname"] != server_name
                            or remote_relation
                            != expected_relations[relation["relname"]]
                            or relation["derived_select"] != expected_usage
                            or relation["reader_select"] != expected_usage
                            or relation["hasUnexpectedAcl"]
                            or relation["hasColumnAcl"]
                        ):
                            fail(
                                f"Federation alias {alias_value!r} has an "
                                "unmanaged local foreign-table binding."
                            )

                    cursor.execute("""
                        SELECT usename AS role_name, umoptions
                        FROM pg_catalog.pg_user_mappings
                        WHERE srvname = %s
                    """, (server_name,))
                    mappings = {
                        row["role_name"]: dict(
                            option.split("=", 1)
                            for option in (row["umoptions"] or [])
                        )
                        for row in cursor.fetchall()
                    }
                    expected_mapping = {
                        "user": str(params.get("user", "")),
                        "password": str(params.get("password", "")),
                    }
                    # PostgreSQL masks umoptions for mappings owned by other
                    # roles when those roles intentionally lack server USAGE.
                    # The closed role set and privilege checks below keep the
                    # federation provisioner as the only mapping authority.
                    required_mapping_roles = {derived_role, reader_role}
                    allowed_mapping_roles = required_mapping_roles | {
                        federation_role
                    }
                    if (
                        not required_mapping_roles.issubset(mappings)
                        or not set(mappings).issubset(allowed_mapping_roles)
                        or (
                            alias_row["status"] == "active"
                            and set(mappings) != allowed_mapping_roles
                        )
                        or (
                            federation_role in mappings
                            and mappings[federation_role] != expected_mapping
                        )
                    ):
                        fail(
                            f"Federation alias {alias_value!r} has unexpected "
                            "user mappings."
                        )

                # A sweep to catch what the per-alias walk structurally
                # cannot: a server orphaned by a deleted registry row, or one
                # renamed out of any recognisable shape while keeping its
                # credentials. It classifies nothing by name -- an earlier
                # version matched a "retired_" prefix and would have failed
                # the audit on a healthy live source whose alias was simply
                # called retired_sites. The invariant is stated exactly
                # instead: among the servers this feature provisioned, only
                # one belonging to a live alias may hold user mappings, since
                # those hold the remote user and password in the catalogue in
                # plain text.
                cursor.execute(
                    "SELECT s.srvname, "
                    "(SELECT count(*) FROM pg_catalog.pg_user_mappings AS m "
                    "  WHERE m.srvname = s.srvname) AS mappings "
                    "FROM pg_catalog.pg_foreign_server AS s "
                    "JOIN pg_catalog.pg_foreign_data_wrapper AS w "
                    "  ON w.oid = s.srvfdw "
                    "WHERE w.fdwname = $$postgres_fdw$$ "
                    "AND pg_get_userbyid(s.srvowner) = %s "
                    "AND s.srvname NOT IN ("
                    "  SELECT alias || $$_srv$$ FROM federation._aliases "
                    "  WHERE status <> $$retired$$"
                    ")",
                    (federation_role,),
                )
                for server_row in cursor.fetchall():
                    if not server_row["mappings"]:
                        continue
                    server_label = server_row["srvname"]
                    fail(
                        f"Foreign server {server_label!r} belongs to no live "
                        "federation alias but still holds user mappings."
                    )

print(
    "Runtime, derived, and federation PostgreSQL identities and privileges "
    "verified."
)
' "$(dotenv_value DERIVED_DB_USER)"

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

if selected:
  print(urlencode(selected), end="")
PY
)"
if [[ -n "${mvt_query}" ]]; then
  mvt_file="$(mktemp)"
  trap 'rm -f "${mvt_file}"' EXIT
  curl --fail --silent --show-error "${map_headers[@]}" \
    "${map_url}/api/query?${mvt_query}" \
    --output "${mvt_file}"
  if [[ ! -s "${mvt_file}" ]]; then
    printf 'XYZ returned an empty MVT response for a current workspace layer.\n' >&2
    exit 1
  fi
else
  printf 'Workspace has no database-backed MVT layer; skipped the live MVT render probe.\n'
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

printf 'PASS: bundled PostGIS and sample data, service health, public config identity, browser-runner health, shared SVG icons, XYZ, and Caddy guards.\n'
