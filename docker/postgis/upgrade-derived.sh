#!/bin/sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${ETL_DB_USER:?ETL_DB_USER is required}"
: "${XYZ_DB_USER:?XYZ_DB_USER is required}"
: "${DERIVED_DB_USER:?DERIVED_DB_USER is required}"
: "${DERIVED_DB_PASSWORD:?DERIVED_DB_PASSWORD is required}"
: "${FEDERATION_DB_USER:?FEDERATION_DB_USER is required}"
: "${FEDERATION_DB_PASSWORD:?FEDERATION_DB_PASSWORD is required}"

if [ "${FEDERATION_DB_USER}" = "${POSTGRES_USER}" ] \
  || [ "${FEDERATION_DB_USER}" = "${ETL_DB_USER}" ] \
  || [ "${FEDERATION_DB_USER}" = "${XYZ_DB_USER}" ] \
  || [ "${FEDERATION_DB_USER}" = "${DERIVED_DB_USER}" ]; then
  printf '%s\n' \
    'FEDERATION_DB_USER must be distinct from every administrator, ETL, runtime, and derived role.' >&2
  exit 2
fi

psql \
  --set ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set etl_db_user="${ETL_DB_USER}" \
  --set xyz_db_user="${XYZ_DB_USER}" \
  --set derived_db_user="${DERIVED_DB_USER}" \
  --set derived_db_password="${DERIVED_DB_PASSWORD}" \
  --set federation_db_user="${FEDERATION_DB_USER}" \
  --set federation_db_password="${FEDERATION_DB_PASSWORD}" <<'SQL'
BEGIN;

SELECT pg_catalog.set_config(
  'mapp.upgrade.federation_db_user', :'federation_db_user', true
);

DO $mapp_federation_role$
DECLARE
  federation_role record;
BEGIN
  SELECT
    role.oid,
    role.rolname,
    role.rolsuper,
    role.rolcreatedb,
    role.rolcreaterole,
    role.rolreplication,
    role.rolbypassrls
  INTO federation_role
  FROM pg_catalog.pg_roles AS role
  WHERE role.rolname = pg_catalog.current_setting(
    'mapp.upgrade.federation_db_user'
  );

  IF NOT FOUND THEN
    RETURN;
  END IF;

  IF federation_role.rolsuper
      OR federation_role.rolcreatedb
      OR federation_role.rolcreaterole
      OR federation_role.rolreplication
      OR federation_role.rolbypassrls THEN
    RAISE EXCEPTION
      'Refusing to alter existing federation role % with unsafe attributes',
      federation_role.rolname;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_auth_members AS membership
    WHERE membership.roleid = federation_role.oid
       OR membership.member = federation_role.oid
  ) THEN
    RAISE EXCEPTION
      'Refusing to alter existing federation role % with memberships',
      federation_role.rolname;
  END IF;
END
$mapp_federation_role$;

CREATE EXTENSION IF NOT EXISTS h3;
CREATE EXTENSION IF NOT EXISTS h3_postgis CASCADE;

-- Bring the PostGIS extension metadata up to the linked library. A volume
-- initialised against an older image keeps its recorded extension version
-- after the image's PostGIS moves, so extversion drifts behind
-- PostGIS_Lib_Version(). That is not cosmetic: federation compares the two
-- databases' postgisExtversion before declaring postgis shippable to
-- postgres_fdw, so a stale local extension silently disables spatial
-- pushdown and every predicate is evaluated after pulling the rows across.
DO $mapp_postgis_upgrade$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_extension AS installed
    JOIN pg_catalog.pg_available_extensions AS available
      ON available.name = installed.extname
    WHERE installed.extname LIKE 'postgis%'
      AND installed.extversion IS DISTINCT FROM available.default_version
  ) THEN
    -- PostGIS's own upgrade entry point: it orders the postgis, raster,
    -- topology and tiger extensions correctly, which separate ALTER
    -- EXTENSION statements do not.
    PERFORM public.postgis_extensions_upgrade();
  END IF;
