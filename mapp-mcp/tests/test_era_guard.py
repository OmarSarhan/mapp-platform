"""The obligations of the protocol-era guard, one test class each.

The specification pre-emptively refuses the convenient proof: "enabling
`stateless_http` is not accepted as evidence that the legacy era is disabled".
So every assertion here is about what crosses the ASGI boundary, not about how
the inner application was configured.

That rule now cuts the other way too. This server serves two handshake
revisions as well as the modern one, and the reason the guard did not simply get
shorter is measurable against the installed SDK: left to itself it negotiates an
`initialize` to whatever the client offers -- 2024-11-05 verbatim, `"zzz"`
counter-offered the newest handshake revision -- so the offered revision has to
be read out of the body and checked here. `HandshakeAdmissionTests` is that
control, and every refusal in it is a revision the SDK behind the guard would
have served.

Nothing here writes out a refused revision as a literal. The admitted list grew
once already, and a literal that happened to name a newly admitted revision
would assert the opposite of the truth while still passing, because the test and
the code get edited together. `REFUSED` is derived instead.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import era_guard  # noqa: E402
from app import build_app  # noqa: E402
from asgi_harness import StubIntrospection, active, call, refused_revision  # noqa: E402

TOKEN = "mapp_a_test"
#: The guard runs before authentication, but these tests assert what reaches the
#: runtime, so they carry a credential that resolves. Without one every "the
#: runtime was reached" assertion would pass or fail on authentication instead.
MODERN = {
    "MCP-Protocol-Version": era_guard.MODERN_VERSION,
    "Authorization": f"Bearer {TOKEN}",
}
#: A representative admitted handshake revision. Tests that care about *all* of
#: them iterate `era_guard.HANDSHAKE_VERSIONS` instead of trusting this one.
NEWEST_HANDSHAKE = era_guard.HANDSHAKE_VERSIONS[-1]
HANDSHAKE = {
    "MCP-Protocol-Version": NEWEST_HANDSHAKE,
    "Authorization": f"Bearer {TOKEN}",
}
#: A credential and no declared revision -- the shape of the one request that
#: cannot carry one, and the shape every "reaches the runtime" assertion below
#: needs, since the guard admitting a request only moves it on to authentication.
UNVERSIONED = {"Authorization": f"Bearer {TOKEN}"}

REFUSED = refused_revision()


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
        origin="http://mcp.localhost",
        issuer="http://mcp.localhost",
        inner=inner,
        introspection=StubIntrospection({TOKEN: active()}),
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
        response = call(app, headers={"MCP-Protocol-Version": REFUSED}, body=b"{}")
        self.assertEqual(
            era_guard.UNSUPPORTED_PROTOCOL_VERSION, response.json()["error"]["code"]
        )
        self.assertEqual("unsupported-protocol-version", response.reason)
        # And it says what it does speak, so the client need not guess. Both
        # revisions: a client told only about the modern one cannot discover
        # that the handshake it already speaks would have been accepted.
        self.assertEqual(
            list(era_guard.SERVED_VERSIONS),
            response.json()["error"]["data"]["supported"],
        )
        for admitted in era_guard.HANDSHAKE_VERSIONS:
            self.assertIn(admitted, response.json()["error"]["data"]["supported"])

    def test_the_code_is_the_one_the_ecosystem_uses(self) -> None:
        """Not a project-chosen number, which is what it was.

        A version complaint is only actionable if the client recognises it. The
        SDK defines the constant; this module restates it because it is
        stdlib-only by design, so this is the join that keeps the two in step.
        """
        from mcp_types import UNSUPPORTED_PROTOCOL_VERSION

        self.assertEqual(UNSUPPORTED_PROTOCOL_VERSION, era_guard.UNSUPPORTED_PROTOCOL_VERSION)

    def test_the_two_faults_do_not_share_a_code(self) -> None:
        """If they did, the distinction above would be decorative."""
        app, _, _ = guarded()
        missing = call(app, body=b"{}").json()["error"]["code"]
        older = call(
            app, headers={"MCP-Protocol-Version": REFUSED}, body=b"{}"
        ).json()["error"]["code"]
        self.assertNotEqual(missing, older)


def initialize(offer=NEWEST_HANDSHAKE):
    """A handshake request offering `offer`, or offering nothing when None."""
    import json

    params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
    if offer is not None:
        params["protocolVersion"] = offer
    return json.dumps(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": params}
    ).encode()


class LegacyInitializeTests(unittest.TestCase):
    def test_initialize_under_the_modern_revision_is_not_a_method(self) -> None:
        """A client that declared the modern revision and then sent the
        handshake contradicted itself, and the method genuinely does not exist
        there -- so it is refused as a method, not as a version."""
        app, inner, _ = guarded()
        response = call(app, headers=MODERN, body=initialize())
        self.assertEqual(400, response.status)
        self.assertEqual("legacy-initialize", response.reason)
        self.assertEqual(era_guard.METHOD_NOT_FOUND, response.json()["error"]["code"])
        self.assertEqual(0, inner.calls)

    def test_initialize_hidden_in_a_batch_is_refused_whatever_the_header(self) -> None:
        """Otherwise the guard is bypassed by adding a bracket.

        Now that a header-less initialize is admitted, the bracket is worth more
        than it was: a batch would carry other calls in beside a request that
        declared no revision of its own.
        """
        body = b'[{"method":"tools/list"},{"method":"initialize"}]'
        for headers in (MODERN, HANDSHAKE, {}):
            with self.subTest(headers=headers):
                app, inner, _ = guarded()
                self.assertEqual("batched-initialize", call(app, headers=headers, body=body).reason)
                self.assertEqual(0, inner.calls)

    def test_an_undecodable_body_is_not_treated_as_initialize(self) -> None:
        """A parse error belongs to the runtime; guessing here answers the wrong question."""
        app, inner, _ = guarded()
        self.assertEqual(200, call(app, headers=MODERN, body=b"not json").status)
        self.assertEqual(1, inner.calls)

    def test_an_undecodable_body_without_a_header_is_still_missing_a_version(self) -> None:
        """The header-less door is open only to a handshake, and an unparseable
        body has not shown it is one."""
        app, inner, _ = guarded()
        response = call(app, body=b"not json")
        self.assertEqual("protocol-version-missing", response.reason)
        self.assertEqual(0, inner.calls)


class HandshakeAdmissionTests(unittest.TestCase):
    """The legacy era, held to exactly one revision.

    Every refusal here is a revision the SDK behind this guard would have
    served: measured against the installed version, an offer of 2024-11-05 is
    negotiated verbatim and an offer of "zzz" is counter-offered 2025-11-25. So
    these are not restatements of SDK behaviour -- they are the only thing
    standing between "we serve 2025-11-25" and "we serve whatever is asked".
    """

    def test_a_header_less_handshake_reaches_the_runtime(self) -> None:
        """The request a real client actually sends.

        Claude Code 2.1.272 opens with `initialize` carrying no
        MCP-Protocol-Version header at all -- there is nothing it could put
        there, because this request is what decides the revision. Refusing it
        for a missing header is what made this server unreachable.
        """
        app, inner, _ = guarded()
        self.assertEqual(200, call(app, headers=UNVERSIONED, body=initialize()).status)
        self.assertEqual(1, inner.calls)

    def test_an_uncredentialed_handshake_is_a_401_not_a_protocol_error(self) -> None:
        """How the client discovers where to authenticate.

        A real client opens with an unauthenticated `initialize`, reads the
        RFC 9728 pointer out of the 401, and comes back with a token. If the
        guard answered this with a protocol complaint instead, the client would
        be told to fix a revision when what it needs is to sign in.
        """
        app, _, _ = guarded()
        response = call(app, body=initialize())
        self.assertEqual(401, response.status)

    def test_a_declared_handshake_reaches_the_runtime(self) -> None:
        app, inner, _ = guarded()
        self.assertEqual(200, call(app, headers=HANDSHAKE, body=initialize()).status)
        self.assertEqual(1, inner.calls)

    def test_ordinary_traffic_under_the_handshake_revision_is_served(self) -> None:
        """Everything after the handshake does carry the header, and must pass."""
        app, inner, _ = guarded()
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
        self.assertEqual(200, call(app, headers=HANDSHAKE, body=body).status)
        self.assertEqual(1, inner.calls)

    def test_the_admitted_set_is_exactly_what_was_decided(self) -> None:
        """Pinned deliberately, because everything else here is derived.

        `REFUSED` adapts to whatever the guard admits, which is right for "does
        it refuse what the SDK would serve" and wrong as the only check: a
        widened list would simply shrink the refused set and every other test
        would go on passing. This is the assertion someone has to edit on
        purpose, so the list cannot grow quietly.

        Each entry earns its place by a measured client, not by age:

          2025-06-18  Codex CLI 0.154.0 and Gemini CLI 0.60.0 offer it
          2025-11-25  Claude Code 2.1.272 offers it

        The two the SDK also serves -- 2024-11-05 and 2025-03-26 -- are refused
        because no target ecosystem needs them.
        """
        self.assertEqual(("2025-06-18", "2025-11-25"), era_guard.HANDSHAKE_VERSIONS)
        self.assertEqual(
            ("2025-06-18", "2025-11-25", "2026-07-28"), era_guard.SERVED_VERSIONS
        )

    def test_every_admitted_handshake_revision_is_served(self) -> None:
        """Each entry is here because a shipped client of a target ecosystem
        speaks it and nothing newer, so each has to actually work.

        Iterated rather than spot-checked: the list grew from one to two when
        Codex and Gemini turned out to sit a revision behind Claude, and a test
        pinned to one of them would have passed while the other was refused.
        """
        for offer in era_guard.HANDSHAKE_VERSIONS:
            with self.subTest(offer=offer):
                app, inner, _ = guarded()
                response = call(app, headers=UNVERSIONED, body=initialize(offer))
                self.assertEqual(200, response.status, f"{offer} was refused")
                self.assertEqual(1, inner.calls)
                # And the same revision declared in the header afterwards.
                app, inner, _ = guarded()
                self.assertEqual(
                    200,
                    call(
                        app,
                        headers={"MCP-Protocol-Version": offer,
                                 "Authorization": f"Bearer {TOKEN}"},
                        body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                    ).status,
                )

    def test_an_older_offer_is_refused_though_the_sdk_would_serve_it(self) -> None:
        """The whole reason the guard reads the body.

        Derived as "what the SDK will negotiate, minus what this server admits"
        rather than written out. The literal list had `2025-06-18` in it, and
        when that revision was admitted for Codex and Gemini the test would have
        gone on asserting it was refused -- passing only because the assertion
        and the code were edited together, which is the coupling a derived list
        removes.
        """
        from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

        refused = [
            version
            for version in HANDSHAKE_PROTOCOL_VERSIONS
            if version not in era_guard.HANDSHAKE_VERSIONS
        ]
        self.assertTrue(refused, "the SDK serves nothing this guard refuses")
        for offer in refused:
            with self.subTest(offer=offer):
                app, inner, _ = guarded()
                response = call(app, headers=UNVERSIONED, body=initialize(offer))
                # Status first: an admitted request answers 200 with no error
                # object at all, and reaching into one that is not there reports
                # a TypeError rather than "this revision was served".
                self.assertEqual(400, response.status, f"{offer} was admitted")
                self.assertEqual(
                    era_guard.UNSUPPORTED_PROTOCOL_VERSION,
                    response.json()["error"]["code"],
                )
                self.assertEqual("unsupported-protocol-version", response.reason)
                self.assertIn(offer, response.json()["error"]["message"])
                self.assertEqual(0, inner.calls)

    def test_an_unrecognisable_offer_is_refused_rather_than_counter_offered(self) -> None:
        """The SDK answers "zzz" with a 2025-11-25 counter-offer and carries on.

        That is a server deciding, on a client's behalf, that a revision it
        could not parse was close enough -- which is exactly the silent
        widening the allowlist exists to prevent.
        """
        for offer in ("zzz", "1999-01-01", "2026-07-28-beta", ""):
            with self.subTest(offer=offer):
                app, inner, _ = guarded()
                response = call(app, headers=UNVERSIONED, body=initialize(offer))
                self.assertEqual(400, response.status, f"{offer!r} was admitted")
                self.assertEqual("unsupported-protocol-version", response.reason)
                self.assertEqual(0, inner.calls)

    def test_a_handshake_offering_no_revision_is_refused(self) -> None:
        """Absent is not the admitted revision, and must not read as one."""
        app, inner, _ = guarded()
        response = call(app, headers=UNVERSIONED, body=initialize(None))
        self.assertEqual(400, response.status, "a handshake naming no revision was admitted")
        self.assertEqual("unsupported-protocol-version", response.reason)
        self.assertEqual(0, inner.calls)

    def test_the_handshake_cannot_reach_the_modern_revision(self) -> None:
        """The two eras are different protocols, not two numbers.

        The SDK agrees -- an `initialize` offering 2026-07-28 is negotiated
        *down* to 2025-11-25 -- but a client that asked for the modern revision
        and was silently given the legacy one has been told nothing about it.
        """
        app, inner, _ = guarded()
        response = call(app, headers=HANDSHAKE, body=initialize(era_guard.MODERN_VERSION))
        self.assertEqual(400, response.status, "the modern revision was reached by handshake")
        self.assertEqual("unsupported-protocol-version", response.reason)
        self.assertEqual(0, inner.calls)

    def test_a_header_less_request_that_is_not_a_handshake_is_still_refused(self) -> None:
        """The door opened for `initialize` is not a door for everything else."""
        app, inner, _ = guarded()
        for body in (b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}', b"{}"):
            with self.subTest(body=body):
                response = call(app, body=body)
                self.assertEqual("protocol-version-missing", response.reason)
        self.assertEqual(0, inner.calls)

    def test_a_malformed_header_is_not_rescued_by_a_valid_handshake(self) -> None:
        """A client that sent a version header at all is held to it."""
        app, inner, _ = guarded()
        response = call(
            app, headers={"MCP-Protocol-Version": "banana"}, body=initialize()
        )
        self.assertEqual("protocol-version-malformed", response.reason)
        self.assertEqual(0, inner.calls)


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


class ReceiveDelegationTests(unittest.TestCase):
    """After the body, the guard must hand back the real stream underneath.

    The guard buffers the body to look for `initialize`, so it owes the inner
    application a `receive` that replays it. The first version then answered
    `http.disconnect` to every later call, which looks harmless -- the body is
    finished, what else is there to say.

    A streaming application keeps reading `receive` to notice the client going
    away. A fabricated disconnect therefore tells it the caller left, and it
    abandons the response without ever starting one. The symptom is uvicorn's
    "ASGI callable returned without starting response" and no traceback, because
    nothing raised. Every test here passed throughout: a non-streaming double
    reads once and never asks again.
    """

    def test_a_later_read_reaches_the_real_receive(self) -> None:
        seen = []

        class Streaming:
            async def __call__(self, scope, receive, send):
                seen.append(await receive())
                # What a streaming app does while it works: watch for the
                # client leaving rather than assume it has.
                seen.append(await receive())
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"0")],
                })
                await send({"type": "http.response.body", "body": b""})

        app, _ = build_app(
            origin="http://mcp.localhost",
            issuer="http://mcp.localhost",
            inner=Streaming(),
            introspection=StubIntrospection({TOKEN: active()}),
        )

        # Driven directly rather than through the harness, because the harness
        # answers http.disconnect once its queue empties -- the very value the
        # bug fabricates, so a test using it cannot tell the two apart. This
        # receive yields a marker no guard could invent.
        import asyncio

        body = b'{"method":"tools/list"}'
        queue = [
            {"type": "http.request", "body": body, "more_body": False},
            {"type": "http.request", "body": b"", "more_body": False, "marker": True},
        ]
        messages = []

        async def receive():
            return queue.pop(0) if queue else {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "method": "POST", "path": "/mcp",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in MODERN.items()
            ],
        }
        asyncio.run(app(scope, receive, send))

        start = next(m for m in messages if m["type"] == "http.response.start")
        self.assertEqual(200, start["status"], "the runtime abandoned the response")
        self.assertEqual(body, seen[0]["body"])
        self.assertTrue(
            seen[1].get("marker"),
            "the guard answered its own disconnect instead of the real stream",
        )


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
        """The guard screens POSTs; a GET is the runtime's 405 to give.

        Carries a credential because /mcp is a protected resource, so an
        anonymous GET is refused by authentication before the question this
        test asks can be reached.
        """
        app, inner, _ = guarded()
        call(app, method="GET", path="/mcp", headers=MODERN)
        self.assertEqual(1, inner.calls, "the guard only screens POSTs")


if __name__ == "__main__":
    unittest.main()
