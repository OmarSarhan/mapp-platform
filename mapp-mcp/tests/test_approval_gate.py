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
from runtime import PROPOSALS_APPLY  # noqa: E402
from runtime import XYZ_RELOAD  # noqa: E402
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


class StandingWindowTests(GateTestCase):
    """P8 from this side: when a window decided it, nobody is asked.

    The runtime does not know what a window is and should not. It creates the
    same intent it always creates; the platform either decides it or does not,
    and the one bit that comes back says which. Everything a test here pins is
    about not asking -- because the failure that matters is a tool that
    prompts anyway, which would make the feature pointless, or one that
    proceeds without a receipt, which would make it dangerous.
    """

    def decided_api(self, **over):
        api = FakeConfigApi()
        original = api.post

        def post(**kwargs):
            answer = original(**kwargs)
            if kwargs["path"] == APPROVALS_CREATE["path_template"]:
                return {**answer, "decided": True, "window": "w" * 32, **over}
            return answer

        api.post = post
        return api

    async def test_nobody_is_asked(self) -> None:
        api = self.decided_api()
        ctx = FakeContext(
            elicitation=Capability(form={}),
            form=Answer("accept", Confirmation(True)),
        )
        self.assertEqual(RECEIPT, await self.gate(ctx, config_api=api))
        self.assertEqual(
            [], ctx.asked,
            "a window decided this; prompting anyway defeats the point",
        )

    async def test_a_client_that_cannot_elicit_is_not_sent_anywhere(
        self,
    ) -> None:
        """The dashboard path is for a decision nobody has made. This one has
        been made, so a refusal naming a page would be wrong twice."""
        api = self.decided_api()
        self.assertEqual(
            RECEIPT,
            await self.gate(FakeContext(elicitation=None), config_api=api),
        )

    async def test_the_receipt_is_still_claimed_and_spent(self) -> None:
        """A window substitutes the decider, never the receipt."""
        api = self.decided_api()
        await self.gate(FakeContext(elicitation=None), config_api=api)
        self.assertEqual(
            1, len(api.posted_to(APPROVALS_CLAIM["path_template"])),
            "an auto-decided approval must still claim its receipt",
        )

    async def test_a_decision_that_produced_no_receipt_does_not_proceed(
        self,
    ) -> None:
        """Fail closed. "Decided" without a spendable receipt is not
        permission, and proceeding on it would be acting unapproved."""
        api = FakeConfigApi(statuses=[{"status": "approved", "receipt": None}])
        original = api.post

        def post(**kwargs):
            answer = original(**kwargs)
            if kwargs["path"] == APPROVALS_CREATE["path_template"]:
                return {**answer, "decided": True}
            return answer

        api.post = post
        with self.assertRaises(ToolError) as raised:
            await self.gate(FakeContext(elicitation=None), config_api=api)
        self.assertIn("no receipt", str(raised.exception))

    async def test_an_undecided_intent_still_asks(self) -> None:
        """The flag is read, not assumed. Without this the test above would
        pass on a gate that never asked anybody at all."""
        # The default fake reports no `decided`, and answers the later claim
        # with an approved receipt so the form path runs to completion -- what
        # is asserted is that somebody was asked, not what they said.
        api = FakeConfigApi()
        ctx = FakeContext(
            elicitation=Capability(form={}),
            form=Answer("accept", Confirmation(True)),
        )
        self.assertEqual(RECEIPT, await self.gate(ctx, config_api=api))
        self.assertEqual(1, len(ctx.asked))


