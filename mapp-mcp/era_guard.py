"""The protocol-era guard: outer ASGI middleware that runs before SDK dispatch.

The official Python SDK's Streamable HTTP application serves both the modern and
the legacy handshake eras and exposes no protocol-version allowlist, and
``stateless_http`` changes only legacy session storage rather than disabling that
era. So the allowlist has to live outside it, and it has to be able to answer
without ever calling in.

Four obligations, each separately testable:

1. Admit only an exact ``MCP-Protocol-Version: 2026-07-28``.
2. Report a *missing or malformed* header as a validation error and a *declared
   older* revision as an unsupported-version error. These are different answers
   to different questions, and fabricating a version error for a request that
   named no version tells a client to go and fix something it never sent.
3. Refuse ``initialize``: it is the legacy handshake, and in the modern era it is
   simply not a method. That forces the guard to decode the body before dispatch.
4. Never mint or echo ``Mcp-Session-Id``.

It must not touch the RFC 9728 metadata GET, which is unauthenticated and
read-only by design, nor anything that is not a POST to the RPC path.
"""

from __future__ import annotations

import json
from typing import Any

#: The only revision this server speaks. Compared with ``==`` rather than a
#: prefix or an ordering: "2026-07-28-beta" is not this revision, and a client
#: that sends it has not agreed to this contract.
PROTOCOL_VERSION = "2026-07-28"

VERSION_HEADER = b"mcp-protocol-version"
SESSION_HEADER = b"mcp-session-id"

#: JSON-RPC codes. The specification fixes the *distinction* between a
#: validation failure and a version failure, not the numbers, so these are
#: project choices -- named here so both sides of a test read the same constant
#: rather than a literal that drifts.
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
UNSUPPORTED_PROTOCOL_VERSION = -32001

#: Read ceiling for the body peek. Caddy already caps the MCP origin at 512KiB,
#: but a guard that depends on the proxy in front of it is a guard that stops
#: working the first time someone reaches the component directly.
MAX_BODY_BYTES = 512 * 1024


class ProtocolEraGuard:
    """Wraps an inner ASGI app and admits only the modern era to the RPC path."""

    def __init__(self, app, *, rpc_path: str = "/mcp") -> None:
        self._app = app
        self._rpc_path = rpc_path

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        # Everything but a POST to the RPC path is somebody else's business:
        # the metadata GET is unauthenticated and read-only, and GET/DELETE on
        # the RPC path are the inner app's 405 to give.
        if scope.get("path") != self._rpc_path or scope.get("method") != "POST":
            await self._app(scope, receive, self._strip_session(send))
            return

        headers = _headers(scope)
        declared = headers.get(VERSION_HEADER)
        if declared is None or not declared.strip():
            await _reject(
                send,
                INVALID_REQUEST,
                "Missing MCP-Protocol-Version.",
                reason="protocol-version-missing",
            )
            return
        version = declared.decode("latin-1").strip()
        if not _well_formed(version):
            # Malformed is a validation fault, not a version fault: the value is
            # not a revision at all, so there is no revision to call unsupported.
            await _reject(
                send,
                INVALID_REQUEST,
                "Malformed MCP-Protocol-Version.",
                reason="protocol-version-malformed",
            )
            return
        if version != PROTOCOL_VERSION:
            await _reject(
                send,
                UNSUPPORTED_PROTOCOL_VERSION,
                f"Unsupported MCP protocol version {version}.",
                reason="unsupported-protocol-version",
                supported=[PROTOCOL_VERSION],
            )
            return

        body, replay = await _buffer(receive)
        if body is _TOO_LARGE:
            await _reject(
                send,
                INVALID_REQUEST,
                "Request body exceeds the accepted size.",
                reason="body-too-large",
            )
            return
        if _is_initialize(body):
            # The legacy handshake. Refused as a method rather than as a version
            # so a client that sends it learns the method does not exist here,
            # which is true: the modern era replaced it.
            await _reject(
                send,
                METHOD_NOT_FOUND,
                "initialize is not a method in this protocol revision.",
                reason="legacy-initialize",
            )
            return
        await self._app(scope, replay, self._strip_session(send))

    @staticmethod
    def _strip_session(send):
        """Remove ``Mcp-Session-Id`` from anything the inner app emits.

        The guard refuses the requests that would create a session, so in
        principle nothing downstream can mint one. This is the second control:
        an SDK bump that started emitting the header would otherwise reintroduce
        the legacy era through a response nobody was looking at.
        """

        async def guarded(message):
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != SESSION_HEADER
                ]
            await send(message)

        return guarded


class _TooLarge:
    pass


_TOO_LARGE = _TooLarge()


def _headers(scope) -> dict[bytes, bytes]:
    # Last value wins, which matches how the header would have been read
    # downstream; a duplicated version header is malformed either way because
    # _well_formed rejects anything that is not exactly a date.
    return {name.lower(): value for name, value in scope.get("headers", [])}


def _well_formed(version: str) -> bool:
    """A revision is ``YYYY-MM-DD``. Anything else is not a revision."""
    parts = version.split("-")
    if len(parts) != 3:
        return False
    widths = (4, 2, 2)
    return all(
        part.isdigit() and len(part) == width for part, width in zip(parts, widths)
    )


async def _buffer(receive):
    """Read the body, then hand back a ``receive`` that replays it.

    The guard has to decode the body to see the method, and the inner app still
    needs to read it afterwards, so it is buffered once and replayed rather than
    consumed.
    """
    chunks: list[bytes] = []
    total = 0
    more = True
    while more:
        message = await receive()
        if message.get("type") != "http.request":
            # A disconnect: hand back what we have and let the inner app see it.
            chunks.append(b"")
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return _TOO_LARGE, None
        chunks.append(chunk)
        more = message.get("more_body", False)
    body = b"".join(chunks)

    sent = False

    async def replay():
        """The buffered body once, then the real stream underneath.

        Returning ``http.disconnect`` on the second call looks harmless -- the
        body is finished, so what else is there to say -- and it is not. A
        streaming application keeps reading to notice the client going away, so
        a fabricated disconnect tells it the caller left and it abandons the
        response without sending one. The symptom is "ASGI callable returned
        without starting response" and no traceback, because nothing raised.

        Delegating instead means the disconnect arrives when it actually
        happens.
        """
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay


def _is_initialize(body: bytes) -> bool:
    """True when the payload calls ``initialize``, batch or not.

    An undecodable body is not an initialize: it is the inner app's parse error
    to report, and swallowing it here would answer the wrong question.
    """
    try:
        payload: Any = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return False
    candidates = payload if isinstance(payload, list) else [payload]
    return any(
        isinstance(item, dict) and item.get("method") == "initialize"
        for item in candidates
    )


async def _reject(send, code: int, message: str, *, reason: str, **extra) -> None:
    error: dict[str, Any] = {"code": code, "message": message, "data": {"reason": reason}}
    error["data"].update(extra)
    payload = json.dumps({"jsonrpc": "2.0", "id": None, "error": error}).encode()
    await send({
        "type": "http.response.start",
        "status": 400,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": payload})