END
$mapp_postgis_upgrade$;

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
          ::pg_catalog.regprocedure,
        'public.h3_lat_lng_to_cell(public.geometry,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_lat_lng_to_cell(public.geography,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_latlng_to_cell(public.geometry,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_latlng_to_cell(public.geography,pg_catalog.int4)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_geometry(public.h3index)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_geography(public.h3index)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_boundary_geometry(public.h3index)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_boundary_geography(public.h3index)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_boundary_geometry(public.h3index,pg_catalog.bool)'
          ::pg_catalog.regprocedure,
        'public.h3_cell_to_boundary_geography(public.h3index,pg_catalog.bool)'
          ::pg_catalog.regprocedure,
        'public.h3_cells_to_multi_polygon_geometry(public.h3index[])'
          ::pg_catalog.regprocedure,
        'public.h3_cells_to_multi_polygon_geography(public.h3index[])'
          ::pg_catalog.regprocedure
      ])
  LOOP
    EXECUTE pg_catalog.format(
      'ALTER FUNCTION %s SET search_path = pg_catalog, public',
      routine_oid::pg_catalog.regprocedure
    );
    hardened_count := hardened_count + 1;
  END LOOP;
  IF hardened_count <> 16 THEN
    RAISE EXCEPTION
      'Expected sixteen public h3_postgis SQL wrappers, found %',
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

SELECT format(
  'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
  :'federation_db_user',
  :'federation_db_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = :'federation_db_user'
)
\gexec

ALTER ROLE :"federation_db_user" LOGIN PASSWORD :'federation_db_password';

CREATE EXTENSION IF NOT EXISTS postgres_fdw;
REVOKE USAGE ON FOREIGN DATA WRAPPER postgres_fdw
  FROM :"xyz_db_user", :"derived_db_user";
GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO :"federation_db_user";
ALTER ROLE :"etl_db_user" CONNECTION LIMIT 4;
-- Keep existing volumes aligned with the two upstream 20-client XYZ pools,
-- eight admitted configuration reads, and two runtime-reader probe sessions.
ALTER ROLE :"xyz_db_user" CONNECTION LIMIT 50;
ALTER ROLE :"derived_db_user" CONNECTION LIMIT 4;
ALTER ROLE :"federation_db_user" CONNECTION LIMIT 4;

ALTER ROLE :"derived_db_user" SET search_path = pg_catalog, public;
ALTER ROLE :"federation_db_user" SET search_path = pg_catalog, public;

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

REVOKE CONNECT, TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE :"DBNAME"
  FROM :"xyz_db_user", :"derived_db_user", :"federation_db_user";
GRANT CONNECT, TEMPORARY ON DATABASE :"DBNAME" TO :"etl_db_user";
GRANT CONNECT ON DATABASE :"DBNAME"
  TO :"xyz_db_user", :"derived_db_user", :"federation_db_user";
REVOKE CREATE ON DATABASE :"DBNAME"
  FROM :"xyz_db_user", :"derived_db_user";
GRANT CREATE ON DATABASE :"DBNAME" TO :"federation_db_user";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

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

SELECT pg_catalog.set_config(
  'mapp.upgrade.derived_db_user', :'derived_db_user', true
);
SELECT pg_catalog.set_config(
  'mapp.upgrade.xyz_db_user', :'xyz_db_user', true
);

DO $mapp_federation$
DECLARE
  alias_record record;
  derived_role text := current_setting('mapp.upgrade.derived_db_user');
  federation_role text := current_setting('mapp.upgrade.federation_db_user');
  reader_role text := current_setting('mapp.upgrade.xyz_db_user');
  managed_object record;
  owner_name text;
  local_schema text;
  local_table text;
  remote_relation text;
