#!/bin/sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

mode="${1:-prepare}"
if [ "$#" -gt 1 ]; then
  printf 'Usage: mapp-prepare-spatial-indexes [prepare|check]\n' >&2
  exit 2
fi
case "${mode}" in
  prepare|check) ;;
  *)
    printf 'Usage: mapp-prepare-spatial-indexes [prepare|check]\n' >&2
    exit 2
    ;;
esac

psql \
  --no-psqlrc \
  --set ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set spatial_index_mode="${mode}" <<'SQL'
SELECT pg_catalog.set_config(
  'mapp.spatial_index_mode', :'spatial_index_mode', false
) AS configured_mode
\gset

DO $mapp$
DECLARE
  spatial_column record;
  index_spec record;
  index_name text;
  index_oid pg_catalog.oid;
  native_index_exists boolean;
  managed_index_ready boolean;
  check_only boolean := pg_catalog.current_setting(
    'mapp.spatial_index_mode'
  ) = 'check';
BEGIN
  PERFORM pg_catalog.set_config('search_path', 'pg_catalog, public', true);

  FOR spatial_column IN
    SELECT
      namespace.nspname AS schema_name,
      relation.relname AS relation_name,
      relation.oid AS relation_oid,
      attribute.attname AS column_name,
      attribute.attnum AS column_number,
      type.typname AS spatial_type,
      public.postgis_typmod_srid(attribute.atttypmod) AS srid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    JOIN pg_catalog.pg_attribute AS attribute
      ON attribute.attrelid = relation.oid
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    JOIN pg_catalog.pg_type AS type
      ON type.oid = attribute.atttypid
    JOIN pg_catalog.pg_namespace AS type_namespace
      ON type_namespace.oid = type.typnamespace
    WHERE namespace.nspname IN ('leeds', 'derived_layers')
      AND relation.relkind IN ('r', 'p', 'm')
      AND type_namespace.nspname = 'public'
      AND type.typname IN ('geometry', 'geography')
    ORDER BY relation.oid, attribute.attnum
  LOOP
    IF spatial_column.srid <= 0 THEN
      RAISE EXCEPTION
        'Managed spatial column %.%.% needs a fixed positive SRID before the database is ready',
        spatial_column.schema_name,
        spatial_column.relation_name,
        spatial_column.column_name;
    END IF;

    SELECT pg_catalog.bool_or(
      access_method.amname = 'gist'
      AND index.indisvalid
      AND index.indisready
      AND index.indpred IS NULL
      AND index.indexprs IS NULL
      AND index.indnkeyatts = 1
      AND index.indkey[0] = spatial_column.column_number
    )
    INTO native_index_exists
    FROM pg_catalog.pg_index AS index
    JOIN pg_catalog.pg_class AS index_relation
      ON index_relation.oid = index.indexrelid
    JOIN pg_catalog.pg_am AS access_method
      ON access_method.oid = index_relation.relam
    WHERE index.indrelid = spatial_column.relation_oid;

    IF check_only AND NOT coalesce(native_index_exists, false) THEN
      RAISE EXCEPTION
        'Managed spatial column %.%.% has no valid native GiST index; run ./bin/mapp upgrade-derived',
        spatial_column.schema_name,
        spatial_column.relation_name,
        spatial_column.column_name;
    END IF;

    FOR index_spec IN
      SELECT purpose, expression
      FROM (
        VALUES
          (
            'geom',
            pg_catalog.format('%I', spatial_column.column_name)
          ),
          (
            'geom_cast',
            CASE WHEN spatial_column.spatial_type = 'geography'
              THEN pg_catalog.format(
                '(%I::public.geometry)', spatial_column.column_name
              )
            END
          ),
          (
            'geom_4326',
            CASE
              WHEN spatial_column.srid <= 0 THEN NULL
              WHEN spatial_column.spatial_type = 'geometry'
                   AND spatial_column.srid <> 4326
                THEN pg_catalog.format(
                  'public.ST_Transform(%I, 4326)',
                  spatial_column.column_name
                )
              WHEN spatial_column.spatial_type = 'geography'
                   AND spatial_column.srid <> 4326
                THEN pg_catalog.format(
                  'public.ST_Transform(%I::public.geometry, 4326)',
                  spatial_column.column_name
                )
            END
          ),
          (
            'geom_3857',
            CASE
              WHEN spatial_column.srid <= 0 THEN NULL
              WHEN spatial_column.spatial_type = 'geometry'
                   AND spatial_column.srid <> 3857
                THEN pg_catalog.format(
                  'public.ST_Transform(%I, 3857)',
                  spatial_column.column_name
                )
              WHEN spatial_column.spatial_type = 'geography'
                THEN pg_catalog.format(
                  'public.ST_Transform(%I::public.geometry, 3857)',
                  spatial_column.column_name
                )
            END
          ),
          (
            'geog_4326',
            CASE
              WHEN spatial_column.spatial_type <> 'geometry'
                   OR spatial_column.srid <= 0 THEN NULL
              WHEN spatial_column.srid = 4326
                THEN pg_catalog.format(
                  '(%I::public.geography)', spatial_column.column_name
                )
              ELSE pg_catalog.format(
                '(public.ST_Transform(%I, 4326)::public.geography)',
                spatial_column.column_name
              )
            END
          )
      ) AS requested(purpose, expression)
      WHERE expression IS NOT NULL
    LOOP
      IF index_spec.purpose = 'geom'
         AND coalesce(native_index_exists, false) THEN
        CONTINUE;
      END IF;

      index_name := pg_catalog.left(spatial_column.relation_name, 20)
        || '_' || pg_catalog.left(spatial_column.column_name, 14)
        || '_' || pg_catalog.left(index_spec.purpose, 14)
        || '_' || pg_catalog.substr(pg_catalog.md5(
          spatial_column.schema_name || '.'
          || spatial_column.relation_name || '.'
          || spatial_column.column_name || ':' || index_spec.purpose
        ), 1, 8) || '_gix';

      index_oid := pg_catalog.to_regclass(pg_catalog.format(
        '%I.%I', spatial_column.schema_name, index_name
      ));
      IF index_oid IS NULL THEN
        IF check_only THEN
          RAISE EXCEPTION
            'Managed spatial column %.%.% is missing its valid ready % GiST index; run ./bin/mapp upgrade-derived',
            spatial_column.schema_name,
            spatial_column.relation_name,
            spatial_column.column_name,
            index_spec.purpose;
        END IF;
        EXECUTE pg_catalog.format(
          'CREATE INDEX %I ON %I.%I USING gist (%s)',
          index_name,
          spatial_column.schema_name,
          spatial_column.relation_name,
          index_spec.expression
        );
      ELSE
        SELECT
          access_method.amname = 'gist'
          AND index.indisvalid
          AND index.indisready
          AND index.indpred IS NULL
        INTO managed_index_ready
        FROM pg_catalog.pg_index AS index
        JOIN pg_catalog.pg_class AS index_relation
          ON index_relation.oid = index.indexrelid
        JOIN pg_catalog.pg_am AS access_method
          ON access_method.oid = index_relation.relam
        WHERE index.indexrelid = index_oid;
        IF NOT coalesce(managed_index_ready, false) THEN
          RAISE EXCEPTION
            'Managed spatial index %.% exists but is not a valid ready non-partial GiST index',
            spatial_column.schema_name,
            index_name;
        END IF;
      END IF;
    END LOOP;

    IF NOT check_only THEN
      EXECUTE pg_catalog.format(
        'ANALYZE %I.%I',
        spatial_column.schema_name,
        spatial_column.relation_name
      );
    END IF;
  END LOOP;
END
$mapp$;
SQL
