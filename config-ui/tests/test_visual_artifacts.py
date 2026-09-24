from __future__ import annotations

import base64
import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from visual_artifacts import (
    DOWNLOAD_PREFIX, download_origin, issue_download, read_download,
    read_visual_image, VisualArtifactError,
)
from test_token_b_validation import TokenBTestCase, StubTokens, CONFIG_RESOURCE, GRANT, INSTANCE


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jv1sAAAAASUVORK5CYII="
)


class VisualArtifactTests(unittest.TestCase):
    def test_signed_download_keeps_original_bytes_and_expires(self):
        artifact = read_visual_image(self.root, "run-1/after-map.png", include_data=False)
        self.assertNotIn("data", artifact)
        self.assertEqual((1, 1), (artifact["width"], artifact["height"]))
        download = issue_download(artifact, b"private-key", now=1000)
        ticket = download["path"].removeprefix(DOWNLOAD_PREFIX)
        result = read_download(self.root, ticket, b"private-key", now=4599)
        self.assertEqual(PNG, base64.b64decode(result["data"]))
        for now in (999, 4600, 9999):
            with self.subTest(now=now), self.assertRaises(VisualArtifactError) as caught:
                read_download(self.root, ticket, b"private-key", now=now)
            self.assertEqual(403, caught.exception.status)

    def test_download_rejects_tampering_wrong_key_and_changed_content(self):
        artifact = read_visual_image(self.root, "run-1/after-map.png")
        ticket = issue_download(artifact, b"private-key", now=1000)["path"].removeprefix(DOWNLOAD_PREFIX)
        for malformed, key in ((ticket[:-1] + ("a" if ticket[-1] != "a" else "b"), b"private-key"),
                               (ticket, b"another-key"), ("x" * 900, b"private-key")):
            with self.subTest(ticket=malformed[:10]), patch("visual_artifacts.read_visual_image") as read:
                with self.assertRaises(VisualArtifactError):
                    read_download(self.root, malformed, key, now=1000)
                read.assert_not_called()
        self.image.write_bytes(PNG + b"changed")
        with self.assertRaises(VisualArtifactError) as caught:
            read_download(self.root, ticket, b"private-key", now=1000)
        self.assertEqual(410, caught.exception.status)

    def test_download_rechecks_report_and_symlinks(self):
        artifact = read_visual_image(self.root, "run-1/after-map.png")
        ticket = issue_download(artifact, b"private-key", now=1000)["path"].removeprefix(DOWNLOAD_PREFIX)
        self.report.unlink()
        with self.assertRaises(VisualArtifactError) as caught:
            read_download(self.root, ticket, b"private-key", now=1000)
        self.assertEqual(404, caught.exception.status)

    def test_download_origin_is_explicit_and_not_an_open_redirect(self):
        self.assertEqual("https://preview.example", download_origin("https://preview.example/"))
        self.assertEqual("http://localhost:8181", download_origin("http://localhost:8181"))
        for origin in ("http://example.com", "https://user:secret@example.com", "//example.com",
                       "https://example.com/path", "https://example.com?token=a"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                download_origin(origin)

    def test_download_route_returns_attachment_without_a_dashboard_session(self):
        import app
        artifact = read_visual_image(self.root, "run-1/after-map.png")
        link = issue_download(artifact, app.ARTIFACT_DOWNLOAD_KEY)
        with patch.object(app.CONTROL, "root", self.root.parent):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = "http://127.0.0.1:" + str(server.server_port) + link["path"]
                with urllib.request.urlopen(url, timeout=5) as response:
                    self.assertEqual(PNG, response.read())
                    self.assertEqual('attachment; filename="run-1-after-map.png"',
                                     response.headers["Content-Disposition"])
                    self.assertEqual("private, no-store", response.headers["Cache-Control"])
                    self.assertEqual("no-referrer", response.headers["Referrer-Policy"])
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url + "tampered", timeout=5)
                self.assertEqual(403, caught.exception.code)
                caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)

    def test_download_ticket_is_redacted_from_request_logs(self):
        import app
        handler = object.__new__(app.Handler)
        handler.path = DOWNLOAD_PREFIX + "PRIVATE-CAPABILITY"
        handler.client_address = ("127.0.0.1", 0)
        with patch("builtins.print") as log:
            handler.log_message('"%s" %s', handler.path, "200")
        self.assertNotIn("PRIVATE-CAPABILITY", str(log.call_args))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "artifacts"
        self.run = self.root / "run-1"
        self.run.mkdir(parents=True)
        self.image = self.run / "after-map.png"
        self.image.write_bytes(PNG)
        self.report = self.run / "report.json"
        self.report.write_text(json.dumps({
            "runId": "run-1", "artifacts": {"afterMap": "run-1/after-map.png"},
        }))

    def test_retained_png_is_returned_as_bounded_image_data(self):
        result = read_visual_image(self.root, "run-1/after-map.png")
        self.assertEqual(PNG, base64.b64decode(result["data"]))
        self.assertEqual("image/png", result["mimeType"])
        self.assertEqual(len(PNG), result["sizeBytes"])

    def test_non_image_traversal_encoded_and_absolute_paths_are_rejected(self):
        for relative in (
            "../after-map.png", "/run-1/after-map.png", "run-1/../after-map.png",
            "run-1/%2e%2e.png", "run-1/report.json", "run-1/secrets.png",
            "run-1\\after-map.png", "run-1/after-map.png/extra", ".hidden/map.png",
        ):
            with self.subTest(path=relative), self.assertRaises(VisualArtifactError) as caught:
                read_visual_image(self.root, relative)
            self.assertEqual(400, caught.exception.status)

    def test_only_images_named_by_the_same_run_report_can_be_read(self):
        for report in (
            {}, [], {"runId": "another-run", "artifacts": {"afterMap": "run-1/after-map.png"}},
            {"runId": "run-1", "artifacts": {"afterMap": "run-2/after-map.png"}},
        ):
            with self.subTest(report=report):
                self.report.write_text(json.dumps(report))
                with self.assertRaises(VisualArtifactError) as caught:
                    read_visual_image(self.root, "run-1/after-map.png")
                self.assertEqual(404, caught.exception.status)

    def test_missing_invalid_and_non_png_artifacts_are_unavailable(self):
        self.image.write_bytes(b"secret text")
        with self.assertRaises(VisualArtifactError) as caught:
            read_visual_image(self.root, "run-1/after-map.png")
        self.assertEqual(404, caught.exception.status)
        self.report.write_text("invalid json")
        with self.assertRaises(VisualArtifactError):
            read_visual_image(self.root, "run-1/after-map.png")
        self.report.unlink()
        with self.assertRaises(VisualArtifactError):
            read_visual_image(self.root, "run-1/after-map.png")

    def test_symlinks_at_each_component_are_rejected(self):
        for component in (self.image, self.report, self.run, self.root):
            with self.subTest(component=component.name):
                target = component.with_name(component.name + "-real")
                component.rename(target)
                component.symlink_to(target)
                try:
                    with self.assertRaises(VisualArtifactError) as caught:
                        read_visual_image(self.root, "run-1/after-map.png")
                    self.assertEqual(404, caught.exception.status)
                finally:
                    component.unlink()
                    target.rename(component)

    def test_oversized_image_and_report_are_refused(self):
        for limit in ("MAX_IMAGE_BYTES", "MAX_REPORT_BYTES"):
            with self.subTest(limit=limit), patch("visual_artifacts." + limit, 5):
                with self.assertRaises(VisualArtifactError) as caught:
                    read_visual_image(self.root, "run-1/after-map.png")
                self.assertEqual(413, caught.exception.status)


