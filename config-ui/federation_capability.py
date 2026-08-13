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

from federation_schema import (
    FederationSchemaError,
    enforce_tls_policy,
    validate_observation,
)
from relation_identity import IDENTIFIER_PART_RE, parse_relation
from semantic_sources import PostgresSemanticSources

CONNECT_TIMEOUT_SECONDS = 5
# The statement timeout for every read-only probe in this file comes from
# PostgresSemanticSources._begin_read_only's own SET LOCAL statement_timeout
# (semantic_sources.py) — there used to be a STATEMENT_TIMEOUT_MS constant
# here too, but nothing ever wired it to that (or any) timeout, so editing
# it silently had no effect. Removed rather than wired up: this module
# deliberately reuses the same bounded transaction semantic_sources.py
# already established (see this file's own module docstring), so its
# timeout should stay a single source of truth there, not be duplicated
# here as a second, easily-desynchronized constant.


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
) -> tuple[bool, bool, str]:
    """Returns (all_present_and_selectable, row_level_security_detected,
    schema_fingerprint).

    A relation that no longer exists, or that this connection can no
    longer SELECT, must not be silently skipped — it means the schema
    Discover verified has changed from what was registered, and the
    caller must not report it as "current" evidence.

    "row_level_security_detected" also covers a security-barrier view
    (docs/federation-architecture-waypoint.md: "If any allowlisted
    relation has RLS or a security-barrier view enabled...") — a view
    marked security_barrier is commonly how per-user row filtering is
    implemented without native RLS, and the same "every MAPP caller
    shares one mapped remote user" bypass risk applies to it.

    "schema_fingerprint" combines each allowed relation's column list
    (name/full type including modifiers/nullability, in ordinal position)
    with, for a view, its actual defining query text — closing a gap
    physical_identity can't: an ADD COLUMN on an already-allowlisted
    table, or a CREATE OR REPLACE VIEW that narrows or drops a row-
    filtering predicate while keeping the same output columns, changes
    neither the relation's own oid nor its RLS/security-barrier flags, so
    it would otherwise be invisible to every other check this module
    runs. The type is captured via format_type(atttypid, atttypmod), not
    atttypid alone — a bare type oid is unchanged by a type-modifier-only
    edit (varchar length, numeric precision/scale, or — most importantly
    on a platform built around spatial data — a PostGIS geometry column's
    subtype/SRID, both encoded entirely in the typmod), so atttypid alone
    would let exactly that class of edit through unreviewed. A relation
    gone missing gets the literal string "missing" here too — its absence
    must change the fingerprint, not be silently skipped, for the same
    fail-closed reason as all_present above."""
    all_present = True
    rls_detected = False
    fingerprints = []
    for entry in allowed_relations:
        schema, relation = _parsed_schema_relation(entry)
        cursor.execute(
            """
            SELECT
              relrowsecurity
              OR COALESCE(
                   reloptions && ARRAY['security_barrier=true'], false
                 ) AS bypasses_per_user_access,
              md5(
                COALESCE(
                  (
                    SELECT string_agg(
                      a.attname || ':'
                        || format_type(a.atttypid, a.atttypmod)
                        || ':' || a.attnotnull::text,
                      ',' ORDER BY a.attnum
                    )
                    FROM pg_catalog.pg_attribute AS a
                    WHERE a.attrelid = c.oid
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                  ),
                  ''
                )
                || '|' ||
                COALESCE(
                  CASE WHEN c.relkind = 'v'
                    THEN pg_get_viewdef(c.oid, true)
                  END,
                  ''
                )
              ) AS definition_fingerprint
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
            """,
            (schema, relation),
        )
        row = cursor.fetchone()
        if row is None or not _selectable(cursor, schema, relation):
            all_present = False
            fingerprints.append("missing")
            continue
        if row["bypasses_per_user_access"]:
            rls_detected = True
        fingerprints.append(row["definition_fingerprint"])
    return all_present, rls_detected, "|".join(fingerprints)


def _selectable(cursor: Any, schema: str, relation: str) -> bool:
    """Actually attempt a bounded, zero-row SELECT rather than trusting
    has_table_privilege() alone — a PostgreSQL 15+ security_invoker view
    evaluates privilege on its *underlying* relations using the calling
    role, so has_table_privilege() on the view itself can be true while
    selecting it fails in practice (verified live: a reader granted
    SELECT only on such a view, not its base table, is reported
    privileged but gets "permission denied" on an actual SELECT).

    A permission failure aborts the surrounding transaction unless
    contained — this only ever runs inside detect_capability()'s single
    bounded read-only transaction, which must keep working for whatever
    allowed_relations entry comes next and for the physical-identity read
    afterward. SAVEPOINT/ROLLBACK TO SAVEPOINT contains the failure to
    just this probe, same as a plain ROLLBACK would for the whole
    transaction; the same savepoint name is safely reused across loop
    iterations since it never survives past its own rollback."""
    cursor.execute("SAVEPOINT relation_selectable")
    try:
        cursor.execute(
            sql.SQL("SELECT 1 FROM {}.{} WHERE FALSE").format(
                sql.Identifier(schema), sql.Identifier(relation)
            )
        )
        return True
    except psycopg.Error:
        return False
    finally:
        cursor.execute("ROLLBACK TO SAVEPOINT relation_selectable")


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


