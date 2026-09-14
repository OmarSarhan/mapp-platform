"""HTTP surface for the authorization component.

M1 exposes the token endpoint alone, on a TCP listener, so the adapter can be
driven over a real socket. M3 adds the Unix-domain edge listener, the separate
internal listener, and the disjoint route tables that make an internal path a
404 on the edge socket independently of the Caddy allowlist.

**Phase 0 limitation: nothing registers a client.** ``SqlStore.add_client``
has no production caller -- no operator command, no dynamic registration
endpoint (RFC 7591 is Phase 1) -- so a correctly deployed component starts
with an empty client table and refuses every authorization request as an
unknown client. The tests register their own, which is why the suite proves
the flow works and not that anyone can reach it. Registration is a Phase 1
deliverable and this component is not usable end to end until it exists.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

from authlib.oauth2.rfc6749.errors import OAuth2Error

import exchange
import introspection
import operations
import pages
from authlib_adapter import FormError
from authlib_adapter import MappResponse
from authlib_adapter import parse_form
from models import PendingAuthorization
from models import Grant
from models import Session
from models import PendingLimitReached
from passwords import verify_password
from unix_server import UNIX_PEER
from unix_server import UnixSocketServerMixin

VERSION = (Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip())

JSON_HEADERS = [("Content-Type", "application/json")]
HTML_HEADERS = [("Content-Type", "text/html; charset=utf-8")]

SESSION_COOKIE = "mapp_oauth_session"
SESSION_SECONDS = 1800

#: Mirrors config-ui's login throttle: 8 attempts per 300 seconds per remote.
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 8
_login_lock = threading.Lock()
_login_attempts: dict[str, list[float]] = {}


def _throttled(remote: str) -> bool:
    now = time.monotonic()
    with _login_lock:
        # Drop keys whose windows have fully expired. Without this the table
        # grows once per distinct client address and is never reclaimed, since
        # _clear_throttle only removes the one key that just signed in.
        for key in [
            key
            for key, times in _login_attempts.items()
            if not any(now - t < LOGIN_WINDOW_SECONDS for t in times)
        ]:
            del _login_attempts[key]
        attempts = [t for t in _login_attempts.get(remote, []) if now - t < LOGIN_WINDOW_SECONDS]
        if len(attempts) >= LOGIN_MAX_ATTEMPTS:
            _login_attempts[remote] = attempts
            return True
        attempts.append(now)
        _login_attempts[remote] = attempts
    return False


def _clear_throttle(remote: str) -> None:
    with _login_lock:
        _login_attempts.pop(remote, None)


#: Bucket for requests whose client address cannot be established. Sharing one
#: bucket is deliberate: an unattributable request must still be rate-limited,
#: and giving each malformed header its own bucket would remove the limit.
UNKNOWN_CLIENT = "unknown"


def client_address(handler) -> str:
    """Return the address the throttle should key on.

    On the Unix listener the socket peer is meaningless -- every connection is
    Caddy -- so keying the throttle on it would make one global bucket and let
    a single attacker lock out every other user. The real address arrives in
    X-Forwarded-For, which Caddy *overwrites* (``header_up X-Forwarded-For
    {remote_host}``, with ``-Forwarded`` and ``-X-Real-IP``). That header is
    trustworthy here only because P12 makes the socket exclusive to Caddy, so
    it is read on the Unix listener and ignored on a TCP one.
    """
    if not getattr(handler.server, "trust_forwarded_for", False):
        return handler.client_address[0] if handler.client_address else UNKNOWN_CLIENT
    # get_all, not get: repeating the header is the other way to write a list
    # (RFC 9110 s5.3), and .get returns only the first, so the comma check
    # alone let "XFF: 3.3.3.3" + "XFF: 4.4.4.4" through and keyed on 3.3.3.3.
    values = handler.headers.get_all("X-Forwarded-For") or []
    if len(values) != 1 or "," in values[0]:
        # Caddy sets exactly one value with one address. Anything else means
        # the header was not sanitised as configured, so nothing in it can be
        # attributed to a client.
        return UNKNOWN_CLIENT
    try:
        address = ipaddress.ip_address(values[0].strip())
    except ValueError:
        return UNKNOWN_CLIENT
    # ::ffff:9.9.9.9 and 9.9.9.9 are the same client; without folding they get
    # two throttle buckets and twice the attempts.
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.compressed



def _session_hash(raw: str | None) -> str:
    """Hash a session cookie the same way the store keys it."""
    return hashlib.sha256(raw.encode()).hexdigest() if raw else ""


def _tokens_equal(expected: str, supplied: str) -> bool:
    """Constant-time compare of two form tokens.

    Encoded first: secrets.compare_digest raises TypeError on str operands
    holding non-ASCII, and the supplied value is attacker-controlled, so the
    str form turns 'csrf=e-acute' into an unhandled exception that kills the
    handler thread and drops the connection with no response.
    """
    return secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def _cookie(handler, name: str) -> str | None:
    raw = handler.headers.get("Cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def _html(handler, status: int, body: str, nonce: str, extra=()) -> None:
    headers = list(HTML_HEADERS)
    headers.append(("Content-Security-Policy", pages.content_security_policy(nonce)))
    headers.extend(extra)
    MappResponse(status, body, headers).write_to(handler)


def _redirect(handler, location: str, extra=()) -> None:
    headers = [("Location", location), ("Content-Type", "text/plain; charset=utf-8")]
    headers.extend(extra)
    MappResponse(302, "", headers).write_to(handler)


class AuthHandler(BaseHTTPRequestHandler):
    server_version = f"mapp-mcp-auth/{VERSION}"
    #: Set by the server object. Route tables belong to the listener, not the
    #: handler class, so the two surfaces cannot leak into each other.
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D102 - quiet by default
        return

    #: Set by parse_form once the declared body is off the socket. Until then a
    #: response must not be followed by another request on this connection.
    body_consumed = False

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        self.body_consumed = False
        try:
            self._route(method)
        finally:
            self._close_if_body_unread()

    def _close_if_body_unread(self) -> None:
        """Stop reusing a connection whose request body was never read.

        protocol_version is HTTP/1.1, so the connection is kept alive by
        default. Every path that answers before reading the body -- the 404
        branch, and every FormError parse_form raises before its rfile.read --
        leaves those bytes in the buffer, where the server then parses them as
        the *next* request. One client request produced two responses, and
        because Caddy pools upstream connections the extra response is
        delivered to whoever holds the connection next.

        The rule is deliberately inverted: keep the connection only when the
        request *provably* carried no body, and close in every other case
        including ones this method cannot interpret. An earlier version asked
        the opposite question -- "is Content-Length non-zero?" -- through a
        lenient ``headers.get``, and so kept the connection alive for the two
        inputs that matter most: ``Transfer-Encoding: chunked`` with no
        Content-Length (read as length 0), and a duplicated
        ``Content-Length: 0`` followed by a real one (``.get`` returns only the
        first). Both leaked a second response on the same connection.
        """
        if self.body_consumed:
            return
        if not self._provably_bodyless():
            self.close_connection = True

    def _provably_bodyless(self) -> bool:
        """True only when this request cannot have carried a body.

        get_all rather than get throughout: a repeated header is the other way
        to write an ambiguous value, and reading only the first is what let the
        duplicate-Content-Length case through.
        """
        if self.headers.get_all("Transfer-Encoding"):
            # Any transfer coding means the body is framed by something this
            # server does not read. parse_form refuses these, so the bytes stay.
            return False
        lengths = self.headers.get_all("Content-Length") or []
        if not lengths:
            return True  # no framing header at all: no body
        if len(lengths) > 1:
            return False  # ambiguous framing
        return lengths[0].strip() == "0"

    def _route(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        route = self.server.routes.get((method, path))
        if route is None:
            self._close_if_body_unread()
            self._error(404, "not_found", "Unknown endpoint.")
            return
        try:
            route(self)
        except PendingLimitReached:
            # A refusal to park a new request is a capacity condition, not a
            # server fault: say so, and say when to come back.
            self.close_connection = True
            MappResponse(
                503,
                json.dumps(
                    {
                        "error": "temporarily_unavailable",
                        "error_description": "Too many pending authorization requests.",
                    }
                ),
                JSON_HEADERS + [("Retry-After", "60")],
            ).write_to(self)
        except FormError as exc:
            # parse_form can raise before its rfile.read, so the body may still
            # be on the socket.
            self._close_if_body_unread()
            self._error(400, exc.error, exc.description)
        except OAuth2Error as exc:
            # Delegate to authlib rather than reimplementing RFC 6749's rule:
            # OAuth2Error.__call__ redirects the error to the client when a
            # validated redirect_uri is attached, and returns a body when it is
            # not — which is exactly what must happen for an unregistered
            # redirect target, since redirecting there would be the bug.
            server = getattr(self.server, "authorization", None)
            if server is None:
                self._error(exc.status_code, exc.error, exc.description or "")
                return
            response = server.handle_response(*exc(None))
            if not any(n.lower() == "content-type" for n, _ in response.headers):
                response.headers.append(("Content-Type", "application/json"))
            if response.location:
                # RFC 9207 applies to error redirects too (P1).
                server.annotate_issuer(response)
            response.write_to(self)
        except Exception:  # noqa: BLE001 - deliberately last-resort
            # An unhandled exception used to kill the handler thread with no
            # response written at all, dropping the connection mid-exchange.
            # Answering 500 and closing keeps the failure legible and stops a
            # half-written connection being reused.
            self.close_connection = True
            self.log_error("unhandled error serving %s %s", self.command, self.path)
            try:
                self._error(500, "server_error", "Internal error.")
            except Exception:  # noqa: BLE001 - the socket is already gone
                pass

    def _error(self, status: int, error: str, description: str) -> None:
        body = json.dumps({"error": error, "error_description": description})
        MappResponse(status, body, JSON_HEADERS).write_to(self)


def token_endpoint(handler) -> None:
    server = handler.server.authorization
    request = server.request_from(handler)
    response = server.create_token_response(request=request)
    if not response.headers:
        response.headers = list(JSON_HEADERS)
    response.write_to(handler)


def metadata_endpoint(handler) -> None:
    server = handler.server.authorization
    MappResponse(
        200,
        json.dumps(server.metadata_document()),
        JSON_HEADERS + [("Cache-Control", "public, max-age=300")],
    ).write_to(handler)


def _pending_from(handler, server):
    """Load the parked authorization request named by ?rid=."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    request_ids = query.get("rid") or []
    if len(request_ids) != 1:
        return None
    return server.store.query_pending(request_ids[0])


