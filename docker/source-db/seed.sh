#!/usr/bin/env bash
set -euo pipefail

# Seeds the federation-test "source-db" service with a real copy of one
# small, already-verified Leeds table, so the FDW test rig moves genuine
# geometry data rather than a synthetic fixture. Reproducible and safe to
# re-run — schema/table creation is idempotent, data is replaced wholesale.
#
# Usage: ./docker/source-db/seed.sh
# Requires: source-db already running (see compose.federation-test.yaml)
# and the bundled db already loaded with sample ETL data.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${MAPP_ENV_FILE:-${ROOT_DIR}/.env}"

compose=(docker compose --project-directory "${ROOT_DIR}" --env-file "${ENV_FILE}" \
  --file "${ROOT_DIR}/compose.yaml" --file "${ROOT_DIR}/compose.bundled-db.yaml" \
  --file "${ROOT_DIR}/compose.federation-test.yaml")

# .env allows unquoted values with commas/spaces (fine for Compose's own
# parser) that break a plain `source` — extract only the keys this script
# needs, the same way bin/mapp's dotenv_value() does.
dotenv_value() {
  sed -n "s/^${1}=//p" "${ENV_FILE}" | tail -n 1
}

POSTGRES_USER="$(dotenv_value POSTGRES_USER)"
POSTGRES_DB="$(dotenv_value POSTGRES_DB)"
SOURCE_POSTGRES_USER="$(dotenv_value SOURCE_POSTGRES_USER)"
SOURCE_POSTGRES_DB="$(dotenv_value SOURCE_POSTGRES_DB)"
SOURCE_READER_USER="$(dotenv_value SOURCE_READER_USER)"

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${SOURCE_POSTGRES_USER:?SOURCE_POSTGRES_USER is required}"
: "${SOURCE_POSTGRES_DB:?SOURCE_POSTGRES_DB is required}"
: "${SOURCE_READER_USER:?SOURCE_READER_USER is required}"

"${compose[@]}" exec -T source-db psql \
  --set ON_ERROR_STOP=1 \
  --username "${SOURCE_POSTGRES_USER}" \
  --dbname "${SOURCE_POSTGRES_DB}" \
  --command "CREATE SCHEMA IF NOT EXISTS leeds;"

"${compose[@]}" exec -T db pg_dump \
  --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
  --table=leeds.smoke_control_orders --no-owner --no-privileges --clean --if-exists \
  | "${compose[@]}" exec -T source-db psql \
      --set ON_ERROR_STOP=1 \
      --username "${SOURCE_POSTGRES_USER}" --dbname "${SOURCE_POSTGRES_DB}"

"${compose[@]}" exec -T source-db psql \
  --set ON_ERROR_STOP=1 \
  --username "${SOURCE_POSTGRES_USER}" \
  --dbname "${SOURCE_POSTGRES_DB}" \
  --set reader_user="${SOURCE_READER_USER}" <<'SQL'
GRANT USAGE ON SCHEMA leeds TO :"reader_user";
GRANT SELECT ON leeds.smoke_control_orders TO :"reader_user";
SQL

printf 'Seeded source-db with leeds.smoke_control_orders and granted %s read access.\n' \
  "${SOURCE_READER_USER}"
