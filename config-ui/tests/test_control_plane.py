import json
import stat
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import control_schema
from control_fixture import (
    DATABASE_URL,
    ControlStoreTestCase,
    control_rows,
)
from control_plane import (
    DEVICE_SCOPES,
    TOKEN_SCOPES,
    ControlStore,
    iso,
    now,
    token_hash,
)


class ControlPlaneTests(ControlStoreTestCase):
    def test_existing_sensitive_state_is_made_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposals = root / "proposals"
            proposal_dir = proposals / "legacy-proposal"
            proposal_dir.mkdir(parents=True)
            audit = root / "audit.jsonl"
            proposal = proposal_dir / "proposal.json"
            audit.write_text("{}\n")
            proposal.write_text("{}")
            audit.chmod(0o644)
            proposal_dir.chmod(0o755)
            proposal.chmod(0o644)

            ControlStore(root)

            # The credential itself is no longer a file; what remains on disk
            # is the audit log, the proposals and the lock.
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

    def test_token_names_are_permanently_unique_case_insensitively(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            _raw, record = store.create_token("CLI operator")

            for duplicate in ("CLI operator", " cli OPERATOR "):
                with self.subTest(name=duplicate):
                    with self.assertRaisesRegex(
                        ValueError, "Token names must be unique"
                    ):
                        store.create_token(duplicate)

            self.assertTrue(store.revoke_token(record["id"]))
            with self.assertRaisesRegex(
                ValueError, "Token names must be unique"
            ):
                store.create_token("CLI OPERATOR")
            self.assertEqual(1, len(store.list_tokens()))

    def test_a_refused_session_is_not_refreshed_and_expiry_is_pruned(self):
        """Two properties the JSON store expressed as "did not write".

        A rejected session must not have its last-use stamped -- otherwise a
        wrong CSRF token would keep a session alive indefinitely -- and an
        expired row must be removed even when the call that finds it fails.
        Against SQL those are observable directly rather than through a write
        counter, which is the better assertion anyway: it says what must be
        true of the state, not how many times the store touched it.
        """
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            session, _csrf = store.login(
                "correct horse battery staple",
                "127.0.0.1",
            )
            before = control_rows("sessions")[0]["last_used_at"]

            self.assertFalse(store.session("not-a-session"))
            self.assertFalse(store.session(session, "wrong-csrf", require_csrf=True))
            self.assertEqual(before, control_rows("sessions")[0]["last_used_at"])

            # A valid call does refresh it, so the assertion above is about the
            # refusal and not about the store never writing at all.
            self.assertTrue(store.session(session))
            self.assertGreater(control_rows("sessions")[0]["last_used_at"], before)

            # Age the row past the idle bound; the next call prunes it even
            # though that call is itself refused.
            stale = now() - timedelta(seconds=31 * 60)
            connection = control_schema.connect(DATABASE_URL)
            try:
                connection.execute(
                    "UPDATE control.sessions SET last_used_at = %s", (stale,)
                )
            finally:
                connection.close()
            self.assertFalse(store.session("not-a-session"))
            self.assertEqual([], control_rows("sessions"))
            self.assertFalse(store.session(session))

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

    def test_demo_password_reset_revokes_api_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            session, _csrf = store.login(
                "correct horse battery staple",
                "127.0.0.1",
            )
            active_raw, active = store.create_token("active token")
            revoked_raw, revoked = store.create_token("already revoked")
            self.assertTrue(store.revoke_token(revoked["id"]))
            pending_device = store.start_device_authorization(
                "pending device",
                ["inspect"],
                "127.0.0.2",
            )
            approved_device = store.start_device_authorization(
                "approved device",
                ["inspect"],
                "127.0.0.3",
            )
            self.assertTrue(
                store.approve_device_authorization(approved_device["userCode"])
            )

            store.reset_password("ordinary replacement password")
            self.assertFalse(store.session(session))
            self.assertEqual(
                active["id"],
                store.authenticate_token(active_raw, "127.0.0.1")["id"],
            )
            self.assertIsNone(store.authenticate_token(revoked_raw, "127.0.0.1"))
            self.assertEqual(
                {"pending", "approved"},
                {
                    authorization["status"]
                    for authorization in store.list_device_authorizations()
                },
            )

            store.reset_password(
                "disposable demo password",
                revoke_tokens=True,
            )

            self.assertIsNone(store.authenticate_token(active_raw, "127.0.0.1"))
            self.assertTrue(all(token["revoked"] for token in store.list_tokens()))
            for duplicate_name in (" ACTIVE TOKEN ", " ALREADY REVOKED "):
                with self.subTest(duplicate_name=duplicate_name):
                    with self.assertRaisesRegex(
                        ValueError,
                        "Token names must be unique",
                    ):
                        store.create_token(duplicate_name)
            self.assertEqual(
                {"active token", "already revoked"},
                {token["name"] for token in store.list_tokens()},
            )
            self.assertEqual([], store.list_device_authorizations())
            self.assertEqual(
                {"status": "expired"},
                store.poll_device_authorization(pending_device["deviceId"]),
            )
            self.assertEqual(
                {"status": "expired"},
                store.poll_device_authorization(approved_device["deviceId"]),
            )
            revoked_devices = control_rows("device_authorizations")
            self.assertEqual({"revoked"}, {row["status"] for row in revoked_devices})
            # Revocation also brings the expiry forward, so a revoked record
            # stops occupying a queue slot as well as losing its authority.
            self.assertTrue(all(row["expires_at"] <= now() for row in revoked_devices))
            device_audit = next(
                event
                for event in reversed(store.audit_tail())
                if event["event"] == "device.authorizations_revoked"
            )
            self.assertEqual(
                {"reason": "demo-init", "count": 2},
                device_audit["details"],
            )
            self.assertEqual(
                {"reason": "demo-init"},
                store.audit_tail()[-1]["details"],
            )
            self.assertEqual("token.revoked_all", store.audit_tail()[-1]["event"])

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

            # The former half of this test seeded "not-a-date" into a token's
            # expiry and asserted the store revoked it on sight. A timestamptz
            # column cannot hold that value, so the malformed-record path is
            # now unrepresentable rather than merely unhandled, and the
            # behaviour it protected no longer has an input that reaches it.
            # What remains is the property that still has meaning: an expired
            # token does not authenticate, and a live one does.
            expired_raw, expired = store.create_token(
                "expiring",
                iso(now() + timedelta(days=1)),
            )
            live_raw, live = store.create_token("valid")
            connection = control_schema.connect(DATABASE_URL)
            try:
                connection.execute(
                    "UPDATE control.tokens SET expires_at = %s WHERE token_id = %s",
                    (now() - timedelta(seconds=1), expired["id"]),
                )
            finally:
                connection.close()

            self.assertIsNone(store.authenticate_token(expired_raw, "127.0.0.1"))
            self.assertEqual(
                live["id"],
                store.authenticate_token(live_raw, "127.0.0.1")["id"],
            )
            # An expired token is refused without being marked revoked: expiry
            # and revocation are different states, and conflating them would
            # release the name reservation.
            records = {item["id"]: item for item in store.list_tokens()}
            self.assertIsNone(records[expired["id"]]["revoked"])

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
            self.assertNotIn(raw, str(control_rows("tokens")))
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
            self.assertEqual([], control_rows("tokens"))
            self.assertEqual(
                "approved", control_rows("device_authorizations")[0]["status"]
            )
            self.assertNotIn(
                "mapp_",
                str(control_rows("device_authorizations")),
            )
            authorized = store.poll_device_authorization(started["deviceId"])
            self.assertEqual("authorized", authorized["status"])
            self.assertEqual(
                ["inspect", "propose", "visual", "semantic:inspect"],
                authorized["record"]["scopes"],
            )
            self.assertEqual(
                f"Device: codex [{authorized['record']['id']}]",
                authorized["record"]["name"],
            )
            self.assertIsNotNone(authorized["record"]["expires"])
            self.assertNotIn(
                authorized["token"],
                (Path(directory) / "audit.jsonl").read_text(),
            )
            # The issued bearer token must exist nowhere at rest: not in the
            # device record that authorised it, and not in the token row, which
            # holds only its hash.
            persisted = str(control_rows("device_authorizations")) + str(
                control_rows("tokens")
            )
            self.assertNotIn(authorized["token"], persisted)
            self.assertEqual(
                token_hash(authorized["token"]),
                control_rows("tokens")[0]["token_hash"],
            )
            self.assertEqual(
                "consumed",
                store.poll_device_authorization(started["deviceId"])["status"],
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
            self.assertIsNone(operation["finished"])
            self.assertIsNotNone(terminal["finished"])
            self.assertEqual(terminal["updated"], terminal["finished"])
            self.assertEqual(
                0o600,
                stat.S_IMODE(
                    (store.operations / f"{terminal['id']}.json").stat().st_mode
                ),
            )

    def test_running_operation_progress_is_durable_and_terminal_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            operation = store.create_operation(
                "visual.test", "token:test", {"layer": "Bus Stops"}
            )

            progress = store.update_operation_progress(
                operation["id"],
                stage="page-readiness",
                diagnostics={"pageErrors": []},
            )

            self.assertEqual("running", progress["status"])
            self.assertEqual("page-readiness", progress["stage"])
            self.assertEqual({"pageErrors": []}, progress["diagnostics"])
            terminal = store.finish_operation(
                operation["id"],
                status="failed",
                error={"code": "visual.run_timeout"},
            )
            unchanged = store.update_operation_progress(
                operation["id"], stage="late-worker-result"
            )
            self.assertEqual(terminal, unchanged)

    def test_operation_retention_never_prunes_active_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            active = store.create_operation(
                "visual.test", "token:test", {"layer": "Slow layer"}
            )
            for index in range(500):
                completed = store.create_operation(
                    "visual.test", "token:test", {"index": index}
                )
                store.finish_operation(
                    completed["id"], status="succeeded", result={"ok": True}
                )

            preserved = store.read_operation(active["id"])
            self.assertEqual("running", preserved["status"])
            self.assertIsNone(preserved["finished"])

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
            self.assertEqual(recovered["updated"], recovered["finished"])
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
            self.assertEqual(recovered["updated"], recovered["finished"])
            self.assertEqual("operation.interrupted", recovered["error"]["code"])

    def test_waiting_derived_job_is_not_replayed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ControlStore(root)
            store.initialize("correct horse battery staple", "instance")
            operation = store.create_operation(
                "derived-layer.create",
                "token:test",
                {"name": "waiting_layer", "action": "create"},
            )
            store.update_operation_progress(
                operation["id"],
                stage="waiting-for-worker",
                diagnostics={"queuePosition": 1},
            )

            restarted = ControlStore(root)
            restarted.recover_interrupted_operations()
            recovered = restarted.read_operation(operation["id"])

            self.assertEqual("indeterminate", recovered["status"])
            self.assertEqual("waiting-for-worker", recovered["stage"])
            self.assertEqual(
                "service-recovery", recovered["error"]["failurePhase"],
            )
            self.assertIsNone(recovered["result"])
