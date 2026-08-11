from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Iterator, Mapping

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


DEFAULT_ALLOWLIST = "MAPP:leeds.*"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# Must match DB_KEY in workspace_schema.py and databaseKey in
# schema/workspace.schema.json — one alias grammar, not three. Max length
# 56: see federation_schema.py's ALIAS_RE for why (the shared grammar must
# leave room for the "source_" schema-name prefix federation adds).
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,55}$")
SYSTEM_SCHEMAS = {"information_schema", "derived_layers"}
SOURCE_NAMESPACE = uuid.UUID("b2228ad9-b2cb-5ed1-a906-901d8bb128bf")
RELATION_KINDS = {
    "r": "table",
    "p": "partitioned-table",
    "v": "view",
    "m": "materialized-view",
    "f": "foreign-table",
}
MAX_RELATION_DESCRIPTION = 2000
MAX_FIELD_DESCRIPTION = 1000
GENERATION_SAMPLE_PERCENT = 5
GENERATION_SAMPLE_MAX_ROWS = 100
GENERATION_SAMPLE_MAX_BYTES = 96 * 1024
GENERATION_SAMPLE_MAX_COLUMNS = 20
GENERATION_SAMPLE_VALUE_MAX_CHARS = 512
GENERATION_STATISTICS_MAX_ROWS = 1000
MAX_DISCOVERY_PAGE_FETCH = 101
_GENERATION_FIELDS_SQL = """
    SELECT a.attname AS name,
           pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
           COALESCE(base_type.typname, t.typname) AS "baseType"
    FROM pg_catalog.pg_class AS c
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_attribute AS a
      ON a.attrelid = c.oid
     AND a.attnum > 0
     AND NOT a.attisdropped
    JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
    LEFT JOIN pg_catalog.pg_type AS base_type
      ON base_type.oid = NULLIF(t.typbasetype, 0)
    WHERE n.nspname = %s
      AND c.relname = %s
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND has_schema_privilege(n.oid, 'USAGE')
      AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY a.attnum
"""


class SemanticSourceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int = HTTPStatus.BAD_REQUEST,
        code: str = "semantic.source_invalid_request",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class SourcePattern:
    alias: str
    schema: str
    relation: str

    def permits(self, alias: str, schema: str, relation: str) -> bool:
        return (
            self.alias == alias
            and self.schema == schema
            and (self.relation == "*" or self.relation == relation)
        )


def _system_schema(schema: str) -> bool:
    return schema in SYSTEM_SCHEMAS or schema.startswith("pg_")


def _internal_relation(relation: str) -> bool:
    return relation.startswith("_")


