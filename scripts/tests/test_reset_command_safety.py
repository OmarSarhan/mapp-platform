from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ResetCommandSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "bin/mapp").read_text(encoding="utf-8")

    def test_signals_have_explicit_nonzero_recovery_statuses(self) -> None:
        self.assertIn(
            "trap 'recover_interrupted_reset 130' INT",
            self.script,
        )
        self.assertIn(
            "trap 'recover_interrupted_reset 143' TERM",
            self.script,
        )
        self.assertIn(
            "trap 'recover_interrupted_reset $?' ERR",
            self.script,
        )
        self.assertNotIn(
            "trap recover_interrupted_reset ERR INT TERM",
            self.script,
        )

    def test_volume_and_compensation_fail_closed(self) -> None:
        self.assertIn(
            'volume_inspection="$(docker volume inspect '
            '"${database_volume}" 2>&1)"',
            self.script,
        )
        self.assertIn('== *"No such volume"*', self.script)
        self.assertIn(
            "Could not verify the bundled database volume",
            self.script,
        )
        self.assertIn(
            "The bundled database volume was already removed; "
            "semantic compensation is no longer safe.",
            self.script,
        )
        self.assertIn(
            "up --detach --no-build --wait db semantic-service config-ui",
            self.script,
        )

    def test_each_reset_uses_owner_checked_compensation(self) -> None:
        self.assertIn(
            "reset_gate_owner=\"$(",
            self.script,
        )
        self.assertIn(
            "MAPP_RESET_GATE_OWNER=${reset_gate_owner}",
            self.script,
        )
        self.assertIn(
            'reset_owner=os.environ["MAPP_RESET_GATE_OWNER"]',
            self.script,
        )
        self.assertIn(
            'archive_derived_semantics_before_reset('
            'os.environ["MAPP_RESET_GATE_OWNER"])',
            self.script,
        )
        self.assertNotIn(
            "recover_interrupted_reset_semantics()",
            self.script,
        )

    def test_host_loss_recovery_is_explicit_and_confirmed(self) -> None:
        self.assertIn(
            "recover-reset-system --confirm",
            self.script,
        )
        self.assertIn(
            "recover_interrupted_reset_semantics("
            "force=True, wait_for_ready=True)",
            self.script,
        )

    def test_schema_rollback_is_destructive_and_never_self_confirms(self) -> None:
        """The wrapper must not decide the confirmation for the operator.

        migrate-rollback drops tables. Its plan is printed by config_admin,
        which reads the migration ledger -- so the warning names the versions
        this database actually has rather than a paragraph that goes stale the
        first time a migration is added. What the wrapper must get right is
        narrower and easier to get wrong: forward the operator's arguments
        verbatim and never inject --confirm.
        """
        dispatch = self.script[self.script.index("\n  migrate-rollback)") :]
        dispatch = dispatch[: dispatch.index("\n    ;;")]
        self.assertIn(
            'python config_admin.py migrate-rollback --root /control "${@:2}"',
            dispatch,
        )
        # Executable lines only. The comment above the dispatch explains that
        # --confirm is never injected, and should not have to avoid naming it.
        commands = "\n".join(
            line
            for line in dispatch.splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn("--confirm", commands)

    def test_schema_rollback_is_advertised_as_destructive(self) -> None:
        """An operator reading usage should not have to run it to find out."""
        usage = self.script[self.script.index("usage() {") :]
        usage = usage[: usage.index("\n}\n")]
        line = next(
            item for item in usage.splitlines() if "migrate-rollback" in item
        )
        self.assertIn("destructive", line)
        self.assertIn("--confirm", line)

    def test_epoch_advance_never_self_confirms_and_never_starts_the_app(
        self,
    ) -> None:
        """Two properties, and the second is the one easy to lose.

        The point of the command is to invalidate restored credentials *before*
        anything can accept one, so it must bring up the database and not the
        application. --no-deps is what makes that true, and injecting --confirm
        would take the decision away from the operator.
        """
        dispatch = self.script[self.script.index("\n  advance-recovery-epoch)") :]
        dispatch = dispatch[: dispatch.index("\n    ;;")]
        commands = "\n".join(
            line
            for line in dispatch.splitlines()
            if not line.strip().startswith("#")
        )
        self.assertIn(
            'python config_admin.py advance-recovery-epoch --root /control'
            ' "${@:2}"',
            commands,
        )
        self.assertIn("run --rm --no-deps config-ui", commands)
        self.assertIn("up --detach --wait db", commands)
        self.assertNotIn("--confirm", commands)

    def test_epoch_advance_is_advertised_as_destructive(self) -> None:
        usage = self.script[self.script.index("usage() {") :]
        usage = usage[: usage.index("\n}\n")]
        line = next(
            item for item in usage.splitlines() if "advance-recovery-epoch" in item
        )
        self.assertIn("destructive", line)
        self.assertIn("--confirm", line)

    def test_the_restore_procedure_invalidates_restored_credentials(self) -> None:
        """The mechanism is only reachable if the procedure says to run it.

        A restore document that recovers credentials without invalidating them
        is how the hole stays open in practice, whatever the code can do.
        """
        document = (ROOT / "docs/backup-restore.md").read_text(encoding="utf-8")
        self.assertIn("./bin/mapp advance-recovery-epoch --confirm", document)
        self.assertIn("including ones revoked since", document)
        # Ordered before the stack starts, or a pre-restore credential is
        # usable in the window between.
        self.assertLess(
            document.index("advance-recovery-epoch"),
            document.index("Initialize or clear stale live and preview reload"),
        )

    def test_buildkit_lease_failures_prune_and_retry_once(self) -> None:
        self.assertIn("is_buildkit_lease_failure()", self.script)
        self.assertIn("run_with_buildkit_lease_retry()", self.script)
        self.assertIn('lease "[^"]+": not found', self.script)
        self.assertIn("docker buildx prune --all --force", self.script)
        self.assertIn(
            "Docker BuildKit reported a missing cache lease",
            self.script,
        )
        self.assertIn(
            'run_with_buildkit_lease_retry "${compose[@]}" '
            'up --detach --build "${runtime_services[@]}"',
            self.script,
        )
        self.assertIn(
            'up --detach --build --force-recreate "${runtime_services[@]}"',
            self.script,
        )
        self.assertIn(
            'run_with_buildkit_lease_retry "${compose[@]}" build --pull xyz',
            self.script,
        )

    def test_runtime_start_repairs_stale_edge_port_bindings(self) -> None:
        self.assertIn("clear_edge_environment_overrides", self.script)
        self.assertIn(
            "EDGE_BIND_ADDRESS HTTP_PORT HTTPS_PORT MAP_SITE CONFIG_SITE MCP_SITE CADDY_EMAIL",
            self.script,
        )
        self.assertIn("up|serve|config-ui|reset-system|all)", self.script)
        self.assertIn("ensure_caddy_bindings()", self.script)
        self.assertIn(
            'config --format json | edge_bindings compose',
            self.script,
        )
        self.assertIn(
            'up --detach --no-deps --force-recreate caddy',
            self.script,
        )
        self.assertIn(
            "Caddy port bindings still differ after recreation",
            self.script,
        )
        self.assertGreaterEqual(self.script.count("ensure_caddy_bindings"), 3)


if __name__ == "__main__":
    unittest.main()
