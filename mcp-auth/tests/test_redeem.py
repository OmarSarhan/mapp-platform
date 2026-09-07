"""Redemption: the other half of the operation binding.

The broker validates the *shape* of the digest it is handed at exchange time
and stores it. It never sees the downstream request, so it can never check that
the digest describes anything real -- exchange.py says so in a comment. This
endpoint is where the configuration API brings back a digest computed from the
request actually in front of it, and where a token B stops being a plain
scoped bearer credential.

Everything here is about what must be refused. A token that redeems against
the wrong request, or twice, is a token that authorises more than one effect.
"""

from __future__ import annotations

import base64
import datetime as dt
import http.client
import json
import sys
import threading
import unittest
import urllib.parse
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canonical  # noqa: E402
import introspection  # noqa: E402
from issuer import MappAuthorizationServer  # noqa: E402
from models import Client, Grant  # noqa: E402
from server import ControlServer  # noqa: E402
from stub_store import StubStore  # noqa: E402

MCP_RESOURCE = "http://mcp.localhost/mcp"
CONFIG_RESOURCE = "http://config.localhost/api"
BROKER_SECRET = "broker-secret"
OPERATION = "proposals.apply"
DIGEST = canonical.digest({"envelope": "one"})
OTHER_DIGEST = canonical.digest({"envelope": "two"})


class RedeemTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = StubStore()
        self.store.add_client(
            Client(
                client_id="mapp-mcp-broker", name="Broker", redirect_uris=(),
                scopes=(), token_endpoint_auth_method="client_secret_basic",
                client_secret=BROKER_SECRET,
            )
        )
        self.store.add_client(
            Client(
                client_id="mcp-client", name="Agent",
                redirect_uris=("http://127.0.0.1:9/cb",), scopes=("apply",),
                token_endpoint_auth_method="none",
            )
        )
        self.store.save_grant(
            Grant(grant_id="oauth:g", client_id="mcp-client", subject="operator",
                  scopes=("apply",))
        )

    def mint(self, raw: str, *, single_use: bool = True, digest: str = DIGEST,
             operation_id: str = OPERATION, expires_in: int = 60) -> None:
        issued = dt.datetime.now(dt.timezone.utc)
        self.store.save_exchanged_token(
            raw,
            client_id="mapp-mcp-broker",
            actor_client_id="mcp-client",
            subject="oauth:g",
            scope="apply",
            audience=CONFIG_RESOURCE,
            issued_at=issued,
            expires_at=issued + dt.timedelta(seconds=expires_in),
            operation_id=operation_id,
            request_digest=digest,
            single_use=single_use,
        )

    def form(self, **fields) -> defaultdict:
        datalist: defaultdict = defaultdict(list)
        for name, value in fields.items():
            if value is None:
                continue
            datalist[name].extend(value if isinstance(value, list) else [value])
        return datalist

    def redeem(self, **fields):
        return introspection.redeem(datalist=self.form(**fields), store=self.store)

    def assert_refused(self, error: str, **fields) -> None:
        with self.assertRaises(introspection.IntrospectionError) as caught:
            self.redeem(**fields)
        self.assertEqual(error, caught.exception.error)


class MutatingTokenTests(RedeemTestCase):
    """A mutating operation's token is single-use, and the spend is atomic."""

    def test_a_matching_presentation_redeems(self) -> None:
        self.mint("mapp_b_one")
        result = self.redeem(
            token="mapp_b_one", operation_id=OPERATION, request_digest=DIGEST
        )
        self.assertEqual(
            {"redeemed": True, "single_use": True, "operation_id": OPERATION},
            result,
        )

    def test_a_second_presentation_is_refused(self) -> None:
        """The whole point of single use, and the replay it prevents."""
        self.mint("mapp_b_two")
        self.redeem(
            token="mapp_b_two", operation_id=OPERATION, request_digest=DIGEST
        )
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_two", operation_id=OPERATION, request_digest=DIGEST,
        )

    def test_a_different_request_digest_is_refused(self) -> None:
        """A token minted for one proposal must not apply another.

        This is the binding. Without it a token B is a scoped bearer token
        that happens to record an operation nobody checks.
        """
        self.mint("mapp_b_three")
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_three", operation_id=OPERATION,
            request_digest=OTHER_DIGEST,
        )

    def test_a_refused_digest_does_not_spend_the_token(self) -> None:
        """A wrong presentation must not burn a credential.

        The digest is a predicate of the consuming statement rather than
        something compared after it, so a mismatch cannot mark the row -- and
        the legitimate retry still works.
        """
        self.mint("mapp_b_four")
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_four", operation_id=OPERATION,
            request_digest=OTHER_DIGEST,
        )
        self.assertEqual(
            {"redeemed": True, "single_use": True, "operation_id": OPERATION},
            self.redeem(
                token="mapp_b_four", operation_id=OPERATION, request_digest=DIGEST
            ),
        )

    def test_a_different_operation_is_refused(self) -> None:
        self.mint("mapp_b_five")
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_five", operation_id="semantic.proposals.apply",
            request_digest=DIGEST,
        )

    def test_an_expired_token_is_refused(self) -> None:
        self.mint("mapp_b_stale", expires_in=-1)
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_stale", operation_id=OPERATION, request_digest=DIGEST,
        )

    def test_a_revoked_grant_refuses_redemption(self) -> None:
        """Sixty seconds of life is not the same as being live."""
        self.mint("mapp_b_revoked")
        self.assertTrue(self.store.revoke_grant("oauth:g", "test"))
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_revoked", operation_id=OPERATION, request_digest=DIGEST,
        )

    def test_an_unknown_token_is_refused(self) -> None:
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_nosuch", operation_id=OPERATION, request_digest=DIGEST,
        )


