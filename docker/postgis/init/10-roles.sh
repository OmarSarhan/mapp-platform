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
: "${FEDERATION_DB_USER:?FEDERATION_DB_USER is required}"
: "${FEDERATION_DB_PASSWORD:?FEDERATION_DB_PASSWORD is required}"
: "${SEMANTIC_DB_USER:?SEMANTIC_DB_USER is required}"
: "${SEMANTIC_DB_PASSWORD:?SEMANTIC_DB_PASSWORD is required}"
: "${SEMANTIC_READER_DB_USER:?SEMANTIC_READER_DB_USER is required}"
: "${SEMANTIC_READER_DB_PASSWORD:?SEMANTIC_READER_DB_PASSWORD is required}"

if [ "${FEDERATION_DB_USER}" = "${POSTGRES_USER}" ] \
  || [ "${FEDERATION_DB_USER}" = "${ETL_DB_USER}" ] \
  || [ "${FEDERATION_DB_USER}" = "${XYZ_DB_USER}" ] \
  || [ "${FEDERATION_DB_USER}" = "${DERIVED_DB_USER}" ]; then
  printf '%s\n' \
    'FEDERATION_DB_USER must be distinct from every administrator, ETL, runtime, and derived role.' >&2
  exit 2
fi

for semantic_role in "${SEMANTIC_DB_USER}" "${SEMANTIC_READER_DB_USER}"; do
  if [ "${semantic_role}" = "${POSTGRES_USER}" ] \
    || [ "${semantic_role}" = "${ETL_DB_USER}" ] \
    || [ "${semantic_role}" = "${XYZ_DB_USER}" ] \
    || [ "${semantic_role}" = "${DERIVED_DB_USER}" ] \
    || [ "${semantic_role}" = "${FEDERATION_DB_USER}" ]; then
    printf '%s\n' \
      'SEMANTIC_DB_USER and SEMANTIC_READER_DB_USER must be distinct from every administrator, ETL, runtime, derived, and federation role.' >&2
    exit 2
  fi
done

if [ "${SEMANTIC_DB_USER}" = "${SEMANTIC_READER_DB_USER}" ]; then
  printf '%s\n' \
    'SEMANTIC_READER_DB_USER must be distinct from SEMANTIC_DB_USER: the reader is the read-only backstop for the CRUD role.' >&2
  exit 2
fi

psql \
  --set ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set etl_db_user="${ETL_DB_USER}" \
  --set etl_db_password="${ETL_DB_PASSWORD}" \
  --set xyz_db_user="${XYZ_DB_USER}" \
  --set xyz_db_password="${XYZ_DB_PASSWORD}" \
  --set derived_db_user="${DERIVED_DB_USER}" \
  --set derived_db_password="${DERIVED_DB_PASSWORD}" \
  --set federation_db_user="${FEDERATION_DB_USER}" \
  --set federation_db_password="${FEDERATION_DB_PASSWORD}" \
  --set semantic_db_user="${SEMANTIC_DB_USER}" \
  --set semantic_db_password="${SEMANTIC_DB_PASSWORD}" \
  --set semantic_reader_db_user="${SEMANTIC_READER_DB_USER}" \
  --set semantic_reader_db_password="${SEMANTIC_READER_DB_PASSWORD}" <<'SQL'
CREATE ROLE :"etl_db_user" LOGIN PASSWORD :'etl_db_password';
CREATE ROLE :"xyz_db_user" LOGIN PASSWORD :'xyz_db_password';
CREATE ROLE :"derived_db_user" LOGIN PASSWORD :'derived_db_password';
CREATE ROLE :"federation_db_user"
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'federation_db_password';
CREATE ROLE :"semantic_db_user"
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'semantic_db_password';
CREATE ROLE :"semantic_reader_db_user"
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'semantic_reader_db_password';

ALTER ROLE :"etl_db_user" CONNECTION LIMIT 4;
ALTER ROLE :"xyz_db_user" CONNECTION LIMIT 32;
ALTER ROLE :"derived_db_user" CONNECTION LIMIT 4;
ALTER ROLE :"federation_db_user" CONNECTION LIMIT 4;
ALTER ROLE :"semantic_db_user" CONNECTION LIMIT 4;
ALTER ROLE :"semantic_reader_db_user" CONNECTION LIMIT 4;

ALTER ROLE :"derived_db_user" SET search_path = pg_catalog, public;
ALTER ROLE :"federation_db_user" SET search_path = pg_catalog, public;
ALTER ROLE :"semantic_db_user" SET search_path = pg_catalog, semantic;
ALTER ROLE :"semantic_reader_db_user" SET search_path = pg_catalog, semantic;

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

ALTER ROLE :"federation_db_user" SET work_mem = '16MB';
ALTER ROLE :"federation_db_user" SET hash_mem_multiplier = '1';
ALTER ROLE :"federation_db_user" SET maintenance_work_mem = '64MB';
ALTER ROLE :"federation_db_user" SET max_parallel_workers_per_gather = '2';
ALTER ROLE :"federation_db_user" SET temp_file_limit = '1GB';
ALTER ROLE :"federation_db_user" SET statement_timeout = '30min';
ALTER ROLE :"federation_db_user" SET transaction_timeout = '35min';
ALTER ROLE :"federation_db_user" SET lock_timeout = '5s';
ALTER ROLE :"federation_db_user" SET idle_in_transaction_session_timeout = '1min';

