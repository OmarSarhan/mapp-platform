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

# This harness seeds a throwaway source and mutates the federation registry,
# so it has no business touching a production deployment. It also has to
# recreate config-ui to pick up the overlay, and the compose array below
# cannot carry compose.production.yaml — that overlay demands the production
# host variables, which a test rig has no business requiring. Recreating the
# dashboard without it would silently drop CONFIG_ALLOWED_HOSTS and
# CONFIG_SECURE_COOKIES and leave it serving with development defaults.
# Refusing here rather than in bin/mapp covers direct invocation too.
DEPLOYMENT_ENVIRONMENT="$(dotenv_value MAPP_ENVIRONMENT)"
if [[ "${DEPLOYMENT_ENVIRONMENT}" == "production" ]]; then
  fail "federation-test is disabled when MAPP_ENVIRONMENT=production."
fi

# A dedicated alias so a failed run never leaves a half-configured source
# behind under a name an operator might be using.
ALIAS="e2e_probe"
PROBE_RELATION="leeds.e2e_probe_orders"
PROBE_SCHEMA="${PROBE_RELATION%%.*}"
# Per run, because the fixture is archived at the end and an archived asset
# cannot be re-registered -- the store refuses with asset_exists. A fixed
# identity would either leave a ready test profile in the catalogue forever or
# break every run after the first.
PROBE_SEMANTIC_RUN="$(date -u +%Y%m%d%H%M%S)$$"

compose=(
  docker compose
  --project-directory "${ROOT_DIR}"
  --env-file "${ENV_FILE}"
  --file "${ROOT_DIR}/compose.yaml"
  --file "${ROOT_DIR}/compose.bundled-db.yaml"
  --file "${ROOT_DIR}/compose.federation-test.yaml"
)
# Naming one opt-in overlay drops the other's FEDERATION_DBS_* from config-ui,
# so on a demo deployment this harness would recreate the service without the
# demo credentials and the startup verification pass would withdraw consumer
# access from both demo sources. The guard below catches that and refuses; it
# is more useful to compose the overlays than to be unrunnable wherever the
# demo is on.
if [[ -n "$(dotenv_value MAPP_DEMO_SOURCES)" ]]; then
  compose+=(--file "${ROOT_DIR}/compose.federated-demo.yaml")
fi

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

# Guarded by OWNS_ALIAS: the trap is armed before the harness has proved the
# alias is its own, and this tears down schemas and servers with CASCADE. An
# operator who happens to have registered a real alias under this name must
# never lose it to a test run that merely started.
OWNS_ALIAS=0
# Separate from OWNS_ALIAS. The alias is claimed before seeding, but the
# probe relation is only ours once the CREATE has actually succeeded -- and
# that CREATE deliberately has no preceding drop, so it fails when somebody
# else got there first. Without this the EXIT trap would then drop the
# relation this run never created.
OWNS_PROBE_RELATION=0
# Set once the semantic fixture exists, so cleanup only archives one this run
# actually registered.
OWNS_SEMANTIC_FIXTURE=0
# Whether source-db was already up before this run, so a rig an operator is
# using by hand survives; only a container this harness started gets removed.
SOURCE_DB_PREEXISTING=0
# The same principle applied to what seeding changes inside that rig. Granting
# the reader USAGE and leaving it granted permanently broadens a role in the
# operator's database, which is the opposite of preserving the rig; so each is
# recorded only when this run is the thing that introduced it, and undone only
# then.
PROBE_SCHEMA_CREATED=0
PROBE_USAGE_GRANTED=0