class ReadTokenTests(RedeemTestCase):
    """A read operation's token is not single-use, so nothing is spent.

    There is no consuming statement to carry the predicates, which makes the
    comparison here the entire binding check for every read.
    """

    def test_a_read_token_verifies_without_being_spent(self) -> None:
        self.mint("mapp_b_read", single_use=False, operation_id="layers.values")
        for _ in range(3):
            self.assertEqual(
                {
                    "redeemed": True,
                    "single_use": False,
                    "operation_id": "layers.values",
                },
                self.redeem(
                    token="mapp_b_read", operation_id="layers.values",
                    request_digest=DIGEST,
                ),
            )

    def test_a_read_token_still_refuses_a_different_digest(self) -> None:
        self.mint("mapp_b_read2", single_use=False, operation_id="layers.values")
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_read2", operation_id="layers.values",
            request_digest=OTHER_DIGEST,
        )

    def test_a_read_token_still_refuses_a_different_operation(self) -> None:
        self.mint("mapp_b_read3", single_use=False, operation_id="layers.values")
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_read3", operation_id=OPERATION, request_digest=DIGEST,
        )

    def test_a_revoked_grant_refuses_a_read_token_too(self) -> None:
        self.mint("mapp_b_read4", single_use=False, operation_id="layers.values")
        self.assertTrue(self.store.revoke_grant("oauth:g", "test"))
        self.assert_refused(
            "invalid_grant",
            token="mapp_b_read4", operation_id="layers.values",
            request_digest=DIGEST,
        )


class RequestShapeTests(RedeemTestCase):
    def test_every_parameter_is_required(self) -> None:
        self.mint("mapp_b_shape")
        for missing in ("token", "operation_id", "request_digest"):
            fields = {
                "token": "mapp_b_shape",
                "operation_id": OPERATION,
                "request_digest": DIGEST,
            }
            fields.pop(missing)
            with self.subTest(missing=missing):
                self.assert_refused("invalid_request", **fields)

    def test_a_malformed_digest_is_a_request_error(self) -> None:
        """Distinguished from a refusal: it describes the request, not the token."""
        self.mint("mapp_b_shape2")
        for digest in ("", "mapp-jcs-v1:", "deadbeef", canonical.SCHEME + ":zz",
                       DIGEST + "0", DIGEST.upper()):
            with self.subTest(digest=digest):
                self.assert_refused(
                    "invalid_request",
                    token="mapp_b_shape2", operation_id=OPERATION,
                    request_digest=digest,
                )

    def test_a_bare_scheme_prefix_cannot_be_a_binding(self) -> None:
        """A prefix match would accept a digest that matches anything."""
        self.assert_refused(
            "invalid_request",
            token="mapp_b_x", operation_id=OPERATION,
            request_digest=canonical.SCHEME + ":",
        )

    def test_a_duplicate_parameter_is_refused(self) -> None:
        self.mint("mapp_b_dup")
        self.assert_refused(
            "invalid_request",
            token="mapp_b_dup", operation_id=[OPERATION, "layers.values"],
            request_digest=DIGEST,
        )

    def test_a_token_a_cannot_be_redeemed(self) -> None:
        """It has no binding at all, so there is nothing to redeem it against."""
        from models import Token

        now = dt.datetime.now(dt.timezone.utc)
        self.store.save_token(
            "mapp_a_live",
            Token(token_hash="", client_id="mcp-client", scope="apply",
                  subject="oauth:g", issued_at=int(now.timestamp()),
                  expires_in=900, audience=MCP_RESOURCE),
        )
        self.assert_refused(
            "invalid_grant",
            token="mapp_a_live", operation_id=OPERATION, request_digest=DIGEST,
        )


class RedeemOverHttpTests(RedeemTestCase):
    """The endpoint as deployed: control listener, client-authenticated."""

    def setUp(self) -> None:
        super().setUp()
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

    def post(self, fields, *, auth=("mapp-mcp-broker", BROKER_SECRET)):
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
            connection.request(
                "POST", "/internal/oauth/redeem", body=body, headers=headers
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode() or "{}")
        finally:
            connection.close()

    def test_a_redemption_succeeds_over_http(self) -> None:
        self.mint("mapp_b_http")
        status, body = self.post({
            "token": "mapp_b_http", "operation_id": OPERATION,
            "request_digest": DIGEST,
        })
        self.assertEqual(200, status)
        self.assertTrue(body["redeemed"])

    def test_an_unauthenticated_caller_learns_nothing(self) -> None:
        """401 before any lookup: otherwise the endpoint burns tokens for free."""
        self.mint("mapp_b_http2")
        status, _ = self.post(
            {"token": "mapp_b_http2", "operation_id": OPERATION,
             "request_digest": DIGEST},
            auth=None,
        )
        self.assertEqual(401, status)
        # And the token is still spendable, so the refused call did not consume it.
        self.assertIsNotNone(self.store.exchanged_binding("mapp_b_http2"))
        self.assertIsNotNone(
            self.store.consume_exchanged_token("mapp_b_http2", OPERATION, DIGEST)
        )

    def test_a_public_client_cannot_redeem(self) -> None:
        self.mint("mapp_b_http3")
        status, _ = self.post(
            {"token": "mapp_b_http3", "operation_id": OPERATION,
             "request_digest": DIGEST},
            auth=("mcp-client", ""),
        )
        self.assertEqual(401, status)

    def test_a_refusal_is_a_four_hundred_with_an_oauth_error(self) -> None:
        self.mint("mapp_b_http4")
        status, body = self.post({
            "token": "mapp_b_http4", "operation_id": OPERATION,
            "request_digest": OTHER_DIGEST,
        })
        self.assertEqual(400, status)
        self.assertEqual("invalid_grant", body["error"])


if __name__ == "__main__":
    unittest.main()
