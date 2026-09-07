import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import config_admin

sys.path.insert(0, str(Path(__file__).resolve().parent))

from control_fixture import ControlStoreTestCase  # noqa: E402


class ConfigAdminTests(unittest.TestCase):
    def test_default_control_root_matches_container_mount(self):
        with patch.dict(os.environ, {"CONTROL_DIR": "/control"}):
            args = config_admin.parser().parse_args(["revoke-tokens"])
        self.assertEqual("/control", args.root)

    def test_an_unhandled_command_refuses_instead_of_resetting_the_password(self):
        """The catch-all used to mean "rotate the administrator credential".

        Every handled command returns before the tail of main(), so a command
        in the parser's choices with no dispatch branch fell through to
        reset_password -- silently destroying the credential, printing a new
        one and exiting 0. Driven with a command the parser accepts and nothing
        handles.

        The previous test here only asserted that argparse echoed back a name
        from its own choices list, which is true by construction and could not
        detect this.
        """
        import unittest.mock as mock

        args = config_admin.parser().parse_args(["revoke-tokens"])
        args.command = "not-dispatched"
        store = mock.MagicMock()
        with self.assertRaises(SystemExit) as caught:
            config_admin.main.__wrapped__(args, store) if hasattr(
                config_admin.main, "__wrapped__"
            ) else self._run_main_tail(args, store)
        self.assertIn("no handler", str(caught.exception))
        store.reset_password.assert_not_called()
        store.initialize.assert_not_called()

    def _run_main_tail(self, args, store):
        """main() reads its arguments from sys.argv, so drive it that way."""
        import unittest.mock as mock

        with mock.patch.object(config_admin, "ControlStore", return_value=store), \
             mock.patch.object(config_admin.sys, "argv",
                               ["config_admin.py", "revoke-tokens"]), \
             mock.patch.object(config_admin, "parser") as parser:
            parser.return_value.parse_args.return_value = args
            config_admin.main()

    def test_every_choice_has_a_dispatch_branch(self):
        """Drives each command far enough to prove it is handled somewhere.

        The guard above turns a missing branch into a refusal; this is what
        notices one exists at all.
        """
        import unittest.mock as mock

        handled = []
        for command in config_admin.parser()._actions[1].choices:
            args = config_admin.parser().parse_args([command])
            store = mock.MagicMock()
            for handler in (
                config_admin.advance_recovery_epoch_command,
                config_admin.migrate_rollback_command,
                config_admin.mcp_client_command,
            ):
                try:
                    if handler(args, store):
                        handled.append(command)
                        break
                except SystemExit:
                    handled.append(command)
                    break
            else:
                if command in ("init", "reset-password", "reset-demo",
                               "revoke-tokens"):
                    handled.append(command)
        self.assertEqual(
            sorted(config_admin.parser()._actions[1].choices), sorted(set(handled))
        )


