"""Validation rules for the supported GEOLYTIX XYZ workspace surface.

XYZ deliberately accepts extensible workspace objects and does not publish a
closed JSON schema. This validator is therefore strict for fields the editor
understands and preserves unknown properties for forward compatibility.
"""

from __future__ import annotations

import copy
import re
from typing import Any

DB_KEY = re.compile(r"^[A-Za-z0-9-]+$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
RELATION = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?$"
)
SUPPORTED_FORMATS = {
    "cluster", "geojson", "googleMapTiles", "mapboxStyle", "maplibre",
    "mvt", "tiles", "vector", "wkt",
}
SCALE_UNITS = {"metric", "imperial"}
COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
ICON_TYPES = {
    "dot", "target", "triangle", "square", "diamond", "semiCircle",
    "circle", "markerLetter", "markerColor", "template",
}
FILTER_TYPES = {
    "like", "match", "numeric", "integer", "in", "ni", "date",
    "datetime", "boolean", "null",
}
UNSAFE_EXPRESSION = re.compile(
    r"\b(?:select|insert|update|delete|merge|drop|alter|create|truncate|grant|"
    r"revoke|copy|call|do|execute|prepare|deallocate|vacuum|analyze|refresh|"
    r"set|reset|show|listen|notify|unlisten|pg_sleep|pg_read_file|"
    r"pg_ls_dir|current_setting|set_config|dblink)\b",
    re.IGNORECASE,
)
UNSAFE_EXPRESSION_VALUE = re.compile(
    r"\b(?:current_user|session_user|current_role|current_database|"
    r"current_schema|current_catalog|current_query|current_setting|"
    r"current_date|current_time|current_timestamp|localtime|localtimestamp|"
    r"pg_backend_pid|operator)\b",
    re.IGNORECASE,
)
FUNCTION_CALL = re.compile(
    r"(?<![A-Za-z0-9_$])"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*)"
    r"\s*\(",
)
SAFE_FUNCTIONS = {
    "abs", "array_length", "array_to_string", "btrim", "cardinality",
    "ceil", "ceiling", "char_length", "coalesce", "concat", "concat_ws",
    "date_part", "date_trunc", "extract", "floor", "greatest", "initcap",
    "json_array_length", "json_extract_path_text", "json_typeof",
    "jsonb_array_length", "jsonb_extract_path_text", "jsonb_typeof", "least",
    "left", "length", "lower", "ltrim", "make_date", "mod", "nullif",
    "octet_length", "power", "regexp_replace", "replace", "right", "round",
    "rtrim", "sign", "split_part", "sqrt", "st_area", "st_asgeojson",
    "st_centroid", "st_distance", "st_geometrytype", "st_isvalid",
    "st_length", "st_pointonsurface", "st_srid", "st_transform", "st_x",
    "st_y", "substr", "substring", "to_char", "to_jsonb", "trim", "trunc",
    "upper",
}
SAFE_CALL_SYNTAX = {"in"}
SAFE_CAST_TYPES = {
    "bigint", "bool", "boolean", "date", "decimal", "double precision",
    "float4", "float8", "int", "int2", "int4", "int8", "integer", "json",
    "jsonb", "numeric", "real", "smallint", "text", "time", "timestamp",
    "timestamptz", "timetz", "uuid", "varchar",
}
CAST_TYPE = re.compile(
    r"\s*(?P<type>double\s+precision|[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<array>\s*\[\s*\])?",
    re.IGNORECASE,
)
DOLLAR_QUOTE = re.compile(r"\$[A-Za-z0-9_]*\$")


def _mask_string_literals(expression: str) -> str:
    output = list(expression)
    index = 0
    while index < len(output):
        if output[index] != "'":
            index += 1
            continue
        output[index] = " "
        index += 1
        while index < len(output):
            if output[index] == "'":
                output[index] = " "
                if index + 1 < len(output) and output[index + 1] == "'":
                    output[index + 1] = " "
                    index += 2
                    continue
                index += 1
                break
            output[index] = " "
            index += 1
        else:
            raise ValueError("SQL string literal is not terminated.")
    return "".join(output)


