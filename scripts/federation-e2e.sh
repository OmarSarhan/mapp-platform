#!/usr/bin/env bash
# End-to-end federation test against a real PostgreSQL source.
#
# Every federation defect found so far — a replaced source database passing
# provisioning, an unescaped % raising ProgrammingError before the query
# reached PostgreSQL, LOCK TABLE rejecting a foreign table — was found by
# driving real containers, never by the mocked unit suites. Those suites
# assert on generated SQL strings, which cannot fail the way SQL fails when
# PostgreSQL actually parses and plans it.
#
# So this exercises the real lifecycle over HTTP against a genuinely separate
# source database, and asserts on data rather than on statements.
#
# Run with: ./bin/mapp federation-test
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${MAPP_ENV_FILE:-${ROOT_DIR}/.env}"

dotenv_value() {
  local key="$1"
  local value
  value="$(sed -n "s/^${key}=//p" "${ENV_FILE}" | tail -n 1)"
  value="${value%$'\r'}"
  if [[ "${value}" == \"*\" && "${value}" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "${value}" == \'*\' && "${value}" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "${value}"
}

fail() {
  printf '\nFAIL: %s\n' "$1" >&2
  exit 1
}

step() {
  printf '\n== %s ==\n' "$1"
}

# A dedicated alias so a failed run never leaves a half-configured source
# behind under a name an operator might be using.
ALIAS="e2e_probe"
PROBE_RELATION="leeds.e2e_probe_orders"

compose=(
  docker compose
  --project-directory "${ROOT_DIR}"
  --env-file "${ENV_FILE}"
  --file "${ROOT_DIR}/compose.yaml"
  --file "${ROOT_DIR}/compose.bundled-db.yaml"
  --file "${ROOT_DIR}/compose.federation-test.yaml"
)

POSTGRES_USER="$(dotenv_value POSTGRES_USER)"
POSTGRES_DB="$(dotenv_value POSTGRES_DB)"
SOURCE_POSTGRES_USER="$(dotenv_value SOURCE_POSTGRES_USER)"
SOURCE_POSTGRES_DB="$(dotenv_value SOURCE_POSTGRES_DB)"
SOURCE_READER_USER="$(dotenv_value SOURCE_READER_USER)"
DERIVED_OWNER_ROLE="$(dotenv_value DERIVED_OWNER_ROLE)"
DERIVED_READER_ROLE="$(dotenv_value DERIVED_READER_ROLE)"

platform_sql() {
  "${compose[@]}" exec -T db \
    psql --quiet --no-align --tuples-only \
      --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" --command "$1"
}

source_sql() {
  "${compose[@]}" exec -T source-db \
    psql --quiet --no-align --tuples-only \
      --username "${SOURCE_POSTGRES_USER}" --dbname "${SOURCE_POSTGRES_DB}" \
      --command "$1"
}

# Retirement archives rather than drops, so a repeat run would otherwise
# accumulate archived schemas and collide. The harness owns this alias
# completely, so it removes its own leftovers outright.
cleanup() {
  local archived
  archived="$(platform_sql "
    SELECT string_agg(quote_ident(nspname), ',')
    FROM pg_catalog.pg_namespace
    WHERE nspname LIKE 'retired\\_${ALIAS}\\_%' OR nspname = 'source_${ALIAS}'
  " 2>/dev/null || true)"
  if [[ -n "${archived}" ]]; then
    local schema
    IFS=',' read -ra schemas <<<"${archived}"
    for schema in "${schemas[@]}"; do
      platform_sql "DROP SCHEMA IF EXISTS ${schema} CASCADE" >/dev/null 2>&1 || true
    done
  fi
  platform_sql "
    DO \$cleanup\$
    DECLARE
      target text;
    BEGIN
      FOR target IN
        SELECT srvname FROM pg_catalog.pg_foreign_server
        WHERE srvname LIKE '${ALIAS}\\_%' OR srvname LIKE 'retired\\_${ALIAS}\\_%'
      LOOP
        EXECUTE format('DROP SERVER IF EXISTS %I CASCADE', target);
      END LOOP;
    END
    \$cleanup\$
  " >/dev/null 2>&1 || true
  platform_sql "
    DELETE FROM federation._approvals WHERE alias = '${ALIAS}';
    DELETE FROM federation._observations WHERE alias = '${ALIAS}';
    DELETE FROM federation._aliases WHERE alias = '${ALIAS}';
  " >/dev/null 2>&1 || true
  source_sql "DROP TABLE IF EXISTS ${PROBE_RELATION}" >/dev/null 2>&1 || true
}

trap cleanup EXIT

step "Bringing up the federation source"
# config-ui must be named here even when already running: compose only injects
# FEDERATION_DBS_LEEDS_EXT into the container when the overlay is applied to
# it, and without that Observe fails with federation.connection_ref_not_found.
"${compose[@]}" up -d source-db config-ui >/dev/null
"${compose[@]}" exec -T source-db sh -c 'until pg_isready -q; do sleep 1; done'

step "Seeding the source with real geometry"
cleanup
# A dedicated probe relation copied from the bundled data, so the harness
# never mutates a relation another alias is federating. Collations are pinned
# to C to satisfy the portability rule federation_capability.py enforces.
source_sql "
  CREATE SCHEMA IF NOT EXISTS leeds;
  DROP TABLE IF EXISTS ${PROBE_RELATION};
  CREATE TABLE ${PROBE_RELATION} (
    object_id bigint PRIMARY KEY,
    reference text COLLATE pg_catalog.\"C\",
    geom public.geometry(MultiPolygon, 4326) NOT NULL
  );
  GRANT USAGE ON SCHEMA leeds TO ${SOURCE_READER_USER};
  GRANT SELECT ON ${PROBE_RELATION} TO ${SOURCE_READER_USER};
" >/dev/null

# Move real MultiPolygons across so the aggregation below exercises genuine
# geometry, not synthetic squares. Streamed through a plain SELECT rather than
# pg_dump so it does not depend on dump formatting across versions.
"${compose[@]}" exec -T db psql --quiet --tuples-only --no-align \
  --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" --command "
    COPY (
      SELECT object_id, council_reference, ST_AsEWKT(geom)
      FROM leeds.smoke_control_orders
    ) TO STDOUT
  " \
  | "${compose[@]}" exec -T source-db psql --quiet \
      --username "${SOURCE_POSTGRES_USER}" --dbname "${SOURCE_POSTGRES_DB}" \
      --command "COPY ${PROBE_RELATION} (object_id, reference, geom) FROM STDIN"

seeded="$(source_sql "SELECT count(*) FROM ${PROBE_RELATION}")"
expected="$(platform_sql "SELECT count(*) FROM leeds.smoke_control_orders")"
[[ "${seeded}" -gt 0 ]] || fail "probe relation seeded no rows"
# A partial transfer would still satisfy every assertion below while quietly
# comparing two different datasets, so the equality proof would prove nothing.
[[ "${seeded}" == "${expected}" ]] \
  || fail "seeded ${seeded} rows but the local source has ${expected}"
printf 'seeded %s polygons into the source database\n' "${seeded}"

step "Driving register, observe and provision over HTTP"
"${compose[@]}" exec -T config-ui python3 - "${ALIAS}" "${PROBE_RELATION}" <<'PY'
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from control_plane import ControlStore

alias, relation = sys.argv[1], sys.argv[2]
base = "http://127.0.0.1:8080"

# There is no CLI path for minting a token: POST /api/admin/tokens requires an
# administrator session, and config_admin.py only offers init/reset/revoke. The
# store is the supported seam, and it re-reads auth.json on every call, so the
# running server sees this immediately without a restart.
store = ControlStore(Path(os.environ["CONTROL_DIR"]))
expires = (
    dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
).isoformat()
token, record = store.create_token(
    "federation-e2e",
    expires,
    [
        "federation:register",
        "federation:provision",
        "federation:observe",
    ],
)


def call(method, path, payload=None, expect=200):
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status, body = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read().decode()
    parsed = json.loads(body) if body else {}
    if status != expect:
        raise SystemExit(
            f"{method} {path} returned {status}, expected {expect}: {body}"
        )
    return parsed


try:
    call(
        "POST",
        "/api/federation/aliases",
        {
            "alias": alias,
            "displayName": "Federation end-to-end probe",
            "kind": "postgresql",
            "connectionRef": "LEEDS_EXT",
            "tlsPolicy": "require",
            "allowedRelations": [relation],
            "dataHandlingClassification": "Test fixture, bundled open data.",
            "dataHandlingAcknowledged": True,
        },
        expect=201,
    )
    print("registered")

    observed = call("POST", f"/api/federation/aliases/{alias}/observe", {})
    observation = observed["alias"]["lastObservation"]
    observation_id = observed["alias"]["lastObservationId"]
    if observation["connectivity"] != "reachable":
        raise SystemExit(f"source unreachable: {observation}")
    if observation["schema"] != "current":
        raise SystemExit(f"schema not current: {observation}")
    print(f"observed: connectivity={observation['connectivity']} id={observation_id}")

    # Approval is bound to the exact observation that justified it, so a
    # concurrent re-observe between these two calls must invalidate it.
    provisioned = call(
        "POST",
        f"/api/federation/aliases/{alias}/provision",
        {"expectedObservationId": observation_id},
    )
    if provisioned["alias"]["status"] != "active":
        raise SystemExit(f"unexpected status: {provisioned['alias']}")
    print("provisioned: status=active")

    # A stale approval must be refused rather than silently reapplied.
    call(
        "POST",
        f"/api/federation/aliases/{alias}/provision",
        {"expectedObservationId": observation_id + 10_000},
        expect=400,
    )
    print("stale approval correctly refused")
finally:
    store.revoke_token(record["id"])
PY

step "Provisioning grants working access through the foreign table"
readable="$(platform_sql "
  SET ROLE ${DERIVED_OWNER_ROLE};
  SELECT count(*) FROM source_${ALIAS}.e2e_probe_orders
" | tail -n 1)"
[[ "${readable}" == "${seeded}" ]] \
  || fail "foreign table returned ${readable} rows, expected ${seeded}"
printf 'derived owner reads %s rows through the foreign table\n' "${readable}"

step "Cross-schema H3 aggregation: federated polygons against local data"
# The source relation is a copy of the bundled one, so the federated result
# must equal the local result exactly. That is a far stronger assertion than
# any fixed expected number: it proves the FDW path returns the same geometry
# PostGIS and H3 see locally, rather than merely returning something.
aggregation="$(platform_sql "
  SET ROLE ${DERIVED_OWNER_ROLE};
  WITH federated_cells AS (
    SELECT DISTINCT public.h3_polygon_to_cells(geom, 8) AS cell
    FROM source_${ALIAS}.e2e_probe_orders
  ),
  local_cells AS (
    SELECT DISTINCT public.h3_polygon_to_cells(geom, 8) AS cell
    FROM leeds.smoke_control_orders
  )
  SELECT (SELECT count(*) FROM federated_cells)
      || ' ' || (SELECT count(*) FROM local_cells)
      || ' ' || (SELECT count(*) FROM (
           SELECT cell FROM federated_cells
           EXCEPT SELECT cell FROM local_cells) AS d)
      || ' ' || (SELECT count(*) FROM (
           SELECT cell FROM local_cells
           EXCEPT SELECT cell FROM federated_cells) AS d)
" | tail -n 1)"
read -r federated_cells local_cells federated_only local_only <<<"${aggregation}"
[[ "${federated_cells}" -gt 0 ]] || fail "federated H3 expansion produced no cells"
[[ "${federated_cells}" == "${local_cells}" ]] \
  || fail "H3 cell count differs: federated=${federated_cells} local=${local_cells}"
[[ "${federated_only}" == "0" && "${local_only}" == "0" ]] \
  || fail "H3 cell sets differ: federated_only=${federated_only} local_only=${local_only}"
printf 'H3 r8: %s cells, identical federated and local\n' "${federated_cells}"

# The heavier, more representative workload: intersect the federated polygons
# with the 178k-row local census table and area-weight a measure into an
# equal-area projection. Intersecting in the native SRID keeps the census GiST
# index usable; transforming the indexed side inside the join instead turns
# this from seconds into minutes.
allocation="$(platform_sql "
  SET ROLE ${DERIVED_OWNER_ROLE};
  WITH pairs AS (
    SELECT 'federated' AS side, c.ts001_0001 AS measure,
           ST_Transform(ST_Intersection(f.geom, c.geom), 3035) AS overlap,
           ST_Transform(c.geom, 3035) AS whole
    FROM source_${ALIAS}.e2e_probe_orders AS f
    JOIN leeds.census_2021_england_oa AS c ON ST_Intersects(f.geom, c.geom)
    UNION ALL
    SELECT 'local', c.ts001_0001,
           ST_Transform(ST_Intersection(f.geom, c.geom), 3035),
           ST_Transform(c.geom, 3035)
    FROM leeds.smoke_control_orders AS f
    JOIN leeds.census_2021_england_oa AS c ON ST_Intersects(f.geom, c.geom)
  ),
  allocated AS (
    SELECT side, count(*) AS pairs,
           round(sum(measure * (ST_Area(overlap)
                 / NULLIF(ST_Area(whole), 0)))::numeric, 6) AS total
    FROM pairs GROUP BY side
  )
  SELECT (SELECT pairs FROM allocated WHERE side = 'federated')
      || ' ' || (SELECT total FROM allocated WHERE side = 'federated')
      || ' ' || (SELECT count(DISTINCT (pairs, total)) FROM allocated)
" | tail -n 1)"
read -r pair_count allocated_total distinct_results <<<"${allocation}"
[[ "${pair_count}" -gt 0 ]] || fail "census intersection produced no pairs"
[[ "${distinct_results}" == "1" ]] \
  || fail "area-weighted allocation differs between federated and local sources"
printf 'area-weighted census: %s pairs, %s allocated, identical both sides\n' \
  "${pair_count}" "${allocated_total}"

step "A replaced source database must not provision"
# Dropping and recreating the relation keeps every name and column identical
# but changes its physical identity. Re-observing afterwards means the
# staleness check alone cannot catch it — only the durable accepted identity
# can, which is exactly the regression this guards.
source_sql "
  DROP TABLE ${PROBE_RELATION};
  CREATE TABLE ${PROBE_RELATION} (
    object_id bigint PRIMARY KEY,
    reference text COLLATE pg_catalog.\"C\",
    geom public.geometry(MultiPolygon, 4326) NOT NULL
  );
  GRANT SELECT ON ${PROBE_RELATION} TO ${SOURCE_READER_USER};
" >/dev/null

"${compose[@]}" exec -T config-ui python3 - "${ALIAS}" <<'PY'
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from control_plane import ControlStore

alias = sys.argv[1]
base = "http://127.0.0.1:8080"
store = ControlStore(Path(os.environ["CONTROL_DIR"]))
expires = (
    dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
).isoformat()
token, record = store.create_token(
    "federation-e2e-rebind",
    expires,
    ["federation:provision", "federation:observe"],
)


def call(method, path, payload=None):
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or "{}")


try:
    status, observed = call("POST", f"/api/federation/aliases/{alias}/observe", {})
    if status != 200:
        raise SystemExit(f"observe after replacement failed: {status} {observed}")
    observation_id = observed["alias"]["lastObservationId"]

    status, body = call(
        "POST",
        f"/api/federation/aliases/{alias}/provision",
        {"expectedObservationId": observation_id},
    )
    if body.get("code") != "federation.physical_rebind_not_acknowledged":
        raise SystemExit(
            "a replaced source database was not refused: "
            f"{status} {body}"
        )
    print(f"replacement refused: {body['code']}")

    status, body = call(
        "POST",
        f"/api/federation/aliases/{alias}/provision",
        {
            "expectedObservationId": observation_id,
            "physicalRebindAcknowledged": True,
        },
    )
    if status != 200:
        raise SystemExit(f"acknowledged rebind failed: {status} {body}")
    print("acknowledged rebind accepted")
finally:
    store.revoke_token(record["id"])
PY

step "Retirement archives rather than drops"
"${compose[@]}" exec -T config-ui python3 - "${ALIAS}" <<'PY'
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from control_plane import ControlStore

alias = sys.argv[1]
base = "http://127.0.0.1:8080"
store = ControlStore(Path(os.environ["CONTROL_DIR"]))
expires = (
    dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
).isoformat()
token, record = store.create_token(
    "federation-e2e-retire", expires, ["federation:provision"]
)

request = urllib.request.Request(
    f"{base}/api/federation/aliases/{alias}/retire",
    method="POST",
    data=b"{}",
    headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    },
)
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.loads(response.read().decode() or "{}")
    print("retired:", body["alias"]["archivedSchema"])
