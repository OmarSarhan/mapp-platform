"""Coordination of explicitly disposable derived relations.

Normal publication and preview requests hold a shared filesystem lock. Cleanup
alone takes an exclusive, nonblocking lock, so it never deletes between a
reference check and publication or while a browser request still owns its work.
The database journal is authoritative for identity and deletion authorization.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import uuid


MAX_PROPOSALS = 1000
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024
CLEANUP_BATCH = 20
CLEANUP_INTERVAL_SECONDS = 60
_HELD = threading.local()
_REFERENCE = re.compile(
    r'(?<![\w])(?:"derived_layers"|derived_layers)\s*\.\s*'
    r'(?:"([a-z][a-z0-9_]{0,62})"|([a-z][a-z0-9_]{0,62}))(?![\w])',
    re.IGNORECASE,
)


class DraftLifecycleError(ValueError):
    pass


class DraftCleanupBusy(Exception):
    pass


@contextmanager
def lifecycle_lock(root: Path, *, exclusive: bool = False):
    root = Path(root)
    key = str(root.resolve())
    held = getattr(_HELD, "locks", None)
    if held is None:
        held = _HELD.locks = {}
    if key in held:
        if exclusive and not held[key]:
            raise DraftCleanupBusy("Cannot upgrade a publication lock.")
        yield
        return
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / "draft-lifecycle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
        except BlockingIOError as exc:
            raise DraftCleanupBusy("Publication or preview is active.") from exc
        held[key] = exclusive
        try:
            yield
        finally:
            held.pop(key, None)
    finally:
        os.close(fd)


def normalize_bindings(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 64:
        raise DraftLifecycleError("draftRelations must be an array of at most 64 identities.")
    result, names, identities = [], set(), set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "assetId", "generation"}:
            raise DraftLifecycleError("Each draft relation requires only name, assetId and generation.")
        name, asset, generation = item["name"], item["assetId"], item["generation"]
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) is None:
            raise DraftLifecycleError("Invalid draft relation name.")
        try:
            valid_asset = isinstance(asset, str) and str(uuid.UUID(asset)) == asset
        except ValueError:
            valid_asset = False
        if not valid_asset or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise DraftLifecycleError("Draft identity requires a canonical UUID and positive generation.")
        if name in names or asset in identities:
            raise DraftLifecycleError("Draft identities must be unique.")
        names.add(name)
        identities.add(asset)
        result.append(dict(item))
    return result


def binding_of(draft: dict) -> dict:
    return {key: draft[key] for key in ("name", "assetId", "generation")}


def managed_references(workspace: dict) -> set[str]:
    """Conservatively include raw/named locale tables, zoom maps and inline SQL.

Unresolved references to the derived schema block cleanup rather than pretending
that dynamic SQL or a template contains no dependency. False positives retain
data and surface as blockers, never grant permission to delete.
"""
    if not isinstance(workspace, dict):
        raise DraftLifecycleError("Workspace or proposal state is invalid.")
    result = set()
    pending = [(workspace, False)]
    while pending:
        value, template = pending.pop()
        if isinstance(value, dict):
            # A provider-backed query template can hide its source SQL from
            # workspace inspection. Retain drafts until it is reviewed.
            if isinstance(value.get("src"), str) and (template or value.get("dbs")):
                result.add("*")
            pending.extend((child, template or key in {"templates", "template"}) for key, child in value.items())
        elif isinstance(value, list):
            pending.extend((child, template) for child in value)
        elif isinstance(value, str):
            matches = list(_REFERENCE.finditer(value))
            result.update((match.group(1) or match.group(2)).lower() for match in matches)
            if "derived_layers" in _REFERENCE.sub("", value).lower():
                result.add("*")
    return result


def _read_state(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            raise DraftLifecycleError("Cleanup cannot verify bounded operational state.")
        raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise DraftLifecycleError("Cleanup state is too large.")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DraftLifecycleError("Duplicate keys in cleanup evidence.")
            result[key] = value
        return result
    def invalid_constant(value):
        raise DraftLifecycleError("Nonfinite values in cleanup evidence.")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise DraftLifecycleError("Cleanup state is not an object.")
    return value


def _atomic_state(path: Path, value: dict):
    fd, temporary = tempfile.mkstemp(prefix=".draft-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def retain_browser_lease(root: Path, seconds: int = 210):
    """Retain a crash-safe grace period even if the HTTP request loses the runner.

