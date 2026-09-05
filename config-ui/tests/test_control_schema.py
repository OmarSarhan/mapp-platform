"""The control schema, against a real PostgreSQL server.

The SQL is the thing under test, and a mocked cursor cannot fail the way
PostgreSQL fails when it parses a statement or evaluates a CHECK, so these
talk to a real server. Set ``CONTROL_TEST_DATABASE_URL`` to a scratch database
whose ``control`` schema may be dropped; without it the suite skips rather than
passing on nothing.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import psycopg
    from psycopg import sql
except ModuleNotFoundError:  # pragma: no cover - exercised by the skip below
    psycopg = None
    sql = None

import control_schema as cs

DATABASE_URL = os.getenv("CONTROL_TEST_DATABASE_URL", "")

requires_database = unittest.skipUnless(
    DATABASE_URL and psycopg is not None,
    "set CONTROL_TEST_DATABASE_URL to a scratch PostgreSQL database to run the"
    " control schema tests; its control schema is dropped and recreated before"
    " every test",
)


def reset_schema() -> None:
    """Empty the schema without dropping it.

    The control role deliberately cannot CREATE ON DATABASE -- the schema is
    created once by docker/postgis/init/10-roles.sh as superuser -- so a
    fixture that dropped and recreated the schema would need a privilege
    production denies, and would test a role that does not exist. Dropping the
    tables it owns is what the role can actually do.
    """
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        rows = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s", (cs.SCHEMA,)
        ).fetchall()
        for row in rows:
            name = row[0] if isinstance(row, tuple) else row["tablename"]
            connection.execute(
                sql.SQL("DROP TABLE IF EXISTS {s}.{t} CASCADE").format(
                    s=sql.Identifier(cs.SCHEMA), t=sql.Identifier(name)
                )
            )


def utc(offset_seconds: float = 0.0) -> dt.datetime:
    return cs.utc_now() + dt.timedelta(seconds=offset_seconds)


@requires_database
class LadderTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_schema()
        self.connection = cs.connect(DATABASE_URL)
        self.addCleanup(self.connection.close)

    def test_the_ladder_applies_every_migration_from_nothing(self) -> None:
        self.assertEqual(sorted(cs.MIGRATIONS), cs.migrate(self.connection))

    def test_the_ladder_is_idempotent(self) -> None:
        cs.migrate(self.connection)
        self.assertEqual([], cs.migrate(self.connection))

    def test_a_second_run_adds_no_duplicate_version_rows(self) -> None:
        cs.migrate(self.connection)
        cs.migrate(self.connection)
        rows = self.connection.execute(
            "SELECT version, count(*) AS n FROM control.schema_migrations"
            " GROUP BY version HAVING count(*) > 1"
        ).fetchall()
        self.assertEqual([], rows)

    def test_concurrent_migrations_serialise(self) -> None:
        """Two starts at once must not race CREATE TABLE.

        One advisory lock and one transaction: the loser blocks, then finds the
        ladder complete. Without the lock this raises DuplicateTable.
        """
        errors: list[BaseException] = []
        applied: list[list[int]] = []
        # Below the role's CONNECTION LIMIT of 8, with room for this test's own
        # connection. The barrier is bounded and aborted on failure: opening
        # the connection before an unbounded wait meant one failed connect
        # hung the whole suite instead of failing it.
        workers = 3
        barrier = threading.Barrier(workers)

        def worker() -> None:
            connection = None
            try:
                connection = cs.connect(DATABASE_URL)
                barrier.wait(timeout=30)
                applied.append(cs.migrate(connection))
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
                barrier.abort()
            finally:
                if connection is not None:
                    connection.close()

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertEqual([], [repr(e) for e in errors])
        # Exactly one worker did the work; the rest found it done.
        did_work = [a for a in applied if a]
        self.assertEqual(1, len(did_work), applied)
        self.assertEqual(sorted(cs.MIGRATIONS), did_work[0])


@requires_database
class SchemaShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_schema()
        connection = cs.connect(DATABASE_URL)
        try:
            cs.migrate(connection)
        finally:
            connection.close()

    def setUp(self) -> None:
        self.connection = cs.connect(DATABASE_URL)
        self.addCleanup(self.connection.close)

    def test_no_sequence_or_identity_column_exists(self) -> None:
        """Keys are hashes of secrets the caller already holds.

        test_database_access_contract asserts no grant mentions ON SEQUENCES,
        so a sequence here would need a privilege the contract forbids.
        """
        sequences = self.connection.execute(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = %s",
            (cs.SCHEMA,),
        ).fetchall()
        self.assertEqual([], sequences)
        identities = self.connection.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = %s AND is_identity = 'YES'",
            (cs.SCHEMA,),
        ).fetchall()
        self.assertEqual([], identities)

    def test_every_timestamp_column_carries_a_time_zone(self) -> None:
        naive = self.connection.execute(
            "SELECT table_name, column_name, data_type"
            " FROM information_schema.columns"
            " WHERE table_schema = %s AND data_type LIKE 'timestamp%%'"
            "   AND data_type <> 'timestamp with time zone'",
            (cs.SCHEMA,),
        ).fetchall()
        self.assertEqual([], naive)

    def test_every_table_that_carries_authority_has_a_recovery_epoch(self) -> None:
        expected = {
            "sessions",
            "tokens",
            "device_authorizations",
            "oauth_authorization_codes",
            "oauth_tokens",
            "oauth_pending_authorizations",
            "oauth_sessions",
        }
        rows = self.connection.execute(
            "SELECT table_name FROM information_schema.columns"
            " WHERE table_schema = %s AND column_name = 'recovery_epoch'",
            (cs.SCHEMA,),
        ).fetchall()
        self.assertEqual(expected, {row["table_name"] for row in rows})

    def test_only_one_administrator_credential_can_exist(self) -> None:
        self.connection.execute("DELETE FROM control.admin_credential")
        self.connection.execute(
            "INSERT INTO control.admin_credential(encoded) VALUES('first')"
        )
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.connection.execute(
                "INSERT INTO control.admin_credential(id, encoded) VALUES(2, 'second')"
            )
        self.connection.execute("DELETE FROM control.admin_credential")

    def test_a_consumed_by_without_a_consumed_at_is_refused(self) -> None:
        self._seed_client("check-client")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.connection.execute(
                "INSERT INTO control.oauth_authorization_codes"
                "(code_hash, client_id, redirect_uri, scope, subject,"
                " code_challenge, expires_at, consumed_by)"
                " VALUES('h1','check-client','u','s','sub','c',%s,'someone')",
                (utc(300),),
            )

    def test_a_non_s256_challenge_method_is_refused_by_the_schema(self) -> None:
        self._seed_client("s256-client")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.connection.execute(
                "INSERT INTO control.oauth_authorization_codes"
                "(code_hash, client_id, redirect_uri, scope, subject,"
                " code_challenge, code_challenge_method, expires_at)"
                " VALUES('h2','s256-client','u','s','sub','c','plain',%s)",
                (utc(300),),
            )

    def test_only_a_single_use_token_may_be_consumed(self) -> None:
        self._seed_client("token-client")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.connection.execute(
                "INSERT INTO control.oauth_tokens"
                "(token_hash, client_id, subject, scope, audience, expires_at,"
                " consumed_at, single_use)"
                " VALUES('t1','token-client','sub','s','a',%s,%s,false)",
                (utc(900), utc()),
            )

    def test_a_consumed_device_row_must_say_when(self) -> None:
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.connection.execute(
                "INSERT INTO control.device_authorizations"
                "(id_hash, user_code, device_name, scopes, created_at,"
                " expires_at, status)"
                " VALUES('d1','AAAA-BBBB','dev','{}',%s,%s,'consumed')",
                (utc(), utc(600)),
            )

    def _seed_client(self, client_id: str) -> None:
        self.connection.execute(
            "INSERT INTO control.oauth_clients"
            "(client_id, name, redirect_uris, scopes, grant_types,"
            " token_endpoint_auth_method)"
            " VALUES(%s,'n','{}','{}','{}','none')"
            " ON CONFLICT (client_id) DO NOTHING",
            (client_id,),
        )


@requires_database
class OneShotTests(unittest.TestCase):
    """The conditional UPDATE is the read, and it decides the race."""

    def setUp(self) -> None:
        reset_schema()
        self.connection = cs.connect(DATABASE_URL)
        self.addCleanup(self.connection.close)
        cs.migrate(self.connection)
        self.connection.execute(
            "INSERT INTO control.oauth_clients"
            "(client_id, name, redirect_uris, scopes, grant_types,"
            " token_endpoint_auth_method)"
            " VALUES('c','n','{}','{}','{}','none')"
        )

    def _seed_code(self, code_hash: str, expires_in: float = 300.0) -> None:
        self.connection.execute(
            "INSERT INTO control.oauth_authorization_codes"
            "(code_hash, client_id, redirect_uri, scope, subject,"
            " code_challenge, expires_at)"
            " VALUES(%s,'c','u','s','sub','ch',%s)",
            (code_hash, utc(expires_in)),
        )

    @staticmethod
    def _consume(connection, code_hash: str, consumer: str):
        return connection.execute(
            "UPDATE control.oauth_authorization_codes"
            "   SET consumed_at = now(), consumed_by = %s"
            " WHERE code_hash = %s"
            "   AND client_id = 'c'"
            "   AND consumed_at IS NULL"
            "   AND expires_at > now()"
            " RETURNING code_hash, subject",
            (consumer, code_hash),
        ).fetchone()

    def test_simultaneous_consumers_produce_exactly_one_winner(self) -> None:
        self._seed_code("raced")
        winners: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        workers = 5  # under the role's CONNECTION LIMIT of 8
        barrier = threading.Barrier(workers)

        def worker(index: int) -> None:
            connection = None
            try:
                connection = cs.connect(DATABASE_URL)
                barrier.wait(timeout=30)
                row = self._consume(connection, "raced", f"worker-{index}")
                if row is not None:
                    with lock:
                        winners.append(f"worker-{index}")
            except BaseException as exc:  # noqa: BLE001 - reported below
                with lock:
                    errors.append(exc)
                barrier.abort()
            finally:
                if connection is not None:
                    connection.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual([], [repr(e) for e in errors])
        self.assertEqual(1, len(winners), winners)

    def test_a_replay_is_detectably_a_replay(self) -> None:
        """The row survives consumption, so a second attempt is distinguishable
        from an unrecognised code -- which is what lets a replay be audited."""
        self._seed_code("replayed")
        self.assertIsNotNone(self._consume(self.connection, "replayed", "first"))
        self.assertIsNone(self._consume(self.connection, "replayed", "second"))
        row = self.connection.execute(
            "SELECT consumed_at, consumed_by FROM control.oauth_authorization_codes"
            " WHERE code_hash = 'replayed'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("first", row["consumed_by"])

    def test_an_expired_code_is_never_consumed(self) -> None:
        self._seed_code("stale", expires_in=-1.0)
        self.assertIsNone(self._consume(self.connection, "stale", "someone"))
        row = self.connection.execute(
            "SELECT consumed_at FROM control.oauth_authorization_codes"
            " WHERE code_hash = 'stale'"
        ).fetchone()
        self.assertIsNone(row["consumed_at"])


if __name__ == "__main__":
    unittest.main()
