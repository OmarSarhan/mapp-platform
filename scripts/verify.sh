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
    "${!DBS_@}" ETL_DATABASE_URL POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD \
    ETL_DB_USER ETL_DB_PASSWORD XYZ_DB_USER XYZ_DB_PASSWORD \
    DERIVED_DB_USER DERIVED_DB_PASSWORD DERIVED_DATABASE_URL \
    DERIVED_READER_ROLE \
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
database_mode="$(dotenv_value MAPP_DATABASE_MODE)"
if [[ -v MAPP_DATABASE_MODE && "${MAPP_DATABASE_MODE}" != "${database_mode}" ]]; then
  printf 'Exported MAPP_DATABASE_MODE conflicts with the authoritative value in %s; unset it or update the env file deliberately.\n' \
    "${ENV_FILE}" >&2
  exit 2
fi
case "${database_mode}" in
  bundled)
    compose+=(--file "${ROOT_DIR}/compose.bundled-db.yaml")
    required_services=(db semantic-service xyz xyz-preview config-ui browser-runner egress-proxy caddy)
    ;;
  external)
    required_services=(semantic-service xyz xyz-preview config-ui browser-runner egress-proxy caddy)
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
resolved_derived_reader="$(
  "${compose[@]}" config --format json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["config-ui"]["environment"]["DERIVED_READER_ROLE"], end="")'
)"
if [[ -z "${resolved_derived_dbs}" && -n "${resolved_derived_reader}" ]] \
  || [[ -n "${resolved_derived_dbs}" && -z "${resolved_derived_reader}" ]]; then
  printf 'DERIVED_DATABASE_URL and DERIVED_READER_ROLE must either both be configured or both be empty.\n' >&2
  exit 2
fi
if [[ "${database_mode}" == "external" ]]; then
  printf '%s' "${resolved_dbs}" \
    | python3 "${ROOT_DIR}/scripts/validate_database_url.py"
  if [[ -n "${resolved_derived_dbs}" ]]; then
    printf '%s' "${resolved_derived_dbs}" \
      | python3 "${ROOT_DIR}/scripts/validate_database_url.py"
  fi
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
running_derived_reader="$(
  "${compose[@]}" exec -T config-ui sh -c \
    'printf %s "$DERIVED_READER_ROLE"'
)"
if [[ "${running_derived_reader}" != "${resolved_derived_reader}" ]]; then
  printf 'config-ui is not running with the DERIVED_READER_ROLE value resolved from the current environment. Recreate the service before verification.\n' >&2
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

if [[ "${database_mode}" == "bundled" ]]; then
  "${compose[@]}" exec -T db \
    sh /usr/local/bin/mapp-prepare-spatial-indexes check
  "${compose[@]}" exec -T \
    -e "MAPP_VERIFY_CENSUS_GEOMETRY_SHA256=${census_geometry_sha256}" \
    -e "MAPP_VERIFY_CENSUS_TOPIC_HASHES_JSON=${census_topic_hashes_json}" \
    db sh -c \
    'exec psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      --set ON_ERROR_STOP=1 \
      --set=etl_db_user="$ETL_DB_USER" \
      --set=xyz_db_user="$XYZ_DB_USER" \
      --set=derived_db_user="$DERIVED_DB_USER" \
      --set=census_geometry_sha256="$MAPP_VERIFY_CENSUS_GEOMETRY_SHA256" \
      --set=census_topic_hashes_json="$MAPP_VERIFY_CENSUS_TOPIC_HASHES_JSON"' <<'SQL'
SELECT postgis_full_version();
SELECT set_config('mapp.verify.etl_db_user', :'etl_db_user', false);
SELECT set_config('mapp.verify.xyz_db_user', :'xyz_db_user', false);
SELECT set_config(
  'mapp.verify.derived_db_user',
  :'derived_db_user',
  false
);
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

