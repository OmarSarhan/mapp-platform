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

DO $mapp$
DECLARE
  routine_oid pg_catalog.oid;
  hardened_count pg_catalog.int4 := 0;
BEGIN
  PERFORM pg_catalog.set_config('search_path', 'pg_catalog', true);
  FOR routine_oid IN
    SELECT routine.oid
    FROM pg_catalog.pg_proc AS routine
    JOIN pg_catalog.pg_namespace AS routine_namespace
      ON routine_namespace.oid = routine.pronamespace
    JOIN pg_catalog.pg_depend AS extension_membership
      ON extension_membership.classid =
           'pg_catalog.pg_proc'::pg_catalog.regclass
     AND extension_membership.objid = routine.oid
     AND extension_membership.refclassid =
           'pg_catalog.pg_extension'::pg_catalog.regclass
     AND extension_membership.deptype = 'e'
    JOIN pg_catalog.pg_extension AS extension
      ON extension.oid = extension_membership.refobjid
    WHERE extension.extname = 'h3_postgis'
      AND extension.extnamespace = routine.pronamespace
      AND routine_namespace.nspname = 'public'
      AND routine.oid = ANY(ARRAY[
        'public.h3_polygon_to_cells(public.geometry,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_polygon_to_cells(public.geography,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_polygon_to_cells_experimental(public.geometry,pg_catalog.int4,pg_catalog.text)'
          ::pg_catalog.regprocedure,
        'public.h3_polygon_to_cells_experimental(public.geography,pg_catalog.int4,pg_catalog.text)'
          ::pg_catalog.regprocedure
      ])
  LOOP
    EXECUTE pg_catalog.format(
      'ALTER FUNCTION %s SET search_path = pg_catalog, public',
      routine_oid::pg_catalog.regprocedure
    );
    hardened_count := hardened_count + 1;
  END LOOP;
  IF hardened_count <> 4 THEN
    RAISE EXCEPTION
      'Expected four public h3_postgis polygon SQL wrappers, found %',
      hardened_count;
  END IF;
END
$mapp$;

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

sh /usr/local/bin/mapp-prepare-spatial-indexes ensure
