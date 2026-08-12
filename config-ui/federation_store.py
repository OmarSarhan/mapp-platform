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

from federation_capability import extension_versions
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
              last_observed_connection_identity text,
              provisioned_at timestamptz
            )
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "ALTER TABLE {}._aliases ADD COLUMN IF NOT EXISTS "
            "last_observed_connection_identity text"
        ).format(sql.Identifier(SCHEMA)))
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
        self, alias: str, observation: dict[str, Any], connection_url: str
    ) -> dict[str, Any]:
        """Persist an already-validated observation (see federation_capability.detect_capability)."""
        alias = validate_alias(alias)
        reachable = observation["connectivity"] == "reachable"
        connection_identity = self._connection_identity(connection_url)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    UPDATE {}._aliases
                    SET last_observation = %s,
                        last_observed_connection_identity = %s,
                        status = CASE
                          WHEN provisioned_at IS NULL THEN status
                          WHEN %s THEN 'active'
                          ELSE 'unavailable'
                        END
                    WHERE alias = %s
                    RETURNING alias, provisioned_at
                """).format(sql.Identifier(SCHEMA)),
                (Jsonb(observation), connection_identity, reachable, alias),
            )
            row = cur.fetchone()
            if row is None:
                raise FileNotFoundError(alias)
            # A version drift discovered after provisioning must fail
            # pushdown closed immediately (docs/federation-architecture-
            # waypoint.md: "a version drift detected after provisioning
            # downgrades the alias to pushdown disabled") — re-enabling it
            # afterward is a deliberate federation:provision-scoped
            # reprovisioning action, never automatic, so this only ever
            # drops the option here, never adds it.
            if row["provisioned_at"] is not None:
                server_name = f"{alias}_srv"
                local_versions = extension_versions(cur)
                remote_versions = observation.get("extensionVersions") or {}
                shippable = self._shippable_extensions(
                    local_versions, remote_versions
                )
                current = self._current_shippable_extensions(cur, server_name)
                if current and not shippable:
                    self._apply_shippable_extensions(
                        cur, server_name, current, shippable
                    )
        return self.get(alias)

    # PostGIS/PROJ/GEOS versions may differ between the federation database
    # and a source — execution happens in the federation database, so a
    # pushed-down expression (e.g. ST_Transform) can silently return a
    # different result from the same expression evaluated locally if the
    # versions disagree. Only ever mark postgis shippable when all three
    # exactly match the alias's last observation of the remote.
    _VERSION_MATCH_KEYS = ("postgis", "proj", "geos")

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
            "SELECT srvoptions FROM pg_catalog.pg_foreign_server "
            "WHERE srvname = %s",
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

    _SSL_OPTIONS = ("sslmode", "sslrootcert", "sslcert", "sslkey")

    @staticmethod
    def _current_server_options(cur, server_name: str) -> dict[str, str]:
        cur.execute(
            "SELECT srvoptions FROM pg_catalog.pg_foreign_server "
            "WHERE srvname = %s",
            (server_name,),
        )
        row = cur.fetchone()
        options: dict[str, str] = {}
        for option in (row["srvoptions"] if row and row["srvoptions"] else []):
            key, _, value = option.partition("=")
            options[key] = value
        return options

    @classmethod
    def _reconcile_ssl_options(
        cls, cur, server_name: str, current: dict[str, str], params
    ) -> None:
        """Reconcile sslmode/sslrootcert/sslcert/sslkey to match the
        connectionRef's current connection string — a rotated SSL setting
        (e.g. require -> verify-full, a renewed CA/client certificate, or
        one removed outright) must reach the live server the same way a
        rotated host/password does, or foreign-table queries keep using
        stale, possibly weaker, transport settings."""
        actions = []
        for ssl_option in cls._SSL_OPTIONS:
            value = params.get(ssl_option)
            if value:
                verb = "SET" if ssl_option in current else "ADD"
                actions.append(
                    sql.SQL("{} {} {}").format(
                        sql.SQL(verb),
                        sql.Identifier(ssl_option),
                        sql.Literal(str(value)),
                    )
                )
            elif ssl_option in current:
                actions.append(
                    sql.SQL("DROP {}").format(sql.Identifier(ssl_option))
                )
        if actions:
            cur.execute(
                sql.SQL("ALTER SERVER {} OPTIONS ({})").format(
                    sql.Identifier(server_name), sql.SQL(", ").join(actions)
                )
            )

    @staticmethod
    def _connection_identity(connection_url: str) -> str:
        """The physical endpoint a connectionRef currently resolves to
        (host/port/dbname — deliberately not user/password). Observe and
        Provision each resolve connectionRef independently, at their own
        call time (app.py's resolve_federation_connection_url); binding
        every observation to the endpoint it actually reached lets
        Provision detect a connectionRef rotated to a different endpoint
        since the last Observe, rather than trusting a "current" schema
        flag that only proves some past endpoint was current."""
        params = psycopg.conninfo.conninfo_to_dict(connection_url)
        host = str(params.get("host", ""))
        port = str(params.get("port", "5432"))
        dbname = str(params.get("dbname", ""))
        return f"{host}:{port}/{dbname}"

    def _last_observed_connection_identity(self, alias: str) -> str | None:
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                sql.SQL("""
                    SELECT last_observed_connection_identity
                    FROM {}._aliases
                    WHERE alias = %s
                """).format(sql.Identifier(SCHEMA)),
                (alias,),
            )
            row = cur.fetchone()
            return row["last_observed_connection_identity"] if row else None

    def provision(
        self,
        alias: str,
        connection_url: str,
        *,
        acknowledge_row_level_security: bool = False,
    ) -> dict[str, Any]:
        """Create the real FDW server, user mapping, schema, and foreign
        tables for this alias's allowedRelations the first time this is
        called. Callable again afterward as a reprovisioning action (e.g.
        an admin acting on a drift observation) — a repeat call only
        re-decides and reconciles the server's `extensions` option; it
        does not repeat CREATE SERVER/USER MAPPING/IMPORT FOREIGN SCHEMA,
        which would fail on objects that already exist."""
        alias = validate_alias(alias)
        record = self.get(alias)
        already_provisioned = record["provisionedAt"] is not None
        last_observation = record.get("lastObservation") or {}
        # Every MAPP caller queries a federated source through the same
        # mapped remote user (there is only one connectionRef per alias),
        # so any row-level security the source enforces per connecting
        # user is bypassed entirely once federated — the mapped role's
        # full row set becomes visible to every runtime caller. The
        # generic dataHandlingAcknowledged at registration cannot cover
        # this: RLS is only discovered later, by Observe.
        if last_observation.get("rowLevelSecurityDetected") and not (
            acknowledge_row_level_security
        ):
            raise FederationSchemaError(
                f"Alias {alias!r}'s source has row-level security that "
                "would be bypassed once federated — acknowledge this "
                "explicitly to provision.",
                code="federation.row_level_security_not_acknowledged",
            )
        # Applies to reprovisioning too, not just the first call: the
        # reprovision branch below never re-verifies or re-imports the
        # foreign tables, so without this, /provision on an alias whose
        # latest observation reports "changed" would still reconcile
        # connection settings and report success — silently implying
        # the schema drift it never actually addressed was fine.
        if last_observation.get("schema") != "current":
            raise FederationSchemaError(
                f"Alias {alias!r} has not been observed as current — "
                "observe it again immediately before provisioning.",
                code="federation.observation_not_current",
            )
        # A "current" schema above only proves *some* past Observe call
        # was current — not that it was taken against the endpoint this
        # call is about to provision. If connectionRef's DBS_<NAME> was
        # rotated to a different host/database since that Observe (e.g.
        # the service restarted with a new value), this call would
        # otherwise activate identically-named relations on an endpoint
        # that was never observed or reviewed.
        connection_identity = self._connection_identity(connection_url)
        if self._last_observed_connection_identity(alias) != connection_identity:
            raise FederationSchemaError(
                f"Alias {alias!r}'s connectionRef now resolves to a "
                "different endpoint than its last observation was taken "
                "against — observe it again before provisioning.",
                code="federation.observation_not_current",
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

            # postgres_fdw only ships operators/functions to the remote
            # side for extensions explicitly marked "shippable" — PostGIS
            # isn't in its built-in list, so spatial predicates would
            # otherwise pull the whole remote relation and filter locally.
            # Only mark it shippable when the federation database's own
            # PostGIS/PROJ/GEOS versions exactly match this alias's last
            # observed remote versions (see _shippable_extensions above) —
            # an unconditional claim would risk a silently wrong pushed-
            # down result, not just a remote execution error. This is a
            # same-owner, non-superuser server option; it does not
            # duplicate any data, it only widens what may cross the wire
            # as a pushed-down predicate instead of a full relation.
            local_versions = extension_versions(cur)
            remote_versions = (
                record.get("lastObservation") or {}
            ).get("extensionVersions") or {}
            shippable = self._shippable_extensions(local_versions, remote_versions)

            if already_provisioned:
                # A reprovisioning call is also the only place a rotated
                # connectionRef (new password, host, or database behind the
                # same alias) ever reaches the live foreign server —
                # reconcile it every time, not just the extensions option.
                cur.execute(sql.SQL("""
                    ALTER SERVER {server} OPTIONS (
                        SET host {host}, SET port {port}, SET dbname {dbname}
                    )
                """).format(
                    server=sql.Identifier(server_name),
                    host=sql.Literal(host),
                    port=sql.Literal(port),
                    dbname=sql.Literal(dbname),
                ))
                current_server_options = self._current_server_options(
                    cur, server_name
                )
                self._reconcile_ssl_options(
                    cur, server_name, current_server_options, params
                )
                cur.execute(sql.SQL("""
                    ALTER USER MAPPING FOR CURRENT_USER SERVER {server}
                    OPTIONS (SET user {user}, SET password {password})
                """).format(
                    server=sql.Identifier(server_name),
                    user=sql.Literal(user),
                    password=sql.Literal(password),
                ))
                # DROP + CREATE, not ALTER: an alias provisioned before the
                # reader mapping existed has no mapping for self.reader_role
                # yet to ALTER — this converges it the next time it's
                # reprovisioned, and is a no-op-then-recreate for one that
                # already has a mapping.
                cur.execute(sql.SQL("""
                    DROP USER MAPPING IF EXISTS FOR {reader} SERVER {server}
                """).format(
                    reader=sql.Identifier(self.reader_role),
                    server=sql.Identifier(server_name),
                ))
                cur.execute(sql.SQL("""
                    CREATE USER MAPPING FOR {reader} SERVER {server}
                    OPTIONS (user {user}, password {password})
                """).format(
                    reader=sql.Identifier(self.reader_role),
                    server=sql.Identifier(server_name),
                    user=sql.Literal(user),
                    password=sql.Literal(password),
                ))
                current = self._current_shippable_extensions(cur, server_name)
                self._apply_shippable_extensions(cur, server_name, current, shippable)
                return self.get(alias)

            server_options = [
                sql.SQL("host {}").format(sql.Literal(host)),
                sql.SQL("port {}").format(sql.Literal(port)),
                sql.SQL("dbname {}").format(sql.Literal(dbname)),
                sql.SQL("use_remote_estimate 'true'"),
            ]
            # Forward whatever transport guarantees the operator already put
            # in the connectionRef's connection string (the repo's existing
            # sslmode/sslrootcert convention — see .env.example) instead of
            # letting libpq silently fall back to its own permissive default.
            for ssl_option in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
                value = params.get(ssl_option)
                if value:
                    server_options.append(
                        sql.SQL("{} {}").format(
                            sql.Identifier(ssl_option), sql.Literal(str(value))
                        )
                    )
            if shippable:
                server_options.append(
                    sql.SQL("extensions {}").format(
                        sql.Literal(",".join(shippable))
                    )
                )
            # Not IF NOT EXISTS: same reasoning as the schema below — a
            # not-yet-provisioned alias should never legitimately reach a
            # pre-existing <alias>_srv. Silently reusing one from an
            # unrelated origin (and possibly a different actual remote
            # endpoint) would import and grant access to the wrong data
            # under this alias's name. Fail closed.
            cur.execute(sql.SQL("""
                CREATE SERVER {server}
                FOREIGN DATA WRAPPER postgres_fdw
                OPTIONS ({options})
            """).format(
                server=sql.Identifier(server_name),
                options=sql.SQL(", ").join(server_options),
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
            # A security_invoker=true derived VIEW (derived_layers.py) runs
            # its underlying query as whichever role queries the view — the
            # runtime reader, not the derived owner — so it needs its own
            # mapping too, not just CURRENT_USER's. Both mappings use the
            # same remote credential; there is only one connectionRef per
            # alias. (A materialized view's REFRESH always runs as the
            # derived owner, so this isn't needed for that path, but views
            # are a supported derived-layer kind and must work too.)
            cur.execute(sql.SQL("""
                CREATE USER MAPPING IF NOT EXISTS FOR {reader}
                SERVER {server}
                OPTIONS (user {user}, password {password})
            """).format(
                reader=sql.Identifier(self.reader_role),
                server=sql.Identifier(server_name),
                user=sql.Literal(user),
                password=sql.Literal(password),
            ))
            # Not IF NOT EXISTS: an alias's own reprovisioning never reaches
            # this branch (see already_provisioned above), so the only way
            # this schema could already exist here is an unrelated object —
            # e.g. a stray schema an operator created by hand that happens
            # to collide with source_<alias>. Silently reusing it would
            # import into and then GRANT SELECT ON ALL TABLES IN that
            # schema, exposing whatever was already there. Fail closed.
            cur.execute(
                sql.SQL("CREATE SCHEMA {}").format(
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
            # IMPORT FOREIGN SCHEMA ... LIMIT TO silently imports whatever
            # of the named relations actually exists on the remote — it
            # does not error on one that's missing. Confirm every approved
            # relation actually landed as a foreign table before marking
            # the alias active; the whole transaction rolls back otherwise,
            # rather than leaving an alias active with an incomplete set.
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
            expected = {
                relation.split(".", 1)[1]
                for relation in record["allowedRelations"]
            }
            missing = sorted(expected - imported)
            if missing:
                raise FederationSchemaError(
                    f"Alias {alias!r} provisioning did not import: "
                    f"{missing} — the source schema may have changed "
                    "since it was last observed.",
                    code="federation.import_incomplete",
                )
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
                    UPDATE {}._aliases
                    SET provisioned_at = clock_timestamp(), status = 'active'
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
