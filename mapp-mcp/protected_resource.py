"""RFC 9728 protected-resource metadata, and the challenge that names it.

This is how a client that meets a 401 learns which authorization server to go
to. Three identifiers have to agree exactly -- the document's ``resource``, the
``resource_metadata`` URL in the challenge, and the entry in
``authorization_servers`` -- so all three are derived here from one configured
origin rather than composed separately at three call sites, which is how they
drift apart.
"""

from __future__ import annotations

import json

#: What a client may ask for before it has been granted anything. Deliberately
#: the narrow pair: a greedy client should not be able to read every permission
#: the platform has out of a discovery document.
BOOTSTRAP_SCOPES = ("mcp:connect", "inspect")

#: The canonical resource ends in /mcp, so the path-specific well-known form is
#: the one to prefer -- a root document is unambiguous only when the origin
#: serves exactly one resource, which is not a property to rely on.
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
ROOT_METADATA_PATH = "/.well-known/oauth-protected-resource"


class ProtectedResource:
    def __init__(self, *, origin: str, issuer: str) -> None:
        self.origin = origin.rstrip("/")
        #: Compared, never fetched. mapp-mcp runs on an internal-only network,
        #: so the public issuer is not routable from here; treating it as an
        #: opaque identifier is not a shortcut, it is the only thing that works.
        self.issuer = issuer.rstrip("/")

    @property
    def resource(self) -> str:
        return f"{self.origin}/mcp"

    @property
    def metadata_url(self) -> str:
        return f"{self.origin}{METADATA_PATH}"

    def document(self) -> dict:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(BOOTSTRAP_SCOPES),
            "bearer_methods_supported": ["header"],
        }

    def challenge(self, *, error: str = "", scope: str = "") -> str:
        """The ``WWW-Authenticate`` value for a refusal.

        A *missing* token carries no bearer error code -- there is nothing wrong
        with the credential, there simply is not one -- while an invalid one
        does. Conflating them tells a client holding no token that its token is
        bad, and it will go looking for a credential to repair.
        """
        parts = []
        if error:
            parts.append(f'error="{error}"')
        if scope:
            parts.append(f'scope="{scope}"')
        parts.append(f'resource_metadata="{self.metadata_url}"')
        return "Bearer " + ", ".join(parts)


class MetadataApp:
    """Serves the document at both well-known paths, unauthenticated.

    Read-only and public on purpose: a client cannot authenticate until it has
    read this, so protecting it would be a loop.
    """

    def __init__(self, resource: ProtectedResource, app=None) -> None:
        self._resource = resource
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") not in (
            METADATA_PATH,
            ROOT_METADATA_PATH,
        ):
            if self._app is None:
                await _not_found(send)
                return
            await self._app(scope, receive, send)
            return
        if scope.get("method") not in ("GET", "HEAD"):
            await _method_not_allowed(send)
            return
        payload = json.dumps(self._resource.document()).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                # Discovery is stable and read often; the issuer moving is a
                # deployment event, not a per-request one.
                (b"cache-control", b"public, max-age=300"),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": b"" if scope.get("method") == "HEAD" else payload,
        })


async def _not_found(send) -> None:
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [(b"content-length", b"0")],
    })
    await send({"type": "http.response.body", "body": b""})


async def _method_not_allowed(send) -> None:
    await send({
        "type": "http.response.start",
        "status": 405,
        "headers": [(b"allow", b"GET, HEAD"), (b"content-length", b"0")],
    })
    await send({"type": "http.response.body", "body": b""})
