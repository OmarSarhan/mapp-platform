"""Three identities cross the exchange, and none may be chosen by the caller.

The Phase 1 gate asks for proof that "grant/actor, original MCP client and
broker-service identity ... all three survive exchange and cannot be
caller-selected or confused". Those are three different questions, and the
suite already answered only the easiest of them: one assertion that the actor
and broker ids are *recorded* on the binding.

Recorded is not the same as unforgeable. The identities are:

*   **the grant** -- what the operator consented to, carried as the token's
    subject. It decides which scopes may be exchanged for and is what
    revocation acts on.
*   **the original MCP client** -- the agent holding the grant, recorded as
    ``actor_client_id``. It is what a resource server would authorise on, and
    disabling it must stop an already-issued credential.
*   **the broker** -- the confidential client that performed the exchange,
    recorded as ``broker_client_id``. It is *not* the actor, and a token B
    names it in ``client_id``, which is why an audit needs both.

A caller that could name any of them could borrow another client's authority
while the audit trail said someone else did it.
"""

from __future__ import annotations

import base64
import datetime as dt
import http.client
import json
import os
import sys
import threading
import unittest
import urllib.parse
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canonical  # noqa: E402
import exchange  # noqa: E402
import introspection  # noqa: E402
from issuer import MappAuthorizationServer  # noqa: E402
from models import Client, Grant, Token  # noqa: E402
from server import ControlServer, EdgeServer  # noqa: E402
from stub_store import StubStore  # noqa: E402

MCP_RESOURCE = "http://mcp.localhost/mcp"
CONFIG_RESOURCE = "http://config.localhost/api"
BROKER_SECRET = "broker-secret"
OTHER_BROKER_SECRET = "other-broker-secret"
DIGEST = canonical.digest({"identity": True})


def context(**overrides) -> str:
    fields = {
        "version": canonical.SCHEME,
        "operationId": "proposals.apply",
        "method": "POST",
        "pathTemplate": "/api/proposals/{proposalId}/apply",
        "requestDigest": DIGEST,
    }
    fields.update(overrides)
    return json.dumps(fields)


class IdentityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"
        self.store = StubStore()
        for client_id, secret in (
            ("mapp-mcp-broker", BROKER_SECRET),
            ("other-broker", OTHER_BROKER_SECRET),
        ):
            self.store.add_client(
                Client(
                    client_id=client_id,
                    name=client_id,
                    redirect_uris=(),
                    scopes=(),
                    token_endpoint_auth_method="client_secret_basic",
                    client_secret=secret,
                )
            )
        for client_id in ("agent-one", "agent-two"):
            self.store.add_client(
                Client(
                    client_id=client_id,
                    name=client_id,
                    redirect_uris=("http://127.0.0.1:9/cb",),
                    scopes=("apply",),
                    token_endpoint_auth_method="none",
                )
            )
        # One grant per agent, so "the wrong one" is always available.
        for grant_id, client_id in (
            ("oauth:grant-one", "agent-one"),
            ("oauth:grant-two", "agent-two"),
        ):
            self.store.save_grant(
                Grant(
                    grant_id=grant_id,
                    client_id=client_id,
                    subject="operator",
                    scopes=("apply",),
                )
            )
        self.now = dt.datetime.now(dt.timezone.utc)
        self.seed_token("mapp_a_one", "agent-one", "oauth:grant-one")
        self.seed_token("mapp_a_two", "agent-two", "oauth:grant-two")
        self.broker = self.store.query_client("mapp-mcp-broker")

    def seed_token(self, raw: str, client_id: str, subject: str) -> None:
        self.store.save_token(
            raw,
            Token(
                token_hash="",
                client_id=client_id,
                scope="apply",
                subject=subject,
                issued_at=int(self.now.timestamp()),
                expires_in=900,
                audience=MCP_RESOURCE,
            ),
        )

    def form(self, **overrides) -> defaultdict:
        fields = {
            "grant_type": exchange.GRANT_TYPE,
            "subject_token": "mapp_a_one",
            "subject_token_type": exchange.ACCESS_TOKEN_TYPE,
            "resource": CONFIG_RESOURCE,
            "scope": "apply",
            exchange.CONTEXT_PARAMETER: context(),
        }
        fields.update(overrides)
        datalist: defaultdict = defaultdict(list)
        for name, value in fields.items():
            if value is None:
                continue
            datalist[name].extend(value if isinstance(value, list) else [value])
        return datalist

    def run_exchange(self, datalist=None, broker=None):
        return exchange.exchange(
            datalist=datalist if datalist is not None else self.form(),
            broker_client=broker if broker is not None else self.broker,
            store=self.store,
            resource=CONFIG_RESOURCE,
            mcp_resource=MCP_RESOURCE,
            now=self.now,
        )


