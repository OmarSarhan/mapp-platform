"""The layer tools' own decisions, with the platform stubbed out.

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
from config_api_client import layer_statistics_query  # noqa: E402
from config_api_client import layer_values_query  # noqa: E402
from config_api_client import layers_query  # noqa: E402
from exchange_client import ExchangeRefused  # noqa: E402
from exchange_client import ExchangeUnavailable  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
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


#: `inspect` is in the default because any caller that can *see* a tool holds
#: it -- it is the listing scope -- so a caller without it is not a realistic
#: grant. Tests about a missing scope name the narrower set explicitly.
def caller(
    scopes="mcp:connect inspect derive semantic:inspect", token="mapp_a_live"
):
    return Authenticated(
        {"sub": "oauth:grant", "scope": scopes, "aud": "http://mcp.localhost/mcp"},
        token,
    )


class ToolTestCase(unittest.TestCase):
    def build_named(self, name, *, exchange=None, config_api=None):
        resource = ProtectedResource(
            origin="http://mcp.localhost", issuer="http://mcp.localhost"
        )
        server = build_runtime(
            resource=resource,
            exchange=exchange or FakeExchange(),
            config_api=config_api or FakeConfigApi(),
        )
        return server._tool_manager._tools[name].fn

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
        return server._tool_manager._tools["layers_values"].fn

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
        with self.assertRaises(ToolError) as caught:
            tool(layer_key="Census_OA_Population", field="population_quintile")
        message = str(caught.exception)
        self.assertIn("derive", message)
        self.assertIn("semantic:inspect", message)
        self.assertEqual([], exchange.calls, "it asked for a credential anyway")

    def test_a_partial_grant_names_only_what_is_missing(self) -> None:
        tool = self.build()
        self.as_caller(caller(scopes="mcp:connect derive"))
        with self.assertRaises(ToolError) as caught:
            tool(layer_key="L", field="f")
        self.assertIn("semantic:inspect", str(caught.exception))
        self.assertNotIn("does not carry derive", str(caught.exception))

    def test_no_caller_at_all_is_refused(self) -> None:
        """Unreachable through the middleware; checked because the alternative
        if it ever became reachable is an unauthenticated platform call."""
        tool = self.build()
        self.as_caller(None)
        with self.assertRaises(ToolError):
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
        with self.assertRaises(ToolError) as caught:
            tool(layer_key="L", field="f")
        self.assertIn("exceeds the grant", str(caught.exception))

    def test_an_outage_does_not_read_as_a_permissions_problem(self) -> None:
        """Telling an agent its scopes are wrong during an outage sends it to
        re-authorize, which cannot help and costs the operator a sign-in.

        Asserted on what the message says rather than on its class. Both are
        `ToolError` now -- that is what makes either message reach the model at
        all -- so the distinction the caller acts on lives in the words, and a
        test keyed on the type would pass for a message that said the wrong
        thing.
        """
        tool = self.build(exchange=FakeExchange(raises=ExchangeUnavailable("down")))
        self.as_caller(caller())
        with self.assertRaises(ToolError) as caught:
            tool(layer_key="L", field="f")
        message = str(caught.exception)
        self.assertIn("unavailable", message)
        for misleading in ("Re-authorize", "does not carry", "refused this request"):
            self.assertNotIn(misleading, message)

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
        with self.assertRaises(ToolError) as caught:
            tool(layer_key="L", field="f")
        self.assertIn("auth.binding_refused", str(caught.exception))

    def test_an_unavailable_configuration_api_is_not_a_refusal_either(self) -> None:
        tool = self.build(config_api=FakeConfigApi(raises=ConfigApiUnavailable("down")))
        self.as_caller(caller())
        with self.assertRaises(ToolError) as caught:
            tool(layer_key="L", field="f")
        message = str(caught.exception)
        self.assertIn("unavailable", message)
        self.assertNotIn("refused this request", message)


class AnticipatedFailureTests(ToolTestCase):
    """Every refusal must be the SDK's anticipated-failure type.

    The SDK puts a `ToolError`'s text in the result the model reads and treats
    every other exception as a crash -- replacing the message with "Error
    executing tool layer_values" and logging a traceback at ERROR. So the class
    of these exceptions is the whole reason any of the messages above exist.

    `ValueError` is called out by name because that is what they were, and
    because it is the reflex: it reads as the right exception for a bad
    argument and is silently the wrong one here.
    """

    def failures(self):
        return [
            ("no caller", self.build(), None, dict(layer_key="L", field="f")),
            ("missing scopes", self.build(), caller(scopes="mcp:connect"),
             dict(layer_key="L", field="f")),
            ("broker refusal",
             self.build(exchange=FakeExchange(raises=ExchangeRefused("no"))),
             caller(), dict(layer_key="L", field="f")),
            ("broker outage",
             self.build(exchange=FakeExchange(raises=ExchangeUnavailable("down"))),
             caller(), dict(layer_key="L", field="f")),
            ("platform refusal",
             self.build(config_api=FakeConfigApi(
                 raises=ConfigApiRefused("no", status=403, code="auth.binding_refused"))),
             caller(), dict(layer_key="L", field="f")),
            ("platform outage",
             self.build(config_api=FakeConfigApi(raises=ConfigApiUnavailable("down"))),
             caller(), dict(layer_key="L", field="f")),
        ]

    def test_no_failure_path_raises_a_type_the_sdk_would_call_a_crash(self) -> None:
        for name, tool, who, kwargs in self.failures():
            with self.subTest(failure=name):
                self.as_caller(who)
                with self.assertRaises(Exception) as caught:
                    tool(**kwargs)
                self.assertIsInstance(
                    caught.exception,
                    ToolError,
                    f"{name} raises {type(caught.exception).__name__};"
                    " the SDK discards its message and logs a crash",
                )

    def test_every_failure_says_something_a_caller_can_act_on(self) -> None:
        """A message that survives is only worth surviving if it says what to do."""
        for name, tool, who, kwargs in self.failures():
            with self.subTest(failure=name):
                self.as_caller(who)
                with self.assertRaises(ToolError) as caught:
                    tool(**kwargs)
                message = str(caught.exception)
                self.assertTrue(message.strip(), f"{name} raised an empty message")
                self.assertNotIn("Error executing tool", message)


if __name__ == "__main__":
    unittest.main()


#: One layer-listing response, shaped as the configuration API returns it.
LISTING = {
    "revision": "rev-1",
    "locale": "en",
    "layers": {
        "Census_OA_Population": {
            "name": "Census OA Population",
            "group": "Census",
            "table": "derived_layers.census_oa_population_quintiles",
            "infoj": [
                {"field": "oa_id", "type": "text"},
                {"field": "population_quintile", "type": "integer"},
                {"type": "geometry"},
            ],
        },
        "Bus_Stops": {"name": "Bus Stops", "infoj": [{"field": "town"}]},
    },
}


class ListingTests(ToolTestCase):
    """`layers_list` exists because `layers_values` was unusable without it.

    Every test of the values tool had to name a layer key found by reading the
    workspace file by hand. An agent cannot do that, so in a conversation the
    first question -- "what is there?" -- had no answer.
    """

    def tool(self, name, payload=LISTING, exchange=None):
        return self.build_named(
            name,
            exchange=exchange,
            config_api=FakeConfigApi(answer=payload),
        )

    def test_the_index_carries_what_is_needed_to_choose_and_no_more(self) -> None:
        """Enough to pick a layer and a field; not the whole configuration.

        The underlying response holds every layer in full. Returning that would
        answer "what is there?" by spending most of a context window, and the
        next question is always "and what is in it".
        """
        self.as_caller(caller())
        result = self.tool("layers_list")()
        self.assertEqual("rev-1", result["revision"])
        entries = {item["key"]: item for item in result["layers"]}
        self.assertEqual({"Census_OA_Population", "Bus_Stops"}, set(entries))
        census = entries["Census_OA_Population"]
        self.assertEqual("Census OA Population", census["name"])
        self.assertEqual("Census", census["group"])
        # The relation is the point: it is what catalog_list resolves to the
        # columns layers_values will actually accept.
        self.assertEqual(
            "derived_layers.census_oa_population_quintiles", census["table"]
        )
        # Display fields are named for what they are, never as "fields".
        self.assertEqual(["oa_id", "population_quintile"], census["displayFields"])
        self.assertNotIn("fields", census)
        self.assertNotIn("infoj", census)

    def test_a_geometry_entry_contributes_no_display_field(self) -> None:
        self.as_caller(caller())
        fields = self.tool("layers_list")()["layers"][0]["displayFields"]
        self.assertNotIn(None, fields)
        self.assertNotIn("", fields)

    def test_display_fields_are_never_presented_as_queryable(self) -> None:
        """The bug the first live call found.

        `infoj` describes what the map shows. `layers_values` accepts only a
        real selectable column, checked against `information_schema`. The index
        returned these as `fields`, the agent picked one, and the platform
        refused: "the field 'population_display' is not a selectable column on
        this layer". The names differ per layer, so nothing but the key name
        stops the two being confused again.
        """
        self.as_caller(caller())
        entry = self.tool("layers_list")()["layers"][0]
        self.assertIn("displayFields", entry)
        self.assertIn("table", entry)
        self.assertNotIn("fields", entry)

    def test_a_zoom_keyed_table_is_not_reported_as_a_relation(self) -> None:
        """A layer may map zoom levels to different relations. That shape is not
        queryable by `layers_values`, so reporting it as one relation would
        point the agent at a request that cannot work."""
        payload = {"layers": {"Bus_Stops": {
            "name": "Bus Stops", "table": {"0": None, "15": "source_ops.bus_stops"},
        }}}
        self.as_caller(caller())
        entry = self.tool("layers_list", payload=payload)()["layers"][0]
        self.assertIsNone(entry["table"])

    def test_the_listing_costs_only_the_discovery_scope(self) -> None:
        """A grant holding just the advertised pair can discover what exists.
        Needing `derive` to list would make discovery a privileged act."""
        exchange = FakeExchange()
        self.as_caller(caller(scopes="mcp:connect inspect"))
        self.tool("layers_list", exchange=exchange)()
        self.assertEqual("inspect", exchange.calls[0]["scope"])
        self.assertEqual("layers.list", exchange.calls[0]["operation_id"])

    def test_get_returns_one_layer_whole(self) -> None:
        self.as_caller(caller())
        result = self.tool("layers_get")(layer_key="Census_OA_Population")
        self.assertEqual("Census_OA_Population", result["key"])
        # The detail the index deliberately omits.
        self.assertEqual(
            "derived_layers.census_oa_population_quintiles", result["layer"]["table"]
        )

    def test_an_unknown_key_names_the_ones_that_exist(self) -> None:
        """A wrong key is the likeliest mistake, and the alternatives are
        already in hand -- making the agent call again to learn them wastes a
        turn and a credential."""
        self.as_caller(caller())
        with self.assertRaises(ToolError) as caught:
            self.tool("layers_get")(layer_key="Nope")
        message = str(caught.exception)
        self.assertIn("Nope", message)
        self.assertIn("Census_OA_Population", message)
        self.assertIn("Bus_Stops", message)

    def test_an_empty_workspace_is_reported_not_crashed(self) -> None:
        self.as_caller(caller())
        self.assertEqual([], self.tool("layers_list", payload={"layers": {}})()["layers"])
        with self.assertRaises(ToolError):
            self.tool("layers_get", payload={"layers": {}})(layer_key="anything")

    def test_an_unexpected_shape_degrades_rather_than_raising(self) -> None:
        """A downstream shape change should reach the agent as "nothing here",
        which it can report, rather than a crash it cannot."""
        for payload in ({}, {"layers": None}, {"layers": []}, {"layers": "x"}):
            with self.subTest(payload=payload):
                self.as_caller(caller())
                self.assertEqual([], self.tool("layers_list", payload=payload)()["layers"])


class ListingQueryTests(unittest.TestCase):
    """The query is digested byte for byte, so its shape is a contract."""

    def test_no_locale_is_no_parameter(self) -> None:
        """`locale=` is a different request from no locale, and is refused."""
        self.assertEqual("", layers_query(locale=None))

    def test_a_locale_is_encoded(self) -> None:
        self.assertEqual("locale=en%20GB", layers_query(locale="en GB"))


CATALOG = {
    "databases": ["MAPP"],
    "tables": [
        {"schema": "derived_layers", "table": "census_oa_population_quintiles",
         "columns": [{"name": "oa_id", "type": "text"},
                     {"name": "population_quintile", "type": "integer"},
                     {"name": "geom_3857", "type": "geometry"}]},
        {"schema": "source_ops", "table": "bus_stops",
         # A nameless entry, because the fixture without one let "drop columns
         # with no name" survive mutation: nothing exercised the guard.
         "columns": [{"name": "town", "type": "text"}, {"type": "text"}]},
    ],
}


class CatalogTests(ToolTestCase):
    """`catalog_list` is what makes a field name knowable rather than guessed."""

    def tool(self, payload=CATALOG, exchange=None):
        return self.build_named(
            "catalog_list", exchange=exchange,
            config_api=FakeConfigApi(answer=payload),
        )

    def test_it_reports_columns_with_their_types(self) -> None:
        """The column list is the whole point: `layers_values` accepts a column
        and nothing else, and before this the only way to learn one was to read
        the database by hand."""
        self.as_caller(caller())
        result = self.tool()()
        relations = {r["relation"]: r for r in result["relations"]}
        self.assertIn("derived_layers.census_oa_population_quintiles", relations)
        columns = relations["derived_layers.census_oa_population_quintiles"]["columns"]
        self.assertIn(
            {"name": "population_quintile", "type": "integer"}, columns
        )

    def test_it_filters_to_one_relation_qualified_or_bare(self) -> None:
        """Filtering here rather than in the agent, so a large deployment does
        not answer "which column?" with every column in the database."""
        self.as_caller(caller())
        for wanted in ("derived_layers.census_oa_population_quintiles",
                       "census_oa_population_quintiles",
                       "CENSUS_OA_POPULATION_QUINTILES"):
            with self.subTest(table=wanted):
                result = self.tool()(table=wanted)
                self.assertEqual(1, len(result["relations"]))

    def test_an_unknown_relation_names_the_ones_that_exist(self) -> None:
        self.as_caller(caller())
        with self.assertRaises(ToolError) as caught:
            self.tool()(table="nope")
        self.assertIn("source_ops.bus_stops", str(caught.exception))

    def test_it_costs_only_the_discovery_scope(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller(scopes="mcp:connect inspect"))
        self.tool(exchange=exchange)()
        self.assertEqual("inspect", exchange.calls[0]["scope"])
        self.assertEqual("catalog.list", exchange.calls[0]["operation_id"])

    def test_a_column_with_no_name_is_dropped(self) -> None:
        """A nameless column would reach the agent as {"name": null}, which it
        can only pass back to `layers_values` for a guaranteed refusal."""
        self.as_caller(caller())
        result = self.tool()(table="bus_stops")
        names = [c["name"] for c in result["relations"][0]["columns"]]
        self.assertEqual(["town"], names)
        self.assertNotIn(None, names)

    def test_an_unexpected_shape_degrades_rather_than_raising(self) -> None:
        for payload in ({}, {"tables": None}, {"tables": ["x"]}):
            with self.subTest(payload=payload):
                self.as_caller(caller())
                self.assertEqual([], self.tool(payload=payload)()["relations"])


class StatisticsTests(ToolTestCase):
    """The numeric counterpart to `layers_values`.

    Counting categories and summarising a distribution are different questions,
    and an agent holding only the first reaches for it on continuous data and
    gets thousands of distinct values back.
    """

    def tool(self, *, exchange=None, config_api=None):
        return self.build_named(
            "layers_statistics", exchange=exchange, config_api=config_api
        )

    def test_the_request_exchanged_for_is_the_request_made(self) -> None:
        exchange, config_api = FakeExchange(), FakeConfigApi()
        self.as_caller(caller())
        self.tool(exchange=exchange, config_api=config_api)(
            layer_key="Census_OA_Population", field="population", bins=5
        )
        minted, spent = exchange.calls[0], config_api.calls[0]
        self.assertEqual(minted["path"], spent["path"])
        self.assertEqual(minted["query"], spent["query"])
        self.assertEqual("layers.statistics", minted["operation_id"])

    def test_the_layer_key_is_encoded_into_the_path(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool(exchange=exchange)(layer_key="odd/key", field="f")
        self.assertEqual(
            "/api/layers/odd%2Fkey/statistics", exchange.calls[0]["path"]
        )

    def test_it_costs_the_same_scopes_as_reading_values(self) -> None:
        """Both read the data behind a layer, so neither is the cheaper way in."""
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool(exchange=exchange)(layer_key="L", field="f")
        self.assertEqual("derive semantic:inspect", exchange.calls[0]["scope"])

    def test_a_grant_without_derive_is_refused_before_anything_is_spent(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller(scopes="mcp:connect inspect"))
        with self.assertRaises(ToolError) as caught:
            self.tool(exchange=exchange)(layer_key="L", field="f")
        self.assertIn("derive", str(caught.exception))
        self.assertEqual([], exchange.calls)


class StatisticsQueryTests(unittest.TestCase):
    """The query is digested byte for byte, so its shape is a contract."""

    def test_the_order_is_fixed_regardless_of_argument_order(self) -> None:
        self.assertEqual(
            "field=f&locale=en&bins=5",
            layer_statistics_query(field="f", locale="en", bins=5),
        )

    def test_absent_optionals_are_absent_rather_than_empty(self) -> None:
        """`bins=` is a different request from no bins, and is refused."""
        self.assertEqual(
            "field=f", layer_statistics_query(field="f", locale=None, bins=None)
        )

    def test_a_space_travels_as_percent_twenty(self) -> None:
        """Not as `+`: the configuration API decodes with parse_qsl, which reads
        `+` as a space, so a literal plus would arrive as one."""
        self.assertEqual(
            "field=a%20b", layer_statistics_query(field="a b", locale=None, bins=None)
        )
        self.assertEqual(
            "field=a%2Bb", layer_statistics_query(field="a+b", locale=None, bins=None)
        )