# Retirement archives rather than drops, so a repeat run would otherwise
# accumulate archived schemas and collide. Once ownership is established the
# harness removes its own leftovers outright.
remove_alias_state() {
  # Anchored regex, never LIKE. Two things make a glob wrong here. The alias
  # contains underscores, which LIKE treats as single-character wildcards, and
  # a trailing wildcard matches any longer alias, so "e2e_probe\_%" matches
  # e2e_probe_v2_srv, so the teardown would DROP ... CASCADE another live
  # source while the ownership guard -- which only ever checks the exact name
  # e2e_probe -- saw nothing to stop it. These patterns match this alias and
  # the archive shape retire() generates for it, and nothing else.
  local archive_suffix='_[0-9]{14}_[0-9a-f]{8}'
  local archived
  archived="$(platform_sql "
    SELECT string_agg(quote_ident(nspname), ',')
    FROM pg_catalog.pg_namespace
    WHERE nspname = 'source_${ALIAS}'
       OR nspname ~ '^retired[-_]${ALIAS}${archive_suffix}\$'
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
        WHERE srvname = '${ALIAS}_srv'
           OR srvname ~ '^retired[-_]${ALIAS}${archive_suffix}_srv\$'
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
  if [[ "${OWNS_PROBE_RELATION}" == "1" ]]; then
    source_sql "DROP TABLE IF EXISTS ${PROBE_RELATION}" >/dev/null 2>&1 || true
  fi
  # The table grant leaves with the table; the schema grant does not.
  if [[ "${PROBE_USAGE_GRANTED}" == "1" ]]; then
    source_sql "
      REVOKE USAGE ON SCHEMA ${PROBE_SCHEMA} FROM ${SOURCE_READER_USER}
    " >/dev/null 2>&1 || true
  fi
  # RESTRICT is the safety here: it refuses on a schema holding anything else,
  # so this can only remove one this run created and then emptied.
  if [[ "${PROBE_SCHEMA_CREATED}" == "1" ]]; then
    source_sql "DROP SCHEMA IF EXISTS ${PROBE_SCHEMA} RESTRICT" >/dev/null 2>&1 || true
  fi
}

# Puts back what this run took, and nothing else. Without it the advertised
# self-cleaning test leaves a source-db container and its volume running
# indefinitely on a deployment that had neither before.
cleanup() {
  if [[ "${OWNS_ALIAS}" == "1" ]]; then
    remove_alias_state
  fi
  # Only the container this harness started gets removed; one that was already
  # up belongs to an operator driving the rig by hand. The named
  # source_postgres_data volume is left alone either way — it is cheap to
  # reuse and may hold data they seeded themselves.
  if [[ "${SOURCE_DB_PREEXISTING}" == "0" ]]; then
    "${compose[@]}" rm --stop --force source-db >/dev/null 2>&1 || true
  else
    # The verification stage stops source-db to simulate an outage. Failing
    # between the stop and the restart would otherwise hand an operator back a
    # rig that was running when they lent it to us and is not now.
    "${compose[@]}" start source-db >/dev/null 2>&1 || true
  fi
  # Archive the semantic fixture through the lifecycle rather than leaving a
  # test profile in the catalogue. Archived assets are hidden from catalog and
  # search, which is why the identity is per run: the store refuses to
  # re-register an archived asset, so a fixed one would break every later run.
  if [[ "${OWNS_SEMANTIC_FIXTURE}" == "1" ]]; then
    "${compose[@]}" exec -T config-ui python3 - "${PROBE_SEMANTIC_RUN}" <<'ARCHIVE' >/dev/null 2>&1 || true
import sys
import app

run = sys.argv[1]
asset_id = f"federation-e2e-probe:{run}"
catalog = app.SEMANTIC.request(
    "/v1/catalog", actor="federation-e2e", scopes=["semantic:inspect"],
)
asset = next(
    (a for a in catalog.get("assets") or [] if a.get("id") == asset_id), None
)
if asset is not None:
    app.SEMANTIC.request(
        "/v1/events", method="POST",
        payload={
            "eventId": f"federation-e2e-probe-archive-{run}",
            "assetId": asset_id,
            "type": "archive",
            "generation": int(asset.get("generation", 1)) + 1,
            "generated": asset["generated"],
        },
        actor="federation-e2e", scopes=["semantic:admin"],
    )
ARCHIVE
  fi
  # config-ui is deliberately NOT recreated from the base files. The overlay's
  # only effect on it is to forward FEDERATION_DBS_LEEDS_EXT, and no base
  # compose file forwards any FEDERATION_DBS_<REF> at all — so stripping the
  # overlay would delete the connection reference every alias on the
  # deployment resolves through, leaving provisioned sources unusable and
  # scripts/verify.sh failing on "connectionRef is not configured". The next
  # `./bin/mapp serve` rebuilds config-ui from its own file list regardless,
  # so the composition corrects itself without this doing damage on the way.
}

