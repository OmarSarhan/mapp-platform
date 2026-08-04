from __future__ import annotations

import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from semantic_client import SemanticClient, SemanticClientError


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.status = status
        self.headers = Message()
        supplied_headers = headers or {}
        if not any(
            key.lower() == "content-type" for key in supplied_headers
        ):
            self.headers["Content-Type"] = "application/json"
        for key, value in supplied_headers.items():
            self.headers[key] = value
        self._body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]


class SemanticClientTests(unittest.TestCase):
    def setUp(self):
        self.client = SemanticClient(
            "http://semantic-service:8080",
            "internal-secret",
        )

    def test_request_uses_only_internal_auth_and_trusted_context(self):
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({"catalogRevision": 4})

        with patch.object(self.client.opener, "open", side_effect=open_request):
            result = self.client.request(
                "/v1/status",
                actor="token:abc",
                scopes=["semantic:inspect"],
            )

        request = captured["request"]
        self.assertEqual(result["catalogRevision"], 4)
        self.assertEqual(
            request.headers["Authorization"],
            "Bearer internal-secret",
        )
        self.assertEqual(request.headers["X-mapp-actor"], "token:abc")
        self.assertEqual(
            request.headers["X-mapp-scopes"],
            "semantic:inspect",
        )
        self.assertNotIn("Cookie", request.headers)

    def test_strict_json_and_response_bound_are_enforced(self):
        with patch.object(
            self.client.opener,
            "open",
            return_value=FakeResponse(b'{"value": NaN}'),
        ):
            with self.assertRaisesRegex(
                SemanticClientError,
                "invalid JSON",
            ):
                self.client.request("/v1/status")

        self.client.max_response_bytes = 8
        with patch.object(
            self.client.opener,
            "open",
            return_value=FakeResponse(b'{"value":"too large"}'),
        ):
            with self.assertRaisesRegex(
                SemanticClientError,
                "response is too large",
            ):
                self.client.request("/v1/status")

    def test_success_response_rejects_duplicate_keys(self):
        with patch.object(
            self.client.opener,
            "open",
            return_value=FakeResponse(b'{"value":1,"value":2}'),
        ):
            with self.assertRaisesRegex(
                SemanticClientError,
                "invalid JSON",
            ):
                self.client.request("/v1/status")

    def test_success_response_requires_json_content_type_with_parameters_allowed(self):
        with patch.object(
            self.client.opener,
            "open",
            return_value=FakeResponse(
                {"catalogRevision": 4},
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                },
            ),
        ):
            self.assertEqual(
                self.client.request("/v1/status")["catalogRevision"],
                4,
            )

        with patch.object(
            self.client.opener,
            "open",
            return_value=FakeResponse(
                b'{"catalogRevision":4}',
                headers={"Content-Type": "text/plain"},
            ),
        ):
            with self.assertRaisesRegex(
                SemanticClientError,
                "Content-Type must be application/json",
            ):
                self.client.request("/v1/status")

    def test_http_error_is_structured_without_echoing_internal_token(self):
        body = json.dumps(
            {
                "error": "failed internal-secret",
                "code": "semantic.asset_missing",
            }
        ).encode()
        headers = Message()
        headers["Content-Type"] = "application/json; charset=utf-8"
        error = urllib.error.HTTPError(
            "http://semantic-service:8080/v1/assets/missing",
            404,
            "Not Found",
            headers,
            io.BytesIO(body),
        )
        with patch.object(self.client.opener, "open", side_effect=error):
            with self.assertRaises(SemanticClientError) as raised:
                self.client.request("/v1/assets/missing")

        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(
            raised.exception.payload["code"],
            "semantic.asset_missing",
        )
        self.assertNotIn("internal-secret", str(raised.exception))
        self.assertNotIn(
            "internal-secret",
            json.dumps(raised.exception.payload),
        )

    def test_http_error_rejects_non_json_and_duplicate_key_responses(self):
        cases = (
            (
                "content type",
                b'{"error":"untrusted","code":"untrusted"}',
                "text/plain",
                "Content-Type must be application/json",
            ),
            (
                "duplicate key",
                b'{"error":"first","error":"second","code":"untrusted"}',
                "application/json",
                "invalid JSON",
            ),
        )
        for label, body, content_type, expected in cases:
            with self.subTest(label=label):
                headers = Message()
                headers["Content-Type"] = content_type
                error = urllib.error.HTTPError(
                    "http://semantic-service:8080/v1/status",
                    502,
                    "Bad Gateway",
                    headers,
                    io.BytesIO(body),
                )
                with patch.object(self.client.opener, "open", side_effect=error):
                    with self.assertRaises(SemanticClientError) as raised:
                        self.client.request("/v1/status")

                self.assertEqual(raised.exception.status, 502)
                self.assertIn(expected, str(raised.exception))
                self.assertNotIn("code", raised.exception.payload)

    def test_paths_must_be_internal_absolute_paths(self):
        for path in (
            "v1/status",
            "//attacker.example/status",
            "/v1/../secret",
            "/v1/status?bad\nheader=value",
        ):
            with self.subTest(path=path):
                with self.assertRaises(SemanticClientError):
                    self.client.request(path)


if __name__ == "__main__":
    unittest.main()
