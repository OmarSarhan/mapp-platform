from __future__ import annotations

import asyncio
import base64
import json
import unittest

from authentication import CURRENT_CALLER
from mcp.server.mcpserver.exceptions import ToolError
from protected_resource import ProtectedResource
from runtime import build_runtime
from test_layers_tools import FakeConfigApi, FakeExchange, caller


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jv1sAAAAASUVORK5CYII="
)


class ArtifactImageTests(unittest.TestCase):
    def setUp(self):
        self.exchange = FakeExchange()
        self.artifact = {
            "path": "run-1/after-map.png", "mimeType": "image/png",
            "sizeBytes": len(PNG), "data": base64.b64encode(PNG).decode("ascii"),
        }
        self.api = FakeConfigApi(answer={"artifact": self.artifact})
        self.server = build_runtime(
            resource=ProtectedResource(origin="http://mcp.localhost", issuer="http://mcp.localhost"),
            exchange=self.exchange, config_api=self.api,
        )
        token = CURRENT_CALLER.set(caller(scopes="mcp:connect inspect visual"))
        self.addCleanup(CURRENT_CALLER.reset, token)

    def invoke(self, path="run-1/after-map.png"):
        return asyncio.run(self.server.call_tool("artifacts_image", {"artifact_path": path}))

    def test_sdk_returns_native_image_content_not_json_encoded_image(self):
        result = self.invoke()
        self.assertEqual(1, len(result.content))
        self.assertEqual("image", result.content[0].type)
        self.assertEqual("image/png", result.content[0].mime_type)
        self.assertEqual(PNG, base64.b64decode(result.content[0].data))
        self.assertIsNone(result.structured_content)
        exchange = self.exchange.calls[0]
        self.assertEqual("visual.artifacts.image", exchange["operation_id"])
        self.assertEqual("visual", exchange["scope"])
        self.assertEqual("/api/visual-artifacts/run-1/after-map.png", exchange["path"])
        self.assertEqual(exchange["path"], self.api.calls[0]["path"])

    def test_each_image_fetch_exchanges_a_fresh_credential(self):
        self.invoke()
        self.invoke()
        self.assertEqual(2, len(self.exchange.calls))

    def test_default_includes_download_and_link_mode_omits_inline_bytes(self):
        self.artifact["download"] = {
            "url": "https://mcp.example/artifact-downloads/signed",
            "expiresInSeconds": 300,
        }
        result = self.invoke()
        self.assertEqual(["text", "image"], [part.type for part in result.content])
        self.assertNotIn("data", json.loads(result.content[0].text))
        self.assertEqual("download=both", self.api.calls[-1]["query"])
        self.assertEqual(self.api.calls[-1]["query"], self.exchange.calls[-1]["query"])
        self.artifact.pop("data")
        result = asyncio.run(self.server.call_tool("artifacts_image", {
            "artifact_path": "run-1/after-map.png", "download": "link",
        }))
        self.assertEqual(["text"], [part.type for part in result.content])
        self.assertEqual("download=link", self.api.calls[-1]["query"])

    def test_link_mode_does_not_claim_a_missing_download_exists(self):
        with self.assertRaises(ToolError):
            asyncio.run(self.server.call_tool("artifacts_image", {
                "artifact_path": "run-1/after-map.png", "download": "link",
            }))

    def test_path_and_scope_refusals_do_not_spend_credentials(self):
        for path in ("../after-map.png", "/run-1/after-map.png", "run-1/report.json", "run-1/%2e%2e.png"):
            with self.subTest(path=path), self.assertRaises(ToolError):
                self.invoke(path)
        CURRENT_CALLER.set(caller(scopes="mcp:connect inspect"))
        with self.assertRaisesRegex(ToolError, "visual"):
            self.invoke()
        self.assertEqual([], self.exchange.calls)

    def test_invalid_mismatched_and_oversized_images_are_refused(self):
        for changes in (
            {"path": "run-2/after-map.png"}, {"mimeType": "text/plain"},
            {"data": "invalid!"}, {"sizeBytes": 123}, {"data": "x" * (12 * 1024 * 1024)},
        ):
            with self.subTest(changes=list(changes)):
                self.api.answer = {"artifact": {**self.artifact, **changes}}
                with self.assertRaises(ToolError):
                    self.invoke()
