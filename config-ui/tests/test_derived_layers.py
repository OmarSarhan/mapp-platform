import hashlib
import json
import threading
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import psycopg

from derived_query_guard import GuardReason, QueryGuardViolation
from derived_layers import (
    H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS,
    H3_SCOPE_MAX_ESTIMATED_CELLS,
    MATERIALIZED_MAX_ESTIMATED_BYTES,
    QUERY_PLAN_MAX_INTERMEDIATE_BYTES,
    QUERY_PLAN_MAX_TOTAL_COST,
    DerivedLayerDatabaseOperationError,
    DerivedLayerDependencyError,
    DerivedLayerError,
    DerivedLayerMaterializationTooLarge,
    DerivedLayerMaintenanceError,
    DerivedLayerQueryTooExpensive,
    DerivedLayerResetOwnershipError,
    DerivedLayerStore,
    validate_definition,
)


class DerivedLayerDefinitionTests(unittest.TestCase):
    @staticmethod
    def store_with_cursor(cursor):
        store = DerivedLayerStore("postgresql://database", "mapp_xyz")
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        store._connect = MagicMock(return_value=connection)
        store._initialize = MagicMock()
        store._catalog_query_probe = MagicMock()
        store._validate_catalog_dependencies = MagicMock()
        return store

    @staticmethod
    def explain_plan(
        rows=250,
        width=64,
        *,
        total_cost=1000.0,
        node_type="Seq Scan",
        plans=None,
        workers=None,
    ):
        plan = {
            "Node Type": node_type,
            "Plan Rows": rows,
            "Plan Width": width,
            "Total Cost": total_cost,
        }
        if plans is not None:
            plan["Plans"] = plans
        if workers is not None:
            plan["Workers Planned"] = workers
        return {"QUERY PLAN": [{"Plan": plan}]}

    @staticmethod
    def materialization_probe(**updates):
        probe = {
            "method": "postgresql-explain",
            "estimatedRows": 250,
            "planRowWidthBytes": 64,
            "rowOverheadBytes": 32,
            "safetyMultiplier": 1.2,
            "estimatedBytes": 28_800,
            "maxEstimatedBytes": MATERIALIZED_MAX_ESTIMATED_BYTES,
        }
        probe.update(updates)
        return probe

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

    @staticmethod
    def spatial_scope():
        return {
            "type": "workspace-map-extent",
            "locale": "locale",
            "sourceView": {"lng": -1.5491, "lat": 53.8008, "z": 11},
            "scopeZoom": 10,
            "zoomOffset": -1,
            "viewport": {"width": 1920, "height": 1080, "tileSize": 256},
            "crs": "EPSG:4326",
            "envelopes": [{
                "west": -2.867459375,
                "south": 53.02923019,
                "east": -0.230740625,
                "north": 54.55739493,
            }],
            "selection": "intersects-output-geometry",
            "clipsGeometry": False,
            "guidance": "Fixed output-row guard.",
        }

    def test_schema_initialization_runs_once_before_normal_connections(self):
        store = DerivedLayerStore("postgresql://database", "mapp_xyz")
        first = MagicMock()
        second = MagicMock()
        with patch(
            "derived_layers.psycopg.connect",
            side_effect=[first, second],
        ), patch.object(store, "_initialize") as initialize:
            self.assertIs(first, store._connect())
            self.assertIs(second, store._connect())

        initialize.assert_called_once_with(
            first.cursor.return_value.__enter__.return_value
        )
        first.commit.assert_called_once_with()
        second.commit.assert_not_called()

    def test_connection_pins_search_path_before_schema_or_query_work(self):
        store = DerivedLayerStore("postgresql://database", "mapp_xyz")
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        events = []
        cursor.execute.side_effect = lambda query, *args: events.append(str(query))
        store._initialize = MagicMock(
            side_effect=lambda _cur: events.append("initialize")
        )

        with patch("derived_layers.psycopg.connect", return_value=connection):
            store._connect()

        self.assertEqual(
            "SET SESSION search_path = pg_catalog, public",
            events[0],
        )
        self.assertIn("pg_advisory_xact_lock", events[1])
        self.assertEqual("initialize", events[2])

    def test_mutation_body_database_failure_reports_proven_rollback(self):
        store = DerivedLayerStore("postgresql://database", "mapp_xyz")
        failure = psycopg.ProgrammingError("statement failed")
        connection = MagicMock()
        store._connect = MagicMock(return_value=connection)

        with self.assertRaises(DerivedLayerDatabaseOperationError) as raised:
            with store._mutation_connection():
                raise failure

        self.assertIs(failure, raised.exception.cause)
        self.assertEqual(
            "database-transaction",
            raised.exception.failure_phase,
        )
        self.assertTrue(raised.exception.state_unchanged)
        self.assertTrue(raised.exception.rolled_back)
        self.assertFalse(raised.exception.indeterminate)
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        connection.close.assert_called_once_with()

    def test_mutation_commit_failure_is_indeterminate(self):
        store = DerivedLayerStore("postgresql://database", "mapp_xyz")
        failure = psycopg.OperationalError("connection lost during commit")
        connection = MagicMock()
        connection.commit.side_effect = failure
        store._connect = MagicMock(return_value=connection)

        with self.assertRaises(DerivedLayerDatabaseOperationError) as raised:
            with store._mutation_connection():
                pass

        self.assertIs(failure, raised.exception.cause)
        self.assertEqual("transaction-commit", raised.exception.failure_phase)
        self.assertFalse(raised.exception.state_unchanged)
        self.assertFalse(raised.exception.rolled_back)
        self.assertTrue(raised.exception.indeterminate)
        connection.rollback.assert_not_called()
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_mutation_rollback_failure_is_indeterminate(self):
        store = DerivedLayerStore("postgresql://database", "mapp_xyz")
        statement_failure = psycopg.ProgrammingError("statement failed")
        rollback_failure = psycopg.OperationalError(
            "connection lost during rollback",
        )
        connection = MagicMock()
        connection.rollback.side_effect = rollback_failure
        store._connect = MagicMock(return_value=connection)

        with self.assertRaises(DerivedLayerDatabaseOperationError) as raised:
            with store._mutation_connection():
                raise statement_failure

        self.assertIs(rollback_failure, raised.exception.cause)
        self.assertEqual(
            "transaction-rollback",
            raised.exception.failure_phase,
        )
        self.assertFalse(raised.exception.state_unchanged)
        self.assertFalse(raised.exception.rolled_back)
        self.assertTrue(raised.exception.indeterminate)
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        connection.close.assert_called_once_with()

    def test_schema_initialization_serializes_threads_and_retries_failure(self):
        store = DerivedLayerStore("postgresql://database", "mapp_xyz")
        failed = MagicMock()
        recovered = MagicMock()
        with patch(
            "derived_layers.psycopg.connect",
            side_effect=[failed, recovered],
        ), patch.object(
            store,
            "_initialize",
            side_effect=[RuntimeError("migration failed"), None],
        ) as initialize:
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                store._connect()
            self.assertIs(recovered, store._connect())

        failed.close.assert_called_once_with()
        recovered.commit.assert_called_once_with()
        self.assertEqual(2, initialize.call_count)

        concurrent = DerivedLayerStore(
            "postgresql://database",
            "mapp_xyz",
        )
        first = MagicMock()
        second = MagicMock()
        entered = threading.Event()
        release = threading.Event()
        results = []
        failures = []

        def initialize_once(_cur):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test initialization timed out")

        def connect():
            try:
                results.append(concurrent._connect())
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        with patch(
            "derived_layers.psycopg.connect",
            side_effect=[first, second],
        ), patch.object(
            concurrent,
            "_initialize",
            side_effect=initialize_once,
        ) as initialize:
            first_thread = threading.Thread(target=connect)
            second_thread = threading.Thread(target=connect)
            first_thread.start()
            self.assertTrue(entered.wait(1))
            second_thread.start()
            release.set()
            first_thread.join(2)
            second_thread.join(2)

        self.assertEqual([], failures)
        self.assertCountEqual([first, second], results)
        initialize.assert_called_once()

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

    def test_accepts_only_a_resolved_workspace_map_extent(self):
        result = validate_definition(self.valid(
            spatialScope=self.spatial_scope(),
        ))
        self.assertEqual(
            self.spatial_scope(),
            result["spatialScope"],
        )
        self.assertIsNone(validate_definition(self.valid())["spatialScope"])

        for spatial_scope in (
            {"type": "workspace-map-extent"},
            {**self.spatial_scope(), "scopeZoom": 11},
            {
                **self.spatial_scope(),
                "envelopes": [{
                    "west": -181,
                    "south": 53,
                    "east": -1,
                    "north": 54,
                }],
            },
        ):
            with self.subTest(spatial_scope=spatial_scope):
                with self.assertRaises(DerivedLayerError):
                    validate_definition(self.valid(spatialScope=spatial_scope))

    def test_workspace_scope_wraps_query_with_safe_output_intersection(self):
        definition = validate_definition(self.valid(
            spatialScope={
                **self.spatial_scope(),
                "envelopes": [
                    {
                        "west": 170,
                        "south": -10,
                        "east": 180,
                        "north": 10,
                    },
                    {
                        "west": -180,
                        "south": -10,
                        "east": -170,
                        "north": 10,
                    },
                ],
            },
        ))

        executable = DerivedLayerStore._executable_query(
            definition
        ).as_string(None)

        self.assertIn(definition["query"], executable)
        self.assertEqual(4, executable.count("ST_MakeEnvelope"))
        self.assertIn('WITH "_mapp_h3_scope" ("geom_4326")', executable)
        self.assertIn('"_mapp_spatial_scope"."geom_3857"', executable)
        self.assertIn("ST_MakeEnvelope(170", executable)
        self.assertIn(" OR ", executable)
        self.assertEqual(
            definition["query"],
            DerivedLayerStore._executable_query(
                validate_definition(self.valid())
            ).as_string(None),
        )

    def test_workspace_scope_keeps_whole_dataset_aggregate_inside_guard(self):
        query = (
            "SELECT region_id, sum(value) AS total_value, "
            "ST_Centroid(ST_Collect(geom_3857))::geometry(Point,3857) "
            "AS geom_3857 FROM leeds.metrics GROUP BY region_id"
        )
        definition = validate_definition(self.valid(
            query=query,
            sources=["leeds.metrics"],
            spatialScope=self.spatial_scope(),
        ))

        executable = DerivedLayerStore._executable_query(
            definition
        ).as_string(None)

        self.assertIn(f"FROM ({query}) AS", executable)
        self.assertEqual(1, executable.count("sum(value)"))
        self.assertGreater(executable.index("ST_Intersects"), executable.index(query))

    def test_materialization_probe_has_closed_conservative_arithmetic(self):
        probe = DerivedLayerStore._materialization_probe_result(
            "paths_h3_r9",
            {"QUERY PLAN": [{"Plan": {"Plan Rows": 7, "Plan Width": 101}}]},
        )

        self.assertEqual({
            "method": "postgresql-explain",
            "estimatedRows": 7,
            "planRowWidthBytes": 101,
            "rowOverheadBytes": 32,
            "safetyMultiplier": 1.2,
            "estimatedBytes": 1118,
            "maxEstimatedBytes": MATERIALIZED_MAX_ESTIMATED_BYTES,
        }, probe)

    def test_materialization_probe_rejects_malformed_plans(self):
        for row in (
            None,
            {},
            {"QUERY PLAN": []},
            {"QUERY PLAN": [{"Plan": {"Plan Rows": True, "Plan Width": 1}}]},
            {"QUERY PLAN": [{"Plan": {"Plan Rows": 1, "Plan Width": -1}}]},
        ):
            with self.subTest(row=row):
                with self.assertRaisesRegex(
                    DerivedLayerError,
                    "invalid materialization size probe",
                ):
                    DerivedLayerStore._materialization_probe_result(
                        "paths_h3_r9",
                        row,
                    )

    def test_query_plan_probe_walks_the_complete_plan(self):
        child = {
            "Node Type": "Seq Scan",
            "Plan Rows": 800,
            "Plan Width": 96,
            "Total Cost": 400.0,
            "Workers Planned": 2,
        }
        probe = DerivedLayerStore._query_plan_probe_result(
            "paths_h3_r9",
            self.explain_plan(
                rows=2_400,
                width=64,
                total_cost=2_500.0,
                node_type="Nested Loop",
                plans=[child],
                workers=1,
            ),
            {"polygonToCellsCalls": 0},
        )

        self.assertEqual(2_500.0, probe["estimatedTotalCost"])
        self.assertEqual(2_400, probe["estimatedFinalRows"])
        self.assertEqual(2_400, probe["maxIntermediateRows"])
        self.assertEqual(3.0, probe["maxJoinExpansionRatio"])
        self.assertEqual(2, probe["planNodeCount"])
        self.assertEqual(2, probe["planDepth"])
        self.assertEqual(3, probe["plannedWorkers"])
        self.assertFalse(probe["recursivePlan"])
        self.assertEqual(
            QUERY_PLAN_MAX_TOTAL_COST,
            probe["limits"]["maxTotalCost"],
        )

    def test_query_plan_guard_catches_work_hidden_by_small_aggregate(self):
        scan = {
            "Node Type": "Seq Scan",
            "Plan Rows": 100_000_001,
            "Plan Width": 160,
            "Total Cost": 1_000_000.0,
        }
        with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
            DerivedLayerStore._query_plan_probe_result(
                "paths_h3_r9",
                self.explain_plan(
                    rows=1,
                    width=64,
                    total_cost=2_000_000.0,
                    node_type="Aggregate",
                    plans=[scan],
                ),
                {"polygonToCellsCalls": 0},
            )

        self.assertEqual("paths_h3_r9", raised.exception.name)
        self.assertEqual(
            ["intermediate_rows", "intermediate_bytes"],
            [reason["code"] for reason in raised.exception.reasons],
        )
        self.assertGreater(
            raised.exception.probe["maxIntermediateBytes"],
            QUERY_PLAN_MAX_INTERMEDIATE_BYTES,
        )

    def test_query_plan_guard_reports_recursive_join_and_worker_risks(self):
        left = {
            "Node Type": "Seq Scan",
            "Plan Rows": 1,
            "Plan Width": 8,
            "Total Cost": 1.0,
        }
        right = dict(left)
        with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
            DerivedLayerStore._query_plan_probe_result(
                "paths_h3_r9",
                self.explain_plan(
                    rows=2_000,
                    width=8,
                    total_cost=QUERY_PLAN_MAX_TOTAL_COST + 1,
                    node_type="Recursive Union",
                    plans=[{
                        "Node Type": "Nested Loop",
                        "Plan Rows": 2_000,
                        "Plan Width": 8,
                        "Total Cost": 2.0,
                        "Workers Planned": 9,
                        "Plans": [left, right],
                    }],
                ),
                {"polygonToCellsCalls": 0},
            )

        codes = {reason["code"] for reason in raised.exception.reasons}
        self.assertTrue({
            "recursive_plan", "total_cost", "join_expansion",
            "planned_workers",
        }.issubset(codes))

    def test_query_plan_probe_rejects_malformed_child_nodes(self):
        malformed = self.explain_plan(plans=[{
            "Node Type": "Seq Scan",
            "Plan Rows": 1,
        }])
        with self.assertRaisesRegex(DerivedLayerError, "invalid query plan"):
            DerivedLayerStore._query_plan_probe_result(
                "paths_h3_r9", malformed, {},
            )

    def test_query_plan_guard_limits_final_rows_nodes_and_depth(self):
        with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
            DerivedLayerStore._query_plan_probe_result(
                "paths_h3_r9",
                self.explain_plan(rows=10_000_001, width=8),
                {},
            )
        self.assertIn(
            "final_rows",
            [reason["code"] for reason in raised.exception.reasons],
        )

        leaf = {
            "Node Type": "Result",
            "Plan Rows": 1,
            "Plan Width": 8,
            "Total Cost": 1.0,
        }
        deep = leaf
        for _ in range(32):
            deep = {
                **leaf,
                "Plans": [deep],
            }
        with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
            DerivedLayerStore._query_plan_probe_result(
                "paths_h3_r9",
                {"QUERY PLAN": [{"Plan": deep}]},
                {},
            )
        self.assertIn(
            "plan_depth",
            [reason["code"] for reason in raised.exception.reasons],
        )

        wide = self.explain_plan(plans=[dict(leaf) for _ in range(150)])
        with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
            DerivedLayerStore._query_plan_probe_result(
                "paths_h3_r9", wide, {},
            )
        self.assertIn(
            "plan_nodes",
            [reason["code"] for reason in raised.exception.reasons],
        )

    def test_static_shape_guard_blocks_obvious_explosions(self):
        queries = (
            "WITH RECURSIVE walk AS (SELECT 1) SELECT * FROM walk",
            "SELECT * FROM leeds.paths CROSS JOIN leeds.cells",
            "SELECT * FROM leeds.paths NATURAL JOIN leeds.cells",
            "SELECT * FROM generate_series(1, 100001)",
            "SELECT * FROM generate_series(1, source.dynamic_end)",
            " UNION ALL ".join("SELECT 1" for _ in range(10)),
        )
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(DerivedLayerQueryTooExpensive):
                    validate_definition(self.valid(query=query))

        validate_definition(self.valid(
            query="SELECT value FROM generate_series(1, 100000) AS value",
        ))

    def test_static_shape_guard_blocks_stalling_and_session_functions(self):
        queries = (
            "SELECT pg_sleep(30)",
            "SELECT pg_sleep_for(interval '1 minute')",
            "SELECT pg_advisory_xact_lock(42)",
            'SELECT "public"."pg_try_advisory_lock"(42)',
            "SELECT set_config('work_mem', '1GB', true)",
            'SELECT "set_config"(\'work_mem\', \'1GB\', true)',
        )
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
                    validate_definition(self.valid(query=query))
                self.assertIn(
                    "hazardous_function",
                    [reason["code"] for reason in raised.exception.reasons],
                )

    def test_unicode_quoted_hazardous_functions_cannot_bypass_guard(self):
        queries = (
            r'SELECT U&"pg_sl\0065ep"(30)',
            r'SELECT U&"pg_sl\+000065ep"(30)',
            r'SELECT pg_catalog.U&"pg_advisory_\006Cock"(42)',
            r'''SELECT U&"pg_sl!0065ep" UESCAPE '!'(30)''',
        )
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
                    validate_definition(self.valid(query=query))
                self.assertIn(
                    "hazardous_function",
                    [reason["code"] for reason in raised.exception.reasons],
                )

        validate_definition(self.valid(
            query="SELECT 'pg_sleep(30)'::text AS harmless_literal",
        ))

    def test_h3_polygon_expansion_uses_scope_literal_and_cell_budget(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = self.explain_plan(rows=100_000, width=64)
        store = self.store_with_cursor(cursor)
        query = (
            "SELECT cell::text AS cell_id, "
            "h3_cell_to_boundary_geometry(cell) AS geom_3857 "
            "FROM _mapp_h3_scope CROSS JOIN LATERAL "
            "h3_polygon_to_cells(_mapp_h3_scope.geom_4326, 9) AS cell"
        )

        result = store.preflight_definition(self.valid(
            query=query,
            sources=["leeds.definitive_paths"],
            spatialScope=self.spatial_scope(),
        ))

        h3 = result["queryPlanProbe"]["h3Expansion"]
        self.assertEqual([9], h3["resolutions"])
        self.assertGreater(h3["estimatedScopeCells"], 0)
        self.assertLess(
            h3["estimatedScopeCells"], H3_SCOPE_MAX_ESTIMATED_CELLS,
        )
        executable = cursor.execute.call_args_list[-1].args[0].as_string(None)
        self.assertIn('WITH "_mapp_h3_scope" ("geom_4326")', executable)

    def test_h3_polygon_expansion_rejects_dynamic_unscoped_and_too_fine(self):
        queries = (
            "SELECT * FROM _mapp_h3_scope CROSS JOIN LATERAL "
            "h3_polygon_to_cells(_mapp_h3_scope.geom_4326, resolution)",
            "SELECT * FROM leeds.areas CROSS JOIN LATERAL "
            "h3_polygon_to_cells(areas.geom_4326, 9)",
            "SELECT * FROM _mapp_h3_scope CROSS JOIN LATERAL "
            "h3_polygon_to_cells(_mapp_h3_scope.geom_4326, 15)",
        )
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(DerivedLayerQueryTooExpensive):
                    validate_definition(self.valid(
                        query=query,
                        spatialScope=self.spatial_scope(),
                    ))

    def test_h3_scope_cannot_be_shadowed_by_cte_or_relation_alias(self):
        expansion = (
            "CROSS JOIN LATERAL h3_polygon_to_cells("
            "_mapp_h3_scope.geom_4326, 9) AS cell"
        )
        queries = (
            f"SELECT cell FROM leeds.areas AS _mapp_h3_scope {expansion}",
            f'SELECT cell FROM leeds.areas AS "_mapp_h3_scope" {expansion}',
            f"SELECT cell FROM leeds.areas _mapp_h3_scope {expansion}",
            "WITH _mapp_h3_scope AS ("
            "SELECT geom_4326 FROM leeds.areas) "
            f"SELECT cell FROM _mapp_h3_scope {expansion}",
            r'SELECT cell FROM leeds.areas AS U&"_mapp_h3_sc\006Fpe" '
            + expansion,
            r'''WITH U&"_mapp_h3_sc!006Fpe" UESCAPE '!' AS ('''
            "SELECT geom_4326 FROM leeds.areas) "
            f"SELECT cell FROM _mapp_h3_scope {expansion}",
        )
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
                    validate_definition(self.valid(
                        query=query,
                        spatialScope=self.spatial_scope(),
                    ))
                self.assertIn(
                    "h3_scope_shadowed",
                    [reason["code"] for reason in raised.exception.reasons],
                )

    def test_h3_scope_direct_reads_remain_available(self):
        queries = (
            "SELECT count(*) FROM _mapp_h3_scope",
            "SELECT cell FROM _mapp_h3_scope CROSS JOIN LATERAL "
            "h3_polygon_to_cells(_mapp_h3_scope.geom_4326, 9) AS cell",
            r'SELECT cell FROM U&"_mapp_h3_sc\006Fpe" CROSS JOIN LATERAL '
            r'h3_polygon_to_cells(U&"_mapp_h3_sc\006Fpe".geom_4326, 9) '
            "AS cell",
        )
        for query in queries:
            with self.subTest(query=query):
                validate_definition(self.valid(
                    query=query,
                    spatialScope=self.spatial_scope(),
                ))

    def test_h3_composed_expansion_bounds_grid_and_immediate_children(self):
        small_grid = (
            "SELECT * FROM _mapp_h3_scope CROSS JOIN LATERAL "
            "h3_polygon_to_cells(_mapp_h3_scope.geom_4326, 6) AS cell "
            "CROSS JOIN LATERAL h3_grid_disk(cell, 25) AS neighbour"
        )
        validate_definition(self.valid(
            query=small_grid,
            spatialScope=self.spatial_scope(),
        ))

        large_grid = small_grid.replace(
            "geom_4326, 6", "geom_4326, 7",
        )
        with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
            validate_definition(self.valid(
                query=large_grid,
                spatialScope=self.spatial_scope(),
            ))
        self.assertIn(
            "h3_composed_expansion",
            [reason["code"] for reason in raised.exception.reasons],
        )
        self.assertEqual(
            H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS + 1,
            raised.exception.probe["h3Expansion"]["estimatedExpandedCells"],
        )

        narrower_scope = self.spatial_scope()
        narrower_scope["envelopes"][0]["east"] = -1.55
        children = (
            "SELECT h3_cell_to_children(cell) "
            "FROM _mapp_h3_scope CROSS JOIN LATERAL "
            "h3_polygon_to_cells(_mapp_h3_scope.geom_4326, 10) AS cell"
        )
        with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
            validate_definition(self.valid(
                query=children,
                spatialScope=narrower_scope,
            ))
        self.assertIn(
            "h3_composed_expansion",
            [reason["code"] for reason in raised.exception.reasons],
        )

    def test_quoted_risky_functions_cannot_bypass_static_guards(self):
        queries = (
            'SELECT * FROM "generate_series"(1, 100001)',
            'SELECT * FROM public."h3_polygon_to_cells"('
            'leeds.areas.geom_4326, 9)',
            'SELECT "public"."h3_grid_disk"(cell_id, radius) '
            'FROM leeds.h3_cells',
            'SELECT "h3_cell_to_children"(cell_id, 15) '
            'FROM leeds.h3_cells',
        )
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(DerivedLayerQueryTooExpensive):
                    validate_definition(self.valid(
                        query=query,
                        spatialScope=self.spatial_scope(),
                    ))

        validate_definition(self.valid(
            query='SELECT "h3_cell_to_children"(cell_id) '
            'FROM leeds.h3_cells',
        ))

    def test_h3_safe_aggregation_and_immediate_children_remain_available(self):
        safe_queries = (
            "SELECT h3_cell_to_parent(cell_id, 8) AS cell_id, count(*) "
            "FROM leeds.h3_cells GROUP BY 1",
            "SELECT h3_cell_to_children(cell_id) FROM leeds.h3_cells",
            "SELECT h3_cell_to_children(cell_id, "
            "h3_get_resolution(cell_id) + 1) FROM leeds.h3_cells",
            "SELECT h3_grid_disk(cell_id, 25) FROM leeds.h3_cells",
            "SELECT h3_grid_ring(cell_id, 25) FROM leeds.h3_cells",
        )
        for query in safe_queries:
            with self.subTest(query=query):
                validate_definition(self.valid(query=query))

        unsafe_queries = (
            "SELECT h3_cell_to_children(cell_id, 15) FROM leeds.h3_cells",
            "SELECT h3_uncompact_cells(cells, 12) FROM leeds.h3_cells",
            "SELECT h3_grid_disk(cell_id, radius) FROM leeds.h3_cells",
            "SELECT h3_grid_disk(cell_id, 26) FROM leeds.h3_cells",
            "SELECT h3_grid_ring(cell_id, radius) FROM leeds.h3_cells",
            "SELECT h3_grid_ring(cell_id, 26) FROM leeds.h3_cells",
        )
        for query in unsafe_queries:
            with self.subTest(query=query):
                with self.assertRaises(DerivedLayerQueryTooExpensive):
                    validate_definition(self.valid(query=query))

    def test_h3_path_recursive_and_uncompact_variants_are_blocked(self):
        queries = (
            "SELECT h3_grid_path_cells(origin, destination) "
            "FROM leeds.h3_cells",
            "SELECT h3_grid_path_cells_recursive(origin, destination) "
            "FROM leeds.h3_cells",
            "SELECT h3_line(origin, destination) FROM leeds.h3_cells",
            "SELECT h3_uncompact_cells(cells, 12) FROM leeds.h3_cells",
            "SELECT h3_uncompact(cells, 12) FROM leeds.h3_cells",
            "SELECT h3_cell_to_children_slow(cell_id, 12) "
            "FROM leeds.h3_cells",
            "SELECT h3_to_children_slow(cell_id, 12) FROM leeds.h3_cells",
            r'SELECT U&"h3_grid_path_cells_recurs\0069ve"('
            "origin, destination) FROM leeds.h3_cells",
        )
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
                    validate_definition(self.valid(query=query))
                self.assertIn(
                    "h3_unbounded_expansion",
                    [reason["code"] for reason in raised.exception.reasons],
                )

    def test_capabilities_advertise_closed_query_and_h3_guards(self):
        cursor = MagicMock()
        extensions = [
            {"extname": "h3", "extversion": "4.2"},
            {"extname": "h3_postgis", "extversion": "4.2"},
            {"extname": "postgis", "extversion": "3.5"},
        ]
        wrapper = {
            "kind": "function",
            "object_oid": 100,
            "identity": "public.h3_polygon_to_cells(geometry,integer)",
            "schema": "public",
            "name": "h3_polygon_to_cells",
            "extension": "h3_postgis",
            "extension_schema": "public",
            "implementation_schema": "public",
            "implementation_extension": "h3_postgis",
            "implementation_extension_schema": "public",
            "volatility": "i",
            "returns_set": True,
            "routine_kind": "f",
            "security_definer": False,
            "routine_config": ["search_path=pg_catalog, public"],
            "language": "sql",
            "object_builtin": False,
            "implementation_builtin": False,
            "approved_extension_search_path": (
                "search_path=pg_catalog, public"
            ),
            "geometry_schema": "public",
        }
        cursor.fetchall.side_effect = [extensions, [wrapper]]
        cursor.fetchone.return_value = {"cellCount": 0}
        store = self.store_with_cursor(cursor)

        capabilities = store.capabilities()
        query_guard = capabilities["queryGuard"]

        self.assertEqual({
            "method",
            "stages",
            "limits",
            "shapeLimits",
            "h3",
            "errorCategories",
        }, set(query_guard))
        self.assertEqual({
            "maxTotalCost",
            "maxFinalRows",
            "maxIntermediateRows",
            "maxIntermediateBytes",
            "maxJoinExpansionRatio",
            "maxPlanNodes",
            "maxPlanDepth",
            "maxPlannedWorkers",
        }, set(query_guard["limits"]))
        self.assertEqual({
            "maxEstimatedScopeCells",
            "maxEstimatedExpandedCells",
            "scopeEstimateSafetyMultiplier",
            "maxGridDistance",
        }, set(query_guard["h3"]))
        self.assertEqual({
            "maxJoins",
            "maxCtes",
            "maxSetOperations",
            "maxGroupingSets",
            "maxGeneratedRows",
        }, set(query_guard["shapeLimits"]))
        self.assertEqual(
            [
                "postgresql-ast-guard",
                "postgresql-catalog-guard",
                "postgresql-explain",
            ],
            query_guard["stages"],
        )
        self.assertEqual(
            {
                "invalid": {
                    "code": "derived_layer.query_invalid",
                    "httpStatus": 400,
                },
                "policy": {
                    "code": "derived_layer.query_not_allowed",
                    "httpStatus": 422,
                },
                "compute": {
                    "code": "derived_layer.query_too_expensive",
                    "httpStatus": 409,
                },
            },
            query_guard["errorCategories"],
        )
        self.assertEqual(
            H3_SCOPE_MAX_ESTIMATED_CELLS,
            query_guard["h3"]["maxEstimatedScopeCells"],
        )
        self.assertEqual(
            H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS,
            query_guard["h3"]["maxEstimatedExpandedCells"],
        )
        self.assertTrue(capabilities["h3Available"])
        self.assertEqual(
            {
                "method": "postgresql-catalog-and-execution",
                "ready": True,
            },
            capabilities["h3Readiness"],
        )
        self.assertEqual(4, cursor.execute.call_count)

    def test_capabilities_do_not_advertise_an_unsafe_h3_wrapper(self):
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[
            {"extname": "h3", "extversion": "4.2"},
            {"extname": "h3_postgis", "extversion": "4.2"},
            {"extname": "postgis", "extversion": "3.5"},
        ], [{
            "kind": "function",
            "object_oid": 100,
            "identity": "public.h3_polygon_to_cells(geometry,integer)",
            "schema": "public",
            "name": "h3_polygon_to_cells",
            "extension": "h3_postgis",
            "extension_schema": "public",
            "implementation_schema": "public",
            "implementation_extension": "h3_postgis",
            "implementation_extension_schema": "public",
            "volatility": "i",
            "returns_set": True,
            "routine_kind": "f",
            "security_definer": False,
            "routine_config": [
                "search_path=pg_catalog, public, unsafe"
            ],
            "language": "sql",
            "object_builtin": False,
            "implementation_builtin": False,
            "approved_extension_search_path": (
                "search_path=pg_catalog, public"
            ),
            "geometry_schema": "public",
        }]]
        store = self.store_with_cursor(cursor)

        capabilities = store.capabilities()

        self.assertFalse(capabilities["h3Available"])
        self.assertEqual(
            {
                "method": "postgresql-catalog-and-execution",
                "ready": False,
                "code": "derived_layer.h3_not_ready",
                "stage": "routine-policy",
                "reasons": [{
                    "code": "wrapper_not_approved",
                    "message": (
                        "The H3 polygon wrapper does not satisfy the "
                        "derived-query routine policy."
                    ),
                    "suggestedAction": (
                        "Apply the supported wrapper hardening migration, "
                        "then retry."
                    ),
                }],
            },
            capabilities["h3Readiness"],
        )
        cursor.fetchone.assert_not_called()
        self.assertEqual(2, cursor.execute.call_count)

    def test_h3_readiness_reports_each_bounded_failure_stage(self):
        extensions = {
            "postgis": "3.5.7",
            "h3": "4.2.3",
            "h3_postgis": "4.2.3",
        }
        wrapper = {
            "kind": "function",
            "object_oid": 100,
            "identity": "public.h3_polygon_to_cells(geometry,integer)",
            "schema": "public",
            "name": "h3_polygon_to_cells",
            "extension": "h3_postgis",
            "extension_schema": "public",
            "implementation_schema": "public",
            "implementation_extension": "h3_postgis",
            "implementation_extension_schema": "public",
            "volatility": "i",
            "returns_set": True,
            "routine_kind": "f",
            "security_definer": False,
            "routine_config": ["search_path=pg_catalog, public"],
            "language": "sql",
            "object_builtin": False,
            "implementation_builtin": False,
            "approved_extension_search_path": (
                "search_path=pg_catalog, public"
            ),
            "geometry_schema": "public",
        }

        cases = []

        missing_cursor = MagicMock()
        cases.append((
            {},
            MagicMock(),
            missing_cursor,
            "extension-discovery",
            "missing_extensions",
        ))

        version_cursor = MagicMock()
        cases.append((
            {**extensions, "h3_postgis": "4.2.2"},
            MagicMock(),
            version_cursor,
            "version-validation",
            "unsupported_extension_versions",
        ))

        catalog_cursor = MagicMock()
        catalog_cursor.fetchall.return_value = []
        cases.append((
            extensions,
            MagicMock(),
            catalog_cursor,
            "catalog-resolution",
            "wrapper_not_found",
        ))

        policy_cursor = MagicMock()
        policy_cursor.fetchall.return_value = [{
            **wrapper,
            "routine_config": [
                "search_path=pg_catalog, public, secret_schema"
            ],
        }]
        cases.append((
            extensions,
            MagicMock(),
            policy_cursor,
            "routine-policy",
            "wrapper_not_approved",
        ))

        planning_cursor = MagicMock()
        planning_cursor.fetchall.return_value = [wrapper]
        planning_cursor.execute.side_effect = [
            None,
            psycopg.ProgrammingError("SECRET nested dependency"),
        ]
        cases.append((
            extensions,
            MagicMock(),
            planning_cursor,
            "nested-dependency-resolution",
            "wrapper_dependencies_unresolved",
        ))

        execution_cursor = MagicMock()
        execution_cursor.fetchall.return_value = [wrapper]
        execution_cursor.execute.side_effect = [
            None,
            None,
            psycopg.ProgrammingError("SECRET execution context"),
        ]
        cases.append((
            extensions,
            MagicMock(),
            execution_cursor,
            "execution-probe",
            "execution_probe_failed",
        ))

        result_cursor = MagicMock()
        result_cursor.fetchall.return_value = [wrapper]
        result_cursor.fetchone.return_value = {
            "cellCount": "SECRET invalid value",
        }
        cases.append((
            extensions,
            MagicMock(),
            result_cursor,
            "result-validation",
            "invalid_probe_result",
        ))

        for (
            installed,
            connection,
            cursor,
            expected_stage,
            expected_reason,
        ) in cases:
            with self.subTest(stage=expected_stage):
                readiness = DerivedLayerStore._h3_readiness(
                    connection,
                    cursor,
                    installed,
                )
                self.assertEqual(
                    {
                        "method",
                        "ready",
                        "code",
                        "stage",
                        "reasons",
                    },
                    set(readiness),
                )
                self.assertFalse(readiness["ready"])
                self.assertEqual(
                    "derived_layer.h3_not_ready",
                    readiness["code"],
                )
                self.assertEqual(expected_stage, readiness["stage"])
                self.assertEqual(1, len(readiness["reasons"]))
                reason = readiness["reasons"][0]
                self.assertEqual(
                    {"code", "message", "suggestedAction"},
                    set(reason),
                )
                self.assertEqual(expected_reason, reason["code"])
                self.assertNotIn("SECRET", repr(readiness))

        missing_cursor.execute.assert_not_called()
        version_cursor.execute.assert_not_called()

    def test_create_persists_scope_and_keeps_original_query(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [None, None, self.explain_plan()]
        store = self.store_with_cursor(cursor)
        scope = self.spatial_scope()
        stored = {
            **validate_definition(self.valid(spatialScope=scope)),
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "registering",
                "revision": None,
            },
        }
        store._dependencies = MagicMock(
            return_value=stored["sources"],
        )
        store._validate_output = MagicMock(
            return_value={"geometryType": "Polygon", "srid": 3857},
        )
        store.get_in_transaction = MagicMock(return_value=stored)
        store._semantic_fields = MagicMock(return_value=[])
        store._enqueue_semantic_event = MagicMock()

        result = store.create(
            self.valid(spatialScope=scope),
            "token:test",
        )

        create = next(
            call for call in cursor.execute.call_args_list
            if "CREATE VIEW" in str(call.args[0])
        )
        executable = create.args[0].as_string(None)
        self.assertIn("ST_Intersects", executable)
        self.assertIn(self.valid()["query"], executable)
        insert = next(
            call for call in cursor.execute.call_args_list
            if "INSERT INTO" in str(call.args[0])
            and "_definitions" in str(call.args[0])
        )
        self.assertEqual(self.valid()["query"], insert.args[1][2])
        self.assertEqual(scope, insert.args[1][7].obj)
        self.assertEqual(scope, result["spatialScope"])
        self.assertEqual(250, result["queryPlanProbe"]["estimatedFinalRows"])
        self.assertIn(
            "EXPLAIN",
            "\n".join(
                str(call.args[0]) for call in cursor.execute.call_args_list
            ),
        )

    def test_materialized_create_probes_scoped_query_and_returns_probe(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            None,
            self.explain_plan(),
        ]
        store = self.store_with_cursor(cursor)
        payload = self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        )
        stored = {
            **validate_definition(payload),
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "registering",
                "revision": None,
            },
        }
        def dependencies_before_population(_cur, _name):
            statements = "\n".join(
                str(call.args[0]) for call in cursor.execute.call_args_list
            )
            self.assertIn("WITH NO DATA", statements)
            self.assertNotIn("REFRESH MATERIALIZED VIEW", statements)
            return stored["sources"]

        store._dependencies = MagicMock(
            side_effect=dependencies_before_population,
        )
        store._validate_output_metadata = MagicMock(
            return_value={"geometryType": "Polygon", "srid": 3857},
        )
        store._finalize_materialized_output = MagicMock(
            side_effect=lambda _cur, _definition, probe, **_kwargs: {
                **probe,
                "actualBytes": 24_576,
            },
        )
        store.get_in_transaction = MagicMock(return_value=stored)
        store._semantic_fields = MagicMock(return_value=[])
        store._enqueue_semantic_event = MagicMock()

        result = store.create(payload, "token:test")

        probe = result["materializationProbe"]
        self.assertEqual(250, probe["estimatedRows"])
        self.assertEqual(28_800, probe["estimatedBytes"])
        self.assertEqual(24_576, probe["actualBytes"])
        self.assertEqual(
            250,
            result["queryPlanProbe"]["estimatedFinalRows"],
        )
        explain = next(
            call for call in cursor.execute.call_args_list
            if "EXPLAIN (FORMAT JSON)" in str(call.args[0])
        )
        explain_sql = explain.args[0].as_string(None)
        self.assertIn(payload["query"], explain_sql)
        self.assertIn("ST_Intersects", explain_sql)
        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertLess(
            statements.index("EXPLAIN (FORMAT JSON)"),
            statements.index("CREATE MATERIALIZED VIEW"),
        )
        self.assertLess(
            statements.index("CREATE MATERIALIZED VIEW"),
            statements.index("REFRESH MATERIALIZED VIEW"),
        )
        self.assertIn("WITH NO DATA", statements)
        self.assertEqual(1, statements.count("EXPLAIN (FORMAT JSON)"))
        store._validate_catalog_dependencies.assert_called_once()

    def test_catalog_probe_resolves_dependencies_before_explain(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = self.explain_plan()
        store = self.store_with_cursor(cursor)
        store._catalog_query_probe = (
            DerivedLayerStore._catalog_query_probe.__get__(
                store, DerivedLayerStore
            )
        )
        payload = self.valid(spatialScope=self.spatial_scope())
        store._dependencies = MagicMock(
            return_value=sorted(payload["sources"]),
        )

        store.preflight_definition(payload)

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertLess(
            statements.index("SET LOCAL statement_timeout"),
            statements.index("CREATE VIEW"),
        )
        self.assertLess(
            statements.index("CREATE VIEW"),
            statements.index("EXPLAIN (FORMAT JSON)"),
        )
        self.assertLess(
            statements.index("ROLLBACK TO SAVEPOINT derived_catalog_probe"),
            statements.index("EXPLAIN (FORMAT JSON)"),
        )
        store._validate_catalog_dependencies.assert_called_once()

    def test_catalog_probe_approves_qualified_geometry_cast_before_view(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = self.explain_plan()
        store = self.store_with_cursor(cursor)
        store._catalog_query_probe = (
            DerivedLayerStore._catalog_query_probe.__get__(
                store, DerivedLayerStore
            )
        )
        payload = self.valid(
            query=(
                "SELECT cell_id, "
                "geom_3857::public.geometry(Polygon, 3857) AS geom_3857 "
                "FROM leeds.h3_cells"
            ),
            sources=["leeds.h3_cells"],
            spatialScope=self.spatial_scope(),
        )
        store._dependencies = MagicMock(return_value=payload["sources"])

        def approve_qualified_casts(actual_cursor, inspection):
            self.assertIs(cursor, actual_cursor)
            self.assertEqual(
                [("public", "geometry")],
                [
                    (cast_type.schema, cast_type.name)
                    for cast_type in inspection.qualified_cast_types
                ],
            )
            self.assertFalse(any(
                "CREATE VIEW" in str(call.args[0])
                for call in cursor.execute.call_args_list
            ))

        with patch(
            "derived_layers.validate_qualified_cast_types",
            side_effect=approve_qualified_casts,
        ) as validate_casts:
            store.preflight_definition(payload)

        validate_casts.assert_called_once()
        create = next(
            call for call in cursor.execute.call_args_list
            if "CREATE VIEW" in str(call.args[0])
        )
        self.assertIn(
            "public.geometry(Polygon, 3857)",
            create.args[0].as_string(None),
        )

    def test_qualified_cast_catalog_rejection_precedes_view_and_explain(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = self.explain_plan()
        store = self.store_with_cursor(cursor)
        store._catalog_query_probe = (
            DerivedLayerStore._catalog_query_probe.__get__(
                store, DerivedLayerStore
            )
        )
        store._dependencies = MagicMock()
        payload = self.valid(
            query=(
                "SELECT cell_id, "
                "geom_3857::public.geometry(Polygon, 3857) AS geom_3857 "
                "FROM leeds.h3_cells"
            ),
            sources=["leeds.h3_cells"],
            spatialScope=self.spatial_scope(),
        )
        rejection = GuardReason(
            "unapproved_cast_type",
            "Resolved type public.geometry is not owned by PostGIS.",
        )

        with patch(
            "derived_layers.validate_qualified_cast_types",
            side_effect=QueryGuardViolation((rejection,)),
        ):
            with self.assertRaises(DerivedLayerQueryTooExpensive) as raised:
                store.preflight_definition(payload)

        self.assertEqual(
            {"method": "postgresql-catalog-guard"},
            raised.exception.probe,
        )
        self.assertEqual([rejection.as_dict()], raised.exception.reasons)
        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertNotIn("CREATE VIEW", statements)
        self.assertNotIn("EXPLAIN (FORMAT JSON)", statements)
        store._dependencies.assert_not_called()

    def test_catalog_rejection_cannot_reach_explain_or_population(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = self.explain_plan()
        store = self.store_with_cursor(cursor)
        store._catalog_query_probe = (
            DerivedLayerStore._catalog_query_probe.__get__(
                store, DerivedLayerStore
            )
        )
        payload = self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        )
        store._dependencies = MagicMock(
            return_value=sorted(payload["sources"]),
        )
        store._validate_catalog_dependencies.side_effect = (
            DerivedLayerQueryTooExpensive(
                payload["name"],
                {"method": "postgresql-catalog-guard"},
                [{
                    "code": "unapproved_function",
                    "message": "Resolved user wrapper is not approved.",
                }],
            )
        )

        with self.assertRaises(DerivedLayerQueryTooExpensive):
            store.preflight_definition(payload)

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("CREATE VIEW", statements)
        self.assertNotIn("EXPLAIN (FORMAT JSON)", statements)
        self.assertNotIn("REFRESH MATERIALIZED VIEW", statements)

    def test_catalog_probe_rejects_undeclared_relations_before_explain(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = self.explain_plan()
        store = self.store_with_cursor(cursor)
        store._catalog_query_probe = (
            DerivedLayerStore._catalog_query_probe.__get__(
                store, DerivedLayerStore
            )
        )
        payload = self.valid(spatialScope=self.spatial_scope())
        store._dependencies = MagicMock(return_value=["leeds.hidden"])

        with self.assertRaisesRegex(
            DerivedLayerError,
            "Declared sources do not match",
        ):
            store.preflight_definition(payload)

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertNotIn("EXPLAIN (FORMAT JSON)", statements)

    def test_materialized_finalization_indexes_then_checks_total_size(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"actual_bytes": 512 * 1024 ** 2}
        store = self.store_with_cursor(cursor)
        store._validate_output_rows = MagicMock()
        definition = validate_definition(self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        ))

        result = store._finalize_materialized_output(
            cursor,
            definition,
            self.materialization_probe(),
            create_index=True,
            output={"geometryType": "Polygon", "srid": 3857},
        )

        statements = [
            (
                call.args[0]
                if isinstance(call.args[0], str)
                else call.args[0].as_string(None)
            )
            for call in cursor.execute.call_args_list
        ]
        index_position = next(
            index for index, statement in enumerate(statements)
            if "CREATE UNIQUE INDEX" in statement
        )
        size_position = next(
            index for index, statement in enumerate(statements)
            if "pg_total_relation_size" in statement
        )
        self.assertLess(index_position, size_position)
        self.assertIn("NULLS NOT DISTINCT", statements[index_position])
        spatial_indexes = [
            statement for statement in statements
            if "USING gist" in statement
        ]
        self.assertEqual(3, len(spatial_indexes))
        self.assertTrue(any('"geom_3857"' in item for item in spatial_indexes))
        self.assertTrue(any("ST_Transform" in item and "4326" in item
                            for item in spatial_indexes))
        self.assertTrue(any("geography" in item for item in spatial_indexes))
        self.assertTrue(all(
            statements.index(item) < size_position for item in spatial_indexes
        ))
        self.assertEqual(512 * 1024 ** 2, result["actualBytes"])
        self.assertEqual(28_800, result["estimatedBytes"])
        store._validate_output_rows.assert_called_once_with(
            cursor,
            definition,
            duplicates_enforced=True,
        )

    def test_actual_materialized_size_uses_existing_typed_failure(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "actual_bytes": MATERIALIZED_MAX_ESTIMATED_BYTES + 1,
        }
        store = self.store_with_cursor(cursor)
        store._validate_output_rows = MagicMock()
        definition = validate_definition(self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        ))

        with self.assertRaises(DerivedLayerMaterializationTooLarge) as raised:
            store._finalize_materialized_output(
                cursor,
                definition,
                self.materialization_probe(),
                create_index=True,
                output={"geometryType": "Polygon", "srid": 3857},
            )

        self.assertEqual("paths_h3_r9", raised.exception.name)
        self.assertEqual(
            MATERIALIZED_MAX_ESTIMATED_BYTES + 1,
            raised.exception.probe["actualBytes"],
        )
        self.assertEqual(28_800, raised.exception.probe["estimatedBytes"])
        self.assertIn("after population and indexing", str(raised.exception))

    def test_materialized_spatial_indexes_cover_4326_3857_and_geography(self):
        cursor = MagicMock()
        definition = validate_definition(self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        ))

        index_names = DerivedLayerStore._create_materialized_spatial_indexes(
            cursor,
            definition,
            4326,
        )

        statements = [
            call.args[0].as_string(None)
            for call in cursor.execute.call_args_list
        ]
        self.assertEqual(3, len(index_names))
        self.assertTrue(any('gist ("geom_3857")' in item for item in statements))
        self.assertTrue(any(
            'ST_Transform("geom_3857", 3857)' in item
            for item in statements
        ))
        self.assertTrue(any(
            '"geom_3857"::public.geography' in item
            for item in statements
        ))

    def test_projected_materialized_geometry_transforms_before_geography_cast(self):
        cursor = MagicMock()
        definition = validate_definition(self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        ))

        DerivedLayerStore._create_materialized_spatial_indexes(
            cursor,
            definition,
            3857,
        )

        statements = "\n".join(
            call.args[0].as_string(None)
            for call in cursor.execute.call_args_list
        )
        self.assertIn('ST_Transform("geom_3857", 4326)', statements)
        self.assertIn(
            'ST_Transform("geom_3857", 4326)::public.geography',
            statements,
        )
        self.assertNotIn('"geom_3857"::public.geography', statements)

    def test_replacement_renames_every_materialized_spatial_index(self):
        cursor = MagicMock()
        temporary = {
            **validate_definition(self.valid(
                kind="materialized",
                spatialScope=self.spatial_scope(),
            )),
            "name": "swap_0123456789abcdef0123",
        }

        DerivedLayerStore._rename_materialized_spatial_indexes(
            cursor,
            temporary,
            "paths_h3_r9",
            3857,
        )

        statements = [
            call.args[0].as_string(None)
            for call in cursor.execute.call_args_list
        ]
        self.assertEqual(3, len(statements))
        self.assertTrue(all(
            item.startswith('ALTER INDEX "derived_layers".')
            for item in statements
        ))
        self.assertTrue(all("RENAME TO" in item for item in statements))
        self.assertTrue(all("swap_0123456789abcd" in item for item in statements))

    def test_materialized_duplicate_index_failure_uses_id_error(self):
        cursor = MagicMock()
        cursor.execute.side_effect = psycopg.errors.UniqueViolation()
        definition = validate_definition(self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        ))

        with self.assertRaisesRegex(
            DerivedLayerError,
            "unique value for every row",
        ):
            DerivedLayerStore._create_materialized_id_index(
                cursor,
                definition,
            )

    def test_output_metadata_accepts_explicit_geometry_typmod(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "geometry_type": "Polygon",
            "srid": 3857,
        }
        definition = validate_definition(self.valid(
            spatialScope=self.spatial_scope(),
        ))

        output = DerivedLayerStore._validate_output_metadata(
            cursor,
            definition,
        )

        self.assertEqual(
            {"geometryType": "Polygon", "srid": 3857},
            output,
        )

    def test_output_metadata_rejects_generic_geometry_without_srid(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "geometry_type": "Geometry",
            "srid": 0,
        }
        definition = validate_definition(self.valid(
            spatialScope=self.spatial_scope(),
        ))

        with self.assertRaisesRegex(
            DerivedLayerError,
            "PostGIS geometry with a known coordinate system",
        ):
            DerivedLayerStore._validate_output_metadata(cursor, definition)

    def test_output_validation_bounds_one_scan_and_skips_materialized_group(self):
        definition = validate_definition(self.valid(
            spatialScope=self.spatial_scope(),
        ))
        for duplicates_enforced in (False, True):
            with self.subTest(duplicates_enforced=duplicates_enforced):
                cursor = MagicMock()
                cursor.fetchone.return_value = {
                    "invalid_id": False,
                    "has_geometry": True,
                }

                DerivedLayerStore._validate_output_rows(
                    cursor,
                    definition,
                    duplicates_enforced=duplicates_enforced,
                )

                statements = "\n".join(
                    str(call.args[0])
                    for call in cursor.execute.call_args_list
                )
                self.assertIn(
                    "SET LOCAL statement_timeout = '2min'",
                    statements,
                )
                self.assertIn(
                    "SET LOCAL statement_timeout = '30min'",
                    statements,
                )
                if duplicates_enforced:
                    self.assertNotIn("GROUP BY", statements)
                    self.assertIn("IS NULL", statements)
                else:
                    self.assertEqual(1, statements.count("GROUP BY"))
                    self.assertIn("_mapp_has_geometry", statements)

    def test_materialized_single_null_id_is_rejected_after_index(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "invalid_id": True,
            "has_geometry": True,
        }
        definition = validate_definition(self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        ))

        with self.assertRaisesRegex(
            DerivedLayerError,
            "cannot contain empty values",
        ):
            DerivedLayerStore._validate_output_rows(
                cursor,
                definition,
                duplicates_enforced=True,
            )

    def test_oversized_materialized_create_is_blocked_before_ddl(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            None,
            {
                **self.explain_plan(rows=5_000_000, width=200),
            },
        ]
        store = self.store_with_cursor(cursor)
        payload = self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        )

        with self.assertRaises(DerivedLayerMaterializationTooLarge) as raised:
            store.create(payload, "token:test")

        self.assertEqual("paths_h3_r9", raised.exception.name)
        self.assertGreater(
            raised.exception.probe["estimatedBytes"],
            MATERIALIZED_MAX_ESTIMATED_BYTES,
        )
        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("EXPLAIN (FORMAT JSON)", statements)
        self.assertNotIn("CREATE MATERIALIZED VIEW", statements)
        self.assertNotIn("INSERT INTO", statements)

    def test_actual_oversized_create_is_blocked_before_definition_insert(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            None,
            self.explain_plan(),
        ]
        store = self.store_with_cursor(cursor)
        payload = self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        )
        store._dependencies = MagicMock(
            return_value=validate_definition(payload)["sources"],
        )
        store._validate_output_metadata = MagicMock(
            return_value={"geometryType": "Polygon", "srid": 3857},
        )
        actual_probe = self.materialization_probe(
            actualBytes=MATERIALIZED_MAX_ESTIMATED_BYTES + 1,
        )
        store._finalize_materialized_output = MagicMock(
            side_effect=DerivedLayerMaterializationTooLarge(
                "paths_h3_r9",
                actual_probe,
            ),
        )

        with self.assertRaises(DerivedLayerMaterializationTooLarge):
            store.create(payload, "token:test")

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("WITH NO DATA", statements)
        self.assertIn("REFRESH MATERIALIZED VIEW", statements)
        self.assertNotIn("INSERT INTO", statements)

    def test_view_preflight_runs_compute_probe_without_size_probe(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = self.explain_plan(rows=9, width=40)
        store = self.store_with_cursor(cursor)

        result = store.preflight_definition(self.valid(
            spatialScope=self.spatial_scope(),
        ))

        self.assertEqual(9, result["queryPlanProbe"]["estimatedFinalRows"])
        self.assertNotIn("materializationProbe", result)
        self.assertIn("EXPLAIN (FORMAT JSON)", str(
            cursor.execute.call_args_list[-1].args[0]
        ))

    def test_replace_requires_a_resolved_scope_before_ddl(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        store = self.store_with_cursor(cursor)
        current = {
            **validate_definition(self.valid(
                spatialScope=self.spatial_scope(),
            )),
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "ready",
                "revision": "12",
            },
        }
        replacement = {
            **validate_definition(self.valid()),
            "semanticProfile": {
                **current["semanticProfile"],
                "generation": 2,
                "status": "registering",
                "revision": None,
            },
        }
        store.get_in_transaction = MagicMock(
            side_effect=[current, replacement],
        )
        store._incoming_dependents = MagicMock(return_value=[])
        store._dependent_columns = MagicMock(return_value=[])
        store._column_names = MagicMock(
            side_effect=[
                ["cell_id", "geom_3857"],
                ["cell_id", "geom_3857"],
            ],
        )
        store._column_types = MagicMock(
            side_effect=[
                {"cell_id": "text", "geom_3857": "geometry(Polygon,3857)"},
                {"cell_id": "text", "geom_3857": "geometry(Polygon,3857)"},
            ],
        )
        store._dependencies = MagicMock(
            return_value=replacement["sources"],
        )
        store._validate_output = MagicMock(
            return_value={"geometryType": "Polygon", "srid": 3857},
        )
        store._semantic_fields = MagicMock(return_value=[])
        store._enqueue_semantic_event = MagicMock()

        with self.assertRaisesRegex(
            DerivedLayerError,
            "server-resolved workspace map extent",
        ):
            store.replace(
                "paths_h3_r9",
                self.valid(),
                "token:test",
            )

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertNotIn("CREATE VIEW", statements)
        self.assertNotIn("CREATE MATERIALIZED VIEW", statements)

    def test_oversized_materialized_replace_keeps_existing_relation(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            {
                **self.explain_plan(rows=5_000_000, width=200),
            },
        ]
        store = self.store_with_cursor(cursor)
        current = {
            **validate_definition(self.valid(
                spatialScope=self.spatial_scope(),
            )),
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "ready",
                "revision": "12",
            },
        }
        store.get_in_transaction = MagicMock(return_value=current)

        with self.assertRaises(DerivedLayerMaterializationTooLarge):
            store.replace(
                "paths_h3_r9",
                self.valid(
                    kind="materialized",
                    spatialScope=self.spatial_scope(),
                ),
                "token:test",
            )

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertNotIn("CREATE MATERIALIZED VIEW", statements)
        self.assertNotIn("DROP VIEW", statements)
        self.assertNotIn("DROP MATERIALIZED VIEW", statements)

    def test_actual_oversized_replace_keeps_existing_relation(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [None, self.explain_plan()]
        store = self.store_with_cursor(cursor)
        payload = self.valid(
            kind="materialized",
            spatialScope=self.spatial_scope(),
        )
        current = {
            **validate_definition(self.valid(
                spatialScope=self.spatial_scope(),
            )),
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "ready",
                "revision": "12",
            },
        }
        store.get_in_transaction = MagicMock(return_value=current)
        store._incoming_dependents = MagicMock(return_value=[])
        store._dependent_columns = MagicMock(return_value=[])
        store._column_names = MagicMock(
            side_effect=[
                ["cell_id", "geom_3857"],
                ["cell_id", "geom_3857"],
            ],
        )
        column_types = {
            "cell_id": "text",
            "geom_3857": "geometry(Polygon,3857)",
        }
        store._column_types = MagicMock(
            side_effect=[column_types, column_types],
        )
        store._dependencies = MagicMock(
            return_value=validate_definition(payload)["sources"],
        )
        store._validate_output_metadata = MagicMock(
            return_value={"geometryType": "Polygon", "srid": 3857},
        )
        store._finalize_materialized_output = MagicMock(
            side_effect=DerivedLayerMaterializationTooLarge(
                "paths_h3_r9",
                self.materialization_probe(
                    actualBytes=MATERIALIZED_MAX_ESTIMATED_BYTES + 1,
                ),
            ),
        )

        with self.assertRaises(DerivedLayerMaterializationTooLarge):
            store.replace("paths_h3_r9", payload, "token:test")

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("WITH NO DATA", statements)
        self.assertIn("REFRESH MATERIALIZED VIEW", statements)
        self.assertNotIn("DROP VIEW", statements)
        self.assertNotIn("DROP MATERIALIZED VIEW", statements)

    def test_oversized_materialized_refresh_is_blocked_before_refresh(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            {
                **self.explain_plan(rows=5_000_000, width=200),
            },
        ]
        store = self.store_with_cursor(cursor)
        definition = {
            **validate_definition(self.valid(
                kind="materialized",
                spatialScope=self.spatial_scope(),
            )),
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "ready",
                "revision": "12",
            },
        }
        store.get_in_transaction = MagicMock(return_value=definition)

        with self.assertRaises(DerivedLayerMaterializationTooLarge):
            store.refresh("paths_h3_r9", "token:test")

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("EXPLAIN (FORMAT JSON)", statements)
        self.assertNotIn("REFRESH MATERIALIZED VIEW", statements)
        self.assertNotIn("semantic_generation =", statements)

    def test_actual_oversized_refresh_rolls_back_before_metadata_update(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [None, self.explain_plan()]
        store = self.store_with_cursor(cursor)
        definition = {
            **validate_definition(self.valid(
                kind="materialized",
                spatialScope=self.spatial_scope(),
            )),
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "ready",
                "revision": "12",
            },
        }
        store.get_in_transaction = MagicMock(return_value=definition)
        store._dependencies = MagicMock(return_value=definition["sources"])
        store._validate_output_metadata = MagicMock(return_value={
            "geometryType": "Polygon",
            "srid": 3857,
        })
        store._finalize_materialized_output = MagicMock(
            side_effect=DerivedLayerMaterializationTooLarge(
                "paths_h3_r9",
                self.materialization_probe(
                    actualBytes=MATERIALIZED_MAX_ESTIMATED_BYTES + 1,
                ),
            ),
        )

        with self.assertRaises(DerivedLayerMaterializationTooLarge):
            store.refresh("paths_h3_r9", "token:test")

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("SET LOCAL lock_timeout = '5s'", statements)
        self.assertIn("REFRESH MATERIALIZED VIEW", statements)
        self.assertNotIn("semantic_generation =", statements)

    def test_refresh_dependency_mismatch_is_blocked_before_population(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [None, self.explain_plan()]
        store = self.store_with_cursor(cursor)
        definition = {
            **validate_definition(self.valid(
                kind="materialized",
                spatialScope=self.spatial_scope(),
            )),
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "ready",
                "revision": "12",
            },
        }
        store.get_in_transaction = MagicMock(return_value=definition)
        store._dependencies = MagicMock(return_value=["leeds.other_source"])

        with self.assertRaisesRegex(
            DerivedLayerError,
            "Declared sources do not match",
        ):
            store.refresh("paths_h3_r9", "token:test")

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertNotIn("REFRESH MATERIALIZED VIEW", statements)

    def test_refresh_preflight_uses_the_stored_scoped_definition(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = self.explain_plan(rows=10, width=8)
        store = self.store_with_cursor(cursor)
        definition = {
            **validate_definition(self.valid(
                kind="materialized",
                spatialScope=self.spatial_scope(),
            )),
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "ready",
                "revision": "12",
            },
        }
        store.get_in_transaction = MagicMock(return_value=definition)

        probe = store.preflight_refresh("paths_h3_r9")

        self.assertEqual(480, probe["materializationProbe"]["estimatedBytes"])
        self.assertEqual(10, probe["queryPlanProbe"]["estimatedFinalRows"])
        explain = next(
            call for call in cursor.execute.call_args_list
            if "EXPLAIN (FORMAT JSON)" in str(call.args[0])
        )
        self.assertIn("ST_Intersects", explain.args[0].as_string(None))

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

    def test_semantic_event_contains_safe_ordered_metadata(self):
        created = datetime(2026, 7, 26, 10, 30, tzinfo=timezone.utc)
        definition = {
            **validate_definition(self.valid(
                description="Path cells",
                spatialScope=self.spatial_scope(),
            )),
            "createdAt": created,
            "createdBy": "token:author",
            "refreshedAt": None,
            "semanticProfile": {
                "assetId": str(uuid.uuid4()),
                "generation": 1,
                "status": "registering",
                "revision": None,
            },
        }
        fields = [
            {
                "name": "cell_id",
                "type": "bigint",
                "nullable": False,
                "geometryType": "",
                "srid": None,
            },
            {
                "name": "geom_3857",
                "type": "geometry(Polygon,3857)",
                "nullable": True,
                "geometryType": "Polygon",
                "srid": 3857,
            },
        ]

        event = DerivedLayerStore._semantic_event(
            definition,
            "register",
            "token:author",
            fields,
            event_id=uuid.UUID("d2bd8c5b-7bbf-4995-8c73-66487508ec64"),
            event_at=created,
        )

        serialized = json.dumps(event, sort_keys=True)
        self.assertNotIn(definition["query"], serialized)
        self.assertNotIn('"query"', serialized)
        self.assertEqual(event["generated"]["name"], "paths_h3_r9")
        self.assertEqual(
            [field["name"] for field in event["generated"]["fields"]],
            ["cell_id", "geom_3857"],
        )
        self.assertEqual(
            event["generated"]["sources"],
            ["leeds.definitive_paths", "leeds.h3_cells"],
        )
        self.assertEqual(event["generated"]["idColumn"], "cell_id")
        self.assertEqual(event["generated"]["geometryColumn"], "geom_3857")
        self.assertEqual(event["generated"]["geometryType"], "Polygon")
        self.assertEqual(event["generated"]["srid"], 3857)
        self.assertEqual(
            self.spatial_scope(),
            event["generated"]["spatialScope"],
        )
        self.assertEqual(event["generated"]["actor"], "token:author")
        self.assertEqual(event["generated"]["createdAt"], created.isoformat())
        self.assertRegex(event["generated"]["definitionDigest"], r"^[0-9a-f]{64}$")
        unhashed = {key: value for key, value in event.items() if key != "payloadHash"}
        self.assertEqual(
            event["payloadHash"],
            hashlib.sha256(
                json.dumps(
                    unhashed,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest(),
        )

    def test_semantic_profile_serializes_uuid_and_revision(self):
        asset_id = uuid.UUID("106fdd47-b4ae-4350-a767-06ca9d887a1d")
        item = DerivedLayerStore._with_semantic_profile({
            "name": "paths_h3_r9",
            "semanticAssetId": asset_id,
            "semanticGeneration": 3,
            "semanticStatus": "ready",
            "semanticRevision": "42",
        })

        self.assertEqual(item["semanticProfile"], {
            "assetId": str(asset_id),
            "generation": 3,
            "status": "ready",
            "revision": "42",
        })
        self.assertNotIn("semanticAssetId", item)

    def test_recovery_registration_names_auditable_predecessor(self):
        predecessor = "b630c2db-6f96-49f8-a190-edabc1fc65c8"
        definition = {
            "name": "paths_h3_r9",
            "kind": "view",
            "query": "SELECT id, geom FROM leeds.paths",
            "sources": ["leeds.paths"],
            "idColumn": "id",
            "geometryColumn": "geom",
            "semanticPredecessorAssetId": predecessor,
            "semanticProfile": {
                "assetId": "106fdd47-b4ae-4350-a767-06ca9d887a1d",
                "generation": 1,
            },
        }

        event = DerivedLayerStore._semantic_event(
            definition,
            "register",
            "system:reset-recovery",
            [],
        )

        self.assertEqual(predecessor, event["predecessorAssetId"])
        self.assertNotIn("visibility", event)
        self.assertEqual(
            event["payloadHash"],
            DerivedLayerStore._payload_hash({
                key: value
                for key, value in event.items()
                if key != "payloadHash"
            }),
        )

    def test_initialization_adds_private_semantic_outbox(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []

        DerivedLayerStore._initialize(cursor)

        statements = "\n".join(str(call.args[0]) for call in cursor.execute.call_args_list)
        self.assertIn("spatial_scope", statements)
        self.assertIn("semantic_asset_id", statements)
        self.assertIn("_semantic_outbox", statements)
        self.assertIn("_maintenance", statements)
        self.assertGreaterEqual(
            sum("REVOKE ALL" in str(call.args[0]) for call in cursor.execute.call_args_list),
            2,
        )

    def test_semantic_events_are_atomically_claimed_and_ordered(self):
        cursor = MagicMock()
        expected = [{
            "eventId": str(uuid.uuid4()),
            "status": "pending",
            "payload": {"type": "register"},
        }]
        cursor.fetchall.return_value = expected
        store = self.store_with_cursor(cursor)

        self.assertEqual(store.claim_semantic_events(25), expected)
        claim_parameters = cursor.execute.call_args.args[1]
        self.assertEqual(25, claim_parameters[0])
        uuid.UUID(claim_parameters[1])
        self.assertEqual(60, claim_parameters[2])
        query = str(cursor.execute.call_args.args[0])
        self.assertIn("UPDATE", query)
        self.assertIn("claimed_until", query)
        self.assertIn("claim_id", query)
        self.assertIn("FOR UPDATE OF event SKIP LOCKED", query)
        self.assertIn("ORDER BY event.created_at, event.event_id", query)
        self.assertIn("NOT EXISTS", query)
        self.assertIn("earlier.generation < event.generation", query)
        self.assertIn("earlier.created_at, earlier.event_id", query)
        self.assertIn("payload #>>", query)
        for invalid_limit in (False, 0, 1001):
            with self.subTest(limit=invalid_limit):
                with self.assertRaises(DerivedLayerError):
                    store.claim_semantic_events(invalid_limit)
        for invalid_lease in (True, 14, 601):
            with self.subTest(lease=invalid_lease):
                with self.assertRaises(DerivedLayerError):
                    store.claim_semantic_events(1, invalid_lease)

    def test_stale_outbox_claim_cannot_change_event_or_profile_state(self):
        event_id = "aab0ec9d-4686-4472-802f-69de0e44394b"
        stale_claim = "cb39b58c-3487-49da-94ce-0a9633cc848a"
        for method_name, trailing_arguments in (
            ("mark_semantic_delivered", (17,)),
            ("mark_semantic_retry", ("semantic service unavailable",)),
            ("mark_semantic_repair", ("invalid semantic event",)),
        ):
            with self.subTest(method=method_name):
                cursor = MagicMock()
                cursor.fetchone.return_value = None
                store = self.store_with_cursor(cursor)

                changed = getattr(store, method_name)(
                    event_id,
                    stale_claim,
                    *trailing_arguments,
                )

                self.assertFalse(changed)
                self.assertEqual(1, cursor.execute.call_count)
                statement = str(cursor.execute.call_args.args[0])
                self.assertIn("claim_id = %s", statement)
                self.assertIn("status IN ('pending', 'retrying')", statement)
                self.assertNotIn("_definitions", statement)

    def test_retry_and_repair_update_the_matching_profile_generation(self):
        asset_id = uuid.UUID("b630c2db-6f96-49f8-a190-edabc1fc65c8")
        claim_id = "cb39b58c-3487-49da-94ce-0a9633cc848a"
        generation = 4
        for method_name, expected_status in (
            ("mark_semantic_retry", "registering"),
            ("mark_semantic_repair", "repair_required"),
        ):
            with self.subTest(method=method_name):
                cursor = MagicMock()
                cursor.fetchone.return_value = {
                    "asset_id": asset_id,
                    "generation": generation,
                    "event_type": "replace",
                }
                store = self.store_with_cursor(cursor)

                changed = getattr(store, method_name)(
                    "aab0ec9d-4686-4472-802f-69de0e44394b",
                    claim_id,
                    "semantic service unavailable",
                )

                self.assertTrue(changed)
                self.assertEqual(cursor.execute.call_count, 2)
                profile_call = cursor.execute.call_args_list[1]
                if method_name == "mark_semantic_retry":
                    self.assertEqual(expected_status, profile_call.args[1][0])
                else:
                    self.assertIn(expected_status, str(profile_call.args[0]))
                self.assertEqual(
                    profile_call.args[1],
                    (
                        ("registering", asset_id, generation)
                        if method_name == "mark_semantic_retry"
                        else (asset_id, generation)
                    ),
                )

    def test_repair_requeues_latest_failed_event_by_derived_name(self):
        asset_id = "b630c2db-6f96-49f8-a190-edabc1fc65c8"
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            {
                "assetId": asset_id,
                "generation": 4,
                "type": "replace",
            },
        ]
        store = self.store_with_cursor(cursor)

        profile = store.repair_semantic_profile("paths_h3_r9")

        self.assertEqual(profile, {
            "name": "paths_h3_r9",
            "assetId": asset_id,
            "generation": 4,
            "status": "registering",
            "revision": None,
            "operation": "replace",
        })
        self.assertEqual(cursor.execute.call_count, 4)
        requeue = cursor.execute.call_args_list[2]
        self.assertIn("status = 'pending'", str(requeue.args[0]))
        self.assertIn("payload #>>", str(requeue.args[0]))
        self.assertEqual(requeue.args[1], ("paths_h3_r9",))

    def test_repair_of_dropped_archive_reports_pending_archive(self):
        asset_id = "b630c2db-6f96-49f8-a190-edabc1fc65c8"
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,
            {
                "assetId": asset_id,
                "generation": 5,
                "type": "archive",
            },
        ]
        store = self.store_with_cursor(cursor)

        profile = store.repair_semantic_profile("already_dropped")

        self.assertEqual("pending_archive", profile["status"])
        self.assertEqual("archive", profile["operation"])
        profile_update = cursor.execute.call_args_list[3]
        self.assertIn("SET semantic_status = %s", str(profile_update.args[0]))
        self.assertEqual(
            ("pending_archive", asset_id, 5),
            profile_update.args[1],
        )

    def test_reset_archive_is_queued_without_dropping_the_relation(self):
        asset_id = "b630c2db-6f96-49f8-a190-edabc1fc65c8"
        cursor = MagicMock()
        cursor.fetchall.return_value = [{"name": "paths_h3_r9"}]
        cursor.fetchone.return_value = None
        store = self.store_with_cursor(cursor)
        ready = {
            "name": "paths_h3_r9",
            "semanticProfile": {
                "assetId": asset_id,
                "generation": 2,
                "status": "ready",
                "revision": "8",
            },
        }
        pending = {
            "name": "paths_h3_r9",
            "semanticProfile": {
                "assetId": asset_id,
                "generation": 3,
                "status": "pending_archive",
                "revision": None,
            },
        }
        store.get_in_transaction = MagicMock(side_effect=[ready, pending])
        store._semantic_fields = MagicMock(return_value=[])
        store._enqueue_semantic_event = MagicMock()

        profiles = store.queue_semantic_archives("system:reset-data")

        self.assertEqual(profiles[0]["status"], "pending_archive")
        store._enqueue_semantic_event.assert_called_once_with(
            cursor,
            pending,
            "archive",
            "system:reset-data",
            [],
        )
        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("semantic_generation = semantic_generation + 1", statements)
        self.assertNotIn("DROP VIEW", statements)
        self.assertNotIn("DROP MATERIALIZED VIEW", statements)

    def test_reset_gate_is_durable_and_serializes_reset_attempts(self):
        reset_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        store = self.store_with_cursor(cursor)
        self.assertIsNone(
            store.begin_semantic_reset(
                "system:reset-data",
                reset_owner,
            )
        )

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("pg_advisory_xact_lock", statements)
        self.assertIn("INSERT INTO", statements)
        self.assertIn("_maintenance", statements)
        insert = next(
            call
            for call in cursor.execute.call_args_list
            if "INSERT INTO" in str(call.args[0])
        )
        self.assertEqual(
            ("system:reset-data", reset_owner),
            insert.args[1],
        )

        concurrent_cursor = MagicMock()
        concurrent_cursor.fetchone.return_value = {
            "reset_owner": reset_owner,
        }
        concurrent = self.store_with_cursor(concurrent_cursor)
        with self.assertRaisesRegex(
            DerivedLayerError,
            "already in progress",
        ):
            concurrent.begin_semantic_reset(
                "system:reset-data",
                "ae7846cd-594b-4a42-91b7-06f533906b43",
            )
        blocked = MagicMock()
        blocked.fetchone.return_value = {"operation": "reset-data"}
        with self.assertRaisesRegex(
            DerivedLayerError,
            "paused while reset-data",
        ):
            store._ensure_changes_allowed(blocked)

        mutation_cursor = MagicMock()
        mutation_cursor.fetchone.return_value = {
            "operation": "reset-data",
        }
        mutation = self.store_with_cursor(mutation_cursor)
        with self.assertRaisesRegex(
            DerivedLayerError,
            "paused while reset-data",
        ):
            mutation.create(self.valid(), "token:test")
        mutation_statements = "\n".join(
            str(call.args[0])
            for call in mutation_cursor.execute.call_args_list
        )
        self.assertNotIn("CREATE VIEW", mutation_statements)
        self.assertNotIn("CREATE MATERIALIZED VIEW", mutation_statements)

    def test_reset_owner_must_be_a_canonical_uuid(self):
        for owner in (
            "",
            "not-a-uuid",
            "289D495D-6642-4525-8A63-BB5E4F0C764C",
            "{289d495d-6642-4525-8a63-bb5e4f0c764c}",
        ):
            with self.subTest(owner=owner):
                with self.assertRaisesRegex(
                    DerivedLayerError,
                    "canonical UUID",
                ):
                    DerivedLayerStore._validated_reset_owner(owner)

        self.assertEqual(
            "289d495d-6642-4525-8a63-bb5e4f0c764c",
            DerivedLayerStore._validated_reset_owner(
                "289d495d-6642-4525-8a63-bb5e4f0c764c"
            ),
        )

    def test_outbox_blockers_include_events_for_deleted_relations(self):
        blocker = {
            "eventId": "aab0ec9d-4686-4472-802f-69de0e44394b",
            "assetId": "b630c2db-6f96-49f8-a190-edabc1fc65c8",
            "type": "archive",
            "generation": 3,
            "status": "repair_required",
            "name": "deleted_layer",
            "lastError": "asset not found",
        }
        cursor = MagicMock()
        cursor.fetchall.return_value = [blocker]
        store = self.store_with_cursor(cursor)

        self.assertEqual([blocker], store.semantic_outbox_blockers())
        statement = str(cursor.execute.call_args.args[0])
        self.assertIn("status <> 'delivered'", statement)
        self.assertIn("payload #>>", statement)

    def test_derived_profile_page_pushes_keyset_and_limit_to_postgresql(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        store = self.store_with_cursor(cursor)

        self.assertEqual(
            [],
            store.list_page(after_name="places", fetch_limit=3),
        )
        statement, values = cursor.execute.call_args.args
        self.assertIn("WHERE name > %s", str(statement))
        self.assertIn("ORDER BY name", str(statement))
        self.assertIn("LIMIT %s", str(statement))
        self.assertEqual(("places", 3), values)

    def test_profile_blockers_query_only_page_names_and_tombstones(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        store = self.store_with_cursor(cursor)

        self.assertEqual(
            [],
            store.semantic_outbox_blockers(profile_names=["places"]),
        )
        statement, values = cursor.execute.call_args.args
        self.assertIn("= ANY(%s)", str(statement))
        self.assertIn("NOT EXISTS", str(statement))
        self.assertEqual((["places"],), values)

    def test_profile_and_unmatched_blocker_queries_are_independently_bounded(
        self,
    ):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        store = self.store_with_cursor(cursor)

        self.assertEqual(
            [],
            store.semantic_outbox_blockers(
                profile_names=["places", "roads"],
                include_unmatched=False,
                one_per_profile=True,
                fetch_limit=2,
            ),
        )
        statement, values = cursor.execute.call_args.args
        rendered = str(statement)
        self.assertIn("DISTINCT ON", rendered)
        self.assertIn("= ANY(%s)", rendered)
        self.assertNotIn("NOT EXISTS", rendered)
        self.assertIn("LIMIT %s", rendered)
        self.assertEqual((["places", "roads"], 2), values)

        self.assertEqual(
            [],
            store.semantic_outbox_blockers(
                unmatched_only=True,
                fetch_limit=101,
            ),
        )
        statement, values = cursor.execute.call_args.args
        rendered = str(statement)
        self.assertNotIn("DISTINCT ON", rendered)
        self.assertIn("NOT EXISTS", rendered)
        self.assertIn("LIMIT %s", rendered)
        self.assertEqual((101,), values)

    def test_interrupted_reset_rebinds_to_a_new_semantic_asset(self):
        old_asset = "b630c2db-6f96-49f8-a190-edabc1fc65c8"
        reset_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [{
                "name": "paths_h3_r9",
                "predecessorAssetId": old_asset,
            }],
            [],
            [],
        ]
        cursor.fetchone.side_effect = [
            {"reset_owner": uuid.UUID(reset_owner)},
            {"name": "paths_h3_r9"},
        ]
        store = self.store_with_cursor(cursor)
        definition = {
            "name": "paths_h3_r9",
            "semanticProfile": {
                "assetId": "0e514559-3665-461d-b3e0-a3930b279870",
                "generation": 1,
                "status": "registering",
                "revision": None,
            },
        }
        store.get_in_transaction = MagicMock(return_value=definition)
        store._semantic_fields = MagicMock(return_value=[])
        store._enqueue_semantic_event = MagicMock()

        recovery = store.recover_reset_semantic_profiles(
            "system:reset-recovery",
            reset_owner,
        )

        self.assertIsNotNone(recovery)
        self.assertEqual(reset_owner, recovery["resetOwner"])
        profiles = recovery["profiles"]
        self.assertEqual("registering", profiles[0]["status"])
        store._enqueue_semantic_event.assert_called_once_with(
            cursor,
            definition,
            "register",
            "system:reset-recovery",
            [],
        )
        self.assertEqual(
            old_asset,
            definition["semanticPredecessorAssetId"],
        )
        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("'pending_archive', 'archived'", statements)
        self.assertIn("semantic_asset_id =", statements)
        self.assertNotIn("DELETE FROM", statements)
        self.assertIn("_maintenance", statements)
        update_parameters = [
            call.args[1]
            for call in cursor.execute.call_args_list
            if len(call.args) > 1
            and isinstance(call.args[1], tuple)
            and len(call.args[1]) == 2
        ]
        rebound_asset = next(
            parameters[0]
            for parameters in update_parameters
            if parameters[1] == "paths_h3_r9"
        )
        self.assertNotEqual(old_asset, rebound_asset)
        uuid.UUID(rebound_asset)

    def test_reset_recovery_requeues_its_exact_successor_repair_event(self):
        reset_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        event_id = "b3be9a39-8328-491e-89e8-d27e64dcc044"
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "reset_owner": uuid.UUID(reset_owner),
        }
        cursor.fetchall.side_effect = [
            [],
            [],
            [{
                "eventId": event_id,
                "name": "paths_h3_r9",
            }],
        ]
        store = self.store_with_cursor(cursor)
        definition = {
            "name": "paths_h3_r9",
            "semanticProfile": {
                "assetId": "0e514559-3665-461d-b3e0-a3930b279870",
                "generation": 1,
                "status": "registering",
                "revision": None,
            },
        }
        store.get_in_transaction = MagicMock(return_value=definition)

        recovery = store.recover_reset_semantic_profiles(
            "system:reset-recovery",
            reset_owner,
        )

        profiles = recovery["profiles"]
        self.assertEqual([{
            "name": "paths_h3_r9",
            **definition["semanticProfile"],
        }], profiles)
        event_update = next(
            call
            for call in cursor.execute.call_args_list
            if "SET status = 'pending'" in str(call.args[0])
            and "WHERE event_id = %s" in str(call.args[0])
        )
        self.assertEqual((event_id,), event_update.args[1])
        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("'system:reset-recovery'", statements)
        self.assertNotIn("DELETE FROM", statements)

    def test_repeated_recovery_requeues_the_failed_predecessor_archive(self):
        reset_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        event_id = "b3be9a39-8328-491e-89e8-d27e64dcc044"
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "reset_owner": uuid.UUID(reset_owner),
        }
        cursor.fetchall.side_effect = [
            [],
            [{
                "eventId": event_id,
                "name": "paths_h3_r9",
            }],
            [],
        ]
        store = self.store_with_cursor(cursor)
        definition = {
            "name": "paths_h3_r9",
            "semanticProfile": {
                "assetId": "0e514559-3665-461d-b3e0-a3930b279870",
                "generation": 1,
                "status": "registering",
                "revision": None,
            },
        }
        store.get_in_transaction = MagicMock(return_value=definition)

        recovery = store.recover_reset_semantic_profiles(
            "system:reset-recovery",
            reset_owner,
        )

        self.assertEqual([{
            "name": "paths_h3_r9",
            **definition["semanticProfile"],
        }], recovery["profiles"])
        event_update = next(
            call
            for call in cursor.execute.call_args_list
            if "SET status = 'pending'" in str(call.args[0])
            and "WHERE event_id = %s" in str(call.args[0])
        )
        self.assertEqual((event_id,), event_update.args[1])
        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertIn("successor.payload ->> 'predecessorAssetId'", statements)
        self.assertIn("'system:reset-data'", statements)
        self.assertNotIn("DELETE FROM", statements)

    def test_reset_recovery_gate_closes_only_after_successors_are_ready(self):
        reset_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        blocked_cursor = MagicMock()
        blocked_cursor.fetchone.return_value = {
            "reset_owner": uuid.UUID(reset_owner),
        }
        blocked_cursor.fetchall.return_value = [{"name": "paths_h3_r9"}]
        blocked = self.store_with_cursor(blocked_cursor)

        with self.assertRaisesRegex(
            DerivedLayerMaintenanceError,
            "still waiting for: paths_h3_r9",
        ):
            blocked.complete_reset_semantic_recovery(reset_owner)

        blocked_statements = "\n".join(
            str(call.args[0])
            for call in blocked_cursor.execute.call_args_list
        )
        self.assertNotIn("DELETE FROM", blocked_statements)

        replacement_owner = "ae7846cd-594b-4a42-91b7-06f533906b43"
        replacement_cursor = MagicMock()
        replacement_cursor.fetchone.return_value = {
            "reset_owner": uuid.UUID(replacement_owner),
        }
        replacement = self.store_with_cursor(replacement_cursor)
        with self.assertRaisesRegex(
            DerivedLayerResetOwnershipError,
            "another reset operation",
        ):
            replacement.complete_reset_semantic_recovery(reset_owner)
        replacement_statements = "\n".join(
            str(call.args[0])
            for call in replacement_cursor.execute.call_args_list
        )
        self.assertNotIn("DELETE FROM", replacement_statements)

        ready_cursor = MagicMock()
        ready_cursor.fetchone.side_effect = [
            {"reset_owner": uuid.UUID(reset_owner)},
            {"reset_owner": uuid.UUID(reset_owner)},
        ]
        ready_cursor.fetchall.return_value = []
        ready = self.store_with_cursor(ready_cursor)

        self.assertTrue(
            ready.complete_reset_semantic_recovery(reset_owner)
        )
        ready_statements = "\n".join(
            str(call.args[0])
            for call in ready_cursor.execute.call_args_list
        )
        self.assertIn("semantic_status <> 'ready'", ready_statements)
        self.assertIn("blocker.status <> 'delivered'", ready_statements)
        self.assertIn("DELETE FROM", ready_statements)

    def test_losing_reset_cannot_recover_the_winning_reset_gate(self):
        winning_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        losing_owner = "ae7846cd-594b-4a42-91b7-06f533906b43"
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "reset_owner": uuid.UUID(winning_owner),
        }
        store = self.store_with_cursor(cursor)
        store._enqueue_semantic_event = MagicMock()

        with self.assertRaisesRegex(
            DerivedLayerResetOwnershipError,
            "another reset operation",
        ):
            store.recover_reset_semantic_profiles(
                "system:reset-recovery",
                losing_owner,
            )

        statements = "\n".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        self.assertNotIn("semantic_asset_id =", statements)
        self.assertNotIn("DELETE FROM", statements)
        store._enqueue_semantic_event.assert_not_called()

    def test_delivered_event_and_ready_profile_record_revision(self):
        event_id = "aab0ec9d-4686-4472-802f-69de0e44394b"
        asset_id = "b630c2db-6f96-49f8-a190-edabc1fc65c8"

        delivered_cursor = MagicMock()
        delivered_cursor.fetchone.return_value = {
            "asset_id": asset_id,
            "generation": 3,
            "event_type": "register",
        }
        delivered = self.store_with_cursor(delivered_cursor)
        claim_id = "cb39b58c-3487-49da-94ce-0a9633cc848a"
        self.assertTrue(
            delivered.mark_semantic_delivered(event_id, claim_id, 17)
        )
        self.assertEqual(
            delivered_cursor.execute.call_args_list[0].args[1],
            ("17", event_id, claim_id),
        )
        self.assertEqual(
            delivered_cursor.execute.call_args_list[1].args[1],
            ("ready", "17", asset_id, 3),
        )


if __name__ == "__main__":
    unittest.main()