DO $$
DECLARE
  census_relation regclass := to_regclass(
    'leeds.census_2021_england_oa'
  );
  checked_relation regclass;
  etl_role name := current_setting('mapp.verify.etl_db_user');
  xyz_role name := current_setting('mapp.verify.xyz_db_user');
  derived_role name := current_setting('mapp.verify.derived_db_user');
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
  matching_dataset_metadata_count integer;
  matching_last_run_count integer;
  xyz_default_select boolean;
  derived_default_select boolean;
  unsafe_table_default boolean;
  unsafe_sequence_default boolean;
  unsafe_sequence_grant boolean;
  public_connect boolean;
  public_temporary boolean;
  generated_kind text;
  expected_geometry_sha256 text :=
    current_setting('mapp.verify.census_geometry_sha256');
  expected_topic_hashes jsonb :=
    current_setting('mapp.verify.census_topic_hashes_json')::jsonb;
  expected_topic_hash_count integer;
  variable_topic_hash_mismatch_count integer;
  dataset_topic_hash_count integer;
  dataset_topic_hash_mismatch_count integer;
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
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = defaults.defaclnamespace
      CROSS JOIN LATERAL
        pg_catalog.aclexplode(defaults.defaclacl) AS privilege
      JOIN pg_catalog.pg_roles AS grantee
        ON grantee.oid = privilege.grantee
      WHERE owner.rolname = etl_role
        AND namespace.nspname = 'leeds'
        AND defaults.defaclobjtype = 'r'
        AND grantee.rolname = xyz_role
        AND privilege.privilege_type = 'SELECT'
        AND NOT privilege.is_grantable
    ),
    EXISTS (
      SELECT 1
      FROM pg_catalog.pg_default_acl AS defaults
      JOIN pg_catalog.pg_roles AS owner
        ON owner.oid = defaults.defaclrole
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = defaults.defaclnamespace
      CROSS JOIN LATERAL
        pg_catalog.aclexplode(defaults.defaclacl) AS privilege
      JOIN pg_catalog.pg_roles AS grantee
        ON grantee.oid = privilege.grantee
      WHERE owner.rolname = etl_role
        AND namespace.nspname = 'leeds'
        AND defaults.defaclobjtype = 'r'
        AND grantee.rolname = derived_role
        AND privilege.privilege_type = 'SELECT'
        AND NOT privilege.is_grantable
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
        AND (
          defaults.defaclnamespace = 0
          OR namespace.nspname = 'leeds'
        )
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
        AND (
          defaults.defaclnamespace = 0
          OR namespace.nspname = 'leeds'
        )
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
    xyz_default_select,
    derived_default_select,
    unsafe_table_default,
    unsafe_sequence_default;

  IF NOT xyz_default_select OR NOT derived_default_select THEN
    RAISE EXCEPTION
      'ETL table default privileges must grant SELECT to both the runtime reader and derived owner';
  END IF;
  IF unsafe_table_default THEN
    RAISE EXCEPTION
      'Runtime reader and derived owner default privileges must be non-grantable SELECT only';
  END IF;
  IF unsafe_sequence_default THEN
    RAISE EXCEPTION
      'Runtime reader and derived owner defaults must not permit sequence mutation';
  END IF;

  SELECT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'leeds'
      AND relation.relkind = 'S'
      AND (
        has_sequence_privilege(xyz_role, relation.oid, 'USAGE')
        OR has_sequence_privilege(xyz_role, relation.oid, 'UPDATE')
        OR has_sequence_privilege(
          derived_role,
          relation.oid,
          'USAGE'
        )
        OR has_sequence_privilege(
          derived_role,
          relation.oid,
          'UPDATE'
        )
      )
  )
  INTO unsafe_sequence_grant;

  IF unsafe_sequence_grant THEN
    RAISE EXCEPTION
      'Runtime reader and derived owner must not mutate Leeds sequences';
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
  IF has_schema_privilege(xyz_role, 'leeds', 'CREATE')
     OR has_schema_privilege(derived_role, 'leeds', 'CREATE') THEN
    RAISE EXCEPTION
      'Runtime reader and derived owner must not create objects in the Leeds source schema';
  END IF;

  IF census_relation IS NULL THEN
    RAISE NOTICE 'Optional Census relation leeds.census_2021_england_oa is absent; skipped Census verification';
    RETURN;
  END IF;

  FOREACH checked_relation IN ARRAY ARRAY[
    census_relation,
    to_regclass('leeds.census_datasets'),
    to_regclass('leeds.census_variables'),
    to_regclass('leeds._census_etl_runs')
  ] LOOP
    IF checked_relation IS NULL THEN
      RAISE EXCEPTION
        'A required Census relation or metadata relation is missing';
    END IF;
    IF NOT EXISTS (
      SELECT 1
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_roles AS owner
        ON owner.oid = relation.relowner
      WHERE relation.oid = checked_relation
        AND owner.rolname = etl_role
    ) THEN
      RAISE EXCEPTION
        'ETL role must own Census relation %',
        checked_relation;
    END IF;
    IF has_schema_privilege(xyz_role, 'leeds', 'USAGE') IS NOT TRUE
       OR has_table_privilege(
         xyz_role,
         checked_relation,
         'SELECT'
       ) IS NOT TRUE
       OR has_schema_privilege(derived_role, 'leeds', 'USAGE') IS NOT TRUE
       OR has_table_privilege(
         derived_role,
         checked_relation,
         'SELECT'
       ) IS NOT TRUE THEN
      RAISE EXCEPTION
        'Runtime reader and derived owner must both read %',
        checked_relation;
    END IF;
    IF has_table_privilege(xyz_role, checked_relation, 'INSERT')
       OR has_table_privilege(xyz_role, checked_relation, 'UPDATE')
       OR has_table_privilege(xyz_role, checked_relation, 'DELETE')
       OR has_table_privilege(xyz_role, checked_relation, 'TRUNCATE')
       OR has_table_privilege(xyz_role, checked_relation, 'REFERENCES')
       OR has_table_privilege(xyz_role, checked_relation, 'TRIGGER')
       OR has_table_privilege(derived_role, checked_relation, 'INSERT')
       OR has_table_privilege(derived_role, checked_relation, 'UPDATE')
       OR has_table_privilege(derived_role, checked_relation, 'DELETE')
       OR has_table_privilege(derived_role, checked_relation, 'TRUNCATE')
       OR has_table_privilege(
         derived_role,
         checked_relation,
         'REFERENCES'
       )
       OR has_table_privilege(
         derived_role,
         checked_relation,
         'TRIGGER'
       )
       OR has_any_column_privilege(
         xyz_role,
         checked_relation,
         'INSERT'
       )
       OR has_any_column_privilege(
         xyz_role,
         checked_relation,
         'UPDATE'
       )
       OR has_any_column_privilege(
         xyz_role,
         checked_relation,
         'REFERENCES'
       )
       OR has_any_column_privilege(
         derived_role,
         checked_relation,
         'INSERT'
       )
       OR has_any_column_privilege(
         derived_role,
         checked_relation,
         'UPDATE'
       )
       OR has_any_column_privilege(
         derived_role,
         checked_relation,
         'REFERENCES'
       ) THEN
      RAISE EXCEPTION
        'Runtime reader and derived owner must not mutate Census relation %',
        checked_relation;
    END IF;
  END LOOP;

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

