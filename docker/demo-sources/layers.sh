#!/usr/bin/env bash
# Rebuild the saved demo workspace from a seeded pair of source databases.
#
# Run ./docker/demo-sources/seed.sh first. This takes it from "two databases
# holding data" to "a map served from them", so the arrangement survives this
# machine: register both aliases, observe, profile the exposed relations,
# provision, reconcile the workspace's derived layers, and publish the exact
# versioned map configuration.
#
# Idempotent. Every step tolerates its own prior success, so a partial run can
# simply be repeated.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${MAPP_ENV_FILE:-${ROOT_DIR}/.env}"
DEMO_DIR="$(dirname "${BASH_SOURCE[0]}")"
DERIVED_FIXTURES="${DEMO_DIR}/derived-layers"
DEMO_WORKSPACE="${DEMO_DIR}/workspace-demo.json"

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
import datetime as dt, os, secrets
from pathlib import Path
from control_plane import ControlStore

store = ControlStore(Path(os.environ["CONTROL_DIR"]))
expires = (
    # Four sequential derived mutations can each spend 30 minutes waiting for
    # admission and 60 minutes running. Leave the preceding bounded semantic
    # generation phase ample room too; the EXIT trap still revokes this token
    # as soon as the demo finishes.
    dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)
).isoformat()
token, record = store.create_token(
    "demo-layers-" + secrets.token_hex(8),
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

# Sweep credentials from an earlier interrupted demo as well as the token from
# this run. The name namespace is reserved by this harness, and revocation is
# idempotent.
# This closes the gap where the container minted a token but the shell died
# before it captured TOKEN_ID and armed its per-token cleanup.
revoke_demo_tokens() {
  "${compose[@]}" exec -T config-ui python - <<'PY' >/dev/null 2>&1 || true
import os
from pathlib import Path
from control_plane import ControlStore

store = ControlStore(Path(os.environ["CONTROL_DIR"]))
for record in store.list_tokens():
    name = record.get("name")
    if isinstance(name, str) and name.startswith("demo-layers-") \
            and not record.get("revoked"):
        store.revoke_token(record["id"])
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

TOKEN_ID=""
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"; if [[ -n "${TOKEN_ID}" ]]; then revoke_token "${TOKEN_ID}"; fi; revoke_demo_tokens' EXIT
read -r TOKEN TOKEN_ID < <(mint_token)
[ -n "${TOKEN:-}" ] || fail "could not mint a token; is config-ui running?"

step() { printf '\n== %s\n' "$*"; }

# Generic derived-layer planning deliberately resolves its spatial scope from
# the live workspace. The packaged seed and saved demo have the same scope, so
# clean and repeated demo runs are deterministic. Refuse a different live
# scope before mutating federation or derived state: silently planning against
# it would publish the saved map with truncated or misplaced derived output.
current_workspace="$(api GET /api/workspace)"
python3 - "${DEMO_WORKSPACE}" "${current_workspace}" <<'PY' || fail \
  "the live workspace scope differs from the saved demo; restore the packaged workspace scope before rebuilding the demo"
import json
import sys

saved = json.load(open(sys.argv[1], encoding="utf-8"))
response = json.loads(sys.argv[2])
current = response.get("workspace")
if not isinstance(current, dict):
    raise SystemExit("could not read the live workspace")

saved_locale = saved.get("locale") or {}
current_locale = current.get("locale") or {}
for label, expected, actual in (
    ("database", saved.get("dbs"), current.get("dbs")),
    ("extent", saved_locale.get("extent"), current_locale.get("extent")),
    ("view", saved_locale.get("view"), current_locale.get("view")),
):
    if actual != expected:
        print(
            "  Saved demo %s does not match the live workspace %s."
            % (label, label),
            file=sys.stderr,
        )
        raise SystemExit(1)
PY

scope_revision="$(printf '%s' "${current_workspace}" | jqp \
  "print(d.get('revision') or '')")"
[ -n "${scope_revision}" ] || fail "could not read the live workspace revision"
scope_response="$(api GET '/api/derived-layers/map-extent?locale=locale')"
DEMO_RESOLVED_SCOPE="$(printf '%s' "${scope_response}" | python3 -c "
import json,sys
scope=json.load(sys.stdin).get('spatialScope')
if not isinstance(scope, dict):
    raise SystemExit('the API could not resolve the saved demo extent')
print(json.dumps(scope, separators=(',', ':'))) ")"
latest_scope_revision="$(api GET /api/workspace | jqp \
  "print(d.get('revision') or '')")"
[ "${latest_scope_revision}" = "${scope_revision}" ] || fail \
  "the live workspace changed while the demo planning scope was being resolved"

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
# Fields per relation to describe. MAPP_DEMO_FIELD_LIMIT in .env is the single
# source of truth; empty means no cap. Fresh environments default it to 50
# because census_2021_england_oa alone has 470 columns and each field is a
# model call.
DESCRIBE_FIELD_LIMIT="$(dotenv_value MAPP_DEMO_FIELD_LIMIT)"
case "${DESCRIBE_FIELD_LIMIT}" in
  '') ;;
  *[!0-9]*) fail \
    "MAPP_DEMO_FIELD_LIMIT must be a positive whole number or empty, not '${DESCRIBE_FIELD_LIMIT}'" ;;
  *[1-9]*) ;;
  *) fail \
    "MAPP_DEMO_FIELD_LIMIT must be a positive whole number or empty, not '${DESCRIBE_FIELD_LIMIT}'" ;;
esac
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
import concurrent.futures
import json, os, shutil, sys, time, urllib.error, urllib.request

