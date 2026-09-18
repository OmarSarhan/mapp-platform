"""M2 evidence: the browser-shaped authorization code flow, end to end.

Drives the real server over a real socket through every step an administrator's
browser actually takes: authorize, redirect to login, sign in, consent, and the
redirect carrying the code. Then spends the code at the token endpoint, so the
M1 and M2 halves are proven to join up.

Redirects are followed by hand rather than by a client library, because the
assertions are about the redirects themselves — the Location, the iss parameter
and the Set-Cookie.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from authlib.oauth2.rfc7636 import create_s256_code_challenge  # noqa: E402

from issuer import MappAuthorizationServer  # noqa: E402
from models import Client  # noqa: E402
from passwords import password_hash  # noqa: E402
from server import EdgeServer  # noqa: E402
from stub_store import StubStore  # noqa: E402

ISSUER = "http://mcp.localhost"
REDIRECT_URI = "http://127.0.0.1:9/callback"
VERIFIER = "a" * 64
PASSWORD = "correct horse battery staple"


class AuthorizationFlowTests(unittest.TestCase):
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
                scopes=("mcp:connect", "inspect", "propose"),
                token_endpoint_auth_method="none",
            )
        )
        self.authorization = MappAuthorizationServer(
            self.store,
            issuer=ISSUER,
            resource=ISSUER + "/mcp",
            scopes_supported=("mcp:connect", "inspect", "propose"),
            admin_password_hash=password_hash(PASSWORD),
        )
        self.httpd = EdgeServer(("127.0.0.1", 0), self.authorization)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    # -- transport -------------------------------------------------------

    def request(self, method: str, path: str, *, body=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {}
            payload = None
            if body is not None:
                payload = body if isinstance(body, str) else urllib.parse.urlencode(body)
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                headers["Content-Length"] = str(len(payload.encode()))
            if cookie:
                headers["Cookie"] = cookie
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8", "replace")
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

    def sign_in(self):
        """Walk authorize -> login -> signed-in, returning (cookie, rid)."""
        status, headers, _ = self.request("GET", self.authorize_url())
        self.assertEqual(302, status)
        login_path = headers["Location"]
        self.assertTrue(login_path.startswith("/oauth/login?rid="))
        rid = urllib.parse.parse_qs(urllib.parse.urlsplit(login_path).query)["rid"][0]

        status, _, page = self.request("GET", login_path)
        self.assertEqual(200, status)
        self.assertIn("Administrator password", page)
        csrf = self._hidden(page, "csrf")

        status, headers, _ = self.request(
            "POST", "/oauth/login", body={"rid": rid, "csrf": csrf, "password": PASSWORD}
        )
        self.assertEqual(302, status)
        cookie_header = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("SameSite=Lax", cookie_header)
        self.assertIn("Path=/oauth", cookie_header)
        cookie = cookie_header.split(";", 1)[0]
        return cookie, rid

    @staticmethod
    def _hidden(page: str, name: str) -> str:
        marker = f'name="{name}" value="'
        start = page.index(marker) + len(marker)
        return page[start : page.index('"', start)]

    # -- the headline evidence -------------------------------------------

    def test_full_flow_issues_a_code_and_then_a_token(self) -> None:
        cookie, rid = self.sign_in()

        status, _, page = self.request("GET", f"/oauth/authorize?rid={rid}", cookie=cookie)
        self.assertEqual(200, status)
        self.assertIn("Authorise this client?", page)
        self.assertIn("Claude Code", page)
        # Scopes listed individually, not as a run-together string.
        self.assertIn("<code>mcp:connect</code>", page)
        self.assertIn("<code>inspect</code>", page)
        csrf = self._hidden(page, "csrf")

        status, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": csrf, "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(302, status)
        target = urllib.parse.urlsplit(headers["Location"])
        returned = urllib.parse.parse_qs(target.query)
        self.assertEqual(REDIRECT_URI, f"{target.scheme}://{target.netloc}{target.path}")
        self.assertEqual(["client-state-123"], returned["state"])
        # RFC 9207, required by P1 on success responses.
        self.assertEqual([ISSUER], returned["iss"])
        code = returned["code"][0]

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
        self.assertEqual(200, status, raw)
        token = json.loads(raw)
        self.assertEqual("mcp:connect inspect", token["scope"])
        self.assertTrue(token["access_token"].startswith("mapp_a_"))
        self.assertNotIn("refresh_token", token)

    def test_denial_redirects_with_access_denied_and_iss(self) -> None:
        cookie, rid = self.sign_in()
        status, _, page = self.request("GET", f"/oauth/authorize?rid={rid}", cookie=cookie)
        csrf = self._hidden(page, "csrf")

        status, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": csrf, "decision": "deny"},
            cookie=cookie,
        )
        self.assertEqual(302, status)
        returned = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)
        self.assertEqual(["access_denied"], returned["error"])
        self.assertEqual(["client-state-123"], returned["state"])
        # P1 requires iss on error responses too, which is why the response
        # object exposes a writable location rather than being a tuple.
        self.assertEqual([ISSUER], returned["iss"])
        self.assertNotIn("code", returned)

    # -- the property the stored query buys ------------------------------

    def test_consent_submission_cannot_widen_scope(self) -> None:
        cookie, rid = self.sign_in()
        _, _, page = self.request("GET", f"/oauth/authorize?rid={rid}", cookie=cookie)
        csrf = self._hidden(page, "csrf")

        status, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={
                "rid": rid,
                "csrf": csrf,
                "decision": "allow",
                # A compromised or buggy client tampering with the consent form.
                "scope": "mcp:connect inspect propose",
                "redirect_uri": "http://evil.example/callback",
                "client_id": "someone-else",
            },
            cookie=cookie,
        )
        self.assertEqual(302, status)
        target = urllib.parse.urlsplit(headers["Location"])
        self.assertEqual(REDIRECT_URI, f"{target.scheme}://{target.netloc}{target.path}")
        code = urllib.parse.parse_qs(target.query)["code"][0]
        record = self.store.query_authorization_code(code, "mcp-client")
        self.assertIsNotNone(record)
        self.assertEqual("mcp:connect inspect", record.scope)
        self.assertEqual(REDIRECT_URI, record.redirect_uri)

    def test_consent_is_single_use(self) -> None:
        cookie, rid = self.sign_in()
        _, _, page = self.request("GET", f"/oauth/authorize?rid={rid}", cookie=cookie)
        csrf = self._hidden(page, "csrf")
        body = {"rid": rid, "csrf": csrf, "decision": "allow"}
        first, _, _ = self.request("POST", "/oauth/authorize", body=body, cookie=cookie)
        self.assertEqual(302, first)
        second, _, raw = self.request("POST", "/oauth/authorize", body=body, cookie=cookie)
        self.assertEqual(400, second)
        self.assertEqual("invalid_request", json.loads(raw)["error"])

    # -- refusals --------------------------------------------------------

    def test_consent_without_a_session_is_refused(self) -> None:
        """Reaches the session branch, not the form-token branch.

        Sending csrf="x" here would be refused by the token check first, which
        returns the same 403, so the test would pass with both session controls
        deleted -- and an unauthenticated caller who knew the rid would then be
        issued an authorization code.
        """
        cookie, rid = self.sign_in()
        # Render the consent page so the record carries a real form token.
        _, _, page = self.request(
            "GET", self.authorize_url() + f"&rid={rid}", cookie=cookie
        )
        csrf = self._hidden(page, "csrf")
        status, headers, raw = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": csrf, "decision": "allow"},
        )  # deliberately no cookie
        self.assertEqual(403, status)
        self.assertEqual("access_denied", json.loads(raw)["error"])
        self.assertNotIn("Location", headers)

    def test_consent_with_a_wrong_form_token_is_refused(self) -> None:
        cookie, rid = self.sign_in()
        status, _, raw = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": "not-the-token", "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(403, status)
        self.assertEqual("access_denied", json.loads(raw)["error"])

    def test_wrong_password_re_renders_the_form(self) -> None:
        status, headers, _ = self.request("GET", self.authorize_url())
        rid = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)["rid"][0]
        _, _, page = self.request("GET", f"/oauth/login?rid={rid}")
        csrf = self._hidden(page, "csrf")
        status, headers, page = self.request(
            "POST", "/oauth/login", body={"rid": rid, "csrf": csrf, "password": "wrong"}
        )
        self.assertEqual(401, status)
        self.assertIn("was not recognised", page)
        self.assertNotIn("Set-Cookie", headers)

    def test_unknown_redirect_uri_is_refused_without_redirecting(self) -> None:
        status, headers, raw = self.request(
            "GET", self.authorize_url(redirect_uri="http://evil.example/cb")
        )
        # An unregistered redirect target must never be redirected to.
        self.assertNotIn("Location", headers)
        self.assertEqual(400, status)
        self.assertIn("not supported by client", raw)

    def test_missing_code_challenge_is_refused_before_consent(self) -> None:
        """PKCE presence is our check, not authlib's.

        authlib's CodeChallenge(required=True) returns early when neither
        code_challenge nor code_challenge_method is present, and only consults
        `required` at the token endpoint. Without the override in
        MappAuthorizationCodeGrant this request reaches the login screen and is
        refused only after the operator has authorised it.
        """
        status, headers, raw = self.request(
            "GET", self.authorize_url(code_challenge=None, code_challenge_method=None)
        )
        self.assertEqual(302, status)
        # Refused by redirecting the error to the registered callback, per
        # RFC 6749: the client is known and the redirect_uri is validated.
        returned = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)
        self.assertEqual(["invalid_request"], returned["error"])
        self.assertIn("code_challenge", returned["error_description"][0])
        self.assertEqual([ISSUER], returned["iss"])
        self.assertNotIn("code", returned)

    # -- discovery -------------------------------------------------------

    def test_metadata_is_served_and_omits_registration(self) -> None:
        status, headers, raw = self.request("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(200, status)
        self.assertEqual("application/json", headers["Content-Type"])
        document = json.loads(raw)
        self.assertEqual(ISSUER, document["issuer"])
        self.assertEqual(["S256"], document["code_challenge_methods_supported"])
        self.assertTrue(document["authorization_response_iss_parameter_supported"])
        # Dynamic Client Registration is refused, so it must not be advertised.
        self.assertNotIn("registration_endpoint", document)

    def test_metadata_passes_rfc8414_validation(self) -> None:
        from authlib.oauth2.rfc8414 import AuthorizationServerMetadata
        from authlib.oauth2.rfc9207 import AuthorizationServerMetadata as IssMetadata

        # rfc8414 requires an https issuer, so validate against a production-shaped
        # fixture rather than the http dev origin.
        production = MappAuthorizationServer(
            StubStore(),
            issuer="https://mcp.example.test",
            resource="https://mcp.example.test/mcp",
            scopes_supported=("mcp:connect", "inspect"),
        ).metadata_document()
        AuthorizationServerMetadata(production).validate(metadata_classes=[IssMetadata])

    def test_the_consent_policy_admits_the_client_s_own_redirect_origin(
        self,
    ) -> None:
        """O20, measured in three engines and then fixed here.

        The consent POST is answered with a 302 to the client's cross-origin
        redirect_uri. Chromium and WebKit re-check `form-action` across that
        redirect and blocked it; Firefox did not. All three delivered the POST
        first, so the grant was created every time and only the authorization
        code was lost -- the operator had consented, the platform held a live
        grant, and the agent got nothing. A clean refusal would have been
        better; this looked like success on one side and silence on the other.

        The origin comes from the pending record, which is the redirect the
        server already matched exactly, so the policy cannot be widened by
        anything a submission proposes.
        """
        cookie, rid = self.sign_in()
        _, headers, _ = self.request(
            "GET", f"/oauth/authorize?rid={rid}", cookie=cookie
        )
        policy = headers["Content-Security-Policy"]
        origin = "http://127.0.0.1:9"
        self.assertIn(f"form-action 'self' {origin}", policy)
        # Still an origin, never the bare path or a wildcard.
        self.assertNotIn("*", policy)
        self.assertNotIn("/callback", policy)

    def test_the_login_policy_stays_self_only(self) -> None:
        """Only the consent submission crosses origins. Widening the login page
        too would extend the directive to a page that never needs it."""
        _, _, _ = self.request("GET", self.authorize_url())
        cookie, rid = self.sign_in()
        _, headers, _ = self.request("GET", f"/oauth/login?rid={rid}")
        self.assertIn("form-action 'self';", headers["Content-Security-Policy"])

    def test_content_security_policy_forbids_script(self) -> None:
        status, headers, _ = self.request("GET", self.authorize_url())
        rid = urllib.parse.parse_qs(urllib.parse.urlsplit(headers["Location"]).query)["rid"][0]
        _, headers, page = self.request("GET", f"/oauth/login?rid={rid}")
        policy = headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("form-action 'self'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn("script-src", policy)
        self.assertNotIn("<script", page)


if __name__ == "__main__":
    unittest.main()