def expression_function_names(expression: str) -> set[str]:
    masked = _mask_string_literals(expression)
    output: set[str] = set()
    for match in FUNCTION_CALL.finditer(masked):
        name = match.group("name").lower()
        if name in SAFE_CALL_SYNTAX:
            continue
        if "." in name or name not in SAFE_FUNCTIONS:
            raise ValueError(f"SQL function is not allowed: {name}.")
        output.add(name)
    return output


def expression_error(expression: str) -> str | None:
    if len(expression) > 4000:
        return "SQL expression is too long; the maximum is 4000 characters."
    if (
        any(marker in expression for marker in (";", "--", "/*", "*/"))
        or DOLLAR_QUOTE.search(expression)
    ):
        return "Comments, dollar quoting, and multiple SQL statements are not allowed."
    try:
        masked = _mask_string_literals(expression)
    except ValueError as exc:
        return str(exc)
    if '"' in masked:
        return "Quoted identifiers are not supported in calculated expressions."
    if UNSAFE_EXPRESSION.search(masked) or UNSAFE_EXPRESSION_VALUE.search(masked):
        return (
            "Use one allowlisted scalar expression; statements, subqueries, "
            "session values, arbitrary execution, and file/system access are not allowed."
        )
    for marker in re.finditer(r"::", masked):
        cast = CAST_TYPE.match(masked, marker.end())
        if not cast:
            return "Cast types must use a supported built-in type."
        cast_type = " ".join(cast.group("type").lower().split())
        if cast_type not in SAFE_CAST_TYPES:
            return f"Cast type is not allowed: {cast_type}."
    try:
        expression_function_names(expression)
    except ValueError as exc:
        return str(exc)
    return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _error(errors: list[dict[str, str]], path: str, message: str) -> None:
    errors.append({"path": path, "message": message})