BASE = os.environ["MAPP_BASE"]
# Empty means no cap, so it has to survive int() rather than reach it.
LIMIT = int(os.environ["MAPP_FIELD_LIMIT"] or 0)
# Each generation holds one of the service's ten Gemini slots, and the
# eleventh is refused outright with semantic.generation_busy rather than
# queued -- the acquire is non-blocking. Stay under it.
WORKERS = 8
MAX_PROPOSAL_OPERATIONS = 100
RATE_LIMIT_RETRY_DELAYS = (5, 15, 45)
RETRYABLE_GENERATION_CODES = {
    "semantic.generation_rate_limited",
    "semantic.generation_busy",
    "http_429",
}
HEADERS = {
    "Host": os.environ["MAPP_HOST"],
    "Authorization": "Bearer " + os.environ["MAPP_TOKEN"],
    "Content-Type": "application/json",
}


class DemoSemanticError(RuntimeError):
    pass


class DemoGenerationRateLimited(DemoSemanticError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


ANNOTATION_KEYS = ("displayName", "description", "tags", "caveats")


def has_curated_annotation(value):
    return isinstance(value, dict) and any(
        key in value for key in ANNOTATION_KEYS
    )


class ProgressBar:
    """One in-place TTY bar or bounded newline snapshots for captured logs."""

    def __init__(self, total, limit_text, stream=sys.stdout):
        self.total = total
        self.limit_text = limit_text
        self.stream = stream
        self.completed = 0
        self.tty = stream.isatty()
        self.active = False
        self.last_bucket = -1
        self.last_length = 0

    def _limit_summary(self):
        if LIMIT > 0:
            return (
                "MAPP_DEMO_FIELD_LIMIT=%s; the first %s fields per relation "
                "are included" % (self.limit_text, self.limit_text)
            )
        return "MAPP_DEMO_FIELD_LIMIT is empty; all fields are included"

    def _line(self):
        percent = 100 if self.total == 0 else self.completed * 100 // self.total
        columns = shutil.get_terminal_size(fallback=(100, 24)).columns
        suffix = "%d/%d targets settled (%3d%%)" % (
            self.completed, self.total, percent,
        )
        available = columns - len("    Gemini descriptions [] ") - len(suffix)
        if available < 10:
            return "    Gemini descriptions " + suffix
        width = min(28, available)
        filled = width if self.total == 0 else width * self.completed // self.total
        return "    Gemini descriptions [%s%s] %s" % (
            "#" * filled,
            "-" * (width - filled),
            suffix,
        )

    def _render(self, *, final=False, force=False):
        percent = 100 if self.total == 0 else self.completed * 100 // self.total
        bucket = percent // 10
        if not self.tty and not force and not final and bucket <= self.last_bucket:
            return
        line = self._line()
        if self.tty:
            padding = " " * max(0, self.last_length - len(line))
            self.stream.write("\r" + line + padding)
            if final:
                self.stream.write("\n")
            self.last_length = len(line)
        else:
            self.stream.write(line + "\n")
        self.stream.flush()
        self.last_bucket = bucket
        if final:
            self.active = False

    def start(self):
        self.stream.write(
            "    Gemini descriptions: %d targets (%s; normally one model "
            "call each).\n"
            % (self.total, self._limit_summary())
        )
        self.stream.flush()
        self.active = True
        self._render(final=self.total == 0, force=True)

    def advance(self):
        self.completed += 1
        if self.completed > self.total:
            raise RuntimeError("semantic progress exceeded its planned total")
        self._render(final=self.completed == self.total)

    def close(self):
        if not self.active:
            return
        if self.tty:
            self.stream.write("\n")
        else:
            self.stream.write(
                "    Gemini descriptions stopped at %d/%d targets settled.\n"
                % (self.completed, self.total)
            )
        self.stream.flush()
        self.active = False

    def note(self, message):
        if self.tty and self.active:
            self.stream.write("\n")
            self.last_length = 0
        self.stream.write("    " + message + "\n")
        self.stream.flush()
        if self.tty and self.active:
            self._render(force=True)


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
    if result.get("code") in RETRYABLE_GENERATION_CODES:
        raise DemoGenerationRateLimited(result["code"])
    draft = result.get("draft")
    if not isinstance(draft, dict):
        raise DemoSemanticError(
            "generate %s: %s" % (target["kind"], result.get("code"))
        )
    return draft["operations"], draft["baseVersion"]


def retry_rate_limited(asset_id, target, progress, error):
    latest = error
    for retry_number, delay in enumerate(RATE_LIMIT_RETRY_DELAYS, start=1):
        progress.note(
            "Description request returned %s; waiting %d seconds before "
            "retry %d/%d."
            % (
                latest.code,
                delay,
                retry_number,
                len(RATE_LIMIT_RETRY_DELAYS),
            )
        )
        time.sleep(delay)
        try:
            return drafted_operations(asset_id, target)
        except DemoGenerationRateLimited as next_error:
            latest = next_error
    raise latest


def draft_serially(plan, index, target, drafts, progress, retry_state):
    try:
        drafted = drafted_operations(plan["asset_id"], target)
    except DemoGenerationRateLimited as error:
        retry_state["serial"] = True
        drafted = retry_rate_limited(
            plan["asset_id"], target, progress, error,
        )
    drafts[index] = drafted
    progress.advance()


def settle_worker_batch(plan, batch, pool, drafts, progress, retry_state):
    futures = {
        pool.submit(
            drafted_operations,
            plan["asset_id"],
            target,
        ): (index, target)
        for index, target in batch
    }
    rate_limited = []
    errors = []
    for future in concurrent.futures.as_completed(futures):
        index, target = futures[future]
        try:
            drafts[index] = future.result()
        except DemoGenerationRateLimited as error:
            rate_limited.append((index, target, error))
        except Exception as error:
            errors.append(error)
        else:
            progress.advance()
    if errors:
        raise errors[0]
    if not rate_limited:
        return

    # A simultaneous provider limit must not create eight independent retry
    # loops. Use one failed target as the recovery probe, then keep all later
    # generation serial for the rest of this demo run.
    retry_state["serial"] = True
    rate_limited.sort(key=lambda item: item[0])
    index, target, error = rate_limited[0]
    drafts[index] = retry_rate_limited(
        plan["asset_id"], target, progress, error,
    )
    progress.advance()
    for index, target, _error in rate_limited[1:]:
        draft_serially(
            plan, index, target, drafts, progress, retry_state,
        )


def chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def apply_operations(asset_id, base_version, operations, explanation):
    """Generation only drafts -- "proposalCreated": false. Nothing reaches the
    catalogue until the draft is checked, proposed and applied, so the demo
    applies its own drafts unreviewed. That is right for a showcase and wrong
    for curated content; the explanation recorded on each proposal says so."""
    for batch in chunks(operations, MAX_PROPOSAL_OPERATIONS):
        request = {
            "assetId": asset_id,
            "baseVersion": base_version,
            "operations": batch,
            "explanation": explanation,
        }
        checked = call("POST", "/api/semantic/proposals/check", request)
        check = checked.get("check")
        if not isinstance(check, dict):
            raise DemoSemanticError("check: %s" % checked.get("code"))
        created = call(
            "POST", "/api/semantic/proposals",
            dict(request, fingerprint=check["fingerprint"]),
        )
        proposal = created.get("proposal") or {}
        if not proposal.get("id"):
            raise DemoSemanticError("propose: %s" % created.get("code"))
        applied = call(
            "POST", "/api/semantic/proposals/%s/apply" % proposal["id"],
            {"confirmed": True},
        )
        if (applied.get("proposal") or {}).get("state") != "applied":
            raise DemoSemanticError("apply: %s" % applied.get("code"))
        asset = applied.get("asset") or {}
        next_version = asset.get("version")
        if isinstance(next_version, bool) or not isinstance(next_version, int):
            raise DemoSemanticError("apply: semantic.invalid_response")
        base_version = next_version


def prepare_relation(schema, relation):
    synced = call(
        "POST", "/api/semantic/source/sync",
        {"alias": "MAPP", "schema": schema, "relation": relation},
    )
    asset = synced.get("asset") or {}
    asset_id = asset.get("id")
    if not asset_id:
        raise DemoSemanticError(
            "%s.%s: %s" % (schema, relation, synced.get("code"))
        )
    fields = [
        field for field in (asset.get("generated") or {}).get("fields") or []
        if field.get("id")
    ]
    curated = asset.get("curated")
    if not isinstance(curated, dict):
        raise DemoSemanticError(
            "%s.%s: semantic asset curated metadata is invalid"
            % (schema, relation)
        )
    curated_fields = curated.get("fields", {})
    if not isinstance(curated_fields, dict):
        raise DemoSemanticError(
            "%s.%s: semantic field annotations are invalid"
            % (schema, relation)
        )
    # There is normally one model call per included field; a 429 adds bounded
    # retries. Preserve generated field order and take the leading configured
    # number from wide relations.
    selected_fields = fields[:LIMIT] if LIMIT > 0 else fields
    omitted_fields = len(fields) - len(selected_fields)
    if len(selected_fields) > 60:
        print(
            "    %s.%s: describing %d fields, %d model calls at a time; lower"
            " MAPP_DEMO_FIELD_LIMIT in .env to cap this"
            % (schema, relation, len(selected_fields), WORKERS),
            flush=True,
        )
    targets = ([] if has_curated_annotation(curated) else [
        {"kind": "table"}
    ]) + [
        {"kind": "field", "fieldId": field["id"]}
        for field in selected_fields
        if not has_curated_annotation(curated_fields.get(field["id"]))
    ]
    return {
        "schema": schema,
        "relation": relation,
        "asset_id": asset_id,
        "fields": fields,
        "selected_fields": selected_fields,
        "omitted_fields": omitted_fields,
        "targets": targets,
    }


def describe_relation(plan, pool, progress, retry_state):
    targets = plan["targets"]
    drafts = [None] * len(targets)
    # Probe the first missing target before fanning out. A project-wide Gemini
    # quota can reject every request; discovering that once must not launch all
    # remaining fields into the same exhausted quota.
    if targets:
        draft_serially(
            plan, 0, targets[0], drafts, progress, retry_state,
        )
    # Submit at most one worker window at a time while the provider is healthy.
    # After any 429, the recovery probe and all remaining targets are serial so
    # a retry cannot become another synchronized request burst. Results still
    # return to their input slots for deterministic proposals.
    indexed_targets = list(enumerate(targets[1:], start=1))
    while indexed_targets:
        if retry_state["serial"]:
            index, target = indexed_targets.pop(0)
            draft_serially(
                plan, index, target, drafts, progress, retry_state,
            )
            continue
        batch = indexed_targets[:WORKERS]
        del indexed_targets[:WORKERS]
        settle_worker_batch(
            plan, batch, pool, drafts, progress, retry_state,
        )

    operations = []
    base_version = None
    for drafted, version in drafts:
        if drafted is None:
            continue
        if base_version is None:
            base_version = version
        elif version != base_version:
            raise DemoSemanticError(
                "%s.%s: asset changed mid-description"
                % (plan["schema"], plan["relation"])
            )
        operations.extend(drafted)
    if operations:
        apply_operations(plan["asset_id"], base_version, operations, (
            "Gemini drafts for the demo showcase, applied without review by "
            "./bin/mapp demo. Treat them as a starting point, not as curated "
            "content."
        ))
    selected_count = len(plan["selected_fields"])
    scope = "table and %d fields" % selected_count
    if plan["omitted_fields"]:
        scope = "table and first %d of %d fields" % (
            selected_count, len(plan["fields"]),
        )
    omitted_note = ""
    if plan["omitted_fields"]:
        omitted_note = (
            ", %d remaining field%s not described because "
            "MAPP_DEMO_FIELD_LIMIT=%d"
            % (
                plan["omitted_fields"],
                "" if plan["omitted_fields"] == 1 else "s",
                LIMIT,
            )
        )
    return "    %s.%s: %s%s" % (
        plan["schema"], plan["relation"],
        ("%s described" % scope) if operations else "already described",
        omitted_note,
    )


arguments = sys.argv[1:]
try:
    if len(arguments) % 2:
        raise DemoSemanticError("relation arguments must be schema/name pairs")
    plans = [
        prepare_relation(schema, relation)
        for schema, relation in zip(arguments[::2], arguments[1::2])
    ]
    progress = ProgressBar(
        sum(len(plan["targets"]) for plan in plans),
        os.environ["MAPP_FIELD_LIMIT"],
    )
    summaries = []
    rate_limited = None
    retry_state = {"serial": False}
    progress.start()
    try:
        # Generation only drafts, so targets within one relation are
        # independent. One shared pool retains the eight-request bound. The
        # resulting operations are applied before moving to the next relation;
        # large sets span sequential proposals because the API caps each one.
        with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
            for plan in plans:
                try:
                    summaries.append(describe_relation(
                        plan, pool, progress, retry_state,
                    ))
                except DemoGenerationRateLimited as error:
                    rate_limited = error
                    break
    finally:
        # Keyboard interrupts, request failures and invalid provider responses
        # must never leave the next console message on the progress-bar line.
        progress.close()
    for summary in summaries:
        print(summary)
    if rate_limited is not None:
        if rate_limited.code == "semantic.generation_busy":
            reason = (
                "the configuration service remained at Gemini generation "
                "capacity"
            )
            guidance = (
                "Let other generation requests finish and rerun "
                "./bin/mapp demo"
            )
        elif rate_limited.code == "semantic.generation_rate_limited":
            reason = (
                "the configured Gemini project or model remained rate limited"
            )
            guidance = (
                "Check the active limit in Google AI Studio and rerun "
                "./bin/mapp demo after it resets"
            )
        else:
            reason = "the generation endpoint continued returning HTTP 429"
            guidance = (
                "Check other MAPP generation traffic and the active limit in "
                "Google AI Studio, then rerun ./bin/mapp demo"
            )
        print(
            "    Gemini descriptions paused because %s after %d retries."
            % (reason, len(RATE_LIMIT_RETRY_DELAYS)),
            file=sys.stderr,
        )
        print(
            "    Any completed target results from the unfinished relation "
            "were not applied and will be generated again on a rerun.",
            file=sys.stderr,
        )
        print(
            "    The demo will continue with existing descriptions and "
            "structural profiles. %s; already curated targets will be skipped."
            % guidance,
            file=sys.stderr,
        )
except DemoSemanticError as error:
    print("    " + str(error), file=sys.stderr)
    raise SystemExit(1)
PY
}

