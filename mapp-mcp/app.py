"""The application factory.

Everything is assembled here and nowhere else, because the conformance fixture
is specified as being "created by the same application factory ... only its
handler registry and test authorization policy differ". A fixture that built its
own stack would be testing a different server from the one that ships.

It is also A4's containment rule: the transport lives behind one module, so a
protocol revision or an SDK bump is a contained change rather than an edit
scattered across handlers.
"""

from __future__ import annotations

import os

from authentication import BearerAuthentication
from era_guard import ProtocolEraGuard
from introspection_client import IntrospectionClient
from protected_resource import MetadataApp
from protected_resource import ProtectedResource


async def _not_implemented(scope, receive, send) -> None:
    """Stands in for the SDK application until it is wired.

    Deliberately a 501 rather than a stub success: everything in front of it --
    the era guard, the metadata document, the challenge -- is real and testable
    now, and a fake success here would make those tests pass for a reason that
    has nothing to do with them.
    """
    payload = b'{"jsonrpc":"2.0","id":null,"error":{"code":-32603,' \
              b'"message":"The MCP runtime is not wired yet."}}'
    await send({
        "type": "http.response.start",
        "status": 501,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


def build_app(
    *,
    origin: str | None = None,
    issuer: str | None = None,
    inner=None,
    introspection=None,
):
    """Compose the stack: metadata, era guard, authentication, runtime.

    The metadata document is served in front of everything so no layer needs an
    exception for it: an unauthenticated read-only GET is not an RPC request and
    should meet neither a protocol-version check nor a credential check.

    The era guard runs before authentication, not after. A request in the wrong
    protocol era is refused whatever credential it carries, and putting
    authentication first would mean introspecting a token for a request that was
    never going to be dispatched -- work done, and a revocation window consumed,
    on behalf of a client this server does not speak to.
    """
    origin = origin or os.environ.get("MCP_SITE", "http://mcp.localhost")
    issuer = issuer or os.environ.get("MCP_ISSUER", origin)
    resource = ProtectedResource(origin=origin, issuer=issuer)
    runtime = inner if inner is not None else _not_implemented
    if introspection is None:
        introspection = IntrospectionClient(
            os.environ.get("MCP_AUTH_URL", "http://mcp-auth:8080"),
            client_id=os.environ.get("MAPP_MCP_CLIENT_ID", "mapp-mcp"),
            client_secret=os.environ.get("MAPP_MCP_CLIENT_SECRET", ""),
            resource=f"{origin.rstrip('/')}/mcp",
        )
    authenticated = BearerAuthentication(
        runtime, introspection=introspection, resource=resource
    )
    return MetadataApp(resource, ProtocolEraGuard(authenticated)), resource
