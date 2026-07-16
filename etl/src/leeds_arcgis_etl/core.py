from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from .config import LayerConfig


class ValidationError(RuntimeError):
    pass


class TransformError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedFeature:
    object_id: int
    values: tuple[Any, ...]
    source_attributes: dict[str, Any]
    geometry: dict[str, Any] | None
    source_hash: str


@dataclass(frozen=True)
class LayerInspection:
    name: str
    source_srid: int
    max_record_count: int
    field_types: dict[str, str]


COMPATIBLE_FIELD_TYPES = {
    "text": {
        "esriFieldTypeString",
        "esriFieldTypeGUID",
        "esriFieldTypeGlobalID",
        "esriFieldTypeXML",
    },
    "integer": {
        "esriFieldTypeSmallInteger",
        "esriFieldTypeInteger",
        "esriFieldTypeOID",
    },
    "bigint": {
        "esriFieldTypeSmallInteger",
        "esriFieldTypeInteger",
        "esriFieldTypeBigInteger",
        "esriFieldTypeOID",
    },
    "double precision": {
        "esriFieldTypeSingle",
        "esriFieldTypeDouble",
        "esriFieldTypeSmallInteger",
        "esriFieldTypeInteger",
    },
    "boolean": {"esriFieldTypeSmallInteger", "esriFieldTypeInteger"},
    "date": {
        "esriFieldTypeDate",
        "esriFieldTypeDateOnly",
        "esriFieldTypeTimestampOffset",
    },
    "timestamptz": {
        "esriFieldTypeDate",
        "esriFieldTypeDateOnly",
        "esriFieldTypeTimestampOffset",
    },
    "jsonb": {
        "esriFieldTypeString",
        "esriFieldTypeBlob",
        "esriFieldTypeRaster",
    },
}


def _source_srid(metadata: dict[str, Any]) -> int | None:
    spatial_reference = metadata.get("sourceSpatialReference") or metadata.get(
        "spatialReference"
    )
    if not isinstance(spatial_reference, dict):
        extent = metadata.get("extent")
        if isinstance(extent, dict):
            spatial_reference = extent.get("spatialReference")
    if not isinstance(spatial_reference, dict):
        return None
    value = spatial_reference.get("latestWkid") or spatial_reference.get("wkid")
    return value if isinstance(value, int) else None


def validate_metadata(layer: LayerConfig, metadata: dict[str, Any]) -> LayerInspection:
    if metadata.get("geometryType") != layer.source_geometry_type:
        raise ValidationError(
            f"{layer.key}: expected {layer.source_geometry_type}, got "
            f"{metadata.get('geometryType')}"
        )
    if metadata.get("hasM") is True:
        raise ValidationError(
            f"{layer.key}: M-valued layers cannot be exported as GeoJSON"
        )
    query_capabilities = metadata.get("advancedQueryCapabilities")
    if not isinstance(query_capabilities, dict) or not query_capabilities.get(
        "supportsPagination"
    ):
        raise ValidationError(f"{layer.key}: source does not advertise pagination")
    if not query_capabilities.get("supportsOrderBy"):
        raise ValidationError(f"{layer.key}: source does not advertise ordered queries")
    formats = str(metadata.get("supportedQueryFormats", "")).lower()
    if "geojson" not in formats:
        raise ValidationError(f"{layer.key}: source does not advertise GeoJSON output")

    source_srid = _source_srid(metadata)
    if source_srid != layer.expected_source_srid:
        raise ValidationError(
            f"{layer.key}: expected source SRID {layer.expected_source_srid}, got "
            f"{source_srid}"
        )

    raw_fields = metadata.get("fields")
    if not isinstance(raw_fields, list):
        raise ValidationError(f"{layer.key}: metadata has no fields array")
    field_types = {
        field.get("name"): field.get("type")
        for field in raw_fields
        if isinstance(field, dict)
        and isinstance(field.get("name"), str)
        and isinstance(field.get("type"), str)
    }
    oid_type = field_types.get(layer.object_id_field)
    if oid_type != "esriFieldTypeOID":
        raise ValidationError(
            f"{layer.key}: {layer.object_id_field} is not an ArcGIS OID field"
        )
    for column in layer.columns:
        source_type = field_types.get(column.source)
        if source_type is None:
            raise ValidationError(
                f"{layer.key}: configured source field {column.source} is absent"
            )
        compatible = COMPATIBLE_FIELD_TYPES[column.postgres_type]
        if source_type not in compatible:
            raise ValidationError(
                f"{layer.key}: cannot safely map {column.source} ({source_type}) to "
                f"{column.postgres_type}"
            )

    max_record_count = metadata.get("maxRecordCount")
    if not isinstance(max_record_count, int) or max_record_count < 1:
        raise ValidationError(f"{layer.key}: invalid maxRecordCount")
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise ValidationError(f"{layer.key}: metadata has no layer name")
    return LayerInspection(
        name=name,
        source_srid=source_srid,
        max_record_count=max_record_count,
        field_types=field_types,
    )


