#!/bin/sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${ETL_DB_USER:?ETL_DB_USER is required}"
: "${ETL_DB_PASSWORD:?ETL_DB_PASSWORD is required}"
: "${XYZ_DB_USER:?XYZ_DB_USER is required}"
: "${XYZ_DB_PASSWORD:?XYZ_DB_PASSWORD is required}"
: "${DERIVED_DB_USER:?DERIVED_DB_USER is required}"
: "${DERIVED_DB_PASSWORD:?DERIVED_DB_PASSWORD is required}"

psql \
  --set ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set etl_db_user="${ETL_DB_USER}" \
  --set etl_db_password="${ETL_DB_PASSWORD}" \
  --set xyz_db_user="${XYZ_DB_USER}" \
  --set xyz_db_password="${XYZ_DB_PASSWORD}" \
  --set derived_db_user="${DERIVED_DB_USER}" \
  --set derived_db_password="${DERIVED_DB_PASSWORD}" <<'SQL'
CREATE ROLE :"etl_db_user" LOGIN PASSWORD :'etl_db_password';
CREATE ROLE :"xyz_db_user" LOGIN PASSWORD :'xyz_db_password';
CREATE ROLE :"derived_db_user" LOGIN PASSWORD :'derived_db_password';

GRANT CONNECT, TEMPORARY ON DATABASE :"DBNAME" TO :"etl_db_user";
GRANT CONNECT ON DATABASE :"DBNAME" TO :"xyz_db_user";
GRANT CONNECT ON DATABASE :"DBNAME" TO :"derived_db_user";

CREATE SCHEMA leeds AUTHORIZATION :"etl_db_user";
GRANT USAGE ON SCHEMA leeds TO :"xyz_db_user";
GRANT USAGE ON SCHEMA leeds TO :"derived_db_user";

CREATE SCHEMA derived_layers AUTHORIZATION :"derived_db_user";
REVOKE ALL ON SCHEMA derived_layers FROM PUBLIC;
GRANT USAGE ON SCHEMA derived_layers TO :"xyz_db_user";

ALTER DEFAULT PRIVILEGES FOR ROLE :"etl_db_user" IN SCHEMA leeds
  GRANT SELECT ON TABLES TO :"xyz_db_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"etl_db_user" IN SCHEMA leeds
  GRANT SELECT ON TABLES TO :"derived_db_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"etl_db_user" IN SCHEMA leeds
  GRANT USAGE, SELECT ON SEQUENCES TO :"xyz_db_user";

SQL
