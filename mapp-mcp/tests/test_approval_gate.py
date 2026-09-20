"""Getting a person's permission for one exact request.

The gate is deliberately not a tool: nothing the model can invoke reaches it,
so there is no tool surface to drive it through. Wave 6's first mutating tool
is what calls it in earnest; these pin its behaviour until then, and the
properties they pin are the ones the whole mechanism rests on -- that the
receipt is bound to the request a person was shown, that a decline stops
everything, and that a client which cannot ask anybody is told so rather than
left waiting.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from authentication import Authenticated  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from protected_resource import ProtectedResource  # noqa: E402
from runtime import APPROVALS_CLAIM  # noqa: E402
from runtime import APPROVALS_CONFIRM  # noqa: E402
from runtime import APPROVALS_CREATE  # noqa: E402
from runtime import PROPOSALS_SHOW  # noqa: E402
from runtime import _approval_message  # noqa: E402
from runtime import _elicitation_modes  # noqa: E402
from runtime import build_runtime  # noqa: E402
from authentication import CURRENT_CALLER  # noqa: E402


HANDLE = "handle-value"
RECEIPT = "receipt-value"
APPROVAL_URL = "http://mapp.localhost/#approvals/" + "a" * 64

PACKET = {"summary": "Rename the passport layer folder.", "changeCount": 112}


class FakeExchange:
    """Digests like the real one, so the intent and the credential agree."""

    def __init__(self) -> None:
        self.digests = []

    def request_digest(self, **kwargs):
        self.digests.append(kwargs)
        return "d" * 64

    def exchange(self, **kwargs):
        return "mapp_b_minted"


class FakeConfigApi:
    """Answers the approval routes; records everything, including receipts."""

    def __init__(self, *, statuses=None) -> None:
        # Consumed in order, so a test can make the first poll pending.
        self.statuses = list(statuses or [{"status": "approved",
                                           "receipt": RECEIPT}])
        self.calls = []

    def get(self, **kwargs):
        self.calls.append({"method": "GET", **kwargs})
        return {}

    def post(self, **kwargs):
        self.calls.append({"method": "POST", **kwargs})
        if kwargs["path"] == APPROVALS_CREATE["path_template"]:
            return {"handle": HANDLE, "reference": "a" * 64,
                    "approvalUrl": APPROVAL_URL}
        if kwargs["path"] == APPROVALS_CLAIM["path_template"]:
            return (self.statuses.pop(0) if len(self.statuses) > 1
                    else self.statuses[0])
        if kwargs["path"] == APPROVALS_CONFIRM["path_template"]:
            return {"decided": kwargs["body"]["accepted"]}
        return {}

    def posted_to(self, path):
        return [call for call in self.calls
                if call["method"] == "POST" and call["path"] == path]


class Capability:
    def __init__(self, *, form=None, url=None) -> None:
        self.form = form
        self.url = url


class Capabilities:
    def __init__(self, elicitation) -> None:
        self.elicitation = elicitation


class FakeSession:
    def __init__(self, elicitation) -> None:
        self.client_capabilities = Capabilities(elicitation)
        self.completed = []

    async def send_elicit_complete(self, elicitation_id):
        self.completed.append(elicitation_id)


class Answer:
    def __init__(self, action, data=None) -> None:
        self.action = action
        self.data = data


class Confirmation:
    def __init__(self, approve) -> None:
        self.approve = approve


class FakeContext:
    """A client, as far as the gate can tell.

    Records what it was asked, because what the person is shown is the whole
    substance of the control: a prompt the model composed would be a prompt
    the model could make persuasive.
    """

    def __init__(self, *, elicitation, form=None, url=None) -> None:
        self.session = FakeSession(elicitation)
        self.form_answer = form
        self.url_answer = url
        self.asked = []

    async def elicit(self, message, schema):
        self.asked.append(("form", message, schema))
        return self.form_answer

    async def elicit_url(self, *, message, url, elicitation_id):
        self.asked.append(("url", message, url, elicitation_id))
        return self.url_answer


class GateTestCase(unittest.IsolatedAsyncioTestCase):
    def build(self, *, config_api=None, exchange=None):
        server = build_runtime(
            resource=ProtectedResource(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"
            ),
            exchange=exchange or FakeExchange(),
            config_api=config_api or FakeConfigApi(),
        )
        return server.approval_gate

    def as_caller(self):
        token = CURRENT_CALLER.set(Authenticated(
            {"sub": "oauth:grant", "scope": "mcp:connect inspect apply",
             "aud": "http://mcp.localhost/mcp"},
            "mapp_a_live",
        ))
        self.addCleanup(CURRENT_CALLER.reset, token)

    async def gate(self, ctx, *, config_api=None, exchange=None):
        self.as_caller()
        run = self.build(config_api=config_api, exchange=exchange)
        return await run(
            ctx,
            PROPOSALS_SHOW,
            path="/api/proposals/p-1",
            tool_name="proposals_apply",
            packet=PACKET,
        )


class FormModeTests(GateTestCase):
    async def test_a_yes_produces_a_receipt(self) -> None:
        api = FakeConfigApi()
        ctx = FakeContext(
            elicitation=Capability(form={}),
            form=Answer("accept", Confirmation(True)),
        )
        self.assertEqual(RECEIPT, await self.gate(ctx, config_api=api))
        self.assertEqual("form", ctx.asked[0][0])
        self.assertTrue(
            api.posted_to(APPROVALS_CONFIRM["path_template"])[0]
            ["body"]["accepted"]
        )

    async def test_an_empty_elicitation_object_means_form(self) -> None:
        """Claude Code 2.1.276 declares `elicitation: {}`. Reading that as "no
        modes" would send the largest of the three measured clients down the
        dashboard path for no reason."""
        ctx = FakeContext(
            elicitation=Capability(),
            form=Answer("accept", Confirmation(True)),
        )
        self.assertEqual(RECEIPT, await self.gate(ctx))
        self.assertEqual("form", ctx.asked[0][0])

    async def test_declining_stops_everything_and_says_so(self) -> None:
        api = FakeConfigApi()
        ctx = FakeContext(
            elicitation=Capability(form={}),
            form=Answer("decline"),
        )
        with self.assertRaises(ToolError) as raised:
            await self.gate(ctx, config_api=api)
        self.assertIn("declined", str(raised.exception))
        self.assertEqual(
            [], api.posted_to(APPROVALS_CLAIM["path_template"]),
            "a declined request must not go on to claim a receipt",
        )

    async def test_a_decline_is_recorded_rather_than_left_pending(self) -> None:
        """A row left pending stays decidable by somebody else afterwards, and
        the person's no never reaches the audit trail."""
        api = FakeConfigApi()
        ctx = FakeContext(
            elicitation=Capability(form={}),
            form=Answer("decline"),
        )
        with self.assertRaises(ToolError):
            await self.gate(ctx, config_api=api)
        confirmed = api.posted_to(APPROVALS_CONFIRM["path_template"])
        self.assertEqual(1, len(confirmed))
        self.assertIs(False, confirmed[0]["body"]["accepted"])

    async def test_accepting_the_prompt_while_answering_no_is_a_no(self) -> None:
        """A client can render the form, have the person accept the dialog and
        still carry `approve: false`. The dialog is not the answer."""
        ctx = FakeContext(
            elicitation=Capability(form={}),
            form=Answer("accept", Confirmation(False)),
        )
        with self.assertRaises(ToolError) as raised:
            await self.gate(ctx)
        self.assertIn("declined", str(raised.exception))

    async def test_a_cancelled_prompt_is_not_an_approval(self) -> None:
        ctx = FakeContext(
            elicitation=Capability(form={}),
            form=Answer("cancel"),
        )
        with self.assertRaises(ToolError):
            await self.gate(ctx)


