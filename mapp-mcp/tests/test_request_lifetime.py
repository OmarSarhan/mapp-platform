import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import anyio
import request_http
import runtime
import config_api_client

from app import build_app
from authentication import Authenticated, CURRENT_CALLER
from asgi_harness import StubIntrospection, active
from config_api_client import ConfigApiClient, ConfigApiUnavailable
from era_guard import HANDSHAKE_VERSIONS, MODERN_VERSION
from exchange_client import ExchangeClient
from introspection_client import IntrospectionClient
from protected_resource import ProtectedResource
from runtime import XYZ_RELOAD, build_runtime, build_runtime_app
from request_scope import CURRENT_REQUEST, RequestScope, RequestStopped, run_worker
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

    async def test_resolver_admission_survives_serial_expiry_and_origin_close(self):
        release = threading.Event()
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        resolve = socket.getaddrinfo
        requests = []
        jobs = []

        def delayed(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise RuntimeError("resolver fixture timed out")
            return resolve(*args, **kwargs)

        submit = request_http._RESOLVERS.submit

        def record_submit(*args, **kwargs):
            job = submit(*args, **kwargs)
            jobs.append(job)
            return job

        async def handle(reader, writer):
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
            api = ConfigApiClient(endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            with (patch.object(socket, "getaddrinfo", delayed),
                  patch.object(request_http._RESOLVERS, "submit", record_submit)):
                try:
                    for i in range(request_http._RESOLVER_LIMIT + 4):
                        request = RequestScope()
                        token = CURRENT_REQUEST.set(request)
                        try:
                            pending = asyncio.create_task(run_worker(api.get, path="/", query="",
                                token="fixture", timeout=1 if i == 0 else .12))
                            if i == 0:
                                await asyncio.wait_for(entered.wait(), 1)
                                request.close()
                            with self.assertRaises(ConfigApiUnavailable):
                                await asyncio.wait_for(pending, .5)
                            self.assertFalse(request._hops)
                        finally:
                            CURRENT_REQUEST.reset(token)
                    self.assertEqual(request_http._RESOLVER_LIMIT, len(jobs))
                    self.assertTrue(all(job.running() for job in jobs))
                    self.assertFalse(requests)
                finally:
                    release.set()
                    await asyncio.gather(*(asyncio.wrap_future(job) for job in jobs))
                token = CURRENT_REQUEST.set(RequestScope())
                try:
                    await run_worker(api.get, path="/fresh", query="", token="fixture")
                finally:
                    CURRENT_REQUEST.reset(token)
                self.assertEqual([b"/fresh"], [head.split()[1] for head in requests])

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

    async def test_proposal_ack_and_missing_ack_through_modern_mcp(self):
        received = []

        async def handler(reader, writer):
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":")[1]) for line in header.lower().splitlines()
                          if line.startswith(b"content-length:"))
            await reader.readexactly(length)
            received.append(header.split()[1])
            if len(received) == 1:
                body = b'{"proposal":{"id":"p-created"}}'
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                             + b"\r\n\r\n" + body)
                await writer.drain()
            else:
                await reader.read()
            writer.close()
            await writer.wait_closed()

        summarize = runtime._proposal_summary
        format_expired = threading.Event()

        def delayed(proposal):
            self.assertEqual("p-created", proposal["id"])
            time.sleep(CURRENT_REQUEST.get().remaining() + .02)
            format_expired.set()
            return summarize(proposal)

        async with await asyncio.start_server(handler, "127.0.0.1", 0) as server:
            api = ConfigApiClient(endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            sdk = build_runtime_app(resource=ProtectedResource(origin=ORIGIN, issuer=ORIGIN),
                                    exchange=FakeExchange(), config_api=api)
            sdk.tool_timeouts["proposals_create"] = .3
            app, _ = build_app(inner=sdk, introspection=StubIntrospection({
                TOKEN: active(scopes="mcp:connect inspect propose")}))

            async def with_name(scope, receive, send):
                scope["headers"].append((b"mcp-name", b"proposals_create"))
                await app(scope, receive, send)

            async with sdk.router.lifespan_context(sdk):
                client = Session(with_name)
                for count in (1, 2):
                    with patch.object(runtime, "_proposal_summary", delayed):
                        _, _, body = await client.request({"jsonrpc": "2.0", "id": count,
                            "method": "tools/call", "params": {"_meta": MODERN_META, "name": "proposals_create",
                            "arguments": {"operations": [], "revision": "fixture", "check_fingerprint": "fixture"}}},
                            version=MODERN_VERSION, method_header="tools/call")
                    self.assertEqual([b"/api/proposals"] * count, received)
                    result = rpc_result(body)["result"]
                    detail = result["structuredContent"]
                    if count == 1:
                        self.assertTrue(format_expired.is_set())
                        self.assertFalse(result["isError"])
                        self.assertEqual("p-created", detail["proposalId"])
                        self.assertTrue(detail["acknowledged"])
                    else:
                        self.assertTrue(result["isError"])
        error = detail["error"]
        self.assertTrue(error["indeterminate"])
        self.assertFalse(error["retryable"])
        self.assertTrue(error["requestId"])

    async def test_complete_proposal_reply_survives_cleanup_expiry_and_rejects_missing_id(self):
        cases = [(201, b'{"proposal":{"id":"p-ack"}}', True, None)]
        cases += [(status, body, False, None) for status in (200, 201)
                  for body in (b'{}', b'null', b'{"proposal":{"id":""}}')]
        acknowledgement = b'{"proposal":{"id":"p-unframed"}}'
        chunk = f"{len(acknowledgement):x}\r\n".encode() + acknowledgement + b"\r\n0\r\n"
        cases += [(201, body, False, headers) for body, headers in (
            (acknowledgement, b"Connection: close"),
            (chunk, b"Transfer-Encoding: chunked"),
            (chunk + b"\r\n", b"Transfer-Encoding: chunked"),
            (acknowledgement, b"Content-Length: 40\r\nContent-Length: 40"),
            (acknowledgement, b"Content-Length: \xa0" + str(len(acknowledgement)).encode()),
        )]
        received = []
        current = None

        async def handle(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":")[1]) for line in head.lower().splitlines()
                          if line.startswith(b"content-length:"))
            await reader.readexactly(length)
            received.append(head.split()[1])
            status, body, _, framing = current
            framing = framing if framing is not None else f"Content-Length: {len(body)}".encode()
            writer.write(f"HTTP/1.1 {status} OK\r\n".encode() + framing + b"\r\n\r\n" + body)
            await writer.drain()
            if current[3] is not None:
                await reader.read()  # The client must refuse before peer EOF.
            writer.close()
            await writer.wait_closed()

        original = config_api_client.urlopen

        @contextmanager
        def delayed_exit(*args, **kwargs):
            with original(*args, **kwargs) as response:
                yield response
                if current[2]:
                    # The client has copied the complete framed body; the real
                    # HTTP context still performs its final deadline check.
                    time.sleep(CURRENT_REQUEST.get().remaining() + .02)

        async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
            api = ConfigApiClient(endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            sdk = build_runtime_app(resource=ProtectedResource(origin=ORIGIN, issuer=ORIGIN),
                                    exchange=FakeExchange(), config_api=api)
            sdk.tool_timeouts["proposals_create"] = .6
            app, _ = build_app(inner=sdk, introspection=StubIntrospection({
                TOKEN: active(scopes="mcp:connect inspect propose")}))

            async def named(scope, receive, send):
                scope["headers"].append((b"mcp-name", b"proposals_create"))
                await app(scope, receive, send)

            async with sdk.router.lifespan_context(sdk):
                for index, current in enumerate(cases, 1):
                    with self.subTest(status=current[0], body=current[1]), patch.object(
                        config_api_client, "urlopen", delayed_exit
                    ):
                        _, _, body = await Session(named).request({"jsonrpc": "2.0", "id": index,
                            "method": "tools/call", "params": {"_meta": MODERN_META,
                            "name": "proposals_create", "arguments": {"operations": [],
                            "revision": "fixture", "check_fingerprint": "fixture"}}},
                            version=MODERN_VERSION, method_header="tools/call")
                        result = rpc_result(body)["result"]
                        detail = result["structuredContent"]
                        self.assertEqual([b"/api/proposals"] * index, received)
                        if current[2]:
                            self.assertFalse(result["isError"])
                            self.assertEqual("p-ack", detail["proposalId"])
                            self.assertTrue(detail["acknowledged"])
                        else:
                            self.assertTrue(result["isError"])
                            self.assertTrue(detail["error"]["indeterminate"])
                            self.assertFalse(detail["error"]["retryable"])
                            self.assertTrue(detail["error"]["requestId"])

    async def test_approved_reply_survives_local_expiry_at_http_cleanup(self):
        reference = "a" * 64
        recovery = {"approvalReference": reference, "operationId": reference[:32]}
        received = []
        for status, payload in (
            (200, {"requestedGeneration": 9, "operation": {"id": reference[:32]}}),
            (503, {"code": "database.busy", "indeterminate": True, "retryable": False,
                   "mutation": {"receipt": "consumed", "tokenB": "spent", "targetStarted": True,
                                "published": {"requestedGeneration": 9}, "recovery": recovery}}),
        ):
            async def respond(reader, writer):
                head = await reader.readuntil(b"\r\n\r\n")
                size = next(int(line.split(b":")[1]) for line in head.lower().splitlines()
                            if line.startswith(b"content-length:"))
                await reader.readexactly(size)
                received.append(head.split()[1])
                data = json.dumps(payload).encode()
                writer.write(f"HTTP/1.1 {status} Reply\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
                api = ConfigApiClient(endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
                post = api.post

                def approved(**kwargs):
                    if kwargs["path"] == "/api/approvals":
                        return {"handle": "fixture", "reference": reference, "decided": True}
                    if kwargs["path"] == "/api/approvals/claim":
                        return {"status": "approved", "receipt": RECEIPT}
                    return post(**kwargs)

                original = config_api_client.urlopen
                @contextmanager
                def delayed_exit(*args, **kwargs):
                    with original(*args, **kwargs) as response:
                        yield response
                        time.sleep(CURRENT_REQUEST.get().remaining() + .02)

                length = request_http.framed_length
                def delayed_error_length(headers):
                    # request_http has read the framed error into memory.
                    time.sleep(CURRENT_REQUEST.get().remaining() + .02)
                    return length(headers)

                sdk = build_runtime(resource=ProtectedResource(origin=ORIGIN, issuer=ORIGIN),
                                    exchange=FakeExchange(), config_api=api)
                sdk.tool_timeouts["xyz_reload"] = .3
                request = CURRENT_REQUEST.set(RequestScope())
                caller = CURRENT_CALLER.set(Authenticated(active(scopes="mcp:connect inspect reload"), TOKEN))
                try:
                    with patch.object(api, "post", approved), patch.object(config_api_client, "urlopen", delayed_exit), patch.object(
                            request_http, "framed_length", delayed_error_length):
                        result = await sdk.call_tool("xyz_reload", {})
                finally:
                    CURRENT_CALLER.reset(caller)
                    CURRENT_REQUEST.reset(request)
                if status == 200:
                    self.assertFalse(result.is_error)
                    self.assertEqual(payload, result.structured_content)
                else:
                    error = result.structured_content["error"]
                    self.assertEqual(payload["mutation"], error["mutation"])
                    self.assertFalse(error["retryable"])
                    self.assertTrue(error["indeterminate"])
        self.assertEqual([b"/api/xyz/reload"] * 2, received)

    async def test_expired_approval_handoff_keeps_public_acknowledgement(self):
        api = FakeConfigApi()
        post = api.post
        def expire_after_claim(**kwargs):
            result = post(**kwargs)
            if kwargs["path"] == "/api/approvals":
                result["decided"] = True
            if kwargs["path"] == "/api/approvals/claim":
                CURRENT_REQUEST.get().deadline = time.monotonic() - 1
            return result

        sdk = build_runtime(resource=ProtectedResource(origin=ORIGIN, issuer=ORIGIN),
                            exchange=FakeExchange(), config_api=api)
        request = CURRENT_REQUEST.set(RequestScope())
        caller = CURRENT_CALLER.set(Authenticated(active(scopes="mcp:connect inspect reload"), TOKEN))
        try:
            with patch.object(api, "post", expire_after_claim):
                result = await sdk.call_tool("xyz_reload", {})
        finally:
            CURRENT_CALLER.reset(caller)
            CURRENT_REQUEST.reset(request)
        error = result.structured_content["error"]
        self.assertEqual("a" * 64, error["approvalReference"])
        self.assertTrue(error["receiptClaimed"])
        self.assertFalse(error["retryable"])
        self.assertNotIn(RECEIPT, json.dumps(error))
        self.assertEqual([], api.posted_to("/api/xyz/reload"))

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

    async def test_claim_waiter_expires_before_worker_release(self):
        caller = CURRENT_CALLER.set(Authenticated(active(scopes="mcp:connect inspect reload"), TOKEN))
        self.addCleanup(CURRENT_CALLER.reset, caller)
        limiter = anyio.to_thread.current_default_thread_limiter()
        self.addCleanup(setattr, limiter, "total_tokens", limiter.total_tokens)
        limiter.total_tokens = 1
        for path in ("standing", "form", "remembered"):
            with self.subTest(path=path):
                api = FakeConfigApi()
                exchange = FakeExchange()
                sdk = build_runtime(resource=ProtectedResource(origin=ORIGIN, issuer=ORIGIN),
                                    exchange=exchange, config_api=api)
                if path == "remembered":
                    sdk.remembered_approvals[(CURRENT_CALLER.get().grant_id, exchange.request_digest())] = (
                        "fixture", api.approval_url)
                holder = object()
                token = CURRENT_REQUEST.set(RequestScope())

                async def occupy_before_claim(fn, *args, **kwargs):
                    result = await run_worker(fn, *args, **kwargs)
                    preceding = kwargs.get("path")
                    if path == "standing" and preceding == "/api/approvals":
                        result["decided"] = True
                    if ((path == "standing" and preceding == "/api/approvals") or
                        (path == "form" and preceding == "/api/approvals/confirm") or
                        (path == "remembered" and fn == exchange.request_digest)):
                        limiter.acquire_on_behalf_of_nowait(holder)
                    return result

                ctx = FakeContext(elicitation=Capability(form={}), form=Answer("accept", Confirmation(True)))
                with patch.dict(runtime.APPROVALS_CLAIM, timeout=.12), patch.object(runtime, "io_call", occupy_before_claim):
                    pending = asyncio.create_task(sdk.approval_gate(ctx, XYZ_RELOAD,
                        path=XYZ_RELOAD["path_template"], tool_name="xyz_reload", packet={}))
                    try:
                        async with asyncio.timeout(1):
                            while not limiter.statistics().tasks_waiting:
                                await asyncio.sleep(.001)
                            with self.assertRaises(RequestStopped):
                                await asyncio.shield(pending)
                        self.assertEqual((holder,), limiter.statistics().borrowers)
                        self.assertEqual(0, limiter.statistics().tasks_waiting)
                    finally:
                        if holder in limiter.statistics().borrowers:
                            limiter.release_on_behalf_of(holder)
                        await asyncio.gather(pending, return_exceptions=True)
                        CURRENT_REQUEST.reset(token)
                self.assertEqual([], api.posted_to("/api/approvals/claim"))

    async def test_guidance_worker_expires_before_file_read(self):
        _, sdk, _, _ = composed()
        limiter = anyio.to_thread.current_default_thread_limiter()
        self.addCleanup(setattr, limiter, "total_tokens", limiter.total_tokens)
        limiter.total_tokens = 1
        holder = object()

        async def after_auth(scope, receive, send):
            limiter.acquire_on_behalf_of_nowait(holder)
            await sdk(scope, receive, send)

        app, _ = build_app(inner=after_auth, introspection=StubIntrospection({TOKEN: active()}))

        async def limited(scope, receive, send):
            scope["headers"].extend([(b"x-mapp-timeout-seconds", b".2"),
                                     (b"mcp-name", b"mapp://guidance/workflow")])
            await app(scope, receive, send)

        async with sdk.router.lifespan_context(sdk):
            with patch.object(Path, "read_text", autospec=True) as read:
                pending = asyncio.create_task(Session(limited).request({"jsonrpc": "2.0", "id": 2,
                    "method": "resources/read", "params": {"_meta": MODERN_META, "uri": "mapp://guidance/workflow"}},
                    version=MODERN_VERSION, method_header="resources/read"))
                try:
                    async with asyncio.timeout(1):
                        while not limiter.statistics().tasks_waiting:
                            await asyncio.sleep(.001)
                        _, _, body = await asyncio.shield(pending)
                    self.assertIn("error", rpc_result(body))
                    self.assertEqual(0, limiter.statistics().tasks_waiting)
                finally:
                    if holder in limiter.statistics().borrowers:
                        limiter.release_on_behalf_of(holder)
                    await asyncio.gather(pending, return_exceptions=True)
                read.assert_not_called()

    async def test_url_completion_cannot_hand_off_receipt_after_expiry_or_close(self):
        caller = CURRENT_CALLER.set(Authenticated(active(scopes="mcp:connect inspect reload"), TOKEN))
        self.addCleanup(CURRENT_CALLER.reset, caller)
        for close in (False, True):
            with self.subTest(close=close):
                api = FakeConfigApi()
                sdk = build_runtime(resource=ProtectedResource(origin=ORIGIN, issuer=ORIGIN),
                                    exchange=FakeExchange(), config_api=api)
                request = RequestScope()
                token = CURRENT_REQUEST.set(request)
                entered = asyncio.Event()
                ctx = FakeContext(elicitation=Capability(url={}), url=Answer("accept"))

                async def notify(digest):
                    entered.set()
                    await asyncio.Future()

                ctx.session.send_elicit_complete = notify
                try:
                    pending = asyncio.create_task(sdk.approval_gate(ctx, dict(XYZ_RELOAD, timeout=.12),
                        path=XYZ_RELOAD["path_template"], tool_name="xyz_reload", packet={}))
                    await asyncio.wait_for(entered.wait(), 1)
                    if close:
                        request.close()
                    with self.assertRaises(RequestStopped):
                        await asyncio.wait_for(pending, .4)
                    self.assertEqual(1, len(api.posted_to("/api/approvals/claim")))
                finally:
                    CURRENT_REQUEST.reset(token)

    async def test_native_cancel_retains_worker_until_socket_and_worker_exit(self):
        requests = []
        received, eof = asyncio.Event(), asyncio.Event()
        release = threading.Event()

        async def handle(reader, writer):
            requests.append((await reader.readuntil(b"\r\n\r\n")).split()[1])
            if requests[-1] == b"/held":
                received.set()
                if await reader.read() == b"":
                    eof.set()
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
            api = ConfigApiClient(endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")

            def held():
                try:
                    api.get(path="/held", query="", token="fixture")
                    api.post(path="/late", query="", token="fixture", body={})
                finally:
                    if not release.wait(2):
                        raise RuntimeError("worker fixture timed out")

            request = RequestScope()
            token = CURRENT_REQUEST.set(request)
            limiter = anyio.to_thread.current_default_thread_limiter()
            self.addCleanup(setattr, limiter, "total_tokens", limiter.total_tokens)
            limiter.total_tokens = 1
            task = asyncio.create_task(run_worker(held))
            try:
                await asyncio.wait_for(received.wait(), 1)
                task.cancel()
                await asyncio.wait_for(eof.wait(), .5)
                self.assertEqual(1, limiter.borrowed_tokens)
                self.assertFalse(task.done())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                CURRENT_REQUEST.reset(token)
            self.assertTrue(task.cancelled())
            self.assertFalse(request._hops)
            self.assertEqual(0, limiter.borrowed_tokens)
            token = CURRENT_REQUEST.set(RequestScope())
            try:
                await run_worker(api.get, path="/fresh", query="", token="fixture")
            finally:
                CURRENT_REQUEST.reset(token)
            self.assertEqual([b"/held", b"/fresh"], requests)

    async def test_scoped_ftp_redirect_stops_before_target(self):
        received = []

        async def handle(reader, writer):
            received.append((await reader.readuntil(b"\r\n\r\n")).split()[1])
            writer.write(b"HTTP/1.1 302 Found\r\nLocation: ftp://127.0.0.1/fixture\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
            request = RequestScope()
            token = CURRENT_REQUEST.set(request)
            try:
                def post():
                    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/start"
                    with request_http.urlopen(urllib.request.Request(url, data=b""), timeout=1):
                        self.fail("unexpected redirect success")

                with patch.object(urllib.request.FTPHandler, "ftp_open") as target:
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        await run_worker(post)
                    caught.exception.close()
                    self.assertEqual(302, caught.exception.code)
                    target.assert_not_called()
                self.assertEqual([b"/start"], received)
                self.assertFalse(request._hops)
                self.assertEqual(0, anyio.to_thread.current_default_thread_limiter().borrowed_tokens)
            finally:
                CURRENT_REQUEST.reset(token)
