#!/bin/sh
set -eu

pg_isready \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" >/dev/null

# This baseline must let pre-role-split volumes start so bin/mapp can run the
# transactional upgrade. scripts/verify.sh enforces the federation role after it.
ready="$(
  psql \
    --no-psqlrc \
    --tuples-only \
    --no-align \
    --username "${POSTGRES_USER}" \
    --dbname "${POSTGRES_DB}" \
    --set etl_db_user="${ETL_DB_USER}" \
    --set xyz_db_user="${XYZ_DB_USER}" \
    --set derived_db_user="${DERIVED_DB_USER}" <<'SQL'
SELECT CASE WHEN
  EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis')
  AND EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'h3')
  AND EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'h3_postgis')
  AND to_regclass('public.instance_runtime') IS NOT NULL
  AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'etl_db_user')
  AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'xyz_db_user')
  AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'derived_db_user')
  AND EXISTS (
    SELECT 1
    FROM pg_namespace
    WHERE nspname = 'leeds'
      AND nspowner = (SELECT oid FROM pg_roles WHERE rolname = :'etl_db_user')
  )
  AND EXISTS (
    SELECT 1
    FROM pg_namespace
    WHERE nspname = 'derived_layers'
      AND nspowner = (SELECT oid FROM pg_roles WHERE rolname = :'derived_db_user')
  )
THEN 1 ELSE 0 END;
SQL
)"

[ "${ready}" = "1" ]
