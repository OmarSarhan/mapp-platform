"""SqlStore against a real server, holding the same contract as StubStore.

The stub is what the rest of the suite runs on, so these tests exist to prove
the SQL implementation agrees with it where it must, and differs only where the
move to a database is the point: the database decides a race between processes,
and a consumed record survives as evidence rather than vanishing.

Set ``CONTROL_TEST_DATABASE_URL`` to a scratch database whose ``control``
schema may be emptied; without it these skip rather than passing on nothing.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config-ui"))

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - exercised by the skip below
    psycopg = None

from models import AuthorizationCode, Client, Grant, PendingAuthorization, Session, Token

DATABASE_URL = os.getenv("CONTROL_TEST_DATABASE_URL", "")

requires_database = unittest.skipUnless(
    DATABASE_URL and psycopg is not None,
    "set CONTROL_TEST_DATABASE_URL to a scratch PostgreSQL database to run the"
    " SqlStore tests",
)

if DATABASE_URL and psycopg is not None:
    import control_schema as cs

    from sql_store import MAX_PENDING_PER_SOURCE, PendingLimitReached, SqlStore


def _client(client_id: str = "mcp-client") -> Client:
    return Client(
        client_id=client_id,
        name="Claude Code",
        redirect_uris=("http://127.0.0.1:9/cb",),
        scopes=("mcp:connect", "inspect"),
        token_endpoint_auth_method="none",
    )


@requires_database
class SqlStoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        connection = cs.connect(DATABASE_URL)
        try:
            cs.migrate(connection)
        finally:
            connection.close()

    def setUp(self) -> None:
        self.store = SqlStore(DATABASE_URL)
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            for table in cs.TABLES_IN_DELETE_ORDER:
                connection.execute(f"DELETE FROM control.{table}")
        self.store.add_client(_client())
        # The grant every seeded code and token belongs to. There is no
        # foreign key to enforce that -- control_schema.py declines to add one
        # deliberately, and a token whose subject resolves to no grant writes
        # cleanly. The linkage is an application rule, checked in
        # introspection.py and exchange.py and in both halves of the token-B
        # verification surface, and pinned by tests/store_contract.py.
        self.store.save_grant(
            Grant(
                grant_id="oauth:grant-1",
                client_id="mcp-client",
                subject="admin",
                scopes=("mcp:connect", "inspect"),
            )
        )

    # -- clients ---------------------------------------------------------

    def test_a_client_round_trips(self) -> None:
        found = self.store.query_client("mcp-client")
        self.assertEqual(("mcp:connect", "inspect"), found.scopes)
        self.assertEqual(("http://127.0.0.1:9/cb",), found.redirect_uris)

    def test_a_disabled_client_is_not_returned(self) -> None:
        """The same rule the in-memory double enforces.

        SqlStore could not persist disablement at all, so this rule was only
        ever exercised against the double -- each store's tests build their own
        fixture, so the two could disagree about who may act and both suites
        would stay green.
        """
        self.store.add_client(
            Client(
                client_id="disabled-client", name="Gone", redirect_uris=(),
                scopes=("mcp:connect",), token_endpoint_auth_method="none",
                disabled=True,
            )
        )
        self.assertIsNone(self.store.query_client("disabled-client"))

    def test_re_enabling_a_client_makes_it_visible_again(self) -> None:
        self.store.add_client(
            Client(client_id="toggle", name="T", redirect_uris=(),
                   scopes=("mcp:connect",), token_endpoint_auth_method="none",
                   disabled=True)
        )
        self.assertIsNone(self.store.query_client("toggle"))
        self.store.add_client(
            Client(client_id="toggle", name="T", redirect_uris=(),
                   scopes=("mcp:connect",), token_endpoint_auth_method="none")
        )
        self.assertIsNotNone(self.store.query_client("toggle"))

    def test_an_unknown_client_is_none(self) -> None:
        self.assertIsNone(self.store.query_client("nosuch"))

    def test_a_client_secret_is_not_stored_in_the_clear(self) -> None:
        self.store.add_client(
            Client(
                client_id="confidential",
                name="Broker",
                redirect_uris=("http://127.0.0.1:9/cb",),
                scopes=("mcp:connect",),
                token_endpoint_auth_method="client_secret_basic",
                client_secret="s3cret",
            )
        )
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            row = connection.execute(
                "SELECT client_secret_hash FROM control.oauth_clients"
                " WHERE client_id = 'confidential'"
            ).fetchone()
        self.assertNotIn("s3cret", str(row))
        # And the comparison still works through the hashed path.
        found = self.store.query_client("confidential")
        self.assertTrue(found.check_client_secret("s3cret"))
        self.assertFalse(found.check_client_secret("wrong"))

    # -- authorization codes ---------------------------------------------

    def _code(self, code: str = "c1", expires_in: float = 300.0) -> AuthorizationCode:
        return AuthorizationCode(
            code=code,
            client_id="mcp-client",
            redirect_uri="http://127.0.0.1:9/cb",
            scope="mcp:connect",
            subject="oauth:grant-1",
            code_challenge="challenge",
            expires_at=time.time() + expires_in,
        )

    def test_a_code_is_not_stored_in_the_clear(self) -> None:
        self.store.save_authorization_code(self._code("secret-code"))
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            rows = connection.execute(
                "SELECT code_hash FROM control.oauth_authorization_codes"
            ).fetchall()
        self.assertNotIn("secret-code", str(rows))

    def test_consuming_returns_the_record_once(self) -> None:
        self.store.save_authorization_code(self._code())
        first = self.store.consume_authorization_code_for("c1", "mcp-client")
        self.assertIsNotNone(first)
        self.assertEqual("oauth:grant-1", first.subject)
        self.assertIsNone(self.store.consume_authorization_code_for("c1", "mcp-client"))

    def test_a_consumed_code_survives_as_replay_evidence(self) -> None:
        """The stub deletes; this marks. That difference is the point."""
        self.store.save_authorization_code(self._code())
        self.store.consume_authorization_code_for("c1", "mcp-client")
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            row = connection.execute(
                "SELECT consumed_at, consumed_by FROM control.oauth_authorization_codes"
            ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual("mcp-client", row[1])

    def test_another_client_cannot_consume_the_code(self) -> None:
        self.store.add_client(_client("second"))
        self.store.save_authorization_code(self._code())
        self.assertIsNone(self.store.consume_authorization_code_for("c1", "second"))
        # And the rightful owner's code survives the attempt.
        self.assertIsNotNone(self.store.consume_authorization_code_for("c1", "mcp-client"))

    def test_an_expired_code_is_never_consumed(self) -> None:
        self.store.save_authorization_code(self._code("stale", expires_in=-1.0))
        self.assertIsNone(self.store.consume_authorization_code_for("stale", "mcp-client"))

    def test_concurrent_consumers_across_connections_yield_one_winner(self) -> None:
        """The database decides, not a process-local lock.

        This is what the stub could not prove: its lock only serialises threads
        in one process, while two config-ui workers are two processes.
        """
        self.store.save_authorization_code(self._code("raced"))
        winners: list[int] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        workers = 5  # below the role's CONNECTION LIMIT of 8
        barrier = threading.Barrier(workers)

        def worker(index: int) -> None:
            try:
                store = SqlStore(DATABASE_URL)
                barrier.wait(timeout=30)
                if store.consume_authorization_code_for("raced", "mcp-client"):
                    with lock:
                        winners.append(index)
            except BaseException as exc:  # noqa: BLE001 - reported below
                with lock:
                    errors.append(exc)
                barrier.abort()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual([], [repr(e) for e in errors])
        self.assertEqual(1, len(winners), winners)

    # -- pending authorizations ------------------------------------------

    def _pending(self, request_id: str = "r1", **kwargs) -> PendingAuthorization:
        fields = dict(
            request_id=request_id,
            query="response_type=code",
            client_id="mcp-client",
            redirect_uri="http://127.0.0.1:9/cb",
            scopes=("mcp:connect",),
            csrf="form-token",
            expires_at=time.time() + 600,
        )
        fields.update(kwargs)
        return PendingAuthorization(**fields)

    def test_an_unbound_record_cannot_be_consumed(self) -> None:
        # Refused by the session_hash equality: the record stores '' and the
        # submission carries 'sess'. The harder case, where the submission also
        # carries nothing, is test_an_empty_session_never_matches.
        self.store.save_pending(self._pending())
        self.assertIsNone(self.store.consume_pending("r1", "form-token", "sess"))

    def test_binding_rotates_the_form_token(self) -> None:
        self.store.save_pending(self._pending())
        bound = self.store.bind_pending_session("r1", "sess")
        self.assertNotEqual("form-token", bound.csrf)
        # The pre-binding token is dead.
        self.assertIsNone(self.store.consume_pending("r1", "form-token", "sess"))
        self.assertIsNotNone(self.store.consume_pending("r1", bound.csrf, "sess"))

    def test_a_wrong_session_cannot_consume(self) -> None:
        self.store.save_pending(self._pending())
        bound = self.store.bind_pending_session("r1", "sess")
        self.assertIsNone(self.store.consume_pending("r1", bound.csrf, "other"))
        # Non-destructive: the operator's record survives a refused attempt.
        self.assertIsNotNone(self.store.consume_pending("r1", bound.csrf, "sess"))

    def test_an_empty_session_never_matches(self) -> None:
        """An unbound record must not match a submission carrying no session.

        Both store '' , so a bare equality would consume a record the operator
        was never shown. Guarded in Python and again in SQL; either alone
        suffices, so this fails only when both are gone -- which is the state
        that is actually exploitable.
        """
        self.store.save_pending(self._pending())
        self.assertIsNone(self.store.consume_pending("r1", "form-token", ""))

    def test_one_source_cannot_fill_the_table(self) -> None:
        refused = 0
        for index in range(MAX_PENDING_PER_SOURCE + 5):
            try:
                self.store.save_pending(
                    self._pending(f"flood-{index}", source="198.51.100.7")
                )
            except PendingLimitReached:
                refused += 1
        self.assertGreater(refused, 0)
        # A different source is unaffected.
        self.store.save_pending(self._pending("other", source="203.0.113.5"))
        self.assertIsNotNone(self.store.query_pending("other"))

    def test_sweeping_removes_expired_records(self) -> None:
        self.store.save_pending(self._pending("gone", expires_at=time.time() - 1))
        self.store.save_pending(self._pending("live"))
        self.store.sweep_expired()
        self.assertIsNone(self.store.query_pending("gone"))
        self.assertIsNotNone(self.store.query_pending("live"))

    # -- tokens and sessions ---------------------------------------------

    def test_a_token_is_keyed_by_its_hash(self) -> None:
        raw = "mapp_a_secret"
        self.store.save_token(
            raw,
            Token(
                token_hash="",
                client_id="mcp-client",
                scope="mcp:connect",
                subject="oauth:grant-1",
                issued_at=int(time.time()),
                expires_in=900,
            ),
        )
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            rows = connection.execute(
                "SELECT token_hash FROM control.oauth_tokens"
            ).fetchall()
        self.assertNotIn(raw, str(rows))
        self.assertIsNotNone(self.store.query_token(raw))
        self.assertEqual(1, self.store.token_count())

    def test_a_session_round_trips_and_expires(self) -> None:
        now = time.time()
        self.store.save_session(
            "cookie", Session(subject="admin", auth_time=now, expires_at=now + 600)
        )
        self.assertEqual("admin", self.store.query_session("cookie").subject)
        self.store.save_session(
            "stale", Session(subject="admin", auth_time=now, expires_at=now - 1)
        )
        self.assertIsNone(self.store.query_session("stale"))

    def test_a_session_is_keyed_by_its_hash(self) -> None:
        now = time.time()
        self.store.save_session(
            "cookie-secret", Session(subject="admin", auth_time=now, expires_at=now + 600)
        )
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            rows = connection.execute(
                "SELECT session_hash FROM control.oauth_sessions"
            ).fetchall()
        self.assertNotIn("cookie-secret", str(rows))


@requires_database
class ExchangedTokenTests(unittest.TestCase):
    """Token B against the real schema: bound, single-use, unchainable."""

    @classmethod
    def setUpClass(cls) -> None:
        connection = cs.connect(DATABASE_URL)
        try:
            cs.migrate(connection)
        finally:
            connection.close()

    def setUp(self) -> None:
        import datetime as dt

        from collections import defaultdict

        import canonical
        import exchange

        self.dt = dt
        self.canonical = canonical
        self.exchange = exchange
        self.defaultdict = defaultdict
        self.resource = "http://config.localhost/api"
        self.mcp_resource = "http://mcp.localhost/mcp"
        self.store = SqlStore(DATABASE_URL)
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                "TRUNCATE control.oauth_authorization_codes,"
                " control.oauth_pending_authorizations, control.oauth_tokens,"
                " control.oauth_sessions, control.oauth_grants,"
                " control.oauth_clients CASCADE"
            )
        self.broker = Client(
            client_id="broker", name="B", redirect_uris=(), scopes=(),
            token_endpoint_auth_method="client_secret_basic", client_secret="s",
        )
        self.store.add_client(self.broker)
        self.store.add_client(_client("mcp-client"))
        self.now = dt.datetime.now(dt.timezone.utc)
        self.store.save_grant(
            Grant(grant_id="oauth:grant-1", client_id="mcp-client",
                  subject="admin", scopes=("apply", "derive", "semantic:inspect"))
        )
        self.store.save_token(
            "mapp_a_sub",
            Token(
                token_hash="", client_id="mcp-client", scope="apply",
                subject="oauth:grant-1", issued_at=int(self.now.timestamp()),
                expires_in=900, audience=self.mcp_resource,
            ),
        )

    def _request(self, **overrides):
        import json

        context = {
            "version": self.canonical.SCHEME,
            "operationId": "proposals.apply",
            "method": "POST",
            "pathTemplate": "/api/proposals/{proposalId}/apply",
            "requestDigest": self.canonical.digest({"proposalId": "p1"}),
        }
        fields = {
            "grant_type": self.exchange.GRANT_TYPE,
            "subject_token": "mapp_a_sub",
            "subject_token_type": self.exchange.ACCESS_TOKEN_TYPE,
            "resource": self.resource,
            "scope": "apply",
            self.exchange.CONTEXT_PARAMETER: json.dumps(context),
        }
        fields.update(overrides)
        datalist = self.defaultdict(list)
        for name, value in fields.items():
            datalist[name].append(value)
        return datalist

    def _exchange(self, **overrides):
        return self.exchange.exchange(
            datalist=self._request(**overrides),
            broker_client=self.broker,
            store=self.store,
            resource=self.resource,
            mcp_resource=self.mcp_resource,
            now=self.now,
        )

    def test_the_issued_token_is_bound_and_single_use(self) -> None:
        token = self._exchange()
        self.assertEqual(60, token["expires_in"])
        self.assertNotIn("refresh_token", token)
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            row = connection.execute(
                "SELECT operation_id, single_use, audience, actor_client_id,"
                " broker_client_id FROM control.oauth_tokens"
                " WHERE audience = %s", (self.resource,)
            ).fetchone()
        self.assertEqual("proposals.apply", row[0])
        self.assertTrue(row[1])
        self.assertEqual(self.resource, row[2])
        # The originating client comes from the subject token, the broker from
        # client authentication; an audit can tell "who asked" from "who
        # brokered" only because they are stored separately.
        self.assertEqual("mcp-client", row[3])
        self.assertEqual("broker", row[4])

    def test_a_single_use_token_is_spent_exactly_once(self) -> None:
        token = self._exchange()["access_token"]
        digest = self.canonical.digest({"proposalId": "p1"})
        self.assertIsNotNone(
            self.store.consume_exchanged_token(token, "proposals.apply", digest)
        )
        self.assertIsNone(
            self.store.consume_exchanged_token(token, "proposals.apply", digest)
        )

    def test_it_cannot_be_spent_against_another_operation(self) -> None:
        # The binding is the point: a token minted for one operation must not
        # work for another even inside its sixty seconds.
        token = self._exchange()["access_token"]
        self.assertIsNone(
            self.store.consume_exchanged_token(
                token, "layers.values", self.canonical.digest({"proposalId": "p1"})
            )
        )

    def test_a_token_b_cannot_be_exchanged_again(self) -> None:
        """Otherwise one sixty-second credential chains into an endless series."""
        token = self._exchange()["access_token"]
        with self.assertRaises(self.exchange.ExchangeError) as caught:
            self._exchange(subject_token=token)
        self.assertEqual("invalid_grant", caught.exception.error)

    def test_the_stored_expiry_agrees_with_the_advertised_lifetime(self) -> None:
        """Asserted against the literal, not against the same constant.

        Comparing expires_in to TOKEN_B_MAX_LIFETIME could not fail: it is the
        value that produced it. This pins the number and checks the persisted
        record agrees with what the client was told.
        """
        token = self._exchange()
        self.assertEqual(60, token["expires_in"])
        binding = self.store.exchanged_binding(token["access_token"])
        issued_for = (binding["expires_at"] - self.now).total_seconds()
        self.assertAlmostEqual(60, issued_for, delta=2)

    def test_a_mismatched_digest_leaves_the_token_intact(self) -> None:
        """A wrong request must not burn the credential.

        With the digest merely returned rather than tested, a presentation
        carrying the right operation and the wrong body spent the token, and
        the legitimate retry then found it consumed.
        """
        token = self._exchange()["access_token"]
        right = self.canonical.digest({"proposalId": "p1"})
        wrong = self.canonical.digest({"proposalId": "p2"})
        self.assertIsNone(
            self.store.consume_exchanged_token(token, "proposals.apply", wrong)
        )
        self.assertIsNotNone(
            self.store.consume_exchanged_token(token, "proposals.apply", right)
        )

    def test_an_expired_token_b_is_not_consumable(self) -> None:
        token = self._exchange()["access_token"]
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                "UPDATE control.oauth_tokens SET expires_at = now() - interval '1 second'"
                " WHERE audience = %s", (self.resource,)
            )
        self.assertIsNone(
            self.store.consume_exchanged_token(
                token, "proposals.apply", self.canonical.digest({"proposalId": "p1"})
            )
        )

    def test_a_revoked_token_b_is_not_consumable(self) -> None:
        token = self._exchange()["access_token"]
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                "UPDATE control.oauth_tokens SET revoked_at = now()"
                " WHERE audience = %s", (self.resource,)
            )
        self.assertIsNone(
            self.store.consume_exchanged_token(
                token, "proposals.apply", self.canonical.digest({"proposalId": "p1"})
            )
        )

    def test_a_non_single_use_token_is_not_consumable(self) -> None:
        """A read token has no consume path, so its binding is read instead."""
        self.store.save_token(
            "mapp_a_reader",
            Token(
                token_hash="", client_id="mcp-client", scope="derive semantic:inspect",
                subject="oauth:grant-1", issued_at=int(self.now.timestamp()),
                expires_in=900, audience=self.mcp_resource,
            ),
        )
        token = self._exchange(
            subject_token="mapp_a_reader",
            scope="derive semantic:inspect",
            **{self.exchange.CONTEXT_PARAMETER: __import__("json").dumps({
                "version": self.canonical.SCHEME,
                "operationId": "layers.values",
                "method": "GET",
                "pathTemplate": "/api/layers/{layerKey}/values",
                "requestDigest": self.canonical.digest({"layerKey": "l1"}),
            })},
        )["access_token"]
        self.assertIsNone(
            self.store.consume_exchanged_token(
                token, "layers.values", self.canonical.digest({"layerKey": "l1"})
            )
        )
        binding = self.store.exchanged_binding(token)
        self.assertFalse(binding["single_use"])
        self.assertEqual("layers.values", binding["operation_id"])

    def test_a_refused_scope_persists_no_token(self) -> None:
        self.store.save_token(
            "mapp_a_narrow",
            Token(
                token_hash="", client_id="mcp-client", scope="derive",
                subject="oauth:grant-1", issued_at=int(self.now.timestamp()),
                expires_in=900, audience=self.mcp_resource,
            ),
        )
        before = self.store.token_count()
        with self.assertRaises(self.exchange.ExchangeError):
            self._exchange(subject_token="mapp_a_narrow")
        self.assertEqual(before, self.store.token_count())


if __name__ == "__main__":
    unittest.main()


@requires_database
class OperatorCredentialTests(unittest.TestCase):
    """One credential, written by config-ui and verified by this component.

    passwords.py claims byte-compatibility with config-ui's hasher. Nothing
    checked it, and for a while nothing needed to: the consent screen read
    MCP_AUTH_ADMIN_PASSWORD_HASH, a variable no compose file, .env.example or
    script ever set, while ./bin/mapp init wrote control.admin_credential --
    a table this component did not read. The two halves of the claim were
    never joined, so the deployed component could authenticate nobody.

    This is the join: config-ui hashes, the control schema stores, SqlStore
    reads, and mcp-auth's own verifier accepts. A change to either hasher
    breaks it here rather than in production.
    """

    @classmethod
    def setUpClass(cls) -> None:
        connection = cs.connect(DATABASE_URL)
        try:
            cs.migrate(connection)
        finally:
            connection.close()

    def setUp(self) -> None:
        self.store = SqlStore(DATABASE_URL)
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute("DELETE FROM control.admin_credential")

    def _write_as_config_ui(self, password: str) -> str:
        import control_plane

        encoded = control_plane.password_hash(password)
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                "INSERT INTO control.admin_credential(id, encoded) VALUES(1, %s)"
                " ON CONFLICT (id) DO UPDATE SET encoded = EXCLUDED.encoded",
                (encoded,),
            )
        return encoded

    def test_a_credential_written_by_config_ui_verifies_here(self) -> None:
        import passwords

        self._write_as_config_ui("correct horse battery staple")
        stored = self.store.admin_password_hash()
        self.assertTrue(stored)
        self.assertTrue(
            passwords.verify_password("correct horse battery staple", stored)
        )
        self.assertFalse(passwords.verify_password("wrong", stored))

    def test_both_hashers_agree_on_the_encoding(self) -> None:
        """Not merely mutually verifiable -- the same format, parameters and all."""
        import control_plane

        import passwords

        self.assertEqual(passwords.PBKDF2_ROUNDS, control_plane.PBKDF2_ROUNDS)
        theirs = control_plane.password_hash("shared secret")
        mine = passwords.password_hash("shared secret")
        self.assertEqual(theirs.split("$")[:2], mine.split("$")[:2])
        # And each verifier accepts the other's output.
        self.assertTrue(passwords.verify_password("shared secret", theirs))
        self.assertTrue(control_plane.verify_password("shared secret", mine))

    def test_an_absent_credential_reads_as_empty_not_as_an_error(self) -> None:
        """A component that starts before ./bin/mapp init must not crash.

        It must refuse every sign-in instead, which an empty hash does:
        verify_password returns False for it.
        """
        import passwords

        self.assertEqual("", self.store.admin_password_hash())
        self.assertFalse(passwords.verify_password("anything", ""))


@requires_database
class RefreshRotationConcurrencyTests(unittest.TestCase):
    """Rotation must have exactly one winner, across connections.

    "Rotated every use" is only meaningful if two simultaneous presentations of
    the same token cannot both produce a replacement. And the loser must be a
    *replay* -- which revokes the family and the grant -- because a store that
    quietly let the second one through would make a stolen token as good as a
    legitimate one.

    That is also the sharpest edge of open item O7: a client that retries a
    timed-out refresh is indistinguishable from an attacker here, and this test
    pins the containment rather than the comfort.
    """

    @classmethod
    def setUpClass(cls) -> None:
        connection = cs.connect(DATABASE_URL)
        try:
            cs.migrate(connection)
        finally:
            connection.close()

    def setUp(self) -> None:
        self.store = SqlStore(DATABASE_URL)
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            for table in cs.TABLES_IN_DELETE_ORDER:
                connection.execute(f"DELETE FROM control.{table}")
        self.store.add_client(_client())
        self.store.save_grant(
            Grant(
                grant_id="oauth:grant-1",
                client_id="mcp-client",
                subject="operator",
                scopes=("mcp:connect", "inspect"),
            )
        )
        self.store.start_refresh_family(
            "mapp_r_start",
            family_id="fam",
            grant_id="oauth:grant-1",
            client_id="mcp-client",
            scope="mcp:connect",
        )

    def test_simultaneous_rotations_yield_one_replacement(self) -> None:
        outcomes: list = []

        def rotate(index):
            store = SqlStore(DATABASE_URL)
            try:
                outcomes.append(
                    store.rotate_refresh_token("mapp_r_start", f"mapp_r_new_{index}")
                )
            except BaseException as exc:  # noqa: BLE001 - recorded then asserted
                outcomes.append(exc)

        threads = [threading.Thread(target=rotate, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertTrue(all(isinstance(item, dict) for item in outcomes), outcomes)
        rotated = [item for item in outcomes if item["outcome"] == "rotated"]
        self.assertEqual(1, len(rotated), outcomes)
        # Exactly one replacement exists, and it is the winner's.
        winner = rotated[0]
        live = [
            index
            for index in range(6)
            if self.store.refresh_token_state(f"mapp_r_new_{index}") is not None
        ]
        self.assertEqual(1, len(live))
        self.assertEqual("fam", winner["family_id"])

    def test_the_losers_are_replays_and_the_family_is_contained(self) -> None:
        """Whether a loser reports 'replayed' depends on the timing of its read.

        A loser that reaches the diagnostic after the winner committed sees a
        consumed token and reports a replay; one that reads inside the winner's
        window sees it unconsumed and reports 'unknown'. Both refuse, which is
        the property that matters. What must always hold is that a *later*
        presentation of the spent token is a replay and the family is contained.
        """
        outcomes: list = []

        def rotate(index):
            store = SqlStore(DATABASE_URL)
            outcomes.append(
                store.rotate_refresh_token("mapp_r_start", f"mapp_r_new_{index}")
            )

        threads = [threading.Thread(target=rotate, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(
            1, sum(1 for item in outcomes if item["outcome"] == "rotated")
        )
        for item in outcomes:
            with self.subTest(outcome=item["outcome"]):
                self.assertIn(
                    item["outcome"], {"rotated", "replayed", "unknown", "grant-revoked"}
                )
        # Now, unambiguously after the fact:
        after = self.store.rotate_refresh_token("mapp_r_start", "mapp_r_late")
        self.assertIn(after["outcome"], {"replayed", "grant-revoked", "family-revoked"})
        self.assertTrue(self.store.query_grant("oauth:grant-1").is_revoked())
        self.assertIsNotNone(self.store.query_refresh_family("fam")["revoked_at"])