sync_one source_census census_2021_england_oa
sync_one source_census census_variables
for relation in bus_stops definitive_paths smoke_control_orders; do
  sync_one source_ops "${relation}"
done

if [ "${DESCRIBE}" = "1" ]; then
  step "Describing the relations and their fields"
  # This is the long step, and the knob that governs it is a line in .env
  # rather than something to discover afterwards.
  if [ -n "${DESCRIBE_FIELD_LIMIT}" ]; then
    printf '  MAPP_DEMO_FIELD_LIMIT=%s in .env: describing the first %s fields\n' \
      "${DESCRIBE_FIELD_LIMIT}" "${DESCRIBE_FIELD_LIMIT}"
    printf '  of each relation (or every field when there are fewer), normally one model call each.\n'
    printf '  Raise the limit for a fuller catalogue, or empty it for no limit.\n'
  else
    printf '  MAPP_DEMO_FIELD_LIMIT is empty in .env: describing every field.\n'
    printf '  Set it to a number there if you want a shorter run.\n'
  fi
  describe_relations \
    source_census census_2021_england_oa \
    source_census census_variables \
    source_ops bus_stops \
    source_ops definitive_paths \
    source_ops smoke_control_orders
fi

# ----------------------------------------------------------------- derived --
fixture_definition() { # manifest
  python3 - "$1" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
manifest = json.load(open(path, encoding="utf-8"))
definition_keys = {
    "name", "kind", "sources", "idColumn", "geometryColumn",
    "description", "spatialScope",
}
local_keys = {"queryFile", "legacyQuerySha256"}
unknown = sorted(set(manifest) - definition_keys - local_keys)
missing = sorted((definition_keys | {"queryFile"}) - set(manifest))
if unknown or missing:
    raise SystemExit(
        "%s has invalid keys (missing=%s, unknown=%s)"
        % (path, missing, unknown)
    )
query_file = manifest["queryFile"]
if (
    not isinstance(query_file, str)
    or not query_file
    or Path(query_file).name != query_file
):
    raise SystemExit("%s queryFile must be an adjacent file name" % path)
query_path = path.parent / query_file
query = query_path.read_text(encoding="utf-8").strip()
if not query:
    raise SystemExit("%s is empty" % query_path)
legacy = manifest.get("legacyQuerySha256", [])
if (
    not isinstance(legacy, list)
    or any(
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in legacy
    )
):
    raise SystemExit("%s legacyQuerySha256 must contain SHA-256 hex values" % path)
description = manifest.get("description")
if (
    not isinstance(description, str)
    or not description.startswith("MAPP demo fixture v")
):
    raise SystemExit("%s must carry the reserved MAPP demo ownership marker" % path)
definition = {key: manifest[key] for key in definition_keys}
definition["query"] = query
print(json.dumps(definition, separators=(",", ":")))
PY
}

