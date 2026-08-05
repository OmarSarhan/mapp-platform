from __future__ import annotations

from dataclasses import dataclass
from math import prod
from typing import Any, Iterable

from pglast import Error as PgLastError
from pglast import parse_sql


APPROVED_EXTENSIONS = frozenset({"postgis", "h3", "h3_postgis"})
CONTROLLED_EXTENSION_SCHEMA = "public"
MAX_GENERATED_ROWS = 100_000
RESERVED_NAME_PREFIX = "_mapp_"
SERVER_H3_SCOPE = "_mapp_h3_scope"
SERVER_H3_GEOMETRY = "geom_4326"
FORBIDDEN_RELATION_SCHEMAS = frozenset({
    "derived_layers",
    "information_schema",
})

HAZARDOUS_FUNCTIONS = frozenset({
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_advisory_unlock",
    "pg_advisory_unlock_all",
    "pg_advisory_unlock_shared",
    "pg_advisory_xact_lock",
    "pg_advisory_xact_lock_shared",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_try_advisory_lock",
    "pg_try_advisory_lock_shared",
    "pg_try_advisory_xact_lock",
    "pg_try_advisory_xact_lock_shared",
    "set_config",
})

DANGEROUS_CATALOG_FUNCTIONS = frozenset({
    "current_setting",
    "currval",
    "cursor_to_xml",
    "cursor_to_xmlschema",
    "database_to_xml",
    "database_to_xml_and_xmlschema",
    "database_to_xmlschema",
    "lo_close",
    "lo_create",
    "lo_export",
    "lo_from_bytea",
    "lo_get",
    "lo_import",
    "lo_lseek",
    "lo_lseek64",
    "lo_open",
    "lo_put",
    "lo_tell",
    "lo_tell64",
    "lo_truncate",
    "lo_truncate64",
    "lo_unlink",
    "lowrite",
    "lastval",
    "nextval",
    "pg_backup_start",
    "pg_backup_stop",
    "pg_cancel_backend",
    "pg_create_logical_replication_slot",
    "pg_create_physical_replication_slot",
    "pg_create_restore_point",
    "pg_drop_replication_slot",
    "pg_log_standby_snapshot",
    "pg_logical_emit_message",
    "pg_ls_archive_statusdir",
    "pg_ls_dir",
    "pg_ls_logdir",
    "pg_ls_logicalsnapdir",
    "pg_ls_replslotdir",
    "pg_ls_tmpdir",
    "pg_ls_waldir",
    "pg_promote",
    "pg_read_binary_file",
    "pg_read_file",
    "pg_reload_conf",
    "pg_replication_origin_advance",
    "pg_replication_origin_create",
    "pg_replication_origin_drop",
    "pg_replication_origin_progress",
    "pg_replication_origin_session_reset",
    "pg_replication_origin_session_setup",
    "pg_replication_origin_xact_reset",
    "pg_replication_origin_xact_setup",
    "pg_rotate_logfile",
    "pg_stat_file",
    "pg_switch_wal",
    "pg_terminate_backend",
    "pg_wal_replay_pause",
    "pg_wal_replay_resume",
    "query_to_xml",
    "query_to_xml_and_xmlschema",
    "schema_to_xml",
    "schema_to_xml_and_xmlschema",
    "schema_to_xmlschema",
    "setval",
    "table_to_xml",
    "table_to_xml_and_xmlschema",
    "table_to_xmlschema",
    "ts_stat",
})

APPROVED_ROUTINE_LANGUAGES = frozenset({"internal", "c", "sql"})
APPROVED_BUILTIN_AGGREGATES = frozenset({
    "avg", "bit_and", "bit_or", "bit_xor", "bool_and", "bool_or",
    "corr", "count", "covar_pop", "covar_samp", "every", "max", "min",
    "regr_avgx", "regr_avgy", "regr_count", "regr_intercept", "regr_r2",
    "regr_slope", "regr_sxx", "regr_sxy", "regr_syy", "stddev",
    "stddev_pop", "stddev_samp", "sum", "var_pop", "var_samp", "variance",
})
APPROVED_EXTENSION_AGGREGATES = frozenset({"st_3dextent", "st_extent"})
APPROVED_EXTENSION_TYPE_OWNERS = {
    "box2d": frozenset({"postgis"}),
    "box3d": frozenset({"postgis"}),
    "geography": frozenset({"postgis"}),
    "geometry": frozenset({"postgis"}),
    "h3index": frozenset({"h3"}),
    "spheroid": frozenset({"postgis"}),
}
APPROVED_EXTENSION_TYPES = frozenset(APPROVED_EXTENSION_TYPE_OWNERS)
POSTGIS_GEOMETRY_TYPE_BASES = frozenset({
    "circularstring",
    "compoundcurve",
    "curvepolygon",
    "geometry",
    "geometrycollection",
    "linestring",
    "multicurve",
    "multilinestring",
    "multipoint",
    "multipolygon",
    "multisurface",
    "point",
    "polygon",
    "polyhedralsurface",
    "tin",
    "triangle",
})
APPROVED_POSTGIS_GEOMETRY_TYPMODS = frozenset(
    f"{geometry_type}{dimensions}"
    for geometry_type in POSTGIS_GEOMETRY_TYPE_BASES
    for dimensions in ("", "z", "m", "zm")
)
APPROVED_UNQUALIFIED_BUILTIN_TYPES = frozenset({
    "bit",
    "bool",
    "bpchar",
    "bytea",
    "char",
    "cidr",
    "date",
    "float4",
    "float8",
    "inet",
    "int2",
    "int4",
    "int8",
    "interval",
    "json",
    "jsonb",
    "macaddr",
    "macaddr8",
    "money",
    "name",
    "numeric",
    "oid",
    "pg_lsn",
    "text",
    "time",
    "timestamp",
    "timestamptz",
    "timetz",
    "tsquery",
    "tsvector",
    "uuid",
    "varbit",
    "varchar",
    "xml",
})

H3_POLYGON_FUNCTIONS = frozenset({
    "h3_polygon_to_cells",
    "h3_polygon_to_cells_experimental",
})
H3_GRID_FUNCTIONS = frozenset({
    "h3_grid_disk",
    "h3_grid_disk_distances",
    "h3_grid_ring",
    "h3_grid_ring_unsafe",
    "h3_k_ring",
    "h3_k_ring_distances",
    "h3_hex_ring",
})
H3_RING_FUNCTIONS = frozenset({
    "h3_grid_ring",
    "h3_grid_ring_unsafe",
    "h3_hex_ring",
})
H3_UNBOUNDED_FUNCTIONS = frozenset({
    "h3_cell_to_children_slow",
    "h3_grid_path_cells",
    "h3_grid_path_cells_recursive",
    "h3_line",
    "h3_to_children_slow",
    "h3_uncompact",
    "h3_uncompact_cells",
})

