"""The guidance documents the runtime serves as MCP resources.

Two different things are pinned here, and they fail for different reasons.

The first is ordinary: the resources register, they read, and what they name is
what exists. A resource that lists and then fails to read is worse than one
that was never offered, because a client discovers it and builds on it.

The second is a cross-repository problem with no clean solution. This guidance
is *adapted* from `mapp-config-cli/docs/agent-workflow.md` -- the judgement in
that document is hard-won and mostly surface-agnostic, but its invocations are
not, and "use `config-cli` as the only remote write interface" is false for an
agent whose only interface is these tools. Prose cannot be auto-synchronised
across repositories. What can be detected is that the source moved, so its
digest is pinned: a change there fails here and somebody re-reads it, which is
the honest version of keeping two documents in step.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protected_resource import ProtectedResource  # noqa: E402
from runtime import GUIDANCE, GUIDANCE_DIR, build_runtime  # noqa: E402

#: The CLI's agent workflow, as it stood when this guidance was adapted from
#: it. Not a copy and not a contract -- a marker. When it changes, somebody
#: should read what changed and decide whether the adaptation still holds.
#:
#: Update it deliberately, in the same commit as the re-reading, and say in the
#: message what moved. Bumping it to make a red suite green is the one use that
#: defeats the point.
CLI_WORKFLOW_DIGEST = (
    "f1fe8acf2ee75e47a192a6dc61450c8c3d5884ae6215dfcaba78c8ebbaea9571"
)
CLI_WORKFLOW = (
    Path(__file__).resolve().parents[3] / "mapp-config-cli" / "docs"
    / "agent-workflow.md"
)


class FakeExchange:
    def request_digest(self, **kwargs):
        return "d" * 64

    def exchange(self, **kwargs):
        return "mapp_b_minted"


class FakeConfigApi:
    def get(self, **kwargs):
        return {}

    def post(self, **kwargs):
        return {}


def runtime():
    return build_runtime(
        resource=ProtectedResource(
            origin="http://mcp.localhost", issuer="http://mcp.localhost"
        ),
        exchange=FakeExchange(),
        config_api=FakeConfigApi(),
    )


class ResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = runtime()
        self.listed = asyncio.run(self.server.list_resources())

    def test_every_declared_document_is_offered(self) -> None:
        self.assertEqual(
            sorted(entry["uri"] for entry in GUIDANCE),
            sorted(str(resource.uri) for resource in self.listed),
        )

    def test_every_offered_document_reads(self) -> None:
        """A resource that lists and then fails to read is worse than one that
        was never offered. The likeliest cause is a file the image did not
        copy, which no import would catch."""
        for entry in GUIDANCE:
            with self.subTest(uri=entry["uri"]):
                contents = asyncio.run(
                    self.server.read_resource(entry["uri"])
                )
                text = "".join(part.content for part in contents)
                self.assertGreater(len(text), 500, "suspiciously short")
                self.assertTrue(text.startswith("#"), "not a markdown document")

    def test_the_files_are_where_the_dockerfile_copies_them(self) -> None:
        """The image copies the directory, so a document added outside it
        would work in the tree and be absent in the deployment."""
        dockerfile = (
            Path(__file__).resolve().parents[1] / "Dockerfile"
        ).read_text()
        self.assertIn("guidance /app/guidance", dockerfile)
        for entry in GUIDANCE:
            with self.subTest(path=entry["path"]):
                self.assertTrue((GUIDANCE_DIR / entry["path"]).is_file())

    def test_it_is_served_as_markdown(self) -> None:
        for resource in self.listed:
            with self.subTest(uri=str(resource.uri)):
                self.assertEqual("text/markdown", resource.mime_type)

    def test_guidance_is_not_also_a_tool(self) -> None:
        """A resource is readable without invoking anything. Offering the same
        text as a tool as well would make reading the instructions an action
        with a result to interpret, and cost a scope for nothing."""
        for name in self.server._tool_manager._tools:
            self.assertNotIn("guidance", name)


#: Names that look like tools and are not. Pinned rather than pattern-matched
#: away, so each is a decision somebody made: `derived_layers` is the schema
#: the platform fixes managed relations into, and appears in the guidance for
#: exactly that reason.
NOT_TOOLS = frozenset({"derived_layers"})


class ContentTests(unittest.TestCase):
    """What the guidance says has to be true of *this* surface.

    The failure worth preventing is guidance that names something the agent
    does not have. It is adapted from a CLI document, so the specific risk is
    a `config-cli` invocation surviving the adaptation -- advice an agent
    cannot follow and may report as impossible.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = {
            entry["path"]: (GUIDANCE_DIR / entry["path"]).read_text()
            for entry in GUIDANCE
        }
        cls.tools = set(runtime()._tool_manager._tools)

    def test_no_document_tells_an_agent_to_run_the_cli(self) -> None:
        for path, text in self.documents.items():
            with self.subTest(document=path):
                self.assertNotIn("config-cli", text)

    def test_every_tool_it_names_exists(self) -> None:
        """Naming a tool that does not exist sends an agent looking for it.
        Checked against the registry rather than a list, so a renamed tool
        fails here."""
        for path, text in self.documents.items():
            named = set(re.findall(r"`([a-z][a-z0-9_]{4,})`", text))
            # Shaped like a tool: snake_case with a prefix this surface uses.
            # A bare word is not a claim about the registry -- `operations` is
            # a parameter of proposals_check, and prose in backticks is prose.
            candidates = {
                name for name in named
                if "_" in name and name.split("_")[0] in {
                    "layers", "proposals", "semantic", "derived", "catalog",
                    "dependencies", "operations", "xyz", "capabilities",
                    "describe", "federation", "icons", "plugins",
                }
            } - NOT_TOOLS
            with self.subTest(document=path):
                self.assertEqual(
                    set(), candidates - self.tools,
                    f"{path} names tools that do not exist",
                )

    def test_the_workflow_states_the_order_the_platform_enforces(self) -> None:
        text = self.documents["workflow.md"]
        for step in ("proposals_check", "proposals_create", "proposals_apply",
                     "xyz_status"):
            with self.subTest(step=step):
                self.assertIn(step, text)
        self.assertLess(
            text.index("proposals_check"), text.index("proposals_apply"),
            "the document should teach the order it describes",
        )

    def test_the_derived_guidance_names_the_scope_it_needs(self) -> None:
        self.assertIn("derive:manage", self.documents["derived-layers.md"])


class SourceDriftTests(unittest.TestCase):
    """The cross-repository half, and the part that will fail one day."""

    def test_the_cli_workflow_has_not_moved_since_this_was_adapted(
        self,
    ) -> None:
        if not CLI_WORKFLOW.exists():
            self.skipTest(
                "mapp-config-cli is not checked out beside this repository"
            )
        digest = hashlib.sha256(CLI_WORKFLOW.read_bytes()).hexdigest()
        self.assertEqual(
            CLI_WORKFLOW_DIGEST,
            digest,
            "mapp-config-cli/docs/agent-workflow.md changed. Read what changed"
            " and decide whether the adapted guidance in mapp-mcp/guidance/"
            " still holds, then update CLI_WORKFLOW_DIGEST in the same commit."
            " Bumping it to clear this failure is the one use that defeats it.",
        )


if __name__ == "__main__":
    unittest.main()
