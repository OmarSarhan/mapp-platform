"""The evidence bundle's counts must be the code's counts.

The bundle is a gate artefact: it is what a reviewer reads instead of counting
for themselves. Its surface table said "16 read-only tools" and "17 of the
platform's 55 actions" for three waves after those numbers stopped being true,
because nothing but a person re-reading it would notice.

Derived from the same definitions the components use, so a wave that adds a
tool or an action fails here rather than quietly ageing the document.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "docs" / "mcp-phase0-evidence.md"

sys.path.insert(0, str(ROOT / "config-ui"))
sys.path.insert(0, str(ROOT / "mcp-auth"))


class EvidenceBundleCountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = BUNDLE.read_text()

    def claimed(self, pattern):
        found = re.search(pattern, self.text)
        self.assertIsNotNone(found, f"the bundle no longer states {pattern!r}")
        return int(found.group(1))

    def test_the_tool_count_matches_the_runtime(self):
        registered = (ROOT / "mapp-mcp" / "runtime.py").read_text().count(
            "    @tool("
        )
        self.assertEqual(registered, self.claimed(r"(\d+) read-only tools"))

    def test_the_allowlist_and_action_counts_match_the_tables(self):
        from control_api import ACTION_SCHEMAS
        from operations import OPERATIONS

        allowlisted, actions = re.search(
            r"(\d+) of the platform's (\d+) actions", self.text
        ).groups()
        self.assertEqual(len(OPERATIONS), int(allowlisted))
        self.assertEqual(len(ACTION_SCHEMAS), int(actions))

    def test_the_scope_vocabulary_matches_what_the_issuer_accepts(self):
        import operations
        import server

        accepted = server.MCP_SCOPES | operations.all_required_scopes()
        self.assertEqual(len(accepted), self.claimed(r"(\d+) accepted"))


if __name__ == "__main__":
    unittest.main()
