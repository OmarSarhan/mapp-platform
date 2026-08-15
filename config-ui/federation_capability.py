"""Bounded, read-only capability detection for a registered source alias.

Expected source failures become observations; invalid caller input raises a
FederationSchemaError. Freshness remains unknown because one probe has no
historical baseline.
"""

from __future__ import annotations

import json
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


def _database_default_collation_identity(
    cursor: Any,
) -> tuple[str, str | None]:
    """Return database encoding and an attested PG17 default identity."""
    cursor.execute(
        """
        SELECT d.datlocprovider AS provider,
               pg_catalog.pg_encoding_to_char(d.encoding) AS encoding,
               pg_catalog.current_setting('server_version_num')::integer
                 / 10000 AS server_major,
               d.datcollate AS lc_collate,
               d.datctype AS lc_ctype,
               d.datlocale AS locale,
               d.daticurules AS icu_rules,
               d.datcollversion AS recorded_version,
               pg_catalog.pg_database_collation_actual_version(d.oid)
                 AS actual_version
        FROM pg_catalog.pg_database AS d
        WHERE d.datname = pg_catalog.current_database()
        """
    )
    row = cursor.fetchone()
    encoding = row["encoding"]
    provider = {"b": "builtin", "c": "libc", "i": "icu"}.get(
        row["provider"]
    )
    lc_collate = row["lc_collate"] if provider == "libc" else None
    lc_ctype = row["lc_ctype"] if provider == "libc" else None
    if lc_collate in {"C", "POSIX"}:
        lc_collate = "C/POSIX"
    if lc_ctype in {"C", "POSIX"}:
        lc_ctype = "C/POSIX"
    stable_c = (
        provider == "libc"
        and lc_collate == "C/POSIX"
        and lc_ctype == "C/POSIX"
    )
    recorded_version = row["recorded_version"]
    actual_version = row["actual_version"]
    if (
        provider is None
        or recorded_version != actual_version
        or (actual_version is None and not stable_c)
    ):
        return encoding, None
    identity = {
        "provider": provider,
        "encoding": encoding,
        "serverMajor": row["server_major"] if provider == "builtin" else None,
        "lcCollate": lc_collate,
        "lcCtype": lc_ctype,
        "locale": row["locale"] if provider != "libc" else None,
        "icuRules": row["icu_rules"] if provider == "icu" else None,
        "recordedVersion": recorded_version,
        "actualVersion": actual_version,
    }
    return encoding, json.dumps(identity, sort_keys=True, separators=(",", ":"))


def _collation_compatibility(
    cursor: Any,
    local_default_collation: tuple[str, str | None],
) -> tuple[bool, bool]:
    """Return portable-encoding and attested-default compatibility."""
    remote_encoding, remote_identity = _database_default_collation_identity(
        cursor
    )
    local_encoding, local_identity = local_default_collation
    encoding_matches = remote_encoding == local_encoding
    default_matches = (
        encoding_matches
        and remote_identity is not None
        and remote_identity == local_identity
    )
    return encoding_matches, default_matches


