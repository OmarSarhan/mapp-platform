"""Validation rules for the supported GEOLYTIX XYZ workspace surface.

XYZ deliberately accepts extensible workspace objects and does not publish a
closed JSON schema. This validator exposes only the audited surface of the
pinned framework and rejects unadvertised properties instead of stripping or
silently preserving them.
"""

from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from plugin_registry import available_plugins, validate_workspace_plugins

# Must match ALIAS_RE in semantic_sources.py — one alias grammar, not two.
DB_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")
XYZ_LAYER_KEY = re.compile(r"^[A-Za-z0-9 :_-]+$")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
FIXED_FILTER_NUMBER_RE = re.compile(
    r"^[+-]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)(?:[eE][+-]?[0-9]+)?$"
)
RELATION = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)?$"
)
SUPPORTED_FORMATS = {
    "cluster", "geojson", "googleMapTiles", "mapboxStyle", "maplibre",
    "mvt", "tiles", "vector", "wkt",
}
SCALE_UNITS = {"metric", "imperial"}
COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
CSS_CLASS_LIST = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_-]*(?: +[A-Za-z_][A-Za-z0-9_-]*)*$"
)
ICON_TYPES = {
    "dot", "target", "triangle", "square", "diamond", "semiCircle",
    "circle", "markerLetter", "markerColor", "template",
}
FILTER_TYPES = {
    "like", "match", "numeric", "integer", "in", "ni", "date",
    "datetime", "boolean", "null",
}
_CONTRACT = json.loads(
    (Path(__file__).parent / "schema/workspace.schema.json").read_text(encoding="utf-8")
)
TOP_LEVEL_KEYS = frozenset(_CONTRACT["properties"])
LOCALE_KEYS = frozenset(_CONTRACT["$defs"]["locale"]["properties"])
LAYER_KEYS = frozenset(_CONTRACT["$defs"]["layer"]["properties"])
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


def _finite_number_string(value: str) -> bool:
    if not FIXED_FILTER_NUMBER_RE.fullmatch(value):
        return False
    try:
        return math.isfinite(float(value))
    except ValueError:
        return False


def _finite_number(value: Any) -> bool:
    if not _number(value):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _error(errors: list[dict[str, str]], path: str, message: str) -> None:
    errors.append({"path": path, "message": message})


def _reject_unknown(value: dict, allowed: frozenset[str], path: str, errors) -> None:
    for key in value:
        if key not in allowed:
            _error(
                errors,
                f"{path}.{key}" if path else key,
                "Property is not supported by the pinned XYZ workspace contract.",
            )


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
    _reject_unknown(data, TOP_LEVEL_KEYS, "", errors)

    if "key" in data and (not isinstance(data["key"], str) or not data["key"].strip()):
        _error(errors, "key", "Must be a non-empty string.")

    _validate_dbs(data.get("dbs"), "dbs", errors, available_dbs, required=False)
    if "roles" in data:
        _validate_roles(data["roles"], "roles", errors)
    templates = data.get("templates")
    if templates is not None:
        if not isinstance(templates, dict):
            _error(errors, "templates", "Must be an object keyed by template name.")
        else:
            for key, template in templates.items():
                template_path = f"templates.{key}"
                if not isinstance(key, str) or not key.strip():
                    _error(errors, "templates", "Template keys must be non-empty strings.")
                _validate_template_definition(
                    template, template_path, errors, available_dbs
                )

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
    errors.extend(validate_workspace_plugins(data))
    return errors


def _validate_dbs(value, path, errors, available_dbs, *, required):
    if value is None:
        if required:
            _error(errors, path, "A database connection name is required.")
        return
    if not isinstance(value, str) or not DB_KEY.fullmatch(value):
        _error(
            errors,
            path,
            "Must start with a letter and contain only letters, numbers, "
            "hyphens, or underscores (63 characters max).",
        )
    elif available_dbs is not None and value not in available_dbs:
        _error(errors, path, f"No DBS_{value} connection is configured.")