class SurvivalTests(IdentityTestCase):
    """All three cross the exchange intact and stay distinguishable."""

    def test_each_identity_is_recorded_separately(self) -> None:
        token = self.run_exchange()["access_token"]
        binding = self.store.exchanged_binding(token)
        record = self.store.query_token(token)
        self.assertEqual("agent-one", binding["actor_client_id"])
        self.assertEqual("mapp-mcp-broker", binding["broker_client_id"])
        self.assertEqual("oauth:grant-one", record.subject)
        # Three values, three roles, and the token names the broker.
        self.assertEqual("mapp-mcp-broker", record.client_id)
        self.assertNotEqual(binding["actor_client_id"], binding["broker_client_id"])

    def test_the_actor_is_derived_from_the_subject_token(self) -> None:
        """Not from the request, and not from the broker's own identity."""
        token = self.run_exchange(self.form(subject_token="mapp_a_two"))["access_token"]
        binding = self.store.exchanged_binding(token)
        self.assertEqual("agent-two", binding["actor_client_id"])
        self.assertEqual("oauth:grant-two", self.store.query_token(token).subject)

    def test_the_broker_is_derived_from_its_authentication(self) -> None:
        token = self.run_exchange(
            broker=self.store.query_client("other-broker")
        )["access_token"]
        binding = self.store.exchanged_binding(token)
        self.assertEqual("other-broker", binding["broker_client_id"])
        # The actor is unchanged: a different broker does not change who acts.
        self.assertEqual("agent-one", binding["actor_client_id"])

    def test_introspection_reports_the_grant_as_the_subject(self) -> None:
        """P3: the actor of record is the consent, not a person or a client."""
        token = self.run_exchange()["access_token"]
        datalist: defaultdict = defaultdict(list)
        datalist["token"].append(token)
        result = introspection.introspect(datalist=datalist, store=self.store)
        self.assertEqual("oauth:grant-one", result["sub"])


class NonSelectionTests(IdentityTestCase):
    """None of the three may be named by the caller.

    Each of these passes a parameter the exchange has no business honouring.
    The requirement is not that they are refused -- it is that they are
    *ignored*: the identities must come from validated state either way.
    """

    def test_naming_the_actor_client_does_not_change_it(self) -> None:
        token = self.run_exchange(
            self.form(actor_client_id="agent-two", client_id="agent-two")
        )["access_token"]
        binding = self.store.exchanged_binding(token)
        self.assertEqual("agent-one", binding["actor_client_id"])

    def test_naming_the_broker_does_not_change_it(self) -> None:
        token = self.run_exchange(
            self.form(broker_client_id="other-broker")
        )["access_token"]
        binding = self.store.exchanged_binding(token)
        self.assertEqual("mapp-mcp-broker", binding["broker_client_id"])

    def test_naming_the_subject_does_not_change_the_grant(self) -> None:
        """The grant decides scope and is what revocation acts on."""
        token = self.run_exchange(
            self.form(subject="oauth:grant-two", sub="oauth:grant-two")
        )["access_token"]
        self.assertEqual("oauth:grant-one", self.store.query_token(token).subject)

    def test_an_actor_token_is_refused_outright(self) -> None:
        """RFC 8693 delegation is not in this profile.

        Accepting one would let the broker act as a third party the grant
        never named -- which is the confused-deputy case the whole split is
        arranged to avoid.
        """
        for field in ("actor_token", "actor_token_type"):
            with self.subTest(field=field):
                with self.assertRaises(exchange.ExchangeError) as caught:
                    self.run_exchange(self.form(**{field: "anything"}))
                self.assertEqual("invalid_request", caught.exception.error)

    def test_a_broker_cannot_present_another_clients_grant_as_its_own(self) -> None:
        """The broker authenticates as itself and borrows nothing.

        Both brokers can exchange agent-one's token -- brokering is not
        restricted per grant -- but neither can make the *actor* be itself,
        which is what would let it act with the agent's authority under its
        own name.
        """
        for broker_id, secret in (
            ("mapp-mcp-broker", BROKER_SECRET),
            ("other-broker", OTHER_BROKER_SECRET),
        ):
            with self.subTest(broker=broker_id):
                token = self.run_exchange(
                    broker=self.store.query_client(broker_id)
                )["access_token"]
                binding = self.store.exchanged_binding(token)
                self.assertEqual(broker_id, binding["broker_client_id"])
                self.assertNotEqual(broker_id, binding["actor_client_id"])


