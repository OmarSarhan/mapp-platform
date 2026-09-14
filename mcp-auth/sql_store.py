"""The control-schema store: the same contract as StubStore, over PostgreSQL.

StubStore (tests/stub_store.py) is the in-memory stand-in this replaces. The
interfaces are identical on purpose, and tests/store_contract.py asserts the
shared shape against both so a divergence fails rather than hides. Two
behaviours still differ, and both differences are the point of moving to SQL:

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
from psycopg.types.json import Jsonb

from models import AuthorizationCode
from models import Client
from models import Grant
from models import PendingLimitReached
from models import PendingAuthorization
from models import Session
from models import Token

SCHEMA = "control"
CONNECT_TIMEOUT_SECONDS = 10

#: Live parked requests permitted per source. Matches StubStore: a global cap
#: alone lets one unauthenticated caller lock everyone out of /oauth/authorize.
MAX_PENDING_PER_SOURCE = 64
MAX_PENDING = 10_000


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


#: Detail keys the audit writer refuses outright. Section 11 forbids logging
#: authorization headers, raw tokens, codes, refresh identifiers, broker
#: assertions, cookies, CSRF values and approval state, and a redaction rule
#: that lives only in a review comment is a rule that will one day be missed.
#: Refusing is deliberate rather than scrubbing: a silently emptied field
#: leaves a caller believing it recorded something.
FORBIDDEN_AUDIT_KEYS = frozenset(
    {
        "access_token",
        "assertion",
        "authorization",
        "client_secret",
        "code",
        "code_verifier",
        "cookie",
        "csrf",
        "password",
        "refresh_token",
        "secret",
        "subject_token",
        "token",
    }
)


class AuditRefused(ValueError):
    """A detail field would have put a credential in the audit trail."""


def _check_audit_detail(detail: dict) -> None:
    for key in detail:
        if str(key).lower().replace("-", "_") in FORBIDDEN_AUDIT_KEYS:
            raise AuditRefused(
                f"{key!r} may not appear in an audit record; record an"
                " identifier the credential resolves to instead."
            )



class SqlStore:
    """Backed by the `control` schema. One short-lived connection per call.

    Pooling is deliberately absent: the authorization endpoints are low rate
    and each call is a single statement, so a pool would add a failure mode
    (a connection left mid-transaction) for no measurable gain. The role's
    CONNECTION LIMIT is the backstop.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

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
                " token_endpoint_auth_method, client_secret_hash, disabled_at)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,"
                "        CASE WHEN %s THEN now() ELSE NULL END)"
                " ON CONFLICT (client_id) DO UPDATE SET"
                "   name = EXCLUDED.name,"
                "   redirect_uris = EXCLUDED.redirect_uris,"
                "   scopes = EXCLUDED.scopes,"
                "   grant_types = EXCLUDED.grant_types,"
                "   token_endpoint_auth_method = EXCLUDED.token_endpoint_auth_method,"
                "   client_secret_hash = EXCLUDED.client_secret_hash,"
                "   disabled_at = EXCLUDED.disabled_at",
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
                    # Without this the SQL store could not express a disabled
                    # client at all, so the rule was only ever exercised
                    # against the in-memory double and the two stores could
                    # disagree about who may act.
                    client.disabled,
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

    def ping(self) -> None:
        """Prove the control schema is reachable, or raise.

        The health check used to answer from the process alone, so the
        container reported healthy while every OAuth request failed on the
        database it could not reach -- and `compose up --wait` treated that as
        a successful start.
        """
        with self._connect() as connection:
            connection.execute("SELECT 1")

    # -- rotating refresh families ---------------------------------------

    #: Section 4's approved bounds. Idle moves on every rotation; absolute
    #: never does, which is what stops an indefinitely-refreshed session
    #: outliving the consent it came from.
    REFRESH_IDLE_SECONDS = 12 * 60 * 60
    REFRESH_ABSOLUTE_SECONDS = 30 * 24 * 60 * 60
    #: How long after a rotation a re-presentation is a retry rather than a
    #: replay. Without it, three ordinary events -- a lost response, two
    #: concurrent refreshes, a restart between the commit and the reply --
    #: are indistinguishable from theft, and each costs the operator a
    #: browser sign-in. Thirty seconds is Okta's default and the middle of
    #: the 0-60 range Cognito allows. The cost is stated exactly: a thief
    #: holding a stolen token has this long to use it alongside the
    #: legitimate client before either of them trips detection.
    REFRESH_GRACE_SECONDS = 30

    def start_refresh_family(
        self,
        raw_token: str,
        *,
        family_id: str,
        grant_id: str,
        client_id: str,
        scope: str,
        idle_seconds: int | None = None,
        absolute_seconds: int | None = None,
    ) -> None:
        """Open a family and issue its first token, in one transaction.

        The two are never separately visible: a family with no token is a row
        nothing can use, and a token with no family cannot be authorised.
        """
        idle = idle_seconds or self.REFRESH_IDLE_SECONDS
        absolute = absolute_seconds or self.REFRESH_ABSOLUTE_SECONDS
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                connection.execute(
                    "INSERT INTO control.oauth_refresh_families"
                    "(family_id, grant_id, client_id, scope, absolute_expires_at)"
                    " VALUES(%s,%s,%s,%s, now() + make_interval(secs => %s))",
                    (family_id, grant_id, client_id, scope, absolute),
                )
                connection.execute(
                    "INSERT INTO control.oauth_refresh_tokens"
                    "(token_hash, family_id, idle_expires_at)"
                    " VALUES(%s,%s, now() + make_interval(secs => %s))",
                    (token_digest(raw_token), family_id, idle),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def rotate_refresh_token(
        self,
        old_raw: str,
        new_raw: str,
        *,
        idle_seconds: int | None = None,
        grace_seconds: float | None = None,
    ) -> dict:
        """Spend one refresh token and issue its successor, or explain why not.

        One transaction, so a client is never left holding a spent token and no
        replacement. The conditional UPDATE is the authority: two simultaneous
        presentations of the same token cannot both rotate, which is what makes
        "rotated every use" mean anything.

        **A replay revokes the family and its grant here, not in the caller.**
        Section 4 requires that, and detection separated from the act leaves a
        window in which a replay is known and nothing has happened -- which is
        the window an attacker with a stolen token wants. The cost is real and
        is the specification's own open item O7: a client that retries after a
        network timeout is indistinguishable from an attacker replaying, and
        loses the operator's consent.

        Returns an outcome rather than raising, because the caller has to
        distinguish "refuse" from "refuse and tell the operator something is
        wrong", and an exception type per reason would be worse.
        """
        idle = idle_seconds or self.REFRESH_IDLE_SECONDS
        # `is None`, not `or`: zero is a meaningful value here -- it turns the
        # grace window off, which is what a test forcing a replay asks for and
        # what a deployment wanting the strict OAuth 2.1 behaviour sets. `or`
        # would silently substitute thirty seconds for it.
        grace = self.REFRESH_GRACE_SECONDS if grace_seconds is None else grace_seconds
        old_digest = token_digest(old_raw)
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                rotated = connection.execute(
                    "UPDATE control.oauth_refresh_tokens AS t"
                    "   SET consumed_at = now()"
                    " WHERE t.token_hash = %s"
                    "   AND t.consumed_at IS NULL"
                    "   AND t.idle_expires_at > now()"
                    "   AND EXISTS ("
                    "     SELECT 1 FROM control.oauth_refresh_families f"
                    "       JOIN control.oauth_grants g"
                    "         ON g.grant_id = f.grant_id"
                    "      WHERE f.family_id = t.family_id"
                    "        AND f.revoked_at IS NULL"
                    "        AND f.absolute_expires_at > now()"
                    "        AND g.revoked_at IS NULL)"
                    " RETURNING family_id",
                    (old_digest,),
                ).fetchone()
                if rotated is not None:
                    family_id = rotated["family_id"]
                    family = connection.execute(
                        "SELECT grant_id, client_id, scope, absolute_expires_at"
                        "  FROM control.oauth_refresh_families"
                        " WHERE family_id = %s",
                        (family_id,),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO control.oauth_refresh_tokens"
                        "(token_hash, family_id, idle_expires_at)"
                        " VALUES(%s,%s, now() + make_interval(secs => %s))",
                        (token_digest(new_raw), family_id, idle),
                    )
                    connection.execute(
                        "UPDATE control.oauth_refresh_tokens SET replaced_by = %s"
                        " WHERE token_hash = %s",
                        (token_digest(new_raw), old_digest),
                    )
                    connection.execute("COMMIT")
                    return {
                        "outcome": "rotated",
                        "family_id": family_id,
                        "grant_id": family["grant_id"],
                        "client_id": family["client_id"],
                        "scope": family["scope"],
                    }

                # It did not rotate. Why decides whether this is merely a
                # refusal or evidence of a stolen credential.
                state = connection.execute(
                    "SELECT t.family_id, t.consumed_at,"
                    "       t.consumed_at > now() - make_interval(secs => %s)"
                    "         AS within_grace,"
                    "       t.idle_expires_at <= now() AS idle_expired,"
                    "       f.revoked_at IS NOT NULL AS family_revoked,"
                    "       f.absolute_expires_at <= now() AS family_expired,"
                    "       g.revoked_at IS NOT NULL AS grant_revoked"
                    "  FROM control.oauth_refresh_tokens t"
                    "  LEFT JOIN control.oauth_refresh_families f"
                    "    ON f.family_id = t.family_id"
                    "  LEFT JOIN control.oauth_grants g"
                    "    ON g.grant_id = f.grant_id"
                    " WHERE t.token_hash = %s",
                    (grace, old_digest),
                ).fetchone()
                if state is None:
                    connection.execute("COMMIT")
                    return {"outcome": "unknown"}
                # Revocation is checked before consumption, so a token spent
                # before a restore reports the restore rather than a replay. It
                # used to report "replayed" -- a stolen-credential signal -- for
                # a family the operator's own restore had revoked.
                if state["grant_revoked"]:
                    connection.execute("COMMIT")
                    return {"outcome": "grant-revoked", "family_id": state["family_id"]}
                if state["family_revoked"]:
                    connection.execute("COMMIT")
                    return {"outcome": "family-revoked", "family_id": state["family_id"]}
                if state["consumed_at"] is not None and state["within_grace"] and not state["family_expired"]:
                    # A retry, not a replay. The client presented a token that
                    # was spent moments ago, which is what a lost response, a
                    # restart or two concurrent refreshes all look like. The
                    # successor it should have received cannot be handed over
                    # again -- only the hash was kept -- so this issues a fresh
                    # one instead, which is also what Okta, Auth0 and Ory do
                    # inside their grace windows.
                    #
                    # The absolute bound still applies: grace may not carry a
                    # family past the expiry its consent fixed.
                    # FOR UPDATE, and it is load-bearing. Two retries racing
                    # would otherwise each INSERT its successor and then run a
                    # supersede whose READ COMMITTED snapshot predates the
                    # other's insert -- so neither consumes the other and the
                    # family ends with two live tokens, which is the fork this
                    # whole design exists to prevent. The family row orders
                    # them; the second waits and then sees the first's token.
                    family = connection.execute(
                        "SELECT grant_id, client_id, scope"
                        "  FROM control.oauth_refresh_families"
                        " WHERE family_id = %s FOR UPDATE",
                        (state["family_id"],),
                    ).fetchone()
                    new_digest = token_digest(new_raw)
                    # Insert first: replaced_by is a foreign key to this table,
                    # so the successor has to exist before anything points at it.
                    connection.execute(
                        "INSERT INTO control.oauth_refresh_tokens"
                        "(token_hash, family_id, idle_expires_at)"
                        " VALUES(%s,%s, now() + make_interval(secs => %s))",
                        (new_digest, state["family_id"], idle),
                    )
                    # Supersede whatever this family's live token was, so the
                    # family still holds exactly one. Keyed on the family
                    # rather than on the presented token's replaced_by,
                    # because the chain may already have moved past it -- and
                    # two live tokens would be a fork nothing else here
                    # permits. The token just inserted is excluded, or it
                    # would consume itself.
                    connection.execute(
                        "UPDATE control.oauth_refresh_tokens"
                        "   SET consumed_at = now(), replaced_by = %s"
                        " WHERE family_id = %s"
                        "   AND consumed_at IS NULL"
                        "   AND token_hash <> %s",
                        (new_digest, state["family_id"], new_digest),
                    )
                    # Recorded as well as permitted. A window that silently
                    # forgives is a window nobody can size: an operator seeing
                    # these constantly has a client retrying for a reason, and
                    # one seeing none can close the window without guessing.
                    self._write_audit(
                        connection,
                        "refresh.retried",
                        actor=family["grant_id"],
                        client_id=family["client_id"],
                        grant_id=family["grant_id"],
                        detail={"familyId": state["family_id"]},
                    )
                    connection.execute("COMMIT")
                    return {
                        "outcome": "rotated",
                        # The decision is the same as an ordinary rotation --
                        # a distinct outcome would be refused by every caller
                        # that tests for "rotated" -- but the reason differs,
                        # and an audit log will want it.
                        "grace": True,
                        "family_id": state["family_id"],
                        "grant_id": family["grant_id"],
                        "client_id": family["client_id"],
                        "scope": family["scope"],
                    }
                if state["consumed_at"] is not None:
                    # The replay. Revoke the family and the grant in this same
                    # transaction, so the detection and the consequence cannot
                    # be separated by a crash or a slow caller.
                    connection.execute(
                        "UPDATE control.oauth_refresh_families"
                        "   SET revoked_at = now(), revoked_reason = %s"
                        " WHERE family_id = %s AND revoked_at IS NULL",
                        ("refresh-replay", state["family_id"]),
                    )
                    grant = connection.execute(
                        "UPDATE control.oauth_grants"
                        "   SET revoked_at = now(), revoked_reason = %s"
                        " WHERE grant_id = ("
                        "     SELECT grant_id FROM control.oauth_refresh_families"
                        "      WHERE family_id = %s)"
                        "   AND revoked_at IS NULL"
                        " RETURNING grant_id, client_id",
                        ("refresh-replay", state["family_id"]),
                    ).fetchone()  # noqa: E501 - RETURNING carries the client for the audit
                    # In this transaction, not after it. A replay that
                    # revoked a grant and left no record is exactly the state
                    # an operator cannot diagnose: the agent stops, the error
                    # is invalid_grant, and revoked_reason is a column nothing
                    # reads. P19 requires the record to commit with the change.
                    if grant is not None:
                        # The same event name an operator revocation writes.
                        # "Why is this grant dead" must be one query whatever
                        # killed it; refresh.replayed below adds what is
                        # specific to this cause rather than replacing it.
                        self._write_audit(
                            connection,
                            "grant.revoked",
                            actor=grant["grant_id"],
                            client_id=grant["client_id"],
                            grant_id=grant["grant_id"],
                            detail={"reason": "refresh-replay"},
                        )
                    self._write_audit(
                        connection,
                        "refresh.replayed",
                        actor=grant["grant_id"] if grant else "unknown",
                        grant_id=grant["grant_id"] if grant else None,
                        detail={
                            "familyId": state["family_id"],
                            "grantRevoked": grant is not None,
                            "consequence": "family and grant revoked",
                        },
                    )
                    connection.execute("COMMIT")
                    return {
                        "outcome": "replayed",
                        "family_id": state["family_id"],
                        "grant_revoked": grant["grant_id"] if grant else None,
                    }
                connection.execute("COMMIT")
                if state["family_expired"]:
                    return {"outcome": "family-expired", "family_id": state["family_id"]}
                if state["idle_expired"]:
                    return {"outcome": "idle-expired", "family_id": state["family_id"]}
                # No condition explains it: the row moved between the update and
                # the read. Refuse rather than guess.
                return {"outcome": "unknown", "family_id": state["family_id"]}
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def revoke_refresh_family(self, family_id: str, reason: str = "") -> bool:
        """Revoke one family, reporting whether this call did it.

        Does not touch the grant. An operator withdrawing a refresh family is
        not withdrawing the consent; only a replay does both, and that is
        decided where the replay is detected.
        """
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE control.oauth_refresh_families"
                "   SET revoked_at = now(), revoked_reason = %s"
                " WHERE family_id = %s AND revoked_at IS NULL"
                " RETURNING family_id",
                (reason or None, family_id),
            ).fetchone()
        return row is not None

    def revoke_refresh_families_for_grant(self, grant_id: str, reason: str = "") -> int:
        """Revoke every live family of one grant.

        Revoking a grant already stops a refresh -- the rotation statement
        joins the grant -- so this is not what makes revocation correct. It is
        what stops a revoked grant leaving live-looking families behind for an
        operator to puzzle over.
        """
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE control.oauth_refresh_families"
                "   SET revoked_at = now(), revoked_reason = %s"
                " WHERE grant_id = %s AND revoked_at IS NULL",
                (reason or None, grant_id),
            )
        return result.rowcount

    def query_refresh_family(self, family_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT family_id, grant_id, client_id, scope, created_at,"
                " absolute_expires_at, revoked_at, revoked_reason"
                "  FROM control.oauth_refresh_families WHERE family_id = %s",
                (family_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def refresh_token_state(self, raw_token: str) -> dict | None:
        """Read one refresh token without spending it, for assertions and audit."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT family_id, issued_at, idle_expires_at, consumed_at,"
                " replaced_by"
                "  FROM control.oauth_refresh_tokens WHERE token_hash = %s",
                (token_digest(raw_token),),
            ).fetchone()
        return dict(row) if row is not None else None

    # -- the operator credential -----------------------------------------

    def admin_password_hash(self) -> str:
        """The administrator credential, as ./bin/mapp init wrote it.

        Read live rather than captured at start-up, so changing the password
        takes effect without restarting this component -- and so a component
        that starts before the credential exists does not cache the absence.

        This is the whole reason passwords.py is byte-compatible with
        config-ui's hasher: one credential, written by config_admin.py into
        control.admin_credential and verified here. It was previously taken
        from MCP_AUTH_ADMIN_PASSWORD_HASH, which nothing anywhere set, so a
        correctly deployed component could not authenticate anybody.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT encoded FROM control.admin_credential WHERE id = 1"
            ).fetchone()
        return row["encoded"] if row is not None else ""

    # -- grants ----------------------------------------------------------

    def save_grant(self, grant: Grant) -> Grant:
        """Write the consent, and the record of it, together.

        This row *is* the consent -- the operator looked at a scope list and
        approved it -- so it is the one event whose absence would leave the
        audit trail showing credentials arriving from nowhere.
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO control.oauth_grants"
                "(grant_id, client_id, subject, scopes) VALUES(%s,%s,%s,%s)",
                (grant.grant_id, grant.client_id, grant.subject, list(grant.scopes)),
            )
            self._write_audit(
                connection,
                "grant.created",
                # The operator session that approved it, not the grant: this
                # is the one moment a human acted, and the actor column should
                # say so. Every later event on this grant has the grant as its
                # actor, which is what P3 means by the grant being the actor.
                actor=grant.subject or "operator",
                client_id=grant.client_id,
                grant_id=grant.grant_id,
                detail={"scopes": list(grant.scopes)},
            )
        return grant

    def query_grant(self, grant_id: str) -> Grant | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM control.oauth_grants WHERE grant_id = %s",
                (grant_id,),
            ).fetchone()
        if row is None:
            return None
        return Grant(
            grant_id=row["grant_id"],
            client_id=row["client_id"],
            subject=row["subject"],
            scopes=tuple(row["scopes"]),
            revoked_at=_epoch(row["revoked_at"]),
        )

    # -- the durable audit trail -----------------------------------------

    @staticmethod
    def _write_audit(
        connection,
        event: str,
        *,
        actor: str,
        client_id: str | None = None,
        grant_id: str | None = None,
        detail: dict | None = None,
    ) -> str:
        """Append one record on an existing connection.

        Takes the connection rather than opening one, because P19 requires the
        record to commit with the change it describes. A caller that is already
        inside a transaction passes it in and the two are one act; a crash
        between them is not possible, so a revoked grant nobody can explain is
        not possible either.
        """
        detail = detail or {}
        _check_audit_detail(detail)
        event_id = secrets.token_urlsafe(18)
        connection.execute(
            "INSERT INTO control.audit_event"
            "(event_id, event, actor, client_id, grant_id, detail)"
            " VALUES(%s,%s,%s,%s,%s,%s)",
            (event_id, event, actor, client_id, grant_id, Jsonb(detail)),
        )
        return event_id

    def record_audit(
        self,
        event: str,
        *,
        actor: str,
        client_id: str | None = None,
        grant_id: str | None = None,
        detail: dict | None = None,
    ) -> str:
        """Append one record in its own transaction.

        For decisions that are not themselves a database write. Anything that
        *is* one writes through _write_audit on the same connection instead.
        """
        with self._connect() as connection:
            return self._write_audit(
                connection,
                event,
                actor=actor,
                client_id=client_id,
                grant_id=grant_id,
                detail=detail,
            )

    def audit_tail(self, limit: int = 200, *, grant_id: str | None = None) -> list[dict]:
        """The recent tail, newest first -- the query an operator actually runs."""
        with self._connect() as connection:
            if grant_id is None:
                rows = connection.execute(
                    "SELECT event_id, recorded_at, event, actor, client_id,"
                    " grant_id, detail FROM control.audit_event"
                    " ORDER BY recorded_at DESC, event_id DESC LIMIT %s",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT event_id, recorded_at, event, actor, client_id,"
                    " grant_id, detail FROM control.audit_event"
                    " WHERE grant_id = %s"
                    " ORDER BY recorded_at DESC, event_id DESC LIMIT %s",
                    (grant_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def revoke_grant(self, grant_id: str, reason: str = "") -> bool:
        """Revoke a grant, reporting whether this call was the one that did it.

        One conditional statement, so two operators revoking at once cannot
        both believe they acted. Nothing else is touched: tokens are not
        updated row by row, because every path that reads one resolves it
        through the grant -- introspection, the exchange, and both halves of
        the token-B verification surface (consume_exchanged_token and
        exchanged_binding). So one write invalidates every credential derived
        from the grant, including a token B already issued and not yet spent.

        That was previously true of introspection alone. The two statements
        that actually spend or verify a token B did not join the grant at all,
        so a revoked grant reported inactive and its token still spent.
        """
        with self._connect() as connection:
            row = connection.execute(
                "UPDATE control.oauth_grants"
                "   SET revoked_at = now(), revoked_reason = %s"
                " WHERE grant_id = %s AND revoked_at IS NULL"
                " RETURNING grant_id, client_id",
                (reason or None, grant_id),
            ).fetchone()
            # Only when this call was the one that revoked. The statement is
            # conditional precisely so two callers cannot both believe they
            # acted, and an audit trail claiming two revocations of one grant
            # would undo that.
            if row is not None:
                self._write_audit(
                    connection,
                    "grant.revoked",
                    actor=grant_id,
                    client_id=row["client_id"],
                    grant_id=grant_id,
                    detail={"reason": reason or "unspecified"},
                )
        return row is not None

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

    #: Test-only reader. The flow never calls it: authlib asks the grant for a
    #: code and the grant consumes it through consume_authorization_code_for,
    #: because a one-shot record is read by the statement that spends it. This
    #: exists so a test can assert on what was stored without spending it.
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
                " expires_at, revoked_at)"
                " VALUES(%s,%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s),"
                # Dropped silently before, so a record saved already revoked
                # came back live -- the stub kept the flag and this did not.
                " CASE WHEN %s THEN now() END)",
                (
                    digest,
                    token.client_id,
                    token.subject,
                    token.scope,
                    # Written as given. There used to be a store-level default
                    # of "mcp" behind an `or`, which is the same placeholder
                    # models.py and exchange.py both removed for being
                    # dangerous -- the rule had been applied at two of three
                    # sites. An unset audience now fails the exchange's
                    # comparison, which is what it is for.
                    token.audience,
                    token.issued_at,
                    token.issued_at + token.expires_in,
                    bool(token.revoked),
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

        NOT YET ENFORCED DOWNSTREAM. The design has the configuration API
        re-check the operation and digest on every call, which is what would
        stop a token minted for one proposal being spent on another inside its
        sixty seconds. That half is M7 and no code outside this component
        reads the binding today, so a token B is currently a plain scoped
        bearer credential for the configuration API with a recorded, unchecked
        binding. consume_exchanged_token exists for that caller and has none
        yet.
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

    def consume_exchanged_token(
        self, raw_token: str, operation_id: str, request_digest: str
    ):
        """Spend a single-use token B, or report that it is not spendable.

        The request digest is a predicate of the consuming statement, not
        something returned for the caller to compare afterwards. Returning it
        meant a presentation carrying the right operation but a different
        request body burned the token before anyone noticed the mismatch --
        the legitimate retry would then find it spent.

        One conditional statement, so two calls presenting the same token
        cannot both proceed, which is the entire value of single use.
        """
        with self._connect() as connection:
            return connection.execute(
                "UPDATE control.oauth_tokens SET consumed_at = now()"
                " WHERE token_hash = %s"
                "   AND operation_id = %s"
                "   AND request_digest = %s"
                "   AND single_use"
                "   AND consumed_at IS NULL"
                "   AND revoked_at IS NULL"
                "   AND expires_at > now()"
                # The live grant is a predicate of the spend, not a check the
                # caller is trusted to have made. Without it, revoking a grant
                # made the token report inactive to introspection and still
                # spend successfully here -- and this is the only statement
                # that actually spends one.
                "   AND EXISTS ("
                "     SELECT 1 FROM control.oauth_grants g"
                "      WHERE g.grant_id = oauth_tokens.subject"
                "        AND g.revoked_at IS NULL)"
                " RETURNING scope, subject, request_digest",
                (token_digest(raw_token), operation_id, request_digest),
            ).fetchone()

    def exchanged_binding(self, raw_token: str):
        """Read a token B's binding without spending it, or refuse.

        A read operation's token is not single-use, so it can never go through
        consume_exchanged_token -- which makes this the whole verification
        surface for a read, and therefore the place the grant has to be
        checked. The join is inner on purpose: a token whose subject resolves
        to no grant, or to a revoked one, yields no row and the caller sees
        None. Returning the binding and leaving the grant to the caller would
        put the decision in the one place that cannot be audited from here.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT t.operation_id, t.request_digest, t.single_use,"
                " t.audience, t.actor_client_id, t.broker_client_id,"
                " t.consumed_at, t.expires_at"
                " FROM control.oauth_tokens t"
                " JOIN control.oauth_grants g ON g.grant_id = t.subject"
                " WHERE t.token_hash = %s AND g.revoked_at IS NULL",
                (token_digest(raw_token),),
            ).fetchone()
        return dict(row) if row is not None else None

    #: Test-only. Nothing in the flow counts tokens; assertions about growth do.
    def exchanged_token_count(self, subject: str, window_seconds: int) -> int:
        """How many token B this grant has been issued inside the window.

        Counted from the rows themselves rather than a counter column: a
        counter would need writing, expiring and reconciling, and a consumed
        or revoked token still counts against a burst -- it was still minted.
        operation_id is what distinguishes a token B from a token A; only the
        exchange sets it.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT count(*) AS n FROM control.oauth_tokens"
                " WHERE subject = %s AND operation_id IS NOT NULL"
                "   AND issued_at > now() - make_interval(secs => %s)",
                (subject, window_seconds),
            ).fetchone()
        return int(row["n"])

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
            # Refresh families expire on their own column, and their tokens
            # go with them through the foreign key. Nothing rotated after the
            # absolute expiry, so no replay evidence is lost by then: a
            # presentation of one of these tokens was already refused.
            # Without this the two tables grow for the life of the
            # deployment -- every consent opens a family and every rotation
            # adds a row, and neither is removed by anything else.
            cursor = connection.execute(
                sql.SQL(
                    "DELETE FROM {s}.oauth_refresh_families"
                    " WHERE absolute_expires_at < now()"
                ).format(s=sql.Identifier(SCHEMA))
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