def authorize_get(handler) -> None:
    server = handler.server.authorization
    pending = _pending_from(handler, server)

    if pending is not None:
        # Returning from login. Rebuild from the stored, validated query.
        request = server.request_from(handler, read_body=False, query_override=pending.query)
    else:
        request = server.request_from(handler, read_body=False)

    # Validates client, redirect_uri, response_type, scope and PKCE presence.
    # Raises OAuth2Error, which the dispatcher turns into a redirect or a body.
    grant = server.get_consent_grant(request=request, end_user=None)

    session = server.store.query_session(_cookie(handler, SESSION_COOKIE))
    if session is None:
        if pending is None:
            pending = server.store.save_pending(
                PendingAuthorization(
                    request_id=secrets.token_urlsafe(24),
                    query=urllib.parse.urlsplit(handler.path).query,
                    client_id=request.client.get_client_id(),
                    redirect_uri=request.payload.redirect_uri,
                    scopes=tuple((request.scope or "").split()),
                    csrf=secrets.token_urlsafe(24),
                    source=client_address(handler),
                )
            )
        _redirect(handler, f"/oauth/login?rid={urllib.parse.quote(pending.request_id)}")
        return

    if pending is None:
        pending = server.store.save_pending(
            PendingAuthorization(
                request_id=secrets.token_urlsafe(24),
                query=urllib.parse.urlsplit(handler.path).query,
                client_id=request.client.get_client_id(),
                redirect_uri=request.payload.redirect_uri,
                scopes=tuple((request.scope or "").split()),
                csrf=secrets.token_urlsafe(24),
                source=client_address(handler),
            )
        )

    # Bind this record to the session now being shown the consent page. The
    # form token alone proves nothing: whoever created the record knows it.
    pending = server.store.bind_pending_session(
        pending.request_id, _session_hash(_cookie(handler, SESSION_COOKIE))
    )
    if pending is None:
        handler._error(400, "invalid_request", "Unknown or expired authorization request.")
        return

    nonce = pages.new_nonce()
    _html(
        handler,
        200,
        pages.consent_page(
            request_id=pending.request_id,
            csrf=pending.csrf,
            nonce=nonce,
            client_name=getattr(request.client, "name", pending.client_id),
            client_id=pending.client_id,
            redirect_uri=pending.redirect_uri,
            scopes=pending.scopes,
        ),
        nonce,
    )


