from __future__ import annotations

import base64
import contextlib
import datetime as dt
import decimal
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any


UTC = dt.timezone.utc
PBKDF2_ROUNDS = 310_000
SESSION_IDLE_SECONDS = 30 * 60
SESSION_MAX_SECONDS = 12 * 60 * 60
DEVICE_AUTH_SECONDS = 10 * 60
DEVICE_TOKEN_SECONDS = 30 * 24 * 60 * 60
MIN_PASSWORD_LENGTH = 12
AUDIT_MAX_BYTES = 10 * 1024 * 1024
AUDIT_RETAIN_BYTES = 5 * 1024 * 1024
AUDIT_READ_BYTES = 2 * 1024 * 1024
AUDIT_RECORD_MAX_BYTES = 64 * 1024
FAILED_TOKEN_AUDIT_INTERVAL = 60
DEVICE_SCOPES = {
    "inspect", "propose", "visual", "apply", "reload", "derive",
    "semantic:inspect", "semantic:source", "semantic:generate",
    "semantic:data", "semantic:propose", "semantic:apply", "semantic:admin",
}
TOKEN_SCOPES = {"full", *DEVICE_SCOPES}

# Authentication, audit, and proposal state must never be created with
# process-default world-readable permissions, even briefly.
os.umask(0o077)


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(value: dt.datetime | None = None) -> str:
    return (value or now()).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO-8601 string.")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone.")
    return parsed.astimezone(UTC)


def require_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Administrator passwords must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    return password


def _reject_json_constant(value: str):
    raise ValueError(f"{value} is not valid JSON")


def _strict_json(raw: str) -> Any:
    return json.loads(raw, parse_constant=_reject_json_constant)


def json_default(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )


