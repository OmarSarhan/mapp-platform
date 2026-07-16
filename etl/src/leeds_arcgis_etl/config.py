from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
POSTGRES_TYPES = {
    "text",
    "integer",
    "bigint",
    "double precision",
    "boolean",
    "date",
    "timestamptz",
    "jsonb",
}
GEOMETRY_TYPES = {"Point", "MultiLineString", "MultiPolygon"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ColumnConfig:
    source: str
    target: str
    postgres_type: str


@dataclass(frozen=True)
class LayerConfig:
    key: str
    description: str
    source_url: str
    target_table: str
    where: str
    object_id_field: str
    source_geometry_type: str
    target_geometry_type: str
    expected_source_srid: int
    columns: tuple[ColumnConfig, ...]
    minimum_source_count: int = 1

    @property
    def out_fields(self) -> tuple[str, ...]:
        return (self.object_id_field, *(column.source for column in self.columns))


@dataclass(frozen=True)
class AppConfig:
    target_schema: str
    page_size: int
    http_timeout_seconds: float
    http_retries: int
    layers: tuple[LayerConfig, ...]


def _identifier(value: Any, context: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ConfigError(f"{context} must be a lowercase PostgreSQL identifier")
    return value


def _required_string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _load_column(raw: Any, context: str) -> ColumnConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} must be an object")
    source = _required_string(raw, "source", context)
    target = _identifier(raw.get("target"), f"{context}.target")
    postgres_type = _required_string(raw, "type", context).lower()
    if postgres_type not in POSTGRES_TYPES:
        raise ConfigError(
            f"{context}.type must be one of {', '.join(sorted(POSTGRES_TYPES))}"
        )
    return ColumnConfig(source=source, target=target, postgres_type=postgres_type)


def _load_layer(raw: Any, index: int) -> LayerConfig:
    context = f"layers[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} must be an object")

    source_url = _required_string(raw, "source_url", context).rstrip("/")
    if not source_url.startswith("https://"):
        raise ConfigError(f"{context}.source_url must use HTTPS")

    target_geometry_type = _required_string(raw, "target_geometry_type", context)
    if target_geometry_type not in GEOMETRY_TYPES:
        raise ConfigError(
            f"{context}.target_geometry_type must be one of "
            f"{', '.join(sorted(GEOMETRY_TYPES))}"
        )

    columns_raw = raw.get("columns")
    if not isinstance(columns_raw, list) or not columns_raw:
        raise ConfigError(f"{context}.columns must be a non-empty array")
    columns = tuple(
        _load_column(column, f"{context}.columns[{column_index}]")
        for column_index, column in enumerate(columns_raw)
    )
    source_names = [column.source for column in columns]
    target_names = [column.target for column in columns]
    if len(source_names) != len(set(source_names)):
        raise ConfigError(f"{context} has duplicate source fields")
    if len(target_names) != len(set(target_names)):
        raise ConfigError(f"{context} has duplicate target columns")
    reserved = {
        "object_id",
        "source_attributes",
        "geom",
        "geom_3857",
        "source_hash",
        "first_seen_at",
        "last_changed_at",
        "last_seen_at",
        "last_seen_run_id",
    }
    conflicts = reserved.intersection(target_names)
    if conflicts:
        raise ConfigError(
            f"{context} target columns conflict with managed columns: "
            f"{', '.join(sorted(conflicts))}"
        )

    expected_source_srid = raw.get("expected_source_srid")
    if not isinstance(expected_source_srid, int) or expected_source_srid <= 0:
        raise ConfigError(f"{context}.expected_source_srid must be a positive integer")
    minimum_source_count = raw.get("minimum_source_count", 1)
    if (
        isinstance(minimum_source_count, bool)
        or not isinstance(minimum_source_count, int)
        or minimum_source_count < 0
    ):
        raise ConfigError(
            f"{context}.minimum_source_count must be a non-negative integer"
        )

    return LayerConfig(
        key=_identifier(raw.get("key"), f"{context}.key"),
        description=_required_string(raw, "description", context),
        source_url=source_url,
        target_table=_identifier(raw.get("target_table"), f"{context}.target_table"),
        where=_required_string(raw, "where", context),
        object_id_field=_required_string(raw, "object_id_field", context),
        source_geometry_type=_required_string(raw, "source_geometry_type", context),
        target_geometry_type=target_geometry_type,
        expected_source_srid=expected_source_srid,
        columns=columns,
        minimum_source_count=minimum_source_count,
    )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")

    layers_raw = raw.get("layers")
    if not isinstance(layers_raw, list) or not layers_raw:
        raise ConfigError("layers must be a non-empty array")
    layers = tuple(_load_layer(layer, index) for index, layer in enumerate(layers_raw))
    keys = [layer.key for layer in layers]
    tables = [layer.target_table for layer in layers]
    if len(keys) != len(set(keys)):
        raise ConfigError("layer keys must be unique")
    if len(tables) != len(set(tables)):
        raise ConfigError("target tables must be unique")

    page_size = raw.get("page_size", 500)
    if not isinstance(page_size, int) or not 1 <= page_size <= 10_000:
        raise ConfigError("page_size must be an integer between 1 and 10000")
    timeout = raw.get("http_timeout_seconds", 30)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("http_timeout_seconds must be positive")
    retries = raw.get("http_retries", 4)
    if not isinstance(retries, int) or not 0 <= retries <= 10:
        raise ConfigError("http_retries must be an integer between 0 and 10")

    return AppConfig(
        target_schema=_identifier(raw.get("target_schema"), "target_schema"),
        page_size=page_size,
        http_timeout_seconds=float(timeout),
        http_retries=retries,
        layers=layers,
    )
