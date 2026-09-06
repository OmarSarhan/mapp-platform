"""Regressions for the defects an adversarial review of M1/M2 reproduced.

Each test here exists because a probe demonstrated a working attack, or because
a mutation of the production code left the whole suite green. They are grouped
by the property they pin rather than by the file they touch, because several of
the fixes span two modules.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from authlib.oauth2.rfc7636 import create_s256_code_challenge  # noqa: E402

from issuer import MappAuthorizationServer  # noqa: E402
from models import AuthorizationCode, Client  # noqa: E402
from passwords import password_hash  # noqa: E402
from server import EdgeServer  # noqa: E402
from stub_store import StubStore  # noqa: E402

ISSUER = "http://mcp.localhost"
REDIRECT_URI = "http://127.0.0.1:9/callback"
VERIFIER = "a" * 64
PASSWORD = "correct horse battery staple"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

    def setUp(self) -> None:
        self.store = StubStore()
        self.store.add_client(
            Client(
                client_id="mcp-client",
                name="Claude Code",
                redirect_uris=(REDIRECT_URI,),
                scopes=("mcp:connect", "inspect"),
                token_endpoint_auth_method="none",
            )
        )
        self.authorization = MappAuthorizationServer(
            self.store,
            issuer=ISSUER,
            resource=ISSUER + "/mcp",
            scopes_supported=("mcp:connect", "inspect", "propose"),
            admin_password_hash=password_hash(PASSWORD),
            secure_cookies=False,
        )
        self.httpd = EdgeServer(("127.0.0.1", 0), self.authorization)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, *, body=None, cookie=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            sent = dict(headers or {})
            payload = None
            if body is not None:
                payload = body if isinstance(body, str) else urllib.parse.urlencode(body)
                sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
                sent.setdefault("Content-Length", str(len(payload.encode())))
            if cookie:
                sent["Cookie"] = cookie
            connection.request(method, path, body=payload, headers=sent)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode(
                "utf-8", "replace"
            )
        finally:
            connection.close()

    def authorize_url(self, **overrides) -> str:
        params = {
            "response_type": "code",
            "client_id": "mcp-client",
            "redirect_uri": REDIRECT_URI,
            "scope": "mcp:connect inspect",
            "state": "client-state-123",
            "code_challenge": create_s256_code_challenge(VERIFIER),
            "code_challenge_method": "S256",
        }
        params.update(overrides)
        return "/oauth/authorize?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )

    @staticmethod
    def _hidden(page: str, name: str) -> str:
        marker = f'name="{name}" value="'
        start = page.index(marker) + len(marker)
        return page[start : page.index('"', start)]

    def sign_in(self, url=None):
        """Return (cookie, rid) for a signed-in operator on `url`."""
        status, headers, _ = self.request("GET", url or self.authorize_url())
        self.assertEqual(302, status)
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]
        _, _, page = self.request("GET", headers["Location"])
        csrf = self._hidden(page, "csrf")
        status, headers, _ = self.request(
            "POST", "/oauth/login", body={"rid": rid, "csrf": csrf, "password": PASSWORD}
        )
        self.assertEqual(302, status)
        return headers["Set-Cookie"].split(";", 1)[0], rid


class ResponseQueueTests(Base):
    """One request must never produce two responses.

    protocol_version is HTTP/1.1, so an unread request body is parsed as the
    next request on the same connection. Caddy pools upstream connections, so
    the surplus response is delivered to whoever holds the connection next.
    """

    def _raw_exchange(self, raw: bytes) -> bytes:
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            client.sendall(raw)
            chunks = []
            while True:
                try:
                    received = client.recv(65536)
                except socket.timeout:
                    break
                if not received:
                    break
                chunks.append(received)
            return b"".join(chunks)
        finally:
            client.close()

    @staticmethod
    def _count_responses(raw: bytes) -> int:
        return raw.count(b"HTTP/1.0 ") + raw.count(b"HTTP/1.1 ")

    def _assert_single_response(self, raw: bytes) -> None:
        """One response, and the connection announced its own end.

        Counting status lines alone is not sufficient: when the leftover body
        fails to parse as a request line, parse_request leaves request_version
        at the HTTP/0.9 default and send_response_only writes no status line at
        all, so a smuggled response can be served with nothing to count. The
        load-bearing assertion is that the server said it was closing.
        """
        self.assertEqual(1, self._count_responses(raw), raw[:400])
        self.assertIn(b"Connection: close", raw)

    def test_an_unrouted_path_with_a_body_yields_one_response(self) -> None:
        smuggled = b"GET /.well-known/oauth-authorization-server HTTP/1.1\r\nHost: x\r\n\r\n"
        raw = (
            b"POST /nope HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled
        )
        self._assert_single_response(self._raw_exchange(raw))

    def test_a_rejected_content_type_yields_one_response(self) -> None:
        smuggled = b"GET /.well-known/oauth-authorization-server HTTP/1.1\r\nHost: x\r\n\r\n"
        raw = (
            b"POST /oauth/token HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled
        )
        body = self._raw_exchange(raw)
        self._assert_single_response(body)
        self.assertIn(b"invalid_request", body)

    def test_an_oversized_body_yields_one_response(self) -> None:
        smuggled = b"GET /.well-known/oauth-authorization-server HTTP/1.1\r\nHost: x\r\n\r\n"
        padding = b"x" * 9000
        raw = (
            b"POST /oauth/token HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: " + str(len(smuggled) + len(padding)).encode()
            + b"\r\n\r\n" + smuggled + padding
        )
        self._assert_single_response(self._raw_exchange(raw))


class TransferEncodingTests(Base):
    """Any Transfer-Encoding header is refused, however it is spelled."""

    def _send(self, te_headers: bytes) -> bytes:
        body = b"grant_type=authorization_code"
        raw = (
            b"POST /oauth/token HTTP/1.1\r\nHost: x\r\n"
            + te_headers
            + b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            client.sendall(raw)
            chunks = []
            while True:
                try:
                    received = client.recv(65536)
                except socket.timeout:
                    break
                if not received:
                    break
                chunks.append(received)
            return b"".join(chunks)
        finally:
            client.close()

    def _assert_refused(self, raw: bytes) -> None:
        """Refused with a 400 and no body parsing, whatever the wording.

        A duplicated header is caught by the duplicate rule and a single one by
        the chunked rule; both are refusals, and pinning the exact string would
        make the test about the message rather than the property.
        """
        self.assertIn(b"HTTP/1.1 400", raw)
        self.assertIn(b"invalid_request", raw)
        # The decisive part: authlib never saw the body, so no grant error.
        self.assertNotIn(b"unsupported_grant_type", raw)
        self.assertNotIn(b"invalid_client", raw)

    def test_a_plain_chunked_header_is_refused(self) -> None:
        raw = self._send(b"Transfer-Encoding: chunked\r\n")
        self._assert_refused(raw)
        self.assertIn(b"Chunked request bodies", raw)

    def test_an_empty_header_before_chunked_is_still_refused(self) -> None:
        # .get() returns only the first occurrence and reads "" as absent, so
        # this pair used to slip through and be framed by Content-Length --
        # the request reached authlib and was answered 401 instead of 400.
        self._assert_refused(b"".join([self._send(b"Transfer-Encoding:\r\nTransfer-Encoding: chunked\r\n")]))

    def test_a_lone_empty_header_is_refused(self) -> None:
        self._assert_refused(self._send(b"Transfer-Encoding:\r\n"))

    def test_chunked_without_content_length_does_not_leak_a_second_response(self) -> None:
        """The vector every other case here missed.

        Each test above sends a Content-Length, which is the only reason the
        connection was closed. With Transfer-Encoding alone the old guard read
        the absent Content-Length as zero, kept the connection, and served the
        smuggled request as a second response.
        """
        smuggled = b"GET /.well-known/oauth-authorization-server HTTP/1.1\r\nHost: x\r\n\r\n"
        raw = (
            b"POST /oauth/token HTTP/1.1\r\nHost: x\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n\r\n" + smuggled
        )
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            client.sendall(raw)
            chunks = []
            while True:
                try:
                    received = client.recv(65536)
                except socket.timeout:
                    break
                if not received:
                    break
                chunks.append(received)
            body = b"".join(chunks)
        finally:
            client.close()
        self.assertNotIn(b"issuer", body, "metadata leaked as a second response")
        self.assertEqual(1, body.count(b"HTTP/1.1 "), body[:400])

    def test_duplicate_content_length_does_not_leak_a_second_response(self) -> None:
        # `.get` returned only the first value, so "0" then a real length kept
        # the connection alive with the body still buffered.
        smuggled = b"GET /.well-known/oauth-authorization-server HTTP/1.1\r\nHost: x\r\n\r\n"
        raw = (
            b"POST /oauth/token HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: 0\r\n"
            b"Content-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled
        )
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            client.sendall(raw)
            chunks = []
            while True:
                try:
                    received = client.recv(65536)
                except socket.timeout:
                    break
                if not received:
                    break
                chunks.append(received)
            body = b"".join(chunks)
        finally:
            client.close()
        self.assertNotIn(b"issuer", body, "metadata leaked as a second response")


class ConsentBindingTests(Base):
    """Consent must be bound to the session that was shown the consent page."""

    def setUp(self) -> None:
        super().setUp()
        self.store.add_client(
            Client(
                client_id="other-client",
                name="Attacker",
                redirect_uris=("http://127.0.0.1:9/attacker",),
                scopes=("mcp:connect", "inspect", "propose"),
                token_endpoint_auth_method="none",
            )
        )

    def _attacker_pending(self):
        """An unauthenticated caller parks a request and reads its form token."""
        url = self.authorize_url(
            client_id="other-client",
            redirect_uri="http://127.0.0.1:9/attacker",
            scope="mcp:connect inspect propose",
        )
        status, headers, _ = self.request("GET", url)
        self.assertEqual(302, status)
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]
        _, _, page = self.request("GET", headers["Location"])
        return rid, self._hidden(page, "csrf")

    def test_a_session_cannot_consent_to_a_request_it_was_never_shown(self) -> None:
        attacker_rid, attacker_csrf = self._attacker_pending()
        # The operator signs in on their own, benign request.
        cookie, _ = self.sign_in()
        status, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": attacker_rid, "csrf": attacker_csrf, "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(403, status)
        self.assertNotIn("Location", headers)

    def test_the_operators_own_consent_still_succeeds(self) -> None:
        """The binding must not break the flow it protects."""
        cookie, rid = self.sign_in()
        status, _, page = self.request("GET", self.authorize_url() + f"&rid={rid}", cookie=cookie)
        self.assertEqual(200, status)
        csrf = self._hidden(page, "csrf")
        status, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": csrf, "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(302, status)
        self.assertIn("code=", headers["Location"])

    def test_a_non_ascii_form_token_is_refused_not_fatal(self) -> None:
        # secrets.compare_digest raises TypeError on non-ASCII str operands,
        # which killed the handler thread and dropped the connection.
        cookie, rid = self.sign_in()
        # Render consent first: without the session binding in place, the
        # consume short-circuits before the csrf is ever compared, and the test
        # would assert a 403 that has nothing to do with the comparison.
        self.request("GET", self.authorize_url() + f"&rid={rid}", cookie=cookie)
        status, _, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": "é", "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(403, status)

    def test_a_non_ascii_login_token_is_refused_not_fatal(self) -> None:
        status, headers, _ = self.request("GET", self.authorize_url())
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]
        status, _, _ = self.request(
            "POST",
            "/oauth/login",
            body={"rid": rid, "csrf": "é", "password": PASSWORD},
        )
        self.assertIn(status, (401, 403))


class PkceEnforcementTests(Base):
    """PKCE is required of every client, and only S256 is accepted."""

    def setUp(self) -> None:
        super().setUp()
        self.store.add_client(
            Client(
                client_id="confidential",
                name="Broker",
                redirect_uris=(REDIRECT_URI,),
                scopes=("mcp:connect",),
                token_endpoint_auth_method="client_secret_basic",
                client_secret="s3cret",
            )
        )

    def test_a_confidential_client_must_still_send_a_challenge(self) -> None:
        # RFC 9700 s2.1.1 requires PKCE for every client using this grant. The
        # earlier condition exempted anyone holding a secret.
        url = self.authorize_url(
            client_id="confidential",
            scope="mcp:connect",
            code_challenge=None,
            code_challenge_method=None,
        )
        status, headers, _ = self.request("GET", url)
        self.assertEqual(302, status)
        self.assertIn("error=invalid_request", headers["Location"])

    def test_a_public_client_must_send_a_challenge(self) -> None:
        url = self.authorize_url(code_challenge=None, code_challenge_method=None)
        status, headers, _ = self.request("GET", url)
        self.assertEqual(302, status)
        self.assertIn("error=invalid_request", headers["Location"])

    def test_the_plain_challenge_method_is_refused(self) -> None:
        # With `plain` the challenge IS the verifier, so anything that sees the
        # authorization URL recovers it. Metadata advertises S256 only.
        url = self.authorize_url(code_challenge="b" * 64, code_challenge_method="plain")
        status, headers, _ = self.request("GET", url)
        self.assertEqual(302, status)
        self.assertIn("error=", headers["Location"])

    def test_metadata_and_enforcement_agree(self) -> None:
        _, _, raw = self.request("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(["S256"], json.loads(raw)["code_challenge_methods_supported"])


class RedirectUriExactnessTests(Base):
    """Registered redirect URIs match exactly, never by prefix."""

    def test_a_suffix_extension_of_a_registered_uri_is_refused(self) -> None:
        # A prefix test would admit this and hand codes to an attacker host,
        # while still refusing the wholly-unrelated URI the older test used.
        status, headers, _ = self.request(
            "GET", self.authorize_url(redirect_uri=REDIRECT_URI + ".attacker.example/steal")
        )
        self.assertEqual(400, status)
        self.assertNotIn("Location", headers)

    def test_an_appended_path_is_refused(self) -> None:
        status, headers, _ = self.request(
            "GET", self.authorize_url(redirect_uri=REDIRECT_URI + "/extra")
        )
        self.assertEqual(400, status)
        self.assertNotIn("Location", headers)


class ScopeNarrowingTests(Base):
    """An unheld scope is refused, never silently reduced to nothing."""

    def test_requesting_only_unheld_scopes_is_refused(self) -> None:
        status, headers, _ = self.request("GET", self.authorize_url(scope="propose"))
        self.assertEqual(302, status)
        self.assertIn("error=invalid_scope", headers["Location"])

    def test_omitting_scope_entirely_is_refused(self) -> None:
        status, headers, _ = self.request("GET", self.authorize_url(scope=None))
        self.assertEqual(302, status)
        self.assertIn("error=invalid_scope", headers["Location"])

    def test_partial_narrowing_keeps_only_held_scopes(self) -> None:
        url = self.authorize_url(scope="mcp:connect propose")
        cookie, rid = self.sign_in(url)
        _, _, page = self.request("GET", url + f"&rid={rid}", cookie=cookie)
        self.assertIn("mcp:connect", page)
        self.assertNotIn("propose", page)
        csrf = self._hidden(page, "csrf")
        _, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": csrf, "decision": "allow"},
            cookie=cookie,
        )
        code = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["code"][0]
        record = self.store.query_authorization_code(code, "mcp-client")
        # The stored scope must come from the resolved request.scope, not from
        # the raw payload: "propose" is in scopes_supported but not held by
        # this client, so the two sources differ here and are distinguishable.
        self.assertEqual("mcp:connect", record.scope)


class ContentSecurityPolicyTests(Base):
    """Every response this component serves carries a CSP.

    Caddy sets none on the four proxied auth paths -- its header directive
    replaces the upstream's, which would strip the nonce policy off the consent
    and login pages -- so anything this component fails to set is simply absent
    at the edge.
    """

    def test_a_json_error_carries_a_policy(self) -> None:
        _, headers, _ = self.request(
            "GET", "/oauth/authorize?response_type=code&client_id=nosuch"
        )
        self.assertIn("Content-Security-Policy", headers)

    def test_the_metadata_document_carries_a_policy(self) -> None:
        _, headers, _ = self.request("GET", "/.well-known/oauth-authorization-server")
        self.assertIn("Content-Security-Policy", headers)

    def test_an_unrouted_path_carries_a_policy(self) -> None:
        _, headers, _ = self.request("GET", "/nope")
        self.assertIn("Content-Security-Policy", headers)

    def test_the_consent_page_keeps_its_nonce_policy(self) -> None:
        cookie, rid = self.sign_in()
        _, headers, _ = self.request(
            "GET", self.authorize_url() + f"&rid={rid}", cookie=cookie
        )
        # The HTML policy must remain the nonce one, not the JSON blanket.
        self.assertIn("nonce-", headers["Content-Security-Policy"])


class HeaderSafetyTests(unittest.TestCase):
    """No header value may split the response."""

    def test_a_newline_in_a_header_value_is_refused(self) -> None:
        from authlib_adapter import MappResponse

        response = MappResponse(
            302, "", [("Location", "http://x/cb\r\nX-Injected: 1")]
        )
        with self.assertRaises(ValueError):
            response.write_to(object())

    def test_an_ordinary_header_still_writes(self) -> None:
        from authlib_adapter import MappResponse

        written: list = []

        class _Handler:
            command = "GET"
            close_connection = False

            def send_response(self, status): written.append(("status", status))
            def send_header(self, name, value): written.append((name, value))
            def end_headers(self): written.append(("end", None))
            wfile = type("W", (), {"write": staticmethod(lambda b: written.append(("body", b)))})()

        MappResponse(200, "{}", [("Content-Type", "application/json")]).write_to(_Handler())
        self.assertIn(("status", 200), written)


class ClientSecretTests(Base):
    """A confidential client's secret comparison cannot be crashed."""

    def setUp(self) -> None:
        super().setUp()
        self.store.add_client(
            Client(
                client_id="confidential",
                name="Broker",
                redirect_uris=(REDIRECT_URI,),
                scopes=("mcp:connect",),
                token_endpoint_auth_method="client_secret_basic",
                client_secret="s3cret",
            )
        )

    def test_a_non_ascii_secret_is_refused_not_fatal(self) -> None:
        import base64

        credentials = base64.b64encode("confidential:é".encode()).decode()
        status, _, _ = self.request(
            "POST",
            "/oauth/token",
            body={"grant_type": "authorization_code", "code": "x", "client_id": "confidential"},
            headers={"Authorization": "Basic " + credentials},
        )
        # A refusal, not a dropped connection.
        self.assertIn(status, (400, 401))


