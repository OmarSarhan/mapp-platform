"""M1 evidence: a real token request, over a real socket, through authlib.

This is the Phase 0 artefact for P1's "proving rather than assuming its custom
constrained exchange support". One POST exercises four unknowns at once — the
adapter's payload/form/headers surface, the form parser the platform did not
have, authlib's secure-transport check against the configured dev origin, and
the model mixins.

Pattern follows semantic-service/tests/test_server.py: a real ThreadingHTTPServer
on port 0 in a daemon thread, driven with http.client. The config-ui idiom of
constructing a handler with object.__new__ cannot express form bodies, redirects
or Set-Cookie, so it is unsuitable here.
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

from authlib_adapter import build_request  # noqa: E402
from issuer import MappAuthorizationServer  # noqa: E402
from models import AuthorizationCode, Client  # noqa: E402
from server import EdgeServer  # noqa: E402
from stub_store import StubStore  # noqa: E402

ISSUER = "http://mcp.localhost"
REDIRECT_URI = "http://127.0.0.1:9/callback"
VERIFIER = "a" * 64


class TokenEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # F4: authlib's is_secure_transport accepts http only for literal
        # "localhost" or a loopback IP, so the documented dev origin
        # http://mcp.localhost is refused without this. Read via os.getenv on
        # every call, so it can be toggled per test.
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

    def setUp(self) -> None:
        self.store = StubStore()
        self.store.add_client(
            Client(
                client_id="mcp-client",
                name="Test MCP client",
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
        )
        self.httpd = EdgeServer(("127.0.0.1", 0), self.authorization)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    # -- helpers ---------------------------------------------------------

    def seed_code(self, code: str = "seeded-code", *, challenge: str | None = None) -> None:
        self.store.save_authorization_code(
            AuthorizationCode(
                code=code,
                client_id="mcp-client",
                redirect_uri=REDIRECT_URI,
                scope="mcp:connect inspect",
                subject="oauth:grant-1",
                code_challenge=(
                    challenge if challenge is not None else create_s256_code_challenge(VERIFIER)
                ),
            )
        )

    def post(self, path: str, fields, *, content_type: str = "application/x-www-form-urlencoded"):
        body = fields if isinstance(fields, str) else urllib.parse.urlencode(fields)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            headers = {"Content-Length": str(len(body.encode()))}
            if content_type is not None:
                headers["Content-Type"] = content_type
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        try:
            return response.status, json.loads(raw)
        except json.JSONDecodeError:
            return response.status, {"raw": raw.decode("utf-8", "replace")}

    def token_request(self, **overrides):
        fields = {
            "grant_type": "authorization_code",
            "code": "seeded-code",
            "redirect_uri": REDIRECT_URI,
            "client_id": "mcp-client",
            "code_verifier": VERIFIER,
        }
        fields.update(overrides)
        return self.post("/oauth/token", {k: v for k, v in fields.items() if v is not None})

    # -- the headline evidence -------------------------------------------

    def test_authorization_code_exchange_issues_token_a(self) -> None:
        self.seed_code()
        status, body = self.token_request()
        self.assertEqual(200, status, body)
        self.assertEqual("Bearer", body["token_type"])
        self.assertEqual(900, body["expires_in"])
        self.assertEqual("mcp:connect inspect", body["scope"])
        self.assertTrue(body["access_token"].startswith("mapp_a_"))
        # P6 approves refresh for the code flow but the client does not hold the
        # grant type, and authlib decides by asking check_grant_type, so the
        # absence here is structural rather than a special case.
        self.assertNotIn("refresh_token", body)
        self.assertEqual(1, self.store.token_count())

    def test_no_refresh_token_is_issued_while_it_could_not_be_redeemed(self) -> None:
        """A client holding the grant type still gets none, and that is correct.

        Nothing persists a refresh token and no RefreshTokenGrant is
        registered, so a minted `mapp_r_` value is inert. This test previously
        asserted its presence as though it were a working capability.
        """
        self.store.add_client(
            Client(
                client_id="refresh-client",
                name="Client holding the refresh grant",
                redirect_uris=(REDIRECT_URI,),
                scopes=("inspect",),
                token_endpoint_auth_method="none",
                grant_types=("authorization_code", "refresh_token"),
            )
        )
        self.store.save_authorization_code(
            AuthorizationCode(
                code="refresh-code",
                client_id="refresh-client",
                redirect_uri=REDIRECT_URI,
                scope="inspect",
                subject="oauth:grant-2",
                code_challenge=create_s256_code_challenge(VERIFIER),
            )
        )
        status, body = self.token_request(code="refresh-code", client_id="refresh-client")
        self.assertEqual(200, status, body)
        self.assertNotIn("refresh_token", body)

        # The reason it must not be issued: redeeming one is unsupported.
        status, body = self.post(
            "/oauth/token",
            {"grant_type": "refresh_token", "refresh_token": "mapp_r_anything",
             "client_id": "refresh-client"},
        )
        self.assertEqual(400, status)
        self.assertEqual("unsupported_grant_type", body["error"])

    def test_the_client_grant_types_still_control_the_generator(self) -> None:
        """Both directions, against the generator itself.

        Asserting only the absence lets the generator pass for the wrong
        reason: one that never mints a refresh token looks identical to a
        client that is not allowed one. With issuance enabled, the flag authlib
        derives from the client's grant types must still be what decides.
        """
        import issuer as issuer_module

        original = issuer_module.REFRESH_TOKENS_IMPLEMENTED
        issuer_module.REFRESH_TOKENS_IMPLEMENTED = True
        try:
            allowed = self.authorization._generate_token(
                "authorization_code", None, scope="inspect", include_refresh_token=True
            )
            refused = self.authorization._generate_token(
                "authorization_code", None, scope="inspect", include_refresh_token=False
            )
        finally:
            issuer_module.REFRESH_TOKENS_IMPLEMENTED = original
        self.assertTrue(allowed["refresh_token"].startswith("mapp_r_"))
        self.assertNotIn("refresh_token", refused)

    def test_only_the_hash_of_the_token_is_stored(self) -> None:
        self.seed_code()
        _, body = self.token_request()
        raw = body["access_token"]
        record = self.store.query_token(raw)
        self.assertIsNotNone(record)
        self.assertNotIn(raw, json.dumps(record.__dict__))

    def test_authorization_code_is_single_use(self) -> None:
        self.seed_code()
        first, _ = self.token_request()
        self.assertEqual(200, first)
        status, body = self.token_request()
        self.assertEqual(400, status)
        self.assertEqual("invalid_grant", body["error"])

    # -- PKCE ------------------------------------------------------------

    def test_missing_code_verifier_is_refused(self) -> None:
        self.seed_code()
        status, body = self.token_request(code_verifier=None)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])

    def test_wrong_code_verifier_is_refused(self) -> None:
        self.seed_code()
        status, body = self.token_request(code_verifier="b" * 64)
        self.assertEqual(400, status)
        self.assertEqual("invalid_grant", body["error"])

    def test_verifier_without_a_challenge_is_refused(self) -> None:
        # RFC 9700 section 4.8, enforced inside authlib.
        self.seed_code(challenge="")
        status, body = self.token_request()
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])

    # -- the form parser the platform lacked -----------------------------

    def test_json_body_is_refused_on_the_token_endpoint(self) -> None:
        self.seed_code()
        status, body = self.post(
            "/oauth/token",
            json.dumps({"grant_type": "authorization_code"}),
            content_type="application/json",
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])
        self.assertIn("x-www-form-urlencoded", body["error_description"])

    def test_missing_content_type_is_refused(self) -> None:
        self.seed_code()
        status, body = self.post("/oauth/token", {"grant_type": "authorization_code"}, content_type=None)
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])

    def test_a_bare_parameter_name_is_refused(self) -> None:
        self.seed_code()
        status, body = self.post("/oauth/token", "grant_type")
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])

    def test_unknown_endpoint_is_not_found(self) -> None:
        status, body = self.post("/internal/oauth/exchange", {"grant_type": "x"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])


class SecureTransportTests(unittest.TestCase):
    """F4, asserted rather than assumed."""

    class _Handler:
        command = "GET"
        path = "/oauth/authorize?client_id=x"
        headers: dict = {}

    def test_dev_origin_is_refused_without_the_escape_hatch(self) -> None:
        previous = os.environ.pop("AUTHLIB_INSECURE_TRANSPORT", None)
        try:
            from authlib.oauth2.rfc6749.errors import InsecureTransportError

            with self.assertRaises(InsecureTransportError):
                build_request(self._Handler(), issuer="http://mcp.localhost", read_body=False)
        finally:
            # Restore unconditionally: leaving the key unset when it had no
            # previous value would change process-global state for every later
            # test, which only stays harmless while the runner is sequential.
            if previous is None:
                os.environ.pop("AUTHLIB_INSECURE_TRANSPORT", None)
            else:
                os.environ["AUTHLIB_INSECURE_TRANSPORT"] = previous

    def test_dev_origin_is_accepted_with_it(self) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"
        request = build_request(self._Handler(), issuer="http://mcp.localhost", read_body=False)
        self.assertEqual("x", request.payload.client_id)


if __name__ == "__main__":
    unittest.main()