BEGIN
  SELECT pg_catalog.pg_get_userbyid(namespace.nspowner)
    INTO owner_name
  FROM pg_catalog.pg_namespace AS namespace
  WHERE namespace.nspname = 'federation';

  SELECT relation.relname, owner_role.rolname AS object_owner
    INTO managed_object
  FROM pg_catalog.pg_class AS relation
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
  JOIN pg_catalog.pg_roles AS owner_role
    ON owner_role.oid = relation.relowner
  WHERE namespace.nspname = 'federation'
    AND relation.relname IN (
      '_aliases', '_observations', '_observations_id_seq',
      '_approvals', '_approvals_id_seq'
    )
    AND owner_role.rolname NOT IN (derived_role, federation_role)
  LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION
      'Refusing to migrate known federation object federation.% owned by %',
      managed_object.relname,
      managed_object.object_owner;
  END IF;

  IF owner_name IS NULL THEN
    EXECUTE pg_catalog.format(
      'CREATE SCHEMA federation AUTHORIZATION %I', federation_role
    );
  ELSIF owner_name NOT IN (derived_role, federation_role) THEN
    RAISE EXCEPTION
      'Refusing to take ownership of existing federation schema owned by %',
      owner_name;
  ELSIF owner_name = derived_role THEN
    EXECUTE pg_catalog.format(
      'ALTER SCHEMA federation OWNER TO %I', federation_role
    );
  END IF;

  FOR managed_object IN
    SELECT relation.relname, relation.relkind
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    JOIN pg_catalog.pg_roles AS owner_role
      ON owner_role.oid = relation.relowner
    WHERE namespace.nspname = 'federation'
      AND relation.relname IN (
        '_aliases', '_observations', '_observations_id_seq',
        '_approvals', '_approvals_id_seq'
      )
      AND relation.relkind IN ('r', 'p', 'S')
      AND owner_role.rolname = derived_role
  LOOP
    EXECUTE pg_catalog.format(
      'ALTER %s federation.%I OWNER TO %I',
      CASE WHEN managed_object.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
      managed_object.relname,
      federation_role
    );
  END LOOP;

  IF pg_catalog.to_regclass('federation._aliases') IS NOT NULL THEN
    ALTER TABLE federation._aliases
      ADD COLUMN IF NOT EXISTS accepted_schema_fingerprint text;
    ALTER TABLE federation._aliases
      ADD COLUMN IF NOT EXISTS accepted_physical_identity text;
    ALTER TABLE federation._aliases
      ADD COLUMN IF NOT EXISTS accepted_connection_identity text;
    ALTER TABLE federation._aliases
      ADD COLUMN IF NOT EXISTS last_observation_id bigint;

    UPDATE federation._aliases
    SET status = 'unavailable'
    WHERE provisioned_at IS NOT NULL
      AND status = 'active'
      AND (
        accepted_schema_fingerprint IS NULL
        OR accepted_physical_identity IS NULL
        OR accepted_connection_identity IS NULL
        OR last_observation_id IS NULL
      );

    FOR alias_record IN
      SELECT alias, allowed_relations, status
      FROM federation._aliases
      WHERE provisioned_at IS NOT NULL
    LOOP
      local_schema := 'source_' || alias_record.alias;
      SELECT pg_catalog.pg_get_userbyid(namespace.nspowner)
        INTO owner_name
      FROM pg_catalog.pg_namespace AS namespace
      WHERE namespace.nspname = local_schema;
      IF owner_name IS NOT NULL
          AND owner_name NOT IN (derived_role, federation_role) THEN
        RAISE EXCEPTION
          'Refusing to migrate known federation schema % owned by %',
          local_schema,
          owner_name;
      END IF;
      IF owner_name = derived_role THEN
        EXECUTE pg_catalog.format(
          'ALTER SCHEMA %I OWNER TO %I', local_schema, federation_role
        );
        owner_name := federation_role;
      END IF;
      IF owner_name = federation_role THEN
        IF alias_record.status = 'active' THEN
          EXECUTE pg_catalog.format(
            'GRANT USAGE ON SCHEMA %I TO %I, %I',
            local_schema, derived_role, reader_role
          );
        ELSE
          EXECUTE pg_catalog.format(
            'REVOKE USAGE ON SCHEMA %I FROM %I, %I',
            local_schema, derived_role, reader_role
          );
        END IF;
      END IF;

      FOREACH remote_relation IN ARRAY alias_record.allowed_relations
      LOOP
        local_table := split_part(remote_relation, '.', 2);
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)
          INTO owner_name
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = local_schema
          AND relation.relname = local_table
          AND relation.relkind = 'f';
        IF owner_name IS NOT NULL
            AND owner_name NOT IN (derived_role, federation_role) THEN
          RAISE EXCEPTION
            'Refusing to migrate known federation table %.% owned by %',
            local_schema,
            local_table,
            owner_name;
        END IF;
        IF owner_name = derived_role THEN
          EXECUTE pg_catalog.format(
            'ALTER FOREIGN TABLE %I.%I OWNER TO %I',
            local_schema, local_table, federation_role
          );
          owner_name := federation_role;
        END IF;
        IF owner_name = federation_role THEN
          IF alias_record.status = 'active' THEN
            EXECUTE pg_catalog.format(
              'GRANT SELECT ON TABLE %I.%I TO %I, %I',
              local_schema, local_table, derived_role, reader_role
            );
          ELSE
            EXECUTE pg_catalog.format(
              'REVOKE SELECT ON TABLE %I.%I FROM %I, %I',
              local_schema, local_table, derived_role, reader_role
            );
          END IF;
        END IF;
      END LOOP;

      SELECT pg_catalog.pg_get_userbyid(server.srvowner)
        INTO owner_name
      FROM pg_catalog.pg_foreign_server AS server
      WHERE server.srvname = alias_record.alias || '_srv';
      IF owner_name IS NOT NULL
          AND owner_name NOT IN (derived_role, federation_role) THEN
        RAISE EXCEPTION
          'Refusing to migrate known federation server % owned by %',
          alias_record.alias || '_srv',
          owner_name;
      END IF;
      IF owner_name = derived_role THEN
        EXECUTE pg_catalog.format(
          'ALTER SERVER %I OWNER TO %I',
          alias_record.alias || '_srv', federation_role
        );
        owner_name := federation_role;
      END IF;
      IF owner_name = federation_role THEN
        EXECUTE pg_catalog.format(
          'REVOKE USAGE ON FOREIGN SERVER %I FROM %I, %I',
          alias_record.alias || '_srv', derived_role, reader_role
        );
      END IF;
    END LOOP;
  END IF;
