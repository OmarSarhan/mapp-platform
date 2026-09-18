"""Client for the authorization component's control listener.

Modelled on semantic_client.py, and narrow on purpose: two calls, both to
``mcp-control``, both authenticated as a confidential OAuth client over HTTP
Basic. It exists so app.py can treat a token B as a credential rather than as
a route -- which is what keeps the two edit sites in app.py to two functions.

``introspect`` establishes who is calling and with what scope. ``redeem``
spends the credential against the request actually in front of the handler.
They are separate calls because the scope decision has to happen before a
handler dispatches, while the digest cannot be computed until the body has
been read -- and the redemption must cover the body.

The endpoint is reached by service name on an internal Docker network. That is
not a substitute for authentication and is not treated as one: every call
carries client credentials, and a failure to reach the component is a refusal,
never a pass.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 5.0


class McpTokenClientError(RuntimeError):
    """A call failed, or the component refused it.

    ``refused`` distinguishes "the component says no" from "the component is
    unreachable". Both deny the request -- the difference is only ever for the
    operator reading a log line, never for the authorization decision.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error: str | None = None,
        refused: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.error = error
        self.refused = refused


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class McpTokenClient:
    def __init__(
        self,
        endpoint: str,
        client_id: str,
        client_secret: str,
        *,
        resource: str,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise McpTokenClientError(
                "The authorization component endpoint must be an internal HTTP"
                " root URL."
            )
        if not client_id or not client_secret:
            raise McpTokenClientError(
                "Authorization component client credentials are required."
            )
        if not resource:
            raise McpTokenClientError(
                "The configuration-API resource identifier is required."
            )
        self.endpoint = endpoint.rstrip("/")
        self.resource = resource
        self.timeout = timeout
        # Percent-encoded per RFC 6749 s2.3.1 before being joined, so a colon
        # in either half cannot move the boundary between them.
        credentials = (
            f"{urllib.parse.quote(client_id, safe='')}"
            f":{urllib.parse.quote(client_secret, safe='')}"
        )
        self._authorization = "Basic " + base64.b64encode(
            credentials.encode("utf-8")
        ).decode("ascii")
        self._secret = client_secret
        self.opener = urllib.request.build_opener(_RejectRedirects())

    def _post(self, path: str, fields: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + path,
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
            with self.opener.open(request, timeout=self.timeout) as response:
                return self._decode(response)
        except urllib.error.HTTPError as exc:
            payload = {}
            try:
                payload = self._decode(exc)
            except McpTokenClientError:
                pass
            error = str(payload.get("error") or "")
            raise McpTokenClientError(
                self._scrub(
                    str(
                        payload.get("error_description")
                        or error
                        or "The authorization component refused the request."
                    )
                ),
                status=exc.code,
                error=error or None,
                refused=True,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise McpTokenClientError(
                f"The authorization component is unavailable: {self._scrub(str(exc))}"
            ) from None

    def _scrub(self, text: str) -> str:
        # The secret is never in a response, but an exception string can carry
        # a request URL, and a future caller could put one in a field. Cheap.
        return text.replace(self._secret, "[redacted]")

    def _decode(self, response) -> dict[str, Any]:
        headers = getattr(response, "headers", None)
        value = headers.get("Content-Type") if headers is not None else None
        if str(value or "").split(";", 1)[0].strip().lower() != "application/json":
            raise McpTokenClientError(
                "The authorization component response was not JSON."
            )
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise McpTokenClientError(
                "The authorization component response is too large."
            )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpTokenClientError(
                "The authorization component returned invalid JSON."
            ) from exc
        if not isinstance(decoded, dict):
            raise McpTokenClientError(
                "The authorization component returned an invalid object."
            )
        return decoded

    def introspect(self, token: str) -> dict[str, Any]:
        """Resolve a token, or report it inactive.

        The resource is always sent, so the component compares audiences for
        us: a token minted for the MCP endpoint must not resolve as active
        here. That check is the audience separation between token A and token
        B, and it belongs on every call rather than in a comment.
        """
        payload = self._post(
            "/internal/oauth/introspect",
            {"token": token, "resource": self.resource},
        )
        if payload.get("active") is not True:
            return {"active": False}
        return payload

    def redeem(self, token: str, operation_id: str, request_digest: str) -> dict[str, Any]:
        """Spend the token against one operation and one request digest.

        Raises on refusal rather than returning a falsy value: a caller that
        forgets to check a boolean would execute the effect anyway, and this is
        the call that stands between a request and a consequential mutation.
        """
        payload = self._post(
            "/internal/oauth/redeem",
            {
                "token": token,
                "operation_id": operation_id,
                "request_digest": request_digest,
            },
        )
        if payload.get("redeemed") is not True:
            raise McpTokenClientError(
                "The authorization component did not confirm redemption.",
                refused=True,
            )
        return payload