"${compose[@]}" exec -T \
  -e "MAPP_VERIFY_CENSUS_GEOMETRY_SHA256=${census_geometry_sha256}" \
  -e "MAPP_VERIFY_CENSUS_TOPIC_HASHES_JSON=${census_topic_hashes_json}" \
  xyz node --input-type=module -e '
  import pg from "pg";

  const fail = (message) => {
    throw new Error(message);
  };
  const expectedGeometryHash =
    process.env.MAPP_VERIFY_CENSUS_GEOMETRY_SHA256;
  let expectedTopicHashes;
  try {
    expectedTopicHashes = JSON.parse(
      process.env.MAPP_VERIFY_CENSUS_TOPIC_HASHES_JSON,
    );
  } catch {
    fail("The pinned Census topic hashes are not valid JSON.");
  }
  if (
    !/^[0-9a-f]{64}$/.test(expectedGeometryHash)
    || !expectedTopicHashes
    || Array.isArray(expectedTopicHashes)
    || Object.keys(expectedTopicHashes).length !== 47
    || Object.values(expectedTopicHashes).some(
      (value) => !/^[0-9a-f]{64}$/.test(value),
    )
  ) {
    fail("The pinned Census source-hash contract is invalid.");
  }
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
    const declaredReader = process.argv[2];
    const bundledReader = process.argv[3];
    if (
      !audit
      || !audit.postgis
      || audit.currentUser !== audit.sessionUser
      || audit.currentUser !== uriUser
      || (declaredReader && audit.currentUser !== declaredReader)
      || (process.argv[1] === "bundled"
        && audit.currentUser !== bundledReader)
      || !audit.canLogin
      || audit.superuser
      || audit.canCreateDatabase
      || audit.canCreateRole
      || audit.replication
      || audit.bypassesRls
      || audit.ownsDatabase
      || audit.hasTemporary
      || audit.hasPublicDatabasePrivilege
      || audit.canCreateSchema
      || audit.hasUnsafeRelationPrivilege
      || audit.hasUnsafeSequencePrivilege
      || audit.hasUnsafeMembership
    ) {
      fail("The active DBS_MAPP session is not the required read-only runtime identity.");
    }

    if (process.argv[1] === "bundled") {
      for (const relation of [
        "leeds.bus_stops",
        "leeds.definitive_paths",
        "leeds.smoke_control_orders",
      ]) {
        await pool.query(`SELECT 1 FROM ${relation} LIMIT 0`);
      }
    }

    const relation = await pool.query(
      "SELECT to_regclass($1)::text AS name",
      ["leeds.census_2021_england_oa"],
    );
    if (relation.rows[0]?.name) {
      const census = await pool.query(
        "SELECT count(*)::text AS row_count FROM leeds.census_2021_england_oa",
      );
      if (census.rows[0]?.row_count !== "178605") {
        fail("The runtime reader did not observe the reviewed Census row count.");
      }

      const datasetResult = await pool.query(`
        SELECT geometry_source_sha256, source_metadata
        FROM leeds.census_datasets
        WHERE dataset_key = $$census_2021_england_oa$$
      `);
      const dataset = datasetResult.rows[0];
      if (
        datasetResult.rowCount !== 1
        || dataset.geometry_source_sha256 !== expectedGeometryHash
        || dataset.source_metadata?.geometry?.sha256 !== expectedGeometryHash
      ) {
        fail("The loaded Census geometry hash does not match the pinned manifest.");
      }

      const metadataTopics = dataset.source_metadata?.topics;
      if (!Array.isArray(metadataTopics) || metadataTopics.length !== 47) {
        fail("The loaded Census dataset does not record all 47 topic sources.");
      }
      const metadataTopicHashes = new Map();
      for (const topic of metadataTopics) {
        if (
          !topic
          || typeof topic.topic_id !== "string"
          || typeof topic.archive_sha256 !== "string"
          || metadataTopicHashes.has(topic.topic_id)
        ) {
          fail("The loaded Census dataset has invalid or duplicate topic metadata.");
        }
        metadataTopicHashes.set(topic.topic_id, topic.archive_sha256);
      }

      const variableResult = await pool.query(`
        SELECT
          topic_id,
          array_agg(
            DISTINCT source_sha256
            ORDER BY source_sha256
          ) AS source_hashes
        FROM leeds.census_variables
        WHERE dataset_key = $$census_2021_england_oa$$
        GROUP BY topic_id
      `);
      if (variableResult.rowCount !== 47) {
        fail("The loaded Census variable catalogue does not contain 47 topics.");
      }
      const variableTopicHashes = new Map(
        variableResult.rows.map(
          (row) => [
            row.topic_id,
            row.source_hashes?.length === 1
              ? row.source_hashes[0]
              : null,
          ],
        ),
      );
      for (const [topicId, expectedHash] of Object.entries(
        expectedTopicHashes,
      )) {
        if (
          metadataTopicHashes.get(topicId) !== expectedHash
          || variableTopicHashes.get(topicId) !== expectedHash
        ) {
          fail(
            `The loaded Census source hash for ${topicId} does not match the pinned manifest.`,
          );
        }
      }
    }
  } finally {
    await pool.end();
  }
