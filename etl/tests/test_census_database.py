from __future__ import annotations

import os
import unittest
import uuid
from types import SimpleNamespace
from typing import Any

from psycopg import connect, sql

from leeds_arcgis_etl.census_database import (
    ABANDONED_RUN_ERROR,
    OA_CODE_COMMENT,
    TABLE_COMMENT,
    CensusCodeSetError,
    CensusDatabaseError,
    CensusDatasetMetadata,
    CensusPostgresStore,
    CensusVariableMetadata,
)
from leeds_arcgis_etl.config import ColumnConfig, LayerConfig
from leeds_arcgis_etl.core import PreparedFeature


def sample_config() -> Any:
    geometry = LayerConfig(
        key="census_2021_oa_geometry",
        description="OA geometry",
        source_url="https://example.test/FeatureServer/0",
        target_table="census_2021_england_oa",
        where="OA21CD LIKE 'E%'",
        object_id_field="FID",
        source_geometry_type="esriGeometryPolygon",
        target_geometry_type="MultiPolygon",
        expected_source_srid=27700,
        columns=(ColumnConfig("OA21CD", "oa21cd", "text"),),
        minimum_source_count=2,
    )
    topics = (
        SimpleNamespace(target_columns=("ts001_0001", "ts001_0002")),
        SimpleNamespace(target_columns=("ts002_0001",)),
    )
    return SimpleNamespace(
        target_schema="leeds",
        target_table="census_2021_england_oa",
        geometry_layer=geometry,
        topics=topics,
    )


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rowcount = connection.default_rowcount

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def execute(self, statement: Any, params: Any = None) -> None:
        rendered = statement if isinstance(statement, str) else statement.as_string()
        self.connection.statements.append((rendered, params))
        self.rowcount = self.connection.default_rowcount
        if self.connection.fail_on and self.connection.fail_on in rendered:
            raise RuntimeError("injected SQL failure")

    def executemany(self, statement: Any, rows: Any) -> None:
        rendered = statement.as_string()
        materialized = list(rows)
        self.connection.statements.append((rendered, materialized))
        self.rowcount = len(materialized)
        if self.connection.fail_on and self.connection.fail_on in rendered:
            raise RuntimeError("injected SQL failure")

    def copy(self, statement: Any) -> "FakeCopy":
        rendered = statement.as_string()
        self.connection.statements.append((rendered, self.connection.copy_rows))
        return FakeCopy(self.connection)

    def fetchone(self) -> Any:
        if self.connection.responses:
            return self.connection.responses.pop(0)
        return (True,)

    def fetchall(self) -> list[tuple[str, str, str, bool, bool]]:
        return self.connection.schema_rows


