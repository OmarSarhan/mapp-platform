"""Minting a credential bound to one configuration-API request.

The digest this builds is checked by a different component against a request it
reconstructs itself, so a mistake here surfaces there as a 403 that names
nothing. These tests are about the two things this side controls: that the
envelope it digests describes the request it is about to make, and that the
identity it binds to is the platform's rather than one it was told.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canonical  # noqa: E402
import execution_envelope  # noqa: E402
from exchange_client import CONTEXT_PARAMETER  # noqa: E402
from exchange_client import ExchangeClient  # noqa: E402
from exchange_client import ExchangeRefused  # noqa: E402
from exchange_client import ExchangeUnavailable  # noqa: E402

INSTANCE = "f73ee47d100d7b1dc8a39d14644072f8"

REQUEST = dict(
    operation_id="layers.values",
    method="GET",
    path_template="/api/layers/{layerKey}/values",
    path="/api/layers/roads/values",
    query="field=name&limit=10",
    body=None,
    scope="derive semantic:inspect",
)


class FakeClient(ExchangeClient):
    """Replaces only the two network calls, so the envelope logic is the real one."""

    def __init__(self, *, answer=None, raises=None, instance=INSTANCE, **kwargs):
        kwargs.setdefault("broker_endpoint", "http://mcp-auth:8080")
        kwargs.setdefault("config_api_endpoint", "http://config-ui:8080")
        kwargs.setdefault("config_api_resource", "http://config.localhost/api")
        kwargs.setdefault("client_id", "mapp-mcp")
        kwargs.setdefault("client_secret", "runtime-secret")
        super().__init__(**kwargs)
        self.answer = answer or {"access_token": "mapp_b_minted"}
        self.raises = raises
        self.posted = []
        self.identity_calls = 0
        self._fixed_instance = instance

    def instance_id(self):
        self.identity_calls += 1
        if self._fixed_instance is None:
            raise ExchangeUnavailable("no identity")
        return self._fixed_instance

    def _post(self, fields):
        self.posted.append(fields)
        if self.raises is not None:
            raise self.raises
        return self.answer


class BindingTests(unittest.TestCase):
    def test_the_digest_describes_the_request_being_made(self) -> None:
        """Recomputed independently here, exactly as the other side will.

        Asserting that *some* digest was sent would pass for a client that
        digested the wrong request, which is the failure this is for.
        """
        client = FakeClient()
        client.exchange(subject_token="mapp_a_live", **REQUEST)
        context = json.loads(client.posted[0][CONTEXT_PARAMETER])
        self.assertEqual(
            execution_envelope.digest(
                instance=INSTANCE,
                method="GET",
                operation_id="layers.values",
                path_template="/api/layers/{layerKey}/values",
                path="/api/layers/roads/values",
                query="field=name&limit=10",
                body=None,
                resolved_defaults=None,
                confirmation_fields=None,
                revision_binding=None,
            ),
            context["requestDigest"],
        )

    def test_the_context_carries_the_scheme_the_other_side_expects(self) -> None:
        client = FakeClient()
        client.exchange(subject_token="mapp_a_live", **REQUEST)
        context = json.loads(client.posted[0][CONTEXT_PARAMETER])
        self.assertEqual(canonical.SCHEME, context["version"])
        self.assertEqual("layers.values", context["operationId"])
        self.assertEqual("GET", context["method"])
        self.assertEqual("/api/layers/{layerKey}/values", context["pathTemplate"])

    def test_a_different_request_gets_a_different_digest(self) -> None:
        """The binding is per request, not per operation."""
        one = FakeClient()
        one.exchange(subject_token="mapp_a_live", **REQUEST)
        other = FakeClient()
        other.exchange(
            subject_token="mapp_a_live", **dict(REQUEST, query="field=name&limit=11")
        )
        self.assertNotEqual(
            json.loads(one.posted[0][CONTEXT_PARAMETER])["requestDigest"],
            json.loads(other.posted[0][CONTEXT_PARAMETER])["requestDigest"],
        )

    def test_the_subject_token_and_audience_are_sent(self) -> None:
        client = FakeClient()
        client.exchange(subject_token="mapp_a_live", **REQUEST)
        sent = client.posted[0]
        self.assertEqual("mapp_a_live", sent["subject_token"])
        self.assertEqual("http://config.localhost/api", sent["resource"])
        self.assertEqual("derive semantic:inspect", sent["scope"])

    def test_the_minted_credential_is_returned(self) -> None:
        client = FakeClient(answer={"access_token": "mapp_b_minted"})
        self.assertEqual(
            "mapp_b_minted", client.exchange(subject_token="mapp_a_live", **REQUEST)
        )

    def test_an_answer_with_no_credential_is_a_fault_not_a_token(self) -> None:
        client = FakeClient(answer={"token_type": "Bearer"})
        with self.assertRaises(ExchangeUnavailable):
            client.exchange(subject_token="mapp_a_live", **REQUEST)


class IdentityTests(unittest.TestCase):
    """The instance is fetched, not configured, and that is the point.

    It lives in the control schema, which this component cannot read. A value
    copied into configuration would be a second source that drifts, and drift
    is invisible until every call is refused.
    """

    def test_the_identity_is_read_before_the_digest_is_built(self) -> None:
        client = FakeClient()
        client.exchange(subject_token="mapp_a_live", **REQUEST)
        self.assertEqual(1, client.identity_calls)

    def test_an_unreadable_identity_stops_the_exchange(self) -> None:
        """Rather than binding to a guess, which would refuse at the far end."""
        client = FakeClient(instance=None)
        with self.assertRaises(ExchangeUnavailable):
            client.exchange(subject_token="mapp_a_live", **REQUEST)
        self.assertEqual([], client.posted, "it asked the broker anyway")


class RefusalTests(unittest.TestCase):
    """A refusal is about the request; unavailability is about us."""

    def test_a_broker_refusal_is_reported_as_one(self) -> None:
        client = FakeClient(raises=ExchangeRefused("scope exceeds the grant",
                                                   error="invalid_scope"))
        with self.assertRaises(ExchangeRefused) as caught:
            client.exchange(subject_token="mapp_a_live", **REQUEST)
        self.assertEqual("invalid_scope", caught.exception.error)

    def test_an_unavailable_broker_is_not_a_refusal(self) -> None:
        """They mean different things to a caller.

        A refusal says this request will never work as asked. Unavailability
        says nothing about the request at all, and a tool that conflated them
        would tell an operator their scopes were wrong during an outage.
        """
        client = FakeClient(raises=ExchangeUnavailable("down"))
        with self.assertRaises(ExchangeUnavailable):
            client.exchange(subject_token="mapp_a_live", **REQUEST)
        self.assertNotIsInstance(ExchangeUnavailable("x"), ExchangeRefused)


class HttpErrorMappingTests(unittest.TestCase):
    """The real `_post`, so the status-to-meaning mapping is exercised.

    Every other test here replaces `_post` outright, which is right for the
    envelope logic and left this untested -- deleting the 401/403 branch
    survived mutation. The branch matters: a 401 or 403 from the broker is
    *this component's* credential being wrong, and reporting it as a refusal
    would tell an operator their agent's scopes were at fault during what is
    actually a deployment error.
    """

    def client(self, status: int, payload: dict):
        import io
        import urllib.error

        real = ExchangeClient(
            broker_endpoint="http://mcp-auth:8080",
            config_api_endpoint="http://config-ui:8080",
            config_api_resource="http://config.localhost/api",
            client_id="mapp-mcp",
            client_secret="runtime-secret",
        )
        body = json.dumps(payload).encode()

        def raise_http(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, status, "refused", {}, io.BytesIO(body)
            )

        import exchange_client

        self._saved = exchange_client.urllib.request.urlopen
        exchange_client.urllib.request.urlopen = raise_http
        self.addCleanup(
            setattr, exchange_client.urllib.request, "urlopen", self._saved
        )
        return real

    def test_a_401_is_our_credential_not_the_caller_s_request(self) -> None:
        client = self.client(401, {"error": "invalid_client"})
        with self.assertRaises(ExchangeUnavailable):
            client._post({"grant_type": "x"})

    def test_a_403_is_also_ours(self) -> None:
        """The capability separation answers 403 when a client may not exchange."""
        client = self.client(403, {"error": "unauthorized_client"})
        with self.assertRaises(ExchangeUnavailable):
            client._post({"grant_type": "x"})

    def test_a_400_is_about_the_request(self) -> None:
        client = self.client(400, {
            "error": "invalid_scope",
            "error_description": "scope exceeds the grant",
        })
        with self.assertRaises(ExchangeRefused) as caught:
            client._post({"grant_type": "x"})
        self.assertEqual("invalid_scope", caught.exception.error)
        self.assertIn("exceeds the grant", str(caught.exception))

    def test_an_unreadable_error_body_is_still_a_refusal_not_a_crash(self) -> None:
        client = self.client(400, {})
        with self.assertRaises(ExchangeRefused):
            client._post({"grant_type": "x"})


class SecretScrubbingTests(unittest.TestCase):
    def test_the_client_secret_never_appears_in_an_error(self) -> None:
        """An exception string can carry a request URL, and a future caller
        could put one in a field. Cheap to make impossible."""
        client = FakeClient()
        self.assertIn("[redacted]", client._scrub("boom runtime-secret boom"))
        self.assertNotIn("runtime-secret", client._scrub("boom runtime-secret"))


if __name__ == "__main__":
    unittest.main()