' "${database_mode}" "${resolved_derived_reader}" "$(dotenv_value XYZ_DB_USER)"

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


database_mode = sys.argv[1]
bundled_derived_role = sys.argv[2]
database_url = os.environ.get("DERIVED_DATABASE_URL", "")
reader_role = os.environ.get("DERIVED_READER_ROLE", "")

if not database_url:
    if reader_role:
        fail(
            "DERIVED_READER_ROLE must be empty when derived database "
            "management is disabled."
        )
    print("Derived database management is disabled and internally consistent.")
    raise SystemExit(0)
if not reader_role:
    fail("DERIVED_READER_ROLE is required with DERIVED_DATABASE_URL.")

try:
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.username is None:
        fail("DERIVED_DATABASE_URL must contain an explicit PostgreSQL login.")
    uri_user = unquote_to_bytes(parsed.username).decode("utf-8")
except (UnicodeDecodeError, ValueError):
    fail("DERIVED_DATABASE_URL contains an invalid encoded login identity.")

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
        # federation._aliases is created lazily by
        # FederationAliasStore._initialize() on first use, not by database
        # init/upgrade — a fresh deployment that has never taken a
        # federation API call has the `federation` schema (init script) but
        # not yet this table. Referencing it unconditionally below would
        # abort this entire audit with UndefinedTable. Provisioning always
        # goes through that same store, so an absent table also means no
        # alias could ever have been provisioned — substitute an always-
        # empty stand-in rather than the real table in that case.
        cursor.execute("SELECT to_regclass($$federation._aliases$$) AS oid")
        federation_registry_source = (
            "federation._aliases"
            if cursor.fetchone()["oid"] is not None
            else "(SELECT NULL::text AS alias, "
            "NULL::timestamptz AS provisioned_at, "
            "NULL::text[] AS allowed_relations WHERE FALSE)"
        )
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
              -- Schemas the derived owner may legitimately own: the two
              -- fixed schemas plus one dynamic source_<alias> schema per
              -- registered, provisioned federation alias
              -- (config-ui/federation_store.py provision()). Ownership
              -- alone is NOT exempted, and a source_<alias>-shaped name
              -- alone is not either: the derived owner has database-level
              -- CREATE (needed for provision() to work at all), so it
              -- could create a same-shaped schema itself without ever
              -- going through the federation API. Exemption requires
              -- ownership AND (one of the two fixed names OR a row in
              -- federation._aliases naming this exact schema, provisioned)
              -- — so an unexpected schema (e.g. public, or a same-pattern
              -- schema with no matching registry entry) ending up owned by
              -- this role still trips every check below instead of being
              -- silently trusted.
              EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname !~ $$^pg_$$
                  AND namespace.nspname <> $$information_schema$$
                  AND NOT (
                    namespace.nspowner = login_role.oid
                    AND (
                      namespace.nspname IN ($$derived_layers$$, $$federation$$)
                      OR EXISTS (
                        SELECT 1 FROM federation._aliases AS fed_alias
                        WHERE namespace.nspname
                                = ($$source_$$ || fed_alias.alias)
                          AND fed_alias.provisioned_at IS NOT NULL
                      )
                    )
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
                    AND (
                      namespace.nspname IN ($$derived_layers$$, $$federation$$)
                      OR EXISTS (
                        SELECT 1
                        FROM federation._aliases AS fed_alias
                        WHERE namespace.nspname
                                = ($$source_$$ || fed_alias.alias)
                          AND fed_alias.provisioned_at IS NOT NULL
                          AND relation.relkind = $$f$$
                          AND EXISTS (
                            SELECT 1
                            FROM pg_catalog.pg_foreign_table AS foreign_table
                            JOIN pg_catalog.pg_foreign_server AS foreign_server
                              ON foreign_server.oid = foreign_table.ftserver
                            WHERE foreign_table.ftrelid = relation.oid
                              AND foreign_server.srvname = (fed_alias.alias || $$_srv$$)
                              AND (
                                (
                                  SELECT split_part(option, $$=$$, 2)
                                  FROM unnest(foreign_table.ftoptions) AS option
                                  WHERE option LIKE $$schema_name=%$$
                                )
                                || $$.$$ ||
                                (
                                  SELECT split_part(option, $$=$$, 2)
                                  FROM unnest(foreign_table.ftoptions) AS option
                                  WHERE option LIKE $$table_name=%$$
                                )
                              ) = ANY(fed_alias.allowed_relations)
                          )
                      )
                    )
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
                    AND (
                      namespace.nspname IN ($$derived_layers$$, $$federation$$)
                      OR EXISTS (
                        SELECT 1
                        FROM federation._aliases AS fed_alias
                        WHERE namespace.nspname
                                = ($$source_$$ || fed_alias.alias)
                          AND fed_alias.provisioned_at IS NOT NULL
                          AND relation.relkind = $$f$$
                          AND EXISTS (
                            SELECT 1
                            FROM pg_catalog.pg_foreign_table AS foreign_table
                            JOIN pg_catalog.pg_foreign_server AS foreign_server
                              ON foreign_server.oid = foreign_table.ftserver
                            WHERE foreign_table.ftrelid = relation.oid
                              AND foreign_server.srvname = (fed_alias.alias || $$_srv$$)
                              AND (
                                (
                                  SELECT split_part(option, $$=$$, 2)
                                  FROM unnest(foreign_table.ftoptions) AS option
                                  WHERE option LIKE $$schema_name=%$$
                                )
                                || $$.$$ ||
                                (
                                  SELECT split_part(option, $$=$$, 2)
                                  FROM unnest(foreign_table.ftoptions) AS option
                                  WHERE option LIKE $$table_name=%$$
                                )
                              ) = ANY(fed_alias.allowed_relations)
                          )
                      )
                    )
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
                    OR EXISTS (
                      SELECT 1
                      FROM pg_catalog.pg_namespace AS namespace
                      WHERE namespace.nspname !~ $$^pg_$$
                        AND namespace.nspname <> $$information_schema$$
                        AND NOT (
                          namespace.nspowner = reachable_role.oid
                          AND (
                            namespace.nspname
                              IN ($$derived_layers$$, $$federation$$)
                            OR EXISTS (
                              SELECT 1 FROM federation._aliases
                                AS fed_alias
                              WHERE namespace.nspname
                                      = ($$source_$$ || fed_alias.alias)
                                AND fed_alias.provisioned_at IS NOT NULL
                            )
                          )
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
                          AND (
                            namespace.nspname
                              IN ($$derived_layers$$, $$federation$$)
                            OR EXISTS (
                              SELECT 1
                              FROM federation._aliases AS fed_alias
                              WHERE namespace.nspname
                                      = ($$source_$$ || fed_alias.alias)
                                AND fed_alias.provisioned_at IS NOT NULL
                                AND relation.relkind = $$f$$
                                AND EXISTS (
                                  SELECT 1
                                  FROM pg_catalog.pg_foreign_table AS foreign_table
                                  JOIN pg_catalog.pg_foreign_server AS foreign_server
                                    ON foreign_server.oid = foreign_table.ftserver
                                  WHERE foreign_table.ftrelid = relation.oid
                                    AND foreign_server.srvname = (fed_alias.alias || $$_srv$$)
                                    AND (
                                      (
                                        SELECT split_part(option, $$=$$, 2)
                                        FROM unnest(foreign_table.ftoptions) AS option
                                        WHERE option LIKE $$schema_name=%$$
                                      )
                                      || $$.$$ ||
                                      (
                                        SELECT split_part(option, $$=$$, 2)
                                        FROM unnest(foreign_table.ftoptions) AS option
                                        WHERE option LIKE $$table_name=%$$
                                      )
                                    ) = ANY(fed_alias.allowed_relations)
                                )
                            )
                          )
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
                          AND (
                            namespace.nspname
                              IN ($$derived_layers$$, $$federation$$)
                            OR EXISTS (
                              SELECT 1
                              FROM federation._aliases AS fed_alias
                              WHERE namespace.nspname
                                      = ($$source_$$ || fed_alias.alias)
                                AND fed_alias.provisioned_at IS NOT NULL
                                AND relation.relkind = $$f$$
                                AND EXISTS (
                                  SELECT 1
                                  FROM pg_catalog.pg_foreign_table AS foreign_table
                                  JOIN pg_catalog.pg_foreign_server AS foreign_server
                                    ON foreign_server.oid = foreign_table.ftserver
                                  WHERE foreign_table.ftrelid = relation.oid
                                    AND foreign_server.srvname = (fed_alias.alias || $$_srv$$)
                                    AND (
                                      (
                                        SELECT split_part(option, $$=$$, 2)
                                        FROM unnest(foreign_table.ftoptions) AS option
                                        WHERE option LIKE $$schema_name=%$$
                                      )
                                      || $$.$$ ||
                                      (
                                        SELECT split_part(option, $$=$$, 2)
                                        FROM unnest(foreign_table.ftoptions) AS option
                                        WHERE option LIKE $$table_name=%$$
                                      )
                                    ) = ANY(fed_alias.allowed_relations)
                                )
                            )
                          )
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
        cursor.execute(
            audit_sql.replace("federation._aliases", federation_registry_source)
        )
        audit = cursor.fetchone()
        if audit and audit["searchPath"] != "pg_catalog, public":
            fail(
                "The active DERIVED_DATABASE_URL owner search_path must be "
                "exactly pg_catalog, public."
            )
        if (
            not audit
            or not audit["postgis"]
            or audit["databaseName"] != reader_session["database_name"]
            or audit["currentUser"] != audit["sessionUser"]
            or audit["currentUser"] != uri_user
            or audit["currentUser"] == reader_role
            or (
                database_mode == "bundled"
                and audit["currentUser"] != bundled_derived_role
            )
            or not audit["canLogin"]
            or audit["superuser"]
            or audit["canCreateDatabase"]
            or audit["canCreateRole"]
            or audit["replication"]
            or audit["bypassesRls"]
            or audit["ownsDatabase"]
            or audit["hasTemporary"]
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

        if database_mode == "bundled":
            for relation in (
                "leeds.bus_stops",
                "leeds.definitive_paths",
                "leeds.smoke_control_orders",
            ):
                cursor.execute(f"SELECT 1 FROM {relation} LIMIT 0")
            cursor.execute(
                "SELECT to_regclass(%s)::text AS name",
                ("leeds.census_2021_england_oa",),
            )
            if cursor.fetchone()["name"]:
                cursor.execute(
                    "SELECT count(*)::bigint AS row_count "
                    "FROM leeds.census_2021_england_oa"
                )
                if cursor.fetchone()["row_count"] != 178605:
                    fail(
                        "The derived owner did not observe the reviewed "
                        "Census row count."
                    )

print("Runtime and derived PostgreSQL identities and privileges verified.")
' "${database_mode}" "$(dotenv_value DERIVED_DB_USER)"

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