class UrlModeTests(GateTestCase):
    async def test_the_person_is_sent_to_the_page_for_this_request(self) -> None:
        ctx = FakeContext(
            elicitation=Capability(form={}, url={}),
            url=Answer("accept"),
        )
        self.assertEqual(RECEIPT, await self.gate(ctx))
        mode, _message, url, _id = ctx.asked[0]
        self.assertEqual("url", mode)
        self.assertEqual(APPROVAL_URL, url)

    async def test_url_is_preferred_when_the_client_offers_both(self) -> None:
        """The decision is then made in a browser session the agent does not
        control, looking at the rendered evidence rather than a summary."""
        ctx = FakeContext(
            elicitation=Capability(form={}, url={}),
            url=Answer("accept"),
            form=Answer("accept", Confirmation(True)),
        )
        await self.gate(ctx)
        self.assertEqual(["url"], [asked[0] for asked in ctx.asked])

    async def test_it_waits_for_a_decision_that_has_not_been_made(self) -> None:
        api = FakeConfigApi(statuses=[
            {"status": "pending"},
            {"status": "approved", "receipt": RECEIPT},
        ])
        ctx = FakeContext(
            elicitation=Capability(url={}), url=Answer("accept"),
        )
        self.assertEqual(RECEIPT, await self.gate(ctx, config_api=api))
        self.assertEqual(
            2, len(api.posted_to(APPROVALS_CLAIM["path_template"]))
        )

    async def test_the_client_is_told_the_out_of_band_step_is_over(self) -> None:
        ctx = FakeContext(
            elicitation=Capability(url={}), url=Answer("accept"),
        )
        await self.gate(ctx)
        self.assertEqual(["d" * 64], ctx.session.completed)

    async def test_refusing_to_open_the_page_is_a_decline(self) -> None:
        api = FakeConfigApi()
        ctx = FakeContext(
            elicitation=Capability(url={}), url=Answer("decline"),
        )
        with self.assertRaises(ToolError):
            await self.gate(ctx, config_api=api)
        self.assertEqual([], api.posted_to(APPROVALS_CLAIM["path_template"]))

    async def test_a_declined_request_is_reported_as_a_decision(self) -> None:
        api = FakeConfigApi(statuses=[{"status": "declined", "receipt": None}])
        ctx = FakeContext(
            elicitation=Capability(url={}), url=Answer("accept"),
        )
        with self.assertRaises(ToolError) as raised:
            await self.gate(ctx, config_api=api)
        self.assertIn("declined", str(raised.exception))

    async def test_an_expired_request_says_so_rather_than_failing(self) -> None:
        api = FakeConfigApi(statuses=[{"status": "expired"}])
        ctx = FakeContext(
            elicitation=Capability(url={}), url=Answer("accept"),
        )
        with self.assertRaises(ToolError) as raised:
            await self.gate(ctx, config_api=api)
        self.assertIn("expired", str(raised.exception))


    async def test_nobody_deciding_is_reported_with_where_to_go(self) -> None:
        """A call that waits forever shows as running for the life of the
        session, and the person cannot tell a slow decision from a dead
        server."""
        import runtime

        original = runtime.APPROVAL_WAIT_SECONDS
        runtime.APPROVAL_WAIT_SECONDS = 0
        self.addCleanup(setattr, runtime, "APPROVAL_WAIT_SECONDS", original)
        api = FakeConfigApi(statuses=[{"status": "pending"}])
        ctx = FakeContext(
            elicitation=Capability(url={}), url=Answer("accept"),
        )
        with self.assertRaises(ToolError) as raised:
            await self.gate(ctx, config_api=api)
        self.assertIn(APPROVAL_URL, str(raised.exception))
        self.assertIn("still waiting", str(raised.exception))

    async def test_an_approved_request_with_no_receipt_does_not_proceed(
        self,
    ) -> None:
        """The platform mints a receipt once, so a second claim answers
        approved with nothing spendable. Proceeding on that would mean making
        the call without the permission it needs."""
        api = FakeConfigApi(statuses=[{"status": "approved", "receipt": None}])
        ctx = FakeContext(
            elicitation=Capability(url={}), url=Answer("accept"),
        )
        with self.assertRaises(ToolError) as raised:
            await self.gate(ctx, config_api=api)
        self.assertIn("no receipt", str(raised.exception))