def parse_arcgis_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TransformError(f"boolean is not an ArcGIS date: {value!r}")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise TransformError(f"non-finite ArcGIS epoch: {value!r}")
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise TransformError(f"invalid ArcGIS epoch: {value!r}") from exc
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            numeric = float(stripped)
        except ValueError:
            numeric = None
        if numeric is not None:
            return parse_arcgis_datetime(numeric)
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TransformError(f"invalid ArcGIS date: {value!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise TransformError(f"unsupported ArcGIS date value: {value!r}")


def convert_value(value: Any, postgres_type: str) -> Any:
    if value is None:
        return None
    if postgres_type == "text":
        return value if isinstance(value, str) else str(value)
    if postgres_type in {"integer", "bigint"}:
        if isinstance(value, bool):
            raise TransformError(f"boolean cannot be loaded as {postgres_type}")
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TransformError(
                f"cannot convert {value!r} to {postgres_type}"
            ) from exc
    if postgres_type == "double precision":
        if isinstance(value, bool):
            raise TransformError("boolean cannot be loaded as double precision")
        try:
            converted = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise TransformError(
                f"cannot convert {value!r} to double precision"
            ) from exc
        if not math.isfinite(converted):
            raise TransformError(f"non-finite double value: {value!r}")
        return converted
    if postgres_type == "boolean":
        if value in (True, 1, "1", "true", "TRUE", "yes", "YES"):
            return True
        if value in (False, 0, "0", "false", "FALSE", "no", "NO"):
            return False
        raise TransformError(f"cannot convert {value!r} to boolean")
    if postgres_type == "timestamptz":
        return parse_arcgis_datetime(value)
    if postgres_type == "date":
        timestamp = parse_arcgis_datetime(value)
        return timestamp.date() if timestamp else None
    if postgres_type == "jsonb":
        return value
    raise TransformError(f"unsupported PostgreSQL type: {postgres_type}")


def feature_hash(properties: dict[str, Any], geometry: dict[str, Any] | None) -> str:
    try:
        canonical = json.dumps(
            {"properties": properties, "geometry": geometry},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TransformError(f"feature is not canonical JSON: {exc}") from exc
    return hashlib.sha256(canonical).hexdigest()


def _validate_geometry(layer: LayerConfig, geometry: dict[str, Any] | None) -> None:
    if geometry is None:
        return
    geometry_type = geometry.get("type")
    allowed = {
        "Point": {"Point"},
        "MultiLineString": {"LineString", "MultiLineString"},
        "MultiPolygon": {"Polygon", "MultiPolygon"},
    }[layer.target_geometry_type]
    if geometry_type not in allowed:
        raise TransformError(
            f"{layer.key}: expected one of {sorted(allowed)}, got {geometry_type!r}"
        )
    if "coordinates" not in geometry:
        raise TransformError(f"{layer.key}: geometry has no coordinates")


def prepare_feature(layer: LayerConfig, feature: dict[str, Any]) -> PreparedFeature:
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise TransformError(f"{layer.key}: feature has no properties object")
    raw_object_id = properties.get(layer.object_id_field)
    if isinstance(raw_object_id, bool):
        raise TransformError(f"{layer.key}: invalid boolean object ID")
    try:
        object_id = int(raw_object_id)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TransformError(
            f"{layer.key}: invalid object ID {raw_object_id!r}"
        ) from exc

    geometry = feature.get("geometry")
    if geometry is not None and not isinstance(geometry, dict):
        raise TransformError(f"{layer.key}: invalid geometry object")
    _validate_geometry(layer, geometry)
    values = tuple(
        convert_value(properties.get(column.source), column.postgres_type)
        for column in layer.columns
    )
    return PreparedFeature(
        object_id=object_id,
        values=values,
        source_attributes=properties,
        geometry=geometry,
        source_hash=feature_hash(properties, geometry),
    )
