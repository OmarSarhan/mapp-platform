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
BEGIN;

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
ALTER ROLE :"etl_db_user" CONNECTION LIMIT 4;
ALTER ROLE :"xyz_db_user" CONNECTION LIMIT 32;
ALTER ROLE :"derived_db_user" CONNECTION LIMIT 4;

ALTER ROLE :"derived_db_user" SET search_path = pg_catalog, public;

ALTER ROLE :"xyz_db_user" SET work_mem = '8MB';
ALTER ROLE :"xyz_db_user" SET hash_mem_multiplier = '1';
ALTER ROLE :"xyz_db_user" SET maintenance_work_mem = '32MB';
ALTER ROLE :"xyz_db_user" SET max_parallel_workers_per_gather = '1';
ALTER ROLE :"xyz_db_user" SET temp_file_limit = '256MB';
ALTER ROLE :"xyz_db_user" SET statement_timeout = '15s';
ALTER ROLE :"xyz_db_user" SET transaction_timeout = '30s';
ALTER ROLE :"xyz_db_user" SET lock_timeout = '5s';
ALTER ROLE :"xyz_db_user" SET idle_in_transaction_session_timeout = '30s';

ALTER ROLE :"derived_db_user" SET work_mem = '16MB';
ALTER ROLE :"derived_db_user" SET hash_mem_multiplier = '1';
ALTER ROLE :"derived_db_user" SET maintenance_work_mem = '64MB';
ALTER ROLE :"derived_db_user" SET max_parallel_workers_per_gather = '2';
ALTER ROLE :"derived_db_user" SET temp_file_limit = '1GB';
ALTER ROLE :"derived_db_user" SET statement_timeout = '30min';
ALTER ROLE :"derived_db_user" SET transaction_timeout = '35min';
ALTER ROLE :"derived_db_user" SET lock_timeout = '5s';
ALTER ROLE :"derived_db_user" SET idle_in_transaction_session_timeout = '1min';

REVOKE CONNECT, TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE :"DBNAME"
  FROM :"xyz_db_user", :"derived_db_user";
GRANT CONNECT, TEMPORARY ON DATABASE :"DBNAME" TO :"etl_db_user";
GRANT CONNECT ON DATABASE :"DBNAME"
  TO :"xyz_db_user", :"derived_db_user";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA leeds TO :"xyz_db_user";
GRANT USAGE ON SCHEMA leeds TO :"derived_db_user";
GRANT SELECT ON ALL TABLES IN SCHEMA leeds TO :"xyz_db_user";
GRANT SELECT ON ALL TABLES IN SCHEMA leeds TO :"derived_db_user";
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA leeds
  FROM :"xyz_db_user";

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
  GRANT SELECT ON TABLES TO :"xyz_db_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"etl_db_user" IN SCHEMA leeds
  GRANT SELECT ON TABLES TO :"derived_db_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"etl_db_user" IN SCHEMA leeds
  REVOKE ALL PRIVILEGES ON SEQUENCES FROM :"xyz_db_user";

COMMIT;
SQL
