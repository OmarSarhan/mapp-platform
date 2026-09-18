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
from config_api_client import limit_query
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

PROPOSALS_LIST = {
    "operation_id": "proposals.list",
    "method": "GET",
    "path_template": "/api/proposals",
    # Reading the queue is the GET catch-all's `inspect`; adding to it is
    # `propose`, which no read tool asks for.
    "scopes": ("inspect",),
}

PROPOSALS_CHECK = {
    "operation_id": "proposals.check",
    "method": "POST",
    "path_template": "/api/proposals/check",
    "scopes": ("propose",),
}

SEMANTIC_PROPOSALS_CHECK = {
    "operation_id": "semantic.proposals.check",
    "method": "POST",
    "path_template": "/api/semantic/proposals/check",
    "scopes": ("semantic:propose",),
}

PROPOSALS_SHOW = {
    "operation_id": "proposals.show",
    "method": "GET",
    "path_template": "/api/proposals/{proposalId}",
    "scopes": ("inspect",),
}

SEMANTIC_PROPOSALS_LIST = {
    "operation_id": "semantic.proposals.list",
    "method": "GET",
    "path_template": "/api/semantic/proposals",
    "scopes": ("semantic:inspect",),
}

SEMANTIC_PROPOSALS_SHOW = {
    "operation_id": "semantic.proposals.show",
    "method": "GET",
    "path_template": "/api/semantic/proposals/{proposalId}",
    "scopes": ("semantic:inspect",),
}

XYZ_STATUS = {
    "operation_id": "xyz.status",
    "method": "GET",
    "path_template": "/api/xyz/status",
    "scopes": ("inspect",),
}

OPERATIONS_SHOW = {
    "operation_id": "operations.show",
    "method": "GET",
    "path_template": "/api/operations/{operationId}",
    "scopes": ("derive",),
}

DERIVED_LAYERS_CAPABILITIES = {
    "operation_id": "derived-layers.capabilities",
    "method": "GET",
    "path_template": "/api/derived-layers/capabilities",
    "scopes": ("inspect",),
}

SQL_TEST = {
    "operation_id": "sql.test",
    "method": "POST",
    "path_template": "/api/sql/test",
    # The same authority `layers_values` needs, and for the same reason: both
    # return values read from a configured layer's own relation.
    "scopes": ("derive",),
}

SQL_CAPABILITIES = {
    "operation_id": "sql.capabilities",
    "method": "GET",
    "path_template": "/api/sql/capabilities",
    "scopes": ("inspect",),
}

CAPABILITIES_LIST = {
    "operation_id": "capabilities.list",
    "method": "GET",
    "path_template": "/api/capabilities",
    "scopes": ("inspect",),
}

SCHEMA = {
    "operation_id": "schema",
    "method": "GET",
    "path_template": "/api/schema",
    "scopes": ("inspect",),
}

RULES = {
    "operation_id": "rules",
    "method": "GET",
    "path_template": "/api/rules",
    "scopes": ("inspect",),
}

EXAMPLES = {
    "operation_id": "examples",
    "method": "GET",
    "path_template": "/api/examples",
    "scopes": ("inspect",),
}

PLUGINS_LIST = {
    "operation_id": "plugins.list",
    "method": "GET",
    "path_template": "/api/plugins",
    "scopes": ("inspect",),
}

DEPENDENCIES_LIST = {
    "operation_id": "dependencies.list",
    "method": "GET",
    "path_template": "/api/dependencies",
    "scopes": ("inspect",),
}

ICONS_LIST = {
    "operation_id": "icons.list",
    "method": "GET",
    "path_template": "/api/icons",
    "scopes": ("inspect",),
}

SEMANTIC_STATUS = {
    "operation_id": "semantic.status",
    "method": "GET",
    "path_template": "/api/semantic/status",
    "scopes": ("semantic:inspect",),
}

SEMANTIC_SOURCE_RELATIONS = {
    "operation_id": "semantic.source.relations",
    "method": "GET",
    "path_template": "/api/semantic/source/relations",
    # Both, because the configuration API demands both: `semantic:source`
    # authorises the action and `semantic:inspect` is listed alongside it.
    # Requiring only the first would mint a credential the platform refuses.
    "scopes": ("semantic:inspect", "semantic:source"),
}

SEMANTIC_DERIVED_PROFILES_LIST = {
    "operation_id": "semantic.derived-profiles.list",
    "method": "GET",
    "path_template": "/api/semantic/derived-profiles",
    "scopes": ("semantic:inspect",),
}

SEMANTIC_DERIVED_PROFILES_SHOW = {
    "operation_id": "semantic.derived-profiles.show",
    "method": "GET",
    "path_template": "/api/semantic/derived-profiles/{name}",
    "scopes": ("semantic:inspect",),
}

SEMANTIC_CATALOG_HISTORY = {
    "operation_id": "semantic.catalog.history",
    "method": "GET",
    "path_template": "/api/semantic/catalog/objects/{assetId}/history",
    "scopes": ("semantic:inspect",),
}

DERIVED_LAYERS_JOBS = {
    "operation_id": "derived-layers.background-jobs",
    "method": "GET",
    "path_template": "/api/derived-layers/background-jobs",
    "scopes": ("inspect",),
}

DERIVED_LAYERS_MAP_EXTENT = {
    "operation_id": "derived-layers.map-extent",
    "method": "GET",
    "path_template": "/api/derived-layers/map-extent",
    "scopes": ("inspect",),
}

