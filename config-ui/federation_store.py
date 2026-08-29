"""Durable federation alias registry and postgres_fdw provisioner."""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import psycopg
from http import HTTPStatus
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from federation_capability import (
    _database_default_collation_identity,
    detect_capability,
    extension_versions,
    host_capability,
    verify_remote_state,
)
from federation_schema import (
    FederationSchemaError,
    enforce_tls_policy,
    validate_alias,
    validate_group_definition,
    validate_group_name,
    validate_registration,
)

SCHEMA = "federation"

MAX_ALIASES = 100

# GET /api/federation/groups is unpaginated, so the registry-wide count is
# bounded here for the same reason MAX_ALIASES bounds the alias list. Groups
# create no alias rows, so the two limits do not interact.
MAX_GROUPS = 50


class FederationAliasStore:
    def __init__(
        self, connection_string: str, reader_role: str, derived_role: str
    ):
        if not connection_string:
            raise FederationSchemaError(
                "Federation alias registry is not configured.",
                code="federation.not_configured",
            )
        self.connection_string = connection_string
        self.reader_role = reader_role
        self.derived_role = derived_role
        self._initialization_lock = threading.Lock()
        self._initialized = False

    def _connect(self):
        connection = psycopg.connect(
            self.connection_string,
            autocommit=False,
            row_factory=dict_row,
        )
        try:
            with connection.cursor() as cur:
                cur.execute("SET SESSION search_path = pg_catalog, public")
            if self._initialized:
                return connection
            with self._initialization_lock:
                if not self._initialized:
                    with connection.cursor() as cur:
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (f"{SCHEMA}:schema",),
                        )
                        self._initialize(cur)
                    connection.commit()
                    self._initialized = True
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _initialize(cur) -> None:
        # The federation schema is created and owned by the dedicated
        # federation provisioner in docker/postgis's role-setup scripts.
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._aliases (
              alias text PRIMARY KEY,
              display_name text NOT NULL,
              kind text NOT NULL CHECK (kind = 'postgresql'),
              connection_ref text NOT NULL,
              allowed_relations text[] NOT NULL,
              status text NOT NULL
                CHECK (status IN ('pending', 'active', 'unavailable', 'retired')),
              freshness_strategy text NOT NULL
                CHECK (freshness_strategy IN (
                  'manual', 'maximumAge', 'timestampColumn', 'versionRelation'
                )),
              data_handling_classification text NOT NULL,
              registered_by text NOT NULL,
              registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              last_observation jsonb,
              last_observed_connection_identity text,
              tls_policy text NOT NULL DEFAULT 'require'
                CHECK (tls_policy IN ('require', 'verify-ca', 'verify-full')),
              provisioned_at timestamptz,
              approved_by text,
              approved_at timestamptz,
              physical_identity text,
              observed_at timestamptz,
              row_level_security_acknowledged boolean NOT NULL DEFAULT false,
              accepted_schema_fingerprint text,
              accepted_physical_identity text,
              accepted_connection_identity text,
              last_observation_id bigint,
              retired_at timestamptz,
              retired_by text,
              archived_schema text,
              archived_server text,
              groups text[] NOT NULL DEFAULT '{{}}'
            )
        """).format(sql.Identifier(SCHEMA)))
        # Retirement archives rather than deletes: the alias row and its whole
        # observation history are retained, and archived_schema records where
        # the physical objects were moved to, so the audit trail stays
        # inspectable in the catalogue and not only as metadata here.
        for column, column_type in (
            ("retired_at", "timestamptz"),
            ("retired_by", "text"),
            ("archived_schema", "text"),
            ("archived_server", "text"),
        ):
            cur.execute(sql.SQL(
                "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
                + column + " " + column_type
            ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "last_observed_connection_identity text"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS approved_by text"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "approved_at timestamptz"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "physical_identity text"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "observed_at timestamptz"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "row_level_security_acknowledged boolean NOT NULL DEFAULT false"
        ).format(sql.Identifier(SCHEMA)))
        # Group labels. This migration is load-bearing well beyond groups:
        # _SELECT_COLUMNS names the column unconditionally, so omitting it
        # fails every alias read with UndefinedColumn -- the federation panel,
        # federation list/show, and the periodic verifier alike. verify.sh
        # carries a first-party comment recording that exact failure for
        # archived_schema, which is why it probes information_schema first.
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "groups text[] NOT NULL DEFAULT '{{}}'"
        ).format(sql.Identifier(SCHEMA)))
        # NULL until a successful provision() explicitly accepts a live
        # schema_fingerprint (see provision()'s docstring) — deliberately
        # not backfilled from last_observation, which reflects whatever
        # the most recent Observe merely *saw*, not what a human actually
        # reviewed and accepted.
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "accepted_schema_fingerprint text"
        ).format(sql.Identifier(SCHEMA)))
        # The durable counterpart to physical_identity, for the same reason
        # accepted_schema_fingerprint is the durable counterpart to
        # last_observation's fingerprint: physical_identity records whatever
        # the most recent Observe merely *saw*, so comparing a live probe
        # against it only ever catches a replacement that happened between
        # Observe and Provision — never one that was already observed. This
        # column records the identity a successful provision() actually
        # accepted, so a source database replaced and then re-observed is
        # still caught (docs/federation-architecture-waypoint.md: "The same
        # table name at a new physical database is not the same source
        # unless an operator performs an explicit, evidenced rebind").
        # Deliberately not backfilled from physical_identity — that would
        # retroactively "accept" whatever is there now, which is exactly the
        # replacement this exists to catch.
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "accepted_physical_identity text"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "accepted_connection_identity text"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "last_observation_id bigint"
        ).format(sql.Identifier(SCHEMA)))
        # DEFAULT backfills any alias registered before tlsPolicy was
        # persisted with the weakest of the three valid policies — the
        # conservative choice: it neither claims a stronger guarantee than
        # was ever actually reviewed, nor breaks a currently-working
        # connectionRef that only ever supplied sslmode=require.
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "tls_policy text NOT NULL DEFAULT 'require'"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "REVOKE ALL ON {}._aliases FROM PUBLIC"
        ).format(sql.Identifier(SCHEMA)))
        # Keep evidence append-only even though _aliases exposes only latest.
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._observations (
              id bigserial PRIMARY KEY,
              alias text NOT NULL REFERENCES {}._aliases (alias),
              observation jsonb NOT NULL,
              connection_identity text,
              physical_identity text,
              observed_at timestamptz NOT NULL,
              recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
        """).format(sql.Identifier(SCHEMA), sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "CREATE INDEX IF NOT EXISTS _observations_alias_observed_at_idx "
            "ON {}._observations (alias, observed_at DESC)"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "REVOKE ALL ON {}._observations FROM PUBLIC"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._approvals (
              id bigserial PRIMARY KEY,
              alias text NOT NULL REFERENCES {}._aliases (alias),
              observation_id bigint NOT NULL REFERENCES {}._observations (id),
              approved_by text NOT NULL,
              approved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              acknowledged_row_level_security boolean NOT NULL,
              acknowledged_schema_change boolean NOT NULL,
              acknowledged_physical_rebind boolean NOT NULL
            )
        """).format(
            sql.Identifier(SCHEMA),
            sql.Identifier(SCHEMA),
            sql.Identifier(SCHEMA),
        ))
        cur.execute(sql.SQL(
            "REVOKE ALL ON {}._approvals FROM PUBLIC"
        ).format(sql.Identifier(SCHEMA)))
        # Group definitions. A group is a label: a name, an optional
        # description, and who created it. It grants nothing, revokes nothing,
        # and changes no PostgreSQL privilege. No CHECK on the name -- every
        # CHECK in this schema is a value enumeration, and the name grammar
        # lives in federation_schema.py beside the alias grammar. No foreign
        # key from _aliases.groups either: PostgreSQL has no array-element
        # foreign keys, so define-before-assign and delete-detaches are
        # enforced by the store.
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._groups (
              name text PRIMARY KEY,
              description text,
              created_by text NOT NULL,
              created_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "REVOKE ALL ON {}._groups FROM PUBLIC"
        ).format(sql.Identifier(SCHEMA)))

    _SELECT_COLUMNS = sql.SQL("""
        alias, display_name AS "displayName", kind,
        connection_ref AS "connectionRef",
        allowed_relations AS "allowedRelations", status,
        freshness_strategy AS "freshnessStrategy",
        data_handling_classification AS "dataHandlingClassification",
        registered_by AS "registeredBy",
        registered_at AS "registeredAt",
        last_observation AS "lastObservation",
        last_observation_id AS "lastObservationId",
        tls_policy AS "tlsPolicy",
        provisioned_at AS "provisionedAt",
        approved_by AS "approvedBy",
        approved_at AS "approvedAt",
        row_level_security_acknowledged AS "rowLevelSecurityAcknowledged",
        retired_at AS "retiredAt",
        retired_by AS "retiredBy",
        archived_schema AS "archivedSchema",
        archived_server AS "archivedServer",
        groups,
        (
          accepted_schema_fingerprint IS NOT NULL
          AND accepted_physical_identity IS NOT NULL
          AND accepted_connection_identity IS NOT NULL
        ) AS "acceptedEvidenceComplete",
        -- The physical counterpart of lastObservation.acceptedSchemaCurrent.
        -- Without it a client can show that the schema drifted but not that
        -- the source is a different database, which is the condition the
        -- physical-rebind acknowledgement exists to override. NULL means
        -- there is nothing to compare yet, not "matches": the subquery is
        -- empty until an observation exists and an identity has been accepted.
        (
          SELECT observation.physical_identity IS NOT DISTINCT FROM
                 _aliases.accepted_physical_identity
          FROM {schema}._observations AS observation
          WHERE observation.id = _aliases.last_observation_id
            AND _aliases.accepted_physical_identity IS NOT NULL
        ) AS "acceptedPhysicalIdentityCurrent"
    """).format(schema=sql.Identifier(SCHEMA))

    def register(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        record = validate_registration(payload)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{SCHEMA}:register",),
            )
            # Every retained row reserves a slot. In particular, an
            # unavailable alias can become active again, and list() relies on
            # the registry itself remaining bounded.
            cur.execute(
                sql.SQL("""
                    SELECT count(*) AS count,
                           count(*) FILTER (WHERE status = 'retired')
                             AS retired,
                           count(*) FILTER (WHERE alias = %s) > 0 AS exists
                    FROM {}._aliases
                """).format(sql.Identifier(SCHEMA)),
                (record["alias"],),
            )
            registry = cur.fetchone()
            if registry["exists"]:
                raise FileExistsError(record["alias"])
            if registry["count"] >= MAX_ALIASES:
                # Retired rows still reserve a slot but no longer appear in
                # list(), so an operator counting what they can see would
                # otherwise find this limit inexplicable.
                retired = registry["retired"]
                detail = (
                    f" {retired} of them retired and hidden from the alias"
                    " list." if retired else ""
                )
                raise FederationSchemaError(
                    f"Federation alias limit ({MAX_ALIASES}) reached.{detail}",
                    code="federation.alias_limit_reached",
                )
            cur.execute(
                sql.SQL("""
                    INSERT INTO {}._aliases
                      (alias, display_name, kind, connection_ref,
                       allowed_relations, status, freshness_strategy,
                       data_handling_classification, registered_by,
                       tls_policy)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (alias) DO NOTHING
                    RETURNING alias
                """).format(sql.Identifier(SCHEMA)),
                (
                    record["alias"],
                    record["displayName"],
                    record["kind"],
                    record["connectionRef"],
                    list(record["allowedRelations"]),
                    record["status"],
                    record["freshnessStrategy"],
                    record["dataHandlingClassification"],
                    actor,
                    record["tlsPolicy"],
                ),
            )
            if cur.fetchone() is None:
                raise FileExistsError(record["alias"])
            return self._get_with_cursor(cur, record["alias"])

    def get(self, alias: str) -> dict[str, Any]:
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            return self._get_with_cursor(cur, alias)

    def _get_with_cursor(self, cur, alias: str) -> dict[str, Any]:
        """Read an alias through an already-open cursor. `alias` must already
        be validated.

        observe() and provision() return this rather than calling get(), so
        the row they hand back is the one their own transaction produced.
        get() opens a separate connection, which — because it can only see
        committed data — would either miss the caller's own uncommitted work
        or, once committed, race a concurrent writer and return somebody
        else's result. app.py records the returned connectivity in the
        caller's audit event, so returning another request's observation
        would misattribute the evidence."""
        cur.execute(
            sql.SQL("SELECT {} FROM {}._aliases WHERE alias = %s").format(
                self._SELECT_COLUMNS, sql.Identifier(SCHEMA)
            ),
            (alias,),
        )
        item = cur.fetchone()
        if not item:
            raise FileNotFoundError(alias)
        return item

    def host_capability(self) -> dict[str, Any]:
        """Whether the host can still attach sources.

        Every alias observation answers "is that source reachable". Nothing
        answered "can this database still federate at all", so a capability
        revoked on the host surfaced only as every source failing at once.
        """
        with self._connect() as connection, connection.cursor() as cur:
            return host_capability(cur, SCHEMA)

    def list(self) -> list[dict[str, Any]]:
        """Aliases available for normal use, newest registration state first.

        Retired aliases are omitted, matching the archive contract the
        semantic store already follows (its list queries filter
        `status = 'ready'`): normal collections omit archived assets while
        exact-ID lookup stays available. get() still returns a retired alias
        by name, so its history and archive location remain reachable.
        """
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT {} FROM {}._aliases WHERE status <> 'retired' "
                    "ORDER BY alias LIMIT %s"
                ).format(self._SELECT_COLUMNS, sql.Identifier(SCHEMA)),
                (MAX_ALIASES + 1,),
            )
            return list(cur.fetchall())

    _GROUP_COLUMNS = sql.SQL("""
        name, description,
        created_by AS "createdBy", created_at AS "createdAt",
        (
          SELECT count(*)
          FROM {}._aliases AS labelled
          WHERE labelled.status <> 'retired'
            AND _groups.name = ANY (labelled.groups)
        ) AS "memberCount"
    """)

    def list_groups(self) -> list[dict[str, Any]]:
        """Defined group labels with live, non-retired member counts.

        Membership is metadata: this count says how many sources an operator
        labelled, never what anybody may read. Retired aliases are excluded to
        match list()'s archive contract, so the count and the panel agree.
        """
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT {} FROM {}._groups ORDER BY name LIMIT %s"
                ).format(
                    self._GROUP_COLUMNS.format(sql.Identifier(SCHEMA)),
                    sql.Identifier(SCHEMA),
                ),
                (MAX_GROUPS + 1,),
            )
            return list(cur.fetchall())

    def define_group(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        """Create a label.

        Grants nothing, revokes nothing, and changes no PostgreSQL privilege.
        It records which sources are meant to be used together; cross-database
        querying already works between any two provisioned sources, because
        they are foreign tables in one host database, so a group could only
        ever restrict that and this one deliberately does not.
        """
        definition = validate_group_definition(payload)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{SCHEMA}:groups",),
            )
            # Existence is resolved in the same query as the count, and
            # answered first. Checking the ceiling alone meant that at exactly
            # MAX_GROUPS, redefining a group that already existed reported
            # federation.group_limit -- automation would go and delete an
            # unrelated group to make room for one already there.
            cur.execute(
                sql.SQL(
                    "SELECT count(*) AS count,"
                    " count(*) FILTER (WHERE name = %s) AS taken"
                    " FROM {}._groups"
                ).format(sql.Identifier(SCHEMA)),
                (definition["name"],),
            )
            registry = cur.fetchone()
            if registry["taken"]:
                raise FederationSchemaError(
                    f"Federation group {definition['name']!r} already exists.",
                    status=HTTPStatus.CONFLICT,
                    code="federation.group_exists",
                )
            if registry["count"] >= MAX_GROUPS:
                raise FederationSchemaError(
                    f"Federation group limit ({MAX_GROUPS}) reached.",
                    status=HTTPStatus.CONFLICT,
                    code="federation.group_limit",
                )
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}._groups (name, description, created_by)"
                    " VALUES (%s, %s, %s)"
                    " ON CONFLICT (name) DO NOTHING RETURNING name"
                ).format(sql.Identifier(SCHEMA)),
                (definition["name"], definition["description"], actor),
            )
            if cur.fetchone() is None:
                # A coded error, not FileExistsError: app.py answers that with
                # a 409 carrying no code, and the CLI branches on the code.
                raise FederationSchemaError(
                    f"Federation group {definition['name']!r} already exists.",
                    status=HTTPStatus.CONFLICT,
                    code="federation.group_exists",
                )
            cur.execute(
                sql.SQL(
                    "SELECT {} FROM {}._groups WHERE name = %s"
                ).format(
                    self._GROUP_COLUMNS.format(sql.Identifier(SCHEMA)),
                    sql.Identifier(SCHEMA),
                ),
                (definition["name"],),
            )
            return cur.fetchone()

    def delete_group(self, name: str) -> dict[str, Any]:
        """Delete a label and detach it from every alias, retired ones included.

        Deletion, not archival: retire() archives because an alias names a real
        physical attachment whose history is evidence. A label names nothing
        and exposes nothing, so there is no trail to preserve beyond the audit
        event the route records.
        """
        name = validate_group_name(name)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{SCHEMA}:groups",),
            )
            cur.execute(
                sql.SQL(
                    "DELETE FROM {}._groups WHERE name = %s RETURNING name"
                ).format(sql.Identifier(SCHEMA)),
                (name,),
            )
            if cur.fetchone() is None:
                raise FederationSchemaError(
                    f"Federation group {name!r} is not defined.",
                    status=HTTPStatus.NOT_FOUND,
                    code="federation.group_not_found",
                )
            cur.execute(
                sql.SQL(
                    "UPDATE {}._aliases SET groups = array_remove(groups, %s)"
                    " WHERE %s = ANY (groups) RETURNING alias"
                ).format(sql.Identifier(SCHEMA)),
                (name, name),
            )
            detached = sorted(row["alias"] for row in cur.fetchall())
            return {"name": name, "detachedAliases": detached}

    def set_alias_groups(
        self, alias: str, groups: tuple[str, ...]
    ) -> dict[str, Any]:
        """Replace an alias's whole label set. Changes no privilege."""
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{SCHEMA}:groups",),
            )
            if groups:
                cur.execute(
                    sql.SQL(
                        "SELECT name FROM {}._groups WHERE name = ANY (%s)"
                    ).format(sql.Identifier(SCHEMA)),
                    (list(groups),),
                )
                defined = {row["name"] for row in cur.fetchall()}
                missing = sorted(set(groups) - defined)
                if missing:
                    raise FederationSchemaError(
                        "Federation groups are not defined: "
                        + ", ".join(missing),
                        status=HTTPStatus.NOT_FOUND,
                        code="federation.group_not_found",
                    )
            cur.execute(
                sql.SQL(
                    "UPDATE {}._aliases SET groups = %s WHERE alias = %s"
                    " RETURNING alias"
                ).format(sql.Identifier(SCHEMA)),
                (list(groups), alias),
            )
            if cur.fetchone() is None:
                # Coded, not FileNotFoundError: the generic handler answers
                # that with a 404 whose body is the alias name and no code at
                # all, and the CLI branches on codes. The alias GET route
                # already reports a missing alias this way.
                raise FederationSchemaError(
                    f"The federation alias {alias!r} does not exist.",
                    status=HTTPStatus.NOT_FOUND,
                    code="federation.alias_not_found",
                )
            return self._get_with_cursor(cur, alias)

    def observe(
        self,
        alias: str,
        connection_url: str,
        *,
        allowed_relations: tuple[str, ...],
        tls_policy: str,
    ) -> dict[str, Any]:
        """Probe and persist under the alias's local ordering lock."""
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{SCHEMA}:observe:{alias}",),
            )
            # This transaction stays open across detect_capability below, so
            # the role's idle_in_transaction_session_timeout of one minute
            # applies to a remote probe, not to an idle session. The probe is
            # bounded per statement (5s remote statement_timeout, 5s connect)
            # but not in aggregate: it costs 11+5N round-trips for N allowed
            # relations, so a high-latency source with many relations exceeds
            # a minute and PostgreSQL terminates the session before anything
            # is persisted -- leaving the alias never verified and never
            # revoked, which is the opposite of what a slow source deserves.
            # transaction_timeout remains the outer bound.
            cur.execute(
                "SET LOCAL idle_in_transaction_session_timeout = '10min'"
            )
            local_default_collation = (
                _database_default_collation_identity(cur)
            )
            (
                observation,
                observed_at,
                physical_identity,
                remote_column_shapes,
            ) = detect_capability(
                connection_url,
                allowed_relations=allowed_relations,
                tls_policy=tls_policy,
                local_default_collation=local_default_collation,
            )
            self._persist_observation(
                cur, alias, observation, connection_url, physical_identity,
                observed_at, remote_column_shapes,
            )
            return self._get_with_cursor(cur, alias)

    def _persist_observation(
        self,
        cur,
        alias: str,
        observation: dict[str, Any],
        connection_url: str,
        physical_identity: str | None,
        observed_at: datetime,
        remote_column_shapes: dict[str, str],
    ) -> None:
        """Write one observation after its per-alias lock is held."""
        reachable = observation["connectivity"] == "reachable"
        connection_identity = self._connection_identity(connection_url)
        cur.execute(
            sql.SQL("""
                SELECT provisioned_at, allowed_relations,
                       accepted_schema_fingerprint,
                       accepted_physical_identity,
                       accepted_connection_identity,
                       row_level_security_acknowledged
                FROM {}._aliases
                WHERE alias = %s
                FOR UPDATE
            """).format(sql.Identifier(SCHEMA)),
            (alias,),
        )
        registry_state = cur.fetchone()
        if registry_state is None:
            raise FileNotFoundError(alias)

        incoming_fingerprint = observation.get("schemaFingerprint")
        if incoming_fingerprint is not None:
            accepted_fingerprint = registry_state["accepted_schema_fingerprint"]
            observation = {
                **observation,
                "acceptedSchemaCurrent": (
                    accepted_fingerprint is None
                    or accepted_fingerprint == incoming_fingerprint
                ),
            }
        cur.execute(
            sql.SQL("""
                INSERT INTO {}._observations
                  (alias, observation, connection_identity,
                   physical_identity, observed_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """).format(sql.Identifier(SCHEMA)),
            (
                alias,
                Jsonb(observation),
                connection_identity,
                physical_identity,
                observed_at,
            ),
        )
        observation_id = cur.fetchone()["id"]

        provisioned = registry_state["provisioned_at"] is not None
        evidence_current = (
            reachable
            and observation.get("schema") == "current"
            and registry_state["accepted_schema_fingerprint"] is not None
            and registry_state["accepted_schema_fingerprint"]
                == incoming_fingerprint
            and registry_state["accepted_physical_identity"] is not None
            and registry_state["accepted_physical_identity"]
                == physical_identity
            and registry_state["accepted_connection_identity"] is not None
            and registry_state["accepted_connection_identity"]
                == connection_identity
            and (
                not observation.get("rowLevelSecurityDetected", False)
                or registry_state["row_level_security_acknowledged"]
            )
        )
        shippable = None
        if provisioned and reachable and "extensionVersions" in observation:
            shippable = self._shippable_extensions(
                extension_versions(cur), observation["extensionVersions"]
            )
            # Remove only the known PostGIS hint before validating state;
            # unexpected extension options require explicit reprovisioning.
            server_name = f"{alias}_srv"
            current = self._current_shippable_extensions(cur, server_name)
            if current == ["postgis"] and not shippable:
                self._apply_shippable_extensions(
                    cur, server_name, current, shippable
                )
        if provisioned and evidence_current:
            evidence_current = self._local_state_matches(
                cur,
                alias,
                registry_state["allowed_relations"],
                remote_column_shapes,
                connection_url,
                shippable or [],
            )
        cur.execute(
            sql.SQL("""
                UPDATE {}._aliases
                SET last_observation = %s,
                    last_observation_id = %s,
                    last_observed_connection_identity = %s,
                    physical_identity = %s,
                    observed_at = %s,
                    status = CASE
                      -- Retirement is terminal. Without this an observe
                      -- would resurrect a retired alias to active or
                      -- unavailable, because retire() deliberately leaves
                      -- provisioned_at set, and a later provision would then
                      -- reinstate access to a source somebody had removed.
                      WHEN status = 'retired' THEN 'retired'
                      WHEN provisioned_at IS NULL THEN status
                      WHEN %s THEN 'active'
                      ELSE 'unavailable'
                    END
                WHERE alias = %s
            """).format(sql.Identifier(SCHEMA)),
            (
                Jsonb(observation),
                observation_id,
                connection_identity,
                physical_identity,
                observed_at,
                evidence_current,
                alias,
            ),
        )

        if provisioned:
            self._apply_consumer_access(cur, alias, grant=evidence_current)

    def _apply_consumer_access(self, cur, alias: str, *, grant: bool) -> None:
        """Give both consumer roles access to a source schema, or take it away.

        Extracted so the two callers cannot drift apart: an observation decides
        this from evidence currency, and mark_unverifiable() decides it from
        the source having become impossible to verify at all. A schema owned by
        another role is left alone either way -- provisioning refuses that
        state, and silently re-granting on someone else's schema would be worse
        than declining to touch it.
        """
        schema_name = f"source_{alias}"
        if not self._local_schema_owned(cur, schema_name):
            return
        action = "GRANT" if grant else "REVOKE"
        preposition = "TO" if grant else "FROM"
        for role in dict.fromkeys((self.derived_role, self.reader_role)):
            cur.execute(
                sql.SQL(
                    f"{action} USAGE ON SCHEMA {{}} {preposition} {{}}"
                ).format(sql.Identifier(schema_name), sql.Identifier(role))
            )
            cur.execute(
                sql.SQL(
                    f"{action} SELECT ON ALL TABLES IN SCHEMA {{}} "
                    f"{preposition} {{}}"
                ).format(sql.Identifier(schema_name), sql.Identifier(role))
            )

    @contextmanager
    def alias_reconciliation(self, alias: str):
        """Hold the per-alias lock while a caller mirrors this alias elsewhere.

        Yields the alias's status as read under that lock, or None if it has
        no row, so the caller mirrors what is true now rather than what it
        observed a moment ago.

        The semantic mirror lives outside every transaction this store opens:
        observe() releases its lock on commit, and the mirroring HTTP call
        happens afterwards. Without this, a pass that observed an alias as
        active could write "available" after a retirement had already
        committed -- and since retirement is excluded from every later pass,
        that write would stand forever, leaving profiles authorising a schema
        that had been renamed away.

        Only writes that mark a source *available* can do that damage; marking
        one unavailable is always the safe direction, so those callers do not
        need this. It reuses the observe key deliberately, which also makes
        retirement mutually exclusive with observe and provision, where
        previously only the row lock separated them.
        """
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{SCHEMA}:observe:{alias}",),
            )
            cur.execute(
                sql.SQL("SELECT status FROM {}._aliases WHERE alias = %s")
                .format(sql.Identifier(SCHEMA)),
                (alias,),
            )
            row = cur.fetchone()
            yield None if row is None else row["status"]

    def mark_unverifiable(self, alias: str) -> bool:
        """Withdraw consumer access from a provisioned source that cannot be probed.

        A connectionRef removed from the environment leaves an alias that no
        observation can ever reach, while its foreign tables keep working:
        the user mapping still holds the remote credential, so both consumer
        roles carry on reading a source the deployment can no longer verify.
        Nothing else closes that, because every other revoke path runs from an
        observation and there is no observation to run.

        Recoverable on purpose. Restoring the variable lets the next pass
        observe normally and _persist_observation grants access straight back,
        so a configuration slip during a deploy costs a window, not a
        reprovision. Retirement remains the way to decommission a source
        properly -- it also drops the credential this cannot touch.

        Returns True when it changed something, so a caller can log once
        rather than on every pass.
        """
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{SCHEMA}:observe:{alias}",),
            )
            cur.execute(
                sql.SQL("""
                    SELECT status, provisioned_at
                    FROM {}._aliases
                    WHERE alias = %s
                    FOR UPDATE
                """).format(sql.Identifier(SCHEMA)),
                (alias,),
            )
            row = cur.fetchone()
            # Retirement is terminal and already revoked; an unprovisioned
            # alias exposes nothing. Neither has access left to withdraw.
            if (
                row is None
                or row["status"] == "retired"
                or row["provisioned_at"] is None
            ):
                return False
            already = row["status"] == "unavailable"
            cur.execute(
                sql.SQL(
                    "UPDATE {}._aliases SET status = 'unavailable' "
                    "WHERE alias = %s"
                ).format(sql.Identifier(SCHEMA)),
                (alias,),
            )
            self._apply_consumer_access(cur, alias, grant=False)
            return not already

    # PostGIS/PROJ/GEOS versions may differ between the federation database
    # and a source — execution happens in the federation database, so a
    # pushed-down expression (e.g. ST_Transform) can silently return a
    # different result from the same expression evaluated locally if the
    # versions disagree. postgisExtversion is a separate, additional
    # requirement, not a replacement for the library-version check above:
    # a same-library, different-extversion pair still evaluates shared
    # expressions identically, but the older SQL script on one side may be
    # missing a function or operator the newer one added — pushing down an
    # expression that uses it would fail at the SQL level rather than
    # merely evaluate differently (see extension_versions()'s docstring).
    # Only ever mark postgis shippable when every one of these exactly
    # matches the alias's last observation of the remote.
    _VERSION_MATCH_KEYS = ("postgis", "postgisExtversion", "proj", "geos")

    @staticmethod
    def _shippable_extensions(
        local_versions: dict[str, str], remote_versions: dict[str, str]
    ) -> list[str]:
        matches = all(
            local_versions.get(key) and local_versions.get(key) == remote_versions.get(key)
            for key in FederationAliasStore._VERSION_MATCH_KEYS
        )
        return ["postgis"] if matches else []

    @staticmethod
    def _current_shippable_extensions(cur, server_name: str) -> list[str]:
        cur.execute(
            "SELECT s.srvoptions FROM pg_catalog.pg_foreign_server AS s "
            "JOIN pg_catalog.pg_foreign_data_wrapper AS f ON f.oid = s.srvfdw "
            "WHERE s.srvname = %s AND f.fdwname = 'postgres_fdw' "
            "AND s.srvowner = (SELECT oid FROM pg_catalog.pg_roles "
            "WHERE rolname = current_user)",
            (server_name,),
        )
        row = cur.fetchone()
        for option in (row["srvoptions"] if row and row["srvoptions"] else []):
            key, _, value = option.partition("=")
            if key == "extensions":
                return [name for name in value.split(",") if name]
        return []

    @staticmethod
    def _apply_shippable_extensions(
        cur, server_name: str, current: list[str], desired: list[str]
    ) -> None:
        if current == desired:
            return
        if current:
            cur.execute(
                sql.SQL("ALTER SERVER {} OPTIONS (DROP extensions)").format(
                    sql.Identifier(server_name)
                )
            )
        if desired:
            cur.execute(
                sql.SQL("ALTER SERVER {} OPTIONS (ADD extensions {})").format(
                    sql.Identifier(server_name),
                    sql.Literal(",".join(desired)),
                )
            )

    # libpq settings from the connectionRef that must be reproduced on the
    # foreign server, because postgres_fdw opens its own connection and would
    # otherwise fall back to libpq defaults. hostaddr belongs here with the
    # TLS settings: when a connectionRef supplies it, Observe and
    # verify_remote_state() connect to THAT address, so a server built from
    # host alone can resolve somewhere else entirely — the identity, schema,
    # and privileges that were verified would then describe a different
    # database than the one runtime queries actually reach. (host is still
    # sent too, since with hostaddr present libpq uses host purely for TLS
    # name verification.) Not user/password: those live in the user mapping.
    _FORWARDED_CONNECTION_OPTIONS = (
        "hostaddr", "sslmode", "sslrootcert", "gssencmode",
    )

    @classmethod
    def _desired_server_options(
        cls, params: dict[str, Any], shippable: list[str]
    ) -> dict[str, str]:
        options = {
            "host": str(params.get("host", "")),
            "port": str(params.get("port", "5432")),
            "dbname": str(params.get("dbname", "")),
            "use_remote_estimate": "true",
        }
        for name in cls._FORWARDED_CONNECTION_OPTIONS:
            if params.get(name):
                options[name] = str(params[name])
        if shippable:
            options["extensions"] = ",".join(shippable)
        return options

    @staticmethod
    def _current_server_options(cur, server_name: str) -> dict[str, str]:
        cur.execute(
            "SELECT s.srvoptions, f.fdwname, "
            "s.srvowner = (SELECT oid FROM pg_catalog.pg_roles "
            "WHERE rolname = current_user) AS owned "
            "FROM pg_catalog.pg_foreign_server AS s "
            "JOIN pg_catalog.pg_foreign_data_wrapper AS f "
            "ON f.oid = s.srvfdw WHERE s.srvname = %s",
            (server_name,),
        )
        row = cur.fetchone()
        if (
            row is None
            or row["fdwname"] != "postgres_fdw"
            or not row["owned"]
        ):
            raise FederationSchemaError(
                f"Federation server {server_name!r} is missing or invalid.",
                code="federation.local_state_invalid",
            )
        options: dict[str, str] = {}
        for option in (row["srvoptions"] if row and row["srvoptions"] else []):
            key, _, value = option.partition("=")
            options[key] = value
        return options

    @staticmethod
    def _local_schema_owned(cur, schema_name: str) -> bool:
        cur.execute(
            "SELECT n.nspowner = (SELECT oid FROM pg_catalog.pg_roles "
            "WHERE rolname = current_user) AS owned "
            "FROM pg_catalog.pg_namespace AS n WHERE n.nspname = %s",
            (schema_name,),
        )
        row = cur.fetchone()
        return bool(row and row["owned"])

    def _local_state_matches(
        self,
        cur,
        alias: str,
        allowed_relations,
        remote_column_shapes: dict[str, str],
        connection_url: str,
        shippable: list[str],
    ) -> bool:
        server_name = f"{alias}_srv"
        schema_name = f"source_{alias}"
        params = psycopg.conninfo.conninfo_to_dict(connection_url)
        try:
            current_options = self._current_server_options(cur, server_name)
            current_extensions = current_options.pop("extensions", None)
            if current_options != self._desired_server_options(params, []):
                return False
            if current_extensions is not None and (
                not shippable
                or current_extensions != ",".join(shippable)
            ):
                return False
        except FederationSchemaError:
            return False

        cur.execute(
            "SELECT usename AS role_name, usename = current_user AS is_current, "
            "umoptions FROM pg_catalog.pg_user_mappings WHERE srvname = %s",
            (server_name,),
        )
        mappings = cur.fetchall()
        current_mappings = [row for row in mappings if row["is_current"]]
        if len(current_mappings) != 1:
            return False
        expected_roles = {
            current_mappings[0]["role_name"],
            self.derived_role,
            self.reader_role,
        }
        if {row["role_name"] for row in mappings} != expected_roles:
            return False
        current_mapping_options = {}
        for option in (current_mappings[0]["umoptions"] or []):
            name, _, value = option.partition("=")
            current_mapping_options[name] = value
        if current_mapping_options != {
            "user": str(params.get("user", "")),
            "password": str(params.get("password", "")),
        }:
            return False
        if not self._local_schema_owned(cur, schema_name):
            return False

        expected = {
            relation.split(".", 1)[1]: relation.split(".", 1)
            for relation in allowed_relations
        }
        bindings = self._local_relation_bindings(cur, schema_name)
        return (
            bindings.keys() == expected.keys()
            and all(
                self._binding_matches(
                    bindings[remote_table],
                    server_name,
                    remote_schema,
                    remote_table,
                )
                and bindings[remote_table]["column_shape_fingerprint"]
                    == remote_column_shapes.get(f"{remote_schema}.{remote_table}")
                for remote_table, (remote_schema, _) in expected.items()
            )
        )

    @staticmethod
    def _reconcile_server_options(
        cur,
        server_name: str,
        current: dict[str, str],
        desired: dict[str, str],
    ) -> None:
        actions = []
        for option_name in sorted(current.keys() | desired.keys()):
            value = desired.get(option_name)
            if value is not None:
                verb = "SET" if option_name in current else "ADD"
                if current.get(option_name) != value:
                    actions.append(
                        sql.SQL("{} {} {}").format(
                            sql.SQL(verb),
                            sql.Identifier(option_name),
                            sql.Literal(value),
                        )
                    )
            elif option_name in current:
                actions.append(
                    sql.SQL("DROP {}").format(sql.Identifier(option_name))
                )
        if actions:
            cur.execute(
                sql.SQL("ALTER SERVER {} OPTIONS ({})").format(
                    sql.Identifier(server_name), sql.SQL(", ").join(actions)
                )
            )

    @staticmethod
    def _import_relations(
        cur,
        alias: str,
        server_name: str,
        schema_name: str,
        allowed_relations,
    ) -> None:
        """Import the allowlist and fail if postgres_fdw omits any item."""
        for relation in allowed_relations:
            remote_schema, remote_table = relation.split(".", 1)
            cur.execute(sql.SQL("""
                IMPORT FOREIGN SCHEMA {remote_schema}
                LIMIT TO ({table})
                FROM SERVER {server}
                INTO {local_schema}
            """).format(
                remote_schema=sql.Identifier(remote_schema),
                table=sql.Identifier(remote_table),
                server=sql.Identifier(server_name),
                local_schema=sql.Identifier(schema_name),
            ))
        cur.execute(
            sql.SQL("""
                SELECT c.relname
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind = 'f'
            """),
            (schema_name,),
        )
        imported = {row["relname"] for row in cur.fetchall()}
        expected = {relation.split(".", 1)[1] for relation in allowed_relations}
        missing = sorted(expected - imported)
        if missing:
            raise FederationSchemaError(
                f"Alias {alias!r} provisioning did not import: "
                f"{missing} — the source schema may have changed "
                "since it was last observed.",
                code="federation.import_incomplete",
            )

    @staticmethod
    def _local_relation_bindings(cur, schema_name: str) -> dict[str, Any]:
        cur.execute(
            """
            SELECT c.relname, c.relkind,
                   c.relowner = (SELECT oid FROM pg_catalog.pg_roles
                                 WHERE rolname = current_user) AS owned,
                   s.srvname,
                   (SELECT option_value
                    FROM pg_catalog.pg_options_to_table(ft.ftoptions)
                    WHERE option_name = 'schema_name') AS remote_schema,
                   (SELECT option_value
                    FROM pg_catalog.pg_options_to_table(ft.ftoptions)
                    WHERE option_name = 'table_name') AS remote_table,
                   encode(sha256(convert_to(COALESCE((
                     SELECT jsonb_agg(jsonb_build_object(
                       'name', a.attname,
                       'remoteName', COALESCE((
                         SELECT option_value
                         FROM pg_catalog.pg_options_to_table(a.attfdwoptions)
                         WHERE option_name = 'column_name'
                       ), a.attname),
                       'type', jsonb_build_array(tn.nspname, t.typname),
                       'typmod', a.atttypmod,
                       'notNull', a.attnotnull,
                       'collation', CASE WHEN co.oid IS NULL THEN NULL
                         ELSE jsonb_build_array(cn.nspname, co.collname)
                       END
                     ) ORDER BY a.attnum)
                     FROM pg_catalog.pg_attribute AS a
                     JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
                     JOIN pg_catalog.pg_namespace AS tn
                       ON tn.oid = t.typnamespace
                     LEFT JOIN pg_catalog.pg_collation AS co
                       ON co.oid = a.attcollation
                     LEFT JOIN pg_catalog.pg_namespace AS cn
                       ON cn.oid = co.collnamespace
                     WHERE a.attrelid = c.oid
                       AND a.attnum > 0
                       AND NOT a.attisdropped
                   ), '[]'::jsonb)::text, 'UTF8')), 'hex')
                     AS column_shape_fingerprint
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = c.relnamespace
            LEFT JOIN pg_catalog.pg_foreign_table AS ft
              ON ft.ftrelid = c.oid
            LEFT JOIN pg_catalog.pg_foreign_server AS s
              ON s.oid = ft.ftserver
            WHERE n.nspname = %s
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            """,
            (schema_name,),
        )
        return {row["relname"]: row for row in cur.fetchall()}

    @staticmethod
    def _binding_matches(
        binding, server_name: str, remote_schema: str, remote_table: str
    ) -> bool:
        return (
            binding is not None
            and binding["relkind"] == "f"
            and binding["owned"]
            and binding["srvname"] == server_name
            and binding["remote_schema"] == remote_schema
            and binding["remote_table"] == remote_table
        )

    @staticmethod
    def _connection_identity(connection_url: str) -> str:
        """The remote endpoint and login role a connectionRef currently
        resolves to (host/port/dbname/user — deliberately not password,
        a credential rather than an identity: re-authenticating the same
        role is already reconciled on every provision/reprovision call
        regardless of this check). Observe and Provision each resolve
        connectionRef independently, at their own call time (app.py's
        resolve_federation_connection_url); binding every observation to
        the endpoint and role it actually reached lets Provision detect a
        connectionRef rotated to a different endpoint OR a different
        remote user since the last Observe — has_table_privilege and any
        row-level security the source enforces are evaluated per-role, so
        a role change alone can invalidate a "current" schema flag just as
        much as a host change can."""
        params = psycopg.conninfo.conninfo_to_dict(connection_url)
        host = str(params.get("host", ""))
        hostaddr = str(params.get("hostaddr", ""))
        port = str(params.get("port", "5432"))
        dbname = str(params.get("dbname", ""))
        user = str(params.get("user", ""))
        endpoint = f"{host}[{hostaddr}]" if hostaddr else host
        return f"{user}@{endpoint}:{port}/{dbname}"

    def provision(
        self,
        alias: str,
        connection_url: str,
        actor: str,
        *,
        expected_observation_id: int,
        acknowledge_row_level_security: bool = False,
        acknowledge_schema_change: bool = False,
        acknowledge_physical_rebind: bool = False,
    ) -> dict[str, Any]:
        """Approve one exact observation and reconcile its local FDW state.

        Remote state is checked before local DDL and again after any import.
        A remote trusted to serve live data can still change after those
        checks; callers that need immutable approval must snapshot data.
        """
        alias = validate_alias(alias)
        if (
            isinstance(expected_observation_id, bool)
            or not isinstance(expected_observation_id, int)
            or expected_observation_id < 1
            or expected_observation_id > 9223372036854775807
        ):
            raise FederationSchemaError(
                "expectedObservationId must be a positive integer.",
                code="federation.invalid_observation_id",
            )
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{SCHEMA}:observe:{alias}",),
            )
            cur.execute(
                sql.SQL("""
                    SELECT {}, last_observed_connection_identity,
                           physical_identity, accepted_schema_fingerprint,
                           accepted_physical_identity,
                           accepted_connection_identity,
                           last_observation_id
                    FROM {}._aliases
                    WHERE alias = %s
                    FOR UPDATE
                """).format(self._SELECT_COLUMNS, sql.Identifier(SCHEMA)),
                (alias,),
            )
            row = cur.fetchone()
            if row is None:
                raise FileNotFoundError(alias)
            record = dict(row)
            last_observed_connection_identity = record.pop(
                "last_observed_connection_identity"
            )
            last_observed_physical_identity = record.pop("physical_identity")
            accepted_schema_fingerprint = record.pop("accepted_schema_fingerprint")
            accepted_physical_identity = record.pop("accepted_physical_identity")
            accepted_connection_identity = record.pop(
                "accepted_connection_identity"
            )
            last_observation_id = record.pop("last_observation_id")
            already_provisioned = record["provisionedAt"] is not None
            last_observation = record.get("lastObservation") or {}
            if record["status"] == "retired":
                # Terminal, and not merely cosmetic: retire() renamed the
                # server and schema out from under this alias, so the
                # reconcile path below would fail confusingly anyway. Refusing
                # here keeps retirement an actual guarantee that access is not
                # silently reinstated.
                raise FederationSchemaError(
                    f"Alias {alias!r} is retired and cannot be provisioned. "
                    "Register a new alias for the source instead.",
                    code="federation.alias_retired",
                )
            if last_observation_id != expected_observation_id:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s observation changed before approval.",
                    code="federation.observation_not_current",
                )
            if last_observation.get("schema") != "current":
                raise FederationSchemaError(
                    f"Alias {alias!r} has not been observed as current — "
                    "observe it again immediately before provisioning.",
                    code="federation.observation_not_current",
                )
            connection_identity = self._connection_identity(connection_url)
            if last_observed_connection_identity != connection_identity:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s connectionRef now resolves to a "
                    "different endpoint or remote role than its last "
                    "observation was taken against — observe it again before "
                    "provisioning.",
                    code="federation.observation_not_current",
                )
            enforce_tls_policy(record["tlsPolicy"], connection_url)
            local_default_collation = (
                _database_default_collation_identity(cur)
            )
            try:
                (
                    current_physical_identity,
                    live_remote_versions,
                    relations_verified,
                    rls_detected,
                    current_schema_fingerprint,
                    remote_column_shapes,
                ) = verify_remote_state(
                    connection_url,
                    tuple(record["allowedRelations"]),
                    local_default_collation=local_default_collation,
                )
            except psycopg.Error as exc:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source could not be reached to verify "
                    "its physical identity before provisioning — observe it "
                    "again.",
                    code="federation.observation_not_current",
                ) from exc
            if not relations_verified:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source's allowed relations are no "
                    "longer all present and selectable by the connecting "
                    "role — observe it again and review before "
                    "provisioning.",
                    code="federation.observation_not_current",
                )
            if (
                last_observed_physical_identity != current_physical_identity
                or last_observation.get("schemaFingerprint")
                    != current_schema_fingerprint
                or last_observation.get("rowLevelSecurityDetected", False)
                    != rls_detected
                or last_observation.get("extensionVersions", {})
                    != live_remote_versions
            ):
                raise FederationSchemaError(
                    f"Alias {alias!r}'s live evidence no longer matches "
                    "the observation being approved — observe it again.",
                    code="federation.observation_not_current",
                )
            if rls_detected and not acknowledge_row_level_security:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source has row-level security that "
                    "cannot distinguish individual platform callers: all "
                    "callers use one mapped remote role. Acknowledge this "
                    "shared identity explicitly to provision.",
                    code="federation.row_level_security_not_acknowledged",
                )
            rls_acknowledged = rls_detected and acknowledge_row_level_security
            if (
                (
                    accepted_physical_identity is not None
                    and accepted_physical_identity != current_physical_identity
                )
                or (
                    accepted_connection_identity is not None
                    and accepted_connection_identity != connection_identity
                )
            ) and not acknowledge_physical_rebind:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source binding differs from the one "
                    "previously accepted. Acknowledge this rebind explicitly "
                    "to provision.",
                    code="federation.physical_rebind_not_acknowledged",
                )
            schema_fingerprint_changed = (
                accepted_schema_fingerprint is not None
                and accepted_schema_fingerprint != current_schema_fingerprint
            )
            if schema_fingerprint_changed and not acknowledge_schema_change:
                raise FederationSchemaError(
                    f"Alias {alias!r}'s source's relation definitions "
                    "(columns or view query) no longer match what was "
                    "last explicitly accepted — acknowledge this "
                    "explicitly to provision.",
                    code="federation.schema_change_not_acknowledged",
                )
            params = psycopg.conninfo.conninfo_to_dict(connection_url)
            user = str(params.get("user", ""))
            password = str(params.get("password", ""))
            server_name = f"{alias}_srv"
            schema_name = f"source_{alias}"
            expected_bindings = {
                relation.split(".", 1)[1]: relation.split(".", 1)
                for relation in record["allowedRelations"]
            }

            cur.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")

            local_versions = extension_versions(cur)
            shippable = self._shippable_extensions(
                local_versions, live_remote_versions
            )
            desired_server_options = self._desired_server_options(
                params, shippable
            )

            if already_provisioned:
                current_server_options = self._current_server_options(
                    cur, server_name
                )
                self._reconcile_server_options(
                    cur,
                    server_name,
                    current_server_options,
                    desired_server_options,
                )
                if not self._local_schema_owned(cur, schema_name):
                    raise FederationSchemaError(
                        f"Federation schema {schema_name!r} is missing or "
                        "owned by another role.",
                        code="federation.local_state_invalid",
                )
                bindings = self._local_relation_bindings(cur, schema_name)
                if bindings.keys() - expected_bindings.keys():
                    raise FederationSchemaError(
                        f"Federation schema {schema_name!r} contains an "
                        "unmanaged relation.",
                        code="federation.local_state_invalid",
                    )
                relations_to_import = []
                for relation in record["allowedRelations"]:
                    remote_schema, remote_table = relation.split(".", 1)
                    binding = bindings.get(remote_table)
                    if binding is not None and (
                        binding["relkind"] != "f" or not binding["owned"]
                    ):
                        raise FederationSchemaError(
                            f"Federation relation {schema_name}.{remote_table} "
                            "is not an owned foreign table.",
                            code="federation.local_state_invalid",
                        )
                    binding_current = self._binding_matches(
                        binding, server_name, remote_schema, remote_table
                    )
                    column_shape_current = (
                        binding is not None
                        and binding["column_shape_fingerprint"]
                            == remote_column_shapes.get(relation)
                    )
                    if (
                        accepted_schema_fingerprint is None
                        or schema_fingerprint_changed
                        or not binding_current
                        or not column_shape_current
                    ):
                        if binding is not None:
                            try:
                                cur.execute(
                                    sql.SQL("DROP FOREIGN TABLE {}.{}").format(
                                        sql.Identifier(schema_name),
                                        sql.Identifier(remote_table),
                                    )
                                )
                            except psycopg.errors.DependentObjectsStillExist as exc:
                                # Re-provisioning re-imports the foreign table,
                                # and a managed derived layer reading it blocks
                                # the drop. retire() already refuses this case
                                # by name; without this, provision surfaced a
                                # raw psycopg error as a 502, which reads as
                                # "the registry is down" rather than "something
                                # still depends on this". PostgreSQL already
                                # names the dependants, so quote it rather than
                                # asking the derived store separately.
                                raise FederationSchemaError(
                                    f"Alias {alias!r} cannot be re-provisioned "
                                    "while managed relations still read its "
                                    "foreign tables. Drop or repoint them "
                                    "first. PostgreSQL reported: "
                                    + " ".join(
                                        (exc.diag.message_detail or "").split()
                                    ),
                                    code="federation.alias_in_use",
                                    status=HTTPStatus.CONFLICT,
                                ) from exc
                        relations_to_import.append(relation)
            else:
                server_options = [
                    sql.SQL("{} {}").format(
                        sql.Identifier(name), sql.Literal(value)
                    )
                    for name, value in desired_server_options.items()
                ]
                cur.execute(sql.SQL("""
                    CREATE SERVER {server}
                    FOREIGN DATA WRAPPER postgres_fdw
                    OPTIONS ({options})
                """).format(
                    server=sql.Identifier(server_name),
                    options=sql.SQL(", ").join(server_options),
                ))
                cur.execute(
                    sql.SQL("CREATE SCHEMA {}").format(
                        sql.Identifier(schema_name)
                    )
                )
                relations_to_import = list(record["allowedRelations"])

            for role in (None, self.derived_role, self.reader_role):
                principal = (
                    sql.SQL("CURRENT_USER")
                    if role is None
                    else sql.Identifier(role)
                )
                cur.execute(sql.SQL("""
                    DROP USER MAPPING IF EXISTS FOR {principal} SERVER {server}
                """).format(
                    principal=principal,
                    server=sql.Identifier(server_name),
                ))
                cur.execute(sql.SQL("""
                    CREATE USER MAPPING FOR {principal} SERVER {server}
                    OPTIONS (user {user}, password {password})
                """).format(
                    principal=principal,
                    server=sql.Identifier(server_name),
                    user=sql.Literal(user),
                    password=sql.Literal(password),
                ))

            if relations_to_import:
                self._import_relations(
                    cur,
                    alias,
                    server_name,
                    schema_name,
                    relations_to_import,
                )
                try:
                    imported_state = verify_remote_state(
                        connection_url,
                        tuple(record["allowedRelations"]),
                        local_default_collation=local_default_collation,
                    )
                except psycopg.Error as exc:
                    raise FederationSchemaError(
                        f"Alias {alias!r}'s source changed while importing.",
                        code="federation.observation_not_current",
                    ) from exc
                if imported_state != (
                    current_physical_identity,
                    live_remote_versions,
                    relations_verified,
                    rls_detected,
                    current_schema_fingerprint,
                    remote_column_shapes,
                ):
                    raise FederationSchemaError(
                        f"Alias {alias!r}'s source changed while importing.",
                        code="federation.observation_not_current",
                    )

            local_bindings = self._local_relation_bindings(cur, schema_name)
            valid_local_state = local_bindings.keys() == expected_bindings.keys()
            valid_local_state = valid_local_state and all(
                self._binding_matches(
                    local_bindings[remote_table],
                    server_name,
                    remote_schema,
                    remote_table,
                )
                and local_bindings[remote_table]["column_shape_fingerprint"]
                    == remote_column_shapes.get(
                        f"{remote_schema}.{remote_table}"
                    )
                for remote_table, (remote_schema, _) in expected_bindings.items()
            )
            if not valid_local_state:
                raise FederationSchemaError(
                    f"Federation schema {schema_name!r} does not exactly "
                    "match its registered allowlist.",
                    code="federation.local_state_invalid",
                )

            for role in dict.fromkeys((self.derived_role, self.reader_role)):
                cur.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema_name), sql.Identifier(role)
                    )
                )
                cur.execute(
                    sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                        sql.Identifier(schema_name), sql.Identifier(role)
                    )
                )

            cur.execute(
                sql.SQL("""
                    INSERT INTO {}._approvals
                      (alias, observation_id, approved_by,
                       acknowledged_row_level_security,
                       acknowledged_schema_change,
                       acknowledged_physical_rebind)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """).format(sql.Identifier(SCHEMA)),
                (
                    alias,
                    expected_observation_id,
                    actor,
                    acknowledge_row_level_security,
                    acknowledge_schema_change,
                    acknowledge_physical_rebind,
                ),
            )
            cur.execute(
                sql.SQL("""
                    UPDATE {}._aliases
                    SET provisioned_at = COALESCE(
                            provisioned_at, clock_timestamp()
                        ),
                        status = 'active',
                        approved_by = %s,
                        approved_at = clock_timestamp(),
                        row_level_security_acknowledged = %s,
                        accepted_schema_fingerprint = %s,
                        accepted_physical_identity = %s,
                        accepted_connection_identity = %s
                    WHERE alias = %s
                """).format(sql.Identifier(SCHEMA)),
                (
                    actor,
                    rls_acknowledged,
                    current_schema_fingerprint,
                    current_physical_identity,
                    connection_identity,
                    alias,
                ),
            )
            return self._get_with_cursor(cur, alias)

    def retire(self, alias: str, actor: str) -> dict[str, Any]:
        """Stop serving an alias and archive its physical objects.

        Retirement is not deletion. The alias row and its whole observation
        and approval history are retained, and the foreign server, schema, and
        foreign tables are renamed rather than dropped, so the exact-identity
        audit trail stays inspectable in the catalogue itself rather than only
        as metadata in this registry.

        The caller must have established that no derived layer still reads
        from this alias — see app.py, which refuses using
        DerivedLayerStore.affected_by_source_schema(). That check lives at the
        route because the federation registry deliberately does not import the
        derived-layer store. Revoking access here would otherwise leave a
        dependent materialized view refreshing against a source nobody
        believes is still connected.

        Revoking precedes the rename because revoking is the step that
        actually stops the source serving rows; a rename alone does not, since
        PostgreSQL tracks dependencies by oid and any existing grant would
        simply follow the object to its new name.
        """
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    SELECT status, provisioned_at
                    FROM {}._aliases
                    WHERE alias = %s
                    FOR UPDATE
                """).format(sql.Identifier(SCHEMA)),
                (alias,),
            )
            row = cur.fetchone()
            if row is None:
                raise FileNotFoundError(alias)
            if row["status"] == "retired":
                raise FederationSchemaError(
                    f"Alias {alias!r} is already retired.",
                    code="federation.already_retired",
                )

            archived_schema = None
            # Recorded only once the rename actually happens. The audit must
            # never have to guess this name: deriving it from archived_schema
            # is wrong (the alias is truncated four characters further for the
            # server), and classifying it by a "retired_" prefix is wrong too
            # (ALIAS_RE permits an alias called retired_sites, whose live
            # server would then be read as an archive).
            archived_server_recorded = None
            schema_name = f"source_{alias}"
            # Three distinct local states, and they must not be conflated.
            # "Absent" is benign — there is nothing to revoke or archive, and
            # refusing would strand the alias forever since there is no delete
            # route. "Present but owned by another role" is not benign: the
            # REVOKEs below would be skipped while the alias still reported
            # itself retired, leaving both consumer roles holding the USAGE
            # and SELECT that provisioning granted. provision() already treats
            # that state as local_state_invalid, and retirement must not be
            # the one operation that silently accepts it.
            cur.execute(
                "SELECT pg_get_userbyid(nspowner) = current_user AS owned "
                "FROM pg_catalog.pg_namespace WHERE nspname = %s",
                (schema_name,),
            )
            local_schema = cur.fetchone()
            if local_schema is not None and not local_schema["owned"]:
                raise FederationSchemaError(
                    f"Federation schema {schema_name!r} is owned by another "
                    "role, so its access cannot be revoked. Resolve the "
                    "ownership before retiring the alias.",
                    code="federation.local_state_invalid",
                )
            # The schema and the server are archived under independent
            # gates. Tying the server work to the schema existing left a
            # decommissioned source holding live credentials whenever the
            # schema had already been removed by hand — the exact state the
            # branch below deliberately tolerates.
            if row["provisioned_at"] is not None:
                server_name = f"{alias}_srv"
                # Identifiers truncate silently at 63 bytes, so the archive
                # name is budgeted rather than left to truncate. Truncating
                # the alias alone is not enough to keep it unique: two aliases
                # sharing a long prefix and provisioned in the same second
                # would produce the same name, and since provisioned_at never
                # changes the second retirement would fail on every retry. A
                # short digest of the full alias makes the name unique per
                # alias, and the timestamp keeps it unique per provisioning.
                digest = hashlib.blake2s(
                    alias.encode(), digest_size=4
                ).hexdigest()
                suffix = (
                    row["provisioned_at"].strftime("_%Y%m%d%H%M%S")
                    + "_" + digest
                )
                # The hyphen is load-bearing. ALIAS_RE is
                # ^[A-Za-z][A-Za-z0-9_]{0,55}$, so no alias can contain one,
                # which puts archive names in a namespace live aliases cannot
                # reach. With "retired_" they shared one: the archive name for
                # a short alias is itself a legal alias (32 + len(alias)
                # characters, so legal whenever the alias is 24 or fewer), and
                # registering it would create a live server occupying the exact
                # name retirement later needs. Retiring the original would then
                # fail at ALTER SERVER ... RENAME TO and keep failing until the
                # squatter was retired. The digest distinguishes two archives
                # from each other; it does not distinguish an archive from a
                # live alias.
                prefix = "retired-"
                archived_schema = (
                    prefix + alias[: 63 - len(prefix) - len(suffix)] + suffix
                )
                archived_server = (
                    prefix + alias[: 63 - len(prefix) - len(suffix) - 4]
                    + suffix + "_srv"
                )

                if local_schema is None:
                    # Nothing local to revoke or rename, so nothing is
                    # archived and no archive name is recorded.
                    archived_schema = None
                else:
                    for role in dict.fromkeys(
                        (self.derived_role, self.reader_role)
                    ):
                        cur.execute(
                            sql.SQL(
                                "REVOKE SELECT ON ALL TABLES IN SCHEMA {} "
                                "FROM {}"
                            ).format(
                                sql.Identifier(schema_name),
                                sql.Identifier(role),
                            )
                        )
                        cur.execute(
                            sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(
                                sql.Identifier(schema_name),
                                sql.Identifier(role),
                            )
                        )
                    # A rename onto an existing name fails loudly rather than
                    # merging two archives, which is the behaviour we want.
                    cur.execute(
                        sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                            sql.Identifier(schema_name),
                            sql.Identifier(archived_schema),
                        )
                    )
                # The user mappings are the one thing retirement does drop.
                # "Nothing is dropped" exists to preserve the audit trail, and
                # a mapping is not audit evidence — it is the live credential
                # for the remote, stored in the catalogue in plain text. There
                # is no reason for a decommissioned source to keep working
                # credentials indefinitely, and the archived server, schema and
                # foreign tables still record exactly what was connected.
                # Guarded for the same reason the schema is: a server already
                # removed by hand must not make the alias un-retirable.
                cur.execute(
                    "SELECT 1 FROM pg_catalog.pg_foreign_server WHERE srvname = %s",
                    (server_name,),
                )
                if cur.fetchone():
                    cur.execute(
                        "SELECT usename AS role_name "
                        "FROM pg_catalog.pg_user_mappings "
                        "WHERE srvname = %s AND usename IS NOT NULL",
                        (server_name,),
                    )
                    for mapping in cur.fetchall():
                        cur.execute(
                            sql.SQL(
                                "DROP USER MAPPING IF EXISTS FOR {} SERVER {}"
                            ).format(
                                sql.Identifier(mapping["role_name"]),
                                sql.Identifier(server_name),
                            )
                        )
                    cur.execute(
                        sql.SQL("ALTER SERVER {} RENAME TO {}").format(
                            sql.Identifier(server_name),
                            sql.Identifier(archived_server),
                        )
                    )
                    archived_server_recorded = archived_server

            cur.execute(
                sql.SQL("""
                    UPDATE {}._aliases
                    SET status = 'retired',
                        retired_at = clock_timestamp(),
                        retired_by = %s,
                        archived_schema = %s,
                        archived_server = %s
                    WHERE alias = %s
                """).format(sql.Identifier(SCHEMA)),
                (actor, archived_schema, archived_server_recorded, alias),
            )
            return self._get_with_cursor(cur, alias)