class FakeCopy:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "FakeCopy":
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def write_row(self, row: Any) -> None:
        self.connection.copy_rows.append(tuple(row))


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.responses: list[Any] = []
        self.default_rowcount = 2
        self.fail_on: str | None = None
        self.commits = 0
        self.rollbacks = 0
        self.copy_rows: list[tuple[Any, ...]] = []
        self.schema_rows: list[tuple[str, str, str, bool, bool]] = [
            ("oa21cd", "text", "", True, True),
            ("ts001_0001", "double precision", "", False, False),
            ("ts001_0002", "double precision", "", False, False),
            ("ts002_0001", "double precision", "", False, False),
            ("geom", "geometry(MultiPolygon,4326)", "", True, False),
            ("geom_3857", "geometry(MultiPolygon,3857)", "s", False, False),
        ]

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class CensusDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = FakeConnection()
        self.store = CensusPostgresStore(
            self.connection, sample_config()  # type: ignore[arg-type]
        )

    def rendered_sql(self) -> str:
        return "\n".join(statement for statement, _ in self.connection.statements)

    def test_initialization_creates_stable_wide_table_metadata_and_indexes(self) -> None:
        self.store.initialize()
        ddl = self.rendered_sql()

        self.assertIn("SELECT PostGIS_Version()", ddl)
        self.assertIn("has_schema_privilege", ddl)
        self.assertIn('"leeds"."census_2021_england_oa"', ddl)
        self.assertIn("oa21cd text PRIMARY KEY", ddl)
        self.assertIn('"ts001_0001" double precision', ddl)
        self.assertIn("geometry(MultiPolygon, 4326) NOT NULL", ddl)
        self.assertIn("ST_Transform(geom, 3857)", ddl)
        self.assertIn("USING gist (geom)", ddl)
        self.assertIn("USING gist (geom_3857)", ddl)
        self.assertIn('"leeds"."census_datasets"', ddl)
        self.assertIn('"leeds"."census_variables"', ddl)
        self.assertIn('"leeds"."_census_etl_runs"', ddl)
        self.assertIn("geometry_repairs integer NOT NULL", ddl)
        self.assertIn(
            'COMMENT ON TABLE "leeds"."census_2021_england_oa" IS '
            "'ONS Census 2021 summary statistics",
            ddl,
        )
        for column_name, comment in (
            ("oa21cd", "Official 2021 Output Area code"),
            ("geom", "ONS Output Areas (December 2021)"),
            ("geom_3857", "Stored generated EPSG:3857 transform"),
        ):
            self.assertIn(
                f'COMMENT ON COLUMN "leeds"."census_2021_england_oa".'
                f'"{column_name}" IS \'{comment}',
                ddl,
            )
        self.assertNotIn("GRANT ", ddl)

    def test_initialization_reports_missing_postgis_concisely(self) -> None:
        self.connection.fail_on = "PostGIS_Version"

        with self.assertRaisesRegex(
            CensusDatabaseError,
            "PostGIS is not installed or is not visible",
        ):
            self.store.initialize()

        self.assertEqual(self.connection.rollbacks, 1)
        self.assertNotIn("CREATE TABLE", self.rendered_sql())

    def test_initialization_reports_missing_schema_privilege_concisely(self) -> None:
        self.connection.responses = [("3.5",), (False,)]

        with self.assertRaisesRegex(
            CensusDatabaseError,
            "does not exist or is not writable.*USAGE, CREATE",
        ):
            self.store.initialize()

        self.assertEqual(self.connection.rollbacks, 1)
        self.assertNotIn("CREATE TABLE", self.rendered_sql())

    def test_lock_is_one_session_advisory_lock_for_stable_target(self) -> None:
        self.connection.responses = [(True,), (True,)]

        self.assertTrue(self.store.acquire_lock())
        self.store.release_lock()

        lock_statements = [
            item
            for item in self.connection.statements
            if "advisory_" in item[0]
        ]
        self.assertEqual(len(lock_statements), 2)
        self.assertIn("pg_try_advisory_lock", lock_statements[0][0])
        self.assertIn("pg_advisory_unlock", lock_statements[1][0])
        self.assertEqual(
            lock_statements[0][1],
            ("mapp-explore-etl-census:leeds.census_2021_england_oa",),
        )

    def test_start_run_atomically_closes_stale_run_before_new_insert(self) -> None:
        run_id = uuid.uuid4()

        self.store.start_run(run_id)

        update_statement, update_params = self.connection.statements[-2]
        insert_statement, insert_params = self.connection.statements[-1]
        self.assertIn("UPDATE", update_statement)
        self.assertIn("status = 'failed'", update_statement)
        self.assertIn("finished_at = CURRENT_TIMESTAMP", update_statement)
        self.assertIn("status = 'running'", update_statement)
        self.assertEqual(
            update_params,
            (
                ABANDONED_RUN_ERROR,
                "census_2021_england_oa",
                "census_2021_england_oa",
            ),
        )
        self.assertIn("INSERT INTO", insert_statement)
        self.assertEqual(
            insert_params,
            (
                run_id,
                "census_2021_england_oa",
                "census_2021_england_oa",
            ),
        )
        self.assertEqual(self.connection.commits, 1)
        self.assertEqual(self.connection.rollbacks, 0)

    def test_new_run_insert_failure_rolls_back_stale_run_closure(self) -> None:
        self.connection.fail_on = "INSERT INTO"

        with self.assertRaisesRegex(RuntimeError, "injected SQL failure"):
            self.store.start_run(uuid.uuid4())

        self.assertEqual(self.connection.commits, 0)
        self.assertEqual(self.connection.rollbacks, 1)

    def test_abandoned_run_error_is_clear_and_bounded(self) -> None:
        self.assertEqual(
            ABANDONED_RUN_ERROR,
            "abandoned: previous Census ETL session ended before completion",
        )
        self.assertLessEqual(len(ABANDONED_RUN_ERROR), 256)

    def test_staging_is_session_local_and_survives_chunk_commits(self) -> None:
        run_id = uuid.UUID("12345678-1234-5678-1234-567812345678")

        geometry_stage = self.store.create_geometry_stage(run_id)
        topic_stage = self.store.create_topic_stage(
            run_id,
            0,
            ("ts001_0001", "ts001_0002"),
        )

        self.assertEqual(
            geometry_stage,
            "census_geometry_12345678123456781234567812345678",
        )
        ddl = self.rendered_sql()
        self.assertEqual(ddl.count("CREATE TEMP TABLE"), 2)
        self.assertEqual(ddl.count("ON COMMIT PRESERVE ROWS"), 2)
        self.assertNotIn("CREATE UNLOGGED TABLE", ddl)
        self.assertNotIn('"leeds"."census_geometry_', ddl)
        self.assertEqual(self.connection.commits, 2)

    def test_geometry_stage_supports_reviewed_identity_only_source_columns(
        self,
    ) -> None:
        selected_config = sample_config()
        connection = FakeConnection()
        store = CensusPostgresStore(
            connection, selected_config  # type: ignore[arg-type]
        )

        store.create_geometry_stage(uuid.uuid4())

        statement = connection.statements[-1][0]
        self.assertIn(
            "oa21cd text PRIMARY KEY, "
            "geom geometry(MultiPolygon, 4326) NOT NULL",
            statement,
        )

    def test_topic_rows_use_one_streaming_copy_transaction(self) -> None:
        self.store.copy_topic_rows(
            "census_topic_safe",
            ("ts001_0001", "ts001_0002"),
            iter(
                [
                    ("E00000001", 10, 20),
                    ("E00000002", 30, 40),
                ]
            ),
        )

        statement, copied_rows = self.connection.statements[-1]
        self.assertIn('COPY pg_temp."census_topic_safe"', statement)
        self.assertIn("FROM STDIN", statement)
        self.assertEqual(len(copied_rows), 2)
        self.assertEqual(self.connection.commits, 1)
        self.assertEqual(self.connection.rollbacks, 0)

    def test_geometry_page_uses_bound_geojson_and_identifier_safe_sql(self) -> None:
        feature = PreparedFeature(
            object_id=1,
            values=("E00000001",),
            source_attributes={"OA21CD": "E00000001"},
            geometry={
                "type": "Polygon",
                "coordinates": [
                    [
                        [-1.5, 53.8],
                        [-1.4, 53.8],
                        [-1.4, 53.9],
                        [-1.5, 53.8],
                    ]
                ],
            },
            source_hash="a" * 64,
        )

        self.store.insert_geometry_page("census_geometry_safe", [feature])

        statement, rows = self.connection.statements[-1]
        self.assertIn('INSERT INTO pg_temp."census_geometry_safe"', statement)
        self.assertIn("ST_Multi", statement)
        self.assertNotIn("E00000001", statement)
        self.assertEqual(rows[0][0], "E00000001")

    def test_geometry_validation_rejects_wrong_count_and_invalid_geometry(self) -> None:
        self.connection.responses = [
            (1, 0, 0, 0, 0, 1, ["E00000001"])
        ]

        with self.assertRaisesRegex(
            CensusDatabaseError,
            "expected=2, rows=1.*invalid_geometries=1",
        ):
            self.store.validate_geometry("census_geometry_safe", 2, 64)
        self.assertIn("ST_IsEmpty(geom)", self.rendered_sql())

    def test_invalid_geometry_is_repaired_within_reviewed_limit(self) -> None:
        self.connection.responses = [
            (2, 0, 0, 0, 0, 1, ["E00000002"]),
            (2, 0, 0, 0, 0, 0, []),
        ]
        self.connection.default_rowcount = 1

        repaired = self.store.validate_geometry(
            "census_geometry_safe",
            2,
            64,
        )

        self.assertEqual(repaired, ("E00000002",))
        sql_text = self.rendered_sql()
        self.assertEqual(sql_text.count("ST_MakeValid(geom)"), 1)
        self.assertIn("ST_CollectionExtract", sql_text)
        self.assertIn("array_agg(oa21cd ORDER BY oa21cd)", sql_text)
        self.assertEqual(self.connection.commits, 1)

    def test_geometry_repair_limit_fails_before_any_update(self) -> None:
        self.connection.responses = [
            (2, 0, 0, 0, 0, 2, ["E00000001", "E00000002"])
        ]

        with self.assertRaisesRegex(
            CensusDatabaseError,
            r"invalid=2, maximum=1.*no geometries were repaired",
        ):
            self.store.validate_geometry("census_geometry_safe", 2, 1)

        self.assertNotIn("UPDATE pg_temp", self.rendered_sql())
        self.assertEqual(self.connection.rollbacks, 1)

    def test_non_polygon_repair_result_is_rejected_and_rolled_back(self) -> None:
        self.connection.responses = [
            (2, 0, 0, 0, 0, 1, ["E00000001"]),
            (2, 0, 0, 1, 0, 0, []),
        ]
        self.connection.default_rowcount = 1

        with self.assertRaisesRegex(
            CensusDatabaseError,
            "did not produce valid non-empty MultiPolygon.*empty_geometries=1",
        ):
            self.store.validate_geometry("census_geometry_safe", 2, 64)

        self.assertIn("ST_MakeValid(geom)", self.rendered_sql())
        self.assertEqual(self.connection.commits, 0)
        self.assertEqual(self.connection.rollbacks, 1)

    def test_geometry_repair_candidate_count_and_order_must_be_consistent(
        self,
    ) -> None:
        for candidates in (
            ["E00000001"],
            ["E00000002", "E00000001"],
            ["E00000001", "E00000001"],
        ):
            with self.subTest(candidates=candidates):
                connection = FakeConnection()
                connection.responses = [
                    (2, 0, 0, 0, 0, 2, candidates)
                ]
                store = CensusPostgresStore(
                    connection,
                    sample_config(),  # type: ignore[arg-type]
                )

                with self.assertRaisesRegex(
                    CensusDatabaseError,
                    "repair candidate audit is inconsistent",
                ):
                    store.validate_geometry(
                        "census_geometry_safe",
                        2,
                        64,
                    )
                self.assertEqual(connection.rollbacks, 1)

    def test_topic_set_mismatch_rolls_back_without_wide_update(self) -> None:
        self.connection.responses = [(2, 1, 0)]

        with self.assertRaisesRegex(CensusCodeSetError, "missing=1"):
            self.store.apply_topic(
                "census_geometry_safe",
                "census_topic_safe",
                ("ts001_0001",),
                2,
            )

        self.assertEqual(self.connection.rollbacks, 1)
        self.assertNotIn("UPDATE pg_temp", self.rendered_sql())

    def test_wide_snapshot_is_written_once_from_narrow_topic_joins(self) -> None:
        run_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        self.connection.default_rowcount = 2

        wide = self.store.assemble_wide_stage(
            run_id,
            "census_geometry_safe",
            (
                ("census_topic_a", ("ts001_0001", "ts001_0002")),
                ("census_topic_b", ("ts002_0001",)),
            ),
            2,
        )

        sql_text = self.rendered_sql()
        self.assertEqual(
            wide,
            "census_wide_12345678123456781234567812345678",
        )
        self.assertEqual(sql_text.count("INSERT INTO pg_temp"), 1)
        self.assertEqual(sql_text.count("JOIN pg_temp"), 2)
        self.assertNotIn("UPDATE pg_temp", sql_text)
        self.assertIn('"topic_0"."ts001_0001"', sql_text)
        self.assertIn('"topic_1"."ts002_0001"', sql_text)

    def test_publish_failure_rolls_back_target_and_metadata_together(self) -> None:
        run_id = uuid.uuid4()
        dataset = CensusDatasetMetadata(
            dataset_key="census_2021_england_oa",
            oa_count=2,
            variable_count=3,
            geometry_repairs=32,
            geometry_source_url="https://example.test/FeatureServer/0",
            geometry_source_sha256="a" * 64,
            source_metadata={"release": "Census 2021"},
        )
        variables = (
            CensusVariableMetadata(
                column_name="ts001_0001",
                topic_id="TS001",
                topic_title="Topic",
                ordinal=1,
                label="People's label",
                source_url="https://example.test/topic.zip",
                source_member="topic-oa.csv",
                source_sha256="b" * 64,
                source_metadata={"version": 1},
            ),
        )
        self.connection.default_rowcount = 2
        self.connection.fail_on = "census_datasets"

        with self.assertRaisesRegex(RuntimeError, "injected SQL failure"):
            self.store.publish(
                "census_wide_safe",
                run_id,
                dataset,
                variables,
            )

        sql_text = self.rendered_sql()
        self.assertIn(
            'TRUNCATE TABLE "leeds"."census_2021_england_oa"',
            sql_text,
        )
        self.assertIn(
            'INSERT INTO "leeds"."census_2021_england_oa"',
            sql_text,
        )
        self.assertEqual(self.connection.commits, 0)
        self.assertEqual(self.connection.rollbacks, 1)
        dataset_statement = next(
            item
            for item in self.connection.statements
            if "INSERT INTO" in item[0] and "census_datasets" in item[0]
        )
        self.assertIn("geometry_repairs", dataset_statement[0])
        self.assertEqual(dataset_statement[1][4], 32)
        variable_comment = next(
            item
            for item in self.connection.statements
            if 'COMMENT ON COLUMN "leeds"."census_2021_england_oa".'
            '"ts001_0001"' in item[0]
        )
        self.assertEqual(
            variable_comment[1],
            None,
        )
        self.assertIn(
            "IS 'Census 2021 TS001 measure 1: People''s label'",
            variable_comment[0],
        )

    def test_variable_comments_are_bounded_before_stable_replacement(self) -> None:
        dataset = CensusDatasetMetadata(
            dataset_key="census_2021_england_oa",
            oa_count=2,
            variable_count=1,
            geometry_repairs=0,
            geometry_source_url="https://example.test/FeatureServer/0",
            geometry_source_sha256="a" * 64,
            source_metadata={"release": "Census 2021"},
        )
        variable = CensusVariableMetadata(
            column_name="ts001_0001",
            topic_id="TS001",
            topic_title="Topic",
            ordinal=1,
            label="x" * 2_001,
            source_url="https://example.test/topic.zip",
            source_member="topic-oa.csv",
            source_sha256="b" * 64,
            source_metadata={"version": 1},
        )

        with self.assertRaisesRegex(
            CensusDatabaseError,
            "variable comment exceeds 2000 characters",
        ):
            self.store.publish(
                "census_wide_safe",
                uuid.uuid4(),
                dataset,
                (variable,),
            )

        self.assertEqual(self.connection.statements, [])

    def test_run_progress_audits_geometry_repairs(self) -> None:
        run_id = uuid.uuid4()

        self.store.record_progress(
            run_id,
            geometry_rows=178_605,
            geometry_repairs=32,
            topics_loaded=0,
        )

        statement, params = self.connection.statements[-1]
        self.assertIn("geometry_repairs = %s", statement)
        self.assertEqual(params, (178_605, 32, 0, run_id))

    def test_rejects_unsafe_or_duplicate_configured_identifiers(self) -> None:
        config = sample_config()
        config.target_table = "census;drop"
        with self.assertRaisesRegex(CensusDatabaseError, "target_table"):
            CensusPostgresStore(self.connection, config)  # type: ignore[arg-type]

        config = sample_config()
        config.topics = (
            SimpleNamespace(target_columns=("oa21cd",)),
        )
        with self.assertRaisesRegex(CensusDatabaseError, "must be unique"):
            CensusPostgresStore(self.connection, config)  # type: ignore[arg-type]

    def test_stable_table_schema_drift_is_not_silently_retained(self) -> None:
        self.connection.schema_rows.append(
            ("stale_column", "text", "", False, False)
        )

        with self.assertRaisesRegex(
            CensusDatabaseError,
            r"extra=\['stale_column'\]",
        ):
            self.store.initialize()

        self.assertEqual(self.connection.commits, 0)
        self.assertEqual(self.connection.rollbacks, 1)

    def test_stable_identity_and_geometry_nullability_are_enforced(self) -> None:
        for drift, expected_error in (
            (
                ("oa21cd", "text", "", True, False),
                r"primary_key=\[\]",
            ),
            (
                ("geom", "geometry(MultiPolygon,4326)", "", False, False),
                "geom_not_null=False",
            ),
        ):
            with self.subTest(expected_error=expected_error):
                connection = FakeConnection()
                name = drift[0]
                connection.schema_rows = [
                    drift if row[0] == name else row
                    for row in connection.schema_rows
                ]
                store = CensusPostgresStore(
                    connection, sample_config()  # type: ignore[arg-type]
                )
                with self.assertRaisesRegex(CensusDatabaseError, expected_error):
                    store.initialize()
                self.assertEqual(connection.rollbacks, 1)