trap cleanup EXIT

step "Bringing up the federation source"
if [[ -n "$("${compose[@]}" ps --quiet source-db 2>/dev/null)" ]]; then
  SOURCE_DB_PREEXISTING=1
fi
step "Checking the deployment's other sources would survive the recreate"
# Ordered before the `up -d` below, not after it. This reads the running
# container to learn what it would lose, so running it afterwards would
# compare a recreated container against itself, find nothing missing, and
# pass precisely when the loss had already happened.
#
# Recreating config-ui with this compose set gives it only the connection
# references these files forward. On a deployment whose other aliases get their
# FEDERATION_DBS_<REF> from a different overlay, those references would vanish
# from the container -- and the startup verification pass, which is global
# rather than scoped like the two explicit ticks below, would then find them
# unverifiable and withdraw their consumer access. Cleanup deliberately leaves
# the recreated container running, so that would outlast the run.
#
# Refusing is the right answer rather than trying to reconstruct another
# overlay's environment: the harness cannot know what it is not being given.
if [[ -n "$("${compose[@]}" ps --quiet config-ui 2>/dev/null)" ]]; then
  before_refs="$("${compose[@]}" exec -T config-ui sh -c \
    'env | sed -n "s/^\(FEDERATION_DBS_[A-Za-z0-9_]*\)=.*/\1/p" | sort' \
    2>/dev/null || true)"
  after_refs="$("${compose[@]}" config 2>/dev/null \
    | sed -n 's/^ *\(FEDERATION_DBS_[A-Za-z0-9_]*\):.*/\1/p' | sort -u || true)"
  lost="$(comm -23 <(printf '%s\n' "${before_refs}") \
                   <(printf '%s\n' "${after_refs}") | tr -d ' ' | grep -v '^$' || true)"
  if [[ -n "${lost}" ]]; then
    fail "Recreating config-ui with this harness's compose files would drop
connection references the running container has: $(echo "${lost}" | tr '\n' ' ')
The startup verification pass would then withdraw consumer access for the
aliases using them. Add the overlay that provides them to this harness, or run
it on a deployment that does not need it."
  fi
fi

# config-ui must be named here even when already running: compose only injects
# FEDERATION_DBS_LEEDS_EXT into the container when the overlay is applied to
# it, and without that Observe fails with federation.connection_ref_not_found.
"${compose[@]}" up -d --wait source-db config-ui >/dev/null
"${compose[@]}" exec -T source-db sh -c 'until pg_isready -q; do sleep 1; done'

step "Claiming the probe alias and relation"
# Both names are ones an operator could legitimately be using, and the cleanup
# below is irreversible, so the harness proves each is free before it takes
# ownership. The relation matters as much as the alias: it lives in the
# operator's source-db, which this harness deliberately leaves running when it
# found it running, so treating that rig as expendable while claiming to
# preserve it would be the worse contradiction. A crashed run (SIGKILL, where
# the EXIT trap never fired) is the one case that legitimately leaves residue;
# clearing that is an explicit decision a human makes, not something a test
# does on their behalf.
claimed=""
# These must fail closed. Suppressing errors into a default of 0 would turn a
# database that is merely unreachable for a moment into proof that the alias is
# free, and the teardown that follows drops schemas and servers with CASCADE.
# The registry table legitimately does not exist before the store first
# initialises, so that case is answered in SQL rather than by swallowing the
# error -- to_regclass returns NULL instead of raising.
if ! existing="$(platform_sql "
  SELECT CASE
           WHEN to_regclass('federation._aliases') IS NULL THEN 0
           ELSE (SELECT count(*) FROM federation._aliases
                  WHERE alias = '${ALIAS}')
         END
