from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
from control_api import proposal_check, proposal_read
from control_plane import ControlStore
from control_fixture import ControlStoreTestCase


BINDING = {"name": "draft_percent", "assetId": "12345678-1234-1234-1234-123456789abc", "generation": 1}
LAYER = {"format": "geojson", "table": "derived_layers.draft_percent", "qID": "id", "geom": "geom"}
ORIGINAL = {"locale": {"layers": {}}}
CANDIDATE = {"locale": {"layers": {"Percent": LAYER}}}
OPERATIONS = [{"op": "set", "path": "/locale/layers/Percent", "value": LAYER}]


def derived():
    store = Mock()
    draft = {**BINDING, "createdBy": "token:creator", "expiresAt": "2099-01-01T00:00:00Z", "state": "active", "proposalId": None}
    store.get.return_value = {"name": BINDING["name"], "draft": draft, "semanticProfile": {"assetId": BINDING["assetId"], "generation": 1}}
    store.drafts_for_names.return_value = [draft]
    return store


class DraftCleanupStatusTests(unittest.TestCase):
    def test_watcher_reports_bounded_sweep_and_retention_outcomes(self):
        lifecycle = Mock()
        lifecycle.sweep.return_value = [{"outcome": "dropped"}, {"outcome": "other-proposal-reference"}]
        with (patch.object(app, "draft_lifecycle", return_value=lifecycle),
              patch.object(app, "DRAFT_CLEANUP_STATUS", {}),
              patch.object(app.DRAFT_CLEANUP_WAKE, "wait", side_effect=StopIteration),
              patch.object(app, "schedule_semantic_outbox") as outbox):
            with self.assertRaises(StopIteration):
                app.run_draft_cleanup()
            status = app.draft_cleanup_status()
            self.assertIsNotNone(status["lastCompletedAt"])
            self.assertIsNone(status["lastError"])
            self.assertEqual({"dropped": 1, "other-proposal-reference": 1}, status["outcomes"])
            self.assertEqual(20, status["batchLimit"])
            outbox.assert_called_once_with()

    def test_watcher_reports_failure_without_exposing_exception_details(self):
        lifecycle = Mock()
        lifecycle.sweep.side_effect = ValueError("private database details")
        with (patch.object(app, "draft_lifecycle", return_value=lifecycle),
              patch.object(app, "DRAFT_CLEANUP_STATUS", {}),
              patch.object(app.DRAFT_CLEANUP_WAKE, "wait", side_effect=StopIteration),
              patch.object(app.LOGGER, "exception")):
            with self.assertRaises(StopIteration):
                app.run_draft_cleanup()
            status = app.draft_cleanup_status()
            self.assertEqual("ValueError", status["lastError"])
            self.assertNotIn("private", str(status))


class DraftCheckTests(unittest.TestCase):
    def test_check_fingerprint_binds_exact_draft_identity_and_generation(self):
        base = proposal_check(ORIGINAL, "rev", CANDIDATE, OPERATIONS, [])
        checked = proposal_check(ORIGINAL, "rev", CANDIDATE, OPERATIONS, [], draft_relations=[BINDING])
        changed = proposal_check(ORIGINAL, "rev", CANDIDATE, OPERATIONS, [], draft_relations=[{**BINDING, "generation": 2}])
        self.assertEqual([BINDING], checked["draftRelations"])
        self.assertNotEqual(base["checkFingerprint"], checked["checkFingerprint"])
        self.assertNotEqual(changed["checkFingerprint"], checked["checkFingerprint"])
        self.assertEqual(base, proposal_check(ORIGINAL, "rev", CANDIDATE, OPERATIONS, [], draft_relations=[]))


