#!/bin/sh
set -eu

# This container plays the part of an independently-operated external
# source database for federation testing.
# It deliberately runs plain postgis/postgis, not MAPP's own H3-enabled image
# — a real external source would not have MAPP's extensions installed.

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${SOURCE_READER_USER:?SOURCE_READER_USER is required}"
: "${SOURCE_READER_PASSWORD:?SOURCE_READER_PASSWORD is required}"

psql \
  --set ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set reader_user="${SOURCE_READER_USER}" \
  --set reader_password="${SOURCE_READER_PASSWORD}" <<'SQL'
CREATE ROLE :"reader_user" LOGIN PASSWORD :'reader_password';
-- One packaged source can be read concurrently by the 50-session runtime
-- role, four derived jobs, four federation jobs, three semantic context reads,
-- and three additional source-reader sessions of headroom.
ALTER ROLE :"reader_user" CONNECTION LIMIT 64;
GRANT CONNECT ON DATABASE :"DBNAME" TO :"reader_user";
SQL
