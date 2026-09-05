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

from models import AuthorizationCode, Client, PendingAuthorization, Session, Token

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
            for table in (
                "oauth_authorization_codes",
                "oauth_pending_authorizations",
                "oauth_tokens",
                "oauth_sessions",
                "oauth_clients",
            ):
                connection.execute(f"DELETE FROM control.{table}")
        self.store.add_client(_client())

    # -- clients ---------------------------------------------------------

    def test_a_client_round_trips(self) -> None:
        found = self.store.query_client("mcp-client")
        self.assertEqual(("mcp:connect", "inspect"), found.scopes)
        self.assertEqual(("http://127.0.0.1:9/cb",), found.redirect_uris)

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


if __name__ == "__main__":
    unittest.main()