END
$mapp_federation$;

REVOKE ALL ON SCHEMA federation FROM PUBLIC;
REVOKE ALL ON SCHEMA federation
  FROM :"xyz_db_user", :"derived_db_user";
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA federation
  FROM :"xyz_db_user", :"derived_db_user";
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA federation
  FROM :"xyz_db_user", :"derived_db_user";

CREATE TABLE IF NOT EXISTS public.mapp_platform_layer_dependencies (
  alias text NOT NULL,
  relation text NOT NULL,
  CONSTRAINT mapp_platform_layer_dependencies_pkey
    PRIMARY KEY (alias, relation),
  CONSTRAINT mapp_platform_layer_dependencies_relation_format
    CHECK (
      relation ~ '^[a-zA-Z_][a-zA-Z0-9_]*(\\.[a-zA-Z_][a-zA-Z0-9_]*)?$'
    )
);

CREATE OR REPLACE FUNCTION public.mapp_sync_platform_layer_dependencies(
  p_alias text,
  p_relations jsonb
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $mapp_platform_sync$
BEGIN
  IF p_alias IS NULL OR btrim(p_alias) = '' THEN
    RAISE EXCEPTION 'mapp_sync_platform_layer_dependencies requires a non-empty alias.';
  END IF;

  DELETE FROM public.mapp_platform_layer_dependencies
  WHERE alias = p_alias;

  IF p_relations IS NULL THEN
    RETURN;
  END IF;

  INSERT INTO public.mapp_platform_layer_dependencies (alias, relation)
  SELECT
    p_alias,
    lower(replace(lower(jsonb_array_elements_text(p_relations)), '"', ''))
  WHERE p_relations IS NOT NULL
  ON CONFLICT (alias, relation) DO NOTHING;
END;
$mapp_platform_sync$;

CREATE OR REPLACE FUNCTION public.mapp_block_platform_layer_drops()
RETURNS event_trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $mapp_platform_guard$
DECLARE
  cmd record;
  normalized_relation text;
  object_relation text;
BEGIN
  IF pg_has_role(current_user, 'pg_database_owner', 'MEMBER') THEN
    RETURN;
  END IF;

  FOR cmd IN
    SELECT *
    FROM pg_event_trigger_ddl_commands()
  LOOP
    IF cmd.object_type NOT IN ('table', 'view', 'materialized view') THEN
      CONTINUE;
    END IF;

    object_relation := lower(replace(cmd.object_identity, '"', ''));
    IF object_relation IS NULL OR object_relation = '' THEN
      CONTINUE;
    END IF;
    IF cmd.schema_name IS NOT NULL AND btrim(cmd.schema_name) <> '' THEN
      normalized_relation := format('%s.%s', lower(cmd.schema_name), object_relation);
    ELSE
      normalized_relation := object_relation;
    END IF;

    IF EXISTS (
      SELECT 1
      FROM public.mapp_platform_layer_dependencies
      WHERE lower(relation) = normalized_relation
    ) THEN
      RAISE EXCEPTION USING
        MESSAGE = (
          format(
            'DROP is blocked by active platform references for %s; update the '
            'workspace or dependencies before deleting this relation.',
            normalized_relation
          )
        ),
        ERRCODE = '55006';
    END IF;
  END LOOP;
END;
$mapp_platform_guard$;

DROP EVENT TRIGGER IF EXISTS mapp_block_platform_layer_drops;
CREATE EVENT TRIGGER mapp_block_platform_layer_drops
ON ddl_command_end
WHEN TAG IN ('DROP TABLE', 'DROP VIEW', 'DROP MATERIALIZED VIEW')
EXECUTE FUNCTION public.mapp_block_platform_layer_drops();

GRANT EXECUTE ON FUNCTION public.mapp_sync_platform_layer_dependencies(
  text,
  jsonb
) TO PUBLIC;

COMMIT;
SQL

sh /usr/local/bin/mapp-prepare-spatial-indexes ensure
