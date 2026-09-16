"""The protocol-era guard: outer ASGI middleware that runs before SDK dispatch.

The official Python SDK's Streamable HTTP application serves both the modern
per-request era and the legacy handshake era, and exposes no protocol-version
allowlist. ``stateless_http`` changes only legacy session storage rather than
disabling that era. So the allowlist has to live outside it, and it has to be
able to answer without ever calling in.

This server serves three revisions -- two handshake, one modern -- and the
handshake ones are why the guard is more than a string comparison. Measured
against the installed SDK, an ``initialize`` is negotiated to *whatever the
client offers*: 2024-11-05 and 2025-03-26 are accepted as readily as the two
admitted here, and an unrecognisable offer such as ``"zzz"`` is silently
counter-offered the newest handshake revision rather than refused.
Nothing in the SDK constrains the handshake to one revision. So admitting the
handshake era without reading the offered version out of the body would not
admit the listed revisions; it would admit every one the SDK has ever spoken.

Five obligations, each separately testable:

1. Admit only ``MCP-Protocol-Version`` values in :data:`SERVED_VERSIONS`.
2. Report a *malformed* header as a validation error and a *declared unserved*
   revision as an unsupported-version error. These are different answers to
   different questions, and fabricating a version error for a request whose
   header was not a revision at all tells a client to fix the wrong thing.
3. Admit ``initialize`` only as the handshake era, and only when the version it
   offers in the body is one of :data:`HANDSHAKE_VERSIONS`. Under the modern
   revision ``initialize`` is not a method at all and is refused as one. That
   forces the guard to decode the body before dispatch.
4. Never mint or echo ``Mcp-Session-Id``. Serving the handshake era does not
   relax this: with ``stateless_http`` the SDK completes a full legacy session
   -- initialize, notification, list, call -- and mints no session identifier,
   so the obligation survives the era being admitted rather than being traded
   away for it.
5. Admit a header-less request to the handshake era, never the modern one. The
   ``initialize`` that opens a handshake cannot carry the header, because it is
   what decides the revision; and a client may omit it afterwards too -- Gemini
   CLI 0.60.0 does, against its own specification's requirement. The modern era
   is unreachable without it by construction rather than by this guard, since it
   carries the revision in ``params._meta`` and requires the two to agree.

It must not touch the RFC 9728 metadata GET, which is unauthenticated and
read-only by design, nor anything that is not a POST to the RPC path.
"""

from __future__ import annotations

import json
from typing import Any

#: The per-request-envelope era: no handshake, a revision on every request.
MODERN_VERSION = "2026-07-28"

#: The handshake revisions admitted, and no others. Each is here because a
#: shipped client of a target ecosystem speaks it and nothing newer, measured
#: rather than assumed:
#:
#:   2025-06-18  Codex CLI 0.154.0, Gemini CLI 0.60.0
#:   2025-11-25  Claude Code 2.1.272
#:
#: An explicit list rather than "everything older", and that distinction is the
#: whole control. The SDK will negotiate anything a client offers, so the set of
#: revisions this server speaks is whatever is written here and nowhere else --
#: 2024-11-05 and 2025-03-26 are served by the SDK and refused by this guard.
#:
#: Adding one is a deliberate act with a name attached. The list grew from one
#: to two when Codex and Gemini both turned out to sit a revision behind Claude;
#: it should not grow because something "looked old enough".
HANDSHAKE_VERSIONS = ("2025-06-18", "2025-11-25")

#: Membership is tested with ``in`` against exact strings rather than a prefix
#: or an ordering: "2026-07-28-beta" is not a revision this server speaks, and a
#: client that sends it has not agreed to this contract.
SERVED_VERSIONS = (*HANDSHAKE_VERSIONS, MODERN_VERSION)

VERSION_HEADER = b"mcp-protocol-version"
SESSION_HEADER = b"mcp-session-id"

