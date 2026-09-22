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

    def test_standing_approval_covers_all_gated_operations(self) -> None:
        from control_api import ACTION_SCHEMAS, requires_approval, windowable
        for operation in ACTION_SCHEMAS:
            self.assertEqual(requires_approval(operation), windowable(operation))
        self.assertFalse(windowable("unknown.operation"))
        self.assertIn("no time or action", THREAT_MODEL)
        self.assertIn("one platform instance", THREAT_MODEL)

    def test_no_windowable_operation_escapes_the_receipt(self) -> None:
        """Defends: "a window substitutes the decider, never the receipt."

        An operation a window may decide but which needs no receipt would be
        auto-approved into nothing at all -- the window would be the only
        control, rather than the thing that answers on a person's behalf.
        """
        from control_api import requires_approval, windowable

        for name in operations.OPERATIONS:
            if windowable(name):
                with self.subTest(operation=name):
                    self.assertTrue(requires_approval(name))

    def test_client_approval_retains_recent_administrator_authentication(self) -> None:
        from control_plane import ControlStore
        self.assertIn("within 15 minutes", THREAT_MODEL)
        self.assertEqual(15, ControlStore.WINDOW_RECENCY.total_seconds() / 60)
        self.assertIn("stays enabled until turned off", THREAT_MODEL)

    def test_the_two_approval_windows_are_what_the_document_says(self) -> None:
        """Defends: "fifteen minutes to decide ... and five minutes to claim
        and spend".

        Two numbers in prose, describing two predicates in SQL. Prose is where
        a reader learns how long an approval lives, so a change to either
        constant that leaves the document alone teaches the wrong thing to
        everybody who reads it instead of the code.
        """
        from control_plane import ControlStore

        self.assertIn("fifteen minutes to decide", THREAT_MODEL)
        self.assertIn("five minutes to claim and spend", THREAT_MODEL)
        self.assertEqual(
            15, ControlStore.APPROVAL_LIFETIME.total_seconds() / 60
        )
        self.assertEqual(
            5, ControlStore.RECEIPT_LIFETIME.total_seconds() / 60
        )
        self.assertLess(
            ControlStore.RECEIPT_LIFETIME,
            ControlStore.APPROVAL_LIFETIME,
            "the spend window must be the tighter of the two",
        )

    def test_every_apply_class_operation_requires_approval(self) -> None:
        """Defends: "a grant with `apply` no longer authorises every apply for
        its lifetime".

        The claim is about the allowlist, not about one operation, so it is
        checked across the allowlist. An operation that mutates a workspace and
        does not require approval is the sentence becoming false.
        """
        from control_api import requires_approval

        from control_api import NO_APPROVAL_RISKS

        # The exemptions are read from the platform rather than restated. A
        # restated pair went stale the moment wave 7 allowlisted a dry run.
        unguarded = sorted(
            name for name, operation in operations.OPERATIONS.items()
            if operation.mutating
            and not requires_approval(name)
            and ACTION_SCHEMAS[name]["risk"] not in NO_APPROVAL_RISKS
        )
        self.assertEqual(
            [], unguarded,
            "these mutate and ask nobody, and are not the deliberate"
            " propose/visual exemptions",
        )

    def test_the_measured_client_capabilities_are_the_ones_recorded(
        self,
    ) -> None:
        """Defends the capability table's three rows.

        They were measured on one date against three versions. Nothing in the
        code can re-measure them, so what is pinned is that the document still
        names the date and the versions it claims to have measured -- a table
        silently updated to a newer client is a measurement nobody made.
        """
        for claim in (
            "2026-09-18",
            "Codex CLI 0.155.0",
            "Claude Code 2.1.276",
            "Gemini CLI 0.58.0",
        ):
            with self.subTest(claim=claim):
                self.assertIn(claim, THREAT_MODEL)

    #: The mutations an operator can currently grant, pinned so that adding
    #: one is a deliberate edit here and not a side effect of allowlisting.
    #: Split at Phase 1 wave 6, because the two halves now rest on different
    #: controls and collapsing them would hide which one is load-bearing.
    #:
    #: Unattended: an agent holding the scope does these without asking. Each
    #: writes a proposal record or attaches evidence to one; none changes what
    #: the map serves.
    UNATTENDED_MUTATIONS = [
        "proposals.create",
        "semantic.proposals.create",
        "proposals.preview-plan",
        "proposals.preview-test",
        "proposals.preview-screenshot",
        # A dry run, single-use only so a probe cannot be replayed. It writes
        # nothing, which is why it is here rather than below.
        "derived-layers.plan",
    ]

    #: Attended: the scope is necessary and not sufficient. Each requires an
    #: approval receipt bound to that exact request, so holding the grant buys
    #: the ability to *ask*.
    ATTENDED_MUTATIONS = [
        "proposals.apply",
        "semantic.proposals.apply",
        "xyz.reload",
        "derived-layers.create",
        "derived-layers.replace",
        "derived-layers.refresh",
        "derived-layers.drop",
    ]

    def test_the_reachable_mutations_are_the_two_pinned_lists(self) -> None:
        """What an agent can change, and under what condition.

        Until Phase 1 wave 3 this list was empty. Wave 3 allowlisted the two
        creates; wave 6 made the applies grantable. Adding to either list
        should be a deliberate edit here rather than a side effect of
        allowlisting an operation or offering a scope.
        """
        start = DASHBOARD.index("export const MCP_SCOPE_OPTIONS=[")
        block = DASHBOARD[start : DASHBOARD.index("];", start)]
        offered = set(re.findall(r"\{id:'([^']+)'", block))
        reachable = sorted(
            name for name, op in operations.OPERATIONS.items()
            if op.mutating and set(op.required_scopes) <= offered
        )
        self.assertEqual(
            sorted(self.UNATTENDED_MUTATIONS + self.ATTENDED_MUTATIONS),
            reachable,
        )

    def test_no_unattended_mutation_changes_a_workspace(self) -> None:
        """The property the first list rests on, checked rather than asserted.

        Each is either a proposal create, whose effect is an entry in a queue,
        or a proposal preview, whose effect is an artifact attached to one.
        Neither alters what the map serves. Since wave 6 that is no longer
        kept true by the scopes being unofferable -- it is kept true by these
        being the only mutations that ask nobody.
        """
        from control_api import requires_approval

        allowed_risks = {"propose", "visual"}
        for name in self.UNATTENDED_MUTATIONS:
            with self.subTest(operation=name):
                if name == "derived-layers.plan":
                    # The one entry that does not act on a proposal. It is a
                    # probe: it reports what a definition would do and creates
                    # nothing, which is the same claim by a different route.
                    self.assertEqual(
                        "database-plan", ACTION_SCHEMAS[name]["risk"]
                    )
                    self.assertFalse(requires_approval(name))
                    continue
                self.assertTrue(
                    name.startswith("proposals.")
                    or name.startswith("semantic.proposals."),
                    f"{name} is reachable and does not act on a proposal",
                )
                self.assertIn(ACTION_SCHEMAS[name]["risk"], allowed_risks)
                self.assertFalse(requires_approval(name))

    def test_every_attended_mutation_actually_asks(self) -> None:
        """The property the second list rests on. A name added there without
        the receipt requirement would be an unattended mutation described as
        an attended one, which is the worst way for this document to be
        wrong."""
        from control_api import requires_approval

        for name in self.ATTENDED_MUTATIONS:
            with self.subTest(operation=name):
                self.assertIn(name, operations.OPERATIONS)
                self.assertTrue(requires_approval(name))

    def test_applying_is_reachable_only_behind_a_person(self) -> None:
        """Defends: "a person still decides whether any of it happens".

        Until Phase 1 wave 6 this held because `apply` could not be granted at
        all, and the test said so. That is no longer the control -- the three
        scopes are offered now -- so what it checks is the control that
        replaced it: every operation those scopes buy requires an approval
        receipt, which only a person can produce. If an apply-class operation
        ever becomes exempt, the sentence stops being true and this is where
        it shows.

        `derive:manage` joined them at wave 7, which is the last pin to come
        off. Nothing that mutates is held by unofferability any more -- every
        one of them is held by the receipt.
        """
        from control_api import requires_approval

        start = DASHBOARD.index("export const MCP_SCOPE_OPTIONS=[")
        offered = set(re.findall(
            r"\{id:'([^']+)'", DASHBOARD[start : DASHBOARD.index("];", start)]
        ))
        for scope in ("apply", "semantic:apply", "reload", "derive:manage"):
            with self.subTest(scope=scope):
                self.assertIn(
                    scope, offered, "wave 6 made this grantable"
                )
                from control_api import NO_APPROVAL_RISKS

                for name, operation in operations.OPERATIONS.items():
                    if scope not in operation.required_scopes:
                        continue
                    if ACTION_SCHEMAS[name]["risk"] in NO_APPROVAL_RISKS:
                        # A class the platform exempts on purpose. The same
                        # scope buys `derived-layers.plan`, a probe that
                        # reports what a definition would do and creates
                        # nothing -- it is single-use so it cannot be
                        # replayed, but there is nothing to approve.
                        continue
                    self.assertTrue(
                        requires_approval(name),
                        f"{name} costs {scope} and asks nobody",
                    )

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
