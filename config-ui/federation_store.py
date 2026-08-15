"""Durable federation alias registry and postgres_fdw provisioner."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from federation_capability import (
    _database_default_collation_identity,
    detect_capability,
    extension_versions,
    verify_remote_state,
)
from federation_schema import (
    FederationSchemaError,
    enforce_tls_policy,
    validate_alias,
    validate_registration,
)

SCHEMA = "federation"

MAX_ALIASES = 100


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
              archived_schema text
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
        archived_schema AS "archivedSchema"
    """)

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
                           count(*) FILTER (WHERE alias = %s) > 0 AS exists
                    FROM {}._aliases
                """).format(sql.Identifier(SCHEMA)),
                (record["alias"],),
            )
            registry = cur.fetchone()
            if registry["exists"]:
                raise FileExistsError(record["alias"])
            if registry["count"] >= MAX_ALIASES:
                raise FederationSchemaError(
                    f"Federation alias limit ({MAX_ALIASES}) reached.",
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

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT {} FROM {}._aliases ORDER BY alias LIMIT %s"
                ).format(self._SELECT_COLUMNS, sql.Identifier(SCHEMA)),
                (MAX_ALIASES + 1,),
            )
            return list(cur.fetchall())

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
            schema_name = f"source_{alias}"
            if self._local_schema_owned(cur, schema_name):
                action = "GRANT" if evidence_current else "REVOKE"
                preposition = "TO" if evidence_current else "FROM"
                for role in dict.fromkeys((self.derived_role, self.reader_role)):
                    cur.execute(
                        sql.SQL(
                            f"{action} USAGE ON SCHEMA {{}} {preposition} {{}}"
                        ).format(
                            sql.Identifier(schema_name), sql.Identifier(role)
                        )
                    )
                    cur.execute(
                        sql.SQL(
                            f"{action} SELECT ON ALL TABLES IN SCHEMA {{}} "
                            f"{preposition} {{}}"
                        ).format(
                            sql.Identifier(schema_name), sql.Identifier(role)
                        )
                    )

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
                            cur.execute(
                                sql.SQL("DROP FOREIGN TABLE {}.{}").format(
                                    sql.Identifier(schema_name),
                                    sql.Identifier(remote_table),
                                )
                            )
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
            schema_name = f"source_{alias}"
            # "Provisioned but locally absent or foreign-owned" is a modelled
            # state elsewhere in this class, so retirement must tolerate it
            # too: without this guard the REVOKE below raises undefined_schema
            # and the alias could never be retired at all. Recording the
            # retirement is still correct — there is simply nothing left to
            # archive.
            if row["provisioned_at"] is not None and self._local_schema_owned(
                cur, schema_name
            ):
                server_name = f"{alias}_srv"
                # Identifiers truncate silently at 63 bytes, so budget the
                # suffix rather than letting a long alias produce a collision
                # between two archives. provisioned_at is used as the suffix
                # because it is already unique per provisioning and comes from
                # the database clock.
                suffix = row["provisioned_at"].strftime("_%Y%m%d%H%M%S")
                prefix = "retired_"
                archived_schema = (
                    prefix + alias[: 63 - len(prefix) - len(suffix)] + suffix
                )
                archived_server = (
                    prefix + alias[: 63 - len(prefix) - len(suffix) - 4]
                    + suffix + "_srv"
                )

                for role in dict.fromkeys((self.derived_role, self.reader_role)):
                    cur.execute(
                        sql.SQL(
                            "REVOKE SELECT ON ALL TABLES IN SCHEMA {} FROM {}"
                        ).format(
                            sql.Identifier(schema_name), sql.Identifier(role)
                        )
                    )
                    cur.execute(
                        sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(
                            sql.Identifier(schema_name), sql.Identifier(role)
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
                cur.execute(
                    sql.SQL("ALTER SERVER {} RENAME TO {}").format(
                        sql.Identifier(server_name),
                        sql.Identifier(archived_server),
                    )
                )

            cur.execute(
                sql.SQL("""
                    UPDATE {}._aliases
                    SET status = 'retired',
                        retired_at = clock_timestamp(),
                        retired_by = %s,
                        archived_schema = %s
                    WHERE alias = %s
                """).format(sql.Identifier(SCHEMA)),
                (actor, archived_schema, alias),
            )
            return self._get_with_cursor(cur, alias)
