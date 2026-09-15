"""The MCP runtime: the official SDK, held behind this project's own guards.

Taking the SDK is a deliberate exception to the platform's near-zero-dependency
posture -- 28 packages, including a native cryptography stack, against a runtime
that otherwise needs three pure-Python ones. The reason is in requirements.txt
and it is not weight: MCP 2026-07-28 is not a small protocol to serve correctly,
and a hand-written implementation is a second piece of software to keep right
across revisions, whose failure mode is the worst kind -- passes curl, fails a
real client.

It is contained rather than trusted wholesale, and the containment is not
theoretical. The SDK serves both the modern and legacy handshake eras and
exposes no protocol-version allowlist, so ``era_guard`` runs in front of it and
refuses the legacy era before dispatch. ``stateless_http`` changes only legacy
session storage and is not accepted as evidence the era is off.

Nothing here decides authorization. By the time a request arrives the caller has
been resolved and its grant is on the ASGI scope, so a handler reads what the
operator consented to rather than asking again -- and a tool that forgot to
check would find nothing to check with.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
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


def build_runtime(*, resource) -> Any:
    """The SDK application, with the read-only surface registered.

    `resource` is the protected-resource description, so the runtime can report
    the identity a client discovered rather than composing a second one.
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
            " protocol revision in use."
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
            "protocolVersion": "2026-07-28",
            "runtime": f"{RUNTIME_NAME}/{RUNTIME_VERSION}",
        }

    return server


def build_runtime_app(*, resource):
    """The ASGI application the guard wraps.

    Mounted at the path the request actually carries. Nothing strips it on the
    way in: Caddy proxies ``/mcp`` to the socket unchanged and the era guard
    passes ``scope["path"]`` through untouched, so an SDK mounted at ``/`` sees
    ``/mcp`` and answers Starlette's plain-text 404 -- which looks like a
    missing route rather than a mounting mistake.

    ``RPC_PATH`` is shared with the guard for that reason: two places deciding
    what the RPC path is means one of them is eventually wrong.
    """
    server = build_runtime(resource=resource)
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
        # No session state, because this server can never have any: the era
        # guard refuses `initialize` and strips `Mcp-Session-Id`, so the SDK
        # would wait for a session that cannot be established and return
        # without answering at all -- "ASGI callable returned without starting
        # response", which says nothing about why.
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
