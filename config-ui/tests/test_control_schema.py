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

    def test_every_table_that_carries_authority_reserves_a_recovery_epoch(self) -> None:
        """Reserved storage, not an enforced control.

        Nothing writes or reads this column yet, so this pins that the storage
        exists and nothing more. Renamed because the previous name read as
        though snapshot recovery were implemented.
        """
        # Derived from the sweep's own lists rather than written out. A
        # hardcoded set breaks on every migration that adds an authority-
        # bearing table -- migration 6 did exactly that -- and the property
        # worth asserting is that the column and the sweep agree, which a copy
        # cannot express.
        #
        # The grant matters most: every token resolves through it, so a restore
        # that invalidated tokens but left grants live would re-authorise
        # everything on the next exchange.
        expected = set(cs.EPOCH_REVOKED_TABLES) | set(cs.EPOCH_DELETED_TABLES)
        self.assertIn("oauth_grants", expected)
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

    def test_two_live_tokens_cannot_share_a_name(self) -> None:
        self._token("shared", revoked=False)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self._token("shared", revoked=False)
        self.connection.execute("DELETE FROM control.tokens")

    def test_a_case_variant_live_name_is_refused(self) -> None:
        """name_key is folded in Python and stored, never recomputed in SQL.

        PostgreSQL lower() is not str.casefold -- 'SS' and 'ß' fold together in
        Python and not in SQL -- so deriving the key here would reserve a
        different set of names than the store checks.
        """
        self._token("Shared", revoked=False)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self._token("SHARED", revoked=False)
        self.connection.execute("DELETE FROM control.tokens")

    def test_revoked_tokens_may_share_a_name(self) -> None:
        """Which is what lets real history import.

        One deployment holds 278 tokens with 7 repeated folded names, 51 of
        them `federation-e2e`, all created before the harness appended a random
        suffix and all since revoked. A full unique index would make that data
        unimportable; the store still refuses to reuse the name for a new
        token, which is the rule that matters.
        """
        self._token("recycled", revoked=True)
        self._token("recycled", revoked=True)
        self._token("recycled", revoked=False)
        rows = self.connection.execute(
            "SELECT count(*) AS n FROM control.tokens WHERE name_key = 'recycled'"
        ).fetchone()
        self.assertEqual(3, rows["n"])
        self.connection.execute("DELETE FROM control.tokens")

    _token_seq = 0

    def _token(self, name: str, *, revoked: bool) -> None:
        type(self)._token_seq += 1
        suffix = str(self._token_seq)
        self.connection.execute(
            "INSERT INTO control.tokens"
            "(token_hash, token_id, name, name_key, created_at, scopes, revoked_at)"
            " VALUES(%s,%s,%s,%s,now(),'{}',%s)",
            (
                "hash-" + suffix,
                "id-" + suffix,
                name,
                name.casefold(),
                utc(-3600) if revoked else None,
            ),
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


@requires_database
class RollbackLadderTests(unittest.TestCase):
    """The half of the ladder that did not exist until Phase 1.

    Forward-only migrations made every schema change a one-way door: a bad
    migration could only be fixed by another one, under whatever pressure
    produced the first. Phase 1 requires "tested forward and rollback
    migrations before issuing production-like state", and the interesting word
    is tested -- writing the down-steps by reading the up-steps produced two
    ownership errors that only a round trip found.
    """

    def setUp(self) -> None:
        reset_schema()
        self.connection = cs.connect(DATABASE_URL)
        self.addCleanup(self.connection.close)

    # -- structure -------------------------------------------------------

    def test_the_delete_order_covers_every_table_the_ladder_creates(self) -> None:
        """The drift guard for six fixtures that used to hold their own copy.

        Migration 6 broke every copy at once -- its oauth_refresh_families
        references oauth_clients, so a list written before it existed deleted
        the clients first and hit a foreign key. One list now, and this fails
        if a migration adds a table without extending it.
        """
        cs.migrate(self.connection)
        self.assertEqual(
            cs.all_tables(self.connection),
            set(cs.TABLES_IN_DELETE_ORDER) | {"schema_migrations"},
        )

    def test_the_delete_order_is_actually_safe(self) -> None:
        """Asserting the membership is not enough; the order has to work.

        A list containing every table but in the wrong sequence passes the
        check above and fails every fixture, which is precisely what happened.
        """
        cs.migrate(self.connection)
        # Seed one row wherever a foreign key could bite: a client, a grant
        # referencing it, a refresh family referencing the client, and a token
        # referencing the family.
        self.connection.execute(
            "INSERT INTO control.oauth_clients(client_id, name, redirect_uris,"
            " scopes, grant_types, token_endpoint_auth_method)"
            " VALUES('c','n','{}','{}','{}','none')"
        )
        self.connection.execute(
            "INSERT INTO control.oauth_grants(grant_id, client_id, subject,"
            " scopes) VALUES('g','c','op','{}')"
        )
        self.connection.execute(
            "INSERT INTO control.oauth_refresh_families(family_id, grant_id,"
            " client_id, scope, absolute_expires_at)"
            " VALUES('f','g','c','apply', now() + interval '30 days')"
        )
        self.connection.execute(
            "INSERT INTO control.oauth_refresh_tokens(token_hash, family_id,"
            " idle_expires_at) VALUES('t','f', now() + interval '12 hours')"
        )
        for table in cs.TABLES_IN_DELETE_ORDER:
            with self.subTest(table=table):
                self.connection.execute(
                    sql.SQL("DELETE FROM {schema}.{table}").format(
                        schema=sql.Identifier(cs.SCHEMA),
                        table=sql.Identifier(table),
                    )
                )

    def test_every_migration_has_a_rollback(self) -> None:
        """The drift guard. A migration added without one reintroduces the door."""
        self.assertEqual(sorted(cs.MIGRATIONS), sorted(cs.ROLLBACKS))

    def test_every_destructive_rollback_is_a_real_migration(self) -> None:
        self.assertTrue(set(cs.DESTRUCTIVE_ROLLBACKS) <= set(cs.MIGRATIONS))

    def test_each_loss_description_says_what_is_lost(self) -> None:
        for version, loss in cs.DESTRUCTIVE_ROLLBACKS.items():
            with self.subTest(version=version):
                self.assertGreater(len(loss), 20, "a warning must name the loss")

    # -- the round trip --------------------------------------------------

    def snapshot(self) -> dict:
        """Everything the ladder is responsible for building."""
        run = self.connection.execute
        return {
            "tables": sorted(
                row["table_name"]
                for row in run(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = 'control'"
                ).fetchall()
            ),
            "columns": sorted(
                (row["table_name"], row["column_name"])
                for row in run(
                    "SELECT table_name, column_name FROM information_schema.columns"
                    " WHERE table_schema = 'control'"
                ).fetchall()
            ),
            "indexes": sorted(
                row["indexname"]
                for row in run(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'control'"
                ).fetchall()
            ),
            "constraints": sorted(
                row["conname"]
                for row in run(
                    "SELECT conname FROM pg_constraint c"
                    " JOIN pg_namespace n ON n.oid = c.connamespace"
                    " WHERE n.nspname = 'control'"
                ).fetchall()
            ),
        }

    def test_the_ladder_goes_down_and_back_up_to_the_same_schema(self) -> None:
        """The headline property, and the only one that caught the real bugs.

        Both errors were ownership mistakes -- a rollback dropping something a
        different migration owned. Neither is visible in the down-step alone;
        both are obvious the moment the ladder is rebuilt.
        """
        cs.migrate(self.connection)
        before = self.snapshot()
        # Derived, not written out: a hardcoded list breaks on every migration
        # added, which is churn masquerading as a failing test.
        self.assertEqual(
            sorted(cs.MIGRATIONS, reverse=True),
            cs.rollback(self.connection, 0, accept_data_loss=True),
        )
        self.assertEqual([], cs.applied_versions(self.connection))
        self.assertEqual(sorted(cs.MIGRATIONS), cs.migrate(self.connection))
        self.assertEqual(before, self.snapshot())

    def test_descending_one_step_at_a_time_undoes_exactly_one_version(self) -> None:
        """Each step is a single version, not everything above the target.

        `rollback(n)` undoes every applied version above `n`, so descending has
        to be done a step at a time to exercise the per-step path -- asking for
        `version - 1` from a full ladder undoes the whole top instead, which is
        how this test was wrong first time.
        """
        cs.migrate(self.connection)
        before = self.snapshot()
        for version in sorted(cs.MIGRATIONS, reverse=True):
            with self.subTest(version=version):
                self.assertEqual(
                    [version],
                    cs.rollback(
                        self.connection, version - 1, accept_data_loss=True
                    ),
                )
        self.assertEqual([], cs.applied_versions(self.connection))
        self.assertEqual(sorted(cs.MIGRATIONS), cs.migrate(self.connection))
        self.assertEqual(before, self.snapshot())

    def test_undoing_the_newest_migration_and_recovering(self) -> None:
        """The operator scenario this exists for: one bad migration, undone.

        A different path from a full teardown, and the one that will actually
        be used -- a migration goes wrong and has to come back out without
        taking the rest of the schema with it.
        """
        cs.migrate(self.connection)
        before = self.snapshot()
        top = max(cs.MIGRATIONS)
        self.assertEqual(
            [top], cs.rollback(self.connection, top - 1, accept_data_loss=True)
        )
        self.assertEqual(
            [version for version in sorted(cs.MIGRATIONS) if version < top],
            cs.applied_versions(self.connection),
        )
        self.assertEqual([top], cs.migrate(self.connection))
        self.assertEqual(before, self.snapshot())

    # -- the two ownership bugs the round trip found ---------------------

    def test_rolling_back_the_binding_keeps_single_use(self) -> None:
        """`single_use` belongs to migration 2, not 3.

        An earlier draft dropped it here, which left the schema unable to
        re-apply migration 3 at all -- its CHECK references that column.
        """
        cs.migrate(self.connection)
        cs.rollback(self.connection, 2, accept_data_loss=True)
        columns = {
            row["column_name"]
            for row in self.connection.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = 'control' AND table_name = 'oauth_tokens'"
            ).fetchall()
        }
        self.assertIn("single_use", columns)
        self.assertFalse(
            columns
            & {"operation_id", "request_digest", "actor_client_id", "broker_client_id"}
        )

    def test_rolling_back_the_grant_drops_its_table(self) -> None:
        """oauth_grants belongs to migration 4.

        An earlier draft assumed migration 2 owned it and dropped only the two
        indexes, so re-applying 4 failed on a duplicate table.
        """
        cs.migrate(self.connection)
        cs.rollback(self.connection, 3, accept_data_loss=True)
        tables = {
            row["table_name"]
            for row in self.connection.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'control'"
            ).fetchall()
        }
        self.assertNotIn("oauth_grants", tables)
        self.assertIn("oauth_tokens", tables, "migration 2's tables must survive")

    # -- refusals --------------------------------------------------------

    def test_a_destructive_rollback_is_refused_and_names_the_loss(self) -> None:
        """An operator reaching for this under pressure learns the cost first.

        Checked one step at a time. The ladder descends newest-first, so a
        multi-step request is refused by whichever step it reaches first --
        which would leave the deeper warnings untested.
        """
        cs.migrate(self.connection)
        for version in sorted(cs.DESTRUCTIVE_ROLLBACKS, reverse=True):
            with self.subTest(version=version):
                with self.assertRaises(cs.IrreversibleMigration) as caught:
                    cs.rollback(self.connection, version - 1)
                message = str(caught.exception)
                self.assertIn(f"migration {version}", message)
                self.assertIn(cs.DESTRUCTIVE_ROLLBACKS[version], message)
                # Nothing moved, so the next subtest starts where this did.
                self.assertIn(version, cs.applied_versions(self.connection))
            cs.rollback(self.connection, version - 1, accept_data_loss=True)

    def test_a_step_not_declared_destructive_needs_no_confirmation(self) -> None:
        """The mechanism, exercised rather than assumed.

        Every migration in the ladder today carries state, so every rollback is
        declared destructive and the unconfirmed path has no live example. That
        makes it exactly the branch worth driving deliberately: a future
        structural-only migration must not demand a confirmation an operator
        then learns to supply reflexively.
        """
        cs.migrate(self.connection)
        original = dict(cs.DESTRUCTIVE_ROLLBACKS)
        top = max(cs.MIGRATIONS)
        cs.DESTRUCTIVE_ROLLBACKS.pop(top, None)
        try:
            self.assertEqual([top], cs.rollback(self.connection, top - 1))
        finally:
            cs.DESTRUCTIVE_ROLLBACKS.clear()
            cs.DESTRUCTIVE_ROLLBACKS.update(original)
        self.assertEqual([top], cs.migrate(self.connection))

    def test_a_rollback_to_the_current_version_does_nothing(self) -> None:
        cs.migrate(self.connection)
        self.assertEqual([], cs.rollback(self.connection, max(cs.MIGRATIONS)))
        self.assertEqual([], cs.rollback(self.connection, max(cs.MIGRATIONS) + 5))
        self.assertEqual(sorted(cs.MIGRATIONS), cs.applied_versions(self.connection))

    def test_a_migration_with_no_rollback_refuses_rather_than_skipping(self) -> None:
        """The drift case, driven rather than assumed.

        test_every_migration_has_a_rollback catches a missing entry in the
        registry. This catches what happens at runtime if one is missing
        anyway: the ladder must refuse to step past it, because silently
        skipping would leave the schema holding that migration's objects while
        the ledger claims it was undone -- and the next migrate would not
        re-apply it.

        Exercised by removing an entry, since every migration has one today,
        which is what let this branch survive a mutation.
        """
        cs.migrate(self.connection)
        top = max(cs.MIGRATIONS)
        original = cs.ROLLBACKS.pop(top)
        try:
            with self.assertRaises(cs.IrreversibleMigration) as caught:
                cs.rollback(self.connection, 0, accept_data_loss=True)
            self.assertIn(f"Migration {top} has no rollback", str(caught.exception))
        finally:
            cs.ROLLBACKS[top] = original
        # And nothing moved: the refusal happens inside the transaction.
        self.assertEqual(sorted(cs.MIGRATIONS), cs.applied_versions(self.connection))

    def test_a_negative_target_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            cs.rollback(self.connection, -1)

    def test_a_failed_rollback_leaves_the_ledger_agreeing_with_the_schema(self) -> None:
        """One transaction, so a mid-ladder failure is not a half-rolled schema.

        Simulated by a rollback that raises: the ledger must be untouched, not
        partially decremented, or the next migrate would try to re-apply
        something still present.
        """
        cs.migrate(self.connection)
        original = cs.ROLLBACKS[3]

        def explode(connection):
            raise RuntimeError("simulated failure part-way down")

        cs.ROLLBACKS[3] = explode
        try:
            with self.assertRaises(RuntimeError):
                cs.rollback(self.connection, 1, accept_data_loss=True)
        finally:
            cs.ROLLBACKS[3] = original
        self.assertEqual(sorted(cs.MIGRATIONS), cs.applied_versions(self.connection))
        self.assertIn("oauth_grants", self.snapshot()["tables"])

    def test_concurrent_rollbacks_serialise(self) -> None:
        """Same advisory lock as migrate, so one waits rather than racing a DROP."""
        cs.migrate(self.connection)
        top = max(cs.MIGRATIONS)
        expected = [top]
        outcomes: list = []

        def step():
            connection = cs.connect(DATABASE_URL)
            try:
                outcomes.append(
                    cs.rollback(connection, top - 1, accept_data_loss=True)
                )
            except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
                outcomes.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=step) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertTrue(
            all(isinstance(item, list) for item in outcomes), outcomes
        )
        # Exactly one thread did the work; the rest found it already done.
        self.assertEqual(1, sum(1 for item in outcomes if item == expected))
        self.assertEqual(3, sum(1 for item in outcomes if item == []))
