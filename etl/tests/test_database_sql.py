from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from typing import Any

from leeds_arcgis_etl.config import load_config
from leeds_arcgis_etl.core import PreparedFeature

try:
    from leeds_arcgis_etl.database import DatabaseError, PostgresStore
except ModuleNotFoundError:  # The production image installs psycopg.
    DatabaseError = None  # type: ignore[assignment,misc]
    PostgresStore = None  # type: ignore[assignment,misc]


ROOT = Path(__file__).resolve().parents[1]


class FakeCursor:
    def __init__(self, statements: list[tuple[str, Any]]) -> None:
        self.statements = statements
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def execute(self, statement: Any, params: Any = None) -> None:
        rendered = statement if isinstance(statement, str) else statement.as_string()
        self.statements.append((rendered, params))

    def executemany(self, statement: Any, rows: Any) -> None:
        rendered = statement.as_string()
        materialized = list(rows)
        self.statements.append((rendered, materialized))

    def fetchone(self) -> tuple[str]:
        return (True,)


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.statements)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


@unittest.skipIf(PostgresStore is None, "psycopg is installed in the ETL image")
class DatabaseSQLTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "config" / "layers.json")
        self.connection = FakeConnection()
        self.store = PostgresStore(self.connection, self.config)  # type: ignore[misc,arg-type]

    def test_ddl_has_generated_web_mercator_geometry_and_indexes(self) -> None:
        self.store.initialize()
        ddl = "\n".join(statement for statement, _ in self.connection.statements)
        self.assertIn("geom geometry(Point, 4326)", ddl)
        self.assertIn("GENERATED ALWAYS AS (ST_Transform(geom, 3857)) STORED", ddl)
        self.assertIn("USING gist (geom_3857)", ddl)

    def test_upsert_placeholder_count_matches_row(self) -> None:
        layer = self.config.layers[1]
        feature = PreparedFeature(
            object_id=1,
            values=("path", "surface", 10.0, 0.01, 1.0, 3.2, "FP", "LEEDS"),
            source_attributes={"OBJECTID": 1},
            geometry={
                "type": "LineString",
                "coordinates": [[-1.5, 53.8], [-1.4, 53.9]],
            },
            source_hash="a" * 64,
        )
        self.store.upsert_page(layer, [feature], uuid.uuid4())
        statement, rows = self.connection.statements[-1]
        self.assertEqual(statement.count("%s"), len(rows[0]))
        self.assertIn("ST_Multi", statement)
        self.assertNotIn('geom_3857") VALUES', statement)

    def test_layer_lock_uses_session_advisory_lock(self) -> None:
        layer = self.config.layers[0]
        self.assertTrue(self.store.acquire_layer_lock(layer))
        statement, params = self.connection.statements[-1]
        self.assertIn("pg_try_advisory_lock", statement)
        self.assertEqual(params, ("mapp-explore-etl:leeds.bus_stops",))

        self.store.release_layer_lock(layer)
        statement, params = self.connection.statements[-1]
        self.assertIn("pg_advisory_unlock", statement)
        self.assertEqual(params, ("mapp-explore-etl:leeds.bus_stops",))

    def test_finish_run_analyzes_target_before_marking_success(self) -> None:
        layer = self.config.layers[0]

        self.store.finish_run(
            layer,
            uuid.uuid4(),
            rows_seen=10,
            rows_deleted=1,
            ending_count=10,
        )

        statements = [statement for statement, _ in self.connection.statements]
        analyze_index = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith("ANALYZE")
        )
        success_index = next(
            index
            for index, statement in enumerate(statements)
            if "SET status = 'succeeded'" in statement
        )
        self.assertEqual(
            statements[analyze_index],
            'ANALYZE "leeds"."bus_stops"',
        )
        self.assertLess(analyze_index, success_index)


class ScriptedFakeCursor:
    def __init__(self, statements: list[tuple[str, Any]], fetchone_results: list) -> None:
        self.statements = statements
        self.fetchone_results = fetchone_results

    def __enter__(self) -> "ScriptedFakeCursor":
        return self

    def __exit__(self, *_args: Any) -> None:
        return False

    def execute(self, statement: Any, params: Any = None) -> None:
        rendered = statement if isinstance(statement, str) else statement.as_string()
        self.statements.append((rendered, params))

    def fetchone(self) -> Any:
        return self.fetchone_results.pop(0)