def _catalog_name(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= 63
    except UnicodeEncodeError:
        return False


def parse_allowlist(value: str) -> tuple[SourcePattern, ...]:
    if not isinstance(value, str):
        raise ValueError("SEMANTIC_SOURCE_ALLOWLIST must be a string.")
    if not value.strip():
        return ()
    patterns: list[SourcePattern] = []
    for raw in value.split(","):
        entry = raw.strip()
        match = re.fullmatch(
            r"([A-Za-z][A-Za-z0-9_-]{0,55}):"
            r"([A-Za-z_][A-Za-z0-9_]{0,62})\."
            r"(\*|[A-Za-z_][A-Za-z0-9_]{0,62})",
            entry,
        )
        if match is None:
            raise ValueError(
                "SEMANTIC_SOURCE_ALLOWLIST entries must use "
                "ALIAS:schema.relation or ALIAS:schema.*."
            )
        pattern = SourcePattern(*match.groups())
        if _system_schema(pattern.schema):
            raise ValueError(
                "SEMANTIC_SOURCE_ALLOWLIST cannot include system or "
                "managed derived schemas."
            )
        if pattern not in patterns:
            patterns.append(pattern)
    return tuple(patterns)


def parse_exclusions(value: str) -> tuple[SourcePattern, ...]:
    try:
        return parse_allowlist(value)
    except ValueError as exc:
        raise ValueError(
            str(exc).replace(
                "SEMANTIC_SOURCE_ALLOWLIST",
                "SEMANTIC_SOURCE_EXCLUSIONS",
            )
        ) from exc


def validate_source_selector(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, dict) or set(value) != {
        "alias",
        "schema",
        "relation",
    }:
        raise SemanticSourceError(
            "Source sync requires only alias, schema, and relation."
        )
    alias = value.get("alias")
    schema = value.get("schema")
    relation = value.get("relation")
    if not isinstance(alias, str) or ALIAS_RE.fullmatch(alias) is None:
        raise SemanticSourceError("Source alias is invalid.")
    if not isinstance(schema, str) or IDENTIFIER_RE.fullmatch(schema) is None:
        raise SemanticSourceError("Source schema is invalid.")
    if (
        not isinstance(relation, str)
        or IDENTIFIER_RE.fullmatch(relation) is None
    ):
        raise SemanticSourceError("Source relation is invalid.")
    if _system_schema(schema):
        raise SemanticSourceError(
            "System and managed derived schemas are not semantic sources.",
            status=HTTPStatus.FORBIDDEN,
            code="semantic.source_not_allowed",
        )
    return alias, schema, relation


def source_asset_id(alias: str, schema: str, relation: str) -> str:
    identity = f"postgresql\0{alias}\0{schema}\0{relation}"
    return str(uuid.uuid5(SOURCE_NAMESPACE, identity))


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _description(
    value: Any,
    *,
    label: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise SemanticSourceError(
            f"{label} must be text of at most {maximum} characters.",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="semantic.source_metadata_invalid",
        )
    cleaned = value.strip()
    return cleaned or None


def source_generated(relation: dict[str, Any]) -> dict[str, Any]:
    fields = [dict(field) for field in relation["fields"]]
    identity_columns = [
        field["name"]
        for field in fields
        if field.get("primaryKey") or field.get("unique")
    ]
    geometry_fields = [
        field for field in fields if field.get("geometryType")
    ]
    definition = {
        "binding": {
            "adapter": "postgresql",
            "alias": relation["alias"],
            "schema": relation["schema"],
            "relation": relation["relation"],
        },
        "kind": relation["kind"],
        "fields": fields,
    }
    if relation.get("description"):
        definition["description"] = relation["description"]
    generated = {
        "name": relation["relation"],
        "qualifiedName": f"{relation['schema']}.{relation['relation']}",
        **definition,
        "definitionDigest": _canonical_hash(definition),
    }
    if len(identity_columns) == 1:
        generated["idColumn"] = identity_columns[0]
    if geometry_fields:
        generated["geometryColumn"] = geometry_fields[0]["name"]
        generated["geometryType"] = (
            geometry_fields[0].get("geometryType") or None
        )
        generated["srid"] = geometry_fields[0].get("srid")
    return generated


class PostgresSemanticSources:
    _RELATIONS_SQL = """
        SELECT n.nspname AS schema,
               c.relname AS relation,
               c.relkind AS relation_kind
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND has_schema_privilege(n.oid, 'USAGE')
          AND has_table_privilege(c.oid, 'SELECT')
        ORDER BY n.nspname, c.relname
    """
    _FIELDS_SQL = """
        SELECT c.relkind AS relation_kind,
               pg_catalog.obj_description(c.oid, 'pg_class')
                 AS relation_description,
               a.attname AS name,
               pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
               pg_catalog.col_description(c.oid, a.attnum) AS description,
               NOT a.attnotnull AS nullable,
               CASE WHEN a.atttypid = 'geometry'::regtype
                 THEN postgis_typmod_type(a.atttypmod)
                 ELSE ''
               END AS "geometryType",
               CASE WHEN a.atttypid = 'geometry'::regtype
                 THEN postgis_typmod_srid(a.atttypmod)
                 ELSE NULL
               END AS srid,
               COALESCE(ix.is_primary, false) AS "primaryKey",
               COALESCE(ix.is_unique, false) AS "unique"
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_attribute AS a
          ON a.attrelid = c.oid
         AND a.attnum > 0
         AND NOT a.attisdropped
        LEFT JOIN (
          SELECT i.indrelid,
                 key.attnum,
                 bool_or(i.indisprimary) AS is_primary,
                 bool_or(i.indisunique AND i.indnkeyatts = 1) AS is_unique
          FROM pg_catalog.pg_index AS i
          JOIN LATERAL unnest(i.indkey) WITH ORDINALITY
            AS key(attnum, position)
            ON key.position <= i.indnkeyatts
          WHERE i.indisvalid
            AND i.indisready
            AND i.indpred IS NULL
            AND i.indexprs IS NULL
          GROUP BY i.indrelid, key.attnum
        ) AS ix ON ix.indrelid = c.oid AND ix.attnum = a.attnum
        WHERE n.nspname = %s
          AND c.relname = %s
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND has_schema_privilege(n.oid, 'USAGE')
          AND has_table_privilege(c.oid, 'SELECT')
        ORDER BY a.attnum
    """

    def __init__(
        self,
        connections: Mapping[str, str],
        allowlist: tuple[SourcePattern, ...],
        exclusions: tuple[SourcePattern, ...] = (),
    ) -> None:
        self.connections = dict(connections)
        self.allowlist = allowlist
        self.exclusions = exclusions

    def _permitted(self, alias: str, schema: str, relation: str) -> bool:
        return (
            not _system_schema(schema)
            and not _internal_relation(relation)
            and not any(
                pattern.permits(alias, schema, relation)
                for pattern in self.exclusions
            )
            and any(
                pattern.permits(alias, schema, relation)
                for pattern in self.allowlist
            )
        )

    def _aliases(self) -> list[str]:
        return sorted({
            pattern.alias
            for pattern in self.allowlist
            if pattern.alias in self.connections
        })

    def configuration_fingerprint(self, key: bytes) -> str:
        if not isinstance(key, bytes) or not key:
            raise ValueError("Semantic source fingerprint key is invalid.")
        aliases = self._aliases()
        value = {
            "connections": {
                alias: self.connections[alias]
                for alias in aliases
            },
            "allowlist": [
                [pattern.alias, pattern.schema, pattern.relation]
                for pattern in self.allowlist
            ],
            "exclusions": [
                [pattern.alias, pattern.schema, pattern.relation]
                for pattern in self.exclusions
            ],
        }
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hmac.new(key, encoded, hashlib.sha256).hexdigest()

    @staticmethod
    def _begin_read_only(cursor) -> None:
        cursor.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        cursor.execute("SET LOCAL statement_timeout = '5000ms'")
        cursor.execute("SET LOCAL lock_timeout = '2000ms'")

    def _connection_url(self, alias: str) -> str:
        url = self.connections.get(alias)
        if url is None:
            raise SemanticSourceError(
                "The configured database alias was not found.",
                status=HTTPStatus.NOT_FOUND,
                code="semantic.source_not_found",
            )
        return url

    def generation_context(
        self,
        alias: str,
        schema: str,
        relation: str,
        *,
        fields: list[dict[str, Any]],
        target_kind: str,
        field_name: str | None = None,
        sample_rows: bool = False,
        statistics: bool = False,
        sample_seed: float = 0.314159,
    ) -> dict[str, Any]:
        if not self._permitted(alias, schema, relation):
            raise SemanticSourceError(
                "The requested relation is not allowed as a semantic source.",
                status=HTTPStatus.FORBIDDEN,
                code="semantic.source_not_allowed",
            )
        return postgres_generation_context(
            self._connection_url(alias),
            schema=schema,
            relation=relation,
            fields=fields,
            target_kind=target_kind,
            field_name=field_name,
            sample_rows=sample_rows,
            statistics=statistics,
            sample_seed=sample_seed,
        )

    def discover(self) -> list[dict[str, Any]]:
        relations: list[dict[str, Any]] = []
        for alias in self._aliases():
            with psycopg.connect(
                self.connections[alias],
                connect_timeout=5,
                row_factory=dict_row,
            ) as connection:
                with connection.cursor() as cursor:
                    self._begin_read_only(cursor)
                    cursor.execute(self._RELATIONS_SQL)
                    rows = cursor.fetchall()
            for row in rows:
                schema = row["schema"]
                relation = row["relation"]
                if not self._permitted(alias, schema, relation):
                    continue
                relations.append({
                    "alias": alias,
                    "schema": schema,
                    "relation": relation,
                    "kind": RELATION_KINDS[row["relation_kind"]],
                    "assetId": source_asset_id(alias, schema, relation),
                })
        return sorted(
            relations,
            key=lambda item: (
                item["alias"],
                item["schema"],
                item["relation"],
            ),
        )

    def _paged_relations_query(
        self,
        alias: str,
        after: tuple[str, str] | None,
        fetch_limit: int,
    ) -> tuple[str, tuple[Any, ...]]:
        allowed = [
            pattern for pattern in self.allowlist
            if pattern.alias == alias
        ]
        if not allowed:
            raise ValueError("Semantic source page position is invalid.")
        values: list[Any] = []
        allow_clauses: list[str] = []
        for pattern in allowed:
            if pattern.relation == "*":
                allow_clauses.append("n.nspname = %s")
                values.append(pattern.schema)
            else:
                allow_clauses.append(
                    "(n.nspname = %s AND c.relname = %s)"
                )
                values.extend((pattern.schema, pattern.relation))

        exclusion_clauses: list[str] = []
        for pattern in self.exclusions:
            if pattern.alias != alias:
                continue
            if pattern.relation == "*":
                exclusion_clauses.append("n.nspname = %s")
                values.append(pattern.schema)
            else:
                exclusion_clauses.append(
                    "(n.nspname = %s AND c.relname = %s)"
                )
                values.extend((pattern.schema, pattern.relation))

        exclusion_sql = (
            "AND NOT (" + " OR ".join(exclusion_clauses) + ")"
            if exclusion_clauses
            else ""
        )
        after_sql = ""
        if after is not None:
            after_sql = (
                "AND (n.nspname > %s OR "
                "(n.nspname = %s AND c.relname > %s))"
            )
            values.extend((after[0], after[0], after[1]))
        values.append(fetch_limit)
        statement = f"""
            SELECT n.nspname AS schema,
                   c.relname AS relation,
                   c.relkind AS relation_kind
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND has_schema_privilege(n.oid, 'USAGE')
              AND has_table_privilege(c.oid, 'SELECT')
              AND left(c.relname, 1) <> '_'
              AND ({' OR '.join(allow_clauses)})
              {exclusion_sql}
              {after_sql}
            ORDER BY n.nspname, c.relname
            LIMIT %s
        """
        return statement, tuple(values)

    def discover_page(
        self,
        *,
        after: tuple[str, str, str] | None,
        fetch_limit: int,
    ) -> list[dict[str, Any]]:
        if (
            isinstance(fetch_limit, bool)
            or not isinstance(fetch_limit, int)
            or not 1 <= fetch_limit <= MAX_DISCOVERY_PAGE_FETCH
        ):
            raise ValueError("Semantic source page limit is invalid.")
        aliases = self._aliases()
        if after is not None and (
            not isinstance(after, tuple)
            or len(after) != 3
            or not isinstance(after[0], str)
            or ALIAS_RE.fullmatch(after[0]) is None
            or not _catalog_name(after[1])
            or not _catalog_name(after[2])
            or after[0] not in aliases
        ):
            raise ValueError("Semantic source page position is invalid.")

        relations: list[dict[str, Any]] = []
        for alias in aliases:
            if after is not None and alias < after[0]:
                continue
            remaining = fetch_limit - len(relations)
            if remaining == 0:
                break
            alias_after = (
                (after[1], after[2])
                if after is not None and alias == after[0]
                else None
            )
            statement, values = self._paged_relations_query(
                alias,
                alias_after,
                remaining,
            )
            with psycopg.connect(
                self.connections[alias],
                connect_timeout=5,
                row_factory=dict_row,
            ) as connection:
                with connection.cursor() as cursor:
                    self._begin_read_only(cursor)
                    cursor.execute(statement, values)
                    rows = cursor.fetchmany(remaining)
            relations.extend({
                "alias": alias,
                "schema": row["schema"],
                "relation": row["relation"],
                "kind": RELATION_KINDS[row["relation_kind"]],
                "assetId": source_asset_id(
                    alias,
                    row["schema"],
                    row["relation"],
                ),
            } for row in rows)
        return relations

    @contextmanager
    def locked_relation(
        self,
        alias: str,
        schema: str,
        relation: str,
    ) -> Iterator[dict[str, Any]]:
        if not self._permitted(alias, schema, relation):
            raise SemanticSourceError(
                "The requested relation is not allowed as a semantic source.",
                status=HTTPStatus.FORBIDDEN,
                code="semantic.source_not_allowed",
            )
        url = self._connection_url(alias)
        try:
            with psycopg.connect(
                url,
                connect_timeout=5,
                row_factory=dict_row,
            ) as connection:
                with connection.cursor() as cursor:
                    self._begin_read_only(cursor)
                    # Foreign tables cannot be locked ("This operation is
                    # not supported for foreign tables") — skip the lock
                    # for them. Every other relkind we support still takes
                    # it; REPEATABLE READ's own snapshot already fixes the
                    # view for the pair of queries below regardless.
                    cursor.execute(
                        "SELECT c.relkind FROM pg_catalog.pg_class AS c "
                        "JOIN pg_catalog.pg_namespace AS n "
                        "ON n.oid = c.relnamespace "
                        "WHERE n.nspname = %s AND c.relname = %s",
                        (schema, relation),
                    )
                    precheck = cursor.fetchone()
                    if precheck is None or precheck["relkind"] != "f":
                        cursor.execute(
                            sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE").format(
                                sql.Identifier(schema, relation)
                            )
                        )
                    cursor.execute(self._FIELDS_SQL, (schema, relation))
                    rows = cursor.fetchall()
                    if not rows:
                        raise SemanticSourceError(
                            "The semantic source was not found or is not selectable.",
                            status=HTTPStatus.NOT_FOUND,
                            code="semantic.source_not_found",
                        )
                    relation_kind = rows[0]["relation_kind"]
                    if (
                        relation_kind not in RELATION_KINDS
                        or any(
                            row["relation_kind"] != relation_kind
                            for row in rows
                        )
                    ):
                        raise SemanticSourceError(
                            "The semantic source changed during inspection.",
                            status=HTTPStatus.CONFLICT,
                            code="semantic.source_changed",
                        )
                    relation_description = _description(
                        rows[0]["relation_description"],
                        label="Relation description",
                        maximum=MAX_RELATION_DESCRIPTION,
                    )
                    if any(
                        _description(
                            row["relation_description"],
                            label="Relation description",
                            maximum=MAX_RELATION_DESCRIPTION,
                        )
                        != relation_description
                        for row in rows
                    ):
                        raise SemanticSourceError(
                            "The semantic source changed during inspection.",
                            status=HTTPStatus.CONFLICT,
                            code="semantic.source_changed",
                        )
                    yield {
                        "alias": alias,
                        "schema": schema,
                        "relation": relation,
                        "kind": RELATION_KINDS[relation_kind],
                        "assetId": source_asset_id(alias, schema, relation),
                        **(
                            {"description": relation_description}
                            if relation_description
                            else {}
                        ),
                        "fields": [
                            {
                                "name": row["name"],
                                "type": row["type"],
                                "nullable": row["nullable"],
                                "primaryKey": row["primaryKey"],
                                "unique": row["unique"],
                                **(
                                    {"description": field_description}
                                    if (
                                        field_description := _description(
                                            row["description"],
                                            label=(
                                                f"Description for field "
                                                f"{row['name']}"
                                            ),
                                            maximum=MAX_FIELD_DESCRIPTION,
                                        )
                                    )
                                    else {}
                                ),
                                **(
                                    {
                                        "geometryType": row["geometryType"],
                                        "srid": row["srid"],
                                    }
                                    if row["geometryType"]
                                    else {}
                                ),
                            }
                            for row in rows
                        ],
                    }
        except SemanticSourceError:
            raise
        except (
            psycopg.errors.InsufficientPrivilege,
            psycopg.errors.InvalidSchemaName,
            psycopg.errors.UndefinedTable,
        ) as exc:
            raise SemanticSourceError(
                "The semantic source was not found or is not selectable.",
                status=HTTPStatus.NOT_FOUND,
                code="semantic.source_not_found",
            ) from exc


def _generation_fields(
    fields: list[dict[str, Any]],
    *,
    target_kind: str,
    field_name: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if target_kind not in {"table", "field"}:
        raise SemanticSourceError(
            "Generation context must target a table or field.",
            code="semantic.generation_context_invalid",
        )
    usable: list[dict[str, Any]] = []
    omitted: list[str] = []
    seen: set[str] = set()
    target_found = False
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if (
            not isinstance(name, str)
            or IDENTIFIER_RE.fullmatch(name) is None
            or name in seen
        ):
            continue
        seen.add(name)
        if target_kind == "field" and name != field_name:
            continue
        if target_kind == "field":
            target_found = True
        field_type = field.get("type")
        base_type = field.get("baseType")
        normalized_field_type = (
            field_type.lower().strip()
            if isinstance(field_type, str)
            else ""
        )
        normalized_base_type = (
            base_type.lower().lstrip("_")
            if isinstance(base_type, str)
            else ""
        )
        geometry = bool(field.get("geometryType")) or (
            normalized_field_type.startswith(("geometry", "geography"))
        ) or (
            normalized_base_type in {"geometry", "geography"}
        )
        binary = (
            normalized_field_type == "bytea"
            or normalized_field_type.startswith("bytea[")
            or normalized_base_type == "bytea"
        )
        if geometry or binary:
            omitted.append(name)
            continue
        usable.append(field)

    if target_kind == "field" and (
        not isinstance(field_name, str)
        or IDENTIFIER_RE.fullmatch(field_name) is None
        or not target_found
    ):
        raise SemanticSourceError(
            "The selected field is unavailable for generation context.",
            status=HTTPStatus.NOT_FOUND,
            code="semantic.field_not_found",
        )
    if target_kind == "table" and len(usable) > GENERATION_SAMPLE_MAX_COLUMNS:
        omitted.extend(
            field["name"]
            for field in usable[GENERATION_SAMPLE_MAX_COLUMNS:]
        )
        usable = usable[:GENERATION_SAMPLE_MAX_COLUMNS]
    return usable, omitted


def _verified_live_fields(
    cursor,
    *,
    schema: str,
    relation: str,
    expected_fields: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected: list[tuple[str, str]] = []
    for field in expected_fields:
        if (
            not isinstance(field, dict)
            or not isinstance(field.get("name"), str)
            or IDENTIFIER_RE.fullmatch(field["name"]) is None
            or not isinstance(field.get("type"), str)
            or not field["type"]
        ):
            raise SemanticSourceError(
                "Generated semantic fields are invalid.",
                status=HTTPStatus.CONFLICT,
                code="semantic.generation_context_stale",
            )
        expected.append((field["name"], field["type"]))
    if len({name for name, _ in expected}) != len(expected):
        raise SemanticSourceError(
            "Generated semantic fields are invalid.",
            status=HTTPStatus.CONFLICT,
            code="semantic.generation_context_stale",
        )
    cursor.execute(_GENERATION_FIELDS_SQL, (schema, relation))
    rows = cursor.fetchall()
    live = [
        (row.get("name"), row.get("type"))
        for row in rows
        if isinstance(row, Mapping)
    ]
    if not rows or live != expected:
        raise SemanticSourceError(
            "The database schema changed after the semantic profile was generated. "
            "Synchronize or repair the profile before sending data context.",
            status=HTTPStatus.CONFLICT,
            code="semantic.generation_context_stale",
        )
    return [dict(row) for row in rows]


def _bounded_sample_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    bounded: list[dict[str, Any]] = []
    for row in rows[:GENERATION_SAMPLE_MAX_ROWS]:
        candidate = [*bounded, dict(row)]
        encoded = json.dumps(
            candidate,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > GENERATION_SAMPLE_MAX_BYTES:
            break
        bounded.append(dict(row))
    return bounded


def _estimated_row_count(row: Any) -> int | None:
    if not isinstance(row, dict):
        return None
    plan = row.get("QUERY PLAN")
    if (
        isinstance(plan, list)
        and plan
        and isinstance(plan[0], dict)
        and isinstance(plan[0].get("Plan"), dict)
    ):
        estimate = plan[0]["Plan"].get("Plan Rows")
        if (
            isinstance(estimate, (int, float))
            and not isinstance(estimate, bool)
            and estimate >= 0
        ):
            return int(estimate)
    return None


def postgres_generation_context(
    connection_url: str,
    *,
    schema: str,
    relation: str,
    fields: list[dict[str, Any]],
    target_kind: str,
    field_name: str | None = None,
    sample_rows: bool = False,
    statistics: bool = False,
    sample_seed: float = 0.314159,
) -> dict[str, Any]:
    """Read explicitly requested, bounded context for Gemini generation.

    Authorization is the caller's responsibility. Identifiers and selected
    fields are nevertheless closed over the generated semantic profile so no
    user-provided SQL or arbitrary column selection reaches PostgreSQL.
    """
    if (
        not isinstance(schema, str)
        or IDENTIFIER_RE.fullmatch(schema) is None
        or not isinstance(relation, str)
        or IDENTIFIER_RE.fullmatch(relation) is None
        or _internal_relation(relation)
        or not isinstance(sample_rows, bool)
        or not isinstance(statistics, bool)
        or isinstance(sample_seed, bool)
        or not isinstance(sample_seed, (int, float))
        or not -1 <= sample_seed <= 1
    ):
        raise SemanticSourceError(
            "Generation context request is invalid.",
            code="semantic.generation_context_invalid",
        )
    if not sample_rows and not statistics:
        return {}
    relation_identifier = sql.Identifier(schema, relation)
    context: dict[str, Any] = {}
    try:
        with psycopg.connect(
            connection_url,
            connect_timeout=5,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                PostgresSemanticSources._begin_read_only(cursor)
                cursor.execute(
                    sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE").format(
                        relation_identifier
                    )
                )
                live_fields = _verified_live_fields(
                    cursor,
                    schema=schema,
                    relation=relation,
                    expected_fields=fields,
                )
                selected_fields, omitted_columns = _generation_fields(
                    live_fields,
                    target_kind=target_kind,
                    field_name=field_name,
                )
                if sample_rows:
                    if selected_fields:
                        cursor.execute("SELECT setseed(%s)", (sample_seed,))
                        projections = [
                            sql.SQL("left({}::text, {}) AS {}").format(
                                sql.Identifier(field["name"]),
                                sql.Literal(
                                    GENERATION_SAMPLE_VALUE_MAX_CHARS
                                ),
                                sql.Identifier(field["name"]),
                            )
                            for field in selected_fields
                        ]
                        cursor.execute(
                            sql.SQL(
                                "SELECT {} FROM {} "
                                "WHERE random() < 0.05 LIMIT {}"
                            ).format(
                                sql.SQL(", ").join(projections),
                                relation_identifier,
                                sql.Literal(GENERATION_SAMPLE_MAX_ROWS),
                            )
                        )
                        selected_rows = cursor.fetchall()
                        rows = _bounded_sample_rows(selected_rows)
                        truncated = (
                            len(rows) < len(selected_rows)
                            or len(selected_rows)
                            >= GENERATION_SAMPLE_MAX_ROWS
                        )
                    else:
                        rows = []
                        truncated = False
                    context["sampleRows"] = {
                        "percent": GENERATION_SAMPLE_PERCENT,
                        "maxRows": GENERATION_SAMPLE_MAX_ROWS,
                        "maxBytes": GENERATION_SAMPLE_MAX_BYTES,
                        "columns": [
                            field["name"] for field in selected_fields
                        ],
                        "omittedColumns": omitted_columns,
                        "returnedRows": len(rows),
                        "truncated": truncated,
                        "rows": rows,
                    }

                if statistics and target_kind == "table":
                    cursor.execute(
                        sql.SQL(
                            "EXPLAIN (FORMAT JSON) SELECT 1 FROM {}"
                        ).format(relation_identifier)
                    )
                    estimate = _estimated_row_count(cursor.fetchone())
                    context["statistics"] = {
                        "scope": "table",
                        "estimatedRowCount": estimate,
                        "columnCount": len(live_fields),
                        "nonGeometryColumnCount": sum(
                            1
                            for field in live_fields
                            if not (
                                str(field.get("type", "")).lower().startswith(
                                    ("geometry", "geography")
                                )
                                or str(
                                    field.get("baseType", "")
                                ).lower().lstrip("_")
                                in {"geometry", "geography"}
                            )
                        ),
                        "sampledColumnCount": len(selected_fields),
                        "omittedColumns": omitted_columns,
                    }
                elif statistics:
                    if not selected_fields:
                        context["statistics"] = {
                            "scope": "field",
                            "field": field_name,
                            "available": False,
                            "reason": "unsupported-column-type",
                        }
                    else:
                        selected_name = selected_fields[0]["name"]
                        cursor.execute("SELECT setseed(%s)", (sample_seed,))
                        cursor.execute(
                            sql.SQL(
                                "WITH sampled AS ("
                                " SELECT {}::text AS value"
                                " FROM {}"
                                " WHERE random() < 0.05"
                                " LIMIT {}"
                                ")"
                                " SELECT count(*)::integer AS \"sampledRows\","
                                " count(value)::integer AS \"nonNullCount\","
                                " count(DISTINCT value)::integer"
                                " AS \"distinctCount\","
                                " min(length(value))::integer"
                                " AS \"minimumLength\","
                                " max(length(value))::integer"
                                " AS \"maximumLength\","
                                " avg(length(value))::double precision"
                                " AS \"averageLength\""
                                " FROM sampled"
                            ).format(
                                sql.Identifier(selected_name),
                                relation_identifier,
                                sql.Literal(GENERATION_STATISTICS_MAX_ROWS),
                            )
                        )
                        row = cursor.fetchone()
                        values = (
                            dict(row) if isinstance(row, Mapping) else {}
                        )
                        sampled = values.get("sampledRows")
                        non_null = values.get("nonNullCount")
                        values["scope"] = "field"
                        values["field"] = selected_name
                        values["samplePercent"] = GENERATION_SAMPLE_PERCENT
                        values["maxSampledRows"] = (
                            GENERATION_STATISTICS_MAX_ROWS
                        )
                        values["nullCount"] = (
                            sampled - non_null
                            if (
                                isinstance(sampled, int)
                                and not isinstance(sampled, bool)
                                and isinstance(non_null, int)
                                and not isinstance(non_null, bool)
                            )
                            else None
                        )
                        context["statistics"] = values
    except SemanticSourceError:
        raise
    except psycopg.Error as exc:
        raise SemanticSourceError(
            "Optional generation context could not be read.",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="semantic.generation_context_unavailable",
        ) from exc
    return context
