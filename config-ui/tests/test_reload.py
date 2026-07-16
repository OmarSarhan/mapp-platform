import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from control_api import (
    proposal_create,
    proposal_read,
    proposal_write,
    workspace_hash,
)
from control_plane import ControlStore


class ReloadTests(unittest.TestCase):
    def test_effective_locale_paths_keep_default_at_locale(self):
        paths = [
            path
            for path, _ in app.locale_items({
                "locale": {"layers": {}},
                "locales": {"cy": {"name": "Cymraeg"}},
            })
        ]
        self.assertEqual(["locale", "locales.cy"], paths)

    def test_save_and_reload_uses_exact_saved_byte_fingerprint(self):
        encoded = b'{"value":1.0,"negativeZero":-0.0,"small":1e-7}\n'
        expected = hashlib.sha256(encoded).hexdigest()
        with (
            patch("app.save_workspace", return_value=(encoded, "next-revision")),
            patch(
                "app.request_reload",
                return_value={"requestedGeneration": 7},
            ) as request_reload,
            patch(
                "app.wait_reload",
                return_value={"completed": True},
            ) as wait_reload,
        ):
            _, revision, fingerprint, reload_result = app.save_and_reload(
                {"value": 1.0},
                "current-revision",
            )
        self.assertEqual("next-revision", revision)
        self.assertEqual(expected, fingerprint)
        request_reload.assert_called_once_with(expected)
        wait_reload.assert_called_once_with(7, expected, 30)
        self.assertTrue(app.reload_completed(reload_result))

    def test_reload_coordination_failure_is_reported_after_save(self):
        encoded = b'{"value":1.0}\n'
        with (
            patch("app.save_workspace", return_value=(encoded, "next-revision")),
            patch("app.request_reload", side_effect=OSError("synthetic")),
        ):
            _, revision, _, reload_result = app.save_and_reload(
                {"value": 1.0},
                "current-revision",
            )
        self.assertEqual("next-revision", revision)
        self.assertFalse(app.reload_completed(reload_result))
        self.assertIn("error", reload_result)

    def test_workspace_io_rejects_nonstandard_json_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace.json"
            workspace.write_text('{"key":"demo","plugin":{"value":NaN}}\n')
            with patch("app.WORKSPACE", workspace):
                with self.assertRaises(ValueError):
                    app.read_workspace()

            workspace.write_text('{"key":"demo"}\n')
            with patch("app.WORKSPACE", workspace):
                raw, _ = app.read_workspace()
                with self.assertRaises(ValueError):
                    app.save_workspace(
                        {"key": "demo", "plugin": {"value": float("nan")}},
                        app.revision(raw),
                    )

    def _proposal_fixture(self, directory):
        workspace = Path(directory) / "workspace.json"
        workspace.write_text('{"key":"demo","locale":{"layers":{}}}\n')
        store = ControlStore(Path(directory) / "control")
        store.initialize("correct horse battery staple", "instance")
        with patch("app.WORKSPACE", workspace):
            raw, original = app.read_workspace()
            candidate = {
                **original,
                "title": "Approved candidate",
            }
            proposal = proposal_create(
                store,
                original,
                app.revision(raw),
                candidate,
                [{"op": "set", "path": "/title", "value": "Approved candidate"}],
                [{
                    "op": "add",
                    "path": "/title",
                    "old": None,
                    "value": "Approved candidate",
                }],
                "token:test",
            )
        return workspace, store, proposal

    def test_proposal_is_recorded_applied_before_reload_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, store, proposal = self._proposal_fixture(directory)

            def observe_applied(*_args):
                stored = proposal_read(store, proposal["id"])
                self.assertEqual("applied", stored["status"])
                return {"completed": True}

            with (
                patch("app.WORKSPACE", workspace),
                patch(
                    "app.request_reload",
                    return_value={"requestedGeneration": 4},
                ),
                patch("app.wait_reload", side_effect=observe_applied),
            ):
                applied, reload_result = app.apply_proposal_and_reload(
                    store,
                    proposal,
                    actor="token:test",
                )

            self.assertEqual("applied", applied["status"])
            self.assertTrue(app.reload_completed(reload_result))
            _, saved = (
                workspace.read_bytes(),
                app.strict_json_loads(workspace.read_bytes()),
            )
            self.assertEqual("Approved candidate", saved["title"])

    def test_interrupted_proposal_recovers_an_exact_committed_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, store, proposal = self._proposal_fixture(directory)
            proposal["status"] = "applying"
            proposal_write(store, proposal)
            encoded = (
                app.json.dumps(
                    proposal["candidate"],
                    indent=2,
                    ensure_ascii=False,
                    allow_nan=False,
                ) + "\n"
            ).encode()
            workspace.write_bytes(encoded)

            with (
                patch("app.WORKSPACE", workspace),
                patch("app.save_workspace") as save_workspace,
                patch(
                    "app.request_reload",
                    return_value={"requestedGeneration": 5},
                ),
                patch(
                    "app.wait_reload",
                    return_value={"completed": True},
                ),
            ):
                applied, _ = app.apply_proposal_and_reload(
                    store,
                    proposal,
                    actor="token:test",
                )

            save_workspace.assert_not_called()
            self.assertTrue(applied["applicationRecovered"])
            self.assertEqual(
                workspace_hash(proposal["candidate"]),
                workspace_hash(app.strict_json_loads(workspace.read_bytes())),
            )

    def test_interrupted_proposal_conflicts_if_workspace_changed_elsewhere(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, store, proposal = self._proposal_fixture(directory)
            proposal["status"] = "applying"
            proposal_write(store, proposal)
            workspace.write_text(
                '{"key":"demo","locale":{"layers":{}},"other":true}\n'
            )

            with patch("app.WORKSPACE", workspace):
                with self.assertRaises(FileExistsError):
                    app.apply_proposal_and_reload(
                        store,
                        proposal,
                        actor="token:test",
                    )

            stored = proposal_read(store, proposal["id"])
            self.assertEqual("conflicted", stored["status"])


if __name__ == "__main__":
    unittest.main()