def _validate_template_definition(value, path, errors, available_dbs):
    if not isinstance(value, dict):
        _error(errors, path, "Must be a template object.")
        return
    for key in ("src", "template"):
        if key in value and not isinstance(value[key], str):
            _error(errors, f"{path}.{key}", "Must be a string.")
    if "src" in value and not value["src"].strip():
        _error(errors, f"{path}.src", "Must be non-empty when set.")
    _validate_dbs(
        value.get("dbs"), f"{path}.dbs", errors, available_dbs, required=False
    )
    for key in ("module", "nonblocking", "value_only", "reduce", "admin", "layer"):
        if key in value and not isinstance(value[key], bool):
            _error(errors, f"{path}.{key}", "Must be true or false.")
    timeout = value.get("statement_timeout")
    if timeout is not None and (
        not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 0
    ):
        _error(errors, f"{path}.statement_timeout", "Must be a non-negative integer in milliseconds.")
    if "roles" in value:
        _validate_roles(value["roles"], f"{path}.roles", errors)


def _validate_template_reference(value, path, errors, available_dbs):
    if isinstance(value, str) and value.strip():
        return
    if isinstance(value, dict):
        _validate_template_definition(value, path, errors, available_dbs)
        return
    _error(errors, path, "Must be a non-empty template key or template object.")


def _validate_roles(value, path, errors):
    if not isinstance(value, dict):
        _error(errors, path, "Must be an object keyed by role name.")
        return
    for role, override in value.items():
        if not isinstance(role, str) or not role:
            _error(errors, path, "Role names must be non-empty strings.")
        if override is not None and not isinstance(override, (bool, dict)):
            _error(errors, f"{path}.{role}", "Must be true, false, null, or an object override.")


def _validate_locale(locale, path, errors, available_dbs):
    if not isinstance(locale, dict):
        _error(errors, path, "Must be an object.")
        return
    plugin_keys = {
        plugin["configurationKey"]
        for plugin in available_plugins()
        if "locale" in plugin["scope"]
    }
    _reject_unknown(locale, LOCALE_KEYS | plugin_keys, path, errors)
    if "name" in locale and not isinstance(locale["name"], str):
        _error(errors, f"{path}.name", "Must be a string.")
    if "roles" in locale:
        _validate_roles(locale["roles"], f"{path}.roles", errors)
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
    if "template" in locale:
        _validate_template_reference(
            locale["template"], f"{path}.template", errors, available_dbs
        )
    if "templates" in locale:
        if not isinstance(locale["templates"], list):
            _error(errors, f"{path}.templates", "Must be an array of template references.")
        else:
            for index, reference in enumerate(locale["templates"]):
                _validate_template_reference(
                    reference, f"{path}.templates.{index}", errors, available_dbs
                )
    for key in ("plugins", "syncPlugins"):
        value = locale.get(key)
        if value is not None and not (
            isinstance(value, list)
            and all(isinstance(item, str) and item.strip() for item in value)
        ):
            _error(errors, f"{path}.{key}", "Must be an array of non-empty strings.")
    if isinstance(locale.get("plugins"), list) and len(locale["plugins"]) != len(set(locale["plugins"])):
        _error(errors, f"{path}.plugins", "Must not contain duplicate module references.")
    _validate_bundled_plugins(locale, path, errors)
    dictionary = locale.get("keyvalue_dictionary")
    if dictionary is not None:
        _validate_keyvalue_dictionary(dictionary, f"{path}.keyvalue_dictionary", errors)
    layers = locale.get("layers")
    if layers is None:
        return
    if not isinstance(layers, dict):
        _error(errors, f"{path}.layers", "Must be an object keyed by layer name.")
        return
    for key, layer in layers.items():
        if not isinstance(key, str) or not key or not XYZ_LAYER_KEY.fullmatch(key):
            _error(
                errors,
                f"{path}.layers.{key}",
                "Layer keys may contain only letters, numbers, spaces, colons, underscores, or hyphens; use name for display punctuation.",
            )
        _validate_layer(key, layer, f"{path}.layers.{key}", errors, available_dbs)