except urllib.error.HTTPError as error:
    raise SystemExit(f"retire failed: {error.code} {error.read().decode()}")
finally:
    store.revoke_token(record["id"])
PY

# concat_ws with coalesce, not ||: a NULL from any subquery would otherwise
# propagate through the whole concatenation, leave every shell variable empty,
# and make the harness report whichever assertion happens to be checked first
# rather than the one that actually failed.
archive_state="$(platform_sql "
  SELECT concat_ws(' ',
    (SELECT count(*) FROM pg_catalog.pg_namespace
     WHERE nspname = 'source_${ALIAS}'),
    (SELECT count(*) FROM pg_catalog.pg_namespace AS n
     JOIN federation._aliases AS a ON a.archived_schema = n.nspname
     WHERE a.alias = '${ALIAS}'),
    (SELECT count(*) FROM pg_catalog.pg_class AS c
     JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
     JOIN federation._aliases AS a ON a.archived_schema = n.nspname
     WHERE a.alias = '${ALIAS}' AND c.relkind = 'f'),
    (SELECT count(*) FROM federation._aliases
     WHERE alias = '${ALIAS}' AND status = 'retired'),
    (SELECT coalesce(bool_and(
       NOT has_schema_privilege(r.role_name, n.nspname, 'USAGE')), false)::int
     FROM federation._aliases AS a
     JOIN pg_catalog.pg_namespace AS n ON n.nspname = a.archived_schema
     CROSS JOIN (VALUES ('${DERIVED_OWNER_ROLE}'), ('${DERIVED_READER_ROLE}'))
       AS r(role_name)
     WHERE a.alias = '${ALIAS}'),
    (SELECT count(*) FROM pg_catalog.pg_user_mappings
     WHERE srvname LIKE '${ALIAS}\_%'
        OR srvname LIKE 'retired\_${ALIAS}\_%')
  )