FEDERATION_GROUPS = {
    "operation_id": "federation.groups.list",
    "method": "GET",
    "path_template": "/api/federation/groups",
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


def _proposal_summary(proposal):
    """One queue entry: what changed, when, and whether it landed.

    The explanation is why a proposal exists and is the only field a person
    reads first, so it survives; `candidateHash`, `originalRevision` and the
    plugin fingerprint are integrity material that answer nothing an agent can
    act on. `actor` is not dropped here -- the configuration API withholds
    credential identifiers from an exchanged credential before this sees them,
    which is where that decision belongs.
    """
    proposal = proposal if isinstance(proposal, dict) else {}
    explanation = proposal.get("explanation")
    return {
        "proposalId": proposal.get("id"),
        "status": proposal.get("status"),
        "created": proposal.get("created"),
        "explanation": (explanation[:200] + "…")
        if isinstance(explanation, str) and len(explanation) > 200
        else explanation,
    }


def _change_preview(value):
    """What a changed value is, in a form that survives a conversation.

    A workspace diff carries whole layer definitions -- the largest on this
    instance is 44,580 bytes across 46 entries -- so returning values verbatim
    is not a summary of a change, it is the change. A scalar is already short
    and is kept: "visible became false" is the answer, not a description of it.
    A container is reduced to its shape, and for an object that means its keys,
    because which fields of a layer a proposal sets is the question a reviewer
    actually asks of it.
    """
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 120 else value[:120] + "…"
    if isinstance(value, list):
        return {"type": "list", "items": len(value)}
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value)
        preview = {"type": "object", "keys": keys[:20]}
        if len(keys) > 20:
            preview["truncated"] = len(keys) - 20
        return preview
    # Nothing else appears in a stored proposal; named rather than dropped so an
    # unexpected shape is visible instead of silently becoming null.
    return {"type": type(value).__name__}


def _change_summary(entry):
    """One diff entry: where it lands, what it does, and what it becomes."""
    entry = entry if isinstance(entry, dict) else {}
    return {
        "op": entry.get("op"),
        "path": entry.get("path"),
        "was": _change_preview(entry.get("old")),
        "becomes": _change_preview(entry.get("value")),
    }


def _refusal_detail(errors):
    """The platform's field-level errors, rendered for a person to act on.

    Bounded at five because a candidate workspace can fail every rule at once,
    and a refusal that fills a conversation is its own failure.
    """
    entries = []
    for error in (errors if isinstance(errors, list) else [])[:5]:
        if not isinstance(error, dict):
            continue
        message = error.get("message")
        if not message:
            continue
        path = error.get("path")
        entries.append(f"{path}: {message}" if path else str(message))
    return ("\n" + "\n".join(entries)) if entries else ""


def _nearest_actions(wanted, actions):
    """Name the close matches, because the near miss here is predictable.

    An action id and the tool that calls it are spelled differently -- the tool
    is `sql_test`, the action is `sql.test` -- and a caller holding one reaches
    for the other. A real client did exactly that during acceptance, read a
    refusal that named no alternative, and gave up rather than retrying.

    Matched on the identifier with its separators removed, so `sql_test`,
    `sql.test` and `sqltest` all find each other, which is the whole of the
    confusion being corrected.
    """
    def flatten(value):
        return str(value).replace("_", "").replace("-", "").replace(".", "").lower()

    target = flatten(wanted)
    near = sorted({
        entry.get("id") for entry in actions
        if isinstance(entry, dict) and entry.get("id")
        and flatten(entry["id"]) == target
    })
    if not near:
        return ""
    return " Did you mean " + " or ".join(repr(name) for name in near) + "?"


def _without_meta(payload):
    """The response minus its request-correlation envelope.

    Every configuration API response carries `meta.requestId`. It identifies
    the HTTP call in the platform's own logs and answers nothing an agent
    asked, so it is dropped rather than spent in a conversation.

    Applied by each read tool rather than inside `spend`, which would be the
    obvious place. `meta` is also where the configuration API puts
    `operationId` when a response carries an asynchronous operation, so a
    blanket strip would remove the handle a mutating tool needs to follow its
    own work. Reads never carry one; mutations are not this surface.
    """
    payload = payload if isinstance(payload, dict) else {}
    return {key: value for key, value in payload.items() if key != "meta"}


def _history_entry(entry):
    """One recorded change to a catalogued asset, without the asset.

    Each stored event embeds a full snapshot of the asset as it then was, so
    the history of a single asset reaches 4,925 bytes over a handful of events
    and grows with every one. What a reader wants from a history is the shape
    of the change -- when, what kind, which version, and the proposal that
    carried it -- and `semantic_catalog_show` already returns the asset as it
    now stands. `actor` is kept: the configuration API withholds credential
    identifiers from an exchanged credential before this sees them, which is
    where that decision belongs.
    """
    entry = entry if isinstance(entry, dict) else {}
    return {
        "eventId": entry.get("eventId"),
        "changedAt": entry.get("changedAt"),
        "changeType": entry.get("changeType"),
        "version": entry.get("version"),
        "generation": entry.get("generation"),
        "proposalId": entry.get("proposalId"),
        "catalogRevision": entry.get("catalogRevision"),
        "actor": entry.get("actor"),
    }


