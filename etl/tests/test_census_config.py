from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from leeds_arcgis_etl.census_config import (
    EXPECTED_ENGLAND_OA_COUNT,
    NOMIS_OA_MEMBER_TEMPLATE,
    NOMIS_URL_TEMPLATE,
    ONS_GEOMETRY_URL,
    REVIEWED_TOPICS,
    CensusConfigError,
    load_census_config,
)


ROOT = Path(__file__).resolve().parents[2]
BAKED_CONFIG = ROOT / "etl" / "config" / "census.json"
INSTANCE_CONFIG = ROOT / "instance" / "etl" / "census.json"


class CensusConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = json.loads(BAKED_CONFIG.read_text(encoding="utf-8"))

    def _load_modified(self, mutate) -> None:
        raw = copy.deepcopy(self.raw)
        mutate(raw)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "census.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            load_census_config(path)

    def test_baked_and_instance_manifests_are_identical_and_reviewed(self) -> None:
        self.assertEqual(
            BAKED_CONFIG.read_bytes(),
            INSTANCE_CONFIG.read_bytes(),
        )
        config = load_census_config(BAKED_CONFIG)

        self.assertEqual(config.target_schema, "leeds")
        self.assertEqual(config.target_table, "census_2021_england_oa")
        self.assertEqual(
            config.expected_england_oa_count,
            EXPECTED_ENGLAND_OA_COUNT,
        )
        self.assertEqual(config.max_geometry_repairs, 64)
        self.assertEqual(
            config.geometry_sha256,
            "db07dd3f8f6846177cbbd5be6955cd2665ddb1ef498c2a8ceff5b5c11274d0b4",
        )
        self.assertEqual(
            tuple(
                (topic.id, topic.title, topic.value_column_count)
                for topic in config.topics
            ),
            REVIEWED_TOPICS,
        )
        self.assertEqual(len(config.topics), 47)
        self.assertEqual(
            sum(topic.value_column_count for topic in config.topics),
            467,
        )
        self.assertEqual(
            len({topic.sha256 for topic in config.topics}),
            len(config.topics),
        )

    def test_topics_derive_only_the_official_url_member_and_target_names(self) -> None:
        config = load_census_config(BAKED_CONFIG)
        first = config.topics[0]
        age = config.topics[6]

        self.assertEqual(
            first.source_url,
            NOMIS_URL_TEMPLATE.format(slug="ts001"),
        )
        self.assertEqual(
            age.oa_member,
            NOMIS_OA_MEMBER_TEMPLATE.format(slug="ts007a"),
        )
        self.assertEqual(
            first.target_columns,
            ("ts001_0001", "ts001_0002", "ts001_0003"),
        )

    def test_geometry_is_the_reviewed_ons_england_oa_layer(self) -> None:
        layer = load_census_config(BAKED_CONFIG).geometry_layer

        self.assertEqual(layer.source_url, ONS_GEOMETRY_URL)
        self.assertEqual(layer.where, "OA21CD LIKE 'E%'")
        self.assertEqual(layer.object_id_field, "FID")
        self.assertEqual(layer.minimum_source_count, EXPECTED_ENGLAND_OA_COUNT)
        self.assertEqual(
            tuple(
                (column.source, column.target, column.postgres_type)
                for column in layer.columns
            ),
            (("OA21CD", "oa21cd", "text"),),
        )

    def test_closed_contract_rejects_unknown_and_missing_properties(self) -> None:
        with self.subTest("unknown root"):
            with self.assertRaisesRegex(
                CensusConfigError, "unsupported properties: surprise"
            ):
                self._load_modified(lambda raw: raw.__setitem__("surprise", True))
        with self.subTest("missing topic pin"):
            with self.assertRaisesRegex(
                CensusConfigError, "missing properties: sha256"
            ):
                self._load_modified(lambda raw: raw["topics"][0].pop("sha256"))
        with self.subTest("missing geometry pin"):
            with self.assertRaisesRegex(
                CensusConfigError, "missing properties: geometry_sha256"
            ):
                self._load_modified(lambda raw: raw.pop("geometry_sha256"))

    def test_catalogue_cannot_be_discovered_or_silently_changed(self) -> None:
        mutations = (
            lambda raw: raw["topics"].pop(),
            lambda raw: raw["topics"][0].__setitem__("title", "Changed"),
            lambda raw: raw["topics"][0].__setitem__("value_column_count", 4),
            lambda raw: raw["topics"].reverse(),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                with self.assertRaisesRegex(
                    CensusConfigError,
                    "exactly match the reviewed",
                ):
                    self._load_modified(mutate)

    def test_invalid_pins_and_bounds_are_rejected(self) -> None:
        cases = (
            (
                lambda raw: raw["topics"][0].__setitem__("sha256", "A" * 64),
                "lowercase hexadecimal",
            ),
            (
                lambda raw: raw.__setitem__("geometry_sha256", "A" * 64),
                "geometry_sha256 must be a lowercase hexadecimal",
            ),
            (
                lambda raw: raw.__setitem__("geometry_sha256", "a" * 63),
                "geometry_sha256 must be a lowercase hexadecimal",
            ),
            (
                lambda raw: raw["topics"][0].__setitem__("archive_bytes", 0),
                "positive integer",
            ),
            (
                lambda raw: raw["topics"][0].__setitem__(
                    "archive_bytes", raw["max_archive_bytes"] + 1
                ),
                "exceeds max_archive_bytes",
            ),
            (
                lambda raw: raw.__setitem__("spool_memory_bytes", 536870913),
                "must not exceed",
            ),
            (
                lambda raw: raw.__setitem__("http_timeout_seconds", float("inf")),
                "finite number",
            ),
            (
                lambda raw: raw.__setitem__("http_retries", 11),
                "between 0 and 10",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CensusConfigError, message):
                    self._load_modified(mutate)

    def test_target_count_and_geometry_are_pinned(self) -> None:
        cases = (
            (
                lambda raw: raw.__setitem__(
                    "expected_england_oa_count",
                    EXPECTED_ENGLAND_OA_COUNT - 1,
                ),
                "expected_england_oa_count",
            ),
            (
                lambda raw: raw.__setitem__("target_schema", "public"),
                "target_schema",
            ),
            (
                lambda raw: raw["geometry"].__setitem__("where", "1=1"),
                "reviewed ONS OA 2021 source: where",
            ),
            (
                lambda raw: raw["geometry"]["columns"][0].__setitem__(
                    "source", "OTHER"
                ),
                "reviewed ONS OA 2021 source: columns",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CensusConfigError, message):
                    self._load_modified(mutate)

    def test_geometry_repair_bound_rejects_invalid_values(self) -> None:
        for invalid in (-1, True, 1_001):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    CensusConfigError,
                    "max_geometry_repairs must be an integer between 0 and 1000",
                ):
                    self._load_modified(
                        lambda raw, value=invalid: raw.__setitem__(
                            "max_geometry_repairs", value
                        )
                    )


if __name__ == "__main__":
    unittest.main()
