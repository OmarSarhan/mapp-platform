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

# Imported at module scope because the fixtures below read its shared
# table order; a method-local import left it invisible to setUp.
try:
    import control_schema as cs  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - covered by the skip
    cs = None  # type: ignore[assignment]

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
            for table in cs.TABLES_IN_DELETE_ORDER:
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
        # The registration command grants refresh_token as well as
        # authorization_code, so a real operator-registered client is handed
        # one here. Kept on the instance rather than returned so the many
        # callers that only want token A are unaffected.
        self.issued = token
        self.assertTrue(token["refresh_token"].startswith("mapp_r_"))
        return token["access_token"]

    def refresh(self, refresh_token: str):
        return self.request(
            self.edge,
            "POST",
            "/oauth/token",
            body={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            },
        )

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

    # -- P6's refresh family ---------------------------------------------
    #
    # The storage layer had no caller outside its own tests: a family could be
    # opened and rotated by a test, and nothing in the running component ever
    # did either. These drive it through /oauth/token instead.

    def test_a_refresh_buys_a_new_token_a_and_a_new_refresh_token(self) -> None:
        first = self.obtain_token_a()
        original = self.issued["refresh_token"]

        status, _, body = self.refresh(original)
        self.assertEqual(200, status, body)
        refreshed = json.loads(body)
        self.assertTrue(refreshed["access_token"].startswith("mapp_a_"))
        self.assertNotEqual(first, refreshed["access_token"])
        # Rotation: the successor is a different value, and it is not the one
        # the generator would have minted -- it is the one the store wrote.
        self.assertNotEqual(original, refreshed["refresh_token"])
        self.assertIsNotNone(
            self.store.refresh_token_state(refreshed["refresh_token"]),
            "the successor handed to the client must be the persisted one",
        )

    def test_the_refreshed_token_a_carries_the_same_grant_and_audience(self) -> None:
        """Otherwise refresh would be a way to acquire authority sideways."""
        original_a = self.obtain_token_a()
        before = self.store.query_token(original_a)
        status, _, body = self.refresh(self.issued["refresh_token"])
        self.assertEqual(200, status, body)
        after = self.store.query_token(json.loads(body)["access_token"])
        self.assertEqual(before.subject, after.subject)
        self.assertEqual(before.scope, after.scope)
        self.assertEqual(MCP_RESOURCE, after.audience)
        self.assertEqual(self.client_id, after.client_id)

    def test_a_retry_just_after_a_refresh_keeps_the_consent(self) -> None:
        """What an operator meets in practice, over the real endpoint.

        The response is two socket writes -- headers, then body -- and the
        rotation commits before either, so a dropped connection leaves the
        client holding a token the server has already spent. Without the retry
        window that costs the operator an interactive sign-in; here the retry
        is answered with a working token and the consent survives.
        """
        token_a = self.obtain_token_a()
        original = self.issued["refresh_token"]
        grant_id = self.store.query_token(token_a).subject

        status, _, body = self.refresh(original)
        self.assertEqual(200, status, body)
        first = json.loads(body)["refresh_token"]

        status, _, body = self.refresh(original)
        self.assertEqual(200, status, body)
        second = json.loads(body)["refresh_token"]

        self.assertNotEqual(first, second)
        self.assertFalse(self.store.query_grant(grant_id).is_revoked())
        # The one the client never received is superseded, so the family still
        # holds exactly one live token.
        self.assertIsNotNone(self.store.refresh_token_state(first)["consumed_at"])
        self.assertIsNone(self.store.refresh_token_state(second)["consumed_at"])

    def test_the_spent_refresh_token_is_replay_and_costs_the_grant(self) -> None:
        """Section 4, and the price O7 records.

        Detection and consequence are one transaction in the store, so what
        this asserts at the endpoint is that the endpoint reaches it.

        The window is closed on this store instance because the two requests
        are a fraction of a second apart and would otherwise be read as the
        retry they resemble. Closing it is what a deployment choosing strict
        OAuth 2.1 behaviour does, so this is also that configuration's test.
        """
        self.store.REFRESH_GRACE_SECONDS = 0
        token_a = self.obtain_token_a()
        original = self.issued["refresh_token"]
        grant_id = self.store.query_token(token_a).subject

        status, _, body = self.refresh(original)
        self.assertEqual(200, status, body)
        successor = json.loads(body)["refresh_token"]

        status, _, body = self.refresh(original)
        self.assertEqual(400, status)
        self.assertEqual("invalid_grant", json.loads(body)["error"])

        grant = self.store.query_grant(grant_id)
        self.assertTrue(grant.is_revoked(), "a replay must revoke the grant")
        # And the successor dies with the family, so the legitimate holder is
        # not left refreshing against a revoked consent.
        status, _, body = self.refresh(successor)
        self.assertEqual(400, status)
        self.assertEqual("invalid_grant", json.loads(body)["error"])

    def test_revoking_the_grant_stops_the_refresh(self) -> None:
        """The grant is the unit of revocation, so it has to reach this too."""
        token_a = self.obtain_token_a()
        grant_id = self.store.query_token(token_a).subject
        self.assertTrue(self.store.revoke_grant(grant_id, "operator"))
        status, _, body = self.refresh(self.issued["refresh_token"])
        self.assertEqual(400, status)
        self.assertEqual("invalid_grant", json.loads(body)["error"])

    def test_a_refresh_cannot_widen_the_scope(self) -> None:
        """RFC 6749 s6, and the same rule the exchange follows.

        The refused request still spends the presented token -- rotation
        happens where the race is decided, before authlib checks the scope --
        so this also pins that the client is refused rather than quietly
        given what it asked for.
        """
        self.obtain_token_a(scope="apply")
        status, _, body = self.request(
            self.edge,
            "POST",
            "/oauth/token",
            body={
                "grant_type": "refresh_token",
                "refresh_token": self.issued["refresh_token"],
                "client_id": self.client_id,
                "scope": "apply inspect",
            },
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_scope", json.loads(body)["error"])

    def test_a_refresh_may_narrow_the_scope(self) -> None:
        self.obtain_token_a(scope="inspect apply")
        status, _, body = self.request(
            self.edge,
            "POST",
            "/oauth/token",
            body={
                "grant_type": "refresh_token",
                "refresh_token": self.issued["refresh_token"],
                "client_id": self.client_id,
                "scope": "inspect",
            },
        )
        self.assertEqual(200, status, body)
        refreshed = json.loads(body)
        self.assertEqual("inspect", refreshed["scope"])
        self.assertEqual("inspect", self.store.query_token(
            refreshed["access_token"]
        ).scope)

    def test_offline_access_never_becomes_a_granted_scope(self) -> None:
        """Refresh is decided by the client's grant types, not by a scope.

        MCP clients commonly ask for `offline_access` out of habit. It is not
        in the operation-derived vocabulary and no client is registered for
        it, so ``get_allowed_scope`` drops it: the grant, the token and the
        response's ``scope`` all carry only what was registered, and RFC 6749
        s5.1 makes that member the client's notice that its request was
        narrowed. Asked for alone it is refused outright, because a narrowing
        to nothing is not a narrowing.

        What must never happen is the other reading -- that the presence of
        the string is what turns refresh on. Refresh arrives here without it.
        """
        token_a = self.obtain_token_a(scope="apply offline_access")
        self.assertEqual("apply", self.issued["scope"])
        self.assertTrue(self.issued["refresh_token"].startswith("mapp_r_"))
        self.assertEqual(("apply",), self.store.query_grant(
            self.store.query_token(token_a).subject
        ).scopes)

        status, headers, _ = self.request(
            self.edge, "GET", self.authorize_url("offline_access")
        )
        self.assertEqual(302, status)
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )
        self.assertEqual(["invalid_scope"], query["error"])

    def test_another_client_cannot_spend_this_grants_refresh_token(self) -> None:
        """A refresh token is a bearer credential, so the family says whose.

        The presenting client is compared against the family's, not against
        anything in the request. The presentation still spends the token --
        rotation is where the race is decided -- which is the right direction
        for a credential that has demonstrably left its owner.
        """
        self.obtain_token_a()
        stolen = self.issued["refresh_token"]
        other = self.control.register_oauth_client(
            name="Another agent",
            redirect_uris=["http://127.0.0.1:33419/callback"],
            scopes=["apply"],
        )
        status, _, body = self.request(
            self.edge,
            "POST",
            "/oauth/token",
            body={
                "grant_type": "refresh_token",
                "refresh_token": stolen,
                "client_id": other,
            },
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_grant", json.loads(body)["error"])

    def test_a_refresh_family_belongs_to_one_grant(self) -> None:
        """Two consents are two families, so revoking one cannot free the other."""
        self.obtain_token_a()
        first = self.issued["refresh_token"]
        first_family = self.store.refresh_token_state(first)["family_id"]
        self.obtain_token_a()
        second = self.issued["refresh_token"]
        second_family = self.store.refresh_token_state(second)["family_id"]
        self.assertNotEqual(first_family, second_family)
        self.assertNotEqual(
            self.store.query_refresh_family(first_family)["grant_id"],
            self.store.query_refresh_family(second_family)["grant_id"],
        )


if __name__ == "__main__":
    unittest.main()