class TwoCallFlowTests(GateTestCase):
    """The path every shipped client actually takes.

    No client can be elicited on this transport, so the working flow is: ask,
    tell the person where, and pick up their answer on the next call. Which
    means the second call must find the *first* approval. Asking again would
    create a second pending row, the person would have approved the first, and
    the loop could never complete however patient either of them was.
    """

    def build_gate(self, api):
        server = build_runtime(
            resource=ProtectedResource(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"
            ),
            exchange=FakeExchange(),
            config_api=api,
        )
        return server.approval_gate

    async def ask(self, gate, ctx, *, grant="oauth:grant"):
        token = CURRENT_CALLER.set(Authenticated(
            {"sub": grant, "scope": "mcp:connect inspect apply",
             "aud": "http://mcp.localhost/mcp"},
            "mapp_a_live",
        ))
        try:
            return await gate(
                ctx, PROPOSALS_SHOW, path="/api/proposals/p-1",
                tool_name="proposals_apply", packet=PACKET,
            )
        finally:
            CURRENT_CALLER.reset(token)

    def mute(self):
        return FakeContext(elicitation=None)

    async def test_the_first_call_asks_and_says_where(self) -> None:
        api = FakeConfigApi(statuses=[{"status": "pending"}])
        gate = self.build_gate(api)
        with self.assertRaises(ToolError) as raised:
            await self.ask(gate, self.mute())
        self.assertIn(APPROVAL_URL, str(raised.exception))
        self.assertIn("Rename the passport layer folder.",
                      str(raised.exception))
        self.assertEqual(
            1, len(api.posted_to(APPROVALS_CREATE["path_template"]))
        )

    async def test_the_second_call_collects_the_answer(self) -> None:
        # Only the second call claims: the first has nothing to collect.
        api = FakeConfigApi(statuses=[{"status": "approved",
                                       "receipt": RECEIPT}])
        gate = self.build_gate(api)
        with self.assertRaises(ToolError):
            await self.ask(gate, self.mute())
        self.assertEqual(RECEIPT, await self.ask(gate, self.mute()))
        self.assertEqual(
            1, len(api.posted_to(APPROVALS_CREATE["path_template"])),
            "the second call must not ask for a second approval",
        )

    async def test_a_third_call_asks_afresh_once_the_answer_is_spent(
        self,
    ) -> None:
        """The receipt is minted once, so holding the handle after spending it
        would make every later attempt fail on a used approval."""
        api = FakeConfigApi(statuses=[{"status": "approved",
                                       "receipt": RECEIPT}])
        gate = self.build_gate(api)
        with self.assertRaises(ToolError):
            await self.ask(gate, self.mute())
        await self.ask(gate, self.mute())
        with self.assertRaises(ToolError):
            await self.ask(gate, self.mute())
        self.assertEqual(
            2, len(api.posted_to(APPROVALS_CREATE["path_template"]))
        )

    async def test_one_grants_approval_is_not_another_grants(self) -> None:
        """Keyed by the grant the approval was asked under. A shared key would
        hand one consent's answer to a different consent."""
        api = FakeConfigApi(statuses=[{"status": "pending"}])
        gate = self.build_gate(api)
        with self.assertRaises(ToolError):
            await self.ask(gate, self.mute(), grant="oauth:one")
        with self.assertRaises(ToolError):
            await self.ask(gate, self.mute(), grant="oauth:two")
        self.assertEqual(
            2, len(api.posted_to(APPROVALS_CREATE["path_template"])),
            "a second grant must ask for its own approval",
        )

    async def test_a_decline_made_in_the_dashboard_ends_it(self) -> None:
        api = FakeConfigApi(statuses=[{"status": "declined",
                                       "receipt": None}])
        gate = self.build_gate(api)
        with self.assertRaises(ToolError):
            await self.ask(gate, self.mute())
        with self.assertRaises(ToolError) as raised:
            await self.ask(gate, self.mute())
        self.assertIn("declined", str(raised.exception))

    async def test_what_is_remembered_is_bounded(self) -> None:
        """An agent decides how often to ask, so this is the one structure
        here that grows on its say-so."""
        import runtime

        api = FakeConfigApi(statuses=[{"status": "pending"}])
        server = build_runtime(
            resource=ProtectedResource(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"
            ),
            exchange=FakeExchange(),
            config_api=api,
        )
        original = runtime.APPROVAL_MEMORY_LIMIT
        runtime.APPROVAL_MEMORY_LIMIT = 3
        self.addCleanup(
            setattr, runtime, "APPROVAL_MEMORY_LIMIT", original
        )
        for index in range(6):
            with self.assertRaises(ToolError):
                token = CURRENT_CALLER.set(Authenticated(
                    {"sub": f"oauth:{index}", "scope": "mcp:connect inspect",
                     "aud": "http://mcp.localhost/mcp"},
                    "mapp_a_live",
                ))
                try:
                    await server.approval_gate(
                        self.mute(), PROPOSALS_SHOW,
                        path="/api/proposals/p-1",
                        tool_name="proposals_apply", packet=PACKET,
                    )
                finally:
                    CURRENT_CALLER.reset(token)
        self.assertEqual(3, len(server.remembered_approvals))


