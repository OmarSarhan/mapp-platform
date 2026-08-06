import json
import stat
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from control_plane import (
    DEVICE_SCOPES,
    TOKEN_SCOPES,
    ControlStore,
    iso,
    now,
    token_hash,
)


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

    def test_invalid_sessions_do_not_write_but_expiry_pruning_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            session, _csrf = store.login(
                "correct horse battery staple",
                "127.0.0.1",
            )

            with patch.object(store, "_write", wraps=store._write) as write:
                self.assertFalse(store.session("not-a-session"))
                self.assertFalse(
                    store.session(session, "wrong-csrf", require_csrf=True)
                )
                write.assert_not_called()

            state = store._state()
            state["sessions"][0]["lastUsed"] = iso(
                now() - timedelta(seconds=31 * 60)
            )
            store._write(state)

            with patch.object(store, "_write", wraps=store._write) as write:
                self.assertFalse(store.session("not-a-session"))
                write.assert_called_once()
            self.assertEqual([], store._state()["sessions"])

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
            for invalid_expiry in (
                "not-a-date",
                "2026-07-26T12:00:00",
                iso(now() - timedelta(seconds=1)),
                7,
            ):
                with self.subTest(expires=invalid_expiry):
                    with self.assertRaises(ValueError):
                        store.create_token("invalid", invalid_expiry)

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

    def test_semantic_token_scopes_are_closed_canonical_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ControlStore(root)
            store.initialize("correct horse battery staple", "instance")
            expiry = iso(now() + timedelta(days=30))

            raw, record = store.create_token(
                "semantic curator",
                expiry,
                [
                    "semantic:inspect",
                    "semantic:propose",
                    "semantic:inspect",
                    "semantic:apply",
                ],
            )

            self.assertEqual(
                [
                    "semantic:inspect",
                    "semantic:propose",
                    "semantic:apply",
                ],
                record["scopes"],
            )
            self.assertEqual(expiry, record["expires"])
            self.assertNotIn(raw, (root / "auth.json").read_text())
            audit = [
                json.loads(line)
                for line in (root / "audit.jsonl").read_text().splitlines()
            ]
            created = audit[-1]
            self.assertEqual("token.created", created["event"])
            self.assertEqual(record["id"], created["details"]["id"])
            self.assertEqual(record["scopes"], created["details"]["scopes"])
            self.assertEqual(expiry, created["details"]["expires"])
            self.assertNotIn(raw, json.dumps(created))

    def test_every_supported_scope_is_issued_without_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")

            for scope in sorted(TOKEN_SCOPES):
                with self.subTest(credential="token", scope=scope):
                    raw, record = store.create_token(
                        f"single scope {scope}",
                        scopes=[scope],
                    )
                    self.assertEqual([scope], record["scopes"])
                    self.assertEqual(
                        [scope],
                        store.authenticate_token(raw, "127.0.0.1")["scopes"],
                    )

            for index, scope in enumerate(sorted(DEVICE_SCOPES)):
                with self.subTest(credential="device", scope=scope):
                    started = store.start_device_authorization(
                        f"single scope {scope}",
                        [scope],
                        f"127.0.0.{index + 1}",
                    )
                    self.assertEqual([scope], started["scopes"])

    def test_token_scope_validation_never_expands_explicit_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")

            for scopes in (
                [],
                {},
                ["semantic:unknown"],
                ["full", "semantic:inspect"],
                ["semantic:inspect", 7],
            ):
                with self.subTest(scopes=scopes):
                    with self.assertRaises(ValueError):
                        store.create_token("invalid", scopes=scopes)

            _raw, legacy = store.create_token("legacy default", scopes=None)
            self.assertEqual(["full"], legacy["scopes"])
            self.assertEqual(1, len(store.list_tokens()))

    def test_scoped_device_authorization_is_expiring_and_one_time(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            started = store.start_device_authorization(
                "codex",
                ["inspect", "propose", "visual", "semantic:inspect"],
                "127.0.0.1",
            )
            self.assertEqual("pending", store.poll_device_authorization(started["deviceId"])["status"])
            self.assertTrue(store.approve_device_authorization(started["userCode"]))
            approved_state = store._state()
            self.assertEqual([], approved_state["tokens"])
            self.assertEqual(
                "approved",
                approved_state["deviceAuthorizations"][0]["status"],
            )
            self.assertNotIn(
                "mapp_",
                (Path(directory) / "auth.json").read_text(),
            )
            authorized = store.poll_device_authorization(started["deviceId"])
            self.assertEqual("authorized", authorized["status"])
            self.assertEqual(
                ["inspect", "propose", "visual", "semantic:inspect"],
                authorized["record"]["scopes"],
            )
            self.assertIsNotNone(authorized["record"]["expires"])
            self.assertNotIn(
                authorized["token"],
                (Path(directory) / "audit.jsonl").read_text(),
            )
            persisted = (Path(directory) / "auth.json").read_text()
            self.assertNotIn(authorized["token"], persisted)
            self.assertNotIn('"token":', persisted)
            self.assertEqual(
                token_hash(authorized["token"]),
                store._state()["tokens"][0]["hash"],
            )
            self.assertEqual(
                "consumed",
                store.poll_device_authorization(started["deviceId"])["status"],
            )

    def test_legacy_raw_device_credentials_are_purged_and_revoked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ControlStore(root)
            store.initialize("correct horse battery staple", "instance")
            started = store.start_device_authorization(
                "legacy-agent",
                ["inspect"],
                "127.0.0.1",
            )
            raw, record = store.create_token(
                "Device: legacy-agent",
                iso(now() + timedelta(days=30)),
                ["inspect"],
            )
            state = store._state()
            authorization = state["deviceAuthorizations"][0]
            authorization.update({
                "status": "approved",
                "token": raw,
                "tokenRecord": record,
            })
            store._write(state)

            migrated = ControlStore(root)
            migrated_state = migrated._state()
            migrated_authorization = migrated_state["deviceAuthorizations"][0]
            migrated_token = next(
                item
                for item in migrated_state["tokens"]
                if item["id"] == record["id"]
            )

            self.assertNotIn("token", migrated_authorization)
            self.assertNotIn("tokenRecord", migrated_authorization)
            self.assertIsNotNone(
                migrated_authorization["legacyCredentialPurged"],
            )
            self.assertIsNotNone(migrated_token["revoked"])
            self.assertIsNone(
                migrated.authenticate_token(raw, "127.0.0.1"),
            )
            self.assertNotIn(raw, (root / "auth.json").read_text())
            self.assertEqual(
                "authorized",
                migrated.poll_device_authorization(started["deviceId"])["status"],
            )

    def test_operation_records_are_private_and_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            operation = store.create_operation(
                "visual.test",
                "token:test",
                {"layer": "Bus Stops"},
            )
            self.assertEqual("running", operation["status"])
            terminal = store.finish_operation(
                operation["id"],
                status="failed",
                error={"code": "visual.failed", "message": "No canvas."},
            )
            self.assertEqual("failed", store.read_operation(operation["id"])["status"])
            self.assertEqual(
                0o600,
                stat.S_IMODE(
                    (store.operations / f"{terminal['id']}.json").stat().st_mode
                ),
            )

    def test_operation_results_normalize_database_native_values(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            operation = store.create_operation(
                "derived-layer.create",
                "token:test",
                {"name": "places"},
            )
            identifier = uuid.UUID("12345678-1234-5678-1234-567812345678")
            terminal = store.finish_operation(
                operation["id"],
                status="succeeded",
                result={
                    "derivedLayer": {
                        "name": "places",
                        "createdAt": datetime(
                            2026, 7, 21, 11, 11, 52, 489807,
                            tzinfo=timezone.utc,
                        ),
                        "businessDate": date(2026, 7, 21),
                        "rowCount": Decimal("2941"),
                        "requestId": identifier,
                    }
                },
            )

            self.assertEqual("succeeded", terminal["status"])
            stored = store.read_operation(operation["id"])
            layer = stored["result"]["derivedLayer"]
            self.assertEqual("2026-07-21T11:11:52.489807+00:00", layer["createdAt"])
            self.assertEqual("2026-07-21", layer["businessDate"])
            self.assertEqual("2941", layer["rowCount"])
            self.assertEqual(str(identifier), layer["requestId"])

    def test_cancellation_request_is_nonterminal_until_worker_confirms(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            operation = store.create_operation(
                "derived-layer.create",
                "token:test",
                {"name": "places"},
            )

            cancelling = store.request_operation_cancellation(operation["id"])
            self.assertEqual("cancelling", cancelling["status"])
            self.assertIsNotNone(cancelling["cancellationRequested"])
            cancelled = store.finish_operation(
                operation["id"],
                status="cancelled",
                error={"code": "derived_layer.cancelled"},
            )
            self.assertEqual("cancelled", cancelled["status"])

    def test_terminal_operation_cannot_be_overwritten_by_racing_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            operation = store.create_operation(
                "derived-layer.create", "token:test",
            )
            succeeded = store.finish_operation(
                operation["id"], status="succeeded", result={"ok": True},
            )

            preserved = store.finish_operation(
                operation["id"],
                status="cancelled",
                error={"code": "derived_layer.cancelled"},
            )
            self.assertEqual(succeeded, preserved)

    def test_running_operations_become_indeterminate_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ControlStore(root)
            store.initialize("correct horse battery staple", "instance")
            operation = store.create_operation(
                "proposal.apply",
                "token:test",
                {"proposalId": "proposal-1"},
            )

            restarted = ControlStore(root)
            restarted.recover_interrupted_operations()
            recovered = restarted.read_operation(operation["id"])

            self.assertEqual("indeterminate", recovered["status"])
            self.assertEqual("operation.interrupted", recovered["error"]["code"])
            self.assertTrue(recovered["error"]["indeterminate"])
            self.assertEqual(
                "service-recovery",
                recovered["error"]["failurePhase"],
            )
            self.assertIn("before retrying", recovered["error"]["suggestedAction"])
            self.assertIsNone(recovered["result"])

    def test_cancelling_operations_become_indeterminate_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ControlStore(root)
            store.initialize("correct horse battery staple", "instance")
            operation = store.create_operation(
                "derived-layer.refresh", "token:test",
            )
            store.request_operation_cancellation(operation["id"])

            restarted = ControlStore(root)
            restarted.recover_interrupted_operations()
            recovered = restarted.read_operation(operation["id"])

            self.assertEqual("indeterminate", recovered["status"])
            self.assertEqual("operation.interrupted", recovered["error"]["code"])
