"""The MCP runtime: the official SDK, held behind this project's own guards.

Taking the SDK is a deliberate exception to the platform's near-zero-dependency
posture -- 28 packages, including a native cryptography stack, against a runtime
that otherwise needs three pure-Python ones. The reason is in requirements.txt
and it is not weight: MCP 2026-07-28 is not a small protocol to serve correctly,
and a hand-written implementation is a second piece of software to keep right
across revisions, whose failure mode is the worst kind -- passes curl, fails a
real client.

It is contained rather than trusted wholesale, and the containment is not
theoretical. The SDK serves both the modern and legacy handshake eras, exposes
no protocol-version allowlist, and negotiates a handshake to whatever revision
the client offers -- so ``era_guard`` runs in front of it and holds the served
set to the two revisions in ``era_guard.SERVED_VERSIONS``. ``stateless_http``
changes only legacy session storage and is not accepted as evidence about which
eras are served.

Nothing here decides authorization. By the time a request arrives the caller has
been resolved and its grant is on the ASGI scope, so a handler reads what the
operator consented to rather than asking again -- and a tool that forgot to
check would find nothing to check with.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit

import era_guard
from authentication import CURRENT_CALLER
from config_api_client import ConfigApiClient
from config_api_client import ConfigApiRefused
from config_api_client import ConfigApiUnavailable
from config_api_client import layer_statistics_query
from config_api_client import layer_values_query
from config_api_client import layers_query
from config_api_client import semantic_search_query
from exchange_client import ExchangeRefused
from exchange_client import ExchangeUnavailable
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

#: Advertised to clients as the server's identity. Not a version of the
#: platform: it is the version of this protocol surface, and it moves when the
#: tool contract does rather than when MAPP does.
RUNTIME_NAME = "mapp-mcp"
RUNTIME_VERSION = "0.1.0"

#: The scope a caller must hold for a tool merely to be listed. Anything a tool
#: *does* is checked again against the grant when it is called.
INSPECT_SCOPE = "inspect"

#: The one place the RPC path is written. The edge routes it, the guard screens
#: it and the SDK mounts at it, and nothing rewrites it in between.
RPC_PATH = "/mcp"


#: The operations these tools act through, named exactly as the broker's
#: allowlist names them. Not a vendored copy of that table: the broker decides
#: what may be exchanged for, and a second copy of that decision would be a
#: second thing to keep in step.
#:
#: Tool names follow the CLI's command vocabulary with spaces as underscores --
#: `layers list` is `layers_list` -- so an operator reading a transcript and an
#: operator at a terminal are using the same words for the same thing.
LAYERS_LIST = {
    "operation_id": "layers.list",
    "method": "GET",
    "path_template": "/api/layers",
    # The GET catch-all scope. Listing exposes workspace configuration and
    # nothing from the data, so it costs the discovery scope a client already
    # holds to see the tools at all.
    "scopes": ("inspect",),
}

CATALOG_LIST = {
    "operation_id": "catalog.list",
    "method": "GET",
    "path_template": "/api/catalog",
    "scopes": ("inspect",),
}

DERIVED_LAYERS_LIST = {
    "operation_id": "derived-layers.list",
    "method": "GET",
    "path_template": "/api/derived-layers",
    "scopes": ("inspect",),
}

FEDERATION_LIST = {
    "operation_id": "federation.aliases.list",
    "method": "GET",
    "path_template": "/api/federation/aliases",
    # The read scope. `federation:provision` can serve a third-party database
    # and is never needed to look at the registry.
    "scopes": ("federation:observe",),
}

FEDERATION_SHOW = {
    "operation_id": "federation.aliases.show",
    "method": "GET",
    "path_template": "/api/federation/aliases/{alias}",
    "scopes": ("federation:observe",),
}

SEMANTIC_CATALOG_LIST = {
    "operation_id": "semantic.catalog.export",
    "method": "GET",
    "path_template": "/api/semantic/catalog",
    "scopes": ("semantic:inspect",),
}

SEMANTIC_CATALOG_SEARCH = {
    "operation_id": "semantic.catalog.search",
    "method": "GET",
    "path_template": "/api/semantic/catalog/search",
    "scopes": ("semantic:inspect",),
}

SEMANTIC_CATALOG_SHOW = {
    "operation_id": "semantic.catalog.show",
    "method": "GET",
    "path_template": "/api/semantic/catalog/objects/{assetId}",
    "scopes": ("semantic:inspect",),
}

LAYERS_STATISTICS = {
    "operation_id": "layers.statistics",
    "method": "GET",
    "path_template": "/api/layers/{layerKey}/statistics",
    "scopes": ("derive", "semantic:inspect"),
}

LAYERS_VALUES = {
    "operation_id": "layers.values",
    "method": "GET",
    "path_template": "/api/layers/{layerKey}/values",
    # The action declares `derive` and additionally needs `semantic:inspect` to
    # read the field it aggregates over. Neither is advertised in discovery, so
    # a client holding only the bootstrap scopes has to ask for them.
    "scopes": ("derive", "semantic:inspect"),
}


def _layers_of(payload):
    """The (key, layer) pairs in a layer-listing response, whatever its shape.

    The configuration API returns layers as an object keyed by layer key. A
    tolerant reader here means a shape change downstream degrades to an empty
    list rather than an exception the agent cannot act on -- and the tools above
    report an empty workspace honestly rather than crashing.
    """
    layers = (payload or {}).get("layers")
    if isinstance(layers, dict):
        return list(layers.items())
    if isinstance(layers, list):
        return [
            (item.get("key") or item.get("layer") or "", item)
            for item in layers
            if isinstance(item, dict)
        ]
    return []


#: Fields of a federated-alias record that an agent is not given.
#:
#: `registeredBy` and `approvedBy` are credential identifiers -- which token
#: registered or approved a source. That is audit information about a *person's*
#: credential, and `federation:observe` is a grant to read the source registry,
#: not to enumerate the operator credentials behind it.
#:
#: The observation's `schemaFingerprint` and `extensionVersions` are the
#: platform's own verification machinery: several hundred characters per alias
#: that answer no question an agent can act on, and which push the useful fields
#: out of a context window.
ALIAS_WITHHELD = ("registeredBy", "approvedBy", "lastObservationId")
OBSERVATION_KEPT = ("connectivity", "schema", "lastConnected", "sourceFreshness")


def _observation_summary(observation):
    """Whether the source is reachable and current, without the fingerprints."""
    if not isinstance(observation, dict):
        return None
    return {key: observation.get(key) for key in OBSERVATION_KEPT}


def _alias_summary(alias):
    """One registry entry: what it is, what it serves, and whether it is live."""
    alias = alias if isinstance(alias, dict) else {}
    return {
        "alias": alias.get("alias"),
        "displayName": alias.get("displayName"),
        "kind": alias.get("kind"),
        "status": alias.get("status"),
        "allowedRelations": alias.get("allowedRelations") or [],
        "groups": alias.get("groups") or [],
        "dataHandlingClassification": alias.get("dataHandlingClassification"),
        "observation": _observation_summary(alias.get("lastObservation")),
    }


def _alias_detail(alias):
    """The whole record bar the credential identifiers and the fingerprints."""
    alias = alias if isinstance(alias, dict) else {}
    detail = {
        key: value for key, value in alias.items()
        if key not in ALIAS_WITHHELD and key != "lastObservation"
    }
    detail["observation"] = _observation_summary(alias.get("lastObservation"))
    return detail


def _asset_summary(asset):
    """One catalogue entry, reduced to what identifies it.

    The stored asset carries curated meaning, generated drafts, source state and
    provenance. The index needs the identifier and enough words to recognise it;
    `semantic_catalog_show` is where the meaning itself lives, and returning all
    of it here would answer "what is catalogued?" by spending a context window.
    """
    asset = asset if isinstance(asset, dict) else {}
    curated = asset.get("curated") if isinstance(asset.get("curated"), dict) else {}
    description = curated.get("description")
    return {
        "assetId": asset.get("id"),
        "name": curated.get("displayName"),
        # Truncated deliberately. These run to paragraphs, and the whole point
        # of an index is that it fits beside the other ten.
        "description": (description[:200] + "…")
        if isinstance(description, str) and len(description) > 200
        else description,
        "tags": curated.get("tags") or [],
    }


def _layer_summary(key, layer):
    """One index entry: enough to choose a layer, not enough to describe it.

    `table` is the load-bearing part. `layers_values` accepts only a real
    selectable *column* of the layer's relation -- the configuration API checks
    it against `information_schema` -- and the relation is what `catalog_list`
    resolves to columns.

    `displayFields` is named for what it is. These come from `infoj`, which
    describes what the map shows: some entries are calculated, some are
    geometry, and they are not interchangeable with columns. An earlier version
    of this returned them as `fields`, and the first live call chose one and was
    refused -- "the field 'population_display' is not a selectable column on
    this layer". Presenting them as queryable was the bug; presenting them at
    all is still useful, because they are what a person calls the data.
    """
    layer = layer if isinstance(layer, dict) else {}
    info = layer.get("infoj")
    display = [
        entry.get("field")
        for entry in (info if isinstance(info, list) else [])
        if isinstance(entry, dict) and entry.get("field")
    ]
    table = layer.get("table")
    return {
        "key": key,
        "name": layer.get("name") or key,
        "group": layer.get("group"),
        # A zoom-keyed mapping rather than one relation is a valid layer shape
        # and is not queryable by `layers_values`; reported as-is rather than
        # flattened into something that looks usable.
        "table": table if isinstance(table, str) else None,
        "displayFields": display,
    }


def build_runtime(*, resource, exchange=None, config_api=None) -> Any:
    """The SDK application, with the read-only surface registered.

    `resource` is the protected-resource description, so the runtime can report
    the identity a client discovered rather than composing a second one.
    `exchange` and `config_api` are injected so the tools can be driven without
    a broker or a platform behind them.
    """
    server = MCPServer(
        name=RUNTIME_NAME,
        version=RUNTIME_VERSION,
        title="MAPP",
        description=(
            "Read-only access to a MAPP instance's configured layers and"
            " semantic profiles."
        ),
    )

    @server.tool(
        name="describe_instance",
        description=(
            "The MAPP instance this server speaks for: its resource identity,"
            " the authorization server that issues credentials for it, and the"
            " protocol revisions it serves."
        ),
    )
    def describe_instance() -> dict:
        """Deliberately the first tool, and deliberately trivial.

        It needs no platform call, so it exercises the whole path -- discovery,
        the credential, the guard, dispatch, the result shape -- without
        depending on anything downstream being reachable. When a client cannot
        talk to this server, the answer to "is it the transport or the
        platform?" should not require reading logs.
        """
        return {
            "resource": resource.resource,
            "authorizationServer": resource.issuer,
            # Read from the guard rather than restated. A literal here was a
            # single revision that stayed "2026-07-28" while a caller reached
            # this tool over 2025-11-25 -- a server reporting an era it was not
            # speaking to the very client asking.
            "protocolVersions": list(era_guard.SERVED_VERSIONS),
            "runtime": f"{RUNTIME_NAME}/{RUNTIME_VERSION}",
        }

    def spend(operation, *, path, query=""):
        """Obtain one request-bound credential and spend it. The whole path.

        Every tool that touches the platform goes through here, so the scope
        check, the exchange, the binding and the failure vocabulary exist once.
        The alternative is each tool repeating forty lines, which is how the
        fourth tool ends up subtly different from the first.

        The credential authorises exactly this request and no other: the path
        and query are digested at exchange time and sent unchanged, so they are
        built once by the caller and passed here rather than reassembled. Two
        constructions of the same string is one chance for them to differ, and
        that surfaces at the configuration API as a refusal naming no cause.

        Every anticipated failure is a ``ToolError``, and that is load-bearing
        rather than stylistic. The SDK puts a ``ToolError``'s text in the result
        the model reads and treats any other exception as a crash -- replacing
        the message with "Error executing tool <name>" and logging a traceback.
        These were ``ValueError`` and ``RuntimeError`` once, so every message
        written to be acted on was discarded before reaching the caller. Found
        by driving a real client; the unit tests called the functions directly
        and agreed with the code while the property was false.
        """
        caller = CURRENT_CALLER.get()
        if caller is None:
            # Unreachable through the middleware, which refuses before dispatch.
            # Checked anyway, because the alternative if it ever became
            # reachable is an unauthenticated platform call.
            raise ToolError("This tool requires an authenticated caller.")
        missing = [s for s in operation["scopes"] if s not in caller.scopes]
        if missing:
            # Refused here rather than at the exchange, so the message names the
            # scopes to ask for. The broker would refuse it too, with an error
            # that says the scope exceeded the grant and not which scope.
            raise ToolError(
                "This grant does not carry "
                + " and ".join(sorted(missing))
                + ". Re-authorize requesting "
                + " ".join(operation["scopes"])
                + " to use this tool."
            )
        try:
            token_b = exchange.exchange(
                subject_token=caller.token,
                operation_id=operation["operation_id"],
                method=operation["method"],
                path_template=operation["path_template"],
                path=path,
                query=query,
                body=None,
                scope=" ".join(operation["scopes"]),
            )
        except ExchangeRefused as refusal:
            raise ToolError(f"The platform refused this request: {refusal}") from None
        except ExchangeUnavailable:
            # Deliberately not the underlying text: it describes this
            # component's plumbing, and an agent cannot act on it.
            raise ToolError(
                "The authorization component is unavailable; try again."
            ) from None
        try:
            return config_api.get(path=path, query=query, token=token_b)
        except ConfigApiRefused as refusal:
            raise ToolError(
                f"The platform refused this request: {refusal}"
                + (f" ({refusal.code})" if refusal.code else "")
            ) from None
        except ConfigApiUnavailable:
            raise ToolError(
                "The configuration API is unavailable; try again."
            ) from None

    @server.tool(
        name="layers_list",
        description=(
            "Every configured layer in the workspace, as a compact index:"
            " key, display name, group, the relation it reads, and the fields"
            " it displays. Use this to discover a layer_key. Note that"
            " layers_values needs a *column* of the layer's table, which"
            " catalog_list resolves -- displayFields are not always columns."
        ),
    )
    def layers_list(locale: str | None = None) -> dict:
        """Discovery, and the reason the other layer tools are usable at all.

        `layers_values` needs a layer key and a field name, and before this
        existed an agent had no way to learn either -- every test of it required
        a key found by reading the workspace file by hand.

        Deliberately an index rather than the configuration itself. The
        underlying response carries every layer in full, which is far more than
        an agent needs to choose one and costs tokens in a conversation where
        the next question is "and what is in it". `layers_get` returns one
        layer whole, which is the shape that actually wants detail.
        """
        query = layers_query(locale=locale)
        payload = spend(LAYERS_LIST, path=LAYERS_LIST["path_template"], query=query)
        return {
            "revision": payload.get("revision"),
            "locale": payload.get("locale"),
            "layers": [_layer_summary(key, layer)
                       for key, layer in _layers_of(payload)],
        }

    @server.tool(
        name="layers_get",
        description=(
            "One configured layer in full: its data source, fields, styling and"
            " filters. Takes a layer_key from layers_list."
        ),
    )
    def layers_get(layer_key: str, locale: str | None = None) -> dict:
        """The same read as `layers_list`, returning one layer rather than an index.

        There is no per-layer endpoint -- the configuration API serves the whole
        set and the CLI filters client-side, so this does the same. Filtering
        here rather than making the agent do it keeps the response bounded,
        which matters more for a model than for a terminal.
        """
        query = layers_query(locale=locale)
        payload = spend(LAYERS_LIST, path=LAYERS_LIST["path_template"], query=query)
        for key, layer in _layers_of(payload):
            if key == layer_key:
                return {
                    "revision": payload.get("revision"),
                    "locale": payload.get("locale"),
                    "key": key,
                    "layer": layer,
                }
        known = [key for key, _ in _layers_of(payload)]
        # Naming the alternatives, because a wrong key is the likeliest mistake
        # and the agent can act on the list without a second round trip.
        raise ToolError(
            f"No layer {layer_key!r} in this workspace."
            + (f" Configured layers: {', '.join(sorted(known))}." if known else "")
        )

    @server.tool(
        name="catalog_list",
        description=(
            "Database relations available to this instance, with each column's"
            " name and type. This is where a queryable field name comes from:"
            " layers_values accepts a column of the layer's table, which"
            " layers_list reports. Metadata only -- never row values."
        ),
    )
    def catalog_list(table: str | None = None) -> dict:
        """Column metadata, which is the only honest source of a queryable field.

        `layers_list` reports which relation a layer reads; this reports what is
        in that relation. The pair is what makes `layers_values` callable
        without guessing, which it was not before: the first live attempt used a
        display field and was refused.

        `table` filters to one relation, matched against "schema.table" or the
        bare table name. Filtering happens here rather than in the agent so a
        large deployment does not answer "which column?" with every column in
        the database.
        """
        payload = spend(CATALOG_LIST, path=CATALOG_LIST["path_template"])
        wanted = (table or "").strip().lower()
        relations = []
        for item in (payload.get("tables") or []):
            if not isinstance(item, dict):
                continue
            qualified = f"{item.get('schema')}.{item.get('table')}"
            if wanted and wanted not in (qualified.lower(), str(item.get("table")).lower()):
                continue
            relations.append({
                "relation": qualified,
                "columns": [
                    {"name": column.get("name"), "type": column.get("type")}
                    for column in (item.get("columns") or [])
                    if isinstance(column, dict) and column.get("name")
                ],
            })
        if wanted and not relations:
            known = [
                f"{i.get('schema')}.{i.get('table')}"
                for i in (payload.get("tables") or []) if isinstance(i, dict)
            ]
            raise ToolError(
                f"No relation matching {table!r}."
                + (f" Available: {', '.join(sorted(known))}." if known else "")
            )
        return {"databases": payload.get("databases"), "relations": relations}

    @server.tool(
        name="derived_layers_list",
        description=(
            "Managed derived relations: which exist, and how each was built."
            " This is where a layer's numbers come from -- the recipe, its"
            " sources and when it was last refreshed."
        ),
    )
    def derived_layers_list() -> dict:
        """Provenance for the relations `catalog_list` reports the shape of.

        Most of this instance's layers read `derived_layers.*`, which are built
        rather than ingested. An agent quoting a number from one should be able
        to say where it came from, and this is the only tool that can answer it.
        """
        return spend(
            DERIVED_LAYERS_LIST, path=DERIVED_LAYERS_LIST["path_template"]
        )

    @server.tool(
        name="derived_layers_show",
        description=(
            "One managed derived relation in full: its recipe, source"
            " relations, and refresh state. Takes a name from"
            " derived_layers_list."
        ),
    )
    def derived_layers_show(name: str) -> dict:
        """One entry from the same read.

        There is no per-name route -- the configuration API serves the set and
        the CLI filters client-side -- so this does the same, for the same
        reason `layers_get` does: a bounded answer matters more to a model than
        to a terminal.
        """
        payload = spend(
            DERIVED_LAYERS_LIST, path=DERIVED_LAYERS_LIST["path_template"]
        )
        entries = payload.get("derivedLayers")
        entries = entries if isinstance(entries, list) else []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
        known = [
            entry.get("name") for entry in entries
            if isinstance(entry, dict) and entry.get("name")
        ]
        raise ToolError(
            f"No derived layer {name!r} on this instance."
            + (f" Managed relations: {', '.join(sorted(known))}." if known else "")
        )

    @server.tool(
        name="federation_list",
        description=(
            "External sources federated into this instance, with each alias's"
            " status. Reveals which third-party databases the platform is"
            " configured to read; needs the federation:observe scope."
        ),
    )
    def federation_list() -> dict:
        """Where data comes from when it does not come from here.

        Separated from the other reads by scope on purpose. The others describe
        this instance; this describes its dependencies on other people's
        databases, which is a different question and a different grant.
        """
        payload = spend(FEDERATION_LIST, path=FEDERATION_LIST["path_template"])
        aliases = payload.get("aliases")
        host = payload.get("host") if isinstance(payload.get("host"), dict) else {}
        return {
            # Whether federation works at all, without naming the database or
            # the role it runs as -- those are this instance's internals and
            # answer nothing about the sources.
            "federationReady": host.get("federationReady"),
            "aliases": [
                _alias_summary(alias)
                for alias in (aliases if isinstance(aliases, list) else [])
            ],
        }

    @server.tool(
        name="federation_show",
        description=(
            "One federated source alias: its status, the evidence accepted for"
            " it, and the group labels it carries. Takes an alias from"
            " federation_list. Needs the federation:observe scope."
        ),
    )
    def federation_show(alias: str) -> dict:
        """One alias, including the evidence behind its current state."""
        path = FEDERATION_SHOW["path_template"].replace(
            "{alias}", quote(alias, safe="")
        )
        payload = spend(FEDERATION_SHOW, path=path)
        record = payload.get("alias")
        if not isinstance(record, dict):
            # Fail closed rather than pass an unrecognised payload through.
            # The first version fell back to detailing the whole response, so a
            # shape this did not expect carried every withheld field straight to
            # the agent -- withholding that depends on the response being the
            # shape you assumed is not withholding.
            raise ToolError(
                f"The registry returned no alias record for {alias!r}."
            )
        return _alias_detail(record)

    @server.tool(
        name="semantic_catalog_list",
        description=(
            "Every catalogued semantic asset: what the platform records about"
            " what its data means. An index of identifier, name and summary;"
            " semantic_catalog_show returns one asset's full meaning."
        ),
    )
    def semantic_catalog_list() -> dict:
        """What is described, as opposed to what exists.

        `layers_list` and `catalog_list` answer what the platform holds and what
        shape it is in. This answers what any of it *means* -- which is the
        difference between an agent that can query a column and one that knows
        whether the column is worth querying.
        """
        payload = spend(
            SEMANTIC_CATALOG_LIST, path=SEMANTIC_CATALOG_LIST["path_template"]
        )
        assets = payload.get("assets")
        return {
            "catalogRevision": payload.get("catalogRevision"),
            "assets": [
                _asset_summary(asset)
                for asset in (assets if isinstance(assets, list) else [])
            ],
        }

    @server.tool(
        name="semantic_catalog_search",
        description=(
            "Search catalogued semantic assets by text. Returns matches ranked"
            " by relevance with their identifiers, for semantic_catalog_show."
        ),
    )
    def semantic_catalog_search(query: str, limit: int | None = None) -> dict:
        """The entry point when the question is about meaning rather than shape.

        An agent asked "which layer covers air quality" cannot answer it from
        layer keys and column names -- the words it is given are the ones a
        person used, and those live in the catalogue's descriptions.
        """
        return spend(
            SEMANTIC_CATALOG_SEARCH,
            path=SEMANTIC_CATALOG_SEARCH["path_template"],
            query=semantic_search_query(query=query, limit=limit),
        )

    @server.tool(
        name="semantic_catalog_show",
        description=(
            "One semantic asset in full: its description, per-field meaning,"
            " caveats and tags. Takes an assetId from semantic_catalog_search"
            " or semantic_catalog_list."
        ),
    )
    def semantic_catalog_show(asset_id: str) -> dict:
        """Where the per-field meaning is, which is the point of the wave.

        `catalog_list` says a relation has a column called `population_quintile`
        of type integer. This says what a quintile means here, what it was
        derived from, and what the curator warned about it -- and those caveats
        are the difference between an agent reporting a number and an agent
        reporting a number that means something.
        """
        path = SEMANTIC_CATALOG_SHOW["path_template"].replace(
            "{assetId}", quote(asset_id, safe="")
        )
        return spend(SEMANTIC_CATALOG_SHOW, path=path)

    @server.tool(
        name="layers_statistics",
        description=(
            "Distribution summary for one numeric column of one layer: range,"
            " central tendency and a histogram. Takes a column name from"
            " catalog_list, as layers_values does. Aggregates only, never rows."
        ),
    )
    def layers_statistics(
        layer_key: str,
        field: str,
        locale: str | None = None,
        bins: int | None = None,
    ) -> dict:
        """The numeric counterpart to `layers_values`.

        `layers_values` counts categories, which answers "what values are
        there"; this summarises a distribution, which answers "what does it look
        like". A layer field is usually one or the other, and an agent that has
        only the first reaches for it on continuous data and gets thousands of
        distinct values back.

        Same field rule as `layers_values`: a real selectable column of the
        layer's relation, which `catalog_list` resolves. The platform refuses
        anything else, naming the column it could not find.
        """
        path = LAYERS_STATISTICS["path_template"].replace(
            "{layerKey}", quote(layer_key, safe="")
        )
        return spend(
            LAYERS_STATISTICS,
            path=path,
            query=layer_statistics_query(field=field, locale=locale, bins=bins),
        )

    @server.tool(
        name="layers_values",
        description=(
            "Bounded category counts for one field of one configured layer."
            " Returns aggregate counts from the layer's effective restrictions,"
            " never raw rows."
        ),
    )
    def layers_values(
        layer_key: str,
        field: str,
        locale: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """One read, through the full binding.

        Renamed from `layer_values` to match the CLI's `layers values`. The
        vocabulary is shared on purpose: an operator reading an agent transcript
        and an operator at a terminal should be using the same words.
        """
        path = LAYERS_VALUES["path_template"].replace(
            "{layerKey}", quote(layer_key, safe="")
        )
        return spend(
            LAYERS_VALUES,
            path=path,
            query=layer_values_query(field=field, locale=locale, limit=limit),
        )

    return server


def build_runtime_app(*, resource, exchange=None, config_api=None):
    """The ASGI application the guard wraps.

    Mounted at the path the request actually carries. Nothing strips it on the
    way in: Caddy proxies ``/mcp`` to the socket unchanged and the era guard
    passes ``scope["path"]`` through untouched, so an SDK mounted at ``/`` sees
    ``/mcp`` and answers Starlette's plain-text 404 -- which looks like a
    missing route rather than a mounting mistake.

    ``RPC_PATH`` is shared with the guard for that reason: two places deciding
    what the RPC path is means one of them is eventually wrong.
    """
    server = build_runtime(
        resource=resource, exchange=exchange, config_api=config_api
    )
    # DNS-rebinding protection, pointed at the origin this server actually
    # serves. It is on by default and defaults to 127.0.0.1, which is why an
    # otherwise correct request through the edge answers 421 "Invalid Host
    # header": the deployment's host is mcp.localhost, not the SDK's guess.
    #
    # Kept on rather than disabled. A browser on an operator's machine can be
    # made to POST to a loopback service; the Host and Origin allowlists are
    # what stop that reaching an authenticated MCP endpoint, and the credential
    # is in the request rather than a cookie only because this server refuses
    # cookies at all.
    host = urlsplit(resource.origin).hostname or "localhost"
    return server.streamable_http_app(
        streamable_http_path=RPC_PATH,
        # No session state. Under the handshake era the SDK would otherwise
        # mint and require `Mcp-Session-Id`, which the guard strips -- the
        # client would send back a session the server had been told to forget,
        # and every request after initialize would be refused.
        #
        # Measured, not assumed: with this on, a full legacy session --
        # initialize, notifications/initialized, tools/list, tools/call --
        # completes and no session identifier is ever emitted. That is what
        # lets the legacy era be served without giving up the no-session
        # obligation.
        #
        # This is a consequence of the era decision, not the control that
        # enforces it. The specification is explicit that enabling it "is not
        # accepted as evidence that the legacy era is disabled"; the guard is
        # the evidence, and it is asserted on the wire.
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            # Both spellings: a client may or may not carry the port, and an
            # allowlist that accepts one and refuses the other turns a correct
            # deployment into an intermittent 421.
            allowed_hosts=[host, f"{host}:*"],
            allowed_origins=[resource.origin, f"{resource.origin}:*"],
        ),
    )
