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

    #: The mutations an operator can currently grant, pinned so that adding
    #: one is a deliberate edit here and not a side effect of allowlisting.
    #: Both write a proposal record and no workspace.
    REACHABLE_MUTATIONS = ["proposals.create", "semantic.proposals.create"]

    def test_only_queue_writing_mutations_are_reachable(self) -> None:
        """What an agent can change, and what it still cannot.

        Until Phase 1 wave 3 this list was empty and the assertion said so.
        Allowlisting the two creates is the point at which an agent can write
        durable state, and the property that replaces "nothing" is narrower
        than it looks: both write a proposal record and neither touches a
        workspace. What they produce is an entry in a review queue that a
        person must still decide on, and `proposals_show` has been readable
        since Phase 0, so the thing an agent creates is the thing a human
        reads before anything happens.

        Applying is not on this list and is a different scope. If
        `proposals.apply` ever appears here, the review step has become
        optional and this test is where that shows.
        """
        start = DASHBOARD.index("export const MCP_SCOPE_OPTIONS=[")
        block = DASHBOARD[start : DASHBOARD.index("];", start)]
        offered = set(re.findall(r"\{id:'([^']+)'", block))
        reachable = sorted(
            name for name, op in operations.OPERATIONS.items()
            if op.mutating and set(op.required_scopes) <= offered
        )
        self.assertEqual(sorted(self.REACHABLE_MUTATIONS), reachable)

    def test_no_reachable_mutation_changes_a_workspace(self) -> None:
        """The property the list above rests on, checked rather than asserted
        in prose: every reachable mutation is a proposal create, whose effect
        is an entry in a queue. `apply`, `reload` and the derived-layer
        lifecycle all cost scopes no dashboard option offers.
        """
        for name in self.REACHABLE_MUTATIONS:
            with self.subTest(operation=name):
                self.assertTrue(
                    name.endswith("proposals.create"),
                    f"{name} is reachable and is not a proposal create",
                )
                self.assertEqual("propose", ACTION_SCHEMAS[name]["risk"])

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
