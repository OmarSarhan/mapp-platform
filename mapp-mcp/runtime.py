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

import contextlib
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.parse import urlsplit

import anyio

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
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from pydantic import Field

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

PROPOSALS_PREVIEW_PLAN = {
    "operation_id": "proposals.preview-plan",
    "method": "POST",
    "path_template": "/api/proposals/{proposalId}/visual-plan",
    "scopes": ("visual",),
}

PROPOSALS_PREVIEW_TEST = {
    "operation_id": "proposals.preview-test",
    "method": "POST",
    "path_template": "/api/proposals/{proposalId}/visual-test",
    "scopes": ("visual",),
    # 422 carries the outcome, not an error: the checks failed and the
    # images exist.
    "result_statuses": (422,),
    # Measured at 20.8 and 24.4 seconds for one render; the read default of
    # 15 would report a working platform as unavailable. Under the
    # credential's 60-second life on purpose.
    "timeout": 45.0,
}

PROPOSALS_PREVIEW_SCREENSHOT = {
    "operation_id": "proposals.preview-screenshot",
    "method": "POST",
    "path_template": "/api/proposals/{proposalId}/screenshot",
    "scopes": ("visual",),
    # 422 carries the outcome, not an error: the checks failed and the
    # images exist.
    "result_statuses": (422,),
    # Measured at 20.8 and 24.4 seconds for one render; the read default of
    # 15 would report a working platform as unavailable. Under the
    # credential's 60-second life on purpose.
    "timeout": 45.0,
}

PROPOSALS_CREATE = {
    "operation_id": "proposals.create",
    "method": "POST",
    "path_template": "/api/proposals",
    "scopes": ("propose",),
}