" | tail -n 1)"
read -r live_schema archived_schema archived_tables retired_rows revoked \
  retained_mappings <<<"${archive_state}"
[[ "${live_schema}" == "0" ]] || fail "the live source schema still exists after retirement"
[[ "${archived_schema}" == "1" ]] || fail "no archived schema was recorded"
[[ "${archived_tables}" -gt 0 ]] || fail "the archive dropped the foreign tables"
[[ "${retired_rows}" == "1" ]] || fail "the alias row was deleted rather than retained"
[[ "${revoked}" == "1" ]] || fail "a consumer role can still reach the archived schema"
# The archive keeps the audit trail, but must not keep working credentials for
# a decommissioned source.
[[ "${retained_mappings}" == "0" ]] \
  || fail "the archived server still holds ${retained_mappings} user mapping(s)"
printf 'archived: %s foreign table(s) preserved, both roles revoked, '\
'credentials dropped, row retained\n' "${archived_tables}"

step "Observing a retired alias must not resurrect it"
"${compose[@]}" exec -T config-ui python3 - "${ALIAS}" <<'PY'
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from control_plane import ControlStore

alias = sys.argv[1]
base = "http://127.0.0.1:8080"
store = ControlStore(Path(os.environ["CONTROL_DIR"]))
expires = (
    dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
).isoformat()
# Observe needs federation:provision because it opens a live outbound
# connection; reading the alias back needs federation:observe. Both.
token, record = store.create_token(
    "federation-e2e-terminal",
    expires,
    ["federation:provision", "federation:observe"],
)