def authorize_post(handler) -> None:
    server = handler.server.authorization
    form, _ = parse_form(handler, max_bytes=4096)

    session = server.store.query_session(_cookie(handler, SESSION_COOKIE))
    if session is None:
        handler._error(403, "access_denied", "No administrator session.")
        return

    pending = server.store.query_pending(form.get("rid", ""))
    if pending is None:
        handler._error(400, "invalid_request", "Unknown or expired authorization request.")
        return
    # One conditional consume carries every authorising predicate: the form
    # token, and the binding to the session the consent page was rendered to.
    # Checking them here rather than before the removal is what makes a
    # rejected submission non-destructive -- consuming first meant a wrong
    # csrf destroyed the operator's parked request.
    consumed = server.store.consume_pending(
        form.get("rid", ""),
        form.get("csrf", ""),
        _session_hash(_cookie(handler, SESSION_COOKIE)),
    )
    if consumed is None:
        handler._error(403, "access_denied", "Invalid form token for this session.")
        return
    pending = consumed

    # Rebuilt from the stored query, so nothing in this POST body can change
    # scope, redirect_uri, client_id or resource. The decision is the only input.
    request = server.request_from(handler, read_body=False, query_override=pending.query)
    server.get_consent_grant(request=request, end_user=session.subject)

    allowed = form.get("decision") == "allow"
    grant_id = None
    if allowed:
        # The consent becomes a record here, and its id becomes the token
        # subject. Before this, `subject` was the operator session's "admin"
        # for every consent by every client, so two grants were
        # indistinguishable and there was nothing for revocation to act on.
        # P3 says the actor is the grant rather than a directory user; this is
        # the line that makes that true.
        grant_id = "oauth:" + secrets.token_urlsafe(18)
        server.store.save_grant(
            Grant(
                grant_id=grant_id,
                client_id=pending.client_id,
                # Which operator session authorised it, not who they are: a
                # single shared administrator identity (P3) makes this a record
                # of provenance, not an identity claim.
                subject=session.subject,
                scopes=tuple((request.scope or "").split()),
            )
        )
    response = server.create_authorization_response(
        request=request,
        grant_user=grant_id,
        grant=server.get_consent_grant(
            request=request, end_user=session.subject
        ),
    )
    if not any(name.lower() == "content-type" for name, _ in response.headers):
        response.headers.append(("Content-Type", "text/plain; charset=utf-8"))
    response.write_to(handler)