class ElicitationRequiresASessionTests(unittest.TestCase):
    """Why this server holds sessions, pinned where somebody will look.

    Until Phase 1 wave 7 the runtime was stateless and `era_guard` obligation
    4 forbade minting a session identifier. Under that arrangement elicitation
    could not run at all: neither served era carried a server-initiated
    request. Forcing the capability on and retrying produced, verbatim,
    "Cannot send 'elicitation/create': this transport context has no
    back-channel for server-initiated requests" -- so every approval went to a
    dashboard, over two tool calls.

    Sessions were enabled to close that, which cost the obligation. These pin
    the two halves of the trade, because both are the kind of thing a later
    change would undo without noticing: turning `stateless_http` back on to
    "reduce state" would silently return every approval to the dashboard, and
    reinstating the strip would leave clients holding a session the server had
    been told to forget.

    What the obligation was worth is the part most easily overread: it never
    enforced the era decision. `test_era_guard` still holds the served set,
    and `CrossSessionElicitationTests` holds the property that actually
    matters here -- one session cannot answer another's prompt.
    """

    def test_the_runtime_keeps_sessions(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "runtime.py").read_text()
        self.assertIn("stateless_http=False", source)

    def test_the_guard_no_longer_strips_the_session_header(self) -> None:
        """A strip with sessions on is worse than either arrangement alone:
        the server issues an identifier and then removes it from the response,
        so every request after the handshake is refused."""
        guard = (Path(__file__).resolve().parents[1] / "era_guard.py").read_text()
        self.assertNotIn("_strip_session", guard)

    def test_the_trade_is_recorded_where_the_obligation_was(self) -> None:
        """The obligation is stated in the guard's own docstring, which is
        where somebody checks what this server promises."""
        guard = (Path(__file__).resolve().parents[1] / "era_guard.py").read_text()
        self.assertIn("Withdrawn at Phase 1 wave 7", guard)
        self.assertIn("no back-channel for server-initiated", guard)


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