@unittest.skipUnless(
    os.getenv("CENSUS_TEST_DATABASE_URL"),
    "set CENSUS_TEST_DATABASE_URL to run PostGIS integration tests",
)
class CensusGeometryRepairPostGISTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(os.environ["CENSUS_TEST_DATABASE_URL"])
        self.store = CensusPostgresStore(self.connection, sample_config())
        self.stage = self.store.create_geometry_stage(uuid.uuid4())

    def tearDown(self) -> None:
        self.connection.close()

    def insert_geometries(self, geometries: list[tuple[str, str]]) -> None:
        with self.connection.cursor() as cursor:
            cursor.executemany(
                sql.SQL(
                    "INSERT INTO pg_temp.{} (oa21cd, geom) "
                    "VALUES (%s, ST_GeomFromText(%s, 4326))"
                ).format(sql.Identifier(self.stage)),
                geometries,
            )
        self.connection.commit()

    def geometry_state(self, code: str) -> tuple[bool, bool, str, int]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT ST_IsValid(geom), ST_IsEmpty(geom), "
                    "GeometryType(geom), ST_SRID(geom) "
                    "FROM pg_temp.{} WHERE oa21cd = %s"
                ).format(sql.Identifier(self.stage)),
                (code,),
            )
            row = cursor.fetchone()
        self.connection.commit()
        if row is None:
            raise AssertionError(f"missing geometry {code}")
        return row

    def test_initialization_persists_relation_comments(self) -> None:
        target_table = f"census_comment_test_{uuid.uuid4().hex}"
        selected_config = sample_config()
        selected_config.target_table = target_table
        store = CensusPostgresStore(self.connection, selected_config)

        try:
            store.initialize()
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT obj_description(
                               to_regclass(%s),
                               'pg_class'
                           ),
                           col_description(
                               to_regclass(%s),
                               (
                                   SELECT attnum
                                   FROM pg_catalog.pg_attribute
                                   WHERE attrelid = to_regclass(%s)
                                     AND attname = 'oa21cd'
                                     AND NOT attisdropped
                               )
                           )
                    """,
                    (
                        f"leeds.{target_table}",
                        f"leeds.{target_table}",
                        f"leeds.{target_table}",
                    ),
                )
                comments = cursor.fetchone()
            self.connection.commit()

            self.assertIsNotNone(comments)
            self.assertEqual(comments[0], TABLE_COMMENT)
            self.assertEqual(comments[1], OA_CODE_COMMENT)
        finally:
            self.connection.rollback()
            with self.connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}.{}").format(
                        sql.Identifier("leeds"),
                        sql.Identifier(target_table),
                    )
                )
            self.connection.commit()

    def test_bow_tie_is_repaired_to_valid_nonempty_multipolygon(self) -> None:
        self.insert_geometries(
            [
                (
                    "E00000001",
                    "MULTIPOLYGON(((0 0,2 2,0 2,2 0,0 0)))",
                )
            ]
        )
        self.assertFalse(self.geometry_state("E00000001")[0])

        repaired = self.store.validate_geometry(self.stage, 1, 64)

        self.assertEqual(repaired, ("E00000001",))
        self.assertEqual(
            self.geometry_state("E00000001"),
            (True, False, "MULTIPOLYGON", 4326),
        )

    def test_empty_geometry_is_rejected_without_repair(self) -> None:
        self.insert_geometries(
            [("E00000001", "MULTIPOLYGON EMPTY")]
        )

        with self.assertRaisesRegex(
            CensusDatabaseError,
            "before repair.*empty_geometries=1",
        ):
            self.store.validate_geometry(self.stage, 1, 64)

        self.assertEqual(
            self.geometry_state("E00000001"),
            (True, True, "MULTIPOLYGON", 4326),
        )

    def test_make_valid_without_polygon_output_is_rejected_and_rolled_back(
        self,
    ) -> None:
        self.insert_geometries(
            [
                (
                    "E00000001",
                    "MULTIPOLYGON(((0 0,1 1,2 2,0 0)))",
                )
            ]
        )
        original_state = self.geometry_state("E00000001")
        self.assertEqual(
            original_state,
            (False, False, "MULTIPOLYGON", 4326),
        )

        with self.assertRaisesRegex(
            CensusDatabaseError,
            "did not produce valid non-empty MultiPolygon",
        ):
            self.store.validate_geometry(self.stage, 1, 64)

        self.assertEqual(self.geometry_state("E00000001"), original_state)

    def test_over_limit_fails_before_repair_and_preserves_invalid_rows(self) -> None:
        self.insert_geometries(
            [
                (
                    "E00000001",
                    "MULTIPOLYGON(((0 0,2 2,0 2,2 0,0 0)))",
                ),
                (
                    "E00000002",
                    "MULTIPOLYGON(((10 10,12 12,10 12,12 10,10 10)))",
                ),
            ]
        )

        with self.assertRaisesRegex(
            CensusDatabaseError,
            r"invalid=2, maximum=1.*no geometries were repaired",
        ):
            self.store.validate_geometry(self.stage, 2, 1)

        with self.connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT count(*) FROM pg_temp.{} "
                    "WHERE NOT ST_IsValid(geom)"
                ).format(sql.Identifier(self.stage))
            )
            invalid_count = cursor.fetchone()
        self.connection.commit()
        self.assertEqual(invalid_count, (2,))

    def test_start_run_closes_only_prior_matching_running_rows(self) -> None:
        selected_config = sample_config()
        selected_config.target_schema = "pg_temp"
        store = CensusPostgresStore(self.connection, selected_config)
        stale_run = uuid.uuid4()
        completed_run = uuid.uuid4()
        other_target_run = uuid.uuid4()
        new_run = uuid.uuid4()
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE _census_etl_runs (
                    run_id uuid PRIMARY KEY,
                    dataset_key text NOT NULL,
                    target_table text NOT NULL,
                    status text NOT NULL,
                    started_at timestamp with time zone NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    finished_at timestamp with time zone,
                    error text
                ) ON COMMIT PRESERVE ROWS
                """
            )
            cursor.executemany(
                """
                INSERT INTO _census_etl_runs
                    (run_id, dataset_key, target_table, status)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    (
                        stale_run,
                        selected_config.target_table,
                        selected_config.target_table,
                        "running",
                    ),
                    (
                        completed_run,
                        selected_config.target_table,
                        selected_config.target_table,
                        "succeeded",
                    ),
                    (
                        other_target_run,
                        "other_target",
                        "other_target",
                        "running",
                    ),
                ),
            )
        self.connection.commit()

        store.start_run(new_run)

        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT run_id, status, finished_at IS NOT NULL, error
                FROM _census_etl_runs
                ORDER BY run_id
                """
            )
            rows = {
                run_id: (status, finished, error)
                for run_id, status, finished, error in cursor.fetchall()
            }
        self.connection.commit()
        self.assertEqual(
            rows[stale_run],
            ("failed", True, ABANDONED_RUN_ERROR),
        )
        self.assertEqual(rows[new_run], ("running", False, None))
        self.assertEqual(rows[completed_run], ("succeeded", False, None))
        self.assertEqual(rows[other_target_run], ("running", False, None))


class CensusDatasetPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = FakeConnection()
        self.store = CensusPostgresStore(
            self.connection, sample_config()  # type: ignore[arg-type]
        )

    def rendered_sql(self) -> str:
        return "\n".join(statement for statement, _ in self.connection.statements)

    def test_ddl_creates_a_singleton_publication_table(self) -> None:
        self.store.initialize()
        ddl = self.rendered_sql()
        self.assertIn("dataset_publication", ddl)
        self.assertIn("singleton boolean PRIMARY KEY DEFAULT true", ddl)
        self.assertIn("CHECK (singleton)", ddl)
        self.assertIn("release_id text NOT NULL UNIQUE", ddl)
        self.assertNotIn("_dataset_publication", ddl)

    def test_publish_release_computes_row_count_and_commits(self) -> None:
        self.connection.responses = [None, (250,)]

        self.store.publish_release(
            dataset_id="census",
            release_id="release-1",
            schema_version=1,
            source_hash="a" * 64,
            geometry_contract_version=1,
        )

        self.assertEqual(1, self.connection.commits)
        self.assertEqual(0, self.connection.rollbacks)
        insert_statement, params = self.connection.statements[-1]
        self.assertIn("INSERT INTO", insert_statement)
        self.assertIn("dataset_publication", insert_statement)
        self.assertIn("ON CONFLICT (singleton) DO UPDATE", insert_statement)
        self.assertEqual(
            {"census_2021_england_oa": 250}, params[4].obj
        )

    def test_publish_release_rejects_a_schema_version_regression(self) -> None:
        self.connection.responses = [(5, 2)]

        with self.assertRaises(CensusDatabaseError):
            self.store.publish_release(
                dataset_id="census",
                release_id="release-2",
                schema_version=4,
                source_hash="b" * 64,
                geometry_contract_version=2,
            )
        self.assertEqual(0, self.connection.commits)
        self.assertEqual(1, self.connection.rollbacks)

    def test_publish_release_rejects_a_geometry_contract_version_regression(
        self,
    ) -> None:
        self.connection.responses = [(5, 2)]

        with self.assertRaises(CensusDatabaseError):
            self.store.publish_release(
                dataset_id="census",
                release_id="release-2",
                schema_version=5,
                source_hash="b" * 64,
                geometry_contract_version=1,
            )
        self.assertEqual(0, self.connection.commits)
        self.assertEqual(1, self.connection.rollbacks)

    def test_publish_release_allows_an_unchanged_version_republish(self) -> None:
        self.connection.responses = [(5, 2), (250,)]

        self.store.publish_release(
            dataset_id="census",
            release_id="release-2",
            schema_version=5,
            source_hash="c" * 64,
            geometry_contract_version=2,
        )

        self.assertEqual(1, self.connection.commits)
        self.assertEqual(0, self.connection.rollbacks)


if __name__ == "__main__":
    unittest.main()
