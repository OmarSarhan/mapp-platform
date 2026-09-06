"""Introspection, revocation, and the claim the milestone exists to prove.

That claim is one sentence in the scope document: revoking the grant
invalidates an already-issued token B rather than waiting for it to expire.
Token B lives sixty seconds, so "it expires soon" is not revocation, and the
difference is only observable if a grant is a record that tokens resolve
through. Most of this file is about that.

The rest is RFC 7662's other obligation, which is easy to get wrong in the
generous direction: an inactive response must not describe the token. Saying
*why* it is inactive tells whoever holds a stolen credential whether it was
ever real, whose it was, and when it lapsed.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canonical  # noqa: E402
import exchange  # noqa: E402
import introspection  # noqa: E402
from issuer import MappAuthorizationServer  # noqa: E402
from models import Client, Grant, Token  # noqa: E402
from server import ControlServer  # noqa: E402
from stub_store import StubStore  # noqa: E402

MCP_RESOURCE = "http://mcp.localhost/mcp"
CONFIG_RESOURCE = "http://config.localhost/api"
BROKER_SECRET = "broker-secret"


class IntrospectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = StubStore()
        self.store.add_client(
            Client(
                client_id="mcp-client",
                name="Claude Code",
                redirect_uris=("http://127.0.0.1:9/cb",),
                scopes=("apply",),
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
                client_secret=BROKER_SECRET,
            )
        )
        self.now = dt.datetime.now(dt.timezone.utc)
        self.grant = self.store.save_grant(
            Grant(
                grant_id="oauth:grant-1",
                client_id="mcp-client",
                subject="admin",
                scopes=("apply",),
            )
        )
        self.store.save_token("mapp_a_live", self._token())

    def _token(self, *, expires_in: int = 900, revoked: bool = False,
               subject: str = "oauth:grant-1", audience: str = MCP_RESOURCE) -> Token:
        return Token(
            token_hash="",
            client_id="mcp-client",
            scope="apply",
            subject=subject,
            issued_at=int(self.now.timestamp()),
            expires_in=expires_in,
            revoked=revoked,
            audience=audience,
        )

    def _form(self, **fields) -> defaultdict:
        datalist: defaultdict = defaultdict(list)
        for name, value in fields.items():
            if value is None:
                continue
            datalist[name].extend(value if isinstance(value, list) else [value])
        return datalist

    def introspect(self, **fields) -> dict:
        return introspection.introspect(
            datalist=self._form(**fields), store=self.store
        )


class ActiveTests(IntrospectionTestCase):
    def test_a_live_token_is_active(self) -> None:
        result = self.introspect(token="mapp_a_live")
        self.assertTrue(result["active"])
        self.assertEqual("apply", result["scope"])
        self.assertEqual("mcp-client", result["client_id"])
        self.assertEqual(MCP_RESOURCE, result["aud"])

    def test_the_subject_is_the_grant_not_a_person(self) -> None:
        """P3: the actor is the grant.

        A single shared administrator identity would make a username field
        say nothing, and would imply an identity model this platform does not
        have.
        """
        self.assertEqual("oauth:grant-1", self.introspect(token="mapp_a_live")["sub"])
        self.assertNotIn("username", self.introspect(token="mapp_a_live"))

    def test_a_matching_resource_is_accepted(self) -> None:
        self.assertTrue(
            self.introspect(token="mapp_a_live", resource=MCP_RESOURCE)["active"]
        )


class InactiveTests(IntrospectionTestCase):
    """Every refusal, and none of them explains itself."""

    def assert_inactive(self, **fields) -> None:
        result = self.introspect(**fields)
        self.assertEqual({"active": False}, result)

    def test_an_unknown_token_is_inactive_and_says_nothing_else(self) -> None:
        # Not an error: raising would distinguish "never existed" from
        # "revoked", which is the oracle the endpoint must not be.
        self.assert_inactive(token="mapp_a_nosuch")

    def test_an_expired_token_is_inactive(self) -> None:
        self.store.save_token("mapp_a_stale", self._token(expires_in=-1))
        self.assert_inactive(token="mapp_a_stale")

    def test_a_revoked_token_is_inactive(self) -> None:
        self.store.save_token("mapp_a_dead", self._token(revoked=True))
        self.assert_inactive(token="mapp_a_dead")

    def test_a_token_whose_grant_was_revoked_is_inactive(self) -> None:
        self.assertTrue(self.store.revoke_grant("oauth:grant-1"))
        self.assert_inactive(token="mapp_a_live")

    def test_a_token_with_no_resolvable_grant_is_inactive(self) -> None:
        """Fails closed, which is what rows predating grants require."""
        self.store.save_token("mapp_a_orphan", self._token(subject="oauth:gone"))
        self.assert_inactive(token="mapp_a_orphan")

    def test_a_token_with_an_empty_subject_is_inactive(self) -> None:
        self.store.save_token("mapp_a_blank", self._token(subject=""))
        self.assert_inactive(token="mapp_a_blank")

    def test_a_token_of_a_disabled_client_is_inactive(self) -> None:
        self.store.add_client(
            Client(
                client_id="mcp-client",
                name="Claude Code",
                redirect_uris=("http://127.0.0.1:9/cb",),
                scopes=("apply",),
                token_endpoint_auth_method="none",
                disabled=True,
            )
        )
        self.assert_inactive(token="mapp_a_live")

    def test_a_mismatched_resource_is_inactive(self) -> None:
        # Exact audience: a token for the configuration API must not introspect
        # as though it were for the MCP endpoint.
        self.assert_inactive(token="mapp_a_live", resource=CONFIG_RESOURCE)

    def test_an_inactive_response_carries_no_other_member(self) -> None:
        """RFC 7662 s2.2: do not describe an inactive token."""
        self.store.save_token("mapp_a_dead", self._token(revoked=True))
        for token in ("mapp_a_nosuch", "mapp_a_dead"):
            with self.subTest(token=token):
                self.assertEqual(["active"], list(self.introspect(token=token)))


class RequestShapeTests(IntrospectionTestCase):
    def test_a_missing_token_is_a_request_error(self) -> None:
        with self.assertRaises(introspection.IntrospectionError):
            self.introspect()

    def test_a_duplicate_token_parameter_is_refused(self) -> None:
        with self.assertRaises(introspection.IntrospectionError):
            self.introspect(token=["a", "b"])

    def test_a_token_type_hint_is_accepted_and_ignored(self) -> None:
        # Honouring it would let a caller probe one token space at a time.
        self.assertTrue(
            self.introspect(token="mapp_a_live", token_type_hint="refresh_token")["active"]
        )


class GrantRevocationTests(IntrospectionTestCase):
    """The claim this milestone exists to prove."""

    def _exchange_for_token_b(self) -> str:
        context = json.dumps(
            {
                "version": canonical.SCHEME,
                "operationId": "proposals.apply",
                "method": "POST",
                "pathTemplate": "/api/proposals/{proposalId}/apply",
                "requestDigest": canonical.digest({"proposalId": "p1"}),
            }
        )
        datalist = self._form(
            grant_type=exchange.GRANT_TYPE,
            subject_token="mapp_a_live",
            subject_token_type=exchange.ACCESS_TOKEN_TYPE,
            resource=CONFIG_RESOURCE,
            scope="apply",
            **{exchange.CONTEXT_PARAMETER: context},
        )
        return exchange.exchange(
            datalist=datalist,
            broker_client=self.store.query_client("mapp-mcp-broker"),
            store=self.store,
            resource=CONFIG_RESOURCE,
            mcp_resource=MCP_RESOURCE,
            now=self.now,
        )["access_token"]

    def test_revoking_the_grant_invalidates_an_already_issued_token_b(self) -> None:
        """Not "it expires in sixty seconds" -- invalid now.

        This is the sentence the milestone exists to make true. Token B is a
        separate record with its own expiry, so without a grant to resolve
        through, revocation could only wait it out.
        """
        token_b = self._exchange_for_token_b()
        self.assertTrue(self.introspect(token=token_b)["active"])
        self.assertTrue(self.store.revoke_grant("oauth:grant-1"))
        self.assertEqual({"active": False}, self.introspect(token=token_b))

    def test_revoking_the_grant_stops_new_exchanges(self) -> None:
        self.store.revoke_grant("oauth:grant-1")
        with self.assertRaises(exchange.ExchangeError) as caught:
            self._exchange_for_token_b()
        self.assertEqual("invalid_grant", caught.exception.error)

    def test_a_disabled_client_cannot_exchange(self) -> None:
        """A grant held by a client that may no longer act mints nothing.

        Revoking the grant is one way to stop a client; disabling the client
        is the other, and the exchange has to honour both or the second is
        decoration.
        """
        self.store.add_client(
            Client(
                client_id="mcp-client", name="Claude Code",
                redirect_uris=("http://127.0.0.1:9/cb",), scopes=("apply",),
                token_endpoint_auth_method="none", disabled=True,
            )
        )
        with self.assertRaises(exchange.ExchangeError) as caught:
            self._exchange_for_token_b()
        self.assertEqual("invalid_grant", caught.exception.error)

    def test_revoking_the_grant_invalidates_token_a(self) -> None:
        self.store.revoke_grant("oauth:grant-1")
        self.assertEqual({"active": False}, self.introspect(token="mapp_a_live"))

    def test_revocation_is_reported_once(self) -> None:
        # Conditional, so two operators revoking at once cannot both believe
        # they acted and the audit records one revocation.
        self.assertTrue(self.store.revoke_grant("oauth:grant-1"))
        self.assertFalse(self.store.revoke_grant("oauth:grant-1"))

    def test_revoking_an_unknown_grant_reports_false(self) -> None:
        self.assertFalse(self.store.revoke_grant("oauth:nosuch"))

    def test_the_revoke_endpoint_revokes_the_whole_grant(self) -> None:
        """RFC 7009 revokes a token; this revokes its consent.

        Revoking only the presented token would let the next exchange mint a
        replacement immediately, so withdrawing consent would not withdraw
        anything.
        """
        token_b = self._exchange_for_token_b()
        introspection.revoke(
            datalist=self._form(token=token_b), store=self.store
        )
        self.assertEqual({"active": False}, self.introspect(token="mapp_a_live"))
        self.assertEqual({"active": False}, self.introspect(token=token_b))

    def test_revoking_an_unknown_token_reports_nothing(self) -> None:
        # RFC 7009 s2.2: the response is the same either way, so it cannot be
        # used to discover whether a token was real.
        self.assertEqual(
            {},
            introspection.revoke(
                datalist=self._form(token="mapp_a_nosuch"), store=self.store
            ),
        )


class ControlListenerTests(unittest.TestCase):
    """Both endpoints authenticated, and reachable only on the control side."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

    def setUp(self) -> None:
        self.store = StubStore()
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
        self.store.add_client(
            Client(
                client_id="mcp-client", name="C", redirect_uris=(), scopes=("apply",),
                token_endpoint_auth_method="none",
            )
        )
        now = dt.datetime.now(dt.timezone.utc)
        self.store.save_grant(
            Grant(grant_id="oauth:g", client_id="mcp-client", subject="admin",
                  scopes=("apply",))
        )
        self.store.save_token(
            "mapp_a_live",
            Token(token_hash="", client_id="mcp-client", scope="apply",
                  subject="oauth:g", issued_at=int(now.timestamp()), expires_in=900,
                  audience=MCP_RESOURCE),
        )
        self.authorization = MappAuthorizationServer(
            self.store, issuer="http://mcp.localhost", resource=MCP_RESOURCE,
            config_api_resource=CONFIG_RESOURCE, scopes_supported=("apply",),
        )
        self.httpd = ControlServer(("127.0.0.1", 0), self.authorization)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, path, fields, *, auth=("mapp-mcp-broker", BROKER_SECRET)):
        body = urllib.parse.urlencode(fields)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body.encode())),
        }
        if auth is not None:
            raw = f"{auth[0]}:{auth[1]}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode() or "{}")
        finally:
            connection.close()

    def test_introspection_answers_an_authenticated_caller(self) -> None:
        status, body = self.post("/internal/oauth/introspect", {"token": "mapp_a_live"})
        self.assertEqual(200, status)
        self.assertTrue(body["active"])

    def test_introspection_refuses_an_unauthenticated_caller(self) -> None:
        """Before interpreting the body.

        RFC 7662 s4: an open introspection endpoint is an oracle for guessing
        tokens, and this one would answer for the platform's credentials.
        """
        status, body = self.post(
            "/internal/oauth/introspect", {"token": "mapp_a_live"}, auth=None
        )
        self.assertEqual(401, status)
        self.assertEqual("invalid_client", body["error"])

    def test_revocation_refuses_an_unauthenticated_caller(self) -> None:
        status, _ = self.post(
            "/internal/oauth/revoke", {"token": "mapp_a_live"}, auth=None
        )
        self.assertEqual(401, status)
        # And nothing was revoked.
        self.assertFalse(self.store.query_grant("oauth:g").is_revoked())

    def test_a_wrong_secret_is_refused(self) -> None:
        status, _ = self.post(
            "/internal/oauth/introspect", {"token": "mapp_a_live"},
            auth=("mapp-mcp-broker", "wrong"),
        )
        self.assertEqual(401, status)

    def test_a_public_client_cannot_introspect(self) -> None:
        status, _ = self.post(
            "/internal/oauth/introspect", {"token": "mapp_a_live"},
            auth=("mcp-client", ""),
        )
        self.assertEqual(401, status)

    def test_a_missing_token_is_a_four_hundred(self) -> None:
        status, body = self.post("/internal/oauth/introspect", {})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])

    def test_revocation_over_http_revokes_the_grant(self) -> None:
        status, _ = self.post("/internal/oauth/revoke", {"token": "mapp_a_live"})
        self.assertEqual(200, status)
        self.assertTrue(self.store.query_grant("oauth:g").is_revoked())


if __name__ == "__main__":
    unittest.main()
