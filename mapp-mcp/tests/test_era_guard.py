"""The four obligations of the protocol-era guard, one test class each.

The specification pre-emptively refuses the convenient proof: "enabling
`stateless_http` is not accepted as evidence that the legacy era is disabled".
So every assertion here is about what crosses the ASGI boundary, not about how
the inner application was configured.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import era_guard  # noqa: E402
from app import build_app  # noqa: E402
from asgi_harness import call  # noqa: E402

MODERN = {"MCP-Protocol-Version": era_guard.PROTOCOL_VERSION}


class Reached:
    """An inner app that records whether it ran, and what body it could read."""

    def __init__(self, *, session_id: str | None = None) -> None:
        self.calls = 0
        self.body = None
        self._session_id = session_id

    async def __call__(self, scope, receive, send):
        self.calls += 1
        chunks = []
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        self.body = b"".join(chunks)
        headers = [(b"content-length", b"0")]
        if self._session_id is not None:
            headers.append((b"mcp-session-id", self._session_id.encode()))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


def guarded(**kwargs):
    inner = Reached(**kwargs)
    app, resource = build_app(
        origin="http://mcp.localhost", issuer="http://mcp.localhost", inner=inner
    )
    return app, inner, resource


class VersionAdmissionTests(unittest.TestCase):
    def test_the_exact_revision_reaches_the_runtime(self) -> None:
        app, inner, _ = guarded()
        self.assertEqual(200, call(app, headers=MODERN, body=b"{}").status)
        self.assertEqual(1, inner.calls)

    def test_a_near_miss_is_not_the_revision(self) -> None:
        """Compared with ==, so a suffixed or padded value is a different string.

        A prefix or ordering comparison would admit "2026-07-28-beta", which is
        a client asserting agreement with a contract this server has not seen.
        """
        app, inner, _ = guarded()
        for value in ("2026-07-28-beta", "2026-07-29", "2027-01-01"):
            with self.subTest(value=value):
                response = call(app, headers={"MCP-Protocol-Version": value}, body=b"{}")
                self.assertEqual(400, response.status)
                self.assertEqual(0, inner.calls)

    def test_a_missing_version_is_a_validation_fault_not_a_version_fault(self) -> None:
        """The distinction the specification names explicitly.

        "Missing or malformed modern metadata receives the appropriate
        validation error, not a fabricated version error." A client that sent no
        version told to fix its version goes looking for a revision to change.
        """
        app, inner, _ = guarded()
        for headers in ({}, {"MCP-Protocol-Version": "   "}):
            with self.subTest(headers=headers):
                response = call(app, headers=headers, body=b"{}")
                self.assertEqual(era_guard.INVALID_REQUEST, response.json()["error"]["code"])
                self.assertEqual("protocol-version-missing", response.reason)
        self.assertEqual(0, inner.calls)

    def test_a_malformed_version_is_also_a_validation_fault(self) -> None:
        app, _, _ = guarded()
        for value in ("banana", "2026-7-28", "26-07-28", "2026/07/28", "2026-07"):
            with self.subTest(value=value):
                response = call(app, headers={"MCP-Protocol-Version": value}, body=b"{}")
                self.assertEqual(era_guard.INVALID_REQUEST, response.json()["error"]["code"])
                self.assertEqual("protocol-version-malformed", response.reason)

    def test_a_well_formed_older_revision_is_a_version_fault(self) -> None:
        app, _, _ = guarded()
        response = call(app, headers={"MCP-Protocol-Version": "2025-06-18"}, body=b"{}")
        self.assertEqual(
            era_guard.UNSUPPORTED_PROTOCOL_VERSION, response.json()["error"]["code"]
        )
        self.assertEqual("unsupported-protocol-version", response.reason)
        # And it says what it does speak, so the client need not guess.
        self.assertEqual(
            [era_guard.PROTOCOL_VERSION], response.json()["error"]["data"]["supported"]
        )

    def test_the_two_faults_do_not_share_a_code(self) -> None:
        """If they did, the distinction above would be decorative."""
        app, _, _ = guarded()
        missing = call(app, body=b"{}").json()["error"]["code"]
        older = call(
            app, headers={"MCP-Protocol-Version": "2025-06-18"}, body=b"{}"
        ).json()["error"]["code"]
        self.assertNotEqual(missing, older)


class LegacyInitializeTests(unittest.TestCase):
    def test_initialize_never_reaches_the_runtime(self) -> None:
        app, inner, _ = guarded()
        response = call(app, headers=MODERN, body=b'{"jsonrpc":"2.0","method":"initialize"}')
        self.assertEqual(400, response.status)
        self.assertEqual("legacy-initialize", response.reason)
        self.assertEqual(era_guard.METHOD_NOT_FOUND, response.json()["error"]["code"])
        self.assertEqual(0, inner.calls)

    def test_initialize_hidden_in_a_batch_is_still_refused(self) -> None:
        """Otherwise the guard is bypassed by adding a bracket."""
        app, inner, _ = guarded()
        body = b'[{"method":"tools/list"},{"method":"initialize"}]'
        self.assertEqual("legacy-initialize", call(app, headers=MODERN, body=body).reason)
        self.assertEqual(0, inner.calls)

    def test_an_undecodable_body_is_not_treated_as_initialize(self) -> None:
        """A parse error belongs to the runtime; guessing here answers the wrong question."""
        app, inner, _ = guarded()
        self.assertEqual(200, call(app, headers=MODERN, body=b"not json").status)
        self.assertEqual(1, inner.calls)


class SessionHeaderTests(unittest.TestCase):
    def test_a_session_id_from_the_runtime_is_stripped(self) -> None:
        """The second control, and the one that survives an SDK bump.

        The guard already refuses the requests that would create a session, so
        nothing downstream should mint one. This is what catches it if something
        downstream starts doing so anyway.
        """
        app, _, _ = guarded(session_id="legacy-session-1")
        response = call(app, headers=MODERN, body=b"{}")
        self.assertEqual(200, response.status)
        self.assertNotIn("mcp-session-id", response.headers)

    def test_it_is_stripped_on_pass_through_paths_too(self) -> None:
        app, _, _ = guarded(session_id="legacy-session-2")
        response = call(app, method="GET", path="/elsewhere")
        self.assertNotIn("mcp-session-id", response.headers)


class BodyHandlingTests(unittest.TestCase):
    def test_the_runtime_still_reads_the_body_the_guard_decoded(self) -> None:
        """The guard consumes receive to peek, so it has to replay it."""
        app, inner, _ = guarded()
        call(app, headers=MODERN, body=b'{"method":"tools/list"}')
        self.assertEqual(b'{"method":"tools/list"}', inner.body)

    def test_a_chunked_body_is_reassembled_and_replayed_whole(self) -> None:
        app, inner, _ = guarded()
        call(app, headers=MODERN, chunks=[b'{"method":', b'"tools/list"}'])
        self.assertEqual(b'{"method":"tools/list"}', inner.body)

    def test_a_body_past_the_ceiling_is_refused_without_buffering_it_all(self) -> None:
        """Caddy caps this too, but a guard that trusts the proxy in front of it
        stops working the moment anything reaches the component directly."""
        app, inner, _ = guarded()
        oversize = b"x" * (era_guard.MAX_BODY_BYTES + 1)
        response = call(app, headers=MODERN, body=oversize)
        self.assertEqual("body-too-large", response.reason)
        self.assertEqual(0, inner.calls)


class PassThroughTests(unittest.TestCase):
    def test_the_metadata_get_never_meets_the_version_check(self) -> None:
        """It is unauthenticated and read-only: a client reads it *before* it
        can know anything, so requiring a protocol header would be a loop."""
        app, _, _ = guarded()
        response = call(
            app, method="GET", path="/.well-known/oauth-protected-resource/mcp"
        )
        self.assertEqual(200, response.status)

    def test_a_post_to_another_path_is_not_screened(self) -> None:
        """The guard screens one endpoint, not the whole application.

        Dropping the path test survived mutation until this existed, because
        every other test here posts to /mcp. A guard that screened every POST
        would impose the RPC protocol header on anything else the component
        ever serves -- and would do it silently, since the only symptom is a
        400 on a route that never asked for a version.
        """
        app, inner, _ = guarded()
        response = call(app, method="POST", path="/elsewhere", body=b"{}")
        self.assertEqual(200, response.status)
        self.assertEqual(1, inner.calls)

    def test_a_get_on_the_rpc_path_is_the_runtime_s_answer_not_the_guard_s(self) -> None:
        app, inner, _ = guarded()
        call(app, method="GET", path="/mcp")
        self.assertEqual(1, inner.calls, "the guard only screens POSTs")


if __name__ == "__main__":
    unittest.main()
