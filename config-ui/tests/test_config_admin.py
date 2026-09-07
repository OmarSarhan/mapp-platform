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

    def test_every_command_is_reachable_from_the_parser(self):
        """A command in the choices list with no branch would exit silently."""
        for command in (
            "init",
            "reset-password",
            "reset-demo",
            "revoke-tokens",
            "mcp-client-register",
            "mcp-client-list",
            "mcp-client-disable",
            "migrate-rollback",
        ):
            with self.subTest(command=command):
                args = config_admin.parser().parse_args([command])
                self.assertEqual(command, args.command)


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

        handled, output = self.run_command("--to", "2", "--confirm")
        self.assertTrue(handled)
        self.assertIn("[4, 3]", output)
        self.assertIn("forward ladder", output)
        self.assertEqual([1, 2], self.store.rollback_plan(0)["applied"])
        # And the forward ladder is how you come back up.
        with self.store._db() as connection:
            self.assertEqual([3, 4], cs.migrate(connection))

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


if __name__ == "__main__":
    unittest.main()
