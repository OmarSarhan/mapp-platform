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
from runtime import build_runtime_app  # noqa: E402

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


class FakeConfigApi:
    def __init__(self) -> None:
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return {"total": 2, "values": [{"value": "a", "count": 2}]}


class Session:
    """Requests sharing one lifespan, as a connected client's would."""

    def __init__(self, app) -> None:
        self._app = app
        self.session_ids = []

    async def request(self, body, *, version=None, token=TOKEN, method_header=None):
        raw = json.dumps(body).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            (b"host", b"mcp.localhost"),
        ]
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

        await asyncio.wait_for(self._app(scope, receive, send), timeout=20)
        start = next(m for m in messages if m["type"] == "http.response.start")
        headers_out = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in start.get("headers", [])
        }
        self.session_ids.append(headers_out.get("mcp-session-id"))
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
                V = era_guard.HANDSHAKE_VERSION
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
        self.assertEqual(era_guard.HANDSHAKE_VERSION, result["protocolVersion"])

    def test_no_session_identifier_is_ever_minted(self) -> None:
        """The obligation that serving this era was most likely to cost.

        The legacy transport is session-based, so admitting it could have meant
        admitting `Mcp-Session-Id` -- which the guard strips, leaving a client
        holding a session the server had been told to forget. It does not,
        because the runtime is stateless; this is what says so on the wire.
        """
        transcript, _, _ = self.drive()
        self.assertEqual(
            [None, None, None, None],
            transcript["session_ids"],
            "a session identifier crossed the boundary",
        )

    def test_both_tools_are_listed_over_the_legacy_era(self) -> None:
        transcript, _, _ = self.drive()
        listed = rpc_result(transcript["tools/list"][2])["result"]["tools"]
        self.assertEqual(
            ["describe_instance", "layer_values"], sorted(t["name"] for t in listed)
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
                    "name": "layer_values",
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
        self.assertIn(era_guard.HANDSHAKE_VERSION, described["protocolVersions"])
        self.assertEqual(list(era_guard.SERVED_VERSIONS), described["protocolVersions"])


#: The modern era carries per request what the handshake negotiated once. Both
#: keys are required, and the SDK refuses the request outright without them --
#: which is the concrete difference between the two eras, not a version string.
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": era_guard.MODERN_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}


class ToolFailureVisibilityTests(unittest.TestCase):
    """What the *client* is given when a tool refuses.

    This is the test that was missing. Every other test of `layer_values` calls
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
                return await Session(app).request(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "layer_values",
                            "arguments": {"layer_key": "L", "field": "f"},
                        },
                    },
                    version=era_guard.HANDSHAKE_VERSION,
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
            "Error executing tool layer_values",
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
            ["describe_instance", "layer_values"], sorted(t["name"] for t in listed)
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
                        "params": {"protocolVersion": era_guard.HANDSHAKE_VERSION},
                    },
                    version=era_guard.MODERN_VERSION,
                )

        status, _, body = run(session())
        self.assertEqual(400, status)
        self.assertEqual("legacy-initialize", json.loads(body)["error"]["data"]["reason"])


if __name__ == "__main__":
    unittest.main()
