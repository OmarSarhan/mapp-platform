"""The tool's own decisions, with the platform stubbed out.

What it does with a broker and a configuration API is proved end to end against
the running stack. What is worth pinning here is the part that is this tool's
judgement: which requests it refuses before spending anything, and what it tells
a caller when the platform says no.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from authentication import Authenticated  # noqa: E402
from authentication import CURRENT_CALLER  # noqa: E402
from config_api_client import ConfigApiRefused  # noqa: E402
from config_api_client import ConfigApiUnavailable  # noqa: E402
from config_api_client import layer_values_query  # noqa: E402
from exchange_client import ExchangeRefused  # noqa: E402
from exchange_client import ExchangeUnavailable  # noqa: E402
from protected_resource import ProtectedResource  # noqa: E402
from runtime import build_runtime  # noqa: E402


class FakeExchange:
    def __init__(self, *, raises=None) -> None:
        self.raises = raises
        self.calls = []

    def exchange(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return "mapp_b_minted"


class FakeConfigApi:
    def __init__(self, *, answer=None, raises=None) -> None:
        self.answer = answer if answer is not None else {"total": 3}
        self.raises = raises
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.answer


def caller(scopes="derive semantic:inspect mcp:connect", token="mapp_a_live"):
    return Authenticated(
        {"sub": "oauth:grant", "scope": scopes, "aud": "http://mcp.localhost/mcp"},
        token,
    )


class ToolTestCase(unittest.TestCase):
    def build(self, *, exchange=None, config_api=None):
        resource = ProtectedResource(
            origin="http://mcp.localhost", issuer="http://mcp.localhost"
        )
        server = build_runtime(
            resource=resource,
            exchange=exchange or FakeExchange(),
            config_api=config_api or FakeConfigApi(),
        )
        # Reach the registered function rather than dispatching through the
        # SDK: the SDK's own path is exercised against the deployed stack, and
        # what these assert is the tool's logic.
        return server._tool_manager._tools["layer_values"].fn

    def as_caller(self, who):
        token = CURRENT_CALLER.set(who)
        self.addCleanup(CURRENT_CALLER.reset, token)


class ScopeTests(ToolTestCase):
    def test_a_grant_without_the_scopes_is_refused_before_anything_is_spent(
        self,
    ) -> None:
        """Refused here, not at the broker, so the message names what to ask for.

        The broker would refuse it too -- with an error saying the scope
        exceeded the grant, and not which scope. An agent cannot act on that;
        it can act on "re-authorize requesting derive semantic:inspect".
        """
        exchange = FakeExchange()
        tool = self.build(exchange=exchange)
        self.as_caller(caller(scopes="mcp:connect inspect"))
        with self.assertRaises(ValueError) as caught:
            tool(layer_key="Census_OA_Population", field="population_quintile")
        message = str(caught.exception)
        self.assertIn("derive", message)
        self.assertIn("semantic:inspect", message)
        self.assertEqual([], exchange.calls, "it asked for a credential anyway")

    def test_a_partial_grant_names_only_what_is_missing(self) -> None:
        tool = self.build()
        self.as_caller(caller(scopes="mcp:connect derive"))
        with self.assertRaises(ValueError) as caught:
            tool(layer_key="L", field="f")
        self.assertIn("semantic:inspect", str(caught.exception))
        self.assertNotIn("does not carry derive", str(caught.exception))

    def test_no_caller_at_all_is_refused(self) -> None:
        """Unreachable through the middleware; checked because the alternative
        if it ever became reachable is an unauthenticated platform call."""
        tool = self.build()
        self.as_caller(None)
        with self.assertRaises(ValueError):
            tool(layer_key="L", field="f")


class BindingTests(ToolTestCase):
    def test_the_request_exchanged_for_is_the_request_made(self) -> None:
        """The property the whole binding rests on.

        The credential is minted for one path and query; sending a different
        one is refused by the configuration API with nothing to say why. Built
        once and used twice is what makes them the same by construction.
        """
        exchange = FakeExchange()
        config_api = FakeConfigApi()
        tool = self.build(exchange=exchange, config_api=config_api)
        self.as_caller(caller())
        tool(layer_key="Census_OA_Population", field="population_quintile", limit=5)
        minted, spent = exchange.calls[0], config_api.calls[0]
        self.assertEqual(minted["path"], spent["path"])
        self.assertEqual(minted["query"], spent["query"])
        self.assertEqual("mapp_b_minted", spent["token"])

    def test_the_caller_s_own_credential_is_the_subject(self) -> None:
        """Not this component's. The grant being narrowed is the operator's."""
        exchange = FakeExchange()
        tool = self.build(exchange=exchange)
        self.as_caller(caller(token="mapp_a_the_caller"))
        tool(layer_key="L", field="f")
        self.assertEqual("mapp_a_the_caller", exchange.calls[0]["subject_token"])

    def test_the_layer_key_is_encoded_into_the_path(self) -> None:
        """A key with a slash would otherwise change which route was addressed."""
        exchange = FakeExchange()
        tool = self.build(exchange=exchange)
        self.as_caller(caller())
        tool(layer_key="odd/key", field="f")
        self.assertEqual(
            "/api/layers/odd%2Fkey/values", exchange.calls[0]["path"]
        )


class QueryShapeTests(unittest.TestCase):
    """The query is digested byte for byte, so its shape is a contract."""

    def test_the_order_is_fixed_regardless_of_argument_order(self) -> None:
        """Tool arguments arrive as an unordered object; the digest is not."""
        self.assertEqual(
            "field=f&locale=en&limit=5",
            layer_values_query(field="f", locale="en", limit=5),
        )

    def test_absent_optionals_are_absent_rather_than_empty(self) -> None:
        """`limit=` is a different request from no limit, and the API refuses it."""
        self.assertEqual("field=f", layer_values_query(field="f", locale=None, limit=None))

    def test_a_space_travels_as_percent_twenty(self) -> None:
        """Not as `+`.

        The configuration API decodes with parse_qsl, which reads `+` as a
        space -- so a literal plus encoded as `+` arrives as a space and the
        request the digest covers is not the request that happens.
        """
        self.assertEqual("field=a%20b", layer_values_query(field="a b", locale=None, limit=None))
        self.assertEqual("field=a%2Bb", layer_values_query(field="a+b", locale=None, limit=None))


class FailureReportingTests(ToolTestCase):
    """A refusal is about the request; unavailability is about the platform."""

    def test_a_platform_refusal_reaches_the_caller(self) -> None:
        tool = self.build(
            exchange=FakeExchange(raises=ExchangeRefused("scope exceeds the grant"))
        )
        self.as_caller(caller())
        with self.assertRaises(ValueError) as caught:
            tool(layer_key="L", field="f")
        self.assertIn("exceeds the grant", str(caught.exception))

    def test_an_outage_does_not_read_as_a_permissions_problem(self) -> None:
        """Telling an agent its scopes are wrong during an outage sends it to
        re-authorize, which cannot help and costs the operator a sign-in."""
        tool = self.build(exchange=FakeExchange(raises=ExchangeUnavailable("down")))
        self.as_caller(caller())
        with self.assertRaises(RuntimeError) as caught:
            tool(layer_key="L", field="f")
        self.assertNotIsInstance(caught.exception, ValueError)
        self.assertIn("unavailable", str(caught.exception))

    def test_a_binding_refusal_carries_the_platform_s_code(self) -> None:
        """auth.binding_refused is the one an operator needs to see: it means
        the two sides disagreed about which request was authorised."""
        tool = self.build(
            config_api=FakeConfigApi(
                raises=ConfigApiRefused(
                    "binding refused", status=403, code="auth.binding_refused"
                )
            )
        )
        self.as_caller(caller())
        with self.assertRaises(ValueError) as caught:
            tool(layer_key="L", field="f")
        self.assertIn("auth.binding_refused", str(caught.exception))

    def test_an_unavailable_configuration_api_is_not_a_refusal_either(self) -> None:
        tool = self.build(config_api=FakeConfigApi(raises=ConfigApiUnavailable("down")))
        self.as_caller(caller())
        with self.assertRaises(RuntimeError) as caught:
            tool(layer_key="L", field="f")
        self.assertNotIsInstance(caught.exception, ValueError)


if __name__ == "__main__":
    unittest.main()
