"""In-memory store for M1 only.

M1 exists to prove the authlib adapter against real HTTP, so the store is the
one thing deliberately faked. M4 replaces this with the Postgres ``control``
schema behind the same four methods; nothing above this line should need to
change when it does.

The interface is narrow on purpose — if it grows here, that is a signal the
adapter is leaking storage concerns.
"""

from __future__ import annotations

import hashlib
import secrets
import threading

from models import AuthorizationCode
from models import Client
from models import PendingLimitReached
from models import PendingAuthorization
from models import Session
from models import Token


def token_digest(raw: str) -> str:
    """The single definition of how a bearer secret is keyed at rest."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _equal(expected: str, supplied: str) -> bool:
    """Constant-time compare that cannot raise on non-ASCII input."""
    return secrets.compare_digest(expected.encode("utf-8"), (supplied or "").encode("utf-8"))


class StubStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, Client] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._tokens: dict[str, Token] = {}
        #: Operation binding for exchanged tokens, keyed the same way.
        self._exchanged: dict[str, dict] = {}
        self._pending: dict[str, PendingAuthorization] = {}
        self._sessions: dict[str, Session] = {}

    # -- clients ---------------------------------------------------------

    def add_client(self, client: Client) -> Client:
        with self._lock:
            self._clients[client.client_id] = client
        return client

    def query_client(self, client_id: str) -> Client | None:
        with self._lock:
            return self._clients.get(client_id)

    # -- authorization codes ---------------------------------------------

    def save_authorization_code(self, code: AuthorizationCode) -> AuthorizationCode:
        with self._lock:
            self._codes[code.code] = code
        return code

    def query_authorization_code(self, code: str, client_id: str) -> AuthorizationCode | None:
        with self._lock:
            record = self._codes.get(code)
        if record is None or record.client_id != client_id or record.is_expired():
            return None
        return record

    def consume_authorization_code_for(
        self, code: str, client_id: str
    ) -> AuthorizationCode | None:
        """Atomically take the code, or return None because someone else did.

        Every predicate is evaluated under the same lock as the removal, so
        this is one step rather than a read followed by a write. That matters:
        it is the only thing making a code single-use when two exchanges race.
        The Postgres form in M4 is the same shape --
        ``DELETE ... WHERE code = %s AND client_id = %s AND expires_at > now()
        RETURNING ...`` -- so the client and expiry predicates live here too
        rather than in a separate query, which would reopen the window.
        """
        with self._lock:
            record = self._codes.get(code)
            if record is None or record.client_id != client_id or record.is_expired():
                # An expired or mismatched code is left in place; the sweeper
                # removes expired rows, and removing it here would let a wrong
                # client_id delete another client's live code.
                return None
            del self._codes[code]
            return record

    # -- tokens ----------------------------------------------------------

    def save_token(self, raw_token: str, token: Token) -> Token:
        """Store under the hash, and refuse a record whose own hash disagrees.

        The hash was computed twice -- once in issuer._generate_token onto
        Token.token_hash, once here for the key -- and nothing read the record
        field, so the two could silently diverge once M4 moves the key into a
        Postgres primary key. One expression now produces both, and the check
        makes a divergence fail loudly rather than at rest.
        """
        digest = token_digest(raw_token)
        if token.token_hash and not _equal(token.token_hash, digest):
            raise ValueError("Token record hash does not match the raw token.")
        token.token_hash = digest
        with self._lock:
            self._tokens[digest] = token
        return token

    def query_token(self, raw_token: str) -> Token | None:
        with self._lock:
            return self._tokens.get(token_digest(raw_token))

    def save_exchanged_token(
        self,
        raw_token: str,
        *,
        client_id: str,
        actor_client_id: str,
        subject: str,
        scope: str,
        audience: str,
        issued_at,
        expires_at,
        operation_id: str,
        request_digest: str,
        single_use: bool,
    ) -> None:
        """In-memory twin of the SQL form, holding the same binding."""
        digest = token_digest(raw_token)
        record = Token(
            token_hash=digest,
            client_id=client_id,
            scope=scope,
            subject=subject,
            issued_at=int(issued_at.timestamp()),
            expires_in=int((expires_at - issued_at).total_seconds()),
            audience=audience,
        )
        with self._lock:
            self._tokens[digest] = record
            self._exchanged[digest] = {
                "operation_id": operation_id,
                "request_digest": request_digest,
                "single_use": single_use,
                "actor_client_id": actor_client_id,
                "broker_client_id": client_id,
                "consumed": False,
            }

    def consume_exchanged_token(
        self, raw_token: str, operation_id: str, request_digest: str
    ):
        """Spend a single-use token B, atomically, or report it unspendable.

        The digest is a predicate here too, so the two stores refuse the same
        presentations. They also have to agree on what a *spent* token looks
        like afterwards: this one used to mark only its side table, so the
        same token reported is_revoked() False here and True from SQL.
        """
        digest = token_digest(raw_token)
        with self._lock:
            binding = self._exchanged.get(digest)
            record = self._tokens.get(digest)
            if (
                binding is None
                or record is None
                or binding["consumed"]
                or not binding["single_use"]
                or binding["operation_id"] != operation_id
                or binding["request_digest"] != request_digest
                or record.is_expired()
                or record.is_revoked()
            ):
                return None
            binding["consumed"] = True
            # Mirrors the SQL store, which folds consumed_at into `revoked`.
            record.revoked = True
            return {
                "scope": record.scope,
                "subject": record.subject,
                "request_digest": binding["request_digest"],
            }

    def exchanged_binding(self, raw_token: str):
        """Read the binding without spending it, for assertions."""
        with self._lock:
            return self._exchanged.get(token_digest(raw_token))

    def token_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    # -- pending authorizations ------------------------------------------

    #: Backstop across all callers.
    MAX_PENDING = 10_000
    #: The control that actually matters. A global cap alone is worse than no
    #: cap for availability: parking records is unauthenticated and costs the
    #: attacker nothing, so one source could fill the shared table in seconds
    #: and lock every legitimate user out of /oauth/authorize for the record
    #: TTL. Bounding per source means a flood denies only the flooder.
    MAX_PENDING_PER_SOURCE = 64

    def save_pending(self, pending: PendingAuthorization) -> PendingAuthorization:
        # Sweep before admitting: an unauthenticated caller creates these, so
        # reclaiming on the same path that grows the table keeps it bounded
        # without a timer.
        self.sweep_expired()
        with self._lock:
            if len(self._pending) >= self.MAX_PENDING:
                raise PendingLimitReached(len(self._pending))
            if pending.source:
                held = sum(
                    1 for record in self._pending.values() if record.source == pending.source
                )
                if held >= self.MAX_PENDING_PER_SOURCE:
                    raise PendingLimitReached(held)
            self._pending[pending.request_id] = pending
        return pending

    def query_pending(self, request_id: str) -> PendingAuthorization | None:
        with self._lock:
            record = self._pending.get(request_id)
        if record is None or record.is_expired():
            return None
        return record

    def bind_pending_session(
        self, request_id: str, session_hash: str
    ) -> PendingAuthorization | None:
        """Record which session was shown this record's consent page.

        Rebinding is allowed: the operator may reload the consent page, and a
        second sign-in on the same parked request should replace the binding
        rather than fail. What must not happen is a *submission* against a
        record this session was never shown, which the conditional consume
        checks.

        The form token is rotated here, and that is the point. On its own the
        binding is not an independent defence for a rid the attacker chose:
        the csrf of an attacker-minted record is readable from the
        unauthenticated login page, and a single lured top-level GET would
        establish the binding to the operator's session, leaving the record
        submittable with the token the attacker already holds. Rotating on
        bind makes that copy dead. What remains is the ordinary case of a
        stale consent tab failing with 403 on submit -- the record survives, so
        a reload recovers -- which is already what a second browser causes.
        """
        with self._lock:
            record = self._pending.get(request_id)
            if record is None or record.is_expired():
                return None
            record.session_hash = session_hash
            record.csrf = secrets.token_urlsafe(24)
            return record

    def consume_pending(
        self, request_id: str, csrf: str, session_hash: str
    ) -> PendingAuthorization | None:
        """Take the record only if every authorising predicate holds.

        The form token and session binding are checked *inside* the same locked
        step as the removal, rather than by the caller beforehand, so the
        destructive act cannot be ordered before the authorising one. That
        ordering was a real bug: consuming first meant a POST with a valid rid
        and a wrong csrf destroyed the operator's parked request, and their
        genuine submission then failed with "unknown or expired".

        The SQL that replaces this in M4 must carry every predicate, including
        the empty-binding guard, or it will be weaker than the code it
        replaces: ``DELETE ... WHERE request_id = %s AND csrf = %s AND
        session_hash = %s AND session_hash <> '' AND expires_at > now()
        RETURNING``. The ``<> ''`` is not decoration -- without it a record
        that was never shown to anyone matches a submission carrying an empty
        session hash.
        """
        with self._lock:
            record = self._pending.get(request_id)
            if record is None or record.is_expired():
                # Drop an expired record: it can no longer authorise anything.
                self._pending.pop(request_id, None)
                return None
            if not record.session_hash or not _equal(record.session_hash, session_hash):
                return None
            if not _equal(record.csrf, csrf):
                return None
            del self._pending[request_id]
            return record

    def sweep_expired(self) -> int:
        """Drop expired records. Returns how many went.

        GET /oauth/authorize parks a record for any unauthenticated caller with
        valid client parameters, and nothing else ever removes one, so without
        this the table grows without bound -- 500 unauthenticated requests left
        500 live records. The Postgres form is a periodic
        ``DELETE WHERE expires_at < now()``.
        """
        removed = 0
        with self._lock:
            for key in [k for k, v in self._pending.items() if v.is_expired()]:
                del self._pending[key]
                removed += 1
            for key in [k for k, v in self._codes.items() if v.is_expired()]:
                del self._codes[key]
                removed += 1
            for key in [k for k, v in self._sessions.items() if v.is_expired()]:
                del self._sessions[key]
                removed += 1
        return removed

    # -- browser sessions ------------------------------------------------

    def save_session(self, raw_session: str, session: Session) -> Session:
        with self._lock:
            self._sessions[token_digest(raw_session)] = session
        return session

    def query_session(self, raw_session: str | None) -> Session | None:
        if not raw_session:
            return None
        with self._lock:
            record = self._sessions.get(token_digest(raw_session))
        if record is None or record.is_expired():
            return None
        return record
