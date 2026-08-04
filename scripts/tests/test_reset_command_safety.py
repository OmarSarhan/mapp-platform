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
            "recover-reset-data --confirm",
            self.script,
        )
        self.assertIn(
            "recover_interrupted_reset_semantics("
            "force=True, wait_for_ready=True)",
            self.script,
        )

    def test_full_bundled_etl_includes_census(self) -> None:
        self.assertIn("run_all_etl()", self.script)
        self.assertIn("run_sample_etl", self.script)
        self.assertIn("run_census_etl", self.script)
        self.assertIn("including Census", self.script)
        self.assertNotIn("reload only configured ETL data", self.script)


if __name__ == "__main__":
    unittest.main()