The runner accepts at most 180 seconds plus bounded shutdown. A single global
lease conservatively delays all draft cleanup; it cannot be pruned with ordinary
visual operation history or accidentally cleared by another concurrent run.
"""
    root = Path(root)
    with lifecycle_lock(root):
        fd = os.open(root / "draft-browser-lease.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = root / "draft-browser-lease.json"
            previous = _read_state(path).get("until", 0) if path.exists() else 0
            if isinstance(previous, bool) or not isinstance(previous, (int, float)) or not math.isfinite(previous):
                raise DraftLifecycleError("Invalid browser cleanup lease.")
            _atomic_state(path, {"until": max(previous, time.time() + max(seconds, 210))})
        finally:
            os.close(fd)


def _epoch(value) -> float:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise DraftLifecycleError("Draft expiry is invalid.")
    if parsed.tzinfo is None:
        raise DraftLifecycleError("Draft expiry must include a timezone.")
    return parsed.timestamp()


class DraftLifecycle:
    def __init__(self, control, derived, read_workspace):
        self.control, self.derived, self.read_workspace = control, derived, read_workspace

    def validate_bindings(self, bindings, candidate, original, actor, *, proposal_id=None):
        bindings = normalize_bindings(bindings)
        references, originals = managed_references(candidate), managed_references(original)
        for binding in bindings:
            if binding["name"] not in references or binding["name"] in originals:
                raise DraftLifecycleError("A bound draft must be newly referenced by this proposal.")
            item = self.derived.get(binding["name"], include_query=False)
            draft = item.get("draft") if isinstance(item, dict) else None
            if not isinstance(draft, dict) or binding_of(draft) != binding:
                raise DraftLifecycleError("The draft identity changed or is not disposable.")
            if draft["state"] != "active" or _epoch(draft["expiresAt"]) <= time.time():
                raise DraftLifecycleError("The draft has expired or is no longer active.")
            if draft.get("proposalId") not in (None, proposal_id):
                raise DraftLifecycleError("The draft already belongs to another proposal.")
            if actor != "admin" and draft.get("createdBy") != actor:
                raise DraftLifecycleError("Only the draft creator can bind it to a proposal.")
        return bindings

    def validate_publication(self, proposal):
        for binding in normalize_bindings(proposal.get("draftRelations", [])):
            item = self.derived.get(binding["name"], include_query=False)
            draft = item.get("draft") if isinstance(item, dict) else None
            if (
                not isinstance(draft, dict) or binding_of(draft) != binding
                or item.get("semanticProfile", {}).get("assetId") != binding["assetId"]
                or item.get("semanticProfile", {}).get("generation") != binding["generation"]
                or draft.get("proposalId") != proposal["id"]
                or draft.get("state") not in {"active", "adopted"}
                or (draft["state"] == "active" and _epoch(draft["expiresAt"]) <= time.time())
            ):
                raise DraftLifecycleError("A proposal draft expired or changed; create a new checked proposal.")

    def prepare_publication(self, workspace):
        names = managed_references(workspace) - {"*"}
        if not names:
            return None
        bindings = [binding_of(item) for item in self.derived.drafts_for_names(sorted(names))]
        if not bindings:
            return None
        bindings = normalize_bindings(bindings)
        directory = self.control.root / "draft-publications"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / f"{uuid.uuid4().hex}.json"
        record = {"state": "intent", "draftRelations": bindings}
        _atomic_state(path, record)
        return path, record

    def commit_publication(self, intent):
        if intent is None:
            return
        path, record = intent
        record = {**record, "state": "committed"}
        _atomic_state(path, record)
        self.derived.adopt_drafts(record["draftRelations"])
        # Adoption is durable before removing the recovery intent. Retaining a
        # stale file after an unlink failure is harmless and fails conservative.
        path.unlink()

    def publication_state(self, binding):
        directory = self.control.root / "draft-publications"
        if directory.is_symlink():
            raise DraftLifecycleError("Publication recovery directory cannot be verified.")
        scanned = 0
        deadline = time.monotonic() + 2
        for number, path in enumerate(directory.glob("*.json")):
            scanned += path.stat().st_size
            if number >= MAX_PROPOSALS or scanned > MAX_SCAN_BYTES or time.monotonic() > deadline:
                raise DraftLifecycleError("Publication recovery scan is incomplete.")
            record = _read_state(path)
            state = record.get("state")
            if state not in {"intent", "committed"}:
                raise DraftLifecycleError("Publication recovery state is invalid.")
            if binding in normalize_bindings(record.get("draftRelations")):
                return state
        return None

    def proposals(self) -> list[dict]:
        result = []
        scanned = 0
        deadline = time.monotonic() + 2
        if self.control.proposals.is_symlink():
            raise DraftLifecycleError("Proposal directory cannot be verified.")
        for path in self.control.proposals.glob("*/proposal.json"):
            scanned += path.stat().st_size
            if len(result) >= MAX_PROPOSALS or path.parent.is_symlink() or scanned > MAX_SCAN_BYTES or time.monotonic() > deadline:
                raise DraftLifecycleError("Proposal scan is incomplete; cleanup is blocked.")
            proposal = _read_state(path)
            if proposal.get("id") != path.parent.name or proposal.get("status") not in {
                "pending", "applying", "applied", "declined", "cancelled",
                "conflicted", "draft_binding_failed",
            }:
                raise DraftLifecycleError("Proposal identity or state cannot be verified.")
            for side in ("original", "candidate"):
                digest = hashlib.sha256(json.dumps(
                    proposal.get(side), sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False,
                ).encode()).hexdigest()
                if proposal.get(side + "Hash") != digest:
                    raise DraftLifecycleError("Proposal workspace integrity cannot be verified.")
            references = managed_references(proposal.get("candidate")) | managed_references(proposal.get("original"))
            result.append({
                "id": proposal["id"], "status": proposal["status"],
                "actor": proposal.get("actor"), "references": references,
                "draftRelations": normalize_bindings(proposal.get("draftRelations", [])),
            })
        return result

    def reconcile_one(self, draft: dict) -> str:
        binding = binding_of(draft)
        name = binding["name"]
        live = managed_references(self.read_workspace()[1])
        if name in live:
            self.derived.adopt_drafts([binding])
            return "adopted-live"
        if "*" in live:
            return "unresolved-live-reference"
        publication = self.publication_state(binding)
        if publication == "committed":
            self.derived.adopt_drafts([binding])
            return "adopted-publication"
        if publication == "intent":
            return "publication-reconciliation-required"
        proposals = self.proposals()
        owner_id = draft.get("proposalId")
        owner = next((p for p in proposals if p["id"] == owner_id), None)
        if owner_id and owner is None:
            return "owner-proposal-unavailable"
        # Recover the file-before-database binding window using exact recorded
        # identities and creator authority, never names or creation timestamps.
        if not owner_id:
            claims = [p for p in proposals if p["status"] == "pending" and binding in p["draftRelations"]]
            if len(claims) == 1 and claims[0]["actor"] in (draft["createdBy"], "admin"):
                if _epoch(draft["expiresAt"]) > time.time():
                    self.derived.bind_drafts([binding], claims[0]["id"], claims[0]["actor"], allow_other_owner=claims[0]["actor"] == "admin")
                    owner = claims[0]
                    owner_id = owner["id"]
            elif claims:
                return "proposal-binding-conflict"
        if owner is not None and binding not in owner["draftRelations"]:
            return "owner-binding-mismatch"
        if owner is not None and owner["status"] == "applied":
            self.derived.adopt_drafts([binding], proposal_id=owner_id)
            return "adopted-proposal"
        if owner is not None and owner["status"] in {"applying", "conflicted"}:
            return "apply-reconciliation-required"
        for proposal in proposals:
            if proposal["id"] == owner_id:
                continue
            if proposal["status"] in {"pending", "applying", "conflicted"} and (
                name in proposal["references"] or "*" in proposal["references"]
            ):
                return "other-proposal-reference"
        declined = owner is not None and owner["status"] in {"declined", "cancelled"}
        if not declined and _epoch(draft["expiresAt"]) > time.time():
            return "retained-until-expiry"
        lease_path = self.control.root / "draft-browser-lease.json"
        if lease_path.exists() or lease_path.is_symlink():
            until = _read_state(lease_path).get("until")
            if isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until):
                raise DraftLifecycleError("Browser lease cannot be verified.")
            if until > time.time():
                return "browser-preview-lease"
        # Queued background previews have not yet acquired their worker lock.
        if self.control.operations.is_symlink():
            raise DraftLifecycleError("Operation directory cannot be verified.")
        scanned = 0
        deadline = time.monotonic() + 2
        for number, path in enumerate(self.control.operations.glob("*.json")):
            scanned += path.stat().st_size
            if number >= MAX_PROPOSALS or scanned > MAX_SCAN_BYTES or time.monotonic() > deadline:
                raise DraftLifecycleError("Visual operation scan is incomplete.")
            operation = _read_state(path)
            if operation.get("kind") in {"proposal.screenshot", "proposal.visual-test", "visual.test"} and operation.get("status") == "running":
                return "queued-preview"
        self.derived.cleanup_draft(binding)
        self.control.audit("derived_draft.cleaned", actor="system:draft-cleanup", details={**binding, "reason": "proposal-declined" if declined else "expired"})
        return "dropped"

    def sweep(self) -> list[dict]:
        results = []
        for draft in self.derived.list_drafts(limit=CLEANUP_BATCH):
            try:
                with lifecycle_lock(self.control.root, exclusive=True):
                    reason = self.reconcile_one(draft)
                    if reason not in {"dropped", "adopted-live", "adopted-proposal", "adopted-publication"}:
                        self.derived.record_draft_cleanup(binding_of(draft), reason)
            except DraftCleanupBusy:
                break
            except Exception as exc:
                # Store a bounded class name, never database SQL/credentials.
                reason = "cleanup-blocked"
                self.derived.record_draft_cleanup(binding_of(draft), reason, type(exc).__name__)
            results.append({**binding_of(draft), "outcome": reason})
        return results