def login_get(handler) -> None:
    server = handler.server.authorization
    pending = _pending_from(handler, server)
    if pending is None:
        handler._error(400, "invalid_request", "Unknown or expired authorization request.")
        return
    nonce = pages.new_nonce()
    _html(
        handler,
        200,
        pages.login_page(request_id=pending.request_id, csrf=pending.csrf, nonce=nonce),
        nonce,
    )


def login_post(handler) -> None:
    server = handler.server.authorization
    form, _ = parse_form(handler, max_bytes=4096)
    remote = client_address(handler)

    pending = server.store.query_pending(form.get("rid", ""))
    if pending is None:
        handler._error(400, "invalid_request", "Unknown or expired authorization request.")
        return
    if not _tokens_equal(pending.csrf, form.get("csrf", "")):
        handler._error(403, "access_denied", "Invalid form token.")
        return
    if _throttled(remote):
        handler._error(429, "access_denied", "Too many sign-in attempts.")
        return

    if not verify_password(form.get("password", ""), server.admin_password_hash):
        nonce = pages.new_nonce()
        _html(
            handler,
            401,
            pages.login_page(
                request_id=pending.request_id,
                csrf=pending.csrf,
                nonce=nonce,
                error="That password was not recognised.",
            ),
            nonce,
        )
        return

    _clear_throttle(remote)
    raw = secrets.token_urlsafe(32)
    now = time.time()
    server.store.save_session(
        raw,
        Session(subject="admin", auth_time=now, expires_at=now + SESSION_SECONDS),
    )
    # Path=/oauth and SameSite=Lax: Lax rides the top-level GET navigation that an
    # agent's browser handoff produces, which Strict would drop, while still
    # blocking cross-site POST. Secure is added by the production overlay.
    cookie = (
        f"{SESSION_COOKIE}={raw}; Path=/oauth; HttpOnly; SameSite=Lax; "
        f"Max-Age={SESSION_SECONDS}"
    )
    if server.secure_cookies:
        cookie += "; Secure"
    _redirect(
        handler,
        f"/oauth/authorize?rid={urllib.parse.quote(pending.request_id)}",
        extra=[("Set-Cookie", cookie)],
    )


