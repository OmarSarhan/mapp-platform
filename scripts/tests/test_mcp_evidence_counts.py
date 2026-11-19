"""Check the surface counts in the documents that replaced the Phase 0 bundle."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

sys.path.insert(0, str(ROOT / "config-ui"))
sys.path.insert(0, str(ROOT / "mcp-auth"))


class DocumentedSurfaceCountTests(unittest.TestCase):
    def assert_claim(self, filename, claim):
        text = " ".join((DOCS / filename).read_text().split())
        self.assertIn(claim, text, filename)

    def test_the_tool_count_matches_the_runtime(self):
        registered = (ROOT / "mapp-mcp" / "runtime.py").read_text().count(
            "    @tool("
        )
        self.assert_claim("mcp-threat-model.md", f"publishes {registered} tools")

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
        self.assert_claim("mcp-getting-started.md", f"Of the {len(writing)} that write")

    def test_the_allowlist_and_action_counts_match_the_tables(self):
        from control_api import ACTION_SCHEMAS
        from operations import OPERATIONS

        self.assert_claim("mcp-authorization.md",
                          f"allowlist covers {len(OPERATIONS)} of the platform's {len(ACTION_SCHEMAS)} actions")

    def test_the_scope_vocabulary_matches_what_the_issuer_accepts(self):
        import operations
        import server

        accepted = server.MCP_SCOPES | operations.all_required_scopes()
        self.assert_claim("mcp-authorization.md", f"issuer accepts {len(accepted)} scopes")


if __name__ == "__main__":
    unittest.main()
