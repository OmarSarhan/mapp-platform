from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from leeds_arcgis_etl.census_config import CensusConfigError
from leeds_arcgis_etl.census_main import (
    CensusCheckError,
    CensusLoadError,
    _check_sources,
    _geometry_summary,
    _parse_args,
    _selected_topics,
    main,
)
from leeds_arcgis_etl.config import ColumnConfig, LayerConfig


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "etl" / "config" / "census.json"
FIXTURE_GEOMETRY_SHA256 = (
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


def geometry_feature(
    object_id: int,
    code: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"FID": object_id, "OA21CD": code},
        "geometry": geometry
        if geometry is not None
        else {
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


def check_config(*, geometry_sha256: str = FIXTURE_GEOMETRY_SHA256) -> Any:
    return SimpleNamespace(
        geometry_layer=geometry_layer(),
        geometry_sha256=geometry_sha256,
        expected_england_oa_count=2,
    )


class FakeArcGISClient:
    def __init__(
        self,
        features: list[dict[str, Any]] | None = None,
        counts: list[int] | None = None,
    ) -> None:
        self.features = features or [
            geometry_feature(1, "E00000001"),
            geometry_feature(2, "E00000002"),
        ]
        self.counts = list(counts or [2, 2])
        self.pages_called = 0

    def metadata(self, _layer: Any) -> dict[str, Any]:
        return geometry_metadata()

    def count(self, _layer: Any) -> int:
        return self.counts.pop(0)

    def pages(self, _layer: Any, _metadata: Any) -> Any:
        self.pages_called += 1
        yield 0, self.features


class CensusArgumentTests(unittest.TestCase):
    def test_check_topic_is_normalized_and_may_be_repeated(self) -> None:
        args = _parse_args(
            [
                "--config",
                "reviewed.json",
                "--check-source",
                "--topic",
                " ts001 ",
                "--topic",
                "TS007a",
            ]
        )

        self.assertEqual(args.config, "reviewed.json")
        self.assertTrue(args.check_source)
        self.assertEqual(args.topics, ["TS001", "TS007A"])

    def test_topic_requires_source_check_and_rejects_duplicates(self) -> None:
        cases = (
            (["--topic", "TS001"], "--topic may only be used"),
            (
                ["--check-source", "--topic", "TS001", "--topic", "ts001"],
                "duplicate --topic TS001",
            ),
            (["--check-source", "--topic", "  "], "non-empty topic ID"),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                error = io.StringIO()
                with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                    _parse_args(argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, error.getvalue())

    def test_selected_topics_preserve_requested_order_and_reject_unknown(self) -> None:
        topics = (
            SimpleNamespace(id="TS001"),
            SimpleNamespace(id="TS007A"),
        )
        config = SimpleNamespace(topics=topics)

        selected = _selected_topics(config, ["TS007A", "TS001"])
        self.assertEqual([topic.id for topic in selected], ["TS007A", "TS001"])
        with self.assertRaisesRegex(CensusConfigError, "unknown.*TS999"):
            _selected_topics(config, ["TS999"])


class CensusSourceCheckTests(unittest.TestCase):
    def test_geometry_summary_hashes_every_feature_with_the_pinned_algorithm(
        self,
    ) -> None:
        client = FakeArcGISClient()

        summary = _geometry_summary(check_config(), client)  # type: ignore[arg-type]

        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["sha256"], FIXTURE_GEOMETRY_SHA256)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(client.pages_called, 1)
        self.assertEqual(client.counts, [])

    def test_geometry_summary_rejects_hash_mismatch(self) -> None:
        with self.assertRaisesRegex(
            CensusCheckError,
            r"SHA-256 does not match the pinned release.*expected=0{64}",
        ):
            _geometry_summary(
                check_config(geometry_sha256="0" * 64),
                FakeArcGISClient(),  # type: ignore[arg-type]
            )

    def test_geometry_summary_rejects_invalid_duplicate_and_null_features(
        self,
    ) -> None:
        cases = (
            (
                [
                    geometry_feature(1, "W00000001"),
                    geometry_feature(2, "E00000002"),
                ],
                "invalid England OA code",
            ),
            (
                [
                    geometry_feature(1, "E00000001"),
                    geometry_feature(2, "E00000001"),
                ],
                "duplicate OA code",
            ),
            (
                [
                    {
                        **geometry_feature(1, "E00000001"),
                        "geometry": None,
                    },
                    geometry_feature(2, "E00000002"),
                ],
                "geometry is null",
            ),
        )
        for features, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CensusCheckError, message):
                    _geometry_summary(
                        check_config(),
                        FakeArcGISClient(features=features),  # type: ignore[arg-type]
                    )

    def test_check_sources_reports_success_and_failure_as_json_status(self) -> None:
        topic = SimpleNamespace(
            id="TS001",
            title="Residents",
            source_url="https://example.test/ts001.zip",
        )
        geometry = {
            "key": "geometry",
            "sha256": FIXTURE_GEOMETRY_SHA256,
            "status": "ok",
        }
        topic_summary = {"id": "TS001", "status": "ok"}

        output = io.StringIO()
        with (
            patch(
                "leeds_arcgis_etl.census_main._geometry_summary",
                return_value=geometry,
            ),
            patch(
                "leeds_arcgis_etl.census_main._topic_summary",
                return_value=topic_summary,
            ),
            redirect_stdout(output),
        ):
            status = _check_sources(
                check_config(),
                (topic,),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")

        output = io.StringIO()
        with (
            patch(
                "leeds_arcgis_etl.census_main._geometry_summary",
                side_effect=CensusCheckError("pin mismatch"),
            ),
            patch(
                "leeds_arcgis_etl.census_main._topic_summary",
                return_value=topic_summary,
            ),
            redirect_stdout(output),
        ):
            status = _check_sources(
                check_config(),
                (topic,),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["geometry"]["error"], "pin mismatch")


class CensusMainStatusTests(unittest.TestCase):
    def test_invalid_config_and_missing_database_url_return_usage_status(self) -> None:
        self.assertEqual(main(["--config", "/does/not/exist"]), 2)

        with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
            self.assertEqual(main(["--config", str(CONFIG_PATH)]), 2)

    def test_source_check_returns_delegate_status_without_database_url(self) -> None:
        with (
            patch(
                "leeds_arcgis_etl.census_main._check_sources",
                return_value=1,
            ) as check_sources,
            patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False),
        ):
            status = main(
                [
                    "--config",
                    str(CONFIG_PATH),
                    "--check-source",
                    "--topic",
                    "TS001",
                ]
            )

        self.assertEqual(status, 1)
        self.assertEqual(check_sources.call_count, 1)
        self.assertEqual(check_sources.call_args.args[1][0].id, "TS001")

    def test_load_success_prints_json_and_expected_failure_returns_one(self) -> None:
        result = SimpleNamespace(
            run_id="12345678-1234-5678-1234-567812345678",
            target_table="census_2021_england_oa",
            oa_count=178_605,
            topic_count=47,
            variable_count=467,
            geometry_repairs=32,
        )
        output = io.StringIO()
        with (
            patch(
                "leeds_arcgis_etl.census_main._run_load",
                return_value=result,
            ),
            patch.dict(
                os.environ,
                {"DATABASE_URL": "postgresql://example.invalid/mapp"},
                clear=False,
            ),
            redirect_stdout(output),
        ):
            status = main(["--config", str(CONFIG_PATH)])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["geometry_repairs"], 32)

        with (
            patch(
                "leeds_arcgis_etl.census_main._run_load",
                side_effect=CensusLoadError("database unavailable"),
            ),
            patch.dict(
                os.environ,
                {"DATABASE_URL": "postgresql://example.invalid/mapp"},
                clear=False,
            ),
        ):
            self.assertEqual(main(["--config", str(CONFIG_PATH)]), 1)


if __name__ == "__main__":
    unittest.main()