fixture_decision() { # manifest current-response resolved-definition
  python3 - "$1" "$2" "$3" <<'PY'
import hashlib
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
response = json.loads(sys.argv[2])
desired = json.loads(sys.argv[3])
current = response.get("derivedLayer")
if not isinstance(current, dict):
    if response.get("code") == "derived_layer.not_found":
        print("create")
        raise SystemExit
    raise SystemExit(
        "derived-layer lookup failed: %s %s"
        % (response.get("code"), str(response.get("error") or "")[:160])
    )

keys = (
    "name", "kind", "query", "sources", "idColumn", "geometryColumn",
    "description", "spatialScope",
)

def project(value):
    output = {key: value.get(key) for key in keys}
    output["query"] = str(output.get("query") or "").strip()
    output["sources"] = sorted(output.get("sources") or [])
    output["description"] = str(output.get("description") or "").strip()
    return output

current_definition = project(current)
desired_definition = project(desired)
if current_definition == desired_definition:
    print("same")
    raise SystemExit

# An exact definition with only the ownership description missing is safe to
# adopt.
if all(
    current_definition[key] == desired_definition[key]
    for key in keys
    if key != "description"
):
    print("replace")
    raise SystemExit

# The first saved workspace predates fixture ownership markers. Adopt only the
# exact target or recovered query, interface and resolved scope; never a merely
# similar same-name user definition. A future fixture SQL change must carry
# the previous fixture hash in legacyQuerySha256 before it can replace it.
signature_keys = (
    "name", "kind", "sources", "idColumn", "geometryColumn", "spatialScope",
)
query_hash = hashlib.sha256(
    current_definition["query"].encode("utf-8")
).hexdigest()
target_hash = hashlib.sha256(
    desired_definition["query"].encode("utf-8")
).hexdigest()
if (
    all(
        current_definition[key] == desired_definition[key]
        for key in signature_keys
    )
    and query_hash in {
        target_hash,
        *manifest.get("legacyQuerySha256", []),
    }
):
    print("replace")
    raise SystemExit

raise SystemExit(
    "derived layer %r already exists but is not owned by this demo; "
    "rename or remove that definition before rerunning"
    % desired_definition["name"]
)
PY
}

