from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Literal

from leeds_arcgis_etl.census_pipeline import (
    CensusConsistencyError,
    run_census,
)
from leeds_arcgis_etl.config import ColumnConfig, LayerConfig


GEOMETRY_SHA256 = (
    "e0ae4e6443f63d1adef9aaddd3902d2cdd79a074568b5be998bf78aff98dbcfb"
)


def geometry_layer() -> LayerConfig:
    return LayerConfig(
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


def geometry_metadata() -> dict[str, Any]:
    return {
        "name": "Output Areas 2021 EW BGC V2",
        "geometryType": "esriGeometryPolygon",
        "hasM": False,
        "sourceSpatialReference": {"wkid": 27700},
        "advancedQueryCapabilities": {
            "supportsPagination": True,
            "supportsOrderBy": True,
        },
        "supportedQueryFormats": "JSON, geoJSON",
        "maxRecordCount": 2_000,
        "fields": [
            {"name": "FID", "type": "esriFieldTypeOID"},
            {"name": "OA21CD", "type": "esriFieldTypeString"},
        ],
    }


def geometry_feature(object_id: int, code: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "FID": object_id,
            "OA21CD": code,
        },
        "geometry": {
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
    }


def topic(
    topic_id: str = "TS001",
    columns: tuple[str, ...] = ("ts001_0001", "ts001_0002"),
) -> Any:
    return SimpleNamespace(
        id=topic_id,
        title=f"{topic_id} title",
        target_columns=columns,
    )


def config(topics: tuple[Any, ...] | None = None) -> Any:
    return SimpleNamespace(
        target_schema="leeds",
        target_table="census_2021_england_oa",
        geometry_layer=geometry_layer(),
        geometry_sha256=GEOMETRY_SHA256,
        expected_england_oa_count=2,
        max_geometry_repairs=64,
        topics=topics or (topic(),),
    )


class FakeArcGISClient:
    page_size = 2

    def __init__(
        self,
        pages: list[list[dict[str, Any]]] | None = None,
        counts: list[int] | None = None,
    ) -> None:
        self.raw_pages = pages or [
            [
                geometry_feature(1, "E00000001"),
                geometry_feature(2, "E00000002"),
            ]
        ]
        self.counts = counts or [2, 2]

    def metadata(self, _layer: Any) -> dict[str, Any]:
        return geometry_metadata()

    def count(self, _layer: Any) -> int:
        return self.counts.pop(0)

    def pages(self, _layer: Any, _metadata: Any) -> Any:
        offset = 0
        for page in self.raw_pages:
            yield offset, page
            offset += len(page)


def source(topic_id: str) -> Any:
    has_metadata = topic_id != "TS007A"
    return SimpleNamespace(
        source_url=f"https://example.test/{topic_id}.zip",
        oa_member=f"{topic_id.lower()}-oa.csv",
        metadata_member=(
            f"metadata/{topic_id.lower()}-2021-1.txt"
            if has_metadata
            else None
        ),
        metadata_sha256="2" * 64 if has_metadata else None,
        metadata_text=(
            "Title: Topic\n\nCoverage\nEngland and Wales\n"
            "Protecting personal data\nCell key perturbation\n"
            if has_metadata
            else None
        ),
        archive_sha256=(topic_id[-1].lower() or "a") * 64,
        archive_bytes=12_345,
        title=f"{topic_id} title",
        issued="2023-01-01" if has_metadata else None,
        version=1 if has_metadata else None,
    )


class TopicContext:
    def __init__(self, stream: Any, exit_error: Exception | None = None) -> None:
        self.stream = stream
        self.exit_error = exit_error

    def __enter__(self) -> Any:
        return self.stream

    def __exit__(
        self, exc_type: Any, _exc: Any, _traceback: Any
    ) -> Literal[False]:
        if exc_type is None and self.exit_error is not None:
            raise self.exit_error
        return False


class FakeNomisClient:
    def __init__(self, streams: dict[str, TopicContext]) -> None:
        self.streams = streams
        self.opened: list[str] = []

    def open_topic(self, selected_topic: Any) -> TopicContext:
        self.opened.append(selected_topic.id)
        return self.streams[selected_topic.id]


def topic_context(
    selected_topic: Any,
    codes: tuple[str, ...] = ("E00000001", "E00000002"),
    *,
    values: tuple[tuple[int, ...], ...] = ((10, 20), (30, 40)),
    labels: tuple[str, ...] = ("Residents; measures: Value", "Other; measures: Value"),
    exit_error: Exception | None = None,
) -> TopicContext:
    rows = iter(
        SimpleNamespace(oa_code=code, values=row_values)
        for code, row_values in zip(codes, values, strict=True)
    )
    stream = SimpleNamespace(
        target_columns=selected_topic.target_columns,
        labels=labels,
        rows=rows,
        source=source(selected_topic.id),
    )
    return TopicContext(stream, exit_error)


class FakeStore:
    def __init__(self, selected_config: Any) -> None:
        self.lock_available = True
        self.events: list[Any] = []
        self.failed = False
        self.released = False
        self.published: tuple[Any, ...] | None = None
        self.geometry_repair_candidates: tuple[str, ...] = ()
        self.repair_extent = None
        self.statistic_columns = tuple(
            column
            for selected_topic in selected_config.topics
            for column in selected_topic.target_columns
        )

    def acquire_lock(self) -> bool:
        self.events.append("acquire")
        return self.lock_available

    def release_lock(self) -> None:
        self.events.append("release")
        self.released = True

    def initialize(self) -> None:
        self.events.append("initialize")

    def start_run(self, run_id: Any) -> None:
        self.events.append(("start", run_id))

    def create_geometry_stage(self, _run_id: Any) -> str:
        self.events.append("create_geometry")
        return "geometry_stage"

    def insert_geometry_page(self, _stage: str, features: Any) -> None:
        self.events.append(("geometry_page", len(features)))

    def validate_geometry(
        self,
        _stage: str,
        expected: int,
        maximum: int,
        *,
        repair_extent: Any = None,
    ) -> tuple[str, ...]:
        self.repair_extent = repair_extent
        self.events.append(("validate_geometry", expected, maximum))
        return self.geometry_repair_candidates

    def record_progress(self, _run_id: Any, **progress: Any) -> None:
        self.events.append(("progress", progress))

    def create_topic_stage(
        self,
        _run_id: Any,
        index: int,
        _columns: Any,
    ) -> str:
        stage = f"topic_stage_{index}"
        self.events.append(("create_topic", stage))
        return stage

    def copy_topic_rows(
        self,
        stage: str,
        _columns: Any,
        rows: Any,
    ) -> None:
        materialized = list(rows)
        self.events.append(("topic_copy", stage, len(materialized)))

    def apply_topic(
        self,
        _geometry_stage: str,
        topic_stage: str,
        _columns: Any,
        expected: int,
    ) -> None:
        self.events.append(("validate_topic_set", topic_stage, expected))

    def assemble_wide_stage(
        self,
        _run_id: Any,
        _geometry_stage: str,
        topic_stages: Any,
        expected: int,
    ) -> str:
        self.events.append(("assemble_once", len(topic_stages), expected))
        return "wide_stage"

    def cleanup_stages(self, stages: Any) -> None:
        self.events.append(("cleanup", tuple(stages)))

    def publish(
        self,
        stage: str,
        run_id: Any,
        dataset: Any,
        variables: Any,
    ) -> None:
        self.events.append(("publish", stage))
        self.published = (run_id, dataset, tuple(variables))

    def fail_run(self, _run_id: Any, _error: str) -> None:
        self.events.append("failed")
        self.failed = True


class CensusPipelineTests(unittest.TestCase):
    def test_success_validates_sources_writes_wide_once_and_preserves_metadata(
        self,
    ) -> None:
        selected_config = config()
        selected_topic = selected_config.topics[0]
        nomis = FakeNomisClient(
            {selected_topic.id: topic_context(selected_topic)}
        )
        store = FakeStore(selected_config)

        result = run_census(
            selected_config,
            FakeArcGISClient(),  # type: ignore[arg-type]
            nomis,  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
        )

        self.assertEqual(result.oa_count, 2)
        self.assertEqual(result.geometry_repairs, 0)
        self.assertEqual(result.topic_count, 1)
        self.assertEqual(result.variable_count, 2)
        self.assertIsNone(store.repair_extent)
        self.assertEqual(nomis.opened, ["TS001"])
        self.assertEqual(
            [event for event in store.events if event[0] == "assemble_once"],
            [("assemble_once", 1, 2)],
        )
        self.assertFalse(
            any(
                isinstance(event, tuple) and event[0] == "update_wide"
                for event in store.events
            )
        )
        self.assertLess(
            store.events.index(
                ("cleanup", ("geometry_stage", "topic_stage_0"))
            ),
            store.events.index(("publish", "wide_stage")),
        )
        self.assertIsNotNone(store.published)
        _, dataset, variables = store.published  # type: ignore[misc]
        self.assertEqual(dataset.geometry_repairs, 0)
        self.assertEqual(dataset.geometry_source_sha256, GEOMETRY_SHA256)
        self.assertEqual(dataset.source_metadata["geometry"]["repairs"], 0)
        self.assertEqual(dataset.geometry_source_sha256.__len__(), 64)
        self.assertEqual(
            [variable.label for variable in variables],
            ["Residents; measures: Value", "Other; measures: Value"],
        )
        self.assertTrue(
            all(variable.source_sha256 == "1" * 64 for variable in variables)
        )
        self.assertEqual(
            dataset.source_metadata["topics"][0]["metadata_member"],
            "metadata/ts001-2021-1.txt",
        )
        self.assertIn(
            "Protecting personal data",
            dataset.source_metadata["topics"][0]["metadata_text"],
        )
        self.assertEqual(
            dataset.source_metadata["topics"][0]["metadata_sha256"],
            "2" * 64,
        )
        self.assertTrue(
            all(
                "metadata_text" not in variable.source_metadata
                for variable in variables
            )
        )
        self.assertTrue(
            all(
                variable.source_metadata["metadata_sha256"] == "2" * 64
                for variable in variables
            )
        )
        self.assertFalse(store.failed)
        self.assertTrue(store.released)

    def test_repair_extent_is_passed_to_geometry_validation_and_metadata(
        self,
    ) -> None:
        selected_config = config()
        selected_topic = selected_config.topics[0]
        repair_extent = (-1.85, 53.65, -1.2, 54.0)
        nomis = FakeNomisClient(
            {selected_topic.id: topic_context(selected_topic)}
        )
        store = FakeStore(selected_config)

        run_census(
            selected_config,
            FakeArcGISClient(),  # type: ignore[arg-type]
            nomis,  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
            repair_extent=repair_extent,
        )

        self.assertEqual(store.repair_extent, repair_extent)
        self.assertIsNotNone(store.published)
        _run_id, dataset, _variables = store.published  # type: ignore[misc]
        self.assertEqual(
            dataset.source_metadata["geometry"]["repair_extent"],
            list(repair_extent),
        )

    def test_geometry_hash_mismatch_fails_before_spatial_validation(self) -> None:
        selected_config = config()
        selected_config.geometry_sha256 = "0" * 64
        store = FakeStore(selected_config)

        with self.assertRaisesRegex(
            CensusConsistencyError,
            r"SHA-256 does not match the pinned release.*expected=0{64}",
        ):
            run_census(
                selected_config,
                FakeArcGISClient(),  # type: ignore[arg-type]
                FakeNomisClient({}),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )

        self.assertNotIn(("validate_geometry", 2, 64), store.events)
        self.assertIsNone(store.published)
        self.assertTrue(store.failed)
        self.assertTrue(store.released)

    def test_repaired_geometry_count_is_warned_returned_and_audited(self) -> None:
        selected_config = config()
        selected_topic = selected_config.topics[0]
        store = FakeStore(selected_config)
        store.geometry_repair_candidates = ("E00000002",)
        nomis = FakeNomisClient(
            {selected_topic.id: topic_context(selected_topic)}
        )

        with self.assertLogs(
            "leeds_arcgis_etl.census_pipeline",
            level="WARNING",
        ) as logs:
            result = run_census(
                selected_config,
                FakeArcGISClient(),  # type: ignore[arg-type]
                nomis,  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )

        self.assertEqual(result.geometry_repairs, 1)
        self.assertIn("repaired 1 invalid official OA geometries", logs.output[0])
        self.assertIsNotNone(store.published)
        _, dataset, _variables = store.published  # type: ignore[misc]
        self.assertEqual(dataset.geometry_repairs, 1)
        self.assertEqual(dataset.source_metadata["geometry"]["repairs"], 1)
        self.assertEqual(
            dataset.source_metadata["geometry"]["repair_candidates"],
            ["E00000002"],
        )
        progress = [
            event[1]
            for event in store.events
            if isinstance(event, tuple) and event[0] == "progress"
        ]
        self.assertTrue(
            all(item["geometry_repairs"] == 1 for item in progress)
        )

    def test_metadata_less_topic_remains_explicit_in_dataset_and_variables(
        self,
    ) -> None:
        selected_topic = topic(
            "TS007A",
            ("ts007a_0001", "ts007a_0002"),
        )
        selected_config = config((selected_topic,))
        store = FakeStore(selected_config)
        nomis = FakeNomisClient(
            {selected_topic.id: topic_context(selected_topic)}
        )

        run_census(
            selected_config,
            FakeArcGISClient(),  # type: ignore[arg-type]
            nomis,  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
        )

        self.assertIsNotNone(store.published)
        _, dataset, variables = store.published  # type: ignore[misc]
        topic_source = dataset.source_metadata["topics"][0]
        self.assertIsNone(topic_source["metadata_member"])
        self.assertIsNone(topic_source["metadata_sha256"])
        self.assertIsNone(topic_source["metadata_text"])
        self.assertTrue(
            all(
                variable.source_metadata["metadata_sha256"] is None
                and "metadata_text" not in variable.source_metadata
                for variable in variables
            )
        )

    def test_unavailable_lock_rejects_run_before_any_write(self) -> None:
        selected_config = config()
        store = FakeStore(selected_config)
        store.lock_available = False

        with self.assertRaisesRegex(CensusConsistencyError, "dataset lock"):
            run_census(
                selected_config,
                FakeArcGISClient(),  # type: ignore[arg-type]
                FakeNomisClient({}),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )

        self.assertEqual(store.events, ["acquire"])
        self.assertFalse(store.released)

    def test_duplicate_geometry_code_fails_before_publication_and_cleans_stage(
        self,
    ) -> None:
        selected_config = config()
        store = FakeStore(selected_config)
        client = FakeArcGISClient(
            pages=[
                [geometry_feature(1, "E00000001")],
                [geometry_feature(2, "E00000001")],
            ]
        )

        with self.assertRaisesRegex(CensusConsistencyError, "duplicate OA code"):
            run_census(
                selected_config,
                client,  # type: ignore[arg-type]
                FakeNomisClient({}),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )

        self.assertIsNone(store.published)
        self.assertIn(("cleanup", ("geometry_stage",)), store.events)
        self.assertTrue(store.failed)
        self.assertTrue(store.released)

    def test_geometry_count_mismatch_keeps_old_target_untouched(self) -> None:
        selected_config = config()
        store = FakeStore(selected_config)

        with self.assertRaisesRegex(CensusConsistencyError, "pinned England OA"):
            run_census(
                selected_config,
                FakeArcGISClient(counts=[1]),  # type: ignore[arg-type]
                FakeNomisClient({}),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )

        self.assertIsNone(store.published)
        self.assertFalse(any(event == "assemble_once" for event in store.events))
        self.assertTrue(store.failed)

    def test_topic_duplicate_is_rejected_before_assembly(self) -> None:
        selected_config = config()
        selected_topic = selected_config.topics[0]
        store = FakeStore(selected_config)
        nomis = FakeNomisClient(
            {
                selected_topic.id: topic_context(
                    selected_topic,
                    codes=("E00000001", "E00000001"),
                )
            }
        )

        with self.assertRaisesRegex(CensusConsistencyError, "duplicate OA code"):
            run_census(
                selected_config,
                FakeArcGISClient(),  # type: ignore[arg-type]
                nomis,  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )

        self.assertIsNone(store.published)
        self.assertIn(
            ("cleanup", ("geometry_stage", "topic_stage_0")),
            store.events,
        )
        self.assertTrue(store.failed)

    def test_topic_code_set_must_exactly_equal_geometry(self) -> None:
        selected_config = config()
        selected_topic = selected_config.topics[0]
        store = FakeStore(selected_config)
        nomis = FakeNomisClient(
            {
                selected_topic.id: topic_context(
                    selected_topic,
                    codes=("E00000001", "E99999999"),
                )
            }
        )

        with self.assertRaisesRegex(
            CensusConsistencyError,
            r"missing=1, extra=1.*E00000002.*E99999999",
        ):
            run_census(
                selected_config,
                FakeArcGISClient(),  # type: ignore[arg-type]
                nomis,  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )

        self.assertIsNone(store.published)
        self.assertFalse(any(event == "assemble_once" for event in store.events))
        self.assertTrue(store.failed)

    def test_stream_exit_validation_failure_prevents_metadata_and_publish(self) -> None:
        selected_config = config()
        selected_topic = selected_config.topics[0]
        store = FakeStore(selected_config)
        nomis = FakeNomisClient(
            {
                selected_topic.id: topic_context(
                    selected_topic,
                    exit_error=RuntimeError("CRC failure"),
                )
            }
        )

        with self.assertRaisesRegex(RuntimeError, "CRC failure"):
            run_census(
                selected_config,
                FakeArcGISClient(),  # type: ignore[arg-type]
                nomis,  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )

        self.assertIsNone(store.published)
        self.assertTrue(store.failed)
        self.assertTrue(store.released)


if __name__ == "__main__":
    unittest.main()
