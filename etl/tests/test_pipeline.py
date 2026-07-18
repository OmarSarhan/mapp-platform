from __future__ import annotations

import unittest
from typing import Any

from leeds_arcgis_etl.arcgis import ArcGISError
from leeds_arcgis_etl.pipeline import ConsistencyError, run_layer
from test_core import sample_layer, sample_metadata


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


class FakeClient:
    page_size = 2

    def __init__(self, counts: list[int], pages: list[list[dict[str, Any]]]) -> None:
        self.counts = counts
        self.raw_pages = pages

    def metadata(self, _layer: Any) -> dict[str, object]:
        return sample_metadata()

    def count(self, _layer: Any) -> int:
        return self.counts.pop(0)

    def pages(self, _layer: Any, _metadata: Any) -> Any:
        offset = 0
        for page in self.raw_pages:
            yield offset, page
            offset += len(page)


class CountErrorClient(FakeClient):
    def __init__(self) -> None:
        super().__init__([], [])

    def count(self, _layer: Any) -> int:
        raise ArcGISError("ArcGIS service error: source unavailable")


class FakeStore:
    def __init__(self) -> None:
        self.upserted: list[int] = []
        self.reconciled = False
        self.finished = False
        self.failed = False
        self.lock_available = True
        self.lock_released = False

    def acquire_layer_lock(self, *_args: Any) -> bool:
        return self.lock_available

    def release_layer_lock(self, *_args: Any) -> None:
        self.lock_released = True

    def start_run(self, *_args: Any) -> None:
        pass

    def register_layer(self, *_args: Any) -> None:
        pass

    def upsert_page(self, _layer: Any, features: Any, _run_id: Any) -> None:
        self.upserted.extend(feature.object_id for feature in features)

    def reconcile(self, *_args: Any) -> int:
        self.reconciled = True
        return 4

    def finish_run(self, *_args: Any, **_kwargs: Any) -> None:
        self.finished = True

    def fail_run(self, *_args: Any) -> None:
        self.failed = True


class PipelineTests(unittest.TestCase):
    def test_complete_scan_reconciles(self) -> None:
        store = FakeStore()
        result = run_layer(
            sample_layer(),
            FakeClient([3, 3], [[feature(1), feature(2)], [feature(3)]]),  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
        )
        self.assertEqual(result.rows_seen, 3)
        self.assertEqual(result.rows_deleted, 4)
        self.assertEqual(store.upserted, [1, 2, 3])
        self.assertTrue(store.reconciled)
        self.assertTrue(store.finished)
        self.assertFalse(store.failed)
        self.assertTrue(store.lock_released)

    def test_count_drift_skips_deletion_and_marks_failed(self) -> None:
        store = FakeStore()
        with self.assertRaisesRegex(ConsistencyError, "stale-row deletion was skipped"):
            run_layer(
                sample_layer(),
                FakeClient([2, 3], [[feature(1), feature(2)]]),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )
        self.assertFalse(store.reconciled)
        self.assertFalse(store.finished)
        self.assertTrue(store.failed)
        self.assertTrue(store.lock_released)

    def test_implausibly_small_source_skips_loading_and_deletion(self) -> None:
        store = FakeStore()
        layer = sample_layer()
        object.__setattr__(layer, "minimum_source_count", 3)
        with self.assertRaisesRegex(ConsistencyError, "configured minimum 3"):
            run_layer(
                layer,
                FakeClient([2], [[feature(1), feature(2)]]),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )
        self.assertEqual(store.upserted, [])
        self.assertFalse(store.reconciled)
        self.assertFalse(store.finished)
        self.assertTrue(store.failed)
        self.assertTrue(store.lock_released)

    def test_source_error_records_failure_without_reconciliation(self) -> None:
        store = FakeStore()
        with self.assertRaisesRegex(ArcGISError, "source unavailable"):
            run_layer(
                sample_layer(),
                CountErrorClient(),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )
        self.assertEqual(store.upserted, [])
        self.assertFalse(store.reconciled)
        self.assertFalse(store.finished)
        self.assertTrue(store.failed)
        self.assertTrue(store.lock_released)

    def test_duplicate_ids_skip_deletion(self) -> None:
        store = FakeStore()
        with self.assertRaisesRegex(ConsistencyError, "duplicate object IDs"):
            run_layer(
                sample_layer(),
                FakeClient([2], [[feature(1)], [feature(1)]]),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )
        self.assertFalse(store.reconciled)
        self.assertTrue(store.failed)
        self.assertTrue(store.lock_released)

    def test_concurrent_run_is_rejected_before_writes(self) -> None:
        store = FakeStore()
        store.lock_available = False
        with self.assertRaisesRegex(ConsistencyError, "another ETL run"):
            run_layer(
                sample_layer(),
                FakeClient([1, 1], [[feature(1)]]),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )
        self.assertEqual(store.upserted, [])
        self.assertFalse(store.reconciled)
        self.assertFalse(store.failed)
        self.assertFalse(store.lock_released)


if __name__ == "__main__":
    unittest.main()
