"""Closed validation contracts shared by the federation API and store."""

from __future__ import annotations

import re
from http import HTTPStatus
from math import isfinite
from typing import Any

import psycopg

from control_plane import parse_time
from relation_identity import IDENTIFIER_PART_RE, parse_relation

# Must match ALIAS_RE in semantic_sources.py and DB_KEY in workspace_schema.py
# — one alias grammar, not three (federation architecture waypoint, decision
# #12). Duplicated rather than imported for the same reason
# IDENTIFIER_PART_RE is: avoiding a dependency-chain import for one regex.
# Max length 56, not PostgreSQL's usual 63: a federation alias becomes the
# schema name `source_<alias>` (federation_store.py), and "source_" is 7
# bytes — decision #12's original 63-char bound left no room for that
# prefix and would silently truncate/collide for longer aliases. No hyphen:
# a federation alias must also be usable, unquoted, as a schema/relation
# name component elsewhere (semantic_sources.py's IDENTIFIER_RE,
# derived_layers.py's IDENTIFIER_PART_RE) — those already reject hyphens,
# so an alias containing one could register and provision but never be
# synced into the semantic catalog or used as a derived-layer source.
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,55}$")

ALIAS_KINDS = frozenset({"postgresql"})
ALIAS_STATUSES = frozenset({"pending", "active", "unavailable", "retired"})
FRESHNESS_STRATEGIES = frozenset({"manual"})
TLS_POLICIES = frozenset({"require", "verify-ca", "verify-full"})

CONNECTIVITY_STATES = frozenset(
    {"reachable", "unavailable", "unauthorized", "unknown"}
)
SCHEMA_STATES = frozenset({"current", "changed", "unknown"})
SOURCE_FRESHNESS_STATES = frozenset(
    {"current", "possibly_stale", "stale", "unknown"}
)

MAX_DISPLAY_NAME = 200
MAX_CONNECTION_REF = 200
MAX_DATA_HANDLING_CLASSIFICATION = 2000
MAX_ALLOWED_RELATIONS = 100

# A source may carry a handful of labels; ten is generous for a fact that
# grants nothing. MAX_GROUP_DESCRIPTION matches MAX_DISPLAY_NAME above --
# both bound a short human sentence in a payload.
MAX_GROUPS_PER_ALIAS = 10
MAX_GROUP_DESCRIPTION = 200
MAX_IDENTIFIER_PART = 63
MAX_SOURCE_VERSION = 200


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
    if not isinstance(value, str) or value not in allowed:
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
            "numbers, or underscores (56 characters max)."
        )
    return value


def validate_group_name(value: Any) -> str:
    """Validate a federation group label.

    Reuses ALIAS_RE unmodified. The 56-character bound is inherited rather
    than derived: an alias is bounded because it becomes a source_<alias>
    schema and "source_" spends 7 of PostgreSQL's 63 bytes, whereas a group
    name becomes no database identifier at all. One grammar, because the name
    is a URL path segment and the route patterns reuse this body verbatim --
    a second grammar would let the regex and the validator drift.

    Case-sensitive, matching alias handling: "leeds" and "Leeds" are distinct.
    """
    if not isinstance(value, str) or not ALIAS_RE.fullmatch(value):
        raise FederationSchemaError(
            "group name must start with a letter and contain only letters, "
            "numbers, or underscores (56 characters max)."
        )
    return value


def _normalized_group_membership(value: Any) -> tuple[str, ...]:
    """Normalise an alias's whole label set.

    Unlike allowedRelations this may be empty -- an empty label set is the
    normal state and the only way to clear one. Duplicates are refused rather
    than absorbed: repeating a name is an operator mistake worth surfacing.
    Sorted at write, so the stored array renders and compares deterministically.
    """
    if not isinstance(value, list) or isinstance(value, (str, bytes)):
        raise FederationSchemaError("groups must be a list of group names.")
    if len(value) > MAX_GROUPS_PER_ALIAS:
        raise FederationSchemaError(
            f"groups must name at most {MAX_GROUPS_PER_ALIAS} groups."
        )
    names = [validate_group_name(item) for item in value]
    if len(set(names)) != len(names):
        raise FederationSchemaError("groups must not repeat a group name.")
    return tuple(sorted(names))


def validate_group_membership(payload: Any) -> tuple[str, ...]:
    fields = _closed_object(
        payload,
        label="Group membership",
        required=frozenset({"groups"}),
    )
    return _normalized_group_membership(fields["groups"])


