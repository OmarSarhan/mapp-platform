#!/usr/bin/env bash
# Rebuild the two-source federated demo from a seeded pair of source databases.
#
# Run ./docker/demo-sources/seed.sh first. This takes it from "two databases
# holding data" to "a map served from them", so the arrangement survives this
# machine: register both aliases, observe, profile the exposed relations,
# provision, build the derived layers, and put them on the map.
#
# Idempotent. Every step tolerates its own prior success, so a partial run can
# simply be repeated.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${MAPP_ENV_FILE:-${ROOT_DIR}/.env}"
RECIPES="$(dirname "${BASH_SOURCE[0]}")/recipes"

dotenv_value() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1; }

compose=(
  docker compose
  --project-directory "${ROOT_DIR}"
  --env-file "${ENV_FILE}"
  --file "${ROOT_DIR}/compose.yaml"
  --file "${ROOT_DIR}/compose.bundled-db.yaml"
  --file "${ROOT_DIR}/compose.federated-demo.yaml"
)

fail() { printf '%s\n' "$*" >&2; exit 1; }

# The configuration service is reached through Caddy, which routes by host.
CONFIG_SITE="$(dotenv_value CONFIG_SITE | tr ',' '\n' | head -n 1 | tr -d ' ')"
CONFIG_HOST="${CONFIG_SITE#*://}"
HTTP_PORT="$(dotenv_value HTTP_PORT)"
BASE="http://localhost:${HTTP_PORT:-3000}"

# There is no CLI path for minting a token: POST /api/admin/tokens needs an
# administrator session. The control store is the supported seam and re-reads
# auth.json on every call, so the running service sees this without a restart.
mint_token() {
  "${compose[@]}" exec -T config-ui python - <<'PY'
import datetime as dt, os
from pathlib import Path
from control_plane import ControlStore

store = ControlStore(Path(os.environ["CONTROL_DIR"]))
expires = (
    dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=45)
).isoformat()
token, record = store.create_token(
    "demo-layers",
    expires,
    [
        "inspect",
        "propose",
        "apply",
        "reload",
        "derive",
        "semantic:inspect",
        "semantic:source",
        "semantic:generate",
        "semantic:propose",
        "semantic:apply",
        "semantic:data",
        "federation:register",
        "federation:observe",
        "federation:provision",
    ],
)
print(token + " " + record["id"])
PY
}

revoke_token() {
  "${compose[@]}" exec -T config-ui python - "$1" <<'PY' >/dev/null 2>&1 || true
import os, sys
from pathlib import Path
from control_plane import ControlStore
ControlStore(Path(os.environ["CONTROL_DIR"])).revoke_token(sys.argv[1])
PY
}

api() { # method path [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "${body}" ]; then
    curl -sS --fail-with-body -X "${method}" -H "Host: ${CONFIG_HOST}" \
      -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
      --data-binary "${body}" "${BASE}${path}"
  else
    curl -sS --fail-with-body -X "${method}" -H "Host: ${CONFIG_HOST}" \
      -H "Authorization: Bearer ${TOKEN}" "${BASE}${path}"
  fi
}

jqp() { python3 -c "import json,sys; d=json.load(sys.stdin); $1"; }

read -r TOKEN TOKEN_ID < <(mint_token)
[ -n "${TOKEN:-}" ] || fail "could not mint a token; is config-ui running?"
trap 'revoke_token "${TOKEN_ID}"' EXIT

step() { printf '\n== %s\n' "$*"; }

