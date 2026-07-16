from __future__ import annotations

import os
import unittest
from pathlib import Path

from leeds_arcgis_etl.arcgis import ArcGISClient
from leeds_arcgis_etl.config import load_config
from leeds_arcgis_etl.core import prepare_feature, validate_metadata


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.getenv("RUN_LIVE_ARCGIS_TESTS") == "1",
    "set RUN_LIVE_ARCGIS_TESTS=1 to query the Leeds endpoint",
)
class LiveSourceTests(unittest.TestCase):
    def test_every_configured_layer_has_valid_metadata_and_geojson(self) -> None:
        config = load_config(ROOT / "config" / "layers.json")
        client = ArcGISClient(
            timeout_seconds=config.http_timeout_seconds,
            retries=config.http_retries,
            page_size=2,
        )
        for layer in config.layers:
            with self.subTest(layer=layer.key):
                metadata = client.metadata(layer)
                validate_metadata(layer, metadata)
                self.assertGreater(client.count(layer), 0)
                _, raw_features = next(client.pages(layer, metadata))
                prepared = [prepare_feature(layer, feature) for feature in raw_features]
                self.assertTrue(prepared)
                self.assertTrue(
                    all(feature.geometry is not None for feature in prepared)
                )


if __name__ == "__main__":
    unittest.main()