def validate_group_definition(payload: Any) -> dict[str, Any]:
    fields = _closed_object(
        payload,
        label="Group definition",
        required=frozenset({"name"}),
        optional=frozenset({"description"}),
    )
    description = fields.get("description")
    return {
        "name": validate_group_name(fields["name"]),
        # None is stored as SQL NULL; "" is a 400 rather than a silent NULL,
        # because _bounded_text rejects empty and whitespace-only text.
        "description": (
            None
            if description is None
            else _bounded_text(
                description,
                label="description",
                maximum=MAX_GROUP_DESCRIPTION,
            )
        ),
    }


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
    if len(value) > MAX_ALLOWED_RELATIONS:
        raise FederationSchemaError(
            f"allowedRelations must contain at most {MAX_ALLOWED_RELATIONS} "
            "relations."
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
        if len(schema) > MAX_IDENTIFIER_PART or len(relation) > MAX_IDENTIFIER_PART:
            raise FederationSchemaError(
                "allowedRelations schema and relation names must be at most "
                f"{MAX_IDENTIFIER_PART} characters."
            )
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
    plus the extension-version, row-level-security, and schema-fingerprint
    evidence Discover (step 2) also records. `extensionVersions`,
    `rowLevelSecurityDetected`, and `schemaFingerprint` are optional so a
    pre-Discover observation (connectivity-only) can still validate.
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
        optional=frozenset({
            "extensionVersions", "rowLevelSecurityDetected", "schemaFingerprint",
        }),
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
    valid_source_version = (
        source_version is None
        or (
            isinstance(source_version, str)
            and len(source_version) <= MAX_SOURCE_VERSION
        )
        or (
            isinstance(source_version, int)
            and not isinstance(source_version, bool)
            and len(str(source_version)) <= MAX_SOURCE_VERSION
        )
        or (
            isinstance(source_version, float)
            and isfinite(source_version)
        )
    )
    if not valid_source_version:
        raise FederationSchemaError(
            "sourceVersion must be null or a finite text/number scalar of "
            f"at most {MAX_SOURCE_VERSION} characters."
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
            optional=frozenset(
                {"postgresql", "postgis", "postgisExtversion", "proj", "geos"}
            ),
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

    schema_fingerprint = fields.get("schemaFingerprint")
    if schema_fingerprint is not None:
        if not isinstance(schema_fingerprint, str) or not schema_fingerprint:
            raise FederationSchemaError(
                "schemaFingerprint must be non-empty text or omitted."
            )
        result["schemaFingerprint"] = schema_fingerprint

    return result


# libpq sslmode values in increasing strictness. Only require/verify-ca/
# verify-full are valid tlsPolicy values (TLS_POLICIES above) — disable/
# allow/prefer aren't meaningful things to *require*.
_SSLMODE_STRENGTH = {
    "disable": 0,
    "allow": 1,
    "prefer": 2,
    "require": 3,
    "verify-ca": 4,
    "verify-full": 5,
}

_SUPPORTED_CONNECTION_OPTIONS = frozenset({
    "dbname",
    "gssencmode",
    "host",
    "hostaddr",
    "password",
    "port",
    "sslmode",
    "sslrootcert",
    "user",
})


def enforce_tls_policy(tls_policy: str, connection_url: str) -> None:
    """Reject a connectionRef whose actual sslmode is weaker than the
    alias's registered tlsPolicy. Registration validates tlsPolicy as an
    attestation of what the operator requires, but nothing previously
    checked the FEDERATION_DBS_<NAME> connection string actually delivers
    it — a registered "verify-full" alias could observe and provision over
    plaintext (sslmode=disable) without ever being flagged."""
    _enum(tls_policy, label="tlsPolicy", allowed=TLS_POLICIES)
    try:
        params = psycopg.conninfo.conninfo_to_dict(connection_url)
    except psycopg.Error as exc:
        raise FederationSchemaError(
            "connectionRef must be valid PostgreSQL connection information."
        ) from exc

    unsupported = sorted(set(params) - _SUPPORTED_CONNECTION_OPTIONS)
    if unsupported:
        raise FederationSchemaError(
            "connectionRef contains unsupported options: "
            + ", ".join(unsupported)
            + "."
        )

    required = ("host", "dbname", "user", "password")
    if any(not str(params.get(name, "")).strip() for name in required):
        raise FederationSchemaError(
            "connectionRef must explicitly set a TCP host, database, user, "
            "and password."
        )

    if any(
        "," in str(params.get(name, ""))
        for name in ("host", "hostaddr", "port")
    ):
        raise FederationSchemaError(
            "connectionRef must identify exactly one TCP endpoint."
        )
    if str(params["host"]).startswith("/"):
        raise FederationSchemaError(
            "connectionRef must use explicit TCP hosts; Unix sockets are "
            "not valid for federation TLS."
        )

    if params.get("gssencmode") != "disable":
        raise FederationSchemaError(
            "connectionRef must set gssencmode=disable so its registered "
            "TLS policy cannot be bypassed by GSS encryption."
        )

    sslrootcert = params.get("sslrootcert")
    if sslrootcert not in (None, "system"):
        raise FederationSchemaError(
            "connectionRef may only use sslrootcert=system; filesystem TLS "
            "credentials are not shared safely with the FDW runtime."
        )
    if tls_policy in {"verify-ca", "verify-full"} and sslrootcert != "system":
        raise FederationSchemaError(
            f"tlsPolicy {tls_policy!r} requires sslrootcert=system."
        )

    sslmode = str(params.get("sslmode", "prefer"))
    if _SSLMODE_STRENGTH.get(sslmode, -1) < _SSLMODE_STRENGTH[tls_policy]:
        raise FederationSchemaError(
            f"This connectionRef's sslmode {sslmode!r} does not meet the "
            f"registered tlsPolicy {tls_policy!r}.",
            code="federation.tls_policy_not_met",
        )