# ---------------------------------------------------------------- register --
step "Registering the two demo sources"
register() { # alias display connectionRef relations-json classification
  local existing probe
  # The only call whose failure is the expected answer: an unregistered alias
  # is a 404 carrying federation.alias_not_found, which is what we are asking.
  # curl announces that 404 on stderr, and on a first run -- where every alias
  # is unregistered -- that is the first thing the operator sees, so silence
  # it here and read the answer from the body. A curl that fails for any other
  # reason still stops the script, at the jqp below, on an empty body.
  probe="$(api GET "/api/federation/aliases/$1" 2>/dev/null || true)"
  existing="$(printf '%s' "${probe}" | jqp "print((d.get('alias') or {}).get('status') or d.get('code'))")"
  if [ "${existing}" != "federation.alias_not_found" ]; then
    # An alias with this name is not necessarily this demo's alias. census and
    # ops are ordinary words, and adopting somebody else's source would
    # observe it, provision it, profile its relations and -- with semantics on
    # -- send its sample rows and column statistics to a model. The connection
    # reference is what identifies it: the demo owns exactly the two the
    # overlay supplies.
    local existing_ref
    existing_ref="$(printf '%s' "${probe}" \
      | jqp "print((d.get('alias') or {}).get('connectionRef') or '')")"
    if [ "${existing_ref}" != "$3" ]; then
      fail "alias '$1' already exists and points at connectionRef '${existing_ref}', not the demo's '$3'. Retire or rename it before running the demo."
    fi
    printf '  %-8s already registered (%s)\n' "$1" "${existing}"
    return 0
  fi
  api POST /api/federation/aliases "$(python3 -c "
import json,sys
print(json.dumps({
  'alias': sys.argv[1], 'displayName': sys.argv[2], 'kind': 'postgresql',
  'connectionRef': sys.argv[3], 'tlsPolicy': 'require',
  'allowedRelations': json.loads(sys.argv[4]),
  'dataHandlingClassification': sys.argv[5],
  'dataHandlingAcknowledged': True,
}))" "$1" "$2" "$3" "$4" "$5")" \
    | jqp "print('  $1 ->', d.get('code') or 'registered')"
}
register census "Census 2021 (federated)" CENSUS \
  '["leeds.census_2021_england_oa","leeds.census_variables"]' \
  "ONS Census 2021 open data, OGL v3."
register ops "Leeds operational data (federated)" OPS \
  '["leeds.bus_stops","leeds.definitive_paths","leeds.smoke_control_orders"]' \
  "Leeds council open data, OGL v3."

# ----------------------------------------------------------------- observe --
step "Observing both sources"
for alias in census ops; do
  api POST "/api/federation/aliases/${alias}/observe" '{}' \
    | jqp "a=d.get('alias') or {}; o=a.get('lastObservation') or {}; print(f\"  ${alias}: {d.get('code') or o.get('connectivity')} obs={a.get('lastObservationId')}\")"
done

# --------------------------------------------------------------- provision --
step "Provisioning (the only step that serves data)"
for alias in census ops; do
  observation="$(api GET "/api/federation/aliases/${alias}" | jqp "print((d.get('alias') or {}).get('lastObservationId'))")"
  # seed.sh replaces the source relations by design (pg_dump --clean), so on
  # any rebuild the physical identity and the schema fingerprint have both
  # moved and provisioning refuses without an explicit acknowledgement. That
  # refusal is correct -- it is the guard against a source being silently
  # repointed at a different database -- and running this script IS the
  # operator saying "rebuild the demo". It is acknowledged here and nowhere
  # else.
  api POST "/api/federation/aliases/${alias}/provision" \
    "{\"expectedObservationId\": ${observation}, \
      \"physicalRebindAcknowledged\": true, \
      \"schemaChangeAcknowledged\": true}" \
    | jqp "print(f\"  ${alias}: {d.get('code') or (d.get('alias') or {}).get('status')}\")"
done

# ---------------------------------------------------------------- semantic --
# Must follow provisioning, not precede it. Profiling reads the source_<alias>
# foreign tables, and provision() is what creates them and grants the consumer
# roles access -- so before it there is nothing to profile, and re-seeding an
# already-provisioned source withdraws that access until it is provisioned
# again. SEMANTIC_SOURCE_ALLOWLIST must already name source_census.* and
# source_ops.*; .env.example ships with them.
step "Profiling the exposed relations"
sync_one() {
  api POST /api/semantic/source/sync \
    "{\"alias\":\"MAPP\",\"schema\":\"$1\",\"relation\":\"$2\"}" \
    | jqp "a=d.get('asset') or {}; print(f\"  $1.$2: {d.get('code') or a.get('status')}\")"
}
# Describing a relation and its fields is what makes the catalogue readable
# rather than a list of column names, and it needs a model. On by default when
# GEMINI_APIKEY is set; ./bin/mapp demo --no-semantics turns it off, and an
# empty key skips it rather than failing, so the demo still builds without one.
# Every draft is generated with the bounded data context -- sample rows and
# column statistics -- rather than from metadata alone.
GEMINI_KEY="$(dotenv_value GEMINI_APIKEY)"
DESCRIBE_FIELD_LIMIT="${MAPP_DEMO_FIELD_LIMIT:-40}"
DESCRIBE=1
if [ "${MAPP_DEMO_SEMANTICS:-1}" = "0" ]; then
  DESCRIBE=0
  printf '  (descriptions disabled by --no-semantics)\n'