wait_for_derived_profile() { # name
  local name="$1" response status attempt
  for attempt in $(seq 1 180); do
    response="$(api GET "/api/derived-layers/${name}")"
    status="$(printf '%s' "${response}" | jqp \
      "print(((d.get('derivedLayer') or {}).get('semanticProfile') or {}).get('status') or '')")"
    case "${status}" in
      ready) return 0 ;;
      repair_required|archived)
        fail "derived layer '${name}' has semantic status '${status}' and cannot be published"
        ;;
    esac
    sleep 1
  done
  fail "timed out waiting for derived layer '${name}' to become semantically ready"
}

derived_capacity_state() { # background-jobs response
  python3 - "$1" <<'PY'
import json
import sys

response = json.loads(sys.argv[1])
jobs = response.get("backgroundJobs")
if not isinstance(jobs, dict):
    raise SystemExit("derived background capacity response is invalid")
active = jobs.get("activeJobs")
maximum = jobs.get("maxActiveJobs")
if (
    isinstance(active, bool)
    or not isinstance(active, int)
    or active < 0
    or isinstance(maximum, bool)
    or not isinstance(maximum, int)
    or maximum < 1
):
    raise SystemExit("derived background capacity counts are invalid")
state = "available" if active < maximum else "full"
print("%s\t%d\t%d" % (state, active, maximum))
PY
}

background_derived_mutation() { # path json-body name
  local path="$1" body="$2" name="$3"
  local response http_status payload response_code status_url
  local operation_response operation_status operation_stage last_stage=""
  local capacity_response capacity_values capacity_state active_jobs max_active_jobs
  local attempt wait_announced=0

  # Inspect the database-independent queue before submitting. Derived mutation
  # admission follows its database preflight, so repeatedly probing with POST
  # while all slots are known to be occupied would repeat expensive planning.
  # The 429 branch remains necessary for the race between this GET and POST.
  for attempt in $(seq 1 360); do
    capacity_response="$(api GET /api/derived-layers/background-jobs)"
    capacity_values="$(derived_capacity_state "${capacity_response}")"
    IFS=$'\t' read -r \
      capacity_state active_jobs max_active_jobs <<<"${capacity_values}"
    if [ "${capacity_state}" = "full" ]; then
      if [ "${wait_announced}" -eq 0 ]; then
        printf '  %-42s waiting for a derived worker slot (%s/%s active)\n' \
          "${name}" "${active_jobs}" "${max_active_jobs}"
        wait_announced=1
      fi
      sleep 5
      continue
    fi
    if ! response="$(curl -sS -X POST \
      -H "Host: ${CONFIG_HOST}" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H 'Content-Type: application/json' \
      --data-binary "${body}" \
      --write-out $'\n%{http_code}' \
      "${BASE}${path}")"
    then
      fail "could not submit background derived operation for '${name}'"
    fi
    http_status="${response##*$'\n'}"
    payload="${response%$'\n'*}"
    if [ "${http_status}" = "202" ]; then
      status_url="$(printf '%s' "${payload}" | jqp \
        "print(d.get('statusUrl') or '')")"
      [ -n "${status_url}" ] || fail \
        "background derived operation for '${name}' omitted its status URL"
      break
    fi
    response_code="$(printf '%s' "${payload}" | jqp \
      "print(d.get('code') or '')")"
    if [ "${http_status}" = "429" ] \
      && [ "${response_code}" = "derived_layer.background_capacity" ]
    then
      if [ "${wait_announced}" -eq 0 ]; then
        printf '  %-42s waiting for a derived worker slot\n' "${name}"
        wait_announced=1
      fi
      sleep 5
      continue
    fi
    printf '%s' "${payload}" | python3 -c "
import json,sys
d=json.load(sys.stdin)
raise SystemExit(
    '  background request refused: %s %s'
    % (d.get('code') or '${http_status}', str(d.get('error') or '')[:240])
)"
  done
  [ -n "${status_url:-}" ] || fail \
    "timed out waiting to submit background derived operation for '${name}'"

  for attempt in $(seq 1 1800); do
    operation_response="$(api GET "${status_url}")"
    IFS=$'\t' read -r operation_status operation_stage < <(
      printf '%s' "${operation_response}" | python3 -c "
import json,sys
operation=(json.load(sys.stdin).get('operation') or {})
print('%s\t%s' % (operation.get('status') or '', operation.get('stage') or ''))"
    )
    if [ -n "${operation_stage}" ] \
      && [ "${operation_stage}" != "${last_stage}" ]
    then
      printf '  %-42s %s\n' "${name}" "${operation_stage}"
      last_stage="${operation_stage}"
    fi
    case "${operation_status}" in
      succeeded) return 0 ;;
      running|cancelling) sleep 2 ;;
      failed|cancelled|indeterminate)
        printf '%s' "${operation_response}" | python3 -c "