class AdvertisedScopeTests(Base):
    """Metadata must not advertise a scope this issuer does not place."""

    def test_the_default_configuration_omits_apply(self) -> None:
        import server as server_module

        # The store is injected: build_authorization() now refuses to run
        # without a control database, because the deployed component must
        # never silently fall back to the in-memory double.
        authorization = server_module.build_authorization(StubStore())
        self.assertNotIn("apply", authorization.metadata_document()["scopes_supported"])


class SecureCookieSettingTests(Base):
    """MCP_AUTH_SECURE_COOKIES is a word, not a truthiness test.

    The platform's identically-named sibling setting is compared against the
    literal "true", and .env teaches operators to write `false`. Under
    bool(os.environ.get(...)) that string is True, so the operators who
    explicitly turned Secure cookies off were the ones who got them on -- over
    plain HTTP, where the browser then never returns the cookie and sign-in
    fails with nothing in the logs to explain it.
    """

    def build(self, value):
        import os

        import server as server_module

        previous = os.environ.get("MCP_AUTH_SECURE_COOKIES")
        if value is None:
            os.environ.pop("MCP_AUTH_SECURE_COOKIES", None)
        else:
            os.environ["MCP_AUTH_SECURE_COOKIES"] = value
        try:
            return server_module.build_authorization(StubStore()).secure_cookies
        finally:
            if previous is None:
                os.environ.pop("MCP_AUTH_SECURE_COOKIES", None)
            else:
                os.environ["MCP_AUTH_SECURE_COOKIES"] = previous

    def test_only_the_word_true_enables_secure_cookies(self) -> None:
        for value in ("true", "TRUE", " True "):
            with self.subTest(value=value):
                self.assertTrue(self.build(value))

    def test_everything_else_leaves_them_off(self) -> None:
        for value in (None, "", "false", "False", "0", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(self.build(value))


class LoginTokenTests(Base):
    """The login form token is checked, not merely present."""

    def test_a_wrong_login_token_is_refused_and_sets_no_cookie(self) -> None:
        status, headers, _ = self.request("GET", self.authorize_url())
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]
        status, headers, _ = self.request(
            "POST",
            "/oauth/login",
            body={"rid": rid, "csrf": "not-the-token", "password": PASSWORD},
        )
        # The correct password must not be enough on its own.
        self.assertEqual(403, status)
        self.assertNotIn("Set-Cookie", headers)


