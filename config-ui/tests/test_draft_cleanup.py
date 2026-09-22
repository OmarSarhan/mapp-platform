import copy
from datetime import datetime, timezone
import json
import itertools
import os
from pathlib import Path
import selectors
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import MagicMock, patch

import derived_drafts
from control_api import workspace_hash
from derived_drafts import (
    DraftCleanupBusy,
    DraftLifecycle,
    DraftLifecycleError,
    binding_of,
    lifecycle_lock,
    managed_references,
    retain_browser_lease,
)


NOW = 1_800_000_000


class FakeDerivedStore:
    def __init__(self):
        self.items = {}
        self.cleanup_draft = MagicMock(side_effect=self._cleanup)
        self.adopt_drafts = MagicMock(side_effect=self._adopt)
        self.bind_drafts = MagicMock(side_effect=self._bind)
        self.record_draft_cleanup = MagicMock()

    def list_drafts(self, limit=100):
        return [copy.deepcopy(item["draft"]) for item in self.items.values()
                if item.get("draft", {}).get("state") == "active"][:limit]

    def get(self, name, **kwargs):
        return copy.deepcopy(self.items[name])

    def drafts_for_names(self, names):
        return [draft for draft in self.list_drafts() if draft["name"] in names]

    def _adopt(self, bindings, proposal_id=None):
        for binding in bindings:
            self.items[binding["name"]]["draft"]["state"] = "adopted"

    def _cleanup(self, binding):
        self.items[binding["name"]]["draft"]["state"] = "dropped"

    def _bind(self, bindings, proposal_id, actor, **kwargs):
        for binding in bindings:
            self.items[binding["name"]]["draft"]["proposalId"] = proposal_id


class DraftCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.control = SimpleNamespace(
            root=self.root, proposals=self.root / "proposals",
            operations=self.root / "operations", audit=MagicMock(),
        )
        self.control.proposals.mkdir()
        self.control.operations.mkdir()
        self.store = FakeDerivedStore()
        self.workspace = {}
        self.lifecycle = DraftLifecycle(self.control, self.store, lambda: (b"{}", self.workspace))
        clock_patch = patch("derived_drafts.time.time", return_value=NOW)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def draft(self, *, name="preview_percent", expires=3600, proposal=None, creator="creator"):
        draft = {
            "name": name, "assetId": str(uuid.uuid4()), "generation": 1,
            "state": "active", "proposalId": proposal, "createdBy": creator,
            "expiresAt": datetime.fromtimestamp(NOW + expires, timezone.utc).isoformat(),
        }
        self.store.items[name] = {
            "name": name, "draft": draft,
            "semanticProfile": {"assetId": draft["assetId"], "generation": 1},
        }
        return draft

    @staticmethod
    def workspace_for(name):
        return {"locales": {"en": {"layers": {"preview": {"table": f"derived_layers.{name}"}}}}}

    def proposal(self, draft, status="pending", *, proposal_id="owner", actor="creator", **overrides):
        value = {
            "id": proposal_id, "status": status, "actor": actor,
            "original": {}, "candidate": self.workspace_for(draft["name"]),
            "draftRelations": [binding_of(draft)], **overrides,
        }
        value["originalHash"] = workspace_hash(value["original"])
        value["candidateHash"] = workspace_hash(value["candidate"])
        directory = self.control.proposals / proposal_id
        directory.mkdir(exist_ok=True)
        path = directory / "proposal.json"
        path.write_text(json.dumps(value))
        return path

    def publication(self, draft, state):
        directory = self.root / "draft-publications"
        directory.mkdir(exist_ok=True)
        path = directory / "recovery.json"
        path.write_text(json.dumps({"state": state, "draftRelations": [binding_of(draft)]}))
        return path

    def test_expired_unattached_draft_drops_and_audits_without_enrolling_permanent(self):
        draft = self.draft(expires=-1)
        self.store.items["existing_permanent"] = {"name": "existing_permanent"}
        result = self.lifecycle.sweep()
        self.assertEqual("dropped", result[0]["outcome"])
        self.store.cleanup_draft.assert_called_once_with(binding_of(draft))
        self.assertEqual({"name": "existing_permanent"}, self.store.items["existing_permanent"])
        self.assertEqual("expired", self.control.audit.call_args.kwargs["details"]["reason"])

    def test_declined_and_cancelled_drafts_wait_for_browser_lease_then_drop(self):
        for status in ("declined", "cancelled"):
            with self.subTest(status=status):
                draft = self.draft(name=status, proposal=status)
                self.proposal(draft, status, proposal_id=status)
                retain_browser_lease(self.root)
                self.assertEqual("browser-preview-lease", self.lifecycle.reconcile_one(draft))
                (self.root / "draft-browser-lease.json").write_text(json.dumps({"until": NOW - 1}))
                self.assertEqual("dropped", self.lifecycle.reconcile_one(draft))

    def test_pending_unexpired_owner_is_retained(self):
        draft = self.draft(proposal="owner")
        self.proposal(draft)
        self.assertEqual("retained-until-expiry", self.lifecycle.reconcile_one(draft))
        self.store.cleanup_draft.assert_not_called()

    def test_pending_expired_owner_is_eligible_for_approved_expiry_cleanup(self):
        draft = self.draft(expires=-1, proposal="owner")
        self.proposal(draft)
        self.assertEqual("dropped", self.lifecycle.reconcile_one(draft))

    def test_applied_and_live_references_adopt(self):
        applied = self.draft(name="applied_relation", proposal="owner", expires=-1)
        self.proposal(applied, "applied")
        self.assertEqual("adopted-proposal", self.lifecycle.reconcile_one(applied))
        self.store.adopt_drafts.assert_called_once_with([binding_of(applied)], proposal_id="owner")
        live = self.draft(name="live_relation", expires=-1)
        self.workspace = self.workspace_for(live["name"])
        self.assertEqual("adopted-live", self.lifecycle.reconcile_one(live))
        self.store.cleanup_draft.assert_not_called()

    def test_applying_and_conflicted_owners_are_retained_after_expiry(self):
        for status in ("applying", "conflicted"):
            with self.subTest(status=status):
                draft = self.draft(name=status, proposal=status, expires=-1)
                self.proposal(draft, status, proposal_id=status)
                self.assertEqual("apply-reconciliation-required", self.lifecycle.reconcile_one(draft))
        self.store.cleanup_draft.assert_not_called()

    def test_other_pending_proposal_original_and_candidate_references_block(self):
        draft = self.draft(expires=-1)
        for field in ("original", "candidate"):
            with self.subTest(field=field):
                workspaces = {"original": {}, "candidate": {}, field: self.workspace_for(draft["name"])}
                self.proposal(draft, proposal_id="other", draftRelations=[], **workspaces)
                self.assertEqual("other-proposal-reference", self.lifecycle.reconcile_one(draft))
        self.store.cleanup_draft.assert_not_called()

    def test_missing_owner_and_mismatched_binding_remain_blocked(self):
        draft = self.draft(proposal="owner", expires=-1)
        self.assertEqual("owner-proposal-unavailable", self.lifecycle.reconcile_one(draft))
        self.proposal(draft, "declined", draftRelations=[])
        self.assertEqual("owner-binding-mismatch", self.lifecycle.reconcile_one(draft))
        self.store.cleanup_draft.assert_not_called()

    def test_binding_requires_exact_identity_creator_and_new_reference(self):
        draft = self.draft()
        binding, candidate = binding_of(draft), self.workspace_for(draft["name"])
        self.assertEqual([binding], self.lifecycle.validate_bindings([binding], candidate, {}, "creator"))
        self.assertEqual([binding], self.lifecycle.validate_bindings([binding], candidate, {}, "admin"))
        for changed, original, actor in (
            ({**binding, "generation": 2}, {}, "creator"),
            (binding, {}, "someone-else"),
            (binding, candidate, "creator"),
        ):
            with self.subTest(actor=actor, changed=changed), self.assertRaises(DraftLifecycleError):
                self.lifecycle.validate_bindings([changed], candidate, original, actor)

    def test_interrupted_binding_recovers_only_exact_creator_claim(self):
        draft = self.draft()
        self.proposal(draft)
        self.assertEqual("retained-until-expiry", self.lifecycle.reconcile_one(draft))
        self.store.bind_drafts.assert_called_once_with([binding_of(draft)], "owner", "creator", allow_other_owner=False)
        other = self.draft(name="unauthorized")
        self.proposal(other, proposal_id="other", actor="someone-else")
        self.assertEqual("proposal-binding-conflict", self.lifecycle.reconcile_one(other))
        self.assertEqual(1, self.store.bind_drafts.call_count)

    def test_ambiguous_publication_intent_retains_then_committed_intent_adopts(self):
        draft = self.draft(expires=-1)
        self.publication(draft, "intent")
        self.assertEqual("publication-reconciliation-required", self.lifecycle.reconcile_one(draft))
        self.publication(draft, "committed")
        self.assertEqual("adopted-publication", self.lifecycle.reconcile_one(draft))
        self.store.cleanup_draft.assert_not_called()

    def test_committed_publication_recovers_after_failed_adoption(self):
        draft = self.draft()
        intent = self.lifecycle.prepare_publication(self.workspace_for(draft["name"]))
        self.store.adopt_drafts.side_effect = RuntimeError("temporary failure")
        with self.assertRaises(RuntimeError):
            self.lifecycle.commit_publication(intent)
        self.assertEqual("committed", json.loads(intent[0].read_text())["state"])
        self.store.adopt_drafts.side_effect = self.store._adopt
        self.assertEqual("adopted-publication", self.lifecycle.reconcile_one(draft))

    def test_failed_drop_records_only_sanitized_error_and_retries(self):
        draft = self.draft(expires=-1)
        self.store.cleanup_draft.side_effect = RuntimeError("private SQL and credentials")
        self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.record_draft_cleanup.assert_called_once_with(binding_of(draft), "cleanup-blocked", "RuntimeError")
        self.store.cleanup_draft.side_effect = self.store._cleanup
        self.assertEqual("dropped", self.lifecycle.sweep()[0]["outcome"])
        self.assertEqual(2, self.store.cleanup_draft.call_count)

    def test_invalid_unreadable_and_unknown_proposal_state_fail_closed(self):
        draft = self.draft(expires=-1)
        path = self.proposal(draft, draftRelations=[])
        for invalid in ("{invalid", "[]", '{"id":"owner","id":"other"}', '{"value":NaN}'):
            with self.subTest(invalid=invalid):
                path.write_text(invalid)
                self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.proposal(draft, "unknown-status", draftRelations=[])
        self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        target = self.root / "unreadable-target.json"
        path.replace(target)
        path.symlink_to(target)
        self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.cleanup_draft.assert_not_called()

    def test_missing_or_invalid_live_workspace_fails_closed(self):
        self.draft(expires=-1)
        self.workspace = None
        self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.cleanup_draft.assert_not_called()

    def test_tampered_proposal_workspace_hashes_block_cleanup(self):
        draft = self.draft(expires=-1)
        for field in ("original", "candidate"):
            with self.subTest(field=field):
                path = self.proposal(draft, "declined", draftRelations=[])
                record = json.loads(path.read_text())
                record[field] = {"tampered": True}
                path.write_text(json.dumps(record))
                self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.cleanup_draft.assert_not_called()

    def test_scan_limits_fail_closed(self):
        draft = self.draft(expires=-1)
        self.proposal(draft, "declined", draftRelations=[])
        with patch("derived_drafts.MAX_SCAN_BYTES", 1):
            self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.cleanup_draft.assert_not_called()

    def test_aggregate_publication_evidence_size_blocks_cleanup(self):
        self.draft(expires=-1)
        directory = self.root / "draft-publications"
        directory.mkdir()
        evidence = json.dumps({"state": "intent", "draftRelations": []})
        for number in range(2):
            (directory / f"intent-{number}.json").write_text(evidence)
        with patch("derived_drafts.MAX_SCAN_BYTES", len(evidence.encode()) + 1):
            self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.cleanup_draft.assert_not_called()

    def test_aggregate_operation_evidence_size_blocks_cleanup(self):
        self.draft(expires=-1)
        evidence = json.dumps({"kind": "proposal.screenshot", "status": "succeeded"})
        for number in range(2):
            (self.control.operations / f"preview-{number}.json").write_text(evidence)
        with patch("derived_drafts.MAX_SCAN_BYTES", len(evidence.encode()) + 1):
            self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.cleanup_draft.assert_not_called()

    def test_publication_recovery_scan_deadline_blocks_cleanup(self):
        draft = self.draft(expires=-1)
        self.publication(draft, "committed")
        ticks = itertools.count(0, 3)
        with patch("derived_drafts.time.monotonic", side_effect=lambda: next(ticks)):
            self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.adopt_drafts.assert_not_called()
        self.store.cleanup_draft.assert_not_called()

    def test_operation_scan_deadline_blocks_cleanup(self):
        self.draft(expires=-1)
        (self.control.operations / "preview.json").write_text(json.dumps({"status": "succeeded"}))
        ticks = itertools.count(0, 3)
        with patch("derived_drafts.time.monotonic", side_effect=lambda: next(ticks)):
            self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.cleanup_draft.assert_not_called()

    def test_empty_symlink_evidence_directories_fail_closed(self):
        self.draft(expires=-1)
        for directory in (self.control.proposals, self.control.operations, self.root / "draft-publications"):
            with self.subTest(directory=directory.name):
                if directory.exists():
                    directory.rmdir()
                target = self.root / (directory.name + "-symlink-target")
                target.mkdir()
                directory.symlink_to(target, target_is_directory=True)
                try:
                    self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
                finally:
                    directory.unlink()
                    directory.mkdir()
        self.store.cleanup_draft.assert_not_called()

    def test_queued_preview_and_corrupt_preview_state_protect_relations(self):
        draft = self.draft(expires=-1)
        path = self.control.operations / "preview.json"
        path.write_text(json.dumps({"kind": "proposal.screenshot", "status": "running"}))
        self.assertEqual("queued-preview", self.lifecycle.reconcile_one(draft))
        path.write_text("invalid")
        self.assertEqual("cleanup-blocked", self.lifecycle.sweep()[0]["outcome"])
        self.store.cleanup_draft.assert_not_called()

    def test_mixed_dynamic_and_provider_references_fail_closed(self):
        workspace = {"query": 'SELECT * FROM derived_layers.known JOIN derived_layers.${dynamic} USING (id)'}
        self.assertEqual({"known", "*"}, managed_references(workspace))
        self.assertIn("*", managed_references({"query": {"dbs": "MAPP", "src": "hidden.sql"}}))
        self.assertIn("*", managed_references({
            "dbs": "MAPP", "templates": {"lookup": {"src": "hidden.sql"}},
        }))
        self.assertIn("*", managed_references({
            "dbs": "MAPP", "layers": {"preview": {"template": {"src": "hidden.sql"}}},
        }))
        draft = self.draft(expires=-1)
        self.workspace = workspace
        self.assertEqual("unresolved-live-reference", self.lifecycle.reconcile_one(draft))
        self.store.cleanup_draft.assert_not_called()


