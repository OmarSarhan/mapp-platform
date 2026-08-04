from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.cleanup_temp import cleanup, cleanup_candidates


NOW = dt.datetime(2026, 8, 4, 12, 0, tzinfo=dt.timezone.utc)


class CleanupTempTests(unittest.TestCase):
    def _state(self, root: Path) -> Path:
        state = root / "var"
        (state / "control" / "artifacts").mkdir(parents=True)
        (state / "control" / "proposals" / "proposal-1").mkdir(parents=True)
        (state / "semantic").mkdir()
        return state

    def _artifact(self, state: Path, name: str) -> Path:
        run = state / "control" / "artifacts" / name
        run.mkdir()
        (run / "page.png").write_bytes(b"png")
        (run / "report.json").write_text("{}", encoding="utf-8")
        return run

    def test_dry_run_lists_only_old_disposable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(Path(directory))
            old_run = self._artifact(
                state,
                "2026-07-20T10-00-00-000Z-Bus_Stops-a1b2c3d4",
            )
            recent_run = self._artifact(
                state,
                "2026-08-01T10-00-00-000Z-Bus_Stops-b1c2d3e4",
            )
            unrecognized = self._artifact(state, "manual-evidence")
            important = state / "control" / "proposals" / "proposal-1" / "proposal.json"
            important.write_text("{}", encoding="utf-8")
            stale_temp = state / "semantic" / ".semantic.json.abcdef123456.tmp"
            stale_temp.write_text("temporary", encoding="utf-8")
            old_time = (NOW - dt.timedelta(days=8)).timestamp()
            os.utime(stale_temp, (old_time, old_time))

            with patch("scripts.cleanup_temp._utc_now", return_value=NOW):
                result = cleanup(state, confirm=False)

            self.assertEqual(2, result["candidateCount"])
            self.assertEqual(0, result["removedCount"])
            self.assertTrue(old_run.exists())
            self.assertTrue(stale_temp.exists())
            self.assertTrue(recent_run.exists())
            self.assertTrue(unrecognized.exists())
            self.assertTrue(important.exists())

    def test_confirm_removes_only_old_artifacts_and_atomic_temps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(Path(directory))
            old_run = self._artifact(
                state,
                "2026-07-20T10-00-00-000Z-candidate-layer-a1b2c3d4",
            )
            recent_run = self._artifact(
                state,
                "2026-08-01T10-00-00-000Z-live-layer-b1c2d3e4",
            )
            stale_temp = state / "control" / ".auth.json.abcdef1234567890.tmp"
            stale_temp.write_text("temporary", encoding="utf-8")
            old_time = (NOW - dt.timedelta(days=8)).timestamp()
            os.utime(stale_temp, (old_time, old_time))
            auth = state / "control" / "auth.json"
            auth.write_text("{}", encoding="utf-8")

            with patch("scripts.cleanup_temp._utc_now", return_value=NOW):
                result = cleanup(state, confirm=True)

            self.assertEqual(2, result["removedCount"])
            self.assertFalse(old_run.exists())
            self.assertFalse(stale_temp.exists())
            self.assertTrue(recent_run.exists())
            self.assertTrue(auth.exists())

    def test_symlinked_or_unexpected_artifact_trees_are_preserved(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self._state(root)
            outside = root / "outside"
            outside.mkdir()
            run = self._artifact(
                state,
                "2026-07-20T10-00-00-000Z-Bus_Stops-a1b2c3d4",
            )
            (run / "outside").symlink_to(outside, target_is_directory=True)

            candidates = cleanup_candidates(state, now=NOW)

            self.assertNotIn(run, candidates)
            self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