def _physical_identity_from_cursor(
    cursor: Any, allowed_relations: tuple[str, ...]
) -> str:
    """The remote's actual physical database identity — the cluster's
    system_identifier, the connected database's own oid, and each
    allowed relation's own oid, joined together.

    system_identifier changes if the whole cluster was rebuilt or
    restored from scratch onto the same connection parameters; the
    database oid changes if just this database was dropped and
    recreated within an otherwise-unchanged cluster. Neither changes
    across a physical/PITR restore or an in-place logical restore that
    never drops the database — both replace the source's actual
    contents while preserving every cluster/database-level marker. Each
    relation's own oid does change whenever it's recreated (as any
    `pg_restore --clean`-style restore does to every object it
    restores), so tracking it closes that gap; a relation that's gone
    missing entirely also changes the identity rather than being
    silently skipped, which is the same fail-closed direction
    `federation_capability._verify_allowed_relations` already takes.

    Takes an already-open, already-`_begin_read_only`'d cursor rather than
    opening its own connection: computing this from a *separate* connection
    than whatever verified `allowed_relations`' schema would let the remote
    drop and recreate a relation in between, pairing a physical identity for
    the *new* relation with schema evidence that only ever inspected the
    *old* one — Provision would then see that new identity still match and
    import the replacement relation without it ever having been verified."""
    cursor.execute("SELECT system_identifier FROM pg_control_system()")
    system_identifier = cursor.fetchone()["system_identifier"]
    cursor.execute(
        "SELECT oid FROM pg_catalog.pg_database "
        "WHERE datname = current_database()"
    )
    database_oid = cursor.fetchone()["oid"]
    relation_oids = []
    for entry in allowed_relations:
        schema, relation = _parsed_schema_relation(entry)
        cursor.execute(
            "SELECT c.oid FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n "
            "ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s",
            (schema, relation),
        )
        row = cursor.fetchone()
        relation_oids.append(str(row["oid"]) if row else "missing")
    return f"{system_identifier}/{database_oid}/{','.join(relation_oids)}"


