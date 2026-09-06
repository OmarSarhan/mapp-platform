"""The control-schema store: the same contract as StubStore, over PostgreSQL.

StubStore is the in-memory stand-in this replaces. The interfaces are identical
on purpose -- the same suite runs against both -- but two behaviours differ,
and both differences are the point of moving to SQL:

*   **A one-shot record is consumed by the statement that reads it.** The stub
    removes a record under a lock; here the conditional
    ``UPDATE ... WHERE consumed_at IS NULL ... RETURNING`` is the read, so the
    database decides the race rather than a process-local lock. Two processes
    now behave the way two threads did.
*   **A consumed record survives.** The stub deletes; this marks. A replay
    therefore arrives at a row that says when it was spent and by whom, which
    is what lets a replay be distinguished from an unrecognised credential.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from models import AuthorizationCode
from models import Client
from models import PendingAuthorization
from models import Session
from models import Token

SCHEMA = "control"
CONNECT_TIMEOUT_SECONDS = 10

#: Live parked requests permitted per source. Matches StubStore: a global cap
#: alone lets one unauthenticated caller lock everyone out of /oauth/authorize.
MAX_PENDING_PER_SOURCE = 64
MAX_PENDING = 10_000


class PendingLimitReached(RuntimeError):
    """Too many live parked authorization requests."""


def token_digest(raw: str) -> str:
    """The single definition of how a bearer secret is keyed at rest."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _utc(value: dt.datetime | float | None) -> dt.datetime | None:
    """Accept the models' float epochs and hand PostgreSQL an aware datetime."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromtimestamp(value, dt.timezone.utc)


def _epoch(value: dt.datetime | None) -> float | None:
    return None if value is None else value.timestamp()


class SqlStore:
    """Backed by the `control` schema. One short-lived connection per call.

    Pooling is deliberately absent: the authorization endpoints are low rate
    and each call is a single statement, so a pool would add a failure mode
    (a connection left mid-transaction) for no measurable gain. The role's
    CONNECTION LIMIT is the backstop.
    """

    def __init__(self, dsn: str, *, audience: str = "mcp") -> None:
        self._dsn = dsn
        #: Recorded on every issued token so introspection can refuse a token
        #: minted for a different resource (P5's audience separation).
        self._audience = audience

    def _connect(self) -> psycopg.Connection:
        connection = psycopg.connect(
            self._dsn,
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )
        try:
            connection.execute(
                sql.SQL("SET SESSION search_path = pg_catalog, {s}").format(
                    s=sql.Identifier(SCHEMA)
                )
            )
        except BaseException:
            connection.close()
            raise
        return connection

    # -- clients ---------------------------------------------------------

    def add_client(self, client: Client) -> Client:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO control.oauth_clients"
                "(client_id, name, redirect_uris, scopes, grant_types,"
                " token_endpoint_auth_method, client_secret_hash)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (client_id) DO UPDATE SET"
                "   name = EXCLUDED.name,"
                "   redirect_uris = EXCLUDED.redirect_uris,"
                "   scopes = EXCLUDED.scopes,"
                "   grant_types = EXCLUDED.grant_types,"
                "   token_endpoint_auth_method = EXCLUDED.token_endpoint_auth_method,"
                "   client_secret_hash = EXCLUDED.client_secret_hash",
                (
                    client.client_id,
                    client.name,
                    list(client.redirect_uris),
                    list(client.scopes),
                    list(client.grant_types),
                    client.token_endpoint_auth_method,
                    # Stored hashed: a client secret at rest is a credential,
                    # and the comparison happens on the digest either way.
                    token_digest(client.client_secret) if client.client_secret else None,
                ),
            )
        return client

    def query_client(self, client_id: str) -> Client | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM control.oauth_clients"
                " WHERE client_id = %s AND disabled_at IS NULL",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        return Client(
            client_id=row["client_id"],
            name=row["name"],
            redirect_uris=tuple(row["redirect_uris"]),
            scopes=tuple(row["scopes"]),
            token_endpoint_auth_method=row["token_endpoint_auth_method"],
            grant_types=tuple(row["grant_types"]),
            # The stored value is already a digest, and Client.check_client_secret
            # compares digests, so the raw secret never has to exist here.
            client_secret=row["client_secret_hash"] or "",
            secret_is_hashed=True,
        )

    # -- authorization codes ---------------------------------------------

    def save_authorization_code(self, code: AuthorizationCode) -> AuthorizationCode:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO control.oauth_authorization_codes"
                "(code_hash, client_id, redirect_uri, scope, subject,"
                " code_challenge, code_challenge_method, expires_at)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    token_digest(code.code),
                    code.client_id,
                    code.redirect_uri,
                    code.scope,
                    code.subject,
                    code.code_challenge,
                    code.code_challenge_method or "S256",
                    _utc(code.expires_at),
                ),
            )
        return code

    def query_authorization_code(
        self, code: str, client_id: str
    ) -> AuthorizationCode | None:
        """Non-consuming read, for tests and diagnostics only.

        The grant never uses this: consuming and reading must be one statement,
        which is consume_authorization_code_for.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM control.oauth_authorization_codes"
                " WHERE code_hash = %s AND client_id = %s"
                "   AND consumed_at IS NULL AND expires_at > now()",
                (token_digest(code), client_id),
            ).fetchone()
        return self._code_from(row, code)

    def consume_authorization_code_for(
        self, code: str, client_id: str
    ) -> AuthorizationCode | None:
        """Take the code, or return None because someone else did.

        Every predicate sits inside the one statement that removes it from
        play, so concurrent exchanges of one code produce exactly one token.
        The row is marked rather than deleted: a replay then meets a record
        that says when it was spent, which an audit can act on.
        """
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE control.oauth_authorization_codes"
                "   SET consumed_at = now(), consumed_by = %s"
                " WHERE code_hash = %s AND client_id = %s"
                "   AND consumed_at IS NULL AND expires_at > now()"
                " RETURNING *",
                (client_id, token_digest(code), client_id),
            ).fetchone()
        return self._code_from(row, code)

    @staticmethod
    def _code_from(row, code: str) -> AuthorizationCode | None:
        if row is None:
            return None
        return AuthorizationCode(
            code=code,
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            scope=row["scope"],
            subject=row["subject"],
            code_challenge=row["code_challenge"],
            code_challenge_method=row["code_challenge_method"],
            expires_at=_epoch(row["expires_at"]),
        )

    # -- tokens ----------------------------------------------------------

    def save_token(self, raw_token: str, token: Token) -> Token:
        digest = token_digest(raw_token)
        if token.token_hash and token.token_hash != digest:
            raise ValueError("Token record hash does not match the raw token.")
        token.token_hash = digest
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO control.oauth_tokens"
                "(token_hash, client_id, subject, scope, audience, issued_at,"
                " expires_at)"
                " VALUES(%s,%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
                (
                    digest,
                    token.client_id,
                    token.subject,
                    token.scope,
                    self._audience,
                    token.issued_at,
                    token.issued_at + token.expires_in,
                ),
            )
        return token

    def query_token(self, raw_token: str) -> Token | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM control.oauth_tokens WHERE token_hash = %s",
                (token_digest(raw_token),),
            ).fetchone()
        if row is None:
            return None
        issued = row["issued_at"]
        return Token(
            token_hash=row["token_hash"],
            client_id=row["client_id"],
            scope=row["scope"],
            subject=row["subject"],
            issued_at=int(issued.timestamp()),
            expires_in=int((row["expires_at"] - issued).total_seconds()),
            revoked=row["revoked_at"] is not None or row["consumed_at"] is not None,
            audience=row["audience"],
        )

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
        """Persist a token B, bound to the one operation it authorises.

        The binding is not advisory: the configuration API re-checks the
        operation and the request digest on every call, so a token minted for
        one proposal cannot be spent on another even within its sixty seconds.
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO control.oauth_tokens"
                "(token_hash, client_id, subject, scope, audience, issued_at,"
                " expires_at, single_use, operation_id, request_digest,"
                " actor_client_id, broker_client_id)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    token_digest(raw_token),
                    client_id,
                    subject,
                    scope,
                    audience,
                    issued_at,
                    expires_at,
                    single_use,
                    operation_id,
                    request_digest,
                    actor_client_id,
                    client_id,
                ),
            )

    def consume_exchanged_token(self, raw_token: str, operation_id: str):
        """Spend a single-use token B, or report that it is already spent.

        One conditional statement, so two calls presenting the same token
        cannot both proceed -- which is the entire value of single use.
        """
        with self._connect() as connection:
            return connection.execute(
                "UPDATE control.oauth_tokens SET consumed_at = now()"
                " WHERE token_hash = %s"
                "   AND operation_id = %s"
                "   AND single_use"
                "   AND consumed_at IS NULL"
                "   AND revoked_at IS NULL"
                "   AND expires_at > now()"
                " RETURNING scope, subject, request_digest",
                (token_digest(raw_token), operation_id),
            ).fetchone()

    def token_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT count(*) AS n FROM control.oauth_tokens"
            ).fetchone()
        return int(row["n"])

    # -- pending authorizations ------------------------------------------

    def save_pending(self, pending: PendingAuthorization) -> PendingAuthorization:
        self.sweep_expired()
        with self._connect() as connection:
            if pending.source:
                row = connection.execute(
                    "SELECT count(*) AS n FROM control.oauth_pending_authorizations"
                    " WHERE source = %s AND consumed_at IS NULL AND expires_at > now()",
                    (pending.source,),
                ).fetchone()
                if int(row["n"]) >= MAX_PENDING_PER_SOURCE:
                    raise PendingLimitReached(int(row["n"]))
            row = connection.execute(
                "SELECT count(*) AS n FROM control.oauth_pending_authorizations"
                " WHERE consumed_at IS NULL AND expires_at > now()"
            ).fetchone()
            if int(row["n"]) >= MAX_PENDING:
                raise PendingLimitReached(int(row["n"]))
            connection.execute(
                "INSERT INTO control.oauth_pending_authorizations"
                "(request_id, query, client_id, redirect_uri, scopes, csrf,"
                " session_hash, source, expires_at)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    pending.request_id,
                    pending.query,
                    pending.client_id,
                    pending.redirect_uri,
                    list(pending.scopes),
                    pending.csrf,
                    pending.session_hash,
                    pending.source,
                    _utc(pending.expires_at),
                ),
            )
        return pending

    def query_pending(self, request_id: str) -> PendingAuthorization | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM control.oauth_pending_authorizations"
                " WHERE request_id = %s AND consumed_at IS NULL AND expires_at > now()",
                (request_id,),
            ).fetchone()
        return self._pending_from(row)

    def bind_pending_session(
        self, request_id: str, session_hash: str
    ) -> PendingAuthorization | None:
        """Bind the record to this session, rotating its form token.

        Rotation is the security-relevant half: the token on the
        unauthenticated login page is readable by whoever parked the record, so
        without rotation the binding alone would not stop them submitting it
        once a lured navigation had bound it to an operator.
        """
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE control.oauth_pending_authorizations"
                "   SET session_hash = %s, csrf = %s"
                " WHERE request_id = %s AND consumed_at IS NULL AND expires_at > now()"
                " RETURNING *",
                (session_hash, secrets.token_urlsafe(24), request_id),
            ).fetchone()
        return self._pending_from(row)

    def consume_pending(
        self, request_id: str, csrf: str, session_hash: str
    ) -> PendingAuthorization | None:
        """Take the record only if every authorising predicate holds.

        The form token and session binding are evaluated by the same statement
        that consumes, so the destructive act cannot be ordered before the
        authorising one -- a rejected submission leaves the operator's parked
        request intact.

        The empty binding is refused twice, in Python and again in SQL, and
        that redundancy is deliberate rather than accidental: an unbound record
        stores ``''``, so without either guard a submission carrying no session
        cookie would compare ``'' = ''`` and consume a record nobody had been
        shown. Removing one guard changes nothing; removing both is a live
        vulnerability, which is what
        test_an_empty_session_never_matches pins.
        """
        if not session_hash:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE control.oauth_pending_authorizations"
                "   SET consumed_at = now()"
                " WHERE request_id = %s"
                "   AND csrf = %s"
                "   AND session_hash = %s"
                "   AND session_hash <> ''"
                "   AND consumed_at IS NULL"
                "   AND expires_at > now()"
                " RETURNING *",
                (request_id, csrf, session_hash),
            ).fetchone()
        return self._pending_from(row)

    @staticmethod
    def _pending_from(row) -> PendingAuthorization | None:
        if row is None:
            return None
        return PendingAuthorization(
            request_id=row["request_id"],
            query=row["query"],
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            scopes=tuple(row["scopes"]),
            csrf=row["csrf"],
            session_hash=row["session_hash"],
            source=row["source"],
            expires_at=_epoch(row["expires_at"]),
        )

    def sweep_expired(self) -> int:
        """Drop expired records. Returns how many went.

        Parking a record is unauthenticated, so the table grows without this.
        Consumed rows are kept -- they are the replay evidence -- and only
        expiry removes them.
        """
        removed = 0
        with self._connect() as connection:
            for table in (
                "oauth_pending_authorizations",
                "oauth_authorization_codes",
                "oauth_sessions",
            ):
                cursor = connection.execute(
                    sql.SQL("DELETE FROM {s}.{t} WHERE expires_at < now()").format(
                        s=sql.Identifier(SCHEMA), t=sql.Identifier(table)
                    )
                )
                removed += cursor.rowcount or 0
        return removed

    # -- browser sessions ------------------------------------------------

    def save_session(self, raw_session: str, session: Session) -> Session:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO control.oauth_sessions"
                "(session_hash, subject, auth_time, expires_at)"
                " VALUES(%s,%s,to_timestamp(%s),to_timestamp(%s))"
                " ON CONFLICT (session_hash) DO UPDATE SET"
                "   expires_at = EXCLUDED.expires_at",
                (
                    token_digest(raw_session),
                    session.subject,
                    session.auth_time,
                    session.expires_at,
                ),
            )
        return session

    def query_session(self, raw_session: str | None) -> Session | None:
        if not raw_session:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM control.oauth_sessions"
                " WHERE session_hash = %s AND expires_at > now()",
                (token_digest(raw_session),),
            ).fetchone()
        if row is None:
            return None
        return Session(
            subject=row["subject"],
            auth_time=row["auth_time"].timestamp(),
            expires_at=row["expires_at"].timestamp(),
        )
