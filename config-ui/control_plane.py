from __future__ import annotations

import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any


UTC = dt.timezone.utc
PBKDF2_ROUNDS = 310_000
SESSION_IDLE_SECONDS = 30 * 60
SESSION_MAX_SECONDS = 12 * 60 * 60
MIN_PASSWORD_LENGTH = 12
AUDIT_MAX_BYTES = 10 * 1024 * 1024
AUDIT_RETAIN_BYTES = 5 * 1024 * 1024
AUDIT_READ_BYTES = 2 * 1024 * 1024
AUDIT_RECORD_MAX_BYTES = 64 * 1024
FAILED_TOKEN_AUDIT_INTERVAL = 60

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

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            raise RuntimeError("Control-plane authentication is not initialized. Run ./bin/mapp init.")
        return _strict_json(self.state_path.read_text())

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
            })
            self.audit("auth.initialized", actor="local-admin")
            return True

    def instance_id(self) -> str:
        with self._locked():
            return self._state()["instanceId"]

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
            for item in state["sessions"]:
                created = parse_time(item["created"])
                used = parse_time(item["lastUsed"])
                expired = (
                    not created or not used
                    or (current - created).total_seconds() > SESSION_MAX_SECONDS
                    or (current - used).total_seconds() > SESSION_IDLE_SECONDS
                )
                if expired:
                    continue
                if hmac.compare_digest(item["hash"], token_hash(session)):
                    if require_csrf and (not csrf or not hmac.compare_digest(item["csrfHash"], token_hash(csrf))):
                        retained.append(item)
                        continue
                    item["lastUsed"] = iso(current)
                    valid = True
                retained.append(item)
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

    def create_token(self, name: str, expires: str | None = None) -> tuple[str, dict]:
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
        record = {
            "id": secrets.token_hex(8),
            "name": name.strip(),
            "hash": token_hash(raw),
            "created": iso(),
            "expires": normalized_expiry,
            "lastUsed": None,
            "revoked": None,
            "scopes": ["full"],
        }
        with self._locked():
            state = self._state()
            state["tokens"].append(record)
            self._write(state)
        self.audit("token.created", actor="admin", details={"id": record["id"], "name": name})
        return raw, self.public_token(record)

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