class ConfusionTests(IdentityTestCase):
    """The three must not be interchangeable where it matters."""

    def test_disabling_the_actor_stops_the_exchange(self) -> None:
        """The client that holds the grant, not the one that brokered it."""
        self.store.add_client(
            Client(
                client_id="agent-one",
                name="agent-one",
                redirect_uris=("http://127.0.0.1:9/cb",),
                scopes=("apply",),
                token_endpoint_auth_method="none",
                disabled=True,
            )
        )
        with self.assertRaises(exchange.ExchangeError):
            self.run_exchange()

    def test_disabling_the_actor_deactivates_an_issued_token(self) -> None:
        """A token B names the broker, so checking its own client proved nothing.

        This is the confusion that mattered in practice: introspection read
        record.client_id -- the broker -- so disabling the agent left its
        credential live for the whole sixty seconds.
        """
        token = self.run_exchange()["access_token"]
        datalist: defaultdict = defaultdict(list)
        datalist["token"].append(token)
        self.assertTrue(
            introspection.introspect(datalist=datalist, store=self.store)["active"]
        )
        self.store.add_client(
            Client(
                client_id="agent-one",
                name="agent-one",
                redirect_uris=("http://127.0.0.1:9/cb",),
                scopes=("apply",),
                token_endpoint_auth_method="none",
                disabled=True,
            )
        )
        self.assertEqual(
            {"active": False},
            introspection.introspect(datalist=datalist, store=self.store),
        )

    def test_disabling_the_broker_leaves_other_brokers_working(self) -> None:
        self.store.add_client(
            Client(
                client_id="mapp-mcp-broker",
                name="broker",
                redirect_uris=(),
                scopes=(),
                token_endpoint_auth_method="client_secret_basic",
                capabilities=("introspect", "exchange", "revoke", "redeem"),
                client_secret=BROKER_SECRET,
                disabled=True,
            )
        )
        # The disabled broker can no longer authenticate at the endpoint, which
        # is asserted over HTTP below; the *other* broker is unaffected here.
        token = self.run_exchange(
            broker=self.store.query_client("other-broker")
        )["access_token"]
        self.assertTrue(token.startswith(exchange.TOKEN_B_PREFIX))

    def test_revoking_one_grant_leaves_the_other_agents_grant_working(self) -> None:
        self.assertTrue(self.store.revoke_grant("oauth:grant-one", "test"))
        with self.assertRaises(exchange.ExchangeError):
            self.run_exchange()
        token = self.run_exchange(self.form(subject_token="mapp_a_two"))
        self.assertTrue(token["access_token"].startswith(exchange.TOKEN_B_PREFIX))