def call(method, path, payload=None):
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or "{}")


try:
    status, body = call("POST", f"/api/federation/aliases/{alias}/observe", {})
    if status != 200:
        raise SystemExit(f"observe on a retired alias failed: {status} {body}")
    status, body = call("GET", f"/api/federation/aliases/{alias}")
    if status != 200:
        raise SystemExit(f"reading the alias back failed: {status} {body}")
    if body["alias"]["status"] != "retired":
        raise SystemExit(
            f"an observe resurrected a retired alias to {body['alias']['status']}"
        )
    print("still retired after observe")
finally:
    store.revoke_token(record["id"])
PY

step "The privilege audit accepts the archived state"
# The retired-alias branch of verify.sh is the part that has been wrong twice:
# once selecting a column the store creates lazily, once hard-failing on a
# state retirement deliberately produces. Both slipped through because the
# audit and the store were only ever exercised apart. Running the real audit
# here, while a retired alias with an archive is present, ties them together.
verify_log="$(mktemp)"
if ! "${ROOT_DIR}/scripts/verify.sh" >"${verify_log}" 2>&1; then
  printf 'verify.sh failed with a retired alias present:\n' >&2
  tail -n 20 "${verify_log}" >&2
  rm -f "${verify_log}"
  fail "the privilege audit rejected the archived state"
fi
rm -f "${verify_log}"
printf 'verify.sh passes with a retired, archived alias present\n'

printf '\nPASS: federation register, observe, provision, cross-schema H3 aggregation, '
printf 'replacement refusal, retirement, archival and privilege audit verified '
printf 'against a real source.\n'