" 2>&1)"; then
  fail "Could not determine whether alias '${ALIAS}' already exists, so the
harness cannot prove the name is free: ${existing}"
fi
if [[ "${existing}" != "0" ]]; then
  claimed="federation alias '${ALIAS}'"
fi
if ! probe_exists="$(source_sql "
  SELECT count(*) FROM pg_catalog.pg_class AS c
  JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
  WHERE n.nspname = '${PROBE_SCHEMA}' AND c.relname = '${PROBE_RELATION#*.}'
" 2>&1)"; then
  fail "Could not determine whether '${PROBE_RELATION}' already exists, so the
harness cannot prove the relation is free: ${probe_exists}"
fi
if [[ "${probe_exists}" != "0" ]]; then
  claimed="${claimed:+${claimed} and }source relation '${PROBE_RELATION}'"
fi
if [[ -n "${claimed}" ]]; then
  if [[ "${MAPP_FEDERATION_E2E_RESET:-0}" != "1" ]]; then
    fail "This deployment already has ${claimed}. If that is residue from a
killed run, re-run with MAPP_FEDERATION_E2E_RESET=1 to clear it; otherwise
rename or retire it first — this harness drops the alias schema and server
with CASCADE, and drops the source relation outright."
  fi
  printf 'MAPP_FEDERATION_E2E_RESET=1: clearing pre-existing %s\n' "${claimed}"
  # Only an explicit reset authorises dropping a relation this run did not
  # create. Everywhere else the teardown waits for the CREATE to prove
  # ownership, so a create that loses a race cannot take somebody else's
  # relation with it.
  OWNS_PROBE_RELATION=1
fi
OWNS_ALIAS=1
remove_alias_state

step "Seeding the source with real geometry"
# Recorded before anything is granted: an operator whose schema already grants
# the reader USAGE keeps it, and one whose schema does not gets it back the way
# it was. The schema check has to come first because has_schema_privilege
# errors outright on a schema that does not exist.
# Fail closed, like the ownership guard above. Swallowing an error here is
# worse than it looks: a transient failure would record a pre-existing schema
# as one this run created and its existing grant as one this run added, so
# cleanup would revoke an operator's own USAGE and could drop their schema once
# the probe table left it empty.
if ! schema_present="$(source_sql "
  SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname = '${PROBE_SCHEMA}'
" 2>&1)"; then
  fail "Could not determine whether schema '${PROBE_SCHEMA}' exists on the
source, so the harness cannot tell what it would be creating: ${schema_present}"
fi
# These record what seeding *intends*, not what it owns. The flags that gate
# teardown are set only once the DDL below has actually run -- the same race
# that made the relation flag fail open applies here: another process can
# create the schema between this check and the seed, and a failed CREATE TABLE
# would then have the EXIT trap revoke that schema's pre-existing grant.
if [[ "${schema_present}" == "0" ]]; then
  probe_schema_absent=1
  probe_usage_missing=1
else
  probe_schema_absent=0
  if ! reader_usage="$(source_sql "
    SELECT has_schema_privilege('${SOURCE_READER_USER}', '${PROBE_SCHEMA}', 'USAGE')
  " 2>&1)"; then
    fail "Could not read '${SOURCE_READER_USER}' privileges on
'${PROBE_SCHEMA}', so the harness cannot tell what it would be granting:
${reader_usage}"
  fi
  if [[ "${reader_usage}" == "f" ]]; then
    probe_usage_missing=1
  else
    probe_usage_missing=0
  fi
fi