def extension_versions(cursor: Any) -> dict[str, str]:
    """PostgreSQL/PostGIS/PROJ/GEOS versions visible on `cursor`'s own
    connection. Used both for a remote alias (Discover/Observe evidence)
    and for the federation database itself (federation_store.py's
    version-match gate for postgres_fdw's `extensions` option — see
    docs/federation-architecture-waypoint.md's "Decided" pushdown-safety
    rule)."""
    cursor.execute("SELECT current_setting('server_version') AS version")
    versions = {"postgresql": cursor.fetchone()["version"]}

    cursor.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'postgis'"
    )
    extension_row = cursor.fetchone()
    if not extension_row:
        return versions

    # PostGIS_Lib_Version() reports the actual linked library — the signal
    # that governs operator/function *behavior* — not
    # pg_extension.extversion, which only reflects the installed SQL
    # extension script and can lag behind a library upgrade until
    # `ALTER EXTENSION postgis UPDATE` runs (PostGIS_Full_Version()'s own
    # "[EXTENSION] ... needs upgrade" note is exactly this drift). Neither
    # signal alone is sufficient for the pushdown-safety gate this feeds
    # (federation_store.py's _shippable_extensions): a same-library,
    # different-extversion pair still evaluates expressions identically,
    # but the *other* side's SQL catalog may be missing a function or
    # operator the newer script added — pushing down an expression that
    # uses it would fail at the SQL level, not just evaluate differently.
    # Both are captured and both must match.
    versions["postgisExtversion"] = extension_row["extversion"]
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
    cursor: Any,
    allowed_relations: tuple[str, ...],
    *,
    default_collation_matches: bool,
    portable_collation_encoding_matches: bool,
) -> tuple[bool, bool, str, dict[str, str]]:
    """Return selectability, access risk, schema and column-shape fingerprints.

    The fingerprint hashes canonical JSON containing each allowed relation's
    columns and view/RLS policy semantics. Qualified collation names and JSON
    fields avoid oid instability and delimiter collisions. Missing relations,
    unsupported kinds, and columns that cannot be imported fail closed. Types
    are limited to PostgreSQL's bootstrap catalog and the bundled database's
    public PostGIS extension. C/POSIX collations are portable between matching
    database encodings; the database default is importable only when its
    effective source and local identities match. A view attests itself, not
    policies of transitive dependencies; source administrators remain trusted
    for those.
    """
    all_present = True
    rls_detected = False
    fingerprints = []
    column_shapes = {}
    for entry in allowed_relations:
        schema, relation = _parsed_schema_relation(entry)
        cursor.execute(
            """
            SELECT
              relrowsecurity
              OR COALESCE(
                   reloptions && ARRAY['security_barrier=true'], false
                 ) AS bypasses_per_user_access,
              NOT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_attribute AS import_attribute
                JOIN pg_catalog.pg_type AS import_type
                  ON import_type.oid = import_attribute.atttypid
                JOIN pg_catalog.pg_namespace AS import_namespace
                  ON import_namespace.oid = import_type.typnamespace
                LEFT JOIN pg_catalog.pg_collation AS import_collation
                  ON import_collation.oid = import_attribute.attcollation
                LEFT JOIN pg_catalog.pg_namespace AS import_collation_namespace
                  ON import_collation_namespace.oid =
                       import_collation.collnamespace
                WHERE import_attribute.attrelid = c.oid
                  AND import_attribute.attnum > 0
                  AND NOT import_attribute.attisdropped
                  AND (
                    NOT (
                      (
                        import_namespace.nspname = 'pg_catalog'
                        AND import_type.oid < 16384
                      )
                      OR (
                        import_namespace.nspname = 'public'
                        AND EXISTS (
                          SELECT 1
                          FROM pg_catalog.pg_depend AS type_dependency
                          JOIN pg_catalog.pg_extension AS type_extension
                            ON type_extension.oid = type_dependency.refobjid
                          WHERE type_dependency.classid =
                                  'pg_catalog.pg_type'::pg_catalog.regclass
                            AND type_dependency.objid = import_type.oid
                            AND type_dependency.refclassid =
                                  'pg_catalog.pg_extension'::pg_catalog.regclass
                            AND type_dependency.deptype = 'e'
                            AND type_extension.extname = 'postgis'
                        )
                      )
                    )
                    OR (
                      import_attribute.attcollation <> 0
                      AND NOT COALESCE(
                        import_collation_namespace.nspname = 'pg_catalog'
                        AND (
                          (
                            import_collation.collname IN ('C', 'POSIX')
                            AND %s
                          )
                          OR (
                            import_collation.collname = 'default'
                            AND %s
                          )
                        ),
                        false
                      )
                    )
                  )
              ) AS columns_importable,
              encode(sha256(convert_to(COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                  'name', a.attname,
                  'remoteName', a.attname,
                  'type', jsonb_build_array(tn.nspname, t.typname),
                  'typmod', a.atttypmod,
                  'notNull', a.attnotnull,
                  'collation', CASE WHEN co.oid IS NULL THEN NULL
                    ELSE jsonb_build_array(cn.nspname, co.collname)
                  END
                ) ORDER BY a.attnum)
                FROM pg_catalog.pg_attribute AS a
                JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
                JOIN pg_catalog.pg_namespace AS tn ON tn.oid = t.typnamespace
                LEFT JOIN pg_catalog.pg_collation AS co
                  ON co.oid = a.attcollation
                LEFT JOIN pg_catalog.pg_namespace AS cn
                  ON cn.oid = co.collnamespace
                WHERE a.attrelid = c.oid
                  AND a.attnum > 0
                  AND NOT a.attisdropped
              ), '[]'::jsonb)::text, 'UTF8')), 'hex')
                AS column_shape_fingerprint,
              encode(sha256(convert_to(jsonb_build_object(
                'schema', n.nspname,
                'relation', c.relname,
                'relkind', c.relkind,
                'owner', pg_get_userbyid(c.relowner),
                'rowSecurity', c.relrowsecurity,
                'forceRowSecurity', c.relforcerowsecurity,
                'rowSecurityActive', pg_catalog.row_security_active(c.oid),
                'currentRoleOwnsRelation', pg_has_role(
                  current_user, c.relowner, 'USAGE'
                ),
                'currentRoleBypassesRls', COALESCE((
                  SELECT r.rolsuper OR r.rolbypassrls
                  FROM pg_catalog.pg_roles AS r
                  WHERE r.rolname = current_user
                ), false),
                'securityBarrier', COALESCE(
                  c.reloptions && ARRAY['security_barrier=true'], false
                ),
                'securityInvoker', COALESCE(
                  c.reloptions && ARRAY['security_invoker=true'], false
                ),
                'viewDefinition', CASE WHEN c.relkind IN ('v', 'm')
                  THEN pg_get_viewdef(c.oid, false)
                  ELSE NULL
                END,
                'columns', COALESCE((
                  SELECT jsonb_agg(jsonb_build_object(
                    'position', a.attnum,
                    'name', a.attname,
                    'type', format_type(a.atttypid, a.atttypmod),
                    'notNull', a.attnotnull,
                    'collation', CASE WHEN co.oid IS NULL THEN NULL
                      ELSE jsonb_build_array(cn.nspname, co.collname)
                    END
                  ) ORDER BY a.attnum)
                  FROM pg_catalog.pg_attribute AS a
                  LEFT JOIN pg_catalog.pg_collation AS co
                    ON co.oid = a.attcollation
                  LEFT JOIN pg_catalog.pg_namespace AS cn
                    ON cn.oid = co.collnamespace
                  WHERE a.attrelid = c.oid
                    AND a.attnum > 0
                    AND NOT a.attisdropped
                ), '[]'::jsonb),
                'policies', COALESCE((
                  SELECT jsonb_agg(jsonb_build_object(
                    'name', p.polname,
                    'command', p.polcmd,
                    'permissive', p.polpermissive,
                    'appliesToCurrentRole', 0 = ANY(p.polroles) OR EXISTS (
                      SELECT 1
                      FROM unnest(p.polroles) AS applicable_role(role_oid)
                      WHERE pg_has_role(current_user, role_oid, 'USAGE')
                    ),
                    'roles', to_jsonb(ARRAY(
                      SELECT CASE WHEN role_oid = 0 THEN 'PUBLIC'
                        ELSE pg_get_userbyid(role_oid)
                      END
                      FROM unnest(p.polroles) AS policy_role(role_oid)
                      ORDER BY CASE WHEN role_oid = 0 THEN 'PUBLIC'
                        ELSE pg_get_userbyid(role_oid)
                      END
                    )),
                    'using', pg_get_expr(p.polqual, p.polrelid, false),
                    'withCheck', pg_get_expr(
                      p.polwithcheck, p.polrelid, false
                    )
                  ) ORDER BY p.polname)
                  FROM pg_catalog.pg_policy AS p
                  WHERE p.polrelid = c.oid
                ), '[]'::jsonb)
              )::text, 'UTF8')), 'hex') AS definition_fingerprint
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND c.relkind IN ('r', 'p', 'v', 'm')
            """,
            (
                portable_collation_encoding_matches,
                default_collation_matches,
                schema,
                relation,
            ),
        )
        row = cursor.fetchone()
        if row is None or not _selectable(cursor, schema, relation):
            all_present = False
            fingerprints.append("missing")
            continue
        if not row["columns_importable"]:
            all_present = False
        if row["bypasses_per_user_access"]:
            rls_detected = True
        fingerprints.append(row["definition_fingerprint"])
        column_shapes[entry] = row["column_shape_fingerprint"]
    return all_present, rls_detected, "|".join(fingerprints), column_shapes


