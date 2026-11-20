import asyncio
import json
import time
import unittest
from unittest.mock import patch

import anyio

from app import build_app
from authentication import Authenticated, CURRENT_CALLER
from asgi_harness import StubIntrospection, active
from config_api_client import ConfigApiClient, ConfigApiUnavailable
from era_guard import HANDSHAKE_VERSIONS, MODERN_VERSION
from exchange_client import ExchangeClient
from introspection_client import IntrospectionClient
from protected_resource import ProtectedResource
from runtime import XYZ_RELOAD, build_runtime, build_runtime_app
from request_scope import CURRENT_REQUEST, RequestScope, run_worker
from test_legacy_session import MODERN_META, ORIGIN, TOKEN, Session, composed, rpc_result
from test_legacy_session import FakeExchange
from test_approval_gate import Answer, Capability, Confirmation, FakeConfigApi, FakeContext, RECEIPT


def tool_call(version):
    params = {"name": "capabilities_list", "arguments": {}}
    if version == MODERN_VERSION:
        params["_meta"] = MODERN_META
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params}


class RequestLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def drive_guard(self, receive, cap="1"):
        calls = StubIntrospection({TOKEN: active()})

        async def unexpected(*args):
            self.fail("An incomplete or cancelled request reached the runtime")

        app, _ = build_app(inner=unexpected, introspection=calls)
        headers = [(b"authorization", ("Bearer " + TOKEN).encode()),
                   (b"x-mapp-timeout-seconds", cap.encode())]
        sent = []

        async def send(message):
            sent.append(message)

        await asyncio.wait_for(app({"type": "http", "path": "/mcp", "method": "POST",
                                    "headers": headers}, receive, send), 1)
        self.assertEqual(0, calls.calls)
        return sent

    async def test_disconnect_before_and_during_body(self):
        for prefix in ([], [{"type": "http.request", "body": b"{", "more_body": True}]):
            events = iter([*prefix, {"type": "http.disconnect"}])

            async def receive():
                return next(events)

            self.assertEqual([], await self.drive_guard(receive))

    async def test_dripping_and_invalid_caller_budgets(self):
        async def receive():
            await asyncio.sleep(.01)
            return {"type": "http.request", "body": b"x", "more_body": True}

        for cap in (".05", "inf", "nan", "malformed"):
            started = time.monotonic()
            sent = await self.drive_guard(receive, cap)
            self.assertEqual(503, sent[0]["status"])
            self.assertLess(time.monotonic() - started, .15)

    async def test_disconnect_in_auth_worker_queue(self):
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1
        self.addCleanup(setattr, limiter, "total_tokens", original)
        body = True

        async def receive():
            nonlocal body
            if body:
                body = False
                return {"type": "http.request", "body": b"{}", "more_body": False}
            while not limiter.statistics().tasks_waiting:
                await asyncio.sleep(.001)
            return {"type": "http.disconnect"}

        holder = object()
        await limiter.acquire_on_behalf_of(holder)
        try:
            await self.drive_guard(receive)
            self.assertEqual(0, limiter.statistics().tasks_waiting)
        finally:
            limiter.release_on_behalf_of(holder)

    async def test_chunked_replay_through_each_sdk_era(self):
        app, runtime, exchange, _ = composed()

        async def chunked(scope, receive, send):
            message = await receive()
            raw = message["body"]
            parts = iter([{"type": "http.request", "body": raw[:7], "more_body": True},
                          {"type": "http.request", "body": raw[7:], "more_body": False}])

            async def chunks():
                part = next(parts, None)
                return part if part is not None else await receive()

            if (b"mcp-method", b"tools/call") in scope["headers"]:
                scope["headers"].append((b"mcp-name", b"capabilities_list"))
            await app(scope, chunks, send)

        async with runtime.router.lifespan_context(runtime):
            for version in (*HANDSHAKE_VERSIONS, MODERN_VERSION):
                client = Session(chunked)
                if version != MODERN_VERSION:
                    status, _, _ = await client.request({"jsonrpc": "2.0", "id": 0,
                        "method": "initialize", "params": {"protocolVersion": version,
                        "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
                    self.assertEqual(200, status)
                    await client.request({"jsonrpc": "2.0", "method": "notifications/initialized"}, version=version)
                count = len(exchange.calls)
                status, _, body = await client.request(tool_call(version), version=version, method_header="tools/call")
                self.assertEqual(200, status)
                self.assertFalse(rpc_result(body)["result"].get("isError"))
                self.assertEqual(count + 1, len(exchange.calls))

    async def test_disconnect_in_sdk_worker_queue_and_reuse_handshake_session(self):
        _, runtime, exchange, _ = composed()
        limiter = anyio.to_thread.current_default_thread_limiter()
        original = limiter.total_tokens
        limiter.total_tokens = 1
        self.addCleanup(setattr, limiter, "total_tokens", original)
        holder = object()
        hold = False

        async def after_auth(scope, receive, send):
            if hold:
                limiter.acquire_on_behalf_of_nowait(holder)
            await runtime(scope, receive, send)

        app, _ = build_app(inner=after_auth, introspection=StubIntrospection({TOKEN: active()}))
        async with runtime.router.lifespan_context(runtime), asyncio.timeout(3):
            client = Session(app)
            version = HANDSHAKE_VERSIONS[-1]
            await client.request({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
            await client.request({"jsonrpc": "2.0", "method": "notifications/initialized"}, version=version)
            hold = True
            disconnect = asyncio.Event()
            messages = asyncio.Queue()
            messages.put_nowait({"type": "http.request", "body": json.dumps(tool_call(version)).encode()})

            async def receive():
                if not messages.empty():
                    return messages.get_nowait()
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                pass  # caller abandons the stream

            scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [
                (b"mcp-session-id", client.session.encode()), (b"mcp-protocol-version", version.encode()),
                (b"authorization", ("Bearer " + TOKEN).encode()), (b"host", b"mcp.localhost"),
                (b"content-type", b"application/json"), (b"accept", b"application/json, text/event-stream")]}
            pending = asyncio.create_task(app(scope, receive, send))
            try:
                while not limiter.statistics().tasks_waiting:
                    await asyncio.sleep(.001)
                disconnect.set()
                while limiter.statistics().tasks_waiting:
                    await asyncio.sleep(.001)
                self.assertEqual([], exchange.calls)
            finally:
                hold = False
                if holder in limiter.statistics().borrowers:
                    limiter.release_on_behalf_of(holder)
            await pending
            self.assertEqual([], exchange.calls)
            status, _, body = await client.request(tool_call(version) | {"id": 3}, version=version)
            self.assertEqual(200, status)
            self.assertFalse(rpc_result(body)["result"].get("isError"))
            self.assertEqual(1, len(exchange.calls))

    async def test_cold_requests_share_the_tool_budget(self):
        stages = []
        exchange_closed = asyncio.Event()
        budget = .48

        async def handle(reader, writer):
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
                path = head[0].split()[1]
                headers = dict(line.lower().split(": ", 1) for line in head[1:] if line)
                await reader.readexactly(int(headers.get("content-length", 0)))
                stages.append((path, float(headers["x-mapp-timeout-seconds"])))
                if path.endswith("/exchange"):
                    # Like each previous stage, this one alone fits. A fresh
                    # timer here would mint a credential and allow dispatch.
                    try:
                        if await asyncio.wait_for(reader.read(1), .18) == b"":
                            exchange_closed.set()
                            return
                    except TimeoutError:
                        pass
                    payload = {"access_token": "fixture"}
                else:
                    await asyncio.sleep(.18)
                    payload = active() if path.endswith("/introspect") else {"instanceId": "fixture"}
                body = json.dumps(payload).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
            endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            auth = dict(client_id="fixture", client_secret="fixture")
            introspection = IntrospectionClient(endpoint, resource=ORIGIN + "/mcp", **auth)
            exchange = ExchangeClient(broker_endpoint=endpoint, config_api_endpoint=endpoint,
                                      config_api_resource="http://config.localhost/api", **auth)
            runtime = build_runtime_app(resource=ProtectedResource(origin=ORIGIN, issuer=ORIGIN),
                                        exchange=exchange, config_api=ConfigApiClient(endpoint=endpoint))
            runtime.tool_timeouts["capabilities_list"] = budget
            app, _ = build_app(inner=runtime, introspection=introspection)

            async def with_name(scope, receive, send):
                scope["headers"].append((b"mcp-name", b"capabilities_list"))
                await app(scope, receive, send)

            async with runtime.router.lifespan_context(runtime):
                started = time.monotonic()
                _, _, text = await Session(with_name).request(tool_call(MODERN_VERSION),
                    version=MODERN_VERSION, method_header="tools/call")
                self.assertTrue(rpc_result(text)["result"]["isError"])
                await asyncio.wait_for(exchange_closed.wait(), .1)
                self.assertLess(time.monotonic() - started, budget + .1)

        self.assertEqual(["/internal/oauth/introspect", "/api/public/identity", "/internal/oauth/exchange"],
                         [path for path, _ in stages])
        first, second, last = [value for _, value in stages]
        self.assertLessEqual(first, budget)
        self.assertLess(second, first - .1)
        self.assertLess(last, second - .1)

    async def test_origin_closes_overlapping_header_and_body_reads(self):
        connected = asyncio.Queue()
        closed = asyncio.Queue()

        async def handle(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            path = head.split()[1]
            if path == b"/body":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n{")
                await writer.drain()
            await connected.put(path)
            if await reader.read(1) == b"":
                await closed.put(path)
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
            api = ConfigApiClient(endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            request = RequestScope()
            token = CURRENT_REQUEST.set(request)
            try:
                tasks = [asyncio.create_task(run_worker(api.get, path=path, query="", token="fixture"))
                         for path in ("/headers", "/body")]
                for _ in tasks:
                    await asyncio.wait_for(connected.get(), 1)
                request.close()
                for _ in tasks:
                    await asyncio.wait_for(closed.get(), .5)
                errors = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), .5)
                for error in errors:
                    self.assertIsInstance(error, ConfigApiUnavailable)
            finally:
                request.close()
                CURRENT_REQUEST.reset(token)

    async def test_cancel_while_resolving_never_connects(self):
        request = RequestScope()
        token = CURRENT_REQUEST.set(request)
        self.addCleanup(CURRENT_REQUEST.reset, token)
        resolving = asyncio.Event()
        release = asyncio.Event()
        resolve = request.loop.getaddrinfo

        async def delayed(*args, **kwargs):
            resolving.set()
            await release.wait()
            return await resolve(*args, **kwargs)

        api = ConfigApiClient(endpoint="http://127.0.0.1:1")
        with patch.object(request.loop, "getaddrinfo", delayed), patch.object(request.loop, "sock_connect") as connect:
            pending = asyncio.create_task(run_worker(api.get, path="/", query="", token="fixture"))
            await asyncio.wait_for(resolving.wait(), 1)
            request.close()
            with self.assertRaises(ConfigApiUnavailable):
                await asyncio.wait_for(pending, .5)
            release.set()
            connect.assert_not_called()

    async def test_delayed_http_delivery_still_closes_on_sender_budget(self):
        # Delay the receiver's observation of the headers. Starting its local
        # timer at that point gives it longer than the sender has left.
        observed = asyncio.get_running_loop().create_future()

        async def handle(reader, writer):
            await asyncio.sleep(.15)
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            headers = dict(line.lower().split(": ", 1) for line in head[1:] if line)
            relative = float(headers["x-mapp-timeout-seconds"])
            start = time.monotonic()
            eof = await reader.read(1)
            observed.set_result((relative, time.monotonic() - start, eof))
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
            api = ConfigApiClient(endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            token = CURRENT_REQUEST.set(RequestScope(".35"))
            try:
                with self.assertRaises(ConfigApiUnavailable):
                    await run_worker(api.get, path="/", query="", token="fixture")
                relative, waited, eof = await asyncio.wait_for(observed, .1)
                self.assertEqual(b"", eof)
                self.assertLess(waited, relative - .05)
            finally:
                CURRENT_REQUEST.reset(token)

    async def test_submitted_write_timeout_keeps_indeterminate_diagnostics(self):
        received = []

        async def handler(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            received.append(header.split()[1])
            await reader.read()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(handler, "127.0.0.1", 0) as server:
            api = ConfigApiClient(endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            sdk = build_runtime(resource=ProtectedResource(origin=ORIGIN, issuer=ORIGIN),
                                exchange=FakeExchange(), config_api=api)
            caller = CURRENT_CALLER.set(Authenticated(active(scopes="mcp:connect inspect propose"), TOKEN))
            request = CURRENT_REQUEST.set(RequestScope(".15"))
            try:
                result = await sdk.call_tool("proposals_create", {
                    "operations": [], "revision": "fixture", "check_fingerprint": "fixture"})
            finally:
                CURRENT_REQUEST.reset(request)
                CURRENT_CALLER.reset(caller)
        self.assertEqual([b"/api/proposals"], received)
        error = result.structured_content["error"]
        self.assertTrue(error["indeterminate"])
        self.assertFalse(error["retryable"])
        self.assertTrue(error["requestId"])

    async def test_human_confirmation_outlives_preparation_budget(self):
        api = FakeConfigApi()
        sdk = build_runtime(resource=ProtectedResource(origin=ORIGIN, issuer=ORIGIN),
                            exchange=FakeExchange(), config_api=api)
        operation = dict(XYZ_RELOAD, timeout=.1)
        request = RequestScope()
        request.tighten(operation["timeout"])
        token = CURRENT_REQUEST.set(request)
        self.addCleanup(CURRENT_REQUEST.reset, token)
        caller = CURRENT_CALLER.set(Authenticated(active(scopes="mcp:connect inspect reload"), TOKEN))
        self.addCleanup(CURRENT_CALLER.reset, caller)

        class SlowConfirmation(FakeContext):
            async def elicit(self, message, schema):
                await asyncio.sleep(.15)
                return Answer("accept", Confirmation(True))

        receipt = await sdk.approval_gate(SlowConfirmation(elicitation=Capability(form={})),
            operation, path=operation["path_template"], tool_name="xyz_reload", packet={})
        self.assertEqual(RECEIPT, receipt)
        self.assertGreater(request.remaining(), 0)
        self.assertLessEqual(request.remaining(), operation["timeout"])