class ApplyToolTests(unittest.IsolatedAsyncioTestCase):
    """The tools that change what the map serves.

    Driven through the registered functions with the platform stubbed, because
    what is this tool's own judgement is: what it shows the person before
    asking, what it refuses before asking anybody, and how it reports an apply
    that committed while the reload did not.
    """

    PROPOSAL = {
        "proposal": {
            "id": "p-1",
            "status": "pending",
            "explanation": "Rename the passport layer folder.",
            "originalRevision": "r-1", "candidateHash": "a" * 64,
            "diff": [
                {"op": "replace", "path": "/locale/layers/A/group",
                 "old": "Old", "value": "New"},
                {"op": "replace", "path": "/locale/layers/B/group",
                 "old": "Old", "value": "New"},
            ],
            "warnings": [{"ruleId": "layer.group", "message": "check me"}],
        },
        "revision": "r-1",
    }

    APPLIED = {
        "proposal": {"id": "p-1", "status": "applied",
                     "appliedRevision": "r-2"},
        "reload": {"status": {"completed": True}},
        "operation": {"id": "op-9"},
    }

    class Api:
        def __init__(self, outer, *, applied=None, proposal=None) -> None:
            self.outer = outer
            self.applied = applied if applied is not None else outer.APPLIED
            self.proposal = proposal if proposal is not None else outer.PROPOSAL
            self.calls = []

        def get(self, **kwargs):
            self.calls.append({"method": "GET", **kwargs})
            if "/proposals/" in kwargs["path"]:
                return self.proposal
            if kwargs["path"].startswith("/api/visual-operations/"):
                return {"review": {"eligible": True, "complete": True, "binding": {"proposalId": self.proposal["proposal"]["id"], "candidateHash": "a" * 64, "originalRevision": "r-1", "evidenceOperationId": "op-7", "evidenceFingerprint": "b" * 64}, "captures": []}, "operation": {"result": {"visual": {"passed": True,
                    "diagnosis": {"candidate": {"checks": [
                        {"id": "visual.layer_activation", "passed": False},
                    ]}},
                    "artifacts": {"afterMap": "run/after.png"},
                }}}}
            return {}

        def post(self, **kwargs):
            self.calls.append({"method": "POST", **kwargs})
            path = kwargs["path"]
            if path == APPROVALS_CREATE["path_template"]:
                return {"handle": HANDLE, "reference": "a" * 64,
                        "approvalUrl": APPROVAL_URL}
            if path == APPROVALS_CLAIM["path_template"]:
                return {"status": "approved", "receipt": RECEIPT}
            if path == APPROVALS_CONFIRM["path_template"]:
                return {"decided": True}
            return self.applied

        def posted_to(self, path):
            return [c for c in self.calls
                    if c["method"] == "POST" and c["path"] == path]

    #: What the `authoring-apply` preset grants, so these exercise the grant
    #: an operator can actually issue rather than an invented one.
    APPLYING = ("mcp:connect inspect derive semantic:inspect propose"
                " semantic:propose visual apply semantic:apply reload")

    def build(self, name, api, scopes=None):
        server = build_runtime(
            resource=ProtectedResource(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"
            ),
            exchange=FakeExchange(),
            config_api=api,
        )
        token = CURRENT_CALLER.set(Authenticated(
            {"sub": "oauth:grant",
             "scope": scopes or self.APPLYING,
             "aud": "http://mcp.localhost/mcp"},
            "mapp_a_live",
        ))
        self.addCleanup(CURRENT_CALLER.reset, token)
        return server._tool_manager._tools[name].fn

    def agreeing(self):
        return FakeContext(
            elicitation=Capability(form={}),
            form=Answer("accept", Confirmation(True)),
        )

    async def test_preview_mismatch_and_partial_evidence_do_not_request_approval(self):
        for mutation in ("binding", "incomplete", "expired"):
            api = self.Api(self)
            original_get = api.get
            def get(**kwargs):
                result = original_get(**kwargs)
                if "review" in result:
                    if mutation == "binding": result["review"]["binding"]["candidateHash"] = "c" * 64
                    if mutation == "incomplete": result["review"]["complete"] = False
                    if mutation == "expired": result["review"]["eligible"] = False
                return result
            api.get = get
            apply = self.build("proposals_apply", api)
            with self.subTest(mutation=mutation), self.assertRaises(ToolError):
                await apply(self.agreeing(), "p-1", "op-7")
            self.assertFalse(api.posted_to(APPROVALS_CREATE["path_template"]))

    async def test_partial_evidence_acknowledgment_is_in_the_confirmed_body(self):
        api = self.Api(self)
        original_get = api.get
        def get(**kwargs):
            result = original_get(**kwargs)
            if "review" in result: result["review"]["complete"] = False
            return result
        api.get = get
        apply = self.build("proposals_apply", api)
        await apply(self.agreeing(), "p-1", "op-7", acknowledge_incomplete_preview=True)
        self.assertTrue(api.posted_to("/api/proposals/p-1/apply")[0]["body"]["acknowledgeIncompletePreview"])

    async def test_the_person_is_shown_the_diff_not_the_arguments(self) -> None:
        """A summary assembled from what the model passed in would let the
        agent describe its own change."""
        api = self.Api(self)
        apply = self.build("proposals_apply", api)
        await apply(self.agreeing(), "p-1", "op-7")
        packet = api.posted_to(APPROVALS_CREATE["path_template"])[0]["body"]["packet"]
        self.assertEqual("Rename the passport layer folder.", packet["summary"])
        self.assertEqual(2, packet["changeCount"])
        self.assertEqual(
            ["/locale/layers/A/group", "/locale/layers/B/group"],
            [change["path"] for change in packet["changes"]],
        )
        self.assertEqual("r-1", packet["originalRevision"])

    async def test_warnings_reach_the_person_deciding(self) -> None:
        api = self.Api(self)
        apply = self.build("proposals_apply", api)
        await apply(self.agreeing(), "p-1", "op-7")
        packet = api.posted_to(APPROVALS_CREATE["path_template"])[0]["body"]["packet"]
        self.assertEqual(
            [{"ruleId": "layer.group", "message": "check me"}],
            packet["warnings"],
        )

    async def test_apply_approval_discloses_permanent_draft_promotion(self) -> None:
        binding = [{"name": "preview_metric", "assetId": "asset-id", "generation": 1}]
        proposal = {**self.PROPOSAL, "proposal": {
            **self.PROPOSAL["proposal"], "draftRelations": binding,
        }}
        api = self.Api(self, proposal=proposal)
        apply = self.build("proposals_apply", api)
        await apply(self.agreeing(), "p-1", "op-7")
        packet = api.posted_to(APPROVALS_CREATE["path_template"])[0]["body"]["packet"]
        self.assertEqual(binding, packet["draftRelations"])
        self.assertIn("permanently", packet["note"])

    async def test_evidence_is_read_from_the_platform_not_taken_on_trust(
        self,
    ) -> None:
        """The agent names which run to show; what that run found comes from
        the platform, so an agent cannot report a pass that did not happen."""
        api = self.Api(self)
        apply = self.build("proposals_apply", api)
        await apply(self.agreeing(), "p-1", "op-7")
        packet = api.posted_to(APPROVALS_CREATE["path_template"])[0]["body"]["packet"]
        self.assertTrue(packet["evidence"]["passed"])
        self.assertEqual(
            {"afterMap": "run/after.png"}, packet["evidence"]["artifacts"]
        )

    async def test_evidence_that_cannot_be_read_says_so_rather_than_vanishing(
        self,
    ) -> None:
        """Reading visual evidence costs `visual`, which a hand-picked grant may
        not carry. Refusing the apply over its illustration would refuse the
        wrong thing; dropping it quietly would let the person believe no
        render was asked for."""
        api = self.Api(self)
        apply = self.build(
            "proposals_apply", api, scopes="mcp:connect inspect apply",
        )
        with self.assertRaisesRegex(ToolError, "Visual permission is required"):
            await apply(self.agreeing(), "p-1", "op-7")
        self.assertEqual([], api.posted_to(APPROVALS_CREATE["path_template"]))

    async def test_no_evidence_is_asked_for_when_none_is_named(self) -> None:
        api = self.Api(self)
        apply = self.build("proposals_apply", api)
        with self.assertRaisesRegex(ToolError, "preview is required"):
            await apply(self.agreeing(), "p-1")
        self.assertEqual(
            [], [c for c in api.calls if "/api/visual-operations/" in c["path"]]
        )

    async def test_a_proposal_that_is_not_pending_is_refused_before_asking(
        self,
    ) -> None:
        """A person asked to approve an applied proposal would be agreeing to
        nothing, and the platform would refuse afterwards anyway -- with a
        prompt already spent."""
        api = self.Api(self, proposal={
            "proposal": {"id": "p-1", "status": "applied"}, "revision": "r-2",
        })
        apply = self.build("proposals_apply", api)
        ctx = self.agreeing()
        with self.assertRaises(ToolError) as raised:
            await apply(ctx, "p-1", "op-7")
        self.assertIn("applied", str(raised.exception))
        self.assertEqual([], ctx.asked)
        self.assertEqual(
            [], api.posted_to(APPROVALS_CREATE["path_template"])
        )

    async def test_declining_applies_nothing(self) -> None:
        api = self.Api(self)
        apply = self.build("proposals_apply", api)
        ctx = FakeContext(
            elicitation=Capability(form={}), form=Answer("decline"),
        )
        with self.assertRaises(ToolError):
            await apply(ctx, "p-1", "op-7")
        self.assertEqual(
            [], api.posted_to("/api/proposals/p-1/apply"),
            "a declined apply must not reach the platform",
        )

    async def test_the_request_approved_is_the_request_made(self) -> None:
        """One body object, digested for the approval and sent with the
        credential, so the two cannot describe different requests."""
        api = self.Api(self)
        apply = self.build("proposals_apply", api)
        await apply(self.agreeing(), "p-1", "op-7")
        applied = api.posted_to("/api/proposals/p-1/apply")
        self.assertEqual(1, len(applied))
        self.assertEqual({"approved": True, "candidateHash": "a" * 64, "originalRevision": "r-1", "evidenceOperationId": "op-7", "evidenceFingerprint": "b" * 64, "acknowledgeIncompletePreview": False}, applied[0]["body"])
        self.assertEqual(RECEIPT, applied[0]["receipt"])

    async def test_a_successful_apply_reports_what_happened(self) -> None:
        api = self.Api(self)
        apply = self.build("proposals_apply", api)
        result = await apply(self.agreeing(), "p-1", "op-7")
        self.assertTrue(result["applied"])
        self.assertTrue(result["mapReloaded"])
        self.assertEqual("r-2", result["appliedRevision"])
        self.assertEqual("op-9", result["operationId"])
        self.assertNotIn("note", result)

    async def test_a_committed_apply_whose_reload_lagged_says_do_not_retry(
        self,
    ) -> None:
        """Two facts with different consequences. Collapsing them into one
        boolean invites a second apply of a change that already happened."""
        api = self.Api(self, applied={
            "proposal": {"id": "p-1", "status": "applied",
                         "appliedRevision": "r-2"},
            "reload": {"status": {"completed": False},
                       "error": "Reload coordination failed: TimeoutError"},
            "operation": {"id": "op-9"},
        })
        apply = self.build("proposals_apply", api)
        result = await apply(self.agreeing(), "p-1", "op-7")
        self.assertTrue(result["applied"])
        self.assertFalse(result["mapReloaded"])
        self.assertIn("Do not apply again", result["note"])
        self.assertIn("xyz_reload", result["note"])
        self.assertIn("TimeoutError", result["reloadError"])

    async def test_the_504_carrying_the_result_is_not_read_as_a_refusal(
        self,
    ) -> None:
        """The platform answers 504 with the whole result when the workspace
        was written and the reload was not observed. Reducing that to a
        refusal would tell an agent to retry a change that happened."""
        self.assertIn(504, PROPOSALS_APPLY["result_statuses"])
        self.assertIn(504, XYZ_RELOAD["result_statuses"])

    async def test_a_semantic_apply_shows_its_own_changes(self) -> None:
        api = self.Api(self, proposal={
            "proposal": {"id": "s-1", "explanation": "Rename a measure.",
                         "changes": [{"op": "replace", "path": "/a"}]},
        })
        apply = self.build("semantic_proposals_apply", api)
        await apply(self.agreeing(), "s-1")
        packet = api.posted_to(APPROVALS_CREATE["path_template"])[0]["body"]["packet"]
        self.assertEqual("Rename a measure.", packet["summary"])
        self.assertEqual(1, packet["changeCount"])
        self.assertEqual(
            {"confirmed": True},
            api.posted_to("/api/semantic/proposals/s-1/apply")[0]["body"],
        )

    async def test_a_reload_asks_before_it_reloads(self) -> None:
        api = self.Api(self, applied={"reload": {"status": {"completed": True}}})
        reload = self.build("xyz_reload", api)
        ctx = self.agreeing()
        await reload(ctx)
        self.assertEqual(1, len(ctx.asked))
        self.assertEqual(
            {"confirmed": True}, api.posted_to("/api/xyz/reload")[0]["body"]
        )
        self.assertEqual(RECEIPT, api.posted_to("/api/xyz/reload")[0]["receipt"])

    async def test_a_proposal_id_is_encoded_into_the_path(self) -> None:
        api = self.Api(self)
        apply = self.build("proposals_apply", api)
        await apply(self.agreeing(), "p/1", "op-7")
        self.assertTrue(
            any(c["path"] == "/api/proposals/p%2F1/apply"
                for c in api.calls),
            [c["path"] for c in api.calls],
        )

    async def test_the_change_window_is_bounded(self) -> None:
        """A person is deciding, not auditing. The panel says how many were
        left out and the proposal holds them all."""
        api = self.Api(self, proposal={
            "proposal": {
                "id": "p-1", "status": "pending", "explanation": "Many.",
                "originalRevision": "r-1", "candidateHash": "a" * 64,
                "diff": [{"op": "replace", "path": f"/a/{n}"}
                         for n in range(50)],
            },
            "revision": "r-1",
        })
        apply = self.build("proposals_apply", api)
        await apply(self.agreeing(), "p-1", "op-7")
        packet = api.posted_to(APPROVALS_CREATE["path_template"])[0]["body"]["packet"]
        self.assertEqual(50, packet["changeCount"])
        self.assertEqual(20, len(packet["changes"]))


class DerivedLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """The derived-layer lifecycle, where the guards are the wave.

    These act on the database directly: no proposal, no queue, no diff to read
    first. So what a person is shown has to be assembled by the tool, and the
    one destructive tool has to refuse before it asks rather than after.
    """

    ENTRY = {
        "name": "census_h3", "kind": "materialized",
        "query": "SELECT 1", "sources": ["source_census.oa"],
        "refreshedAt": "2026-09-01T00:00:00Z",
    }

    class Api:
        def __init__(self, outer, *, dependents=None) -> None:
            self.outer = outer
            self.dependents = dependents or []
            self.calls = []

        def get(self, **kwargs):
            self.calls.append({"method": "GET", **kwargs})
            if kwargs["path"] == "/api/derived-layers":
                return {"derivedLayers": [self.outer.ENTRY]}
            if kwargs["path"] == "/api/dependencies":
                return {"dependencies": self.dependents}
            return {}

        def post(self, **kwargs):
            self.calls.append({"method": "POST", **kwargs})
            path = kwargs["path"]
            if path == APPROVALS_CREATE["path_template"]:
                return {"handle": HANDLE, "reference": "a" * 64,
                        "approvalUrl": APPROVAL_URL}
            if path == APPROVALS_CLAIM["path_template"]:
                return {"status": "approved", "receipt": RECEIPT}
            if path == APPROVALS_CONFIRM["path_template"]:
                return {"decided": True}
            return {"derivedLayer": {"name": "census_h3"}}

        def posted_to(self, path):
            return [c for c in self.calls
                    if c["method"] == "POST" and c["path"] == path]

        def packet(self):
            return self.posted_to(
                APPROVALS_CREATE["path_template"]
            )[0]["body"]["packet"]

    def build(self, name, api):
        server = build_runtime(
            resource=ProtectedResource(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"
            ),
            exchange=FakeExchange(),
            config_api=api,
        )
        token = CURRENT_CALLER.set(Authenticated(
            {"sub": "oauth:grant",
             "scope": "mcp:connect inspect derive semantic:inspect"
                      " derive:manage",
             "aud": "http://mcp.localhost/mcp"},
            "mapp_a_live",
        ))
        self.addCleanup(CURRENT_CALLER.reset, token)
        return server._tool_manager._tools[name].fn

    def agreeing(self):
        return FakeContext(
            elicitation=Capability(form={}),
            form=Answer("accept", Confirmation(True)),
        )

    def reading(self, *, workspace=(), derived=()):
        return [{"alias": "MAPP", "relation": "derived_layers.census_h3",
                 "workspaceLayers": list(workspace),
                 "derivedLayers": list(derived)}]

    async def test_a_drop_with_dependents_is_refused_before_anybody_is_asked(
        self,
    ) -> None:
        """A person prompted to approve something that cannot happen has been
        asked to spend attention on nothing."""
        api = self.Api(self, dependents=self.reading(
            workspace=["locale:Census_OA_Population"]))
        drop = self.build("derived_layers_drop", api)
        ctx = self.agreeing()
        with self.assertRaises(ToolError) as raised:
            await drop(ctx, "census_h3")
        self.assertIn("locale:Census_OA_Population", str(raised.exception))
        self.assertEqual([], ctx.asked)
        self.assertEqual(
            [], api.posted_to(APPROVALS_CREATE["path_template"]),
            "a refused drop must not ask for an approval",
        )

    async def test_another_derived_layer_blocks_a_drop_too(self) -> None:
        api = self.Api(self, dependents=self.reading(derived=["other_h3"]))
        drop = self.build("derived_layers_drop", api)
        with self.assertRaises(ToolError) as raised:
            await drop(self.agreeing(), "census_h3")
        self.assertIn("other_h3", str(raised.exception))

    async def test_a_clean_drop_asks_and_names_what_it_removes(self) -> None:
        api = self.Api(self, dependents=self.reading())
        drop = self.build("derived_layers_drop", api)
        self.assertIsNotNone(await drop(self.agreeing(), "census_h3"))
        packet = api.packet()
        self.assertIn("cannot be undone", packet["summary"])
        self.assertIn("census_h3", packet["summary"])
        self.assertEqual("remove", packet["changes"][0]["op"])
        self.assertIsNone(packet["changes"][0]["becomes"])
        self.assertEqual({"workspaceLayers": [], "derivedLayers": []},
                         packet["dependents"])

    async def test_the_drop_is_bound_to_the_relation_named(self) -> None:
        api = self.Api(self, dependents=self.reading())
        drop = self.build("derived_layers_drop", api)
        await drop(self.agreeing(), "census_h3")
        dropped = api.posted_to("/api/derived-layers/census_h3/drop")
        self.assertEqual(1, len(dropped))
        self.assertEqual({"confirmed": True}, dropped[0]["body"])
        self.assertEqual(RECEIPT, dropped[0]["receipt"])

    async def test_a_name_is_encoded_never_interpreted(self) -> None:
        """Named exactly. No pattern, and nothing the path could reinterpret."""
        api = self.Api(self, dependents=self.reading())
        api.get = lambda **kw: (
            {"derivedLayers": [{**self.ENTRY, "name": "a/b"}]}
            if kw["path"] == "/api/derived-layers"
            else {"dependencies": []}
        )
        drop = self.build("derived_layers_drop", api)
        await drop(self.agreeing(), "a/b")
        self.assertTrue(
            any(c["path"] == "/api/derived-layers/a%2Fb/drop"
                for c in api.calls),
            [c["path"] for c in api.calls],
        )

    async def test_dropping_something_that_is_not_there_says_what_is(
        self,
    ) -> None:
        api = self.Api(self, dependents=self.reading())
        drop = self.build("derived_layers_drop", api)
        with self.assertRaises(ToolError) as raised:
            await drop(self.agreeing(), "no_such_thing")
        self.assertIn("census_h3", str(raised.exception))

    async def test_a_replace_shows_what_it_is_today(self) -> None:
        """A replace is the quiet one: everything reading the relation keeps
        working and starts returning different numbers."""
        api = self.Api(self, dependents=self.reading(
            workspace=["locale:Census_OA_Population"]))
        replace = self.build("derived_layers_replace", api)
        await replace(
            self.agreeing(), "census_h3", "SELECT 2",
            ["source_census.oa"], "oa_id", "geom_3857",
        )
        packet = api.packet()
        self.assertEqual("replace", packet["changes"][0]["op"])
        self.assertEqual("SELECT 1", packet["changes"][0]["was"])
        self.assertEqual("SELECT 2", packet["changes"][0]["becomes"])
        self.assertEqual(["locale:Census_OA_Population"],
                         packet["dependents"]["workspaceLayers"])
        self.assertIn("different numbers", packet["note"])

    async def test_a_replace_with_no_dependents_says_so(self) -> None:
        api = self.Api(self, dependents=self.reading())
        replace = self.build("derived_layers_replace", api)
        await replace(
            self.agreeing(), "census_h3", "SELECT 2",
            ["source_census.oa"], "oa_id", "geom_3857",
        )
        self.assertIn("Nothing else reads", api.packet()["note"])

    async def test_replace_exposes_materialization_conversion_in_approval(self) -> None:
        for target_kind in ("view", "materialized"):
            api = self.Api(self, dependents=self.reading())
            # The fixture starts materialized; use an inspected view for the reverse direction.
            if target_kind == "materialized":
                original_get = api.get
                def get(**kwargs):
                    result = original_get(**kwargs)
                    if kwargs["path"] == "/api/derived-layers":
                        result["derivedLayers"] = [{**entry, "kind": "view"} for entry in result["derivedLayers"]]
                    return result
                api.get = get
            replace = self.build("derived_layers_replace", api)
            await replace(self.agreeing(), "census_h3", "SELECT 1", ["source_census.oa"],
                          "oa_id", "geom_3857", kind=target_kind)
            packet = api.packet()
            self.assertIn("Convert census_h3", packet["summary"])
            self.assertEqual(target_kind, packet["changes"][0]["becomes"])
            mutation = next(call for call in api.calls if call["path"].endswith("/replace"))
            self.assertEqual(target_kind, mutation["body"]["kind"])

    async def test_a_create_carries_the_definition_it_would_write(
        self,
    ) -> None:
        """There is no proposal to read, so the definition is the only
        description of the change that exists."""
        api = self.Api(self)
        create = self.build("derived_layers_create", api)
        await create(
            self.agreeing(), "new_h3", "SELECT 1", ["source_census.oa"],
            "oa_id", "geom_3857",
        )
        packet = api.packet()
        self.assertEqual("create", packet["changes"][0]["op"])
        self.assertEqual(["source_census.oa"],
                         packet["definition"]["sources"])
        self.assertEqual("view", packet["definition"]["kind"])
        self.assertFalse(packet["planned"])
        self.assertIn("persistent database relation", packet["summary"])
        self.assertIn("never applied", packet["summary"])
        self.assertIn("persistent database relation", packet["note"])
        self.assertIn("never applied", packet["note"])
        self.assertIn("separate approval", packet["note"])

    async def test_a_plan_fingerprint_is_carried_and_noted(self) -> None:
        api = self.Api(self)
        create = self.build("derived_layers_create", api)
        await create(
            self.agreeing(), "new_h3", "SELECT 1", ["source_census.oa"],
            "oa_id", "geom_3857", plan_fingerprint="sha256:" + "f" * 64,
        )
        self.assertTrue(api.packet()["planned"])
        created = api.posted_to("/api/derived-layers")[0]["body"]
        self.assertEqual("sha256:" + "f" * 64, created["planFingerprint"])

    async def test_planning_asks_nobody_and_writes_nothing(self) -> None:
        """The dry run. If this ever needs approval, the parallel with
        proposals_check has been broken."""
        api = self.Api(self)
        plan = self.build("derived_layers_plan", api)
        plan("new_h3", "SELECT 1", ["source_census.oa"], "oa_id", "geom_3857")
        self.assertEqual(
            [], api.posted_to(APPROVALS_CREATE["path_template"])
        )
        self.assertEqual(
            1, len(api.posted_to("/api/derived-layers/plan"))
        )

    async def test_a_refresh_asks_and_names_the_sources(self) -> None:
        api = self.Api(self)
        refresh = self.build("derived_layers_refresh", api)
        await refresh(self.agreeing(), "census_h3")
        packet = api.packet()
        self.assertEqual("refresh", packet["changes"][0]["op"])
        self.assertEqual(["source_census.oa"], packet["sources"])
        self.assertEqual(
            {"confirmed": True, "background": True},
            api.posted_to("/api/derived-layers/census_h3/refresh")[0]["body"],
        )

    async def test_declining_a_drop_removes_nothing(self) -> None:
        api = self.Api(self, dependents=self.reading())
        drop = self.build("derived_layers_drop", api)
        ctx = FakeContext(
            elicitation=Capability(form={}), form=Answer("decline"),
        )
        with self.assertRaises(ToolError):
            await drop(ctx, "census_h3")
        self.assertEqual(
            [], api.posted_to("/api/derived-layers/census_h3/drop")
        )


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
