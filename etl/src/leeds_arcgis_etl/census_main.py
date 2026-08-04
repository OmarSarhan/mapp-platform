from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .arcgis import ArcGISClient, ArcGISError
from .census_config import (
    CensusConfig,
    CensusConfigError,
    CensusTopic,
    load_census_config,
)
from .census_geometry import CensusGeometryAudit, CensusGeometryError
from .core import (
    LayerInspection,
    TransformError,
    ValidationError,
    prepare_feature,
    validate_metadata,
)
from .nomis import NomisClient, NomisError


LOGGER = logging.getLogger(__name__)


class CensusCheckError(RuntimeError):
    pass


class CensusLoadError(RuntimeError):
    pass


def _default_config_path() -> str:
    configured = os.getenv("ETL_CENSUS_CONFIG")
    if configured:
        return configured
    mounted = Path("/config/census.json")
    if mounted.exists():
        return str(mounted)
    return str(Path(__file__).resolve().parents[2] / "config" / "census.json")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load or validate the reviewed England Census 2021 OA dataset"
    )
    parser.add_argument("--config", default=_default_config_path())
    parser.add_argument(
        "--check-source",
        action="store_true",
        help="fully validate pinned live sources without connecting to PostgreSQL",
    )
    parser.add_argument(
        "--topic",
        action="append",
        dest="topics",
        help="check only this topic ID; repeat for more than one",
    )
    args = parser.parse_args(argv)

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_topic in args.topics or ():
        topic_id = raw_topic.strip().upper()
        if not topic_id:
            parser.error("--topic must be a non-empty topic ID")
        if topic_id in seen:
            parser.error(f"duplicate --topic {topic_id}")
        seen.add(topic_id)
        normalized.append(topic_id)
    args.topics = normalized or None

    if args.topics and not args.check_source:
        parser.error("--topic may only be used with --check-source")
    return args


def _selected_topics(
    config: CensusConfig,
    requested: Sequence[str] | None,
) -> tuple[CensusTopic, ...]:
    if not requested:
        return config.topics
    if len(requested) != len(set(requested)):
        raise CensusConfigError("duplicate topic IDs are not allowed")
    by_id = {topic.id: topic for topic in config.topics}
    unknown = set(requested).difference(by_id)
    if unknown:
        raise CensusConfigError(
            f"unknown Census topic IDs: {', '.join(sorted(unknown))}"
        )
    return tuple(by_id[topic_id] for topic_id in requested)


def _geometry_summary(
    config: CensusConfig,
    client: ArcGISClient,
) -> dict[str, Any]:
    layer = config.geometry_layer
    metadata = client.metadata(layer)
    inspection: LayerInspection = validate_metadata(layer, metadata)
    starting_count = client.count(layer)
    if starting_count != config.expected_england_oa_count:
        raise CensusCheckError(
            "Census geometry count does not match the pinned England OA count "
            f"(expected={config.expected_england_oa_count}, "
            f"source={starting_count})"
        )
    try:
        geometry_audit = CensusGeometryAudit(layer, metadata)
        for offset, raw_features in client.pages(layer, metadata):
            for page_index, raw_feature in enumerate(raw_features):
                geometry_audit.add(
                    prepare_feature(layer, raw_feature),
                    offset=offset + page_index,
                )
    except CensusGeometryError as exc:
        raise CensusCheckError(str(exc)) from exc

    ending_count = client.count(layer)
    if (
        geometry_audit.row_count != config.expected_england_oa_count
        or len(geometry_audit.codes) != config.expected_england_oa_count
        or ending_count != config.expected_england_oa_count
    ):
        raise CensusCheckError(
            "Census geometry source changed or pagination was incomplete "
            f"(expected={config.expected_england_oa_count}, "
            f"fetched={geometry_audit.row_count}, "
            f"unique={len(geometry_audit.codes)}, end={ending_count})"
        )
    if geometry_audit.sha256 != config.geometry_sha256:
        raise CensusCheckError(
            "Census geometry source SHA-256 does not match the pinned release "
            f"(expected={config.geometry_sha256}, "
            f"received={geometry_audit.sha256})"
        )
    return {
        "key": layer.key,
        "name": inspection.name,
        "url": layer.source_url,
        "count": geometry_audit.row_count,
        "sha256": geometry_audit.sha256,
        "source_srid": inspection.source_srid,
        "max_record_count": inspection.max_record_count,
        "status": "ok",
    }


