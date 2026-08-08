from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from derived_query_guard import (
    H3_GRID_FUNCTIONS,
    H3_POLYGON_FUNCTIONS,
    H3_RING_FUNCTIONS,
    QueryAstInspection,
    QueryGuardViolation,
    h3_polygon_wrapper_is_approved,
    inspect_h3_polygon_wrapper,
    validate_qualified_cast_types,
    validate_query_ast,
    validate_relation_routines,
)


SCHEMA = "derived_layers"
MATERIALIZED_MAX_ESTIMATED_BYTES = 1024 ** 3
MATERIALIZED_ROW_OVERHEAD_BYTES = 32
MATERIALIZED_ESTIMATE_SAFETY_MULTIPLIER = 1.2
MATERIALIZED_PROBE_METHOD = "postgresql-explain"
OUTPUT_VALIDATION_STATEMENT_TIMEOUT = "2min"
QUERY_PLAN_PROBE_METHOD = "postgresql-explain"
QUERY_PLAN_MAX_TOTAL_COST = 50_000_000
QUERY_PLAN_MAX_FINAL_ROWS = 10_000_000
QUERY_PLAN_MAX_INTERMEDIATE_ROWS = 100_000_000
QUERY_PLAN_MAX_INTERMEDIATE_BYTES = 16 * 1024 ** 3
QUERY_PLAN_MAX_JOIN_EXPANSION_RATIO = 1_000
QUERY_PLAN_MAX_NODES = 150
QUERY_PLAN_MAX_DEPTH = 32
QUERY_PLAN_MAX_PLANNED_WORKERS = 8
QUERY_PLANNING_VERSION = "1"
QUERY_PLANNING_METHOD = "postgresql-explain-bounded-generator-pairs"
QUERY_PLANNING_MAX_NESTED_LOOP_PAIR_ROWS = 100_000_000
QUERY_PLANNING_BOUND_PROPAGATING_NODES = frozenset({
    "Aggregate",
    "Gather",
    "Gather Merge",
    "Group",
    "Incremental Sort",
    "Materialize",
    "Memoize",
    "Result",
    "Sort",
    "Subquery Scan",
    "Unique",
    "WindowAgg",
})
QUERY_SHAPE_MAX_JOINS = 12
QUERY_SHAPE_MAX_CTES = 12
QUERY_SHAPE_MAX_SET_OPERATIONS = 8
QUERY_SHAPE_MAX_GROUPING_SETS = 16
QUERY_SHAPE_MAX_GENERATED_ROWS = 100_000
H3_SCOPE_MAX_ESTIMATED_CELLS = 2_000_000
H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS = 10_000_000
H3_SCOPE_ESTIMATE_SAFETY_MULTIPLIER = 1.5
H3_MAX_GRID_DISTANCE = 25
H3_READINESS_METHOD = "postgresql-catalog-and-execution"
H3_READINESS_FAILURES = {
    "extension-discovery": (
        "missing_extensions",
        "Required PostGIS, H3, and H3 PostGIS extensions are not all installed.",
        "Install the supported extensions, then retry the readiness check.",
    ),
    "version-validation": (
        "unsupported_extension_versions",
        "The installed PostGIS and H3 extension versions are not a supported combination.",
        "Use PostGIS 3.5.x and matching H3 and H3 PostGIS 4.2.x versions, then retry.",
    ),
    "catalog-resolution": (
        "wrapper_not_found",
        "The required extension-owned H3 polygon wrapper could not be resolved.",
        "Verify the H3 PostGIS installation and exact geometry wrapper overload, then retry.",
    ),
    "routine-policy": (
        "wrapper_not_approved",
        "The H3 polygon wrapper does not satisfy the derived-query routine policy.",
        "Apply the supported wrapper hardening migration, then retry.",
    ),
    "nested-dependency-resolution": (
        "wrapper_dependencies_unresolved",
        "PostgreSQL could not plan the H3 polygon wrapper with its nested dependencies.",
        "Repair the wrapper search path or extension dependencies, then retry.",
    ),
    "execution-probe": (
        "execution_probe_failed",
        "The bounded H3 readiness probe did not execute successfully.",
        "Check the supported extension installation and wrapper configuration, then retry.",
    ),
    "result-validation": (
        "invalid_probe_result",
        "The bounded H3 readiness probe returned an invalid result.",
        "Repair or reinstall the supported H3 extensions, then retry.",
    ),
}
H3_AVERAGE_HEX_AREA_KM2 = (
    4_357_449.416078381,
    609_788.441794133,
    86_801.780398997,
    12_393.434655088,
    1_770.347654491,
    252.903858182,
    36.129062164,
    5.161293360,
    0.737327598,
    0.105332513,
    0.015047502,
    0.002149643,
    0.000307092,
    0.000043870,
    0.000006267,
    0.000000895,
)
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RELATION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
NUMERIC_FIELD_TYPE_RE = re.compile(
    r"^(?:smallint|integer|bigint|int2|int4|int8|real|float4|float8|"
    r"double\s+precision|numeric(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?|"
    r"decimal(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?)$",
    re.IGNORECASE,
)
AREA_WEIGHTED_H3_RECIPE_NAME = "area-weighted-h3"
AREA_WEIGHTED_H3_RECIPE_VERSION = 1
AREA_WEIGHTED_H3_AREA_SRID = 27700
AREA_WEIGHTED_H3_MAX_MEASURES = 32
AREA_WEIGHTED_H3_OUTPUT_COLUMNS = frozenset({
    "h3_id", "h3_resolution", "geom_3857",
})
FORBIDDEN_SQL = re.compile(
    r"\b(?:alter|call|comment|copy|create|delete|do|drop|execute|grant|insert|"
    r"listen|merge|notify|refresh|reset|revoke|set|truncate|update|vacuum)\b",
    re.IGNORECASE,
)
class DerivedLayerError(ValueError):
    pass


class DerivedLayerDatabaseOperationError(Exception):
    """Database failure with authoritative derived-mutation state."""

    def __init__(
        self,
        cause: psycopg.Error,
        *,
        failure_phase: str,
        state_unchanged: bool,
        rolled_back: bool = False,
        indeterminate: bool = False,
    ):
        self.cause = cause
        self.failure_phase = failure_phase
        self.state_unchanged = state_unchanged
        self.rolled_back = rolled_back
        self.indeterminate = indeterminate
        super().__init__("Derived-layer database operation failed.")


class DerivedLayerCancellationRequested(DerivedLayerError):
    """A background mutation was cancelled before its transaction committed."""


class DerivedLayerCancellation:
    """Coordinate cancellation with one background database transaction."""

    def __init__(self):
        self._lock = threading.Lock()
        self._requested = threading.Event()
        self._connection = None
        self._database_finished = False

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def bind(self, connection) -> None:
        with self._lock:
            self._connection = connection
            requested = self._requested.is_set()
        if requested:
            raise DerivedLayerCancellationRequested(
                "The derived-layer operation was cancelled."
            )

    def checkpoint(self) -> None:
        if self._requested.is_set():
            raise DerivedLayerCancellationRequested(
                "The derived-layer operation was cancelled."
            )

    def request(self) -> bool:
        """Request rollback, returning false once the database phase is over."""
        with self._lock:
            if self._database_finished:
                return False
            self._requested.set()
            connection = self._connection
        if connection is not None:
            try:
                connection.cancel_safe(timeout=5.0)
            except psycopg.Error:
                # The pre-commit checkpoint still guarantees rollback if the
                # cancel packet cannot interrupt the active statement.
                pass
        return True

    def finish_database(self) -> None:
        with self._lock:
            self._connection = None
            self._database_finished = True


class DerivedLayerResetOwnershipError(DerivedLayerError):
    pass


class DerivedLayerMaintenanceError(DerivedLayerError):
    pass


class DerivedLayerContentionError(DerivedLayerError):
    """A competing database transaction owns derived-mutation admission."""

    def __init__(self, contention_scope: str):
        if contention_scope != "derived-mutation":
            raise ValueError("Invalid derived-layer contention scope.")
        self.contention_scope = contention_scope
        super().__init__(
            "Another derived-layer database transaction is active."
        )


class DerivedLayerMaterializationTooLarge(DerivedLayerError):
    def __init__(self, name: str, probe: dict[str, Any]):
        self.name = name
        self.probe = probe
        if "actualBytes" in probe:
            message = (
                f"Materialized derived layer {name!r} uses "
                f"{probe['actualBytes'] / (1024 ** 3):.2f} GiB after "
                "population and indexing, above the "
                f"{probe['maxEstimatedBytes'] / (1024 ** 3):.2f} GiB limit."
            )
        else:
            message = (
                f"Materialized derived layer {name!r} is estimated at "
                f"{probe['estimatedBytes'] / (1024 ** 3):.2f} GiB, above the "
                f"{probe['maxEstimatedBytes'] / (1024 ** 3):.2f} GiB limit."
            )
        super().__init__(message)


class DerivedLayerQueryTooExpensive(DerivedLayerError):
    def __init__(
        self,
        name: str,
        probe: dict[str, Any],
        reasons: list[dict[str, str]],
        *,
        query_planning_probe: dict[str, Any] | None = None,
    ):
        self.name = name
        self.probe = probe
        self.reasons = reasons
        self.query_planning_probe = query_planning_probe
        super().__init__(
            f"Derived layer {name!r} query is too expensive: "
            + "; ".join(reason["message"] for reason in reasons)
        )


class DerivedLayerSourceMismatchError(DerivedLayerError):
    def __init__(
        self,
        declared_sources: list[str],
        resolved_sources: list[str],
    ):
        self.declared_sources = sorted(set(declared_sources))
        self.resolved_sources = sorted(set(resolved_sources))
        self.missing_sources = sorted(
            set(self.resolved_sources) - set(self.declared_sources)
        )
        self.extra_sources = sorted(
            set(self.declared_sources) - set(self.resolved_sources)
        )
        details = []
        if self.missing_sources:
            details.append("add " + ", ".join(self.missing_sources))
        if self.extra_sources:
            details.append("remove " + ", ".join(self.extra_sources))
        super().__init__(
            "Declared sources do not match PostgreSQL dependencies"
            + (": " + "; ".join(details) if details else ".")
        )


class DerivedLayerDependencyError(DerivedLayerError):
    def __init__(self, name: str, dependents: list[str], *, removed_columns=None, dependent_columns=None):
        self.name = name
        self.dependents = dependents
        self.removed_columns = removed_columns or []
        self.dependent_columns = dependent_columns or []
        super().__init__(
            f"derived_layers.{name} is used by other PostgreSQL objects and "
            "cannot be replaced or dropped."
        )


