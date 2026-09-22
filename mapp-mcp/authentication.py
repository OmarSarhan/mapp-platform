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
import contextvars
import json
import logging

from introspection_client import IntrospectionUnavailable

LOGGER = logging.getLogger("mapp_mcp.authentication")

#: What a client must hold merely to open a session. Anything a tool needs is
#: checked per tool, against the grant, later.
CONNECT_SCOPE = "mcp:connect"

#: The caller a handler is serving, for the duration of one request.
#:
#: A contextvar rather than the ASGI scope, because the SDK owns everything
#: between this middleware and a tool and offers no way through it. Verified
#: empirically to survive the SDK's task boundaries rather than assumed -- a
#: tool that silently saw None would fall back to "no credential" and report a
#: permissions problem during what is actually a plumbing one.
#:
#: Reading the Authorization header again inside the tool was the alternative,
#: and it would re-derive trust this middleware has already established, and
#: still not carry the resolved scopes.
CURRENT_CALLER: contextvars.ContextVar = contextvars.ContextVar(
    "mapp_mcp_caller", default=None
)


class Authenticated:
    """The resolved caller, carried on the ASGI scope for handlers to read."""

    __slots__ = (
        "grant_id", "client_id", "scopes", "audience", "expires_at", "_token"
    )

    def __init__(self, record: dict, token: str = "") -> None:
        self.grant_id = record.get("sub") or ""
        self.client_id = record.get("client_id") or ""
        self.scopes = frozenset((record.get("scope") or "").split())
        self.audience = record.get("aud") or ""
        self.expires_at = record.get("exp")
        #: The credential itself, kept because a tool that acts has to present
        #: it as the subject token of an exchange. Underscored and excluded from
        #: repr deliberately: it is the one field here that is a secret, and
        #: everything else about this object is safe to print.
        self._token = token

    @property
    def token(self) -> str:
        """The raw token A, for presenting to the exchange and nowhere else."""
        return self._token

    def __repr__(self) -> str:
        """Never the credential.

        This object ends up in tracebacks and in anything that logs the ASGI
        scope, and a default repr over __slots__ would put a live token A in
        both. Section 11 forbids exactly that, and the cheapest way to keep the
        rule is to make the unsafe rendering impossible rather than remembered.
        """
        return (
            f"Authenticated(grant_id={self.grant_id!r},"
            f" client_id={self.client_id!r},"
            f" scopes={sorted(self.scopes)!r})"
        )

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

        caller = Authenticated(record, token)
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
        # Set per request. Nothing resets it afterwards on purpose: each request
        # runs in its own context, so a value from another one cannot be read
        # here, and clearing it would only matter if that were false.
        CURRENT_CALLER.set(caller)
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
        # The body stays generic: the caller is unauthenticated, and what broke
        # inside this deployment is not theirs to read. The detail is not
        # discarded though, which it used to be -- the parameter was accepted
        # and never used, so the one sentence naming the cause was computed and
        # thrown away on every failure.
        #
        # It matters because the two causes need opposite responses and produce
        # the same 503. "the authorization component is unavailable" is a
        # component that is down; "introspection refused this component" is a
        # runtime whose own credential is not registered, which happens when
        # `./bin/mapp mcp-runtime-register` has not run or ran against a
        # different MAPP_MCP_CLIENT_SECRET. An operator reading the wire
        # message alone goes looking for a service that is running perfectly.
        LOGGER.error("refusing an RPC call with 503: %s", detail)
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