class ScriptedFakeConnection:
    def __init__(self, fetchone_results: list) -> None:
        self.statements: list[tuple[str, Any]] = []
        self._fetchone_results = fetchone_results
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> ScriptedFakeCursor:
        return ScriptedFakeCursor(self.statements, self._fetchone_results)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass


@unittest.skipIf(PostgresStore is None, "psycopg is installed in the ETL image")
class DatasetPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(ROOT / "config" / "layers.json")
        self.target_tables = [layer.target_table for layer in self.config.layers]

    def store(self, *, previous_row=None) -> tuple[PostgresStore, ScriptedFakeConnection]:
        fetchone_results = [previous_row] + [
            (100 + index,) for index in range(len(self.target_tables))
        ]
        connection = ScriptedFakeConnection(fetchone_results)
        return PostgresStore(connection, self.config), connection  # type: ignore[arg-type]

    def test_ddl_creates_a_singleton_publication_table(self) -> None:
        # initialize() itself calls fetchone() twice first: once (discarded)
        # for the PostGIS_Version() probe, once for the schema-writability
        # check, which must return a truthy first element.
        connection = ScriptedFakeConnection([(True,), (True,)])
        store = PostgresStore(connection, self.config)  # type: ignore[arg-type]
        store.initialize()
        ddl = "\n".join(statement for statement, _ in connection.statements)
        self.assertIn("CREATE TABLE IF NOT EXISTS", ddl)
        self.assertIn("dataset_publication", ddl)
        self.assertIn("singleton boolean PRIMARY KEY DEFAULT true", ddl)
        self.assertIn("CHECK (singleton)", ddl)
        self.assertIn("release_id text NOT NULL UNIQUE", ddl)
        self.assertNotIn("_dataset_publication", ddl)

    def test_publish_release_computes_row_counts_and_commits(self) -> None:
        store, connection = self.store(previous_row=None)

        store.publish_release(
            dataset_id="leeds",
            release_id="release-1",
            schema_version=1,
            source_hash="a" * 64,
            geometry_contract_version=1,
        )

        self.assertEqual(1, connection.commits)
        self.assertEqual(0, connection.rollbacks)
        insert_statement, params = connection.statements[-1]
        self.assertIn("INSERT INTO", insert_statement)
        self.assertIn("dataset_publication", insert_statement)
        self.assertIn("ON CONFLICT (singleton) DO UPDATE", insert_statement)
        row_counts = params[4].obj
        self.assertEqual(
            {table: 100 + index for index, table in enumerate(self.target_tables)},
            row_counts,
        )

    def test_publish_release_rejects_a_schema_version_regression(self) -> None:
        store, connection = self.store(previous_row=(5, 2))

        with self.assertRaises(DatabaseError):
            store.publish_release(
                dataset_id="leeds",
                release_id="release-2",
                schema_version=4,
                source_hash="b" * 64,
                geometry_contract_version=2,
            )
        self.assertEqual(0, connection.commits)
        self.assertEqual(1, connection.rollbacks)

    def test_publish_release_rejects_a_geometry_contract_version_regression(self) -> None:
        store, connection = self.store(previous_row=(5, 2))

        with self.assertRaises(DatabaseError):
            store.publish_release(
                dataset_id="leeds",
                release_id="release-2",
                schema_version=5,
                source_hash="b" * 64,
                geometry_contract_version=1,
            )
        self.assertEqual(0, connection.commits)
        self.assertEqual(1, connection.rollbacks)

    def test_publish_release_allows_an_unchanged_version_republish(self) -> None:
        store, connection = self.store(previous_row=(5, 2))

        store.publish_release(
            dataset_id="leeds",
            release_id="release-2",
            schema_version=5,
            source_hash="c" * 64,
            geometry_contract_version=2,
        )

        self.assertEqual(1, connection.commits)
        self.assertEqual(0, connection.rollbacks)


if __name__ == "__main__":
    unittest.main()
