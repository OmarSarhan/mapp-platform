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

import control_schema


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
    # `derive:manage` is separate from `derive` and both are issuable. Phase 1
    # wave 1 split them and gave the derived-layer write routes the narrower
    # one, but left this vocabulary alone -- so no CLI token could be issued
    # with it, and every `derived-layers` write refused with
    # `auth.scope_required` for anyone not carrying `full`. That included
    # `./bin/mapp demo`, which is where it surfaced.
    "inspect", "propose", "visual", "apply", "reload", "derive",
    "derive:manage",
    "semantic:inspect", "semantic:source", "semantic:generate",
    "semantic:data", "semantic:propose", "semantic:apply", "semantic:admin",
    "federation:register", "federation:provision", "federation:observe",
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


#: Risk classes a standing approval window may cover, and the only ones.
#:
#: An allowlist rather than an exemption list, which is the opposite direction
#: from NO_APPROVAL_RISKS above and deliberately so. There, the safe default is
#: "requires approval", so what is written down is the exemption. Here the safe
#: default is "no window may decide this", so what is written down is the
#: permission -- a risk class nobody has considered cannot be auto-approved.
#:
#: These four are the specification's *Workspace mutation* permission class:
#: apply, reload and the managed-relation lifecycle. What is deliberately
#: absent is broader than a list of scopes, because the specification excludes
#: whole classes: *Semantic administration* (`semantic-apply`,
#: `semantic-archive`, `semantic-repair`) and *Federation mutation*
#: (`federation-register`, `federation-provision`, `federation-observe`) each
#: require a per-action decision, always. `federation:retire` is the reason
#: the unit is an action class and not a scope -- it is not a scope at all,
#: since the platform's retire action runs under `federation:provision`.
WINDOWABLE_ACTION_CLASSES = frozenset({
    "apply",
    "reload",
    "database-definition",
    "database-refresh",
})


class ControlStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
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
        #: Nothing connects in the constructor. config-ui builds this store at
        #: module import (app.py) and has no depends_on for the database, so
        #: connecting eagerly would make the service unstartable whenever
        #: PostgreSQL is merely slower to come up.
        self._migrated = False
        self._db_lock = threading.RLock()
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
                finished = iso()
                operation.update({
                    "status": "indeterminate",
                    "updated": finished,
                    "finished": finished,
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

    @contextlib.contextmanager
    def _db(self, *, migrate: bool = True):
        """A connection to the control schema, closed when the call ends.

        One connection per operation rather than one held per store. Holding
        one open is tempting -- these are short statements -- but a store is
        constructed per process and per test, and a held connection is only
        released when the object is collected, which exhausted the role's
        CONNECTION LIMIT of 8 as soon as more than a handful existed at once.
        Connecting per call keeps that limit meaningful; the cost is a few
        milliseconds on a dashboard request.

        ``migrate=False`` is for the operations that are *about* the schema
        version. Migrating on first use is right for ordinary work -- the store
        is built at module import, so an eager connect would break every suite
        that never touches the control plane -- but it is exactly wrong for the
        ladder commands. `migrate-rollback --to N` without `--confirm` promises
        to print a plan and change nothing, and it was applying the entire
        forward ladder before computing that plan: a schema at version 4 came
        back at 6, and the command then exited 2 saying nothing had happened.
        The recovery-epoch precondition had the same defect from the same
        cause: it checks for migration 5 on a connection that had already
        migrated past it, so the refusal the restore procedure documents could
        never fire.
        """
        connection = control_schema.connect()
        try:
            if migrate:
                with self._db_lock:
                    if not self._migrated:
                        control_schema.migrate(connection)
                        self._migrated = True
            yield connection
        finally:
            connection.close()

    def _require_initialized(self, connection) -> str:
        row = connection.execute(
            "SELECT encoded FROM control.admin_credential WHERE id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "Control-plane authentication is not initialized. Run ./bin/mapp init."
            )
        return row["encoded"]

    def initialize(self, password: str, instance_id: str | None = None) -> bool:
        """Create the administrator credential, or report it already exists.

        The insert is conditional rather than a read followed by a write, so two
        simultaneous first starts cannot both believe they initialised the
        store -- which would print two different passwords, only one of which
        works.
        """
        require_password(password)
        with self._db() as connection:
            row = connection.execute(
                "INSERT INTO control.admin_credential(id, encoded)"
                " VALUES(1, %s) ON CONFLICT (id) DO NOTHING"
                " RETURNING encoded",
                (password_hash(password),),
            ).fetchone()
            # Ahead of the early return, not after it. An instance that
            # already has a credential can still be missing its identity, and
            # this is precisely the command the error for that state tells the
            # operator to run -- so it has to be the command that fixes it.
            connection.execute(
                "INSERT INTO control.metadata(key, value) VALUES('instance_id', %s)"
                " ON CONFLICT (key) DO NOTHING",
                (instance_id or secrets.token_hex(16),),
            )
            if row is None:
                return False
        self.audit("auth.initialized", actor="local-admin")
        return True

    def instance_id(self) -> str:
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "SELECT value FROM control.metadata WHERE key = 'instance_id'"
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "Control-plane authentication is not initialized. Run ./bin/mapp init."
                )
            return row["value"]

    def ensure_oauth_client(
        self,
        client_id: str,
        client_secret: str,
        *,
        name: str,
        capabilities: tuple[str, ...] = (),
    ) -> None:
        """Provision this service's own confidential client, idempotently.

        The authorization component authenticates every caller of its control
        listener as an OAuth confidential client, and Phase 0 has no client
        registration endpoint -- RFC 7591 is Phase 1. That gap is deliberate
        for *agent* clients, which must be registered by a person. It is not a
        reason for this service to be unable to talk to the component: the
        client here is the configuration API itself, provisioned by the service
        that owns the schema the row lives in. control_schema.py declares
        control.oauth_clients, and this module runs its migrations, so writing
        one row for ourselves is the same class of act.

        The secret is stored as a sha256 digest because that is what the
        component's Client.check_client_secret compares against -- a client
        secret at rest is a credential. No redirect URIs, no scopes and no
        grant types: this client authenticates to the control listener and can
        do nothing else. It never appears in an authorization request, so a
        redirect URI would be a capability with no purpose.
        """
        if not client_id or not client_secret:
            raise ValueError("An OAuth client id and secret are both required.")
        digest = hashlib.sha256(client_secret.encode("utf-8")).hexdigest()
        with self._db() as connection:
            self._require_initialized(connection)
            connection.execute(
                "INSERT INTO control.oauth_clients"
                "(client_id, name, redirect_uris, scopes, grant_types,"
                " token_endpoint_auth_method, client_secret_hash, capabilities,"
                " disabled_at)"
                " VALUES(%s,%s,'{}','{}','{}','client_secret_basic',%s,%s,NULL)"
                " ON CONFLICT (client_id) DO UPDATE SET"
                "   name = EXCLUDED.name,"
                "   client_secret_hash = EXCLUDED.client_secret_hash,"
                "   token_endpoint_auth_method"
                "     = EXCLUDED.token_endpoint_auth_method,"
                # Re-enables a client an operator disabled. That is the
                # intent: the secret is supplied by the deployment, so a
                # restart with a valid secret is the deployment asserting this
                # client should work.
                "   capabilities = EXCLUDED.capabilities,"
                "   disabled_at = NULL",
                (client_id, name, digest, list(capabilities)),
            )

    # -- schema ladder ---------------------------------------------------

    def rollback_plan(self, to_version: int) -> dict:
        """What a rollback to ``to_version`` would undo, and what it would cost.

        Read-only. Exists so an operator sees the price before paying it, and
        so the warning is *computed from the ledger* rather than written out by
        hand -- a hardcoded paragraph goes stale the first time a migration is
        added, and this one is read under pressure.
        """
        with self._db(migrate=False) as connection:
            applied = control_schema.applied_versions(connection)
        undo = [version for version in sorted(applied, reverse=True) if version > to_version]
        return {
            "applied": applied,
            "undo": undo,
            "losses": {
                version: control_schema.DESTRUCTIVE_ROLLBACKS[version]
                for version in undo
                if version in control_schema.DESTRUCTIVE_ROLLBACKS
            },
            "missing": [
                version
                for version in undo
                if version not in control_schema.ROLLBACKS
            ],
        }

    def rollback_schema(self, to_version: int, *, accept_data_loss: bool) -> list[int]:
        """Step the schema ladder down. Returns the versions undone."""
        with self._db(migrate=False) as connection:
            return control_schema.rollback(
                connection, to_version, accept_data_loss=accept_data_loss
            )

    # -- recovery epoch --------------------------------------------------

    def recovery_epoch(self) -> int:
        with self._db(migrate=False) as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "SELECT control.current_recovery_epoch() AS epoch"
            ).fetchone()
            return int(row["epoch"])

    def advance_recovery_epoch(self, *, reason: str = "restore") -> dict:
        """Invalidate every credential minted before now. Returns what it did.

        The control for the one hole a database restore opens: a snapshot
        contains credentials that were valid when it was taken, including ones
        revoked since, so restoring it hands them back. Phase 1 requires that a
        restore "cannot make a pre-restore grant/A mapping, refresh family or B
        usable", and this is how.

        Applied once, at restore, rather than as a predicate on every read.
        The epoch is bumped and everything stamped below it is invalidated in
        the same transaction, so ordinary reads -- which already filter on a
        revocation -- need no knowledge of any of this.

        Two shapes of invalidation, because the tables do not share a column.
        Anything with a revocation records one, so the trail survives and a
        replay is still distinguishable from an unrecognised credential.
        Sessions, one-shot codes and in-flight authorizations are removed:
        none has a revoked_at, and none is worth keeping as evidence. `tokens`
        is revoked rather than removed because a revoked token's
        case-insensitive name must stay reserved.

        Every live credential is invalidated, not only those stamped below the
        new epoch. That is deliberate, and it took a surviving mutation to see
        why: at sweep time nothing can legitimately carry the new epoch, so an
        epoch predicate selects exactly the same rows as "all live" -- except
        in one case, where it selects fewer. Restoring a *newer* snapshot over
        an older database leaves rows stamped above the current counter, and a
        `recovery_epoch < new` filter would leave precisely those live. They
        are credentials from a state this database does not recognise, so they
        are the last ones that should survive.

        The stamp therefore records which restore era a credential was minted
        in -- useful when reading an audit trail, and what makes the column
        default worth having -- but it is not the filter. The filter is "is it
        live", and the reason that is safe is that this runs once, inside the
        advance, so anything minted afterwards comes after the sweep.

        **Assumes nothing else is minting credentials.** A restore is a
        quiesced operation; a credential created between the sweep and the
        commit would survive it. Two concurrent advances cannot interleave: the
        counter upsert takes a row lock held to commit, which serialises the
        whole transaction -- measured, rather than assumed, which is why there
        is no separate advisory lock here.
        """
        revoked: dict[str, int] = {}
        deleted: dict[str, int] = {}
        with self._db(migrate=False) as connection:
            self._require_initialized(connection)
            connection.execute("BEGIN")
            try:
                # The precondition is the function, not the row. If migration
                # 5 has not run there is no mechanism at all and an advance
                # would silently do nothing useful.
                present = connection.execute(
                    "SELECT 1 FROM pg_proc p"
                    "  JOIN pg_namespace n ON n.oid = p.pronamespace"
                    " WHERE n.nspname = %s AND p.proname = 'current_recovery_epoch'",
                    (control_schema.SCHEMA,),
                ).fetchone()
                if present is None:
                    raise RuntimeError(
                        "The recovery epoch is not available; migration 5 has"
                        " not been applied, so a restore cannot invalidate"
                        " pre-restore credentials."
                    )
                # Upsert, not update. A missing row means zero -- which is what
                # current_recovery_epoch() already reports -- and an absent row
                # must not be able to brick the one control that matters at
                # restore time.
                row = connection.execute(
                    "INSERT INTO control.metadata(key, value)"
                    " VALUES('recovery_epoch', '1')"
                    " ON CONFLICT (key) DO UPDATE"
                    "   SET value = ((control.metadata.value::bigint) + 1)::text"
                    " RETURNING value::bigint AS epoch"
                ).fetchone()
                epoch = int(row["epoch"])
                # Which of them can record *why*. Only some carry a
                # revoked_reason: oauth_grants and oauth_refresh_families do,
                # `tokens` and `oauth_tokens` do not. Derived rather than
                # listed, so a migration that adds the column starts being
                # used without anyone remembering to update a constant -- and
                # so this cannot assume a column that is not there, which is
                # what a first attempt at recording the reason did.
                reasoned = {
                    row["table_name"]
                    for row in connection.execute(
                        "SELECT table_name FROM information_schema.columns"
                        " WHERE table_schema = %s AND column_name = 'revoked_reason'",
                        (control_schema.SCHEMA,),
                    ).fetchall()
                }
                note = f"recovery-epoch:{reason}"[:200]
                for table in control_schema.EPOCH_REVOKED_TABLES:
                    if table in reasoned:
                        statement = control_schema.sql.SQL(
                            "UPDATE {schema}.{table}"
                            "   SET revoked_at = now(), revoked_reason = %s"
                            " WHERE revoked_at IS NULL"
                        )
                        parameters = (note,)
                    else:
                        statement = control_schema.sql.SQL(
                            "UPDATE {schema}.{table} SET revoked_at = now()"
                            " WHERE revoked_at IS NULL"
                        )
                        parameters = ()
                    result = connection.execute(
                        statement.format(
                            schema=control_schema.sql.Identifier(control_schema.SCHEMA),
                            table=control_schema.sql.Identifier(table),
                        ),
                        parameters,
                    )
                    revoked[table] = result.rowcount
                for table in control_schema.EPOCH_DELETED_TABLES:
                    result = connection.execute(
                        control_schema.sql.SQL(
                            "DELETE FROM {schema}.{table}"
                        ).format(
                            schema=control_schema.sql.Identifier(control_schema.SCHEMA),
                            table=control_schema.sql.Identifier(table),
                        ),
                    )
                    deleted[table] = result.rowcount
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        self.audit(
            "control.recovery_epoch_advanced",
            actor="operator",
            details={"epoch": epoch, "reason": reason,
                     "revoked": revoked, "deleted": deleted},
        )
        return {"epoch": epoch, "revoked": revoked, "deleted": deleted}

    # -- agent OAuth clients ---------------------------------------------

    #: Hosts for which plain http is an acceptable redirect target. RFC 8252
    #: s7.3 makes loopback the native-app pattern; anything else on http would
    #: carry an authorization code over plaintext.
    LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

    @classmethod
    def check_redirect_uri(cls, value: str) -> str:
        """Validate one redirect URI, or refuse.

        The authorization server matches these exactly -- no prefix, no
        wildcard -- so a permissive entry here is the whole vulnerability: a
        redirect URI an attacker can influence is a working way to have
        authorization codes delivered to them.
        """
        from urllib.parse import urlsplit

        if not isinstance(value, str) or not value.strip():
            raise ValueError("A redirect URI is required.")
        # Checked before stripping, not after. Stripping first silently accepted
        # a trailing CRLF as the stripped value -- a normalization where the
        # rule everywhere else here is refusal, and the one input shape that
        # matters most is the one that looks like header injection.
        if any(character in value for character in ("\r", "\n", "\t", "\x00")):
            raise ValueError("Redirect URI contains a control character.")
        # A plain leading or trailing space is shell copy-paste, so it is
        # trimmed; an interior space is not, and is refused below.
        value = value.strip(" ")
        if " " in value:
            raise ValueError(f"Redirect URI contains whitespace: {value!r}")
        parsed = urlsplit(value)
        if not parsed.scheme:
            raise ValueError(f"Redirect URI must be absolute: {value!r}")
        if parsed.fragment:
            # RFC 6749 s3.1.2: the endpoint URI must not include a fragment.
            raise ValueError(f"Redirect URI must not carry a fragment: {value!r}")
        if parsed.username or parsed.password:
            raise ValueError(f"Redirect URI must not carry userinfo: {value!r}")
        scheme = parsed.scheme.lower()
        if scheme == "http" and parsed.hostname not in cls.LOOPBACK_HOSTS:
            raise ValueError(
                f"Redirect URI must use https unless it is loopback: {value!r}"
            )
        if scheme in {"http", "https"}:
            if not parsed.netloc:
                raise ValueError(f"Redirect URI has no host: {value!r}")
        elif "." not in scheme:
            # RFC 8252 s7.1: a native app's private-use scheme is a reverse-DNS
            # name it controls. Without that rule the "any other scheme"
            # allowance admitted javascript:, data: and file: -- which have no
            # business being a redirect target and were accepted outright.
            raise ValueError(
                f"A private-use redirect scheme must be a reverse-DNS name the"
                f" application controls, as RFC 8252 requires: {value!r}"
            )
        # Compared against the raw text, not against parsed.scheme: urlsplit
        # lower-cases the scheme itself, so comparing the two is comparing a
        # value with itself and the normalisation never fired.
        raw_scheme = value[: len(scheme)]
        if raw_scheme != scheme:
            # Schemes are case-insensitive (RFC 3986 s3.1) but the
            # authorization server matches redirect URIs byte for byte, so a
            # stored mixed-case scheme would only ever match a client that
            # repeated the same casing.
            value = scheme + value[len(scheme):]
        return value

    @staticmethod
    def check_scope(value: str) -> str:
        """Validate a scope's shape, not its membership of a vocabulary.

        Deliberately not checked against a list here. The authorization server
        decides which scopes it will issue, and it derives that from the
        operation allowlist; restating the vocabulary in a third place is the
        drift this platform has already been bitten by. A scope the server will
        not issue produces a refusal at the authorization request, which is a
        clear failure rather than a silent one.
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError("A scope is required.")
        value = value.strip()
        if any(character.isspace() for character in value):
            raise ValueError(f"A scope may not contain whitespace: {value!r}")
        if not all(character.isprintable() for character in value):
            raise ValueError(f"A scope must be printable: {value!r}")
        return value

    def register_oauth_client(
        self, *, name: str, redirect_uris, scopes, actor: str = "local-admin"
    ) -> str:
        """Register a public agent client and return its generated id.

        Public, not confidential: an agent is a native or desktop application
        that cannot keep a secret, so it authenticates with PKCE alone -- the
        authorization server requires S256 of every client, including
        confidential ones. Issuing a secret here would create a credential that
        has to live on the operator's machine and could not be protected.

        Registration is deliberately an operator command rather than an
        endpoint. RFC 7591 dynamic registration would let a client register
        itself, and P2 requires one *pinned* client per ecosystem -- a person
        decides which agent may ask for consent.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A client name is required.")
        uris = tuple(self.check_redirect_uri(item) for item in redirect_uris)
        if not uris:
            raise ValueError("At least one redirect URI is required.")
        if len(set(uris)) != len(uris):
            raise ValueError("Duplicate redirect URIs.")
        wanted = tuple(self.check_scope(item) for item in scopes)
        if not wanted:
            raise ValueError("At least one scope is required.")
        if len(set(wanted)) != len(wanted):
            raise ValueError("Duplicate scopes.")
        for reserved in ("full", "admin"):
            if reserved in wanted:
                # Never issued for the MCP resource, and `full` is on the
                # broker's hard deny-list, so a client holding it could reach
                # every unclassified route.
                raise ValueError(f"The {reserved!r} scope is never issued to an agent.")
        client_id = "mcp-" + secrets.token_urlsafe(12)
        with self._db() as connection:
            self._require_initialized(connection)
            connection.execute(
                "INSERT INTO control.oauth_clients"
                "(client_id, name, redirect_uris, scopes, grant_types,"
                " token_endpoint_auth_method, client_secret_hash)"
                # Both grant types. The authorization code is how a grant
                # begins; refresh is how it survives token A's 15 minutes
                # without sending the operator back to the consent screen
                # every quarter of an hour. The grant type is also what
                # MappRefreshTokenGrant checks before accepting a refresh, so
                # omitting it here would issue a credential and then refuse it.
                " VALUES(%s,%s,%s,%s,'{authorization_code,refresh_token}',"
                "'none',NULL)",
                (client_id, name.strip(), list(uris), list(wanted)),
            )
        # Auditable because it is an authorization decision: an operator has
        # decided which agent may ask for consent at all. Nothing here is
        # secret -- a public client has no secret, and the client id is the
        # value the operator hands to the user to put in their configuration.
        self.audit(
            "oauth.client_registered",
            actor=actor,
            details={
                "clientId": client_id,
                "name": name.strip(),
                "scopes": list(wanted),
                "redirectUris": list(uris),
            },
        )
        return client_id

    def list_oauth_clients(self) -> list[dict]:
        with self._db() as connection:
            self._require_initialized(connection)
            rows = connection.execute(
                "SELECT client_id, name, redirect_uris, scopes,"
                " token_endpoint_auth_method, created_at, disabled_at"
                " FROM control.oauth_clients ORDER BY created_at, client_id"
            ).fetchall()
        return [
            {
                "clientId": row["client_id"],
                "name": row["name"],
                "redirectUris": list(row["redirect_uris"]),
                "scopes": list(row["scopes"]),
                "confidential": row["token_endpoint_auth_method"] != "none",
                "created": iso(row["created_at"]),
                "disabled": iso(row["disabled_at"]) if row["disabled_at"] else None,
            }
            for row in rows
        ]

    def list_oauth_grants(self) -> list[dict]:
        """Every consent an operator has given, live or withdrawn.

        A grant is what an administrator actually approved on the consent
        screen: one client, one scope set, at one moment. Registering a client
        grants nothing -- this is the record of the separate act that did.

        The live-family count is the difference between "this consent exists"
        and "something is still using it": a grant whose families are all spent
        or revoked cannot mint another access token, even before it is revoked.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            rows = connection.execute(
                "SELECT g.grant_id, g.client_id, c.name AS client_name,"
                "       g.subject, g.scopes, g.created_at, g.revoked_at,"
                "       g.revoked_reason,"
                "       (SELECT count(*) FROM control.oauth_refresh_families f"
                "         WHERE f.grant_id = g.grant_id"
                "           AND f.revoked_at IS NULL) AS live_families"
                "  FROM control.oauth_grants g"
                "  JOIN control.oauth_clients c ON c.client_id = g.client_id"
                " ORDER BY g.created_at DESC, g.grant_id"
            ).fetchall()
        return [
            {
                "grantId": row["grant_id"],
                "clientId": row["client_id"],
                "clientName": row["client_name"],
                "subject": row["subject"],
                "scopes": list(row["scopes"]),
                "created": iso(row["created_at"]),
                "revoked": iso(row["revoked_at"]) if row["revoked_at"] else None,
                "revokedReason": row["revoked_reason"],
                "liveFamilies": int(row["live_families"]),
            }
            for row in rows
        ]

    def revoke_oauth_grant(
        self, grant_id: str, *, reason: str = "", actor: str = "local-admin"
    ) -> bool:
        """Withdraw a consent, reporting whether this call was the one that did it.

        This is the lever that actually stops an agent, and it is not the same
        as disabling its client. Disabling says "this software may no longer
        ask"; revoking says "what it was already allowed to do is withdrawn",
        and it works even while the client stays registered and usable by
        somebody else.

        One conditional statement, so two operators revoking at once cannot
        both believe they acted. Nothing else has to be touched for the
        revocation to bite: introspection, the exchange and both halves of the
        token-B surface all resolve the grant, so this single write invalidates
        every credential derived from it -- including a token B already issued
        and not yet spent, which is the difference between the design's claim
        and an aspiration.

        The refresh families are revoked for legibility rather than
        correctness. The rotation statement joins the grant and would refuse
        them anyway; leaving them live-looking only gives an operator something
        to puzzle over.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "UPDATE control.oauth_grants"
                "   SET revoked_at = now(), revoked_reason = %s"
                " WHERE grant_id = %s AND revoked_at IS NULL"
                " RETURNING grant_id, client_id",
                (reason or None, grant_id),
            ).fetchone()
            families = 0
            approvals = 0
            windows = 0
            if row is not None:
                families = connection.execute(
                    "UPDATE control.oauth_refresh_families"
                    "   SET revoked_at = now(), revoked_reason = %s"
                    " WHERE grant_id = %s AND revoked_at IS NULL",
                    (reason or None, grant_id),
                ).rowcount
                # In this transaction rather than after it. An approved receipt
                # that has not been spent is authority the grant produced, and
                # a revocation that reached the grant but not the receipt would
                # leave the one credential most worth revoking behind.
                approvals = connection.execute(
                    "UPDATE control.approvals"
                    "   SET revoked_at = now(), revoked_reason = %s"
                    " WHERE grant_id = %s AND revoked_at IS NULL"
                    "   AND status IN ('pending', 'approved')",
                    ((reason or "grant revoked")[:200], grant_id),
                ).rowcount
                # And in the same transaction again, for a stronger reason. An
                # unspent receipt authorises one request; a live window keeps
                # deciding, so a revocation that left one standing would let a
                # revoked grant go on auto-approving until the clock ran out.
                windows = connection.execute(
                    "UPDATE control.approval_windows"
                    "   SET revoked_at = now(), revoked_reason = %s"
                    " WHERE grant_id = %s AND revoked_at IS NULL",
                    ((reason or "grant revoked")[:200], grant_id),
                ).rowcount
        if row is None:
            return False
        self.audit(
            "oauth.grant_revoked",
            actor=actor,
            details={
                "grantId": grant_id,
                "clientId": row["client_id"],
                "reason": reason or "unspecified",
                "refreshFamiliesRevoked": families,
                "approvalsRevoked": approvals,
                "windowsRevoked": windows,
            },
        )
        return True

    def disable_oauth_client(self, client_id: str, *, actor: str = "local-admin") -> bool:
        """Disable a client, reporting whether this call was the one that did it.

        Disabling takes effect immediately at introspection, at the exchange
        and for an already-issued token B, because the checks resolve the
        grant's client rather than the token's. It does not revoke the grants
        themselves: an operator withdrawing a client is not necessarily
        withdrawing the consents, and revoking a grant is a separate act with
        its own audit meaning.

        Refuses a confidential client. Those are service identities rather than
        agents -- the configuration API's own row is the only one -- and
        ensure_oauth_client re-enables them on every start-up from the
        deployment's secret, so disabling one here would quietly undo itself at
        the next restart. Clearing MCP_AUTH_CLIENT_SECRET is the lever for
        that, and it turns the feature off rather than leaving it half on.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            existing = connection.execute(
                "SELECT token_endpoint_auth_method FROM control.oauth_clients"
                " WHERE client_id = %s",
                (client_id,),
            ).fetchone()
            if (
                existing is not None
                and existing["token_endpoint_auth_method"] != "none"
            ):
                raise ValueError(
                    f"{client_id!r} is a confidential service client, not an"
                    " agent. Disabling it here would be undone at the next"
                    " start-up; clear MCP_AUTH_CLIENT_SECRET instead."
                )
            row = connection.execute(
                "UPDATE control.oauth_clients SET disabled_at = now()"
                " WHERE client_id = %s AND disabled_at IS NULL"
                " RETURNING client_id",
                (client_id,),
            ).fetchone()
        disabled = row is not None
        if disabled:
            # Only when this call was the one that did it. Auditing an
            # already-disabled client would record a withdrawal that did not
            # happen, and the return value exists to tell them apart.
            self.audit(
                "oauth.client_disabled",
                actor=actor,
                details={"clientId": client_id},
            )
        return disabled

    def pagination_key(self) -> bytes:
        """Return a stable private key for integrity-bound opaque cursors.

        The material is the instance id and the *encoded* credential exactly as
        stored -- not a re-hash of it. Changing the administrator password
        therefore invalidates outstanding cursors, which is existing behaviour
        and must not change silently: the credential has to move between stores
        byte-for-byte or every issued cursor stops verifying.
        """
        with self._db() as connection:
            encoded = self._require_initialized(connection)
            row = connection.execute(
                "SELECT value FROM control.metadata WHERE key = 'instance_id'"
            ).fetchone()
            material = (
                f"mapp-pagination-v1\0{row['value']}\0{encoded}"
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
        with self._db() as connection:
            encoded = self._require_initialized(connection)
            if not verify_password(password, encoded):
                self.audit("auth.login_failed", actor="admin", remote=remote)
                return None
            session = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(24)
            current = now()
            connection.execute(
                "INSERT INTO control.sessions"
                "(session_hash, csrf_hash, created_at, last_used_at, remote)"
                " VALUES(%s,%s,%s,%s,%s)",
                (token_hash(session), token_hash(csrf), current, current, remote),
            )
        self.audit("auth.login", actor="admin", remote=remote)
        return session, csrf

    def session(self, session: str | None, csrf: str | None = None, *, require_csrf: bool = False) -> bool:
        """Validate and refresh a browser session.

        Both bounds stay in Python rather than becoming SQL intervals so the
        two constants keep one definition, and the refusal is one statement:
        the UPDATE matches only a live row whose CSRF hash agrees, so a wrong
        token cannot refresh the session it failed to authorise.
        """
        if not session:
            return False
        with self._db() as connection:
            current = now()
            idle_floor = current - dt.timedelta(seconds=SESSION_IDLE_SECONDS)
            absolute_floor = current - dt.timedelta(seconds=SESSION_MAX_SECONDS)
            # Expired rows are removed whether or not this call authenticates,
            # matching the pruning the JSON store did as a side effect.
            connection.execute(
                "DELETE FROM control.sessions"
                " WHERE created_at <= %s OR last_used_at <= %s",
                (absolute_floor, idle_floor),
            )
            clauses = ["session_hash = %s", "created_at > %s", "last_used_at > %s"]
            values: list = [token_hash(session), absolute_floor, idle_floor]
            if require_csrf:
                if not csrf:
                    return False
                clauses.append("csrf_hash = %s")
                values.append(token_hash(csrf))
            row = connection.execute(
                "UPDATE control.sessions SET last_used_at = %s"
                " WHERE " + " AND ".join(clauses) + " RETURNING session_hash",
                [current, *values],
            ).fetchone()
            return row is not None

    def session_authenticated_at(self, session: str | None):
        """When this session's password was verified, or None.

        `created_at` and not `last_used_at`: the first is written once, when
        `login` verifies the password, and the second is refreshed on every
        request. Opening a standing approval needs the authentication time, so
        reading the activity time would let a session kept warm by ordinary
        browsing arm an auto-approving window hours after anybody proved who
        they were.

        This answers O8 -- "what timestamp establishes recent authentication"
        -- from the record that already exists, rather than by adding a
        re-authentication field or a step-up endpoint.
        """
        if not session:
            return None
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "SELECT created_at FROM control.sessions WHERE session_hash = %s",
                (token_hash(session),),
            ).fetchone()
        return row["created_at"] if row is not None else None

    def logout(self, session: str | None) -> None:
        if not session:
            return
        with self._db() as connection:
            connection.execute(
                "DELETE FROM control.sessions WHERE session_hash = %s",
                (token_hash(session),),
            )

    def change_password(self, current: str, replacement: str) -> bool:
        with self._db() as connection:
            encoded = self._require_initialized(connection)
            if not verify_password(current, encoded):
                return False
            require_password(replacement)
            connection.execute(
                "UPDATE control.admin_credential"
                "   SET encoded = %s, updated_at = now() WHERE id = 1",
                (password_hash(replacement),),
            )
            # Every session is invalidated: the old password may be known.
            connection.execute("DELETE FROM control.sessions")
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
        with self._db() as connection:
            self._require_initialized(connection)
            # Reservation is permanent and case-insensitive, and it is checked
            # here rather than by a constraint because it covers revoked rows
            # too: the schema's unique index only spans live tokens, since real
            # deployments already hold revoked duplicates from before the rule
            # existed. casefold, not SQL lower(): they disagree on 'SS'/'ß'.
            normalized_name = record["name"].casefold()
            taken = connection.execute(
                "SELECT 1 FROM control.tokens WHERE name_key = %s LIMIT 1",
                (normalized_name,),
            ).fetchone()
            if taken is not None:
                raise ValueError("Token names must be unique.")
            connection.execute(
                "INSERT INTO control.tokens"
                "(token_hash, token_id, name, name_key, created_at, expires_at,"
                " scopes)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    record["hash"],
                    record["id"],
                    record["name"],
                    normalized_name,
                    parse_time(record["created"]),
                    parse_time(record["expires"]),
                    record["scopes"],
                ),
            )
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
        normalized_scopes = list(dict.fromkeys(scopes))
        expires_at = current + dt.timedelta(seconds=DEVICE_AUTH_SECONDS)
        with self._db() as connection:
            self._require_initialized(connection)
            connection.execute("BEGIN")
            try:
                # Expired rows are dropped first, exactly as the JSON store
                # rebuilt its list from the live records, so an abandoned
                # request never permanently occupies a queue slot.
                connection.execute(
                    "DELETE FROM control.device_authorizations WHERE expires_at <= %s",
                    (current,),
                )
                per_client = connection.execute(
                    "SELECT count(*) AS n FROM control.device_authorizations"
                    " WHERE remote = %s AND status = 'pending' AND expires_at > %s",
                    (remote, current),
                ).fetchone()
                if int(per_client["n"]) >= 3:
                    raise ValueError("Too many pending device authorizations from this client.")
                total = connection.execute(
                    "SELECT count(*) AS n FROM control.device_authorizations"
                    " WHERE expires_at > %s",
                    (current,),
                ).fetchone()
                if int(total["n"]) >= 20:
                    raise ValueError("The device authorization queue is full.")
                connection.execute(
                    "INSERT INTO control.device_authorizations"
                    "(id_hash, user_code, device_name, scopes, created_at,"
                    " expires_at, remote, status)"
                    " VALUES(%s,%s,%s,%s,%s,%s,%s,'pending')",
                    (
                        token_hash(device_id),
                        user_code,
                        device_name.strip(),
                        normalized_scopes,
                        current,
                        expires_at,
                        remote,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        self.audit("device.started", actor="anonymous", remote=remote, details={"userCode": user_code})
        return {
            "deviceId": device_id,
            "userCode": user_code,
            "expiresIn": DEVICE_AUTH_SECONDS,
            "interval": 3,
            "scopes": normalized_scopes,
        }

    def list_device_authorizations(self) -> list[dict]:
        with self._db() as connection:
            self._require_initialized(connection)
            rows = connection.execute(
                "SELECT * FROM control.device_authorizations"
                " WHERE expires_at > %s ORDER BY created_at",
                (now(),),
            ).fetchall()
        return [
            {
                "userCode": row["user_code"],
                "deviceName": row["device_name"],
                "scopes": list(row["scopes"]),
                "created": iso(row["created_at"]),
                "expires": iso(row["expires_at"]),
                "remote": row["remote"],
                "status": row["status"],
            }
            for row in rows
        ]

    #: How long a person has to decide before an approval stops being one.
    #: Short, because the digest it carries binds a request whose revision and
    #: workspace can move underneath it, and a decision made about a workspace
    #: that has since changed is not a decision about this request.
    APPROVAL_LIFETIME = dt.timedelta(minutes=15)

    #: How long a decision stays spendable, measured from when it was made.
    #:
    #: Separate from APPROVAL_LIFETIME, and derived from `decided_at` rather
    #: than stored, so a person who takes fourteen minutes to decide does not
    #: leave the agent one minute to act. Without it a receipt is a standing
    #: authorisation: an agent could hold an approved one and spend it at a
    #: moment the person is not expecting, which is precisely the thing this
    #: mechanism exists to prevent. The agent polls for the decision, so it
    #: claims and spends within seconds of one being made; five minutes is
    #: slack, not a working window.
    RECEIPT_LIFETIME = dt.timedelta(minutes=5)

    # -- standing approval windows (P8) ---------------------------------

    #: How long a window may stand, and how much it may authorise while it
    #: does. Time alone is not a bound: at the specification's measured rates
    #: a sixty-minute window would auto-approve roughly three hundred
    #: mutations, so the count is what actually limits one and the clock is
    #: the outer edge.
    WINDOW_MAX_LIFETIME = dt.timedelta(minutes=60)
    WINDOW_MAX_CONSUMPTIONS = 20

    #: How recently the administrator opening a window must have proved who
    #: they are. Checked at creation and deliberately not at consumption --
    #: checking again would make the sixty-minute bound dead letter, since
    #: nothing would be approvable after the first fifteen minutes.
    #:
    #: The timestamp is `control.sessions.created_at`, which is written when
    #: the password verifies and never refreshed, so it is the authentication
    #: time rather than an activity time. That answers O8 without a new column
    #: or a step-up endpoint, and it has a visible consequence worth stating:
    #: an administrator whose session is older than this must sign in again to
    #: open a window. That is the right friction for arming auto-approval.
    WINDOW_RECENCY = dt.timedelta(minutes=15)

    def open_approval_window(
        self,
        *,
        grant_id: str,
        client_id: str,
        instance: str,
        action_class: str,
        created_by: str,
        session_created_at: dt.datetime,
        minutes: int,
        max_consumptions: int,
    ) -> dict:
        """Arm a window, or refuse and say which bound was exceeded.

        Every refusal names the bound rather than returning a bare failure: an
        operator who is told "no" while arming an auto-approving control should
        learn which limit they met, or they will simply try a different number.
        """
        if action_class not in WINDOWABLE_ACTION_CLASSES:
            raise ValueError(
                f"No standing approval may cover {action_class!r}."
            )
        if minutes <= 0 or dt.timedelta(minutes=minutes) > self.WINDOW_MAX_LIFETIME:
            raise ValueError(
                "A window lasts between one minute and"
                f" {int(self.WINDOW_MAX_LIFETIME.total_seconds() // 60)}."
            )
        if max_consumptions <= 0 or max_consumptions > self.WINDOW_MAX_CONSUMPTIONS:
            raise ValueError(
                "A window authorises between one and"
                f" {self.WINDOW_MAX_CONSUMPTIONS} actions."
            )
        current = now()
        if session_created_at < current - self.WINDOW_RECENCY:
            raise ValueError(
                "Sign in again before opening a standing approval: it borrows"
                " your authority, so the platform requires a recent"
                " authentication rather than a long-lived session."
            )
        window_id = secrets.token_hex(16)
        with self._db() as connection:
            self._require_initialized(connection)
            connection.execute(
                "INSERT INTO control.approval_windows"
                " (id, grant_id, client_id, instance, action_class,"
                "  created_by, creator_auth_time, created_at, expires_at,"
                "  max_consumptions)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (window_id, grant_id, client_id, instance, action_class,
                 created_by, session_created_at, current,
                 current + dt.timedelta(minutes=minutes), max_consumptions),
            )
        self.audit(
            "approval.window_opened",
            actor=created_by,
            details={
                "window": window_id, "grant": grant_id, "client": client_id,
                "actionClass": action_class, "minutes": minutes,
                "maxConsumptions": max_consumptions,
            },
        )
        return {
            "id": window_id,
            "expiresAt": iso(current + dt.timedelta(minutes=minutes)),
        }

    def consume_approval_window(
        self, *, grant_id: str, client_id: str, instance: str,
        action_class: str,
    ) -> dict | None:
        """Spend one of a window's consumptions, or return None.

        The conditional UPDATE is the read, for the reason every one-shot here
        works that way: two intents arriving together must not both see the
        last consumption free. The count is incremented in the predicate's own
        statement, so a window with one left decides exactly one of them.

        Expiry, revocation and exhaustion are all in the predicate rather than
        checked afterwards, so there is no moment between deciding a window is
        usable and using it.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "UPDATE control.approval_windows"
                "   SET consumed = consumed + 1"
                " WHERE id = ("
                "         SELECT id FROM control.approval_windows"
                "          WHERE grant_id = %s AND client_id = %s"
                "            AND instance = %s AND action_class = %s"
                "            AND revoked_at IS NULL AND expires_at > %s"
                "            AND consumed < max_consumptions"
                # Oldest first, so a window is used up and closed rather than
                # several being held part-spent.
                "          ORDER BY created_at"
                "          FOR UPDATE SKIP LOCKED"
                "          LIMIT 1)"
                " RETURNING id, created_by, creator_auth_time, expires_at,"
                "           consumed, max_consumptions",
                (grant_id, client_id, instance, action_class, now()),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_approval_windows(self) -> list[dict]:
        """What is standing open, newest first.

        Expired and exhausted windows are included rather than filtered: an
        operator asking what has been armed is owed the ones that have just
        closed too, because "it was open until a minute ago" is the answer to
        most questions about an approval nobody remembers giving.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            rows = connection.execute(
                "SELECT id, grant_id, client_id, instance, action_class,"
                "       created_by, creator_auth_time, created_at, expires_at,"
                "       max_consumptions, consumed, revoked_at, revoked_reason"
                "  FROM control.approval_windows"
                " WHERE created_at > %s"
                " ORDER BY created_at DESC",
                (now() - dt.timedelta(days=1),),
            ).fetchall()
        current = now()
        return [
            {
                **dict(row),
                "live": (
                    row["revoked_at"] is None
                    and row["expires_at"] > current
                    and row["consumed"] < row["max_consumptions"]
                ),
            }
            for row in rows
        ]

    def revoke_approval_window(self, window_id: str, reason: str) -> bool:
        """Close a window now. The conditional UPDATE is the read again, so
        two operators closing the same one cannot both believe they did."""
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "UPDATE control.approval_windows"
                "   SET revoked_at = %s, revoked_reason = %s"
                " WHERE id = %s AND revoked_at IS NULL"
                " RETURNING created_by, action_class",
                (now(), reason[:200], window_id),
            ).fetchone()
        if row is None:
            return False
        self.audit(
            "approval.window_revoked",
            actor="admin",
            details={"window": window_id, "actionClass": row["action_class"],
                     "reason": reason[:200]},
        )
        return True

    def revoke_approval_windows_for_grant(
        self, grant_id: str, reason: str
    ) -> int:
        """Revoking a grant closes every window opened under it.

        A window outlives nothing: it is authority to decide on behalf of a
        grant, so a grant that no longer exists cannot have one standing.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            result = connection.execute(
                "UPDATE control.approval_windows"
                "   SET revoked_at = %s, revoked_reason = %s"
                " WHERE grant_id = %s AND revoked_at IS NULL",
                (now(), reason[:200], grant_id),
            )
        return result.rowcount

    def create_approval(
        self,
        *,
        grant_id: str,
        client_id: str,
        instance: str,
        operation_id: str,
        tool: str,
        request_digest: str,
        risk: str,
        scopes: list[str],
        packet: dict,
    ) -> str:
        """Record what is being asked, and return the handle that names it.

        The handle is returned once and stored only as a hash, like every other
        credential here. It is not authority: it names a row whose status is
        `pending`, and nothing can be spent until a person decides.
        """
        handle = secrets.token_urlsafe(32)
        # Two names for one row, for two parties who must not share a secret.
        # The asker holds `handle` and can claim a receipt with it; the person
        # deciding sees `reference`, which is the handle's digest -- enough to
        # name the row, useless for spending it. The decider never holds
        # anything that could be carried back through the agent.
        reference = token_hash(handle)
        # A standing window substitutes the decider and nothing else. The row
        # below is the same row, carrying the same execution digest, decided
        # into the same single-use receipt -- what a window removes is the
        # human round trip. It is spent *before* the insert so that an intent
        # is never recorded as auto-decided on a consumption that was not
        # actually taken; a window found and not spent would be a race that
        # approved more than it authorised.
        window = None
        if risk in WINDOWABLE_ACTION_CLASSES:
            window = self.consume_approval_window(
                grant_id=grant_id, client_id=client_id, instance=instance,
                action_class=risk,
            )
        with self._db() as connection:
            self._require_initialized(connection)
            connection.execute(
                "INSERT INTO control.approvals"
                " (id_hash, grant_id, client_id, instance, operation_id, tool,"
                "  request_digest, risk, scopes, packet, created_at,"
                "  expires_at, status, decided_at, decided_by,"
                "  decided_by_window)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                "         %s, %s, %s, %s)",
                (
                    reference, grant_id, client_id, instance,
                    operation_id, tool, request_digest, risk, list(scopes),
                    json.dumps(packet), now(), now() + self.APPROVAL_LIFETIME,
                    "pending" if window is None else "approved",
                    None if window is None else now(),
                    # The approval inherits the window creator's identity, so
                    # the audit names a person rather than a mechanism.
                    None if window is None else window["created_by"],
                    None if window is None else window["id"],
                ),
            )
        self.audit(
            "approval.requested",
            actor=grant_id,
            details={
                "operation": operation_id,
                "tool": tool,
                "client": client_id,
                # Never the handle, and never the packet: one is a credential
                # and the other can carry workspace content.
                "digest": request_digest,
            },
        )
        if window is not None:
            # Audited individually against the window that authorised it, not
            # merely as a decision: the question a reader asks afterwards is
            # which standing approval answered for them and how much of it was
            # left, and a record naming only the operator cannot say.
            self.audit(
                "approval.window_consumed",
                actor=window["created_by"],
                details={
                    "window": window["id"],
                    "operation": operation_id,
                    "tool": tool,
                    "grant": grant_id,
                    "digest": request_digest,
                    "consumed": window["consumed"],
                    "maxConsumptions": window["max_consumptions"],
                    "windowExpires": iso(window["expires_at"]),
                    "creatorAuthenticatedAt": iso(window["creator_auth_time"]),
                },
            )
        return {
            "handle": handle,
            "reference": reference,
            # The caller needs to know whether to ask anybody. A tool whose
            # intent was decided on arrival returns its ordinary result rather
            # than reporting that approval is required.
            "decided": window is not None,
            "window": None if window is None else window["id"],
        }

    def read_approval(self, handle: str) -> dict | None:
        """What a person is being asked to decide, by the handle naming it.

        A plain read, unlike the redemption below, because showing somebody a
        request is not spending it.
        """
        return self.read_approval_by_reference(token_hash(handle))

    def read_approval_by_reference(self, reference: str) -> dict | None:
        """The same read, by the name the person deciding was given."""
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "SELECT id_hash AS reference, grant_id, client_id, instance,"
                "       operation_id, tool, request_digest, risk, scopes,"
                "       packet, status, created_at, expires_at, decided_at,"
                "       decided_by"
                "  FROM control.approvals"
                " WHERE id_hash = %s AND revoked_at IS NULL",
                (reference,),
            ).fetchone()
        if row is None:
            return None
        detail = dict(row)
        detail["expired"] = detail["expires_at"] <= now()
        return detail

    def confirm_approval(
        self, handle: str, *, approved: bool, decided_by: str
    ) -> bool:
        """The same decision, named by the handle its requester holds.

        The hashing stays here for the reason `read_approval` keeps it here:
        a caller that has to hash before calling is a caller that can hash
        differently, and the reference is what every predicate matches on.
        """
        return self.decide_approval(
            token_hash(handle), approved=approved, decided_by=decided_by
        )

    def decide_approval(
        self, reference: str, *, approved: bool, decided_by: str
    ) -> bool:
        """Record a person's decision, and mint the receipt if it was yes.

        One conditional statement, so two operators deciding at once cannot
        both believe they did it, and a decision cannot be changed afterwards:
        the predicate requires `pending`, which an approved or declined row is
        no longer.

        Records the decision and nothing else. It does not mint the receipt,
        because the person deciding does so in a browser and is not the party
        that will spend it -- a secret handed to them would have to travel back
        through the agent to be useful, which is the one route it must not
        take. `claim_receipt` mints it for the holder of the handle instead.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "UPDATE control.approvals"
                "   SET status = %s, decided_at = %s, decided_by = %s"
                " WHERE id_hash = %s AND status = 'pending'"
                "   AND expires_at > %s AND revoked_at IS NULL"
                " RETURNING operation_id, grant_id",
                (
                    "approved" if approved else "declined",
                    now(), decided_by, reference, now(),
                ),
            ).fetchone()
        if row is None:
            return False
        self.audit(
            "approval.approved" if approved else "approval.declined",
            actor=decided_by,
            details={"operation": row["operation_id"], "grant": row["grant_id"]},
        )
        return True

    def list_pending_approvals(self) -> list[dict]:
        """What is waiting for a person, newest first.

        Only what is still decidable: a decided or revoked row is history, and
        an expired one cannot be approved however it looks in a list. The
        packet travels with each row because the panel shows the request in
        full without a second call, and because a list that only names an
        operation asks somebody to approve a word.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            rows = connection.execute(
                "SELECT id_hash AS reference, grant_id, client_id,"
                "       operation_id, tool, risk, scopes, packet,"
                "       created_at, expires_at"
                "  FROM control.approvals"
                " WHERE status = 'pending' AND revoked_at IS NULL"
                "   AND expires_at > %s"
                " ORDER BY created_at DESC",
                (now(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_receipt(self, handle: str) -> str | None:
        """Mint the receipt for the holder of the handle, once.

        Gated on a decision having been made: the predicate requires
        `approved`, which only a person can produce. Gated on there being no
        receipt yet, so a second claim returns nothing rather than a second
        spendable secret -- the handle holder gets one, and if it is lost the
        approval has to be asked for again.
        """
        receipt = secrets.token_urlsafe(32)
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "UPDATE control.approvals"
                "   SET receipt_hash = %s"
                " WHERE id_hash = %s AND status = 'approved'"
                "   AND receipt_hash IS NULL AND revoked_at IS NULL"
                "   AND decided_at > %s",
                (token_hash(receipt), token_hash(handle),
                 now() - self.RECEIPT_LIFETIME),
            )
        return receipt if row.rowcount else None

    def redeem_receipt(self, receipt: str, *, request_digest: str) -> dict | None:
        """Spend a receipt against the one request it was issued for.

        The conditional UPDATE is the read. A receipt that was already spent
        does not match `status = 'approved'`, so a replay is a miss rather than
        a second effect, and the consumed row is kept so the replay is
        detectable rather than merely unsuccessful.

        The digest is part of the predicate, not checked afterwards. An
        approval carried to a different path, body or revision than the one a
        person saw does not match the row and is refused by the same statement
        that would otherwise spend it.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "UPDATE control.approvals"
                "   SET status = 'consumed', consumed_at = %s"
                " WHERE receipt_hash = %s AND status = 'approved'"
                "   AND request_digest = %s AND revoked_at IS NULL"
                "   AND decided_at > %s"
                " RETURNING operation_id, grant_id, client_id, decided_by,"
                "           request_digest",
                (now(), token_hash(receipt), request_digest,
                 now() - self.RECEIPT_LIFETIME),
            ).fetchone()
        if row is None:
            return None
        self.audit(
            "approval.consumed",
            actor=row["grant_id"],
            details={
                "operation": row["operation_id"],
                "approvedBy": row["decided_by"],
            },
        )
        return dict(row)

    def revoke_approvals_for_grant(self, grant_id: str, reason: str) -> int:
        """Revoking a grant reaches every approval made under it.

        Including one already approved and not yet spent, which is the case
        that matters: a receipt outlives the decision that produced it by as
        long as nobody spends it.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            result = connection.execute(
                "UPDATE control.approvals"
                "   SET revoked_at = %s, revoked_reason = %s"
                " WHERE grant_id = %s AND revoked_at IS NULL"
                "   AND status IN ('pending', 'approved')",
                (now(), reason[:200], grant_id),
            )
        return result.rowcount

    def approve_device_authorization(self, user_code: str) -> bool:
        """Approve a pending request, in one conditional statement.

        The predicate and the transition are the same statement, so two
        operators approving at once cannot both believe they did it.
        """
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "UPDATE control.device_authorizations"
                "   SET status = 'approved', approved_at = %s"
                " WHERE user_code = %s AND status = 'pending' AND expires_at > %s"
                " RETURNING device_name, scopes",
                (now(), user_code, now()),
            ).fetchone()
        if row is None:
            return False
        self.audit(
            "device.approved",
            actor="admin",
            details={
                "userCode": user_code,
                "deviceName": row["device_name"],
                "scopes": list(row["scopes"]),
            },
        )
        return True

    def poll_device_authorization(self, device_id: str) -> dict:
        """Report status, and mint the token exactly once when approved.

        The consume and the token insert share one transaction, and the consume
        is conditional on the row still being approved, so two simultaneous
        polls cannot both mint a token from one authorization. The file lock
        used to provide that; here the row does.
        """
        digest = token_hash(device_id)
        issued: tuple[str, dict, str] | None = None
        with self._db() as connection:
            self._require_initialized(connection)
            current = now()
            row = connection.execute(
                "SELECT * FROM control.device_authorizations WHERE id_hash = %s",
                (digest,),
            ).fetchone()
            if row is None:
                return {"status": "invalid"}
            if row["expires_at"] <= current:
                return {"status": "expired"}
            if row["status"] == "pending":
                return {"status": "pending"}
            if row["status"] == "consumed":
                return {"status": "consumed"}
            if row["status"] != "approved":
                return {"status": "invalid"}
            scopes = list(row["scopes"])
            if not scopes or any(scope not in DEVICE_SCOPES for scope in scopes):
                return {"status": "invalid"}

            connection.execute("BEGIN")
            try:
                raw = "mapp_" + secrets.token_urlsafe(32)
                token_id = secrets.token_hex(8)
                token_name = f"Device: {row['device_name']} [{token_id}]"
                expires_at = current + dt.timedelta(seconds=DEVICE_TOKEN_SECONDS)
                # Consume first, conditionally: if this returns nothing another
                # poll already took it, and no token is minted.
                consumed = connection.execute(
                    "UPDATE control.device_authorizations"
                    "   SET status = 'consumed', consumed_at = %s, token_id = %s"
                    " WHERE id_hash = %s AND status = 'approved' AND expires_at > %s"
                    " RETURNING remote",
                    (current, token_id, digest, current),
                ).fetchone()
                if consumed is None:
                    connection.execute("ROLLBACK")
                    return {"status": "consumed"}
                connection.execute(
                    "INSERT INTO control.tokens"
                    "(token_hash, token_id, name, name_key, created_at,"
                    " expires_at, scopes)"
                    " VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        token_hash(raw),
                        token_id,
                        token_name,
                        token_name.casefold(),
                        current,
                        expires_at,
                        scopes,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            record = {
                "id": token_id,
                "name": token_name,
                "created": iso(current),
                "expires": iso(expires_at),
                "lastUsed": None,
                "revoked": None,
                "scopes": scopes,
            }
            issued = (raw, record, str(consumed["remote"] or ""))
        raw, record, remote = issued
        self.audit(
            "device.token_issued",
            actor=f"token:{record['id']}",
            remote=remote,
            details={"id": record["id"], "scopes": record["scopes"]},
        )
        return {"status": "authorized", "token": raw, "record": record}

    def create_operation(self, kind: str, actor: str, target: dict | None = None) -> dict:
        operation = {
            "id": secrets.token_hex(16),
            "kind": kind,
            "status": "running",
            "actor": actor,
            "target": target or {},
            "created": iso(),
            "updated": iso(),
            "finished": None,
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
            removable = []
            for path in existing:
                try:
                    record = _strict_json(path.read_text())
                except (OSError, TypeError, ValueError):
                    continue
                status = record.get("status") if isinstance(record, dict) else None
                if status in {
                    "succeeded", "failed", "cancelled", "indeterminate",
                }:
                    removable.append(path)
            excess = max(0, len(existing) - 499)
            for stale in removable[:excess]:
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
            finished = iso()
            operation.update({
                "status": status,
                "updated": finished,
                "finished": finished,
                "result": result,
                "error": error,
            })
            _atomic_json(self.operations / f"{operation_id}.json", operation)
            return operation

    def update_operation_progress(
        self,
        operation_id: str,
        *,
        stage: str,
        diagnostics: dict | None = None,
    ) -> dict:
        """Persist a heartbeat for running work without changing its outcome."""
        if not isinstance(stage, str) or not stage:
            raise ValueError("Operation progress requires a stage.")
        with self._locked():
            operation = self.read_operation(operation_id)
            if operation.get("status") != "running":
                return operation
            operation.update({
                "stage": stage,
                "updated": iso(),
            })
            if diagnostics is not None:
                operation["diagnostics"] = diagnostics
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

    @staticmethod
    def _token_row(row) -> dict:
        """Project a row into the exact shape the JSON store returned.

        The key names and the falsy-when-live `revoked` are load-bearing well
        beyond this module: two output-suppressed revocation sweeps in the demo
        and federation harnesses filter on `record["name"]` and
        `not record.get("revoked")`, and would silently sweep nothing if either
        changed.
        """
        return {
            "id": row["token_id"],
            "name": row["name"],
            "created": iso(row["created_at"]),
            "expires": iso(row["expires_at"]) if row["expires_at"] else None,
            "lastUsed": iso(row["last_used_at"]) if row["last_used_at"] else None,
            "revoked": iso(row["revoked_at"]) if row["revoked_at"] else None,
            "scopes": list(row["scopes"]),
        }

    def list_tokens(self) -> list[dict]:
        with self._db() as connection:
            self._require_initialized(connection)
            rows = connection.execute(
                "SELECT * FROM control.tokens ORDER BY created_at, token_id"
            ).fetchall()
        return [self._token_row(row) for row in rows]

    def authenticate_token(self, raw: str | None, remote: str) -> dict | None:
        """Authenticate a bearer token and stamp its last use.

        One conditional UPDATE does both: a row that is revoked or expired
        cannot be matched, so it cannot have its last-use stamped either, and
        no separate read can go stale between the check and the write.
        """
        if not raw:
            return None
        audit_failure = False
        with self._db() as connection:
            current = now()
            row = connection.execute(
                "UPDATE control.tokens SET last_used_at = %s"
                " WHERE token_hash = %s"
                "   AND revoked_at IS NULL"
                "   AND (expires_at IS NULL OR expires_at > %s)"
                " RETURNING *",
                (current, token_hash(raw), current),
            ).fetchone()
            if row is not None:
                return self._token_row(row)
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
        with self._db() as connection:
            self._require_initialized(connection)
            row = connection.execute(
                "UPDATE control.tokens SET revoked_at = %s"
                " WHERE token_id = %s AND revoked_at IS NULL"
                " RETURNING token_id",
                (now(), token_id),
            ).fetchone()
            found = row is not None
        if found:
            self.audit("token.revoked", actor="admin", details={"id": token_id})
        return found

    def sessions(self) -> list[dict]:
        with self._db() as connection:
            self._require_initialized(connection)
            rows = connection.execute(
                "SELECT * FROM control.sessions ORDER BY created_at"
            ).fetchall()
        return [
            {
                "created": iso(row["created_at"]),
                "lastUsed": iso(row["last_used_at"]),
                "remote": row["remote"],
            }
            for row in rows
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

    def reset_password(self, password: str, *, revoke_tokens: bool = False) -> None:
        require_password(password)
        with self._db() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    "INSERT INTO control.admin_credential(id, encoded) VALUES(1, %s)"
                    " ON CONFLICT (id) DO UPDATE SET"
                    "   encoded = EXCLUDED.encoded, updated_at = now()",
                    (password_hash(password),),
                )
                # Being initialized means both a credential and an instance
                # identity, and this is a path that can create the credential
                # from nothing: `./bin/mapp init --demo` reaches reset-demo,
                # not init. Without this the platform came up with a working
                # password and no instance_id, so `instance_id()` raised, the
                # public identity route closed the connection and every
                # `./bin/mapp all` after a demo init failed verification with
                # an unexplained 502. DO NOTHING because an existing instance
                # keeps its identity across a password reset -- the operator
                # rotated a credential, they did not adopt a new instance.
                connection.execute(
                    "INSERT INTO control.metadata(key, value)"
                    " VALUES('instance_id', %s) ON CONFLICT (key) DO NOTHING",
                    (secrets.token_hex(16),),
                )
                connection.execute("DELETE FROM control.sessions")
                revoked_devices = 0
                if revoke_tokens:
                    revoked_at = now()
                    # Every token record is kept: revocation must not release
                    # its permanently reserved, case-insensitive name.
                    connection.execute(
                        "UPDATE control.tokens SET revoked_at = %s"
                        " WHERE revoked_at IS NULL",
                        (revoked_at,),
                    )
                    # An approved device grant can still mint a token, and every
                    # unexpired record occupies a queue slot, so both forms of
                    # outstanding authority end in the same transaction as the
                    # token revocation.
                    cursor = connection.execute(
                        "UPDATE control.device_authorizations"
                        "   SET status = 'revoked', expires_at = %s"
                        " WHERE status IN ('pending','approved')",
                        (revoked_at,),
                    )
                    revoked_devices = cursor.rowcount or 0
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        # Audited after the commit: an audit line for a rolled-back reset would
        # claim authority that was never revoked.
        self.audit("auth.password_reset", actor="local-admin")
        if revoke_tokens:
            if revoked_devices:
                self.audit(
                    "device.authorizations_revoked",
                    actor="local-admin",
                    details={"reason": "demo-init", "count": revoked_devices},
                )
            self.audit(
                "token.revoked_all",
                actor="local-admin",
                details={"reason": "demo-init"},
            )

    def revoke_all(self) -> None:
        with self._db() as connection:
            self._require_initialized(connection)
            connection.execute(
                "UPDATE control.tokens SET revoked_at = %s WHERE revoked_at IS NULL",
                (now(),),
            )
        self.audit("token.revoked_all", actor="local-admin")
