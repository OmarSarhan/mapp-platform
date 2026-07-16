from __future__ import annotations

import unittest
import uuid
from pathlib import Path
from typing import Any

from leeds_arcgis_etl.config import load_config
from leeds_arcgis_etl.core import PreparedFeature

try:
    from leeds_arcgis_etl.database import PostgresStore
except ModuleNotFoundError:  # The production image installs psycopg.
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


if __name__ == "__main__":
    unittest.main()