# A dedicated probe relation copied from the bundled data, so the harness
# never mutates a relation another alias is federating. Collations are pinned
# to C to satisfy the portability rule federation_capability.py enforces.
# No DROP before the CREATE. remove_alias_state has already cleared this run's
# own residue, so anything here now appeared after the claim -- and dropping it
# unconditionally would destroy a relation somebody else created in the window
# between the two. A plain CREATE fails loudly on collision instead, which is
# the right outcome for a name this harness has already proved was free.
source_sql "
  CREATE SCHEMA IF NOT EXISTS ${PROBE_SCHEMA};
  CREATE TABLE ${PROBE_RELATION} (
    object_id bigint PRIMARY KEY,
    reference text COLLATE pg_catalog.\"C\",
    geom public.geometry(MultiPolygon, 4326) NOT NULL
  );
  GRANT SELECT ON ${PROBE_RELATION} TO ${SOURCE_READER_USER};
" >/dev/null
OWNS_PROBE_RELATION=1
# The CREATE TABLE above succeeded, so this run is what put the schema and its
# contents here. Only now is it safe for teardown to undo either.
PROBE_SCHEMA_CREATED="${probe_schema_absent}"

# Issued only when the reader lacked USAGE outright. has_schema_privilege is
# true for access inherited through PUBLIC or role membership, so granting
# unconditionally would add a direct ACL entry the flag says nothing about --
# and cleanup, which revokes only what the flag recorded, would leave it
# behind, quietly strengthening the role if the inherited privilege is ever
# withdrawn.
if [[ "${probe_usage_missing}" == "1" ]]; then
  source_sql "
    GRANT USAGE ON SCHEMA ${PROBE_SCHEMA} TO ${SOURCE_READER_USER}
  " >/dev/null
  # Recorded after the grant, so a run that never reached this cannot revoke a
  # grant it did not make.
  PROBE_USAGE_GRANTED=1
fi

# Geometry is generated in the source database rather than copied from the
# packaged one. It used to be copied from leeds.smoke_control_orders, which no
# longer exists there: spatial data lives in source databases now, so the
# harness cannot assume the host holds any. Generated rather than synthetic
# squares -- ST_Buffer on a segmented line yields irregular rings with enough
# vertices that H3 expansion, ST_Intersects and the FDW round trip all do real
# work.
PROBE_ROWS=40
source_sql "
  INSERT INTO ${PROBE_RELATION} (object_id, reference, geom)
  SELECT
    n,
    'E2E-' || lpad(n::text, 4, '0'),
    ST_Multi(
      ST_Buffer(
        ST_Segmentize(
          ST_SetSRID(
            ST_MakeLine(
              ST_MakePoint(-1.6 + n * 0.004, 53.75 + n * 0.002),
              ST_MakePoint(-1.5 + n * 0.004, 53.82 + n * 0.002)
            ),
            4326
          ),
          0.004
        ),
        0.006 + (n % 5) * 0.001,
        3
      )
    )::public.geometry(MultiPolygon, 4326)
  FROM generate_series(1, ${PROBE_ROWS}) AS n
" >/dev/null

seeded="$(source_sql "SELECT count(*) FROM ${PROBE_RELATION}")"
[[ "${seeded}" == "${PROBE_ROWS}" ]] \
  || fail "probe relation holds ${seeded} rows, expected ${PROBE_ROWS}"
# Geometry that is valid and genuinely multi-vertex, so the comparisons below
# are exercising PostGIS rather than agreeing about trivia.
geometry_shape="$(source_sql "
  SELECT count(*) FILTER (WHERE NOT ST_IsValid(geom))
      || ' ' || min(ST_NPoints(geom))
  FROM ${PROBE_RELATION}
")"
read -r invalid_geometries min_vertices <<<"${geometry_shape}"
[[ "${invalid_geometries}" == "0" ]] \
  || fail "${invalid_geometries} generated probe geometries are invalid"
[[ "${min_vertices}" -ge 16 ]] \
  || fail "generated probe geometry is too simple: ${min_vertices} vertices"
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