def detect_capability(
    connection_url: str,
    *,
    allowed_relations: tuple[str, ...],
    tls_policy: str,
    version_relation: str | None = None,
) -> tuple[dict[str, Any], datetime, str | None]:
    """Bounded, read-only capability and connectivity detection.

    `allowed_relations` are normalized "schema.relation" strings, matching
    `federation_schema.validate_registration()`'s output. `tls_policy` is
    the alias's registered requirement — enforced against the connection
    string's actual sslmode before connecting, the same as Provision, so a
    weak connectionRef is never even Observed successfully.
    `version_relation`, if given, must be one of `allowed_relations` — its
    single scalar column is read as the observation's `sourceVersion`,
    implementing the freshnessStrategy "versionRelation" evidence
    collection Discover calls for. Never reads anything not on the
    allowlist.

    Returns (observation, observed_at, physical_identity) — observation is
    already validated against `federation_schema.validate_observation()`'s
    closed contract. observed_at marks the moment this probe's REPEATABLE
    READ snapshot was actually fixed (or, on failure, the moment the
    connection was given up as failed) — a connection that stalls, or a
    probe descheduled between connecting and its first query, must not
    make this probe look "older" than a concurrent one that started later
    but whose snapshot was established first.
    FederationAliasStore.record_observation()'s ordering depends on this:
    it compares observed_at, not call order, to decide which of two
    overlapping Observe calls actually saw the more current state.
    physical_identity is computed from this same connection's snapshot when
    reachable (None otherwise) — see _physical_identity_from_cursor for why
    it must not be a second, separate connection."""
    if version_relation is not None and version_relation not in allowed_relations:
        raise FederationSchemaError(
            f"version_relation {version_relation!r} must be one of the "
            "registered allowedRelations."
        )
    enforce_tls_policy(tls_policy, connection_url)

    try:
        with psycopg.connect(
            connection_url,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                PostgresSemanticSources._begin_read_only(cursor)
                # REPEATABLE READ fixes its snapshot at the first query
                # that actually reads data, not at BEGIN and not at
                # connect() — a client-side datetime.now() taken any
                # earlier, even right after connect() succeeds, leaves a
                # scheduling gap (this process descheduled before its
                # first statement reaches the server) that a concurrent,
                # faster-scheduled Observe could establish a later
                # snapshot inside of. Asking the server for its own clock
                # as literally the first query closes that gap: the
                # returned value marks the same instant the snapshot was
                # established, not merely "sometime before it."
                cursor.execute("SELECT clock_timestamp() AS observed_at")
                observed_at = cursor.fetchone()["observed_at"]
                versions = extension_versions(cursor)
                relations_verified, rls_detected, schema_fingerprint = (
                    _verify_allowed_relations(cursor, allowed_relations)
                )
                source_version = (
                    _version_relation_scalar(cursor, version_relation)
                    if version_relation is not None
                    else None
                )
                physical_id = _physical_identity_from_cursor(
                    cursor, allowed_relations
                )
    except psycopg.Error as exc:
        # A rejected credential needs MAPP-side secret rotation, not a
        # wait — the architecture doc requires reporting it distinctly
        # from an outage (docs/federation-architecture-waypoint.md,
        # "Drift and retirement"). psycopg surfaces a connection-phase
        # auth rejection as a bare OperationalError with no SQLSTATE or
        # diagnostics (unlike a query-time error), so the server's FATAL
        # message text is the only signal available. Postgres
        # deliberately uses this same message for both a wrong password
        # and a nonexistent role, to avoid confirming which usernames
        # exist — either way, the remote credential needs attention.
        return validate_observation({
            "connectivity": (
                "unauthorized"
                if "password authentication failed" in str(exc)
                else "unavailable"
            ),
            "schema": "unknown",
            "sourceFreshness": "unknown",
            "lastConnected": None,
            "lastSchemaVerified": None,
            "sourceVersion": None,
        }), datetime.now(timezone.utc), None

    return validate_observation({
        "connectivity": "reachable",
        "schema": "current" if relations_verified else "changed",
        "sourceFreshness": "unknown",
        "lastConnected": _now_iso(),
        "lastSchemaVerified": _now_iso(),
        "sourceVersion": source_version,
        "extensionVersions": versions,
        "rowLevelSecurityDetected": rls_detected,
        "schemaFingerprint": schema_fingerprint,
    }), observed_at, physical_id


def verify_remote_state(
    connection_url: str, allowed_relations: tuple[str, ...]
) -> tuple[str, dict[str, str], bool, bool, str]:
    """Provision's own standalone, live re-check of the remote's physical
    identity, current extension versions, relation existence/selectability,
    RLS/security-barrier exposure, and column/view-definition fingerprint —
    deliberately a fresh connection, not shared with any prior Observe,
    since the whole point is to catch drift that happened *since* Observe's
    snapshot (docs/federation-architecture-waypoint.md, "Drift and
    retirement": "Different physical database | Raise identity conflict;
    require explicit rebind"). A connectionRef's host/port/dbname/user can
    stay byte-for-byte identical while pointing at a genuinely different
    physical database — nothing about the connection string itself would
    ever reveal a same-name replacement.

    Returns (physical_identity, extension_versions, relations_verified,
    row_level_security_detected, schema_fingerprint) — all five collected
    from this one connection's snapshot for the same reason
    `_physical_identity_from_cursor` gives for not being a second connection
    off of Observe's: an in-place extension upgrade, a newly-enabled RLS
    policy, an added column, or a redefined view all change no OID at all,
    so identity alone can't catch any of them, and a decision (pushdown
    safety, the row_level_security_acknowledged gate, or schema currency)
    made against evidence read from a *separate* live connection could
    itself be stale by the time another one of these checks runs — the same
    class of TOCTOU gap as pairing Observe's schema evidence with a second
    connection's physical identity. Provision must not trust
    last_observation's stored rowLevelSecurityDetected/schemaFingerprint any
    more than it trusts a stored physical identity or extension-version set.

    Raises psycopg.Error on a connection failure — callers already
    reachability-gate this (Observe via detect_capability, Provision via
    its own preceding checks), so a failure here is never the first
    sign of an unreachable source."""
    with psycopg.connect(
        connection_url,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            PostgresSemanticSources._begin_read_only(cursor)
            identity = _physical_identity_from_cursor(cursor, allowed_relations)
            versions = extension_versions(cursor)
            relations_verified, rls_detected, schema_fingerprint = (
                _verify_allowed_relations(cursor, allowed_relations)
            )
    return (
        identity,
        versions,
        relations_verified,
        rls_detected,
        schema_fingerprint,
    )