elif [ -z "${GEMINI_KEY}" ]; then
  DESCRIBE=0
  printf '  (no GEMINI_APIKEY configured; skipping descriptions)\n'
fi

describe_relations() { # schema relation [schema relation ...]
  MAPP_BASE="${BASE}" MAPP_HOST="${CONFIG_HOST}" MAPP_TOKEN="${TOKEN}" \
    MAPP_FIELD_LIMIT="${DESCRIBE_FIELD_LIMIT}" python3 - "$@" <<'PY'
import json, os, sys, urllib.error, urllib.request

BASE = os.environ["MAPP_BASE"]
LIMIT = int(os.environ["MAPP_FIELD_LIMIT"])
HEADERS = {
    "Host": os.environ["MAPP_HOST"],
    "Authorization": "Bearer " + os.environ["MAPP_TOKEN"],
    "Content-Type": "application/json",
}


def call(method, path, body=None):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    for name, value in HEADERS.items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Error bodies are flat in some handlers and nested under "error" in
        # others; normalise so one caller check covers both.
        payload = json.loads(error.read() or b"{}")
        nested = payload.get("error")
        payload["code"] = (
            payload.get("code")
            or (nested.get("code") if isinstance(nested, dict) else None)
            or "http_%d" % error.code
        )
        return payload


def fail(message):
    print("    " + message, file=sys.stderr)
    raise SystemExit(1)


def drafted_operations(asset_id, target):
    """Draft one annotation. Returns (operations, baseVersion), or (None, None)
    when the model reproduced what is already curated -- the idempotent case,
    which is how a repeated demo run reports an already-described relation."""
    result = call(
        "POST", "/api/semantic/generate",
        {
            "assetId": asset_id,
            "target": target,
            # The dashboard offers these per generation and defaults them off,
            # because sending rows and statistics to a model is a decision
            # about the data in front of you. The demo makes it for you: its
            # sources are published government open data, and a description
            # drafted from column names alone reads like a restatement of the
            # column names. sampleRows and statistics are both sent for both
            # kinds of target -- the server scopes the statistics to the one
            # column when the target is a field.
            "contextOptions": {"sampleRows": True, "statistics": True},
        },
    )
    if result.get("code") == "semantic.generation_no_change":
        return None, None
    draft = result.get("draft")
    if not isinstance(draft, dict):
        fail("generate %s: %s" % (target["kind"], result.get("code")))
    return draft["operations"], draft["baseVersion"]


def apply_operations(asset_id, base_version, operations, explanation):
    """Generation only drafts -- "proposalCreated": false. Nothing reaches the
    catalogue until the draft is checked, proposed and applied, so the demo
    applies its own drafts unreviewed. That is right for a showcase and wrong
    for curated content; the explanation recorded on each proposal says so."""
    request = {
        "assetId": asset_id,
        "baseVersion": base_version,
        "operations": operations,
        "explanation": explanation,
    }
    checked = call("POST", "/api/semantic/proposals/check", request)
    check = checked.get("check")
    if not isinstance(check, dict):
        fail("check: %s" % checked.get("code"))
    created = call(
        "POST", "/api/semantic/proposals",
        dict(request, fingerprint=check["fingerprint"]),
    )
    proposal = created.get("proposal") or {}
    if not proposal.get("id"):
        fail("propose: %s" % created.get("code"))
    applied = call(
        "POST", "/api/semantic/proposals/%s/apply" % proposal["id"],
        {"confirmed": True},
    )
    if (applied.get("proposal") or {}).get("state") != "applied":
        fail("apply: %s" % applied.get("code"))


arguments = sys.argv[1:]
for schema, relation in zip(arguments[::2], arguments[1::2]):
    synced = call(
        "POST", "/api/semantic/source/sync",
        {"alias": "MAPP", "schema": schema, "relation": relation},
    )
    asset = synced.get("asset") or {}
    asset_id = asset.get("id")
    if not asset_id:
        fail("%s.%s: %s" % (schema, relation, synced.get("code")))
    fields = [
        field for field in (asset.get("generated") or {}).get("fields") or []
        if field.get("id")
    ]
    # One model call per field, sequentially. census_2021_england_oa has 470
    # columns, which would take hours and is not what a demo is for, so wide
    # relations get their table described and their fields left to the
    # structural profile. The cap is announced rather than applied silently.
    over_limit = len(fields) > LIMIT
    targets = [{"kind": "table"}] + ([] if over_limit else [
        {"kind": "field", "fieldId": field["id"]} for field in fields
    ])
    operations = []
    base_version = None
    for target in targets:
        drafted, version = drafted_operations(asset_id, target)
        if drafted is None:
            continue
        if base_version is None:
            base_version = version
        elif version != base_version:
            # Nothing is applied inside this loop, so the asset version cannot
            # move under it. If it did, something else is writing concurrently
            # and the accumulated operations no longer describe one state.
            fail("%s.%s: asset changed mid-description" % (schema, relation))
        operations.extend(drafted)
    if operations:
        apply_operations(asset_id, base_version, operations, (
            "Gemini drafts for the demo showcase, applied without review by "
            "./bin/mapp demo. Treat them as a starting point, not as curated "
            "content."
        ))
    scope = "table" if over_limit else "table and %d fields" % len(fields)
    print("    %s.%s: %s%s" % (
        schema, relation,
        ("%s described" % scope) if operations else "already described",
        (", %d fields over the %d limit not described" % (len(fields), LIMIT))
        if over_limit else "",
    ))
PY
}