step "Geometry survives the federation boundary, and H3 runs over it"
# The source relation is a copy of the bundled one, so the federated result
# must equal the local result exactly. That is a far stronger assertion than
# any fixed expected number: it proves the FDW path returns the same geometry
# PostGIS and H3 see locally, rather than merely returning something.
# What the federation layer must never do is alter geometry in transit, and
# the source database deliberately has no H3 extension -- it models a real
# external source, which would not have MAPP's -- so the comparison is on the
# geometry itself rather than on cells computed twice. Byte-for-byte EWKB
# digests over the same rows, one side read through postgres_fdw from another
# database, the other read in the database that owns them.
federated_geometry="$(platform_sql "
  SET ROLE ${DERIVED_OWNER_ROLE};
  SELECT count(*)
      || ' ' || coalesce(
           md5(string_agg(encode(ST_AsEWKB(geom), 'hex'), ',' ORDER BY object_id)),
           ''
         )
  FROM source_${ALIAS}.e2e_probe_orders
" | tail -n 1)"
source_geometry="$(source_sql "
  SELECT count(*)
      || ' ' || coalesce(
           md5(string_agg(encode(ST_AsEWKB(geom), 'hex'), ',' ORDER BY object_id)),
           ''
         )
  FROM ${PROBE_RELATION}
" | tail -n 1)"
read -r federated_rows federated_digest <<<"${federated_geometry}"
read -r source_rows source_digest <<<"${source_geometry}"
[[ "${federated_rows}" == "${source_rows}" ]] \
  || fail "row count differs across the boundary: federated=${federated_rows} source=${source_rows}"
[[ -n "${federated_digest}" && "${federated_digest}" == "${source_digest}" ]] \
  || fail "geometry differs across the federation boundary"
printf 'geometry: %s rows identical byte-for-byte across the boundary\n' \
  "${federated_rows}"

# H3 over the foreign table, because that is the derived-layer workload and
# it is the host's own extension doing the work on rows it did not store.
federated_cells="$(platform_sql "
  SET ROLE ${DERIVED_OWNER_ROLE};
  SELECT count(*) FROM (
    SELECT DISTINCT public.h3_polygon_to_cells(geom, 8) AS cell
    FROM source_${ALIAS}.e2e_probe_orders
  ) AS expanded
" | tail -n 1)"
[[ "${federated_cells}" -gt 0 ]] || fail "federated H3 expansion produced no cells"
printf 'H3 r8 over the foreign table: %s cells\n' "${federated_cells}"

# The area-weighted census allocation that used to sit here joined
# leeds.census_2021_england_oa in the packaged database, which no longer holds
# spatial data. It is not rebuilt here: docker/demo-sources/layers.sh already
# runs the area-weighted-h3 recipe across source_census and source_ops, and
# scripts/verify.sh asserts the derived owner reads both -- the same workload
# against two real federated sources rather than one synthetic one.

verifier_tick() {
  # Scoped to this alias. The stage below stops the shared source, and an
  # unscoped pass would revoke every other alias pointing at it while teardown
  # only ever restores the probe.
  "${compose[@]}" exec -T config-ui python3 -c "
import app
print(app.verify_federation_sources(only='${ALIAS}'))
" 2>/dev/null
}