def _atomic_bytes(path: Path, encoded: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(
        path,
        (
            json.dumps(
                value,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
                default=json_default,
            ) + "\n"
        ).encode("utf-8"),
    )


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2-sha256${PBKDF2_ROUNDS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2-sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            base64.urlsafe_b64decode(salt),
            int(rounds),
        )
        return hmac.compare_digest(base64.urlsafe_b64encode(digest).decode(), expected)
    except (TypeError, ValueError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ControlStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.state_path = root / "auth.json"
        self.audit_path = root / "audit.jsonl"
        self.process_lock_path = root / ".control.lock"
        self.proposals = root / "proposals"
        self.proposals.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.proposals, 0o700)
        self.operations = root / "operations"
        self.operations.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.operations, 0o700)
        self.lock = threading.RLock()
        self._process_lock_depth = 0
        self._process_lock_fd = os.open(
            self.process_lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        os.chmod(self.process_lock_path, 0o600)
        self._last_failed_token_audit = 0.0
        self._secure_existing_state()
        self._purge_legacy_device_credentials()

    @contextlib.contextmanager
    def _locked(self):
        with self.lock:
            if self._process_lock_depth == 0:
                fcntl.flock(self._process_lock_fd, fcntl.LOCK_EX)
            self._process_lock_depth += 1
            try:
                yield
            finally:
                self._process_lock_depth -= 1
                if self._process_lock_depth == 0:
                    fcntl.flock(self._process_lock_fd, fcntl.LOCK_UN)

    def _secure_existing_state(self) -> None:
        for path in (
            self.state_path,
            self.audit_path,
            self.process_lock_path,
        ):
            if path.is_file() and not path.is_symlink():
                os.chmod(path, 0o600)
        for proposal_dir in self.proposals.iterdir():
            if not proposal_dir.is_dir() or proposal_dir.is_symlink():
                continue
            os.chmod(proposal_dir, 0o700)
            proposal_path = proposal_dir / "proposal.json"
            if proposal_path.is_file() and not proposal_path.is_symlink():
                os.chmod(proposal_path, 0o600)
        for operation_path in self.operations.glob("*.json"):
            if operation_path.is_file() and not operation_path.is_symlink():
                os.chmod(operation_path, 0o600)

    def _purge_legacy_device_credentials(self) -> None:
        """Remove raw device tokens persisted by the earlier staged format."""
        if not self.state_path.exists():
            return
        changed = False
        with self._locked():
            state = self._state()
            current = iso()
            tokens = state.get("tokens")
            if not isinstance(tokens, list):
                tokens = []
            for item in state["deviceAuthorizations"]:
                if not isinstance(item, dict):
                    continue
                raw = item.pop("token", None)
                public_record = item.pop("tokenRecord", None)
                if raw is None and public_record is None:
                    continue
                changed = True
                token_id = (
                    public_record.get("id")
                    if isinstance(public_record, dict)
                    else None
                )
                raw_hash = token_hash(raw) if isinstance(raw, str) else None
                for token in tokens:
                    if not isinstance(token, dict) or token.get("revoked"):
                        continue
                    if (
                        (token_id and token.get("id") == token_id)
                        or (raw_hash and token.get("hash") == raw_hash)
                    ):
                        token["revoked"] = current
                item["legacyCredentialPurged"] = current
            if changed:
                self._write(state)
        if changed:
            self.audit(
                "device.legacy_credential_purged",
                actor="system",
            )

    def recover_interrupted_operations(self) -> None:
        """Fail closed for work abandoned by a previous service process."""
        with self._locked():
            for operation_path in self.operations.glob("*.json"):
                if not operation_path.is_file() or operation_path.is_symlink():
                    continue
                try:
                    operation = _strict_json(operation_path.read_text())
                except (OSError, UnicodeError, ValueError):
                    continue
                if (
                    not isinstance(operation, dict)
                    or operation.get("status") not in {"running", "cancelling"}
                ):
                    continue
                operation.update({
                    "status": "indeterminate",
                    "updated": iso(),
                    "result": None,
                    "error": {
                        "code": "operation.interrupted",
                        "message": (
                            "The service restarted before this operation recorded "
                            "a terminal result. Reconcile target state before retrying."
                        ),
                        "suggestedAction": (
                            "Inspect the operation target and authoritative state "
                            "before retrying."
                        ),
                        "indeterminate": True,
                        "failurePhase": "service-recovery",
                    },
                })
                _atomic_json(operation_path, operation)

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            raise RuntimeError("Control-plane authentication is not initialized. Run ./bin/mapp init.")
        state = _strict_json(self.state_path.read_text())
        state.setdefault("deviceAuthorizations", [])
        return state

    def _write(self, state: dict[str, Any]) -> None:
        _atomic_json(self.state_path, state)

    def initialize(self, password: str, instance_id: str | None = None) -> bool:
        with self._locked():
            if self.state_path.exists():
                return False
            require_password(password)
            self._write({
                "version": 1,
                "instanceId": instance_id or secrets.token_hex(16),
                "adminPassword": password_hash(password),
                "sessions": [],
                "tokens": [],
                "deviceAuthorizations": [],
            })
            self.audit("auth.initialized", actor="local-admin")
            return True

    def instance_id(self) -> str:
        with self._locked():
            return self._state()["instanceId"]

    def pagination_key(self) -> bytes:
        """Return a stable private key for integrity-bound opaque cursors."""
        with self._locked():
            state = self._state()
            material = (
                f"mapp-pagination-v1\0{state['instanceId']}\0"
                f"{state['adminPassword']}"
            ).encode("utf-8")
        return hashlib.sha256(material).digest()

    def _trim_audit(self) -> None:
        try:
            size = self.audit_path.stat().st_size
        except FileNotFoundError:
            return
        if size <= AUDIT_MAX_BYTES:
            return
        with self.audit_path.open("rb") as stream:
            offset = max(0, size - AUDIT_RETAIN_BYTES)
            stream.seek(offset)
            retained = stream.read()
        if offset:
            newline = retained.find(b"\n")
            retained = retained[newline + 1:] if newline >= 0 else b""
        _atomic_bytes(self.audit_path, retained)

    def audit(self, event: str, *, actor: str, remote: str | None = None, details: dict | None = None) -> None:
        record = {
            "time": iso(),
            "event": event,
            "actor": actor,
            "remote": remote,
            "details": details or {},
        }
        encoded = (
            json.dumps(
                record,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
                default=json_default,
            ) + "\n"
        ).encode("utf-8")
        if len(encoded) > AUDIT_RECORD_MAX_BYTES:
            record["details"] = {
                "truncated": True,
                "originalBytes": len(encoded),
            }
            encoded = (
                json.dumps(
                    record,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                    default=json_default,
                ) + "\n"
            ).encode("utf-8")
        with self._locked():
            self._trim_audit()
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.audit_path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())

    def login(self, password: str, remote: str) -> tuple[str, str] | None:
        with self._locked():
            state = self._state()
            if not verify_password(password, state["adminPassword"]):
                self.audit("auth.login_failed", actor="admin", remote=remote)
                return None
            session = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            state["sessions"].append({
                "hash": token_hash(session),
                "csrfHash": token_hash(csrf),
                "created": iso(),
                "lastUsed": iso(),
                "remote": remote,
            })
            self._write(state)
            self.audit("auth.login", actor="admin", remote=remote)
            return session, csrf

    def session(self, session: str | None, csrf: str | None = None, *, require_csrf: bool = False) -> bool:
        if not session:
            return False
        with self._locked():
            state = self._state()
            current = now()
            valid = False
            retained = []
            changed = False
            for item in state["sessions"]:
                created = parse_time(item["created"])
                used = parse_time(item["lastUsed"])
                expired = (
                    not created or not used
                    or (current - created).total_seconds() > SESSION_MAX_SECONDS
                    or (current - used).total_seconds() > SESSION_IDLE_SECONDS
                )
                if expired:
                    changed = True
                    continue
                if hmac.compare_digest(item["hash"], token_hash(session)):
                    if require_csrf and (not csrf or not hmac.compare_digest(item["csrfHash"], token_hash(csrf))):
                        retained.append(item)
                        continue
                    item["lastUsed"] = iso(current)
                    valid = True
                    changed = True
                retained.append(item)
            if changed:
                state["sessions"] = retained
                self._write(state)
            return valid

    def logout(self, session: str | None) -> None:
        if not session:
            return
        with self._locked():
            state = self._state()
            state["sessions"] = [
                item for item in state["sessions"]
                if not hmac.compare_digest(item["hash"], token_hash(session))
            ]
            self._write(state)

    def change_password(self, current: str, replacement: str) -> bool:
        with self._locked():
            state = self._state()
            if not verify_password(current, state["adminPassword"]):
                return False
            require_password(replacement)
            state["adminPassword"] = password_hash(replacement)
            state["sessions"] = []
            self._write(state)
            self.audit("auth.password_changed", actor="admin")
            return True

    def create_token(
        self,
        name: str,
        expires: str | None = None,
        scopes: list[str] | None = None,
    ) -> tuple[str, dict]:
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("Token names must contain 1 to 100 characters.")
        normalized_expiry = None
        if expires is not None:
            if not isinstance(expires, str):
                raise ValueError("Token expiry must be an ISO-8601 timestamp.")
            expiry = parse_time(expires)
            if expiry is None or expiry <= now():
                raise ValueError("Token expiry must be a future ISO-8601 timestamp.")
            normalized_expiry = iso(expiry)
        raw = "mapp_" + secrets.token_urlsafe(32)
        normalized_scopes = ["full"] if scopes is None else scopes
        if (
            not isinstance(normalized_scopes, list)
            or not normalized_scopes
            or any(
                not isinstance(scope, str) or scope not in TOKEN_SCOPES
                for scope in normalized_scopes
            )
        ):
            raise ValueError("Token scopes are invalid.")
        normalized_scopes = list(dict.fromkeys(normalized_scopes))
        if "full" in normalized_scopes and len(normalized_scopes) != 1:
            raise ValueError("The full scope cannot be combined with narrower scopes.")
        record = {
            "id": secrets.token_hex(8),
            "name": name.strip(),
            "hash": token_hash(raw),
            "created": iso(),
            "expires": normalized_expiry,
            "lastUsed": None,
            "revoked": None,
            "scopes": normalized_scopes,
        }
        with self._locked():
            state = self._state()
            state["tokens"].append(record)
            self._write(state)
        self.audit(
            "token.created",
            actor="admin",
            details={
                "id": record["id"],
                "name": record["name"],
                "scopes": record["scopes"],
                "expires": record["expires"],
            },
        )
        return raw, self.public_token(record)

    def start_device_authorization(
        self,
        device_name: str,
        scopes: list[str],
        remote: str,
    ) -> dict:
        if not isinstance(device_name, str) or not device_name.strip() or len(device_name) > 100:
            raise ValueError("Device names must contain 1 to 100 characters.")
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(
                not isinstance(scope, str) or scope not in DEVICE_SCOPES
                for scope in scopes
            )
        ):
            raise ValueError("Requested device scopes are invalid.")
        current = now()
        device_id = secrets.token_urlsafe(24)
        user_code = f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
        record = {
            "idHash": token_hash(device_id),
            "userCode": user_code,
            "deviceName": device_name.strip(),
            "scopes": list(dict.fromkeys(scopes)),
            "created": iso(current),
            "expires": iso(current + dt.timedelta(seconds=DEVICE_AUTH_SECONDS)),
            "remote": remote,
            "status": "pending",
        }
        with self._locked():
            state = self._state()
            active = [
                item for item in state["deviceAuthorizations"]
                if parse_time(item.get("expires")) and parse_time(item["expires"]) > current
            ]
            if sum(
                item.get("remote") == remote and item.get("status") == "pending"
                for item in active
            ) >= 3:
                raise ValueError("Too many pending device authorizations from this client.")
            if len(active) >= 20:
                raise ValueError("The device authorization queue is full.")
            state["deviceAuthorizations"] = [*active, record]
            self._write(state)
        self.audit("device.started", actor="anonymous", remote=remote, details={"userCode": user_code})
        return {
            "deviceId": device_id,
            "userCode": user_code,
            "expiresIn": DEVICE_AUTH_SECONDS,
            "interval": 3,
            "scopes": record["scopes"],
        }

    def list_device_authorizations(self) -> list[dict]:
        with self._locked():
            current = now()
            return [
                {
                    key: item.get(key)
                    for key in ("userCode", "deviceName", "scopes", "created", "expires", "remote", "status")
                }
                for item in self._state()["deviceAuthorizations"]
                if parse_time(item.get("expires")) and parse_time(item["expires"]) > current
            ]

    def approve_device_authorization(self, user_code: str) -> bool:
        approved = False
        approved_details = None
        with self._locked():
            state = self._state()
            current = now()
            for item in state["deviceAuthorizations"]:
                if (
                    item.get("userCode") == user_code
                    and item.get("status") == "pending"
                    and parse_time(item.get("expires"))
                    and parse_time(item["expires"]) > current
                ):
                    item.update({
                        "status": "approved",
                        "approved": iso(current),
                    })
                    self._write(state)
                    approved = True
                    approved_details = {
                        "userCode": user_code,
                        "deviceName": item["deviceName"],
                        "scopes": item["scopes"],
                    }
                    break
        if approved:
            self.audit(
                "device.approved",
                actor="admin",
                details=approved_details,
            )
        return approved

    def poll_device_authorization(self, device_id: str) -> dict:
        digest = token_hash(device_id)
        issued: tuple[str, dict, str] | None = None
        with self._locked():
            state = self._state()
            current = now()
            for item in state["deviceAuthorizations"]:
                if not hmac.compare_digest(str(item.get("idHash", "")), digest):
                    continue
                expiry = parse_time(item.get("expires"))
                if not expiry or expiry <= current:
                    return {"status": "expired"}
                status = item.get("status")
                if status == "pending":
                    return {"status": "pending"}
                if status == "consumed":
                    return {"status": "consumed"}
                if status != "approved":
                    return {"status": "invalid"}

                scopes = item.get("scopes")
                if (
                    not isinstance(scopes, list)
                    or not scopes
                    or any(
                        not isinstance(scope, str) or scope not in DEVICE_SCOPES
                        for scope in scopes
                    )
                ):
                    return {"status": "invalid"}
                raw = "mapp_" + secrets.token_urlsafe(32)
                record = {
                    "id": secrets.token_hex(8),
                    "name": f"Device: {item['deviceName']}",
                    "hash": token_hash(raw),
                    "created": iso(current),
                    "expires": iso(
                        current + dt.timedelta(seconds=DEVICE_TOKEN_SECONDS)
                    ),
                    "lastUsed": None,
                    "revoked": None,
                    "scopes": list(dict.fromkeys(scopes)),
                }
                state["tokens"].append(record)
                item.update({
                    "status": "consumed",
                    "consumed": iso(current),
                    "tokenId": record["id"],
                })
                self._write(state)
                issued = (raw, self.public_token(record), str(item.get("remote", "")))
                break
        if issued:
            raw, record, remote = issued
            self.audit(
                "device.token_issued",
                actor=f"token:{record['id']}",
                remote=remote,
                details={"id": record["id"], "scopes": record["scopes"]},
            )
            return {"status": "authorized", "token": raw, "record": record}
        return {"status": "invalid"}

    def create_operation(self, kind: str, actor: str, target: dict | None = None) -> dict:
        operation = {
            "id": secrets.token_hex(16),
            "kind": kind,
            "status": "running",
            "actor": actor,
            "target": target or {},
            "created": iso(),
            "updated": iso(),
            "result": None,
            "error": None,
        }
        with self._locked():
            existing = sorted(
                (
                    path for path in self.operations.glob("*.json")
                    if path.is_file() and not path.is_symlink()
                ),
                key=lambda path: path.stat().st_mtime_ns,
            )
            for stale in existing[:-499]:
                stale.unlink()
            _atomic_json(self.operations / f"{operation['id']}.json", operation)
        return operation

    def finish_operation(
        self,
        operation_id: str,
        *,
        status: str,
        result: dict | None = None,
        error: dict | None = None,
    ) -> dict:
        if status not in {"succeeded", "failed", "cancelled", "indeterminate"}:
            raise ValueError("Invalid terminal operation status.")
        with self._locked():
            operation = self.read_operation(operation_id)
            if operation.get("status") in {
                "succeeded", "failed", "cancelled", "indeterminate",
            }:
                return operation
            operation.update({
                "status": status,
                "updated": iso(),
                "result": result,
                "error": error,
            })
            _atomic_json(self.operations / f"{operation_id}.json", operation)
            return operation

    def request_operation_cancellation(self, operation_id: str) -> dict:
        """Record a cancellation request without claiming rollback yet."""
        with self._locked():
            operation = self.read_operation(operation_id)
            if operation.get("status") != "running":
                return operation
            operation.update({
                "status": "cancelling",
                "updated": iso(),
                "cancellationRequested": iso(),
            })
            _atomic_json(self.operations / f"{operation_id}.json", operation)
            return operation

    def read_operation(self, operation_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise FileNotFoundError("Operation not found.")
        path = self.operations / f"{operation_id}.json"
        if not path.is_file():
            raise FileNotFoundError("Operation not found.")
        return _strict_json(path.read_text())

    @staticmethod
    def public_token(record: dict) -> dict:
        return {key: record.get(key) for key in ("id", "name", "created", "expires", "lastUsed", "revoked", "scopes")}

    def list_tokens(self) -> list[dict]:
        with self._locked():
            return [
                self.public_token(item)
                for item in self._state()["tokens"]
            ]

    def authenticate_token(self, raw: str | None, remote: str) -> dict | None:
        if not raw:
            return None
        digest = token_hash(raw)
        audit_failure = False
        with self._locked():
            state = self._state()
            current = now()
            found = None
            changed = False
            for item in state["tokens"]:
                try:
                    expiry = parse_time(item.get("expires"))
                except (TypeError, ValueError):
                    if not item.get("revoked"):
                        item["revoked"] = iso(current)
                        changed = True
                    continue
                if (
                    not item.get("revoked")
                    and (not expiry or expiry > current)
                    and hmac.compare_digest(item["hash"], digest)
                ):
                    item["lastUsed"] = iso(current)
                    found = self.public_token(item)
                    break
            if found or changed:
                self._write(state)
            if found:
                return found
            monotonic = time.monotonic()
            if (
                monotonic - self._last_failed_token_audit
                >= FAILED_TOKEN_AUDIT_INTERVAL
            ):
                self._last_failed_token_audit = monotonic
                audit_failure = True
        if audit_failure:
            self.audit("token.auth_failed", actor="unknown", remote=remote)
        return None

    def revoke_token(self, token_id: str) -> bool:
        with self._locked():
            state = self._state()
            found = False
            for item in state["tokens"]:
                if item["id"] == token_id and not item.get("revoked"):
                    item["revoked"] = iso()
                    found = True
            if found:
                self._write(state)
                self.audit("token.revoked", actor="admin", details={"id": token_id})
            return found

    def sessions(self) -> list[dict]:
        with self._locked():
            return [
                {
                    key: item.get(key)
                    for key in ("created", "lastUsed", "remote")
                }
                for item in self._state()["sessions"]
            ]

    def audit_tail(self, limit: int = 200) -> list[dict]:
        with self._locked():
            try:
                size = self.audit_path.stat().st_size
            except FileNotFoundError:
                return []
            with self.audit_path.open("rb") as stream:
                offset = max(0, size - AUDIT_READ_BYTES)
                stream.seek(offset)
                raw = stream.read()
            if offset:
                newline = raw.find(b"\n")
                raw = raw[newline + 1:] if newline >= 0 else b""
            lines = raw.decode("utf-8").splitlines()[
                -max(1, min(limit, 1000)):
            ]
            return [_strict_json(line) for line in lines]

    def reset_password(self, password: str) -> None:
        with self._locked():
            require_password(password)
            state = self._state()
            state["adminPassword"] = password_hash(password)
            state["sessions"] = []
            self._write(state)
            self.audit("auth.password_reset", actor="local-admin")

    def revoke_all(self) -> None:
        with self._locked():
            state = self._state()
            for item in state["tokens"]:
                if not item.get("revoked"):
                    item["revoked"] = iso()
            self._write(state)
            self.audit("token.revoked_all", actor="local-admin")
