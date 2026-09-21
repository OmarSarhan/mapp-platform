"""The operations a token B may be exchanged for.

The broker does not infer an operation from scope. Scope says what a grant is
*allowed* to reach; an operation says what this particular token authorises,
and the two are not interchangeable -- `apply` names a class of effect, while
`proposals.apply` on one proposal names the effect itself. A token bound only
by scope would work against every proposal the grant could reach.

Every entry here mirrors an action the configuration API already publishes in
``ACTION_SCHEMAS``. That is the source of truth, and it lives in another
service that this component cannot import at runtime, so the values are
restated rather than derived -- with ``AllowlistDriftTests`` in
tests/test_exchange.py comparing them against the real table, because a
restated contract that nobody compares is a contract that has already
drifted.

Phase 0 deliberately allowlists a handful rather than all fifty-two: enough to
cover a read with a path parameter and a query, a mutation, and the flagship
high-consequence effect. Adding one is a deliberate act, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Operation:
    """One allowlisted configuration-API operation."""

    operation_id: str
    method: str
    path_template: str
    #: The scopes this operation needs. The exchange requires the request to
    #: name exactly these, and requires every one of them to be held by BOTH
    #: token A and the grant behind it -- the grant being what the operator
    #: actually approved on the consent screen.
    #: A superset of the action's own `scope`, because some actions need a
    #: second scope to read what they act on.
    required_scopes: tuple[str, ...]
    #: True when the effect is consequential enough that the token authorising
    #: it must not be replayable. These get single-use tokens.
    mutating: bool


#: Keyed by operation ID exactly as the configuration API names it.
OPERATIONS: dict[str, Operation] = {
    "catalog.list": Operation(
        operation_id="catalog.list",
        method="GET",
        path_template="/api/catalog",
        # Relation *metadata* -- schema, table, column names and types -- and no
        # row values. That is what makes it an `inspect` read: it describes the
        # shape of the data without reading any of it.
        required_scopes=("inspect",),
        mutating=False,
    ),
    "layers.list": Operation(
        operation_id="layers.list",
        method="GET",
        path_template="/api/layers",
        # The configuration API's GET catch-all scope. Listing layers exposes
        # workspace configuration and nothing from the data itself, which is why
        # it costs `inspect` -- the same discovery scope a client already holds
        # to see the tools at all -- rather than `derive`.
        required_scopes=("inspect",),
        mutating=False,
    ),
    "layers.values": Operation(
        operation_id="layers.values",
        method="GET",
        path_template="/api/layers/{layerKey}/values",
        # The action declares scope 'derive' and additionally requires
        # 'semantic:inspect' to read the field it aggregates over.
        required_scopes=("derive", "semantic:inspect"),
        mutating=False,
    ),
    "layers.statistics": Operation(
        operation_id="layers.statistics",
        method="GET",
        path_template="/api/layers/{layerKey}/statistics",
        # Same pair as layers.values and for the same reason: `derive` to read
        # the data, `semantic:inspect` to resolve the field it summarises.
        required_scopes=("derive", "semantic:inspect"),
        mutating=False,
    ),
    "semantic.catalog.export": Operation(
        operation_id="semantic.catalog.export",
        method="GET",
        path_template="/api/semantic/catalog",
        # Curated meaning, not data. `semantic:inspect` is the read scope for
        # the catalogue and is already in the recommended agent preset.
        required_scopes=("semantic:inspect",),
        mutating=False,
    ),
    "semantic.catalog.search": Operation(
        operation_id="semantic.catalog.search",
        method="GET",
        path_template="/api/semantic/catalog/search",
        required_scopes=("semantic:inspect",),
        mutating=False,
    ),
    "semantic.catalog.show": Operation(
        operation_id="semantic.catalog.show",
        method="GET",
        path_template="/api/semantic/catalog/objects/{assetId}",
        required_scopes=("semantic:inspect",),
        mutating=False,
    ),
    "derived-layers.list": Operation(
        operation_id="derived-layers.list",
        method="GET",
        path_template="/api/derived-layers",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "federation.aliases.list": Operation(
        operation_id="federation.aliases.list",
        method="GET",
        path_template="/api/federation/aliases",
        # The *read* federation scope. `federation:provision` is the one that
        # can serve a third-party database and is never needed to look.
        required_scopes=("federation:observe",),
        mutating=False,
    ),
    "federation.aliases.show": Operation(
        operation_id="federation.aliases.show",
        method="GET",
        path_template="/api/federation/aliases/{alias}",
        required_scopes=("federation:observe",),
        mutating=False,
    ),
    "derived-layers.plan": Operation(
        operation_id="derived-layers.plan",
        method="POST",
        path_template="/api/derived-layers/plan",
        # Two scopes, because the handler resolves semantic sources
        # before it probes anything and imposes its own gate on
        # them. A credential minted for the first alone is refused
        # by the platform for want of the second.
        required_scopes=("derive:manage", "semantic:inspect"),
        # A dry run: it probes the definition, returns what would happen and
        # a fingerprint, and creates nothing -- the same relationship
        # `proposals.check` has to `proposals.create`. Its risk class keeps it
        # out of the approval set for that reason: there is nothing to approve
        # because nothing happens.
        #
        # Single-use anyway. `database-plan` is not a read risk and is not
        # being reclassified as one to save an exchange: probing runs real
        # database work, and a replayable credential for it is a way to spend
        # that work twice. The cost of the conservative choice is one exchange
        # per plan, which is nothing.
        mutating=True,
    ),
    "derived-layers.refresh": Operation(
        operation_id="derived-layers.refresh",
        method="POST",
        path_template="/api/derived-layers/{name}/refresh",
        required_scopes=("derive:manage",),
        mutating=True,
    ),
    "derived-layers.create": Operation(
        operation_id="derived-layers.create",
        method="POST",
        path_template="/api/derived-layers",
        # Two scopes, because the handler resolves semantic sources
        # before it probes anything and imposes its own gate on
        # them. A credential minted for the first alone is refused
        # by the platform for want of the second.
        required_scopes=("derive:manage", "semantic:inspect"),
        mutating=True,
    ),
    "derived-layers.replace": Operation(
        operation_id="derived-layers.replace",
        method="POST",
        path_template="/api/derived-layers/{name}/replace",
        # Two scopes, because the handler resolves semantic sources
        # before it probes anything and imposes its own gate on
        # them. A credential minted for the first alone is refused
        # by the platform for want of the second.
        required_scopes=("derive:manage", "semantic:inspect"),
        mutating=True,
    ),
    "derived-layers.drop": Operation(
        operation_id="derived-layers.drop",
        method="POST",
        path_template="/api/derived-layers/{name}/drop",
        required_scopes=("derive:manage",),
        # The first genuinely destructive operation on this surface. Nothing
        # about the exchange treats it specially -- what guards it is the
        # approval, the platform's own refusal to drop a relation in use, and
        # the tool showing what would break before anybody is asked.
        mutating=True,
    ),
    "federation.aliases.observe": Operation(
        operation_id="federation.aliases.observe",
        method="POST",
        path_template="/api/federation/aliases/{alias}/observe",
        # The action's risk class is federation-observe but its declared scope
        # is federation:provision. The scope is what authorises, so the scope
        # is what is required here -- reading the risk class as if it were a
        # scope would admit a weaker grant.
        required_scopes=("federation:provision",),
        mutating=True,
    ),
    "sql.test": Operation(
        operation_id="sql.test",
        method="POST",
        path_template="/api/sql/test",
        required_scopes=("derive",),
        # Writes nothing: a READ ONLY transaction evaluating one allowlisted
        # scalar expression. POST because it carries a body, not because it
        # changes anything.
        mutating=False,
    ),
    "xyz.status": Operation(
        operation_id="xyz.status",
        method="GET",
        path_template="/api/xyz/status",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "operations.show": Operation(
        operation_id="operations.show",
        method="GET",
        path_template="/api/operations/{operationId}",
        required_scopes=("derive",),
        mutating=False,
    ),
    "derived-layers.capabilities": Operation(
        operation_id="derived-layers.capabilities",
        method="GET",
        path_template="/api/derived-layers/capabilities",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "sql.capabilities": Operation(
        operation_id="sql.capabilities",
        method="GET",
        path_template="/api/sql/capabilities",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "capabilities.list": Operation(
        operation_id="capabilities.list",
        method="GET",
        path_template="/api/capabilities",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "schema": Operation(
        operation_id="schema",
        method="GET",
        path_template="/api/schema",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "rules": Operation(
        operation_id="rules",
        method="GET",
        path_template="/api/rules",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "examples": Operation(
        operation_id="examples",
        method="GET",
        path_template="/api/examples",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "plugins.list": Operation(
        operation_id="plugins.list",
        method="GET",
        path_template="/api/plugins",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "dependencies.list": Operation(
        operation_id="dependencies.list",
        method="GET",
        path_template="/api/dependencies",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "icons.list": Operation(
        operation_id="icons.list",
        method="GET",
        path_template="/api/icons",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "semantic.source.relations": Operation(
        operation_id="semantic.source.relations",
        method="GET",
        path_template="/api/semantic/source/relations",
        required_scopes=("semantic:inspect", "semantic:source"),
        mutating=False,
    ),
    "semantic.status": Operation(
        operation_id="semantic.status",
        method="GET",
        path_template="/api/semantic/status",
        required_scopes=("semantic:inspect", ),
        mutating=False,
    ),
    "semantic.derived-profiles.list": Operation(
        operation_id="semantic.derived-profiles.list",
        method="GET",
        path_template="/api/semantic/derived-profiles",
        required_scopes=("semantic:inspect", ),
        mutating=False,
    ),
    "semantic.derived-profiles.show": Operation(
        operation_id="semantic.derived-profiles.show",
        method="GET",
        path_template="/api/semantic/derived-profiles/{name}",
        required_scopes=("semantic:inspect", ),
        mutating=False,
    ),
    "semantic.catalog.history": Operation(
        operation_id="semantic.catalog.history",
        method="GET",
        path_template="/api/semantic/catalog/objects/{assetId}/history",
        required_scopes=("semantic:inspect", ),
        mutating=False,
    ),
    "derived-layers.background-jobs": Operation(
        operation_id="derived-layers.background-jobs",
        method="GET",
        path_template="/api/derived-layers/background-jobs",
        required_scopes=("inspect", ),
        mutating=False,
    ),
    "derived-layers.map-extent": Operation(
        operation_id="derived-layers.map-extent",
        method="GET",
        path_template="/api/derived-layers/map-extent",
        required_scopes=("inspect", ),
        mutating=False,
    ),
    "federation.groups.list": Operation(
        operation_id="federation.groups.list",
        method="GET",
        path_template="/api/federation/groups",
        required_scopes=("federation:observe", ),
        mutating=False,
    ),
    "proposals.preview-plan": Operation(
        operation_id="proposals.preview-plan",
        method="POST",
        path_template="/api/proposals/{proposalId}/visual-plan",
        required_scopes=("visual",),
        # Renders a proposed change and attaches the result to the proposal.
        # It writes an artifact and starts an asynchronous operation, so the
        # credential is not replayable -- but it applies nothing: the workspace
        # the map serves is untouched either way.
        mutating=True,
    ),
    "proposals.preview-test": Operation(
        operation_id="proposals.preview-test",
        method="POST",
        path_template="/api/proposals/{proposalId}/visual-test",
        required_scopes=("visual",),
        mutating=True,
    ),
    "proposals.preview-screenshot": Operation(
        operation_id="proposals.preview-screenshot",
        method="POST",
        path_template="/api/proposals/{proposalId}/screenshot",
        required_scopes=("visual",),
        mutating=True,
    ),
    "proposals.create": Operation(
        operation_id="proposals.create",
        method="POST",
        path_template="/api/proposals",
        required_scopes=("propose",),
        # Writes a proposal record and no workspace. Single-use anyway: the
        # risk class is `propose`, which is not a read class, and a replayed
        # create would add a second identical entry to somebody's queue.
        mutating=True,
    ),
    "semantic.proposals.create": Operation(
        operation_id="semantic.proposals.create",
        method="POST",
        path_template="/api/semantic/proposals",
        required_scopes=("semantic:propose",),
        mutating=True,
    ),
    "approvals.create": Operation(
        operation_id="approvals.create",
        method="POST",
        path_template="/api/approvals",
        required_scopes=("inspect",),
        # Writes a pending row and authorises nothing. Not single-use: asking
        # twice produces two requests for permission, which is untidy rather
        # than dangerous, and both still need a person.
        mutating=False,
    ),
    "approvals.claim": Operation(
        operation_id="approvals.claim",
        method="POST",
        path_template="/api/approvals/claim",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "approvals.confirm": Operation(
        operation_id="approvals.confirm",
        method="POST",
        path_template="/api/approvals/confirm",
        required_scopes=("inspect",),
        # Relays a decision a person made in their MCP client, for the clients
        # that can elicit a form but cannot send anybody to a browser. Costs
        # `inspect` because the authority is the person's, not the grant's --
        # what the grant buys is the ability to *ask*. The platform cannot
        # verify the elicitation any more than the broker can verify a request
        # digest; see the threat model's division of trust.
        mutating=False,
    ),
    "proposals.check": Operation(
        operation_id="proposals.check",
        method="POST",
        path_template="/api/proposals/check",
        required_scopes=("propose",),
        # A read that costs a write scope. It validates operations against a
        # revision and returns warnings and a fingerprint; it writes nothing,
        # so the credential is not single-use -- replaying it re-validates the
        # same operations and changes nothing.
        mutating=False,
    ),
    "semantic.proposals.check": Operation(
        operation_id="semantic.proposals.check",
        method="POST",
        path_template="/api/semantic/proposals/check",
        required_scopes=("semantic:propose",),
        mutating=False,
    ),
    "proposals.show": Operation(
        operation_id="proposals.show",
        method="GET",
        path_template="/api/proposals/{proposalId}",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "proposals.list": Operation(
        operation_id="proposals.list",
        method="GET",
        path_template="/api/proposals",
        required_scopes=("inspect",),
        mutating=False,
    ),
    "semantic.proposals.list": Operation(
        operation_id="semantic.proposals.list",
        method="GET",
        path_template="/api/semantic/proposals",
        required_scopes=("semantic:inspect",),
        mutating=False,
    ),
    "semantic.proposals.show": Operation(
        operation_id="semantic.proposals.show",
        method="GET",
        path_template="/api/semantic/proposals/{proposalId}",
        required_scopes=("semantic:inspect",),
        mutating=False,
    ),
    "proposals.apply": Operation(
        operation_id="proposals.apply",
        method="POST",
        path_template="/api/proposals/{proposalId}/apply",
        required_scopes=("apply",),
        mutating=True,
    ),
    "xyz.reload": Operation(
        operation_id="xyz.reload",
        method="POST",
        path_template="/api/xyz/reload",
        required_scopes=("reload",),
        # Tells the tile service to pick up the workspace on disk. It changes
        # no data and applies nothing -- it makes an already-applied change
        # visible. Allowlisted for the case `proposals.apply` cannot cover: an
        # apply that committed and then answered 504 because the reload was
        # not observed, where re-requesting one is the recovery and the only
        # alternative is an operator at a terminal.
        mutating=True,
    ),
    "semantic.proposals.apply": Operation(
        operation_id="semantic.proposals.apply",
        method="POST",
        path_template="/api/semantic/proposals/{proposalId}/apply",
        required_scopes=("semantic:apply",),
        mutating=True,
    ),
}


class UnknownOperation(LookupError):
    """The operation is not allowlisted for exchange."""


def lookup(operation_id: str) -> Operation:
    """Resolve an operation ID, refusing anything not allowlisted.

    Refusing by default is the whole mechanism: an operation the broker does
    not know about cannot be exchanged for, so a new configuration-API action
    is unreachable through the broker until someone deliberately adds it here.
    """
    if not isinstance(operation_id, str) or operation_id not in OPERATIONS:
        raise UnknownOperation(operation_id)
    return OPERATIONS[operation_id]


def all_required_scopes() -> frozenset[str]:
    """Every scope some allowlisted operation needs.

    The authorization server derives its acceptance vocabulary from this. It
    used to restate one, and the two disagreed: four of the five operations
    here required scopes the server refused to issue, so they could never be
    exchanged for at all. Nothing caught it because every test built its own
    vocabulary -- the drift was only visible by walking the real flow.
    """
    return frozenset(
        scope
        for operation in OPERATIONS.values()
        for scope in operation.required_scopes
    )
