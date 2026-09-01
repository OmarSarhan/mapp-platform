from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .arcgis import ArcGISClient
from .census_database import (
    CensusDatasetMetadata,
    CensusPostgresStore,
    CensusRepairExtent,
    CensusVariableMetadata,
)
from .census_geometry import CensusGeometryAudit, CensusGeometryError, OA_CODE_RE
from .core import prepare_feature, validate_metadata

if TYPE_CHECKING:
    from .census_config import CensusConfig
    from .nomis import NomisClient


LOGGER = logging.getLogger(__name__)


class CensusConsistencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class CensusRunResult:
    run_id: uuid.UUID
    target_table: str
    oa_count: int
    geometry_repairs: int
    topic_count: int
    variable_count: int


def _compact_source_metadata(source: Any) -> dict[str, Any]:
    return {
        "source_url": source.source_url,
        "oa_member": source.oa_member,
        "metadata_member": source.metadata_member,
        "metadata_sha256": source.metadata_sha256,
        "archive_sha256": source.archive_sha256,
        "archive_bytes": source.archive_bytes,
        "title": source.title,
        "issued": source.issued,
        "version": source.version,
    }


def _clean_up(
    store: CensusPostgresStore,
    stages: list[str],
    run_id: uuid.UUID,
    run_started: bool,
    error: Exception,
) -> None:
    if stages:
        try:
            store.cleanup_stages(stages)
        except Exception:
            LOGGER.exception("could not clean Census staging tables for %s", run_id)
    if run_started:
        try:
            store.fail_run(run_id, str(error))
        except Exception:
            LOGGER.exception("could not record failed Census run %s", run_id)