# These functions can multiply rows or geometry components based on source
# values. Their output cannot be proven bounded from the submitted syntax.
UNBOUNDED_SET_FUNCTIONS = frozenset({
    "aclexplode",
    "generate_subscripts",
    "json_array_elements",
    "json_array_elements_text",
    "json_each",
    "json_each_text",
    "json_object_keys",
    "json_populate_recordset",
    "json_to_recordset",
    "jsonb_array_elements",
    "jsonb_array_elements_text",
    "jsonb_each",
    "jsonb_each_text",
    "jsonb_object_keys",
    "jsonb_populate_recordset",
    "jsonb_to_recordset",
    "regexp_matches",
    "regexp_split_to_table",
    "st_dump",
    "st_dumpaspolygons",
    "st_dumppoints",
    "st_dumprings",
    "st_dumpsegments",
    "st_hexagongrid",
    "st_pixelaspolygons",
    "st_squaregrid",
    "st_subdivide",
    "unnest",
})

# These aggregate transition states grow with every input row. PostgreSQL's
# final-row estimate does not cap their in-memory state.
UNBOUNDED_AGGREGATES = frozenset({
    "array_agg",
    "json_agg",
    "json_agg_strict",
    "json_object_agg",
    "json_object_agg_strict",
    "json_object_agg_unique",
    "json_object_agg_unique_strict",
    "jsonb_agg",
    "jsonb_agg_strict",
    "jsonb_object_agg",
    "jsonb_object_agg_strict",
    "jsonb_object_agg_unique",
    "jsonb_object_agg_unique_strict",
    "range_agg",
    "range_intersect_agg",
    "st_clusterintersecting",
    "st_clusterwithin",
    "st_asgeobuf",
    "st_asmvt",
    "st_collect",
    "st_coverageunion",
    "st_makeline",
    "st_memcollect",
    "st_memunion",
    "st_polygonize",
    "st_union",
    "string_agg",
    "xmlagg",
})
AST_UNBOUNDED_AGGREGATES = frozenset(
    name for name in UNBOUNDED_AGGREGATES if not name.startswith("st_")
)

UNBOUNDED_GEOMETRY_FUNCTIONS = frozenset({
    "st_clusterdbscan",
    "st_clusterkmeans",
    "st_delaunaytriangles",
    "st_segmentize",
    "st_triangulatepolygon",
    "st_voronoipolygons",
})


@dataclass(frozen=True)
class GuardReason:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class QueryGuardViolation(ValueError):
    def __init__(self, reasons: Iterable[GuardReason]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(reason.message for reason in self.reasons))


@dataclass(frozen=True)
class FunctionCall:
    qualified_name: tuple[str, ...]
    arguments: tuple[Any, ...]
    scope_bound: bool
    bounded_set: bool = False
    bound_kind: str | None = None
    generated_rows: int | None = None

    @property
    def name(self) -> str:
        return self.qualified_name[-1].lower()

    @property
    def literal_integers(self) -> tuple[int | None, ...]:
        return tuple(_integer_literal(argument) for argument in self.arguments)


@dataclass(frozen=True, order=True)
class QualifiedCastType:
    schema: str
    name: str


@dataclass(frozen=True)
class QueryAstInspection:
    function_calls: tuple[FunctionCall, ...]
    qualified_cast_types: tuple[QualifiedCastType, ...]
    join_count: int
    cte_count: int
    set_operation_count: int
    grouping_set_count: int

    @property
    def function_names(self) -> tuple[str, ...]:
        return tuple(sorted({call.name for call in self.function_calls}))

    @property
    def bounded_set_functions(self) -> frozenset[str]:
        return frozenset(
            call.name for call in self.function_calls if call.bounded_set
        )

    def calls_named(self, names: Iterable[str]) -> tuple[FunctionCall, ...]:
        wanted = frozenset(name.lower() for name in names)
        return tuple(call for call in self.function_calls if call.name in wanted)


@dataclass(frozen=True)
class RoutineDependency:
    kind: str
    object_oid: int
    identity: str
    schema: str
    name: str
    extension: str | None
    extension_schema: str | None
    implementation_schema: str
    implementation_extension: str | None
    implementation_extension_schema: str | None
    volatility: str
    returns_set: bool
    routine_kind: str
    security_definer: bool
    routine_config: tuple[str, ...] | None
    language: str
    object_builtin: bool
    implementation_builtin: bool
    approved_extension_search_path: str


@dataclass(frozen=True)
class TypeOrCastDependency:
    kind: str
    object_oid: int
    identity: str
    schema: str
    name: str
    extension: str | None
    object_builtin: bool
    implementation_schema: str | None
    implementation_extension: str | None
    implementation_builtin: bool
    volatility: str | None
    security_definer: bool | None
    routine_config: tuple[str, ...] | None
    language: str | None


def _reason(code: str, message: str) -> GuardReason:
    return GuardReason(code, message)


def _children(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield child
    elif isinstance(value, (list, tuple)):
        yield from value


def _walk(value: Any):
    if isinstance(value, dict):
        if "@" in value:
            yield value
        for child in _children(value):
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)


def _enum_name(value: Any) -> str | None:
    if isinstance(value, dict) and "#" in value:
        return value.get("name")
    return None


def _string_node(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("@") == "String":
        text = value.get("sval")
        return text if isinstance(text, str) else None
    return None


def _function_name(node: dict[str, Any]) -> tuple[str, ...]:
    parts = tuple(
        part
        for part in (_string_node(value) for value in node.get("funcname", ()))
        if part is not None
    )
    return parts


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _canonical(child)
            for key, child in value.items()
            if key not in {
                "location", "stmt_location", "stmt_len",
                "list_start", "list_end", "rexpr_list_start",
                "rexpr_list_end",
            }
        }
    if isinstance(value, (list, tuple)):
        return tuple(_canonical(child) for child in value)
    return value