def _relation(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not RELATION_RE.fullmatch(value):
        raise DerivedLayerError(
            "Source relations must be schema-qualified identifiers."
        )
    return tuple(value.split(".", 1))  # type: ignore[return-value]


def _closed_recipe_object(
    value: Any,
    *,
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DerivedLayerError(f"{label} must be an object.")
    optional = optional or set()
    missing = sorted(required - set(value))
    if missing:
        raise DerivedLayerError(
            f"{label} is missing required properties: " + ", ".join(missing)
        )
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise DerivedLayerError(
            f"Unknown {label} properties: " + ", ".join(unknown)
        )
    return value


def _recipe_identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > 63
    ):
        raise DerivedLayerError(
            f"{label} must be a non-empty PostgreSQL identifier of at most "
            "63 bytes."
        )
    return value


def _recipe_profile_field(
    fields_by_name: dict[str, dict[str, Any]],
    name: Any,
    label: str,
) -> dict[str, Any]:
    name = _recipe_identifier(name, label)
    field = fields_by_name.get(name)
    if field is None:
        raise DerivedLayerError(
            f"{label} {name!r} is not present in the ready semantic profile."
        )
    field_type = field.get("type")
    if not isinstance(field_type, str) or not field_type.strip():
        raise DerivedLayerError(f"{label} {name!r} has invalid type metadata.")
    nullable = field.get("nullable")
    if not isinstance(nullable, bool):
        raise DerivedLayerError(
            f"{label} {name!r} has invalid nullable metadata."
        )
    for key in ("primaryKey", "unique"):
        if key in field and not isinstance(field[key], bool):
            raise DerivedLayerError(
                f"{label} {name!r} has invalid {key} metadata."
            )
    field_id = field.get("id")
    if field_id is not None and (
        not isinstance(field_id, str)
        or not field_id.strip()
        or field_id != field_id.strip()
        or len(field_id) > 200
    ):
        raise DerivedLayerError(f"{label} {name!r} has invalid field ID metadata.")
    return field


def _resolved_recipe_field(field: dict[str, Any]) -> dict[str, Any]:
    resolved = {
        "name": field["name"],
        "type": field["type"],
        "nullable": field["nullable"],
    }
    if "id" in field:
        resolved["id"] = field["id"]
    for key in ("primaryKey", "unique", "geometryType", "srid"):
        if key in field:
            resolved[key] = field[key]
    return resolved


def _area_weighted_h3_query(
    *,
    relation: tuple[str, str],
    geometry_column: str,
    geometry_srid: int,
    resolution: int,
    measures: list[dict[str, str]],
) -> str:
    source_geometry = sql.SQL("source.{}").format(
        sql.Identifier(geometry_column)
    )
    measure_projections = []
    pair_measure_columns = []
    weighted_aggregates = []
    final_measure_columns = []
    for index, measure in enumerate(measures, start=1):
        internal_name = f"measure_{index}"
        source_measure = sql.SQL("source.{}::double precision").format(
            sql.Identifier(measure["sourceColumn"])
        )
        if measure["nullHandling"] == "zero":
            source_measure = sql.SQL("COALESCE({}, 0.0)").format(
                source_measure
            )
        measure_projections.append(
            sql.SQL("{} AS {}").format(
                source_measure,
                sql.Identifier(internal_name),
            )
        )
        pair_measure_columns.append(sql.Identifier(internal_name))
        weighted_aggregates.append(sql.SQL(
            "SUM({measure} * intersection_area_m2 / "
            "NULLIF(source_area_m2, 0.0))::double precision AS {output}"
        ).format(
            measure=sql.Identifier(internal_name),
            output=sql.Identifier(measure["outputColumn"]),
        ))
        final_measure_columns.append(
            sql.Identifier(measure["outputColumn"])
        )

    source_relation = sql.SQL("{}.{}").format(
        sql.Identifier(relation[0]),
        sql.Identifier(relation[1]),
    )
    source_scope_geometry = (
        sql.SQL("_mapp_h3_scope.geom_4326")
        if geometry_srid == 4326
        else sql.SQL("public.ST_Transform({}, {})").format(
            sql.SQL("_mapp_h3_scope.geom_4326"),
            sql.Literal(geometry_srid),
        )
    )
    bounded_source_preparation = sql.SQL("""
        source_scope AS MATERIALIZED (
          SELECT {source_scope_geometry} AS geom_source
          FROM _mapp_h3_scope
        ),
        bounded_sources AS MATERIALIZED (
          SELECT
            {measure_projections},
            {source_geometry} AS source_geom
          FROM {source_relation} AS source
          WHERE {source_geometry} IS NOT NULL
            AND EXISTS (
              SELECT 1
              FROM source_scope
              WHERE {source_geometry} && source_scope.geom_source
                AND public.ST_Intersects(
                      {source_geometry},
                      source_scope.geom_source
                    )
            )
        ),
    """).format(
        source_scope_geometry=source_scope_geometry,
        measure_projections=sql.SQL(",\n            ").join(
            measure_projections
        ),
        source_geometry=source_geometry,
        source_relation=source_relation,
    )
    matched_measure_projections = [
        sql.SQL("source.{}").format(column)
        for column in pair_measure_columns
    ]
    if geometry_srid == AREA_WEIGHTED_H3_AREA_SRID:
        source_preparation = bounded_source_preparation
        matched_source = sql.SQL("bounded_sources")
        metric_geometry = sql.SQL("source.source_geom")
    else:
        source_preparation = bounded_source_preparation + sql.SQL("""
        metric_sources AS MATERIALIZED (
          SELECT
            {pair_measure_columns},
            public.ST_Transform(source_geom, {area_srid})
              AS source_geom_27700
          FROM bounded_sources
        ),
        """).format(
            pair_measure_columns=sql.SQL(",\n            ").join(
                pair_measure_columns
            ),
            area_srid=sql.Literal(AREA_WEIGHTED_H3_AREA_SRID),
        )
        matched_source = sql.SQL("metric_sources")
        metric_geometry = sql.SQL("source.source_geom_27700")

    query = sql.SQL("""
        WITH candidate_ids AS (
          SELECT DISTINCT generated.cell AS h3
          FROM _mapp_h3_scope
          CROSS JOIN LATERAL h3_polygon_to_cells_experimental(
            _mapp_h3_scope.geom_4326,
            {resolution},
            'overlapping'
          ) AS generated(cell)
        ),
        cells_4326 AS MATERIALIZED (
          SELECT
            candidate.h3,
            h3_cell_to_boundary_geometry(candidate.h3)
              ::public.geometry(Polygon, 4326) AS geom_4326
          FROM candidate_ids AS candidate
        ),
        cells AS MATERIALIZED (
          SELECT
            h3,
            public.ST_Transform(geom_4326, {area_srid})
              ::public.geometry(Polygon, 27700) AS geom_27700,
            public.ST_Transform(geom_4326, 3857)
              ::public.geometry(Polygon, 3857) AS geom_3857
          FROM cells_4326
        ),
        {source_preparation}
        matched_pairs AS MATERIALIZED (
          SELECT
            cell.h3,
            cell.geom_27700,
            cell.geom_3857,
            {matched_measure_projections},
            {metric_geometry} AS source_geom_27700
          FROM cells AS cell
          JOIN {matched_source} AS source
            ON {metric_geometry} && cell.geom_27700
           AND public.ST_Intersects(
                 {metric_geometry},
                 cell.geom_27700
               )
          WHERE {metric_geometry} IS NOT NULL
        ),
        pair_areas AS MATERIALIZED (
          SELECT
            h3,
            geom_3857,
            {pair_measure_columns},
            public.ST_Area(source_geom_27700) AS source_area_m2,
            public.ST_Area(
              public.ST_Intersection(source_geom_27700, geom_27700)
            ) AS intersection_area_m2
          FROM matched_pairs
        ),
        weighted_cells AS (
          SELECT
            h3,
            geom_3857,
            {weighted_aggregates}
          FROM pair_areas
          WHERE source_area_m2 > 0.0
            AND intersection_area_m2 > 0.0
          GROUP BY h3, geom_3857
        )
        SELECT
          h3::text AS h3_id,
          {resolution}::smallint AS h3_resolution,
          {final_measure_columns},
          geom_3857::public.geometry(Polygon, 3857) AS geom_3857
        FROM weighted_cells
    """).format(
        resolution=sql.Literal(resolution),
        area_srid=sql.Literal(AREA_WEIGHTED_H3_AREA_SRID),
        source_preparation=source_preparation,
        matched_measure_projections=sql.SQL(",\n            ").join(
            matched_measure_projections
        ),
        metric_geometry=metric_geometry,
        matched_source=matched_source,
        pair_measure_columns=sql.SQL(",\n            ").join(
            pair_measure_columns
        ),
        weighted_aggregates=sql.SQL(",\n            ").join(
            weighted_aggregates
        ),
        final_measure_columns=sql.SQL(",\n          ").join(
            final_measure_columns
        ),
    )
    return query.as_string(None).strip()


def area_weighted_h3_recipe_capability(*, available: bool) -> dict[str, Any]:
    return {
        "name": AREA_WEIGHTED_H3_RECIPE_NAME,
        "version": AREA_WEIGHTED_H3_RECIPE_VERSION,
        "available": available,
        "planPath": "/api/derived-layers/recipes/area-weighted-h3/plan",
        "maxMeasures": AREA_WEIGHTED_H3_MAX_MEASURES,
        "resolution": {"minimum": 0, "maximum": 15},
        "spatialScopeType": "workspace-map-extent",
        "areaCrs": f"EPSG:{AREA_WEIGHTED_H3_AREA_SRID}",
        "candidateContainment": "overlapping",
        "mutationAppliedByPlan": False,
    }


def plan_area_weighted_h3_recipe(
    payload: dict[str, Any],
    source_asset: dict[str, Any],
) -> dict[str, Any]:
    """Plan a bounded, area-weighted H3 derived layer without database I/O."""
    request = _closed_recipe_object(
        payload,
        label="Area-weighted H3 recipe",
        required={
            "name", "kind", "source", "resolution", "measures",
            "spatialScope",
        },
        optional={"description"},
    )
    name = request["name"]
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise DerivedLayerError(
            "Recipe name must start with a lowercase letter and contain only "
            "lowercase letters, numbers, and underscores."
        )
    kind = request["kind"]
    if not isinstance(kind, str) or kind not in {"view", "materialized"}:
        raise DerivedLayerError("Recipe kind must be view or materialized.")

    source = _closed_recipe_object(
        request["source"],
        label="Recipe source",
        required={"assetId", "relation", "idColumn", "geometryColumn"},
    )
    asset_id = source["assetId"]
    if (
        not isinstance(asset_id, str)
        or not asset_id.strip()
        or asset_id != asset_id.strip()
        or len(asset_id) > 200
    ):
        raise DerivedLayerError(
            "Recipe source assetId must be non-empty text of at most 200 "
            "characters."
        )
    relation = _relation(source["relation"])
    if relation[0] == SCHEMA:
        raise DerivedLayerError(
            "An area-weighted recipe cannot depend on another managed "
            "derived layer."
        )

    resolution = request["resolution"]
    if (
        isinstance(resolution, bool)
        or not isinstance(resolution, int)
        or not 0 <= resolution <= 15
    ):
        raise DerivedLayerError(
            "Recipe resolution must be an integer from 0 through 15."
        )

    requested_measures = request["measures"]
    if (
        not isinstance(requested_measures, list)
        or isinstance(requested_measures, (str, bytes))
        or not 1 <= len(requested_measures) <= AREA_WEIGHTED_H3_MAX_MEASURES
    ):
        raise DerivedLayerError(
            "Recipe measures must contain between 1 and 32 entries."
        )

    scope = _closed_recipe_object(
        request["spatialScope"],
        label="Recipe spatialScope",
        required={"type"},
        optional={"locale"},
    )
    if scope["type"] != "workspace-map-extent":
        raise DerivedLayerError(
            "Recipe spatialScope.type must be workspace-map-extent."
        )
    locale = scope.get("locale")
    if locale is not None and (
        not isinstance(locale, str)
        or not locale
        or locale != locale.strip()
        or len(locale) > 200
    ):
        raise DerivedLayerError(
            "Recipe spatialScope.locale must be non-empty text of at most "
            "200 characters."
        )
    canonical_scope = {"type": "workspace-map-extent"}
    if locale is not None:
        canonical_scope["locale"] = locale

    description = request.get("description")
    if description is not None and (
        not isinstance(description, str) or len(description) > 2000
    ):
        raise DerivedLayerError(
            "Recipe description must be text of at most 2000 characters."
        )
    description = description.strip() if description is not None else None

    if not isinstance(source_asset, dict):
        raise DerivedLayerError("Recipe source asset must be an object.")
    if source_asset.get("id") != asset_id:
        raise DerivedLayerError(
            "Recipe source assetId does not match the semantic asset."
        )
    if source_asset.get("status") != "ready":
        raise DerivedLayerError(
            "Recipe source asset must have a ready semantic profile."
        )
    asset_version = source_asset.get("version")
    if asset_version is not None and (
        isinstance(asset_version, bool)
        or not isinstance(asset_version, int)
        or asset_version < 1
    ):
        raise DerivedLayerError("Recipe source asset version is invalid.")
    generated = source_asset.get("generated")
    if not isinstance(generated, dict):
        raise DerivedLayerError(
            "Recipe source asset has no generated semantic profile."
        )
    binding = generated.get("binding")
    if not isinstance(binding, dict) or binding.get("adapter") != "postgresql":
        raise DerivedLayerError(
            "Recipe source asset must use a PostgreSQL binding."
        )
    binding_schema = binding.get("schema")
    binding_relation = binding.get("relation")
    if (
        not isinstance(binding_schema, str)
        or not isinstance(binding_relation, str)
        or (binding_schema, binding_relation) != relation
    ):
        raise DerivedLayerError(
            "Recipe source relation does not match the semantic asset binding."
        )
    binding_alias = binding.get("alias")
    if binding_alias is not None and (
        not isinstance(binding_alias, str)
        or not binding_alias.strip()
        or len(binding_alias) > 200
    ):
        raise DerivedLayerError(
            "Recipe source asset has invalid binding alias metadata."
        )
    qualified_name = generated.get("qualifiedName")
    if qualified_name is not None and qualified_name != source["relation"]:
        raise DerivedLayerError(
            "Recipe source relation does not match the semantic profile name."
        )

    fields = generated.get("fields")
    if not isinstance(fields, list) or not fields:
        raise DerivedLayerError(
            "Recipe source asset must have generated field metadata."
        )
    fields_by_name: dict[str, dict[str, Any]] = {}
    for field in fields:
        if not isinstance(field, dict):
            raise DerivedLayerError(
                "Recipe source asset contains invalid field metadata."
            )
        field_name = _recipe_identifier(
            field.get("name"),
            "Semantic profile field name",
        )
        if field_name in fields_by_name:
            raise DerivedLayerError(
                f"Semantic profile contains duplicate field {field_name!r}."
            )
        fields_by_name[field_name] = field

    id_field = _recipe_profile_field(
        fields_by_name,
        source["idColumn"],
        "Recipe ID column",
    )
    if id_field.get("nullable") is not False or not (
        id_field.get("primaryKey") is True or id_field.get("unique") is True
    ):
        raise DerivedLayerError(
            "Recipe ID column must be non-null and primary-key or unique."
        )

    geometry_field = _recipe_profile_field(
        fields_by_name,
        source["geometryColumn"],
        "Recipe geometry column",
    )
    if not re.match(
        r"^geometry(?:\s*\(|$)",
        geometry_field["type"].strip(),
        re.IGNORECASE,
    ):
        raise DerivedLayerError(
            "Recipe geometry column must have PostgreSQL geometry metadata."
        )
    geometry_type = geometry_field.get("geometryType")
    if (
        not isinstance(geometry_type, str)
        or geometry_type.upper() not in {"POLYGON", "MULTIPOLYGON"}
    ):
        raise DerivedLayerError(
            "Recipe geometry column must be Polygon or MultiPolygon."
        )
    geometry_srid = geometry_field.get("srid")
    if (
        isinstance(geometry_srid, bool)
        or not isinstance(geometry_srid, int)
        or geometry_srid <= 0
    ):
        raise DerivedLayerError(
            "Recipe geometry column must have a positive integer SRID."
        )

    measures: list[dict[str, str]] = []
    resolved_measures = []
    seen_source_columns = set()
    seen_output_columns = set()
    for index, value in enumerate(requested_measures, start=1):
        measure = _closed_recipe_object(
            value,
            label=f"Recipe measure {index}",
            required={"sourceColumn", "outputColumn", "nullHandling"},
        )
        source_column = _recipe_identifier(
            measure["sourceColumn"],
            f"Recipe measure {index} sourceColumn",
        )
        if source_column in seen_source_columns:
            raise DerivedLayerError(
                f"Recipe measure sourceColumn {source_column!r} is duplicated."
            )
        seen_source_columns.add(source_column)
        output_column = measure["outputColumn"]
        if not isinstance(output_column, str) or not NAME_RE.fullmatch(
            output_column
        ):
            raise DerivedLayerError(
                f"Recipe measure {index} outputColumn must be an ASCII-safe "
                "lowercase field name."
            )
        if output_column in AREA_WEIGHTED_H3_OUTPUT_COLUMNS:
            raise DerivedLayerError(
                f"Recipe measure outputColumn {output_column!r} is reserved."
            )
        if output_column in seen_output_columns:
            raise DerivedLayerError(
                f"Recipe measure outputColumn {output_column!r} is duplicated."
            )
        seen_output_columns.add(output_column)
        null_handling = measure["nullHandling"]
        if (
            not isinstance(null_handling, str)
            or null_handling not in {"zero", "ignore"}
        ):
            raise DerivedLayerError(
                f"Recipe measure {index} nullHandling must be zero or ignore."
            )
        source_field = _recipe_profile_field(
            fields_by_name,
            source_column,
            f"Recipe measure {index} sourceColumn",
        )
        if not NUMERIC_FIELD_TYPE_RE.fullmatch(source_field["type"].strip()):
            raise DerivedLayerError(
                f"Recipe measure sourceColumn {source_column!r} must have a "
                "built-in numeric type."
            )
        normalized = {
            "sourceColumn": source_column,
            "outputColumn": output_column,
            "nullHandling": null_handling,
        }
        measures.append(normalized)
        resolved_measures.append({
            **normalized,
            "sourceField": _resolved_recipe_field(source_field),
            "outputType": "double precision",
        })

    query = _area_weighted_h3_query(
        relation=relation,
        geometry_column=geometry_field["name"],
        geometry_srid=geometry_srid,
        resolution=resolution,
        measures=measures,
    )
    create_request = {
        "name": name,
        "kind": kind,
        "query": query,
        "sources": [source["relation"]],
        "idColumn": "h3_id",
        "geometryColumn": "geom_3857",
        "spatialScope": canonical_scope,
    }
    if description:
        create_request["description"] = description

    resolved_binding = {
        "adapter": "postgresql",
        **(
            {"alias": binding_alias}
            if binding_alias is not None
            else {}
        ),
        "schema": binding_schema,
        "relation": binding_relation,
    }
    return {
        "recipe": {
            "name": AREA_WEIGHTED_H3_RECIPE_NAME,
            "version": AREA_WEIGHTED_H3_RECIPE_VERSION,
            "areaCrs": f"EPSG:{AREA_WEIGHTED_H3_AREA_SRID}",
            "candidateContainment": "overlapping",
        },
        "createRequest": create_request,
        "source": {
            "assetId": asset_id,
            **(
                {"assetVersion": asset_version}
                if asset_version is not None
                else {}
            ),
            "relation": source["relation"],
            "binding": resolved_binding,
            "idColumn": _resolved_recipe_field(id_field),
            "geometryColumn": _resolved_recipe_field(geometry_field),
            "metricGeometry": {
                "srid": AREA_WEIGHTED_H3_AREA_SRID,
                "mode": (
                    "native"
                    if geometry_srid == AREA_WEIGHTED_H3_AREA_SRID
                    else "transform"
                ),
            },
        },
        "resolution": resolution,
        "measures": resolved_measures,
        "output": {
            "idColumn": "h3_id",
            "resolutionColumn": "h3_resolution",
            "geometryColumn": "geom_3857",
            "geometryType": "Polygon",
            "srid": 3857,
        },
        "assumptions": [
            "Each numeric measure is additive and uniformly distributed "
            "within its source polygon.",
            "Only H3 cells intersecting the server-resolved workspace map "
            "extent are candidates.",
            "Source polygons must intersect that scope, but allocation uses "
            "each accepted polygon and boundary cell in full.",
            "Overlapping source polygons contribute independently and may "
            "therefore double-count their overlap.",
            "Null source values follow each measure's explicit nullHandling "
            "rule.",
        ],
    }


def validate_spatial_scope(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DerivedLayerError(
            "Spatial scope must be resolved by the server before it is stored."
        )
    required = {
        "type", "locale", "sourceView", "scopeZoom", "zoomOffset",
        "viewport", "crs", "envelopes", "selection", "clipsGeometry",
        "guidance",
    }
    if set(value) != required:
        raise DerivedLayerError(
            "Spatial scope must be a complete server-resolved workspace map extent."
        )
    if value.get("type") != "workspace-map-extent":
        raise DerivedLayerError("Unsupported derived-layer spatial scope.")
    if not isinstance(value.get("locale"), str) or not value["locale"]:
        raise DerivedLayerError("Spatial scope locale must be a non-empty string.")

    source_view = value.get("sourceView")
    if not isinstance(source_view, dict) or set(source_view) != {"lng", "lat", "z"}:
        raise DerivedLayerError("Spatial scope sourceView is invalid.")
    for key in ("lng", "lat", "z"):
        number = source_view.get(key)
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
        ):
            raise DerivedLayerError(
                f"Spatial scope sourceView.{key} must be a finite number."
            )
    if (
        source_view["lng"] < -180
        or source_view["lng"] > 180
        or source_view["lat"] < -90
        or source_view["lat"] > 90
        or source_view["z"] < 0
        or source_view["z"] > 30
    ):
        raise DerivedLayerError("Spatial scope sourceView bounds are invalid.")

    scope_zoom = value.get("scopeZoom")
    if (
        isinstance(scope_zoom, bool)
        or not isinstance(scope_zoom, (int, float))
        or not math.isfinite(scope_zoom)
        or scope_zoom < 0
        or scope_zoom > 30
        or not math.isclose(
            float(scope_zoom),
            max(0.0, float(source_view["z"]) - 1.0),
            abs_tol=1e-12,
        )
    ):
        raise DerivedLayerError(
            "Spatial scope zoom must be one level wider than the source view."
        )
    zoom_offset = value.get("zoomOffset")
    if (
        isinstance(zoom_offset, bool)
        or not isinstance(zoom_offset, (int, float))
        or not math.isfinite(zoom_offset)
        or not math.isclose(
            float(zoom_offset),
            float(scope_zoom) - float(source_view["z"]),
            abs_tol=1e-12,
        )
    ):
        raise DerivedLayerError(
            "Spatial scope zoomOffset must match the resolved scope zoom."
        )
    if value.get("viewport") != {
        "width": 1920,
        "height": 1080,
        "tileSize": 256,
    }:
        raise DerivedLayerError(
            "Spatial scope viewport must be the server-defined 1920x1080 viewport."
        )
    if value.get("crs") != "EPSG:4326":
        raise DerivedLayerError("Spatial scope envelopes must use EPSG:4326.")
    if value.get("selection") != "intersects-output-geometry":
        raise DerivedLayerError(
            "Spatial scope must select intersecting output geometry."
        )
    if value.get("clipsGeometry") is not False:
        raise DerivedLayerError("Spatial scope must not clip output geometry.")
    guidance = value.get("guidance")
    if (
        not isinstance(guidance, str)
        or not guidance.strip()
        or len(guidance) > 2000
    ):
        raise DerivedLayerError("Spatial scope guidance is invalid.")

    envelopes = value.get("envelopes")
    if not isinstance(envelopes, list) or not 1 <= len(envelopes) <= 2:
        raise DerivedLayerError("Spatial scope needs one or two EPSG:4326 envelopes.")
    for envelope in envelopes:
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"west", "south", "east", "north"}
        ):
            raise DerivedLayerError("Spatial scope envelope is invalid.")
        for key in ("west", "south", "east", "north"):
            number = envelope.get(key)
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
            ):
                raise DerivedLayerError(
                    f"Spatial scope envelope {key} must be a finite number."
                )
        if (
            envelope["west"] < -180
            or envelope["east"] > 180
            or envelope["south"] < -90
            or envelope["north"] > 90
            or envelope["west"] >= envelope["east"]
            or envelope["south"] >= envelope["north"]
        ):
            raise DerivedLayerError("Spatial scope envelope bounds are invalid.")
    return json.loads(json.dumps(value, allow_nan=False))


