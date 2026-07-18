#!/bin/sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${ETL_DB_USER:?ETL_DB_USER is required}"
: "${XYZ_DB_USER:?XYZ_DB_USER is required}"
: "${DERIVED_DB_USER:?DERIVED_DB_USER is required}"
: "${DERIVED_DB_PASSWORD:?DERIVED_DB_PASSWORD is required}"

psql \
  --set ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set etl_db_user="${ETL_DB_USER}" \
  --set xyz_db_user="${XYZ_DB_USER}" \
  --set derived_db_user="${DERIVED_DB_USER}" \
  --set derived_db_password="${DERIVED_DB_PASSWORD}" <<'SQL'
CREATE EXTENSION IF NOT EXISTS h3;
CREATE EXTENSION IF NOT EXISTS h3_postgis CASCADE;

SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L',
  :'derived_db_user',
  :'derived_db_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = :'derived_db_user'
)
\gexec

ALTER ROLE :"derived_db_user" LOGIN PASSWORD :'derived_db_password';
GRANT CONNECT ON DATABASE :"DBNAME" TO :"derived_db_user";
GRANT USAGE ON SCHEMA leeds TO :"derived_db_user";
GRANT SELECT ON ALL TABLES IN SCHEMA leeds TO :"derived_db_user";

SELECT format(
  'CREATE SCHEMA derived_layers AUTHORIZATION %I',
  :'derived_db_user'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_namespace WHERE nspname = 'derived_layers'
)
\gexec

ALTER SCHEMA derived_layers OWNER TO :"derived_db_user";
REVOKE ALL ON SCHEMA derived_layers FROM PUBLIC;
GRANT USAGE ON SCHEMA derived_layers TO :"xyz_db_user";

ALTER DEFAULT PRIVILEGES FOR ROLE :"etl_db_user" IN SCHEMA leeds
  GRANT SELECT ON TABLES TO :"derived_db_user";
SQL
