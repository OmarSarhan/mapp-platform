#!/usr/bin/env bash
# Seed the two demo source databases from the bundled sample data.
#
# The demo's point is that MAPP's own database holds no source data: census
# lives in one database, the operational layers in another, and both are
# reached over postgres_fdw. This copies rather than moves, so the bundled
# schema stays intact and the arrangement is reversible by dropping the two
# volumes.
#
# Federation requires C collation on every collatable column of an allowlisted
# relation, so each copied table is forced to pg_catalog."C" and then checked.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${MAPP_ENV_FILE:-${ROOT_DIR}/.env}"

dotenv_value() {
  # Same convention as scripts/verify.sh: read the authoritative value from
  # the env file rather than trusting the caller's exported environment.
  sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1
}

compose=(
  docker compose
  --project-directory "${ROOT_DIR}"
  --env-file "${ENV_FILE}"
  --file "${ROOT_DIR}/compose.yaml"
  --file "${ROOT_DIR}/compose.bundled-db.yaml"
  --file "${ROOT_DIR}/compose.federated-demo.yaml"
)

BUNDLED_USER="$(dotenv_value POSTGRES_USER)"
BUNDLED_DB="$(dotenv_value POSTGRES_DB)"
SOURCE_USER="$(dotenv_value SOURCE_POSTGRES_USER)"
READER_USER="$(dotenv_value SOURCE_READER_USER)"
CENSUS_DB="$(dotenv_value CENSUS_POSTGRES_DB)"
OPS_DB="$(dotenv_value OPS_POSTGRES_DB)"

# Census metadata travels with the measures: census_variables carries the ONS
# label for every tsNNN_NNNN column, which is what makes a themed layer
# describable rather than a wall of codes.
CENSUS_TABLES=(
  leeds.census_2021_england_oa
  leeds.census_variables
  leeds.census_datasets
  # The run-bookkeeping table backs the last-successful-run contract that
  # verify now asserts against this database rather than the packaged one.
  leeds._census_etl_runs
)
OPS_TABLES=(
  leeds.bus_stops
  leeds.definitive_paths
  leeds.smoke_control_orders
  leeds.planning_applications_recent
  # Back the ETL run-record report that verify now runs against this database.
  leeds._etl_layers
  leeds._etl_runs
)

seed_one() {
  local service="$1" database="$2"
  shift 2
  local tables=("$@")

  printf 'Seeding %s with %d table(s)...\n' "${service}" "${#tables[@]}"

  # The source containers install openssl and generate a certificate before
  # postgres starts, and their healthcheck is a socket-local pg_isready that
  # can report healthy while docker-entrypoint-initdb.d is still running. Wait
  # for the server itself rather than for the container.
  "${compose[@]}" exec -T "${service}" sh -c 'until pg_isready -q; do sleep 1; done'

  "${compose[@]}" exec -T "${service}" psql \
    --set ON_ERROR_STOP=1 --username "${SOURCE_USER}" --dbname "${database}" \
    --command "CREATE SCHEMA IF NOT EXISTS leeds;" >/dev/null

  local dump_args=()
  local table
  for table in "${tables[@]}"; do
    dump_args+=(--table="${table}")
  done

  "${compose[@]}" exec -T db pg_dump \
    --username "${BUNDLED_USER}" --dbname "${BUNDLED_DB}" \
    "${dump_args[@]}" --no-owner --no-privileges --clean --if-exists \
    | "${compose[@]}" exec -T "${service}" psql \
        --set ON_ERROR_STOP=1 --username "${SOURCE_USER}" \
        --dbname "${database}" >/dev/null

  for table in "${tables[@]}"; do
    "${compose[@]}" exec -T "${service}" psql \
      --set ON_ERROR_STOP=1 --username "${SOURCE_USER}" --dbname "${database}" \
      --set relation="${table}" <<'SQL' >/dev/null
-- psql does not interpolate :variables inside a dollar-quoted body, so the
-- relation is handed over as a GUC rather than pasted in by the shell.
SELECT pg_catalog.set_config('mapp.seed_relation', :'relation', false);
DO $body$
DECLARE
  column_record record;
  relation pg_catalog.regclass :=
    pg_catalog.current_setting('mapp.seed_relation')::pg_catalog.regclass;
BEGIN
  FOR column_record IN
    SELECT a.attname,
           pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type
    FROM pg_catalog.pg_attribute AS a
    WHERE a.attrelid = relation
      AND a.attnum > 0
      AND NOT a.attisdropped
      AND a.attcollation <> 0
  LOOP
    EXECUTE pg_catalog.format(
      'ALTER TABLE %s ALTER COLUMN %I TYPE %s COLLATE pg_catalog."C"',
      relation::pg_catalog.text,
      column_record.attname,
      column_record.data_type
    );
  END LOOP;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_attribute AS a
    JOIN pg_catalog.pg_collation AS co ON co.oid = a.attcollation
    JOIN pg_catalog.pg_namespace AS n ON n.oid = co.collnamespace
    WHERE a.attrelid = relation
      AND a.attnum > 0
      AND NOT a.attisdropped
      AND a.attcollation <> 0
      AND (n.nspname, co.collname) <> ('pg_catalog', 'C')
  ) THEN
    RAISE EXCEPTION 'seeded collatable columns must use pg_catalog.C: %', relation;
  END IF;
END
$body$;
SQL
  done

  "${compose[@]}" exec -T "${service}" psql \
    --set ON_ERROR_STOP=1 --username "${SOURCE_USER}" --dbname "${database}" \
    --set reader_user="${READER_USER}" <<'SQL' >/dev/null
GRANT USAGE ON SCHEMA leeds TO :"reader_user";
GRANT SELECT ON ALL TABLES IN SCHEMA leeds TO :"reader_user";
SQL

  local counts
  counts="$("${compose[@]}" exec -T "${service}" psql --tuples-only --no-align \
    --username "${SOURCE_USER}" --dbname "${database}" \
    --command "SELECT c.relname || '=' || c.reltuples::bigint
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'leeds' AND c.relkind = 'r' ORDER BY c.relname" | tr '\n' ' ')"
  printf '  %s: %s\n' "${service}" "${counts}"
}

seed_one census-db "${CENSUS_DB}" "${CENSUS_TABLES[@]}"
seed_one ops-db "${OPS_DB}" "${OPS_TABLES[@]}"

printf 'Seeded both demo sources and granted %s read access.\n' "${READER_USER}"
