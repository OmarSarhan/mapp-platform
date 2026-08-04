from __future__ import annotations

import os
import unittest
from pathlib import Path

from leeds_arcgis_etl.arcgis import ArcGISClient
from leeds_arcgis_etl.census_config import load_census_config
from leeds_arcgis_etl.core import prepare_feature, validate_metadata
from leeds_arcgis_etl.nomis import NomisClient


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.getenv("RUN_LIVE_CENSUS_TESTS") == "1",
    "set RUN_LIVE_CENSUS_TESTS=1 to query the reviewed ONS and Nomis sources",
)
class LiveCensusSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_census_config(ROOT / "config" / "census.json")

    def test_geometry_contract_and_sample_features_are_current(self) -> None:
        layer = self.config.geometry_layer
        client = ArcGISClient(
            timeout_seconds=self.config.http_timeout_seconds,
            retries=self.config.http_retries,
            page_size=2,
        )

        metadata = client.metadata(layer)
        validate_metadata(layer, metadata)
        self.assertEqual(
            client.count(layer),
            self.config.expected_england_oa_count,
        )
        _, raw_features = next(client.pages(layer, metadata))
        prepared = [prepare_feature(layer, feature) for feature in raw_features]
        self.assertEqual(len(prepared), 2)
        self.assertTrue(
            all(
                feature.values[0].startswith("E") and feature.geometry is not None
                for feature in prepared
            )
        )

    def test_representative_and_metadata_exception_topics_are_current(self) -> None:
        client = NomisClient(self.config)
        topics = {topic.id: topic for topic in self.config.topics}

        for topic_id in ("TS001", "TS007A"):
            with self.subTest(topic=topic_id):
                topic = topics[topic_id]
                with client.open_topic(topic) as stream:
                    codes = {row.oa_code for row in stream.rows}
                    self.assertEqual(
                        len(codes),
                        self.config.expected_england_oa_count,
                    )
                    self.assertEqual(
                        len(stream.labels),
                        topic.value_column_count,
                    )
                    if topic_id == "TS007A":
                        self.assertIsNone(stream.source.metadata_member)
                        self.assertIsNone(stream.source.issued)
                        self.assertIsNone(stream.source.version)
                    else:
                        self.assertIsNotNone(stream.source.metadata_member)
                        self.assertIsNotNone(stream.source.issued)
                        self.assertIsNotNone(stream.source.version)


if __name__ == "__main__":
    unittest.main()