def _validate_bundled_plugins(locale, path, errors):
    object_plugins = {
        "admin", "dark_mode", "fullscreen", "locator", "login", "userIDB",
        "userLayer", "userLocale", "zoomBtn", "zoomToArea",
    }
    for key in object_plugins:
        if key in locale:
            if not isinstance(locale[key], dict):
                _error(errors, f"{path}.{key}", "Must be an object.")
            else:
                allowed = frozenset({"title"}) if key == "userIDB" else frozenset()
                _reject_unknown(locale[key], allowed, f"{path}.{key}", errors)
    consent = locale.get("consent")
    if consent is not None and (
        not isinstance(consent, dict)
        or not isinstance(consent.get("text"), str)
        or not consent["text"].strip()
    ):
        _error(errors, f"{path}.consent.text", "Consent requires non-empty text.")
    if isinstance(consent, dict):
        _reject_unknown(consent, frozenset({"text", "title"}), f"{path}.consent", errors)
    theme = locale.get("custom_theme")
    if theme is not None and (
        not isinstance(theme, dict)
        or not all(isinstance(value, str) for value in theme.values())
    ):
        _error(errors, f"{path}.custom_theme", "Must map CSS colour keys to strings.")
    feature_info = locale.get("feature_info")
    if feature_info is not None and feature_info is not True and not isinstance(feature_info, dict):
        _error(errors, f"{path}.feature_info", "Must be true or an object.")
    elif isinstance(feature_info, dict):
        _reject_unknown(feature_info, frozenset({"features", "css"}), f"{path}.feature_info", errors)
        if "features" in feature_info and not isinstance(feature_info["features"], bool):
            _error(errors, f"{path}.feature_info.features", "Must be true or false.")
        if "css" in feature_info and not isinstance(feature_info["css"], str):
            _error(errors, f"{path}.feature_info.css", "Must be a string.")
    layer_order = locale.get("layer_order")
    if layer_order is not None and not (
        isinstance(layer_order, list)
        and all(isinstance(item, str) for item in layer_order)
        and len(layer_order) == len(set(layer_order))
    ):
        _error(errors, f"{path}.layer_order", "Must be an array of unique layer keys.")
    links = locale.get("link_button")
    if links is not None:
        entries = links if isinstance(links, list) else [links]
        if not isinstance(links, (dict, list)) or not entries:
            _error(errors, f"{path}.link_button", "Must be a link object or non-empty array.")
        else:
            for index, link in enumerate(entries):
                link_path = f"{path}.link_button" + (f".{index}" if isinstance(links, list) else "")
                if not isinstance(link, dict):
                    _error(errors, link_path, "Must be an object.")
                    continue
                _reject_unknown(
                    link,
                    frozenset({"href", "icon_name", "title", "target", "css_class", "css_style", "locale"}),
                    link_path,
                    errors,
                )
                for key in ("href", "icon_name"):
                    if not isinstance(link.get(key), str) or not link[key].strip():
                        _error(errors, f"{link_path}.{key}", "Must be a non-empty string.")
    test = locale.get("test")
    if test is not None:
        if not isinstance(test, dict):
            _error(errors, f"{path}.test", "Must be an object.")
        else:
            _reject_unknown(test, frozenset({"quiet", "showSummary"}), f"{path}.test", errors)
            for key in ("quiet", "showSummary"):
                if key in test and not isinstance(test[key], bool):
                    _error(errors, f"{path}.test.{key}", "Must be true or false.")


