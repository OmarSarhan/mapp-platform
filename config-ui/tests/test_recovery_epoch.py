"""The recovery epoch: what stops a restore handing back revoked credentials.

A database snapshot contains every credential that was valid when it was taken,
including ones revoked since, so restoring it reinstates them. The columns for
this have existed since migration 1 and nothing ever wrote or read them, so the
hole was open and documented as open.

Phase 1 requires the opposite: "a restore advances the recovery epoch and
cannot make a pre-restore grant/A mapping, refresh family or B usable".

The design worth understanding is where the check happens. An earlier analysis
costed it as a read-time predicate on every credential read -- 46 statements
across two components -- and concluded it was not worth doing. That was the
wrong design: every read already filters on a revocation, so the epoch only has
to be applied once, at restore, by revoking what predates it. These tests are
mostly about that once.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import control_schema as cs
from control_plane import ControlStore

from control_fixture import ControlStoreTestCase


class RecoveryEpochTestCase(ControlStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ControlStore(Path(directory.name) / "control")
        self.store.initialize("correct horse battery staple", "instance")
        self.client_id = self.store.register_oauth_client(
            name="Agent",
            redirect_uris=["https://agent.example/cb"],
            scopes=["apply"],
        )

    def seed_credentials(self, suffix: str = "") -> None:
        """One of each shape the sweep has to handle."""
        with self.store._db() as connection:
            connection.execute(
                "INSERT INTO control.oauth_grants(grant_id, client_id, subject,"
                " scopes) VALUES(%s, %s, 'operator', '{apply}')",
                (f"g{suffix}", self.client_id),
            )
            connection.execute(
                "INSERT INTO control.oauth_tokens(token_hash, client_id, subject,"
                " scope, audience, issued_at, expires_at)"
                " VALUES(%s, %s, %s, 'apply', 'aud', now(), now() + interval '15 min')",
                (f"h{suffix}", self.client_id, f"g{suffix}"),
            )
            connection.execute(
                "INSERT INTO control.oauth_sessions(session_hash, subject,"
                " auth_time, expires_at)"
                " VALUES(%s, 'operator', now(), now() + interval '30 min')",
                (f"s{suffix}",),
            )
            connection.execute(
                "INSERT INTO control.oauth_pending_authorizations(request_id,"
                " query, client_id, redirect_uri, scopes, csrf, source, expires_at)"
                " VALUES(%s, 'q', %s, 'https://agent.example/cb', '{apply}', 'c',"
                " '198.51.100.1', now() + interval '5 min')",
                (f"p{suffix}", self.client_id),
            )

    def count(self, table: str) -> int:
        with self.store._db() as connection:
            return connection.execute(
                f"SELECT count(*) AS n FROM control.{table}"
            ).fetchone()["n"]

    def revoked(self, table: str, column: str, value: str) -> bool:
        with self.store._db() as connection:
            row = connection.execute(
                f"SELECT revoked_at FROM control.{table} WHERE {column} = %s",
                (value,),
            ).fetchone()
            return row is not None and row["revoked_at"] is not None


class AdvanceTests(RecoveryEpochTestCase):
    def test_the_epoch_starts_at_zero(self) -> None:
        self.assertEqual(0, self.store.recovery_epoch())

    def test_an_advance_bumps_the_epoch_by_one(self) -> None:
        self.assertEqual(1, self.store.advance_recovery_epoch()["epoch"])
        self.assertEqual(1, self.store.recovery_epoch())
        self.assertEqual(2, self.store.advance_recovery_epoch()["epoch"])

    def test_credentials_with_a_revocation_are_revoked_not_removed(self) -> None:
        """The trail survives, so a replay is still distinguishable.

        A deleted row makes a replayed credential look unrecognised; a revoked
        one says when it stopped being valid.
        """
        self.seed_credentials()
        result = self.store.advance_recovery_epoch()
        self.assertEqual(1, result["revoked"]["oauth_grants"])
        self.assertEqual(1, result["revoked"]["oauth_tokens"])
        self.assertTrue(self.revoked("oauth_grants", "grant_id", "g"))
        self.assertTrue(self.revoked("oauth_tokens", "token_hash", "h"))
        self.assertEqual(1, self.count("oauth_grants"))
        self.assertEqual(1, self.count("oauth_tokens"))

    def test_ephemeral_records_are_removed(self) -> None:
        """Sessions, one-shot codes and in-flight authorizations.

        None has a revoked_at to mark and none is worth keeping as evidence.
        """
        self.seed_credentials()
        result = self.store.advance_recovery_epoch()
        self.assertEqual(1, result["deleted"]["oauth_sessions"])
        self.assertEqual(1, result["deleted"]["oauth_pending_authorizations"])
        self.assertEqual(0, self.count("oauth_sessions"))
        self.assertEqual(0, self.count("oauth_pending_authorizations"))

    def test_client_registrations_survive(self) -> None:
        """A registration is not a credential.

        Re-registering every agent after a restore would be hostile, and the
        configuration API's own row is re-asserted from the deployment secret
        on every start anyway.
        """
        self.seed_credentials()
        self.store.advance_recovery_epoch()
        self.assertEqual(1, self.count("oauth_clients"))

    def test_the_administrator_credential_survives(self) -> None:
        """Otherwise a restore would lock the operator out of their own platform."""
        self.store.advance_recovery_epoch()
        self.assertTrue(self.store.instance_id())
        self.assertTrue(self.store.login("correct horse battery staple", "198.51.100.1"))

    def test_each_advance_invalidates_everything_minted_before_it(self) -> None:
        """Including credentials minted after a *previous* advance.

        A restore invalidates what predates that restore, not merely what
        predates the first one ever performed. Worth pinning because the
        obvious wrong reading -- that surviving one advance grants immunity --
        would leave a window open across a second restore.
        """
        self.seed_credentials("1")
        self.store.advance_recovery_epoch()
        self.seed_credentials("2")
        with self.store._db() as connection:
            stamped = connection.execute(
                "SELECT recovery_epoch AS e FROM control.oauth_grants"
                " WHERE grant_id = 'g2'"
            ).fetchone()["e"]
        self.assertEqual(1, stamped, "a new row carries the current epoch")
        self.assertFalse(self.revoked("oauth_grants", "grant_id", "g2"))
        self.store.advance_recovery_epoch()
        self.assertTrue(self.revoked("oauth_grants", "grant_id", "g2"))

    def test_a_row_inserted_after_an_advance_is_stamped_by_the_column_default(
        self,
    ) -> None:
        """The whole reason no application INSERT had to change.

        Without the function-backed default, every insert would keep writing 0
        and the next advance would sweep credentials it had no business
        touching.
        """
        self.store.advance_recovery_epoch()
        self.store.advance_recovery_epoch()
        self.seed_credentials("3")
        with self.store._db() as connection:
            for table, column, value in (
                ("oauth_grants", "grant_id", "g3"),
                ("oauth_tokens", "token_hash", "h3"),
                ("oauth_sessions", "session_hash", "s3"),
            ):
                with self.subTest(table=table):
                    row = connection.execute(
                        f"SELECT recovery_epoch AS e FROM control.{table}"
                        f" WHERE {column} = %s",
                        (value,),
                    ).fetchone()
                    self.assertEqual(2, row["e"])

    def test_a_credential_stamped_above_the_counter_is_still_swept(self) -> None:
        """Restoring a *newer* snapshot over an older database.

        Those rows carry an epoch above this database's counter, and a
        `recovery_epoch < new` filter would leave exactly them live -- the
        credentials from a state this database does not recognise, which are
        the last that should survive. The sweep is therefore "is it live", not
        a comparison against the counter.

        Found by a surviving mutation: swapping the epoch predicate for a
        tautology changed nothing any test could see, because at sweep time
        nothing legitimately carries the new epoch. This is the one case where
        the two readings differ.
        """
        self.seed_credentials()
        with self.store._db() as connection:
            connection.execute(
                "UPDATE control.oauth_grants SET recovery_epoch = 99"
                " WHERE grant_id = 'g'"
            )
            connection.execute(
                "UPDATE control.oauth_sessions SET recovery_epoch = 99"
                " WHERE session_hash = 's'"
            )
        result = self.store.advance_recovery_epoch()
        self.assertEqual(1, result["epoch"], "the counter is still only bumped by one")
        self.assertTrue(self.revoked("oauth_grants", "grant_id", "g"))
        self.assertEqual(0, self.count("oauth_sessions"))

    def test_an_already_revoked_record_is_not_re_stamped(self) -> None:
        """The sweep only touches live rows, so it cannot rewrite a revocation time."""
        self.seed_credentials()
        with self.store._db() as connection:
            connection.execute(
                "UPDATE control.oauth_grants SET revoked_at = '2020-01-01Z'"
                " WHERE grant_id = 'g'"
            )
        result = self.store.advance_recovery_epoch()
        self.assertEqual(0, result["revoked"]["oauth_grants"])
        with self.store._db() as connection:
            when = connection.execute(
                "SELECT revoked_at FROM control.oauth_grants WHERE grant_id = 'g'"
            ).fetchone()["revoked_at"]
        self.assertEqual(2020, when.year)

    def test_a_restore_revocation_says_it_was_a_restore(self) -> None:
        """Otherwise it is indistinguishable from an operator or a replay.

        Only some tables carry a revoked_reason -- oauth_grants and
        oauth_refresh_families do, `tokens` and `oauth_tokens` do not -- so the
        sweep asks the catalogue which can record one rather than assuming.
        """
        self.seed_credentials()
        self.store.advance_recovery_epoch(reason="restored from 2026-09-01")
        with self.store._db(migrate=False) as connection:
            reason = connection.execute(
                "SELECT revoked_reason FROM control.oauth_grants"
                " WHERE grant_id = 'g'"
            ).fetchone()["revoked_reason"]
        self.assertIn("recovery-epoch", reason)
        self.assertIn("restored from 2026-09-01", reason)

    def test_a_refresh_family_is_swept(self) -> None:
        """Migration 6's coupling, which nothing exercised.

        The family is in EPOCH_REVOKED_TABLES because it carries the
        authority; its tokens are not, because revoking the family kills them.
        """
        client_id = self.store.register_oauth_client(
            name="Agent",
            redirect_uris=["https://agent.example/cb"],
            scopes=["apply"],
        )
        with self.store._db(migrate=False) as connection:
            connection.execute(
                "INSERT INTO control.oauth_grants(grant_id, client_id, subject,"
                " scopes) VALUES('gr', %s, 'operator', '{apply}')",
                (client_id,),
            )
            connection.execute(
                "INSERT INTO control.oauth_refresh_families(family_id, grant_id,"
                " client_id, scope, absolute_expires_at)"
                " VALUES('fam','gr',%s,'apply', now() + interval '30 days')",
                (client_id,),
            )
        result = self.store.advance_recovery_epoch(reason="restore")
        self.assertEqual(1, result["revoked"]["oauth_refresh_families"])
        with self.store._db(migrate=False) as connection:
            row = connection.execute(
                "SELECT revoked_at, revoked_reason FROM"
                " control.oauth_refresh_families WHERE family_id = 'fam'"
            ).fetchone()
        self.assertIsNotNone(row["revoked_at"])
        self.assertIn("recovery-epoch", row["revoked_reason"])

    def test_the_advance_is_audited(self) -> None:
        self.seed_credentials()
        self.store.advance_recovery_epoch(reason="restored from 2026-09-01")
        events = [
            entry
            for entry in self.store.audit_tail(50)
            if entry.get("event") == "control.recovery_epoch_advanced"
        ]
        self.assertEqual(1, len(events))
        details = events[0]["details"]
        self.assertEqual(1, details["epoch"])
        self.assertEqual("restored from 2026-09-01", details["reason"])
        self.assertEqual(1, details["revoked"]["oauth_grants"])


class MigrationTests(RecoveryEpochTestCase):
    """The mechanism's storage, checked through the ladder rather than in place.

    The suite fixture truncates tables but does not re-run migrations, so a
    change to migration 5 has no visible effect on an already-migrated
    database -- which let a mutation reverting the column default to a literal
    zero survive every test here. Rolling the ladder down and back up is what
    exercises the migration itself.
    """

    def defaults(self) -> dict:
        with self.store._db() as connection:
            return {
                row["table_name"]: row["column_default"]
                for row in connection.execute(
                    "SELECT table_name, column_default"
                    "  FROM information_schema.columns"
                    " WHERE table_schema = 'control'"
                    "   AND column_name = 'recovery_epoch'"
                ).fetchall()
            }

    def test_every_epoch_bearing_table_defaults_to_the_function(self) -> None:
        expected = set(cs.EPOCH_REVOKED_TABLES) | set(cs.EPOCH_DELETED_TABLES)
        defaults = self.defaults()
        self.assertEqual(expected, set(defaults))
        for table, default in defaults.items():
            with self.subTest(table=table):
                self.assertIn("current_recovery_epoch", default or "")

    def test_the_defaults_survive_a_rollback_and_re_apply(self) -> None:
        """Which is what makes the migration itself, not just the schema, tested."""
        before = self.defaults()
        self.store.rollback_schema(4, accept_data_loss=True)
        reverted = self.defaults()
        for table, default in reverted.items():
            with self.subTest(table=table, stage="rolled back"):
                self.assertNotIn("current_recovery_epoch", default or "")
        # Derived: rolling back to 4 undoes every migration above it, so the
        # re-apply returns all of them. Hardcoding [5] was the same drift that
        # the ladder tests were already written to avoid, reintroduced here.
        undone = sorted(version for version in cs.MIGRATIONS if version > 4)
        with self.store._db() as connection:
            self.assertEqual(undone, cs.migrate(connection))
        self.assertEqual(before, self.defaults())

    def test_the_function_is_stable_not_immutable(self) -> None:
        """IMMUTABLE would let the planner fold a stale epoch into a cached plan."""
        with self.store._db() as connection:
            volatility = connection.execute(
                "SELECT p.provolatile FROM pg_proc p"
                "  JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'control'"
                "   AND p.proname = 'current_recovery_epoch'"
            ).fetchone()["provolatile"]
        self.assertEqual("s", volatility)

    def test_the_function_reports_zero_when_the_row_is_absent(self) -> None:
        """A lost metadata row must not break the read path."""
        with self.store._db() as connection:
            connection.execute(
                "DELETE FROM control.metadata WHERE key = 'recovery_epoch'"
            )
        self.assertEqual(0, self.store.recovery_epoch())
        # And an advance still works: the counter is an upsert.
        self.assertEqual(1, self.store.advance_recovery_epoch()["epoch"])


class AtomicityTests(RecoveryEpochTestCase):
    def test_concurrent_advances_serialise(self) -> None:
        """The counter upsert's row lock is what serialises, and it is enough.

        There was an advisory lock here too. Removing it changed nothing any
        test could observe, and measurement showed why: the upsert takes a row
        lock held until commit, so the whole advance is already serialised.
        Two locks invite a reader to think the redundant one is doing the work.
        """
        self.seed_credentials()
        outcomes: list = []

        def advance():
            directory = tempfile.TemporaryDirectory()
            try:
                store = ControlStore(Path(directory.name) / "control")
                outcomes.append(store.advance_recovery_epoch())
            except BaseException as exc:  # noqa: BLE001 - recorded then asserted
                outcomes.append(exc)
            finally:
                directory.cleanup()

        threads = [threading.Thread(target=advance) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertTrue(all(isinstance(item, dict) for item in outcomes), outcomes)
        # Four advances, four distinct epochs, and the counter agrees.
        self.assertEqual({1, 2, 3, 4}, {item["epoch"] for item in outcomes})
        self.assertEqual(4, self.store.recovery_epoch())
        # Exactly one of them revoked the seeded grant.
        self.assertEqual(
            1, sum(item["revoked"]["oauth_grants"] for item in outcomes)
        )

    def test_the_epoch_does_not_move_when_the_sweep_fails(self) -> None:
        """One transaction: a failure part-way must not leave a bumped counter.

        A bumped epoch with an unswept table is the worst outcome available --
        the credentials survive and the next advance will not reach them,
        because they now compare as current.
        """
        self.seed_credentials()
        original = cs.EPOCH_DELETED_TABLES
        cs.EPOCH_DELETED_TABLES = original + ("no_such_table",)
        try:
            with self.assertRaises(Exception):
                self.store.advance_recovery_epoch()
        finally:
            cs.EPOCH_DELETED_TABLES = original
        self.assertEqual(0, self.store.recovery_epoch())
        self.assertFalse(self.revoked("oauth_grants", "grant_id", "g"))

    def test_an_advance_before_migration_five_is_refused(self) -> None:
        """The guard, pinned by its message rather than by any exception.

        Asserting that "some exception escapes" did not pin this: deleting the
        whole guard left the test passing, because the sweep then died on a
        dropped table instead. A mutation showed that, so the assertion is now
        on the guard's own words and on the counter not having moved.
        """
        before = self.store.recovery_epoch()
        self.store.rollback_schema(4, accept_data_loss=True)
        with self.assertRaises(RuntimeError) as caught:
            self.store.advance_recovery_epoch()
        message = str(caught.exception)
        self.assertIn("migration 5", message)
        self.assertIn("pre-restore credentials", message)
        # And nothing moved: the refusal precedes the counter.
        with self.store._db(migrate=False) as connection:
            row = connection.execute(
                "SELECT value FROM control.metadata WHERE key = 'recovery_epoch'"
            ).fetchone()
        self.assertIsNone(row, "migration 5's rollback removes the counter")
        self.assertEqual(before, 0)


if __name__ == "__main__":
    unittest.main()