class VisualArtifactRouteTests(unittest.TestCase):
    def test_visual_scope_and_exact_operation_are_required(self):
        import app
        handler = object.__new__(app.Handler)
        path = "/api/visual-artifacts/run-1/after-map.png"
        self.assertEqual("visual", handler._required_scope(path, "GET"))
        self.assertEqual(
            ("visual.artifacts.image", "/api/visual-artifacts/{runId}/{filename}"),
            handler._resolve_operation("GET", path),
        )
        self.assertIsNone(handler._resolve_operation("GET", "/api/artifacts/run-1/after-map.png"))

    def test_route_uses_guarded_json_response(self):
        import app
        handler = object.__new__(app.Handler)
        handler.path = "/api/visual-artifacts/run-1/after-map.png"
        handler.command = "GET"
        handler._host_allowed = lambda: True
        handler._authorized = lambda: "oauth:grant"
        handler._exchanged_token = "test-token"
        handler._exchanged_token_redeemed = False
        handler.wfile = io.BytesIO()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        with patch.object(app, "read_visual_image", return_value={"data": "image"}):
            handler.do_GET()
        self.assertEqual([403], statuses)
        result = json.loads(handler.wfile.getvalue())
        self.assertEqual("auth.binding_not_redeemed", result["code"])
        self.assertNotIn("artifact", result)

    def test_authorization_failure_never_reads_artifacts(self):
        import app
        handler = object.__new__(app.Handler)
        handler.path = "/api/visual-artifacts/run-1/after-map.png"
        handler._host_allowed = lambda: True
        handler._authorized = lambda: None
        with patch.object(app, "read_visual_image") as read:
            handler.do_GET()
        read.assert_not_called()