import json,sys
operation=(json.load(sys.stdin).get('operation') or {})
error=operation.get('error') or {}
raise SystemExit(
    '  background operation ended %s: %s %s'
    % (
        operation.get('status'), error.get('code') or '',
        str(error.get('message') or '')[:240],
    )
)"
        ;;
      *) fail "background operation for '${name}' returned invalid status '${operation_status}'" ;;
    esac
  done
  fail "timed out waiting for background derived operation for '${name}'"
}

validate_xyz_reload() { # reload-response
  python3 - "$1" <<'PY'
import json
import re
import sys

response = json.loads(sys.argv[1])
expected = response.get("expectedWorkspaceFingerprint")
status = response.get("status")
requested = response.get("requestedGeneration")
if (
    not isinstance(expected, str)
    or re.fullmatch(r"[0-9a-f]{64}", expected) is None
    or not isinstance(status, dict)
    or status.get("completed") is not True
    or status.get("healthy") is not True
    or status.get("workspaceFingerprint") != expected
    or isinstance(requested, bool)
    or not isinstance(requested, int)
    or requested < 1
    or isinstance(status.get("appliedGeneration"), bool)
    or not isinstance(status.get("appliedGeneration"), int)
    or status["appliedGeneration"] < requested
):
    raise SystemExit("XYZ did not load the saved workspace fingerprint")
PY
}

ensure_saved_workspace_xyz() {
  local response
  # JSON equality above proves the live workspace has the saved content, but
  # not that both files have identical serialization. Omit a caller-computed
  # digest: the reload endpoint derives the current raw fingerprint while
  # holding the save/reload lock and returns the value it bound.
  printf '  requesting a reload bound to the installed workspace\n'
  response="$(api POST /api/xyz/reload '{"confirmed":true,"timeout":120}')"
  validate_xyz_reload "${response}"
  printf '  XYZ reloaded the saved workspace fingerprint\n'
}

workspace_apply_state() { # saved-workspace http-status response-file
  # The apply response carries the proposal twice (once directly and once as
  # the operation result), and each copy holds the workspace four times over.
  # That is far past the kernel's single-argument limit, so read it from a
  # file rather than argv.
  python3 - "$1" "$2" "$3" <<'PY'
import json
import re
import sys

saved = json.load(open(sys.argv[1], encoding="utf-8"))
try:
    http_status = int(sys.argv[2])
except ValueError as exc:
    raise SystemExit("workspace apply returned an invalid HTTP status") from exc
response = json.load(open(sys.argv[3], encoding="utf-8"))
proposal = response.get("proposal")
reload = response.get("reload")
if http_status not in {200, 504}:
    raise SystemExit(
        "workspace apply refused: %s %s"
        % (response.get("code") or http_status, str(response.get("error") or "")[:240])
    )
if (
    not isinstance(proposal, dict)
    or proposal.get("status") != "applied"
    or proposal.get("candidate") != saved
    or not isinstance(reload, dict)
):
    raise SystemExit("workspace apply did not commit the saved demo")
fingerprint = proposal.get("appliedFingerprint")
reload_status = reload.get("status")
if (
    not isinstance(fingerprint, str)
    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
    or reload.get("expectedWorkspaceFingerprint") != fingerprint
    or not isinstance(reload_status, dict)
):
    raise SystemExit("workspace apply returned invalid reload evidence")
if http_status == 200:
    if (
        reload_status.get("completed") is not True
        or reload_status.get("healthy") is not True
        or reload_status.get("workspaceFingerprint") != fingerprint
    ):
        raise SystemExit("workspace apply did not reload the saved demo")
    print("ready")
else:
    if reload_status.get("completed") is not False:
        raise SystemExit("workspace apply timeout returned invalid reload evidence")
    print("recover")
PY
}