def _reason(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _scope_area_km2(spatial_scope: dict[str, Any] | None) -> float:
    if spatial_scope is None:
        return 0.0
    radius_km = 6371.0088
    area = 0.0
    for envelope in spatial_scope["envelopes"]:
        longitude_width = math.radians(envelope["east"] - envelope["west"])
        latitude_span = abs(
            math.sin(math.radians(envelope["north"]))
            - math.sin(math.radians(envelope["south"]))
        )
        area += radius_km ** 2 * longitude_width * latitude_span
    return area


def _h3_expansion_guard(
    definition: dict[str, Any],
    inspection: QueryAstInspection,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    spatial_scope = definition.get("spatialScope")
    scope_area = _scope_area_km2(spatial_scope)
    reasons: list[dict[str, str]] = []
    resolutions = []
    estimated_scope_cells = 0

    polygon_calls = inspection.calls_named(H3_POLYGON_FUNCTIONS)
    for call in polygon_calls:
        resolution = call.literal_integers[1]
        assert resolution is not None
        resolutions.append(resolution)
        if spatial_scope is not None:
            estimated_scope_cells += math.ceil(
                scope_area
                / H3_AVERAGE_HEX_AREA_KM2[resolution]
                * H3_SCOPE_ESTIMATE_SAFETY_MULTIPLIER
            )

    if polygon_calls and spatial_scope is None:
        reasons.append(_reason(
            "h3_missing_scope",
            "H3 polygon expansion needs a resolved workspace map extent.",
        ))
    if estimated_scope_cells > H3_SCOPE_MAX_ESTIMATED_CELLS:
        reasons.append(_reason(
            "h3_scope_expansion",
            f"Scoped H3 expansion is estimated to generate "
            f"{estimated_scope_cells:,} cells, above the "
            f"{H3_SCOPE_MAX_ESTIMATED_CELLS:,} cell limit; use a coarser "
            "literal resolution.",
        ))

    grid_calls = inspection.calls_named(H3_GRID_FUNCTIONS)
    grid_distances = []
    expansion_multiplier = 1
    for call in grid_calls:
        distance = (
            1 if len(call.arguments) == 1 else call.literal_integers[1]
        )
        assert distance is not None
        grid_distances.append(distance)
        multiplier = (
            max(1, 6 * distance)
            if call.name in H3_RING_FUNCTIONS
            else 3 * distance * (distance + 1) + 1
        )
        expansion_multiplier = min(
            expansion_multiplier * multiplier,
            H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS + 1,
        )

    child_calls = inspection.calls_named({"h3_cell_to_children"})
    for _ in child_calls:
        expansion_multiplier = min(
            expansion_multiplier * 7,
            H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS + 1,
        )

    estimated_expanded_cells = (
        min(
            estimated_scope_cells * expansion_multiplier,
            H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS + 1,
        )
        if estimated_scope_cells else 0
    )
    if estimated_expanded_cells > H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS:
        reasons.append(_reason(
            "h3_composed_expansion",
            "Combined scoped H3 polygon, grid, and child expansion is "
            f"estimated above the "
            f"{H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS:,} cell limit; use a "
            "coarser resolution or a smaller literal grid distance.",
        ))

    info = {
        "polygonToCellsCalls": len(polygon_calls),
        "resolutions": resolutions,
        "scopeAreaKm2": round(scope_area, 3),
        "estimatedScopeCells": estimated_scope_cells,
        "maxEstimatedScopeCells": H3_SCOPE_MAX_ESTIMATED_CELLS,
        "expansionMultiplier": expansion_multiplier,
        "estimatedExpandedCells": estimated_expanded_cells,
        "maxEstimatedExpandedCells": (
            H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS
        ),
        "safetyMultiplier": H3_SCOPE_ESTIMATE_SAFETY_MULTIPLIER,
        "gridDiskCalls": len(grid_calls),
        "maxGridDistance": max(grid_distances, default=0),
        "maxAllowedGridDistance": H3_MAX_GRID_DISTANCE,
    }
    return info, reasons


def _query_shape_guard(definition: dict[str, Any]) -> dict[str, Any]:
    try:
        inspection = validate_query_ast(definition["query"])
    except QueryGuardViolation as exc:
        raise DerivedLayerQueryTooExpensive(
            definition["name"],
            {
                "method": "postgresql-ast-guard",
                "h3Expansion": {},
                "limits": {
                    "maxJoins": QUERY_SHAPE_MAX_JOINS,
                    "maxCtes": QUERY_SHAPE_MAX_CTES,
                    "maxSetOperations": QUERY_SHAPE_MAX_SET_OPERATIONS,
                    "maxGroupingSets": QUERY_SHAPE_MAX_GROUPING_SETS,
                    "maxGeneratedRows": QUERY_SHAPE_MAX_GENERATED_ROWS,
                },
            },
            [reason.as_dict() for reason in exc.reasons],
        ) from exc
    reasons: list[dict[str, str]] = []
    if inspection.join_count > QUERY_SHAPE_MAX_JOINS:
        reasons.append(_reason(
            "too_many_joins",
            f"The query has {inspection.join_count} joins, above the "
            f"{QUERY_SHAPE_MAX_JOINS} join limit.",
        ))
    if inspection.cte_count > QUERY_SHAPE_MAX_CTES:
        reasons.append(_reason(
            "too_many_ctes",
            f"The query has {inspection.cte_count} CTEs, above the "
            f"{QUERY_SHAPE_MAX_CTES} CTE limit.",
        ))
    if inspection.set_operation_count > QUERY_SHAPE_MAX_SET_OPERATIONS:
        reasons.append(_reason(
            "too_many_set_operations",
            f"The query has {inspection.set_operation_count} set operations, above the "
            f"{QUERY_SHAPE_MAX_SET_OPERATIONS} limit.",
        ))
    if inspection.grouping_set_count > QUERY_SHAPE_MAX_GROUPING_SETS:
        reasons.append(_reason(
            "too_many_grouping_sets",
            f"The query has {inspection.grouping_set_count} grouping sets, above the "
            f"{QUERY_SHAPE_MAX_GROUPING_SETS} limit.",
        ))
    h3_expansion, h3_reasons = _h3_expansion_guard(definition, inspection)
    reasons.extend(h3_reasons)
    if reasons:
        raise DerivedLayerQueryTooExpensive(
            definition["name"],
            {
                "method": "postgresql-ast-guard",
                "h3Expansion": h3_expansion,
                "limits": {
                    "maxJoins": QUERY_SHAPE_MAX_JOINS,
                    "maxCtes": QUERY_SHAPE_MAX_CTES,
                    "maxSetOperations": QUERY_SHAPE_MAX_SET_OPERATIONS,
                    "maxGroupingSets": QUERY_SHAPE_MAX_GROUPING_SETS,
                    "maxGeneratedRows": QUERY_SHAPE_MAX_GENERATED_ROWS,
                },
            },
            reasons,
        )
    return h3_expansion


def validate_definition(payload: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(
        set(payload)
        - {
            "name", "kind", "query", "sources", "idColumn",
            "geometryColumn", "description", "spatialScope",
        }
    )
    if unknown:
        raise DerivedLayerError(
            "Unknown derived-layer properties: " + ", ".join(unknown)
        )
    name = payload.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise DerivedLayerError(
            "Name must start with a lowercase letter and contain only "
            "lowercase letters, numbers, and underscores."
        )
    kind = payload.get("kind", "view")
    if kind not in {"view", "materialized"}:
        raise DerivedLayerError("Kind must be view or materialized.")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise DerivedLayerError("A SELECT query is required.")
    query = query.strip()
    if len(query.encode()) > 256 * 1024:
        raise DerivedLayerError("Derived-layer SQL is limited to 256 KiB.")
    if ";" in query or "--" in query or "/*" in query or "*/" in query:
        raise DerivedLayerError(
            "SQL terminators and comments are not allowed."
        )
    if not re.match(r"^(?:select|with)\b", query, re.IGNORECASE):
        raise DerivedLayerError("Derived-layer SQL must be one SELECT query.")
    forbidden = FORBIDDEN_SQL.search(query)
    if forbidden:
        raise DerivedLayerError(
            f"SQL keyword {forbidden.group(0).upper()} is not allowed."
        )
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise DerivedLayerError("Declare at least one source relation.")
    normalized_sources = sorted(
        {".".join(_relation(source)) for source in sources}
    )
    if any(source.startswith(f"{SCHEMA}.") for source in normalized_sources):
        raise DerivedLayerError(
            "A managed derived layer cannot depend on another derived layer."
        )
    id_column = payload.get("idColumn")
    geometry_column = payload.get("geometryColumn")
    for label, value in (
        ("ID column", id_column),
        ("Geometry column", geometry_column),
    ):
        if not isinstance(value, str) or not NAME_RE.fullmatch(value):
            raise DerivedLayerError(
                f"{label} must be a lowercase field name containing only "
                "letters, numbers, and underscores."
            )
    definition = {
        "name": name,
        "kind": kind,
        "query": query,
        "sources": normalized_sources,
        "idColumn": id_column,
        "geometryColumn": geometry_column,
        "description": str(payload.get("description", "")).strip()[:2000],
        "spatialScope": validate_spatial_scope(payload.get("spatialScope")),
    }
    _query_shape_guard(definition)
    return definition


class DerivedLayerStore:
    def __init__(self, connection_string: str, reader_role: str):
        if not connection_string:
            raise DerivedLayerError(
                "Derived-layer database management is not configured."
            )
        if not NAME_RE.fullmatch(reader_role):
            raise DerivedLayerError(
                "DERIVED_READER_ROLE must be a PostgreSQL identifier."
            )
        self.connection_string = connection_string
        self.reader_role = reader_role
        self._initialization_lock = threading.Lock()
        self._initialized = False

    def _connect(self):
        connection = psycopg.connect(
            self.connection_string,
            autocommit=False,
            row_factory=dict_row,
        )
        try:
            with connection.cursor() as cur:
                cur.execute("SET SESSION search_path = pg_catalog, public")
            if self._initialized:
                return connection
            with self._initialization_lock:
                if not self._initialized:
                    with connection.cursor() as cur:
                        cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (f"{SCHEMA}:schema",),
                        )
                        self._initialize(cur)
                    connection.commit()
                    self._initialized = True
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def _mutation_connection(
        self,
        cancellation: DerivedLayerCancellation | None = None,
    ):
        """Expose whether a mutation failed before, during, or after commit."""
        try:
            connection = self._connect()
        except psycopg.Error as exc:
            raise DerivedLayerDatabaseOperationError(
                exc,
                failure_phase="preflight",
                state_unchanged=True,
            ) from exc

        try:
            try:
                if cancellation is not None:
                    cancellation.bind(connection)
                    cancellation.checkpoint()
                yield connection
                if cancellation is not None:
                    cancellation.checkpoint()
            except Exception as body_error:
                try:
                    connection.rollback()
                except psycopg.Error as rollback_error:
                    raise DerivedLayerDatabaseOperationError(
                        rollback_error,
                        failure_phase="transaction-rollback",
                        state_unchanged=False,
                        indeterminate=True,
                    ) from rollback_error
                setattr(body_error, "failure_phase", "database-transaction")
                setattr(body_error, "rolled_back", True)
                if isinstance(body_error, psycopg.Error):
                    raise DerivedLayerDatabaseOperationError(
                        body_error,
                        failure_phase="database-transaction",
                        state_unchanged=True,
                        rolled_back=True,
                    ) from body_error
                raise
            try:
                connection.commit()
            except psycopg.Error as commit_error:
                raise DerivedLayerDatabaseOperationError(
                    commit_error,
                    failure_phase="transaction-commit",
                    state_unchanged=False,
                    indeterminate=True,
                ) from commit_error
        finally:
            if cancellation is not None:
                cancellation.finish_database()
            connection.close()

    @staticmethod
    def _initialize(cur) -> None:
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._definitions (
              name text PRIMARY KEY,
              kind text NOT NULL CHECK (kind IN ('view', 'materialized')),
              query text NOT NULL,
              sources text[] NOT NULL,
              id_column text NOT NULL,
              geometry_column text NOT NULL,
              description text NOT NULL DEFAULT '',
              spatial_scope jsonb,
              created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              created_by text NOT NULL,
              refreshed_at timestamptz,
              semantic_asset_id uuid,
              semantic_generation bigint NOT NULL DEFAULT 0,
              semantic_status text NOT NULL DEFAULT 'repair_required',
              semantic_revision text
            )
        """).format(sql.Identifier(SCHEMA)))
        for definition in (
            "spatial_scope jsonb",
            "semantic_asset_id uuid",
            "semantic_generation bigint NOT NULL DEFAULT 0",
            "semantic_status text NOT NULL DEFAULT 'repair_required'",
            "semantic_revision text",
        ):
            cur.execute(sql.SQL(
                "ALTER TABLE {}._definitions ADD COLUMN IF NOT EXISTS "
                + definition
            ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._semantic_outbox (
              event_id uuid PRIMARY KEY,
              asset_id uuid NOT NULL,
              event_type text NOT NULL
                CHECK (event_type IN ('register', 'replace', 'refresh', 'archive')),
              generation bigint NOT NULL CHECK (generation > 0),
              payload jsonb NOT NULL,
              status text NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                  'pending', 'retrying', 'delivered', 'repair_required'
                )),
              attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
              available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              last_error text,
              semantic_revision text,
              claim_id uuid,
              claimed_until timestamptz,
              created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              delivered_at timestamptz,
              UNIQUE (asset_id, generation)
            )
        """).format(sql.Identifier(SCHEMA)))
        for claim_column in (
            "claim_id uuid",
            "claimed_until timestamptz",
        ):
            cur.execute(sql.SQL(
                "ALTER TABLE {}._semantic_outbox ADD COLUMN IF NOT EXISTS "
                + claim_column
            ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("""
            CREATE TABLE IF NOT EXISTS {}._maintenance (
              operation text PRIMARY KEY
                CHECK (operation IN ('reset-data')),
              actor text NOT NULL,
              reset_owner uuid NOT NULL,
              started_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("""
            ALTER TABLE {}._maintenance
            ADD COLUMN IF NOT EXISTS reset_owner uuid
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("""
            UPDATE {}._maintenance
            SET reset_owner = %s
            WHERE reset_owner IS NULL
        """).format(sql.Identifier(SCHEMA)), (str(uuid.uuid4()),))
        cur.execute(sql.SQL("""
            ALTER TABLE {}._maintenance
            ALTER COLUMN reset_owner SET NOT NULL
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("""
            SELECT name, kind, query, sources,
                   id_column AS "idColumn",
                   geometry_column AS "geometryColumn", description,
                   spatial_scope AS "spatialScope",
                   created_at AS "createdAt", created_by AS "createdBy",
                   refreshed_at AS "refreshedAt"
            FROM {}._definitions
            WHERE semantic_asset_id IS NULL
            ORDER BY name
        """).format(sql.Identifier(SCHEMA)))
        for item in list(cur.fetchall()):
            asset_id = str(uuid.uuid4())
            cur.execute(sql.SQL("""
                UPDATE {}._definitions
                SET semantic_asset_id = %s,
                    semantic_generation = 1,
                    semantic_status = 'registering',
                    semantic_revision = NULL
                WHERE name = %s
                  AND semantic_asset_id IS NULL
                RETURNING semantic_asset_id
            """).format(sql.Identifier(SCHEMA)), (
                asset_id,
                item["name"],
            ))
            if cur.fetchone() is None:
                continue
            item["semanticProfile"] = {
                "assetId": asset_id,
                "generation": 1,
                "status": "registering",
                "revision": None,
            }
            DerivedLayerStore._enqueue_semantic_event(
                cur,
                item,
                "register",
                item["createdBy"],
                DerivedLayerStore._semantic_fields(cur, item["name"]),
            )
        cur.execute(sql.SQL("""
            ALTER TABLE {}._definitions
            ALTER COLUMN semantic_asset_id SET NOT NULL
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("""
            CREATE UNIQUE INDEX IF NOT EXISTS derived_semantic_asset_uidx
            ON {}._definitions (semantic_asset_id)
        """).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL("REVOKE ALL ON {}._definitions FROM PUBLIC").format(
            sql.Identifier(SCHEMA)
        ))
        cur.execute(sql.SQL(
            "REVOKE ALL ON {}._semantic_outbox FROM PUBLIC"
        ).format(sql.Identifier(SCHEMA)))
        cur.execute(sql.SQL(
            "REVOKE ALL ON {}._maintenance FROM PUBLIC"
        ).format(sql.Identifier(SCHEMA)))

    @staticmethod
    def _ensure_changes_allowed(cur) -> None:
        cur.execute(sql.SQL("""
            SELECT operation
            FROM {}._maintenance
            WHERE operation = 'reset-data'
        """).format(sql.Identifier(SCHEMA)))
        if cur.fetchone() is not None:
            raise DerivedLayerMaintenanceError(
                "Derived-layer changes are paused while reset-data archives "
                "semantic profiles."
            )

    @staticmethod
    def _acquire_mutation_lock(connection) -> None:
        with connection.cursor(row_factory=dict_row) as lock_cur:
            lock_cur.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS acquired",
                (SCHEMA,),
            )
            result = lock_cur.fetchone()
        if not isinstance(result, dict) or not isinstance(
            result.get("acquired"), bool
        ):
            raise psycopg.InterfaceError(
                "PostgreSQL returned an invalid derived-mutation lock result."
            )
        if not result["acquired"]:
            raise DerivedLayerContentionError("derived-mutation")

    @staticmethod
    def _dependencies(cur, name: str) -> list[str]:
        cur.execute(
            """
            SELECT DISTINCT source_ns.nspname || '.' || source.relname AS relation
            FROM pg_rewrite AS rewrite
            JOIN pg_depend AS dependency
              ON dependency.classid = 'pg_rewrite'::regclass
             AND dependency.objid = rewrite.oid
            JOIN pg_class AS source ON source.oid = dependency.refobjid
            JOIN pg_namespace AS source_ns ON source_ns.oid = source.relnamespace
            WHERE rewrite.ev_class = %s::regclass
              AND dependency.refobjid <> rewrite.ev_class
              AND source.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND source_ns.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY relation
            """,
            (f"{SCHEMA}.{name}",),
        )
        return [row["relation"] for row in cur.fetchall()]

    @staticmethod
    def _incoming_dependents(cur, name: str) -> list[str]:
        cur.execute(
            """
            SELECT DISTINCT pg_describe_object(
              dependency.classid,
              dependency.objid,
              dependency.objsubid
            ) AS dependent
            FROM pg_depend AS dependency
            LEFT JOIN pg_rewrite AS rewrite
              ON dependency.classid = 'pg_rewrite'::regclass
             AND dependency.objid = rewrite.oid
            WHERE dependency.refobjid = %s::regclass
              AND dependency.refclassid = 'pg_class'::regclass
              AND dependency.deptype = 'n'
              AND COALESCE(rewrite.ev_class, 0) <> dependency.refobjid
            ORDER BY dependent
            """,
            (f"{SCHEMA}.{name}",),
        )
        return [row["dependent"] for row in cur.fetchall()]

    @staticmethod
    def _column_names(cur, name: str) -> list[str]:
        cur.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (f"{SCHEMA}.{name}",),
        )
        return [row["attname"] for row in cur.fetchall()]

    @staticmethod
    def _column_types(cur, name: str) -> dict[str, str]:
        cur.execute(
            """
            SELECT attname, format_type(atttypid, atttypmod) AS data_type
            FROM pg_attribute
            WHERE attrelid = %s::regclass
              AND attnum > 0
              AND NOT attisdropped
            ORDER BY attnum
            """,
            (f"{SCHEMA}.{name}",),
        )
        return {row["attname"]: row["data_type"] for row in cur.fetchall()}

    @staticmethod
    def _semantic_fields(cur, name: str) -> list[dict[str, Any]]:
        cur.execute(
            """
            SELECT
              attname AS name,
              format_type(atttypid, atttypmod) AS type,
              NOT attnotnull AS nullable,
              CASE WHEN atttypid = 'geometry'::regtype
                THEN postgis_typmod_type(atttypmod)
                ELSE ''
              END AS "geometryType",
              CASE WHEN atttypid = 'geometry'::regtype
                THEN postgis_typmod_srid(atttypmod)
                ELSE NULL
              END AS srid
            FROM pg_attribute
            WHERE attrelid = %s::regclass
              AND attnum > 0
              AND NOT attisdropped
            ORDER BY attnum
            """,
            (f"{SCHEMA}.{name}",),
        )
        return list(cur.fetchall())

    @staticmethod
    def _with_semantic_profile(item: dict[str, Any]) -> dict[str, Any]:
        profile = {
            "assetId": str(item.pop("semanticAssetId")),
            "generation": int(item.pop("semanticGeneration")),
            "status": item.pop("semanticStatus"),
            "revision": item.pop("semanticRevision"),
        }
        item["semanticProfile"] = profile
        return item

    @staticmethod
    def _json_timestamp(value: date | datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()).hexdigest()

    @classmethod
    def _semantic_event(
        cls,
        definition: dict[str, Any],
        event_type: str,
        actor: str,
        fields: list[dict[str, Any]],
        *,
        event_id: uuid.UUID | None = None,
        event_at: datetime | None = None,
    ) -> dict[str, Any]:
        profile = definition["semanticProfile"]
        geometry = next(
            (
                field for field in fields
                if field["name"] == definition["geometryColumn"]
            ),
            {},
        )
        definition_digest = cls._payload_hash({
            "name": definition["name"],
            "kind": definition["kind"],
            "query": definition["query"],
            "sources": definition["sources"],
            "idColumn": definition["idColumn"],
            "geometryColumn": definition["geometryColumn"],
            "description": definition.get("description", ""),
            "spatialScope": definition.get("spatialScope"),
        })
        generated = {
            "name": definition["name"],
            "binding": {
                "adapter": "postgresql",
                "schema": SCHEMA,
                "relation": definition["name"],
            },
            "kind": definition["kind"],
            "description": definition.get("description", ""),
            "definitionDigest": definition_digest,
            "sources": list(definition["sources"]),
            "fields": [dict(field) for field in fields],
            "idColumn": definition["idColumn"],
            "geometryColumn": definition["geometryColumn"],
            "geometryType": geometry.get("geometryType") or None,
            "srid": geometry.get("srid"),
            "spatialScope": definition.get("spatialScope"),
            "actor": actor,
            "createdAt": cls._json_timestamp(definition.get("createdAt")),
            "refreshedAt": cls._json_timestamp(definition.get("refreshedAt")),
            "eventAt": cls._json_timestamp(
                event_at or datetime.now(timezone.utc)
            ),
        }
        event = {
            "eventId": str(event_id or uuid.uuid4()),
            "assetId": profile["assetId"],
            "type": event_type,
            "generation": profile["generation"],
            "generated": generated,
            "actor": actor,
        }
        predecessor_asset_id = definition.get(
            "semanticPredecessorAssetId"
        )
        if predecessor_asset_id is not None:
            if event_type != "register":
                raise DerivedLayerError(
                    "Only semantic registration can name a predecessor asset."
                )
            event["predecessorAssetId"] = cls._semantic_uuid(
                predecessor_asset_id,
                "Semantic predecessor asset ID",
            )
        else:
            event["visibility"] = "inspect"
        event["payloadHash"] = cls._payload_hash(event)
        return event

    @classmethod
    def _enqueue_semantic_event(
        cls,
        cur,
        definition: dict[str, Any],
        event_type: str,
        actor: str,
        fields: list[dict[str, Any]],
    ) -> dict[str, Any]:
        event = cls._semantic_event(
            definition,
            event_type,
            actor,
            fields,
        )
        cur.execute(sql.SQL("""
            INSERT INTO {}._semantic_outbox
              (event_id, asset_id, event_type, generation, payload)
            VALUES (%s, %s, %s, %s, %s)
        """).format(sql.Identifier(SCHEMA)), (
            event["eventId"],
            event["assetId"],
            event["type"],
            event["generation"],
            Jsonb(event),
        ))
        return event

    @staticmethod
    def _dependent_columns(cur, name: str) -> list[str]:
        cur.execute(
            """
            SELECT DISTINCT attribute.attname
            FROM pg_depend AS dependency
            JOIN pg_attribute AS attribute
              ON attribute.attrelid = dependency.refobjid
             AND attribute.attnum = dependency.refobjsubid
            WHERE dependency.refobjid = %s::regclass
              AND dependency.refclassid = 'pg_class'::regclass
              AND dependency.deptype = 'n'
              AND dependency.refobjsubid > 0
            ORDER BY attribute.attname
            """,
            (f"{SCHEMA}.{name}",),
        )
        return [row["attname"] for row in cur.fetchall()]

    @staticmethod
    def _validate_output_metadata(
        cur,
        definition: dict[str, Any],
    ) -> dict[str, Any]:
        cur.execute(
            sql.SQL("""
                SELECT
                  postgis_typmod_type(attribute.atttypmod) AS geometry_type,
                  postgis_typmod_srid(attribute.atttypmod) AS srid
                FROM pg_attribute AS attribute
                WHERE attribute.attrelid = {}::regclass
                  AND attribute.attname = %s
                  AND NOT attribute.attisdropped
            """).format(sql.Literal(f"{SCHEMA}.{definition['name']}")),
            (definition["geometryColumn"],),
        )
        geometry_metadata = cur.fetchone()
        if (
            not geometry_metadata
            or geometry_metadata["geometry_type"] in {None, ""}
            or int(geometry_metadata["srid"] or 0) <= 0
        ):
            raise DerivedLayerError(
                "The selected geometry field must contain PostGIS geometry "
                "with a known coordinate system (SRID)."
            )
        return {
            "geometryType": geometry_metadata["geometry_type"],
            "srid": int(geometry_metadata["srid"]),
        }

    @staticmethod
    def _validate_output_rows(
        cur,
        definition: dict[str, Any],
        *,
        duplicates_enforced: bool,
    ) -> None:
        relation = sql.Identifier(SCHEMA, definition["name"])
        identifier = sql.Identifier(definition["idColumn"])
        geometry = sql.Identifier(definition["geometryColumn"])
        cur.execute(
            f"SET LOCAL statement_timeout = "
            f"'{OUTPUT_VALIDATION_STATEMENT_TIMEOUT}'"
        )
        if duplicates_enforced:
            cur.execute(
                sql.SQL("""
                    SELECT
                      EXISTS (
                        SELECT 1 FROM {} WHERE {} IS NULL LIMIT 1
                      ) AS invalid_id,
                      EXISTS (
                        SELECT 1 FROM {} WHERE {} IS NOT NULL LIMIT 1
                      ) AS has_geometry
                """).format(relation, identifier, relation, geometry)
            )
        else:
            # Proving uniqueness requires the complete view output. Do it once,
            # and derive the non-null geometry check from the same grouped scan.
            cur.execute(
                sql.SQL("""
                    SELECT
                      COALESCE(bool_or(
                        _mapp_id IS NULL OR _mapp_count > 1
                      ), FALSE) AS invalid_id,
                      COALESCE(bool_or(
                        _mapp_has_geometry
                      ), FALSE) AS has_geometry
                    FROM (
                      SELECT
                        {} AS _mapp_id,
                        count(*) AS _mapp_count,
                        bool_or({} IS NOT NULL) AS _mapp_has_geometry
                      FROM {}
                      GROUP BY {}
                    ) AS _mapp_output_groups
                """).format(identifier, geometry, relation, identifier)
            )
        validation = cur.fetchone()
        if not isinstance(validation, dict):
            raise DerivedLayerError(
                "PostgreSQL returned an invalid derived output validation."
            )
        if validation.get("invalid_id") is True:
            raise DerivedLayerError(
                "The selected ID field must contain a unique value for every "
                "row and cannot contain empty values."
            )
        if validation.get("has_geometry") is not True:
            raise DerivedLayerError(
                "The derived result has no non-null geometry."
            )
        cur.execute("SET LOCAL statement_timeout = '30min'")

    @classmethod
    def _validate_output(
        cls,
        cur,
        definition: dict[str, Any],
    ) -> dict[str, Any]:
        output = cls._validate_output_metadata(cur, definition)
        cls._validate_output_rows(
            cur,
            definition,
            duplicates_enforced=False,
        )
        return output

    @staticmethod
    def _create_materialized_id_index(
        cur,
        definition: dict[str, Any],
    ) -> str:
        index_name = f"{definition['name']}_qid_uidx"
        try:
            cur.execute(
                sql.SQL(
                    "CREATE UNIQUE INDEX {} ON {} ({}) NULLS NOT DISTINCT"
                ).format(
                    sql.Identifier(index_name),
                    sql.Identifier(SCHEMA, definition["name"]),
                    sql.Identifier(definition["idColumn"]),
                )
            )
        except psycopg.errors.UniqueViolation:
            raise DerivedLayerError(
                "The selected ID field must contain a unique value for every "
                "row and cannot contain empty values."
            ) from None
        return index_name

    @staticmethod
    def _materialized_spatial_index_name(
        name: str,
        geometry_column: str,
        purpose: str,
    ) -> str:
        identity = f"{SCHEMA}.{name}.{geometry_column}:{purpose}"
        digest = hashlib.md5(
            identity.encode(), usedforsecurity=False,
        ).hexdigest()[:8]
        return (
            f"{name[:20]}_{geometry_column[:14]}_{purpose[:14]}_"
            f"{digest}_gix"
        )

    @classmethod
    def _materialized_spatial_index_specs(
        cls,
        definition: dict[str, Any],
        srid: int,
    ) -> list[tuple[str, Any]]:
        geometry = sql.Identifier(definition["geometryColumn"])
        specs: list[tuple[str, Any]] = [
            ("geom", geometry),
        ]
        if srid != 4326:
            specs.append((
                "geom_4326",
                sql.SQL("public.ST_Transform({}, 4326)").format(geometry),
            ))
        if srid != 3857:
            specs.append((
                "geom_3857",
                sql.SQL("public.ST_Transform({}, 3857)").format(geometry),
            ))
        if srid != 27700:
            specs.append((
                "geom_27700",
                sql.SQL("public.ST_Transform({}, 27700)").format(geometry),
            ))
        geography_source = (
            geometry
            if srid == 4326
            else sql.SQL("public.ST_Transform({}, 4326)").format(geometry)
        )
        specs.append((
            "geog_4326",
            sql.SQL("({}::public.geography)").format(geography_source),
        ))
        return specs

    @classmethod
    def _create_materialized_spatial_indexes(
        cls,
        cur,
        definition: dict[str, Any],
        srid: int,
    ) -> list[str]:
        relation = sql.Identifier(SCHEMA, definition["name"])
        index_names = []
        for purpose, expression in cls._materialized_spatial_index_specs(
            definition, srid,
        ):
            index_name = cls._materialized_spatial_index_name(
                definition["name"], definition["geometryColumn"], purpose,
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} USING gist ({})"
                ).format(
                    sql.Identifier(index_name),
                    relation,
                    expression,
                )
            )
            index_names.append(index_name)
        return index_names

    @classmethod
    def _rename_materialized_spatial_indexes(
        cls,
        cur,
        temporary: dict[str, Any],
        final_name: str,
        srid: int,
    ) -> None:
        geometry_column = temporary["geometryColumn"]
        for purpose, _expression in cls._materialized_spatial_index_specs(
            temporary, srid,
        ):
            cur.execute(
                sql.SQL("ALTER INDEX {} RENAME TO {}").format(
                    sql.Identifier(
                        SCHEMA,
                        cls._materialized_spatial_index_name(
                            temporary["name"], geometry_column, purpose,
                        ),
                    ),
                    sql.Identifier(
                        cls._materialized_spatial_index_name(
                            final_name, geometry_column, purpose,
                        )
                    ),
                )
            )

    def _finalize_materialized_output(
        self,
        cur,
        definition: dict[str, Any],
        materialization_probe: dict[str, Any],
        *,
        create_index: bool,
        output: dict[str, Any],
        error_name: str | None = None,
    ) -> dict[str, Any]:
        if create_index:
            self._create_materialized_id_index(cur, definition)
        self._create_materialized_spatial_indexes(
            cur,
            definition,
            int(output["srid"]),
        )
        self._validate_output_rows(
            cur,
            definition,
            duplicates_enforced=True,
        )
        cur.execute(
            sql.SQL("""
                SELECT pg_total_relation_size({}::regclass) AS actual_bytes
            """).format(sql.Literal(f"{SCHEMA}.{definition['name']}"))
        )
        size_row = cur.fetchone()
        actual_bytes = (
            size_row.get("actual_bytes")
            if isinstance(size_row, dict) else None
        )
        if (
            isinstance(actual_bytes, bool)
            or not isinstance(actual_bytes, int)
            or actual_bytes < 0
        ):
            raise DerivedLayerError(
                "PostgreSQL returned an invalid materialized relation size."
            )
        probe = {**materialization_probe, "actualBytes": actual_bytes}
        if actual_bytes > MATERIALIZED_MAX_ESTIMATED_BYTES:
            raise DerivedLayerMaterializationTooLarge(
                error_name or definition["name"],
                probe,
            )
        return probe

    @staticmethod
    def _executable_query(definition: dict[str, Any]):
        query = sql.SQL(definition["query"])
        spatial_scope = definition.get("spatialScope")
        if spatial_scope is None:
            return query

        alias = "_mapp_spatial_scope"
        geometry = sql.Identifier(alias, definition["geometryColumn"])
        predicates = []
        h3_scope_rows = []
        for envelope in spatial_scope["envelopes"]:
            h3_scope_rows.append(sql.SQL("({})").format(sql.SQL(
                "ST_MakeEnvelope({}, {}, {}, {}, 4326)"
            ).format(
                sql.Literal(envelope["west"]),
                sql.Literal(envelope["south"]),
                sql.Literal(envelope["east"]),
                sql.Literal(envelope["north"]),
            )))
            predicates.append(sql.SQL("""
                ST_Intersects(
                  {},
                  ST_Transform(
                    ST_MakeEnvelope({}, {}, {}, {}, 4326),
                    ST_SRID({})
                  )
                )
            """).format(
                geometry,
                sql.Literal(envelope["west"]),
                sql.Literal(envelope["south"]),
                sql.Literal(envelope["east"]),
                sql.Literal(envelope["north"]),
                geometry,
            ))
        return sql.SQL("""
            WITH {} ({}) AS (VALUES {})
            SELECT {}.*
            FROM ({}) AS {}
            WHERE ({})
        """).format(
            sql.Identifier("_mapp_h3_scope"),
            sql.Identifier("geom_4326"),
            sql.SQL(", ").join(h3_scope_rows),
            sql.Identifier(alias),
            query,
            sql.Identifier(alias),
            sql.SQL(" OR ").join(predicates),
        )

    @staticmethod
    def _require_resolved_spatial_scope(definition: dict[str, Any]) -> None:
        if definition.get("spatialScope") is None:
            raise DerivedLayerError(
                "Derived layers require a server-resolved workspace map extent."
            )

    @staticmethod
    def _materialization_probe_result(
        name: str,
        row: Any,
    ) -> dict[str, Any]:
        plan = row.get("QUERY PLAN") if isinstance(row, dict) else None
        root = (
            plan[0].get("Plan")
            if (
                isinstance(plan, list)
                and len(plan) == 1
                and isinstance(plan[0], dict)
            )
            else None
        )
        estimated_rows = root.get("Plan Rows") if isinstance(root, dict) else None
        plan_width = root.get("Plan Width") if isinstance(root, dict) else None
        if (
            isinstance(estimated_rows, bool)
            or not isinstance(estimated_rows, int)
            or estimated_rows < 0
            or isinstance(plan_width, bool)
            or not isinstance(plan_width, int)
            or plan_width < 0
        ):
            raise DerivedLayerError(
                "PostgreSQL returned an invalid materialization size probe."
            )
        estimated_bytes = math.ceil(
            estimated_rows
            * (plan_width + MATERIALIZED_ROW_OVERHEAD_BYTES)
            * MATERIALIZED_ESTIMATE_SAFETY_MULTIPLIER
        )
        probe = {
            "method": MATERIALIZED_PROBE_METHOD,
            "estimatedRows": estimated_rows,
            "planRowWidthBytes": plan_width,
            "rowOverheadBytes": MATERIALIZED_ROW_OVERHEAD_BYTES,
            "safetyMultiplier": MATERIALIZED_ESTIMATE_SAFETY_MULTIPLIER,
            "estimatedBytes": estimated_bytes,
            "maxEstimatedBytes": MATERIALIZED_MAX_ESTIMATED_BYTES,
        }
        if estimated_bytes > MATERIALIZED_MAX_ESTIMATED_BYTES:
            raise DerivedLayerMaterializationTooLarge(name, probe)
        return probe

    @staticmethod
    def _query_plan_limits() -> dict[str, int]:
        return {
            "maxTotalCost": QUERY_PLAN_MAX_TOTAL_COST,
            "maxFinalRows": QUERY_PLAN_MAX_FINAL_ROWS,
            "maxIntermediateRows": QUERY_PLAN_MAX_INTERMEDIATE_ROWS,
            "maxIntermediateBytes": QUERY_PLAN_MAX_INTERMEDIATE_BYTES,
            "maxJoinExpansionRatio": QUERY_PLAN_MAX_JOIN_EXPANSION_RATIO,
            "maxPlanNodes": QUERY_PLAN_MAX_NODES,
            "maxPlanDepth": QUERY_PLAN_MAX_DEPTH,
            "maxPlannedWorkers": QUERY_PLAN_MAX_PLANNED_WORKERS,
        }

    @staticmethod
    def query_planning_capability() -> dict[str, Any]:
        return {
            "version": QUERY_PLANNING_VERSION,
            "method": QUERY_PLANNING_METHOD,
            "maxNestedLoopPairRows": (
                QUERY_PLANNING_MAX_NESTED_LOOP_PAIR_ROWS
            ),
            "reasonCodes": ["nested_loop_pair_work"],
        }

    @classmethod
    def _query_plan_probe_result(
        cls,
        name: str,
        row: Any,
        h3_expansion: dict[str, Any],
    ) -> dict[str, Any]:
        plan = row.get("QUERY PLAN") if isinstance(row, dict) else None
        root = (
            plan[0].get("Plan")
            if (
                isinstance(plan, list)
                and len(plan) == 1
                and isinstance(plan[0], dict)
            )
            else None
        )
        if not isinstance(root, dict):
            raise DerivedLayerError(
                "PostgreSQL returned an invalid query plan probe."
            )
        total_cost = root.get("Total Cost")
        if (
            isinstance(total_cost, bool)
            or not isinstance(total_cost, (int, float))
            or not math.isfinite(total_cost)
            or total_cost < 0
        ):
            raise DerivedLayerError(
                "PostgreSQL returned an invalid query plan probe."
            )

        max_rows = 0
        max_bytes = 0
        max_join_expansion = 1.0
        node_count = 0
        plan_depth = 0
        planned_workers = 0
        recursive_plan = False
        stack = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            if not isinstance(node, dict):
                raise DerivedLayerError(
                    "PostgreSQL returned an invalid query plan probe."
                )
            node_type = node.get("Node Type")
            rows = node.get("Plan Rows")
            width = node.get("Plan Width")
            workers = node.get("Workers Planned", 0)
            children = node.get("Plans", [])
            if (
                not isinstance(node_type, str)
                or not node_type
                or isinstance(rows, bool)
                or not isinstance(rows, int)
                or rows < 0
                or isinstance(width, bool)
                or not isinstance(width, int)
                or width < 0
                or isinstance(workers, bool)
                or not isinstance(workers, int)
                or workers < 0
                or not isinstance(children, list)
                or any(not isinstance(child, dict) for child in children)
            ):
                raise DerivedLayerError(
                    "PostgreSQL returned an invalid query plan probe."
                )
            node_count += 1
            plan_depth = max(plan_depth, depth)
            planned_workers += workers
            recursive_plan = recursive_plan or node_type == "Recursive Union"
            max_rows = max(max_rows, rows)
            max_bytes = max(max_bytes, math.ceil(
                rows
                * (width + MATERIALIZED_ROW_OVERHEAD_BYTES)
                * MATERIALIZED_ESTIMATE_SAFETY_MULTIPLIER
            ))
            if children and (
                "join" in node_type.lower() or node_type == "Nested Loop"
            ):
                child_rows = [child.get("Plan Rows") for child in children]
                if all(
                    not isinstance(value, bool)
                    and isinstance(value, int)
                    and value >= 0
                    for value in child_rows
                ):
                    max_join_expansion = max(
                        max_join_expansion,
                        rows / max(1, max(child_rows)),
                    )
            stack.extend((child, depth + 1) for child in children)

        final_rows = root["Plan Rows"]
        probe = {
            "method": QUERY_PLAN_PROBE_METHOD,
            "estimatedTotalCost": float(total_cost),
            "estimatedFinalRows": final_rows,
            "maxIntermediateRows": max_rows,
            "maxIntermediateBytes": max_bytes,
            "maxJoinExpansionRatio": max_join_expansion,
            "planNodeCount": node_count,
            "planDepth": plan_depth,
            "plannedWorkers": planned_workers,
            "recursivePlan": recursive_plan,
            "h3Expansion": h3_expansion,
            "limits": cls._query_plan_limits(),
        }
        reasons = []
        if recursive_plan:
            reasons.append(_reason(
                "recursive_plan",
                "PostgreSQL planned a Recursive Union, which is not allowed.",
            ))
        if total_cost > QUERY_PLAN_MAX_TOTAL_COST:
            reasons.append(_reason(
                "total_cost",
                f"Estimated PostgreSQL cost {total_cost:g} exceeds the "
                f"{QUERY_PLAN_MAX_TOTAL_COST:g} limit.",
            ))
        if final_rows > QUERY_PLAN_MAX_FINAL_ROWS:
            reasons.append(_reason(
                "final_rows",
                f"Estimated output has {final_rows:,} rows, above the "
                f"{QUERY_PLAN_MAX_FINAL_ROWS:,} feature limit.",
            ))
        if max_rows > QUERY_PLAN_MAX_INTERMEDIATE_ROWS:
            reasons.append(_reason(
                "intermediate_rows",
                f"A plan step is estimated at {max_rows:,} rows, above the "
                f"{QUERY_PLAN_MAX_INTERMEDIATE_ROWS:,} row limit.",
            ))
        if max_bytes > QUERY_PLAN_MAX_INTERMEDIATE_BYTES:
            reasons.append(_reason(
                "intermediate_bytes",
                f"A plan step is estimated at {max_bytes:,} bytes, above the "
                f"{QUERY_PLAN_MAX_INTERMEDIATE_BYTES:,} byte limit.",
            ))
        if max_join_expansion > QUERY_PLAN_MAX_JOIN_EXPANSION_RATIO:
            reasons.append(_reason(
                "join_expansion",
                f"A join expands its largest input by an estimated "
                f"{max_join_expansion:,.1f}x, above the "
                f"{QUERY_PLAN_MAX_JOIN_EXPANSION_RATIO:,}x limit.",
            ))
        if node_count > QUERY_PLAN_MAX_NODES:
            reasons.append(_reason(
                "plan_nodes",
                f"The PostgreSQL plan has {node_count} nodes, above the "
                f"{QUERY_PLAN_MAX_NODES} node limit.",
            ))
        if plan_depth > QUERY_PLAN_MAX_DEPTH:
            reasons.append(_reason(
                "plan_depth",
                f"The PostgreSQL plan is {plan_depth} nodes deep, above the "
                f"{QUERY_PLAN_MAX_DEPTH} depth limit.",
            ))
        if planned_workers > QUERY_PLAN_MAX_PLANNED_WORKERS:
            reasons.append(_reason(
                "planned_workers",
                f"The PostgreSQL plan requests {planned_workers} parallel "
                f"workers, above the {QUERY_PLAN_MAX_PLANNED_WORKERS} limit.",
            ))
        if reasons:
            raise DerivedLayerQueryTooExpensive(name, probe, reasons)
        return probe

    @staticmethod
    def _proven_generated_row_bounds(
        inspection: QueryAstInspection,
        h3_expansion: dict[str, Any],
    ) -> tuple[int, int]:
        literal_rows_per_input = max(
            (
                call.generated_rows
                for call in inspection.function_calls
                if (
                    call.bound_kind == "literal-series"
                    and call.generated_rows is not None
                )
            ),
            default=0,
        )
        h3_rows = h3_expansion.get("estimatedExpandedCells", 0)
        h3_total_rows = (
            h3_rows
            if (
                not isinstance(h3_rows, bool)
                and isinstance(h3_rows, int)
                and h3_rows >= 0
            )
            else 0
        )
        return literal_rows_per_input, h3_total_rows

    @classmethod
    def _query_planning_probe_result(
        cls,
        name: str,
        row: Any,
        inspection: QueryAstInspection,
        h3_expansion: dict[str, Any],
        query_plan_probe: dict[str, Any],
    ) -> dict[str, Any]:
        plan = row.get("QUERY PLAN") if isinstance(row, dict) else None
        root = (
            plan[0].get("Plan")
            if (
                isinstance(plan, list)
                and len(plan) == 1
                and isinstance(plan[0], dict)
            )
            else None
        )
        if not isinstance(root, dict):
            raise DerivedLayerError(
                "PostgreSQL returned an invalid query planning probe."
            )

        nodes: list[dict[str, Any]] = []
        cte_producers: dict[str, dict[str, Any]] = {}
        stack = [root]
        while stack:
            node = stack.pop()
            node_type = node.get("Node Type")
            rows = node.get("Plan Rows")
            children = node.get("Plans", [])
            if (
                not isinstance(node_type, str)
                or not node_type
                or isinstance(rows, bool)
                or not isinstance(rows, int)
                or rows < 0
                or not isinstance(children, list)
                or any(not isinstance(child, dict) for child in children)
            ):
                raise DerivedLayerError(
                    "PostgreSQL returned an invalid query planning probe."
                )
            nodes.append(node)
            subplan_name = node.get("Subplan Name")
            if isinstance(subplan_name, str) and subplan_name.startswith("CTE "):
                cte_producers[subplan_name.removeprefix("CTE ")] = node
            stack.extend(children)

        (
            literal_rows_per_input,
            h3_total_rows,
        ) = cls._proven_generated_row_bounds(
            inspection,
            h3_expansion,
        )
        max_proven_generated_rows = max(
            literal_rows_per_input,
            h3_total_rows,
        )
        effective_rows_cache: dict[int, int] = {}
        generated_subtree_cache: dict[int, bool] = {}

        def regular_children(node: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                child
                for child in node.get("Plans", [])
                if child.get("Parent Relationship") not in {
                    "InitPlan", "SubPlan",
                }
            ]

        def has_generated_rows(
            node: dict[str, Any],
            resolving: frozenset[int] = frozenset(),
        ) -> bool:
            node_id = id(node)
            if node_id in generated_subtree_cache:
                return generated_subtree_cache[node_id]
            if node_id in resolving:
                return False
            node_type = node["Node Type"]
            generated = node_type in {"ProjectSet", "Function Scan"}
            if node_type == "CTE Scan":
                cte_name = node.get("CTE Name")
                producer = (
                    cte_producers.get(cte_name)
                    if isinstance(cte_name, str)
                    else None
                )
                if producer is not None:
                    generated = generated or has_generated_rows(
                        producer,
                        resolving | {node_id},
                    )
            generated = generated or any(
                has_generated_rows(child, resolving | {node_id})
                for child in regular_children(node)
            )
            generated_subtree_cache[node_id] = generated
            return generated

        def effective_rows(
            node: dict[str, Any],
            resolving: frozenset[int] = frozenset(),
        ) -> int:
            node_id = id(node)
            if node_id in effective_rows_cache:
                return effective_rows_cache[node_id]
            planned_rows = node["Plan Rows"]
            if node_id in resolving:
                return planned_rows
            node_type = node["Node Type"]
            corrected_rows = planned_rows
            if node_type == "ProjectSet":
                children = regular_children(node)
                input_rows = (
                    effective_rows(
                        children[0],
                        resolving | {node_id},
                    )
                    if len(children) == 1
                    else 1
                )
                corrected_rows = max(
                    corrected_rows,
                    h3_total_rows,
                    input_rows * literal_rows_per_input,
                )
            elif node_type == "Function Scan":
                corrected_rows = max(
                    corrected_rows,
                    h3_total_rows,
                    literal_rows_per_input,
                )
            elif node_type == "CTE Scan":
                cte_name = node.get("CTE Name")
                producer = (
                    cte_producers.get(cte_name)
                    if isinstance(cte_name, str)
                    else None
                )
                if producer is not None:
                    corrected_rows = max(
                        corrected_rows,
                        effective_rows(
                            producer,
                            resolving | {node_id},
                        ),
                    )
            else:
                children = regular_children(node)
                if (
                    node_type in QUERY_PLANNING_BOUND_PROPAGATING_NODES
                    and len(children) == 1
                    and not (
                        node_type == "Aggregate"
                        and node.get("Strategy") == "Plain"
                    )
                    and node.get("One-Time Filter") != "false"
                ):
                    corrected_rows = max(
                        corrected_rows,
                        effective_rows(
                            children[0],
                            resolving | {node_id},
                        ),
                    )
                elif node_type in {"Append", "Merge Append"} and children:
                    corrected_rows = max(
                        corrected_rows,
                        sum(
                            (
                                effective_rows(
                                    child,
                                    resolving | {node_id},
                                )
                                if has_generated_rows(
                                    child,
                                    resolving | {node_id},
                                )
                                else child["Plan Rows"]
                            )
                            for child in children
                        ),
                    )
            effective_rows_cache[node_id] = corrected_rows
            return corrected_rows

        nested_loop_count = 0
        max_nested_loop_pair_rows = 0
        for node in nodes:
            if node["Node Type"] != "Nested Loop":
                continue
            nested_loop_count += 1
            children = regular_children(node)
            if len(children) != 2:
                continue
            pair_rows = (
                effective_rows(children[0])
                * effective_rows(children[1])
            )
            max_nested_loop_pair_rows = max(
                max_nested_loop_pair_rows,
                pair_rows,
            )

        probe = {
            "version": QUERY_PLANNING_VERSION,
            "method": QUERY_PLANNING_METHOD,
            "maxProvenGeneratedRows": max_proven_generated_rows,
            "nestedLoopCount": nested_loop_count,
            "maxEstimatedNestedLoopPairRows": max_nested_loop_pair_rows,
            "maxAllowedNestedLoopPairRows": (
                QUERY_PLANNING_MAX_NESTED_LOOP_PAIR_ROWS
            ),
        }
        if (
            max_nested_loop_pair_rows
            > QUERY_PLANNING_MAX_NESTED_LOOP_PAIR_ROWS
        ):
            raise DerivedLayerQueryTooExpensive(
                name,
                query_plan_probe,
                [_reason(
                    "nested_loop_pair_work",
                    "PostgreSQL plans up to "
                    f"{max_nested_loop_pair_rows:,} nested-loop input row "
                    "pairs after applying proven generated-row bounds, above "
                    f"the {QUERY_PLANNING_MAX_NESTED_LOOP_PAIR_ROWS:,} "
                    "pair limit.",
                )],
                query_planning_probe=probe,
            )
        return probe

    def _query_probe(
        self,
        cur,
        definition: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        h3_expansion = _query_shape_guard(definition)
        inspection = validate_query_ast(definition["query"])
        cur.execute("SET LOCAL statement_timeout = '5s'")
        self._catalog_query_probe(cur, definition, inspection)
        cur.execute(
            sql.SQL("EXPLAIN (FORMAT JSON) {}").format(
                self._executable_query(definition)
            )
        )
        row = cur.fetchone()
        query_plan_probe = self._query_plan_probe_result(
            definition["name"], row, h3_expansion,
        )
        query_planning_probe = self._query_planning_probe_result(
            definition["name"],
            row,
            inspection,
            h3_expansion,
            query_plan_probe,
        )
        materialization_probe = (
            self._materialization_probe_result(definition["name"], row)
            if definition["kind"] == "materialized" else None
        )
        return query_plan_probe, query_planning_probe, materialization_probe

    @staticmethod
    def _validate_catalog_dependencies(
        cur,
        definition: dict[str, Any],
        relation_name: str,
        inspection: QueryAstInspection,
    ) -> None:
        try:
            validate_relation_routines(
                cur,
                SCHEMA,
                relation_name,
                inspection,
            )
        except QueryGuardViolation as exc:
            raise DerivedLayerQueryTooExpensive(
                definition["name"],
                {"method": "postgresql-catalog-guard"},
                [reason.as_dict() for reason in exc.reasons],
            ) from exc

    def _catalog_query_probe(
        self,
        cur,
        definition: dict[str, Any],
        inspection: QueryAstInspection,
    ) -> None:
        try:
            validate_qualified_cast_types(cur, inspection)
        except QueryGuardViolation as exc:
            raise DerivedLayerQueryTooExpensive(
                definition["name"],
                {"method": "postgresql-catalog-guard"},
                [reason.as_dict() for reason in exc.reasons],
            ) from exc
        probe_name = f"probe_{secrets.token_hex(10)}"
        target = sql.Identifier(SCHEMA, probe_name)
        cur.execute("SAVEPOINT derived_catalog_probe")
        try:
            cur.execute(
                sql.SQL(
                    "CREATE VIEW {} WITH (security_invoker=true, "
                    "security_barrier=true) AS {}"
                ).format(target, self._executable_query(definition))
            )
            dependencies = self._dependencies(cur, probe_name)
            if dependencies != definition["sources"]:
                raise DerivedLayerSourceMismatchError(
                    definition["sources"], dependencies,
                )
            self._validate_catalog_dependencies(
                cur,
                definition,
                probe_name,
                inspection,
            )
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT derived_catalog_probe")
            cur.execute("RELEASE SAVEPOINT derived_catalog_probe")
            raise
        cur.execute("ROLLBACK TO SAVEPOINT derived_catalog_probe")
        cur.execute("RELEASE SAVEPOINT derived_catalog_probe")

    def preflight_definition(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        definition = validate_definition(payload)
        self._require_resolved_spatial_scope(definition)
        with self._connect() as connection, connection.cursor() as cur:
            (
                query_plan_probe,
                query_planning_probe,
                materialization_probe,
            ) = self._query_probe(
                cur, definition,
            )
            result = {
                "queryPlanProbe": query_plan_probe,
                "queryPlanningProbe": query_planning_probe,
            }
            if materialization_probe is not None:
                result["materializationProbe"] = materialization_probe
            return result

    def preflight_refresh(self, name: str) -> dict[str, Any]:
        if not NAME_RE.fullmatch(name):
            raise DerivedLayerError("Invalid derived-layer name.")
        with self._connect() as connection, connection.cursor() as cur:
            definition = self.get_in_transaction(cur, name)
            if not definition:
                raise FileNotFoundError(name)
            if definition["kind"] != "materialized":
                raise DerivedLayerError("Only materialized views can be refreshed.")
            self._require_resolved_spatial_scope(definition)
            (
                query_plan_probe,
                query_planning_probe,
                materialization_probe,
            ) = self._query_probe(
                cur, definition,
            )
            return {
                "queryPlanProbe": query_plan_probe,
                "queryPlanningProbe": query_planning_probe,
                "materializationProbe": materialization_probe,
            }

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(sql.SQL("""
                SELECT name, kind, sources, id_column AS "idColumn",
                       geometry_column AS "geometryColumn", description,
                       spatial_scope AS "spatialScope",
                       created_at AS "createdAt", created_by AS "createdBy",
                       refreshed_at AS "refreshedAt",
                       semantic_asset_id AS "semanticAssetId",
                       semantic_generation AS "semanticGeneration",
                       semantic_status AS "semanticStatus",
                       semantic_revision AS "semanticRevision"
                FROM {}._definitions
                ORDER BY name
            """).format(sql.Identifier(SCHEMA)))
            return [
                self._with_semantic_profile(item)
                for item in cur.fetchall()
            ]

    def list_page(
        self,
        *,
        after_name: str | None,
        fetch_limit: int,
    ) -> list[dict[str, Any]]:
        if after_name is not None and not NAME_RE.fullmatch(after_name):
            raise DerivedLayerError("Derived-layer page position is invalid.")
        if (
            isinstance(fetch_limit, bool)
            or not isinstance(fetch_limit, int)
            or not 1 <= fetch_limit <= 101
        ):
            raise DerivedLayerError("Derived-layer page limit is invalid.")
        with self._connect() as connection, connection.cursor() as cur:
            where = sql.SQL("WHERE name > %s") if after_name else sql.SQL("")
            values = (after_name, fetch_limit) if after_name else (fetch_limit,)
            cur.execute(sql.SQL("""
                SELECT name, kind, sources, id_column AS "idColumn",
                       geometry_column AS "geometryColumn", description,
                       spatial_scope AS "spatialScope",
                       created_at AS "createdAt", created_by AS "createdBy",
                       refreshed_at AS "refreshedAt",
                       semantic_asset_id AS "semanticAssetId",
                       semantic_generation AS "semanticGeneration",
                       semantic_status AS "semanticStatus",
                       semantic_revision AS "semanticRevision"
                FROM {}._definitions
                {}
                ORDER BY name
                LIMIT %s
            """).format(sql.Identifier(SCHEMA), where), values)
            return [
                self._with_semantic_profile(item)
                for item in cur.fetchall()
            ]

    def get(self, name: str, *, include_query: bool = True) -> dict[str, Any]:
        if not NAME_RE.fullmatch(name):
            raise DerivedLayerError("Invalid derived-layer name.")
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(sql.SQL("""
                SELECT name, kind, query, sources,
                       id_column AS "idColumn",
                       geometry_column AS "geometryColumn", description,
                       spatial_scope AS "spatialScope",
                       created_at AS "createdAt", created_by AS "createdBy",
                       refreshed_at AS "refreshedAt",
                       semantic_asset_id AS "semanticAssetId",
                       semantic_generation AS "semanticGeneration",
                       semantic_status AS "semanticStatus",
                       semantic_revision AS "semanticRevision"
                FROM {}._definitions WHERE name = %s
            """).format(sql.Identifier(SCHEMA)), (name,))
            item = cur.fetchone()
            if not item:
                raise FileNotFoundError(name)
            if not include_query:
                item.pop("query", None)
            return self._with_semantic_profile(item)

    def dependents(self, name: str) -> list[str]:
        if not NAME_RE.fullmatch(name):
            raise DerivedLayerError("Invalid derived-layer name.")
        with self._connect() as connection, connection.cursor() as cur:
            return self._incoming_dependents(cur, name)

    def create(
        self,
        payload: dict[str, Any],
        actor: str,
        *,
        cancellation: DerivedLayerCancellation | None = None,
    ) -> dict[str, Any]:
        definition = validate_definition(payload)
        with self._mutation_connection(cancellation) as connection, connection.cursor() as cur:
            # Materialization and output validation can legitimately outlive an
            # HTTP request. The dashboard submits these as durable background
            # operations; retain a finite database-side safety bound.
            cur.execute("SET LOCAL statement_timeout = '30min'")
            self._acquire_mutation_lock(connection)
            cur.execute("SET LOCAL lock_timeout = '5s'")
            self._ensure_changes_allowed(cur)
            self._require_resolved_spatial_scope(definition)
            cur.execute(sql.SQL("SELECT 1 FROM {}._definitions WHERE name = %s").format(
                sql.Identifier(SCHEMA)
            ), (definition["name"],))
            if cur.fetchone():
                raise FileExistsError(definition["name"])
            (
                query_plan_probe,
                query_planning_probe,
                materialization_probe,
            ) = self._query_probe(
                cur, definition,
            )
            cur.execute("SET LOCAL statement_timeout = '30min'")
            query = self._executable_query(definition)
            target = sql.Identifier(SCHEMA, definition["name"])
            if definition["kind"] == "view":
                cur.execute(
                    sql.SQL(
                        "CREATE VIEW {} WITH (security_invoker=true, "
                        "security_barrier=true) AS {}"
                    ).format(target, query)
                )
            else:
                cur.execute(
                    sql.SQL(
                        "CREATE MATERIALIZED VIEW {} AS {} WITH NO DATA"
                    ).format(
                        target, query
                    )
                )
            dependencies = self._dependencies(cur, definition["name"])
            if dependencies != definition["sources"]:
                raise DerivedLayerSourceMismatchError(
                    definition["sources"], dependencies,
                )
            self._validate_catalog_dependencies(
                cur,
                definition,
                definition["name"],
                validate_query_ast(definition["query"]),
            )
            if definition["kind"] == "materialized":
                output = self._validate_output_metadata(cur, definition)
                cur.execute(
                    sql.SQL("REFRESH MATERIALIZED VIEW {}").format(target)
                )
                if materialization_probe is None:
                    raise DerivedLayerError(
                        "Materialized derived layer size probe is missing."
                    )
                materialization_probe = self._finalize_materialized_output(
                    cur,
                    definition,
                    materialization_probe,
                    create_index=True,
                    output=output,
                )
            else:
                output = self._validate_output(cur, definition)
            cur.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    target, sql.Identifier(self.reader_role)
                )
            )
            cur.execute(
                sql.SQL("""
                    INSERT INTO {}._definitions
                      (name, kind, query, sources, id_column, geometry_column,
                       description, spatial_scope, created_by, refreshed_at,
                       semantic_asset_id, semantic_generation,
                       semantic_status, semantic_revision)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                            %s, 1, 'registering', NULL)
                """).format(sql.Identifier(SCHEMA)),
                (
                    definition["name"], definition["kind"], definition["query"],
                    definition["sources"], definition["idColumn"],
                    definition["geometryColumn"], definition["description"],
                    (
                        Jsonb(definition["spatialScope"])
                        if definition["spatialScope"] is not None else None
                    ),
                    actor, definition["kind"] == "materialized",
                    str(uuid.uuid4()),
                ),
            )
            item = self.get_in_transaction(cur, definition["name"])
            item.update(output)
            item["queryPlanProbe"] = query_plan_probe
            item["queryPlanningProbe"] = query_planning_probe
            if materialization_probe is not None:
                item["materializationProbe"] = materialization_probe
            self._enqueue_semantic_event(
                cur,
                item,
                "register",
                actor,
                self._semantic_fields(cur, definition["name"]),
            )
            return item

    def get_in_transaction(self, cur, name: str) -> dict[str, Any]:
        cur.execute(sql.SQL("""
            SELECT name, kind, query, sources, id_column AS "idColumn",
                   geometry_column AS "geometryColumn", description,
                   spatial_scope AS "spatialScope",
                   created_at AS "createdAt", created_by AS "createdBy",
                   refreshed_at AS "refreshedAt",
                   semantic_asset_id AS "semanticAssetId",
                   semantic_generation AS "semanticGeneration",
                   semantic_status AS "semanticStatus",
                   semantic_revision AS "semanticRevision"
            FROM {}._definitions WHERE name = %s
        """).format(sql.Identifier(SCHEMA)), (name,))
        item = cur.fetchone()
        return self._with_semantic_profile(item) if item else None

    def refresh(
        self,
        name: str,
        actor: str = "system",
        *,
        cancellation: DerivedLayerCancellation | None = None,
    ) -> dict[str, Any]:
        if not NAME_RE.fullmatch(name):
            raise DerivedLayerError("Invalid derived-layer name.")
        with self._mutation_connection(cancellation) as connection, connection.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '30min'")
            self._acquire_mutation_lock(connection)
            cur.execute("SET LOCAL lock_timeout = '5s'")
            self._ensure_changes_allowed(cur)
            definition = self.get_in_transaction(cur, name)
            if not definition:
                raise FileNotFoundError(name)
            if definition["kind"] != "materialized":
                raise DerivedLayerError("Only materialized views can be refreshed.")
            self._require_resolved_spatial_scope(definition)
            (
                query_plan_probe,
                query_planning_probe,
                materialization_probe,
            ) = self._query_probe(
                cur, definition,
            )
            cur.execute("SET LOCAL statement_timeout = '30min'")
            dependencies = self._dependencies(cur, name)
            if dependencies != definition["sources"]:
                raise DerivedLayerSourceMismatchError(
                    definition["sources"], dependencies,
                )
            self._validate_catalog_dependencies(
                cur,
                definition,
                name,
                validate_query_ast(definition["query"]),
            )
            output = self._validate_output_metadata(cur, definition)
            cur.execute(
                sql.SQL("REFRESH MATERIALIZED VIEW {}").format(
                    sql.Identifier(SCHEMA, name)
                )
            )
            if materialization_probe is None:
                raise DerivedLayerError(
                    "Materialized derived layer size probe is missing."
                )
            materialization_probe = self._finalize_materialized_output(
                cur,
                definition,
                materialization_probe,
                create_index=False,
                output=output,
            )
            cur.execute(sql.SQL("""
                UPDATE {}._definitions
                SET refreshed_at = clock_timestamp(),
                    semantic_generation = semantic_generation + 1,
                    semantic_status = 'registering',
                    semantic_revision = NULL
                WHERE name = %s
            """).format(sql.Identifier(SCHEMA)), (name,))
            item = self.get_in_transaction(cur, name)
            item["queryPlanProbe"] = query_plan_probe
            item["queryPlanningProbe"] = query_planning_probe
            item["materializationProbe"] = materialization_probe
            self._enqueue_semantic_event(
                cur,
                item,
                "refresh",
                actor,
                self._semantic_fields(cur, name),
            )
            return item

    def replace(
        self,
        name: str,
        payload: dict[str, Any],
        actor: str,
        *,
        cancellation: DerivedLayerCancellation | None = None,
    ) -> dict[str, Any]:
        definition = validate_definition(payload)
        if definition["name"] != name:
            raise DerivedLayerError("Replacement name must match the existing relation.")
        with self._mutation_connection(cancellation) as connection, connection.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '30min'")
            self._acquire_mutation_lock(connection)
            cur.execute("SET LOCAL lock_timeout = '5s'")
            self._ensure_changes_allowed(cur)
            current = self.get_in_transaction(cur, name)
            if not current:
                raise FileNotFoundError(name)
            self._require_resolved_spatial_scope(definition)
            (
                query_plan_probe,
                query_planning_probe,
                materialization_probe,
            ) = self._query_probe(
                cur, definition,
            )
            cur.execute("SET LOCAL statement_timeout = '30min'")
            dependents = self._incoming_dependents(cur, name)
            dependent_columns = self._dependent_columns(cur, name)
            current_columns = self._column_names(cur, name)
            current_types = self._column_types(cur, name)

            temporary_name = f"swap_{secrets.token_hex(10)}"
            temporary = {**definition, "name": temporary_name}
            temporary_target = sql.Identifier(SCHEMA, temporary_name)
            query = self._executable_query(definition)
            if definition["kind"] == "view":
                cur.execute(
                    sql.SQL(
                        "CREATE VIEW {} WITH (security_invoker=true, "
                        "security_barrier=true) AS {}"
                    ).format(temporary_target, query)
                )
            else:
                cur.execute(
                    sql.SQL(
                        "CREATE MATERIALIZED VIEW {} AS {} WITH NO DATA"
                    ).format(
                        temporary_target, query
                    )
                )
            dependencies = self._dependencies(cur, temporary_name)
            if dependencies != definition["sources"]:
                raise DerivedLayerSourceMismatchError(
                    definition["sources"], dependencies,
                )
            self._validate_catalog_dependencies(
                cur,
                definition,
                temporary_name,
                validate_query_ast(definition["query"]),
            )
            output = self._validate_output_metadata(cur, temporary)
            replacement_columns = self._column_names(cur, temporary_name)
            replacement_types = self._column_types(cur, temporary_name)
            removed_columns = sorted(set(current_columns) - set(replacement_columns))
            added_columns = sorted(set(replacement_columns) - set(current_columns))
            changed_columns = sorted(
                column for column in set(current_columns) & set(replacement_columns)
                if current_types[column] != replacement_types[column]
            )
            if dependents:
                raise DerivedLayerDependencyError(
                    name, dependents, removed_columns=removed_columns,
                    dependent_columns=dependent_columns,
                )
            temporary_index = f"{temporary_name}_qid_uidx"
            if definition["kind"] == "materialized":
                cur.execute(
                    sql.SQL("REFRESH MATERIALIZED VIEW {}").format(
                        temporary_target
                    )
                )
                if materialization_probe is None:
                    raise DerivedLayerError(
                        "Materialized derived layer size probe is missing."
                    )
                materialization_probe = self._finalize_materialized_output(
                    cur,
                    temporary,
                    materialization_probe,
                    create_index=True,
                    output=output,
                    error_name=name,
                )
            else:
                self._validate_output_rows(
                    cur,
                    temporary,
                    duplicates_enforced=False,
                )
            cur.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    temporary_target, sql.Identifier(self.reader_role)
                )
            )

            current_keyword = (
                sql.SQL("MATERIALIZED VIEW")
                if current["kind"] == "materialized"
                else sql.SQL("VIEW")
            )
            replacement_keyword = (
                sql.SQL("MATERIALIZED VIEW")
                if definition["kind"] == "materialized"
                else sql.SQL("VIEW")
            )
            cur.execute("SAVEPOINT derived_drop_guard")
            try:
                cur.execute(
                    sql.SQL("DROP {} {} RESTRICT").format(
                        current_keyword, sql.Identifier(SCHEMA, name)
                    )
                )
            except psycopg.errors.DependentObjectsStillExist:
                cur.execute("ROLLBACK TO SAVEPOINT derived_drop_guard")
                raise DerivedLayerDependencyError(
                    name,
                    self._incoming_dependents(cur, name),
                ) from None
            cur.execute(
                sql.SQL("ALTER {} {} RENAME TO {}").format(
                    replacement_keyword,
                    temporary_target,
                    sql.Identifier(name),
                )
            )
            if definition["kind"] == "materialized":
                cur.execute(
                    sql.SQL("ALTER INDEX {} RENAME TO {}").format(
                        sql.Identifier(SCHEMA, temporary_index),
                        sql.Identifier(f"{name}_qid_uidx"),
                    )
                )
                self._rename_materialized_spatial_indexes(
                    cur,
                    temporary,
                    name,
                    int(output["srid"]),
                )
            cur.execute(
                sql.SQL("""
                    UPDATE {}._definitions
                    SET kind = %s, query = %s, sources = %s,
                        id_column = %s, geometry_column = %s,
                        description = %s, spatial_scope = %s, created_by = %s,
                        refreshed_at = CASE WHEN %s
                          THEN clock_timestamp() ELSE NULL END,
                        semantic_generation = semantic_generation + 1,
                        semantic_status = 'registering',
                        semantic_revision = NULL
                    WHERE name = %s
                """).format(sql.Identifier(SCHEMA)),
                (
                    definition["kind"], definition["query"],
                    definition["sources"], definition["idColumn"],
                    definition["geometryColumn"], definition["description"],
                    (
                        Jsonb(definition["spatialScope"])
                        if definition["spatialScope"] is not None else None
                    ),
                    actor, definition["kind"] == "materialized", name,
                ),
            )
            item = self.get_in_transaction(cur, name)
            item.update(output)
            item["queryPlanProbe"] = query_plan_probe
            item["queryPlanningProbe"] = query_planning_probe
            if materialization_probe is not None:
                item["materializationProbe"] = materialization_probe
            item["replacedKind"] = current["kind"]
            item["columnChanges"] = {
                "added": added_columns,
                "removed": removed_columns,
                "changed": changed_columns,
            }
            self._enqueue_semantic_event(
                cur,
                item,
                "replace",
                actor,
                self._semantic_fields(cur, name),
            )
            return item

    def drop(self, name: str, actor: str = "system") -> dict[str, Any]:
        if not NAME_RE.fullmatch(name):
            raise DerivedLayerError("Invalid derived-layer name.")
        with self._mutation_connection() as connection, connection.cursor() as cur:
            self._acquire_mutation_lock(connection)
            cur.execute("SET LOCAL lock_timeout = '5s'")
            self._ensure_changes_allowed(cur)
            definition = self.get_in_transaction(cur, name)
            if not definition:
                raise FileNotFoundError(name)
            dependents = self._incoming_dependents(cur, name)
            if dependents:
                raise DerivedLayerDependencyError(name, dependents)
            fields = self._semantic_fields(cur, name)
            cur.execute(sql.SQL("""
                UPDATE {}._definitions
                SET semantic_generation = semantic_generation + 1,
                    semantic_status = 'pending_archive',
                    semantic_revision = NULL
                WHERE name = %s
            """).format(sql.Identifier(SCHEMA)), (name,))
            definition = self.get_in_transaction(cur, name)
            self._enqueue_semantic_event(
                cur,
                definition,
                "archive",
                actor,
                fields,
            )
            keyword = (
                sql.SQL("MATERIALIZED VIEW")
                if definition["kind"] == "materialized"
                else sql.SQL("VIEW")
            )
            cur.execute("SAVEPOINT derived_drop_guard")
            try:
                cur.execute(
                    sql.SQL("DROP {} {} RESTRICT").format(
                        keyword, sql.Identifier(SCHEMA, name)
                    )
                )
            except psycopg.errors.DependentObjectsStillExist:
                cur.execute("ROLLBACK TO SAVEPOINT derived_drop_guard")
                raise DerivedLayerDependencyError(
                    name,
                    self._incoming_dependents(cur, name),
                ) from None
            cur.execute(sql.SQL("DELETE FROM {}._definitions WHERE name = %s").format(
                sql.Identifier(SCHEMA)
            ), (name,))
        return definition

    def claim_semantic_events(
        self,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 1000
        ):
            raise DerivedLayerError(
                "Semantic outbox limit must be between 1 and 1000."
            )
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 15
            or lease_seconds > 600
        ):
            raise DerivedLayerError(
                "Semantic outbox lease must be between 15 and 600 seconds."
            )
        claim_id = str(uuid.uuid4())
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(sql.SQL("""
                WITH candidates AS (
                  SELECT event.event_id
                  FROM {}._semantic_outbox AS event
                  WHERE event.status IN ('pending', 'retrying')
                    AND event.available_at <= clock_timestamp()
                    AND (
                      event.claimed_until IS NULL
                      OR event.claimed_until <= clock_timestamp()
                    )
                    AND NOT EXISTS (
                      SELECT 1
                      FROM {}._semantic_outbox AS earlier
                      WHERE earlier.asset_id = event.asset_id
                        AND earlier.generation < event.generation
                        AND earlier.status <> 'delivered'
                    )
                    AND NOT EXISTS (
                      SELECT 1
                      FROM {}._semantic_outbox AS earlier
                      WHERE earlier.payload #>> '{{generated,name}}'
                            = event.payload #>> '{{generated,name}}'
                        AND earlier.status <> 'delivered'
                        AND earlier.event_id <> event.event_id
                        AND (
                          (earlier.created_at, earlier.event_id)
                            < (event.created_at, event.event_id)
                          OR (
                            event.event_type = 'register'
                            AND earlier.event_type = 'archive'
                            AND earlier.asset_id <> event.asset_id
                          )
                        )
                    )
                  ORDER BY event.created_at, event.event_id
                  LIMIT %s
                  FOR UPDATE OF event SKIP LOCKED
                )
                UPDATE {}._semantic_outbox AS event
                SET claim_id = %s,
                    claimed_until = (
                      clock_timestamp() + (%s * interval '1 second')
                    ),
                    updated_at = clock_timestamp()
                FROM candidates
                WHERE event.event_id = candidates.event_id
                RETURNING event.event_id::text AS "eventId",
                          event.asset_id::text AS "assetId",
                          event.event_type AS "type",
                          event.generation,
                          event.payload,
                          event.status,
                          event.attempts,
                          event.available_at AS "availableAt",
                          event.last_error AS "lastError",
                          event.created_at AS "createdAt",
                          event.claim_id::text AS "claimId"
            """).format(
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
            ), (limit, claim_id, lease_seconds))
            return list(cur.fetchall())

    def semantic_outbox_blockers(
        self,
        *,
        profile_names: list[str] | None = None,
        include_unmatched: bool = True,
        one_per_profile: bool = False,
        unmatched_only: bool = False,
        fetch_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if profile_names is not None and (
            len(profile_names) > 100
            or any(not NAME_RE.fullmatch(name) for name in profile_names)
        ):
            raise DerivedLayerError("Semantic profile names are invalid.")
        if not isinstance(include_unmatched, bool):
            raise DerivedLayerError("Semantic blocker scope is invalid.")
        if (
            not isinstance(unmatched_only, bool)
            or (unmatched_only and profile_names is not None)
        ):
            raise DerivedLayerError("Semantic blocker scope is invalid.")
        if (
            not isinstance(one_per_profile, bool)
            or (
                one_per_profile
                and (profile_names is None or include_unmatched)
            )
        ):
            raise DerivedLayerError("Semantic blocker grouping is invalid.")
        if (
            fetch_limit is not None
            and (
                isinstance(fetch_limit, bool)
                or not isinstance(fetch_limit, int)
                or not 1 <= fetch_limit <= 101
            )
        ):
            raise DerivedLayerError("Semantic blocker limit is invalid.")
        with self._connect() as connection, connection.cursor() as cur:
            filter_sql = sql.SQL("")
            values: list[Any] = []
            if unmatched_only:
                filter_sql = sql.SQL("""
                    AND NOT EXISTS (
                      SELECT 1
                      FROM {}._definitions AS definition
                      WHERE definition.name =
                            event.payload #>> '{{generated,name}}'
                    )
                """).format(sql.Identifier(SCHEMA))
            elif profile_names is not None:
                if include_unmatched:
                    filter_sql = sql.SQL("""
                        AND (
                          event.payload #>> '{{generated,name}}' = ANY(%s)
                          OR NOT EXISTS (
                            SELECT 1
                            FROM {}._definitions AS definition
                            WHERE definition.name =
                                  event.payload #>> '{{generated,name}}'
                          )
                        )
                    """).format(sql.Identifier(SCHEMA))
                else:
                    filter_sql = sql.SQL("""
                        AND event.payload #>> '{{generated,name}}' = ANY(%s)
                    """)
                values.append(profile_names)
            limit_sql = sql.SQL("")
            if fetch_limit is not None:
                limit_sql = sql.SQL("LIMIT %s")
                values.append(fetch_limit)
            distinct_sql = (
                sql.SQL(
                    "DISTINCT ON (event.payload #>> "
                    "'{{generated,name}}')"
                )
                if one_per_profile
                else sql.SQL("")
            )
            order_sql = (
                sql.SQL(
                    "event.payload #>> '{{generated,name}}', "
                    "event.created_at, event.event_id"
                )
                if one_per_profile
                else sql.SQL("event.created_at, event.event_id")
            )
            cur.execute(sql.SQL("""
                SELECT {}
                       event_id::text AS "eventId",
                       asset_id::text AS "assetId",
                       event_type AS "type",
                       generation,
                       status,
                       attempts,
                       payload #>> '{{generated,name}}' AS name,
                       last_error AS "lastError"
                FROM {}._semantic_outbox AS event
                WHERE event.status <> 'delivered'
                {}
                ORDER BY {}
                {}
            """).format(
                distinct_sql,
                sql.Identifier(SCHEMA),
                filter_sql,
                order_sql,
                limit_sql,
            ), tuple(values))
            return list(cur.fetchall())

    @staticmethod
    def _semantic_uuid(value: str | uuid.UUID, label: str) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (AttributeError, TypeError, ValueError):
            raise DerivedLayerError(f"{label} must be a UUID.") from None

    def mark_semantic_delivered(
        self,
        event_id: str | uuid.UUID,
        claim_id: str | uuid.UUID,
        revision: str | int,
    ) -> bool:
        normalized_event_id = self._semantic_uuid(event_id, "Semantic event ID")
        normalized_claim_id = self._semantic_uuid(claim_id, "Semantic claim ID")
        normalized_revision = str(revision).strip()
        if not normalized_revision:
            raise DerivedLayerError("Semantic revision is required.")
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(sql.SQL("""
                UPDATE {}._semantic_outbox
                SET status = 'delivered',
                    semantic_revision = %s,
                    last_error = NULL,
                    delivered_at = clock_timestamp(),
                    updated_at = clock_timestamp(),
                    claim_id = NULL,
                    claimed_until = NULL
                WHERE event_id = %s
                  AND claim_id = %s
                  AND status IN ('pending', 'retrying')
                RETURNING asset_id, generation, event_type
            """).format(sql.Identifier(SCHEMA)), (
                normalized_revision,
                normalized_event_id,
                normalized_claim_id,
            ))
            event = cur.fetchone()
            if event is None:
                return False
            cur.execute(sql.SQL("""
                UPDATE {}._definitions
                SET semantic_status = %s,
                    semantic_revision = %s
                WHERE semantic_asset_id = %s
                  AND semantic_generation = %s
            """).format(sql.Identifier(SCHEMA)), (
                (
                    "archived"
                    if event["event_type"] == "archive"
                    else "ready"
                ),
                normalized_revision,
                event["asset_id"],
                event["generation"],
            ))
            return True

    def mark_semantic_retry(
        self,
        event_id: str | uuid.UUID,
        claim_id: str | uuid.UUID,
        error: str,
        retry_at: datetime | None = None,
    ) -> bool:
        normalized_event_id = self._semantic_uuid(event_id, "Semantic event ID")
        normalized_claim_id = self._semantic_uuid(claim_id, "Semantic claim ID")
        message = str(error).strip()[:2000]
        if not message:
            raise DerivedLayerError("Semantic retry error is required.")
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(sql.SQL("""
                UPDATE {}._semantic_outbox
                SET status = 'retrying',
                    attempts = attempts + 1,
                    available_at = COALESCE(%s, clock_timestamp()),
                    last_error = %s,
                    updated_at = clock_timestamp(),
                    claim_id = NULL,
                    claimed_until = NULL
                WHERE event_id = %s
                  AND claim_id = %s
                  AND status IN ('pending', 'retrying')
                RETURNING asset_id, generation, event_type
            """).format(sql.Identifier(SCHEMA)), (
                retry_at,
                message,
                normalized_event_id,
                normalized_claim_id,
            ))
            event = cur.fetchone()
            if not event:
                return False
            cur.execute(sql.SQL("""
                UPDATE {}._definitions
                SET semantic_status = %s
                WHERE semantic_asset_id = %s
                  AND semantic_generation = %s
            """).format(sql.Identifier(SCHEMA)), (
                (
                    "pending_archive"
                    if event["event_type"] == "archive"
                    else "registering"
                ),
                event["asset_id"],
                event["generation"],
            ))
            return True

    def mark_semantic_repair(
        self,
        event_id: str | uuid.UUID,
        claim_id: str | uuid.UUID,
        error: str,
    ) -> bool:
        normalized_event_id = self._semantic_uuid(event_id, "Semantic event ID")
        normalized_claim_id = self._semantic_uuid(claim_id, "Semantic claim ID")
        message = str(error).strip()[:2000]
        if not message:
            raise DerivedLayerError("Semantic repair error is required.")
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(sql.SQL("""
                UPDATE {}._semantic_outbox
                SET status = 'repair_required',
                    attempts = attempts + 1,
                    last_error = %s,
                    updated_at = clock_timestamp(),
                    claim_id = NULL,
                    claimed_until = NULL
                WHERE event_id = %s
                  AND claim_id = %s
                  AND status IN ('pending', 'retrying')
                RETURNING asset_id, generation
            """).format(sql.Identifier(SCHEMA)), (
                message,
                normalized_event_id,
                normalized_claim_id,
            ))
            event = cur.fetchone()
            if not event:
                return False
            cur.execute(sql.SQL("""
                UPDATE {}._definitions
                SET semantic_status = 'repair_required'
                WHERE semantic_asset_id = %s
                  AND semantic_generation = %s
            """).format(sql.Identifier(SCHEMA)), (
                event["asset_id"],
                event["generation"],
            ))
            return True

    def repair_semantic_profile(self, name: str) -> dict[str, Any]:
        if not NAME_RE.fullmatch(name):
            raise DerivedLayerError("Invalid derived-layer name.")
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA,))
            self._ensure_changes_allowed(cur)
            cur.execute(sql.SQL("""
                UPDATE {}._semantic_outbox
                SET status = 'pending',
                    attempts = 0,
                    available_at = clock_timestamp(),
                    last_error = NULL,
                    claim_id = NULL,
                    claimed_until = NULL,
                    updated_at = clock_timestamp()
                WHERE event_id = (
                  SELECT event_id
                  FROM {}._semantic_outbox
                  WHERE status = 'repair_required'
                    AND payload #>> '{{generated,name}}' = %s
                  ORDER BY generation DESC, created_at DESC
                  LIMIT 1
                  FOR UPDATE
                )
                RETURNING asset_id::text AS "assetId",
                          generation,
                          event_type AS "type"
            """).format(
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
            ), (name,))
            event = cur.fetchone()
            if not event:
                raise DerivedLayerError(
                    f'Derived layer “{name}” has no repair_required semantic event to retry.'
                )
            status = (
                "pending_archive"
                if event["type"] == "archive"
                else "registering"
            )
            cur.execute(sql.SQL("""
                UPDATE {}._definitions
                SET semantic_status = %s,
                    semantic_revision = NULL
                WHERE semantic_asset_id = %s
                  AND semantic_generation = %s
            """).format(sql.Identifier(SCHEMA)), (
                status,
                event["assetId"],
                event["generation"],
            ))
            return {
                "name": name,
                "assetId": event["assetId"],
                "generation": event["generation"],
                "status": status,
                "revision": None,
                "operation": event["type"],
            }

    def queue_semantic_archives(self, actor: str) -> list[dict[str, Any]]:
        actor = str(actor).strip()
        if not actor or len(actor) > 256:
            raise DerivedLayerError(
                "Semantic archive actor must contain 1 to 256 characters."
            )
        queued = []
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA,))
            cur.execute(sql.SQL(
                "SELECT name FROM {}._definitions ORDER BY name"
            ).format(sql.Identifier(SCHEMA)))
            names = [row["name"] for row in cur.fetchall()]
            for name in names:
                definition = self.get_in_transaction(cur, name)
                profile = definition["semanticProfile"]
                if profile["status"] == "archived":
                    queued.append({
                        "name": name,
                        **profile,
                    })
                    continue
                if profile["status"] == "repair_required":
                    queued.append({"name": name, **profile})
                    continue
                if profile["status"] == "pending_archive":
                    queued.append({"name": name, **profile})
                    continue
                cur.execute(sql.SQL("""
                    UPDATE {}._definitions
                    SET semantic_generation = semantic_generation + 1,
                        semantic_status = 'pending_archive',
                        semantic_revision = NULL
                    WHERE name = %s
                """).format(sql.Identifier(SCHEMA)), (name,))
                definition = self.get_in_transaction(cur, name)
                self._enqueue_semantic_event(
                    cur,
                    definition,
                    "archive",
                    actor,
                    self._semantic_fields(cur, name),
                )
                queued.append({
                    "name": name,
                    **definition["semanticProfile"],
                })
        return queued

    @staticmethod
    def _validated_reset_owner(reset_owner: str) -> str:
        candidate = str(reset_owner).strip()
        try:
            parsed = uuid.UUID(candidate)
        except (AttributeError, ValueError):
            raise DerivedLayerError(
                "Semantic reset owner must be a canonical UUID."
            ) from None
        if candidate != str(parsed):
            raise DerivedLayerError(
                "Semantic reset owner must be a canonical UUID."
            )
        return candidate

    def begin_semantic_reset(
        self,
        actor: str,
        reset_owner: str,
    ) -> None:
        actor = str(actor).strip()
        if not actor or len(actor) > 256:
            raise DerivedLayerError(
                "Semantic reset actor must contain 1 to 256 characters."
            )
        reset_owner = self._validated_reset_owner(reset_owner)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA,))
            cur.execute(sql.SQL("""
                SELECT reset_owner
                FROM {}._maintenance
                WHERE operation = 'reset-data'
                FOR UPDATE
            """).format(sql.Identifier(SCHEMA)))
            if cur.fetchone() is not None:
                raise DerivedLayerError(
                    "A derived-layer reset is already in progress."
                )
            cur.execute(sql.SQL("""
                INSERT INTO {}._maintenance(operation, actor, reset_owner)
                VALUES ('reset-data', %s, %s)
            """).format(sql.Identifier(SCHEMA)), (actor, reset_owner))

    def recover_reset_semantic_profiles(
        self,
        actor: str,
        reset_owner: str | None = None,
    ) -> dict[str, Any] | None:
        actor = str(actor).strip()
        if not actor or len(actor) > 256:
            raise DerivedLayerError(
                "Semantic recovery actor must contain 1 to 256 characters."
            )
        expected_owner = (
            self._validated_reset_owner(reset_owner)
            if reset_owner is not None
            else None
        )
        recovered = []
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA,))
            cur.execute(sql.SQL("""
                SELECT reset_owner
                FROM {}._maintenance
                WHERE operation = 'reset-data'
                FOR UPDATE
            """).format(sql.Identifier(SCHEMA)))
            maintenance = cur.fetchone()
            if maintenance is None:
                return None
            actual_owner = str(maintenance["reset_owner"])
            if expected_owner is not None and actual_owner != expected_owner:
                raise DerivedLayerResetOwnershipError(
                    "The semantic reset maintenance gate belongs to another "
                    "reset operation."
                )
            cur.execute(sql.SQL("""
                SELECT definition.name,
                       definition.semantic_asset_id::text
                         AS "predecessorAssetId"
                FROM {}._definitions AS definition
                WHERE definition.semantic_status IN (
                    'pending_archive', 'archived'
                )
                   OR EXISTS (
                     SELECT 1
                     FROM {}._semantic_outbox AS event
                     WHERE event.asset_id = definition.semantic_asset_id
                       AND event.generation = definition.semantic_generation
                       AND event.event_type = 'archive'
                       AND event.status = 'repair_required'
                       AND event.payload ->> 'actor' = 'system:reset-data'
                   )
                ORDER BY definition.name
                FOR UPDATE
            """).format(
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
            ))
            definitions = list(cur.fetchall())
            for reset_definition in definitions:
                name = reset_definition["name"]
                cur.execute(sql.SQL("""
                    UPDATE {}._definitions
                    SET semantic_asset_id = %s,
                        semantic_generation = 1,
                        semantic_status = 'registering',
                        semantic_revision = NULL
                    WHERE name = %s
                    RETURNING name
                """).format(sql.Identifier(SCHEMA)), (
                    str(uuid.uuid4()),
                    name,
                ))
                if cur.fetchone() is None:
                    continue
                definition = self.get_in_transaction(cur, name)
                definition["semanticPredecessorAssetId"] = (
                    reset_definition["predecessorAssetId"]
                )
                self._enqueue_semantic_event(
                    cur,
                    definition,
                    "register",
                    actor,
                    self._semantic_fields(cur, name),
                )
                recovered.append({
                    "name": name,
                    **definition["semanticProfile"],
                })
            cur.execute(sql.SQL("""
                SELECT predecessor.event_id::text AS "eventId",
                       definition.name
                FROM {}._definitions AS definition
                JOIN {}._semantic_outbox AS successor
                  ON successor.asset_id = definition.semantic_asset_id
                 AND successor.generation =
                     definition.semantic_generation
                 AND successor.event_type = 'register'
                 AND successor.payload ->> 'actor' =
                     'system:reset-recovery'
                 AND successor.payload #>> '{{generated,name}}' =
                     definition.name
                 AND successor.status <> 'delivered'
                JOIN {}._semantic_outbox AS predecessor
                  ON predecessor.asset_id::text =
                     successor.payload ->> 'predecessorAssetId'
                 AND predecessor.event_type = 'archive'
                 AND predecessor.payload ->> 'actor' = 'system:reset-data'
                 AND predecessor.payload #>> '{{generated,name}}' =
                     definition.name
                WHERE predecessor.status = 'repair_required'
                ORDER BY definition.name
                FOR UPDATE OF predecessor
            """).format(
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
            ))
            archive_retry_events = list(cur.fetchall())
            for retry_event in archive_retry_events:
                cur.execute(sql.SQL("""
                    UPDATE {}._semantic_outbox
                    SET status = 'pending',
                        attempts = 0,
                        available_at = clock_timestamp(),
                        last_error = NULL,
                        claim_id = NULL,
                        claimed_until = NULL,
                        updated_at = clock_timestamp()
                    WHERE event_id = %s
                      AND status = 'repair_required'
                """).format(sql.Identifier(SCHEMA)), (
                    retry_event["eventId"],
                ))
                if not any(
                    item["name"] == retry_event["name"]
                    for item in recovered
                ):
                    definition = self.get_in_transaction(
                        cur,
                        retry_event["name"],
                    )
                    recovered.append({
                        "name": retry_event["name"],
                        **definition["semanticProfile"],
                    })
            cur.execute(sql.SQL("""
                SELECT event.event_id::text AS "eventId",
                       definition.name
                FROM {}._definitions AS definition
                JOIN {}._semantic_outbox AS event
                  ON event.asset_id = definition.semantic_asset_id
                 AND event.generation = definition.semantic_generation
                WHERE event.event_type = 'register'
                  AND event.status = 'repair_required'
                  AND event.payload ->> 'actor' = 'system:reset-recovery'
                ORDER BY definition.name
                FOR UPDATE OF definition, event
            """).format(
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
            ))
            retry_events = list(cur.fetchall())
            for retry_event in retry_events:
                cur.execute(sql.SQL("""
                    UPDATE {}._semantic_outbox
                    SET status = 'pending',
                        attempts = 0,
                        available_at = clock_timestamp(),
                        last_error = NULL,
                        claim_id = NULL,
                        claimed_until = NULL,
                        updated_at = clock_timestamp()
                    WHERE event_id = %s
                      AND status = 'repair_required'
                """).format(sql.Identifier(SCHEMA)), (
                    retry_event["eventId"],
                ))
                cur.execute(sql.SQL("""
                    UPDATE {}._definitions
                    SET semantic_status = 'registering',
                        semantic_revision = NULL
                    WHERE name = %s
                """).format(sql.Identifier(SCHEMA)), (
                    retry_event["name"],
                ))
                definition = self.get_in_transaction(
                    cur,
                    retry_event["name"],
                )
                if not any(
                    item["name"] == retry_event["name"]
                    for item in recovered
                ):
                    recovered.append({
                        "name": retry_event["name"],
                        **definition["semanticProfile"],
                    })
        return {
            "resetOwner": actual_owner,
            "profiles": recovered,
        }

    def complete_reset_semantic_recovery(
        self,
        reset_owner: str,
    ) -> bool:
        expected_owner = self._validated_reset_owner(reset_owner)
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (SCHEMA,))
            cur.execute(sql.SQL("""
                SELECT reset_owner
                FROM {}._maintenance
                WHERE operation = 'reset-data'
                FOR UPDATE
            """).format(sql.Identifier(SCHEMA)))
            maintenance = cur.fetchone()
            if maintenance is None:
                return False
            actual_owner = str(maintenance["reset_owner"])
            if actual_owner != expected_owner:
                raise DerivedLayerResetOwnershipError(
                    "The semantic reset maintenance gate belongs to another "
                    "reset operation."
                )
            cur.execute(sql.SQL("""
                SELECT definition.name
                FROM {}._definitions AS definition
                WHERE EXISTS (
                    SELECT 1
                    FROM {}._semantic_outbox AS event
                    WHERE event.asset_id = definition.semantic_asset_id
                      AND event.generation = definition.semantic_generation
                      AND event.event_type = 'register'
                      AND event.payload ->> 'actor' =
                          'system:reset-recovery'
                  )
                  AND (
                    definition.semantic_status <> 'ready'
                    OR EXISTS (
                      SELECT 1
                      FROM {}._semantic_outbox AS blocker
                      WHERE blocker.payload #>> '{{generated,name}}' =
                            definition.name
                        AND blocker.status <> 'delivered'
                    )
                  )
                ORDER BY definition.name
                FOR UPDATE
            """).format(
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
            ))
            incomplete = [
                row["name"] for row in cur.fetchall()
            ]
            if incomplete:
                raise DerivedLayerMaintenanceError(
                    "Semantic reset recovery is still waiting for: "
                    + ", ".join(incomplete)
                )
            cur.execute(sql.SQL("""
                DELETE FROM {}._maintenance
                WHERE operation = 'reset-data'
                  AND reset_owner = %s
                RETURNING reset_owner
            """).format(sql.Identifier(SCHEMA)), (actual_owner,))
            return cur.fetchone() is not None

    def reset_recovery_names(self) -> list[str]:
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(sql.SQL("""
                SELECT definition.name
                FROM {}._definitions AS definition
                WHERE EXISTS (
                  SELECT 1
                  FROM {}._semantic_outbox AS event
                  WHERE event.asset_id = definition.semantic_asset_id
                    AND event.payload ->> 'actor' = 'system:reset-recovery'
                )
                ORDER BY definition.name
            """).format(
                sql.Identifier(SCHEMA),
                sql.Identifier(SCHEMA),
            ))
            return [row["name"] for row in cur.fetchall()]

    @staticmethod
    def _h3_not_ready(stage: str) -> dict[str, Any]:
        reason_code, message, suggested_action = H3_READINESS_FAILURES[stage]
        return {
            "method": H3_READINESS_METHOD,
            "ready": False,
            "code": "derived_layer.h3_not_ready",
            "stage": stage,
            "reasons": [{
                "code": reason_code,
                "message": message,
                "suggestedAction": suggested_action,
            }],
        }

    @staticmethod
    def _supported_h3_versions(extensions: dict[str, str]) -> bool:
        release = re.compile(r"^\d+\.\d+(?:\.\d+)*$")
        postgis = extensions["postgis"]
        h3 = extensions["h3"]
        h3_postgis = extensions["h3_postgis"]
        return (
            release.fullmatch(postgis) is not None
            and release.fullmatch(h3) is not None
            and release.fullmatch(h3_postgis) is not None
            and (postgis == "3.5" or postgis.startswith("3.5."))
            and (h3 == "4.2" or h3.startswith("4.2."))
            and h3_postgis == h3
        )

    @classmethod
    def _h3_readiness(
        cls,
        connection,
        cur,
        extensions: dict[str, str],
    ) -> dict[str, Any]:
        if not {"postgis", "h3", "h3_postgis"}.issubset(extensions):
            return cls._h3_not_ready("extension-discovery")
        if not cls._supported_h3_versions(extensions):
            return cls._h3_not_ready("version-validation")
        try:
            with connection.transaction():
                inspected = inspect_h3_polygon_wrapper(
                    cur,
                    experimental=True,
                )
        except psycopg.Error:
            return cls._h3_not_ready("catalog-resolution")
        if inspected is None:
            return cls._h3_not_ready("catalog-resolution")
        wrapper, geometry_schema = inspected
        if (
            wrapper.name != "h3_polygon_to_cells_experimental"
            or not h3_polygon_wrapper_is_approved(wrapper)
        ):
            return cls._h3_not_ready("routine-policy")
        probe = sql.SQL("""
            SELECT pg_catalog.count(*) AS "cellCount"
            FROM {}.h3_polygon_to_cells_experimental(
              {}.ST_GeomFromText(%s, 4326),
              0,
              'overlapping'
            )
        """).format(
            sql.Identifier(wrapper.schema),
            sql.Identifier(geometry_schema),
        )
        parameters = (
            "POLYGON((-0.01 -0.01,0.01 -0.01,0.01 0.01,"
            "-0.01 0.01,-0.01 -0.01))",
        )
        try:
            with connection.transaction():
                cur.execute(
                    sql.SQL("EXPLAIN (FORMAT JSON) ") + probe,
                    parameters,
                )
        except psycopg.Error:
            return cls._h3_not_ready("nested-dependency-resolution")
        try:
            with connection.transaction():
                cur.execute(probe, parameters)
                result = cur.fetchone()
        except psycopg.Error:
            return cls._h3_not_ready("execution-probe")
        cell_count = result.get("cellCount") if isinstance(result, dict) else None
        if (
            not isinstance(cell_count, int)
            or isinstance(cell_count, bool)
            or cell_count < 1
        ):
            return cls._h3_not_ready("result-validation")
        return {
            "method": H3_READINESS_METHOD,
            "ready": True,
        }

    def capabilities(self) -> dict[str, Any]:
        with self._connect() as connection, connection.cursor() as cur:
            cur.execute(
                """
                SELECT extname, extversion
                FROM pg_extension
                WHERE extname IN ('postgis', 'h3', 'h3_postgis')
                ORDER BY extname
                """
            )
            extensions = {row["extname"]: row["extversion"] for row in cur.fetchall()}
            h3_readiness = self._h3_readiness(connection, cur, extensions)
            return {
                "configured": True,
                "schema": SCHEMA,
                "kinds": ["view", "materialized"],
                "spatialScopeTypes": ["workspace-map-extent"],
                "materializationGuard": {
                    "method": MATERIALIZED_PROBE_METHOD,
                    "maxEstimatedBytes": MATERIALIZED_MAX_ESTIMATED_BYTES,
                    "rowOverheadBytes": MATERIALIZED_ROW_OVERHEAD_BYTES,
                    "safetyMultiplier": (
                        MATERIALIZED_ESTIMATE_SAFETY_MULTIPLIER
                    ),
                },
                "queryGuard": {
                    "method": QUERY_PLAN_PROBE_METHOD,
                    "stages": [
                        "postgresql-ast-guard",
                        "postgresql-catalog-guard",
                        "postgresql-explain",
                    ],
                    "limits": self._query_plan_limits(),
                    "shapeLimits": {
                        "maxJoins": QUERY_SHAPE_MAX_JOINS,
                        "maxCtes": QUERY_SHAPE_MAX_CTES,
                        "maxSetOperations": QUERY_SHAPE_MAX_SET_OPERATIONS,
                        "maxGroupingSets": QUERY_SHAPE_MAX_GROUPING_SETS,
                        "maxGeneratedRows": QUERY_SHAPE_MAX_GENERATED_ROWS,
                    },
                    "h3": {
                        "maxEstimatedScopeCells": (
                            H3_SCOPE_MAX_ESTIMATED_CELLS
                        ),
                        "maxEstimatedExpandedCells": (
                            H3_SCOPE_MAX_ESTIMATED_EXPANDED_CELLS
                        ),
                        "scopeEstimateSafetyMultiplier": (
                            H3_SCOPE_ESTIMATE_SAFETY_MULTIPLIER
                        ),
                        "maxGridDistance": H3_MAX_GRID_DISTANCE,
                    },
                    "errorCategories": {
                        "invalid": {
                            "code": "derived_layer.query_invalid",
                            "httpStatus": 400,
                        },
                        "policy": {
                            "code": "derived_layer.query_not_allowed",
                            "httpStatus": 422,
                        },
                        "compute": {
                            "code": "derived_layer.query_too_expensive",
                            "httpStatus": 409,
                        },
                    },
                },
                "queryPlanning": self.query_planning_capability(),
                "recipes": {
                    "areaWeightedH3": area_weighted_h3_recipe_capability(
                        available=h3_readiness["ready"],
                    ),
                },
                "extensions": extensions,
                "h3Available": h3_readiness["ready"],
                "h3Readiness": h3_readiness,
            }
