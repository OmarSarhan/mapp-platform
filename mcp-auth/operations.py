"""The operations a token B may be exchanged for.

The broker does not infer an operation from scope. Scope says what a grant is
*allowed* to reach; an operation says what this particular token authorises,
and the two are not interchangeable -- `apply` names a class of effect, while
`proposals.apply` on one proposal names the effect itself. A token bound only
by scope would work against every proposal the grant could reach.

Every entry here mirrors an action the configuration API already publishes in
``ACTION_SCHEMAS``. That is the source of truth, and it lives in another
service that this component cannot import at runtime, so the values are
restated rather than derived -- with ``test_the_allowlist_matches_the_platform``
comparing them against the real table, because a restated contract that nobody
compares is a contract that has already drifted.

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
    "layers.values": Operation(
        operation_id="layers.values",
        method="GET",
        path_template="/api/layers/{layerKey}/values",
        # The action declares scope 'derive' and additionally requires
        # 'semantic:inspect' to read the field it aggregates over.
        required_scopes=("derive", "semantic:inspect"),
        mutating=False,
    ),
    "derived-layers.refresh": Operation(
        operation_id="derived-layers.refresh",
        method="POST",
        path_template="/api/derived-layers/{name}/refresh",
        required_scopes=("derive",),
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
    "proposals.apply": Operation(
        operation_id="proposals.apply",
        method="POST",
        path_template="/api/proposals/{proposalId}/apply",
        required_scopes=("apply",),
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
