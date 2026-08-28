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
  probe="$(api GET "/api/federation/aliases/$1" || true)"
  existing="$(printf '%s' "${probe}" | jqp "print((d.get('alias') or {}).get('status') or d.get('code'))")"
  if [ "${existing}" != "federation.alias_not_found" ]; then
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
  '["leeds.bus_stops","leeds.definitive_paths","leeds.smoke_control_orders","leeds.planning_applications_recent"]' \
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
sync_one source_census census_2021_england_oa
sync_one source_census census_variables
for relation in bus_stops definitive_paths smoke_control_orders planning_applications_recent; do
  sync_one source_ops "${relation}"
done

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