step "Group labels round-trip through the real registry"
# The only test in either repository that runs the group SQL against a real
# database. The store tests drive MagicMock cursors, so they cannot see a
# missing ADD COLUMN, an array operator that does not exist, or a member count
# that counts the wrong rows -- and the migration is the one line whose
# absence breaks every alias read, not just this feature.
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
    dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)
).isoformat()
token, record = store.create_token(
    "federation-e2e-groups",
    expires,
    ["federation:register", "federation:observe"],
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
        with urllib.request.urlopen(request, timeout=60) as response:
            status, body = response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read().decode()
    parsed = json.loads(body) if body else {}
    if status != expect:
        raise SystemExit(
            f"{method} {path} returned {status}, expected {expect}: {body}"
        )
    return parsed


def alias_groups():
    for item in call("GET", "/api/federation/aliases")["aliases"]:
        if item["alias"] == alias:
            return item.get("groups")
    raise SystemExit(f"alias {alias} vanished from the registry")


try:
    # Reading the collection at all proves the ADD COLUMN ran: _SELECT_COLUMNS
    # names groups unconditionally, so without the migration this is an
    # UndefinedColumn 502 rather than a list.
    if alias_groups() != []:
        raise SystemExit("a freshly provisioned alias should carry no labels")

    call(
        "POST",
        "/api/federation/groups",
        {"name": "e2e_probe", "description": "End-to-end probe label."},
        expect=201,
    )
    duplicate = call(
        "POST", "/api/federation/groups", {"name": "e2e_probe"}, expect=409
    )
    if duplicate.get("code") != "federation.group_exists":
        raise SystemExit(f"unexpected duplicate code: {duplicate}")

    missing = call(
        "POST",
        f"/api/federation/aliases/{alias}/groups",
        {"groups": ["e2e_probe", "never_defined"]},
        expect=404,
    )
    if missing.get("code") != "federation.group_not_found":
        raise SystemExit(f"unexpected missing-group code: {missing}")
    if alias_groups() != []:
        raise SystemExit("a refused assignment must change nothing")

    call(
        "POST",
        f"/api/federation/aliases/{alias}/groups",
        {"groups": ["e2e_probe"]},
    )
    if alias_groups() != ["e2e_probe"]:
        raise SystemExit("the label did not survive the round trip")

    groups = {
        item["name"]: item
        for item in call("GET", "/api/federation/groups")["groups"]
    }
    if groups["e2e_probe"]["memberCount"] != 1:
        raise SystemExit(f"member count is wrong: {groups['e2e_probe']}")

    deleted = call(
        "POST", "/api/federation/groups/e2e_probe/delete", {}
    )["group"]
    if deleted["detachedAliases"] != [alias]:
        raise SystemExit(f"deletion did not report the detachment: {deleted}")
    if alias_groups() != []:
        raise SystemExit("array_remove left the label attached")
    print("group labels defined, assigned, counted, and detached on delete")
finally:
    # Best effort, because a failure between defining the group and deleting
    # it would otherwise leave e2e_probe in the registry and the next run
    # would fail at its own duplicate check -- the rig tripping over its own
    # residue, which is exactly what OWNS_PROBE_RELATION exists to prevent
    # elsewhere in this file.
    try:
        call("POST", "/api/federation/groups/e2e_probe/delete", {})
    except BaseException:
        pass
    store.revoke_token(record["id"])
PY

step "Seeding a semantic profile bound to the probe source"
# Registered directly rather than through the sync route, which would need
# SEMANTIC_SOURCE_ALLOWLIST widened permanently in .env for a test. This still
# proves the mirror against a real asset over real HTTP; the sync path itself
# is exercised by normal use.
#
# A fixed eventId makes it idempotent at the store: apply_event replays the
# stored response for a repeated id, so repeat runs do not accumulate assets
# and no disposal step is needed -- which matters, because the semantic store
# has no delete, only an operator-confirmed archive.
"${compose[@]}" exec -T config-ui python3 - "${ALIAS}" "${PROBE_RELATION#*.}" \
  "${PROBE_SEMANTIC_RUN}" <<'SEED'
import sys
import app
from semantic_sources import source_asset_id

alias, relation, run = sys.argv[1], sys.argv[2], sys.argv[3]
schema = f"source_{alias}"
event = {
    "eventId": f"federation-e2e-probe-semantic-{run}",
    "assetId": f"federation-e2e-probe:{run}",
    "type": "register",
    "generation": 1,
    "generated": {
        "kind": "foreign-table",
        "name": relation,
        "qualifiedName": f"{schema}.{relation}",
        "binding": {
            "adapter": "postgresql",
            "alias": "MAPP",
            "schema": schema,
            "relation": relation,
        },
        "fields": [
            {"name": "object_id", "type": "bigint", "nullable": False},
        ],
    },
}
result = app.SEMANTIC.request(
    "/v1/events", method="POST", payload=event,
    actor="federation-e2e", scopes=["semantic:admin"],
)
print(
    "semantic profile "
    + ("already present" if result["event"].get("idempotent") else "registered")
)
SEED

# What the catalog reports for that profile: available, unavailable, or missing.
probe_source_state() {
  "${compose[@]}" exec -T config-ui python3 - "${PROBE_SEMANTIC_RUN}" <<'STATE'
import sys
import app
from semantic_sources import source_asset_id

asset_id = f"federation-e2e-probe:{sys.argv[1]}"
catalog = app.SEMANTIC.request(
    "/v1/catalog", actor="federation-e2e", scopes=["semantic:inspect"],
)
for asset in catalog.get("assets") or []:
    if asset.get("id") == asset_id:
        print(asset.get("sourceState") or "available")
        break
else:
    print("missing")
STATE
}

# A previous run retires this alias, which correctly leaves the asset flagged.
# The alias has just been provisioned and is active, so one pass settles the
# mirror to match -- and it does so through the feature itself rather than the
# harness asserting a state it has forced.
verifier_tick >/dev/null
OWNS_SEMANTIC_FIXTURE=1

seeded_state="$(probe_source_state)"
[[ "${seeded_state}" == "available" ]] \
  || fail "a pass did not settle the seeded profile to available (got ${seeded_state})"

step "Periodic verification revokes on outage and restores on recovery"
# The behaviour the timer exists for, and the one that justified accepting
# observe()'s revoke semantics for the periodic path: an outage costs access
# automatically, and recovery returns it automatically. Asserted rather than
# argued, because the whole design rests on the second half being true.
#
# The tick is called directly instead of waiting out the interval. That runs
# the real function the thread runs; waiting 15 minutes would test the clock.

alias_status() {
  platform_sql "SELECT status FROM federation._aliases WHERE alias = '${ALIAS}'"
}

owner_can_read() {
  platform_sql "
    SELECT has_schema_privilege('${DERIVED_OWNER_ROLE}',
                                'source_${ALIAS}', 'USAGE')
  "
}

[[ "$(alias_status)" == "active" ]] || fail "alias was not active before the outage"
[[ "$(owner_can_read)" == "t" ]] || fail "derived owner could not read before the outage"

"${compose[@]}" stop source-db >/dev/null 2>&1
printf 'source stopped; running a verification pass
'
verifier_tick >/dev/null

outage_status="$(alias_status)"
outage_access="$(owner_can_read)"
[[ "${outage_status}" == "unavailable" ]]   || fail "an unreachable source left the alias ${outage_status}, expected unavailable"
[[ "${outage_access}" == "f" ]]   || fail "an unreachable source kept the derived owner readable"
outage_semantics="$(probe_source_state)"
[[ "${outage_semantics}" == "unavailable" ]] \
  || fail "the semantic profile reads ${outage_semantics} during an outage, expected unavailable"
printf 'outage: status=%s, access revoked, semantics=%s\n' \
  "${outage_status}" "${outage_semantics}"

"${compose[@]}" start source-db >/dev/null 2>&1
"${compose[@]}" exec -T source-db sh -c 'until pg_isready -q; do sleep 1; done'
printf 'source recovered; running a verification pass
'
verifier_tick >/dev/null

recovered_status="$(alias_status)"
recovered_access="$(owner_can_read)"
[[ "${recovered_status}" == "active" ]]   || fail "recovery left the alias ${recovered_status}, expected active"
[[ "${recovered_access}" == "t" ]]   || fail "recovery did not restore the derived owner grant"
recovered_semantics="$(probe_source_state)"
[[ "${recovered_semantics}" == "available" ]] \
  || fail "the semantic profile stayed ${recovered_semantics} after recovery"
printf 'recovery: status=%s, access restored, semantics=%s\n' \
  "${recovered_status}" "${recovered_semantics}"

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
printf 'replacement refusal, semantic degradation and recovery, '
printf 'retirement, archival and privilege audit verified '
printf 'against a real source.\n'
