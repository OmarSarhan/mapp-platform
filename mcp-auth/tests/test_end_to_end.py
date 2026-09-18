"""The whole flow, driven for real: consent to token A to token B.

Every other suite here constructs the state it needs. That is what let a
genuine defect survive sixty-three passing exchange tests: each seeded its own
subject token with the audience it wanted, while a token A issued by the
authorization-code flow carried the model's placeholder and could never be
exchanged at all. Nothing that builds its own fixture can catch that.

So this file seeds nothing. It registers clients, walks the browser flow,
spends the code, exchanges the result and spends token B -- and asserts on
what the components actually produced. It is deliberately slow and
deliberately end to end, because the bugs it exists to catch live precisely in
the joins between the pieces the other suites test in isolation.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from authlib.oauth2.rfc7636 import create_s256_code_challenge  # noqa: E402

import canonical  # noqa: E402
import exchange  # noqa: E402
import operations  # noqa: E402
import server as server_module  # noqa: E402
from issuer import MappAuthorizationServer  # noqa: E402
from models import Client  # noqa: E402
from passwords import password_hash  # noqa: E402
from server import ControlServer, EdgeServer  # noqa: E402
from stub_store import StubStore  # noqa: E402

ISSUER = "http://mcp.localhost"
MCP_RESOURCE = ISSUER + "/mcp"
CONFIG_RESOURCE = "http://config.localhost/api"
REDIRECT_URI = "http://127.0.0.1:9/callback"
VERIFIER = "a" * 64
PASSWORD = "correct horse battery staple"
BROKER_SECRET = "broker-secret"


class FullFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

    def static_admin_hash(self) -> str:
        """Overridden below to prove the store-backed path also works."""
        return password_hash(PASSWORD)

    def setUp(self) -> None:
        self.store = StubStore()
        self.store.add_client(
            Client(
                client_id="mcp-client",
                name="Claude Code",
                redirect_uris=(REDIRECT_URI,),
                scopes=server_module.SUPPORTED_SCOPES,
                token_endpoint_auth_method="none",
            )
        )
        self.store.add_client(
            Client(
                client_id="mapp-mcp-broker",
                name="Broker",
                redirect_uris=(),
                scopes=(),
                token_endpoint_auth_method="client_secret_basic",
                capabilities=("introspect", "exchange", "revoke", "redeem"),
                client_secret=BROKER_SECRET,
            )
        )
        self.authorization = MappAuthorizationServer(
            self.store,
            issuer=ISSUER,
            resource=MCP_RESOURCE,
            config_api_resource=CONFIG_RESOURCE,
            # The deployed vocabulary, not a convenient subset. Building its
            # own was how this file missed that four of the five allowlisted
            # operations named scopes the server would not issue.
            scopes_supported=server_module.SUPPORTED_SCOPES,
            admin_password_hash=self.static_admin_hash(),
            secure_cookies=False,
        )
        self.edge = EdgeServer(("127.0.0.1", 0), self.authorization)
        self.control = ControlServer(("127.0.0.1", 0), self.authorization)
        for server in (self.edge, self.control):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        for server in (self.edge, self.control):
            server.shutdown()
            server.server_close()

    # -- transport -------------------------------------------------------

    def _request(self, server, method, path, *, body=None, cookie=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
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
    def _hidden(page: str, name: str) -> str:
        marker = f'name="{name}" value="'
        start = page.index(marker) + len(marker)
        return page[start : page.index('"', start)]

    # -- the flow --------------------------------------------------------

    def authorize_url(self, scope: str = "apply") -> str:
        return "/oauth/authorize?" + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": "mcp-client",
                "redirect_uri": REDIRECT_URI,
                "scope": scope,
                "state": "client-state-123",
                "code_challenge": create_s256_code_challenge(VERIFIER),
                "code_challenge_method": "S256",
            }
        )

    def obtain_token_a(self, scope: str = "apply") -> str:
        """Walk the whole browser flow and spend the code. No shortcuts."""
        url = self.authorize_url(scope)
        status, headers, _ = self._request(self.edge, "GET", url)
        self.assertEqual(302, status)
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]

        _, _, page = self._request(self.edge, "GET", headers["Location"])
        status, headers, _ = self._request(
            self.edge,
            "POST",
            "/oauth/login",
            body={"rid": rid, "csrf": self._hidden(page, "csrf"), "password": PASSWORD},
        )
        self.assertEqual(302, status)
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        _, _, page = self._request(
            self.edge, "GET", url + f"&rid={rid}", cookie=cookie
        )
        status, headers, _ = self._request(
            self.edge,
            "POST",
            "/oauth/authorize",
            body={
                "rid": rid,
                "csrf": self._hidden(page, "csrf"),
                "decision": "allow",
            },
            cookie=cookie,
        )
        self.assertEqual(302, status)
        code = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["code"][0]

        status, _, body = self._request(
            self.edge,
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
        self.assertEqual(200, status, body)
        return json.loads(body)["access_token"]

    def exchange_for_token_b(
        self, token_a: str, *, scope: str = "apply", operation_id: str = "proposals.apply"
    ):
        operation = operations.OPERATIONS[operation_id]
        context = json.dumps(
            {
                "version": canonical.SCHEME,
                "operationId": operation.operation_id,
                "method": operation.method,
                "pathTemplate": operation.path_template,
                "requestDigest": canonical.digest({"proposalId": "p1"}),
            }
        )
        credentials = base64.b64encode(
            f"mapp-mcp-broker:{BROKER_SECRET}".encode()
        ).decode()
        return self._request(
            self.control,
            "POST",
            "/internal/oauth/exchange",
            body={
                "grant_type": exchange.GRANT_TYPE,
                "subject_token": token_a,
                "subject_token_type": exchange.ACCESS_TOKEN_TYPE,
                "resource": CONFIG_RESOURCE,
                "scope": scope,
                exchange.CONTEXT_PARAMETER: context,
            },
            headers={"Authorization": "Basic " + credentials},
        )

    # -- assertions ------------------------------------------------------

    def test_a_real_token_a_carries_the_configured_resource(self) -> None:
        """The defect this file exists for.

        save_token omitted the audience, so a real token A took the model's
        placeholder while the exchange compared against the configured MCP
        resource. Sixty-three exchange tests passed throughout, because each
        seeded its own subject token.
        """
        record = self.store.query_token(self.obtain_token_a())
        self.assertEqual(MCP_RESOURCE, record.audience)

    def test_each_consent_produces_its_own_grant(self) -> None:
        """P3: the actor is the grant, not the operator.

        Before this, `subject` was the operator session's "admin" for every
        consent by every client, so two grants were indistinguishable and
        revocation had nothing to act on. Two consents must now yield two
        grant records, each carrying what was actually consented to.
        """
        first = self.store.query_token(self.obtain_token_a()).subject
        second = self.store.query_token(self.obtain_token_a()).subject
        self.assertNotEqual(first, second)
        for grant_id in (first, second):
            with self.subTest(grant=grant_id):
                self.assertTrue(grant_id.startswith("oauth:"))
                grant = self.store.query_grant(grant_id)
                self.assertIsNotNone(grant)
                self.assertEqual("mcp-client", grant.client_id)
                self.assertEqual(("apply",), grant.scopes)

    def test_revoking_one_grant_leaves_the_other_alone(self) -> None:
        """Revocation is per consent, not per operator or per client."""
        first = self.store.query_token(token_a := self.obtain_token_a()).subject
        other_raw = self.obtain_token_a()
        self.assertTrue(self.store.revoke_grant(first))
        self.assertTrue(self.store.query_grant(first).is_revoked())
        second = self.store.query_token(other_raw).subject
        self.assertFalse(self.store.query_grant(second).is_revoked())

    def test_a_real_token_a_can_be_exchanged_for_token_b(self) -> None:
        status, _, body = self.exchange_for_token_b(self.obtain_token_a())
        self.assertEqual(200, status, body)
        token_b = json.loads(body)
        self.assertTrue(token_b["access_token"].startswith(exchange.TOKEN_B_PREFIX))
        self.assertEqual(CONFIG_RESOURCE, self.store.query_token(
            token_b["access_token"]
        ).audience)

    def test_token_b_is_bound_and_spendable_once(self) -> None:
        status, _, body = self.exchange_for_token_b(self.obtain_token_a())
        self.assertEqual(200, status, body)
        raw = json.loads(body)["access_token"]
        digest = canonical.digest({"proposalId": "p1"})
        self.assertIsNotNone(
            self.store.consume_exchanged_token(raw, "proposals.apply", digest)
        )
        self.assertIsNone(
            self.store.consume_exchanged_token(raw, "proposals.apply", digest)
        )

    def test_token_b_cannot_be_exchanged_again(self) -> None:
        _, _, body = self.exchange_for_token_b(self.obtain_token_a())
        token_b = json.loads(body)["access_token"]
        status, _, refused = self.exchange_for_token_b(token_b)
        self.assertEqual(400, status)
        self.assertEqual("invalid_grant", json.loads(refused)["error"])

    def test_a_scope_beyond_the_consent_is_refused(self) -> None:
        """The consent was for `apply`; asking for more must not widen it."""
        token_a = self.obtain_token_a(scope="apply")
        status, _, body = self.exchange_for_token_b(token_a, scope="mcp:connect")
        self.assertEqual(400, status)
        self.assertEqual("invalid_scope", json.loads(body)["error"])

    def test_the_exchange_is_not_reachable_from_the_edge(self) -> None:
        """Structural, not a Caddy rule: the edge table has no such route."""
        status, _, _ = self._request(
            self.edge, "POST", "/internal/oauth/exchange", body={"grant_type": "x"}
        )
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()


class EveryAllowlistedOperationTests(FullFlowTests):
    """Each allowlisted operation, walked from consent to token B.

    The defect that motivated this: `proposals.apply` was the only operation
    this file ever exercised, and it was the only one that worked. The other
    four required scopes the authorization server refused to issue, so the
    browser flow died at `/oauth/authorize` with `invalid_scope` long before
    the exchange -- in the deployed configuration, not in a fixture.
    """

    def test_every_operation_can_be_consented_to_and_exchanged(self) -> None:
        for name, operation in operations.OPERATIONS.items():
            with self.subTest(operation=name):
                scope = " ".join(operation.required_scopes)
                token_a = self.obtain_token_a(scope=scope)
                status, _, body = self.exchange_for_token_b(
                    token_a, scope=scope, operation_id=name
                )
                self.assertEqual(200, status, f"{name}: {body}")
                self.assertTrue(
                    json.loads(body)["access_token"].startswith(
                        exchange.TOKEN_B_PREFIX
                    )
                )


class StoreSuppliedCredentialTests(FullFlowTests):
    """The whole flow again, with the credential coming from the store.

    This is how the component is actually deployed. The consent screen used to
    check MCP_AUTH_ADMIN_PASSWORD_HASH, which nothing in the repository set and
    which .env.example did not carry, while ./bin/mapp init wrote the operator
    credential into control.admin_credential -- a table this component never
    read. Every test passed because every test injected a hash directly.

    Inheriting the whole flow is the point: if the store-backed credential does
    not work, sign-in fails and every inherited test fails with it.
    """

    def static_admin_hash(self) -> str:
        self.store.set_admin_password_hash(password_hash(PASSWORD))
        return ""

    def test_the_hash_comes_from_the_store_and_not_the_constructor(self) -> None:
        self.assertEqual("", self.authorization._admin_password_hash)
        self.assertTrue(self.authorization.admin_password_hash)

    def test_a_credential_changed_in_the_store_takes_effect_at_once(self) -> None:
        """Read per attempt, not captured at start-up."""
        self.store.set_admin_password_hash(password_hash("a different secret"))
        url = self.authorize_url()
        status, headers, _ = self._request(self.edge, "GET", url)
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]
        _, _, page = self._request(self.edge, "GET", headers["Location"])
        status, _, _ = self._request(
            self.edge, "POST", "/oauth/login",
            body={"rid": rid, "csrf": self._hidden(page, "csrf"),
                  "password": PASSWORD},
        )
        self.assertEqual(401, status)
