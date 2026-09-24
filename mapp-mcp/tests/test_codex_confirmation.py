"""Opt-in real-client confirmation test; no model turn or live platform writes.

Set MAPP_CODEX_TEST_BINARY to a Codex executable. A loopback MAPP runtime uses
fake authentication, exchange and configuration services. The host harness
cancels the prompt; it never approves even the fake operation.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from app import build_app
from asgi_harness import StubIntrospection, active
from protected_resource import ProtectedResource
from runtime import build_runtime_app
from test_legacy_session import FakeConfigApi, FakeExchange


@unittest.skipUnless(os.environ.get("MAPP_CODEX_TEST_BINARY"),
                     "set MAPP_CODEX_TEST_BINARY for real-client conformance")
class CodexConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_https_url_prompt_reaches_host_and_cancel_is_preserved(self):
        await self.run_case('https', 'url')

    async def test_http_form_prompt_reaches_host_and_cancel_is_preserved(self):
        await self.run_case('http', 'form')

    async def run_case(self, scheme, expected_mode):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        resource = ProtectedResource(origin=origin, issuer=origin)
        api = FakeConfigApi(approval_url=f'{scheme}://config.localhost/#approvals/' + 'a' * 64)
        runtime = build_runtime_app(resource=resource, exchange=FakeExchange(),
                                    config_api=api)
        # An intentionally fake credential, recognized only by this fixture.
        fixture_token = "mapp_a_confirmation_fixture"
        app, _ = build_app(
            origin=origin, issuer=origin, inner=runtime,
            introspection=StubIntrospection({fixture_token: active(
                scopes="mcp:connect inspect reload", audience=origin + "/mcp")}),
        )
        http = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
        worker = threading.Thread(target=http.run, kwargs={"sockets": [sock]}, daemon=True)
        worker.start()
        process = None
        scratch_dir = tempfile.TemporaryDirectory(prefix="mapp-confirmation-")
        try:
            async with asyncio.timeout(10):
                while not http.started:
                    await asyncio.sleep(0.01)
            scratch = scratch_dir.name
            with open(Path(scratch) / "client.log", "wb") as log:
                config = (
                    'mcp_servers={confirmation_fixture={url='
                    + json.dumps(origin + "/mcp")
                    + ',http_headers={Authorization='
                    + json.dumps("Bearer " + fixture_token) + '}}}'
                )
                process = await asyncio.create_subprocess_exec(
                    os.environ["MAPP_CODEX_TEST_BINARY"], "-c", config,
                    "app-server", "--stdio", cwd=scratch,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=log,
                )
                prompts = []

                async def send(message):
                    process.stdin.write((json.dumps(message) + "\n").encode())
                    await process.stdin.drain()

                async def request(identifier, method, params):
                    await send({"id": identifier, "method": method, "params": params})
                    async with asyncio.timeout(45):
                        while True:
                            line = await process.stdout.readline()
                            self.assertTrue(line, "Codex app-server closed before responding")
                            message = json.loads(line)
                            if message.get("method") == "mcpServer/elicitation/request":
                                prompts.append(message["params"])
                                await send({"id": message["id"], "result": {
                                    "action": "cancel", "content": None}})
                            if message.get("id") == identifier:
                                self.assertNotIn("error", message)
                                return message["result"]

                await request(1, "initialize", {
                    "clientInfo": {"name": "mapp_confirmation_test", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                })
                await send({"method": "initialized", "params": {}})
                thread = await request(2, "thread/start", {
                    "cwd": scratch, "ephemeral": True,
                    "approvalPolicy": "on-request", "approvalsReviewer": "user",
                })
                self.assertEqual("on-request", thread["approvalPolicy"])
                self.assertEqual("user", thread["approvalsReviewer"])
                result = await request(3, "mcpServer/tool/call", {
                    "threadId": thread["thread"]["id"],
                    "server": "confirmation_fixture", "tool": "xyz_reload",
                    "arguments": {},
                })
                log.flush()
                client_diagnostics = "\n".join(line[-800:] for line in (Path(scratch) / "client.log").read_text().splitlines() if "elicitation" in line.lower())
                self.assertEqual(1, len(prompts), client_diagnostics)
                self.assertEqual(expected_mode, prompts[0]["mode"])
                if expected_mode == 'form':
                    self.assertEqual(['approve'], prompts[0]['requestedSchema']['required'])
                    self.assertEqual('boolean', prompts[0]['requestedSchema']['properties']['approve']['type'])
                self.assertTrue(result["isError"])
                error = result["structuredContent"]["error"]
                self.assertEqual("approval.cancelled", error["code"])
                self.assertEqual("cancel", error["action"])
                self.assertFalse(error["applyAttempted"])
                self.assertEqual(["/api/approvals"], [c["path"] for c in api.calls])
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            http.should_exit = True
            await asyncio.to_thread(worker.join, 5)
            sock.close()
            scratch_dir.cleanup()