class VisualArtifactBindingTests(TokenBTestCase):
    def test_download_issuance_binds_mode_and_never_returns_inline_data(self):
        import app
        import execution_envelope
        tokens = StubTokens(record={
            "active": True, "scope": "visual", "sub": GRANT,
            "aud": CONFIG_RESOURCE, "client_id": "mapp-mcp-broker",
        })
        path = "/api/visual-artifacts/run-1/after-map.png"
        handler = self.build(method="GET", path=path + "?download=link",
                             body=b"", tokens=tokens, receipt=None)
        handler._host_allowed = lambda: True
        artifact = {"path": "run-1/after-map.png", "sha256": "a" * 64,
                    "mimeType": "image/png", "sizeBytes": 123}
        with patch.object(app, "read_visual_image", return_value=artifact) as read:
            handler.do_GET()
        self.assertEqual(200, self.responses[0][0])
        self.assertNotIn("data", self.responses[0][1]["artifact"])
        self.assertIn("download", self.responses[0][1]["artifact"])
        self.assertEqual(False, read.call_args.kwargs["include_data"])
        expected = execution_envelope.digest(
            instance=INSTANCE, method="GET", operation_id="visual.artifacts.image",
            path_template="/api/visual-artifacts/{runId}/{filename}",
            path=path, query="download=link", body=None, resolved_defaults=None,
            confirmation_fields=None, revision_binding=None,
        )
        self.assertEqual([("mapp_b_credential", "visual.artifacts.image", expected)], tokens.redeemed)

    def test_actual_authorization_redeems_the_image_path_before_reading(self):
        import app
        import execution_envelope
        token_service = StubTokens(record={
            "active": True, "scope": "visual", "sub": GRANT,
            "aud": CONFIG_RESOURCE, "client_id": "mapp-mcp-broker",
        })
        handler = self.build(
            method="GET", path="/api/visual-artifacts/run-1/after-map.png",
            body=b"", tokens=token_service, receipt=None,
        )
        handler._host_allowed = lambda: True
        def read(root, relative):
            self.assertTrue(handler._exchanged_token_redeemed)
            self.assertEqual("run-1/after-map.png", relative)
            return {"path": relative, "data": "image"}
        with patch.object(app, "read_visual_image", side_effect=read):
            handler.do_GET()
        self.assertEqual(200, self.responses[0][0])
        expected = execution_envelope.digest(
            instance=INSTANCE, method="GET", operation_id="visual.artifacts.image",
            path_template="/api/visual-artifacts/{runId}/{filename}",
            path=handler.path, query="", body=None, resolved_defaults=None,
            confirmation_fields=None, revision_binding=None,
        )
        self.assertEqual([("mapp_b_credential", "visual.artifacts.image", expected)], token_service.redeemed)

    def test_nonvisual_token_cannot_retrieve_image_or_redeem(self):
        import app
        handler = self.build(
            method="GET", path="/api/visual-artifacts/run-1/after-map.png",
            body=b"", receipt=None,
        )
        handler._host_allowed = lambda: True
        with patch.object(app, "read_visual_image") as read:
            handler.do_GET()
        read.assert_not_called()
        self.assertEqual([], self.tokens.redeemed)
        self.assertEqual(403, self.responses[0][0])
