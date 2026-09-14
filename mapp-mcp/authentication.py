"""Token A at the door, and the three refusals the failure table names.

Every RPC call is authenticated by introspection rather than by reading the
token, because the token is opaque: it carries no claims, and the only thing
that knows whether its grant is still live is the component that issued it.
That is also what makes revocation mean anything -- a revoked grant stops
working on the next call rather than when a signature would have expired.

The three refusals are deliberately distinguishable:

- no token at all -> 401, and the challenge carries no bearer error code
- a token that does not resolve -> 401 with ``error="invalid_token"``
- a live token without the scope -> 403 with ``error="insufficient_scope"``
  and every required scope in one space-delimited value

Collapsing the first two is the tempting simplification and the wrong one: a
client holding no credential told its credential is invalid goes looking for
something to repair rather than something to obtain.
"""

from __future__ import annotations

import asyncio
import json

from introspection_client import IntrospectionUnavailable

#: What a client must hold merely to open a session. Anything a tool needs is
#: checked per tool, against the grant, later.
CONNECT_SCOPE = "mcp:connect"


class Authenticated:
    """The resolved caller, carried on the ASGI scope for handlers to read."""

    __slots__ = ("grant_id", "client_id", "scopes", "audience", "expires_at")

    def __init__(self, record: dict) -> None:
        self.grant_id = record.get("sub") or ""
        self.client_id = record.get("client_id") or ""
        self.scopes = frozenset((record.get("scope") or "").split())
        self.audience = record.get("aud") or ""
        self.expires_at = record.get("exp")

    def has(self, *required: str) -> bool:
        return set(required).issubset(self.scopes)


class BearerAuthentication:
    """Wraps the runtime and resolves token A before anything else runs."""

    def __init__(
        self,
        app,
        *,
        introspection,
        resource,
        rpc_path: str = "/mcp",
        required_scopes: tuple[str, ...] = (CONNECT_SCOPE,),
    ) -> None:
        self._app = app
        self._introspection = introspection
        self._resource = resource
        self._rpc_path = rpc_path
        self._required = tuple(required_scopes)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") != self._rpc_path:
            # The metadata document is read before a client can hold anything,
            # so authenticating it would be a loop.
            await self._app(scope, receive, send)
            return

        token = _bearer(scope)
        if token is None:
            await self._refuse(send, 401, "No bearer credential was presented.")
            return
        try:
            # urllib is blocking and this is an ASGI path, so the call goes to a
            # worker thread. Doing it inline would stall the event loop for the
            # whole timeout whenever the authorizer is slow -- one unavailable
            # dependency becoming an unresponsive server.
            record = await asyncio.to_thread(self._introspection.introspect, token)
        except IntrospectionUnavailable as exc:
            # Fails closed, but as a 503: the credential was never judged, and
            # calling it invalid would tell the client to go and get another one
            # when the fault is entirely ours.
            await self._unavailable(send, str(exc))
            return
        if record.get("active") is not True:
            await self._refuse(
                send, 401, "The credential is not active.", error="invalid_token"
            )
            return

        caller = Authenticated(record)
        if caller.audience != self._resource.resource:
            # The component already compares audiences, because the resource is
            # sent on every introspection. Checked again here because an
            # audience confusion is the one failure that silently grants a
            # credential minted for somewhere else.
            await self._refuse(
                send, 401, "The credential is for another resource.", error="invalid_token"
            )
            return
        if not caller.has(*self._required):
            await self._refuse(
                send,
                403,
                "The credential does not carry the required scope.",
                error="insufficient_scope",
                scope=" ".join(self._required),
            )
            return

        scope = dict(scope)
        scope["mapp.caller"] = caller
        await self._app(scope, receive, send)

    async def _refuse(self, send, status, message, *, error="", scope="") -> None:
        await _write(
            send,
            status,
            {"error": error or "unauthorized", "error_description": message},
            extra_headers=[
                (b"www-authenticate", self._resource.challenge(
                    error=error, scope=scope
                ).encode("latin-1")),
            ],
        )

    async def _unavailable(self, send, detail: str) -> None:
        await _write(
            send,
            503,
            {
                "error": "authorization_unavailable",
                "error_description": "The authorization component is unavailable.",
            },
            extra_headers=[(b"retry-after", b"5")],
        )


def _bearer(scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"authorization":
            continue
        raw = value.decode("latin-1").strip()
        kind, _, token = raw.partition(" ")
        # Case-insensitive scheme per RFC 7235; the token itself is not.
        if kind.lower() != "bearer" or not token.strip():
            return None
        return token.strip()
    return None


async def _write(send, status: int, payload: dict, *, extra_headers=()) -> None:
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            *extra_headers,
        ],
    })
    await send({"type": "http.response.body", "body": body})
