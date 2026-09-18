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
        self.assertEqual(registered, self.claimed(r"(\d+) tools of which"))

    def test_the_stated_write_count_matches_the_tools(self):
        """The surface stopped being read-only in Phase 1 wave 3, so the
        sentence says how much of it writes.

        Counted in tools, not in allowlisted operations: the allowlist carries
        mutating entries with no tool behind them, deliberately, so those two
        numbers are different and the sentence is about the tools a client is
        offered. Derived by joining each registration's declared operation to
        its descriptor's operation id, then asking the allowlist.
        """
        import re

        from operations import OPERATIONS

        source = (ROOT / "mapp-mcp" / "runtime.py").read_text()
        descriptors = dict(re.findall(
            r'^([A-Z_]+) = \{\n    "operation_id": "([a-z0-9.\-]+)"',
            source, re.M,
        ))
        declared = re.findall(r"    @tool\(\n        operation=([A-Za-z_]+),", source)
        writing = [
            name for name in declared
            if name in descriptors
            and OPERATIONS[descriptors[name]].mutating
        ]
        self.assertEqual(len(writing), self.claimed(r"of which (\d+) write"))

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