def run_census(
    config: CensusConfig,
    arcgis_client: ArcGISClient,
    nomis_client: NomisClient,
    store: CensusPostgresStore,
    repair_extent: CensusRepairExtent | None = None,
) -> CensusRunResult:
    """Load and validate Census 2021 OA geometry and statistics, then publish."""

    run_id = uuid.uuid4()
    expected_count = config.expected_england_oa_count
    layer = config.geometry_layer
    stages: list[str] = []
    run_started = False

    if not store.acquire_lock():
        raise CensusConsistencyError(
            "another Census ETL run already holds the dataset lock"
        )
    try:
        try:
            store.initialize()
            store.start_run(run_id)
            run_started = True

            geometry_stage = store.create_geometry_stage(run_id)
            stages.append(geometry_stage)

            metadata = arcgis_client.metadata(layer)
            validate_metadata(layer, metadata)
            starting_count = arcgis_client.count(layer)
            if starting_count != expected_count:
                raise CensusConsistencyError(
                    "Census geometry source count does not match the pinned "
                    f"England OA count (expected={expected_count}, "
                    f"source={starting_count})"
                )

            try:
                geometry_audit = CensusGeometryAudit(layer, metadata)
            except CensusGeometryError as exc:
                raise CensusConsistencyError(str(exc)) from exc
            for offset, raw_features in arcgis_client.pages(layer, metadata):
                prepared = [
                    prepare_feature(layer, feature) for feature in raw_features
                ]
                try:
                    for page_index, feature in enumerate(prepared):
                        geometry_audit.add(
                            feature,
                            offset=offset + page_index,
                        )
                except CensusGeometryError as exc:
                    raise CensusConsistencyError(str(exc)) from exc
                store.insert_geometry_page(geometry_stage, prepared)

            ending_count = arcgis_client.count(layer)
            if (
                geometry_audit.row_count != expected_count
                or len(geometry_audit.codes) != expected_count
                or ending_count != expected_count
            ):
                raise CensusConsistencyError(
                    "Census geometry source changed or pagination was incomplete "
                    f"(expected={expected_count}, "
                    f"fetched={geometry_audit.row_count}, "
                    f"unique={len(geometry_audit.codes)}, end={ending_count})"
                )
            geometry_sha256 = geometry_audit.sha256
            if geometry_sha256 != config.geometry_sha256:
                raise CensusConsistencyError(
                    "Census geometry source SHA-256 does not match the pinned "
                    f"release (expected={config.geometry_sha256}, "
                    f"received={geometry_sha256})"
                )
            geometry_repair_candidates = store.validate_geometry(
                geometry_stage,
                expected_count,
                config.max_geometry_repairs,
                repair_extent=repair_extent,
            )
            geometry_repairs = len(geometry_repair_candidates)
            if geometry_repairs:
                LOGGER.warning(
                    "repaired %s invalid official OA geometries with "
                    "ST_MakeValid (reviewed maximum %s)",
                    geometry_repairs,
                    config.max_geometry_repairs,
                )
            store.record_progress(
                run_id,
                geometry_rows=geometry_audit.row_count,
                geometry_repairs=geometry_repairs,
                topics_loaded=0,
            )

            topic_stages: list[tuple[str, tuple[str, ...]]] = []
            variable_metadata: list[CensusVariableMetadata] = []
            topic_sources: list[dict[str, Any]] = []

            for topic_index, topic in enumerate(config.topics):
                topic_stage = store.create_topic_stage(
                    run_id,
                    topic_index,
                    topic.target_columns,
                )
                stages.append(topic_stage)
                topic_columns = tuple(topic.target_columns)
                topic_seen: set[str] = set()

                with nomis_client.open_topic(topic) as stream:
                    stream_columns = tuple(stream.target_columns)
                    labels = tuple(stream.labels)
                    if stream_columns != topic_columns:
                        raise CensusConsistencyError(
                            f"{topic.id}: streamed target columns differ from "
                            "the pinned topic configuration"
                        )
                    if len(labels) != len(topic_columns):
                        raise CensusConsistencyError(
                            f"{topic.id}: expected {len(topic_columns)} labels, "
                            f"received {len(labels)}"
                        )

                    def checked_rows() -> Any:
                        for row in stream.rows:
                            code = row.oa_code
                            if not OA_CODE_RE.fullmatch(code):
                                raise CensusConsistencyError(
                                    f"{topic.id}: invalid England OA code "
                                    f"{code!r}"
                                )
                            if code in topic_seen:
                                raise CensusConsistencyError(
                                    f"{topic.id}: duplicate OA code {code}"
                                )
                            if len(row.values) != len(topic_columns):
                                raise CensusConsistencyError(
                                    f"{topic.id}: {code} has "
                                    f"{len(row.values)} values; expected "
                                    f"{len(topic_columns)}"
                                )
                            topic_seen.add(code)
                            yield (code, *row.values)

                    # The archive is already local. COPY keeps one transaction
                    # open only while parsing this topic's local CSV.
                    store.copy_topic_rows(
                        topic_stage,
                        topic_columns,
                        checked_rows(),
                    )

                    missing = geometry_audit.codes - topic_seen
                    extra = topic_seen - geometry_audit.codes
                    if missing or extra:
                        raise CensusConsistencyError(
                            f"{topic.id}: OA code set differs from geometry "
                            f"(missing={len(missing)}, extra={len(extra)}, "
                            f"missing_examples={sorted(missing)[:3]}, "
                            f"extra_examples={sorted(extra)[:3]})"
                        )
                    store.apply_topic(
                        geometry_stage,
                        topic_stage,
                        topic_columns,
                        expected_count,
                    )

                    source = stream.source
                    compact_source_details = {
                        "topic_id": topic.id,
                        **_compact_source_metadata(source),
                    }
                    source_details = {
                        **compact_source_details,
                        "metadata_text": source.metadata_text,
                    }
                    variables = [
                        CensusVariableMetadata(
                            column_name=column,
                            topic_id=topic.id,
                            topic_title=topic.title,
                            ordinal=ordinal,
                            label=label,
                            source_url=source.source_url,
                            source_member=source.oa_member,
                            source_sha256=source.archive_sha256,
                            source_metadata=compact_source_details,
                        )
                        for ordinal, (column, label) in enumerate(
                            zip(topic_columns, labels, strict=True),
                            start=1,
                        )
                    ]

                # The context manager performs its own completion checks. Publish
                # metadata only after it has exited successfully.
                topic_stages.append((topic_stage, topic_columns))
                variable_metadata.extend(variables)
                topic_sources.append(source_details)
                store.record_progress(
                    run_id,
                    geometry_rows=geometry_audit.row_count,
                    geometry_repairs=geometry_repairs,
                    topics_loaded=topic_index + 1,
                )

            wide_stage = store.assemble_wide_stage(
                run_id,
                geometry_stage,
                topic_stages,
                expected_count,
            )
            stages.append(wide_stage)

            # Once the write-once wide snapshot exists, the geometry and 47
            # narrow topic tables are no longer needed.
            source_stages = [geometry_stage, *(stage for stage, _ in topic_stages)]
            store.cleanup_stages(source_stages)
            stages = [wide_stage]

            dataset = CensusDatasetMetadata(
                dataset_key=config.target_table,
                oa_count=expected_count,
                variable_count=len(variable_metadata),
                geometry_repairs=geometry_repairs,
                geometry_source_url=layer.source_url,
                geometry_source_sha256=geometry_sha256,
                source_metadata={
                    "release": "Census 2021",
                    "geography": "2021 output areas in England",
                    "geometry": {
                        "source_url": layer.source_url,
                        "sha256": geometry_sha256,
                        "repairs": geometry_repairs,
                        "repair_extent": (
                            list(repair_extent)
                            if repair_extent is not None
                            else None
                        ),
                        "repair_candidates": list(
                            geometry_repair_candidates
                        ),
                        "arcgis_metadata": metadata,
                    },
                    "topics": topic_sources,
                },
            )
            if dataset.variable_count != len(store.statistic_columns):
                raise CensusConsistencyError(
                    "Census variable metadata does not match target columns "
                    f"(metadata={dataset.variable_count}, "
                    f"columns={len(store.statistic_columns)})"
                )
            store.publish(wide_stage, run_id, dataset, variable_metadata)
            stages.clear()

            return CensusRunResult(
                run_id=run_id,
                target_table=config.target_table,
                oa_count=expected_count,
                geometry_repairs=geometry_repairs,
                topic_count=len(config.topics),
                variable_count=dataset.variable_count,
            )
        except Exception as exc:
            _clean_up(store, stages, run_id, run_started, exc)
            raise
    finally:
        store.release_lock()
