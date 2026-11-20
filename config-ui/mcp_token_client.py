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
from concurrent.futures import Future, wait
from contextlib import closing, contextmanager, ExitStack, suppress
import errno
import http.client
import json
import selectors
import socket
import threading
import urllib.parse
from typing import Any

from control_connections import AdmissionTimeout, RequestBudget, current_request, remaining_time

MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 5.0
_POLL = 0.025
# libc resolution can outlive the request. Only start a resolver with a
# reserved slot, and let that worker release the slot when libc returns.
_DNS_LIMIT = 8
_DNS_SLOTS = threading.BoundedSemaphore(_DNS_LIMIT)


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


class RedemptionUnconfirmed(RuntimeError):
    """The one-shot POST may have spent its token. No replay or target dispatch."""


def _resolve(host, port, remaining):
    remaining()
    future = Future()

    def resolve():
        try:
            future.set_result(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except BaseException as exc:
            future.set_exception(exc)
        finally:
            _DNS_SLOTS.release()

    if not _DNS_SLOTS.acquire(blocking=False):
        raise OSError("Authorization component name resolution is at capacity.")
    try:
        threading.Thread(target=resolve, name="oauth-dns", daemon=True).start()
    except BaseException:
        _DNS_SLOTS.release()
        raise
    while not future.done():
        wait([future], timeout=min(_POLL, remaining()))
    remaining()
    return future.result()


@contextmanager
def _connection(host, port, hop, remaining):
    last_error = OSError("No address for the authorization component.")
    for family, kind, proto, _, address in _resolve(host, port, remaining):
        remaining()
        try:
            sock = socket.socket(family, kind, proto)
        except OSError as exc:
            last_error = exc
            continue
        with sock, ExitStack() as ownership:
            def abort():
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

            ownership.callback(hop.arm(abort))
            remaining()
            try:
                sock.setblocking(False)
                error = sock.connect_ex(address)
                if error in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EINTR):
                    with selectors.DefaultSelector() as selector:
                        selector.register(sock, selectors.EVENT_WRITE)
                        while not selector.select(min(_POLL, remaining())):
                            pass
                    error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error:
                    raise OSError(error, "Authorization component connect failed.")
            except AdmissionTimeout:
                raise
            except OSError as exc:
                remaining()
                last_error = exc
                continue
            sock.settimeout(remaining())
            # HTTPConnection may close its own fd at response headers and hand
            # reading to HTTPResponse. Keep our shutdown handle through the body.
            with closing(http.client.HTTPConnection(host, port)) as conn:
                conn.sock = sock.dup()
                yield conn
            return
    raise last_error


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
        self._host = parsed.hostname
        self._port = parsed.port if parsed.port is not None else 80
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

    def _post(self, path: str, fields: dict[str, str]) -> dict[str, Any]:
        parent = current_request()
        hop = RequestBudget(remaining_time(self.timeout), None)

        def remaining():
            return remaining_time(hop.remaining())

        body = urllib.parse.urlencode(fields).encode("utf-8")
        redeem = path == "/internal/oauth/redeem"
        submitted = False
        try:
            with ExitStack() as scope:
                if parent is not None:
                    scope.callback(parent.arm(lambda: hop.cancel("Request cancelled.")))
                conn = scope.enter_context(_connection(self._host, self._port, hop, remaining))
                headers = {
                    "Accept": "application/json",
                    "Authorization": self._authorization,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Mapp-Timeout-Seconds": str(remaining()),
                }
                submitted = True
                conn.request("POST", path, body, headers)
                with conn.getresponse() as response:
                    if response.status == 503 and not redeem:
                        raise AdmissionTimeout("Authorization database is busy.")
                    payload = self._decode(response, framed=redeem)
                    remaining()
                    if 200 <= response.status < 300:
                        return payload
                    error = self._scrub(str(payload.get("error") or ""), fields["token"])
                    refused = 400 <= response.status < 500 and bool(error)
                    message = (payload.get("error_description") or error
                               or "Authorization component request failed.")
                    raise McpTokenClientError(
                        self._scrub(str(message), fields["token"]),
                        status=response.status, error=error or None, refused=refused,
                    )
        except (McpTokenClientError, OSError, http.client.HTTPException) as exc:
            if isinstance(exc, McpTokenClientError) and exc.refused:
                raise
            if submitted and redeem:
                raise RedemptionUnconfirmed("Authorization token redemption was not confirmed.") from None
            remaining()
            if isinstance(exc, (McpTokenClientError, AdmissionTimeout)):
                raise
            raise McpTokenClientError(
                f"The authorization component is unavailable: {self._scrub(str(exc), fields['token'])}"
            ) from None

    def _scrub(self, text: str, token: str) -> str:
        for value in (self._authorization.removeprefix("Basic "), token, self._secret):
            if value:
                text = text.replace(value, "[redacted]")
        return text

    def _decode(self, response, *, framed: bool) -> dict[str, Any]:
        headers = response.headers
        value = headers.get("Content-Type")
        if str(value or "").split(";", 1)[0].strip().lower() != "application/json":
            raise McpTokenClientError(
                "The authorization component response was not JSON."
            )
        if framed:
            # The control listener writes one computed length. A parseable
            # prefix of a truncated acknowledgement cannot confirm a spend.
            lengths = headers.get_all("Content-Length") or []
            if headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
                raise McpTokenClientError("Incomplete authorization component response.")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise McpTokenClientError(
                "The authorization component response is too large."
            )
        if framed and lengths[0].strip(" \t").lstrip("0") != str(len(raw)):
            raise McpTokenClientError("Incomplete authorization component response.")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
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
            raise RedemptionUnconfirmed("Authorization token redemption was not confirmed.")
        return payload