apply_saved_workspace_proposal() { # proposal-id saved-workspace
  local identifier="$1" saved_workspace="$2"
  local http_status payload_file state current_workspace
  payload_file="${WORK_DIR}/workspace-apply.json"
  if ! http_status="$(curl -sS -X POST \
    -H "Host: ${CONFIG_HOST}" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    --data-binary '{"approved":true}' \
    --output "${payload_file}" \
    --write-out '%{http_code}' \
    "${BASE}/api/proposals/${identifier}/apply")"
  then
    fail "could not apply the saved demo workspace proposal"
  fi
  state="$(workspace_apply_state \
    "${saved_workspace}" "${http_status}" "${payload_file}")"
  if [ "${state}" = "ready" ]; then
    printf '  workspace applied and XYZ reloaded\n'
    return 0
  fi

  printf '  workspace committed; recovering its timed-out XYZ reload\n'
  current_workspace="$(api GET /api/workspace)"
  python3 - "${saved_workspace}" "${current_workspace}" <<'PY' || fail \
    "workspace changed after the timed-out apply; inspect it before reloading XYZ"
import json
import sys

saved = json.load(open(sys.argv[1], encoding="utf-8"))
current = json.loads(sys.argv[2]).get("workspace")
raise SystemExit(0 if current == saved else 1)
PY
  ensure_saved_workspace_xyz
}