def _merge(base: Any, override: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(override, dict):
        return copy.deepcopy(override)
    merged = copy.deepcopy(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, list):
            if all(_xyz_array_includes(current, item) for item in value):
                merged[key] = copy.deepcopy(value)
            else:
                merged[key] = copy.deepcopy(current) + copy.deepcopy(value)
        elif isinstance(value, dict):
            if isinstance(current, dict):
                merged[key] = _merge(current, value)
            elif key not in merged or not _xyz_truthy(current):
                merged[key] = _merge({}, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _xyz_array_includes(values: list, item: Any) -> bool:
    if isinstance(item, (dict, list)):
        return any(value is item for value in values)
    if isinstance(item, bool) or item is None or isinstance(item, str):
        return any(type(value) is type(item) and value == item for value in values)
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        return any(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == item
            for value in values
        )
    return any(type(value) is type(item) and value == item for value in values)


def _xyz_truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return bool(value)
    return True


def validate_workspace(data: Any, available_dbs: set[str] | None = None) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(data, dict):
        return [{"path": "$", "message": "Workspace must be a JSON object."}]

    if "key" in data and (not isinstance(data["key"], str) or not data["key"].strip()):
        _error(errors, "key", "Must be a non-empty string.")

    _validate_dbs(data.get("dbs"), "dbs", errors, available_dbs, required=False)

    locale = data.get("locale")
    locales = data.get("locales")
    if locale is not None and not isinstance(locale, dict):
        _error(errors, "locale", "Must be an object.")
    elif isinstance(locale, dict):
        _validate_locale(locale, "locale", errors, available_dbs)
    if locales is not None:
        if not isinstance(locales, dict):
            _error(errors, "locales", "Must be an object keyed by locale name.")
        else:
            usable = 0
            for key, value in locales.items():
                if not isinstance(key, str) or not key:
                    _error(errors, "locales", "Locale keys must be non-empty strings.")
                if not isinstance(value, dict):
                    _error(errors, f"locales.{key}", "Must be an object.")
                    continue
                usable += 1
                if key == "locale":
                    # XYZ resolves the literal locale key to workspace.locale,
                    # so this entry is not a separately rendered alternative.
                    continue
                effective = _merge(
                    locale if isinstance(locale, dict) else {"layers": {}},
                    value,
                )
                _validate_locale(
                    effective,
                    f"locales.{key}",
                    errors,
                    available_dbs,
                )
            if not usable:
                _error(errors, "locales", "Define at least one locale object.")

    if locale is None and locales is None:
        _error(errors, "locale", "Define locale or locales; the editor requires an explicit locale.")
    return errors


def _validate_dbs(value, path, errors, available_dbs, *, required):
    if value is None:
        if required:
            _error(errors, path, "A database connection name is required.")
        return
    if not isinstance(value, str) or not DB_KEY.fullmatch(value):
        _error(errors, path, "Must contain only letters, numbers, or hyphens.")
    elif available_dbs is not None and value not in available_dbs:
        _error(errors, path, f"No DBS_{value} connection is configured.")


def _validate_locale(locale, path, errors, available_dbs):
    if not isinstance(locale, dict):
        _error(errors, path, "Must be an object.")
        return
    if "name" in locale and not isinstance(locale["name"], str):
        _error(errors, f"{path}.name", "Must be a string.")
    extent = locale.get("extent")
    if extent is not None:
        if not isinstance(extent, dict):
            _error(errors, f"{path}.extent", "Must be an object.")
        else:
            for key, low, high in (("north", -90, 90), ("south", -90, 90), ("east", -180, 180), ("west", -180, 180)):
                if key not in extent:
                    continue
                value = extent.get(key)
                if not _number(value) or not low <= value <= high:
                    _error(errors, f"{path}.extent.{key}", f"Must be a number from {low} to {high}.")
            if _number(extent.get("north")) and _number(extent.get("south")) and extent["north"] <= extent["south"]:
                _error(errors, f"{path}.extent", "north must be greater than south.")
            if "mask" in extent and not isinstance(extent["mask"], bool):
                _error(errors, f"{path}.extent.mask", "Must be true or false.")
    view = locale.get("view")
    if view is not None:
        if not isinstance(view, dict):
            _error(errors, f"{path}.view", "Must be an object.")
        else:
            for key, low, high in (("lat", -90, 90), ("lng", -180, 180), ("z", 0, 30)):
                if key not in view:
                    continue
                value = view.get(key)
                if not _number(value) or not low <= value <= high:
                    _error(errors, f"{path}.view.{key}", f"Must be a number from {low} to {high}.")
            if isinstance(extent, dict) and all(_number(extent.get(k)) for k in ("north", "south", "east", "west")):
                if _number(view.get("lat")) and not extent["south"] <= view["lat"] <= extent["north"]:
                    _error(errors, f"{path}.view.lat", "Must fall inside the configured north/south extent.")
                if extent["west"] <= extent["east"] and _number(view.get("lng")) and not extent["west"] <= view["lng"] <= extent["east"]:
                    _error(errors, f"{path}.view.lng", "Must fall inside the configured west/east extent.")
    if "mapviewControls" in locale and not (
        isinstance(locale["mapviewControls"], list)
        and all(isinstance(item, str) for item in locale["mapviewControls"])
    ):
        _error(errors, f"{path}.mapviewControls", "Must be an array of strings.")
    if "ScaleLine" in locale and locale["ScaleLine"] not in SCALE_UNITS:
        _error(errors, f"{path}.ScaleLine", f"Must be one of: {', '.join(sorted(SCALE_UNITS))}.")
    layers = locale.get("layers")
    if layers is None:
        return
    if not isinstance(layers, dict):
        _error(errors, f"{path}.layers", "Must be an object keyed by layer name.")
        return
    for key, layer in layers.items():
        _validate_layer(key, layer, f"{path}.layers.{key}", errors, available_dbs)


def _validate_layer(key, layer, path, errors, available_dbs):
    if not isinstance(key, str) or not key.strip():
        _error(errors, path, "Layer key must be a non-empty string.")
    if not isinstance(layer, dict):
        _error(errors, path, "Layer must be an object.")
        return
    has_template = isinstance(layer.get("template"), str)
    fmt = layer.get("format")
    if fmt not in SUPPORTED_FORMATS and not (fmt is None and has_template):
        _error(errors, f"{path}.format", f"Must be one of: {', '.join(sorted(SUPPORTED_FORMATS))}.")
    if "display" in layer and not isinstance(layer["display"], bool):
        _error(errors, f"{path}.display", "Must be true or false.")
    if "group" in layer and (
        not isinstance(layer["group"], str) or not layer["group"].strip()
    ):
        _error(errors, f"{path}.group", "Must be a non-empty string when set.")
    if fmt is None and has_template:
        _validate_style(layer.get("style"), f"{path}.style", errors)
        return
    if fmt == "tiles":
        has_uri = isinstance(layer.get("URI"), str) and layer["URI"].strip()
        has_template = (
            isinstance(layer.get("template"), str)
            and layer["template"].strip()
        )
        if not has_uri and not has_template:
            _error(
                errors,
                f"{path}.URI",
                "A tile layer requires a non-empty URI or template.",
            )
        return
    if fmt == "googleMapTiles":
        if not isinstance(layer.get("apiKey"), str) or not layer["apiKey"].strip():
            _error(errors, f"{path}.apiKey", "A Google Maps Tiles layer requires an API key.")
        return
    if fmt in {"mapboxStyle", "maplibre"}:
        style = layer.get("style")
        if not isinstance(style, dict) or not (
            isinstance(style.get("URL"), str) or isinstance(style.get("object"), dict)
        ):
            _error(errors, f"{path}.style", "Provide a style URL or inline style object.")
        if fmt == "mapboxStyle" and not isinstance(layer.get("accessToken"), str):
            _error(errors, f"{path}.accessToken", "A Mapbox style layer requires an access token.")
        return
    if fmt in {"cluster", "mvt", "geojson", "vector", "wkt"}:
        _validate_dbs(layer.get("dbs"), f"{path}.dbs", errors, available_dbs, required=False)
        has_features = isinstance(layer.get("features"), list)
        table = layer.get("table")
        tables = layer.get("tables")
        geoms = layer.get("geoms")
        has_tables = isinstance(tables, dict) and bool(tables)
        has_geoms = isinstance(geoms, dict) and bool(geoms)
        if tables is not None and not has_tables:
            _error(errors, f"{path}.tables", "Must be a non-empty zoom-keyed object.")
        elif has_tables:
            for zoom, relation in tables.items():
                if (
                    not isinstance(zoom, str)
                    or not zoom.isdigit()
                    or (
                        relation is not None
                        and (
                            not isinstance(relation, str)
                            or not RELATION.fullmatch(relation)
                        )
                    )
                ):
                    _error(
                        errors,
                        f"{path}.tables",
                        "Zoom keys must map to unquoted relations or null.",
                    )
                    break
        if geoms is not None and not has_geoms:
            _error(errors, f"{path}.geoms", "Must be a non-empty zoom-keyed object.")
        elif has_geoms:
            for zoom, geom in geoms.items():
                if (
                    not isinstance(zoom, str)
                    or not zoom.isdigit()
                    or (
                        geom is not None
                        and (
                            not isinstance(geom, str)
                            or not IDENTIFIER.fullmatch(geom)
                        )
                    )
                ):
                    _error(
                        errors,
                        f"{path}.geoms",
                        "Zoom keys must map to unquoted geometry columns or null.",
                    )
                    break
        if not has_template and not has_features and not has_tables and (
            not isinstance(table, str) or not RELATION.fullmatch(table)
        ):
            _error(errors, f"{path}.table", "Use table/schema.table or zoom-keyed tables.")
        for field in ("geom", "qID"):
            value = layer.get(field)
            field_is_optional = (
                field == "geom"
                and (has_features or has_geoms)
            )
            if not has_template and not field_is_optional and (
                not isinstance(value, str) or not IDENTIFIER.fullmatch(value)
            ):
                _error(errors, f"{path}.{field}", "Must be an unquoted PostgreSQL column identifier.")
        srid = layer.get("srid")
        try:
            valid_srid = 0 < int(srid) <= 999999
        except (TypeError, ValueError):
            valid_srid = False
        if not valid_srid and not has_template:
            _error(errors, f"{path}.srid", "Must be a positive EPSG/SRID integer.")
        elif valid_srid and fmt == "mvt" and int(srid) != 3857:
            _error(errors, f"{path}.srid", "XYZ v4.23.4 requires SRID 3857 for MVT layers.")
        fields: set[str] = set()
        infoj = layer.get("infoj")
        if infoj is not None:
            if not isinstance(infoj, list):
                _error(errors, f"{path}.infoj", "Must be an array.")
            else:
                for index, entry in enumerate(infoj):
                    entry_path = f"{path}.infoj.{index}"
                    if not isinstance(entry, dict):
                        _error(errors, entry_path, "Must be an object.")
                        continue
                    field = entry.get("field")
                    field_optional = (
                        isinstance(entry.get("query"), str)
                        or isinstance(entry.get("key"), str)
                        or entry.get("type") in {"tab", "title"}
                    )
                    if field is None and field_optional:
                        pass
                    elif not isinstance(field, str) or not IDENTIFIER.fullmatch(field):
                        _error(errors, f"{entry_path}.field", "Must be an unquoted result-field identifier.")
                    elif field in fields:
                        _error(errors, f"{entry_path}.field", "Must be unique within infoj.")
                    else:
                        fields.add(field)
                    if "display" in entry and not isinstance(entry["display"], bool):
                        _error(errors, f"{entry_path}.display", "Must be true or false.")
                    if "inline" in entry and not isinstance(entry["inline"], bool):
                        _error(errors, f"{entry_path}.inline", "Must be true or false.")
                    _validate_info_filter(
                        entry.get("filter"),
                        f"{entry_path}.filter",
                        errors,
                    )
                    expression = entry.get("fieldfx")
                    if expression is not None and (not isinstance(expression, str) or not expression.strip()):
                        _error(errors, f"{entry_path}.fieldfx", "Must be a non-empty PostgreSQL expression.")
                    if isinstance(expression, str):
                        error = expression_error(expression)
                        if error:
                            _error(errors, f"{entry_path}.fieldfx", error)
        _validate_layer_filter(
            layer.get("filter"),
            f"{path}.filter",
            errors,
            fields,
        )
        _validate_style(layer.get("style"), f"{path}.style", errors)


def _validate_info_filter(value, path, errors):
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if value not in FILTER_TYPES:
            _error(errors, path, f"Must be one of: {', '.join(sorted(FILTER_TYPES))}.")
        return
    if not isinstance(value, dict):
        _error(errors, path, "Must be true, false, a filter type, or an object.")
        return
    filter_type = value.get("type")
    if filter_type not in FILTER_TYPES:
        _error(errors, f"{path}.type", f"Must be one of: {', '.join(sorted(FILTER_TYPES))}.")
    field = value.get("field")
    if field is not None and (
        not isinstance(field, str) or not IDENTIFIER.fullmatch(field)
    ):
        _error(errors, f"{path}.field", "Must be an unquoted result-field identifier.")
    for key in (
        "leading_wildcard", "dropdown", "dropdown_pills", "dropdown_search",
        "searchbox",
    ):
        if key in value and not isinstance(value[key], bool):
            _error(errors, f"{path}.{key}", "Must be true or false.")
    for key in ("min", "max", "step"):
        if key in value and not _number(value[key]):
            _error(errors, f"{path}.{key}", "Must be a number.")
    if _number(value.get("step")) and value["step"] <= 0:
        _error(errors, f"{path}.step", "Must be greater than zero.")
    if (
        _number(value.get("min"))
        and _number(value.get("max"))
        and value["min"] > value["max"]
    ):
        _error(errors, path, "min must not be greater than max.")
    for key in ("in", "ni"):
        if key in value and not isinstance(value[key], list):
            _error(errors, f"{path}.{key}", "Must be an array.")


def _validate_layer_filter(value, path, errors, fields):
    if value is None:
        return
    if not isinstance(value, dict):
        _error(errors, path, "Must be an object.")
        return
    for key in ("hidden", "viewport", "includeAll"):
        if key in value and not isinstance(value[key], bool):
            _error(errors, f"{path}.{key}", "Must be true or false.")
    for key in ("include", "exclude"):
        selected = value.get(key)
        if selected is None:
            continue
        if not (
            isinstance(selected, list)
            and all(
                isinstance(item, str) and IDENTIFIER.fullmatch(item)
                for item in selected
            )
            and len(selected) == len(set(selected))
        ):
            _error(
                errors,
                f"{path}.{key}",
                "Must be an array of unique result-field identifiers.",
            )
            continue
        for field in selected:
            if field not in fields:
                _error(
                    errors,
                    f"{path}.{key}",
                    f"Unknown infoj field: {field}.",
                )


def _validate_style(style, path, errors):
    if style is None:
        return
    if not isinstance(style, dict):
        _error(errors, path, "Must be an object.")
        return
    if "hidden" in style and not isinstance(style["hidden"], bool):
        _error(errors, f"{path}.hidden", "Must be true or false.")
    elements = style.get("elements")
    if elements is not None and not (
        isinstance(elements, list)
        and all(isinstance(item, str) and item.strip() for item in elements)
        and len(elements) == len(set(elements))
    ):
        _error(
            errors,
            f"{path}.elements",
            "Must be an array of unique, non-empty style element keys.",
        )
    hover = style.get("hover")
    if hover is not None:
        if not isinstance(hover, (dict, str)):
            _error(errors, f"{path}.hover", "Must be a hover object or a key from style.hovers.")
        elif isinstance(hover, dict):
            if not isinstance(hover.get("field"), str) or not IDENTIFIER.fullmatch(hover["field"]):
                _error(errors, f"{path}.hover.field", "Must select an unquoted database column identifier.")
            for key in ("display", "dynamic", "hidden"):
                if key in hover and not isinstance(hover[key], bool):
                    _error(errors, f"{path}.hover.{key}", "Must be true or false.")
            for key in ("title", "label", "query"):
                if key in hover and (not isinstance(hover[key], str) or not hover[key].strip()):
                    _error(errors, f"{path}.hover.{key}", "Must be a non-empty string.")
    for state in ("default", "highlight"):
        value = style.get(state)
        if value is None:
            continue
        if not isinstance(value, dict):
            _error(errors, f"{path}.{state}", "Must be an object.")
            continue
        for key in ("fillColor", "strokeColor"):
            if key in value and (not isinstance(value[key], str) or not COLOR.fullmatch(value[key])):
                _error(errors, f"{path}.{state}.{key}", "Must be a six- or eight-digit hex color.")
        for key, low, high in (("fillOpacity", 0, 1), ("strokeOpacity", 0, 1), ("strokeWidth", 0, 20), ("scale", 0.1, 10)):
            if key in value and (not _number(value[key]) or not low <= value[key] <= high):
                _error(errors, f"{path}.{state}.{key}", f"Must be a number from {low} to {high}.")
        if "lineDash" in value and not (
            isinstance(value["lineDash"], list)
            and value["lineDash"]
            and all(_number(item) and item >= 0 for item in value["lineDash"])
        ):
            _error(errors, f"{path}.{state}.lineDash", "Must be a non-empty array of non-negative numbers.")
        icon = value.get("icon")
        if icon is not None:
            icons = icon if isinstance(icon, list) else [icon]
            if not icons or not all(isinstance(item, dict) for item in icons):
                _error(
                    errors,
                    f"{path}.{state}.icon",
                    "Must be one XYZ icon object or a non-empty icon array.",
                )
                continue
            for index, item in enumerate(icons):
                icon_path = (
                    f"{path}.{state}.icon.{index}"
                    if isinstance(icon, list)
                    else f"{path}.{state}.icon"
                )
                if item.get("type") not in ICON_TYPES and not item.get("url"):
                    _error(errors, f"{icon_path}.type", f"Must be an XYZ v4.23.4 symbol: {', '.join(sorted(ICON_TYPES))}.")
                for key in ("fillColor", "strokeColor", "color", "colorMarker", "colorDot"):
                    if key in item and (not isinstance(item[key], str) or not COLOR.fullmatch(item[key])):
                        _error(errors, f"{icon_path}.{key}", "Must be a six- or eight-digit hex color.")
                for key, low, high in (("strokeWidth", 0, 20), ("scale", 0.1, 10)):
                    if key in item and (not _number(item[key]) or not low <= item[key] <= high):
                        _error(errors, f"{icon_path}.{key}", f"Must be a number from {low} to {high}.")
                if item.get("type") == "markerLetter" and (
                    not isinstance(item.get("letter"), str) or len(item["letter"]) != 1
                ):
                    _error(errors, f"{icon_path}.letter", "Must be exactly one character.")
