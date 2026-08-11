"""Closed validation contracts for federation identities.

Covers exactly the two record shapes the federation architecture waypoint
document (docs/federation-architecture-waypoint.md) specifies precisely: the
Source alias registration (and its Register-step input) and the Observation
record. Both are pure structural/value contracts — no storage, no live
connection, no FDW object — since neither a federation database nor a
registry backing store exists yet. That is deliberate: this module is the
"design-and-test slice" the document's Recommended first implementation task
calls for, not an early start on Waypoint 2.

`physicalIdentity` is intentionally not validated here. The document leaves
its exact evidence fields open pending a real target hosting platform
(decision #10) — inventing a closed shape for it now would be exactly the
kind of premature precision the rest of this module tries to avoid.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import Any

from control_plane import parse_time
from relation_identity import IDENTIFIER_PART_RE, parse_relation

# Must match ALIAS_RE in semantic_sources.py and DB_KEY in workspace_schema.py
# — one alias grammar, not three (federation architecture waypoint, decision
# #12). Duplicated rather than imported for the same reason
# IDENTIFIER_PART_RE is: avoiding a dependency-chain import for one regex.
# Max length 56, not PostgreSQL's usual 63: a federation alias becomes the
# schema name `source_<alias>` (federation_store.py), and "source_" is 7
# bytes — decision #12's original 63-char bound left no room for that
# prefix and would silently truncate/collide for longer aliases.
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,55}$")

ALIAS_KINDS = frozenset({"postgresql"})
ALIAS_STATUSES = frozenset({"pending", "active", "unavailable", "retired"})
FRESHNESS_STRATEGIES = frozenset(
    {"manual", "maximumAge", "timestampColumn", "versionRelation"}
)
TLS_POLICIES = frozenset({"require", "verify-ca", "verify-full"})

CONNECTIVITY_STATES = frozenset({"reachable", "unavailable", "unknown"})
SCHEMA_STATES = frozenset({"current", "changed", "unknown"})
SOURCE_FRESHNESS_STATES = frozenset(
    {"current", "possibly_stale", "stale", "unknown"}
)

MAX_DISPLAY_NAME = 200
MAX_CONNECTION_REF = 200
MAX_DATA_HANDLING_CLASSIFICATION = 2000


class FederationSchemaError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status: int = HTTPStatus.BAD_REQUEST,
        code: str = "federation.schema_invalid",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _closed_object(
    value: Any,
    *,
    label: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FederationSchemaError(f"{label} must be an object.")
    missing = sorted(required - set(value))
    if missing:
        raise FederationSchemaError(
            f"{label} is missing required properties: " + ", ".join(missing)
        )
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise FederationSchemaError(
            f"Unknown {label} properties: " + ", ".join(unknown)
        )
    return value


def _bounded_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise FederationSchemaError(
            f"{label} must be non-empty text of at most {maximum} characters."
        )
    return value.strip()


def _enum(value: Any, *, label: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise FederationSchemaError(
            f"{label} must be one of: {', '.join(sorted(allowed))}."
        )
    return value


def _timestamp_or_none(value: Any, *, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise FederationSchemaError(
            f"{label} must be an ISO-8601 timestamp string or null."
        )
    try:
        parse_time(value)
    except ValueError as exc:
        raise FederationSchemaError(f"{label}: {exc}") from exc
    return value


def validate_alias(value: Any) -> str:
    if not isinstance(value, str) or not ALIAS_RE.fullmatch(value):
        raise FederationSchemaError(
            "alias must start with a letter and contain only letters, "
            "numbers, hyphens, or underscores (63 characters max)."
        )
    return value


def _normalized_allowed_relations(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or isinstance(value, (str, bytes))
    ):
        raise FederationSchemaError(
            "allowedRelations must be a non-empty list of schema-qualified "
            "relation names."
        )
    normalized = []
    for entry in value:
        parsed = parse_relation(
            entry, alias=None, part_pattern=IDENTIFIER_PART_RE
        )
        if parsed is None:
            raise FederationSchemaError(
                f"allowedRelations entry {entry!r} must be a "
                "schema-qualified identifier."
            )
        _, schema, relation = parsed
        normalized.append(f"{schema}.{relation}")
    if len(set(normalized)) != len(normalized):
        raise FederationSchemaError("allowedRelations must not contain duplicates.")
    # provision() imports every allowedRelations entry into one local
    # source_<alias> schema (federation_store.py) — two remote schemas with
    # a same-named table (e.g. public.orders and archive.orders) would both
    # resolve to a single local "orders", so the second IMPORT FOREIGN
    # SCHEMA fails and the alias can never be provisioned. Reject that here,
    # at registration, instead of surfacing it later as a cryptic
    # provisioning failure.
    basenames = [entry.split(".", 1)[1] for entry in normalized]
    if len(set(basenames)) != len(basenames):
        raise FederationSchemaError(
            "allowedRelations must not import two relations with the same "
            "name from different schemas — each becomes one local table "
            "under source_<alias>."
        )
    return tuple(sorted(normalized))


def validate_registration(payload: Any) -> dict[str, Any]:
    """Validate a Source lifecycle step 1 (Register) input payload.

    Returns a normalized record with defaults applied (`freshnessStrategy`
    defaults to `manual` when omitted, per the lifecycle's "optional
    freshness strategy") and `status` set to `pending`. Does not touch
    `registeredBy` — that is attributed from the authenticated principal by
    the caller, not supplied in this payload.
    """
    fields = _closed_object(
        payload,
        label="Alias registration",
        required=frozenset({
            "alias",
            "displayName",
            "kind",
            "connectionRef",
            "tlsPolicy",
            "allowedRelations",
            "dataHandlingClassification",
            "dataHandlingAcknowledged",
        }),
        optional=frozenset({"freshnessStrategy"}),
    )

    alias = validate_alias(fields["alias"])
    display_name = _bounded_text(
        fields["displayName"], label="displayName", maximum=MAX_DISPLAY_NAME
    )
    kind = _enum(fields["kind"], label="kind", allowed=ALIAS_KINDS)
    connection_ref = _bounded_text(
        fields["connectionRef"],
        label="connectionRef",
        maximum=MAX_CONNECTION_REF,
    )
    tls_policy = _enum(
        fields["tlsPolicy"], label="tlsPolicy", allowed=TLS_POLICIES
    )
    allowed_relations = _normalized_allowed_relations(fields["allowedRelations"])
    data_handling_classification = _bounded_text(
        fields["dataHandlingClassification"],
        label="dataHandlingClassification",
        maximum=MAX_DATA_HANDLING_CLASSIFICATION,
    )
    if fields["dataHandlingAcknowledged"] is not True:
        raise FederationSchemaError(
            "dataHandlingAcknowledged must be explicitly true — the "
            "registering principal must acknowledge licensing, attribution, "
            "and personal-data implications before registration completes."
        )
    freshness_strategy = _enum(
        fields.get("freshnessStrategy", "manual"),
        label="freshnessStrategy",
        allowed=FRESHNESS_STRATEGIES,
    )

    return {
        "alias": alias,
        "displayName": display_name,
        "kind": kind,
        "connectionRef": connection_ref,
        "tlsPolicy": tls_policy,
        "allowedRelations": allowed_relations,
        "dataHandlingClassification": data_handling_classification,
        "dataHandlingAcknowledged": True,
        "freshnessStrategy": freshness_strategy,
        "status": "pending",
    }


def validate_observation(payload: Any) -> dict[str, Any]:
    """Validate an Observe-step (lifecycle step 7) observation record.

    Covers exactly the freshness enum block from "Observation versus truth",
    plus the extension-version and row-level-security evidence Discover
    (step 2) also records. `extensionVersions` and `rowLevelSecurityDetected`
    are optional so a pre-Discover observation (connectivity-only) can still
    validate.
    """
    fields = _closed_object(
        payload,
        label="Observation",
        required=frozenset({
            "connectivity",
            "schema",
            "sourceFreshness",
            "lastConnected",
            "lastSchemaVerified",
            "sourceVersion",
        }),
        optional=frozenset({"extensionVersions", "rowLevelSecurityDetected"}),
    )

    connectivity = _enum(
        fields["connectivity"],
        label="connectivity",
        allowed=CONNECTIVITY_STATES,
    )
    schema_state = _enum(
        fields["schema"], label="schema", allowed=SCHEMA_STATES
    )
    source_freshness = _enum(
        fields["sourceFreshness"],
        label="sourceFreshness",
        allowed=SOURCE_FRESHNESS_STATES,
    )
    last_connected = _timestamp_or_none(
        fields["lastConnected"], label="lastConnected"
    )
    last_schema_verified = _timestamp_or_none(
        fields["lastSchemaVerified"], label="lastSchemaVerified"
    )

    source_version = fields["sourceVersion"]
    if source_version is not None and not isinstance(
        source_version, (str, int, float)
    ):
        raise FederationSchemaError(
            "sourceVersion must be an opaque scalar value or null."
        )

    result = {
        "connectivity": connectivity,
        "schema": schema_state,
        "sourceFreshness": source_freshness,
        "lastConnected": last_connected,
        "lastSchemaVerified": last_schema_verified,
        "sourceVersion": source_version,
    }

    extension_versions = fields.get("extensionVersions")
    if extension_versions is not None:
        versions = _closed_object(
            extension_versions,
            label="extensionVersions",
            required=frozenset(),
            optional=frozenset({"postgresql", "postgis", "proj", "geos"}),
        )
        for name, version in versions.items():
            if not isinstance(version, str) or not version.strip():
                raise FederationSchemaError(
                    f"extensionVersions.{name} must be non-empty text."
                )
        result["extensionVersions"] = dict(versions)

    rls_detected = fields.get("rowLevelSecurityDetected")
    if rls_detected is not None:
        if not isinstance(rls_detected, bool):
            raise FederationSchemaError(
                "rowLevelSecurityDetected must be a boolean or omitted."
            )
        result["rowLevelSecurityDetected"] = rls_detected

    return result