def _integer_literal(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    if value.get("@") == "A_Const":
        literal = value.get("val")
        if isinstance(literal, dict) and literal.get("@") == "Integer":
            integer = literal.get("ival")
            return integer if isinstance(integer, int) else None
        return None
    if value.get("@") != "TypeCast":
        return None
    type_name = value.get("typeName", {})
    names = tuple(
        item
        for item in (
            _string_node(part) for part in type_name.get("names", ())
        )
        if item is not None
    )
    if not names or names[-1].lower() not in {
        "int2", "int4", "int8", "smallint", "integer", "bigint",
    }:
        return None
    return _integer_literal(value.get("arg"))


def _postgis_typmods_are_allowed(type_name: dict[str, Any]) -> bool:
    typmods = tuple(type_name.get("typmods", ()))
    if not typmods:
        return True
    if len(typmods) != 2:
        return False
    geometry_type = typmods[0]
    if (
        not isinstance(geometry_type, dict)
        or geometry_type.get("@") != "ColumnRef"
    ):
        return False
    fields = tuple(geometry_type.get("fields", ()))
    subtype = _string_node(fields[0]) if len(fields) == 1 else None
    srid = _integer_literal(typmods[1])
    return (
        isinstance(subtype, str)
        and subtype.lower() in APPROVED_POSTGIS_GEOMETRY_TYPMODS
        and srid is not None
        and srid > 0
    )


def _boolean_literal(value: Any) -> bool | None:
    if not isinstance(value, dict) or value.get("@") != "A_Const":
        return None
    literal = value.get("val")
    if isinstance(literal, dict) and literal.get("@") == "Boolean":
        boolean = literal.get("boolval")
        return boolean if isinstance(boolean, bool) else None
    return None


def _is_scope_column(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("@") != "ColumnRef":
        return False
    fields = tuple(_string_node(item) for item in value.get("fields", ()))
    return fields == (SERVER_H3_SCOPE, SERVER_H3_GEOMETRY)


def _is_immediate_child(arguments: tuple[Any, ...]) -> bool:
    if len(arguments) == 1:
        return True
    if len(arguments) != 2:
        return False
    resolution = arguments[1]
    if not isinstance(resolution, dict) or resolution.get("@") != "A_Expr":
        return False
    operators = tuple(
        _string_node(item) for item in resolution.get("name", ())
    )
    if operators != ("+",) or _integer_literal(resolution.get("rexpr")) != 1:
        return False
    get_resolution = resolution.get("lexpr")
    if (
        not isinstance(get_resolution, dict)
        or get_resolution.get("@") != "FuncCall"
    ):
        return False
    resolution_name = _function_name(get_resolution)
    resolution_arguments = tuple(get_resolution.get("args", ()))
    return (
        bool(resolution_name)
        and resolution_name[-1].lower() == "h3_get_resolution"
        and len(resolution_arguments) == 1
        and _canonical(resolution_arguments[0]) == _canonical(arguments[0])
    )


def _series_rows(arguments: tuple[Any, ...]) -> int | None:
    if len(arguments) not in {2, 3}:
        return None
    values = tuple(_integer_literal(argument) for argument in arguments)
    if any(value is None for value in values):
        return None
    start, stop = int(values[0]), int(values[1])
    step = int(values[2]) if len(values) == 3 else 1
    if step == 0:
        return None
    if (step > 0 and start > stop) or (step < 0 and start < stop):
        return 0
    return abs(stop - start) // abs(step) + 1


def _range_contains_scope(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("@") == "RangeVar":
        return (
            value.get("relname") == SERVER_H3_SCOPE
            and not value.get("schemaname")
            and not value.get("catalogname")
            and value.get("alias") is None
        )
    if value.get("@") == "JoinExpr":
        return _range_contains_scope(value.get("larg")) or _range_contains_scope(
            value.get("rarg")
        )
    return False


def _range_aliases(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    alias = value.get("alias")
    if isinstance(alias, dict) and isinstance(alias.get("aliasname"), str):
        return {alias["aliasname"]}
    if value.get("@") == "RangeVar" and isinstance(value.get("relname"), str):
        return {value["relname"]}
    if value.get("@") == "JoinExpr":
        return _range_aliases(value.get("larg")) | _range_aliases(value.get("rarg"))
    return set()


def _column_qualifiers(value: Any) -> set[str]:
    qualifiers = set()
    for node in _walk(value):
        if node.get("@") != "ColumnRef":
            continue
        fields = tuple(node.get("fields", ()))
        qualifier = _string_node(fields[-2]) if len(fields) >= 2 else None
        if qualifier is not None:
            qualifiers.add(qualifier)
    return qualifiers


def _contains_boolean_or(value: Any) -> bool:
    return any(
        node.get("@") == "BoolExpr"
        and _enum_name(node.get("boolop")) == "OR_EXPR"
        for node in _walk(value)
    )


def _grouping_set_size(node: dict[str, Any]) -> int:
    kind = _enum_name(node.get("kind"))
    content = tuple(node.get("content", ()))
    if kind == "GROUPING_SET_EMPTY":
        return 1
    if kind == "GROUPING_SET_ROLLUP":
        return len(content) + 1
    if kind == "GROUPING_SET_CUBE":
        return 2 ** len(content)
    if kind == "GROUPING_SET_SETS":
        return sum(
            _grouping_set_size(item)
            if isinstance(item, dict) and item.get("@") == "GroupingSet"
            else 1
            for item in content
        )
    return 1


def _call_with_bounds(
    node: dict[str, Any],
    scope_bound: bool,
    reasons: list[GuardReason],
) -> FunctionCall:
    qualified_name = _function_name(node)
    arguments = tuple(node.get("args", ()))
    name = qualified_name[-1].lower() if qualified_name else ""
    bounded_set = False
    bound_kind = None
    generated_rows = None

    if name in HAZARDOUS_FUNCTIONS:
        reasons.append(_reason(
            "hazardous_function",
            f"Transaction-stalling or session-changing function {name} is "
            "not allowed in derived layers.",
        ))
    if name in DANGEROUS_CATALOG_FUNCTIONS:
        reasons.append(_reason(
            "dangerous_catalog_function",
            f"Catalog, file, server-control or dynamic-query function {name} "
            "is not allowed in a derived layer.",
        ))
    if name in H3_UNBOUNDED_FUNCTIONS:
        reasons.append(_reason(
            "h3_unbounded_expansion",
            f"Unbounded H3 expansion function {name} is not allowed.",
        ))
    if name in UNBOUNDED_SET_FUNCTIONS:
        reasons.append(_reason(
            "unbounded_set_function",
            f"Set-returning expansion function {name} cannot be proven "
            "bounded from the submitted query.",
        ))
    if name in UNBOUNDED_GEOMETRY_FUNCTIONS:
        reasons.append(_reason(
            "unbounded_geometry_expansion",
            f"Geometry expansion function {name} cannot be bounded safely.",
        ))
    if name in AST_UNBOUNDED_AGGREGATES:
        reasons.append(_reason(
            "unbounded_aggregate_state",
            f"Aggregate {name} can build an unbounded in-memory value; use "
            "bounded numeric aggregation such as count, sum, avg, min or max.",
        ))

    if name == "generate_series":
        generated_rows = _series_rows(arguments)
        if generated_rows is None or generated_rows > MAX_GENERATED_ROWS:
            reasons.append(_reason(
                "unbounded_row_generator",
                "generate_series must use literal integer bounds and emit no "
                f"more than {MAX_GENERATED_ROWS:,} rows.",
            ))
        else:
            bounded_set = True
            bound_kind = "literal-series"
    elif name in H3_POLYGON_FUNCTIONS:
        resolution = (
            _integer_literal(arguments[1]) if len(arguments) == 2 else None
        )
        if len(arguments) != 2 or not _is_scope_column(arguments[0]):
            reasons.append(_reason(
                "h3_unscoped_polygon_expansion",
                "H3 polygon expansion must read the direct server column "
                "_mapp_h3_scope.geom_4326.",
            ))
        elif not scope_bound:
            reasons.append(_reason(
                "h3_scope_binding",
                "H3 polygon expansion must bind _mapp_h3_scope directly in "
                "the query scope; a similarly named column is not sufficient.",
            ))
        elif resolution is None or not 0 <= resolution <= 15:
            reasons.append(_reason(
                "h3_dynamic_resolution",
                "H3 polygon expansion resolution must be a literal integer "
                "between 0 and 15.",
            ))
        else:
            bounded_set = True
            bound_kind = "server-scope-polygon"
    elif name in H3_GRID_FUNCTIONS:
        distance = (
            1 if len(arguments) == 1
            else _integer_literal(arguments[1]) if len(arguments) == 2
            else None
        )
        if distance is None or not 0 <= distance <= 25:
            reasons.append(_reason(
                "h3_dynamic_grid_distance",
                "H3 grid expansion needs a literal distance between 0 and 25.",
            ))
        else:
            bounded_set = True
            bound_kind = "literal-grid-distance"
    elif name == "h3_cell_to_children":
        if not _is_immediate_child(arguments):
            reasons.append(_reason(
                "h3_unbounded_child_expansion",
                "H3 child expansion is limited to immediate children.",
            ))
        else:
            bounded_set = True
            bound_kind = "immediate-child"

    if name in {"repeat", "lpad", "rpad", "space", "array_fill", "format"}:
        reasons.append(_reason(
            "unbounded_scalar_output",
            f"Scalar constructor {name} can allocate a large value per source "
            "row and is not allowed in a derived layer.",
        ))
    elif name == "st_generatepoints":
        reasons.append(_reason(
            "unbounded_geometry_expansion",
            "ST_GeneratePoints can allocate a large geometry per source row "
            "and is not allowed in a derived layer.",
        ))
    elif name == "st_buffer" and len(arguments) >= 3:
        reasons.append(_reason(
            "unbounded_geometry_expansion",
            "Configured ST_Buffer segment expansion is not allowed; use the "
            "bounded default two-argument form.",
        ))

    return FunctionCall(
        qualified_name=qualified_name,
        arguments=arguments,
        scope_bound=scope_bound,
        bounded_set=bounded_set,
        bound_kind=bound_kind,
        generated_rows=generated_rows,
    )


def inspect_query_ast(query: str) -> QueryAstInspection:
    try:
        statements = parse_sql(query)
    except (PgLastError, ValueError) as exc:
        raise QueryGuardViolation((
            _reason("invalid_sql", f"PostgreSQL could not parse the query: {exc}"),
        )) from exc
    if len(statements) != 1:
        raise QueryGuardViolation((
            _reason("multiple_statements", "Derived-layer SQL must be exactly one SELECT."),
        ))
    raw = statements[0](skip_none=True)
    statement = raw.get("stmt", {})
    if statement.get("@") != "SelectStmt":
        raise QueryGuardViolation((
            _reason("not_select", "Derived-layer SQL must be exactly one SELECT."),
        ))

    reasons: list[GuardReason] = []
    calls: list[FunctionCall] = []
    qualified_cast_types: set[QualifiedCastType] = set()
    join_count = 0
    cte_count = 0
    set_operation_count = 0
    grouping_set_count = 0
    cte_names = {
        node.get("ctename")
        for node in _walk(statement)
        if node.get("@") == "CommonTableExpr"
        and isinstance(node.get("ctename"), str)
    }

    for node in _walk(statement):
        kind = node.get("@")
        alias = node.get("alias")
        alias_name = (
            alias.get("aliasname")
            if isinstance(alias, dict) and alias.get("@") == "Alias"
            else None
        )
        if isinstance(alias_name, str) and alias_name.lower().startswith(
            RESERVED_NAME_PREFIX
        ):
            reasons.append(_reason(
                (
                    "h3_scope_shadowed"
                    if alias_name.lower() == SERVER_H3_SCOPE
                    else "reserved_alias"
                ),
                f"Alias {alias_name} is reserved for server query guards.",
            ))
        if kind == "CommonTableExpr":
            cte_count += 1
            cte_name = node.get("ctename")
            if isinstance(cte_name, str) and cte_name.lower().startswith(
                RESERVED_NAME_PREFIX
            ):
                reasons.append(_reason(
                    (
                        "h3_scope_shadowed"
                        if cte_name.lower() == SERVER_H3_SCOPE
                        else "reserved_cte"
                    ),
                    f"CTE {cte_name} is reserved for server query guards.",
                ))
            if node.get("ctequery", {}).get("@") != "SelectStmt":
                reasons.append(_reason(
                    "modifying_cte",
                    "Data-modifying CTEs are not allowed in derived layers.",
                ))
        elif kind == "WithClause" and node.get("recursive"):
            reasons.append(_reason(
                "recursive_cte",
                "Recursive CTEs are not allowed in derived layers.",
            ))
        elif kind == "SelectStmt":
            if node.get("intoClause") is not None:
                reasons.append(_reason(
                    "select_into",
                    "SELECT INTO is not allowed in derived layers.",
                ))
            if node.get("lockingClause"):
                reasons.append(_reason(
                    "row_locking",
                    "Row-locking SELECT clauses are not allowed in derived layers.",
                ))
            from_clause = tuple(node.get("fromClause", ()))
            if len(from_clause) > 1:
                reasons.append(_reason(
                    "cartesian_join",
                    "Comma-separated FROM items are not allowed; use an "
                    "explicit bounded join condition.",
                ))
            if _enum_name(node.get("op")) not in {None, "SETOP_NONE"}:
                set_operation_count += 1
            grouping_factors = [
                _grouping_set_size(item)
                for item in node.get("groupClause", ())
                if isinstance(item, dict) and item.get("@") == "GroupingSet"
            ]
            if grouping_factors:
                grouping_set_count += prod(grouping_factors)
        elif kind == "JoinExpr":
            join_count += 1
            if node.get("isNatural"):
                reasons.append(_reason(
                    "natural_join",
                    "NATURAL JOIN is not allowed; name a bounded join condition.",
                ))
            if node.get("usingClause"):
                continue
            qualifiers = _column_qualifiers(node.get("quals"))
            left_aliases = _range_aliases(node.get("larg"))
            right_aliases = _range_aliases(node.get("rarg"))
            predicate_links_sides = bool(
                qualifiers & left_aliases and qualifiers & right_aliases
            )
            if (
                node.get("quals") is None
                or _boolean_literal(node.get("quals")) is True
                or _contains_boolean_or(node.get("quals"))
                or not predicate_links_sides
            ):
                rarg = node.get("rarg", {})
                safe_lateral = (
                    isinstance(rarg, dict)
                    and rarg.get("@") == "RangeFunction"
                    and rarg.get("lateral") is True
                )
                if not safe_lateral:
                    reasons.append(_reason(
                        "cartesian_join",
                        "Cartesian, OR-connected, and JOIN ... ON TRUE "
                        "predicates are not allowed; split alternatives into "
                        "separately bounded queries.",
                    ))
        elif kind in {"RangeTableFunc", "JsonTable"}:
            reasons.append(_reason(
                "unbounded_set_function",
                f"{kind} row expansion cannot be proven bounded.",
            ))
        elif kind in {"JsonArrayAgg", "JsonObjectAgg"}:
            reasons.append(_reason(
                "unbounded_aggregate_state",
                "SQL/JSON aggregate output grows without a bounded transition state.",
            ))
        elif kind == "TypeCast":
            type_name = node.get("typeName", {})
            names = tuple(
                part
                for part in (
                    _string_node(item) for item in type_name.get("names", ())
                )
                if part is not None
            )
            qualified_extension_type = (
                len(names) == 2
                and names[1].lower() in APPROVED_EXTENSION_TYPES
            )
            trusted_type = (
                len(names) == 2 and names[0] == "pg_catalog"
            ) or qualified_extension_type or (
                len(names) == 1
                and names[0].lower() in (
                    APPROVED_UNQUALIFIED_BUILTIN_TYPES
                    | APPROVED_EXTENSION_TYPES
                )
            )
            if qualified_extension_type:
                if (
                    names[1].lower() in {"geography", "geometry"}
                    and not _postgis_typmods_are_allowed(type_name)
                ):
                    reasons.append(_reason(
                        "unapproved_cast_typmod",
                        "Schema-qualified PostGIS geometry casts with type "
                        "modifiers require an allowed literal geometry subtype "
                        "and a positive literal SRID.",
                    ))
                else:
                    qualified_cast_types.add(QualifiedCastType(
                        schema=names[0],
                        name=names[1],
                    ))
            if not trusted_type:
                reasons.append(_reason(
                    "unapproved_cast_type",
                    "Explicit casts may target only pg_catalog types or "
                    "approved PostGIS/H3 type names; schema-qualified extension "
                    "types must pass catalog membership validation before "
                    "planning.",
                ))
        elif kind == "A_Expr":
            operator_name = tuple(
                part
                for part in (
                    _string_node(item) for item in node.get("name", ())
                )
                if part is not None
            )
            if len(operator_name) > 1 and operator_name[0] != "pg_catalog":
                reasons.append(_reason(
                    "unapproved_operator_schema",
                    "Explicit schema-qualified operators may use only "
                    "pg_catalog; extension operators must resolve through the "
                    "fixed trusted search_path and pass the catalog OID guard.",
                ))
        elif kind == "RangeVar":
            relation_name = node.get("relname")
            relation_schema = node.get("schemaname")
            if (
                isinstance(relation_schema, str)
                and (
                    relation_schema.lower() in FORBIDDEN_RELATION_SCHEMAS
                    or relation_schema.lower().startswith("pg_")
                )
            ):
                reasons.append(_reason(
                    "unapproved_relation_schema",
                    f"System or managed schema {relation_schema} cannot be "
                    "read by a derived-layer query.",
                ))
            if (
                isinstance(relation_name, str)
                and not node.get("schemaname")
                and not node.get("catalogname")
                and relation_name not in cte_names
                and relation_name != SERVER_H3_SCOPE
            ):
                reasons.append(_reason(
                    "unqualified_relation",
                    f"Base relation {relation_name} must be schema-qualified.",
                ))
            if (
                isinstance(relation_name, str)
                and relation_name.lower().startswith(RESERVED_NAME_PREFIX)
                and not (
                    relation_name == SERVER_H3_SCOPE
                    and not node.get("schemaname")
                    and not node.get("catalogname")
                    and node.get("alias") is None
                )
            ):
                reasons.append(_reason(
                    "reserved_relation",
                    f"Relation name {relation_name} is reserved for server query guards.",
                ))

    def collect_scoped(value: Any, inherited_scope: bool) -> None:
        if not isinstance(value, (dict, list, tuple)):
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                collect_scoped(item, inherited_scope)
            return
        kind = value.get("@")
        if kind == "SelectStmt":
            local_scope = inherited_scope or any(
                _range_contains_scope(item)
                for item in value.get("fromClause", ())
            )
            for key, child in value.items():
                if key in {"@", "withClause"}:
                    continue
                collect_scoped(child, local_scope)
            with_clause = value.get("withClause")
            if isinstance(with_clause, dict):
                for cte in with_clause.get("ctes", ()):
                    collect_scoped(cte.get("ctequery"), False)
            return
        if kind == "FuncCall":
            calls.append(_call_with_bounds(value, inherited_scope, reasons))
        for child in _children(value):
            collect_scoped(child, inherited_scope)

    collect_scoped(statement, False)
    if reasons:
        raise QueryGuardViolation(reasons)
    return QueryAstInspection(
        function_calls=tuple(calls),
        qualified_cast_types=tuple(sorted(qualified_cast_types)),
        join_count=join_count,
        cte_count=cte_count,
        set_operation_count=set_operation_count,
        grouping_set_count=grouping_set_count,
    )


def validate_query_ast(query: str) -> QueryAstInspection:
    return inspect_query_ast(query)


def validate_qualified_cast_types(
    cur,
    inspection: QueryAstInspection,
) -> list[dict[str, Any]]:
    references = inspection.qualified_cast_types
    if not references:
        return []
    cur.execute(
        """
        WITH requested(schema, name) AS (
          SELECT requested_schema.schema, requested_name.name
          FROM pg_catalog.unnest(%s::pg_catalog.text[])
            WITH ORDINALITY AS requested_schema(schema, ordinal)
          JOIN pg_catalog.unnest(%s::pg_catalog.text[])
            WITH ORDINALITY AS requested_name(name, ordinal)
            USING (ordinal)
        )
        SELECT
          requested.schema,
          requested.name,
          target_type.oid AS object_oid,
          target_type.typisdefined AS type_defined,
          target_type.typtype AS type_kind,
          extension.extname AS extension,
          extension_namespace.nspname AS extension_schema
        FROM requested
        LEFT JOIN pg_catalog.pg_namespace AS type_namespace
          ON type_namespace.nspname = requested.schema
        LEFT JOIN pg_catalog.pg_type AS target_type
          ON target_type.typnamespace = type_namespace.oid
         AND target_type.typname = requested.name
        LEFT JOIN pg_catalog.pg_depend AS extension_membership
          ON extension_membership.classid =
               'pg_catalog.pg_type'::pg_catalog.regclass
         AND extension_membership.objid = target_type.oid
         AND extension_membership.refclassid =
               'pg_catalog.pg_extension'::pg_catalog.regclass
         AND extension_membership.deptype = 'e'
        LEFT JOIN pg_catalog.pg_extension AS extension
          ON extension.oid = extension_membership.refobjid
        LEFT JOIN pg_catalog.pg_namespace AS extension_namespace
          ON extension_namespace.oid = extension.extnamespace
        ORDER BY requested.schema, requested.name
        """,
        (
            [reference.schema for reference in references],
            [reference.name for reference in references],
        ),
    )
    rows = [dict(item) for item in cur.fetchall()]
    resolved = {
        (row.get("schema"), row.get("name")): row
        for row in rows
    }
    reasons: list[GuardReason] = []
    for reference in references:
        row = resolved.get((reference.schema, reference.name), {})
        allowed_extensions = APPROVED_EXTENSION_TYPE_OWNERS.get(
            reference.name.lower(),
            frozenset(),
        )
        if not (
            row.get("object_oid") is not None
            and row.get("type_defined") is True
            and row.get("type_kind") == "b"
            and row.get("extension") in allowed_extensions
            and reference.schema == CONTROLLED_EXTENSION_SCHEMA
            and row.get("extension_schema") == reference.schema
        ):
            reasons.append(_reason(
                "unapproved_cast_type",
                f"Qualified cast type {reference.schema}.{reference.name} "
                "must be a defined base type owned by its approved PostGIS/H3 "
                f"extension in the controlled {CONTROLLED_EXTENSION_SCHEMA} "
                "schema, which must be that extension's authoritative schema.",
            ))
    if reasons:
        raise QueryGuardViolation(reasons)
    return rows


def inspect_relation_routines(
    cur,
    schema: str,
    relation: str,
    function_names: Iterable[str] = (),
) -> list[RoutineDependency]:
    """Return resolved routine/operator dependencies for a stored query.

    pg_depend omits references to pinned pg_catalog objects, so volatile and
    set-returning pg_catalog candidates matching submitted function names are
    included conservatively as builtin_candidate rows.
    """
    names = sorted({name.lower() for name in function_names})
    cur.execute(
        """
        WITH approved_extension_namespaces AS (
          SELECT DISTINCT namespace.nspname
          FROM pg_extension AS extension
          JOIN pg_namespace AS namespace
            ON namespace.oid = extension.extnamespace
          WHERE extension.extname = ANY(%s::text[])
        ), approved_extension_path AS (
          SELECT
            'search_path=pg_catalog'
              || COALESCE(
                   ', ' || string_agg(
                     quote_ident(nspname), ', ' ORDER BY nspname
                   ),
                   ''
                 ) AS value
          FROM approved_extension_namespaces
        ), relation_rewrites AS (
          SELECT rewrite.oid
          FROM pg_rewrite AS rewrite
          WHERE rewrite.ev_class = %s::regclass
        ), referenced AS (
          SELECT dependency.refclassid, dependency.refobjid
          FROM relation_rewrites AS rewrite
          JOIN pg_depend AS dependency
            ON dependency.classid = 'pg_rewrite'::regclass
           AND dependency.objid = rewrite.oid
           AND dependency.deptype = 'n'
        ), function_objects AS (
          SELECT
            'function'::text AS kind,
            function.oid AS object_oid,
            function.oid::regprocedure::text AS identity,
            function_ns.nspname AS schema,
            function.proname AS name,
            function.oid AS implementation_oid,
            function_ns.nspname AS implementation_schema,
            function.provolatile AS volatility,
            function.proretset AS returns_set,
            function.prokind AS routine_kind,
            function.prosecdef AS security_definer,
            function.proconfig AS routine_config,
            language.lanname AS language,
            function_ns.nspname = 'pg_catalog'
              AND function.oid < 16384::oid AS object_builtin,
            function_ns.nspname = 'pg_catalog'
              AND function.oid < 16384::oid AS implementation_builtin
          FROM referenced
          JOIN pg_proc AS function
            ON referenced.refclassid = 'pg_proc'::regclass
           AND referenced.refobjid = function.oid
          JOIN pg_namespace AS function_ns
            ON function_ns.oid = function.pronamespace
          JOIN pg_language AS language
            ON language.oid = function.prolang
        ), operator_objects AS (
          SELECT
            'operator'::text AS kind,
            operator.oid AS object_oid,
            operator.oid::regoperator::text AS identity,
            operator_ns.nspname AS schema,
            operator.oprname AS name,
            implementation.oid AS implementation_oid,
            implementation_ns.nspname AS implementation_schema,
            implementation.provolatile AS volatility,
            implementation.proretset AS returns_set,
            implementation.prokind AS routine_kind,
            implementation.prosecdef AS security_definer,
            implementation.proconfig AS routine_config,
            language.lanname AS language,
            operator_ns.nspname = 'pg_catalog'
              AND operator.oid < 16384::oid AS object_builtin,
            implementation_ns.nspname = 'pg_catalog'
              AND implementation.oid < 16384::oid AS implementation_builtin
          FROM referenced
          JOIN pg_operator AS operator
            ON referenced.refclassid = 'pg_operator'::regclass
           AND referenced.refobjid = operator.oid
          JOIN pg_namespace AS operator_ns
            ON operator_ns.oid = operator.oprnamespace
          JOIN pg_proc AS implementation
            ON implementation.oid = operator.oprcode
          JOIN pg_namespace AS implementation_ns
            ON implementation_ns.oid = implementation.pronamespace
          JOIN pg_language AS language
            ON language.oid = implementation.prolang
        ), builtin_candidates AS (
          SELECT
            'builtin_candidate'::text AS kind,
            function.oid AS object_oid,
            function.oid::regprocedure::text AS identity,
            function_ns.nspname AS schema,
            function.proname AS name,
            function.oid AS implementation_oid,
            function_ns.nspname AS implementation_schema,
            function.provolatile AS volatility,
            function.proretset AS returns_set,
            function.prokind AS routine_kind,
            function.prosecdef AS security_definer,
            function.proconfig AS routine_config,
            language.lanname AS language,
            true AS object_builtin,
            true AS implementation_builtin
          FROM pg_proc AS function
          JOIN pg_namespace AS function_ns
            ON function_ns.oid = function.pronamespace
          JOIN pg_language AS language
            ON language.oid = function.prolang
          WHERE function_ns.nspname = 'pg_catalog'
            AND function.proname = ANY(%s::text[])
        ), objects AS (
          SELECT * FROM function_objects
          UNION
          SELECT * FROM operator_objects
          UNION
          SELECT * FROM builtin_candidates
        )
        SELECT
          objects.kind,
          objects.object_oid,
          objects.identity,
          objects.schema,
          objects.name,
          object_extension.extname AS extension,
          object_extension_ns.nspname AS extension_schema,
          objects.implementation_schema,
          implementation_extension.extname AS implementation_extension,
          implementation_extension_ns.nspname
            AS implementation_extension_schema,
          objects.volatility,
          objects.returns_set,
          objects.routine_kind,
          objects.security_definer,
          objects.routine_config,
          objects.language,
          objects.object_builtin,
          objects.implementation_builtin,
          approved_extension_path.value AS approved_extension_search_path
        FROM objects
        CROSS JOIN approved_extension_path
        LEFT JOIN pg_depend AS object_membership
          ON object_membership.classid = CASE objects.kind
               WHEN 'operator' THEN 'pg_operator'::regclass
               ELSE 'pg_proc'::regclass
             END
         AND object_membership.objid = objects.object_oid
         AND object_membership.refclassid = 'pg_extension'::regclass
         AND object_membership.deptype = 'e'
        LEFT JOIN pg_extension AS object_extension
          ON object_extension.oid = object_membership.refobjid
        LEFT JOIN pg_namespace AS object_extension_ns
          ON object_extension_ns.oid = object_extension.extnamespace
        LEFT JOIN pg_depend AS implementation_membership
          ON implementation_membership.classid = 'pg_proc'::regclass
         AND implementation_membership.objid = objects.implementation_oid
         AND implementation_membership.refclassid = 'pg_extension'::regclass
         AND implementation_membership.deptype = 'e'
        LEFT JOIN pg_extension AS implementation_extension
          ON implementation_extension.oid = implementation_membership.refobjid
        LEFT JOIN pg_namespace AS implementation_extension_ns
          ON implementation_extension_ns.oid =
               implementation_extension.extnamespace
        ORDER BY objects.kind, objects.identity
        """,
        (sorted(APPROVED_EXTENSIONS), f'{schema}."{relation}"', names),
    )
    dependencies = []
    for item in cur.fetchall():
        row = dict(item)
        if row.get("routine_config") is not None:
            row["routine_config"] = tuple(row["routine_config"])
        dependencies.append(RoutineDependency(**row))
    return dependencies


def inspect_relation_types_and_casts(
    cur,
    schema: str,
    relation: str,
) -> list[TypeOrCastDependency]:
    cur.execute(
        """
        WITH relation_rewrites AS (
          SELECT rewrite.oid
          FROM pg_rewrite AS rewrite
          WHERE rewrite.ev_class = %s::regclass
        ), referenced AS (
          SELECT dependency.refclassid, dependency.refobjid
          FROM relation_rewrites AS rewrite
          JOIN pg_depend AS dependency
            ON dependency.classid = 'pg_rewrite'::regclass
           AND dependency.objid = rewrite.oid
           AND dependency.deptype = 'n'
        ), type_oids AS (
          SELECT referenced.refobjid AS oid
          FROM referenced
          WHERE referenced.refclassid = 'pg_type'::regclass
          UNION
          SELECT attribute.atttypid
          FROM pg_attribute AS attribute
          WHERE attribute.attrelid = %s::regclass
            AND attribute.attnum > 0
            AND NOT attribute.attisdropped
        ), type_objects AS (
          SELECT
            'type'::text AS kind,
            type.oid AS object_oid,
            format_type(type.oid, NULL) AS identity,
            type_ns.nspname AS schema,
            type.typname AS name,
            type_ns.nspname = 'pg_catalog'
              AND type.oid < 16384::oid AS object_builtin,
            NULL::oid AS implementation_oid,
            NULL::text AS implementation_schema,
            false AS implementation_builtin,
            NULL::"char" AS volatility,
            NULL::boolean AS security_definer,
            NULL::text[] AS routine_config,
            NULL::text AS language
          FROM type_oids
          JOIN pg_type AS type
            ON type_oids.oid = type.oid
          JOIN pg_namespace AS type_ns
            ON type_ns.oid = type.typnamespace
        ), cast_objects AS (
          SELECT
            'cast'::text AS kind,
            cast_entry.oid AS object_oid,
            pg_describe_object('pg_cast'::regclass, cast_entry.oid, 0) AS identity,
            ''::text AS schema,
            format_type(cast_entry.castsource, NULL)
              || ' AS '
              || format_type(cast_entry.casttarget, NULL) AS name,
            cast_entry.oid < 16384::oid AS object_builtin,
            NULLIF(cast_entry.castfunc, 0) AS implementation_oid,
            implementation_ns.nspname AS implementation_schema,
            cast_entry.castfunc = 0 OR (
              implementation_ns.nspname = 'pg_catalog'
              AND implementation.oid < 16384::oid
            ) AS implementation_builtin,
            implementation.provolatile AS volatility,
            implementation.prosecdef AS security_definer,
            implementation.proconfig AS routine_config,
            language.lanname AS language
          FROM referenced
          JOIN pg_cast AS cast_entry
            ON referenced.refclassid = 'pg_cast'::regclass
           AND referenced.refobjid = cast_entry.oid
          LEFT JOIN pg_proc AS implementation
            ON implementation.oid = NULLIF(cast_entry.castfunc, 0)
          LEFT JOIN pg_namespace AS implementation_ns
            ON implementation_ns.oid = implementation.pronamespace
          LEFT JOIN pg_language AS language
            ON language.oid = implementation.prolang
        ), objects AS (
          SELECT * FROM type_objects
          UNION
          SELECT * FROM cast_objects
        )
        SELECT
          objects.kind,
          objects.object_oid,
          objects.identity,
          objects.schema,
          objects.name,
          object_extension.extname AS extension,
          objects.object_builtin,
          objects.implementation_schema,
          implementation_extension.extname AS implementation_extension,
          objects.implementation_builtin,
          objects.volatility,
          objects.security_definer,
          objects.routine_config,
          objects.language
        FROM objects
        LEFT JOIN pg_depend AS object_membership
          ON object_membership.classid = CASE objects.kind
               WHEN 'type' THEN 'pg_type'::regclass
               ELSE 'pg_cast'::regclass
             END
         AND object_membership.objid = objects.object_oid
         AND object_membership.refclassid = 'pg_extension'::regclass
         AND object_membership.deptype = 'e'
        LEFT JOIN pg_extension AS object_extension
          ON object_extension.oid = object_membership.refobjid
        LEFT JOIN pg_depend AS implementation_membership
          ON implementation_membership.classid = 'pg_proc'::regclass
         AND implementation_membership.objid = objects.implementation_oid
         AND implementation_membership.refclassid = 'pg_extension'::regclass
         AND implementation_membership.deptype = 'e'
        LEFT JOIN pg_extension AS implementation_extension
          ON implementation_extension.oid = implementation_membership.refobjid
        ORDER BY objects.kind, objects.identity
        """,
        (f'{schema}."{relation}"', f'{schema}."{relation}"'),
    )
    dependencies = []
    for item in cur.fetchall():
        row = dict(item)
        if row.get("routine_config") is not None:
            row["routine_config"] = tuple(row["routine_config"])
        dependencies.append(TypeOrCastDependency(**row))
    return dependencies


def validate_relation_types_and_casts(
    cur,
    schema: str,
    relation: str,
) -> list[TypeOrCastDependency]:
    dependencies = inspect_relation_types_and_casts(cur, schema, relation)
    reasons: list[GuardReason] = []
    for dependency in dependencies:
        if not (
            dependency.object_builtin
            or dependency.extension in APPROVED_EXTENSIONS
        ):
            reasons.append(_reason(
                f"unapproved_{dependency.kind}",
                f"Resolved {dependency.kind} {dependency.identity} is not a "
                "built-in or approved PostGIS/H3 extension object.",
            ))
            continue
        if dependency.kind != "cast" or dependency.implementation_schema is None:
            continue
        if not (
            dependency.implementation_builtin
            or dependency.implementation_extension in APPROVED_EXTENSIONS
        ):
            reasons.append(_reason(
                "unapproved_cast_routine",
                f"Resolved cast {dependency.identity} uses an unapproved routine.",
            ))
        if dependency.volatility == "v":
            reasons.append(_reason(
                "volatile_routine",
                f"Resolved cast {dependency.identity} uses a VOLATILE routine.",
            ))
        if dependency.security_definer:
            reasons.append(_reason(
                "security_definer_routine",
                f"Resolved cast {dependency.identity} uses SECURITY DEFINER.",
            ))
        if dependency.routine_config:
            reasons.append(_reason(
                "configured_routine",
                f"Resolved cast {dependency.identity} changes session configuration.",
            ))
        if dependency.language not in APPROVED_ROUTINE_LANGUAGES:
            reasons.append(_reason(
                "unapproved_routine_language",
                f"Resolved cast {dependency.identity} uses unapproved language "
                f"{dependency.language}.",
            ))
    if reasons:
        raise QueryGuardViolation(reasons)
    return dependencies


def _is_h3_sql_wrapper(dependency: RoutineDependency) -> bool:
    return (
        dependency.kind == "function"
        and dependency.extension == "h3_postgis"
        and dependency.language == "sql"
        and dependency.name.lower() in H3_POLYGON_FUNCTIONS
    )


def _has_approved_extension_search_path(
    dependency: RoutineDependency,
) -> bool:
    return (
        _is_h3_sql_wrapper(dependency)
        and dependency.implementation_extension == dependency.extension
        and dependency.schema == dependency.extension_schema
        and dependency.implementation_schema
        == dependency.implementation_extension_schema
        and dependency.routine_config
        == (dependency.approved_extension_search_path,)
    )


def validate_relation_routines(
    cur,
    schema: str,
    relation: str,
    inspection: QueryAstInspection,
) -> list[RoutineDependency]:
    dependencies = inspect_relation_routines(
        cur,
        schema,
        relation,
        inspection.function_names,
    )
    reasons: list[GuardReason] = []
    bounded = inspection.bounded_set_functions
    for dependency in dependencies:
        object_approved = (
            dependency.object_builtin
            or dependency.extension in APPROVED_EXTENSIONS
        )
        implementation_approved = (
            dependency.implementation_builtin
            or dependency.implementation_extension in APPROVED_EXTENSIONS
        )
        if not object_approved or not implementation_approved:
            reasons.append(_reason(
                f"unapproved_{dependency.kind}",
                f"Resolved {dependency.kind} {dependency.identity} is not a "
                "pg_catalog or approved PostGIS/H3 extension object.",
            ))
            continue
        if dependency.security_definer:
            reasons.append(_reason(
                "security_definer_routine",
                f"Resolved routine {dependency.identity} is SECURITY DEFINER.",
            ))
        approved_search_path = _has_approved_extension_search_path(dependency)
        requires_approved_search_path = _is_h3_sql_wrapper(dependency)
        if (
            dependency.routine_config and not approved_search_path
        ) or (
            requires_approved_search_path and not approved_search_path
        ):
            reasons.append(_reason(
                "configured_routine",
                f"Resolved routine {dependency.identity} must not change "
                "session configuration. Approved H3 SQL wrappers are the "
                "only exception and must pin search_path exactly to "
                "pg_catalog plus the authoritative schemas of the installed "
                "PostGIS/H3 extensions.",
            ))
        if dependency.language not in APPROVED_ROUTINE_LANGUAGES:
            reasons.append(_reason(
                "unapproved_routine_language",
                f"Resolved routine {dependency.identity} uses unapproved "
                f"language {dependency.language}.",
            ))
        if dependency.name.lower() in DANGEROUS_CATALOG_FUNCTIONS:
            reasons.append(_reason(
                "dangerous_catalog_function",
                f"Resolved routine {dependency.identity} exposes dynamic "
                "queries, files, server state or administrative controls.",
            ))
        if dependency.volatility == "v":
            reasons.append(_reason(
                "volatile_routine",
                f"Resolved routine {dependency.identity} is VOLATILE and is "
                "not allowed in a derived layer.",
            ))
        if dependency.routine_kind == "a" and dependency.name.lower() in (
            UNBOUNDED_AGGREGATES
        ):
            reasons.append(_reason(
                "unbounded_aggregate_state",
                f"Aggregate {dependency.identity} has an unbounded transition state.",
            ))
        if dependency.routine_kind == "a":
            aggregate_allowlist = (
                APPROVED_BUILTIN_AGGREGATES
                if dependency.object_builtin
                else APPROVED_EXTENSION_AGGREGATES
            )
            if dependency.name.lower() not in aggregate_allowlist:
                reasons.append(_reason(
                    "unapproved_aggregate",
                    f"Aggregate {dependency.identity} is not on the fixed-state "
                    "derived-layer allowlist.",
                ))
        if dependency.routine_kind == "w" and not dependency.object_builtin:
            reasons.append(_reason(
                "unapproved_window_routine",
                f"Extension window routine {dependency.identity} can retain an "
                "unbounded partition state.",
            ))
        if dependency.returns_set and dependency.name.lower() not in bounded:
            reasons.append(_reason(
                "unbounded_set_function",
                f"Set-returning routine {dependency.identity} was not proven "
                "bounded by the AST guard.",
            ))
    if reasons:
        raise QueryGuardViolation(reasons)
    validate_relation_types_and_casts(cur, schema, relation)
    return dependencies
