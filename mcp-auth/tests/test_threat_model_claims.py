"""The threat model's load-bearing claims, as assertions.

A threat model is read instead of the code, so a claim in it that quietly stops
being true is worse than one that was never made. These pin the statements an
owner accepted when they accepted the document -- not its prose, which is
review's job, but the facts it rests on.

Each test names the sentence it defends. If a sentence changes, the test should
change with it and be re-accepted, which is the point.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp-auth"))
sys.path.insert(0, str(ROOT / "config-ui"))

import operations  # noqa: E402
import server  # noqa: E402
from control_api import ACTION_SCHEMAS  # noqa: E402

THREAT_MODEL = (ROOT / "docs" / "mcp-threat-model.md").read_text()
DASHBOARD = (ROOT / "config-ui" / "src" / "main.jsx").read_text()


def presets():
    """The scope sets the dashboard offers as one click, read from it."""
    start = DASHBOARD.index("export const MCP_CLIENT_PRESETS=[")
    block = DASHBOARD[start : DASHBOARD.index("\n];", start)]
    return {
        found.group(1): set(re.findall(r"'([^']+)'", found.group(2)))
        for found in re.finditer(
            r"id:'([^']+)',label:'[^']*',scopes:\[([^\]]*)\]", block
        )
    }


class ThreatModelClaimTests(unittest.TestCase):
    def test_full_and_admin_are_never_issuable_for_this_resource(self) -> None:
        """"`full` and `admin` never issued for the MCP resource."

        The configuration API's `_required_scope` returns `full` for any path
        nobody classified, so an allowlisted operation requiring it would make
        every unclassified route reachable at once.
        """
        self.assertIn("`full` and `admin` never issued", THREAT_MODEL)
        for name, operation in operations.OPERATIONS.items():
            with self.subTest(operation=name):
                self.assertNotIn("full", operation.required_scopes)
                self.assertNotIn("admin", operation.required_scopes)
        accepted = server.MCP_SCOPES | operations.all_required_scopes()
        self.assertEqual(set(), accepted & {"full", "admin"})

    def test_no_administrative_surface_is_allowlisted(self) -> None:
        """"no credential administration, no token issuance or revocation, and
        no dashboard or audit administration exposed."

        Checked against the allowlist rather than the tools, because the
        allowlist is what an exchanged credential can reach: a tool is one way
        to spend one, not the boundary.
        """
        self.assertIn("no credential administration", THREAT_MODEL)
        forbidden = ("/api/auth", "/api/tokens", "/api/audit", "/api/mcp",
                     "/api/admin", "/api/connect", "/api/contract")
        for name, operation in operations.OPERATIONS.items():
            path = operation.path_template
            for prefix in forbidden:
                with self.subTest(operation=name, prefix=prefix):
                    self.assertFalse(
                        path.startswith(prefix),
                        f"{name} reaches the administrative surface {path}",
                    )

    def test_no_mutating_operation_is_reachable_by_an_offered_scope(self) -> None:
        """The read surface is read-only in the sense that matters: every
        mutating operation on the allowlist needs a scope no dashboard option
        offers, so no credential an operator can issue reaches one.

        This had one exception until Phase 1 wave 1. `derived-layers.refresh`
        needed only `derive`, which is offered and sits in the default analysis
        preset -- so the scope that lets an agent read aggregate values also
        authorised replacing the relation they come from, and the only thing
        holding it shut was that no tool called it. Splitting `derive` from
        `derive:manage` closed that, and the assertion is now the empty list
        rather than a named exception.
        """
        start = DASHBOARD.index("export const MCP_SCOPE_OPTIONS=[")
        block = DASHBOARD[start : DASHBOARD.index("];", start)]
        offered = set(re.findall(r"\{id:'([^']+)'", block))
        reachable = sorted(
            name for name, op in operations.OPERATIONS.items()
            if op.mutating and set(op.required_scopes) <= offered
        )
        self.assertEqual([], reachable)

    def test_the_default_preset_discloses_only_the_configured_workspace(self) -> None:
        """"`analysis` is the default and excludes both of the scopes that
        disclose anything beyond the configured workspace."
        """
        analysis = presets()["analysis"]
        self.assertNotIn("federation:observe", analysis)
        self.assertNotIn("semantic:source", analysis)

    def test_the_wider_disclosures_are_separately_granted(self) -> None:
        """"`federation:observe` is offered only through a separate
        `analysis-federated` preset ... and `semantic:source` is in no preset
        at all."
        """
        by_name = presets()
        carrying = [n for n, s in by_name.items() if "federation:observe" in s]
        self.assertEqual(["analysis-federated"], carrying)
        self.assertEqual(
            [],
            [n for n, s in by_name.items() if "semantic:source" in s],
        )

    def test_every_allowlisted_read_is_a_get_or_a_declared_read_risk(self) -> None:
        """The surface is described throughout as reads. A non-mutating
        operation that was neither a GET nor classified as a read risk would
        make that description false.
        """
        read_risks = {"aggregate-data-read", "inspect", "read"}
        for name, operation in operations.OPERATIONS.items():
            if operation.mutating:
                continue
            with self.subTest(operation=name):
                self.assertIn(ACTION_SCHEMAS[name]["risk"], read_risks)


if __name__ == "__main__":
    unittest.main()