class NoElicitationTests(GateTestCase):
    async def test_a_client_that_cannot_ask_is_refused_with_somewhere_to_go(
        self,
    ) -> None:
        """Gemini CLI 0.58.0 declares no elicitation. Waiting on a page nobody
        has been told to open is indistinguishable from a broken server."""
        ctx = FakeContext(elicitation=None)
        with self.assertRaises(ToolError) as raised:
            await self.gate(ctx)
        self.assertIn(APPROVAL_URL, str(raised.exception))
        self.assertEqual([], ctx.asked)


class BindingTests(GateTestCase):
    async def test_the_approval_is_bound_to_the_request_it_is_for(self) -> None:
        """The digest is what the platform matches on when the receipt is
        spent, so an approval for one request cannot buy another."""
        api = FakeConfigApi()
        exchange = FakeExchange()
        ctx = FakeContext(
            elicitation=Capability(form={}),
            form=Answer("accept", Confirmation(True)),
        )
        await self.gate(ctx, config_api=api, exchange=exchange)
        self.assertEqual(
            {
                "operation_id": PROPOSALS_SHOW["operation_id"],
                "method": PROPOSALS_SHOW["method"],
                "path_template": PROPOSALS_SHOW["path_template"],
                "path": "/api/proposals/p-1",
                "query": "",
                "body": None,
            },
            exchange.digests[0],
        )
        created = api.posted_to(APPROVALS_CREATE["path_template"])[0]["body"]
        self.assertEqual("d" * 64, created["requestDigest"])
        self.assertEqual(PROPOSALS_SHOW["operation_id"], created["operationId"])
        self.assertEqual("proposals_apply", created["tool"])

    async def test_no_receipt_ever_travels_as_a_tool_argument(self) -> None:
        """The receipt is returned to the caller inside this process and put in
        a header by the client. It is never something the model composes, and
        never something it sees."""
        source = (Path(__file__).resolve().parents[1] / "runtime.py").read_text()
        self.assertNotIn("receipt: str", source,
                         "no tool may take a receipt as a parameter")


