"""The MCP runtime: the official SDK, held behind this project's own guards.

Taking the SDK is a deliberate exception to the platform's near-zero-dependency
posture -- 28 packages, including a native cryptography stack, against a runtime
that otherwise needs three pure-Python ones. The reason is in requirements.txt
and it is not weight: MCP 2026-07-28 is not a small protocol to serve correctly,
and a hand-written implementation is a second piece of software to keep right
across revisions, whose failure mode is the worst kind -- passes curl, fails a
real client.

It is contained rather than trusted wholesale, and the containment is not
theoretical. The SDK serves both the modern and legacy handshake eras, exposes
no protocol-version allowlist, and negotiates a handshake to whatever revision
the client offers -- so ``era_guard`` runs in front of it and holds the served
set to the two revisions in ``era_guard.SERVED_VERSIONS``. ``stateless_http``
changes only legacy session storage and is not accepted as evidence about which
eras are served.

Nothing here decides authorization. By the time a request arrives the caller has
been resolved and its grant is on the ASGI scope, so a handler reads what the
operator consented to rather than asking again -- and a tool that forgot to
check would find nothing to check with.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit

import era_guard
from authentication import CURRENT_CALLER
from config_api_client import ConfigApiClient
from config_api_client import ConfigApiRefused
from config_api_client import ConfigApiUnavailable
from config_api_client import layer_values_query
from exchange_client import ExchangeRefused
from exchange_client import ExchangeUnavailable
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

#: Advertised to clients as the server's identity. Not a version of the
#: platform: it is the version of this protocol surface, and it moves when the
#: tool contract does rather than when MAPP does.
RUNTIME_NAME = "mapp-mcp"
RUNTIME_VERSION = "0.1.0"

#: The scope a caller must hold for a tool merely to be listed. Anything a tool
#: *does* is checked again against the grant when it is called.
INSPECT_SCOPE = "inspect"

#: The one place the RPC path is written. The edge routes it, the guard screens
#: it and the SDK mounts at it, and nothing rewrites it in between.
RPC_PATH = "/mcp"


#: The operation this tool acts through, as the broker's allowlist names it.
#: Not a vendored copy of that table: one tool, one operation, and a fourth copy
#: of the allowlist would be a fourth thing to keep in step.
LAYER_VALUES = {
    "operation_id": "layers.values",
    "method": "GET",
    "path_template": "/api/layers/{layerKey}/values",
    # The action declares `derive` and additionally needs `semantic:inspect` to
    # read the field it aggregates over. Neither is advertised in discovery, so
    # a client holding only the bootstrap scopes has to ask for them.
    "scopes": ("derive", "semantic:inspect"),
}


def build_runtime(*, resource, exchange=None, config_api=None) -> Any:
    """The SDK application, with the read-only surface registered.

    `resource` is the protected-resource description, so the runtime can report
    the identity a client discovered rather than composing a second one.
    `exchange` and `config_api` are injected so the tools can be driven without
    a broker or a platform behind them.
    """
    server = MCPServer(
        name=RUNTIME_NAME,
        version=RUNTIME_VERSION,
        title="MAPP",
        description=(
            "Read-only access to a MAPP instance's configured layers and"
            " semantic profiles."
        ),
    )

    @server.tool(
        name="describe_instance",
        description=(
            "The MAPP instance this server speaks for: its resource identity,"
            " the authorization server that issues credentials for it, and the"
            " protocol revisions it serves."
        ),
    )
    def describe_instance() -> dict:
        """Deliberately the first tool, and deliberately trivial.

        It needs no platform call, so it exercises the whole path -- discovery,
        the credential, the guard, dispatch, the result shape -- without
        depending on anything downstream being reachable. When a client cannot
        talk to this server, the answer to "is it the transport or the
        platform?" should not require reading logs.
        """
        return {
            "resource": resource.resource,
            "authorizationServer": resource.issuer,
            # Read from the guard rather than restated. A literal here was a
            # single revision that stayed "2026-07-28" while a caller reached
            # this tool over 2025-11-25 -- a server reporting an era it was not
            # speaking to the very client asking.
            "protocolVersions": list(era_guard.SERVED_VERSIONS),
            "runtime": f"{RUNTIME_NAME}/{RUNTIME_VERSION}",
        }

    @server.tool(
        name="layer_values",
        description=(
            "Bounded category counts for one field of one configured layer."
            " Returns aggregate counts from the layer's effective restrictions,"
            " never raw rows."
        ),
    )
    def layer_values(
        layer_key: str,
        field: str,
        locale: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """One read, through the full binding.

        The credential this obtains authorises exactly this request and no
        other: the path and query are digested at exchange time and sent
        unchanged, and the configuration API recomputes the digest from what
        actually arrives before it will spend the credential.

        The query is built once, here, by the helper that also fixes its order.
        Rebuilding it for the request would be two constructions of the same
        string and one chance for them to differ -- which surfaces as a refusal
        naming no cause.

        Every anticipated failure is raised as ``ToolError``, and that choice is
        load-bearing rather than stylistic. The SDK treats ``ToolError`` as "a
        failure you saw coming" and puts its text in the result the model reads;
        *any other exception* it treats as a crash, replacing the text with
        "Error executing tool layer_values" and logging a traceback at ERROR.
        These raises were ``ValueError`` and ``RuntimeError``, so every message
        written here to be acted on -- which scope to ask for, which platform
        code refused -- was discarded before it reached the caller, and routine
        scope refusals were logged as crashes. Found by driving a real client
        against the deployed stack; the unit tests called the tool function
        directly and so agreed with the code while the property was false.
        """
        caller = CURRENT_CALLER.get()
        if caller is None:
            # Unreachable through the middleware, which refuses before
            # dispatch. Checked anyway, because the alternative if it ever
            # became reachable is an unauthenticated platform call.
            raise ToolError("This tool requires an authenticated caller.")
        missing = [s for s in LAYER_VALUES["scopes"] if s not in caller.scopes]
        if missing:
            # Refused here rather than at the exchange, so the message names the
            # scopes to ask for. The broker would refuse it too, with an error
            # that says the scope exceeded the grant and not which scope.
            raise ToolError(
                "This grant does not carry "
                + " and ".join(sorted(missing))
                + ". Re-authorize requesting "
                + " ".join(LAYER_VALUES["scopes"])
                + " to use this tool."
            )

        path = LAYER_VALUES["path_template"].replace(
            "{layerKey}", quote(layer_key, safe="")
        )
        query = layer_values_query(field=field, locale=locale, limit=limit)
        try:
            token_b = exchange.exchange(
                subject_token=caller.token,
                operation_id=LAYER_VALUES["operation_id"],
                method=LAYER_VALUES["method"],
                path_template=LAYER_VALUES["path_template"],
                path=path,
                query=query,
                body=None,
                scope=" ".join(LAYER_VALUES["scopes"]),
            )
        except ExchangeRefused as refusal:
            raise ToolError(f"The platform refused this request: {refusal}") from None
        except ExchangeUnavailable:
            # Deliberately not the underlying text: it describes this
            # component's plumbing, and an agent cannot act on it.
            raise ToolError(
                "The authorization component is unavailable; try again."
            ) from None

        try:
            return config_api.get(path=path, query=query, token=token_b)
        except ConfigApiRefused as refusal:
            raise ToolError(
                f"The platform refused this request: {refusal}"
                + (f" ({refusal.code})" if refusal.code else "")
            ) from None
        except ConfigApiUnavailable:
            raise ToolError(
                "The configuration API is unavailable; try again."
            ) from None

    return server


def build_runtime_app(*, resource, exchange=None, config_api=None):
    """The ASGI application the guard wraps.

    Mounted at the path the request actually carries. Nothing strips it on the
    way in: Caddy proxies ``/mcp`` to the socket unchanged and the era guard
    passes ``scope["path"]`` through untouched, so an SDK mounted at ``/`` sees
    ``/mcp`` and answers Starlette's plain-text 404 -- which looks like a
    missing route rather than a mounting mistake.

    ``RPC_PATH`` is shared with the guard for that reason: two places deciding
    what the RPC path is means one of them is eventually wrong.
    """
    server = build_runtime(
        resource=resource, exchange=exchange, config_api=config_api
    )
    # DNS-rebinding protection, pointed at the origin this server actually
    # serves. It is on by default and defaults to 127.0.0.1, which is why an
    # otherwise correct request through the edge answers 421 "Invalid Host
    # header": the deployment's host is mcp.localhost, not the SDK's guess.
    #
    # Kept on rather than disabled. A browser on an operator's machine can be
    # made to POST to a loopback service; the Host and Origin allowlists are
    # what stop that reaching an authenticated MCP endpoint, and the credential
    # is in the request rather than a cookie only because this server refuses
    # cookies at all.
    host = urlsplit(resource.origin).hostname or "localhost"
    return server.streamable_http_app(
        streamable_http_path=RPC_PATH,
        # No session state. Under the handshake era the SDK would otherwise
        # mint and require `Mcp-Session-Id`, which the guard strips -- the
        # client would send back a session the server had been told to forget,
        # and every request after initialize would be refused.
        #
        # Measured, not assumed: with this on, a full legacy session --
        # initialize, notifications/initialized, tools/list, tools/call --
        # completes and no session identifier is ever emitted. That is what
        # lets the legacy era be served without giving up the no-session
        # obligation.
        #
        # This is a consequence of the era decision, not the control that
        # enforces it. The specification is explicit that enabling it "is not
        # accepted as evidence that the legacy era is disabled"; the guard is
        # the evidence, and it is asserted on the wire.
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            # Both spellings: a client may or may not carry the port, and an
            # allowlist that accepts one and refuses the other turns a correct
            # deployment into an intermittent 421.
            allowed_hosts=[host, f"{host}:*"],
            allowed_origins=[resource.origin, f"{resource.origin}:*"],
        ),
    )
