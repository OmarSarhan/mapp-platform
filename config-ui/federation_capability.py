"""Read-only capability and connectivity detection for a registered alias.

Implements the "genuinely new work" Waypoint 1 (non-invasive external read
mode) calls for: connectivity, schema-compatibility, and extension-version
evidence as a first-class, separately reported fact. Builds on the same
bounded read-only transaction already established in semantic_sources.py
(`PostgresSemanticSources._begin_read_only`) rather than a second discovery
path, matching the Discover lifecycle step's own instruction to extend the
existing bounded metadata read.

Freshness *state* is deliberately not assessed here. Comparing a version
signal against history is the verifier's job once an alias registry with
observation history exists (Waypoint 2) — this function only collects the
raw evidence Discover's bullet list calls for and reports `sourceFreshness`
as "unknown", the honest answer for a single bounded pass with no baseline
to compare against.

`detect_capability()` never raises for an expected external-source failure
(unreachable host, authentication failure, insufficient privilege, timeout)
— those produce a valid observation with `connectivity: "unavailable"`
instead. That returned-not-raised failure response is deliberate: whoever
requested the check always gets a reportable result, never a crash. It does
raise FederationSchemaError for a caller/configuration mistake (a
version_relation that isn't on the allowlist, or whose closed contract is
violated) — that is a distinct failure class from source unavailability and
must surface to whoever registered it, not be folded into "unavailable".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from federation_schema import FederationSchemaError, validate_observation
from relation_identity import IDENTIFIER_PART_RE, parse_relation
from semantic_sources import PostgresSemanticSources

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parsed_schema_relation(value: str) -> tuple[str, str]:
    parsed = parse_relation(value, alias=None, part_pattern=IDENTIFIER_PART_RE)
    if parsed is None:
        raise FederationSchemaError(
            f"{value!r} must be a schema-qualified identifier."
        )
    _, schema, relation = parsed
    return schema, relation


def extension_versions(cursor: Any) -> dict[str, str]:
    """PostgreSQL/PostGIS/PROJ/GEOS versions visible on `cursor`'s own
    connection. Used both for a remote alias (Discover/Observe evidence)
    and for the federation database itself (federation_store.py's
    version-match gate for postgres_fdw's `extensions` option — see
    docs/federation-architecture-waypoint.md's "Decided" pushdown-safety
    rule)."""
    cursor.execute("SELECT current_setting('server_version') AS version")
    versions = {"postgresql": cursor.fetchone()["version"]}

    cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")
    if not cursor.fetchone():
        return versions

    # PostGIS_Lib_Version() reports the actual linked library — the signal
    # that governs operator/function *behavior* — not
    # pg_extension.extversion, which only reflects the installed SQL
    # extension script and can lag behind a library upgrade until
    # `ALTER EXTENSION postgis UPDATE` runs (PostGIS_Full_Version()'s own
    # "[EXTENSION] ... needs upgrade" note is exactly this drift — using
    # extversion here would compare stale script bookkeeping, not whether
    # the two sides would actually evaluate an expression identically).
    cursor.execute("SELECT PostGIS_Lib_Version() AS version")
    lib_row = cursor.fetchone()
    if lib_row and lib_row["version"]:
        versions["postgis"] = lib_row["version"]

    cursor.execute("SELECT PostGIS_PROJ_Version() AS version")
    proj_row = cursor.fetchone()
    if proj_row and proj_row["version"]:
        versions["proj"] = proj_row["version"]

    cursor.execute("SELECT PostGIS_GEOS_Version() AS version")
    geos_row = cursor.fetchone()
    if geos_row and geos_row["version"]:
        versions["geos"] = geos_row["version"]

    return versions


def _verify_allowed_relations(
    cursor: Any, allowed_relations: tuple[str, ...]
) -> tuple[bool, bool]:
    """Returns (all_present_and_selectable, row_level_security_detected).

    A relation that no longer exists, or that this connection can no
    longer SELECT, must not be silently skipped — it means the schema
    Discover verified has changed from what was registered, and the
    caller must not report it as "current" evidence.

    "row_level_security_detected" also covers a security-barrier view
    (docs/federation-architecture-waypoint.md: "If any allowlisted
    relation has RLS or a security-barrier view enabled...") — a view
    marked security_barrier is commonly how per-user row filtering is
    implemented without native RLS, and the same "every MAPP caller
    shares one mapped remote user" bypass risk applies to it."""
    all_present = True
    rls_detected = False
    for entry in allowed_relations:
        schema, relation = _parsed_schema_relation(entry)
        cursor.execute(
            """
            SELECT
              relrowsecurity
              OR COALESCE(
                   reloptions && ARRAY['security_barrier=true'], false
                 ) AS bypasses_per_user_access
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND has_table_privilege(c.oid, 'SELECT')
            """,
            (schema, relation),
        )
        row = cursor.fetchone()
        if row is None:
            all_present = False
            continue
        if row["bypasses_per_user_access"]:
            rls_detected = True
    return all_present, rls_detected


def _version_relation_scalar(
    cursor: Any, version_relation: str
) -> str | int | float | None:
    schema, relation = _parsed_schema_relation(version_relation)
    cursor.execute(
        sql.SQL("SELECT * FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(relation)
        )
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise FederationSchemaError(
            "versionRelation must be a closed contract returning exactly one "
            f"row (got {len(rows)})."
        )
    (row,) = rows
    if len(row) != 1:
        raise FederationSchemaError(
            "versionRelation must be a closed contract returning exactly one "
            f"bounded scalar column (got {len(row)})."
        )
    (value,) = row.values()
    if value is not None and not isinstance(value, (str, int, float)):
        raise FederationSchemaError(
            "versionRelation's scalar value must be text, a number, or null."
        )
    return value


def detect_capability(
    connection_url: str,
    *,
    allowed_relations: tuple[str, ...],
    version_relation: str | None = None,
) -> dict[str, Any]:
    """Bounded, read-only capability and connectivity detection.

    `allowed_relations` are normalized "schema.relation" strings, matching
    `federation_schema.validate_registration()`'s output. `version_relation`,
    if given, must be one of `allowed_relations` — its single scalar column
    is read as the observation's `sourceVersion`, implementing the
    freshnessStrategy "versionRelation" evidence collection Discover calls
    for. Never reads anything not on the allowlist.

    Always returns a dict already validated against
    `federation_schema.validate_observation()`'s closed contract.
    """
    if version_relation is not None and version_relation not in allowed_relations:
        raise FederationSchemaError(
            f"version_relation {version_relation!r} must be one of the "
            "registered allowedRelations."
        )

    try:
        with psycopg.connect(
            connection_url,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                PostgresSemanticSources._begin_read_only(cursor)
                versions = extension_versions(cursor)
                relations_verified, rls_detected = _verify_allowed_relations(
                    cursor, allowed_relations
                )
                source_version = (
                    _version_relation_scalar(cursor, version_relation)
                    if version_relation is not None
                    else None
                )
    except psycopg.Error:
        return validate_observation({
            "connectivity": "unavailable",
            "schema": "unknown",
            "sourceFreshness": "unknown",
            "lastConnected": None,
            "lastSchemaVerified": None,
            "sourceVersion": None,
        })

    return validate_observation({
        "connectivity": "reachable",
        "schema": "current" if relations_verified else "changed",
        "sourceFreshness": "unknown",
        "lastConnected": _now_iso(),
        "lastSchemaVerified": _now_iso(),
        "sourceVersion": source_version,
        "extensionVersions": versions,
        "rowLevelSecurityDetected": rls_detected,
    })