class MessageTests(unittest.TestCase):
    """What the person is shown, composed from the packet rather than freely."""

    def test_it_names_the_operation_and_the_scale(self) -> None:
        self.assertEqual(
            "MAPP wants to run proposals.apply."
            " Rename the passport layer folder. It changes 112 things.",
            _approval_message("proposals.apply", PACKET),
        )

    def test_one_change_is_not_pluralised(self) -> None:
        self.assertIn(
            "changes 1 thing.",
            _approval_message("proposals.apply", {"changeCount": 1}),
        )

    def test_an_empty_packet_still_names_the_operation(self) -> None:
        self.assertEqual(
            "MAPP wants to run proposals.apply.",
            _approval_message("proposals.apply", {}),
        )


class CapabilityTests(unittest.TestCase):
    """The three shipped clients, measured on 2026-09-18."""

    def check(self, elicitation):
        return _elicitation_modes(FakeContext(elicitation=elicitation))

    def test_codex_offers_both(self) -> None:
        self.assertEqual(
            frozenset({"form", "url"}),
            self.check(Capability(form={}, url={})),
        )

    def test_claude_code_offers_the_bare_object(self) -> None:
        self.assertEqual(frozenset({"form"}), self.check(Capability()))

    def test_gemini_offers_none(self) -> None:
        self.assertEqual(frozenset(), self.check(None))

    def test_a_client_that_declared_nothing_at_all_offers_none(self) -> None:
        class Bare:
            session = None

        self.assertEqual(frozenset(), _elicitation_modes(Bare()))


class GatedToolTests(unittest.TestCase):
    """A tool whose operation needs permission must ask for it.

    Vacuous today and deliberately written anyway: wave 6 adds the first tools
    that spend an approval, and the plan calls that wave "mostly the join". A
    join is exactly the kind of thing that gets made in one place and
    forgotten in the second, and the forgotten case is a tool that mutates
    without asking anybody -- which nothing else here would notice.
    """

    def test_every_tool_that_needs_approval_goes_through_the_gate(self) -> None:
        import re
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "config-ui")
        )
        import control_api

        source = (Path(__file__).resolve().parents[1] / "runtime.py").read_text()
        ungated = []
        for block in re.split(r"\n    @tool\(", source)[1:]:
            siblings = list(re.finditer(r"\n    (?:async )?def ", block))
            if len(siblings) > 1:
                block = block[: siblings[1].start()]
            name = re.search(r'name="([a-z_]+)"', block)
            spent = re.search(r"spend\(\s*([A-Z_]+)[,)]", block)
            if not (name and spent):
                continue
            descriptor = getattr(
                __import__("runtime"), spent.group(1), None
            )
            if descriptor is None:
                continue
            if not control_api.requires_approval(descriptor["operation_id"]):
                continue
            if "approval_gate(" not in block:
                ungated.append((name.group(1), descriptor["operation_id"]))
        self.assertEqual(
            [],
            ungated,
            "these tools spend an operation that requires approval without"
            " asking anybody for it",
        )


class NotAToolTests(unittest.TestCase):
    """The gate must stay unreachable from the model.

    A tool for `approvals.confirm` would let the model grant its own
    permission, which is the one failure that makes the whole mechanism
    decorative. `approvals.create` and `approvals.claim` are pinned with it
    because a model that can ask and collect on its own initiative is a model
    running the loop rather than being gated by it.
    """

    GATE_ONLY = ("approvals.create", "approvals.claim", "approvals.confirm")

    def test_no_tool_is_registered_for_a_gate_operation(self) -> None:
        server = build_runtime(
            resource=ProtectedResource(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"
            ),
            exchange=FakeExchange(),
            config_api=FakeConfigApi(),
        )
        source = (Path(__file__).resolve().parents[1] / "runtime.py").read_text()
        for name in server._tool_manager._tools:
            # The declared operation of each registered tool, read from the
            # source the registration uses rather than from a restated list.
            block = source.split(f'name="{name}"')[0].rsplit("    @tool(", 1)[-1]
            for operation in self.GATE_ONLY:
                self.assertNotIn(
                    operation, block,
                    f"{name} is registered against {operation}",
                )

    def test_the_gate_operations_are_not_in_the_tool_scope_table(self) -> None:
        """The table the listing filter reads. Anything in it is a tool."""
        server = build_runtime(
            resource=ProtectedResource(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"
            ),
            exchange=FakeExchange(),
            config_api=FakeConfigApi(),
        )
        for name in self.GATE_ONLY:
            self.assertNotIn(name.replace(".", "_"), server.tool_scopes)


if __name__ == "__main__":
    unittest.main()
