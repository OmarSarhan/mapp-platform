from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from leeds_arcgis_etl.config import ColumnConfig, LayerConfig, load_config
from leeds_arcgis_etl.core import (
    TransformError,
    feature_hash,
    parse_arcgis_datetime,
    prepare_feature,
    validate_metadata,
)


ROOT = Path(__file__).resolve().parents[1]


def sample_layer() -> LayerConfig:
    return LayerConfig(
        key="sample",
        description="sample",
        source_url="https://example.test/MapServer/0",
        target_table="sample",
        where="1=1",
        object_id_field="OBJECTID",
        source_geometry_type="esriGeometryPoint",
        target_geometry_type="Point",
        expected_source_srid=27700,
        columns=(
            ColumnConfig("NAME", "name", "text"),
            ColumnConfig("WHEN_", "observed_at", "timestamptz"),
            ColumnConfig("COUNT_", "count", "integer"),
        ),
    )


def sample_metadata() -> dict[str, object]:
    return {
        "name": "Sample points",
        "geometryType": "esriGeometryPoint",
        "hasM": False,
        "sourceSpatialReference": {"wkid": 27700, "latestWkid": 27700},
        "maxRecordCount": 1000,
        "supportedQueryFormats": "JSON, geoJSON, PBF",
        "advancedQueryCapabilities": {
            "supportsPagination": True,
            "supportsOrderBy": True,
        },
        "fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID"},
            {"name": "NAME", "type": "esriFieldTypeString"},
            {"name": "WHEN_", "type": "esriFieldTypeDate"},
            {"name": "COUNT_", "type": "esriFieldTypeInteger"},
        ],
    }


class ConfigTests(unittest.TestCase):
    def test_baked_and_deployed_manifests_match(self) -> None:
        self.assertEqual(
            (ROOT / "config" / "layers.json").read_bytes(),
            (ROOT.parent / "instance" / "etl" / "layers.json").read_bytes(),
        )

    def test_shipped_config_loads(self) -> None:
        config = load_config(ROOT / "config" / "layers.json")
        self.assertEqual(config.target_schema, "leeds")
        self.assertEqual(
            [layer.key for layer in config.layers],
            [
                "bus_stops",
                "definitive_paths",
                "smoke_control_orders",
            ],
        )
        self.assertTrue(
            all(layer.minimum_source_count > 0 for layer in config.layers)
        )

    def test_polygon_sample_uses_distinct_smoke_control_contract(self) -> None:
        config = load_config(ROOT / "config" / "layers.json")
        layer = next(
            layer for layer in config.layers if layer.key == "smoke_control_orders"
        )
        self.assertEqual(
            layer.source_url,
            "https://mapservices.leeds.gov.uk/arcgis/rest/services/"
            "Public/Planning/MapServer/8",
        )
        self.assertEqual(layer.target_table, "smoke_control_orders")
        self.assertEqual(layer.object_id_field, "OBJECTID")
        self.assertEqual(layer.minimum_source_count, 150)
        self.assertEqual(
            {
                column.source: (column.target, column.postgres_type)
                for column in layer.columns
            },
            {
                "INT_ID": ("source_id", "integer"),
                "INT_VERSION": ("version", "integer"),
                "TLC_CURRENT_FLAG": ("current_flag", "text"),
                "REGISTRATION_DATE": ("registered_at", "timestamptz"),
                "LC_REFERENCE": ("council_reference", "text"),
                "LEGISLATION": ("legislation", "text"),
                "REFERENCE": ("reference", "text"),
                "DESCRIPTION": ("description", "text"),
                "DATED": ("order_date", "timestamptz"),
                "LIVE": ("live_flag", "text"),
                "LV_NAME": ("name", "text"),
                "LOCALITY": ("locality", "text"),
                "SHAPE.AREA": ("area_square_metres", "double precision"),
            },
        )

    def test_minimum_source_count_must_be_non_negative(self) -> None:
        raw = json.loads((ROOT / "config" / "layers.json").read_text())
        raw["layers"][0]["minimum_source_count"] = -1
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "layers.json"
            temporary.write_text(json.dumps(raw))
            with self.assertRaisesRegex(
                ValueError,
                "minimum_source_count",
            ):
                load_config(temporary)


class MetadataTests(unittest.TestCase):
    def test_metadata_contract_is_accepted(self) -> None:
        inspection = validate_metadata(sample_layer(), sample_metadata())
        self.assertEqual(inspection.name, "Sample points")
        self.assertEqual(inspection.source_srid, 27700)

    def test_field_type_drift_is_rejected(self) -> None:
        metadata = sample_metadata()
        metadata["fields"][1]["type"] = "esriFieldTypeDouble"  # type: ignore[index]
        with self.assertRaisesRegex(RuntimeError, "cannot safely map NAME"):
            validate_metadata(sample_layer(), metadata)


class TransformTests(unittest.TestCase):
    def test_epoch_milliseconds_are_utc(self) -> None:
        self.assertEqual(
            parse_arcgis_datetime(1_675_209_600_000),
            datetime(2023, 2, 1, tzinfo=timezone.utc),
        )

    def test_iso_datetime_is_normalized_to_utc(self) -> None:
        self.assertEqual(
            parse_arcgis_datetime("2024-01-01T01:00:00+01:00"),
            datetime(2024, 1, 1, tzinfo=timezone.utc),
        )

    def test_hash_is_independent_of_property_order(self) -> None:
        geometry = {"type": "Point", "coordinates": [-1.5, 53.8]}
        self.assertEqual(
            feature_hash({"a": 1, "b": 2}, geometry),
            feature_hash({"b": 2, "a": 1}, geometry),
        )

    def test_feature_is_typed_and_hashed(self) -> None:
        prepared = prepare_feature(
            sample_layer(),
            {
                "type": "Feature",
                "properties": {
                    "OBJECTID": 7,
                    "NAME": "Example",
                    "WHEN_": 1_675_209_600_000,
                    "COUNT_": 3,
                },
                "geometry": {"type": "Point", "coordinates": [-1.5, 53.8]},
            },
        )
        self.assertEqual(prepared.object_id, 7)
        self.assertEqual(prepared.values[0], "Example")
        self.assertEqual(prepared.values[1], datetime(2023, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(prepared.values[2], 3)
        self.assertEqual(len(prepared.source_hash), 64)

    def test_wrong_geometry_is_rejected(self) -> None:
        with self.assertRaisesRegex(TransformError, "expected one of"):
            prepare_feature(
                sample_layer(),
                {
                    "properties": {
                        "OBJECTID": 7,
                        "NAME": "Example",
                        "WHEN_": None,
                        "COUNT_": 3,
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[-1.5, 53.8], [-1.4, 53.9]],
                    },
                },
            )

    def test_non_finite_json_is_rejected(self) -> None:
        with self.assertRaisesRegex(TransformError, "canonical JSON"):
            feature_hash({"bad": float("nan")}, None)


if __name__ == "__main__":
    unittest.main()
