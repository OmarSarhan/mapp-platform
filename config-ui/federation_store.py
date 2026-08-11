"""Durable alias registry for federation (architecture waypoint decision #3).

Wires the previously-unwired `federation_schema`/`federation_capability`
validation and detection logic into a real, queryable Postgres store. The
registry lives in a new `federation` schema inside the *existing* derived
database rather than a separate dedicated federation database — the doc's
migration into a standalone federation database (`MAPP_DATABASE_MODE`
`federated`) is a distinct, later piece of work, not required to prove FDW
mechanics end to end.

Deliberately out of scope here (see the accompanying plan): the doc's
write-only secret-submission/verify-not-read endpoints (we resolve
`connectionRef` through the existing `DBS_<ALIAS>` convention instead),
alias-count ceilings, registration TTL, and retire/reclaim. Also
deliberately out of scope: forcing derived layers through an integration
view before they may read a foreign schema. Instead this module answers a
narrower, explicitly requested question — which derived layers currently
read from a given alias, so removing it is an informed choice rather than a
silent break.
"""

from __future__ import annotations

import threading
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from federation_schema import (
    FederationSchemaError,
    validate_alias,
    validate_registration,
)

SCHEMA = "federation"

# Must match derived_layers.SCHEMA. Hardcoded rather than imported to avoid
# pulling in the whole derived_layers module for one stable constant.
DERIVED_LAYERS_SCHEMA = "derived_layers"


class FederationAliasStore:
    def __init__(self, connection_string: str, reader_role: str):
        if not connection_string:
            raise FederationSchemaError(
                "Federation alias registry is not configured.",
                code="federation.not_configured",
            )
        self.connection_string = connection_string
        self.reader_role = reader_role
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
        # The federation schema itself is provisioned by docker/postgis's
        # role-setup scripts (owned by the derived-owner role), the same way
        # derived_layers is — not created lazily here. The derived-owner
        # role has no CREATE privilege on the database itself to do so.
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
              provisioned_at timestamptz
            )
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "REVOKE ALL ON {}._aliases FROM PUBLIC"
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
        provisioned_at AS "provisionedAt"
    """)

    def register(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        record = validate_registration(payload)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT 1 FROM {}._aliases WHERE alias = %s").format(
                    sql.Identifier(SCHEMA)
                ),
                (record["alias"],),
            )
            if cur.fetchone():
                raise FileExistsError(record["alias"])
            cur.execute(
                sql.SQL("""
                    INSERT INTO {}._aliases
                      (alias, display_name, kind, connection_ref,
                       allowed_relations, status, freshness_strategy,
                       data_handling_classification, registered_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )
        return self.get(record["alias"])

    def get(self, alias: str) -> dict[str, Any]:
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
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
                sql.SQL("SELECT {} FROM {}._aliases ORDER BY alias").format(
                    self._SELECT_COLUMNS, sql.Identifier(SCHEMA)
                )
            )
            return list(cur.fetchall())

    def record_observation(
        self, alias: str, observation: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist an already-validated observation (see federation_capability.detect_capability)."""
        alias = validate_alias(alias)
        status = (
            "active" if observation["connectivity"] == "reachable" else "unavailable"
        )
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    UPDATE {}._aliases
                    SET last_observation = %s, status = %s
                    WHERE alias = %s
                    RETURNING alias
                """).format(sql.Identifier(SCHEMA)),
                (Jsonb(observation), status, alias),
            )
            if cur.fetchone() is None:
                raise FileNotFoundError(alias)
        return self.get(alias)

    def provision(self, alias: str, connection_url: str) -> dict[str, Any]:
        """Create the real FDW server, user mapping, schema, and foreign
        tables for exactly this alias's allowedRelations."""
        alias = validate_alias(alias)
        record = self.get(alias)
        if record["provisionedAt"] is not None:
            raise FederationSchemaError(
                f"Alias {alias!r} is already provisioned.",
                code="federation.already_provisioned",
            )
        params = psycopg.conninfo.conninfo_to_dict(connection_url)
        host = str(params.get("host", ""))
        port = str(params.get("port", "5432"))
        dbname = str(params.get("dbname", ""))
        user = str(params.get("user", ""))
        password = str(params.get("password", ""))
        server_name = f"{alias}_srv"
        schema_name = f"source_{alias}"

        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
            cur.execute(sql.SQL("""
                CREATE SERVER IF NOT EXISTS {server}
                FOREIGN DATA WRAPPER postgres_fdw
                OPTIONS (
                  host {host}, port {port}, dbname {dbname},
                  use_remote_estimate 'true'
                )
            """).format(
                server=sql.Identifier(server_name),
                host=sql.Literal(host),
                port=sql.Literal(port),
                dbname=sql.Literal(dbname),
            ))
            cur.execute(sql.SQL("""
                CREATE USER MAPPING IF NOT EXISTS FOR CURRENT_USER
                SERVER {server}
                OPTIONS (user {user}, password {password})
            """).format(
                server=sql.Identifier(server_name),
                user=sql.Literal(user),
                password=sql.Literal(password),
            ))
            cur.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(schema_name)
                )
            )
            for relation in record["allowedRelations"]:
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
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema_name), sql.Identifier(self.reader_role)
                )
            )
            cur.execute(
                sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                    sql.Identifier(schema_name), sql.Identifier(self.reader_role)
                )
            )
            cur.execute(
                sql.SQL("""
                    UPDATE {}._aliases SET provisioned_at = clock_timestamp()
                    WHERE alias = %s
                """).format(sql.Identifier(SCHEMA)),
                (alias,),
            )
        return self.get(alias)

    def affected_derived_layer_names(self, alias: str) -> list[str]:
        """Derived layers whose declared sources read from this alias's
        foreign schema (`source_<alias>`) — the impact-visibility query."""
        alias = validate_alias(alias)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    SELECT name FROM {}._definitions
                    WHERE EXISTS (
                      SELECT 1 FROM unnest(sources) AS source
                      WHERE split_part(source, '.', 1) = %s
                    )
                    ORDER BY name
                """).format(sql.Identifier(DERIVED_LAYERS_SCHEMA)),
                (f"source_{alias}",),
            )
            return [row["name"] for row in cur.fetchall()]