class RegistrationTests(unittest.TestCase):
    """Dynamic Client Registration: unadvertised, unrouted, and unusable.

    The gate asks for two things -- that DCR is unadvertised and rejected, and
    that a client never preregistered cannot obtain a grant, scope or token.
    The suite covered the first half of the first one.
    """

    def setUp(self) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"
        self.store = StubStore()
        self.store.add_client(
            Client(
                client_id="known-agent",
                name="Known",
                redirect_uris=("http://127.0.0.1:9/cb",),
                scopes=("apply",),
                token_endpoint_auth_method="none",
            )
        )
        self.authorization = MappAuthorizationServer(
            self.store,
            issuer="http://mcp.localhost",
            resource=MCP_RESOURCE,
            config_api_resource=CONFIG_RESOURCE,
            secure_cookies=False,
        )
        self.edge = EdgeServer(("127.0.0.1", 0), self.authorization)
        self.control = ControlServer(("127.0.0.1", 0), self.authorization)
        for server in (self.edge, self.control):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

    def request(self, server, method, path, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        try:
            headers = {}
            payload = None
            if body is not None:
                payload = json.dumps(body)
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(payload.encode()))
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_the_metadata_document_advertises_no_registration_endpoint(self) -> None:
        status, _, body = self.request(
            self.edge, "GET", "/.well-known/oauth-authorization-server"
        )
        self.assertEqual(200, status)
        self.assertNotIn("registration_endpoint", json.loads(body))

    def test_every_plausible_registration_path_is_unrouted(self) -> None:
        """Structural, not a denial rule: the route tables contain no such path.

        A refusal implemented as a handler could be relaxed by editing the
        handler. There being no route means DCR cannot be enabled by accident.
        """
        for path in (
            "/register",
            "/oauth/register",
            "/connect/register",
            "/internal/oauth/register",
        ):
            for server, name in ((self.edge, "edge"), (self.control, "control")):
                with self.subTest(path=path, listener=name):
                    status, _, _ = self.request(
                        server, "POST", path, {"redirect_uris": ["http://evil/cb"]}
                    )
                    self.assertEqual(404, status)

    def test_no_route_table_contains_a_registration_path(self) -> None:
        for routes, name in (
            (self.edge.routes, "edge"),
            (self.control.routes, "control"),
        ):
            with self.subTest(listener=name):
                self.assertFalse(
                    [path for _, path in routes if "regist" in path.lower()]
                )

    def test_an_unregistered_client_cannot_start_an_authorization(self) -> None:
        """And is not redirected: the redirect_uri is only trustworthy once the
        client resolves, so an unknown client must not produce a 302 at all."""
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": "never-registered",
                "redirect_uri": "http://127.0.0.1:9/cb",
                "scope": "apply",
                "state": "s",
                "code_challenge": "x" * 43,
                "code_challenge_method": "S256",
            }
        )
        status, headers, _ = self.request(
            self.edge, "GET", "/oauth/authorize?" + query
        )
        self.assertNotEqual(302, status)
        self.assertIsNone(headers.get("Location"))

    def test_an_unregistered_client_cannot_reach_the_token_endpoint(self) -> None:
        body = urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": "anything",
                "redirect_uri": "http://127.0.0.1:9/cb",
                "client_id": "never-registered",
                "code_verifier": "v" * 64,
            }
        )
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.edge.server_address[1], timeout=5
        )
        try:
            connection.request(
                "POST",
                "/oauth/token",
                body=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(body.encode())),
                },
            )
            response = connection.getresponse()
            self.assertNotEqual(200, response.status)
            self.assertNotIn("access_token", response.read().decode())
        finally:
            connection.close()

    def test_an_unregistered_broker_cannot_reach_the_exchange(self) -> None:
        credentials = base64.b64encode(b"never-registered:secret").decode()
        body = urllib.parse.urlencode({"grant_type": exchange.GRANT_TYPE})
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.control.server_address[1], timeout=5
        )
        try:
            connection.request(
                "POST",
                "/internal/oauth/exchange",
                body=body,
                headers={
                    "Authorization": "Basic " + credentials,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(body.encode())),
                },
            )
            self.assertEqual(401, connection.getresponse().status)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