class DraftLifecycleProcessLockTests(unittest.TestCase):
    def test_shared_processes_coexist_and_cleanup_waits_for_the_last_holder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            program = """
import sys
from pathlib import Path
from derived_drafts import lifecycle_lock
with lifecycle_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.read(1)
"""
            environment = {**os.environ, "PYTHONPATH": str(Path(derived_drafts.__file__).parent)}
            child = subprocess.Popen(
                [sys.executable, "-c", program, str(root)], env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            try:
                with selectors.DefaultSelector() as ready:
                    ready.register(child.stdout, selectors.EVENT_READ)
                    self.assertTrue(ready.select(timeout=5), "Lock holder did not become ready")
                self.assertEqual("locked", child.stdout.readline().strip())
                with lifecycle_lock(root):
                    self.assertIsNone(child.poll())
                with self.assertRaises(DraftCleanupBusy):
                    with lifecycle_lock(root, exclusive=True):
                        self.fail("Exclusive cleanup overlapped another process's preview")
                child.communicate("x", timeout=5)
                self.assertEqual(0, child.returncode)
                with lifecycle_lock(root, exclusive=True):
                    self.assertEqual(0o600, (root / "draft-lifecycle.lock").stat().st_mode & 0o777)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()


if __name__ == "__main__":
    unittest.main()