def healthz(handler) -> None:
    """Healthy means the store answers, not merely that the process is up.

    This returned 200 unconditionally, so the container was healthy -- and
    `compose up --wait` was satisfied -- while every OAuth request failed on a
    database the component could not reach. The probe is a bare SELECT: enough
    to distinguish "reachable" from "not", and it reports no detail, because
    /healthz is answered before any caller is authenticated.
    """
    store = getattr(handler.server.authorization, "store", None)
    ping = getattr(store, "ping", None)
    if ping is not None:
        try:
            ping()
        except Exception:  # noqa: BLE001 - any failure to reach the store is unhealthy
            MappResponse(
                503,
                json.dumps({"status": "unavailable", "version": VERSION}),
                JSON_HEADERS,
            ).write_to(handler)
            return
    MappResponse(200, json.dumps({"status": "ok", "version": VERSION}), JSON_HEADERS).write_to(handler)


def _authenticate_broker(handler):
    """HTTP Basic, resolving to a confidential client, or None.

    The exchange is the most security-critical surface in the design, so the
    caller is authenticated before it is allowed to name a subject token at
    all: an unauthenticated request must never reach the point of being told
    whether some token exists.
    """
    header = handler.headers.get("Authorization") or ""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    client_id, separator, secret = decoded.partition(":")
    if not separator:
        return None
    client = handler.server.authorization.store.query_client(
        urllib.parse.unquote(client_id)
    )
    if client is None:
        return None
    if client.token_endpoint_auth_method != "client_secret_basic":
        return None
    if not client.check_client_secret(urllib.parse.unquote(secret)):
        return None
    return client


def exchange_endpoint(handler) -> None:
    """RFC 8693 token exchange, on the control listener only.

    Registered here and nowhere else. Putting it on /oauth/token would place
    the exchange on an edge-routed path, leaving the Caddy allowlist as the
    only thing between the internet and the ability to mint configuration-API
    credentials.
    """
    server = handler.server.authorization
    _, datalist = parse_form(handler, max_bytes=16 * 1024)
    client = _authenticate_broker(handler)
    if client is None:
        MappResponse(
            401,
            json.dumps({"error": "invalid_client"}),
            JSON_HEADERS + [("WWW-Authenticate", 'Basic realm="exchange"')],
        ).write_to(handler)
        return
    try:
        token = exchange.exchange(
            datalist=datalist,
            broker_client=client,
            store=server.store,
            resource=server.config_api_resource,
            mcp_resource=server.resource,
        )
    except exchange.ExchangeError as exc:
        status = 401 if exc.error == "invalid_client" else 400
        MappResponse(
            status,
            json.dumps({"error": exc.error, "error_description": exc.description}),
            JSON_HEADERS,
        ).write_to(handler)
        return
    MappResponse(200, json.dumps(token), JSON_HEADERS).write_to(handler)


def _control_endpoint(handler, action):
    """Shared shape for the authenticated control endpoints.

    The body is read before the caller is authenticated, so the connection
    stays framed and a rejected request cannot leave an unread body to be
    parsed as the next one. Nothing about the *token* is looked up until
    authentication has succeeded, which is the property that matters: an
    unauthenticated caller never learns whether a token exists, and that is
    the oracle RFC 7662 warns an open introspection endpoint becomes.

    A malformed body is therefore a 400 rather than a 401. That distinction
    describes the request, not the token, so it reveals nothing.
    """
    server = handler.server.authorization
    _, datalist = parse_form(handler, max_bytes=16 * 1024)
    client = _authenticate_broker(handler)
    if client is None:
        MappResponse(
            401,
            json.dumps({"error": "invalid_client"}),
            JSON_HEADERS + [("WWW-Authenticate", 'Basic realm="control"')],
        ).write_to(handler)
        return
    try:
        payload = action(datalist=datalist, store=server.store)
    except introspection.IntrospectionError as exc:
        MappResponse(
            400,
            json.dumps({"error": exc.error, "error_description": exc.description}),
            JSON_HEADERS,
        ).write_to(handler)
        return
    MappResponse(200, json.dumps(payload), JSON_HEADERS).write_to(handler)