def _validate_layer(key, layer, path, errors, available_dbs):
    if not isinstance(key, str) or not key.strip():
        _error(errors, path, "Layer key must be a non-empty string.")
    if not isinstance(layer, dict):
        _error(errors, path, "Layer must be an object.")
        return
    plugin_keys = {
        plugin["configurationKey"]
        for plugin in available_plugins()
        if "layer" in plugin["scope"]
    }
    _reject_unknown(layer, LAYER_KEYS | plugin_keys, path, errors)
    if "roles" in layer:
        _validate_roles(layer["roles"], f"{path}.roles", errors)
    has_template = (
        isinstance(layer.get("template"), str) and bool(layer["template"].strip())
    ) or isinstance(layer.get("template"), dict)
    if "template" in layer:
        _validate_template_reference(
            layer["template"], f"{path}.template", errors, available_dbs
        )
    if "templates" in layer:
        if not isinstance(layer["templates"], list):
            _error(errors, f"{path}.templates", "Must be an array of template references.")
        else:
            for index, reference in enumerate(layer["templates"]):
                _validate_template_reference(
                    reference, f"{path}.templates.{index}", errors, available_dbs
                )
    if "keyvalue_dictionary" in layer:
        _validate_keyvalue_dictionary(
            layer["keyvalue_dictionary"], f"{path}.keyvalue_dictionary", errors
        )
    if "gazetteer" in layer:
        _validate_gazetteer(layer["gazetteer"], f"{path}.gazetteer", errors)
    fmt = layer.get("format")
    if fmt not in SUPPORTED_FORMATS and not (fmt is None and has_template):
        _error(errors, f"{path}.format", f"Must be one of: {', '.join(sorted(SUPPORTED_FORMATS))}.")
    if "display" in layer and not isinstance(layer["display"], bool):
        _error(errors, f"{path}.display", "Must be true or false.")
    plugins = layer.get("plugins")
    if plugins is not None and not (
        isinstance(plugins, list)
        and all(isinstance(item, str) and item.strip() for item in plugins)
        and len(plugins) == len(set(plugins))
    ):
        _error(errors, f"{path}.plugins", "Must be an array of unique, non-empty plugin URLs.")
    viewport_count = layer.get("viewport_layer_count")
    if viewport_count is not None:
        if not isinstance(viewport_count, dict):
            _error(errors, f"{path}.viewport_layer_count", "Must be an object.")
        else:
            debounce = viewport_count.get("debounce")
            if debounce is not None and (
                not _number(debounce) or debounce < 0 or debounce > 5000
            ):
                _error(
                    errors,
                    f"{path}.viewport_layer_count.debounce",
                    "Must be a number from 0 to 5000.",
                )
    if "group" in layer and (
        not isinstance(layer["group"], str) or not layer["group"].strip()
    ):
        _error(errors, f"{path}.group", "Must be a non-empty string when set.")
    if "groupClassList" in layer and (
        not isinstance(layer["groupClassList"], str)
        or not CSS_CLASS_LIST.fullmatch(layer["groupClassList"])
    ):
        _error(
            errors,
            f"{path}.groupClassList",
            "Must be one or more space-separated stylesheet class names.",
        )
    if "groupClassList" in layer and not (
        isinstance(layer.get("group"), str) and layer["group"].strip()
    ):
        _error(
            errors,
            f"{path}.groupClassList",
            "Requires the layer to belong to a group.",
        )
    if fmt is None and has_template:
        _validate_style(layer.get("style"), f"{path}.style", errors)
        return
    if fmt == "tiles":
        has_uri = isinstance(layer.get("URI"), str) and layer["URI"].strip()
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
                    if "style" in entry and entry["style"] is not None and not isinstance(entry["style"], dict):
                        _error(errors, f"{entry_path}.style", "Must be an XYZ feature-style object or null.")
                    dashboard = entry.get("_dashboard")
                    if dashboard is not None and not isinstance(dashboard, dict):
                        _error(errors, f"{entry_path}._dashboard", "Must be an object.")
                    elif isinstance(dashboard, dict) and "styleFromLayerDefault" in dashboard and not isinstance(dashboard["styleFromLayerDefault"], bool):
                        _error(errors, f"{entry_path}._dashboard.styleFromLayerDefault", "Must be true or false.")
                    _validate_info_filter(
                        entry.get("filter"),
                        f"{entry_path}.filter",
                        errors,
                    )
                    if entry.get("fieldfx") and entry.get("filter") not in (None, False):
                        _error(
                            errors,
                            f"{entry_path}.filter",
                            (
                                "XYZ interactive filters must use real layer "
                                "columns; calculated fieldfx entries are only "
                                "safe for feature information."
                            ),
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


def _validate_keyvalue_dictionary(value, path, errors):
    if not isinstance(value, list):
        _error(errors, path, "Must be an array.")
        return
    for index, entry in enumerate(value):
        entry_path = f"{path}.{index}"
        if not isinstance(entry, dict):
            _error(errors, entry_path, "Must be an object.")
            continue
        for key in ("key", "value"):
            if not isinstance(entry.get(key), str) or (key == "key" and not entry[key]):
                _error(errors, f"{entry_path}.{key}", "Must be a non-empty string." if key == "key" else "Must be a string.")
        for key, item in entry.items():
            if not isinstance(item, str):
                _error(errors, f"{entry_path}.{key}", "Dictionary values must be strings.")


def _validate_gazetteer(value, path, errors):
    if not isinstance(value, dict):
        _error(errors, path, "Must be an object.")
        return
    _reject_unknown(
        value,
        frozenset({"provider", "maxZoom", "placeholder", "table", "qterm", "limit", "no_result", "datasets"}),
        path,
        errors,
    )
    datasets = value.get("datasets")
    if datasets is not None and not isinstance(datasets, list):
        _error(errors, f"{path}.datasets", "Must be an array.")
        return
    for index, dataset in enumerate(datasets or []):
        item_path = f"{path}.datasets.{index}"
        if not isinstance(dataset, dict):
            _error(errors, item_path, "Must be an object.")
            continue
        _reject_unknown(
            dataset,
            frozenset({"layer", "table", "qterm", "limit", "no_result", "title", "label", "query", "leading_wildcard"}),
            item_path,
            errors,
        )
        if "layer" in dataset and (
            not isinstance(dataset["layer"], str) or not dataset["layer"].strip()
        ):
            _error(errors, f"{item_path}.layer", "Must name a configured layer.")
        qterm = dataset.get("qterm")
        if not isinstance(qterm, str) or not IDENTIFIER.fullmatch(qterm):
            _error(errors, f"{item_path}.qterm", "Must be an unquoted column identifier.")
        table = dataset.get("table")
        if table is not None and (not isinstance(table, str) or not RELATION.fullmatch(table)):
            _error(errors, f"{item_path}.table", "Use table or schema.table.")
        limit = dataset.get("limit")
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            _error(errors, f"{item_path}.limit", "Must be a positive integer.")


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
    for key in ("count_meta", "viewport_description"):
        if key in value and (
            not isinstance(value[key], str) or not value[key].strip()
        ):
            _error(errors, f"{path}.{key}", "Must be a non-empty string.")
    default = value.get("default")
    if isinstance(default, str):
        if not default.strip():
            _error(errors, f"{path}.default", "Must be a non-empty predicate.")
        else:
            error = expression_error(default)
            if error:
                _error(errors, f"{path}.default", error)
    elif default is not None:
        _validate_fixed_layer_filter(default, f"{path}.default", errors)
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


def _validate_fixed_layer_filter(value, path, errors):
    """Validate the deterministic subset of XYZ's fixed-filter contract."""
    mappings = value if isinstance(value, list) else [value]
    if not mappings or not all(isinstance(item, dict) and item for item in mappings):
        _error(
            errors,
            path,
            "Must be a non-empty filter object or OR-array of filter objects.",
        )
        return
    supported = {
        "eq", "gt", "gte", "lt", "lte", "boolean", "null", "in", "ni",
        "like", "match",
    }
    for mapping_index, mapping in enumerate(mappings):
        mapping_path = (
            f"{path}.{mapping_index}" if isinstance(value, list) else path
        )
        for field, tests in mapping.items():
            field_path = f"{mapping_path}.{field}"
            if not isinstance(field, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", field
            ):
                _error(errors, field_path, "Must use an unquoted field identifier.")
                continue
            if not isinstance(tests, dict) or not tests:
                _error(
                    errors,
                    field_path,
                    "Must be a non-empty object of supported filter operations.",
                )
                continue
            for operation, operand in tests.items():
                operation_path = f"{field_path}.{operation}"
                if operation not in supported:
                    _error(errors, operation_path, "Unsupported fixed-filter operation.")
                elif operation in {"boolean", "null"} and not isinstance(
                    operand, bool
                ):
                    _error(errors, operation_path, "Must be true or false.")
                elif operation in {"like", "match"} and (
                    not isinstance(operand, str) or not operand
                ):
                    _error(errors, operation_path, "Must be a non-empty string.")
                elif operation in {"in", "ni"}:
                    values = operand if isinstance(operand, list) else [operand]
                    if not values or any(
                        item is None
                        or isinstance(item, (dict, list))
                        or not isinstance(item, (str, int, float, bool))
                        or isinstance(item, float) and not math.isfinite(item)
                        for item in values
                    ):
                        _error(
                            errors,
                            operation_path,
                            "Must be a non-empty scalar or array of finite scalars.",
                        )
                elif operation in {"eq", "gt", "gte", "lt", "lte"} and (
                    operand is None
                    or isinstance(operand, (dict, list, bool))
                    or not isinstance(operand, (str, int, float))
                    or isinstance(operand, str) and (
                        not operand
                        or operand != operand.strip()
                        or not _finite_number_string(operand)
                    )
                    or isinstance(operand, (int, float))
                    and not _finite_number(operand)
                ):
                    _error(
                        errors,
                        operation_path,
                        "Must be a finite number or numeric string.",
                    )
                elif operation == "like":
                    try:
                        decoded = unquote(operand, errors="strict")
                    except UnicodeDecodeError:
                        decoded = ""
                    if (
                        re.search(r"%(?![0-9A-Fa-f]{2})", operand)
                        or not any(decoded.split(","))
                    ):
                        _error(
                            errors,
                            operation_path,
                            "Must contain valid non-empty UTF-8 URL-encoded text.",
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
    themes = style.get("themes")
    if themes is not None:
        if not isinstance(themes, dict) or not themes:
            _error(errors, f"{path}.themes", "Must be a non-empty object keyed by theme name.")
        else:
            for key, theme in themes.items():
                if not isinstance(key, str) or not key.strip():
                    _error(errors, f"{path}.themes", "Theme names must be non-empty strings.")
                    continue
                _validate_theme(
                    theme,
                    f"{path}.themes.{key}",
                    errors,
                    style.get("default"),
                )
    theme = style.get("theme")
    if isinstance(theme, str):
        if not theme.strip():
            _error(errors, f"{path}.theme", "Must name a configured style.themes entry.")
        elif not isinstance(themes, dict) or theme not in themes:
            _error(errors, f"{path}.theme", f"Unknown named theme: {theme}.")
    elif theme is not None:
        _validate_theme(theme, f"{path}.theme", errors, style.get("default"))

    for state in ("default", "highlight", "selected", "cluster"):
        value = style.get(state)
        if value is None:
            continue
        _validate_feature_style(value, f"{path}.{state}", errors)


def _validate_theme(theme, path, errors, default_style=None):
    if not isinstance(theme, dict):
        _error(errors, path, "Must be an XYZ theme object.")
        return
    theme_type = theme.get("type")
    if theme_type not in {"basic", "categorized", "graduated", "distributed"}:
        _error(errors, f"{path}.type", "Must be basic, categorized, graduated, or distributed.")
        return
    for key in ("title", "label"):
        if key in theme and (not isinstance(theme[key], str) or not theme[key].strip()):
            _error(errors, f"{path}.{key}", "Must be a non-empty string.")

    if theme_type == "basic":
        if not isinstance(theme.get("style"), dict):
            _error(errors, f"{path}.style", "A basic theme requires a feature-style object.")
        else:
            _validate_feature_style(theme["style"], f"{path}.style", errors)
        if not isinstance(theme.get("label"), str) or not theme["label"].strip():
            _error(errors, f"{path}.label", "A basic theme requires a non-empty legend label.")
        return

    field = theme.get("field")
    fields = theme.get("fields")
    if theme_type == "categorized" and fields is not None:
        usable_fields = (
            fields
            if isinstance(fields, list)
            and fields
            and all(isinstance(item, str) and IDENTIFIER.fullmatch(item) for item in fields)
            else None
        )
        if not (
            usable_fields is not None and len(fields) == len(set(fields))
        ):
            _error(errors, f"{path}.fields", "Must contain unique database field identifiers.")
        fields = usable_fields
        if field is not None:
            _error(errors, f"{path}.field", "Use either field or fields for a categorized theme, not both.")
    elif not isinstance(field, str) or not IDENTIFIER.fullmatch(field):
        if theme_type == "distributed" and field is None:
            pass  # XYZ deliberately defaults distributed themes to the stable id field.
        else:
            _error(errors, f"{path}.field", "Must select a database field identifier.")

    categories = theme.get("categories")
    if not isinstance(categories, list) or not categories:
        _error(errors, f"{path}.categories", "Must contain at least one category.")
        return

    seen = set()
    graduated_values = []
    for index, category in enumerate(categories):
        category_path = f"{path}.categories.{index}"
        if not isinstance(category, dict):
            _error(errors, category_path, "Must be a category object.")
            continue
        if "label" in category and not (
            isinstance(category["label"], (str, int, float))
            and not isinstance(category["label"], bool)
            and (not isinstance(category["label"], str) or category["label"].strip())
        ):
            _error(errors, f"{category_path}.label", "Must be a non-empty string or number.")
        if theme_type in {"categorized", "graduated"} and "value" not in category:
            _error(errors, f"{category_path}.value", "A category requires an exact value.")
        elif "value" in category and isinstance(category["value"], (dict, list)):
            _error(errors, f"{category_path}.value", "Must be a scalar feature value.")

        category_field = category.get("field")
        if fields is not None:
            if category_field not in fields:
                _error(errors, f"{category_path}.field", "Must match one of the theme fields.")
        elif category_field is not None and (
            not isinstance(category_field, str) or not IDENTIFIER.fullmatch(category_field)
        ):
            _error(errors, f"{category_path}.field", "Must be a database field identifier.")

        value = category.get("value")
        duplicate_key = (category_field if fields is not None else None, type(value).__name__, repr(value))
        if "value" in category and duplicate_key in seen:
            _error(errors, f"{category_path}.value", "Duplicate category value for this field.")
        seen.add(duplicate_key)

        category_style = category.get("style")
        if category_style is None and "icon" in category:
            category_style = {"icon": category["icon"]}
        if category_style is not None:
            _validate_feature_style(category_style, f"{category_path}.style", errors)

        if fields is not None and (
            not isinstance(category_style, dict) or "icon" not in category_style
        ):
            _error(errors, f"{category_path}.style.icon", "Multi-field categories require an icon style.")
        elif theme_type == "distributed" and not isinstance(category_style, dict):
            _error(errors, f"{category_path}.style", "Distributed categories require a feature style.")
        elif (
            category_style is None
            and "style" not in category
            and "icon" not in category
            and not isinstance(default_style, dict)
        ):
            _error(errors, f"{category_path}.style", "Provide a category style or a layer default style.")

        if theme_type == "graduated":
            if not _number(value):
                _error(errors, f"{category_path}.value", "Graduated category breaks must be numbers.")
            else:
                graduated_values.append(value)

    if theme_type == "graduated":
        breaks = theme.get("graduated_breaks")
        if breaks not in {"less_than", "greater_than"}:
            _error(errors, f"{path}.graduated_breaks", "Must be less_than or greater_than.")
        if len(graduated_values) == len(categories):
            expected = sorted(graduated_values, reverse=breaks == "greater_than")
            if graduated_values != expected or len(graduated_values) != len(set(graduated_values)):
                _error(
                    errors,
                    f"{path}.categories",
                    f"Break values must be unique and ordered for {breaks}.",
                )


def _validate_feature_style(value, path, errors):
    if not isinstance(value, dict):
        _error(errors, path, "Must be an object.")
        return
    for key in ("fillColor", "strokeColor"):
        if key in value and (not isinstance(value[key], str) or not COLOR.fullmatch(value[key])):
            _error(errors, f"{path}.{key}", "Must be a three-, four-, six-, or eight-digit hex color.")
    for key, low, high in (("fillOpacity", 0, 1), ("strokeOpacity", 0, 1), ("strokeWidth", 0, 20), ("scale", 0.1, 10)):
        if key in value and (not _number(value[key]) or not low <= value[key] <= high):
            _error(errors, f"{path}.{key}", f"Must be a number from {low} to {high}.")
    if "lineDash" in value and not (
        isinstance(value["lineDash"], list)
        and value["lineDash"]
        and all(_number(item) and item >= 0 for item in value["lineDash"])
    ):
        _error(errors, f"{path}.lineDash", "Must be a non-empty array of non-negative numbers.")
    icon = value.get("icon")
    if icon is None:
        return
    icons = icon if isinstance(icon, list) else [icon]
    if not icons or not all(isinstance(item, dict) for item in icons):
        _error(errors, f"{path}.icon", "Must be one XYZ icon object or a non-empty icon array.")
        return
    for index, item in enumerate(icons):
        icon_path = f"{path}.icon.{index}" if isinstance(icon, list) else f"{path}.icon"
        if item.get("type") not in ICON_TYPES and not item.get("url"):
            _error(errors, f"{icon_path}.type", f"Must be an XYZ v4.23.4 symbol: {', '.join(sorted(ICON_TYPES))}.")
        for key in ("fillColor", "strokeColor", "color", "colorMarker", "colorDot"):
            if key in item and (not isinstance(item[key], str) or not COLOR.fullmatch(item[key])):
                _error(errors, f"{icon_path}.{key}", "Must be a three-, four-, six-, or eight-digit hex color.")
        for key, low, high in (("strokeWidth", 0, 20), ("scale", 0.1, 10)):
            if key in item and (not _number(item[key]) or not low <= item[key] <= high):
                _error(errors, f"{icon_path}.{key}", f"Must be a number from {low} to {high}.")
        if item.get("type") == "markerLetter" and (
            not isinstance(item.get("letter"), str) or len(item["letter"]) != 1
        ):
            _error(errors, f"{icon_path}.letter", "Must be exactly one character.")