sync_one source_census census_2021_england_oa
sync_one source_census census_variables
for relation in bus_stops definitive_paths smoke_control_orders; do
  sync_one source_ops "${relation}"
done

if [ "${DESCRIBE}" = "1" ]; then
  step "Describing the relations and their fields"
  describe_relations \
    source_census census_2021_england_oa \
    source_census census_variables \
    source_ops bus_stops \
    source_ops definitive_paths \
    source_ops smoke_control_orders
fi

# ----------------------------------------------------------------- derived --
step "Building derived layers from the federated relations"
for recipe in "${RECIPES}"/*.json; do
  name="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['name'])" "${recipe}")"
  if api GET "/api/derived-layers/${name}" 2>/dev/null | grep -q '"derivedLayer"'; then
    printf '  %-32s already exists\n' "${name}"
    continue
  fi
  # The recipe planner resolves a ready semantic source, constructs the
  # scope-bounded allocation query and runs the create preflight. It applies
  # no mutation: the returned createRequest is submitted separately.
  plan="$(api POST /api/derived-layers/recipes/area-weighted-h3/plan "$(cat "${recipe}")")"
  create="$(printf '%s' "${plan}" | python3 -c "
import json,sys
d = json.load(sys.stdin)
if d.get('code'):
    sys.stderr.write('  plan refused: %s %s\n' % (d['code'], (d.get('error') or '')[:160]))
    raise SystemExit(1)
print(json.dumps(d['recipePlan']['createRequest']))")"
  api POST /api/derived-layers "${create}" \
    | jqp "print(f\"  {d.get('code') or (d.get('derivedLayer') or {}).get('name')}\")"
done

# --------------------------------------------------------------- workspace --
step "Publishing the map layers"
revision="$(api GET /api/workspace | jqp "print(d.get('revision'))")"
proposal="$(python3 -c "
import json,sys
ops = json.load(open(sys.argv[1]))
print(json.dumps({
  'revision': sys.argv[2],
  'operations': [{'op': 'set', 'path': '/locale/layers/' + k, 'value': v}
                 for k, v in ops.items()],
  'explanation': 'Publish the two-source federated demo layers.',
}))" "$(dirname "${BASH_SOURCE[0]}")/workspace-demo.json" "${revision}")"

check="$(api POST /api/proposals/check "${proposal}")"
fingerprint="$(printf '%s' "${check}" | python3 -c "
import json,sys
c = (json.load(sys.stdin).get('check') or {})
if not c.get('valid'):
    sys.stderr.write('  workspace preflight failed: %s\n' % (c.get('errors') or c))
    raise SystemExit(1)
print(c['checkFingerprint'])")"

bound="$(python3 -c "
import json,sys
b = json.loads(sys.argv[1]); b['checkFingerprint'] = sys.argv[2]
print(json.dumps(b))" "${proposal}" "${fingerprint}")"
identifier="$(api POST /api/proposals "${bound}" | jqp "print((d.get('proposal') or {}).get('id') or d.get('code'))")"
api POST "/api/proposals/${identifier}/apply" '{"approved": true}' \
  | jqp "print('  apply:', d.get('code') or 'ok')"

printf '\nDemo rebuilt. Verify with ./bin/mapp verify and open the map.\n'
