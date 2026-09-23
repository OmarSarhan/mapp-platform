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
    if inner is not None:
        runtime = inner
    else:
        # Imported here rather than at module scope: the guard, the metadata
        # surface and the authentication middleware are stdlib-only and their
        # tests run without the SDK installed. Importing it at the top would
        # make 28 packages a prerequisite for testing code that does not use
        # them.
        from config_api_client import ConfigApiClient
        from exchange_client import ExchangeClient
        from runtime import build_runtime_app

        config_api_endpoint = os.environ.get(
            "MCP_CONFIG_API_URL", "http://config-ui:8080"
        )
        config_api_resource = os.environ.get(
            "MCP_CONFIG_API_RESOURCE",
            "http://config.localhost/api",
        )
        runtime = build_runtime_app(
            resource=resource,
            exchange=ExchangeClient(
                broker_endpoint=os.environ.get(
                    "MCP_AUTH_URL", "http://mcp-auth:8080"
                ),
                config_api_endpoint=config_api_endpoint,
                # What a token B is for. Must differ from this server's own
                # resource, or a token A and a token B would share an audience
                # and the separation the design rests on would be gone.
                config_api_resource=config_api_resource,
                client_id=os.environ.get("MAPP_MCP_CLIENT_ID", "mapp-mcp"),
                client_secret=os.environ.get("MAPP_MCP_CLIENT_SECRET", ""),
            ),
            config_api=ConfigApiClient(endpoint=config_api_endpoint),
            config_api_resource=config_api_resource,
        )
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
