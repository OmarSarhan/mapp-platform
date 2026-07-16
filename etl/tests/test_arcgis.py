from __future__ import annotations

import unittest
from typing import Any

from leeds_arcgis_etl.arcgis import ArcGISClient
from test_core import sample_layer


class FakeArcGISClient(ArcGISClient):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(page_size=2, retries=0)
        self.responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def _request(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((url, params))
        return self.responses.pop(0)


def feature(object_id: int) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "OBJECTID": object_id,
            "NAME": f"feature-{object_id}",
            "WHEN_": None,
            "COUNT_": object_id,
        },
        "geometry": {"type": "Point", "coordinates": [-1.5, 53.8]},
    }


class PaginationTests(unittest.TestCase):
    def test_offset_pagination_is_ordered_and_honours_server_limit(self) -> None:
        client = FakeArcGISClient(
            [
                {
                    "type": "FeatureCollection",
                    "features": [feature(1), feature(2)],
                    "exceededTransferLimit": True,
                },
                {
                    "type": "FeatureCollection",
                    "features": [feature(3)],
                    "exceededTransferLimit": False,
                },
            ]
        )
        pages = list(client.pages(sample_layer(), {"maxRecordCount": 1000}))
        self.assertEqual([offset for offset, _ in pages], [0, 2])
        params = [request[1] for request in client.requests]
        self.assertEqual([item["resultOffset"] for item in params], [0, 2])
        self.assertEqual(params[0]["resultRecordCount"], 2)
        self.assertEqual(params[0]["orderByFields"], "OBJECTID ASC")
        self.assertEqual(params[0]["outSR"], "4326")

    def test_short_page_without_transfer_flag_terminates(self) -> None:
        client = FakeArcGISClient(
            [{"type": "FeatureCollection", "features": [feature(1)]}]
        )
        self.assertEqual(
            len(list(client.pages(sample_layer(), {"maxRecordCount": 2}))), 1
        )


if __name__ == "__main__":
    unittest.main()
