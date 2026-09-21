"""Every number the MCP documentation states, checked against the code.

A document nobody compares to the code is one that has already drifted. These
pin the *facts* in the operator-facing pages -- tool counts, preset sizes,
scope lists, time and consumption bounds, the split between what asks and what
does not -- by deriving each from the source and asserting the page says it.

Prose is review's job. What is here is the set of claims a reader would act on
and be wrong about.

The failure this prevents is specific and has already happened once:
`mcp-authorization.md` told an operator that the MCP resource "is not published
at all", which was true when it was written and false by the time anybody
followed it to connect an agent. Nothing compared the sentence to the server.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for component in ("mapp-mcp", "config-ui", "mcp-auth"):
    sys.path.insert(0, str(ROOT / component))

GUIDE = ROOT / "docs" / "mcp-getting-started.md"
REFERENCE = ROOT / "docs" / "mcp-authorization.md"


def flowed(text):
    """One space for any run of whitespace, with blockquote markers removed.

    The claim is what the page says, not how it is wrapped or whether it sits
    in a callout. Matching raw text makes a guard that fails when a paragraph
    is reflowed, which trains people to loosen it rather than fix the fact.
    """
    without_markers = re.sub(r"(?m)^\s*>\s?", "", text)
    return re.sub(r"\s+", " ", without_markers)


def _runtime():
    """The registered tool surface, built exactly as it is served."""
    from protected_resource import ProtectedResource
    from runtime import build_runtime

    class Exchange:
        def request_digest(self, **kwargs):
            return "d" * 64

        def exchange(self, **kwargs):
            return "b"

    class ConfigApi:
        def get(self, **kwargs):
            return {}

        def post(self, **kwargs):
            return {}

    return build_runtime(
        resource=ProtectedResource(
            origin="http://mcp.localhost", issuer="http://mcp.localhost"
        ),
        exchange=Exchange(),
        config_api=ConfigApi(),
    )


def _presets():
    """The dashboard's presets, read from it rather than restated."""
    source = (ROOT / "config-ui" / "src" / "main.jsx").read_text()
    start = source.index("export const MCP_CLIENT_PRESETS=[")
    block = source[start : source.index("\n];", start)]
    return {
        found.group(1): frozenset(re.findall(r"'([^']+)'", found.group(3)))
        for found in re.finditer(
            r"id:'([^']+)',label:'[^']*',scopes:\[([^\]]*)\]".replace(
                "([^']*)',scopes", "([^']*)',scopes"
            ).replace("scopes:\\[([^\\]]*)\\]", "scopes:\\[([^\\]]*)\\]"),
            block,
        )
    }


def _preset_scopes():
    source = (ROOT / "config-ui" / "src" / "main.jsx").read_text()
    start = source.index("export const MCP_CLIENT_PRESETS=[")
    block = source[start : source.index("\n];", start)]
    found = {}
    for match in re.finditer(
        r"\{id:'([^']+)',label:'([^']*)',scopes:\[([^\]]*)\]", block
    ):
        found[match.group(1)] = frozenset(re.findall(r"'([^']+)'", match.group(3)))
    return found


def _visible(server, scopes):
    """How many tools a grant carrying `scopes` is shown."""
    held = frozenset(scopes)
    return sum(
        1
        for name in server._tool_manager._tools
        if frozenset(server.tool_scopes.get(name, ())) <= held
    )