#: JSON-RPC codes. The first two are the JSON-RPC standard ones. The third is
#: the code the MCP ecosystem uses -- it is ``mcp_types.UNSUPPORTED_PROTOCOL_-
#: VERSION`` in the installed SDK, asserted against it by test. It was a
#: project-chosen number until that constant was found, which meant this server
#: answered a version complaint in a dialect no client could read.
#:
#: Restated here rather than imported, because this module is stdlib-only by
#: design and importing the SDK for one integer would end that. The join is a
#: test that asserts the two are equal; it is unconditional, because a suite
#: that quietly stops running is worse than one that fails.
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
UNSUPPORTED_PROTOCOL_VERSION = -32022

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
        version = None
        if declared is not None and declared.strip():
            version = declared.decode("latin-1").strip()
            if not _well_formed(version):
                # Malformed is a validation fault, not a version fault: the
                # value is not a revision at all, so there is no revision to
                # call unsupported.
                await _reject(
                    send,
                    INVALID_REQUEST,
                    "Malformed MCP-Protocol-Version.",
                    reason="protocol-version-malformed",
                )
                return
            if version not in SERVED_VERSIONS:
                await _reject(
                    send,
                    UNSUPPORTED_PROTOCOL_VERSION,
                    f"Unsupported MCP protocol version {version}.",
                    reason="unsupported-protocol-version",
                    supported=list(SERVED_VERSIONS),
                )
                return
        # A declared revision that is served still does not settle the request:
        # only the body says whether this is a handshake, and only the body
        # carries the revision a handshake is actually asking for.
        body, replay = await _buffer(receive)
        if body is _TOO_LARGE:
            await _reject(
                send,
                INVALID_REQUEST,
                "Request body exceeds the accepted size.",
                reason="body-too-large",
            )
            return
        offered = _initialize_offer(body)

        if offered is _BATCHED:
            # The handshake is a single request by construction. Batched, it is
            # either a mistake or an attempt to ride other calls in beside a
            # request that has no declared revision of its own.
            await _reject(
                send,
                METHOD_NOT_FOUND,
                "initialize may not appear in a batch.",
                reason="batched-initialize",
            )
            return
        if offered is _NOT_INITIALIZE:
            # A header-less request after the handshake is admitted to the
            # handshake era, and the absence of the header is what says so.
            #
            # The modern era cannot be entered this way: it carries the revision
            # and the client capabilities in `params._meta` and requires the
            # header to agree with them, and the SDK refuses the request when
            # they disagree or are missing. So "no header" is not an ambiguous
            # request whose era must be guessed -- it is structurally not modern.
            #
            # This was a refusal until Gemini CLI 0.60.0 was measured: it sends
            # no `MCP-Protocol-Version` on anything after `initialize`, which
            # the 2025-06-18 specification requires of clients. Refusing it made
            # the transport correct and the ecosystem unreachable, and the
            # release gate needs all three. Relaxed deliberately, with the
            # modern era's unreachability asserted rather than assumed.
            pass
        elif version == MODERN_VERSION:
            # Refused as a method rather than as a version, because that is what
            # it is: the modern revision replaced the handshake, so a client
            # that declared the modern revision and then sent `initialize`
            # contradicted itself, and the method genuinely does not exist.
            await _reject(
                send,
                METHOD_NOT_FOUND,
                "initialize is not a method in this protocol revision.",
                reason="legacy-initialize",
            )
            return
        elif offered not in HANDSHAKE_VERSIONS:
            # The obligation the SDK does not discharge. Left to it, the offer
            # is simply accepted -- any older revision verbatim, anything
            # unrecognisable counter-offered the newest handshake revision --
            # so this is the only place the handshake era is held to a list.
            await _reject(
                send,
                UNSUPPORTED_PROTOCOL_VERSION,
                f"Unsupported MCP protocol version {offered}.",
                reason="unsupported-protocol-version",
                supported=list(SERVED_VERSIONS),
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


class _Sentinel:
    """Distinguishable from any value a request body could contain.

    ``_NOT_INITIALIZE`` in particular cannot be ``None``: ``None`` is a real
    answer, meaning an ``initialize`` that offered no revision, and conflating
    the two would admit it.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return self._name


_TOO_LARGE = _Sentinel("<too large>")
_NOT_INITIALIZE = _Sentinel("<not initialize>")
_BATCHED = _Sentinel("<batched initialize>")


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


def _initialize_offer(body: bytes):
    """What revision this payload's ``initialize`` asks for, if it is one.

    Three answers, because the caller has three things to do about them:
    :data:`_NOT_INITIALIZE` for an ordinary request, :data:`_BATCHED` when
    ``initialize`` appears inside a batch, and otherwise the offered revision as
    written in ``params.protocolVersion`` -- ``None`` where the payload names no
    revision at all, which is not a revision this server serves and is refused
    as such.

    An undecodable body is not an initialize: it is the inner app's parse error
    to report, and swallowing it here would answer the wrong question.
    """
    try:
        payload: Any = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return _NOT_INITIALIZE
    if isinstance(payload, list):
        return (
            _BATCHED
            if any(
                isinstance(item, dict) and item.get("method") == "initialize"
                for item in payload
            )
            else _NOT_INITIALIZE
        )
    if not isinstance(payload, dict) or payload.get("method") != "initialize":
        return _NOT_INITIALIZE
    params = payload.get("params")
    offered = params.get("protocolVersion") if isinstance(params, dict) else None
    return offered if isinstance(offered, str) else None


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
