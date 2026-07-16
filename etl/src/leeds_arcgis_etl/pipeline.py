from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .arcgis import ArcGISClient
from .config import LayerConfig
from .core import prepare_feature, validate_metadata

if TYPE_CHECKING:
    from .database import PostgresStore


LOGGER = logging.getLogger(__name__)


class ConsistencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    layer_key: str
    run_id: uuid.UUID
    rows_seen: int
    rows_deleted: int
    source_count: int


def run_layer(
    layer: LayerConfig,
    client: ArcGISClient,
    store: PostgresStore,
) -> RunResult:
    run_id = uuid.uuid4()
    rows_seen = 0
    if not store.acquire_layer_lock(layer):
        raise ConsistencyError(
            f"{layer.key}: another ETL run already holds the layer lock"
        )
    try:
        store.start_run(layer, run_id)
        try:
            metadata = client.metadata(layer)
            inspection = validate_metadata(layer, metadata)
            expected_count = client.count(layer)
            if expected_count < layer.minimum_source_count:
                raise ConsistencyError(
                    f"{layer.key}: source count {expected_count} is below the "
                    f"configured minimum {layer.minimum_source_count}; no "
                    "pages were loaded and stale-row deletion was skipped"
                )
            store.register_layer(layer, inspection, metadata, run_id, expected_count)
            LOGGER.info(
                "%s: loading %s source records in pages of at most %s",
                layer.key,
                expected_count,
                min(client.page_size, inspection.max_record_count),
            )

            seen_object_ids: set[int] = set()
            for offset, raw_features in client.pages(layer, metadata):
                prepared = [prepare_feature(layer, feature) for feature in raw_features]
                page_ids = [feature.object_id for feature in prepared]
                duplicate_ids = seen_object_ids.intersection(page_ids)
                if duplicate_ids or len(page_ids) != len(set(page_ids)):
                    examples = sorted(duplicate_ids or set(page_ids))[:5]
                    raise ConsistencyError(
                        f"{layer.key}: duplicate object IDs while paginating at offset "
                        f"{offset}: {examples}"
                    )
                seen_object_ids.update(page_ids)
                store.upsert_page(layer, prepared, run_id)
                rows_seen += len(prepared)
                LOGGER.info(
                    "%s: loaded page at offset %s (%s rows; %s total)",
                    layer.key,
                    offset,
                    len(prepared),
                    rows_seen,
                )

            ending_count = client.count(layer)
            if expected_count != rows_seen or ending_count != expected_count:
                raise ConsistencyError(
                    f"{layer.key}: source changed or pagination was incomplete "
                    f"(start count={expected_count}, fetched={rows_seen}, "
                    f"end count={ending_count}); stale-row deletion was skipped"
                )

            rows_deleted = store.reconcile(layer, run_id)
            store.finish_run(
                layer,
                run_id,
                rows_seen=rows_seen,
                rows_deleted=rows_deleted,
                ending_count=ending_count,
            )
            LOGGER.info(
                "%s: run %s succeeded (%s seen, %s deleted)",
                layer.key,
                run_id,
                rows_seen,
                rows_deleted,
            )
            return RunResult(
                layer_key=layer.key,
                run_id=run_id,
                rows_seen=rows_seen,
                rows_deleted=rows_deleted,
                source_count=ending_count,
            )
        except Exception as exc:
            store.fail_run(run_id, str(exc), rows_seen)
            raise
    finally:
        store.release_layer_lock(layer)