class GuideClaimTests(unittest.TestCase):
    """docs/mcp-getting-started.md."""

    @classmethod
    def setUpClass(cls) -> None:
        # Whitespace-normalised, because the claim is what the page says and
        # not how it is wrapped. Matching raw text makes a guard that fails
        # when a paragraph is reflowed, which trains people to loosen it.
        cls.text = flowed(GUIDE.read_text())
        cls.server = _runtime()

    def test_the_tool_total_is_what_the_runtime_registers(self) -> None:
        total = len(self.server._tool_manager._tools)
        self.assertIn(f"{total} tools against the platform's", self.text)

    def test_the_platform_action_count_is_the_platforms(self) -> None:
        from control_api import ACTION_SCHEMAS

        self.assertIn(f"{len(ACTION_SCHEMAS)} actions", self.text)

    def test_the_read_write_and_asking_split_is_the_real_one(self) -> None:
        """The sentence a reader most relies on: how much of this surface can
        change anything without them."""
        import runtime as rt
        from control_api import requires_approval
        import operations as broker

        source = (ROOT / "mapp-mcp" / "runtime.py").read_text()
        writes, asks = 0, 0
        for block in re.split(r"\n    @tool\(", source)[1:]:
            siblings = list(re.finditer(r"\n    (?:async )?def ", block))
            if len(siblings) > 1:
                block = block[: siblings[1].start()]
            declared = re.search(r"operation=([A-Za-z_]+)", block)
            if not declared:
                continue
            descriptor = getattr(rt, declared.group(1), None)
            if not isinstance(descriptor, dict):
                continue
            name = descriptor["operation_id"]
            if not broker.OPERATIONS.get(name) or not broker.OPERATIONS[name].mutating:
                continue
            if requires_approval(name):
                asks += 1
            else:
                writes += 1
        reads = len(self.server._tool_manager._tools) - writes - asks
        self.assertIn(f"{reads} of the {reads + writes + asks} tools only", self.text)
        self.assertIn(f"{writes + asks} that write", self.text)
        self.assertIn(f"{writes} write into a review queue", self.text)
        self.assertIn(f"remaining {asks} ask", self.text)
        self.assertIn(f"**Asks you** ({asks})", self.text)
        self.assertIn(f"**Does not ask** ({writes})", self.text)
        self.assertIn(f"**Reads** ({reads})", self.text)

    def test_every_preset_row_states_the_tools_it_actually_shows(self) -> None:
        """The table a reader picks from. A count that is too high invites a
        wider grant than they meant."""
        rows = dict(
            re.findall(r"\|\s+\*\*([^*]+)\*\*\s+\|\s+(\d+)\s+\|", self.text)
        )
        self.assertTrue(rows, "no preset table found")
        labels = {
            "discovery": "Discovery",
            "analysis": "Analysis",
            "analysis-federated": "Analysis (federated)",
            "authoring": "Author",
            "authoring-apply": "Author and apply",
            "authoring-derive": "Author, apply and build derived layers",
        }
        presets = _preset_scopes()
        self.assertEqual(
            set(labels), set(presets),
            "the presets changed; the guide's table names them individually",
        )
        for preset, label in labels.items():
            with self.subTest(preset=preset):
                self.assertIn(label, rows, f"{label} is missing from the table")
                self.assertEqual(
                    str(_visible(self.server, presets[preset])),
                    rows[label],
                )

    def test_the_window_bounds_are_the_enforced_ones(self) -> None:
        from control_plane import ControlStore

        self.assertIn(
            f"At most {int(ControlStore.WINDOW_MAX_LIFETIME.total_seconds() // 60)}"
            " minutes", self.text,
        )
        self.assertIn(
            f"at most {ControlStore.WINDOW_MAX_CONSUMPTIONS} actions", self.text
        )
        self.assertIn(
            f"more than {int(ControlStore.WINDOW_RECENCY.total_seconds() // 60)}"
            " minutes ago", self.text,
        )

    def test_the_approval_lifetimes_are_the_enforced_ones(self) -> None:
        from control_plane import ControlStore

        minutes = int(ControlStore.APPROVAL_LIFETIME.total_seconds() // 60)
        spend = int(ControlStore.RECEIPT_LIFETIME.total_seconds() // 60)
        self.assertIn(f"{self._word(minutes)} minutes to decide", self.text)
        self.assertIn(f"{self._word(spend)} minutes from your decision", self.text)

    @staticmethod
    def _word(number):
        return {5: "five", 15: "Fifteen", 60: "sixty"}[number]

    def test_the_callback_port_is_the_one_the_dashboard_uses(self) -> None:
        """The guide tells a reader to register this port. A mismatch with the
        dashboard's generated configuration produces a sign-in that bounces."""
        source = (ROOT / "config-ui" / "src" / "main.jsx").read_text()
        port = re.search(r"MCP_CALLBACK_PORT=(\d+)", source).group(1)
        self.assertIn(f"http://localhost:{port}/callback", self.text)
        self.assertIn(f"http://127.0.0.1:{port}/callback", self.text)
        self.assertIn(f"`{port}` is what the dashboard uses", self.text)

    def test_the_scopes_the_widest_preset_omits_are_named(self) -> None:
        """The guide explains why even the widest preset does not reach every
        tool. The first version of it said both scopes were in no preset at
        all, which was wrong -- `federation:observe` is in `analysis-federated`
        -- and this is what caught it."""
        presets = _preset_scopes()
        widest = max(presets.values(), key=len)
        # Parenthesised: `-` binds tighter than `|`, and without these the
        # expression quietly computed something else entirely.
        offered = frozenset().union(*presets.values()) | {"semantic:source"}
        missing = sorted(offered - widest)
        self.assertEqual(
            ["federation:observe", "semantic:source"], missing,
            "the widest preset's omissions changed; the guide names them",
        )
        self.assertNotIn(
            "semantic:source",
            frozenset().union(*presets.values()),
            "semantic:source is now in a preset; the guide says it is in none",
        )
        for scope in missing:
            with self.subTest(scope=scope):
                self.assertIn(f"`{scope}`", self.text)

    def test_the_windowable_exclusion_is_stated(self) -> None:
        from control_plane import WINDOWABLE_ACTION_CLASSES

        for excluded in ("semantic-apply", "federation-provision"):
            self.assertNotIn(excluded, WINDOWABLE_ACTION_CLASSES)
        self.assertIn("Never semantic or federation changes", self.text)

    def test_the_opt_in_variable_is_described_as_the_code_reads_it(
        self,
    ) -> None:
        """`MAPP_MCP` reads the shell first and `.env` second, so the guide has
        to offer both and say which wins. It was shell-only, and the guide
        warned that putting it in `.env` started nothing -- a warning that
        becomes a false claim the moment the launcher stops being shell-only,
        which is why this is pinned against the launcher rather than alone."""
        launcher = (ROOT / "bin" / "mapp").read_text()
        self.assertIn('"${MAPP_MCP:-$(dotenv_value MAPP_MCP)}"', launcher)
        self.assertIn('dotenv_value MAPP_DEMO_SOURCES', launcher)
        self.assertIn("MAPP_MCP=1 ./bin/mapp all", self.text)
        self.assertIn("takes precedence over `.env`", self.text)
        self.assertNotIn("not in `.env`", self.text)

    def test_the_commands_it_prints_exist(self) -> None:
        launcher = (ROOT / "bin" / "mapp").read_text()
        for command in ("reset-config-password", "mcp-client-register",
                        "mcp-client-list", "ps"):
            with self.subTest(command=command):
                self.assertIn(command, self.text)
                self.assertIn(f"  {command}", launcher)

    def test_the_origin_it_names_is_the_configured_default(self) -> None:
        example = (ROOT / ".env.example").read_text()
        default = re.search(r"^MCP_SITE=(\S+)", example, re.M)
        self.assertIsNotNone(default, "no MCP_SITE default to compare against")
        self.assertIn(default.group(1), self.text)


class ReferenceClaimTests(unittest.TestCase):
    """docs/mcp-authorization.md, which an operator reads to understand the
    thing the guide taught them to use."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = flowed(REFERENCE.read_text())

    def test_it_documents_the_approval_system_at_all(self) -> None:
        """It described the Phase 0 authorization design and stopped there,
        so an operator granting `apply` had no page telling them what that
        meant. Each of these is a mechanism they meet in ordinary use."""
        for concept in ("receipt", "standing approval", "elicit",
                        "derive:manage"):
            with self.subTest(concept=concept):
                self.assertIn(concept, self.text.lower())

    def test_the_published_paths_are_the_ones_caddy_serves(self) -> None:
        """The table said the MCP resource was not published. It is."""
        caddyfile = (ROOT / "docker" / "caddy" / "Caddyfile").read_text()
        self.assertIn("/mcp", caddyfile)
        self.assertIn("| `POST` | `/mcp` |", self.text)
        self.assertNotIn("is not published at all", self.text)

    def test_the_window_bounds_agree_with_the_guide(self) -> None:
        from control_plane import ControlStore

        self.assertIn(
            str(int(ControlStore.WINDOW_MAX_LIFETIME.total_seconds() // 60)),
            self.text,
        )
        self.assertIn(str(ControlStore.WINDOW_MAX_CONSUMPTIONS), self.text)


if __name__ == "__main__":
    unittest.main()
