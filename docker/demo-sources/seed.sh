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

SOURCE_USER="$(dotenv_value SOURCE_POSTGRES_USER)"
READER_USER="$(dotenv_value SOURCE_READER_USER)"
CENSUS_DB="$(dotenv_value CENSUS_POSTGRES_DB)"
OPS_DB="$(dotenv_value OPS_POSTGRES_DB)"

workspace_repair_extent() {
  local workspace="${ROOT_DIR}/var/workspace/workspace.json"
  if [[ ! -f "${workspace}" ]]; then
    workspace="${ROOT_DIR}/instance/workspace.seed.json"
  fi
  python3 - "${workspace}" <<'PY'
import json
import math
import sys

try:
    workspace = json.load(open(sys.argv[1], encoding="utf-8"))
except OSError:
    raise SystemExit(0)
extent = ((workspace.get("locale") or {}).get("extent") or {})
try:
    west = float(extent["west"])
    south = float(extent["south"])
    east = float(extent["east"])
    north = float(extent["north"])
except (KeyError, TypeError, ValueError):
    raise SystemExit(0)
if (
    all(math.isfinite(value) for value in (west, south, east, north))
    and -180 <= west <= east <= 180
    and -90 <= south <= north <= 90
):
    print(f"{west},{south},{east},{north}")
PY
}

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
  # Created by the ETL itself, and read by the run-record report that verify
  # runs against this database.
  leeds._etl_layers
  leeds._etl_runs
)

seed_one() {
  local service="$1" database="$2" password="$3" etl_command="$4"
  shift 4
  local tables=("$@")

  printf 'Loading %s from source...\n' "${service}"

  # The source containers install openssl and generate a certificate before
  # postgres starts, and their healthcheck is a socket-local pg_isready that
  # can report healthy while docker-entrypoint-initdb.d is still running. Wait
  # for the server itself rather than for the container.
  "${compose[@]}" exec -T "${service}" sh -c 'until pg_isready -q; do sleep 1; done'

  "${compose[@]}" exec -T "${service}" psql \
    --set ON_ERROR_STOP=1 --username "${SOURCE_USER}" --dbname "${database}" \
    --command "CREATE SCHEMA IF NOT EXISTS leeds;" >/dev/null

  local table
  # The ETL loads this source database directly, over the network from the
  # publishing authority, running as that database own administrator. MAPP does
  # not own these servers; the identity it uses INTO them stays the read-only
  # SOURCE_READER_USER granted below.
  # shellcheck disable=SC2086
  # --no-deps: the etl service declares depends_on the packaged database, which
  # was right when it loaded that database and is wrong now it loads a source.
  # Without it, seeding a source recreates and waits on MAPP own db container.
  local extra_env=()
  if [[ -n "${MAPP_CENSUS_REPAIR_EXTENT:-}" ]]; then
    extra_env+=(-e "MAPP_CENSUS_REPAIR_EXTENT=${MAPP_CENSUS_REPAIR_EXTENT}")
  fi
  "${compose[@]}" run --rm --build --no-deps \
    -e "DATABASE_URL=postgresql://${SOURCE_USER}:${password}@${service}:5432/${database}?sslmode=require" \
    "${extra_env[@]}" \
    etl ${etl_command} >/dev/null

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
  # Exact counts, not reltuples. reltuples is the planner's estimate and is
  # -1 on a table that has never been analysed, so this line reported
  # "bus_stops=-1" straight after loading 4,233 rows into it -- which reads as
  # a failure at the exact moment somebody is deciding whether the load
  # worked. The largest table here is 178,605 rows; counting is free.
  counts="$("${compose[@]}" exec -T "${service}" psql --tuples-only --no-align \
    --username "${SOURCE_USER}" --dbname "${database}" <<'COUNT_SQL' | tr '\n' ' '
SELECT c.relname || '=' || (
         xpath(
           '/row/count/text()',
           query_to_xml(
             format('SELECT count(*) AS count FROM leeds.%I', c.relname),
             false, true, ''
           )
         )
       )[1]::text::bigint
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'leeds' AND c.relkind = 'r'
ORDER BY c.relname;
COUNT_SQL
)"
  printf '  %s: %s\n' "${service}" "${counts}"
}

# The sample layers use the ETL default entrypoint and its layers.json; the
# census dataset has its own module and config.
seed_one ops-db "${OPS_DB}" "$(dotenv_value OPS_POSTGRES_PASSWORD)" "" \
  "${OPS_TABLES[@]}"
CENSUS_REPAIR_EXTENT="$(workspace_repair_extent)"
if [[ -n "${CENSUS_REPAIR_EXTENT}" ]]; then
  printf 'Limiting Census geometry repair to workspace extent %s.\n' \
    "${CENSUS_REPAIR_EXTENT}"
fi

MAPP_CENSUS_REPAIR_EXTENT="${CENSUS_REPAIR_EXTENT}" \
seed_one census-db "${CENSUS_DB}" "$(dotenv_value CENSUS_POSTGRES_PASSWORD)" \
  "python -m leeds_arcgis_etl.census_main --config /config/census.json" \
  "${CENSUS_TABLES[@]}"

printf 'Seeded both demo sources and granted %s read access.\n' "${READER_USER}"