def introspect_endpoint(handler) -> None:
    _control_endpoint(handler, introspection.introspect)


def revoke_endpoint(handler) -> None:
    _control_endpoint(handler, introspection.revoke)


def redeem_endpoint(handler) -> None:
    _control_endpoint(handler, introspection.redeem)


class EdgeServer(ThreadingHTTPServer):
    """The publicly reachable surface, over TCP. Tests use this directly."""

    daemon_threads = True
    allow_reuse_address = True
    #: A TCP peer is the real peer, so a forwarding header is attacker-supplied
    #: and must never be believed. Only the Unix subclass flips this.
    trust_forwarded_for = False

    def __init__(self, address, authorization) -> None:
        super().__init__(address, AuthHandler)
        self.authorization = authorization
        self.routes = {
            ("GET", "/.well-known/oauth-authorization-server"): metadata_endpoint,
            ("GET", "/oauth/authorize"): authorize_get,
            ("POST", "/oauth/authorize"): authorize_post,
            ("GET", "/oauth/login"): login_get,
            ("POST", "/oauth/login"): login_post,
            ("POST", "/oauth/token"): token_endpoint,
        }


class ControlServer(ThreadingHTTPServer):
    """The internal surface, reachable only on the mcp-control network.

    This one stays TCP: P12 puts introspection, exchange and token-B validation
    on ``mcp-control``, where the clients are config-ui and mapp-mcp on a
    Docker network, so the socket peer is the real peer.
    """

    daemon_threads = True
    allow_reuse_address = True
    trust_forwarded_for = False

    def __init__(self, address, authorization) -> None:
        super().__init__(address, AuthHandler)
        self.authorization = authorization
        self.routes = {
            ("GET", "/healthz"): healthz,
            ("POST", "/internal/oauth/exchange"): exchange_endpoint,
            ("POST", "/internal/oauth/introspect"): introspect_endpoint,
            ("POST", "/internal/oauth/revoke"): revoke_endpoint,
            # The configuration API spends a token B here. On the control
            # listener only: an edge-reachable redemption endpoint would let
            # anyone who captured a token burn it, or worse, execute with it.
            ("POST", "/internal/oauth/redeem"): redeem_endpoint,
        }


def build_store():
    """The store the deployed component uses: PostgreSQL, or nothing.

    This used to construct StubStore unconditionally, so the running service
    kept every credential in memory: a restart discarded all of them, the
    control schema it had been given was never written, and SqlStore -- with
    its atomic single-use consumption and its grant state -- was referenced by
    no production code at all. The in-memory store is a test double and is
    treated as one here.

    Failing closed rather than falling back. A silent fallback is how the
    component ran for a whole milestone against the wrong store while every
    test passed.
    """
    from sql_store import SqlStore

    dsn = os.environ.get("CONTROL_DATABASE_URL") or ""
    if not dsn:
        raise RuntimeError(
            "CONTROL_DATABASE_URL is not set. The authorization component keeps"
            " its records in the control schema and has no in-memory fallback;"
            " see docs/external-postgresql.md for deployments without the"
            " packaged database."
        )
    return SqlStore(dsn)


#: The MCP-side vocabulary: what a token A can carry for the MCP resource
#: itself, independent of anything the broker can exchange it for.
MCP_SCOPES = frozenset({"mcp:connect", "inspect", "propose", "visual"})

#: What the server accepts on an explicit request. Derived rather than
#: restated, because the restated version drifted: it listed neither `derive`,
#: `semantic:inspect`, `federation:provision` nor `semantic:apply`, so four of
#: the five allowlisted operations named scopes authlib refused to issue and
#: could never be exchanged for. Every test passed because each built its own
#: vocabulary. Deriving it means the allowlist and the issuer cannot disagree.
SUPPORTED_SCOPES = tuple(sorted(MCP_SCOPES | operations.all_required_scopes()))


#: The MCP runtime's client id on the control listener. It authenticates here to
#: introspect a token A and to exchange one for a token B; it holds no redirect
#: URI and no scopes, because it never appears in an authorization request.
MCP_RUNTIME_CLIENT_ID = "mapp-mcp"


