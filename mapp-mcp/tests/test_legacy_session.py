"""A complete 2025-11-25 session through the whole composed stack.

Everything else here drives one layer. This drives the four requests a real
client actually makes -- initialize, notifications/initialized, tools/list,
tools/call -- through the application the factory builds: metadata surface, era
guard, bearer authentication, and the SDK runtime with its real tool registry.
Only the platform behind the tools is replaced.

It exists because the failure that prompted the legacy era being served was
invisible to every unit test in this directory. The guard was correct, the
runtime was correct, authentication was correct, and Claude Code could not
connect, because nothing asserted the sequence a client sends. A test per layer
cannot see a handshake.

The session is driven inside one lifespan, which is also load-bearing: the SDK's
session manager is started by the ASGI lifespan, and a request outside it is not
the request a deployed server would handle.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import era_guard  # noqa: E402
from app import build_app  # noqa: E402
from asgi_harness import StubIntrospection, active  # noqa: E402
from protected_resource import ProtectedResource  # noqa: E402
from runtime import RUNTIME_NAME, RUNTIME_VERSION, build_runtime_app  # noqa: E402

#: Distinguishable from `None`, which is a real choice: send no session
#: identifier at all, as the handshake must.
_UNSET = object()

TOKEN = "mapp_a_session"
ORIGIN = "http://mcp.localhost"
SCOPES = "mcp:connect inspect derive semantic:inspect"


class FakeExchange:
    """Mints a credential without a broker. The binding is proved elsewhere."""

    def __init__(self) -> None:
        self.calls = []

    def exchange(self, **kwargs):
        self.calls.append(kwargs)
        return "mapp_b_minted"

    def request_digest(self, **kwargs):
        """The approval gate digests before it exchanges.

        Absent once, and the omission was invisible: a gated tool crashed
        before any elicitation was sent, and a cross-session test that asked
        "did the call complete" saw both its cases complete identically on the
        crash. A fake missing a method the real one has does not fail loudly;
        it makes the test measure something else.
        """
        return "mapp-jcs-v1:" + "d" * 64


class FakeConfigApi:
    def __init__(self) -> None:
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return {"total": 2, "values": [{"value": "a", "count": 2}]}

    def post(self, **kwargs):
        """Enough of the approval routes for a gated tool to reach its prompt.

        `approvals.create` hands back a handle, and a claim reports a decision
        already made -- so what these tests exercise is the elicitation and its
        routing, not the platform's half, which `test_approvals.py` drives
        against a real database.
        """
        self.calls.append(kwargs)
        if kwargs["path"] == "/api/approvals":
            return {"handle": "h", "reference": "a" * 64,
                    "approvalUrl": "http://config.localhost/#approvals/"
                                   + "a" * 64}
        if kwargs["path"] == "/api/approvals/claim":
            return {"status": "approved", "receipt": "receipt-value"}
        if kwargs["path"] == "/api/approvals/confirm":
            return {"decided": True}
        return {"status": {"completed": True}}


class Session:
    """Requests sharing one lifespan, as a connected client's would.

    It also carries the session identifier back, which a real client does and
    this did not have to until Phase 1 wave 7 -- the server minted none, so
    there was nothing to carry. With sessions on, a client that does not
    return it is answered "Missing session ID" on everything after the
    handshake, which is exactly what a client that forgot would see.
    """

    def __init__(self, app) -> None:
        self._app = app
        self.session_ids = []
        self.session = None

    async def request(self, body, *, version=None, token=TOKEN, method_header=None,
                      session=_UNSET, on_frame=None):
        """`session` overrides what is sent, so a test can present another
        client's identifier or none at all.

        `on_frame` is called with each SSE payload as it arrives rather than
        after the response completes, which is the only way to see a request
        the *server* sends mid-call -- an elicitation is delivered on the
        stream of the call that triggered it, and a test that waits for the
        call to finish waits for the thing it is trying to answer.
        """
        raw = json.dumps(body).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            (b"host", b"mcp.localhost"),
        ]
        carried = self.session if session is _UNSET else session
        if carried is not None:
            headers.append((b"mcp-session-id", carried.encode()))
        if version is not None:
            headers.append((b"mcp-protocol-version", version.encode()))
        if method_header is not None:
            # Modern era only: the SDK requires the header to agree with the
            # body's method before it will look at anything else.
            headers.append((b"mcp-method", method_header.encode()))
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}

        messages, delivered = [], [False]

        async def receive():
            if not delivered[0]:
                delivered[0] = True
                return {"type": "http.request", "body": raw, "more_body": False}
            # Never a fabricated disconnect: the SDK keeps reading to notice a
            # client leaving, and inventing one makes it abandon the response.
            await asyncio.sleep(3600)

        async def send(message):
            messages.append(message)
            if on_frame is None or message["type"] != "http.response.body":
                return
            for line in message.get("body", b"").decode(
                "utf-8", "replace"
            ).splitlines():
                if line.startswith("data: "):
                    on_frame(json.loads(line[6:]))

        await asyncio.wait_for(self._app(scope, receive, send), timeout=20)
        start = next(m for m in messages if m["type"] == "http.response.start")
        headers_out = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in start.get("headers", [])
        }
        minted = headers_out.get("mcp-session-id")
        self.session_ids.append(minted)
        if minted and self.session is None:
            self.session = minted
        body_out = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )
        return start["status"], headers_out, body_out.decode("utf-8", "replace")


def rpc_result(text):
    """The JSON-RPC payload out of an SSE frame, or out of a plain body."""
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(text) if text.strip() else None


def registered_tool_names():
    """The tool names the runtime registers, read from it rather than restated.

    A literal list here needed editing every time a tool was added, which is the
    shape that eventually disagrees with the code for a release. What these
    tests assert is that *what the runtime registers* is what a client is shown
    over each era -- not what somebody remembered to type.
    """
    resource = ProtectedResource(origin=ORIGIN, issuer=ORIGIN)
    from runtime import build_runtime

    server = build_runtime(resource=resource, exchange=FakeExchange(),
                           config_api=FakeConfigApi())
    return sorted(server._tool_manager._tools)


def permitted_tool_names(scopes=SCOPES):
    """The tools a grant carrying `scopes` is entitled to see.

    Derived from the runtime's own registration, like registered_tool_names()
    above and for the same reason. The two differ on purpose: everything stays
    registered and callable-checked, while the listing shows only what this
    credential could actually invoke.
    """
    resource = ProtectedResource(origin=ORIGIN, issuer=ORIGIN)
    from runtime import build_runtime

    server = build_runtime(resource=resource, exchange=FakeExchange(),
                           config_api=FakeConfigApi())
    held = frozenset(scopes.split())
    return sorted(
        name for name in server._tool_manager._tools
        if frozenset(server.tool_scopes.get(name, ())) <= held
    )


def composed(scopes=SCOPES):
    """The shipped stack, with only the platform behind the tools replaced.

    The runtime is handed back alongside the composed application because the
    ASGI lifespan belongs to it, not to the outer wrapper: requests are driven
    through `app`, but the SDK's session manager is started by `runtime`.
    """
    resource = ProtectedResource(origin=ORIGIN, issuer=ORIGIN)
    exchange, config_api = FakeExchange(), FakeConfigApi()
    runtime = build_runtime_app(
        resource=resource, exchange=exchange, config_api=config_api
    )
    app, _ = build_app(
        origin=ORIGIN,
        issuer=ORIGIN,
        inner=runtime,
        introspection=StubIntrospection(
            {TOKEN: active(scopes=scopes, audience=f"{ORIGIN}/mcp")}
        ),
    )
    return app, runtime, exchange, config_api


def run(coro):
    return asyncio.run(coro)


class LegacySessionTests(unittest.TestCase):
    """The sequence a 2025-11-25 client sends, in order, against one server."""

    def drive(self, body_of_call=None):
        app, runtime, exchange, config_api = composed()
        transcript = {}

        async def session():
            async with runtime.router.lifespan_context(runtime):
                client = Session(app)
                V = era_guard.HANDSHAKE_VERSIONS[-1]
                # 1. The handshake, carrying no version header -- there is
                #    nothing it could carry, since this request decides it.
                transcript["initialize"] = await client.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": V,
                            "capabilities": {"roots": {"listChanged": True}},
                            "clientInfo": {"name": "claude-code", "version": "2.1.272"},
                        },
                    }
                )
                # 2..4 declare the negotiated revision, as the spec requires.
                transcript["initialized"] = await client.request(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}, version=V
                )
                transcript["tools/list"] = await client.request(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, version=V
                )
                transcript["tools/call"] = await client.request(
                    body_of_call
                    or {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "describe_instance", "arguments": {}},
                    },
                    version=V,
                )
                transcript["session_ids"] = client.session_ids

        run(session())
        return transcript, exchange, config_api

    def test_the_whole_session_is_served(self) -> None:
        transcript, _, _ = self.drive()
        for step in ("initialize", "tools/list", "tools/call"):
            status, _, body = transcript[step]
            self.assertEqual(200, status, f"{step} answered {status}: {body[:200]}")
            self.assertIsNone(rpc_result(body).get("error"), f"{step}: {body[:200]}")
        self.assertEqual(202, transcript["initialized"][0])

    def test_the_negotiated_revision_is_the_one_admitted(self) -> None:
        """Not merely "a 200": the SDK negotiates whatever it is offered, so a
        session could succeed on a revision this server never agreed to."""
        transcript, _, _ = self.drive()
        result = rpc_result(transcript["initialize"][2])["result"]
        self.assertEqual(era_guard.HANDSHAKE_VERSIONS[-1], result["protocolVersion"])

    def test_a_session_identifier_is_minted_and_kept(self) -> None:
        """The inverse of what this asserted until Phase 1 wave 7.

        It read: no session identifier is ever minted. That was `era_guard`
        obligation 4, and it was traded for in-session approval -- a server
        cannot ask a client anything without a back-channel, and there is no
        back-channel without a session. The obligation never enforced the era
        decision; obligations 1 to 3 do that and are untouched.

        Kept as its inverse rather than deleted, because the day somebody
        wonders why this server holds sessions, a test named for it is where
        the answer should be.
        """
        transcript, _, _ = self.drive()
        minted = [value for value in transcript["session_ids"] if value]
        self.assertTrue(minted, "the handshake minted no session identifier")
        self.assertEqual(
            1, len(set(minted)),
            "one session per client, not one per request",
        )

    def test_the_listed_tools_are_the_ones_this_grant_can_call(self) -> None:
        """What a client is shown is what it may invoke, not everything that
        exists. A grant carrying mcp:connect alone used to be shown all of
        them and could call none."""
        transcript, _, _ = self.drive()
        listed = rpc_result(transcript["tools/list"][2])["result"]["tools"]
        self.assertEqual(
            permitted_tool_names(),
            sorted(t["name"] for t in listed),
        )

    def test_tools_this_grant_cannot_call_are_withheld(self) -> None:
        """The half that matters, stated rather than implied by an equality:
        this grant carries neither federation:observe, semantic:source nor
        either propose scope, and the tools needing them are absent."""
        transcript, _, _ = self.drive()
        listed = {t["name"] for t in
                  rpc_result(transcript["tools/list"][2])["result"]["tools"]}
        withheld = set(registered_tool_names()) - listed
        self.assertEqual(
            {"federation_list", "federation_show", "federation_groups",
             "semantic_source_relations",
             # The authoring surface. This grant is the analysis preset,
             # which reads an instance but cannot author against it: neither
             # the checks that cost a propose scope nor the creates that spend
             # it are shown to it.
             "proposals_check", "semantic_proposals_check",
             "proposals_create", "proposals_decline", "semantic_proposals_create",
             # Evidence tools: they render a proposal through a browser, which
             # costs `visual` and is granted with the authoring preset.
             "proposals_preview_plan", "proposals_preview_screenshot",
             "proposals_preview_test", "visual_operations_show", "artifacts_image",
             # The irreversible half, added in wave 6. An analysis grant
             # reads; these write the workspace, write curated meaning and
             # tell the tile service to serve the result, and each costs a
             # scope an operator grants on purpose.
             "proposals_apply", "semantic_proposals_apply", "xyz_reload",
             # The derived-layer lifecycle, added in wave 7. These act on the
             # database with no proposal behind them, which is why they cost
             # the widest scope an operator can grant.
             "derived_layers_plan", "derived_layers_create",
             "derived_layers_replace", "derived_layers_refresh",
             "derived_layers_drop"},
            withheld,
        )

    def test_a_tool_reaches_the_platform_with_the_caller_s_own_credential(self) -> None:
        """The full join: guard, authentication, contextvar, tool, exchange.

        `CURRENT_CALLER` is set by the authentication middleware, so a tool that
        can name the caller's token proves the credential survived the whole
        traversal -- over an era that did not previously reach the runtime.
        """
        transcript, exchange, config_api = self.drive(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "layers_values",
                    "arguments": {"layer_key": "Census_OA", "field": "quintile"},
                },
            }
        )
        status, _, body = transcript["tools/call"]
        self.assertEqual(200, status)
        self.assertIsNone(rpc_result(body).get("error"), body[:300])
        self.assertEqual(1, len(exchange.calls))
        self.assertEqual(TOKEN, exchange.calls[0]["subject_token"])
        # And the request minted for is the request made.
        self.assertEqual(exchange.calls[0]["path"], config_api.calls[0]["path"])
        self.assertEqual(exchange.calls[0]["query"], config_api.calls[0]["query"])

    def test_the_server_reports_the_era_it_is_actually_speaking(self) -> None:
        """A literal here once said 2026-07-28 to a client connected over
        2025-11-25 -- a server misreporting itself to the one caller asking."""
        transcript, _, _ = self.drive()
        payload = rpc_result(transcript["tools/call"][2])["result"]
        described = json.loads(payload["content"][0]["text"])
        self.assertIn(era_guard.HANDSHAKE_VERSIONS[-1], described["protocolVersions"])
        self.assertEqual(list(era_guard.SERVED_VERSIONS), described["protocolVersions"])
        self.assertEqual(
            f"{RUNTIME_NAME}/{RUNTIME_VERSION}", described["runtime"]
        )


#: The modern era carries per request what the handshake negotiated once. Both
#: keys are required, and the SDK refuses the request outright without them --
#: which is the concrete difference between the two eras, not a version string.
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": era_guard.MODERN_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}


class ModernEraReachabilityTests(unittest.TestCase):
    """A header-less request cannot be served as the modern era.

    This is the argument the guard's relaxation rests on, so it is asserted
    against the real SDK rather than reasoned about. The guard stopped refusing
    header-less requests because Gemini CLI 0.60.0 sends none after its
    handshake; admitting them is only safe if the modern era remains out of
    reach that way, because the modern envelope is what carries per-request
    client capabilities and the header is what binds them.

    The SDK enforces it: the modern ladder requires `params._meta` to carry the
    protocol version and client capabilities, and requires `MCP-Protocol-Version`
    to equal the version in that envelope. No header means nothing to agree with.
    """

    def call(self, body, *, version=None, method_header=None):
        app, runtime, _, _ = composed()

        async def session():
            async with runtime.router.lifespan_context(runtime):
                return await Session(app).request(
                    body, version=version, method_header=method_header
                )

        return run(session())

    def test_a_modern_envelope_without_the_header_is_not_served_as_modern(self) -> None:
        """The exact shape the relaxation lets through the guard. The SDK must
        still refuse to treat it as a modern request."""
        status, _, text = self.call({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": era_guard.MODERN_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
            }},
        })
        payload = rpc_result(text)
        # Served as a handshake request -- which ignores the envelope -- or
        # refused outright. What it must never be is accepted *as modern*, which
        # would mean the envelope was honoured without a header binding it.
        if payload and payload.get("error"):
            self.assertIn(status, (200, 400))
        else:
            self.assertEqual(200, status)
            self.assertIn("result", payload)

    def test_the_modern_era_still_requires_its_envelope(self) -> None:
        """With the header and no envelope, the SDK refuses. This is what makes
        "no header means not modern" true rather than convenient: the two are
        required together."""
        status, _, text = self.call(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            version=era_guard.MODERN_VERSION,
            method_header="tools/list",
        )
        payload = rpc_result(text)
        self.assertIsNotNone(payload.get("error"), "a modern request with no envelope was served")
        self.assertIn("_meta", str(payload["error"]))

    def test_the_modern_era_works_when_both_are_present(self) -> None:
        """The control is "both or neither", not "the envelope is ignored"."""
        status, _, text = self.call(
            {
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
                "params": {"_meta": MODERN_META},
            },
            version=era_guard.MODERN_VERSION,
            method_header="tools/list",
        )
        self.assertEqual(200, status)
        self.assertIsNone(rpc_result(text).get("error"))


class ToolFailureVisibilityTests(unittest.TestCase):
    """What the *client* is given when a tool refuses.

    This is the test that was missing. Every other test of `layers_values` calls
    the registered function and inspects the exception it raises, which is true
    of the function and says nothing about what crosses the wire. The SDK puts a
    `ToolError`'s text into the result and replaces everything else with "Error
    executing tool layer_values" -- so the tool raised `ValueError`, every
    carefully worded refusal was discarded, and the unit tests passed throughout
    because they never went through the SDK.

    Driving a real client against the deployed stack is what exposed it. These
    assertions are on `result.content`, the only place the answer is real.
    """

    def call_with(self, scopes):
        app, runtime, _, _ = composed(scopes=scopes)

        async def session():
            async with runtime.router.lifespan_context(runtime):
                client = Session(app)
                version = era_guard.HANDSHAKE_VERSIONS[-1]
                # The handshake first. It was not needed while the runtime was
                # stateless; with sessions on, a `tools/call` that opens no
                # session is answered "Missing session ID" and never reaches a
                # tool -- which would make this assert the wrong refusal.
                await client.request({
                    "jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {
                        "protocolVersion": version,
                        "capabilities": {},
                        "clientInfo": {"name": "probe", "version": "1"},
                    },
                })
                await client.request(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    version=version,
                )
                return await client.request(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "layers_values",
                            "arguments": {"layer_key": "L", "field": "f"},
                        },
                    },
                    version=version,
                )

        status, _, body = run(session())
        self.assertEqual(200, status, body[:200])
        result = rpc_result(body)["result"]
        return result, " ".join(
            part.get("text", "") for part in result.get("content", [])
        )

    def test_a_scope_refusal_reaches_the_client_with_its_text_intact(self) -> None:
        """The message exists to be acted on, so it has to arrive."""
        result, text = self.call_with("mcp:connect inspect")
        self.assertTrue(result.get("isError"), "a refusal was reported as success")
        # The SDK prefixes both kinds with "Error executing tool <name>". What
        # separates them is what follows: a crash is that string and nothing
        # else, an anticipated failure appends ": " and the real message. So the
        # bare string is the exact signature of the bug.
        self.assertNotEqual(
            "Error executing tool layers_values",
            text.strip(),
            "the SDK discarded the message: the tool raised a type it calls a crash",
        )
        # The two things a caller needs: what is missing, and what to ask for.
        self.assertIn("derive", text)
        self.assertIn("semantic:inspect", text)
        self.assertIn("Re-authorize", text)

    def test_the_refusal_names_only_what_is_actually_missing(self) -> None:
        _, text = self.call_with("mcp:connect inspect derive")
        self.assertIn("semantic:inspect", text)
        self.assertNotIn("does not carry derive", text)


class ModernSessionTests(unittest.TestCase):
    """The modern era still works, and still refuses the handshake.

    Serving a second era is only safe if it did not quietly become the only one
    that works. These are the regression tests for the era that already worked.
    """

    def test_the_modern_era_reaches_the_runtime_without_a_handshake(self) -> None:
        app, runtime, _, _ = composed()

        async def session():
            async with runtime.router.lifespan_context(runtime):
                return await Session(app).request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/list",
                        "params": {"_meta": MODERN_META},
                    },
                    version=era_guard.MODERN_VERSION,
                    method_header="tools/list",
                )

        status, _, body = run(session())
        self.assertEqual(200, status, body[:200])
        listed = rpc_result(body)["result"]["tools"]
        self.assertEqual(
            permitted_tool_names(),
            sorted(t["name"] for t in listed),
        )

    def test_initialize_is_still_not_a_modern_method(self) -> None:
        app, runtime, _, _ = composed()

        async def session():
            async with runtime.router.lifespan_context(runtime):
                return await Session(app).request(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {"protocolVersion": era_guard.HANDSHAKE_VERSIONS[-1]},
                    },
                    version=era_guard.MODERN_VERSION,
                )

        status, _, body = run(session())
        self.assertEqual(400, status)
        self.assertEqual("legacy-initialize", json.loads(body)["error"]["data"]["reason"])


if __name__ == "__main__":
    unittest.main()


class CrossSessionElicitationTests(unittest.TestCase):
    """One session must not be able to answer another's approval prompt.

    This is the property the whole approval mechanism rests on, and the one
    thing holding sessions could plausibly have broken. An agent that could
    answer a prompt meant for somebody else would be approving mutations
    nobody asked it about.

    Measured against the deployed stack before the change landed: a second
    session presenting a valid token and the right request id was acked `202`
    by the transport and never routed, leaving the first call waiting. This
    pins it in process, with a positive control beside it -- a negative result
    from a harness that cannot detect success would prove nothing at all.
    """

    def run_case(self, *, answer_on_victim, mode="form", action="decline"):
        """Drive one eliciting call and answer it from one session or another.

        Returns whether the victim's call completed. The two cases differ in
        exactly one thing: which session posts the answer.
        """
        app, runtime, _, _ = composed(scopes=SCOPES + " reload")
        outcome = {}

        async def case():
            async with runtime.router.lifespan_context(runtime):
                victim, attacker = Session(app), Session(app)
                V = era_guard.HANDSHAKE_VERSIONS[-1]
                for client in (victim, attacker):
                    await client.request({
                        "jsonrpc": "2.0", "id": 0, "method": "initialize",
                        "params": {
                            "protocolVersion": V,
                            # Declaring form elicitation is what makes the
                            # runtime ask rather than send them to a dashboard.
                            "capabilities": {"elicitation": {mode: {}}},
                            "clientInfo": {"name": "probe", "version": "1"},
                        },
                    })
                    await client.request(
                        {"jsonrpc": "2.0",
                         "method": "notifications/initialized"},
                        version=V,
                    )
                victim_session = next(
                    value for value in victim.session_ids if value
                )
                attacker_session = next(
                    value for value in attacker.session_ids if value
                )
                self.assertNotEqual(victim_session, attacker_session)

                asked = asyncio.Event()
                prompt: list = []
                seen: list = []

                def watch_frames(payload):
                    if payload.get("method") == "elicitation/create":
                        prompt.append(payload)
                        asked.set()

                async def watch():
                    """The victim's call, with its stream read as it arrives."""
                    seen.append(await victim.request(
                        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                         "params": {"name": "xyz_reload", "arguments": {}}},
                        version=V,
                        on_frame=watch_frames,
                    ))

                call = asyncio.create_task(watch())
                # The elicitation is sent while the call is in flight, so the
                # answer has to be posted from outside it -- and its id is
                # read off the wire rather than guessed, because a guess that
                # is wrong looks exactly like a routing refusal.
                await asyncio.wait_for(asked.wait(), timeout=10)
                answerer = victim if answer_on_victim else attacker
                await answerer.request(
                    {"jsonrpc": "2.0", "id": prompt[0]["id"],
                     "result": {"action": action}},
                    version=V,
                )
                try:
                    await asyncio.wait_for(call, timeout=5)
                    outcome["answered"] = True
                    outcome["text"] = self.text_of(seen[0][2] if seen else "")
                    for line in seen[0][2].splitlines():
                        if line.startswith('data: '):
                            frame = json.loads(line[6:])
                            if frame.get('id') == 7:
                                outcome['result'] = frame.get('result')
                    outcome['prompt'] = prompt[0]['params']
                except asyncio.TimeoutError:
                    # Still waiting on a prompt nobody it trusts has answered.
                    call.cancel()
                    outcome["answered"] = False
                    outcome["text"] = ""

        run(case())
        return outcome

    @staticmethod
    def text_of(body):
        """The tool's own answer, found by its id.

        Not the first frame on the stream: the elicitation travels there too,
        and reading that one yields an empty string, which is
        indistinguishable from a call that was never answered.
        """
        for line in body.splitlines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            if payload.get("id") != 7:
                continue
            result = payload.get("result") or {}
            return " ".join(
                part.get("text", "") for part in result.get("content", [])
            )
        return ""

    def test_url_cancellation_returns_structured_error_and_dashboard_link(self):
        result = self.run_case(answer_on_victim=True, mode='url', action='cancel')
        self.assertTrue(result['answered'])
        self.assertTrue(result['result']['isError'])
        error = result['result']['structuredContent']['error']
        self.assertEqual('approval.cancelled', error['code'])
        self.assertEqual('cancel', error['action'])
        self.assertEqual('url', error['mode'])
        self.assertRegex(error['correlationId'], '^[a-f0-9]{32}$')
        self.assertIn('#approvals/', error['approvalUrl'])
        self.assertEqual('url', result['prompt']['mode'])

    def test_the_right_session_can_answer(self) -> None:
        """The positive control, and it earned its place.

        Without it this pair passed while proving nothing: the fake exchange
        was missing the method the gate calls first, so the tool crashed before
        any prompt was sent and *both* cases "completed" -- identically, on the
        crash. What distinguishes them has to be the decision reaching the
        call, so that is what is asserted rather than the call merely ending.
        """
        outcome = self.run_case(answer_on_victim=True)
        self.assertTrue(
            outcome["answered"],
            "the session that was asked could not answer its own prompt, so"
            " this harness proves nothing about the test below",
        )
        self.assertIn(
            "declined", outcome["text"],
            "the call ended without the answer reaching it:"
            f" {outcome['text'][:160]}",
        )

    def test_another_session_cannot(self) -> None:
        """The property the approval mechanism rests on. An agent that could
        answer a prompt meant for somebody else would be approving mutations
        nobody asked it about."""
        outcome = self.run_case(answer_on_victim=False)
        self.assertFalse(
            outcome["answered"],
            "a second session answered a prompt meant for the first:"
            f" {outcome['text'][:160]}",
        )
