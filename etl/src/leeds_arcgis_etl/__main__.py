from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .arcgis import ArcGISClient, ArcGISError
from .config import AppConfig, ConfigError, load_config
from .core import prepare_feature, validate_metadata


LOGGER = logging.getLogger(__name__)


def _default_config_path() -> str:
    configured = os.getenv("ETL_CONFIG")
    if configured:
        return configured
    mounted = Path("/config/layers.json")
    if mounted.exists():
        return str(mounted)
    return str(Path(__file__).resolve().parents[2] / "config" / "layers.json")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load the configured Leeds ArcGIS layers into PostGIS"
    )
    parser.add_argument("--config", default=_default_config_path())
    parser.add_argument(
        "--layer",
        action="append",
        dest="layers",
        help="load/check only this layer key; repeat for more than one",
    )
    parser.add_argument(
        "--check-source",
        action="store_true",
        help="validate live ArcGIS metadata/counts without connecting to PostgreSQL",
    )
    args = parser.parse_args()
    if args.layers is None:
        configured_layers = os.getenv("ETL_LAYER", "")
        args.layers = [
            layer.strip() for layer in configured_layers.split(",") if layer.strip()
        ] or None
    return args


def _selected_config(config: AppConfig, requested: list[str] | None) -> AppConfig:
    if not requested:
        return config
    requested_set = set(requested)
    known = {layer.key for layer in config.layers}
    unknown = requested_set - known
    if unknown:
        raise ConfigError(f"unknown layer keys: {', '.join(sorted(unknown))}")
    return AppConfig(
        target_schema=config.target_schema,
        page_size=config.page_size,
        http_timeout_seconds=config.http_timeout_seconds,
        http_retries=config.http_retries,
        layers=tuple(layer for layer in config.layers if layer.key in requested_set),
    )


def _check_sources(config: AppConfig, client: ArcGISClient) -> int:
    summaries: list[dict[str, object]] = []
    failed = False
    for layer in config.layers:
        try:
            metadata = client.metadata(layer)
            inspection = validate_metadata(layer, metadata)
            count = client.count(layer)
            first_page = next(client.pages(layer, metadata), (0, []))[1]
            prepared = [prepare_feature(layer, feature) for feature in first_page]
            geometry_types = sorted(
                {
                    feature.geometry["type"]
                    for feature in prepared
                    if feature.geometry is not None
                }
            )
            summaries.append(
                {
                    "key": layer.key,
                    "name": inspection.name,
                    "url": layer.source_url,
                    "count": count,
                    "source_srid": inspection.source_srid,
                    "target_geometry_type": layer.target_geometry_type,
                    "max_record_count": inspection.max_record_count,
                    "sampled_records": len(prepared),
                    "sample_geometry_types": geometry_types,
                    "status": "ok",
                }
            )
        except Exception as exc:
            failed = True
            summaries.append(
                {
                    "key": layer.key,
                    "url": layer.source_url,
                    "status": "failed",
                    "error": str(exc),
                }
            )
    print(json.dumps(summaries, indent=2))
    return 1 if failed else 0


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()
    try:
        config = _selected_config(load_config(args.config), args.layers)
    except ConfigError as exc:
        LOGGER.error("configuration error: %s", exc)
        return 2

    client = ArcGISClient(
        timeout_seconds=config.http_timeout_seconds,
        retries=config.http_retries,
        page_size=config.page_size,
    )
    if args.check_source:
        return _check_sources(config, client)

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        LOGGER.error("DATABASE_URL is required unless --check-source is used")
        return 2

    # Imported lazily so source checks and unit tests do not require a DB driver.
    from .database import PostgresStore, connect_with_retry
    from .pipeline import run_layer

    connection = connect_with_retry(database_url)
    store = PostgresStore(connection, config)
    failed = False
    try:
        store.initialize()
        for layer in config.layers:
            try:
                run_layer(layer, client, store)
            except ArcGISError as exc:
                failed = True
                LOGGER.error(
                    "%s failed safely: %s Stale-row reconciliation was skipped.",
                    layer.key,
                    exc,
                )
            except Exception:
                failed = True
                LOGGER.exception("%s failed", layer.key)
    finally:
        store.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