def _topic_summary(
    config: CensusConfig,
    topic: CensusTopic,
    client: NomisClient,
) -> dict[str, Any]:
    row_count = 0
    with client.open_topic(topic) as stream:
        for _row in stream.rows:
            row_count += 1
        if row_count != config.expected_england_oa_count:
            raise CensusCheckError(
                f"{topic.id}: expected {config.expected_england_oa_count} "
                f"England OA rows, received {row_count}"
            )
        source = stream.source
        return {
            "id": topic.id,
            "title": topic.title,
            "url": source.source_url,
            "oa_member": source.oa_member,
            "metadata_member": source.metadata_member,
            "metadata_sha256": source.metadata_sha256,
            "archive_bytes": source.archive_bytes,
            "archive_sha256": source.archive_sha256,
            "issued": source.issued,
            "version": source.version,
            "measure_count": len(stream.target_columns),
            "row_count": row_count,
            "status": "ok",
        }


def _check_sources(
    config: CensusConfig,
    topics: Sequence[CensusTopic],
    arcgis_client: ArcGISClient,
    nomis_client: NomisClient,
) -> int:
    failed = False
    try:
        geometry: dict[str, Any] = _geometry_summary(config, arcgis_client)
    except (
        ArcGISError,
        ValidationError,
        TransformError,
        CensusCheckError,
    ) as exc:
        failed = True
        geometry = {
            "key": config.geometry_layer.key,
            "url": config.geometry_layer.source_url,
            "status": "failed",
            "error": str(exc),
        }

    topic_summaries: list[dict[str, Any]] = []
    for topic in topics:
        try:
            topic_summaries.append(_topic_summary(config, topic, nomis_client))
        except (NomisError, CensusCheckError) as exc:
            failed = True
            topic_summaries.append(
                {
                    "id": topic.id,
                    "title": topic.title,
                    "url": topic.source_url,
                    "status": "failed",
                    "error": str(exc),
                }
            )

    output = {
        "mode": "check-source",
        "status": "failed" if failed else "ok",
        "geometry": geometry,
        "topics": topic_summaries,
    }
    print(json.dumps(output, indent=2))
    return 1 if failed else 0


def _run_load(
    config: CensusConfig,
    arcgis_client: ArcGISClient,
    nomis_client: NomisClient,
    database_url: str,
) -> Any:
    # Keep PostgreSQL imports out of the source-only check path.
    from .census_database import CensusDatabaseError, CensusPostgresStore
    from .census_pipeline import CensusConsistencyError, run_census
    from .database import DatabaseError, connect_with_retry
    from psycopg import Error as PsycopgError

    connection = None
    try:
        connection = connect_with_retry(database_url)
        store = CensusPostgresStore(connection, config)
        return run_census(config, arcgis_client, nomis_client, store)
    except (
        DatabaseError,
        CensusDatabaseError,
        CensusConsistencyError,
        PsycopgError,
    ) as exc:
        raise CensusLoadError(str(exc)) from exc
    finally:
        if connection is not None:
            connection.close()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    try:
        config = load_census_config(args.config)
        topics = _selected_topics(config, args.topics)
    except CensusConfigError as exc:
        LOGGER.error("Census configuration error: %s", exc)
        return 2

    arcgis_client = ArcGISClient(
        timeout_seconds=config.http_timeout_seconds,
        retries=config.http_retries,
        page_size=2_000,
    )
    nomis_client = NomisClient(config)

    if args.check_source:
        try:
            return _check_sources(
                config,
                topics,
                arcgis_client,
                nomis_client,
            )
        except Exception:
            LOGGER.exception("unexpected Census source-check failure")
            return 1

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        LOGGER.error("DATABASE_URL is required unless --check-source is used")
        return 2

    try:
        result = _run_load(
            config,
            arcgis_client,
            nomis_client,
            database_url,
        )
    except (NomisError, ArcGISError, ValidationError, TransformError) as exc:
        LOGGER.error("Census ETL failed: %s", exc)
        return 1
    except CensusLoadError as exc:
        LOGGER.error("Census ETL failed: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("unexpected Census ETL failure")
        return 1

    print(
        json.dumps(
            {
                "mode": "load",
                "status": "ok",
                "run_id": str(result.run_id),
                "target_table": result.target_table,
                "oa_count": result.oa_count,
                "topic_count": result.topic_count,
                "variable_count": result.variable_count,
                "geometry_repairs": result.geometry_repairs,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