class DraftProposalRoutesTests(ControlStoreTestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.control = ControlStore(Path(directory.name) / "control")
        self.derived = derived()
        for patcher in (
            patch.object(app, "CONTROL", self.control),
            patch.object(app, "DERIVED", self.derived),
            patch.object(app, "read_workspace", return_value=(b"{}", ORIGINAL, "rev")),
            patch.object(app, "validate_candidate", return_value=[]),
            patch.object(app, "semantic_publication_diagnostics", return_value=([], [])),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, path, body, actor="token:creator"):
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: actor
        handler._payload = lambda: copy.deepcopy(body)
        handler._remote = lambda: "127.0.0.1"
        handler._authentication = {"scopes": ["propose", "apply", "inspect"]}
        handler._json = lambda status, body: responses.append((status, body))
        handler.do_POST()
        return responses[0]

    def create(self):
        body = {"revision": "rev", "operations": OPERATIONS, "draftRelations": [BINDING]}
        status, checked = self.request("/api/proposals/check", body)
        self.assertEqual(200, status)
        body["checkFingerprint"] = checked["check"]["checkFingerprint"]
        status, result = self.request("/api/proposals", body)
        self.assertEqual(201, status, result)
        return result["proposal"]

    def test_checked_proposal_persists_and_binds_then_decline_queues_cleanup(self):
        proposal = self.create()
        self.assertEqual([BINDING], proposal_read(self.control, proposal["id"])["draftRelations"])
        self.derived.bind_drafts.assert_called_once_with([BINDING], proposal["id"], "token:creator", allow_other_owner=False)
        with patch.object(app, "DRAFT_CLEANUP_WAKE") as wake:
            status, body = self.request(f"/api/proposals/{proposal['id']}/decline", {"reason": "Cancel draft"})
        self.assertEqual(200, status)
        self.assertEqual("declined", body["proposal"]["status"])
        wake.set.assert_called_once()
        self.derived.cleanup_draft.assert_not_called()

    def test_other_actor_cannot_claim_draft_with_only_propose_scope(self):
        status, body = self.request("/api/proposals/check", {"operations": OPERATIONS, "revision": "rev", "draftRelations": [BINDING]}, actor="token:another")
        self.assertEqual(409, status)
        self.assertEqual("derived_draft.lifecycle_conflict", body["code"])
        self.derived.bind_drafts.assert_not_called()

    def test_binding_failure_leaves_nonpublishable_recovery_record(self):
        self.derived.bind_drafts.side_effect = app.DerivedLayerError("Ownership changed")
        status, _ = self.request("/api/proposals", {"revision": "rev", "operations": OPERATIONS, "draftRelations": [BINDING]})
        self.assertGreaterEqual(status, 400)
        records = list(self.control.proposals.glob("*/proposal.json"))
        self.assertEqual(1, len(records))
        record = json.loads(records[0].read_text())
        self.assertEqual("draft_binding_failed", record["status"])
        self.assertEqual([BINDING], record["draftRelations"])

    def test_expired_or_replaced_draft_cannot_apply(self):
        proposal = self.create()
        self.derived.get.return_value["draft"]["proposalId"] = proposal["id"]
        self.derived.get.return_value["semanticProfile"]["generation"] = 2
        with patch.object(app, "save_workspace") as save:
            status, body = self.request(f"/api/proposals/{proposal['id']}/apply", {"approved": True})
        self.assertEqual(409, status)
        self.assertEqual("derived_draft.lifecycle_conflict", body["code"])
        save.assert_not_called()


class DraftPublicationTests(unittest.TestCase):
    def test_direct_workspace_save_adopts_drafts_without_a_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace.json"
            workspace.write_text(json.dumps(ORIGINAL))
            store = derived()
            with patch.object(app, "WORKSPACE", workspace), patch.object(app, "CONTROL", SimpleNamespace(root=root)), patch.object(app, "DERIVED", store):
                expected = app.read_workspace()[2]
                app.save_workspace(CANDIDATE, expected)
            self.assertEqual(CANDIDATE, json.loads(workspace.read_text()))
            store.adopt_drafts.assert_called_once_with([BINDING])
            self.assertEqual([], list((root / "draft-publications").glob("*.json")))

    def test_committed_workspace_survives_promotion_error_and_keeps_durable_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace.json"
            workspace.write_text(json.dumps(ORIGINAL))
            store = derived()
            store.adopt_drafts.side_effect = RuntimeError("unavailable")
            with patch.object(app, "WORKSPACE", workspace), patch.object(app, "CONTROL", SimpleNamespace(root=root)), patch.object(app, "DERIVED", store), patch.object(app, "LOGGER"):
                expected = app.read_workspace()[2]
                app.save_workspace(CANDIDATE, expected)
                app.save_workspace(ORIGINAL, app.read_workspace()[2])
            record = json.loads(next((root / "draft-publications").glob("*.json")).read_text())
            self.assertEqual("committed", record["state"])
            self.assertEqual([BINDING], record["draftRelations"])
            self.assertEqual(ORIGINAL, json.loads(workspace.read_text()))


if __name__ == "__main__":
    unittest.main()
