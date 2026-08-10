from __future__ import annotations

import hashlib
import json
import re
import unittest
from unittest.mock import patch

import psycopg

from semantic_sources import (
    GENERATION_SAMPLE_MAX_BYTES,
    GENERATION_SAMPLE_MAX_ROWS,
    MAX_FIELD_DESCRIPTION,
    PostgresSemanticSources,
    SemanticSourceError,
    parse_allowlist,
    parse_exclusions,
    postgres_generation_context,
    source_asset_id,
    source_generated,
    validate_source_selector,
)


class FakeCursor:
    def __init__(
        self,
        rows,
        *,
        one=None,
        execute_error=None,
        fetchall_results=None,
    ):
        self.rows = rows
        self.one = one
        self.execute_error = execute_error
        self.fetchall_results = list(fetchall_results or [])
        self.executed = []
        self.fetchall_calls = 0
        self.fetchmany_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        rendered = (
            query.as_string()
            if hasattr(query, "as_string")
            else str(query)
        )
        self.executed.append((rendered, params))
        if self.execute_error and rendered.startswith("LOCK TABLE"):
            raise self.execute_error

    def fetchall(self):
        self.fetchall_calls += 1
        if self.fetchall_results:
            return self.fetchall_results.pop(0)
        return self.rows

    def fetchmany(self, size):
        self.fetchmany_sizes.append(size)
        return self.rows[:size]

    def fetchone(self):
        return self.one


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class SemanticSourceContractTests(unittest.TestCase):
    def test_allowlist_is_exact_and_excludes_private_schemas(self):
        patterns = parse_allowlist(
            "MAPP:leeds.*, REPORTING:published.census"
        )
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://mapp", "REPORTING": "postgresql://report"},
            patterns,
            parse_exclusions("MAPP:leeds.census_datasets"),
        )
        self.assertTrue(sources._permitted("MAPP", "leeds", "roads"))
        self.assertTrue(
            sources._permitted("REPORTING", "published", "census")
        )
        for selector in (
            ("mapp", "leeds", "roads"),
            ("MAPP", "Leeds", "roads"),
            ("REPORTING", "published", "other"),
            ("MAPP", "derived_layers", "roads"),
            ("MAPP", "pg_catalog", "pg_class"),
            ("MAPP", "leeds", "_etl_runs"),
            ("MAPP", "leeds", "_census_etl_runs"),
        ):
            with self.subTest(selector=selector):
                self.assertFalse(sources._permitted(*selector))

        for invalid in (
            "MAPP:derived_layers.*",
            "MAPP:pg_catalog.*",
            "MAPP:leeds",
            "MAPP:leeds.roads.extra",
            "MAPP:leeds.roads,,OTHER:ok.*",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    parse_allowlist(invalid)
        self.assertEqual((), parse_allowlist(""))
        self.assertEqual(
            parse_allowlist("MAPP:leeds.census_datasets"),
            parse_exclusions("MAPP:leeds.census_datasets"),
        )

    def test_selector_is_closed_and_uses_strict_identifiers(self):
        self.assertEqual(
            ("MAPP", "leeds", "census_2021_england_oa"),
            validate_source_selector({
                "alias": "MAPP",
                "schema": "leeds",
                "relation": "census_2021_england_oa",
            }),
        )
        for invalid in (
            {
                "alias": "MAPP",
                "schema": "leeds",
                "relation": "roads",
                "unexpected": True,
            },
            {"alias": "MAPP", "schema": "leeds", "relation": "roads; DROP"},
            {"alias": "MAPP", "schema": "derived_layers", "relation": "roads"},
            {"alias": "mapp/other", "schema": "leeds", "relation": "roads"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SemanticSourceError):
                    validate_source_selector(invalid)

    def test_asset_identity_is_deterministic_and_alias_sensitive(self):
        first = source_asset_id("MAPP", "leeds", "roads")
        self.assertEqual(first, source_asset_id("MAPP", "leeds", "roads"))
        self.assertNotEqual(first, source_asset_id("OTHER", "leeds", "roads"))
        self.assertNotEqual(first, source_asset_id("MAPP", "leeds", "paths"))

    def test_discovery_returns_only_allowlisted_selectable_relations(self):
        cursor = FakeCursor([
            {"schema": "leeds", "relation": "roads", "relation_kind": "r"},
            {"schema": "leeds", "relation": "_etl_runs", "relation_kind": "r"},
            {"schema": "leeds", "relation": "census_datasets", "relation_kind": "r"},
            {"schema": "leeds", "relation": "paths", "relation_kind": "v"},
            {"schema": "secret", "relation": "people", "relation_kind": "r"},
            {
                "schema": "derived_layers",
                "relation": "managed",
                "relation_kind": "v",
            },
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
            parse_exclusions("MAPP:leeds.census_datasets"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            discovered = sources.discover()

        self.assertEqual(
            [
                ("leeds", "paths", "view"),
                ("leeds", "roads", "table"),
            ],
            [
                (item["schema"], item["relation"], item["kind"])
                for item in discovered
            ],
        )
        sql_text = "\n".join(query for query, _ in cursor.executed)
        self.assertIn("pg_catalog.pg_class", sql_text)
        self.assertNotIn("secret.people", sql_text)
        self.assertNotIn("SELECT *", sql_text.upper())

    def test_discovery_admits_a_foreign_table(self):
        cursor = FakeCursor([
            {"schema": "leeds", "relation": "roads", "relation_kind": "r"},
            {"schema": "leeds", "relation": "bus_stops", "relation_kind": "f"},
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            discovered = sources.discover()

        self.assertEqual(
            [
                ("leeds", "bus_stops", "foreign-table"),
                ("leeds", "roads", "table"),
            ],
            [
                (item["schema"], item["relation"], item["kind"])
                for item in discovered
            ],
        )

    def test_bounded_discovery_filters_before_keyset_limit(self):
        cursor = FakeCursor([
            {"schema": "leeds", "relation": "roads", "relation_kind": "r"},
            {"schema": "leeds", "relation": "transit", "relation_kind": "v"},
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
            parse_exclusions("MAPP:leeds.census_datasets"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            discovered = sources.discover_page(
                after=("MAPP", "leeds", "paths"),
                fetch_limit=2,
            )

        self.assertEqual(
            [("MAPP", "leeds", "roads"), ("MAPP", "leeds", "transit")],
            [
                (item["alias"], item["schema"], item["relation"])
                for item in discovered
            ],
        )
        self.assertEqual(0, cursor.fetchall_calls)
        self.assertEqual([2], cursor.fetchmany_sizes)
        statement, values = cursor.executed[-1]
        self.assertIn("left(c.relname, 1) <> '_'", statement)
        self.assertIn("has_schema_privilege", statement)
        self.assertIn("has_table_privilege", statement)
        self.assertIn("AND NOT (", statement)
        self.assertIn("n.nspname > %s", statement)
        self.assertIn("ORDER BY n.nspname, c.relname", statement)
        self.assertIn("LIMIT %s", statement)
        self.assertEqual(
            (
                "leeds",
                "leeds",
                "census_datasets",
                "leeds",
                "leeds",
                "paths",
                2,
            ),
            values,
        )

    def test_bounded_discovery_merges_aliases_with_one_global_limit(self):
        alpha = FakeCursor([
            {"schema": "open", "relation": "alpha", "relation_kind": "r"},
        ])
        mapp = FakeCursor([
            {"schema": "leeds", "relation": "roads", "relation_kind": "r"},
            {"schema": "leeds", "relation": "transit", "relation_kind": "v"},
        ])
        sources = PostgresSemanticSources(
            {
                "ALPHA": "postgresql://alpha",
                "MAPP": "postgresql://mapp",
            },
            parse_allowlist("MAPP:leeds.*,ALPHA:open.*"),
        )

        def connect(url, **_kwargs):
            return FakeConnection(
                alpha if url == "postgresql://alpha" else mapp
            )

        with patch("semantic_sources.psycopg.connect", side_effect=connect):
            discovered = sources.discover_page(after=None, fetch_limit=2)

        self.assertEqual(
            [("ALPHA", "alpha"), ("MAPP", "roads")],
            [(item["alias"], item["relation"]) for item in discovered],
        )
        self.assertEqual([2], alpha.fetchmany_sizes)
        self.assertEqual([1], mapp.fetchmany_sizes)
        self.assertEqual(0, alpha.fetchall_calls + mapp.fetchall_calls)

    def test_bounded_discovery_resumes_after_quoted_identifier(self):
        cursor = FakeCursor([
            {"schema": "leeds", "relation": "roads", "relation_kind": "r"},
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            discovered = sources.discover_page(
                after=("MAPP", "leeds", "2024 roads"),
                fetch_limit=1,
            )

        self.assertEqual("roads", discovered[0]["relation"])
        self.assertEqual(
            ("leeds", "leeds", "leeds", "2024 roads", 1),
            cursor.executed[-1][1],
        )

    def test_source_configuration_fingerprint_tracks_effective_inputs(self):
        key = b"k" * 32
        base = PostgresSemanticSources(
            {"MAPP": "postgresql://reader:secret@database/mapp"},
            parse_allowlist("MAPP:leeds.*"),
        )
        self.assertEqual(
            base.configuration_fingerprint(key),
            base.configuration_fingerprint(key),
        )
        self.assertNotEqual(
            base.configuration_fingerprint(key),
            base.configuration_fingerprint(b"q" * 32),
        )
        self.assertNotEqual(
            base.configuration_fingerprint(key),
            PostgresSemanticSources(
                {"MAPP": "postgresql://different"},
                parse_allowlist("MAPP:leeds.*"),
            ).configuration_fingerprint(key),
        )
        self.assertNotEqual(
            base.configuration_fingerprint(key),
            PostgresSemanticSources(
                {"MAPP": "postgresql://reader:secret@database/mapp"},
                parse_allowlist("MAPP:leeds.*"),
                parse_exclusions("MAPP:leeds.roads"),
            ).configuration_fingerprint(key),
        )
        fingerprint = base.configuration_fingerprint(key)
        connection_url = "postgresql://reader:secret@database/mapp"
        self.assertNotIn("secret", fingerprint)
        self.assertNotEqual(
            hashlib.sha256(connection_url.encode("utf-8")).hexdigest(),
            fingerprint,
        )

    def test_sync_introspection_is_read_only_metadata_and_bounds_comments(self):
        cursor = FakeCursor([
            {
                "relation_kind": "r",
                "relation_description": "Official Census OA variables.",
                "name": "oa21cd",
                "type": "text",
                "description": "2021 Output Area code",
                "nullable": False,
                "geometryType": "",
                "srid": None,
                "primaryKey": True,
                "unique": True,
            },
            {
                "relation_kind": "r",
                "relation_description": "Official Census OA variables.",
                "name": "geom",
                "type": "geometry(MultiPolygon,4326)",
                "description": None,
                "nullable": False,
                "geometryType": "MULTIPOLYGON",
                "srid": 4326,
                "primaryKey": False,
                "unique": False,
            },
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            with sources.locked_relation(
                "MAPP",
                "leeds",
                "census_2021_england_oa",
            ) as relation:
                generated = source_generated(relation)

        self.assertEqual(
            "Official Census OA variables.",
            generated["description"],
        )
        self.assertEqual(
            "2021 Output Area code",
            generated["fields"][0]["description"],
        )
        self.assertNotIn("geometryType", generated["fields"][0])
        self.assertNotIn("srid", generated["fields"][0])
        self.assertEqual("MULTIPOLYGON", generated["fields"][1]["geometryType"])
        sql_text = "\n".join(query for query, _ in cursor.executed)
        self.assertIn(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
            sql_text,
        )
        self.assertIn(
            'LOCK TABLE "leeds"."census_2021_england_oa" IN ACCESS SHARE MODE',
            sql_text,
        )
        self.assertIn("pg_catalog.pg_attribute", sql_text)
        self.assertIn("obj_description", sql_text)
        self.assertIn("col_description", sql_text)
        self.assertIn("i.indisvalid", sql_text)
        self.assertIn("i.indpred IS NULL", sql_text)
        self.assertIn("WITH ORDINALITY", sql_text)
        self.assertIn("key.position <= i.indnkeyatts", sql_text)
        self.assertNotIn("pg_attrdef", sql_text)

        cursor.rows[0]["description"] = "x" * (MAX_FIELD_DESCRIPTION + 1)
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            with self.assertRaises(SemanticSourceError) as error:
                with sources.locked_relation(
                    "MAPP",
                    "leeds",
                    "census_2021_england_oa",
                ):
                    pass
        self.assertEqual("semantic.source_metadata_invalid", error.exception.code)

    def test_locked_relation_admits_a_foreign_table(self):
        cursor = FakeCursor([
            {
                "relation_kind": "f",
                "relation_description": None,
                "name": "id",
                "type": "bigint",
                "description": None,
                "nullable": False,
                "geometryType": "",
                "srid": None,
                "primaryKey": False,
                "unique": False,
            },
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            with sources.locked_relation(
                "MAPP", "leeds", "bus_stops"
            ) as relation:
                self.assertEqual("foreign-table", relation["kind"])

    def test_privilege_loss_and_missing_alias_fail_closed(self):
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
        )
        cursor = FakeCursor(
            [],
            execute_error=psycopg.errors.InsufficientPrivilege(),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            with self.assertRaises(SemanticSourceError) as error:
                with sources.locked_relation("MAPP", "leeds", "roads"):
                    pass
        self.assertEqual("semantic.source_not_found", error.exception.code)

        missing = PostgresSemanticSources(
            {},
            parse_allowlist("MAPP:leeds.*"),
        )
        with self.assertRaises(SemanticSourceError) as error:
            with missing.locked_relation("MAPP", "leeds", "roads"):
                pass
        self.assertEqual("semantic.source_not_found", error.exception.code)

    def test_changed_relation_kind_fails_closed(self):
        cursor = FakeCursor([
            {
                "relation_kind": "r",
                "relation_description": None,
                "name": "id",
                "type": "bigint",
                "description": None,
                "nullable": False,
                "geometryType": "",
                "srid": None,
                "primaryKey": True,
                "unique": True,
            },
            {
                "relation_kind": "v",
                "relation_description": None,
                "name": "label",
                "type": "text",
                "description": None,
                "nullable": True,
                "geometryType": "",
                "srid": None,
                "primaryKey": False,
                "unique": False,
            },
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            with self.assertRaises(SemanticSourceError) as error:
                with sources.locked_relation("MAPP", "leeds", "roads"):
                    pass
        self.assertEqual("semantic.source_changed", error.exception.code)

    def test_generation_context_samples_five_percent_with_hard_caps(self):
        live_fields = [
            {"name": "id", "type": "bigint", "baseType": "int8"},
            {"name": "label", "type": "text", "baseType": "text"},
            {
                "name": "geom",
                "type": "geometry(MultiPolygon,3857)",
                "baseType": "geometry",
            },
            {"name": "payload", "type": "bytea", "baseType": "bytea"},
        ]
        sample_rows = [
            {"id": "1", "label": "City centre"},
            {"id": "2", "label": "Outer ring road"},
        ]
        cursor = FakeCursor(
            [],
            fetchall_results=[live_fields, sample_rows],
            one={"QUERY PLAN": [{"Plan": {"Plan Rows": 178605}}]},
        )
        fields = [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "label", "type": "text", "nullable": True},
            {
                "name": "geom",
                "type": "geometry(MultiPolygon,3857)",
                "nullable": False,
                "geometryType": "MULTIPOLYGON",
            },
            {"name": "payload", "type": "bytea", "nullable": True},
        ]

        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ) as connect:
            context = postgres_generation_context(
                "postgresql://runtime-reader",
                schema="derived_layers",
                relation="arrivals_1951_1960_oa",
                fields=fields,
                target_kind="table",
                sample_rows=True,
                statistics=True,
            )

        sample = context["sampleRows"]
        self.assertEqual(5, sample["percent"])
        self.assertEqual(GENERATION_SAMPLE_MAX_ROWS, sample["maxRows"])
        self.assertEqual(GENERATION_SAMPLE_MAX_BYTES, sample["maxBytes"])
        self.assertEqual(["id", "label"], sample["columns"])
        self.assertEqual(2, sample["returnedRows"])
        self.assertEqual(["geom", "payload"], sample["omittedColumns"])
        self.assertEqual(178605, context["statistics"]["estimatedRowCount"])
        self.assertEqual(4, context["statistics"]["columnCount"])
        self.assertEqual(3, context["statistics"]["nonGeometryColumnCount"])
        self.assertEqual(2, context["statistics"]["sampledColumnCount"])
        sql_text = "\n".join(query for query, _ in cursor.executed)
        self.assertIn("pg_catalog.pg_attribute", sql_text)
        self.assertIn("random() < 0.05", sql_text)
        self.assertIn(f"LIMIT {GENERATION_SAMPLE_MAX_ROWS}", sql_text)
        self.assertIn("EXPLAIN (FORMAT JSON)", sql_text)
        self.assertNotIn('\"geom\"::text', sql_text)
        self.assertNotIn('\"payload\"::text', sql_text)
        self.assertIn("SET TRANSACTION", sql_text)
        self.assertNotIn("SET LOCAL ROLE", sql_text)
        self.assertIn("LOCK TABLE", sql_text)
        self.assertEqual("postgresql://runtime-reader", connect.call_args.args[0])

    def test_generation_field_statistics_are_bounded_and_value_free(self):
        cursor = FakeCursor(
            [],
            fetchall_results=[[
                {"name": "label", "type": "text", "baseType": "text"},
                {"name": "other", "type": "text", "baseType": "text"},
            ]],
            one={
                "sampledRows": 850,
                "nonNullCount": 830,
                "distinctCount": 27,
                "minimumLength": 2,
                "maximumLength": 45,
                "averageLength": 13.25,
            },
        )
        fields = [
            {"name": "label", "type": "text", "nullable": True},
            {"name": "other", "type": "text", "nullable": True},
        ]

        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            context = postgres_generation_context(
                "postgresql://reader",
                schema="leeds",
                relation="roads",
                fields=fields,
                target_kind="field",
                field_name="label",
                sample_rows=False,
                statistics=True,
            )

        statistics = context["statistics"]
        self.assertEqual("field", statistics["scope"])
        self.assertEqual("label", statistics["field"])
        self.assertEqual(850, statistics["sampledRows"])
        self.assertEqual(20, statistics["nullCount"])
        self.assertEqual(27, statistics["distinctCount"])
        self.assertNotIn("rows", context)
        sql_text = "\n".join(query for query, _ in cursor.executed)
        self.assertIn("random() < 0.05", sql_text)
        self.assertIn("LIMIT 1000", sql_text)
        self.assertIn('\"label\"', sql_text)
        self.assertNotIn('\"other\"', sql_text)
        self.assertNotIn("left(", sql_text.lower())

    def test_generation_sample_enforces_utf8_payload_cap(self):
        rows = [{"label": "é" * 512} for _ in range(100)]
        cursor = FakeCursor(
            [],
            fetchall_results=[
                [{"name": "label", "type": "text", "baseType": "text"}],
                rows,
            ],
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            context = postgres_generation_context(
                "postgresql://reader",
                schema="leeds",
                relation="roads",
                fields=[{
                    "name": "label",
                    "type": "text",
                    "nullable": True,
                }],
                target_kind="table",
                sample_rows=True,
            )

        sample = context["sampleRows"]
        encoded = json.dumps(
            sample["rows"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertLessEqual(len(encoded), GENERATION_SAMPLE_MAX_BYTES)
        self.assertLess(sample["returnedRows"], 100)
        self.assertTrue(sample["truncated"])

    def test_generation_context_rejects_a_stale_live_schema_before_sampling(
        self,
    ):
        cursor = FakeCursor(
            [],
            fetchall_results=[[
                {
                    "name": "label",
                    "type": "geometry(Point,4326)",
                    "baseType": "geometry",
                },
            ]],
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            with self.assertRaises(SemanticSourceError) as error:
                postgres_generation_context(
                    "postgresql://reader",
                    schema="leeds",
                    relation="roads",
                    fields=[{
                        "name": "label",
                        "type": "text",
                        "nullable": True,
                    }],
                    target_kind="table",
                    sample_rows=True,
                )

        self.assertEqual(
            "semantic.generation_context_stale",
            error.exception.code,
        )
        sql_text = "\n".join(query for query, _ in cursor.executed)
        self.assertIn("pg_catalog.pg_attribute", sql_text)
        self.assertNotIn("random() < 0.05", sql_text)


WRITE_STATEMENT_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|GRANT|"
    r"REVOKE|COPY|CALL|DO)\b",
    re.IGNORECASE,
)


class NoWriteContractTests(unittest.TestCase):
    """Discovery and observation must never mutate a source database.

    _begin_read_only() sets a genuinely read-only transaction — PostgreSQL
    itself rejects any write statement inside it — but that server-side
    guarantee is only as good as never regressing to a code path that skips
    it. These tests pin both halves: every statement actually issued is
    read-only, and the read-only transaction is established before anything
    else runs.
    """

    def assert_no_write_statements(self, cursor):
        self.assertTrue(cursor.executed, "expected at least one statement")
        self.assertTrue(
            cursor.executed[0][0].startswith(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            ),
            "read-only transaction must be established first",
        )
        for statement, _ in cursor.executed:
            self.assertIsNone(
                WRITE_STATEMENT_RE.match(statement),
                f"unexpected write statement: {statement!r}",
            )

    def test_discover_issues_no_write_statements(self):
        cursor = FakeCursor([
            {"schema": "leeds", "relation": "roads", "relation_kind": "r"},
            {"schema": "leeds", "relation": "bus_stops", "relation_kind": "f"},
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            sources.discover()
        self.assert_no_write_statements(cursor)

    def test_discover_page_issues_no_write_statements(self):
        cursor = FakeCursor([
            {"schema": "leeds", "relation": "roads", "relation_kind": "r"},
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            sources.discover_page(after=None, fetch_limit=10)
        self.assert_no_write_statements(cursor)

    def test_locked_relation_issues_no_write_statements(self):
        cursor = FakeCursor([
            {
                "relation_kind": "f",
                "relation_description": None,
                "name": "id",
                "type": "bigint",
                "description": None,
                "nullable": False,
                "geometryType": "",
                "srid": None,
                "primaryKey": False,
                "unique": False,
            },
        ])
        sources = PostgresSemanticSources(
            {"MAPP": "postgresql://reader"},
            parse_allowlist("MAPP:leeds.*"),
        )
        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            with sources.locked_relation("MAPP", "leeds", "bus_stops"):
                pass
        self.assert_no_write_statements(cursor)
        # LOCK TABLE IN ACCESS SHARE MODE is a local catalog lock preventing
        # concurrent DDL during inspection, not a data mutation — PostgreSQL
        # itself would reject anything stronger inside the read-only
        # transaction established immediately above.
        self.assertIn(
            "LOCK TABLE",
            "\n".join(query for query, _ in cursor.executed),
        )

    def test_generation_context_sampling_issues_no_write_statements(self):
        live_fields = [
            {"name": "id", "type": "bigint", "baseType": "int8"},
            {"name": "label", "type": "text", "baseType": "text"},
        ]
        sample_rows = [{"id": "1", "label": "City centre"}]
        cursor = FakeCursor(
            [],
            fetchall_results=[live_fields, sample_rows],
            one={"QUERY PLAN": [{"Plan": {"Plan Rows": 178605}}]},
        )
        fields = [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "label", "type": "text", "nullable": True},
        ]

        with patch(
            "semantic_sources.psycopg.connect",
            return_value=FakeConnection(cursor),
        ):
            postgres_generation_context(
                "postgresql://runtime-reader",
                schema="derived_layers",
                relation="arrivals",
                fields=fields,
                target_kind="table",
                sample_rows=True,
                statistics=True,
            )
        self.assert_no_write_statements(cursor)
