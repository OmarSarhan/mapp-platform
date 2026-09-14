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
