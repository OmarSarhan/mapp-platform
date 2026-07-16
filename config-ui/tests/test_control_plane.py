import stat
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from control_plane import ControlStore, iso, now


class ControlPlaneTests(unittest.TestCase):
    def test_existing_sensitive_state_is_made_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposals = root / "proposals"
            proposal_dir = proposals / "legacy-proposal"
            proposal_dir.mkdir(parents=True)
            state = root / "auth.json"
            audit = root / "audit.jsonl"
            proposal = proposal_dir / "proposal.json"
            state.write_text("{}")
            audit.write_text("{}\n")
            proposal.write_text("{}")
            state.chmod(0o644)
            audit.chmod(0o644)
            proposal_dir.chmod(0o755)
            proposal.chmod(0o644)

            ControlStore(root)

            self.assertEqual(0o600, stat.S_IMODE(state.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(audit.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(proposal_dir.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(proposal.stat().st_mode))

    def test_login_token_and_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            self.assertTrue(store.initialize("correct horse battery staple", "instance"))
            session, csrf = store.login("correct horse battery staple", "127.0.0.1")
            self.assertTrue(store.session(session, csrf, require_csrf=True))
            raw, record = store.create_token("agent")
            self.assertEqual(record["id"], store.authenticate_token(raw, "127.0.0.1")["id"])
            self.assertTrue(store.revoke_token(record["id"]))
            self.assertIsNone(store.authenticate_token(raw, "127.0.0.1"))
            self.assertEqual(0o700, stat.S_IMODE(Path(directory).stat().st_mode))
            self.assertEqual(
                0o600,
                stat.S_IMODE((Path(directory) / "audit.jsonl").stat().st_mode),
            )
            self.assertEqual(
                0o600,
                stat.S_IMODE((Path(directory) / "auth.json").stat().st_mode),
            )

    def test_password_changes_require_a_nonempty_minimum_length(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            with self.assertRaises(ValueError):
                store.change_password("correct horse battery staple", "")
            self.assertIsNotNone(
                store.login("correct horse battery staple", "127.0.0.1")
            )
            with self.assertRaises(ValueError):
                store.reset_password("short")

    def test_token_expiry_is_validated_and_malformed_legacy_records_are_revoked(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            with self.assertRaises(ValueError):
                store.create_token("invalid", "not-a-date")
            with self.assertRaises(ValueError):
                store.create_token("expired", iso(now() - timedelta(seconds=1)))

            first_raw, first = store.create_token(
                "legacy",
                iso(now() + timedelta(days=1)),
            )
            second_raw, second = store.create_token("valid")
            state = store._state()
            state["tokens"][0]["expires"] = "not-a-date"
            store._write(state)

            self.assertIsNone(
                store.authenticate_token(first_raw, "127.0.0.1")
            )
            self.assertEqual(
                second["id"],
                store.authenticate_token(second_raw, "127.0.0.1")["id"],
            )
            records = {item["id"]: item for item in store.list_tokens()}
            self.assertIsNotNone(records[first["id"]]["revoked"])