def _semantic_change_summary(entry):
    """One semantic diff entry, which is not shaped like a workspace one.

    The workspace diff says `old` and `value`; the semantic diff says `before`
    and `after`, each an object carrying `exists` and, when it does, `value`.
    The difference is meaningful -- absent and present-but-null are distinct
    states for a curated field -- so it is preserved rather than flattened into
    the workspace shape.
    """
    entry = entry if isinstance(entry, dict) else {}

    def side(value):
        value = value if isinstance(value, dict) else {}
        if not value.get("exists"):
            return {"exists": False}
        return {"exists": True, "value": _change_preview(value.get("value"))}

    return {
        "op": entry.get("op"),
        "path": entry.get("path"),
        "was": side(entry.get("before")),
        "becomes": side(entry.get("after")),
    }


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

    #: What each registered tool costs, recorded at registration so the listing
    #: and the call check read the same value. Restating it would be a second
    #: place to forget.
    tool_scopes: dict[str, tuple[str, ...]] = {}

    def tool(*, name, description, operation):
        """Register a tool and record the scopes it needs.

        `operation` is keyword-only and required, with no default. A tool added
        without one would be listed to every caller and then refuse, which is
        exactly the behaviour this exists to remove -- so the omission has to
        be a visible `operation=None` rather than a silent default.
        """
        if operation is not None:
            tool_scopes[name] = tuple(operation["scopes"])
        return server.tool(name=name, description=description)

    @tool(
        operation=None,  # Answers from this process; reaches no platform route.
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

    def spend(operation, *, path, query="", body=None):
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
                body=body,
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
            if operation["method"] == "GET":
                return config_api.get(path=path, query=query, token=token_b)
            # The same body object that was digested, not a rebuild of it.
            return config_api.post(
                path=path, query=query, token=token_b, body=body
            )
        except ConfigApiRefused as refusal:
            # The field-level entries, where the platform sent any. For a
            # validation refusal these are the answer: the top-level message
            # says only that something is wrong, and the entry beneath names
            # the field and what the database said about it.
            raise ToolError(
                f"The platform refused this request: {refusal}"
                + (f" ({refusal.code})" if refusal.code else "")
                + _refusal_detail(refusal.errors)
            ) from None
        except ConfigApiUnavailable:
            raise ToolError(
                "The configuration API is unavailable; try again."
            ) from None

    @tool(
        operation=LAYERS_LIST,
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

    @tool(
        operation=LAYERS_LIST,
        name="layers_get",
        description=(
            "Configured layers in full: data source, fields, styling and"
            " filters. Takes one layer_key from layers_list, or several"
            " separated by commas to get them in a single call."
        ),
    )
    def layers_get(layer_key: str, locale: str | None = None) -> dict:
        """The same read as `layers_list`, returning layers rather than an index.

        There is no per-layer endpoint -- the configuration API serves the whole
        set and the CLI filters client-side, so this does the same. Filtering
        here rather than making the agent do it keeps the response bounded,
        which matters more for a model than for a terminal.

        Several keys are accepted because one call already fetches every layer
        and discards all but one. An agent asked to describe this workspace
        called it six times, which was six identical reads of the same
        response; the comma form makes that one. Kept as a single string rather
        than a list parameter so the common case stays `layer_key="Bus_Stops"`.
        """
        query = layers_query(locale=locale)
        payload = spend(LAYERS_LIST, path=LAYERS_LIST["path_template"], query=query)
        wanted = [part.strip() for part in layer_key.split(",") if part.strip()]
        found = {key: layer for key, layer in _layers_of(payload) if key in wanted}
        if len(wanted) > 1:
            missing = [key for key in wanted if key not in found]
            if missing:
                known = [key for key, _ in _layers_of(payload)]
                raise ToolError(
                    f"No layer {missing[0]!r} in this workspace."
                    f" This workspace has: {', '.join(known)}."
                )
            return {
                "revision": payload.get("revision"),
                "locale": payload.get("locale"),
                # Ordered as asked, so a caller can read the reply against its
                # own request rather than re-matching by key.
                "layers": [{"key": key, "layer": found[key]} for key in wanted],
            }
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

    @tool(
        operation=CATALOG_LIST,
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

    @tool(
        operation=DERIVED_LAYERS_LIST,
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
        return _without_meta(
            spend(DERIVED_LAYERS_LIST, path=DERIVED_LAYERS_LIST["path_template"])
        )

    @tool(
        operation=DERIVED_LAYERS_LIST,
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

    @tool(
        operation=FEDERATION_LIST,
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

    @tool(
        operation=FEDERATION_SHOW,
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

    @tool(
        operation=PROPOSALS_LIST,
        name="proposals_list",
        description=(
            "Workspace changes that have been proposed: what was suggested,"
            " when, and whether it was applied or is still pending review."
            " Read-only -- proposing and applying are not offered."
        ),
    )
    def proposals_list(limit: int | None = None, status: str | None = None) -> dict:
        """The review queue, which is how a change reaches this platform.

        A workspace is not edited directly: a change is proposed, a person
        reviews it, and applying it is a separate act. An agent that can read
        the queue can say what is waiting and what already landed, which is the
        useful half of that loop and the half that alters nothing.

        `status` filters here rather than at the API, which offers no such
        parameter. `limit` is passed through, so a large queue is bounded by the
        platform rather than after the fact -- this instance already holds 81.
        """
        payload = spend(
            PROPOSALS_LIST,
            path=PROPOSALS_LIST["path_template"],
            query=limit_query(limit=limit),
        )
        proposals = payload.get("proposals")
        entries = [
            _proposal_summary(proposal)
            for proposal in (proposals if isinstance(proposals, list) else [])
        ]
        if status is not None:
            entries = [entry for entry in entries if entry["status"] == status]
        return {"proposals": entries}

    @tool(
        operation=PROPOSALS_CHECK,
        name="proposals_check",
        description=(
            "Validate workspace changes without proposing them: whether they"
            " are valid, what they would change, and any warnings. Returns a"
            " checkFingerprint. Takes `operations` and the `revision` they"
            " apply to, which layers_list reports. Writes nothing."
        ),
    )
    def proposals_check(
        operations: list[dict],
        revision: str,
        explanation: str | None = None,
    ) -> dict:
        """The first thing on this surface that costs a write scope to read.

        It changes nothing: the platform applies the operations to a candidate
        in memory, validates it, and reports what would happen. That makes it
        the right way to find out whether a change is well-formed before
        proposing it, and the wrong thing to charge `inspect` for -- knowing
        what the platform would accept is authoring, and authoring is what
        `propose` names.

        `revision` is required rather than resolved here. The platform refuses
        a check against a revision that is no longer current, which is the
        point: an agent that fetched the workspace, thought about it, and
        proposes against what it read is told so, instead of silently checking
        against something it never saw.

        The diff is summarised the way `proposals_show` summarises a stored
        one, for the same reason and against the same measurements.
        """
        body = {"revision": revision, "operations": operations}
        if explanation is not None:
            body["explanation"] = explanation
        payload = spend(
            PROPOSALS_CHECK, path=PROPOSALS_CHECK["path_template"], body=body
        )
        check = payload.get("check")
        check = check if isinstance(check, dict) else {}
        diff = check.get("diff")
        warnings = check.get("warnings")
        return {
            "valid": check.get("valid"),
            # What `proposals_create` will require, and the only reason to keep
            # a fingerprint here at all.
            "checkFingerprint": check.get("checkFingerprint"),
            "originalRevision": check.get("originalRevision"),
            "warnings": warnings if isinstance(warnings, list) else [],
            "changes": [
                _change_summary(entry)
                for entry in (diff if isinstance(diff, list) else [])
            ],
        }

    @tool(
        operation=SEMANTIC_PROPOSALS_CHECK,
        name="semantic_proposals_check",
        description=(
            "Validate changes to one catalogued asset's curated meaning"
            " without proposing them. Takes an assetId, the baseVersion it"
            " applies to, and `operations`. Returns a fingerprint. Writes"
            " nothing."
        ),
    )
    def semantic_proposals_check(
        asset_id: str,
        base_version: int,
        operations: list[dict],
        explanation: str | None = None,
    ) -> dict:
        """The semantic counterpart, and not quite the same shape.

        `baseVersion` plays the part `revision` plays for the workspace: it
        names what the operations were written against, so a change composed
        from a stale reading is refused rather than applied to something else.
        `semantic_catalog_history` reports it.
        """
        body = {
            "assetId": asset_id,
            "baseVersion": base_version,
            "operations": operations,
        }
        if explanation is not None:
            body["explanation"] = explanation
        payload = spend(
            SEMANTIC_PROPOSALS_CHECK,
            path=SEMANTIC_PROPOSALS_CHECK["path_template"],
            body=body,
        )
        check = payload.get("check")
        check = check if isinstance(check, dict) else {}
        diff = check.get("diff")
        return {
            "assetId": check.get("assetId"),
            "baseVersion": check.get("baseVersion"),
            "catalogRevision": payload.get("catalogRevision"),
            "fingerprint": check.get("fingerprint"),
            "changes": [
                _semantic_change_summary(entry)
                for entry in (diff if isinstance(diff, list) else [])
            ],
        }

    @tool(
        operation=PROPOSALS_SHOW,
        name="proposals_show",
        description=(
            "One queued proposal in detail: why it exists, what it would"
            " change, and any warnings raised against it. Each change gives its"
            " path, what was there and what it becomes -- scalars in full,"
            " larger values as their shape; pass `path` to expand one change"
            " completely. Takes a proposalId from proposals_list. Read-only --"
            " applying and declining are not offered."
        ),
    )
    def proposals_show(proposal_id: str, path: str | None = None) -> dict:
        """What a proposal actually changes, which the queue cannot say.

        `proposals_list` says a change is waiting and who it is from. That is
        enough to report the queue and not enough to review anything: the
        decision a person makes about a proposal is made on its diff.

        The whole record is not returned, and that is the substance of this
        tool rather than a caveat. It holds `original` and `candidate` -- the
        entire workspace before and after, 46,172 bytes each at the top of this
        instance -- from which the platform already derived `diff`. Returning
        them would spend a conversation restating a document the agent did not
        ask for so it could compute a difference that arrived alongside it.
        The integrity material (`candidateHash`, `originalHash`, the plugin
        fingerprint) is dropped for a different reason: it answers whether the
        record is intact, which is the platform's question at apply time and
        not one an agent can act on.

        `operations` becomes a count, which is a stronger claim than it looks.
        A stored operation carries the value it would write, so the list
        reaches 40,263 bytes here; reducing it to verbs and paths still left
        112 entries restating `changes`. Across all 81 stored proposals its
        path sequence is identical to the diff's, so the list adds one thing:
        a write verb (`set`) where the diff says what the change is (`replace`).
        A reviewer is asking what the proposal does, and `changes` answers that
        in the same order. The count survives because it is what a divergence
        would show up in -- an `operationCount` unequal to the number of
        changes is a proposal whose write plan is not its diff.

        `path` expands one change, because a shape tells a reviewer which
        fields a layer gains and not what they become. One at a time is
        deliberate -- expanding all of them reconstitutes the diff this exists
        to avoid.
        """
        target = PROPOSALS_SHOW["path_template"].replace(
            "{proposalId}", quote(proposal_id, safe="")
        )
        payload = spend(PROPOSALS_SHOW, path=target)
        proposal = payload.get("proposal")
        proposal = proposal if isinstance(proposal, dict) else {}
        diff = proposal.get("diff")
        diff = diff if isinstance(diff, list) else []
        operations = proposal.get("operations")
        warnings = proposal.get("warnings")

        detail = dict(_proposal_summary(proposal))
        # The list truncates the explanation at 200 characters because a queue
        # is scanned; this is the read where the whole reason is the point.
        detail["explanation"] = proposal.get("explanation")
        detail["originalRevision"] = proposal.get("originalRevision")
        detail["operationCount"] = len(
            operations if isinstance(operations, list) else []
        )
        detail["warnings"] = warnings if isinstance(warnings, list) else []
        detail["changes"] = [_change_summary(entry) for entry in diff]

        if path is not None:
            matches = [
                entry for entry in diff
                if isinstance(entry, dict) and entry.get("path") == path
            ]
            if not matches:
                raise ToolError(
                    f"This proposal changes nothing at {path}. The paths it"
                    " changes are listed in `changes` when `path` is omitted."
                )
            entry = matches[0]
            detail["change"] = {
                "op": entry.get("op"),
                "path": entry.get("path"),
                "was": entry.get("old"),
                "becomes": entry.get("value"),
            }
        return detail

    @tool(
        operation=SEMANTIC_PROPOSALS_LIST,
        name="semantic_proposals_list",
        description=(
            "Proposed changes to curated semantic meaning, with their review"
            " status. The semantic counterpart to proposals_list."
        ),
    )
    def semantic_proposals_list() -> dict:
        """Meaning is proposed and reviewed like configuration is."""
        return _without_meta(
            spend(SEMANTIC_PROPOSALS_LIST, path=SEMANTIC_PROPOSALS_LIST["path_template"])
        )

    @tool(
        operation=SEMANTIC_PROPOSALS_SHOW,
        name="semantic_proposals_show",
        description=(
            "One proposed semantic change in full, including what it would"
            " alter. Takes a proposalId from semantic_proposals_list."
        ),
    )
    def semantic_proposals_show(proposal_id: str) -> dict:
        """The detail a workspace proposal has no endpoint for.

        The workspace counterpart is `proposals_show`, which summarises rather
        than returning the record whole. This does not, because a semantic
        proposal carries curated meaning and no workspace snapshot, so there is
        no equivalent bulk to withhold.
        """
        path = SEMANTIC_PROPOSALS_SHOW["path_template"].replace(
            "{proposalId}", quote(proposal_id, safe="")
        )
        return _without_meta(spend(SEMANTIC_PROPOSALS_SHOW, path=path))

    @tool(
        operation=XYZ_STATUS,
        name="xyz_status",
        description=(
            "Whether the map tile service has picked up the current workspace:"
            " the generation it was asked for, the generation it applied, and"
            " whether it is healthy. Use this after a change to tell a stale"
            " map from a broken one."
        ),
    )
    def xyz_status() -> dict:
        """Whether what was configured is what is being served.

        A workspace change is not live until the tile service reloads. Until
        then the map shows the previous generation, which looks identical to a
        change that failed. `requestedGeneration` against `appliedGeneration`
        is the difference, and nothing else here reports it.
        """
        return _without_meta(spend(XYZ_STATUS, path=XYZ_STATUS["path_template"]))

    @tool(
        operation=OPERATIONS_SHOW,
        name="operations_show",
        description=(
            "One asynchronous operation: its kind, status, stage and when it"
            " changed, with its result and any error reduced to their shape."
            " Use it to tell finished work from failed work. Takes an"
            " operationId."
        ),
    )
    def operations_show(operation_id: str) -> dict:
        """Whether the work finished, and if not, what it said.

        `derived_layers_jobs` says how much work is in flight; this says what
        happened to one piece of it. Refresh, replace and visual checks all
        run asynchronously and report nowhere else.

        `result` and `error.diagnosis` are reduced rather than returned: a
        failed visual test carries 15,381 bytes of run detail and 6,174 of
        per-check diagnosis, against 34 bytes of message saying what went
        wrong. The shape is kept so the reduction is visible -- an agent can
        see that a `visual` block exists and how many keys it has, which is
        the honest way to say "there is more here than you were given".
        """
        path = OPERATIONS_SHOW["path_template"].replace(
            "{operationId}", quote(operation_id, safe="")
        )
        payload = spend(OPERATIONS_SHOW, path=path)
        operation = payload.get("operation")
        operation = operation if isinstance(operation, dict) else {}
        error = operation.get("error")
        error = error if isinstance(error, dict) else {}
        result = operation.get("result")
        result = result if isinstance(result, dict) else {}
        detail = {
            key: operation.get(key)
            for key in ("id", "kind", "status", "stage", "actor", "target",
                        "created", "updated")
        }
        detail["result"] = {
            key: _change_preview(value) for key, value in result.items()
        }
        if error:
            detail["error"] = {
                "code": error.get("code"),
                "message": error.get("message"),
                "diagnosis": _change_preview(error.get("diagnosis")),
            }
        return detail

    @tool(
        operation=DERIVED_LAYERS_CAPABILITIES,
        name="derived_layers_capabilities",
        description=(
            "What derived-layer work this deployment supports: the kinds it"
            " can build, its spatial scope types, and the guards that bound a"
            " materialization or query. Not uniform across instances."
        ),
    )
    def derived_layers_capabilities() -> dict:
        """What may be asked for before asking for it.

        The guards are the useful part: they say how large a derived relation
        may get and what a query may do, which is the difference between a
        plan that will be accepted and one that will be refused for reasons
        an agent cannot otherwise discover.
        """
        return _without_meta(
            spend(
                DERIVED_LAYERS_CAPABILITIES,
                path=DERIVED_LAYERS_CAPABILITIES["path_template"],
            )
        )

    @tool(
        operation=SQL_TEST,
        name="sql_test",
        description=(
            "Try one read-only SQL expression as a calculated field on an"
            " existing layer, and get back its PostgreSQL type and a sample"
            " value. Use it to check an expression before proposing it."
            " sql_capabilities says what an expression may contain. Changes"
            " nothing."
        ),
    )
    def sql_test(
        layer: str,
        expression: str,
        locale: str | None = None,
        field: str | None = None,
        type: str | None = None,
    ) -> dict:
        """Whether an expression works, without proposing it to find out.

        A calculated field is the one part of a layer whose correctness cannot
        be read off the configuration: it is SQL, and whether it parses, what
        type it yields and whether that matches the declared information type
        are all facts about the database. The alternative is proposing a
        change and reading the failure.

        It changes nothing, and that is enforced at the platform rather than
        promised here: the transaction is READ ONLY, the statement timeout is
        five seconds, the search_path is pinned to pg_catalog and public, and
        the function names are allowlisted and checked against being shadowed
        by an untrusted database function. It does return a sample value from
        the relation, which is why it costs `derive` -- the same authority
        `layers_values` needs -- rather than `inspect`.

        The body is built once here and digested at exchange time. It is not
        reassembled before sending, for the reason the path and query are not:
        two constructions of one request is one chance for them to disagree.
        """
        body = {"layer": layer, "expression": expression}
        if locale is not None:
            body["locale"] = locale
        if field is not None:
            body["field"] = field
        if type is not None:
            body["type"] = type
        return _without_meta(
            spend(SQL_TEST, path=SQL_TEST["path_template"], body=body)
        )

    @tool(
        operation=SQL_CAPABILITIES,
        name="sql_capabilities",
        description=(
            "Which SQL an expression may use: the mode, the supported"
            " constructs, what is prohibited, and the statement timeout."
            " Discovery of a constraint, not permission to run anything."
        ),
    )
    def sql_capabilities() -> dict:
        """The rules an expression is judged against, before writing one."""
        return _without_meta(
            spend(SQL_CAPABILITIES, path=SQL_CAPABILITIES["path_template"])
        )

    @tool(
        operation=CAPABILITIES_LIST,
        name="capabilities_list",
        description=(
            "The platform's contract: every action with its method, path,"
            " risk class and the scope it costs. Pass `action` to expand one"
            " in full, including its input and query schemas."
        ),
    )
    def capabilities_list(action: str | None = None) -> dict:
        """What this platform can be asked to do, and what each thing costs.

        Returned as an index because the schemas are the bulk: 57 actions come
        to 35,603 bytes, of which the smallest entry is 110 and the largest
        2,829. The index answers "what exists and what does it cost"; `action`
        answers "what shape does this one take", which is only asked once a
        particular action has been chosen.
        """
        payload = spend(CAPABILITIES_LIST, path=CAPABILITIES_LIST["path_template"])
        actions = payload.get("actions")
        actions = actions if isinstance(actions, list) else []
        if action is not None:
            for entry in actions:
                if isinstance(entry, dict) and entry.get("id") == action:
                    return {"action": entry}
            raise ToolError(
                f"This platform declares no action {action!r}."
                + _nearest_actions(action, actions)
                + " The actions it declares are listed when `action` is"
                " omitted."
            )
        return {
            "apiVersion": payload.get("apiVersion"),
            "contractVersion": payload.get("contractVersion"),
            "actions": [
                {
                    "id": entry.get("id"),
                    "method": entry.get("method"),
                    "path": entry.get("pathTemplate") or entry.get("path"),
                    "risk": entry.get("risk"),
                    "scope": entry.get("scope"),
                }
                for entry in actions
                if isinstance(entry, dict)
            ],
        }

    @tool(
        operation=SCHEMA,
        name="schema",
        description=(
            "The workspace JSON schema: its top level, and the names of the"
            " definitions it is built from. Pass `definition` to expand one."
            " Needed to author a change rather than to read one."
        ),
    )
    def schema(definition: str | None = None) -> dict:
        """The shape a workspace must take, which no read tool implies.

        `layers_list` says what this workspace contains; this says what any
        workspace may contain, which is the question behind "can a layer have
        X". The definitions are the bulk -- 30 of them at 34,600 bytes against
        838 for the top-level properties -- so they are named and expanded one
        at a time.
        """
        payload = spend(SCHEMA, path=SCHEMA["path_template"])
        document = payload.get("schema")
        document = document if isinstance(document, dict) else {}
        defs = document.get("$defs")
        defs = defs if isinstance(defs, dict) else {}
        if definition is not None:
            if definition not in defs:
                raise ToolError(
                    f"This schema defines no {definition!r}. Its definitions"
                    " are listed when `definition` is omitted."
                )
            return {"definition": definition, "schema": defs[definition]}
        return {
            key: value for key, value in document.items() if key != "$defs"
        } | {"definitions": sorted(defs)}

    @tool(
        operation=RULES,
        name="rules",
        description=(
            "The authoring rules a workspace change must satisfy, each with"
            " its identifier and what it requires. Read these before"
            " composing a proposal."
        ),
    )
    def rules() -> dict:
        """What makes a candidate valid, stated rather than discovered."""
        return _without_meta(spend(RULES, path=RULES["path_template"]))

    @tool(
        operation=EXAMPLES,
        name="examples",
        description=(
            "Worked examples of valid workspace operations, each with the"
            " operations it performs and an explanation. The shapes to copy"
            " when composing a change."
        ),
    )
    def examples() -> dict:
        """Known-good operations, which is the fastest way to a valid one."""
        return _without_meta(spend(EXAMPLES, path=EXAMPLES["path_template"]))

    @tool(
        operation=PLUGINS_LIST,
        name="plugins_list",
        description=(
            "The plugins this instance has configured: the bundled and"
            " external sets, the map library version behind them, and whether"
            " the registry is valid."
        ),
    )
    def plugins_list() -> dict:
        """What the map can do beyond the layers themselves.

        A layer may reference a plugin that this instance does not load, which
        is visible nowhere in the layer.
        """
        return _without_meta(
            spend(PLUGINS_LIST, path=PLUGINS_LIST["path_template"])
        )

    @tool(
        operation=DEPENDENCIES_LIST,
        name="dependencies_list",
        description=(
            "Which layers depend on which relations, so the reach of a change"
            " can be weighed before it is made."
        ),
    )
    def dependencies_list() -> dict:
        """What else a change would disturb.

        Every other read describes one thing at a time. This is the only one
        that says what is connected to what, which is the question asked
        before altering anything shared.
        """
        return _without_meta(
            spend(DEPENDENCIES_LIST, path=DEPENDENCIES_LIST["path_template"])
        )

    @tool(
        operation=ICONS_LIST,
        name="icons_list",
        description=(
            "The icons a layer's styling may reference, with their sources."
        ),
    )
    def icons_list() -> dict:
        """The set a style may draw from, which styling alone does not say."""
        return _without_meta(
            spend(ICONS_LIST, path=ICONS_LIST["path_template"])
        )

    @tool(
        operation=SEMANTIC_STATUS,
        name="semantic_status",
        description=(
            "Whether the semantic service is reachable and what it supports on"
            " this instance: search, proposals, generation, derived profiles,"
            " and the catalogue revision its answers are cut from. Use this"
            " when a semantic tool behaves unexpectedly, to tell a disabled"
            " capability from a failure."
        ),
    )
    def semantic_status() -> dict:
        """Which of the semantic tools are worth calling at all.

        Several semantic capabilities are optional per instance. Without this,
        an agent learns that search is unavailable by calling it and reading a
        refusal, which is indistinguishable from a transient fault.
        """
        return _without_meta(
            spend(SEMANTIC_STATUS, path=SEMANTIC_STATUS["path_template"])
        )

    @tool(
        operation=SEMANTIC_SOURCE_RELATIONS,
        name="semantic_source_relations",
        description=(
            "The tables and views available to model: their source alias,"
            " schema, relation name, kind, and the catalogued asset each maps"
            " to. This is the database inventory rather than the configured"
            " workspace, so it includes relations no layer uses. Needs"
            " semantic:source, which is granted separately."
        ),
    )
    def semantic_source_relations() -> dict:
        """What could be modelled, as opposed to what has been.

        Every other read here describes the workspace as configured:
        `layers_list` the layers, `derived_layers_list` the managed relations,
        `semantic_catalog_list` the meaning recorded for them. None of them can
        say what exists but was never used, which is the question behind "is
        there data for X" -- and answering it by proposing a layer and seeing
        it fail is a worse way to find out.

        Deliberately the inventory and not the data. It names relations and
        never reads a row from one; `layers_values` remains the only tool that
        returns values, and it works over configured layers rather than
        anything named here.
        """
        return _without_meta(
            spend(
                SEMANTIC_SOURCE_RELATIONS,
                path=SEMANTIC_SOURCE_RELATIONS["path_template"],
            )
        )

    @tool(
        operation=SEMANTIC_DERIVED_PROFILES_LIST,
        name="semantic_derived_profiles_list",
        description=(
            "Derived profiles: the managed relations the semantic service"
            " models, with the catalogued asset each corresponds to and"
            " whether it is ready. Use a name here with"
            " semantic_derived_profiles_show."
        ),
    )
    def semantic_derived_profiles_list() -> dict:
        """The join between a derived relation and its catalogued meaning.

        `derived_layers_list` says a relation exists and `semantic_catalog_*`
        says what an asset means; this is what says the two are the same thing,
        by carrying both the relation name and its assetId.
        """
        return _without_meta(
            spend(
                SEMANTIC_DERIVED_PROFILES_LIST,
                path=SEMANTIC_DERIVED_PROFILES_LIST["path_template"],
            )
        )

    @tool(
        operation=SEMANTIC_DERIVED_PROFILES_SHOW,
        name="semantic_derived_profiles_show",
        description=(
            "One derived profile by name: its relation, kind, catalogued"
            " assetId, generation and readiness. Takes a name from"
            " semantic_derived_profiles_list."
        ),
    )
    def semantic_derived_profiles_show(name: str) -> dict:
        """One profile, for when the list has already narrowed the question."""
        path = SEMANTIC_DERIVED_PROFILES_SHOW["path_template"].replace(
            "{name}", quote(name, safe="")
        )
        return _without_meta(spend(SEMANTIC_DERIVED_PROFILES_SHOW, path=path))

    @tool(
        operation=SEMANTIC_CATALOG_HISTORY,
        name="semantic_catalog_history",
        description=(
            "How a catalogued asset's meaning changed over time: each event"
            " with its kind, version, when it happened and the proposal that"
            " carried it. The asset as it now stands comes from"
            " semantic_catalog_show. Takes an assetId."
        ),
    )
    def semantic_catalog_history(asset_id: str) -> dict:
        """Why a definition says what it says, which the asset cannot.

        `semantic_catalog_show` returns the current meaning. This answers when
        it became that and what carried the change -- the question asked when a
        definition looks wrong and somebody needs to know whether it was always
        so.

        The stored events embed a snapshot of the asset at each point. Those
        are dropped: an asset's history would otherwise cost its full record
        once per event, and the current record is one tool call away.
        """
        path = SEMANTIC_CATALOG_HISTORY["path_template"].replace(
            "{assetId}", quote(asset_id, safe="")
        )
        payload = spend(SEMANTIC_CATALOG_HISTORY, path=path)
        history = payload.get("history")
        return {
            "assetId": payload.get("assetId"),
            "catalogRevision": payload.get("catalogRevision"),
            "history": [
                _history_entry(entry)
                for entry in (history if isinstance(history, list) else [])
            ],
        }

    @tool(
        operation=DERIVED_LAYERS_JOBS,
        name="derived_layers_jobs",
        description=(
            "Background job capacity for derived layers: how many are"
            " executing, waiting, and the ceiling. Use this to tell work still"
            " running from work that never started."
        ),
    )
    def derived_layers_jobs() -> dict:
        """Whether the platform is busy, which nothing else here reports.

        Refreshing or replacing a derived layer is asynchronous. An agent that
        cannot see the queue has no way to distinguish a slow job from a lost
        one, and the read costs nothing beyond `inspect`.
        """
        return _without_meta(
            spend(DERIVED_LAYERS_JOBS, path=DERIVED_LAYERS_JOBS["path_template"])
        )

    @tool(
        operation=DERIVED_LAYERS_MAP_EXTENT,
        name="derived_layers_map_extent",
        description=(
            "The spatial extent derived-layer work is bounded by: the"
            " configured envelopes, their CRS, the source view and how"
            " geometry at the boundary is selected."
        ),
    )
    def derived_layers_map_extent() -> dict:
        """Why a derived layer covers the ground it does.

        A derived relation is built against the locale's configured extent, so
        a layer that appears to be missing data outside a region is usually
        correct and bounded rather than broken. That is not visible from the
        layer itself.
        """
        return _without_meta(
            spend(
                DERIVED_LAYERS_MAP_EXTENT,
                path=DERIVED_LAYERS_MAP_EXTENT["path_template"],
            )
        )

    @tool(
        operation=FEDERATION_GROUPS,
        name="federation_groups",
        description=(
            "Federation group labels and how many live sources carry each."
            " Groups are metadata an operator applies; they grant no access."
            " Needs federation:observe."
        ),
    )
    def federation_groups() -> dict:
        """The labels sources are organised by, which the alias list implies.

        Membership is a label and never a permission -- cross-source querying
        already works between any two provisioned sources. Said here because an
        agent reading group names would otherwise reasonably infer a boundary
        that does not exist.
        """
        return _without_meta(
            spend(FEDERATION_GROUPS, path=FEDERATION_GROUPS["path_template"])
        )

    @tool(
        operation=SEMANTIC_CATALOG_LIST,
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

    @tool(
        operation=SEMANTIC_CATALOG_SEARCH,
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
        return _without_meta(
            spend(
                SEMANTIC_CATALOG_SEARCH,
                path=SEMANTIC_CATALOG_SEARCH["path_template"],
                query=semantic_search_query(query=query, limit=limit),
            )
        )

    @tool(
        operation=SEMANTIC_CATALOG_SHOW,
        name="semantic_catalog_show",
        description=(
            "One semantic asset: what it is, what it was derived from, its"
            " caveats and tags, and the names of its fields. Pass `field` with"
            " a field name to get that field's type and its curated meaning"
            " together. Takes an assetId from semantic_catalog_search or"
            " semantic_catalog_list."
        ),
    )
    def semantic_catalog_show(asset_id: str, field: str | None = None) -> dict:
        """Where the per-field meaning is, which is the point of the wave.

        `catalog_list` says a relation has a column called `population_quintile`
        of type integer. This says what a quintile means here, what it was
        derived from, and what the curator warned about it -- and those caveats
        are the difference between an agent reporting a number and an agent
        reporting a number that means something.

        The fields are named rather than described, because the census asset on
        this instance answers 110,987 bytes: 470 generated field records and 50
        curated ones, against a few hundred bytes for everything else. A real
        agent asked for it, could not read the reply, and spawned a subagent to
        chunk it -- which is the failure this shape prevents. Names are what it
        wanted anyway: it was looking for age columns, and found them by name.

        `field` also does a join the caller would otherwise have to do itself.
        The generated records are a list keyed by name and the curated ones are
        a map keyed by field id, so pairing a column with its meaning means
        matching one against the other; that is done here.
        """
        path = SEMANTIC_CATALOG_SHOW["path_template"].replace(
            "{assetId}", quote(asset_id, safe="")
        )
        payload = _without_meta(spend(SEMANTIC_CATALOG_SHOW, path=path))
        asset = payload.get("asset")
        if not isinstance(asset, dict):
            return payload
        curated = asset.get("curated")
        curated = curated if isinstance(curated, dict) else {}
        generated = asset.get("generated")
        generated = generated if isinstance(generated, dict) else {}
        generated_fields = generated.get("fields")
        generated_fields = [
            entry for entry in
            (generated_fields if isinstance(generated_fields, list) else [])
            if isinstance(entry, dict)
        ]
        curated_fields = curated.get("fields")
        curated_fields = curated_fields if isinstance(curated_fields, dict) else {}

        if field is not None:
            matched = next(
                (entry for entry in generated_fields
                 if entry.get("name") == field),
                None,
            )
            if matched is None:
                raise ToolError(
                    f"This asset has no field named {field!r}. Its field names"
                    " are listed when `field` is omitted."
                )
            return {
                "assetId": asset.get("id"),
                "field": field,
                "column": matched,
                # Keyed by field id rather than by name, which is why this join
                # exists here instead of in the caller.
                "meaning": curated_fields.get(matched.get("id")),
            }

        detail = {
            "assetId": asset.get("id"),
            "catalogRevision": payload.get("catalogRevision"),
            "displayName": curated.get("displayName"),
            "description": curated.get("description"),
            "tags": curated.get("tags") or [],
            "caveats": curated.get("caveats") or [],
            "relation": generated.get("qualifiedName"),
            "kind": generated.get("kind"),
            "binding": generated.get("binding"),
            "fieldCount": len(generated_fields),
            "curatedFieldCount": len(curated_fields),
            "fields": [
                entry.get("name") for entry in generated_fields
                if entry.get("name")
            ],
        }
        return detail

    @tool(
        operation=LAYERS_STATISTICS,
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
        return _without_meta(
            spend(
                LAYERS_STATISTICS,
                path=path,
                query=layer_statistics_query(field=field, locale=locale, bins=bins),
            )
        )

    @tool(
        operation=LAYERS_VALUES,
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
        return _without_meta(
            spend(
                LAYERS_VALUES,
                path=path,
                query=layer_values_query(field=field, locale=locale, limit=limit),
            )
        )

    registered = server.list_tools

    async def list_tools():
        """Only the tools this credential could actually call.

        A grant carrying `mcp:connect` alone was shown all 37 and could invoke
        none of them; the analysis preset is shown every tool and refused by
        the federation and source ones. Either way the agent discovers what it
        may do by being told no, which wastes a turn per tool and reads to a
        person as a broken server rather than a narrow grant.

        Registration is deliberately not filtered -- `_tool_manager` still
        holds every tool, and `spend` still checks the scope on the way in.
        This filters what is *described*, so the listing and the refusal agree
        instead of contradicting each other. Removing it would widen nothing;
        the call check is the boundary and this is the presentation of it.

        Fails closed: with no caller in context nothing platform-backed is
        listed, because a listing that defaulted to everything would be the
        current behaviour restored the first time the middleware changed.
        """
        caller = CURRENT_CALLER.get()
        held = frozenset(caller.scopes) if caller is not None else frozenset()
        return [
            described
            for described in await registered()
            if frozenset(tool_scopes.get(described.name, ())) <= held
        ]

    server.list_tools = list_tools
    # Exposed so tests can derive what a given grant should see from the same
    # values the filter uses, rather than restating a list that drifts.
    server.tool_scopes = tool_scopes

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
