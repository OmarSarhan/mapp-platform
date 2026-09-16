"""Drive an ASGI app directly, without a server.

The guard is middleware, so the thing under test is the ASGI contract itself:
what it does with scope, what it reads from receive, and exactly what messages
it sends. A real HTTP server in front of it would hide the message sequence,
which is where the obligations live -- "never emits Mcp-Session-Id" is a claim
about a http.response.start message, not about a rendered response.
"""

from __future__ import annotations

import asyncio
import json


class Response:
    def __init__(self, messages) -> None:
        start = next(m for m in messages if m["type"] == "http.response.start")
        self.status = start["status"]
        self.headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in start.get("headers", [])
        }
        self.body = b"".join(
            m.get("body", b"") for m in messages if m["type"] == "http.response.body"
        )

    def json(self):
        return json.loads(self.body)

    @property
    def reason(self):
        """The guard's discriminator, or None when this is not a guard refusal."""
        try:
            return self.json()["error"]["data"]["reason"]
        except (ValueError, KeyError, TypeError):
            return None


def call(app, *, method="POST", path="/mcp", headers=None, body=b"", chunks=None):
    """One request. ``chunks`` sends the body in pieces, as a real client may."""
    raw = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {"type": "http", "method": method, "path": path, "headers": raw}

    pieces = list(chunks) if chunks is not None else [body]
    queue = [
        {"type": "http.request", "body": piece, "more_body": index < len(pieces) - 1}
        for index, piece in enumerate(pieces)
    ]
    messages = []

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return Response(messages)


class StubIntrospection:
    """An introspection endpoint that answers from a dict, without a network.

    Tests of the guard still go through the real factory, so they need a
    credential that resolves; tests of authentication need to choose the answer.
    One stub serves both, and ``calls`` is what proves the cache is a cache.
    """

    def __init__(self, records=None, *, raises=None) -> None:
        self.records = records or {}
        self.raises = raises
        self.calls = 0

    def introspect(self, token):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.records.get(token, {"active": False})


def active(*, scopes="mcp:connect inspect", audience="http://mcp.localhost/mcp",
           grant="oauth:test-grant", client="mcp-test-client"):
    return {
        "active": True,
        "scope": scopes,
        "sub": grant,
        "client_id": client,
        "aud": audience,
        "exp": 9999999999,
    }


def refused_revision() -> str:
    """A revision the SDK will negotiate and this server refuses.

    Derived, never written out. `2025-06-18` was a literal in three tests --
    "a well-formed older revision", "the two faults do not share a code" and
    "the guard refuses before a credential is resolved" -- and when Codex and
    Gemini turned out to need that revision, all three would have gone on
    asserting it was refused. They would have failed loudly here, which is the
    good case; the bad case is a literal that keeps passing while meaning the
    opposite. Deriving it removes the choice.
    """
    import era_guard
    from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

    for version in HANDSHAKE_PROTOCOL_VERSIONS:
        if version not in era_guard.SERVED_VERSIONS:
            return version
    raise AssertionError("the SDK serves no revision this guard refuses")
