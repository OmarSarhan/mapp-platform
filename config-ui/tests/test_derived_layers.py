import unittest
from unittest.mock import MagicMock

from derived_layers import (
    DerivedLayerDependencyError,
    DerivedLayerError,
    DerivedLayerStore,
    validate_definition,
)


class DerivedLayerDefinitionTests(unittest.TestCase):
    def valid(self, **updates):
        value = {
            "name": "paths_h3_r9",
            "kind": "view",
            "query": (
                "SELECT cell_id, geom_3857 "
                "FROM leeds.h3_cells JOIN leeds.definitive_paths "
                "ON ST_Intersects(h3_cells.geom_3857, "
                "definitive_paths.geom_3857)"
            ),
            "sources": ["leeds.h3_cells", "leeds.definitive_paths"],
            "idColumn": "cell_id",
            "geometryColumn": "geom_3857",
        }
        value.update(updates)
        return value

    def test_normalizes_a_select_definition(self):
        result = validate_definition(self.valid())
        self.assertEqual(
            result["sources"],
            ["leeds.definitive_paths", "leeds.h3_cells"],
        )
        self.assertEqual(result["kind"], "view")

    def test_accepts_materialized_view(self):
        result = validate_definition(self.valid(kind="materialized"))
        self.assertEqual(result["kind"], "materialized")

    def test_rejects_statements_and_comments(self):
        for query in (
            "DELETE FROM leeds.definitive_paths",
            "SELECT 1; SELECT 2",
            "SELECT 1 -- hidden",
            "WITH removed AS (DELETE FROM leeds.definitive_paths RETURNING *) "
            "SELECT * FROM removed",
        ):
            with self.subTest(query=query):
                with self.assertRaises(DerivedLayerError):
                    validate_definition(self.valid(query=query))

    def test_rejects_unqualified_and_managed_sources(self):
        for sources in (
            ["definitive_paths"],
            ["derived_layers.another_view"],
        ):
            with self.subTest(sources=sources):
                with self.assertRaises(DerivedLayerError):
                    validate_definition(self.valid(sources=sources))

    def test_requires_safe_names_and_output_columns(self):
        for update in (
            {"name": "Bad Name"},
            {"idColumn": "COUNT(*)"},
            {"geometryColumn": "path.geom"},
        ):
            with self.subTest(update=update):
                with self.assertRaises(DerivedLayerError):
                    validate_definition(self.valid(**update))

    def test_incoming_dependents_are_reported_for_safe_blocking(self):
        class Cursor:
            def execute(self, query, params):
                self.query = query
                self.params = params

            def fetchall(self):
                return [
                    {"dependent": "view reporting.paths"},
                    {"dependent": "materialized view reporting.summary"},
                ]

        cursor = Cursor()
        dependents = DerivedLayerStore._incoming_dependents(
            cursor,
            "paths_h3_r9",
        )
        self.assertEqual(cursor.params, ("derived_layers.paths_h3_r9",))
        self.assertEqual(dependents, [
            "view reporting.paths",
            "materialized view reporting.summary",
        ])
        error = DerivedLayerDependencyError("paths_h3_r9", dependents)
        self.assertEqual(error.dependents, dependents)
        self.assertIn("cannot be replaced or dropped", str(error))

    def test_column_names_preserve_relation_order(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{"attname": "qid"}, {"attname": "geom"}]
        self.assertEqual(
            DerivedLayerStore._column_names(cursor, "paths_h3_r9"),
            ["qid", "geom"],
        )
        self.assertEqual(cursor.execute.call_args.args[1], ("derived_layers.paths_h3_r9",))

    def test_dependency_error_carries_field_impact(self):
        error = DerivedLayerDependencyError(
            "paths_h3_r9",
            ["view reporting.paths"],
            removed_columns=["status"],
            dependent_columns=["status"],
        )
        self.assertEqual(error.removed_columns, ["status"])
        self.assertEqual(error.dependent_columns, ["status"])


if __name__ == "__main__":
    unittest.main()