SEMANTIC_PROPOSALS_CREATE = {
    "operation_id": "semantic.proposals.create",
    "method": "POST",
    "path_template": "/api/semantic/proposals",
    "scopes": ("semantic:propose",),
    # The two fingerprints do not bind the same thing, and the platform's
    # refusal does not say which. Measured: a semantic create is refused when
    # the explanation differs from the one the check was given, while the
    # workspace create accepts it. An agent that learns one rule is caught by
    # the other, so the refusal explains itself.
    "refusal_hints": {
        "semantic.fingerprint_mismatch": (
            "The semantic fingerprint covers the explanation as well as the"
            " operations, so semantic_proposals_check and"
            " semantic_proposals_create must be given the same one. The"
            " workspace pair does not bind the explanation, which is why this"
            " is easy to miss."
        ),
    },
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

#: Asking a person, and collecting the answer. Neither is a tool: an agent
#: cannot request its own approval as an action the model chooses to take, and
#: cannot claim a receipt on its own initiative. They are the two halves of
#: `approval_gate`, which the tools that need permission call, so the only way
#: to reach them is to be performing the operation they authorise.
APPROVALS_CREATE = {
    "operation_id": "approvals.create",
    "method": "POST",
    "path_template": "/api/approvals",
    # The listing scope. Asking for permission is not exercising it, and a
    # caller that can see a tool can ask to use it -- the gate is the receipt.
    "scopes": ("inspect",),
}

APPROVALS_CLAIM = {
    "operation_id": "approvals.claim",
    "method": "POST",
    "path_template": "/api/approvals/claim",
    "scopes": ("inspect",),
}

APPROVALS_CONFIRM = {
    "operation_id": "approvals.confirm",
    "method": "POST",
    "path_template": "/api/approvals/confirm",
    "scopes": ("inspect",),
}

#: How long a tool call will wait for somebody to decide in a browser, and how
#: often it asks. A call that waits forever is worse than one that refuses:
#: the client shows it as running for the life of the session, and the person
#: has no way to tell a slow decision from a broken server. Two minutes is
#: long enough to read a diff and a screenshot; past that the request is still
#: pending and the refusal says where to find it.
APPROVAL_WAIT_SECONDS = 120
APPROVAL_POLL_SECONDS = 2

#: How many outstanding asks one runtime keeps track of. An agent decides how
#: often to ask, so this is bounded rather than trusted; well above any real
#: session and far below anything that matters for memory.
APPROVAL_MEMORY_LIMIT = 256

#: Said the same way wherever a person says no, so a declined approval reads
#: as a decision rather than as a failure the agent should work around.
_APPROVAL_DECLINED = (
    "You declined this change, so nothing was done."
)


class _ApprovalConfirmation(BaseModel):
    """What a form-mode client asks the person.

    One boolean and nothing else. The elicitation spec admits only primitive
    types, and more importantly the decision is yes or no: a free-text field
    here would be a place for the model's framing to reach the person's
    answer.
    """

    approve: bool = Field(
        description="Approve this change? It cannot be undone automatically.",
    )


def _elicitation_modes(ctx) -> frozenset:
    """Which elicitation modes this client declared, measured not assumed.

    Measured against the three shipped clients on 2026-09-18: Codex CLI
    0.155.0 declares `{form, url}`, Claude Code 2.1.276 declares `{}`, and
    Gemini CLI 0.58.0 declares no elicitation at all.

    An empty `elicitation` object means form. The capability predates the
    mode split, when form was the only mode there was, so a client declaring
    the bare object is declaring the original one -- reading it as "no modes"
    would send the largest of the three clients down the dashboard path for
    no reason.
    """
    capabilities = getattr(getattr(ctx, "session", None), "client_capabilities", None)
    elicitation = getattr(capabilities, "elicitation", None)
    if elicitation is None:
        return frozenset()
    modes = set()
    if getattr(elicitation, "url", None) is not None:
        modes.add("url")
    if getattr(elicitation, "form", None) is not None:
        modes.add("form")
    return frozenset(modes or {"form"})


def _approval_message(operation_id: str, packet: dict) -> str:
    """What the person is asked, composed from the packet rather than freely.

    The summary comes from the tool that built the packet, not from the model
    reasoning about how to phrase a request for permission. An agent that
    could write this string could write a persuasive one.
    """
    summary = (packet or {}).get("summary")
    count = (packet or {}).get("changeCount")
    detail = f" {summary}" if isinstance(summary, str) and summary else ""
    scale = (
        f" It changes {count} thing{'' if count == 1 else 's'}."
        if isinstance(count, int) and count > 0
        else ""
    )
    return f"MAPP wants to run {operation_id}.{detail}{scale}"

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

#: The irreversible half of the loop. Each requires an approval receipt, which
#: the configuration API enforces from the action's risk class -- these
#: descriptors could not opt out of it if they tried.
PROPOSALS_APPLY = {
    "operation_id": "proposals.apply",
    "method": "POST",
    "path_template": "/api/proposals/{proposalId}/apply",
    "scopes": ("apply",),
    # Applying writes the workspace and then waits on the tile service, whose
    # own wait is 30 seconds. The read default of 15 would report a working
    # apply as an unavailable configuration API, which is the failure the
    # screenshot operations already taught this surface once.
    "timeout": 60.0,
    # Applied, but the reload was not observed. The platform answers 504 with
    # the whole result -- the proposal is committed and the body says so --
    # and reducing that to "the platform refused this request" would tell an
    # agent to retry a change that already happened.
    "result_statuses": (504,),
    "refusal_hints": {
        "proposal.revision": (
            "The workspace moved since this proposal was made. Nothing was"
            " applied. Compose the change again from the current revision."
        ),
        "proposal.validation": (
            "The proposal no longer passes validation against the current"
            " workspace. Nothing was applied."
        ),
    },
}

SEMANTIC_PROPOSALS_APPLY = {
    "operation_id": "semantic.proposals.apply",
    "method": "POST",
    "path_template": "/api/semantic/proposals/{proposalId}/apply",
    "scopes": ("semantic:apply",),
    "timeout": 45.0,
}

#: The derived-layer lifecycle. These act on the database directly: no
#: proposal, no review queue, no diff to read first. That is why the plan put
#: them after approval was real rather than alongside the rest.
DERIVED_LAYERS_PLAN = {
    "operation_id": "derived-layers.plan",
    "method": "POST",
    "path_template": "/api/derived-layers/plan",
    "scopes": ("derive:manage", "semantic:inspect"),
    # Probing runs real database work against the sources a definition names.
    "timeout": 60.0,
}

DERIVED_LAYERS_CREATE = {
    "operation_id": "derived-layers.create",
    "method": "POST",
    "path_template": "/api/derived-layers",
    "scopes": ("derive:manage", "semantic:inspect"),
    "timeout": 120.0,
    "refusal_hints": {
        "derived_layer.plan_stale": (
            "The plan this was built from no longer describes what would"
            " happen. Run derived_layers_plan again and use the new"
            " planFingerprint."
        ),
    },
}

DERIVED_LAYERS_REPLACE = {
    "operation_id": "derived-layers.replace",
    "method": "POST",
    "path_template": "/api/derived-layers/{name}/replace",
    "scopes": ("derive:manage", "semantic:inspect"),
    "timeout": 120.0,
}

DERIVED_LAYERS_DROP = {
    "operation_id": "derived-layers.drop",
    "method": "POST",
    "path_template": "/api/derived-layers/{name}/drop",
    "scopes": ("derive:manage",),
    "timeout": 60.0,
    "refusal_hints": {
        "derived_layer.in_use": (
            "Something still reads this relation. Remove those layers first --"
            " each removal is its own proposal and its own approval."
        ),
    },
}

DERIVED_LAYERS_REFRESH = {
    "operation_id": "derived-layers.refresh",
    "method": "POST",
    "path_template": "/api/derived-layers/{name}/refresh",
    "scopes": ("derive:manage",),
    # Recomputing a materialised relation is the longest-running thing on this
    # surface, and `background` exists precisely because it can exceed a wait.
    "timeout": 120.0,
}

XYZ_RELOAD = {
    "operation_id": "xyz.reload",
    "method": "POST",
    "path_template": "/api/xyz/reload",
    "scopes": ("reload",),
    "timeout": 60.0,
    # The same distinction as apply: a reload that was requested and not
    # observed completing is a result, not a refusal.
    "result_statuses": (504,),
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


def _applicability(proposal, revision):
    """Whether a pending proposal can still be applied.

    Derived, never stored. A proposal is applied only while the workspace is
    on the revision it was cut from -- `apply_proposal_and_reload` refuses
    otherwise -- so this restates a rule the platform already enforces instead
    of inventing a second one that could disagree with it.

    Not a clock. A proposal does not rot with age: one cut two months ago
    against the current revision applies cleanly, and one cut five minutes ago
    against a superseded revision does not. On this instance 16 of 19 pending
    proposals were already in the second state, reported as `pending`, which
    was true of their status and false of what it implied.

    `None` when the current revision is unknown, which is different from
    knowing the two differ.
    """
    if proposal.get("status") != "pending":
        return None
    if not revision or not proposal.get("originalRevision"):
        return None
    return "applicable" if proposal["originalRevision"] == revision else "superseded"


def _proposal_summary(proposal):
    """One queue entry: what changed, when, and whether it landed.

    The explanation is why a proposal exists and is the only field a person
    reads first, so it survives. `candidateHash` and the plugin fingerprint are
    integrity material that answer nothing an agent can act on.

    `originalRevision` was dropped here on that same reasoning and has been put
    back, because the reasoning was wrong: it is the field that separates a
    proposal which can still be applied from one that cannot, and without it a
    caller reading the queue has no way to tell. `actor` is not dropped here -- the configuration API withholds
    credential identifiers from an exchanged credential before this sees them,
    which is where that decision belongs.
    """
    proposal = proposal if isinstance(proposal, dict) else {}
    explanation = proposal.get("explanation")
    return {
        "proposalId": proposal.get("id"),
        "status": proposal.get("status"),
        "created": proposal.get("created"),
        "originalRevision": proposal.get("originalRevision"),
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


def _visual_outcome(payload):
    """A browser run reduced to what decides whether a change is acceptable.

    The reply is the largest on this surface: 112,998 bytes for one screenshot,
    of which the pixel comparison is 32,538 and the per-check diagnosis 12,334.
    None of that is what a person looks at. They look at the images, and at
    which check failed if one did.

    The artifacts are kept whole -- 650 bytes of paths -- because they are the
    entire product of the call. Everything else is reduced to the question it
    answers: did it pass, where did it stop, and which checks did not.
    """
    payload = payload if isinstance(payload, dict) else {}
    operation = payload.get("operation")
    operation = operation if isinstance(operation, dict) else {}
    result = operation.get("result")
    result = result if isinstance(result, dict) else {}
    visual = result.get("visual")
    visual = visual if isinstance(visual, dict) else {}
    plan = result.get("plan") or payload.get("plan")
    plan = plan if isinstance(plan, dict) else {}

    failed = []
    diagnosis = visual.get("diagnosis")
    for side in ("original", "candidate"):
        checks = (diagnosis or {}).get(side) if isinstance(diagnosis, dict) else None
        checks = (checks or {}).get("checks") if isinstance(checks, dict) else None
        for check in checks if isinstance(checks, list) else []:
            if isinstance(check, dict) and not check.get("passed"):
                failed.append({"side": side, "check": check.get("id"),
                               "observed": _change_preview(check.get("observed"))})

    detail = {
        "proposalId": payload.get("proposalId") or result.get("proposalId"),
        "operationId": operation.get("id") or result.get("operationId"),
        "status": operation.get("status"),
        "layer": plan.get("layer"),
        "layerTitle": plan.get("layerTitle"),
        "passed": visual.get("passed"),
        "failedStage": visual.get("failedStage"),
        "failedChecks": failed,
        # The point of the call: where the rendered images are.
        "artifacts": visual.get("artifacts") or {},
        "warnings": plan.get("warnings") or [],
    }
    error = operation.get("error")
    if isinstance(error, dict):
        detail["error"] = {"code": error.get("code"),
                           "message": error.get("message")}
    return detail


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


#: Where the guidance documents live, beside the runtime that serves them.
GUIDANCE_DIR = Path(__file__).resolve().parent / "guidance"

#: What an agent is told about *using* this surface, as MCP resources rather
#: than tools.
#:
#: A resource is the right primitive for guidance: a client can read it without
#: invoking anything, attach it to context on its own initiative, and it costs
#: no scope and has no side effect. A tool would make reading the instructions
#: an action with a result to interpret.
#:
#: Unfiltered by scope, deliberately. These describe how to use tools the
#: caller can already see; they carry no instance data, name no credential and
#: disclose nothing about the deployment. Withholding them from a narrow grant
#: would make that agent worse at the job without making anything safer.
#:
#: Adapted from `mapp-config-cli/docs/agent-workflow.md` rather than vendored
#: from it. The judgement in that document is hard-won and mostly
#: surface-agnostic, but its invocations are not: an agent here has no
#: `config-cli`, and "use config-cli as the only write interface" is false for
#: it. `test_guidance.py` pins the source's digest, so a change there surfaces
#: as a prompt to re-read rather than as silent divergence.
GUIDANCE = (
    {
        "path": "workflow.md",
        "uri": "mapp://guidance/workflow",
        "name": "Changing a MAPP workspace",
        "description": (
            "The order the platform expects -- establish the target, inspect,"
            " check, propose, show evidence, apply, verify -- and the"
            " safeguards that are not negotiable. Read before proposing or"
            " applying anything."
        ),
    },
    {
        "path": "styling.md",
        "uri": "mapp://guidance/styling",
        "name": "Mapping a styling request onto workspace properties",
        "description": (
            "Which property actually carries a colour, per geometry and symbol"
            " type; why style states are independent; and how to choose and"
            " audit breaks for a graduated metric layer."
        ),
    },
    {
        "path": "derived-layers.md",
        "uri": "mapp://guidance/derived-layers",
        "name": "Managed derived relations",
        "description": (
            "Views against materialized relations, identifier rules, why a"
            " plan comes before a create, how spatial scope is fixed, and what"
            " guards a drop. Needed by anything holding derive:manage."
        ),
    },
)


def _serve_guidance(server, document):
    """Register one guidance document as a readable resource.

    Read from disk per request rather than at import: the files are small, the
    read is local, and a runtime that cached them would serve a stale document
    after an image rebuild that changed one.
    """
    path = GUIDANCE_DIR / document["path"]

    @server.resource(
        document["uri"],
        name=document["name"],
        description=document["description"],
        mime_type="text/markdown",
    )
    def read() -> str:
        return path.read_text(encoding="utf-8")

    return read


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

    def spend(operation, *, path, query="", body=None, receipt=None):
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
            # A descriptor may ask for longer than the read default. Bounded
            # below the credential's own 60-second life, so a tool that is
            # going to fail says so before the token it holds expires.
            deadline = operation.get("timeout")
            if operation["method"] == "GET":
                return config_api.get(
                    path=path, query=query, token=token_b, timeout=deadline,
                    receipt=receipt,
                )
            # The same body object that was digested, not a rebuild of it.
            return config_api.post(
                path=path, query=query, token=token_b, body=body,
                timeout=deadline, receipt=receipt,
            )
        except ConfigApiRefused as refusal:
            # Some operations answer a non-2xx with the result rather than an
            # error. A browser validation that fails its checks returns 422
            # carrying the artifacts and the failing checks, which is exactly
            # what a reviewer needs; reducing it to its message would throw the
            # evidence away. Declared per operation so this is never a guess
            # about what a status means.
            if refusal.status in operation.get("result_statuses", ()) and (
                refusal.body is not None
            ):
                return refusal.body
            # The field-level entries, where the platform sent any. For a
            # validation refusal these are the answer: the top-level message
            # says only that something is wrong, and the entry beneath names
            # the field and what the database said about it.
            hint = (operation.get("refusal_hints") or {}).get(refusal.code)
            raise ToolError(
                f"The platform refused this request: {refusal}"
                + (f" ({refusal.code})" if refusal.code else "")
                + _refusal_detail(refusal.errors)
                + (f" {hint}" if hint else "")
            ) from None
        except ConfigApiUnavailable:
            raise ToolError(
                "The configuration API is unavailable; try again."
            ) from None

    async def approval_gate(
        ctx, operation, *, path, query="", body=None, tool_name, packet,
    ):
        """Get a person's permission for one exact request, or refuse.

        Returns a receipt the caller passes to `spend` for the *same* request.
        The receipt is bound to the canonical digest of that request, so the
        thing approved and the thing done cannot differ: a receipt minted for
        one apply buys nothing against another.

        The person answering is the person driving this session, not an
        operator at a separate dashboard. Routing an agent's mutation through
        a different surface makes the loop unusable in a chat, which is the
        context this component exists for.

        **The model cannot answer on its own behalf.** Nothing here is a tool,
        so the model cannot invoke it; the prompt is rendered by the *client*
        and answered by a person; and an `ElicitResult` is a transport message
        the model has no way to fabricate, exactly as it cannot fabricate a
        tool result it did not receive. A confirmation merely returned to the
        agent and echoed back would satisfy none of that, and is the obvious
        wrong design here.
        """
        caller = CURRENT_CALLER.get()
        digest = exchange.request_digest(
            operation_id=operation["operation_id"],
            method=operation["method"],
            path_template=operation["path_template"],
            path=path,
            query=query,
            body=body,
        )
        # An approval already asked for, for this exact request, by this
        # grant. Without this a second attempt creates a second pending row:
        # the person approves the first, the agent waits on the second, and
        # the loop cannot complete however patient either of them is. It is
        # the difference between a two-call flow and no flow at all.
        #
        # Held here rather than looked up, because the handle is the secret
        # that claims the receipt and the platform keeps only its hash --
        # there is nothing to look it up by. Held *here* rather than passed
        # back through the agent for the reason nothing approval-shaped is
        # ever a tool argument: the model never sees it and cannot supply it.
        remembered = _remembered_approval(caller, digest)
        if remembered is not None:
            handle, approval_url = remembered
            # Asked already. The only question left is whether it has been
            # answered, so ask that before asking the person anything again --
            # a second prompt for a decision already made is how a two-call
            # flow turns into an endless one.
            settled = _settled_decision(handle)
            if settled is not None:
                _forget_approval(caller, digest)
                return settled
        else:
            created = spend(
                APPROVALS_CREATE,
                path=APPROVALS_CREATE["path_template"],
                body={
                    "operationId": operation["operation_id"],
                    "requestDigest": digest,
                    "tool": tool_name,
                    "clientId": getattr(caller, "client_id", "") or "",
                    "packet": packet,
                },
            )
            handle = created.get("handle")
            approval_url = created.get("approvalUrl") or ""
            if not handle:
                raise ToolError(
                    "The platform accepted the approval request but named no"
                    " handle, so there is nothing to wait on."
                )
            if created.get("decided"):
                # A standing window decided it on arrival. Nothing here is
                # skipped except the question: the intent carried the same
                # canonical digest, the receipt is the same single-use
                # receipt bound to it, and it is spent atomically with the
                # effect exactly as an interactively approved one is. What a
                # window substitutes is the decider.
                #
                # Not remembered either, because there is nothing to come back
                # for -- the two-call flow exists to carry a handle across a
                # decision somebody has yet to make.
                return _claim(handle)
            _remember_approval(caller, digest, handle, approval_url)

        message = _approval_message(operation["operation_id"], packet)
        modes = _elicitation_modes(ctx)
        if "url" in modes:
            # The best version: the decision is made in a browser session the
            # agent does not control, looking at the rendered evidence rather
            # than at a summary the model composed.
            outcome = await ctx.elicit_url(
                message=message + " Open the approval page to see the change"
                                  " and decide.",
                url=approval_url,
                elicitation_id=digest,
            )
            if outcome.action != "accept":
                raise ToolError(_APPROVAL_DECLINED)
            receipt = await _await_decision(ctx, handle, approval_url)
            _forget_approval(caller, digest)
            # Tells the client the out-of-band step is over, so it can stop
            # showing the person a link to a page that no longer needs them.
            with contextlib.suppress(Exception):
                await ctx.session.send_elicit_complete(digest)
            return receipt

        if "form" in modes:
            outcome = await ctx.elicit(message, _ApprovalConfirmation)
            accepted = (
                outcome.action == "accept"
                and getattr(outcome.data, "approve", False) is True
            )
            # Recorded either way. A decline is a decision a person made and
            # belongs in the audit trail; leaving the row pending would also
            # leave it decidable by somebody else afterwards.
            spend(
                APPROVALS_CONFIRM,
                path=APPROVALS_CONFIRM["path_template"],
                body={"handle": handle, "accepted": accepted},
            )
            _forget_approval(caller, digest)
            if not accepted:
                raise ToolError(_APPROVAL_DECLINED)
            return _claim(handle)

        # Neither mode. Today that is *every* client, because the transport
        # this runtime serves has no back-channel for server-initiated
        # requests -- measured, not assumed, and pinned by
        # TransportCannotElicitTests. So this is the working path rather than
        # the fallback, and it is a two-call flow: the first call asks and
        # says where, the second spends the answer.
        #
        # Refused rather than left hanging, because nothing has told the
        # person to open the page -- the refusal is what the agent repeats to
        # them, and it carries the summary so they know what they are being
        # asked to allow before they follow a link.
        summary = (packet or {}).get("summary")
        raise ToolError(
            f"This needs your approval before it can happen.{
                ' ' + summary if summary else ''
            } Approve it here: {approval_url}"
            " -- then ask me to try again and I will pick up your decision."
        )

    #: Approvals this process has asked for and not yet spent, keyed by the
    #: grant and the exact request. Small and short-lived: an entry is dropped
    #: the moment the decision is collected, and an approval the platform will
    #: no longer honour is dropped on the next attempt at it. Lost on restart,
    #: which costs a person one extra "ask me again" and no authority -- the
    #: row is still there, still pending, and still theirs to decline.
    remembered_approvals: dict = {}

    def _approval_key(caller, digest):
        # The grant, not the client and not the token: an approval belongs to
        # the consent it was asked under. Keyed by it so two grants asking for
        # the identical request never share one person's answer -- which is
        # what a key of "" for everybody would have meant.
        return (getattr(caller, "grant_id", None) or "", digest)

    def _remembered_approval(caller, digest):
        return remembered_approvals.get(_approval_key(caller, digest))

    def _remember_approval(caller, digest, handle, approval_url):
        if len(remembered_approvals) >= APPROVAL_MEMORY_LIMIT:
            # Bounded, because an agent can ask as often as it likes and this
            # is the one structure here that grows on its say-so. Oldest out:
            # dict order is insertion order, and the oldest ask is the one
            # closest to expiring anyway.
            remembered_approvals.pop(next(iter(remembered_approvals)), None)
        remembered_approvals[_approval_key(caller, digest)] = (
            handle, approval_url,
        )

    def _forget_approval(caller, digest):
        remembered_approvals.pop(_approval_key(caller, digest), None)

    def _settled_decision(handle):
        """The receipt, if somebody has now decided. None while they have not.

        Distinguishes "not answered yet" from every other outcome, because
        only the first should send the person back to the page. A declined or
        expired request is finished and says so.
        """
        claimed = spend(
            APPROVALS_CLAIM,
            path=APPROVALS_CLAIM["path_template"],
            body={"handle": handle},
        )
        status = claimed.get("status")
        if status == "pending":
            return None
        if status == "declined":
            raise ToolError(_APPROVAL_DECLINED)
        if status == "expired":
            raise ToolError(
                "The approval request expired before it was decided. Ask me"
                " to try again if you still want this change."
            )
        receipt = claimed.get("receipt")
        if not receipt:
            raise ToolError(
                "The approval was recorded but no receipt came back, so this"
                " request cannot proceed. Ask me to try again."
            )
        return receipt

    def _claim(handle):
        """Collect the receipt a decision produced, once.

        The platform mints it on the first claim and never again, so this is
        called exactly once per decision and its result is not re-fetchable.
        """
        claimed = spend(
            APPROVALS_CLAIM,
            path=APPROVALS_CLAIM["path_template"],
            body={"handle": handle},
        )
        status = claimed.get("status")
        receipt = claimed.get("receipt")
        if status == "declined":
            raise ToolError(_APPROVAL_DECLINED)
        if status == "expired":
            raise ToolError(
                "The approval request expired before it was decided. Ask me"
                " to try again if you still want this change."
            )
        if not receipt:
            raise ToolError(
                "The approval was recorded but no receipt came back, so this"
                " request cannot proceed. Ask me to try again."
            )
        return receipt

    async def _await_decision(ctx, handle, approval_url):
        """Poll until the person in the browser has decided.

        Bounded, because a tool call that never returns is worse than one that
        refuses: the client shows it as running for as long as the session
        lives. On timeout the request is still pending and still decidable --
        the person is told where, rather than told it failed.
        """
        deadline = APPROVAL_WAIT_SECONDS
        while True:
            # Asked before waiting, not after. Somebody who decided while the
            # client was still rendering the prompt has already answered, and
            # sleeping first would cost every such approval a poll interval
            # for nothing.
            claimed = spend(
                APPROVALS_CLAIM,
                path=APPROVALS_CLAIM["path_template"],
                body={"handle": handle},
            )
            status = claimed.get("status")
            if status != "pending":
                if status == "declined":
                    raise ToolError(_APPROVAL_DECLINED)
                if status == "expired":
                    raise ToolError(
                        "The approval request expired before it was decided."
                        " Ask me to try again if you still want this change."
                    )
                receipt = claimed.get("receipt")
                if not receipt:
                    # The platform mints a receipt once. A second claim of an
                    # approved request answers without one, so this is what a
                    # lost first answer looks like -- not a refusal, and not
                    # something a retry of this call can recover.
                    raise ToolError(
                        "The approval was recorded but no receipt came back,"
                        " so this request cannot proceed. Ask me to try again."
                    )
                return receipt
            if deadline <= 0:
                break
            await anyio.sleep(min(APPROVAL_POLL_SECONDS, deadline))
            deadline -= APPROVAL_POLL_SECONDS
        raise ToolError(
            "Nobody decided within the time this call can wait. The request"
            f" is still waiting at {approval_url} -- approve it there, then"
            " ask me to try again."
        )

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
        operation=DERIVED_LAYERS_PLAN,
        name="derived_layers_plan",
        description=(
            "What creating a derived relation would do, without creating it:"
            " the probes the platform runs against the sources, the shape it"
            " would produce, and a planFingerprint. Pass that fingerprint to"
            " derived_layers_create and the create is refused if anything"
            " moved in between. Creates nothing and asks nobody."
        ),
    )
    def derived_layers_plan(
        name: str,
        query: str,
        sources: list[str],
        id_column: str,
        geometry_column: str,
        kind: str | None = None,
        description: str | None = None,
    ) -> dict:
        """The dry run, and the reason `create` is not the first step.

        `proposals_check` is the same idea for the workspace: find out what
        would happen while nothing has happened yet. It matters more here,
        because a derived layer leaves no proposal for a person to read -- the
        plan is the only description of the change that exists before the
        change does.
        """
        return _without_meta(spend(
            DERIVED_LAYERS_PLAN,
            path=DERIVED_LAYERS_PLAN["path_template"],
            body=_derived_definition(
                name=name, query=query, sources=sources,
                id_column=id_column, geometry_column=geometry_column,
                kind=kind, description=description,
            ),
        ))

    @tool(
        operation=DERIVED_LAYERS_CREATE,
        name="derived_layers_create",
        description=(
            "Create a managed derived relation. Asks you to approve first,"
            " showing the plan. Pass plan_fingerprint from"
            " derived_layers_plan so the create is refused if the sources"
            " moved since you looked. This writes to the database directly --"
            " there is no proposal and no review queue."
        ),
    )
    async def derived_layers_create(
        ctx: Context,
        name: str,
        query: str,
        sources: list[str],
        id_column: str,
        geometry_column: str,
        kind: str | None = None,
        description: str | None = None,
        plan_fingerprint: str | None = None,
        background: bool | None = None,
    ) -> dict:
        """The first tool that creates a database object.

        Everything wave 6 applies was proposed, reviewed and readable first.
        This is not: the relation appears because an agent asked for it and a
        person agreed. So the packet carries the definition itself -- the
        query, the sources, the columns -- because there is nothing else for
        the person to read.
        """
        body = _derived_definition(
            name=name, query=query, sources=sources,
            id_column=id_column, geometry_column=geometry_column,
            kind=kind, description=description,
        )
        if plan_fingerprint is not None:
            body["planFingerprint"] = plan_fingerprint
        if background is not None:
            body["background"] = background
        packet = {
            "summary": f"Create the derived relation {name}.",
            "changeCount": 1,
            "changes": [{"op": "create", "path": f"derived_layers.{name}",
                         "was": None, "becomes": _change_preview(query)}],
            "definition": {
                "name": name, "kind": kind or "view", "sources": sources,
                "idColumn": id_column, "geometryColumn": geometry_column,
            },
            "planned": plan_fingerprint is not None,
        }
        receipt = await approval_gate(
            ctx, DERIVED_LAYERS_CREATE,
            path=DERIVED_LAYERS_CREATE["path_template"], body=body,
            tool_name="derived_layers_create", packet=packet,
        )
        return _without_meta(spend(
            DERIVED_LAYERS_CREATE,
            path=DERIVED_LAYERS_CREATE["path_template"],
            body=body, receipt=receipt,
        ))

    @tool(
        operation=DERIVED_LAYERS_REPLACE,
        name="derived_layers_replace",
        description=(
            "Replace an existing derived relation's definition. Asks you to"
            " approve first, showing what it is today and what it would"
            " become. Anything reading the relation sees the new definition"
            " once this completes."
        ),
    )
    async def derived_layers_replace(
        ctx: Context,
        name: str,
        query: str,
        sources: list[str],
        id_column: str,
        geometry_column: str,
        kind: str | None = None,
        description: str | None = None,
        background: bool | None = None,
    ) -> dict:
        """Replacing is the quiet one, and the packet is why.

        A drop announces itself: the platform refuses it while anything reads
        the relation. A replace does not -- every layer reading it keeps
        working and starts returning different numbers. So the packet carries
        the current definition beside the proposed one, and the dependents,
        because "what reads this" is the question a person should be asked
        here and is not asked by the operation itself.
        """
        current = _derived_entry(name)
        body = _derived_definition(
            name=name, query=query, sources=sources,
            id_column=id_column, geometry_column=geometry_column,
            kind=kind or current.get("kind"), description=description,
        )
        body["confirmed"] = True
        if background is not None:
            body["background"] = background
        dependents = _derived_dependents(name)
        packet = {
            "summary": f"Replace the definition of {name}.",
            "changeCount": 1,
            "changes": [{
                "op": "replace", "path": f"derived_layers.{name}",
                "was": _change_preview(current.get("query")),
                "becomes": _change_preview(query),
            }],
            "dependents": dependents,
            "note": (
                "Everything listed under dependents keeps working and starts"
                " returning different numbers."
                if dependents["workspaceLayers"] or dependents["derivedLayers"]
                else "Nothing else reads this relation."
            ),
        }
        target = DERIVED_LAYERS_REPLACE["path_template"].replace(
            "{name}", quote(name, safe="")
        )
        receipt = await approval_gate(
            ctx, DERIVED_LAYERS_REPLACE, path=target, body=body,
            tool_name="derived_layers_replace", packet=packet,
        )
        return _without_meta(spend(
            DERIVED_LAYERS_REPLACE, path=target, body=body, receipt=receipt,
        ))

    @tool(
        operation=DERIVED_LAYERS_REFRESH,
        name="derived_layers_refresh",
        description=(
            "Recompute a materialised derived relation from its sources. The"
            " definition does not change; the numbers do. Asks you to approve"
            " first. Pass background=true for a long one and follow it with"
            " operations_show."
        ),
    )
    async def derived_layers_refresh(
        ctx: Context, name: str, background: bool | None = None,
    ) -> dict:
        """The mildest of the four, and still asked about.

        It changes no definition and drops nothing -- it recomputes. What makes
        it worth a person's attention is cost: a refresh reads every source row
        again, and an agent that could fire it unattended would be a way to
        spend the database's time.
        """
        current = _derived_entry(name)
        body = {"confirmed": True}
        if background is not None:
            body["background"] = background
        target = DERIVED_LAYERS_REFRESH["path_template"].replace(
            "{name}", quote(name, safe="")
        )
        receipt = await approval_gate(
            ctx, DERIVED_LAYERS_REFRESH, path=target, body=body,
            tool_name="derived_layers_refresh",
            packet={
                "summary": f"Recompute {name} from its sources.",
                "changeCount": 1,
                "changes": [{"op": "refresh",
                             "path": f"derived_layers.{name}",
                             "was": current.get("refreshedAt"),
                             "becomes": "recomputed now"}],
                "sources": current.get("sources") or [],
            },
        )
        return _without_meta(spend(
            DERIVED_LAYERS_REFRESH, path=target, body=body, receipt=receipt,
        ))

    @tool(
        operation=DERIVED_LAYERS_DROP,
        name="derived_layers_drop",
        description=(
            "Permanently remove a managed derived relation, named exactly."
            " Refused outright while any layer or other derived relation"
            " still reads it -- remove those first. Asks you to approve, and"
            " the approval names what would break. This cannot be undone:"
            " recovering means rebuilding the relation."
        ),
    )
    async def derived_layers_drop(ctx: Context, name: str) -> dict:
        """The first genuinely destructive tool on this surface.

        Four things hold it, and none of them is that the scope is hard to
        get. The relation is named exactly, never matched by pattern or
        inferred from what was discussed. The dependents are read *before*
        anybody is asked, so a drop that the platform would refuse is refused
        here instead of costing somebody a prompt. What would break travels in
        the approval, so the person sees the consequence and not just the
        verb. And the receipt is single-use and bound to this one relation.

        The platform refuses an in-use drop on its own; this does not rely on
        that, and the platform does not rely on this. Both, on purpose.
        """
        current = _derived_entry(name)
        dependents = _derived_dependents(name)
        blocking = dependents["workspaceLayers"] + dependents["derivedLayers"]
        if blocking:
            # Refused before asking. The platform would refuse this too, and
            # a person prompted to approve something that cannot happen has
            # been asked to spend attention on nothing.
            raise ToolError(
                f"{name} cannot be dropped: "
                + ", ".join(sorted(blocking))
                + " still read it. Remove those first -- each removal is its"
                " own proposal and its own approval."
            )
        body = {"confirmed": True}
        target = DERIVED_LAYERS_DROP["path_template"].replace(
            "{name}", quote(name, safe="")
        )
        receipt = await approval_gate(
            ctx, DERIVED_LAYERS_DROP, path=target, body=body,
            tool_name="derived_layers_drop",
            packet={
                "summary": f"Permanently remove the derived relation {name}."
                           " This cannot be undone.",
                "changeCount": 1,
                "changes": [{"op": "remove",
                             "path": f"derived_layers.{name}",
                             "was": _change_preview(current.get("query")),
                             "becomes": None}],
                "kind": current.get("kind"),
                "sources": current.get("sources") or [],
                # Empty by the time anybody sees this -- a non-empty list was
                # refused above. Carried anyway, so the person reading the
                # approval can see the question was asked.
                "dependents": dependents,
            },
        )
        return _without_meta(spend(
            DERIVED_LAYERS_DROP, path=target, body=body, receipt=receipt,
        ))

    def _derived_definition(
        *, name, query, sources, id_column, geometry_column, kind, description,
    ):
        """The definition body, built once and in one order.

        `kind` defaults to a view because that is the cheap, always-correct
        choice: a view costs nothing to create and recomputes on read. A
        materialised relation is faster and stale until refreshed, which is a
        decision somebody should make rather than inherit from a default.
        """
        body = {
            "name": name,
            "kind": kind or "view",
            "query": query,
            "sources": list(sources),
            "idColumn": id_column,
            "geometryColumn": geometry_column,
        }
        if description is not None:
            body["description"] = description
        return body

    def _derived_entry(name):
        """The relation as it stands, so a change can be described against it.

        Read rather than assumed. A packet that described the proposed state
        alone would ask somebody to approve a change without showing them what
        is being changed.
        """
        payload = spend(
            DERIVED_LAYERS_LIST, path=DERIVED_LAYERS_LIST["path_template"]
        )
        entries = payload.get("derivedLayers")
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
        known = sorted(
            entry.get("name") for entry in entries
            if isinstance(entry, dict) and entry.get("name")
        ) if isinstance(entries, list) else []
        raise ToolError(
            f"No derived layer {name!r} on this instance."
            + (f" Managed relations: {', '.join(known)}." if known else "")
        )

    def _derived_dependents(name):
        """What else reads this relation, from the platform's own graph.

        `dependencies.list` is the read the dashboard uses to decide whether a
        delete is blocked, so this asks the same question of the same answer
        rather than inferring it from the workspace.
        """
        payload = spend(
            DEPENDENCIES_LIST, path=DEPENDENCIES_LIST["path_template"]
        )
        rows = payload.get("dependencies")
        relation = f"derived_layers.{name}"
        workspace_layers: list = []
        derived_layers: list = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("relation") != relation:
                continue
            for found in row.get("workspaceLayers") or []:
                if found not in workspace_layers:
                    workspace_layers.append(found)
            for found in row.get("derivedLayers") or []:
                if found not in derived_layers:
                    derived_layers.append(found)
        return {
            "workspaceLayers": sorted(workspace_layers),
            "derivedLayers": sorted(derived_layers),
        }

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
        revision = payload.get("revision")
        proposals = payload.get("proposals")
        entries = []
        for proposal in (proposals if isinstance(proposals, list) else []):
            proposal = proposal if isinstance(proposal, dict) else {}
            entry = _proposal_summary(proposal)
            applicable = _applicability(proposal, revision)
            if applicable is not None:
                entry["applicability"] = applicable
            entries.append(entry)
        if status is not None:
            entries = [entry for entry in entries if entry["status"] == status]
        return {
            "revision": revision,
            "proposals": entries,
            # Counted here because the difference is the point: this instance
            # reported 19 pending, of which 3 could actually be applied.
            "pendingApplicable": sum(
                1 for e in entries if e.get("applicability") == "applicable"
            ),
            "pendingSuperseded": sum(
                1 for e in entries if e.get("applicability") == "superseded"
            ),
        }

    @tool(
        operation=PROPOSALS_PREVIEW_PLAN,
        name="proposals_preview_plan",
        description=(
            "What rendering a proposed change would involve: the layer as the"
            " proposal would leave it, the background layers, the effective"
            " filter and any warnings. Renders nothing and costs nothing to"
            " run. Use it before the screenshot to check the right thing"
            " would be drawn."
        ),
    )
    def proposals_preview_plan(
        proposal_id: str,
        layer: str,
        locale: str | None = None,
    ) -> dict:
        """The cheap half of review evidence.

        It answers what *would* be rendered without starting a browser, which
        makes it the right first call: a plan naming the wrong layer, or
        warning that the source cannot be rendered faithfully, is worth knowing
        before spending thirty seconds on a screenshot that will say the same
        thing less clearly.

        The title it reports is the proposal's, not the workspace's, which is
        how you can tell it is reading the candidate.
        """
        path = PROPOSALS_PREVIEW_PLAN["path_template"].replace(
            "{proposalId}", quote(proposal_id, safe="")
        )
        body = {"layer": layer}
        if locale is not None:
            body["locale"] = locale
        payload = _without_meta(
            spend(PROPOSALS_PREVIEW_PLAN, path=path, body=body)
        )
        return payload

    @tool(
        operation=PROPOSALS_PREVIEW_SCREENSHOT,
        name="proposals_preview_screenshot",
        description=(
            "Render a proposed change through a real browser and attach the"
            " images to the proposal, so a person can see what it would look"
            " like before applying it. Returns where the images are, whether"
            " the checks passed, and which failed. Applies nothing. Takes"
            " about five seconds."
        ),
    )
    def proposals_preview_screenshot(
        proposal_id: str,
        layer: str,
        hover: bool = False,
        locale: str | None = None,
    ) -> dict:
        """The evidence a reviewer actually looks at.

        Run in the foreground deliberately. The platform will background this
        and hand back an operation to poll, but `operations_show` costs
        `derive` while the configuration API demands `visual` to inspect an
        operation of this kind -- so an agent that backgrounded it could start
        a screenshot and then not be allowed to read the outcome. Waiting the
        five seconds avoids inventing a way around that.

        `hover` defaults to False and True is refused, which is a limit of
        this surface rather than of the platform.

        Measured against one proposal: `hover=false` renders in 14.4 seconds,
        `hover=true` in 97.1, and omitting it entirely in 100 to 166 -- the
        hover path drives real pointer interaction and waits on tooltips. A
        token B lives sixty seconds, so a hover render cannot finish before the
        credential authorising it expires. Attempting one spends a minute and
        returns "the configuration API is unavailable", which is both slow and
        untrue.

        So it is refused up front, with the alternative named. The dashboard
        and the CLI hold a session rather than a request-bound credential and
        can wait as long as it takes.

        Omitting `hover` would be the honest default and is not available for
        the same reason: the platform reads an absent hover as "decide for
        yourself", and what it decides costs more than the credential has. A
        separate configuration-API bug made an absent hover fail outright
        rather than merely slowly; that is fixed, and this limit is what
        remains.
        """
        if hover:
            raise ToolError(
                "A hover render takes about 97 seconds and the credential"
                " authorising this request lives 60, so it cannot complete"
                " here. Run it from the dashboard or the CLI, which hold a"
                " session rather than a per-request credential. Without hover"
                " this returns in about 15 seconds."
            )
        path = PROPOSALS_PREVIEW_SCREENSHOT["path_template"].replace(
            "{proposalId}", quote(proposal_id, safe="")
        )
        body = {"layer": layer, "hover": hover}
        if locale is not None:
            body["locale"] = locale
        return _visual_outcome(
            spend(PROPOSALS_PREVIEW_SCREENSHOT, path=path, body=body)
        )

    @tool(
        operation=PROPOSALS_PREVIEW_TEST,
        name="proposals_preview_test",
        description=(
            "Render a proposed change and compare it against the workspace as"
            " it stands, reporting whether the checks passed and which did"
            " not. The same evidence as the screenshot, judged rather than"
            " just captured. Applies nothing."
        ),
    )
    def proposals_preview_test(
        proposal_id: str,
        layer: str,
        hover: bool = False,
        locale: str | None = None,
    ) -> dict:
        """The judged form: the platform decides whether the render is
        acceptable rather than leaving a person to compare two images.

        The same hover limit applies and for the same measured reason; see
        `proposals_preview_screenshot`.
        """
        if hover:
            raise ToolError(
                "A hover render takes about 97 seconds and the credential"
                " authorising this request lives 60, so it cannot complete"
                " here. Run it from the dashboard or the CLI, which hold a"
                " session rather than a per-request credential. Without hover"
                " this returns in about 15 seconds."
            )
        path = PROPOSALS_PREVIEW_TEST["path_template"].replace(
            "{proposalId}", quote(proposal_id, safe="")
        )
        body = {"layer": layer, "hover": hover}
        if locale is not None:
            body["locale"] = locale
        return _visual_outcome(
            spend(PROPOSALS_PREVIEW_TEST, path=path, body=body)
        )

    @tool(
        operation=PROPOSALS_CREATE,
        name="proposals_create",
        description=(
            "Add a workspace change to the review queue. Takes the same"
            " `operations` and `revision` as proposals_check plus the"
            " `checkFingerprint` it returned, so a proposal can only be made"
            " from a change that was validated. Applies nothing: a person"
            " reviews and applies separately."
        ),
    )
    def proposals_create(
        operations: list[dict],
        revision: str,
        check_fingerprint: str,
        explanation: str | None = None,
    ) -> dict:
        """The first tool here that changes durable state, and the narrowest
        way to do it.

        What it writes is a queue entry. The workspace is untouched, the map
        serves what it served before, and somebody has to read the proposal and
        apply it for anything to happen -- which they can do with the tools
        that already exist, since `proposals_show` returns the diff.

        `check_fingerprint` is required here although the platform accepts a
        create without one. The fingerprint binds this proposal to a specific
        validated candidate: supply a stale one and the platform refuses rather
        than proposing something nobody checked. Making it required means an
        agent cannot propose except from a change it has already run through
        `proposals_check`, which costs one extra call and removes the whole
        class of proposal that was never validated.

        The reply is summarised for the reason `proposals_show` summarises one:
        the platform returns the proposal with `original` and `candidate`
        attached -- 32,579 bytes for a one-line rename, of which 31,640 is the
        workspace twice.
        """
        body = {
            "revision": revision,
            "operations": operations,
            "checkFingerprint": check_fingerprint,
        }
        if explanation is not None:
            body["explanation"] = explanation
        payload = spend(
            PROPOSALS_CREATE, path=PROPOSALS_CREATE["path_template"], body=body
        )
        proposal = payload.get("proposal")
        proposal = proposal if isinstance(proposal, dict) else {}
        diff = proposal.get("diff")
        warnings = proposal.get("warnings")
        detail = dict(_proposal_summary(proposal))
        detail["explanation"] = proposal.get("explanation")
        detail["originalRevision"] = proposal.get("originalRevision")
        detail["warnings"] = warnings if isinstance(warnings, list) else []
        detail["changes"] = [
            _change_summary(entry)
            for entry in (diff if isinstance(diff, list) else [])
        ]
        return detail

    @tool(
        operation=SEMANTIC_PROPOSALS_CREATE,
        name="semantic_proposals_create",
        description=(
            "Add a change to one catalogued asset's curated meaning to the"
            " semantic review queue. Takes the assetId, baseVersion,"
            " `operations` and the `fingerprint` from"
            " semantic_proposals_check. Applies nothing."
        ),
    )
    def semantic_proposals_create(
        asset_id: str,
        base_version: int,
        operations: list[dict],
        fingerprint: str,
        explanation: str | None = None,
    ) -> dict:
        """The semantic counterpart. The platform already requires the
        fingerprint here, so check-then-create is its rule rather than this
        tool's addition."""
        body = {
            "assetId": asset_id,
            "baseVersion": base_version,
            "operations": operations,
            "fingerprint": fingerprint,
        }
        if explanation is not None:
            body["explanation"] = explanation
        payload = spend(
            SEMANTIC_PROPOSALS_CREATE,
            path=SEMANTIC_PROPOSALS_CREATE["path_template"],
            body=body,
        )
        proposal = payload.get("proposal")
        proposal = proposal if isinstance(proposal, dict) else {}
        diff = proposal.get("diff")
        return {
            "proposalId": proposal.get("id"),
            "status": proposal.get("status"),
            "assetId": proposal.get("assetId"),
            "baseVersion": proposal.get("baseVersion"),
            "catalogRevision": payload.get("catalogRevision"),
            "changes": [
                _semantic_change_summary(entry)
                for entry in (diff if isinstance(diff, list) else [])
            ],
        }

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
        detail["currentRevision"] = payload.get("revision")
        applicable = _applicability(proposal, payload.get("revision"))
        if applicable is not None:
            detail["applicability"] = applicable
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
        operation=PROPOSALS_APPLY,
        name="proposals_apply",
        description=(
            "Apply a queued proposal: write its change to the workspace and"
            " reload the map. Asks you to approve first, showing what it would"
            " change, and does nothing until you agree. Takes a proposalId"
            " from proposals_list. Pass evidence_operation_id -- the operation a"
            " proposals_preview_* run returned -- to put the rendered evidence"
            " in front of the person deciding. This is not reversible by an"
            " undo; recovering means proposing the inverse change."
        ),
    )
    async def proposals_apply(
        ctx: Context, proposal_id: str, evidence_operation_id: str | None = None
    ) -> dict:
        """The first tool that changes what the map serves.

        Everything before this either read the instance or added to a queue a
        person works through. This empties that queue, so the question it has
        to answer is not "may this grant apply" -- an operator settled that --
        but "does the person here, now, want this particular change".

        The packet is read before asking, not composed from the arguments.
        What a person is shown has to come from the proposal the platform
        holds; a summary assembled from what the model passed in would let the
        agent describe its own change.
        """
        proposal = _apply_packet(proposal_id, evidence_operation_id)
        target = PROPOSALS_APPLY["path_template"].replace(
            "{proposalId}", quote(proposal_id, safe="")
        )
        # Built once and passed to both. The digest the approval binds and the
        # digest the credential binds are computed from this same object, so
        # they cannot describe different requests.
        body = {"approved": True}
        receipt = await approval_gate(
            ctx, PROPOSALS_APPLY, path=target, body=body,
            tool_name="proposals_apply", packet=proposal,
        )
        return _applied(spend(
            PROPOSALS_APPLY, path=target, body=body, receipt=receipt,
        ))

    @tool(
        operation=SEMANTIC_PROPOSALS_APPLY,
        name="semantic_proposals_apply",
        description=(
            "Apply a queued semantic proposal, writing the proposed meaning"
            " into the catalog. Asks you to approve first. Takes a proposalId"
            " from semantic_proposals_list."
        ),
    )
    async def semantic_proposals_apply(ctx: Context, proposal_id: str) -> dict:
        """The same decision for curated meaning.

        Separate from the workspace apply and separately scoped, because they
        change different things and an operator should be able to hand over
        one without the other.
        """
        packet = _semantic_apply_packet(proposal_id)
        target = SEMANTIC_PROPOSALS_APPLY["path_template"].replace(
            "{proposalId}", quote(proposal_id, safe="")
        )
        body = {"confirmed": True}
        receipt = await approval_gate(
            ctx, SEMANTIC_PROPOSALS_APPLY, path=target, body=body,
            tool_name="semantic_proposals_apply", packet=packet,
        )
        return _without_meta(spend(
            SEMANTIC_PROPOSALS_APPLY, path=target, body=body, receipt=receipt,
        ))

    @tool(
        operation=XYZ_RELOAD,
        name="xyz_reload",
        description=(
            "Ask the tile service to pick up the workspace already on disk."
            " Writes nothing and applies nothing. Use this when an apply"
            " committed but reported that the reload was not observed --"
            " xyz_status says whether that is the case. Asks you to approve"
            " first."
        ),
    )
    async def xyz_reload(ctx: Context) -> dict:
        """Recovery, not part of the ordinary loop.

        `proposals_apply` already reloads; this exists for the case it cannot
        cover, where the apply committed and the reload did not complete
        within the wait. Without it the only way out is an operator at a
        terminal, which is exactly the situation an agent is supposed to make
        rarer.

        It still asks a person. A reload writes nothing, so the reason is not
        the change -- it is that an unattended reload is the one operation on
        this surface an agent could usefully repeat, and the tile service
        would be the thing paying for it.
        """
        body = {"confirmed": True}
        receipt = await approval_gate(
            ctx, XYZ_RELOAD, path=XYZ_RELOAD["path_template"], body=body,
            tool_name="xyz_reload",
            packet={"summary": "Reload the map from the workspace on disk."},
        )
        return _without_meta(spend(
            XYZ_RELOAD, path=XYZ_RELOAD["path_template"], body=body,
            receipt=receipt,
        ))

    def _apply_packet(proposal_id, evidence_operation_id):
        """What the person deciding is shown, read from the platform.

        `proposals_show` is the same read a reviewer would do, so this is the
        diff the dashboard would show them and not a second description of it.
        A proposal that cannot be read cannot be approved: asking somebody to
        agree to a change nobody can describe is worse than refusing.
        """
        target = PROPOSALS_SHOW["path_template"].replace(
            "{proposalId}", quote(proposal_id, safe="")
        )
        payload = spend(PROPOSALS_SHOW, path=target)
        proposal = payload.get("proposal")
        proposal = proposal if isinstance(proposal, dict) else {}
        diff = proposal.get("diff")
        diff = diff if isinstance(diff, list) else []
        status = proposal.get("status")
        if status != "pending":
            # Refused before anybody is asked. A person asked to approve an
            # applied proposal would be agreeing to nothing, and the platform
            # would refuse afterwards anyway -- with a prompt already spent.
            raise ToolError(
                f"This proposal is {status}, so there is nothing to apply."
                " proposals_list shows which are still pending."
            )
        applicable = _applicability(proposal, payload.get("revision"))
        packet = {
            "summary": proposal.get("explanation")
            or f"Apply proposal {proposal_id}.",
            "changeCount": len(diff),
            # A window, not the diff. The whole thing can be hundreds of
            # entries and the person is deciding, not auditing -- the panel
            # says how many were left out and the proposal holds them all.
            "changes": [_change_summary(entry) for entry in diff[:20]],
            "proposalId": proposal.get("id"),
            "originalRevision": proposal.get("originalRevision"),
            "currentRevision": payload.get("revision"),
        }
        if applicable is not None:
            packet["applicability"] = applicable
        warnings = proposal.get("warnings")
        if isinstance(warnings, list) and warnings:
            packet["warnings"] = warnings
        if evidence_operation_id is not None:
            packet["evidence"] = _apply_evidence(evidence_operation_id)
        return packet

    def _apply_evidence(operation_id):
        """The rendered result of a preview run, for the person deciding.

        Read here rather than taken as an argument. The agent names which run
        to show; what that run found comes from the platform, so an agent
        cannot report a pass that did not happen.

        A failure here does not fail the apply. Reading an operation costs
        `derive`, which every preset but `discovery` carries and a hand-picked
        grant may not, and an apply that refuses because its *illustration*
        could not be fetched would be refusing the wrong thing. What is not
        done is dropping it quietly: the packet says evidence was asked for
        and why it is missing, so the person decides knowing there is a render
        they are not looking at.
        """
        target = OPERATIONS_SHOW["path_template"].replace(
            "{operationId}", quote(operation_id, safe="")
        )
        try:
            return _visual_outcome(spend(OPERATIONS_SHOW, path=target))
        except ToolError as refusal:
            return {
                "operationId": operation_id,
                "unavailable": str(refusal),
            }

    def _semantic_apply_packet(proposal_id):
        """The semantic counterpart, read the same way and for the reason."""
        target = SEMANTIC_PROPOSALS_SHOW["path_template"].replace(
            "{proposalId}", quote(proposal_id, safe="")
        )
        payload = _without_meta(spend(SEMANTIC_PROPOSALS_SHOW, path=target))
        proposal = payload.get("proposal")
        proposal = proposal if isinstance(proposal, dict) else payload
        changes = proposal.get("changes")
        changes = changes if isinstance(changes, list) else []
        return {
            "summary": proposal.get("explanation")
            or proposal.get("rationale")
            or f"Apply semantic proposal {proposal_id}.",
            "changeCount": len(changes),
            "changes": [_change_summary(entry) for entry in changes[:20]],
            "proposalId": proposal.get("id") or proposal_id,
        }

    def _applied(payload):
        """What happened, separating the write from the tile service.

        The platform answers 504 with the whole result when the workspace was
        written and the reload was not observed completing. That is two facts
        and they have different consequences: the change is live in the
        configuration either way, and whether the map serves it yet is what
        xyz_status answers and xyz_reload can retry. Reporting one boolean
        would collapse them and invite a second apply of a change that already
        happened.
        """
        payload = payload if isinstance(payload, dict) else {}
        proposal = payload.get("proposal")
        proposal = proposal if isinstance(proposal, dict) else {}
        reload_result = payload.get("reload")
        reload_result = reload_result if isinstance(reload_result, dict) else {}
        status = reload_result.get("status")
        status = status if isinstance(status, dict) else {}
        completed = bool(status.get("completed"))
        applied = {
            "applied": proposal.get("status") == "applied",
            "proposalId": proposal.get("id"),
            "appliedRevision": proposal.get("appliedRevision"),
            "mapReloaded": completed,
        }
        if not completed:
            applied["note"] = (
                "The change is applied and the workspace is updated. The tile"
                " service was asked to reload and had not confirmed it within"
                " the wait. Do not apply again -- check xyz_status, and use"
                " xyz_reload if it is still serving the old workspace."
            )
            if reload_result.get("error"):
                applied["reloadError"] = reload_result["error"]
        operation = payload.get("operation")
        if isinstance(operation, dict) and operation.get("id"):
            applied["operationId"] = operation["id"]
        return applied

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

    for document in GUIDANCE:
        _serve_guidance(server, document)

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
    # Exposed for the same reason and no other: the gate is deliberately not a
    # tool -- nothing the model can invoke reaches it -- so a test has no way
    # in through the tool surface. Wave 6's first mutating tool is what calls
    # it in earnest; until then this is how its behaviour is pinned.
    server.approval_gate = approval_gate
    # Exposed so a test can assert the bound holds, rather than assert that
    # the code that enforces it exists.
    server.remembered_approvals = remembered_approvals

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
        # Session state, which this server refused until Phase 1 wave 7.
        #
        # The reversal buys exactly one thing, and it is the thing the owner
        # asked for: a person approves a mutation in the session they are
        # working in, rather than at a dashboard over two tool calls. Without
        # a session there is no back-channel, and without a back-channel the
        # server cannot ask a client anything -- measured on 2026-09-20, where
        # forcing the elicitation capability on produced "Cannot send
        # 'elicitation/create': this transport context has no back-channel for
        # server-initiated requests".
        #
        # What it costs is `era_guard`'s obligation 4, and it is worth being
        # exact about what that obligation was. It never enforced the era
        # decision -- the guard does that, on the wire, and still does. What
        # it bought was a smaller surface: no server-side state keyed by an
        # identifier a client presents. That is given up knowingly.
        #
        # What did not change, checked rather than assumed. Authentication is
        # per request from the bearer token, so a session identifier alone
        # authorises nothing. And one session cannot answer another's
        # elicitation: a second session presenting a valid token and the right
        # request id is acked by the transport and never routed, leaving the
        # first call waiting. `CrossSessionElicitationTests` pins that, with a
        # positive control beside it -- a negative result from a harness that
        # cannot detect success would prove nothing.
        stateless_http=False,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            # Both spellings: a client may or may not carry the port, and an
            # allowlist that accepts one and refuses the other turns a correct
            # deployment into an intermittent 421.
            allowed_hosts=[host, f"{host}:*"],
            allowed_origins=[resource.origin, f"{resource.origin}:*"],
        ),
    )
