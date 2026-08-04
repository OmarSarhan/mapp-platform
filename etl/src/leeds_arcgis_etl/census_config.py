from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import LayerConfig, _load_layer


EXPECTED_ENGLAND_OA_COUNT = 178_605
TARGET_SCHEMA = "leeds"
TARGET_TABLE = "census_2021_england_oa"
NOMIS_URL_TEMPLATE = (
    "https://www.nomisweb.co.uk/output/census/2021/census2021-{slug}.zip"
)
NOMIS_OA_MEMBER_TEMPLATE = "census2021-{slug}-oa.csv"
ONS_GEOMETRY_URL = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Output_Areas_2021_EW_BGC_V2/FeatureServer/0"
)

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOPIC_ID_RE = re.compile(r"^TS\d{3}A?$")
_ROOT_KEYS = {
    "target_schema",
    "target_table",
    "geometry",
    "geometry_sha256",
    "expected_england_oa_count",
    "max_geometry_repairs",
    "http_timeout_seconds",
    "http_retries",
    "max_archive_bytes",
    "spool_memory_bytes",
    "topics",
}
_TOPIC_KEYS = {"id", "title", "value_column_count", "archive_bytes", "sha256"}

# This is an audited catalogue, not a discovery cache. Wales-only TS032-TS036
# are deliberately absent.
REVIEWED_TOPICS: tuple[tuple[str, str, int], ...] = (
    (
        "TS001",
        "Number of usual residents in households and communal establishments",
        3,
    ),
    ("TS002", "Legal partnership status", 18),
    ("TS003", "Household composition", 22),
    ("TS004", "Country of birth", 15),
    ("TS005", "Passports held", 34),
    ("TS006", "Population density", 1),
    ("TS007A", "Age by five-year age bands", 19),
    ("TS008", "Sex", 3),
    ("TS011", "Households by deprivation dimensions", 6),
    ("TS015", "Year of arrival in UK", 13),
    ("TS016", "Length of residence", 6),
    ("TS017", "Household size", 10),
    ("TS018", "Age of arrival in the UK", 19),
    ("TS019", "Migrant Indicator", 5),
    ("TS020", "Number of non-UK short-term residents by sex", 3),
    ("TS021", "Ethnic group", 25),
    ("TS023", "Multiple ethnic group", 6),
    ("TS025", "Household language", 5),
    ("TS026", "Multiple main languages in households", 6),
    ("TS027", "National identity - UK", 20),
    ("TS029", "Proficiency in english", 7),
    ("TS030", "Religion", 10),
    ("TS037", "General health", 6),
    ("TS038", "Disability", 7),
    ("TS039", "Provision of unpaid care", 9),
    ("TS040", "Number of disabled people in the household", 4),
    ("TS041", "Number of Households", 1),
    ("TS044", "Accommodation type", 9),
    ("TS045", "Car or van availability", 5),
    ("TS046", "Central heating", 13),
    ("TS050", "Number of bedrooms", 5),
    ("TS051", "Number of rooms", 10),
    ("TS052", "Occupancy rating for bedrooms", 6),
    ("TS053", "Occupancy rating for rooms", 6),
    ("TS054", "Tenure", 13),
    ("TS055", "Purpose of second address", 10),
    ("TS056", "Second address indicator", 4),
    ("TS058", "Distance travelled to work", 11),
    ("TS059", "Hours worked", 7),
    ("TS061", "Method of travel to work", 12),
    ("TS062", "NS-SeC", 10),
    ("TS063", "Occupation", 10),
    ("TS065", "Unemployment history", 4),
    ("TS066", "Economic activity status", 31),
    ("TS067", "Highest level of qualification", 8),
    ("TS068", "Schoolchildren and full-time students", 3),
    ("TS075", "Multi religion households", 7),
)


class CensusConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CensusTopic:
    id: str
    title: str
    value_column_count: int
    archive_bytes: int
    sha256: str

    @property
    def slug(self) -> str:
        return self.id.lower()

    @property
    def source_url(self) -> str:
        return NOMIS_URL_TEMPLATE.format(slug=self.slug)

    @property
    def oa_member(self) -> str:
        return NOMIS_OA_MEMBER_TEMPLATE.format(slug=self.slug)

    @property
    def target_columns(self) -> tuple[str, ...]:
        return tuple(
            f"{self.slug}_{ordinal:04d}"
            for ordinal in range(1, self.value_column_count + 1)
        )


@dataclass(frozen=True)
class CensusConfig:
    target_schema: str
    target_table: str
    geometry_layer: LayerConfig
    geometry_sha256: str
    expected_england_oa_count: int
    max_geometry_repairs: int
    http_timeout_seconds: float
    http_retries: int
    max_archive_bytes: int
    spool_memory_bytes: int
    topics: tuple[CensusTopic, ...]


def _closed_object(
    raw: Any,
    *,
    keys: set[str],
    context: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CensusConfigError(f"{context} must be an object")
    unknown = set(raw).difference(keys)
    missing = keys.difference(raw)
    if unknown:
        raise CensusConfigError(
            f"{context} has unsupported properties: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise CensusConfigError(
            f"{context} is missing properties: {', '.join(sorted(missing))}"
        )
    return raw


def _positive_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CensusConfigError(f"{context} must be a positive integer")
    return value


def _load_topic(raw: Any, index: int, max_archive_bytes: int) -> CensusTopic:
    context = f"topics[{index}]"
    item = _closed_object(raw, keys=_TOPIC_KEYS, context=context)

    topic_id = item["id"]
    if not isinstance(topic_id, str) or not _TOPIC_ID_RE.fullmatch(topic_id):
        raise CensusConfigError(f"{context}.id must match TS### or TS###A")
    title = item["title"]
    if not isinstance(title, str) or not title.strip() or title != title.strip():
        raise CensusConfigError(f"{context}.title must be a trimmed non-empty string")
    value_column_count = _positive_integer(
        item["value_column_count"], f"{context}.value_column_count"
    )
    archive_bytes = _positive_integer(item["archive_bytes"], f"{context}.archive_bytes")
    if archive_bytes > max_archive_bytes:
        raise CensusConfigError(f"{context}.archive_bytes exceeds max_archive_bytes")
    sha256 = item["sha256"]
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise CensusConfigError(
            f"{context}.sha256 must be a lowercase hexadecimal SHA-256"
        )
    return CensusTopic(
        id=topic_id,
        title=title,
        value_column_count=value_column_count,
        archive_bytes=archive_bytes,
        sha256=sha256,
    )


def _validate_geometry(layer: LayerConfig, target_table: str) -> None:
    expected_columns = (("OA21CD", "oa21cd", "text"),)
    actual_columns = tuple(
        (column.source, column.target, column.postgres_type) for column in layer.columns
    )
    checks = {
        "key": layer.key == "census_2021_oa_geometry",
        "source_url": layer.source_url == ONS_GEOMETRY_URL,
        "target_table": layer.target_table == target_table,
        "where": layer.where == "OA21CD LIKE 'E%'",
        "object_id_field": layer.object_id_field == "FID",
        "source_geometry_type": layer.source_geometry_type == "esriGeometryPolygon",
        "target_geometry_type": layer.target_geometry_type == "MultiPolygon",
        "expected_source_srid": layer.expected_source_srid == 27700,
        "minimum_source_count": (
            layer.minimum_source_count == EXPECTED_ENGLAND_OA_COUNT
        ),
        "columns": actual_columns == expected_columns,
    }
    invalid = [name for name, valid in checks.items() if not valid]
    if invalid:
        raise CensusConfigError(
            "geometry does not match the reviewed ONS OA 2021 source: "
            + ", ".join(invalid)
        )


def load_census_config(path: str | Path) -> CensusConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CensusConfigError(f"cannot read config {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CensusConfigError(f"invalid JSON in {config_path}: {exc}") from exc

    root = _closed_object(raw, keys=_ROOT_KEYS, context="config")
    target_schema = root["target_schema"]
    if (
        not isinstance(target_schema, str)
        or not _IDENTIFIER_RE.fullmatch(target_schema)
        or target_schema != TARGET_SCHEMA
    ):
        raise CensusConfigError(f"target_schema must be {TARGET_SCHEMA!r}")
    target_table = root["target_table"]
    if (
        not isinstance(target_table, str)
        or not _IDENTIFIER_RE.fullmatch(target_table)
        or target_table != TARGET_TABLE
    ):
        raise CensusConfigError(f"target_table must be {TARGET_TABLE!r}")

    expected_count = root["expected_england_oa_count"]
    if expected_count != EXPECTED_ENGLAND_OA_COUNT or isinstance(expected_count, bool):
        raise CensusConfigError(
            f"expected_england_oa_count must be {EXPECTED_ENGLAND_OA_COUNT}"
        )
    max_geometry_repairs = root["max_geometry_repairs"]
    if (
        isinstance(max_geometry_repairs, bool)
        or not isinstance(max_geometry_repairs, int)
        or not 0 <= max_geometry_repairs <= 1_000
    ):
        raise CensusConfigError(
            "max_geometry_repairs must be an integer between 0 and 1000"
        )

    timeout = root["http_timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 300
    ):
        raise CensusConfigError(
            "http_timeout_seconds must be a finite number no greater than 300"
        )
    retries = root["http_retries"]
    if (
        isinstance(retries, bool)
        or not isinstance(retries, int)
        or not 0 <= retries <= 10
    ):
        raise CensusConfigError("http_retries must be an integer between 0 and 10")
    max_archive_bytes = _positive_integer(
        root["max_archive_bytes"], "max_archive_bytes"
    )
    if max_archive_bytes > 2_147_483_648:
        raise CensusConfigError("max_archive_bytes must not exceed 2147483648")
    spool_memory_bytes = _positive_integer(
        root["spool_memory_bytes"], "spool_memory_bytes"
    )
    if spool_memory_bytes > max_archive_bytes:
        raise CensusConfigError("spool_memory_bytes must not exceed max_archive_bytes")

    try:
        geometry_layer = _load_layer(root["geometry"], 0)
    except ValueError as exc:
        raise CensusConfigError(f"invalid geometry: {exc}") from exc
    _validate_geometry(geometry_layer, target_table)
    geometry_sha256 = root["geometry_sha256"]
    if (
        not isinstance(geometry_sha256, str)
        or not _SHA256_RE.fullmatch(geometry_sha256)
    ):
        raise CensusConfigError(
            "geometry_sha256 must be a lowercase hexadecimal SHA-256"
        )

    topics_raw = root["topics"]
    if not isinstance(topics_raw, list):
        raise CensusConfigError("topics must be an array")
    topics = tuple(
        _load_topic(topic, index, max_archive_bytes)
        for index, topic in enumerate(topics_raw)
    )
    reviewed = tuple(
        (topic.id, topic.title, topic.value_column_count) for topic in topics
    )
    if reviewed != REVIEWED_TOPICS:
        raise CensusConfigError(
            "topics must exactly match the reviewed England Census 2021 catalogue"
        )
    if sum(topic.value_column_count for topic in topics) != 467:
        raise CensusConfigError("reviewed topic value-column total must be 467")

    return CensusConfig(
        target_schema=target_schema,
        target_table=target_table,
        geometry_layer=geometry_layer,
        geometry_sha256=geometry_sha256,
        expected_england_oa_count=expected_count,
        max_geometry_repairs=max_geometry_repairs,
        http_timeout_seconds=float(timeout),
        http_retries=retries,
        max_archive_bytes=max_archive_bytes,
        spool_memory_bytes=spool_memory_bytes,
        topics=topics,
    )
