from __future__ import annotations

import re
import secrets
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


SCHEMA = "derived_layers"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RELATION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
FORBIDDEN_SQL = re.compile(
    r"\b(?:alter|call|comment|copy|create|delete|do|drop|execute|grant|insert|"
    r"listen|merge|notify|refresh|reset|revoke|set|truncate|update|vacuum)\b",
    re.IGNORECASE,
)


class DerivedLayerError(ValueError):
    pass


class DerivedLayerDependencyError(DerivedLayerError):
    def __init__(self, name: str, dependents: list[str], *, removed_columns=None, dependent_columns=None):
        self.name = name
        self.dependents = dependents
        self.removed_columns = removed_columns or []
        self.dependent_columns = dependent_columns or []
        super().__init__(
            f"derived_layers.{name} is used by other PostgreSQL objects and "
            "cannot be replaced or dropped."
        )


def _relation(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not RELATION_RE.fullmatch(value):
        raise DerivedLayerError(
            "Source relations must be schema-qualified identifiers."
        )
    return tuple(value.split(".", 1))  # type: ignore[return-value]


def validate_definition(payload: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(
        set(payload)
        - {
            "name", "kind", "query", "sources", "idColumn",
            "geometryColumn", "description",
        }
    )
    if unknown:
        raise DerivedLayerError(
            "Unknown derived-layer properties: " + ", ".join(unknown)
        )
    name = payload.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise DerivedLayerError(
            "Name must start with a lowercase letter and contain only "
            "lowercase letters, numbers, and underscores."
        )
    kind = payload.get("kind", "view")
    if kind not in {"view", "materialized"}:
        raise DerivedLayerError("Kind must be view or materialized.")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise DerivedLayerError("A SELECT query is required.")
    query = query.strip()
    if len(query.encode()) > 256 * 1024:
        raise DerivedLayerError("Derived-layer SQL is limited to 256 KiB.")
    if ";" in query or "--" in query or "/*" in query or "*/" in query:
        raise DerivedLayerError(
            "SQL terminators and comments are not allowed."
        )
    if not re.match(r"^(?:select|with)\b", query, re.IGNORECASE):
        raise DerivedLayerError("Derived-layer SQL must be one SELECT query.")
    forbidden = FORBIDDEN_SQL.search(query)
    if forbidden:
        raise DerivedLayerError(
            f"SQL keyword {forbidden.group(0).upper()} is not allowed."
        )
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise DerivedLayerError("Declare at least one source relation.")
    normalized_sources = sorted(
        {".".join(_relation(source)) for source in sources}
    )
    if any(source.startswith(f"{SCHEMA}.") for source in normalized_sources):
        raise DerivedLayerError(
            "A managed derived layer cannot depend on another derived layer."
        )
    id_column = payload.get("idColumn")
    geometry_column = payload.get("geometryColumn")
    for label, value in (
        ("ID column", id_column),
        ("Geometry column", geometry_column),
    ):
        if not isinstance(value, str) or not NAME_RE.fullmatch(value):
            raise DerivedLayerError(
                f"{label} must be a lowercase field name containing only "
                "letters, numbers, and underscores."
            )
    return {
        "name": name,
        "kind": kind,
        "query": query,
        "sources": normalized_sources,
        "idColumn": id_column,
        "geometryColumn": geometry_column,
        "description": str(payload.get("description", "")).strip()[:2000],
    }


class DerivedLayerStore:
    def __init__(self, connection_string: str, reader_role: str):
        if not connection_string:
            raise DerivedLayerError(
                "Derived-layer database management is not configured."
            )
        if not NAME_RE.fullmatch(reader_role):
            raise DerivedLayerError(
                "DERIVED_READER_ROLE must be a PostgreSQL identifier."
            )
        self.connection_string = connection_string
        self.reader_role = reader_role

    def _connect(self):
        return psycopg.connect(
            self.connection_string,
            autocommit=False,
            row_factory=dict_row,
        )

    @staticmethod
    def _initialize(cur) -> None:
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._definitions (
              name text PRIMARY KEY,
              kind text NOT NULL CHECK (kind IN ('view', 'materialized')),
              query text NOT NULL,
              sources text[] NOT NULL,
              id_column text NOT NULL,
              geometry_column text NOT NULL,
              description text NOT NULL DEFAULT '',
              created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              created_by text NOT NULL,
              refreshed_at timestamptz
            )
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("REVOKE ALL ON {}._definitions FROM PUBLIC").format(
            sql.Identifier(SCHEMA)
        ))

    @staticmethod
    def _dependencies(cur, name: str) -> list[str]:
        cur.execute(
            """
            SELECT DISTINCT source_ns.nspname || '.' || source.relname AS relation
            FROM pg_rewrite AS rewrite
            JOIN pg_depend AS dependency
              ON dependency.classid = 'pg_rewrite'::regclass
             AND dependency.objid = rewrite.oid
            JOIN pg_class AS source ON source.oid = dependency.refobjid
            JOIN pg_namespace AS source_ns ON source_ns.oid = source.relnamespace
            WHERE rewrite.ev_class = %s::regclass
              AND dependency.refobjid <> rewrite.ev_class
              AND source.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND source_ns.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY relation
            """,
            (f"{SCHEMA}.{name}",),
        )
        return [row["relation"] for row in cur.fetchall()]

    @staticmethod
    def _incoming_dependents(cur, name: str) -> list[str]:
        cur.execute(
            """
            SELECT DISTINCT pg_describe_object(
              dependency.classid,
              dependency.objid,
              dependency.objsubid
            ) AS dependent
            FROM pg_depend AS dependency
            LEFT JOIN pg_rewrite AS rewrite
              ON dependency.classid = 'pg_rewrite'::regclass
             AND dependency.objid = rewrite.oid
            WHERE dependency.refobjid = %s::regclass
              AND dependency.refclassid = 'pg_class'::regclass
              AND dependency.deptype = 'n'
              AND COALESCE(rewrite.ev_class, 0) <> dependency.refobjid
            ORDER BY dependent
            """,
            (f"{SCHEMA}.{name}",),
        )
        return [row["dependent"] for row in cur.fetchall()]

    @staticmethod
    def _column_names(cur, name: str) -> list[str]:
        cur.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (f"{SCHEMA}.{name}",),
        )
        return [row["attname"] for row in cur.fetchall()]

    @staticmethod
    def _column_types(cur, name: str) -> dict[str, str]:
        cur.execute(
            """
            SELECT attname, format_type(atttypid, atttypmod) AS data_type
            FROM pg_attribute
            WHERE attrelid = %s::regclass
              AND attnum > 0
              AND NOT attisdropped
            ORDER BY attnum
            """,
            (f"{SCHEMA}.{name}",),
        )
        return {row["attname"]: row["data_type"] for row in cur.fetchall()}

    @staticmethod
    def _dependent_columns(cur, name: str) -> list[str]:
        cur.execute(
            """
            SELECT DISTINCT attribute.attname
            FROM pg_depend AS dependency
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = dependency.refobjid
             AND attribute.attnum = dependency.refobjsubid
            WHERE dependency.refobjid = %s::regclass
              AND dependency.refclassid = 'pg_class'::regclass
              AND dependency.deptype = 'n'
              AND dependency.refobjsubid > 0
            ORDER BY attribute.attname
            """,
            (f"{SCHEMA}.{name}",),
        )
        return [row["attname"] for row in cur.fetchall()]

    @staticmethod
    def _validate_output(cur, definition: dict[str, Any]) -> dict[str, Any]:
        relation = sql.Identifier(SCHEMA, definition["name"])
        identifier = sql.Identifier(definition["idColumn"])
        geometry = sql.Identifier(definition["geometryColumn"])
        cur.execute(
            sql.SQL("""
                SELECT
                  postgis_typmod_type(attribute.atttypmod) AS geometry_type,
                  postgis_typmod_srid(attribute.atttypmod) AS srid
                FROM pg_attribute AS attribute
                WHERE attribute.attrelid = {}::regclass
                  AND attribute.attname = %s
                  AND NOT attribute.attisdropped
            """).format(sql.Literal(f"{SCHEMA}.{definition['name']}")),
            (definition["geometryColumn"],),
        )
        geometry_metadata = cur.fetchone()
        if (
            not geometry_metadata
            or geometry_metadata["geometry_type"] in {None, ""}
            or int(geometry_metadata["srid"] or 0) <= 0
        ):
            raise DerivedLayerError(
                "The selected geometry field must contain PostGIS geometry "
                "with a known coordinate system (SRID)."
            )
        cur.execute(
            sql.SQL("""
                SELECT {}
                FROM {}
                GROUP BY {}
                HAVING {} IS NULL OR count(*) > 1
                LIMIT 1
            """).format(identifier, relation, identifier, identifier)
        )
        if cur.fetchone():
            raise DerivedLayerError(
                "The selected ID field must contain a unique value for every "
                "row and cannot contain empty values."
            )
        cur.execute(
            sql.SQL("SELECT 1 FROM {} WHERE {} IS NOT NULL LIMIT 1").format(
                relation, geometry
            )
        )
        if not cur.fetchone():
            raise DerivedLayerError(
                "The derived result has no non-null geometry."
            )
        return {
            "geometryType": geometry_metadata["geometry_type"],
            "srid": int(geometry_metadata["srid"]),
        }

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection, connection.cursor() as cur:
            self._initialize(cur)
            cur.execute(sql.SQL("""
                SELECT name, kind, sources, id_column AS "idColumn",
                       geometry_column AS "geometryColumn", description,
                       created_at AS "createdAt", created_by AS "createdBy",
                       refreshed_at AS "refreshedAt"
                FROM {}._definitions
                ORDER BY name
            """).format(sql.Identifier(SCHEMA)))
            return list(cur.fetchall())

    def get(self, name: str, *, include_query: bool = True) -> dict[str, Any]:
        if not NAME_RE.fullmatch(name):
            raise DerivedLayerError("Invalid derived-layer name.")
        with self._connect() as connection, connection.cursor() as cur:
            self._initialize(cur)
            cur.execute(sql.SQL("""
                SELECT name, kind, query, sources,
                       id_column AS "idColumn",
                       geometry_column AS "geometryColumn", description,
                       created_at AS "createdAt", created_by AS "createdBy",
                       refreshed_at AS "refreshedAt"
                FROM {}._definitions WHERE name = %s
            """).format(sql.Identifier(SCHEMA)), (name,))
            item = cur.fetchone()
            if not item:
                raise FileNotFoundError(name)
            if not include_query:
                item.pop("query", None)
            return item

    def dependents(self, name: str) -> list[str]:
        if not NAME_RE.fullmatch(name):
            raise DerivedLayerError("Invalid derived-layer name.")
        with self._connect() as connection, connection.cursor() as cur:
            self._initialize(cur)
            return self._incoming_dependents(cur, name)

    def create(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        definition = validate_definition(payload)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '30s'")
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA,))
            self._initialize(cur)
            cur.execute(sql.SQL("SELECT 1 FROM {}._definitions WHERE name = %s").format(
                sql.Identifier(SCHEMA)
            ), (definition["name"],))
            if cur.fetchone():
                raise FileExistsError(definition["name"])
            query = sql.SQL(definition["query"])
            target = sql.Identifier(SCHEMA, definition["name"])
            if definition["kind"] == "view":
                cur.execute(
                    sql.SQL(
                        "CREATE VIEW {} WITH (security_invoker=true, "
                        "security_barrier=true) AS {}"
                    ).format(target, query)
                )
            else:
                cur.execute(
                    sql.SQL("CREATE MATERIALIZED VIEW {} AS {}").format(
                        target, query
                    )
                )
            dependencies = self._dependencies(cur, definition["name"])
            if dependencies != definition["sources"]:
                raise DerivedLayerError(
                    "Declared sources do not match PostgreSQL dependencies: "
                    + ", ".join(dependencies)
                )
            output = self._validate_output(cur, definition)
            if definition["kind"] == "materialized":
                cur.execute(
                    sql.SQL(
                        "CREATE UNIQUE INDEX {} ON {} ({}) NULLS NOT DISTINCT"
                    ).format(
                        sql.Identifier(f"{definition['name']}_qid_uidx"),
                        target,
                        sql.Identifier(definition["idColumn"]),
                    )
                )
            cur.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    target, sql.Identifier(self.reader_role)
                )
            )
            cur.execute(
                sql.SQL("""
                    INSERT INTO {}._definitions
                      (name, kind, query, sources, id_column, geometry_column,
                       description, created_by, refreshed_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s THEN clock_timestamp() ELSE NULL END)
                """).format(sql.Identifier(SCHEMA)),
                (
                    definition["name"], definition["kind"], definition["query"],
                    definition["sources"], definition["idColumn"],
                    definition["geometryColumn"], definition["description"],
                    actor, definition["kind"] == "materialized",
                ),
            )
            item = self.get_in_transaction(cur, definition["name"])
            item.update(output)
            return item

    def get_in_transaction(self, cur, name: str) -> dict[str, Any]:
        cur.execute(sql.SQL("""
            SELECT name, kind, query, sources, id_column AS "idColumn",
                   geometry_column AS "geometryColumn", description,
                   created_at AS "createdAt", created_by AS "createdBy",
                   refreshed_at AS "refreshedAt"
            FROM {}._definitions WHERE name = %s
        """).format(sql.Identifier(SCHEMA)), (name,))
        return cur.fetchone()

    def refresh(self, name: str) -> dict[str, Any]:
        definition = self.get(name)
        if definition["kind"] != "materialized":
            raise DerivedLayerError("Only materialized views can be refreshed.")
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5min'")
            cur.execute(
                sql.SQL("REFRESH MATERIALIZED VIEW {}").format(
                    sql.Identifier(SCHEMA, name)
                )
            )
            cur.execute(sql.SQL("""
                UPDATE {}._definitions
                SET refreshed_at = clock_timestamp()
                WHERE name = %s
            """).format(sql.Identifier(SCHEMA)), (name,))
            return self.get_in_transaction(cur, name)

    def replace(self, name: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        definition = validate_definition(payload)
        if definition["name"] != name:
            raise DerivedLayerError("Replacement name must match the existing relation.")
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5min'")
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA,))
            self._initialize(cur)
            current = self.get_in_transaction(cur, name)
            if not current:
                raise FileNotFoundError(name)
            dependents = self._incoming_dependents(cur, name)
            dependent_columns = self._dependent_columns(cur, name)
            current_columns = self._column_names(cur, name)
            current_types = self._column_types(cur, name)

            temporary_name = f"swap_{secrets.token_hex(10)}"
            temporary = {**definition, "name": temporary_name}
            temporary_target = sql.Identifier(SCHEMA, temporary_name)
            query = sql.SQL(definition["query"])
            if definition["kind"] == "view":
                cur.execute(
                    sql.SQL(
                        "CREATE VIEW {} WITH (security_invoker=true, "
                        "security_barrier=true) AS {}"
                    ).format(temporary_target, query)
                )
            else:
                cur.execute(
                    sql.SQL("CREATE MATERIALIZED VIEW {} AS {}").format(
                        temporary_target, query
                    )
                )
            dependencies = self._dependencies(cur, temporary_name)
            if dependencies != definition["sources"]:
                raise DerivedLayerError(
                    "Declared sources do not match PostgreSQL dependencies: "
                    + ", ".join(dependencies)
                )
            output = self._validate_output(cur, temporary)
            replacement_columns = self._column_names(cur, temporary_name)
            replacement_types = self._column_types(cur, temporary_name)
            removed_columns = sorted(set(current_columns) - set(replacement_columns))
            added_columns = sorted(set(replacement_columns) - set(current_columns))
            changed_columns = sorted(
                column for column in set(current_columns) & set(replacement_columns)
                if current_types[column] != replacement_types[column]
            )
            if dependents:
                raise DerivedLayerDependencyError(
                    name, dependents, removed_columns=removed_columns,
                    dependent_columns=dependent_columns,
                )
            temporary_index = f"{temporary_name}_qid_uidx"
            if definition["kind"] == "materialized":
                cur.execute(
                    sql.SQL(
                        "CREATE UNIQUE INDEX {} ON {} ({}) NULLS NOT DISTINCT"
                    ).format(
                        sql.Identifier(temporary_index),
                        temporary_target,
                        sql.Identifier(definition["idColumn"]),
                    )
                )
            cur.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    temporary_target, sql.Identifier(self.reader_role)
                )
            )

            current_keyword = (
                sql.SQL("MATERIALIZED VIEW")
                if current["kind"] == "materialized"
                else sql.SQL("VIEW")
            )
            replacement_keyword = (
                sql.SQL("MATERIALIZED VIEW")
                if definition["kind"] == "materialized"
                else sql.SQL("VIEW")
            )
            cur.execute("SAVEPOINT derived_drop_guard")
            try:
                cur.execute(
                    sql.SQL("DROP {} {} RESTRICT").format(
                        current_keyword, sql.Identifier(SCHEMA, name)
                    )
                )
            except psycopg.errors.DependentObjectsStillExist:
                cur.execute("ROLLBACK TO SAVEPOINT derived_drop_guard")
                raise DerivedLayerDependencyError(
                    name,
                    self._incoming_dependents(cur, name),
                ) from None
            cur.execute(
                sql.SQL("ALTER {} {} RENAME TO {}").format(
                    replacement_keyword,
                    temporary_target,
                    sql.Identifier(name),
                )
            )
            if definition["kind"] == "materialized":
                cur.execute(
                    sql.SQL("ALTER INDEX {} RENAME TO {}").format(
                        sql.Identifier(SCHEMA, temporary_index),
                        sql.Identifier(f"{name}_qid_uidx"),
                    )
                )
            cur.execute(
                sql.SQL("""
                    UPDATE {}._definitions
                    SET kind = %s, query = %s, sources = %s,
                        id_column = %s, geometry_column = %s,
                        description = %s, created_by = %s,
                        refreshed_at = CASE WHEN %s
                          THEN clock_timestamp() ELSE NULL END
                    WHERE name = %s
                """).format(sql.Identifier(SCHEMA)),
                (
                    definition["kind"], definition["query"],
                    definition["sources"], definition["idColumn"],
                    definition["geometryColumn"], definition["description"],
                    actor, definition["kind"] == "materialized", name,
                ),
            )
            item = self.get_in_transaction(cur, name)
            item.update(output)
            item["replacedKind"] = current["kind"]
            item["columnChanges"] = {
                "added": added_columns,
                "removed": removed_columns,
                "changed": changed_columns,
            }
            return item

    def drop(self, name: str) -> dict[str, Any]:
        if not NAME_RE.fullmatch(name):
            raise DerivedLayerError("Invalid derived-layer name.")
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA,))
            self._initialize(cur)
            definition = self.get_in_transaction(cur, name)
            if not definition:
                raise FileNotFoundError(name)
            dependents = self._incoming_dependents(cur, name)
            if dependents:
                raise DerivedLayerDependencyError(name, dependents)
            keyword = (
                sql.SQL("MATERIALIZED VIEW")
                if definition["kind"] == "materialized"
                else sql.SQL("VIEW")
            )
            cur.execute("SAVEPOINT derived_drop_guard")
            try:
                cur.execute(
                    sql.SQL("DROP {} {} RESTRICT").format(
                        keyword, sql.Identifier(SCHEMA, name)
                    )
                )
            except psycopg.errors.DependentObjectsStillExist:
                cur.execute("ROLLBACK TO SAVEPOINT derived_drop_guard")
                raise DerivedLayerDependencyError(
                    name,
                    self._incoming_dependents(cur, name),
                ) from None
            cur.execute(sql.SQL("DELETE FROM {}._definitions WHERE name = %s").format(
                sql.Identifier(SCHEMA)
            ), (name,))
        return definition

    def capabilities(self) -> dict[str, Any]:
        with self._connect() as connection, connection.cursor() as cur:
            self._initialize(cur)
            cur.execute(
                """
                SELECT extname, extversion
                FROM pg_extension
                WHERE extname IN ('postgis', 'h3', 'h3_postgis')
                ORDER BY extname
                """
            )
            extensions = {row["extname"]: row["extversion"] for row in cur.fetchall()}
            return {
                "configured": True,
                "schema": SCHEMA,
                "kinds": ["view", "materialized"],
                "extensions": extensions,
                "h3Available": "h3" in extensions and "h3_postgis" in extensions,
            }