class CodeExpiryTests(Base):
    """The expiry predicate inside the atomic consume is load-bearing."""

    def test_an_expired_code_is_refused(self) -> None:
        record = AuthorizationCode(
            code="stale-code",
            client_id="mcp-client",
            redirect_uri=REDIRECT_URI,
            scope="mcp:connect",
            subject="oauth:grant-1",
            code_challenge=create_s256_code_challenge(VERIFIER),
        )
        record.expires_at = 0.0  # in the past
        self.store.save_authorization_code(record)
        status, _, raw = self.request(
            "POST",
            "/oauth/token",
            body={
                "grant_type": "authorization_code",
                "code": "stale-code",
                "redirect_uri": REDIRECT_URI,
                "client_id": "mcp-client",
                "code_verifier": VERIFIER,
            },
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_grant", json.loads(raw)["error"])


class SingleUseCodeTests(Base):
    """One authorization code yields exactly one token, even under a race."""

    def _seed(self, code: str) -> None:
        self.store.save_authorization_code(
            AuthorizationCode(
                code=code,
                client_id="mcp-client",
                redirect_uri=REDIRECT_URI,
                scope="mcp:connect inspect",
                subject="oauth:grant-1",
                code_challenge=create_s256_code_challenge(VERIFIER),
            )
        )

    def _exchange(self, code: str):
        status, _, raw = self.request(
            "POST",
            "/oauth/token",
            body={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": "mcp-client",
                "code_verifier": VERIFIER,
            },
        )
        return status

    def test_concurrent_exchanges_of_one_code_yield_one_token(self) -> None:
        # Without a tiny switch interval the interpreter rarely preempts inside
        # the window, and the pre-fix ordering (query, mint, then delete) passes
        # this test every time -- so it would not measure atomicity at all.
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, previous)
        self._seed("raced-code")
        results: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def worker() -> None:
            barrier.wait()
            status = self._exchange("raced-code")
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(6, len(results))
        # Before the fix this was 6 successes, in 20 of 20 trials: authlib
        # deleted the code only after minting the token, so every racing
        # request queried a live code.
        self.assertEqual(1, results.count(200), f"statuses={sorted(results)}")
        self.assertEqual(1, len(self.store._tokens))

    def test_a_code_belonging_to_another_client_is_refused(self) -> None:
        self._seed("other-clients-code")
        self.store.add_client(
            Client(
                client_id="second",
                name="Second",
                redirect_uris=(REDIRECT_URI,),
                scopes=("mcp:connect",),
                token_endpoint_auth_method="none",
            )
        )
        status, _, _ = self.request(
            "POST",
            "/oauth/token",
            body={
                "grant_type": "authorization_code",
                "code": "other-clients-code",
                "redirect_uri": REDIRECT_URI,
                "client_id": "second",
                "code_verifier": VERIFIER,
            },
        )
        self.assertEqual(400, status)
        # The rightful owner's code must survive a wrong-client attempt.
        self.assertEqual(200, self._exchange("other-clients-code"))


if __name__ == "__main__":
    unittest.main()
