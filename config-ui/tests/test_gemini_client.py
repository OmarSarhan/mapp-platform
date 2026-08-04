from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http import HTTPStatus
from unittest.mock import patch

from gemini_client import GeminiClientError, GeminiSemanticClient


class FakeResponse:
    def __init__(self, payload, *, content_type="application/json"):
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.body = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8")
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def response(profile=None):
    profile = profile or {
        "displayName": "Roads",
        "description": "Road centreline geometry.",
        "tags": ["transport"],
        "caveats": ["Meaning is inferred from metadata only."],
    }
    return {
        "candidates": [{
            "finishReason": "STOP",
            "content": {
                "parts": [{"text": json.dumps(profile)}],
            },
        }]
    }


class GeminiSemanticClientTests(unittest.TestCase):
    def setUp(self):
        self.client = GeminiSemanticClient(
            "gemini-secret",
            model="gemini-3.6-flash",
        )

    def test_one_shot_structured_request_keeps_key_out_of_body_and_url(self):
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(response())

        with patch.object(
            self.client.opener,
            "open",
            side_effect=open_request,
        ):
            result = self.client.generate(
                {"target": {"kind": "table"}, "table": {"name": "roads"}},
                target_kind="table",
            )

        request = captured["request"]
        body = json.loads(request.data)
        self.assertEqual("Roads", result["displayName"])
        self.assertEqual(30, captured["timeout"])
        self.assertTrue(request.full_url.startswith(
            "https://generativelanguage.googleapis.com/v1beta/models/"
        ))
        self.assertNotIn("gemini-secret", request.full_url)
        self.assertNotIn(b"gemini-secret", request.data)
        self.assertEqual(
            "gemini-secret",
            request.headers["X-goog-api-key"],
        )
        self.assertIs(body["store"], False)
        self.assertIn("systemInstruction", body)
        self.assertNotIn("system_instruction", body)
        system_text = body["systemInstruction"]["parts"][0]["text"]
        self.assertIn("untrusted data", system_text)
        self.assertIn("bounded, non-exhaustive hints", system_text)
        for unsupported_claim in (
            "completeness",
            "quality",
            "uniqueness",
            "sensitivity",
        ):
            self.assertIn(unsupported_claim, system_text)
        config = body["generationConfig"]
        response_format = config["responseFormat"]["text"]
        self.assertEqual("APPLICATION_JSON", response_format["mimeType"])
        self.assertFalse(response_format["schema"]["additionalProperties"])
        self.assertNotIn("minLength", json.dumps(response_format["schema"]))
        self.assertNotIn("maxLength", json.dumps(response_format["schema"]))
        self.assertNotIn("temperature", config)
        self.assertNotIn("topP", config)
        self.assertNotIn("topK", config)

    def test_output_is_closed_bounded_and_duplicate_free(self):
        invalid_profiles = (
            {
                "displayName": "Roads",
                "description": "Description",
                "tags": [],
                "caveats": [],
                "extra": "unsupported",
            },
            {
                "displayName": " ",
                "description": "Description",
                "tags": [],
                "caveats": [],
            },
            {
                "displayName": "Roads",
                "description": "Description",
                "tags": ["transport", "TRANSPORT"],
                "caveats": [],
            },
            {
                "displayName": "Roads",
                "description": "Description",
                "tags": ["x"] * 13,
                "caveats": [],
            },
        )
        for profile in invalid_profiles:
            with self.subTest(profile=profile):
                with patch.object(
                    self.client.opener,
                    "open",
                    return_value=FakeResponse(response(profile)),
                ):
                    with self.assertRaises(GeminiClientError):
                        self.client.generate({}, target_kind="table")

        duplicate_inner = response()
        duplicate_inner["candidates"][0]["content"]["parts"][0]["text"] = (
            '{"displayName":"A","displayName":"B",'
            '"description":"D","tags":[],"caveats":[]}'
        )
        with patch.object(
            self.client.opener,
            "open",
            return_value=FakeResponse(duplicate_inner),
        ):
            with self.assertRaisesRegex(GeminiClientError, "invalid"):
                self.client.generate({}, target_kind="table")

    def test_envelope_content_type_size_and_shape_are_strict(self):
        signed = response()
        signed["candidates"][0]["content"]["parts"][0][
            "thoughtSignature"
        ] = "opaque-provider-signature"
        with patch.object(
            self.client.opener,
            "open",
            return_value=FakeResponse(signed),
        ):
            self.assertEqual(
                "Roads",
                self.client.generate(
                    {},
                    target_kind="table",
                )["displayName"],
            )

        cases = (
            (
                FakeResponse(response(), content_type="text/plain"),
                "invalid response",
            ),
            (FakeResponse(b'{"candidates":[],"candidates":[]}'), "invalid JSON"),
            (FakeResponse({"candidates": []}), "invalid semantic draft"),
            (
                FakeResponse({
                    "candidates": [{
                        "finishReason": "MAX_TOKENS",
                        "content": {"parts": [{"text": "{}"}]},
                    }]
                }),
                "invalid semantic draft",
            ),
            (
                FakeResponse({
                    "candidates": [{
                        "finishReason": "STOP",
                        "content": {
                            "parts": [{
                                "text": json.dumps({
                                    "displayName": "Roads",
                                    "description": "Description",
                                    "tags": [],
                                    "caveats": [],
                                }),
                                "unexpected": "provider field",
                            }],
                        },
                    }]
                }),
                "invalid semantic draft",
            ),
            (
                FakeResponse({
                    "candidates": [{
                        "finishReason": "STOP",
                        "content": {
                            "parts": [{
                                "text": json.dumps({
                                    "displayName": "Roads",
                                    "description": "Description",
                                    "tags": [],
                                    "caveats": [],
                                }),
                                "thoughtSignature": 7,
                            }],
                        },
                    }]
                }),
                "invalid semantic draft",
            ),
        )
        for fake, expected in cases:
            with self.subTest(expected=expected):
                with patch.object(
                    self.client.opener,
                    "open",
                    return_value=fake,
                ):
                    with self.assertRaisesRegex(
                        GeminiClientError,
                        expected,
                    ):
                        self.client.generate({}, target_kind="table")

        self.client.max_response_bytes = 8
        with patch.object(
            self.client.opener,
            "open",
            return_value=FakeResponse(response()),
        ):
            with self.assertRaisesRegex(GeminiClientError, "too large"):
                self.client.generate({}, target_kind="table")

    def test_upstream_errors_redirects_and_timeouts_are_sanitized(self):
        headers = Message()
        headers["Content-Type"] = "application/json"
        upstream = urllib.error.HTTPError(
            self.client.endpoint,
            302,
            "gemini-secret redirect",
            headers,
            io.BytesIO(b'{"error":"gemini-secret provider detail"}'),
        )
        for error, expected_status, expected_code in (
            (
                upstream,
                HTTPStatus.BAD_GATEWAY,
                "semantic.generation_failed",
            ),
            (
                urllib.error.URLError("gemini-secret DNS detail"),
                HTTPStatus.BAD_GATEWAY,
                "semantic.generation_failed",
            ),
            (
                TimeoutError("gemini-secret timeout"),
                HTTPStatus.GATEWAY_TIMEOUT,
                "semantic.generation_timeout",
            ),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(
                    self.client.opener,
                    "open",
                    side_effect=error,
                ):
                    with self.assertRaises(GeminiClientError) as raised:
                        self.client.generate({}, target_kind="table")
                self.assertEqual(expected_status, raised.exception.status)
                self.assertEqual(expected_code, raised.exception.code)
                self.assertNotIn("gemini-secret", str(raised.exception))

    def test_invalid_model_target_context_and_concurrency_fail_closed(self):
        invalid_configurations = (
            {"api_key": " key"},
            {"api_key": "key\nheader"},
            {"api_key": "key", "model": "models/other"},
            {"api_key": "key", "timeout": 0},
            {"api_key": "key", "timeout": 121},
            {"api_key": "key", "timeout": float("nan")},
            {"api_key": "key", "max_response_bytes": 0},
            {"api_key": "key", "max_concurrency": 0},
            {"api_key": "key", "max_concurrency": 11},
            {"api_key": "key", "max_concurrency": True},
        )
        for configuration in invalid_configurations:
            with self.subTest(configuration=configuration):
                with self.assertRaises(GeminiClientError) as config_error:
                    GeminiSemanticClient(**configuration)
                self.assertEqual(
                    "semantic.generation_not_configured",
                    config_error.exception.code,
                )

        with self.assertRaises(GeminiClientError) as target_error:
            self.client.generate({}, target_kind="row")
        self.assertEqual(
            "semantic.generation_invalid_request",
            target_error.exception.code,
        )

        with self.assertRaises(GeminiClientError) as context_error:
            self.client.generate(
                {"value": "x" * (256 * 1024)},
                target_kind="table",
            )
        self.assertEqual(
            "semantic.generation_context_too_large",
            context_error.exception.code,
        )

        single_slot_client = GeminiSemanticClient(
            "gemini-secret",
            max_concurrency=1,
        )
        single_slot_client._generation_slot.acquire()
        try:
            with self.assertRaises(GeminiClientError) as busy:
                single_slot_client.generate({}, target_kind="table")
        finally:
            single_slot_client._generation_slot.release()
        self.assertEqual("semantic.generation_busy", busy.exception.code)

    def test_generation_slots_allow_parallel_requests_and_fail_busy_at_capacity(self):
        client = GeminiSemanticClient(
            "gemini-secret",
            max_concurrency=2,
        )
        entered = threading.Barrier(3)
        release = threading.Event()

        def open_request(_request, timeout):
            self.assertEqual(30, timeout)
            entered.wait(timeout=2)
            if not release.wait(timeout=2):
                raise AssertionError("parallel Gemini request was not released")
            return FakeResponse(response())

        with patch.object(client.opener, "open", side_effect=open_request):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        client.generate,
                        {"target": {"kind": "table"}},
                        target_kind="table",
                    )
                    for _ in range(2)
                ]
                try:
                    entered.wait(timeout=2)
                    with self.assertRaises(GeminiClientError) as busy:
                        client.generate({}, target_kind="table")
                    self.assertEqual(
                        "semantic.generation_busy",
                        busy.exception.code,
                    )
                finally:
                    release.set()
                self.assertEqual(
                    ["Roads", "Roads"],
                    [future.result(timeout=2)["displayName"] for future in futures],
                )

        default_client = GeminiSemanticClient("gemini-secret")
        acquired = [
            default_client._generation_slot.acquire(blocking=False)
            for _ in range(10)
        ]
        try:
            self.assertTrue(all(acquired))
            self.assertFalse(
                default_client._generation_slot.acquire(blocking=False)
            )
        finally:
            for did_acquire in acquired:
                if did_acquire:
                    default_client._generation_slot.release()

    def test_provider_rate_limit_is_distinct_and_never_retried(self):
        headers = Message()
        headers["Content-Type"] = "application/json"
        error = urllib.error.HTTPError(
            self.client.endpoint,
            429,
            "Too Many Requests",
            headers,
            io.BytesIO(b'{"error":"provider detail"}'),
        )
        with patch.object(
            self.client.opener,
            "open",
            side_effect=error,
        ) as opened:
            with self.assertRaises(GeminiClientError) as raised:
                self.client.generate({}, target_kind="table")
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, raised.exception.status)
        self.assertEqual(
            "semantic.generation_rate_limited",
            raised.exception.code,
        )
        opened.assert_called_once()


if __name__ == "__main__":
    unittest.main()