-- Catalogue CRUD, not spatial work: deliberately tighter than the derived
-- owner. read_snapshot() holds a REPEATABLE READ READ ONLY transaction for the
-- length of one paginated response, which transaction_timeout bounds.
ALTER ROLE :"semantic_db_user" SET work_mem = '4MB';
ALTER ROLE :"semantic_db_user" SET hash_mem_multiplier = '1';
ALTER ROLE :"semantic_db_user" SET maintenance_work_mem = '16MB';
ALTER ROLE :"semantic_db_user" SET max_parallel_workers_per_gather = '0';
ALTER ROLE :"semantic_db_user" SET temp_file_limit = '64MB';
ALTER ROLE :"semantic_db_user" SET statement_timeout = '15s';
ALTER ROLE :"semantic_db_user" SET transaction_timeout = '30s';
ALTER ROLE :"semantic_db_user" SET lock_timeout = '5s';
ALTER ROLE :"semantic_db_user" SET idle_in_transaction_session_timeout = '1min';

ALTER ROLE :"semantic_reader_db_user" SET work_mem = '4MB';
ALTER ROLE :"semantic_reader_db_user" SET hash_mem_multiplier = '1';
ALTER ROLE :"semantic_reader_db_user" SET maintenance_work_mem = '16MB';
ALTER ROLE :"semantic_reader_db_user" SET max_parallel_workers_per_gather = '0';
ALTER ROLE :"semantic_reader_db_user" SET temp_file_limit = '64MB';
ALTER ROLE :"semantic_reader_db_user" SET statement_timeout = '15s';
ALTER ROLE :"semantic_reader_db_user" SET transaction_timeout = '30s';
ALTER ROLE :"semantic_reader_db_user" SET lock_timeout = '5s';
ALTER ROLE :"semantic_reader_db_user" SET idle_in_transaction_session_timeout = '1min';

REVOKE CONNECT, TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"DBNAME" TO :"etl_db_user";
GRANT CONNECT ON DATABASE :"DBNAME" TO :"xyz_db_user";
GRANT CONNECT ON DATABASE :"DBNAME" TO :"derived_db_user";
GRANT CONNECT ON DATABASE :"DBNAME" TO :"federation_db_user";
GRANT CONNECT ON DATABASE :"DBNAME" TO :"semantic_db_user";
GRANT CONNECT ON DATABASE :"DBNAME" TO :"semantic_reader_db_user";
REVOKE CREATE ON DATABASE :"DBNAME"
  FROM :"xyz_db_user", :"derived_db_user",
       :"semantic_db_user", :"semantic_reader_db_user";
GRANT CREATE ON DATABASE :"DBNAME" TO :"federation_db_user";

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA derived_layers AUTHORIZATION :"derived_db_user";
REVOKE ALL ON SCHEMA derived_layers FROM PUBLIC;
GRANT USAGE ON SCHEMA derived_layers TO :"xyz_db_user";

-- Federation alias registry, its observation history and group labels.
-- Not workspace-visible: no grant to xyz_db_user.
CREATE SCHEMA federation AUTHORIZATION :"federation_db_user";
REVOKE ALL ON SCHEMA federation FROM PUBLIC;
REVOKE ALL ON SCHEMA federation
  FROM :"xyz_db_user", :"derived_db_user",
       :"semantic_db_user", :"semantic_reader_db_user";

-- Semantic catalogue (semantic-service), replacing the SQLite store. Owned by
-- the CRUD role, which creates the tables through its own migrations; the
-- reader role is the structural backstop behind the API/CLI semantic scopes
-- and never gets more than SELECT. Not workspace-visible: no grant to
-- xyz_db_user.
CREATE SCHEMA semantic AUTHORIZATION :"semantic_db_user";
REVOKE ALL ON SCHEMA semantic FROM PUBLIC;
REVOKE ALL ON SCHEMA semantic
  FROM :"xyz_db_user", :"derived_db_user", :"federation_db_user";
GRANT USAGE ON SCHEMA semantic TO :"semantic_reader_db_user";
GRANT SELECT ON ALL TABLES IN SCHEMA semantic TO :"semantic_reader_db_user";
-- The schema is empty at init: the default privileges below, not the grant
-- above, are what keep the store's tables readable once it migrates them in.
ALTER DEFAULT PRIVILEGES FOR ROLE :"semantic_db_user" IN SCHEMA semantic
  GRANT SELECT ON TABLES TO :"semantic_reader_db_user";

-- Enables cross-database federation testing: one explicit postgres_fdw
-- source, provisioned on demand by config-ui/federation_store.py's
-- FederationAliasStore.provision(). Installing the extension and granting
-- FDW USAGE here is a one-time, superuser-only step; CREATE SERVER/CREATE
-- USER MAPPING/IMPORT FOREIGN SCHEMA happen later, per alias, under the
-- dedicated federation provisioner.
CREATE EXTENSION IF NOT EXISTS postgres_fdw;
REVOKE USAGE ON FOREIGN DATA WRAPPER postgres_fdw
  FROM :"xyz_db_user", :"derived_db_user",
       :"semantic_db_user", :"semantic_reader_db_user";
GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO :"federation_db_user";

SQL