def _selectable(cursor: Any, schema: str, relation: str) -> bool:
    """Verify SELECT privilege for every column without reading rows."""
    cursor.execute("SAVEPOINT relation_selectable")
    try:
        cursor.execute(
            sql.SQL("SELECT * FROM {}.{} WHERE FALSE").format(
                sql.Identifier(schema), sql.Identifier(relation)
            )
        )
    except psycopg.Error:
        cursor.execute("ROLLBACK TO SAVEPOINT relation_selectable")
        cursor.execute("RELEASE SAVEPOINT relation_selectable")
        return False
    cursor.execute("RELEASE SAVEPOINT relation_selectable")
    return True


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
            "WHERE n.nspname = %s AND c.relname = %s "
            "AND c.relkind IN ('r', 'p', 'v', 'm')",
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
    local_default_collation: tuple[str, str | None],
) -> tuple[dict[str, Any], datetime, str | None, dict[str, str]]:
    """Bounded, read-only capability and connectivity detection.

    `allowed_relations` are normalized "schema.relation" strings, matching
    `federation_schema.validate_registration()`'s output. `tls_policy` is
    the alias's registered requirement — enforced against the connection
    string's actual sslmode before connecting, the same as Provision, so a
    weak connectionRef is never even Observed successfully. The private local
    default-collation identity gates importability without expanding the
    observation contract.
    Returns observation, observed_at, physical_identity, and private canonical
    column-shape hashes. The observation is already validated against
    `federation_schema.validate_observation()`'s closed contract; the hashes do
    not expand that public contract. observed_at is a local audit timestamp
    captured just before the first catalog query; it is not an ordering
    authority. physical_identity and the hashes come from this same snapshot
    when reachable — see _physical_identity_from_cursor for why it must not be
    a second, separate connection."""
    enforce_tls_policy(tls_policy, connection_url)

    try:
        with psycopg.connect(
            connection_url,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                try:
                    PostgresSemanticSources._begin_read_only(cursor)
                    observed_at = datetime.now(timezone.utc)
                    (
                        encoding_matches,
                        default_collation_matches,
                    ) = _collation_compatibility(
                        cursor, local_default_collation
                    )
                    versions = extension_versions(cursor)
                    (
                        relations_verified,
                        rls_detected,
                        schema_fingerprint,
                        column_shapes,
                    ) = _verify_allowed_relations(
                        cursor,
                        allowed_relations,
                        default_collation_matches=default_collation_matches,
                        portable_collation_encoding_matches=encoding_matches,
                    )
                    physical_id = _physical_identity_from_cursor(
                        cursor, allowed_relations
                    )
                except psycopg.Error:
                    return validate_observation({
                        "connectivity": "reachable",
                        "schema": "unknown",
                        "sourceFreshness": "unknown",
                        "lastConnected": _now_iso(),
                        "lastSchemaVerified": None,
                        "sourceVersion": None,
                    }), datetime.now(timezone.utc), None, {}
    except psycopg.Error as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        message = str(exc).lower()
        unauthorized = (
            isinstance(sqlstate, str) and sqlstate.startswith("28")
        ) or any(marker in message for marker in (
            "authentication failed",
            "no pg_hba.conf entry",
            "no password supplied",
        ))
        return validate_observation({
            "connectivity": "unauthorized" if unauthorized else "unavailable",
            "schema": "unknown",
            "sourceFreshness": "unknown",
            "lastConnected": None,
            "lastSchemaVerified": None,
            "sourceVersion": None,
        }), datetime.now(timezone.utc), None, {}

    return validate_observation({
        "connectivity": "reachable",
        "schema": "current" if relations_verified else "changed",
        "sourceFreshness": "unknown",
        "lastConnected": _now_iso(),
        "lastSchemaVerified": _now_iso(),
        "sourceVersion": None,
        "extensionVersions": versions,
        "rowLevelSecurityDetected": rls_detected,
        "schemaFingerprint": schema_fingerprint,
    }), observed_at, physical_id, column_shapes


def verify_remote_state(
    connection_url: str,
    allowed_relations: tuple[str, ...],
    *,
    local_default_collation: tuple[str, str | None],
) -> tuple[str, dict[str, str], bool, bool, str, dict[str, str]]:
    """Read all live provisioning evidence in one repeatable-read snapshot.

    Returns physical identity, extension versions, relation selectability,
    access-control detection, schema fingerprint, and per-relation canonical
    column-shape hashes. The private local default-collation identity gates
    importability without expanding the returned tuple. Connection/query
    failures are left to the provisioning caller as psycopg errors.
    """
    with psycopg.connect(
        connection_url,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            PostgresSemanticSources._begin_read_only(cursor)
            (
                encoding_matches,
                default_collation_matches,
            ) = _collation_compatibility(
                cursor, local_default_collation
            )
            identity = _physical_identity_from_cursor(cursor, allowed_relations)
            versions = extension_versions(cursor)
            (
                relations_verified,
                rls_detected,
                schema_fingerprint,
                column_shapes,
            ) = _verify_allowed_relations(
                cursor,
                allowed_relations,
                default_collation_matches=default_collation_matches,
                portable_collation_encoding_matches=encoding_matches,
            )
    return (
        identity,
        versions,
        relations_verified,
        rls_detected,
        schema_fingerprint,
        column_shapes,
    )
