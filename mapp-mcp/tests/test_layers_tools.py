"""The layer tools' own decisions, with the platform stubbed out.

What it does with a broker and a configuration API is proved end to end against
the running stack. What is worth pinning here is the part that is this tool's
judgement: which requests it refuses before spending anything, and what it tells
a caller when the platform says no.
"""

from __future__ import annotations

import asyncio
import json
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
from config_api_client import limit_query  # noqa: E402
from config_api_client import semantic_search_query  # noqa: E402
from exchange_client import ExchangeRefused  # noqa: E402
from exchange_client import ExchangeUnavailable  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from protected_resource import ProtectedResource  # noqa: E402
from runtime import _without_meta  # noqa: E402
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
        self.calls.append({"method": "GET", **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.answer

    def post(self, **kwargs):
        # Recorded with its method, so a test can assert what was actually
        # sent rather than what the tool meant to send. Without the marker a
        # POST routed as a GET reads exactly like a correct call.
        self.calls.append({"method": "POST", **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.answer


#: `inspect` is in the default because any caller that can *see* a tool holds
#: it -- it is the listing scope -- so a caller without it is not a realistic
#: grant. Tests about a missing scope name the narrower set explicitly.
WIDE = (
    "mcp:connect inspect derive semantic:inspect federation:observe"
)
WIDEST = WIDE + " semantic:source"


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


CATALOGUE = {
    "catalogRevision": 53,
    "assets": [
        {"id": "asset-1", "curated": {
            "displayName": "Census OA Population",
            "description": "x" * 260,
            "tags": ["census", "population"],
            "fields": {"population_quintile": "Quintile of resident count."},
            "caveats": ["Drafted from a sample of 14 rows."],
        }},
        {"id": "asset-2", "curated": {"displayName": "Bus Stops"}},
    ],
}


# Cut from a live semantic_catalog_show against the deployed stack: `curated`
# keys its fields by field id while `generated` lists them by name, which is
# the join the tool now performs. An earlier fixture here keyed curated fields
# by name -- an invented shape, in a test whose own docstring warns about them.
ASSET = {
    "catalogRevision": 53,
    "asset": {
        "id": "asset-1",
        "createdAt": "2026-09-14T14:27:42.604Z",
        "curated": {
            "displayName": "Census OA Population",
            "description": "Resident counts per output area.",
            "tags": ["census"],
            "caveats": ["Drafted from a sample of 14 rows."],
            "fields": {
                "field:aaa": {"description": "Quintile of resident count."},
            },
        },
        "generated": {
            "name": "census_oa",
            "kind": "view",
            "qualifiedName": "public.census_oa",
            "binding": {"adapter": "postgresql", "schema": "public"},
            "fields": [
                {"id": "field:bbb", "name": "oa21cd", "type": "text",
                 "nullable": False, "primaryKey": True, "unique": True},
                {"id": "field:aaa", "name": "population_quintile",
                 "type": "integer", "nullable": True, "primaryKey": False,
                 "unique": False},
            ],
        },
    },
}


class SemanticCatalogTests(ToolTestCase):
    """Meaning, as distinct from shape.

    `catalog_list` says a relation has `population_quintile` of type integer.
    These say what a quintile means here and what the curator warned about it,
    which is the difference between reporting a number and reporting a number
    that means something.
    """

    def tool(self, name, payload=CATALOGUE, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def test_the_index_identifies_assets_without_carrying_their_meaning(self) -> None:
        """The stored asset holds curated meaning, generated drafts, source
        state and provenance. An index of eleven of those is a context window."""
        self.as_caller(caller())
        result = self.tool("semantic_catalog_list")()
        self.assertEqual(53, result["catalogRevision"])
        first = result["assets"][0]
        self.assertEqual("asset-1", first["assetId"])
        self.assertEqual("Census OA Population", first["name"])
        self.assertEqual(["census", "population"], first["tags"])
        # The meaning itself is semantic_catalog_show's job.
        self.assertNotIn("fields", first)
        self.assertNotIn("caveats", first)

    def test_a_long_description_is_truncated_in_the_index(self) -> None:
        """These run to paragraphs; the point of an index is that it fits."""
        self.as_caller(caller())
        description = self.tool("semantic_catalog_list")()["assets"][0]["description"]
        self.assertLessEqual(len(description), 201)
        self.assertTrue(description.endswith("…"))

    def test_an_asset_with_no_curated_meaning_still_lists(self) -> None:
        """A generated-but-uncurated asset is a normal state, not an error."""
        self.as_caller(caller())
        entry = self.tool("semantic_catalog_list", payload={"assets": [{"id": "a"}]})()
        self.assertEqual("a", entry["assets"][0]["assetId"])
        self.assertIsNone(entry["assets"][0]["name"])

    def test_show_passes_the_asset_through_whole(self) -> None:
        """Truncating here would remove the per-field meaning this exists for.

        The fixture is the shape the platform actually returns -- the asset
        wrapped alongside `catalogRevision` -- because the first version of this
        test invented an unwrapped one and passed against it while the live call
        returned something else. A fixture is a claim about the far side, and an
        invented one is a claim nobody checked.
        """
        self.as_caller(caller())
        result = self.tool("semantic_catalog_show", payload=ASSET)(asset_id="asset-1")
        # What identifies the asset survives whole; it is small.
        self.assertEqual("Census OA Population", result["displayName"])
        self.assertEqual(["Drafted from a sample of 14 rows."], result["caveats"])
        self.assertEqual("public.census_oa", result["relation"])
        # The revision travels with it: an agent quoting meaning should be able
        # to say which revision of the catalogue it read.
        self.assertEqual(53, result["catalogRevision"])
        # The fields are named, not described. On this instance the census
        # asset answers 110,987 bytes of field records; a real agent asked for
        # it, could not read the reply, and spawned a subagent to chunk it.
        self.assertEqual(["oa21cd", "population_quintile"], result["fields"])
        self.assertEqual(2, result["fieldCount"])
        self.assertEqual(1, result["curatedFieldCount"])
        self.assertNotIn("Quintile of resident count.", json.dumps(result))

    def test_one_field_joins_its_column_to_its_meaning(self) -> None:
        """The per-field meaning this tool exists for, still reachable -- and
        the join done here rather than by the caller. The generated records are
        a list keyed by name and the curated ones a map keyed by field id, so
        pairing a column with its meaning means matching one against the
        other."""
        self.as_caller(caller())
        detail = self.tool("semantic_catalog_show", payload=ASSET)(
            asset_id="asset-1", field="population_quintile")
        self.assertEqual("integer", detail["column"]["type"])
        self.assertEqual("Quintile of resident count.",
                         detail["meaning"]["description"])

    def test_a_field_with_no_curated_meaning_says_so(self) -> None:
        """470 generated fields against 50 curated ones on this instance, so
        most columns have no meaning recorded and that has to be visible."""
        self.as_caller(caller())
        detail = self.tool("semantic_catalog_show", payload=ASSET)(
            asset_id="asset-1", field="oa21cd")
        self.assertEqual("text", detail["column"]["type"])
        self.assertIsNone(detail["meaning"])

    def test_an_unknown_field_is_refused_by_name(self) -> None:
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.tool("semantic_catalog_show", payload=ASSET)(
                asset_id="asset-1", field="nope")
        self.assertIn("nope", str(raised.exception))

    def test_the_asset_id_is_encoded_into_the_path(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool("semantic_catalog_show", exchange=exchange)(asset_id="odd/id")
        self.assertEqual(
            "/api/semantic/catalog/objects/odd%2Fid", exchange.calls[0]["path"]
        )

    def test_all_three_cost_only_the_semantic_read_scope(self) -> None:
        """None of them reads data, so none should cost a data scope."""
        for name, kwargs in (
            ("semantic_catalog_list", {}),
            ("semantic_catalog_search", {"query": "population"}),
            ("semantic_catalog_show", {"asset_id": "a"}),
        ):
            with self.subTest(tool=name):
                exchange = FakeExchange()
                self.as_caller(caller(scopes="mcp:connect inspect semantic:inspect"))
                self.tool(name, exchange=exchange)(**kwargs)
                self.assertEqual("semantic:inspect", exchange.calls[0]["scope"])

    def test_an_unexpected_shape_degrades_rather_than_raising(self) -> None:
        for payload in ({}, {"assets": None}, {"assets": "x"}):
            with self.subTest(payload=payload):
                self.as_caller(caller())
                self.assertEqual(
                    [], self.tool("semantic_catalog_list", payload=payload)()["assets"]
                )


class SemanticSearchQueryTests(unittest.TestCase):
    def test_the_order_is_fixed_and_the_term_is_encoded(self) -> None:
        self.assertEqual(
            "q=air%20quality&limit=5",
            semantic_search_query(query="air quality", limit=5),
        )

    def test_an_absent_limit_is_absent_rather_than_empty(self) -> None:
        self.assertEqual("q=x", semantic_search_query(query="x", limit=None))

    def test_a_plus_is_not_a_space(self) -> None:
        self.assertEqual("q=a%2Bb", semantic_search_query(query="a+b", limit=None))


DERIVED = {"derivedLayers": [
    {"name": "census_oa_population_quintiles", "recipe": "quintile",
     "sources": ["source_ops.census_oa"], "refreshedAt": "2026-08-01T00:00:00Z"},
    {"name": "definitive_paths_length_costs", "recipe": "length-cost"},
]}

#: Shaped as the platform actually answers, including the fields an agent is
#: deliberately not given. A fixture without them could not show they are gone.
ALIASES = {
    "host": {"federationReady": True, "role": "mapp_federation", "database": "mapp"},
    "aliases": [
        {"alias": "census", "displayName": "Census 2021 (federated)",
         "kind": "postgresql", "status": "active", "groups": ["census"],
         "allowedRelations": ["leeds.census_2021_england_oa"],
         "registeredBy": "token:28fee603566a89e6",
         "approvedBy": "token:38e77aa97a8aab1f",
         "lastObservationId": 1882,
         "lastObservation": {
             "connectivity": "reachable", "schema": "current",
             "lastConnected": "2026-09-16T15:48:04Z", "sourceFreshness": "unknown",
             "schemaFingerprint": "cf0a770e" * 8,
             "extensionVersions": {"postgis": "3.5.7", "proj": "9.8.1 …"},
         }},
        {"alias": "ops", "status": "active"},
    ],
}


class DerivedLayerTests(ToolTestCase):
    """Provenance for the relations `catalog_list` reports the shape of.

    Most of this instance's layers read `derived_layers.*`, which are built
    rather than ingested, so "where did this number come from" has an answer
    only these can give.
    """

    def tool(self, name, payload=DERIVED, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def test_the_listing_is_passed_through(self) -> None:
        self.as_caller(caller())
        result = self.tool("derived_layers_list")()
        self.assertEqual(2, len(result["derivedLayers"]))

    def test_show_returns_one_entry(self) -> None:
        self.as_caller(caller())
        entry = self.tool("derived_layers_show")(name="census_oa_population_quintiles")
        self.assertEqual(["source_ops.census_oa"], entry["sources"])

    def test_an_unknown_name_lists_the_managed_relations(self) -> None:
        """The alternatives are already in hand; making the agent call again to
        learn them wastes a turn and a credential."""
        self.as_caller(caller())
        with self.assertRaises(ToolError) as caught:
            self.tool("derived_layers_show")(name="nope")
        message = str(caught.exception)
        self.assertIn("census_oa_population_quintiles", message)
        self.assertIn("definitive_paths_length_costs", message)

    def test_it_costs_only_the_discovery_scope(self) -> None:
        """Reading how a relation was built is configuration, not data."""
        exchange = FakeExchange()
        self.as_caller(caller(scopes="mcp:connect inspect"))
        self.tool("derived_layers_list", exchange=exchange)()
        self.assertEqual("inspect", exchange.calls[0]["scope"])

    def test_an_unexpected_shape_degrades_rather_than_raising(self) -> None:
        for payload in ({}, {"derivedLayers": None}, {"derivedLayers": "x"}):
            with self.subTest(payload=payload):
                self.as_caller(caller())
                with self.assertRaises(ToolError):
                    self.tool("derived_layers_show", payload=payload)(name="any")


class FederationTests(ToolTestCase):
    """Where data comes from when it does not come from here.

    Separated from the other reads by scope on purpose: these describe the
    platform's dependencies on other people's databases, which is a different
    disclosure from anything the instance's own configuration reveals.
    """

    def tool(self, name, payload=ALIASES, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def test_both_cost_the_read_scope_and_never_the_provisioning_one(self) -> None:
        """`federation:provision` is the only scope that can serve a third-party
        database. Looking at the registry must never require it."""
        shown = {"alias": {"alias": "census", "status": "active"}}
        for name, payload, kwargs in (
            ("federation_list", ALIASES, {}),
            ("federation_show", shown, {"alias": "census"}),
        ):
            with self.subTest(tool=name):
                exchange = FakeExchange()
                self.as_caller(
                    caller(scopes="mcp:connect inspect federation:observe")
                )
                self.tool(name, payload=payload, exchange=exchange)(**kwargs)
                self.assertEqual("federation:observe", exchange.calls[0]["scope"])
                self.assertNotIn("provision", exchange.calls[0]["scope"])

    def test_a_grant_without_the_federation_scope_is_refused(self) -> None:
        """It is not in the recommended preset, so most agents will not hold it
        and the message has to say which scope to ask for."""
        exchange = FakeExchange()
        self.as_caller(caller())
        with self.assertRaises(ToolError) as caught:
            self.tool("federation_list", exchange=exchange)()
        self.assertIn("federation:observe", str(caught.exception))
        self.assertEqual([], exchange.calls)

    def test_the_alias_is_encoded_into_the_path(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller(scopes="mcp:connect inspect federation:observe"))
        self.tool(
            "federation_show",
            payload={"alias": {"alias": "odd/alias"}},
            exchange=exchange,
        )(alias="odd/alias")
        self.assertEqual(
            "/api/federation/aliases/odd%2Falias", exchange.calls[0]["path"]
        )


class FederationDisclosureTests(ToolTestCase):
    """What a `federation:observe` grant is *not* a grant to read.

    The scope permits reading the source registry and the evidence behind each
    alias. It is not a grant to enumerate the operator credentials that acted on
    them, nor to learn this instance's own database role -- and the raw response
    carries both.
    """

    def tool(self, name, payload=ALIASES, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def test_credential_identifiers_never_reach_the_agent(self) -> None:
        """`registeredBy` and `approvedBy` name the token that acted. An agent
        asking which sources exist has no use for them, and they identify a
        person's credential."""
        self.as_caller(caller(scopes="mcp:connect inspect federation:observe"))
        shown = {"alias": dict(ALIASES["aliases"][0])}
        for name, payload, kwargs in (
            ("federation_list", ALIASES, {}),
            ("federation_show", shown, {"alias": "census"}),
        ):
            with self.subTest(tool=name):
                rendered = json.dumps(self.tool(name, payload=payload)(**kwargs))
                self.assertNotIn("registeredBy", rendered)
                self.assertNotIn("approvedBy", rendered)
                self.assertNotIn("28fee603566a89e6", rendered)

    def test_an_unrecognised_payload_is_refused_not_passed_through(self) -> None:
        """The failure this test class exists for.

        `federation_show` fell back to detailing the whole response when it
        found no alias record, so a shape it did not expect carried every
        withheld field to the agent. Withholding that depends on the response
        being the shape you assumed is not withholding.
        """
        self.as_caller(caller(scopes="mcp:connect inspect federation:observe"))
        for payload in (ALIASES, {}, {"alias": "not-a-record"}):
            with self.subTest(payload=str(payload)[:40]):
                with self.assertRaises(ToolError):
                    self.tool("federation_show", payload=payload)(alias="census")

    def test_the_instance_s_own_role_and_database_are_not_disclosed(self) -> None:
        """`host` answers whether federation works. Its role and database name
        describe this deployment, not the federated sources."""
        self.as_caller(caller(scopes="mcp:connect inspect federation:observe"))
        result = self.tool("federation_list")()
        self.assertTrue(result["federationReady"])
        rendered = json.dumps(result)
        self.assertNotIn("mapp_federation", rendered)
        self.assertNotIn("\"database\"", rendered)

    def test_schema_fingerprints_and_extension_versions_are_dropped(self) -> None:
        """Hundreds of characters per alias answering nothing an agent can act
        on, which push the useful fields out of the window."""
        self.as_caller(caller(scopes="mcp:connect inspect federation:observe"))
        rendered = json.dumps(self.tool("federation_list")())
        self.assertNotIn("schemaFingerprint", rendered)
        self.assertNotIn("extensionVersions", rendered)

    def test_what_the_agent_does_get_is_enough_to_answer_with(self) -> None:
        """Withholding is only defensible if the useful part survives."""
        self.as_caller(caller(scopes="mcp:connect inspect federation:observe"))
        entry = self.tool("federation_list")()["aliases"][0]
        self.assertEqual("census", entry["alias"])
        self.assertEqual("active", entry["status"])
        self.assertEqual(["leeds.census_2021_england_oa"], entry["allowedRelations"])
        self.assertEqual("reachable", entry["observation"]["connectivity"])

    def test_show_keeps_the_record_minus_what_is_withheld(self) -> None:
        detail = {"alias": {"alias": "census", "tlsPolicy": "require",
                            "registeredBy": "token:x",
                            "lastObservation": {"connectivity": "reachable",
                                                "schemaFingerprint": "abc"}}}
        self.as_caller(caller(scopes="mcp:connect inspect federation:observe"))
        result = self.tool("federation_show", payload=detail)(alias="census")
        self.assertEqual("require", result["tlsPolicy"])
        self.assertEqual("reachable", result["observation"]["connectivity"])
        self.assertNotIn("registeredBy", json.dumps(result))
        self.assertNotIn("schemaFingerprint", json.dumps(result))


QUEUE = {"proposals": [
    {"id": "p-1", "status": "applied", "created": "2026-09-14T14:28:41Z",
     "actor": "[withheld]", "explanation": "y" * 260,
     "candidateHash": "abc", "originalRevision": "rev", 
     "pluginCatalogueFingerprint": "fp"},
    {"id": "p-2", "status": "pending", "created": "2026-09-02T17:05:47Z",
     "explanation": "Rename the sample layer."},
]}


class ProposalQueueTests(ToolTestCase):
    """The review queue: the half of propose-review-apply that alters nothing.

    A workspace is not edited directly -- a change is proposed, a person reviews
    it, and applying it is a separate act. Reading the queue says what is
    waiting and what landed, and that is all this surface offers.
    """

    def tool(self, name, payload=QUEUE, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def test_the_index_keeps_the_explanation_and_drops_the_integrity_material(
        self,
    ) -> None:
        """The explanation is why a proposal exists and is what a person reads
        first. The hashes and fingerprints answer nothing an agent can act on."""
        self.as_caller(caller())
        entry = self.tool("proposals_list")()["proposals"][0]
        self.assertEqual("p-1", entry["proposalId"])
        self.assertEqual("applied", entry["status"])
        self.assertTrue(entry["explanation"].startswith("y"))
        for dropped in ("candidateHash", "originalRevision",
                        "pluginCatalogueFingerprint"):
            self.assertNotIn(dropped, entry)

    def test_a_long_explanation_is_truncated(self) -> None:
        self.as_caller(caller())
        explanation = self.tool("proposals_list")()["proposals"][0]["explanation"]
        self.assertLessEqual(len(explanation), 201)
        self.assertTrue(explanation.endswith("…"))

    def test_status_filters_without_a_second_round_trip(self) -> None:
        """The API offers no status parameter, so the alternative is the agent
        fetching everything and filtering, which costs it a context window."""
        self.as_caller(caller())
        pending = self.tool("proposals_list")(status="pending")["proposals"]
        self.assertEqual(["p-2"], [p["proposalId"] for p in pending])

    def test_limit_is_passed_to_the_platform_not_applied_afterwards(self) -> None:
        """Bounding after the fact still transfers the whole queue; this
        instance already holds 81."""
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool("proposals_list", exchange=exchange)(limit=5)
        self.assertEqual("limit=5", exchange.calls[0]["query"])

    def test_reading_the_queue_never_costs_the_proposing_scope(self) -> None:
        """`propose` is what it costs to add to the queue. A read tool that
        asked for it would make looking indistinguishable from writing."""
        exchange = FakeExchange()
        self.as_caller(caller(scopes="mcp:connect inspect"))
        self.tool("proposals_list", exchange=exchange)()
        self.assertEqual("inspect", exchange.calls[0]["scope"])
        self.assertNotIn("propose", exchange.calls[0]["scope"])

    def test_the_semantic_pair_costs_only_the_semantic_read_scope(self) -> None:
        for name, kwargs in (
            ("semantic_proposals_list", {}),
            ("semantic_proposals_show", {"proposal_id": "sp-1"}),
        ):
            with self.subTest(tool=name):
                exchange = FakeExchange()
                self.as_caller(caller(scopes="mcp:connect inspect semantic:inspect"))
                self.tool(name, exchange=exchange)(**kwargs)
                self.assertEqual("semantic:inspect", exchange.calls[0]["scope"])

    def test_the_proposal_id_is_encoded_into_the_path(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool("semantic_proposals_show", exchange=exchange)(proposal_id="odd/id")
        self.assertEqual(
            "/api/semantic/proposals/odd%2Fid", exchange.calls[0]["path"]
        )

    def test_an_unexpected_shape_degrades_rather_than_raising(self) -> None:
        for payload in ({}, {"proposals": None}, {"proposals": "x"}):
            with self.subTest(payload=payload):
                self.as_caller(caller())
                self.assertEqual(
                    [], self.tool("proposals_list", payload=payload)()["proposals"]
                )


# Cut from var/control/proposals/*/proposal.json rather than composed here: the
# stored record carries `original` and `candidate` as well as `diff`, and every
# one of `operations`, `diff[].value` holds whole layer definitions. A fixture
# that omitted those would agree with the summariser about a bulk that is not
# there, which is the failure this file has hit before.
DETAIL = {"proposal": {
    "id": "1786156792-af56c7e17281-da498c",
    "status": "pending",
    "created": "2026-09-14T14:28:41Z",
    "actor": "[withheld]",
    "explanation": "e" * 260,
    "originalRevision": "rev-9",
    "originalHash": "0" * 64,
    "candidateHash": "1" * 64,
    "pluginCatalogueFingerprint": "fp",
    "original": {"locale": {"layers": {"Existing": {"name": "snapshot-only"}}}},
    "candidate": {"locale": {"layers": {"Existing": {"name": "snapshot-only"},
                                        "Arrivals": {"name": "snapshot-only"}}}},
    "warnings": ["The relation has no spatial index."],
    "operations": [
        {"op": "set", "path": "/locale/layers/Arrivals",
         "value": {"name": "Arrivals", "table": "derived_layers.arrivals_oa"}},
        {"op": "remove", "path": "/locale/layers/Old"},
        {"op": "set", "path": "/locale/layers/Existing/display", "value": False},
    ],
    "diff": [
        {"op": "add", "path": "/locale/layers/Arrivals", "old": None,
         "value": {"name": "Arrivals", "display": True, "format": "mvt",
                   "dbs": "MAPP", "table": "derived_layers.arrivals_oa",
                   "geom": "geom_3857", "srid": "3857", "qID": "oa21cd",
                   "opacity": 1, "infoj": [{"field": "oa21cd"}]}},
        {"op": "replace", "path": "/locale/layers/Existing/display",
         "old": True, "value": False},
    ],
}}


class ProposalDetailTests(ToolTestCase):
    """What a proposal changes, which is the read a review is made on.

    The queue says a change is waiting. The decision about it is made on its
    diff, and before this tool there was no path from an MCP credential to one:
    the endpoint existed and no allowlist named it.
    """

    def tool(self, payload=DETAIL, exchange=None):
        return self.build_named(
            "proposals_show", exchange=exchange,
            config_api=FakeConfigApi(answer=payload),
        )

    def test_the_change_paths_are_reported(self) -> None:
        self.as_caller(caller())
        changes = self.tool()(proposal_id="p-1")["changes"]
        self.assertEqual(
            [("add", "/locale/layers/Arrivals"),
             ("replace", "/locale/layers/Existing/display")],
            [(c["op"], c["path"]) for c in changes],
        )

    def test_a_scalar_change_reports_both_values(self) -> None:
        """"display became false" is the answer, not a description of it."""
        self.as_caller(caller())
        change = self.tool()(proposal_id="p-1")["changes"][1]
        self.assertEqual(True, change["was"])
        self.assertEqual(False, change["becomes"])

    def test_a_structured_change_reports_its_keys_not_its_contents(self) -> None:
        """Which fields a layer gains is the question; the layer itself is the
        44KB this tool exists to not send."""
        self.as_caller(caller())
        change = self.tool()(proposal_id="p-1")["changes"][0]
        self.assertIsNone(change["was"])
        self.assertEqual("object", change["becomes"]["type"])
        self.assertIn("table", change["becomes"]["keys"])
        self.assertNotIn("derived_layers.arrivals_oa", json.dumps(change))

    def test_the_workspace_snapshots_never_reach_the_agent(self) -> None:
        """`original` and `candidate` are the whole workspace twice, and the
        platform already derived `diff` from them."""
        self.as_caller(caller())
        detail = self.tool()(proposal_id="p-1")
        for dropped in ("original", "candidate"):
            self.assertNotIn(dropped, detail)
        # Their contents, not merely their keys: a changed path legitimately
        # names a layer, so the sentinel lives only inside the snapshots.
        self.assertNotIn("snapshot-only", json.dumps(detail))

    def test_the_integrity_material_is_dropped(self) -> None:
        """It answers whether the record is intact, which is the platform's
        question at apply time and not one an agent can act on."""
        self.as_caller(caller())
        detail = self.tool()(proposal_id="p-1")
        for dropped in ("originalHash", "candidateHash",
                        "pluginCatalogueFingerprint"):
            self.assertNotIn(dropped, detail)

    def test_operations_become_a_count_because_they_restate_the_diff(self) -> None:
        """A stored operation carries the value it would write (40,263 bytes
        here), and across all 81 stored proposals its path sequence is the
        diff's. The count survives because a divergence would show up in it."""
        self.as_caller(caller())
        detail = self.tool()(proposal_id="p-1")
        # Three against two changes, so a count taken from the diff fails here.
        self.assertEqual(3, detail["operationCount"])
        self.assertNotEqual(len(detail["changes"]), detail["operationCount"])
        self.assertNotIn("operations", detail)
        self.assertNotIn("/locale/layers/Old", json.dumps(detail))

    def test_the_whole_explanation_survives_unlike_in_the_queue(self) -> None:
        """The list truncates at 200 because a queue is scanned. This is the
        read where the whole reason is the point."""
        self.as_caller(caller())
        self.assertEqual(260, len(self.tool()(proposal_id="p-1")["explanation"]))

    def test_the_warnings_raised_against_it_survive(self) -> None:
        self.as_caller(caller())
        self.assertEqual(
            ["The relation has no spatial index."],
            self.tool()(proposal_id="p-1")["warnings"],
        )

    def test_one_change_can_be_expanded_in_full(self) -> None:
        """A shape says which fields a layer gains, not what they become."""
        self.as_caller(caller())
        detail = self.tool()(proposal_id="p-1",
                             path="/locale/layers/Arrivals")["change"]
        self.assertEqual("derived_layers.arrivals_oa", detail["becomes"]["table"])
        self.assertIsNone(detail["was"])

    def test_expanding_is_opt_in_so_the_default_read_stays_compact(self) -> None:
        self.as_caller(caller())
        self.assertNotIn("change", self.tool()(proposal_id="p-1"))

    def test_an_unchanged_path_is_refused_by_name(self) -> None:
        """Returning an empty expansion would read as "this proposal changes
        nothing there" and as "you asked for the wrong path" identically."""
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.tool()(proposal_id="p-1", path="/locale/layers/Absent")
        self.assertIn("/locale/layers/Absent", str(raised.exception))

    def test_the_proposal_id_is_encoded_into_the_path(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool(exchange=exchange)(proposal_id="odd/id")
        self.assertEqual("/api/proposals/odd%2Fid", exchange.calls[0]["path"])

    def test_reading_a_proposal_never_costs_the_proposing_scope(self) -> None:
        """Looking at a change and being able to make one are different
        authorities, and this tool asks only for the first."""
        exchange = FakeExchange()
        self.as_caller(caller(scopes="mcp:connect inspect"))
        self.tool(exchange=exchange)(proposal_id="p-1")
        self.assertEqual("inspect", exchange.calls[0]["scope"])

    def test_an_unexpected_shape_degrades_rather_than_raising(self) -> None:
        for payload in ({}, {"proposal": None}, {"proposal": {"diff": "x"}},
                        {"proposal": {"operations": 3}}):
            with self.subTest(payload=payload):
                self.as_caller(caller())
                detail = self.tool(payload=payload)(proposal_id="p-1")
                self.assertEqual([], detail["changes"])
                self.assertEqual(0, detail["operationCount"])

    def test_a_malformed_change_entry_is_named_rather_than_dropped(self) -> None:
        """An unexpected value shape should be visible, not silently null."""
        self.as_caller(caller())
        payload = {"proposal": {"diff": [{"op": "add", "path": "/x",
                                          "value": {1, 2}}]}}
        change = self.tool(payload=payload)(proposal_id="p-1")["changes"][0]
        self.assertEqual({"type": "set"}, change["becomes"])

    def test_a_long_string_value_is_truncated(self) -> None:
        self.as_caller(caller())
        payload = {"proposal": {"diff": [{"op": "replace", "path": "/x",
                                          "old": None, "value": "z" * 300}]}}
        becomes = self.tool(payload=payload)(proposal_id="p-1")["changes"][0]["becomes"]
        self.assertEqual(121, len(becomes))
        self.assertTrue(becomes.endswith("…"))

    def test_a_wide_object_reports_how_many_keys_it_withheld(self) -> None:
        """Silently showing 20 of 30 keys reads as a layer with 20 fields."""
        self.as_caller(caller())
        value = {f"field{n:02d}": n for n in range(30)}
        payload = {"proposal": {"diff": [{"op": "add", "path": "/x",
                                          "old": None, "value": value}]}}
        becomes = self.tool(payload=payload)(proposal_id="p-1")["changes"][0]["becomes"]
        self.assertEqual(20, len(becomes["keys"]))
        self.assertEqual(10, becomes["truncated"])


# Every fixture below is cut from a live response captured through
# config.localhost against the deployed stack, key for key. Composing them here
# would mean the summarisers agree with an invented shape -- the failure this
# file has hit more than once.
STATUS = {
    "ok": True, "serviceVersion": "0.4.0", "schemaVersion": 3,
    "catalogRevision": 53,
    "capabilities": {"catalog": True, "search": True, "proposals": True,
                     "curatedProposals": True, "derivedProfiles": True,
                     "generatedEvents": ["created", "updated"],
                     "pagination": {"maxLimit": 200}},
    "meta": {"requestId": "503aa83b584c98781c320443fef3719a"},
}
PROFILES = {
    "derivedProfiles": [
        {"name": "census_oa_country_birth_categories",
         "relation": "derived_layers.census_oa_country_birth_categories",
         "kind": "view", "assetId": "440c4b84-4c84-4452-acff-7f4cbb9ca1bd",
         "generation": 1, "status": "ready", "revision": "46"},
    ],
    "catalogRevision": 53, "deliveryBlockers": [], "deliveryBlockersMore": False,
    "meta": {"requestId": "r1"},
}
PROFILE = {
    "catalogRevision": 53,
    "derivedProfile": {"name": "census_oa_country_birth_categories",
                       "relation": "derived_layers.census_oa_country_birth_categories",
                       "kind": "view", "status": "ready"},
    "meta": {"requestId": "r2"},
}
HISTORY = {
    "assetId": "440c4b84-4c84-4452-acff-7f4cbb9ca1bd",
    "catalogRevision": 53,
    "history": [
        {"eventId": "ev-1", "changedAt": "2026-09-14T14:27:42.604Z",
         "changeType": "generated", "version": 1, "generation": 1,
         "proposalId": None, "catalogRevision": 46,
         "actor": "[withheld]",
         # The platform stores a snapshot of the asset at each event; this is
         # the bulk the summariser exists to drop.
         "asset": {"createdAt": "2026-09-14T14:27:42.604Z", "curated": {},
                   "generated": {"binding": {"adapter": "postgresql",
                                             "relation": "census_oa_country_birth_categories",
                                             "schema": "derived_layers"},
                                 "definitionDigest": "5abff3e0" * 8}}},
    ],
    "meta": {"requestId": "r3"},
}
JOBS = {
    "backgroundJobs": {"observedAt": "2026-09-18T09:00:00Z", "activeJobs": 0,
                       "maxActiveJobs": 2, "executingJobs": 0,
                       "waitingJobs": 0, "activeOperations": []},
    "meta": {"requestId": "r4"},
}
EXTENT = {
    "spatialScope": {"type": "workspace-map-extent", "locale": "locale",
                     "crs": "EPSG:4326", "scopeZoom": 10,
                     "envelopes": [{"west": -1.85, "south": 53.65,
                                    "east": -1.2, "north": 54.0}],
                     "selection": "intersects-output-geometry",
                     "clipsGeometry": False},
    "meta": {"requestId": "r5"},
}
GROUPS = {
    "groups": [{"name": "census", "description": "Census sources",
                "createdBy": "[withheld]", "createdAt": "2026-09-01T00:00:00Z",
                "memberCount": 3}],
    "meta": {"requestId": "r6"},
}


RELATIONS = {
    "relations": [
        {"alias": "MAPP", "schema": "source_census",
         "relation": "census_2021_england_oa", "kind": "foreign-table",
         "assetId": "7fd220ac-8671-58db-8bf9-c6cfec31cbf2"},
    ],
    "meta": {"requestId": "r7"},
}


class SourceInventoryTests(ToolTestCase):
    """What could be modelled, as opposed to what has been.

    Every other read describes the workspace as configured. This names
    relations that exist and were never used, which is the question behind "is
    there data for X" -- and it sits behind its own scope because it discloses
    the database's inventory rather than the platform's configuration.
    """

    def tool(self, payload=RELATIONS, exchange=None):
        return self.build_named(
            "semantic_source_relations", exchange=exchange,
            config_api=FakeConfigApi(answer=payload),
        )

    def test_it_reports_the_relation_and_where_it_lives(self) -> None:
        self.as_caller(caller(scopes=WIDEST))
        entry = self.tool()()["relations"][0]
        self.assertEqual("source_census", entry["schema"])
        self.assertEqual("census_2021_england_oa", entry["relation"])
        self.assertEqual("MAPP", entry["alias"])

    def test_it_drops_the_request_envelope(self) -> None:
        self.as_caller(caller(scopes=WIDEST))
        self.assertNotIn("meta", self.tool()())

    def test_it_costs_both_scopes_the_platform_demands(self) -> None:
        """The configuration API lists semantic:inspect alongside
        semantic:source, so a credential minted for only the latter is refused
        by the platform after the exchange has already succeeded."""
        exchange = FakeExchange()
        self.as_caller(caller(scopes=WIDEST))
        self.tool(exchange=exchange)()
        self.assertEqual("semantic:inspect semantic:source",
                         exchange.calls[0]["scope"])

    def test_it_is_refused_without_the_source_scope(self) -> None:
        """A grant good for every other semantic read must not reach this one:
        the inventory is disclosed separately from the catalogue."""
        self.as_caller(caller(scopes="mcp:connect inspect semantic:inspect"))
        with self.assertRaises(ToolError) as raised:
            self.tool()()
        self.assertIn("semantic:source", str(raised.exception))

    def test_it_never_returns_a_row_from_a_relation(self) -> None:
        """It names relations and reads nothing from them; layers_values stays
        the only tool that returns values, and only over configured layers."""
        self.as_caller(caller(scopes=WIDEST))
        detail = self.tool()()
        self.assertEqual({"relations"}, set(detail))
        self.assertEqual({"alias", "schema", "relation", "kind", "assetId"},
                         set(detail["relations"][0]))


class InstanceStateToolTests(ToolTestCase):
    """The reads that say what state the platform is in rather than what it
    holds: is the semantic service available, is background work running, what
    ground derived layers are bounded by, and how a meaning got to be what it
    is."""

    def tool(self, name, payload, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def test_the_request_envelope_is_dropped_by_every_new_tool(self) -> None:
        """`meta.requestId` identifies the HTTP call in the platform's logs and
        answers nothing an agent asked."""
        for name, payload, kwargs in (
            ("semantic_status", STATUS, {}),
            ("semantic_derived_profiles_list", PROFILES, {}),
            ("semantic_derived_profiles_show", PROFILE, {"name": "p"}),
            ("derived_layers_jobs", JOBS, {}),
            ("derived_layers_map_extent", EXTENT, {}),
            ("federation_groups", GROUPS, {}),
        ):
            with self.subTest(tool=name):
                self.as_caller(caller(scopes=WIDE))
                detail = self.tool(name, payload)(**kwargs)
                self.assertNotIn("meta", detail)
                self.assertNotIn("requestId", json.dumps(detail))

    def test_status_reports_availability_and_capabilities(self) -> None:
        """Optional capabilities are why this exists: without it an agent
        learns search is unavailable by calling it and reading a refusal."""
        self.as_caller(caller())
        detail = self.tool("semantic_status", STATUS)()
        self.assertIs(True, detail["ok"])
        self.assertIs(True, detail["capabilities"]["search"])
        self.assertEqual(53, detail["catalogRevision"])

    def test_profiles_carry_the_relation_and_its_asset(self) -> None:
        """This is the join: derived_layers_list names a relation, the catalog
        tools describe an asset, and nothing else says they are the same."""
        self.as_caller(caller())
        entry = self.tool("semantic_derived_profiles_list", PROFILES)()["derivedProfiles"][0]
        self.assertEqual("derived_layers.census_oa_country_birth_categories",
                         entry["relation"])
        self.assertEqual("440c4b84-4c84-4452-acff-7f4cbb9ca1bd", entry["assetId"])

    def test_history_keeps_the_change_and_drops_the_asset_snapshot(self) -> None:
        """Each stored event embeds the asset as it then was, so a history
        costs the full record once per event; the current one is one call
        away via semantic_catalog_show."""
        self.as_caller(caller())
        detail = self.tool("semantic_catalog_history", HISTORY)(asset_id="a-1")
        entry = detail["history"][0]
        self.assertEqual("generated", entry["changeType"])
        self.assertEqual("ev-1", entry["eventId"])
        self.assertNotIn("asset", entry)
        self.assertNotIn("definitionDigest", json.dumps(detail))
        self.assertEqual("440c4b84-4c84-4452-acff-7f4cbb9ca1bd", detail["assetId"])

    def test_history_keeps_the_actor_the_platform_already_withheld(self) -> None:
        """Dropping it here would hide that the platform redacts it, and the
        decision belongs at the configuration API, not in each tool."""
        self.as_caller(caller())
        detail = self.tool("semantic_catalog_history", HISTORY)(asset_id="a-1")
        self.assertEqual("[withheld]", detail["history"][0]["actor"])

    def test_jobs_report_the_queue_and_its_ceiling(self) -> None:
        """Refresh is asynchronous; without the queue an agent cannot tell a
        slow job from one that never started."""
        self.as_caller(caller())
        jobs = self.tool("derived_layers_jobs", JOBS)()["backgroundJobs"]
        self.assertEqual(0, jobs["executingJobs"])
        self.assertEqual(2, jobs["maxActiveJobs"])

    def test_map_extent_reports_the_envelope_and_its_crs(self) -> None:
        """A derived layer missing data outside a region is usually bounded
        rather than broken, and that is invisible from the layer."""
        self.as_caller(caller())
        scope = self.tool("derived_layers_map_extent", EXTENT)()["spatialScope"]
        self.assertEqual("EPSG:4326", scope["crs"])
        self.assertEqual(-1.85, scope["envelopes"][0]["west"])

    def test_groups_report_membership_counts(self) -> None:
        self.as_caller(caller(scopes=WIDE))
        group = self.tool("federation_groups", GROUPS)()["groups"][0]
        self.assertEqual("census", group["name"])
        self.assertEqual(3, group["memberCount"])

    def test_each_tool_costs_exactly_its_declared_scope(self) -> None:
        """A read tool that asked for more than it needs makes looking
        indistinguishable from acting."""
        for name, payload, kwargs, scope in (
            ("semantic_status", STATUS, {}, "semantic:inspect"),
            ("semantic_derived_profiles_list", PROFILES, {}, "semantic:inspect"),
            ("semantic_derived_profiles_show", PROFILE, {"name": "p"}, "semantic:inspect"),
            ("semantic_catalog_history", HISTORY, {"asset_id": "a"}, "semantic:inspect"),
            ("derived_layers_jobs", JOBS, {}, "inspect"),
            ("derived_layers_map_extent", EXTENT, {}, "inspect"),
            ("federation_groups", GROUPS, {}, "federation:observe"),
        ):
            with self.subTest(tool=name):
                exchange = FakeExchange()
                self.as_caller(caller(scopes=WIDE))
                self.tool(name, payload, exchange=exchange)(**kwargs)
                self.assertEqual(scope, exchange.calls[0]["scope"])

    def test_federation_groups_is_refused_without_the_observe_scope(self) -> None:
        """It names the third-party sources an operator grouped, so it sits
        behind the same scope as the alias list rather than plain inspect."""
        self.as_caller(caller(scopes="mcp:connect inspect semantic:inspect"))
        with self.assertRaises(ToolError) as raised:
            self.tool("federation_groups", GROUPS)()
        self.assertIn("federation:observe", str(raised.exception))

    def test_the_identifier_is_encoded_into_the_path(self) -> None:
        for name, kwargs, expected in (
            ("semantic_derived_profiles_show", {"name": "odd/name"},
             "/api/semantic/derived-profiles/odd%2Fname"),
            ("semantic_catalog_history", {"asset_id": "odd/id"},
             "/api/semantic/catalog/objects/odd%2Fid/history"),
        ):
            with self.subTest(tool=name):
                exchange = FakeExchange()
                self.as_caller(caller())
                self.tool(name, PROFILE if "profiles" in name else HISTORY,
                          exchange=exchange)(**kwargs)
                self.assertEqual(expected, exchange.calls[0]["path"])

    def test_an_unexpected_shape_degrades_rather_than_raising(self) -> None:
        for payload in ({}, {"history": None}, {"history": "x"}):
            with self.subTest(payload=payload):
                self.as_caller(caller())
                detail = self.tool("semantic_catalog_history", payload)(asset_id="a")
                self.assertEqual([], detail["history"])

    def test_a_response_that_is_not_an_object_degrades_to_an_empty_one(self) -> None:
        """Unguarded this raises AttributeError, which the SDK reports as
        "Error executing tool" with a traceback rather than as a refusal."""
        for payload in ([], "x", 3):
            with self.subTest(payload=payload):
                self.as_caller(caller())
                self.assertEqual({}, self.tool("semantic_status", payload)())
        # A JSON `null` body reaches the helper as None, which the config-api
        # fake cannot express -- it reads a None answer as "use the default".
        self.assertEqual({}, _without_meta(None))


# Cut from live responses through config.localhost, as everything else here is.
XYZ = {"requestedGeneration": 65, "appliedGeneration": 64,
       "workspaceFingerprint": "4a93161e", "startedAt": "2026-09-17T21:28:13.045Z",
       "healthy": True, "meta": {"requestId": "r8"}}
OPERATION = {"operation": {
    "id": "1182d109999bdf48d63e1141f2aa6af6",
    "kind": "derived-layer.create", "status": "cancelled", "stage": None,
    "actor": "[withheld]",
    "target": {"name": "bus_stop_buffer_population", "action": "create"},
    "created": "2026-09-01T21:47:59.199341Z",
    "updated": "2026-09-01T21:52:02.000000Z",
    # A failed visual test carries 15,381 bytes here and 6,174 in diagnosis,
    # against 34 bytes of message saying what actually went wrong.
    "result": {"source": "live", "error": "Browser validation did not pass.",
               "visual": {"runId": "2026-08-08", "frames": [1, 2, 3]},
               "plan": {"layer": "X", "layerTitle": "Y"}},
    "error": {"code": "visual.failed",
              "message": "Browser validation did not pass.",
              "diagnosis": {"outcome": "failed", "checks": [{"id": "visual.http"}]}},
}, "meta": {"requestId": "r9"}}
CAPABILITIES = {
    "apiVersion": "1", "contractVersion": "1", "instanceId": "abc",
    "actions": [
        {"id": "catalog.list", "method": "GET", "pathTemplate": "/api/catalog",
         "risk": "inspect", "scope": "inspect"},
        {"id": "derived-layers.create", "method": "POST",
         "path": "/api/derived-layers", "risk": "derive", "scope": "derive",
         # The bulk: the largest real entry is 2,829 bytes, almost all schema.
         "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    ],
    "meta": {"requestId": "r10"},
}
SCHEMA_DOC = {"schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "workspace.schema.json", "title": "GEOLYTIX XYZ workspace",
    "type": "object", "additionalProperties": False,
    "properties": {"locale": {"type": "object"}},
    # 30 definitions at 34,600 bytes against 838 for properties.
    "$defs": {"layer": {"type": "object"}, "color": {"type": "string"}},
}, "meta": {"requestId": "r11"}}


class ContractAndProgressToolTests(ToolTestCase):
    """The contract an agent authors against, and the progress of work it
    cannot otherwise follow."""

    def tool(self, name, payload, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def test_xyz_status_exposes_the_generation_gap(self) -> None:
        """A workspace change is not live until the tile service reloads, and
        until then a stale map looks exactly like a failed change."""
        self.as_caller(caller())
        detail = self.tool("xyz_status", XYZ)()
        self.assertEqual(65, detail["requestedGeneration"])
        self.assertEqual(64, detail["appliedGeneration"])

    def test_an_operations_bulk_is_reduced_to_its_shape(self) -> None:
        """Kept as shape rather than dropped, so the reduction is visible: an
        agent can see a `visual` block exists and how many keys it has."""
        self.as_caller(caller())
        detail = self.tool("operations_show", OPERATION)(operation_id="op-1")
        self.assertEqual("derived-layer.create", detail["kind"])
        # Scalars survive; containers become their shape.
        self.assertEqual("Browser validation did not pass.",
                         detail["result"]["error"])
        self.assertEqual("object", detail["result"]["visual"]["type"])
        self.assertIn("frames", detail["result"]["visual"]["keys"])
        self.assertEqual(["frames", "runId"], detail["result"]["visual"]["keys"])
        self.assertNotIn("2026-08-08", json.dumps(detail))
        self.assertEqual("visual.failed", detail["error"]["code"])
        self.assertEqual("object", detail["error"]["diagnosis"]["type"])

    def test_an_operation_costs_derive_not_inspect(self) -> None:
        """The route admits any credential and then demands the scope the
        operation's kind would have cost. An exchanged credential carries only
        what is declared here, so `inspect` would make this unusable for every
        kind; `derive` is exactly the set an agent could have caused."""
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool("operations_show", OPERATION, exchange=exchange)(operation_id="o")
        self.assertEqual("derive", exchange.calls[0]["scope"])

    def test_the_contract_is_indexed_rather_than_returned(self) -> None:
        """57 actions come to 35,603 bytes, nearly all of it input schemas."""
        self.as_caller(caller())
        detail = self.tool("capabilities_list", CAPABILITIES)()
        self.assertEqual(["catalog.list", "derived-layers.create"],
                         [a["id"] for a in detail["actions"]])
        self.assertNotIn("inputSchema", json.dumps(detail))
        # The path is reported whichever key the platform used.
        self.assertEqual("/api/derived-layers", detail["actions"][1]["path"])

    def test_one_action_can_be_expanded_with_its_schema(self) -> None:
        self.as_caller(caller())
        action = self.tool("capabilities_list", CAPABILITIES)(
            action="derived-layers.create")["action"]
        self.assertIn("inputSchema", action)

    def test_an_unknown_action_is_refused_by_name(self) -> None:
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.tool("capabilities_list", CAPABILITIES)(action="nope")
        self.assertIn("nope", str(raised.exception))

    def test_a_tool_name_finds_the_action_it_calls(self) -> None:
        """The predictable near miss: the tool is `sql_test` and the action is
        `sql.test`. A real client passed the former during acceptance, read a
        refusal that named no alternative, and gave up instead of retrying."""
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.tool("capabilities_list", CAPABILITIES)(
                action="derived_layers_create")
        self.assertIn("Did you mean 'derived-layers.create'?",
                      str(raised.exception))

    def test_an_unrelated_name_suggests_nothing(self) -> None:
        """A suggestion that fires on anything teaches the caller to ignore
        it."""
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.tool("capabilities_list", CAPABILITIES)(action="nonsense")
        self.assertNotIn("Did you mean", str(raised.exception))

    def test_the_schema_names_its_definitions_rather_than_inlining_them(self) -> None:
        """30 definitions at 34,600 bytes against 838 for the top level."""
        self.as_caller(caller())
        detail = self.tool("schema", SCHEMA_DOC)()
        self.assertEqual(["color", "layer"], detail["definitions"])
        self.assertNotIn("$defs", detail)
        self.assertIn("properties", detail)

    def test_one_definition_can_be_expanded(self) -> None:
        self.as_caller(caller())
        detail = self.tool("schema", SCHEMA_DOC)(definition="layer")
        self.assertEqual({"type": "object"}, detail["schema"])

    def test_an_unknown_definition_is_refused_by_name(self) -> None:
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.tool("schema", SCHEMA_DOC)(definition="nope")
        self.assertIn("nope", str(raised.exception))

    def test_the_reference_reads_cost_only_inspect(self) -> None:
        for name in ("xyz_status", "sql_capabilities", "capabilities_list",
                     "schema", "rules", "examples", "plugins_list",
                     "dependencies_list", "icons_list",
                     "derived_layers_capabilities"):
            with self.subTest(tool=name):
                exchange = FakeExchange()
                self.as_caller(caller())
                payload = {"schema": {}} if name == "schema" else {"actions": []}
                self.tool(name, payload, exchange=exchange)()
                self.assertEqual("inspect", exchange.calls[0]["scope"])

    def test_the_operation_id_is_encoded_into_the_path(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool("operations_show", OPERATION, exchange=exchange)(
            operation_id="odd/id")
        self.assertEqual("/api/operations/odd%2Fid", exchange.calls[0]["path"])

    def test_unexpected_shapes_degrade_rather_than_raising(self) -> None:
        for name, payload, kwargs in (
            ("operations_show", {"operation": None}, {"operation_id": "o"}),
            ("capabilities_list", {"actions": "x"}, {}),
            ("schema", {"schema": None}, {}),
        ):
            with self.subTest(tool=name):
                self.as_caller(caller())
                self.assertIsInstance(self.tool(name, payload)(**kwargs), dict)


SQL_RESULT = {"valid": True, "postgresType": "double precision", "sample": 4,
              "message": "Expression is compatible with the selected"
                         " information type.",
              "meta": {"requestId": "r12"}}


class SqlTestTests(ToolTestCase):
    """The only tool that sends a body, and the only read whose answer is a
    fact about the database rather than about the configuration."""

    def tool(self, payload=SQL_RESULT, exchange=None, config_api=None):
        return self.build_named(
            "sql_test", exchange=exchange,
            config_api=config_api or FakeConfigApi(answer=payload),
        )

    def test_it_reports_the_type_and_a_sample(self) -> None:
        self.as_caller(caller())
        detail = self.tool()(layer="Stops", expression="population * 2")
        self.assertIs(True, detail["valid"])
        self.assertEqual("double precision", detail["postgresType"])
        self.assertNotIn("meta", detail)

    def test_it_reaches_the_platform_as_a_post(self) -> None:
        """Routed on the operation's method. As a GET the body is never sent
        at all, and the platform refuses for a reason that names nothing."""
        api = FakeConfigApi(answer=SQL_RESULT)
        self.as_caller(caller())
        self.tool(config_api=api)(layer="Stops", expression="1")
        self.assertEqual("POST", api.calls[0]["method"])
        self.assertEqual({"layer": "Stops", "expression": "1"},
                         api.calls[0]["body"])

    def test_the_body_is_bound_at_exchange_time(self) -> None:
        """The digest covers the body, so what is sent and what was digested
        have to be the same object rather than two constructions of it."""
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool(exchange=exchange)(layer="Stops", expression="1 + 1")
        self.assertEqual({"layer": "Stops", "expression": "1 + 1"},
                         exchange.calls[0]["body"])

    def test_optional_arguments_are_omitted_rather_than_sent_as_null(self) -> None:
        """An absent member and a null member are different bodies, and the
        digest distinguishes them."""
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool(exchange=exchange)(layer="Stops", expression="1", type="integer")
        body = exchange.calls[0]["body"]
        self.assertEqual({"layer", "expression", "type"}, set(body))
        self.assertNotIn("locale", body)
        self.assertNotIn("field", body)

    def test_it_costs_derive_because_it_returns_a_value(self) -> None:
        """Not `inspect`: the sample comes out of the layer's own relation,
        which is the authority layers_values needs."""
        exchange = FakeExchange()
        self.as_caller(caller())
        self.tool(exchange=exchange)(layer="Stops", expression="1")
        self.assertEqual("derive", exchange.calls[0]["scope"])

    def test_it_is_refused_without_derive(self) -> None:
        self.as_caller(caller(scopes="mcp:connect inspect"))
        with self.assertRaises(ToolError) as raised:
            self.tool()(layer="Stops", expression="1")
        self.assertIn("derive", str(raised.exception))


class RefusalDetailTests(ToolTestCase):
    """A validation refusal's field-level entries are the answer, not noise.

    "Expression test failed" says only that something is wrong. The entry
    beneath it names the field and what PostgreSQL said, which for sql_test is
    the entire product of the call.
    """

    def build_refusing(self, errors):
        class Refusing:
            def get(self, **kwargs):
                raise ConfigApiRefused("Expression test failed.", status=422,
                                       code="", errors=errors)

            def post(self, **kwargs):
                raise ConfigApiRefused("Expression test failed.", status=422,
                                       code="", errors=errors)
        return self.build_named("sql_test", config_api=Refusing())

    def test_the_field_errors_reach_the_caller(self) -> None:
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.build_refusing(
                [{"path": "fieldfx", "message": "SQL function is not allowed:"
                                                " pg_sleep."}]
            )(layer="Stops", expression="pg_sleep(30)")
        message = str(raised.exception)
        self.assertIn("Expression test failed.", message)
        self.assertIn("fieldfx: SQL function is not allowed: pg_sleep.", message)

    def test_a_refusal_with_no_entries_is_unchanged(self) -> None:
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.build_refusing(None)(layer="Stops", expression="1")
        self.assertEqual(
            "The platform refused this request: Expression test failed.",
            str(raised.exception),
        )

    def test_the_entries_are_bounded(self) -> None:
        """A candidate can fail every rule at once, and a refusal that fills a
        conversation is its own failure."""
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.build_refusing(
                [{"path": f"f{n}", "message": f"m{n}"} for n in range(12)]
            )(layer="Stops", expression="1")
        self.assertEqual(5, str(raised.exception).count("\nf"))


class LayersGetBatchTests(ToolTestCase):
    """One call already reads every layer, so reading six should not be six.

    An agent asked to describe this workspace called layers_get once per layer:
    six identical fetches of the same response, five of them discarded down to
    a single entry each.
    """

    @staticmethod
    def payload():
        return {"revision": "rev-1", "locale": "locale", "layers": {
            "A": {"table": "public.a"},
            "B": {"table": "public.b"},
            "C": {"table": "public.c"},
        }}

    def tool(self, exchange=None):
        return self.build_named(
            "layers_get", exchange=exchange,
            config_api=FakeConfigApi(answer=self.payload()),
        )

    def test_one_key_is_unchanged(self) -> None:
        """The common case keeps its shape: a key and a layer, not a list."""
        self.as_caller(caller())
        detail = self.tool()(layer_key="B")
        self.assertEqual("B", detail["key"])
        self.assertEqual({"table": "public.b"}, detail["layer"])
        self.assertNotIn("layers", detail)

    def test_several_keys_come_back_in_one_call(self) -> None:
        api = FakeConfigApi(answer=self.payload())
        self.as_caller(caller())
        detail = self.build_named("layers_get", config_api=api)(
            layer_key="A,C")
        self.assertEqual(["A", "C"], [e["key"] for e in detail["layers"]])
        self.assertEqual(1, len(api.calls), "one platform read, not one per key")

    def test_the_order_asked_for_is_the_order_returned(self) -> None:
        """So a caller reads the reply against its own request rather than
        re-matching by key."""
        self.as_caller(caller())
        detail = self.tool()(layer_key="C,A,B")
        self.assertEqual(["C", "A", "B"], [e["key"] for e in detail["layers"]])

    def test_whitespace_around_keys_is_tolerated(self) -> None:
        self.as_caller(caller())
        detail = self.tool()(layer_key=" A , C ")
        self.assertEqual(["A", "C"], [e["key"] for e in detail["layers"]])

    def test_an_unknown_key_in_a_batch_is_refused_by_name(self) -> None:
        """Returning the two that matched would read as "this workspace has
        two of the three", which is a different and wrong answer."""
        self.as_caller(caller())
        with self.assertRaises(ToolError) as raised:
            self.tool()(layer_key="A,nope")
        self.assertIn("'nope'", str(raised.exception))
        self.assertIn("A, B, C", str(raised.exception))


class ToolVisibilityTests(ToolTestCase):
    """A tool a grant cannot call is not shown to it.

    Found by acceptance: a grant carrying mcp:connect alone was listed all 37
    tools and could invoke none of them, and the analysis preset was listed
    every tool and refused by the federation and source ones. Either way an
    agent discovers what it may do by being told no, one wasted turn at a time,
    and a person reads that as a broken server rather than a narrow grant.
    """

    def listed(self, scopes):
        server = self.build()
        self.as_caller(caller(scopes=scopes))
        return sorted(t.name for t in asyncio.run(server.list_tools()))

    def build(self, *, exchange=None, config_api=None):
        resource = ProtectedResource(
            origin="http://mcp.localhost", issuer="http://mcp.localhost"
        )
        return build_runtime(
            resource=resource,
            exchange=exchange or FakeExchange(),
            config_api=config_api or FakeConfigApi(),
        )

    def test_a_connect_only_grant_is_shown_nothing_it_cannot_call(self) -> None:
        """It can call nothing that reaches the platform, so it is shown
        nothing that does. describe_instance answers from this process."""
        self.assertEqual(["describe_instance"], self.listed("mcp:connect"))

    def test_inspect_reveals_the_tools_it_authorises(self) -> None:
        listed = self.listed("mcp:connect inspect")
        self.assertIn("layers_list", listed)
        self.assertIn("proposals_show", listed)
        # Needs derive; needs semantic:inspect; needs federation:observe.
        for hidden in ("layers_values", "semantic_status", "federation_list"):
            self.assertNotIn(hidden, listed)

    def test_each_scope_adds_exactly_the_tools_that_need_it(self) -> None:
        base = set(self.listed("mcp:connect inspect"))
        widened = set(self.listed("mcp:connect inspect federation:observe"))
        self.assertEqual(
            {"federation_list", "federation_show", "federation_groups"},
            widened - base,
        )

    def test_a_tool_needing_two_scopes_appears_only_with_both(self) -> None:
        """semantic_source_relations requires semantic:inspect alongside
        semantic:source, because the configuration API demands both. Holding
        either alone reveals nothing, which is the subset test doing its job
        rather than a membership test on the action's own scope."""
        base = set(self.listed("mcp:connect inspect"))
        for partial in ("semantic:source", "semantic:inspect"):
            with self.subTest(scopes=partial):
                widened = set(self.listed(f"mcp:connect inspect {partial}"))
                self.assertNotIn("semantic_source_relations", widened)
        both = set(self.listed(
            "mcp:connect inspect semantic:inspect semantic:source"))
        self.assertIn("semantic_source_relations", both - base)

    def test_a_widened_grant_is_shown_everything(self) -> None:
        listed = self.listed(WIDEST)
        self.assertIn("semantic_source_relations", listed)
        self.assertIn("federation_groups", listed)
        self.assertIn("sql_test", listed)

    def test_the_listing_fails_closed_without_a_caller(self) -> None:
        """A listing that defaulted to everything would be the old behaviour
        restored the first time the middleware changed."""
        server = self.build()
        CURRENT_CALLER.set(None)
        self.assertEqual(["describe_instance"],
                         sorted(t.name for t in asyncio.run(server.list_tools())))

    def test_everything_stays_registered_whatever_is_listed(self) -> None:
        """Registration is not filtered: the call check is the boundary and
        this is only its presentation. A hidden tool invoked directly is still
        refused by spend, and still refused by the platform after that."""
        server = self.build()
        self.as_caller(caller(scopes="mcp:connect"))
        self.assertEqual(1, len(asyncio.run(server.list_tools())))
        self.assertIn("federation_list", server._tool_manager._tools)
        self.assertIn("sql_test", server._tool_manager._tools)

    def test_the_registration_wrapper_cannot_default_its_operation(self) -> None:
        """Read from the source, because the failure is in code not yet
        written. A tool registered without `operation` records no scopes, and
        no scopes is a subset of every grant -- so it would be shown to
        everyone, including the connect-only grant this exists to protect.
        Every tool declares one today, so nothing else here would notice the
        default being added."""
        source = (Path(__file__).resolve().parents[1] / "runtime.py").read_text()
        self.assertIn("def tool(*, name, description, operation):", source,
                      "the registration wrapper must require `operation`")
        registrations = source.count("    @tool(")
        declared = source.count("        operation=")
        self.assertEqual(
            registrations,
            declared,
            "every @tool registration must pass operation= explicitly",
        )

    def test_each_tool_declares_the_operation_it_actually_spends(self) -> None:
        """The one drift the wrapper cannot prevent by itself.

        Requiring `operation=` makes a tool declare something; it does not make
        that something be what the body spends. Declaring a cheaper operation
        than the tool spends would list it to a grant that cannot call it,
        which is the behaviour this whole filter removes -- and declaring a
        dearer one would hide a tool that works. Neither is a way past the
        scope check, because spend() still reads its own descriptor, so this
        is a correctness guard rather than a security one.
        """
        import re

        source = (Path(__file__).resolve().parents[1] / "runtime.py").read_text()
        mismatched = []
        for block in re.split(r"\n    @tool\(", source)[1:]:
            name = re.search(r'name="([a-z_]+)"', block)
            declared = re.search(r"operation=([A-Za-z_]+)", block)
            spent = re.search(r"spend\(\s*([A-Z_]+)[,)]", block)
            if not (name and declared):
                continue
            spends = spent.group(1) if spent else None
            if declared.group(1) == "None":
                if spends:
                    mismatched.append((name.group(1), "None", spends))
            elif declared.group(1) != spends:
                mismatched.append((name.group(1), declared.group(1), spends))
        self.assertEqual(
            [],
            mismatched,
            "a tool declares one operation and spends another, so it is listed"
            " under the wrong scopes",
        )

    def test_every_platform_backed_tool_declares_its_scopes(self) -> None:
        """The registration wrapper requires `operation`, so a tool cannot be
        added without one. This pins that describe_instance is the only tool
        that legitimately declares none."""
        server = self.build()
        undeclared = set(server._tool_manager._tools) - set(server.tool_scopes)
        self.assertEqual({"describe_instance"}, undeclared)


# Both cut from live probes of the deployed stack. The two diffs are shaped
# differently on purpose: the workspace reports `old`/`value`, the semantic
# catalogue reports `before`/`after` objects carrying `exists`, because absent
# and present-but-null are distinct states for a curated field.
CHECK = {"check": {
    "valid": True,
    "proposalCreated": False,
    "originalRevision": "16c8b6dd",
    "originalHash": "e8eefb22",
    "candidateHash": "9cade65c",
    "pluginCatalogueFingerprint": "f77fbba3",
    "checkFingerprint": "e67d258af19860e4",
    "operations": [{"op": "set", "path": "/locale/layers/Bus_Stops/name",
                    "value": "Bus Stops (renamed)"}],
    "diff": [{"op": "replace", "path": "/locale/layers/Bus_Stops/name",
              "old": "Bus Stops", "value": "Bus Stops (renamed)"}],
    "explanation": "probe",
    "warnings": [],
}, "meta": {"requestId": "r13"}}

SEMANTIC_CHECK = {"catalogRevision": 53, "check": {
    "assetId": "440c4b84-4c84-4452-acff-7f4cbb9ca1bd",
    "baseVersion": 1,
    "fingerprint": "caf9c8a50647cf4f",
    "operations": [{"op": "set", "path": "/curated/description",
                    "value": "probe"}],
    "diff": [{"op": "set", "path": "/curated/description",
              "before": {"exists": False},
              "after": {"exists": True, "value": "probe"}}],
    "explanation": "probe",
}, "meta": {"requestId": "r14"}}

AUTHORING = WIDE + " propose semantic:propose visual"


class ProposalCheckTests(ToolTestCase):
    """Validating a change without proposing it.

    The first tool on this surface that costs a write scope to read. It changes
    nothing -- the platform applies the operations to a candidate in memory and
    reports what would happen -- but knowing what the platform would accept is
    authoring, and `inspect` is the wrong price for that.
    """

    def tool(self, name="proposals_check", payload=CHECK, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def test_it_reports_validity_and_the_fingerprint_create_will_need(self) -> None:
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.tool()(
            operations=[{"op": "set", "path": "/x", "value": 1}],
            revision="16c8b6dd")
        self.assertIs(True, detail["valid"])
        self.assertEqual("e67d258af19860e4", detail["checkFingerprint"])

    def test_the_diff_is_summarised_not_echoed(self) -> None:
        """Same treatment as a stored proposal, against the same measurements:
        a real change set carries whole layer definitions."""
        self.as_caller(caller(scopes=AUTHORING))
        change = self.tool()(operations=[], revision="r")["changes"][0]
        self.assertEqual("replace", change["op"])
        self.assertEqual("Bus Stops", change["was"])
        self.assertEqual("Bus Stops (renamed)", change["becomes"])

    def test_the_operations_the_caller_sent_are_not_returned(self) -> None:
        """The platform echoes them; repeating them to the agent that wrote
        them spends a conversation on what it already knows."""
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.tool()(operations=[], revision="r")
        self.assertNotIn("operations", detail)
        self.assertNotIn("explanation", detail)

    def test_the_integrity_material_is_dropped(self) -> None:
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.tool()(operations=[], revision="r")
        for dropped in ("originalHash", "candidateHash",
                        "pluginCatalogueFingerprint"):
            self.assertNotIn(dropped, detail)

    def test_the_revision_and_operations_reach_the_platform_as_a_post(self) -> None:
        api = FakeConfigApi(answer=CHECK)
        self.as_caller(caller(scopes=AUTHORING))
        self.build_named("proposals_check", config_api=api)(
            operations=[{"op": "set", "path": "/x", "value": 1}],
            revision="rev-9")
        sent = api.calls[0]
        self.assertEqual("POST", sent["method"])
        self.assertEqual("rev-9", sent["body"]["revision"])
        self.assertEqual([{"op": "set", "path": "/x", "value": 1}],
                         sent["body"]["operations"])

    def test_an_absent_explanation_is_omitted_rather_than_null(self) -> None:
        api = FakeConfigApi(answer=CHECK)
        self.as_caller(caller(scopes=AUTHORING))
        self.build_named("proposals_check", config_api=api)(
            operations=[], revision="r")
        self.assertNotIn("explanation", api.calls[0]["body"])

    def test_it_costs_propose_and_is_refused_without_it(self) -> None:
        """A grant that can read an instance cannot check a change against it.
        Reading and authoring are separate things to hand someone."""
        exchange = FakeExchange()
        self.as_caller(caller(scopes=AUTHORING))
        self.tool(exchange=exchange)(operations=[], revision="r")
        self.assertEqual("propose", exchange.calls[0]["scope"])

        self.as_caller(caller(scopes=WIDE))
        with self.assertRaises(ToolError) as raised:
            self.tool()(operations=[], revision="r")
        self.assertIn("propose", str(raised.exception))

    def test_the_semantic_diff_keeps_its_own_shape(self) -> None:
        """`before`/`after` with `exists`, not `old`/`value`. Absent and
        present-but-null are different states for a curated field, and
        flattening them into the workspace shape would lose that."""
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.tool("semantic_proposals_check", SEMANTIC_CHECK)(
            asset_id="a-1", base_version=1, operations=[])
        change = detail["changes"][0]
        self.assertEqual({"exists": False}, change["was"])
        self.assertEqual({"exists": True, "value": "probe"}, change["becomes"])
        self.assertEqual("caf9c8a50647cf4f", detail["fingerprint"])

    def test_the_semantic_check_sends_the_base_version(self) -> None:
        """It plays the part `revision` plays for the workspace: a change
        composed from a stale reading is refused rather than applied."""
        api = FakeConfigApi(answer=SEMANTIC_CHECK)
        self.as_caller(caller(scopes=AUTHORING))
        self.build_named("semantic_proposals_check", config_api=api)(
            asset_id="a-1", base_version=7, operations=[])
        self.assertEqual({"assetId": "a-1", "baseVersion": 7, "operations": []},
                         api.calls[0]["body"])

    def test_the_semantic_check_costs_the_semantic_propose_scope(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller(scopes=AUTHORING))
        self.tool("semantic_proposals_check", SEMANTIC_CHECK,
                  exchange=exchange)(asset_id="a", base_version=1, operations=[])
        self.assertEqual("semantic:propose", exchange.calls[0]["scope"])

    def test_unexpected_shapes_degrade_rather_than_raising(self) -> None:
        for payload in ({}, {"check": None}, {"check": {"diff": "x"}}):
            with self.subTest(payload=payload):
                self.as_caller(caller(scopes=AUTHORING))
                detail = self.tool(payload=payload)(operations=[], revision="r")
                self.assertEqual([], detail["changes"])


# Cut from a live create. The bulk is the point: `original` and `candidate`
# came to 31,640 of the 32,579 bytes, for a one-line rename.
CREATED = {"proposal": {
    "id": "1789749116-af15f72b46fd-217e2c",
    "status": "pending",
    "created": "2026-09-18T16:31:56Z",
    "actor": "[withheld]",
    "explanation": "shape probe",
    "originalRevision": "16c8b6dd",
    "originalHash": "e8eefb22",
    "candidateHash": "9cade65c",
    "pluginCatalogueFingerprint": "f77fbba3",
    "original": {"locale": {"layers": {"Bus_Stops": {"name": "snapshot-only"}}}},
    "candidate": {"locale": {"layers": {"Bus_Stops": {"name": "snapshot-only"}}}},
    "warnings": [],
    "operations": [{"op": "set", "path": "/locale/layers/Bus_Stops/name",
                    "value": "Bus Stops (shape probe)"}],
    "diff": [{"op": "replace", "path": "/locale/layers/Bus_Stops/name",
              "old": "Bus Stops", "value": "Bus Stops (shape probe)"}],
}, "meta": {"requestId": "r15"}}

SEMANTIC_CREATED = {"catalogRevision": 53, "proposal": {
    "id": "sp-9", "status": "pending",
    "assetId": "440c4b84-4c84-4452-acff-7f4cbb9ca1bd", "baseVersion": 1,
    "diff": [{"op": "set", "path": "/curated/description",
              "before": {"exists": False},
              "after": {"exists": True, "value": "probe"}}],
}, "meta": {"requestId": "r16"}}


class ProposalCreateTests(ToolTestCase):
    """The first tool here that changes durable state.

    What it writes is a queue entry: the workspace is untouched and somebody
    has to read the proposal and apply it for anything to happen.
    """

    def tool(self, name="proposals_create", payload=CREATED, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def call(self, fn, **over):
        args = {"operations": [{"op": "set", "path": "/x", "value": 1}],
                "revision": "16c8b6dd", "check_fingerprint": "fp-1"}
        args.update(over)
        return fn(**args)

    def test_it_reports_the_queue_entry_it_created(self) -> None:
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.call(self.tool())
        self.assertEqual("1789749116-af15f72b46fd-217e2c", detail["proposalId"])
        self.assertEqual("pending", detail["status"])

    def test_the_workspace_snapshots_never_reach_the_agent(self) -> None:
        """31,640 of 32,579 bytes for a one-line rename, and the platform has
        already derived the diff from them."""
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.call(self.tool())
        for dropped in ("original", "candidate"):
            self.assertNotIn(dropped, detail)
        self.assertNotIn("snapshot-only", json.dumps(detail))

    def test_the_change_it_made_is_reported_back(self) -> None:
        """So an agent can confirm what it proposed without a second call."""
        self.as_caller(caller(scopes=AUTHORING))
        change = self.call(self.tool())["changes"][0]
        self.assertEqual("Bus Stops", change["was"])
        self.assertEqual("Bus Stops (shape probe)", change["becomes"])

    def test_the_check_fingerprint_is_required_and_sent(self) -> None:
        """The platform accepts a create without one. Requiring it here means
        an agent cannot propose except from a change it has validated."""
        api = FakeConfigApi(answer=CREATED)
        self.as_caller(caller(scopes=AUTHORING))
        self.call(self.build_named("proposals_create", config_api=api),
                  check_fingerprint="fp-7")
        self.assertEqual("fp-7", api.calls[0]["body"]["checkFingerprint"])
        self.assertEqual("POST", api.calls[0]["method"])

        with self.assertRaises(TypeError):
            self.build_named("proposals_create")(
                operations=[], revision="r")

    def test_it_costs_propose_and_is_single_use(self) -> None:
        """A replayed create would add a second identical entry to somebody's
        queue, which is why the risk class is not a read class."""
        exchange = FakeExchange()
        self.as_caller(caller(scopes=AUTHORING))
        self.call(self.tool(exchange=exchange))
        self.assertEqual("propose", exchange.calls[0]["scope"])

    def test_it_is_refused_without_the_propose_scope(self) -> None:
        self.as_caller(caller(scopes=WIDE))
        with self.assertRaises(ToolError) as raised:
            self.call(self.tool())
        self.assertIn("propose", str(raised.exception))

    def test_the_semantic_create_sends_what_the_platform_requires(self) -> None:
        api = FakeConfigApi(answer=SEMANTIC_CREATED)
        self.as_caller(caller(scopes=AUTHORING))
        self.build_named("semantic_proposals_create", config_api=api)(
            asset_id="a-1", base_version=3, operations=[], fingerprint="f-2")
        self.assertEqual(
            {"assetId": "a-1", "baseVersion": 3, "operations": [],
             "fingerprint": "f-2"},
            api.calls[0]["body"],
        )

    def test_the_semantic_create_keeps_the_semantic_diff_shape(self) -> None:
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.build_named(
            "semantic_proposals_create",
            config_api=FakeConfigApi(answer=SEMANTIC_CREATED))(
            asset_id="a", base_version=1, operations=[], fingerprint="f")
        self.assertEqual("sp-9", detail["proposalId"])
        self.assertEqual({"exists": False}, detail["changes"][0]["was"])

    def test_a_fingerprint_mismatch_explains_the_asymmetry(self) -> None:
        """Measured on the deployed stack: a semantic create is refused when
        the explanation differs from the one its check was given, while the
        workspace create accepts it. The platform's message names neither the
        cause nor the difference, so the tool does."""
        class Refusing:
            def post(self, **kwargs):
                raise ConfigApiRefused(
                    "Proposal fingerprint does not match the checked"
                    " operation.",
                    status=409, code="semantic.fingerprint_mismatch",
                )
        self.as_caller(caller(scopes=AUTHORING))
        with self.assertRaises(ToolError) as raised:
            self.build_named(
                "semantic_proposals_create", config_api=Refusing())(
                asset_id="a", base_version=1, operations=[], fingerprint="f")
        message = str(raised.exception)
        self.assertIn("same one", message)
        self.assertIn("explanation", message)

    def test_a_hint_is_only_added_for_the_code_it_names(self) -> None:
        """A hint that fires on every refusal teaches the caller to skip the
        end of the message."""
        class Refusing:
            def post(self, **kwargs):
                raise ConfigApiRefused("Refused.", status=403,
                                       code="auth.scope_required")
        self.as_caller(caller(scopes=AUTHORING))
        with self.assertRaises(ToolError) as raised:
            self.build_named(
                "semantic_proposals_create", config_api=Refusing())(
                asset_id="a", base_version=1, operations=[], fingerprint="f")
        self.assertNotIn("explanation", str(raised.exception))

    def test_unexpected_shapes_degrade_rather_than_raising(self) -> None:
        for payload in ({}, {"proposal": None}, {"proposal": {"diff": "x"}}):
            with self.subTest(payload=payload):
                self.as_caller(caller(scopes=AUTHORING))
                detail = self.call(self.tool(payload=payload))
                self.assertEqual([], detail["changes"])


# Cut down from a real screenshot reply of 112,998 bytes: the pixel comparison
# was 32,538 of it and the per-check diagnosis 12,334, against 650 bytes of
# artifact paths, which are the only part a person looks at.
SHOT = {
    "proposalId": "1789749575-1b410882203d-a080a5",
    "source": "candidate",
    "operation": {
        "id": "c58af381da79db9701a347d285eacf02",
        "kind": "proposal.screenshot",
        "status": "failed",
        "error": {
            "code": "visual.failed",
            "message": "Browser validation did not pass.",
            # 6,174 bytes of per-check detail in the real reply.
            "diagnosis": {"outcome": "failed", "checks": ["x" * 60]},
        },
        "result": {
            "proposalId": "1789749575-1b410882203d-a080a5",
            "operationId": "c58af381da79db9701a347d285eacf02",
            "plan": {
                "layer": "Bus_Stops",
                "layerTitle": "Bus Stops (wave 3, proposed by an agent)",
                "warnings": [
                    "This layer uses an external or advanced XYZ source, so the visual check uses the configured workspace view."
                ],
                "effectiveDataset": {
                    "layerKey": "Bus_Stops"
                }
            },
            "visual": {
                "passed": False,
                "failedStage": "layer-registration",
                "runId": "2026-09-18T22-09-18-540Z-1789749575-1b410882203d-a080a5-1b410882203d2050-Bus_Stops-08527ed8",
                "artifacts": {
                    "beforePage": "2026-09-18T22-09-10-561Z-Bus_Stops-18840fb0/before-page.png",
                    "beforeMap": "2026-09-18T22-09-10-561Z-Bus_Stops-18840fb0/before-map.png",
                    "afterPage": "2026-09-18T22-09-18-540Z-1789749575-1b410882203d-a080a5-1b410882203d2050-Bus_Stops-08527ed8/before-page.png",
                    "afterMap": "2026-09-18T22-09-18-540Z-1789749575-1b410882203d-a080a5-1b410882203d2050-Bus_Stops-08527ed8/before-map.png",
                    "beforeReport": "2026-09-18T22-09-10-561Z-Bus_Stops-18840fb0/report.json",
                    "afterReport": "2026-09-18T22-09-18-540Z-1789749575-1b410882203d-a080a5-1b410882203d2050-Bus_Stops-08527ed8/report.json",
                    "beforeHoverTooltip": None,
                    "afterHoverTooltip": None
                },
                "diagnosis": {
                    "original": {
                        "checks": [
                            {
                                "id": "visual.http",
                                "passed": True,
                                "observed": 200
                            },
                            {
                                "id": "visual.layer_activation",
                                "passed": False,
                                "observed": {
                                    "registered": False,
                                    "detail": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                                }
                            }
                        ]
                    },
                    "candidate": {
                        "checks": [
                            {
                                "id": "visual.http",
                                "passed": True,
                                "observed": 200
                            }
                        ]
                    }
                },
                "comparison": {
                    "pixels": [
                        1,
                        2,
                        3
                    ],
                    "diffRatio": 0.4
                }
            }
        }
    },
    "meta": {
        "requestId": "r17"
    }
}

PLAN = {"proposalId": "p-1", "source": "candidate",
        "plan": {"layer": "Bus_Stops", "layerTitle": "Bus Stops (proposed)",
                 "warnings": ["external source"]},
        "meta": {"requestId": "r18"}}


class PreviewEvidenceTests(ToolTestCase):
    """Evidence a person can decide on, without the 113KB it arrives in."""

    def tool(self, name, payload, exchange=None):
        return self.build_named(
            name, exchange=exchange, config_api=FakeConfigApi(answer=payload)
        )

    def test_the_plan_reports_the_proposed_title_not_the_live_one(self) -> None:
        """How you can tell it read the candidate rather than the workspace."""
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.tool("proposals_preview_plan", PLAN)(
            proposal_id="p-1", layer="Bus_Stops")
        self.assertEqual("Bus Stops (proposed)", detail["plan"]["layerTitle"])
        self.assertNotIn("meta", detail)

    def test_the_artifacts_survive_whole(self) -> None:
        """They are the product of the call: 650 bytes of paths to the images
        somebody is going to look at."""
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.tool("proposals_preview_screenshot", SHOT)(
            proposal_id="p-1", layer="Bus_Stops")
        self.assertIn("beforeMap", detail["artifacts"])
        self.assertIn("afterMap", detail["artifacts"])

    def test_the_pixel_comparison_does_not(self) -> None:
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.tool("proposals_preview_screenshot", SHOT)(
            proposal_id="p-1", layer="Bus_Stops")
        self.assertNotIn("comparison", json.dumps(detail))
        self.assertNotIn("diffRatio", json.dumps(detail))

    def test_only_the_failing_checks_are_reported(self) -> None:
        """Nine checks ran and one failed. Reporting all nine is 12,334 bytes
        of mostly `passed: true`."""
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.tool("proposals_preview_screenshot", SHOT)(
            proposal_id="p-1", layer="Bus_Stops")
        self.assertEqual(
            [("original", "visual.layer_activation")],
            [(f["side"], f["check"]) for f in detail["failedChecks"]],
        )
        self.assertFalse(detail["passed"])
        self.assertEqual("layer-registration", detail["failedStage"])

    def test_the_error_is_reported_without_its_diagnosis(self) -> None:
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.tool("proposals_preview_screenshot", SHOT)(
            proposal_id="p-1", layer="Bus_Stops")
        self.assertEqual("visual.failed", detail["error"]["code"])
        self.assertEqual({"code", "message"}, set(detail["error"]))
        # 6,174 bytes of per-check diagnosis in the real reply.
        self.assertNotIn("diagnosis", json.dumps(detail))

    def test_hover_is_sent_as_false_because_nothing_else_completes(self) -> None:
        """Measured on one proposal: hover=false renders in 14.4 seconds,
        hover=true in 97.1, and omitting it in 100 to 166. A token B lives 60,
        so false is the only value that finishes before the credential
        authorising the request expires."""
        api = FakeConfigApi(answer=SHOT)
        self.as_caller(caller(scopes=AUTHORING))
        self.build_named("proposals_preview_screenshot", config_api=api)(
            proposal_id="p-1", layer="Bus_Stops")
        self.assertIs(False, api.calls[0]["body"]["hover"])
        self.assertEqual("POST", api.calls[0]["method"])

    def test_asking_for_hover_is_refused_before_it_is_attempted(self) -> None:
        """Rather than spending a minute and reporting the platform as
        unavailable, which is both slow and untrue. The refusal names the
        measurement and the surface that can do it."""
        for name in ("proposals_preview_screenshot", "proposals_preview_test"):
            with self.subTest(tool=name):
                api = FakeConfigApi(answer=SHOT)
                self.as_caller(caller(scopes=AUTHORING))
                with self.assertRaises(ToolError) as raised:
                    self.build_named(name, config_api=api)(
                        proposal_id="p-1", layer="Bus_Stops", hover=True)
                message = str(raised.exception)
                self.assertIn("60", message)
                self.assertIn("dashboard", message)
                # Refused here, so nothing was spent reaching the platform.
                self.assertEqual([], api.calls)

    def test_each_preview_costs_the_visual_scope(self) -> None:
        for name in ("proposals_preview_plan", "proposals_preview_screenshot",
                     "proposals_preview_test"):
            with self.subTest(tool=name):
                exchange = FakeExchange()
                self.as_caller(caller(scopes=AUTHORING))
                self.tool(name, SHOT, exchange=exchange)(
                    proposal_id="p-1", layer="Bus_Stops")
                self.assertEqual("visual", exchange.calls[0]["scope"])

    def test_a_grant_without_visual_is_refused(self) -> None:
        self.as_caller(caller(scopes=WIDE + " propose"))
        with self.assertRaises(ToolError) as raised:
            self.tool("proposals_preview_screenshot", SHOT)(
                proposal_id="p-1", layer="Bus_Stops")
        self.assertIn("visual", str(raised.exception))

    def test_the_proposal_id_is_encoded_into_the_path(self) -> None:
        exchange = FakeExchange()
        self.as_caller(caller(scopes=AUTHORING))
        self.tool("proposals_preview_screenshot", SHOT, exchange=exchange)(
            proposal_id="odd/id", layer="L")
        self.assertEqual("/api/proposals/odd%2Fid/screenshot",
                         exchange.calls[0]["path"])

    def test_unexpected_shapes_degrade_rather_than_raising(self) -> None:
        for payload in ({}, {"operation": None}, {"operation": {"result": "x"}}):
            with self.subTest(payload=payload):
                self.as_caller(caller(scopes=AUTHORING))
                detail = self.tool("proposals_preview_screenshot", payload)(
                    proposal_id="p", layer="L")
                self.assertEqual([], detail["failedChecks"])
                self.assertEqual({}, detail["artifacts"])

    def test_a_failed_render_is_an_answer_not_an_error(self) -> None:
        """The platform answers 422 with the whole result when the checks
        fail -- artifacts included. That is the reviewer's evidence: the images
        exist and something is wrong with them. Reducing it to its message
        would throw away the only reason the call was made."""
        class Refusing:
            def post(self, **kwargs):
                raise ConfigApiRefused(
                    "Browser validation did not pass.", status=422,
                    code="visual.failed", body=SHOT,
                )
        self.as_caller(caller(scopes=AUTHORING))
        detail = self.build_named(
            "proposals_preview_screenshot", config_api=Refusing())(
            proposal_id="p-1", layer="Bus_Stops")
        self.assertIs(False, detail["passed"])
        self.assertIn("beforeMap", detail["artifacts"])

    def test_another_refusal_status_still_refuses(self) -> None:
        """Only the declared statuses carry a result. A 403 is a refusal
        whatever body it happens to have."""
        class Refusing:
            def post(self, **kwargs):
                raise ConfigApiRefused(
                    "Refused.", status=403, code="auth.scope_required",
                    body=SHOT,
                )
        self.as_caller(caller(scopes=AUTHORING))
        with self.assertRaises(ToolError):
            self.build_named(
                "proposals_preview_screenshot", config_api=Refusing())(
                proposal_id="p-1", layer="Bus_Stops")

    def test_a_render_is_allowed_longer_than_a_read(self) -> None:
        """Measured at 20.8 and 24.4 seconds against a 15-second read default,
        which reported a working platform as unavailable. Bounded under the
        credential's own 60-second life."""
        from runtime import PROPOSALS_PREVIEW_SCREENSHOT, PROPOSALS_PREVIEW_TEST

        for descriptor in (PROPOSALS_PREVIEW_SCREENSHOT, PROPOSALS_PREVIEW_TEST):
            with self.subTest(operation=descriptor["operation_id"]):
                self.assertGreater(descriptor["timeout"], 25)
                self.assertLess(descriptor["timeout"], 60)


class MetaEnvelopeTests(unittest.TestCase):
    """No read tool returns the request-correlation envelope.

    Read from the source rather than asserted tool by tool, because that is how
    this drifted in the first place: whether a tool leaked `meta` depended on
    whether its endpoint happened to emit one, so derived_layers_list,
    semantic_catalog_show and semantic_catalog_search returned it while the
    tools either side of them did not. A tool added later returning `spend(...)`
    straight out would rejoin that set silently.
    """

    def test_no_tool_returns_a_platform_response_unfiltered(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "runtime.py"
        ).read_text()
        offenders = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith("return spend(")
        ]
        self.assertEqual(
            [],
            offenders,
            "a tool returns the configuration API response unfiltered; wrap it"
            " in _without_meta so the request envelope is not spent in a"
            " conversation",
        )


class LimitQueryTests(unittest.TestCase):
    def test_an_absent_limit_is_no_parameter(self) -> None:
        """`limit=` is a different request from no limit, and is refused."""
        self.assertEqual("", limit_query(limit=None))

    def test_a_limit_is_rendered_as_one_pair(self) -> None:
        self.assertEqual("limit=25", limit_query(limit=25))
