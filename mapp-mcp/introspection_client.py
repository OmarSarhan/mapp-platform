"""Resolving token A, over the control listener.

RFC 7662 introspection against ``http://mcp-auth:8080``, not against the issuer.
The issuer is the public MCP origin and mapp-mcp runs on an internal-only
network, so the issuer is a string to compare and never an address to fetch; an
implementation that tried would hang until its timeout with nothing in the logs
to say why.

stdlib only, and the transport deliberately mirrors ``config-ui``'s client
rather than improving on it: two components calling the same endpoint with two
different notions of what a refusal looks like is how they end up disagreeing
about whether a token is live.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

#: P6 allows a short positive cache. Bounded hard: a revoked grant has to stop
#: working promptly, and this is the whole of the window in which it does not.
MAX_CACHE_SECONDS = 30


class IntrospectionUnavailable(RuntimeError):
    """The component could not be reached, or did not answer usefully.

    Distinct from "the token is inactive" on purpose. An unreachable authorizer
    must not read as a refused credential: one is a 503 the operator should see,
    the other is a 401 the client should act on.
    """


INACTIVE: dict[str, Any] = {"active": False}


class IntrospectionClient:
    def __init__(
        self,
        endpoint: str,
        *,
        client_id: str,
        client_secret: str,
        resource: str,
        timeout: float = 5.0,
        cache_seconds: float = MAX_CACHE_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        #: Always sent, so the component compares audiences for us. A token
        #: minted for the configuration API must not resolve as active here,
        #: and that separation belongs on every call rather than in a comment.
        self.resource = resource
        self.timeout = timeout
        self._cache_seconds = min(cache_seconds, MAX_CACHE_SECONDS)
        self._clock = clock
        self._secret = client_secret
        self._authorization = "Basic " + base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def introspect(self, token: str) -> dict[str, Any]:
        cached = self._cache.get(token)
        now = self._clock()
        if cached is not None and cached[0] > now:
            return cached[1]
        payload = self._post({"token": token, "resource": self.resource})
        record = payload if payload.get("active") is True else INACTIVE
        if self._cache_seconds > 0:
            # Only the answer is cached, never the credential: the key is the
            # token the caller already holds, and nothing is written down.
            self._cache[token] = (now + self._cache_seconds, record)
        return record

    def _post(self, fields: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + "/internal/oauth/introspect",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": self._authorization,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return self._decode(response)
        except urllib.error.HTTPError as exc:
            # A 401 here is *this component's* credential being wrong, not the
            # agent's. Reporting it as an inactive token would blame the client
            # for a deployment fault and send an operator looking in the wrong
            # place entirely.
            raise IntrospectionUnavailable(
                self._scrub(f"introspection refused this component: HTTP {exc.code}")
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise IntrospectionUnavailable(
                self._scrub(f"the authorization component is unavailable: {exc}")
            ) from None

    def _decode(self, response) -> dict[str, Any]:
        headers = getattr(response, "headers", None)
        content_type = headers.get("Content-Type") if headers is not None else None
        if str(content_type or "").split(";", 1)[0].strip().lower() != "application/json":
            raise IntrospectionUnavailable("introspection did not answer JSON")
        try:
            payload = json.loads(response.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise IntrospectionUnavailable("introspection answered unreadable JSON") from None
        if not isinstance(payload, dict):
            raise IntrospectionUnavailable("introspection answered a non-object")
        return payload

    def _scrub(self, text: str) -> str:
        return text.replace(self._secret, "[redacted]") if self._secret else text
