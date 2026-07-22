"""Discover trusted, manifest-backed XYZ browser plugins without importing them."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

def _default_plugin_root() -> Path:
    candidates = (
        Path("/instance-public/plugins"),
        Path("/workspace/instance/public/plugins"),
        Path(__file__).resolve().parent.parent / "instance/public/plugins",
    )
    return next((path for path in candidates if path.is_dir()), candidates[-1])


PLUGIN_ROOT = Path(os.environ.get("PLUGIN_DIR", str(_default_plugin_root())))
XYZ_VERSION = os.environ.get("XYZ_VERSION", "v4.23.4").removeprefix("v")
XYZ_COMMIT = os.environ.get(
    "XYZ_COMMIT", "a6f03c07dd7aaae2e9ab04087143ee0400e15cb9"
)
ID = re.compile(r"^[a-z][a-z0-9-]*$")
KEY = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
ALLOWED_ASSERTIONS = {
    "registration", "locale-dispatch", "layer-dispatch", "selector-exists",
    "selector-visible", "no-plugin-console-errors",
}
BUILTIN_KEYS = {
    "admin", "consent", "custom_theme", "dark_mode", "feature_info",
    "fullscreen", "layer_order", "link_button", "locator", "login",
    "svg_templates", "test", "userIDB", "userLayer", "userLocale",
    "zoomBtn", "zoomToArea",
}


def _version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value)
    return tuple(map(int, match.groups())) if match else None


def compatible(spec: str, current: str = XYZ_VERSION) -> bool:
    """Evaluate the deliberately small range grammar used by plugin manifests."""
    actual = _version(current)
    if not actual or not isinstance(spec, str) or not spec.strip():
        return False
    for token in spec.split():
        match = re.fullmatch(r"(>=|<=|>|<|=|\^|~)?(v?\d+\.\d+\.\d+)", token)
        if not match:
            return False
        operator, raw = match.groups()
        wanted = _version(raw)
        if not wanted:
            return False
        if operator in (None, "=") and actual != wanted:
            return False
        if operator == ">=" and actual < wanted:
            return False
        if operator == "<=" and actual > wanted:
            return False
        if operator == ">" and actual <= wanted:
            return False
        if operator == "<" and actual >= wanted:
            return False
        if operator == "^" and not (actual >= wanted and actual < (wanted[0] + 1, 0, 0)):
            return False
        if operator == "~" and not (actual >= wanted and actual < (wanted[0], wanted[1] + 1, 0)):
            return False
    return True


def _closed_schema(schema: Any, path: str = "configurationSchema") -> list[str]:
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path} must be a JSON Schema object."]
    schema_type = schema.get("type")
    if path == "configurationSchema" and schema_type != "object":
        errors.append(f"{path} must have type object.")
    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            errors.append(f"{path}.additionalProperties must be false.")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            errors.append(f"{path}.properties must be an object.")
        else:
            for key, child in properties.items():
                errors.extend(_closed_schema(child, f"{path}.properties.{key}"))
    elif schema_type == "array":
        errors.extend(_closed_schema(schema.get("items"), f"{path}.items"))
    elif schema_type not in {"string", "number", "integer", "boolean", "null"}:
        errors.append(f"{path}.type is unsupported.")
    return errors


def _contained_file(directory: Path, relative: Any, suffixes: set[str]) -> tuple[Path | None, str | None]:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None, "must be a non-empty relative path."
    candidate = directory / relative
    try:
        resolved = candidate.resolve(strict=True)
        root = directory.resolve(strict=True)
    except OSError:
        return None, "does not exist."
    if candidate.is_symlink() or root not in resolved.parents or not resolved.is_file():
        return None, "must be a regular non-symlink file inside the plugin directory."
    if resolved.suffix.lower() not in suffixes:
        return None, f"must use one of: {', '.join(sorted(suffixes))}."
    return resolved, None


def _entry(directory: Path, manifest_path: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"directory": directory.name, "available": False, "diagnostics": [f"plugin.json is invalid: {type(exc).__name__}."]}
    if not isinstance(manifest, dict):
        return {"directory": directory.name, "available": False, "diagnostics": ["plugin.json must contain an object."]}
    required = {
        "id", "name", "version", "xyzVersion", "entry", "registrationKey",
        "scope", "dispatch", "configurationKey", "configurationSchema",
        "summary", "prerequisites", "previewAssertions", "documentation",
    }
    allowed = required | {"dependencies", "aliases"}
    errors.extend(f"Missing required field: {key}." for key in sorted(required - manifest.keys()))
    errors.extend(f"Unsupported manifest field: {key}." for key in sorted(manifest.keys() - allowed))
    plugin_id = manifest.get("id")
    registration = manifest.get("registrationKey")
    config_key = manifest.get("configurationKey")
    if not isinstance(plugin_id, str) or not ID.fullmatch(plugin_id):
        errors.append("id must use lowercase letters, digits, and hyphens.")
    if not isinstance(registration, str) or not KEY.fullmatch(registration):
        errors.append("registrationKey must be a JavaScript identifier.")
    if not isinstance(config_key, str) or not KEY.fullmatch(config_key):
        errors.append("configurationKey must be a workspace property identifier.")
    if _version(str(manifest.get("version", ""))) is None:
        errors.append("version must be semantic x.y.z form.")
    for field in ("name", "summary"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            errors.append(f"{field} must be a non-empty string.")
    if not isinstance(manifest.get("documentation"), dict) or not manifest["documentation"]:
        errors.append("documentation must be a non-empty object.")
    xyz_range = manifest.get("xyzVersion")
    is_compatible = compatible(xyz_range) if isinstance(xyz_range, str) else False
    if not is_compatible:
        errors.append(f"xyzVersion does not include pinned XYZ {XYZ_VERSION}.")
    scopes = manifest.get("scope")
    scopes = [scopes] if isinstance(scopes, str) else scopes
    if not isinstance(scopes, list) or not scopes or any(item not in {"locale", "layer"} for item in scopes):
        errors.append("scope must contain locale and/or layer.")
        scopes = []
    dispatch = manifest.get("dispatch")
    dispatch = [dispatch] if isinstance(dispatch, str) else dispatch
    if not isinstance(dispatch, list) or not dispatch or any(item not in {"locale", "layer", "sync"} for item in dispatch):
        errors.append("dispatch must contain locale, layer, and/or sync.")
        dispatch = []
    if "layer" in scopes and "layer" not in dispatch:
        errors.append("Layer-scoped plugins must declare layer dispatch.")
    if "locale" in scopes and not ({"locale", "sync"} & set(dispatch)):
        errors.append("Locale-scoped plugins must declare locale or sync dispatch.")
    errors.extend(_closed_schema(manifest.get("configurationSchema")))
    prerequisites = manifest.get("prerequisites")
    if not isinstance(prerequisites, list) or any(not isinstance(item, str) for item in prerequisites):
        errors.append("prerequisites must be an array of strings.")
    dependencies = manifest.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not ID.fullmatch(item) for item in dependencies):
        errors.append("dependencies must be an array of plugin IDs.")
        dependencies = []
    assertions = manifest.get("previewAssertions")
    if not isinstance(assertions, list):
        errors.append("previewAssertions must be an array.")
        assertions = []
    else:
        for index, assertion in enumerate(assertions):
            if not isinstance(assertion, dict) or assertion.get("type") not in ALLOWED_ASSERTIONS:
                errors.append(f"previewAssertions[{index}] has an unsupported type.")
            elif assertion["type"].startswith("selector-") and not isinstance(assertion.get("selector"), str):
                errors.append(f"previewAssertions[{index}].selector is required.")
    entry_path, entry_error = _contained_file(directory, manifest.get("entry"), {".js", ".mjs"})
    if entry_error:
        errors.append(f"entry {entry_error}")
    aliases = manifest.get("aliases", [])
    alias_files: list[tuple[str, Path]] = []
    if not isinstance(aliases, list) or any(not isinstance(item, str) or not item.startswith("/instance/plugins/") for item in aliases):
        errors.append("aliases must be /instance/plugins/ URL strings.")
        aliases = []
    else:
        for alias in aliases:
            relative_alias = alias.removeprefix("/instance/plugins/")
            alias_path, alias_error = _contained_file(PLUGIN_ROOT, relative_alias, {".js", ".mjs"})
            if alias_error:
                errors.append(f"alias {alias} {alias_error}")
            elif alias_path:
                alias_files.append((alias, alias_path))
    files: list[dict[str, str]] = []
    try:
        for asset in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
            if asset.is_symlink():
                errors.append(f"Plugin packages must not contain symlinks: {asset.relative_to(directory)}.")
            elif asset.is_file() and asset.name != "plugin.json":
                files.append({
                    "path": asset.relative_to(directory).as_posix(),
                    "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                })
    except OSError as exc:
        errors.append(f"Plugin assets could not be hashed: {type(exc).__name__}.")
    for alias, alias_path in alias_files:
        files.append({
            "path": alias,
            "sha256": hashlib.sha256(alias_path.read_bytes()).hexdigest(),
        })
    normalized = copy.deepcopy(manifest)
    return {
        **normalized,
        "scope": scopes,
        "dispatch": dispatch,
        "dependencies": dependencies,
        "aliases": aliases,
        "entryUrl": f"/instance/plugins/{directory.name}/{manifest.get('entry', '')}",
        "files": files,
        "compatible": is_compatible,
        "available": not errors,
        "diagnostics": errors,
        "source": "external",
    }


_CACHE_SIGNATURE: tuple | None = None
_CACHE_VALUE: dict[str, Any] | None = None
_CACHE_CHECKED = 0.0


def _signature() -> tuple:
    if not PLUGIN_ROOT.is_dir():
        return (str(PLUGIN_ROOT), "missing")
    values = []
    for path in sorted(PLUGIN_ROOT.rglob("*"), key=lambda item: item.as_posix()):
        try:
            stat = path.lstat()
            values.append((path.relative_to(PLUGIN_ROOT).as_posix(), stat.st_mode, stat.st_size, stat.st_mtime_ns))
        except OSError:
            values.append((path.as_posix(), "unreadable"))
    return (str(PLUGIN_ROOT), *values)


def _catalogue() -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    if PLUGIN_ROOT.is_dir() and not PLUGIN_ROOT.is_symlink():
        for directory in sorted(PLUGIN_ROOT.iterdir(), key=lambda item: item.name):
            if directory.is_dir() and not directory.is_symlink() and (directory / "plugin.json").is_file():
                entries.append(_entry(directory, directory / "plugin.json"))
    seen_ids: dict[str, int] = {}
    seen_keys: dict[str, int] = {}
    seen_urls: dict[str, int] = {}
    for index, entry in enumerate(entries):
        for field, seen in (("id", seen_ids), ("registrationKey", seen_keys)):
            value = entry.get(field)
            if not isinstance(value, str):
                continue
            if value in seen:
                for target in (entries[seen[value]], entry):
                    target["available"] = False
                    target["diagnostics"].append(f"Duplicate {field}: {value}.")
            else:
                seen[value] = index
        for url in [entry.get("entryUrl"), *entry.get("aliases", [])]:
            if not isinstance(url, str):
                continue
            if url in seen_urls:
                for target in (entries[seen_urls[url]], entry):
                    target["available"] = False
                    target["diagnostics"].append(f"Duplicate plugin URL or alias: {url}.")
            else:
                seen_urls[url] = index
    try:
        base = json.loads((Path(__file__).parent / "schema/workspace.schema.json").read_text(encoding="utf-8"))
        reserved = {
            scope: set(base["$defs"][scope]["properties"])
            for scope in ("locale", "layer")
        }
    except (OSError, KeyError, json.JSONDecodeError):
        reserved = {"locale": set(), "layer": set()}
    for entry in entries:
        if entry.get("registrationKey") in BUILTIN_KEYS or entry.get("configurationKey") in BUILTIN_KEYS:
            entry["available"] = False
            entry["diagnostics"].append("Plugin key collides with the pinned XYZ bundled registry.")
        for scope in entry.get("scope", []):
            if entry.get("configurationKey") in reserved[scope]:
                entry["available"] = False
                entry["diagnostics"].append(
                    f"configurationKey collides with the pinned {scope} schema."
                )
    ids = {entry.get("id") for entry in entries if entry.get("available")}
    for entry in entries:
        missing = sorted(set(entry.get("dependencies", [])) - ids)
        if missing:
            entry["available"] = False
            entry["diagnostics"].append(f"Missing dependencies: {', '.join(missing)}.")
    digestable = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {
        "xyzVersion": XYZ_VERSION,
        "xyzCommit": XYZ_COMMIT,
        "fingerprint": hashlib.sha256(digestable).hexdigest(),
        "external": entries,
        "valid": all(entry.get("available") for entry in entries),
    }


def catalogue() -> dict[str, Any]:
    global _CACHE_SIGNATURE, _CACHE_VALUE, _CACHE_CHECKED
    now = time.monotonic()
    if (
        _CACHE_VALUE is not None
        and _CACHE_SIGNATURE
        and _CACHE_SIGNATURE[0] == str(PLUGIN_ROOT)
        and now - _CACHE_CHECKED < 0.25
    ):
        return copy.deepcopy(_CACHE_VALUE)
    signature = _signature()
    if signature != _CACHE_SIGNATURE or _CACHE_VALUE is None:
        _CACHE_VALUE = _catalogue()
        _CACHE_SIGNATURE = signature
    _CACHE_CHECKED = now
    return copy.deepcopy(_CACHE_VALUE)


def available_plugins() -> list[dict[str, Any]]:
    return [entry for entry in catalogue()["external"] if entry.get("available")]


def composed_schema(base: dict[str, Any]) -> dict[str, Any]:
    output = copy.deepcopy(base)
    for plugin in available_plugins():
        for scope in plugin["scope"]:
            definition = output["$defs"][scope]
            definition["properties"][plugin["configurationKey"]] = copy.deepcopy(plugin["configurationSchema"])
    urls = sorted({url for plugin in available_plugins() for url in [plugin["entryUrl"], *plugin["aliases"]]})
    for scope in ("locale", "layer"):
        output["$defs"][scope]["properties"]["plugins"]["items"] = {"type": "string", "enum": urls}
    return output


def _validate_schema(value: Any, schema: dict[str, Any], path: str, errors: list[dict[str, str]]) -> None:
    if "enum" in schema and value not in schema["enum"]:
        errors.append({"path": path, "message": "Must be one of the values declared by the plugin schema."})
        return
    if schema.get("type") == "object":
        if not isinstance(value, dict):
            errors.append({"path": path, "message": "Must be an object."})
            return
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                errors.append({"path": f"{path}.{required}", "message": "Property is required."})
        for key, item in value.items():
            if key not in properties:
                errors.append({"path": f"{path}.{key}", "message": "Property is not supported by the installed plugin contract."})
            else:
                _validate_schema(item, properties[key], f"{path}.{key}", errors)
    elif schema.get("type") == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        errors.append({"path": path, "message": "Must be a number."})
    elif schema.get("type") == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        errors.append({"path": path, "message": "Must be an integer."})
    elif schema.get("type") == "string" and not isinstance(value, str):
        errors.append({"path": path, "message": "Must be a string."})
    elif schema.get("type") == "boolean" and not isinstance(value, bool):
        errors.append({"path": path, "message": "Must be true or false."})
    elif schema.get("type") == "array":
        if not isinstance(value, list):
            errors.append({"path": path, "message": "Must be an array."})
        else:
            for index, item in enumerate(value):
                _validate_schema(item, schema.get("items", {}), f"{path}.{index}", errors)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append({"path": path, "message": f"Must be at least {schema['minimum']}."})
        if "maximum" in schema and value > schema["maximum"]:
            errors.append({"path": path, "message": f"Must be at most {schema['maximum']}."})
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errors.append({"path": path, "message": f"Must contain at least {schema['minLength']} characters."})


def validate_workspace_plugins(workspace: Any) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    plugins = available_plugins()
    by_url = {url: plugin for plugin in plugins for url in [plugin["entryUrl"], *plugin["aliases"]]}
    by_key = {plugin["configurationKey"]: plugin for plugin in plugins}
    locales: list[tuple[str, Any]] = [("locale", workspace.get("locale"))]
    locales.extend((f"locales.{key}", value) for key, value in (workspace.get("locales") or {}).items())
    for locale_path, locale in locales:
        if not isinstance(locale, dict):
            continue
        objects = [("locale", locale_path, locale)]
        objects.extend(("layer", f"{locale_path}.layers.{key}", layer) for key, layer in (locale.get("layers") or {}).items())
        for scope, path, obj in objects:
            if not isinstance(obj, dict):
                continue
            sources = obj.get("plugins", [])
            configured = set(sources) if isinstance(sources, list) else set()
            for index, source in enumerate(sources if isinstance(sources, list) else []):
                plugin = by_url.get(source)
                if not plugin:
                    errors.append({"path": f"{path}.plugins.{index}", "message": "Plugin URL is not in the installed catalogue."})
                elif scope not in plugin["scope"]:
                    errors.append({"path": f"{path}.plugins.{index}", "message": f"Plugin does not support {scope} scope."})
                else:
                    dependency_plugins = [p for p in plugins if p["id"] in plugin["dependencies"]]
                    missing = [
                        dep for dep in plugin["dependencies"]
                        if not any(
                            candidate["id"] == dep
                            and {candidate["entryUrl"], *candidate["aliases"]} & configured
                            for candidate in plugins
                        )
                    ]
                    if missing:
                        errors.append({"path": f"{path}.plugins.{index}", "message": f"Plugin dependencies must be explicitly configured first: {', '.join(missing)}."})
                    for dependency in dependency_plugins:
                        dependency_indexes = [
                            position for position, item in enumerate(sources)
                            if item in {dependency["entryUrl"], *dependency["aliases"]}
                        ]
                        if dependency_indexes and dependency_indexes[0] >= index:
                            errors.append({"path": f"{path}.plugins.{index}", "message": f"Plugin dependency {dependency['id']} must appear earlier in the plugins array."})
            for key, plugin in by_key.items():
                if key not in obj:
                    continue
                if scope not in plugin["scope"]:
                    errors.append({"path": f"{path}.{key}", "message": f"Plugin configuration is not valid at {scope} scope."})
                    continue
                if not ({plugin["entryUrl"], *plugin["aliases"]} & configured):
                    errors.append({"path": f"{path}.{key}", "message": "Plugin configuration requires its module URL in the same plugins array."})
                _validate_schema(obj[key], plugin["configurationSchema"], f"{path}.{key}", errors)
        sync = locale.get("syncPlugins")
        if isinstance(sync, list):
            allowed = {plugin["registrationKey"] for plugin in plugins if "sync" in plugin["dispatch"]}
            for index, key in enumerate(sync):
                if key not in allowed and key not in {"svg_templates"}:
                    # Bundled sync handling remains validated by the pinned contract.
                    external_keys = {plugin["registrationKey"] for plugin in plugins}
                    if key in external_keys:
                        errors.append({"path": f"{locale_path}.syncPlugins.{index}", "message": "Installed plugin does not declare synchronous dispatch."})
    return errors


def plugin_usage(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for plugin in available_plugins():
        urls = {plugin["entryUrl"], *plugin["aliases"]}
        for locale_path, locale in [("locale", workspace.get("locale")), *((f"locales.{key}", value) for key, value in (workspace.get("locales") or {}).items())]:
            if not isinstance(locale, dict):
                continue
            for scope, path, obj in [("locale", locale_path, locale), *(("layer", f"{locale_path}.layers.{key}", layer) for key, layer in (locale.get("layers") or {}).items())]:
                if isinstance(obj, dict) and urls & set(obj.get("plugins") or []):
                    output.append({"pluginId": plugin["id"], "scope": scope, "path": path, "configured": plugin["configurationKey"] in obj})
    return output
