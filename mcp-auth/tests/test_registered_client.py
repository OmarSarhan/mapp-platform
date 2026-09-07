"""The flow an operator can actually reach, end to end.

Every other suite registers its own client by constructing a ``Client`` and
handing it to the store. That proves the flow works; it cannot prove anyone can
get to it, and for most of Phase 0 nobody could -- nothing registered a client,
so a correctly deployed component refused every authorization request as an
unknown client while the whole suite stayed green.

So this file registers a client the way an operator does, through
``ControlStore.register_oauth_client`` -- the same call the
``mcp-client-register`` command makes -- and then walks discovery,
authorization, consent, the token endpoint, introspection, the exchange and
redemption against the real component over real HTTP with the real SQL store.

It is the target-client spike for the authorization column of P2's matrix, for
one client. The other two ecosystems need their own.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import sys
import threading
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config-ui"))

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - exercised by the skip below
    psycopg = None

from authlib.oauth2.rfc7636 import create_s256_code_challenge  # noqa: E402

import canonical  # noqa: E402
import exchange  # noqa: E402
import operations  # noqa: E402
from issuer import MappAuthorizationServer  # noqa: E402
from models import Client  # noqa: E402
from server import ControlServer, EdgeServer  # noqa: E402

DATABASE_URL = os.getenv("CONTROL_TEST_DATABASE_URL", "")
ISSUER = "http://mcp.localhost"
MCP_RESOURCE = ISSUER + "/mcp"
CONFIG_RESOURCE = "http://config.localhost/api"
REDIRECT_URI = "http://127.0.0.1:33418/callback"
VERIFIER = "b" * 64
PASSWORD = "correct horse battery staple"
BROKER_SECRET = "broker-secret"

requires_database = unittest.skipUnless(
    DATABASE_URL and psycopg is not None,
    "set CONTROL_TEST_DATABASE_URL to a scratch PostgreSQL database",
)


@requires_database
class RegisteredClientFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"
        import control_schema as cs

        connection = cs.connect(DATABASE_URL)
        try:
            cs.migrate(connection)
        finally:
            connection.close()

    def setUp(self) -> None:
        import control_plane
        from sql_store import SqlStore

        os.environ["CONTROL_DATABASE_URL"] = DATABASE_URL
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            for table in (
                "oauth_authorization_codes",
                "oauth_pending_authorizations",
                "oauth_tokens",
                "oauth_sessions",
                "oauth_grants",
                "oauth_clients",
            ):
                connection.execute(f"DELETE FROM control.{table}")

        # The operator's half. ControlStore needs a root for the file-backed
        # state it still owns; nothing here touches it.
        self.root = Path(
            os.environ.get("TMPDIR", "/tmp")
        ) / f"mcp-client-spike-{os.getpid()}"
        self.control = control_plane.ControlStore(self.root)
        if not self.control.initialize(PASSWORD):
            self.control.reset_password(PASSWORD)
        self.client_id = self.control.register_oauth_client(
            name="Claude Code",
            redirect_uris=[REDIRECT_URI],
            scopes=["mcp:connect", "inspect", "apply"],
        )

        self.store = SqlStore(DATABASE_URL)
        # The broker is confidential and is not an agent, so it is registered
        # the way the configuration API's own client is rather than through the
        # agent command -- which deliberately refuses to issue a secret.
        self.store.add_client(
            Client(
                client_id="mapp-mcp-broker",
                name="Broker",
                redirect_uris=(),
                scopes=(),
                token_endpoint_auth_method="client_secret_basic",
                client_secret=BROKER_SECRET,
            )
        )
        self.authorization = MappAuthorizationServer(
            self.store,
            issuer=ISSUER,
            resource=MCP_RESOURCE,
            config_api_resource=CONFIG_RESOURCE,
            secure_cookies=False,
        )
        self.edge = EdgeServer(("127.0.0.1", 0), self.authorization)
        self.internal = ControlServer(("127.0.0.1", 0), self.authorization)
        for server in (self.edge, self.internal):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        for server in (self.edge, self.internal):
            server.shutdown()
            server.server_close()
        os.environ.pop("CONTROL_DATABASE_URL", None)

    # -- transport -------------------------------------------------------

    def request(self, server, method, path, *, body=None, cookie=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=10
        )
        try:
            sent = dict(headers or {})
            payload = None
            if body is not None:
                payload = urllib.parse.urlencode(body)
                sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
                sent.setdefault("Content-Length", str(len(payload.encode())))
            if cookie:
                sent["Cookie"] = cookie
            connection.request(method, path, body=payload, headers=sent)
            response = connection.getresponse()
            return (
                response.status,
                dict(response.getheaders()),
                response.read().decode("utf-8", "replace"),
            )
        finally:
            connection.close()

    @staticmethod
    def hidden(page: str, name: str) -> str:
        marker = f'name="{name}" value="'
        start = page.index(marker) + len(marker)
        return page[start : page.index('"', start)]

    # -- the spike -------------------------------------------------------

    def test_discovery_advertises_only_the_safe_scopes(self) -> None:
        """A client reads this before it asks for anything.

        P2: metadata carries only the safe discovery scopes so a greedy client
        cannot auto-request every permission it sees.
        """
        status, _, body = self.request(
            self.edge, "GET", "/.well-known/oauth-authorization-server"
        )
        self.assertEqual(200, status)
        metadata = json.loads(body)
        self.assertEqual(ISSUER, metadata["issuer"])
        self.assertEqual(["code"], metadata["response_types_supported"])
        self.assertIn("S256", metadata["code_challenge_methods_supported"])
        self.assertNotIn("apply", metadata["scopes_supported"])

    def authorize_url(self, scope: str) -> str:
        return "/oauth/authorize?" + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": REDIRECT_URI,
                "scope": scope,
                "state": "spike-state",
                "code_challenge": create_s256_code_challenge(VERIFIER),
                "code_challenge_method": "S256",
            }
        )

    def obtain_token_a(self, scope: str = "apply") -> str:
        url = self.authorize_url(scope)
        status, headers, _ = self.request(self.edge, "GET", url)
        self.assertEqual(302, status)
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]

        _, _, page = self.request(self.edge, "GET", headers["Location"])
        status, headers, _ = self.request(
            self.edge,
            "POST",
            "/oauth/login",
            body={
                "rid": rid,
                "csrf": self.hidden(page, "csrf"),
                "password": PASSWORD,
            },
        )
        self.assertEqual(302, status, "the operator credential did not authenticate")
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        _, _, page = self.request(self.edge, "GET", url + f"&rid={rid}", cookie=cookie)
        status, headers, _ = self.request(
            self.edge,
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": self.hidden(page, "csrf"), "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(302, status)
        location = urllib.parse.urlsplit(headers["Location"])
        self.assertTrue(
            headers["Location"].startswith(REDIRECT_URI),
            f"consent redirected somewhere unregistered: {headers['Location']}",
        )
        query = urllib.parse.parse_qs(location.query)
        self.assertEqual(["spike-state"], query["state"])
        self.assertEqual([ISSUER], query["iss"], "RFC 9207 iss is required")

        status, _, body = self.request(
            self.edge,
            "POST",
            "/oauth/token",
            body={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": REDIRECT_URI,
                "client_id": self.client_id,
                "code_verifier": VERIFIER,
            },
        )
        self.assertEqual(200, status, body)
        token = json.loads(body)
        self.assertEqual("Bearer", token["token_type"])
        self.assertEqual(900, token["expires_in"])
        self.assertNotIn("refresh_token", token)
        return token["access_token"]

    def test_a_registered_client_completes_the_authorization_flow(self) -> None:
        """The claim that was false for most of Phase 0.

        Nothing registered a client, so this flow was unreachable however
        green the suite was. The client here comes from the operator command.
        """
        token_a = self.obtain_token_a()
        self.assertTrue(token_a.startswith("mapp_a_"))
        record = self.store.query_token(token_a)
        self.assertEqual(MCP_RESOURCE, record.audience)
        self.assertTrue(record.subject.startswith("oauth:"))

    def test_the_grant_records_the_registered_client(self) -> None:
        record = self.store.query_token(self.obtain_token_a())
        grant = self.store.query_grant(record.subject)
        self.assertEqual(self.client_id, grant.client_id)
        self.assertEqual(("apply",), grant.scopes)

    def test_a_scope_the_client_was_not_registered_for_is_refused(self) -> None:
        """The registered scope list is a ceiling, checked before consent."""
        status, headers, _ = self.request(
            self.edge, "GET", self.authorize_url("federation:provision")
        )
        self.assertEqual(302, status)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )
        self.assertEqual(["invalid_scope"], query["error"])
        self.assertEqual([ISSUER], query["iss"], "iss is required on errors too")

    def test_introspection_resolves_the_registered_client(self) -> None:
        token_a = self.obtain_token_a()
        credentials = base64.b64encode(
            f"mapp-mcp-broker:{BROKER_SECRET}".encode()
        ).decode()
        status, _, body = self.request(
            self.internal,
            "POST",
            "/internal/oauth/introspect",
            body={"token": token_a, "resource": MCP_RESOURCE},
            headers={"Authorization": "Basic " + credentials},
        )
        self.assertEqual(200, status)
        record = json.loads(body)
        self.assertTrue(record["active"])
        self.assertEqual(self.client_id, record["client_id"])
        self.assertEqual(MCP_RESOURCE, record["aud"])

    def test_the_exchange_and_redemption_work_for_a_registered_client(self) -> None:
        """Consent to token A to token B to spent, with nothing seeded."""
        token_a = self.obtain_token_a()
        operation = operations.OPERATIONS["proposals.apply"]
        digest = canonical.digest({"spike": True})
        context = json.dumps(
            {
                "version": canonical.SCHEME,
                "operationId": operation.operation_id,
                "method": operation.method,
                "pathTemplate": operation.path_template,
                "requestDigest": digest,
            }
        )
        credentials = base64.b64encode(
            f"mapp-mcp-broker:{BROKER_SECRET}".encode()
        ).decode()
        status, _, body = self.request(
            self.internal,
            "POST",
            "/internal/oauth/exchange",
            body={
                "grant_type": exchange.GRANT_TYPE,
                "subject_token": token_a,
                "subject_token_type": exchange.ACCESS_TOKEN_TYPE,
                "resource": CONFIG_RESOURCE,
                "scope": "apply",
                exchange.CONTEXT_PARAMETER: context,
            },
            headers={"Authorization": "Basic " + credentials},
        )
        self.assertEqual(200, status, body)
        token_b = json.loads(body)["access_token"]
        self.assertTrue(token_b.startswith(exchange.TOKEN_B_PREFIX))

        for expected in (200, 400):
            status, _, body = self.request(
                self.internal,
                "POST",
                "/internal/oauth/redeem",
                body={
                    "token": token_b,
                    "operation_id": "proposals.apply",
                    "request_digest": digest,
                },
                headers={"Authorization": "Basic " + credentials},
            )
            self.assertEqual(expected, status, body)
        self.assertEqual("invalid_grant", json.loads(body)["error"])

    def test_disabling_the_client_stops_it_authorizing(self) -> None:
        """The operator's other half: withdraw a client without touching grants."""
        self.obtain_token_a()
        self.assertTrue(self.control.disable_oauth_client(self.client_id))
        status, headers, _ = self.request(
            self.edge, "GET", self.authorize_url("apply")
        )
        # An unknown client cannot be redirected to: the redirect_uri is only
        # trustworthy once the client is resolved, so this must not be a 302.
        self.assertNotEqual(302, status)
        self.assertIsNone(headers.get("Location"))


if __name__ == "__main__":
    unittest.main()