def provision_runtime_client(store) -> bool:
    """Write mapp-mcp's confidential client row, from the deployment's secret.

    Reports whether it did, so a caller can say which.

    mapp-mcp cannot do what config-ui does. The configuration API provisions its
    own row because it owns the control schema and holds the DSN, which is why
    its secret reaches exactly one service and this component never sees the
    plaintext. mapp-mcp has no database credential by design -- it reaches
    platform state only through authenticated API calls -- so somebody else has
    to write the row, and the only candidates are this component, which already
    holds the DSN, or an operator running a command before the platform works.

    So the plaintext reaches two services rather than one: here, to be hashed,
    and mapp-mcp, to be presented. That is a real cost and the alternative is
    worse in a different way -- a platform that does not start until somebody
    remembers a registration step. Only the digest is stored either way.

    Empty secret means the feature is off, and the row is not written. A blank
    secret that provisioned a row would be a confidential client authenticated
    by the empty string.
    """
    secret = os.environ.get("MAPP_MCP_CLIENT_SECRET", "").strip()
    if not secret:
        return False
    from models import Client

    store.add_client(
        Client(
            client_id=MCP_RUNTIME_CLIENT_ID,
            name="MAPP MCP runtime",
            redirect_uris=(),
            scopes=(),
            grant_types=(),
            token_endpoint_auth_method="client_secret_basic",
            client_secret=secret,
        )
    )
    return True


def build_authorization(store=None):
    from issuer import MappAuthorizationServer

    issuer = os.environ.get("MCP_ISSUER", "http://mcp.localhost")
    resolved = store if store is not None else build_store()
    # Before the server is built, so a runtime that connects immediately after
    # this component reports healthy finds its row already there.
    provision_runtime_client(resolved)
    return MappAuthorizationServer(
        resolved,
        issuer=issuer,
        resource=os.environ.get("MCP_RESOURCE", issuer + "/mcp"),
        config_api_resource=os.environ.get(
            "MCP_CONFIG_API_RESOURCE", os.environ.get("CONFIG_SITE", "http://config.localhost") + "/api"
        ),
        # What the server will *accept* on an explicit request.
        scopes_supported=SUPPORTED_SCOPES,
        # What discovery *advertises*. P2: metadata carries only the safe
        # discovery scopes, so a greedy client cannot auto-request every
        # permission up front; a privileged scope needs a deliberate step-up
        # request, which the acceptance list above still admits.
        advertised_scopes=("mcp:connect", "inspect"),
        # No admin_password_hash: the issuer reads control.admin_credential
        # through the store. It used to come from MCP_AUTH_ADMIN_PASSWORD_HASH,
        # which nothing in the repository ever set and which .env.example did
        # not carry -- so ./bin/mapp doctor could not report it missing either,
        # and a correctly deployed component could authenticate nobody.
        #
        # Compared against the literal "true", not truthiness. The platform
        # teaches operators to write `false`, and bool("false") is True, so the
        # truthiness form turned Secure cookies ON for exactly the operators
        # who had written them off -- over plain HTTP, where the cookie is then
        # never sent and sign-in silently fails.
        secure_cookies=os.environ.get("MCP_AUTH_SECURE_COOKIES", "").strip().lower()
        == "true",
    )


class EdgeUnixServer(UnixSocketServerMixin, EdgeServer):
    """The edge surface as deployed: an AF_UNIX socket Caddy connects to.

    The route table is inherited from `EdgeServer` unchanged, so the control
    routes are absent from this listener as a property of the object rather
    than of the Caddy allowlist. Removing a path from the Caddyfile and
    removing it from this table are independent controls, and both hold.
    """

    trust_forwarded_for = True


def serve(edge_socket_path: str, control_address, authorization=None):
    """Start both listeners. Returns the two server objects, already bound."""
    authorization = authorization or build_authorization()
    edge = EdgeUnixServer(edge_socket_path, authorization)
    control = ControlServer(control_address, authorization)
    return edge, control


def main() -> None:
    import threading as _threading

    socket_path = os.environ.get("MCP_AUTH_SOCKET", "/run/mapp-auth/mapp-auth.sock")
    control_port = int(os.environ.get("MCP_AUTH_CONTROL_PORT", "8080"))
    edge, control = serve(socket_path, ("0.0.0.0", control_port))
    worker = _threading.Thread(target=control.serve_forever, daemon=True)
    worker.start()
    try:
        edge.serve_forever()
    finally:
        edge.server_close()
        control.shutdown()
        control.server_close()


if __name__ == "__main__":
    main()
