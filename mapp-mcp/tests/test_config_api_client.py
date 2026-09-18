"""The configuration API client against a real server.

Driven over a real socket rather than by stubbing urlopen, because what is
under test here is the parsing of an error response -- status, code, and the
field-level entries -- and a stub would agree with whatever this file assumed
those looked like.
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config_api_client import ConfigApiClient  # noqa: E402
from config_api_client import ConfigApiRefused  # noqa: E402
from config_api_client import ConfigApiUnavailable  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    status = 200
    body: dict = {}
    seen: list = []

    def _respond(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        Handler.seen.append({
            "method": self.command,
            "path": self.path,
            "body": json.loads(raw) if raw else None,
            "authorization": self.headers.get("Authorization"),
            "contentType": self.headers.get("Content-Type"),
        })
        encoded = json.dumps(Handler.body).encode()
        self.send_response(Handler.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args):
        pass


class ConfigApiClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.client = ConfigApiClient(endpoint=f"http://{host}:{port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.status = 200
        Handler.body = {"ok": True}
        Handler.seen = []

    def test_a_get_sends_no_body_and_no_content_type(self):
        """A GET with a body is a different request from one without, and the
        digest the credential is bound to distinguishes them."""
        self.client.get(path="/api/thing", query="a=1", token="t")
        sent = Handler.seen[0]
        self.assertEqual("GET", sent["method"])
        self.assertEqual("/api/thing?a=1", sent["path"])
        self.assertIsNone(sent["body"])
        self.assertIsNone(sent["contentType"])
        self.assertEqual("Bearer t", sent["authorization"])

    def test_a_post_sends_the_body_as_json(self):
        self.client.post(path="/api/sql/test", query="", token="t",
                         body={"layer": "Stops", "expression": "1"})
        sent = Handler.seen[0]
        self.assertEqual("POST", sent["method"])
        self.assertEqual({"layer": "Stops", "expression": "1"}, sent["body"])
        self.assertEqual("application/json", sent["contentType"])

    def test_a_refusal_carries_its_field_level_errors(self):
        """The reason this parsing exists: for a validation refusal the
        entries are the answer, and the top-level message is not."""
        Handler.status = 422
        Handler.body = {
            "error": "Expression test failed.",
            "errors": [{"path": "fieldfx",
                        "message": "SQL function is not allowed: pg_sleep."}],
        }

        with self.assertRaises(ConfigApiRefused) as raised:
            self.client.post(path="/api/sql/test", query="", token="t",
                             body={"layer": "Stops", "expression": "x"})

        refusal = raised.exception
        self.assertEqual(422, refusal.status)
        self.assertEqual("Expression test failed.", str(refusal))
        self.assertEqual(
            [{"path": "fieldfx",
              "message": "SQL function is not allowed: pg_sleep."}],
            refusal.errors,
        )

    def test_a_refusal_without_entries_reports_an_empty_list(self):
        """Never None, so every caller can iterate without checking."""
        Handler.status = 403
        Handler.body = {"error": "Refused.", "code": "auth.scope_required"}

        with self.assertRaises(ConfigApiRefused) as raised:
            self.client.get(path="/api/thing", query="", token="t")

        self.assertEqual([], raised.exception.errors)
        self.assertEqual("auth.scope_required", raised.exception.code)

    def test_a_non_list_errors_member_is_not_trusted(self):
        Handler.status = 422
        Handler.body = {"error": "Refused.", "errors": "not a list"}

        with self.assertRaises(ConfigApiRefused) as raised:
            self.client.get(path="/api/thing", query="", token="t")

        self.assertEqual([], raised.exception.errors)

    def test_an_unreachable_endpoint_is_unavailable_not_refused(self):
        client = ConfigApiClient(endpoint="http://127.0.0.1:1", timeout=1)
        with self.assertRaises(ConfigApiUnavailable):
            client.get(path="/api/thing", query="", token="t")


if __name__ == "__main__":
    unittest.main()
