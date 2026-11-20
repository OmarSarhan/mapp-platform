"""Exercise the actual shell approval boundary without applying a workspace."""
import copy
import json
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "docker/demo-sources/layers.sh").read_text()
FUNCTIONS = SOURCE[SOURCE.index("workspace_approval_request()"):
                   SOURCE.index('\nstep "Reconciling the saved workspace\'s derived layers"')]
PROPOSAL = {"id": "fixture-proposal", "status": "pending",
            "candidateHash": "candidate", "originalRevision": "original",
            "diff": [{"op": "add", "path": "/locale/layers/example"}]}
BINDING = {"proposalId": PROPOSAL["id"], "candidateHash": "candidate",
           "originalRevision": "original", "evidenceOperationId": "a" * 32,
           "evidenceFingerprint": "retained-images"}
PREVIEW = {"operation": {"id": "a" * 32, "status": "succeeded"},
           "review": {"binding": BINDING, "complete": True, "eligible": True,
                      "renderPassed": True, "checksPassed": True, "captures": [
                          {"side": "candidate", "kind": "map", "status": "captured",
                           "download": {"url": "https://fixture.invalid/preview.png"}}
                      ]}}


class DemoApprovalTests(unittest.TestCase):
    def run_flow(self, *, preview=None, confirmation=None, lose_reply=False):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "proposal.json").write_text(json.dumps({"proposal": PROPOSAL}))
            (work / "preview.json").write_text(json.dumps(preview or PREVIEW))
            script = '''set -euo pipefail
fail() { printf '%s\\n' "$*" >&2; exit 1; }
jqp() { python3 -c "import json,sys; d=json.load(sys.stdin); $1"; }
api() {
  case "$2" in
    */screenshot) printf '{"operation":{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}' ;;
    /api/visual-operations/*) cat "$WORK_DIR/preview.json" ;;
    /api/proposals/*) cat "$WORK_DIR/proposal.json" ;;
    *) exit 99 ;;
  esac
}
curl() {
  cp "$WORK_DIR/workspace-approval.json" "$WORK_DIR/submitted.json"
  echo submitted >> "$WORK_DIR/submissions"
  if [[ "$LOSE_REPLY" == 1 ]]; then return 28; fi
  printf '200'
}
workspace_apply_state() { echo ready; }
''' + FUNCTIONS + '\napply_saved_workspace_proposal fixture-proposal unused\n'
            env = {**os.environ, "WORK_DIR": directory, "CONFIG_HOST": "fixture",
                   "BASE": "http://fixture", "TOKEN": "test-only",
                   "LOSE_REPLY": "1" if lose_reply else "0"}
            master = slave = None
            try:
                if confirmation is not None:
                    master, slave = pty.openpty()
                    os.write(master, (confirmation + "\n").encode())
                result = subprocess.run(
                    ["bash", "-c", script], env=env,
                    stdin=slave if slave is not None else subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=5,
                )
            finally:
                if master is not None:
                    os.close(master)
                    os.close(slave)
            submitted = work / "submitted.json"
            if submitted.exists():
                self.assertEqual(["submitted"], (work / "submissions").read_text().splitlines())
            return result, json.loads(submitted.read_text()) if submitted.exists() else None

    def test_noninteractive_run_never_applies(self):
        result, submitted = self.run_flow()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("awaits explicit review and approval", result.stderr)
        self.assertIsNone(submitted)

    def test_exact_confirmation_submits_retained_binding(self):
        result, submitted = self.run_flow(confirmation="APPLY fixture-proposal")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Focused diff:", result.stdout)
        self.assertEqual({"approved": True, **{key: value for key, value in BINDING.items()
                                               if key != "proposalId"}}, submitted)

    def test_wrong_confirmation_never_applies(self):
        result, submitted = self.run_flow(confirmation="APPLY another-proposal")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("declined", result.stderr)
        self.assertIsNone(submitted)

    def test_lost_apply_reply_is_not_replayed(self):
        result, submitted = self.run_flow(confirmation="APPLY fixture-proposal", lose_reply=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("write may have committed", result.stderr)
        self.assertIsNotNone(submitted)

    def test_changed_binding_never_reaches_confirmation_or_apply(self):
        for field in ("proposalId", "candidateHash", "originalRevision"):
            with self.subTest(field=field):
                preview = copy.deepcopy(PREVIEW)
                preview["review"]["binding"][field] = "different"
                result, submitted = self.run_flow(preview=preview,
                                                   confirmation="APPLY fixture-proposal")
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn("type APPLY", result.stdout)
                self.assertIsNone(submitted)

    def test_incomplete_stale_or_failed_review_never_applies(self):
        for field in ("complete", "eligible", "renderPassed", "checksPassed"):
            with self.subTest(field=field):
                preview = copy.deepcopy(PREVIEW)
                preview["review"][field] = False
                result, submitted = self.run_flow(preview=preview,
                                                   confirmation="APPLY fixture-proposal")
                self.assertNotEqual(0, result.returncode)
                self.assertIsNone(submitted)

    def test_missing_map_or_download_cannot_be_confirmed(self):
        for captures in ([], [{"side": "candidate", "kind": "map", "status": "captured"}]):
            with self.subTest(captures=captures):
                preview = copy.deepcopy(PREVIEW)
                preview["review"]["captures"] = captures
                result, submitted = self.run_flow(preview=preview,
                                                   confirmation="APPLY fixture-proposal")
                self.assertNotEqual(0, result.returncode)
                self.assertIsNone(submitted)


if __name__ == "__main__":
    unittest.main()
