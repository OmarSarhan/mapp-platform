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
import datetime as dt, os, secrets
from pathlib import Path
from control_plane import ControlStore

store = ControlStore(Path(os.environ["CONTROL_DIR"]))
expires = (
    dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=45)
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
trap 'if [[ -n "${TOKEN_ID}" ]]; then revoke_token "${TOKEN_ID}"; fi; revoke_demo_tokens' EXIT
read -r TOKEN TOKEN_ID < <(mint_token)
[ -n "${TOKEN:-}" ] || fail "could not mint a token; is config-ui running?"

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
    | jqp "
name = (d.get('derivedLayer') or {}).get('name')
if not name:
    raise SystemExit('  create refused: %s %s' % (d.get('code'), (d.get('error') or '')[:160]))
print('  %-32s created' % name)"
  # Read it back. The create response is the server describing what it did,
  # and printing a name out of it is not evidence the relation is there --
  # which is how this step came to report two layers it had not left behind.
  api GET "/api/derived-layers/${name}" \
    | jqp "
layer = d.get('derivedLayer') or {}
if not layer.get('name'):
    raise SystemExit('  %s is absent after a create that reported success: %s'
                     % ('${name}', d.get('code')))" \
    || fail "derived layer '${name}' was not created"
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
