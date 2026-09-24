import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from proposal_review import review_bundle, validate_review, ProposalReviewError


class ProposalReviewTests(unittest.TestCase):
    def setUp(self):
        self.proposal = {"id": "p-1", "candidateHash": "a" * 64, "originalRevision": "r-1"}
        self.operation = {"id": "op-1", "kind": "proposal.screenshot", "status": "succeeded",
                          "created": datetime.now(timezone.utc).isoformat(),
                          "target": {"proposalId": "p-1", **{k: self.proposal[k] for k in ("candidateHash", "originalRevision")}},
                          "result": {"visual": {"renderPassed": True, "passed": True, "artifacts": {
                              prefix + suffix: f"{prefix}/{suffix.lower()}.png"
                              for prefix in ("before", "after") for suffix in ("Map", "StylingPanel", "InfoPanel")}}}}
        self.reader = patch('proposal_review.read_visual_image', side_effect=lambda root, path, **kwargs: {
            "path": path, "sha256": "b" * 64, "width": 2160, "height": 2160, "sizeBytes": 100,
        }).start()
        self.addCleanup(patch.stopall)

    def bundle(self):
        return review_bundle(self.operation, Path('/unused'), b'test-key', 'http://localhost:8181')

    def test_review_binds_exact_candidate_and_original_resolution_captures(self):
        review = self.bundle()
        self.assertTrue(review['eligible'])
        self.assertTrue(review['complete'])
        self.assertEqual(6, len(review['captures']))
        for item in review['captures']:
            self.assertEqual(2160, item['width'])
            self.assertEqual(3600, item['download']['expiresInSeconds'])
            self.assertTrue(item['download']['url'].startswith('http://localhost:8181/artifact-downloads/'))
        self.assertEqual(review['binding'], validate_review(self.proposal, review['binding'], self.operation, review))
        # Renewed links have no effect on the approval fingerprint.
        with patch('visual_artifacts.time.time', return_value=1):
            self.assertEqual(review['binding'], self.bundle()['binding'])

    def test_changed_bytes_or_confirmation_fields_refuse_apply(self):
        review = self.bundle()
        for key in ('candidateHash', 'originalRevision', 'evidenceOperationId', 'evidenceFingerprint'):
            payload = {**review['binding'], key: 'changed'}
            with self.subTest(key=key), self.assertRaises(ProposalReviewError):
                validate_review(self.proposal, payload, self.operation, review)
        self.reader.side_effect = lambda root, path, **kwargs: {"path": path, "sha256": "c" * 64}
        with self.assertRaises(ProposalReviewError):
            validate_review(self.proposal, review['binding'], self.operation, self.bundle())

    def test_other_proposals_stale_previews_and_failed_maps_cannot_be_acknowledged_away(self):
        for change in ('proposal', 'revision', 'age', 'render', 'pending'):
            with self.subTest(change=change):
                operation = copy.deepcopy(self.operation)
                if change == 'proposal': operation['target']['proposalId'] = 'other'
                if change == 'revision': operation['target']['originalRevision'] = 'old'
                if change == 'age': operation['created'] = '2000-01-01T00:00:00Z'
                if change == 'render': operation['result']['visual']['renderPassed'] = False
                if change == 'pending': operation['status'] = 'running'
                review = review_bundle(operation, Path('/unused'), b'key', '')
                with self.assertRaises(ProposalReviewError):
                    validate_review(self.proposal, {**review['binding'], 'acknowledgeIncompletePreview': True}, operation, review)

    def test_partial_panels_need_explicit_acknowledgement_but_missing_map_blocks(self):
        artifacts = self.operation['result']['visual']['artifacts']
        del artifacts['afterInfoPanel']
        review = self.bundle()
        self.assertTrue(review['eligible'])
        self.assertFalse(review['complete'])
        self.assertEqual(['popup'], review['missingCandidateCaptures'])
        with self.assertRaises(ProposalReviewError):
            validate_review(self.proposal, review['binding'], self.operation, review)
        validate_review(self.proposal, {**review['binding'], 'acknowledgeIncompletePreview': True}, self.operation, review)
        del artifacts['afterMap']
        review = self.bundle()
        with self.assertRaises(ProposalReviewError):
            validate_review(self.proposal, {**review['binding'], 'acknowledgeIncompletePreview': True}, self.operation, review)