step "Reconciling the saved workspace's derived layers"
shopt -s nullglob
fixtures=("${DERIVED_FIXTURES}"/*.json)
shopt -u nullglob
[ "${#fixtures[@]}" -eq 4 ] || fail \
  "the saved demo must contain exactly four derived-layer manifests"

capabilities="$(api GET /api/derived-layers/capabilities)"
printf '%s' "${capabilities}" | python3 -c "
import json,sys
d = json.load(sys.stdin)
planning = d.get('definitionPlanning') or {}
h3 = d.get('h3Readiness') or {}
if d.get('configured') is not True:
    raise SystemExit('  derived-layer management is not configured')
if planning.get('path') != '/api/derived-layers/plan':
    raise SystemExit(
        '  the running API does not advertise derived-layers plan; rebuild '
        'or restart config-ui before running the demo'
    )
if h3.get('ready') is not True:
    raise SystemExit(
        '  H3 is not ready: %s' % (h3.get('reasons') or h3.get('code') or h3)
    )"

definitions=()
derived_names=()
kinds=()
create_requests=()
desired_definitions=()
fixture_actions=()

# Plan and collision-check the complete set before applying the first change.
# A malformed final fixture or a foreign same-name definition therefore leaves
# every managed relation unchanged.
for fixture in "${fixtures[@]}"; do
  definition="$(fixture_definition "${fixture}")"
  name="$(python3 -c \
    "import json,sys; print(json.loads(sys.argv[1])['name'])" \
    "${definition}")"
  kind="$(python3 -c \
    "import json,sys; print(json.loads(sys.argv[1])['kind'])" \
    "${definition}")"
  definitions+=("${definition}")
  derived_names+=("${name}")
  kinds+=("${kind}")

  # The generic planner resolves the versioned workspace scope, verifies the
  # semantic source profiles, and executes every query/access-path/storage
  # preflight. It mutates nothing; creation remains a separate request bound
  # to the returned plan fingerprint.
  plan="$(api POST /api/derived-layers/plan "${definition}")"
  create="$(printf '%s' "${plan}" | python3 -c "
import json,sys
d = json.load(sys.stdin)
p = d.get('derivedLayerPlan')
if not isinstance(p, dict):
    raise SystemExit('  plan refused: %s %s' % (d.get('code'), str(d.get('error') or '')[:160]))
if d.get('mutationApplied') is not False:
    raise SystemExit('  plan response did not prove mutationApplied=false')
request = p.get('createRequest') or {}
fingerprint = request.get('planFingerprint')
if not isinstance(fingerprint, str) or not fingerprint.startswith('sha256:'):
    raise SystemExit('  plan response omitted its create fingerprint')
expected = json.loads(sys.argv[1])
unbound = dict(request); unbound.pop('planFingerprint', None)
if unbound != expected:
    raise SystemExit('  planned create request differs from the saved fixture')
print(json.dumps(request, separators=(',', ':'))) " "${definition}")"
  desired="$(printf '%s' "${plan}" | python3 -c "
import json,sys
d = json.load(sys.stdin)
p = d['derivedLayerPlan']
expected_scope = json.loads(sys.argv[1])
if p.get('resolvedSpatialScope') != expected_scope:
    raise SystemExit('  planned scope differs from the saved demo workspace')
definition = dict(p['createRequest'])
definition.pop('planFingerprint', None)
definition['spatialScope'] = p['resolvedSpatialScope']
print(json.dumps(definition, separators=(',', ':'))) " \
    "${DEMO_RESOLVED_SCOPE}")"

  probe="$(api GET "/api/derived-layers/${name}" 2>/dev/null || true)"
  action="$(fixture_decision "${fixture}" "${probe}" "${desired}")"
  create_requests+=("${create}")
  desired_definitions+=("${desired}")
  fixture_actions+=("${action}")
  printf '  %-42s planned (%s)\n' "${name}" "${action}"
done

for index in "${!fixtures[@]}"; do
  fixture="${fixtures[index]}"
  definition="${definitions[index]}"
  name="${derived_names[index]}"
  kind="${kinds[index]}"
  create="${create_requests[index]}"
  desired="${desired_definitions[index]}"
  action="${fixture_actions[index]}"
  # Close the gap between the all-fixture collision check and this mutation.
  # A concurrently-created exact target is harmless; any other action change
  # requires a fresh full planning pass rather than overwriting new state.
  probe="$(api GET "/api/derived-layers/${name}" 2>/dev/null || true)"
  current_action="$(fixture_decision "${fixture}" "${probe}" "${desired}")"
  if [ "${current_action}" = "same" ]; then
    action="same"
  elif [ "${current_action}" != "${action}" ]; then
    fail "derived layer '${name}' changed after planning; rerun the demo from a fresh plan"
  fi
  case "${action}" in
    create)
      request="$(python3 -c "
import json,sys
d=json.loads(sys.argv[1]); d['background']=True
print(json.dumps(d, separators=(',', ':')))" "${create}")"
      background_derived_mutation /api/derived-layers "${request}" "${name}"
      printf '  %-42s created\n' "${name}"
      ;;
    replace)
      replacement="$(python3 -c "
import json,sys
d = json.loads(sys.argv[1]); d['confirmed'] = True; d['background'] = True
print(json.dumps(d, separators=(',', ':')))" "${definition}")"
      background_derived_mutation \
        "/api/derived-layers/${name}/replace" "${replacement}" "${name}"
      printf '  %-42s updated from the saved fixture\n' "${name}"
      ;;
    same)
      if [ "${kind}" = "materialized" ]; then
        background_derived_mutation \
          "/api/derived-layers/${name}/refresh" \
          '{"confirmed":true,"background":true}' "${name}"
        printf '  %-42s refreshed from the reloaded source\n' "${name}"
      else
        printf '  %-42s already matches\n' "${name}"
      fi
      ;;
    *) fail "invalid fixture action '${action}' for '${name}'" ;;
  esac

  # Read back and compare the complete managed definition. A successful HTTP
  # mutation response alone is not evidence that the intended relation is the
  # one now stored.
  probe="$(api GET "/api/derived-layers/${name}")"
  [ "$(fixture_decision "${fixture}" "${probe}" "${desired}")" = "same" ] \
    || fail "derived layer '${name}' does not match its saved fixture"
done

step "Waiting for the derived semantic profiles"
for name in "${derived_names[@]}"; do
  wait_for_derived_profile "${name}"
  printf '  %-42s ready\n' "${name}"
done

# --------------------------------------------------------------- workspace --
step "Publishing the saved demo workspace"
workspace_response="$(api GET /api/workspace)"
if python3 - "${DEMO_WORKSPACE}" "${workspace_response}" <<'PY'
import json
import sys
saved = json.load(open(sys.argv[1], encoding="utf-8"))
current = json.loads(sys.argv[2]).get("workspace")
raise SystemExit(0 if current == saved else 1)
PY
then
  printf '  workspace already matches the saved demo\n'
  ensure_saved_workspace_xyz
else
  revision="$(printf '%s' "${workspace_response}" | jqp \
    "print(d.get('revision') or '')")"
  [ -n "${revision}" ] || fail "could not read the live workspace revision"
  proposal="$(python3 - "${DEMO_WORKSPACE}" "${revision}" \
    "${workspace_response}" <<'PY'
import json
import sys

workspace = json.load(open(sys.argv[1], encoding="utf-8"))
current = json.loads(sys.argv[3]).get("workspace")
if not isinstance(current, dict):
    raise SystemExit("could not read the current workspace")

def pointer(key):
    return "/" + key.replace("~", "~0").replace("/", "~1")

operations = [
    {"op": "unset", "path": pointer(key)}
    for key in current
    if key not in workspace
]
operations.extend(
    {"op": "set", "path": pointer(key), "value": value}
    for key, value in workspace.items()
)
print(json.dumps({
    "revision": sys.argv[2],
    "operations": operations,
    "explanation": (
        "Load the versioned demo workspace and its four reconciled derived "
        "layers. ./bin/mapp demo applies this saved showcase configuration."
    ),
}, separators=(",", ":")))
PY
)"

  check="$(api POST /api/proposals/check "${proposal}")"
  fingerprint="$(printf '%s' "${check}" | python3 -c "
import hashlib,json,sys
d = json.load(sys.stdin)
c = d.get('check') or {}
if not c.get('valid'):
    raise SystemExit('  workspace preflight failed: %s' % (c.get('errors') or c))
target = json.load(open(sys.argv[1], encoding='utf-8'))
target_hash = hashlib.sha256(json.dumps(
    target, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    allow_nan=False,
).encode()).hexdigest()
if c.get('candidateHash') != target_hash:
    raise SystemExit('  workspace preflight candidate is not the saved demo')
fingerprint = c.get('checkFingerprint')
if not fingerprint:
    raise SystemExit('  workspace preflight omitted its fingerprint')
print(fingerprint) " "${DEMO_WORKSPACE}")"
  bound="$(python3 -c "
import json,sys
d = json.loads(sys.argv[1]); d['checkFingerprint'] = sys.argv[2]
print(json.dumps(d, separators=(',', ':')))" "${proposal}" "${fingerprint}")"
  identifier="$(api POST /api/proposals "${bound}" | jqp "
proposal = d.get('proposal') or {}
identifier = proposal.get('id')
if not identifier or proposal.get('status') != 'pending':
    raise SystemExit('  workspace proposal refused: %s %s' % (d.get('code'), str(d.get('error') or '')[:160]))
print(identifier)")"
  apply_saved_workspace_proposal "${identifier}" "${DEMO_WORKSPACE}"

fi

# Close the read/reload race in the already-matching path as well as proving
# the proposal path's final state. The reload endpoint binds whichever exact
# bytes are current under its lock; this readback proves those bytes still
# represent the saved workspace before the command reports success.
workspace_response="$(api GET /api/workspace)"
python3 - "${DEMO_WORKSPACE}" "${workspace_response}" <<'PY' || fail \
  "the published workspace does not match the saved demo"
import json
import sys
saved = json.load(open(sys.argv[1], encoding="utf-8"))
current = json.loads(sys.argv[2]).get("workspace")
raise SystemExit(0 if current == saved else 1)
PY

printf '\nDemo rebuilt: four derived relations and the saved ten-layer workspace are ready.\n'
printf 'Verify with ./bin/mapp verify and open the map.\n'