class MigrateRollbackCommandTests(ControlStoreTestCase):
    """The operator entry point for the schema ladder.

    Destructive, so the shape matters as much as the effect: the plan is
    printed and nothing changes unless --confirm is given, and the plan is
    computed from the migration ledger rather than written out -- a hardcoded
    warning goes stale the first time a migration is added, and this one is
    read under pressure.
    """

    def setUp(self) -> None:
        super().setUp()
        import control_plane

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = control_plane.ControlStore(Path(directory.name) / "control")
        self.store.initialize("correct horse battery staple", "instance")

    def run_command(self, *argv):
        args = config_admin.parser().parse_args(["migrate-rollback", *argv])
        captured = io.StringIO()
        with redirect_stdout(captured):
            handled = config_admin.migrate_rollback_command(args, self.store)
        return handled, captured.getvalue()

    def test_the_plan_only_run_does_not_migrate_the_schema(self) -> None:
        """Its whole contract is "prints the plan and changes nothing".

        It was applying the entire forward ladder before computing the plan,
        because ControlStore._db migrates on first use: a schema at version 4
        came back at 6, and the command then exited 2 saying nothing had
        happened. Measured through a *separate* connection, because measuring
        through the store's own would be blind to exactly this.
        """
        import control_schema as cs

        with self.store._db(migrate=False) as connection:
            cs.rollback(connection, 4, accept_data_loss=True)
        with self.store._db(migrate=False) as connection:
            self.assertEqual([1, 2, 3, 4], cs.applied_versions(connection))
        with self.assertRaises(SystemExit):
            self.run_command("--to", "2")
        with self.store._db(migrate=False) as connection:
            self.assertEqual([1, 2, 3, 4], cs.applied_versions(connection))

    def test_a_negative_target_is_refused_with_the_plan(self) -> None:
        """The plan and the action have to agree.

        A negative target used to print a full destructive plan and exit 2 --
        the "read the price, then re-run with --confirm" contract -- and the
        confirmed run then died on an unhandled ValueError.
        """
        with self.assertRaises(SystemExit) as caught:
            self.run_command("--to", "-1")
        self.assertIn("zero or a migration version", str(caught.exception))
        with self.assertRaises(SystemExit):
            self.run_command("--to", "-1", "--confirm")

    def test_it_requires_a_target(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.run_command()
        self.assertIn("--to is required", str(caught.exception))

    def test_without_confirm_it_prints_the_plan_and_changes_nothing(self) -> None:
        import control_schema as cs

        before = self.store.rollback_plan(0)["applied"]
        with self.assertRaises(SystemExit) as caught:
            self.run_command("--to", "2")
        self.assertEqual(2, caught.exception.code)
        self.assertEqual(before, self.store.rollback_plan(0)["applied"])
        self.assertEqual(sorted(cs.MIGRATIONS), before)

    def test_the_plan_names_every_version_and_its_loss(self) -> None:
        """Computed from the ledger, so it cannot describe the wrong database."""
        import control_schema as cs

        with self.assertRaises(SystemExit):
            self.run_command("--to", "1")
        # run_command captures stdout even when the command exits, so the plan
        # is recoverable from the same call rather than needing a second one.
        args = config_admin.parser().parse_args(["migrate-rollback", "--to", "1"])
        captured = io.StringIO()
        with redirect_stdout(captured), self.assertRaises(SystemExit):
            config_admin.migrate_rollback_command(args, self.store)
        output = captured.getvalue()
        for version in (4, 3, 2):
            with self.subTest(version=version):
                self.assertIn(f"migration {version}", output)
                self.assertIn(cs.DESTRUCTIVE_ROLLBACKS[version], output)
        self.assertIn("Not touched", output)
        self.assertIn("--confirm", output)

    def test_a_target_at_or_above_the_current_version_is_a_no_op(self) -> None:
        handled, output = self.run_command("--to", "99")
        self.assertTrue(handled)
        self.assertIn("Nothing to do", output)

    def test_with_confirm_it_rolls_back_and_says_how_to_recover(self) -> None:
        import control_schema as cs

        undone = sorted((v for v in cs.MIGRATIONS if v > 2), reverse=True)
        handled, output = self.run_command("--to", "2", "--confirm")
        self.assertTrue(handled)
        self.assertIn(str(undone), output)
        self.assertIn("forward ladder", output)
        self.assertEqual([1, 2], self.store.rollback_plan(0)["applied"])
        # And the forward ladder is how you come back up.
        with self.store._db() as connection:
            self.assertEqual(sorted(undone), cs.migrate(connection))

    def test_a_rollback_short_of_migration_one_keeps_the_credential(self) -> None:
        """Migration 1 owns admin_credential, so stopping above it is safe.

        Worth pinning because it is the difference between an operator undoing
        a bad migration and an operator locking themselves out.
        """
        self.run_command("--to", "2", "--confirm")
        self.assertTrue(self.store.instance_id())

    def test_it_declines_a_command_that_is_not_its_own(self) -> None:
        args = config_admin.parser().parse_args(["revoke-tokens"])
        self.assertFalse(config_admin.migrate_rollback_command(args, self.store))


class AdvanceRecoveryEpochCommandTests(ControlStoreTestCase):
    """The operator entry point for the restore-time invalidation.

    Destructive in the ordinary way rather than the alarming way: nothing is
    lost that cannot be obtained again by signing in and consenting. It is
    confirmed anyway, because it is equally usable as a panic button on a live
    platform, where the effect is identical and the intent is not.
    """

    def setUp(self) -> None:
        super().setUp()
        import control_plane

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = control_plane.ControlStore(Path(directory.name) / "control")
        self.store.initialize("correct horse battery staple", "instance")

    def run_command(self, *argv):
        args = config_admin.parser().parse_args(
            ["advance-recovery-epoch", *argv]
        )
        captured = io.StringIO()
        with redirect_stdout(captured):
            handled = config_admin.advance_recovery_epoch_command(args, self.store)
        return handled, captured.getvalue()

    def seed(self) -> str:
        client_id = self.store.register_oauth_client(
            name="Agent",
            redirect_uris=["https://agent.example/cb"],
            scopes=["apply"],
        )
        with self.store._db() as connection:
            connection.execute(
                "INSERT INTO control.oauth_grants(grant_id, client_id, subject,"
                " scopes) VALUES('g', %s, 'operator', '{apply}')",
                (client_id,),
            )
        return client_id

    def test_without_confirm_it_names_what_it_would_invalidate(self) -> None:
        self.seed()
        with self.assertRaises(SystemExit) as caught:
            self.run_command()
        self.assertEqual(2, caught.exception.code)
        self.assertEqual(0, self.store.recovery_epoch())

    def test_the_plan_says_what_survives_as_well_as_what_does_not(self) -> None:
        """An operator deciding whether to run this needs the boundary too."""
        captured = io.StringIO()
        args = config_admin.parser().parse_args(["advance-recovery-epoch"])
        with redirect_stdout(captured), self.assertRaises(SystemExit):
            config_admin.advance_recovery_epoch_command(args, self.store)
        output = captured.getvalue()
        for phrase in ("dashboard sessions", "CLI API tokens", "MCP grants"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, output)
        self.assertIn("Not touched", output)
        self.assertIn("administrator credential", output)
        self.assertIn("before serving traffic", output)

    def test_with_confirm_it_advances_and_reports_the_counts(self) -> None:
        self.seed()
        handled, output = self.run_command("--confirm")
        self.assertTrue(handled)
        self.assertIn("Recovery epoch is now 1", output)
        self.assertIn("oauth_grants", output)
        self.assertEqual(1, self.store.recovery_epoch())

    def test_it_says_so_when_nothing_was_live(self) -> None:
        """A restore of an idle snapshot is a normal case, not a silent no-op."""
        handled, output = self.run_command("--confirm")
        self.assertTrue(handled)
        self.assertIn("nothing was live", output)

    def test_the_reason_reaches_the_audit_entry(self) -> None:
        self.seed()
        self.run_command("--confirm", "--reason", "restored 2026-09-01")
        events = [
            entry
            for entry in self.store.audit_tail(50)
            if entry.get("event") == "control.recovery_epoch_advanced"
        ]
        self.assertEqual(
            "restored 2026-09-01", events[0]["details"]["reason"]
        )

    def test_it_declines_a_command_that_is_not_its_own(self) -> None:
        args = config_admin.parser().parse_args(["revoke-tokens"])
        self.assertFalse(
            config_admin.advance_recovery_epoch_command(args, self.store)
        )

    def test_a_schema_without_the_mechanism_refuses_with_guidance(self) -> None:
        """The refusal docs/backup-restore.md documents, now reachable.

        It could not fire before: the store migrated to head on first use, so
        the precondition checked for a migration it had just applied. And it
        arrived as a traceback rather than as something an operator could act
        on.
        """
        import control_schema as cs

        with self.store._db(migrate=False) as connection:
            cs.rollback(connection, 4, accept_data_loss=True)
        with self.assertRaises(SystemExit) as caught:
            self.run_command("--confirm")
        self.assertIn("migration 5", str(caught.exception))
        self.assertIn("pre-restore credentials", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
