from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg import sql
except ModuleNotFoundError:  # Allows pure contract/mutation tests without DB extras.
    psycopg = None
    sql = None


API_VERSION = "1.0"
CONTRACT_VERSION = "1.0"
RULES_VERSION = "1.0"
XYZ_VERSION = os.environ.get("XYZ_VERSION", "v4.23.4")
MODULE_ROOT = Path(__file__).parent
LOCAL_RUNTIME = Path(tempfile.gettempdir()) / "mapp-config"
SCHEMA_PATH = Path(
    os.environ.get(
        "WORKSPACE_SCHEMA_PATH",
        str(MODULE_ROOT / "schema/workspace.schema.json"),
    )
)
RELOAD_DIR = Path(os.environ.get("RELOAD_DIR", str(LOCAL_RUNTIME / "reload")))
ARTIFACT_DIR = Path(
    os.environ.get("ARTIFACT_DIR", str(LOCAL_RUNTIME / "control/artifacts"))
)
RELOAD_LOCK = threading.Lock()
PROPOSAL_LOCK = threading.RLock()

RULES = [
    {"id": "workspace.structure", "category": "schema", "description": "Workspace values must satisfy the supported XYZ structure.", "remediation": "Inspect `config-cli schema` and correct the reported path."},
    {"id": "workspace.catalog", "category": "catalog", "description": "Database-backed layers must use selectable relations and columns.", "remediation": "Use `config-cli catalog list` and select a reported table, geometry, and ID."},
    {"id": "workspace.feature_id", "category": "data", "description": "XYZ feature IDs must be non-null and unique.", "remediation": "Choose a primary or unique non-null column for qID."},
    {"id": "workspace.render", "category": "render", "description": "XYZ-equivalent bounded database reads must succeed.", "remediation": "Review the relation, SRID, expressions, and database permissions."},
    {"id": "sql.scalar_read_only", "category": "security", "description": "Calculated information values are one read-only scalar expression.", "remediation": "Remove statements, comments, subqueries, session functions, and system access."},
    {"id": "svg.safe", "category": "security", "description": "Dashboard SVGs must be bounded, parseable, and free of active content.", "remediation": "Use a safe SVG from `config-cli icons list`."},
    {"id": "proposal.revision", "category": "proposal", "description": "A proposal applies only to the revision from which it was created.", "remediation": "Create a new proposal against the current workspace."},
    {"id": "visual.data", "category": "visual", "description": "A database-backed visual test needs non-empty valid geometry.", "remediation": "Load data or provide an explicit centre and zoom."},
]
DATABASE_LAYER_FORMATS = {"cluster", "geojson", "mvt", "vector", "wkt"}


def contract(instance_id: str) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "rulesVersion": RULES_VERSION,
        "xyzVersion": XYZ_VERSION,
        "instanceId": instance_id,
        "authentication": {"type": "bearer", "tokens": "issued by config UI", "scopes": ["full"]},
        "commands": [
            "describe", "schema", "rules", "examples",
            "workspace get", "layers list", "layers get", "catalog list", "icons list",
            "validate", "set", "unset", "amend", "sql capabilities", "sql test",
            "visual-plan", "visual-test", "screenshot",
            "proposals create", "proposals show", "proposals list",
            "proposals apply", "proposals decline",
            "xyz status", "xyz reload",
            "auth status",
        ],
        "workflow": ["inspect", "propose", "validate", "review evidence", "approve", "apply", "reload", "verify"],
        "exitCodes": {"success": 0, "usage": 2, "validation": 3, "conflict": 4, "connectivity": 5, "visual": 6, "authentication": 7},
    }


def workspace_hash(workspace: dict) -> str:
    encoded = json.dumps(
        workspace,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def workspace_fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _reject_json_constant(value: str):
    raise ValueError(f"{value} is not valid JSON")


def strict_json_loads(raw: str | bytes) -> Any:
    return json.loads(raw, parse_constant=_reject_json_constant)


def _atomic_text(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def pointer_parts(pointer: str) -> list[str]:
    if not isinstance(pointer, str):
        raise ValueError("Workspace paths must be strings.")
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("Workspace paths must use RFC 6901 JSON Pointer syntax.")
    parts = []
    for part in pointer[1:].split("/"):
        if re.search(r"~(?:[^01]|$)", part):
            raise ValueError("Workspace path contains an invalid JSON Pointer escape.")
        parts.append(part.replace("~1", "/").replace("~0", "~"))
    return parts


def _list_index(part: str, length: int, *, allow_end: bool = False) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", part):
        raise ValueError(f"JSON Pointer array index is invalid: {part!r}.")
    index = int(part)
    maximum = length if allow_end else length - 1
    if index < 0 or index > maximum:
        raise ValueError(f"JSON Pointer array index is out of range: {part}.")
    return index


def _pointer_child(parent: Any, part: str) -> Any:
    if isinstance(parent, list):
        return parent[_list_index(part, len(parent))]
    if isinstance(parent, dict):
        if part not in parent:
            raise ValueError(f"JSON Pointer path does not exist at {part!r}.")
        return parent[part]
    raise ValueError("JSON Pointer cannot traverse through a scalar value.")


def pointer_get(document: Any, pointer: str) -> Any:
    value = document
    for part in pointer_parts(pointer):
        value = _pointer_child(value, part)
    return value


def apply_operations(document: dict, operations: list[dict]) -> tuple[dict, list[dict]]:
    if not isinstance(document, dict):
        raise ValueError("Workspace must be a JSON object.")
    if not isinstance(operations, list):
        raise ValueError("Operations must be an array.")
    candidate = copy.deepcopy(document)
    diff = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("Each workspace operation must be an object.")
        action = operation.get("op")
        path = operation.get("path")
        parts = pointer_parts(path)
        if not parts:
            raise ValueError("Replacing or deleting the workspace root is not supported.")
        parent: Any = candidate
        for part in parts[:-1]:
            parent = _pointer_child(parent, part)
        key = parts[-1]
        if action == "set":
            old = None
            exists = False
            submitted = copy.deepcopy(operation.get("value"))
            if isinstance(parent, list):
                index = _list_index(key, len(parent), allow_end=True)
                if index < len(parent):
                    old = copy.deepcopy(parent[index])
                    exists = True
                if index == len(parent):
                    parent.append(copy.deepcopy(submitted))
                else:
                    parent[index] = copy.deepcopy(submitted)
            elif isinstance(parent, dict):
                if key in parent:
                    old = copy.deepcopy(parent[key])
                    exists = True
                parent[key] = copy.deepcopy(submitted)
            else:
                raise ValueError("JSON Pointer parent must be an object or array.")
            diff.append({
                "op": "replace" if exists else "add",
                "path": path,
                "old": old,
                "value": copy.deepcopy(submitted),
            })
        elif action == "unset":
            if isinstance(parent, list):
                old = copy.deepcopy(
                    parent.pop(_list_index(key, len(parent)))
                )
            elif isinstance(parent, dict):
                if key not in parent:
                    raise ValueError(f"JSON Pointer path does not exist at {key!r}.")
                old = copy.deepcopy(parent.pop(key))
            else:
                raise ValueError("JSON Pointer parent must be an object or array.")
            diff.append({"op": "remove", "path": path, "old": old})
        else:
            raise ValueError(f"Unsupported operation: {action}")
    return candidate, diff


def schema(pointer: str | None = None) -> Any:
    data = strict_json_loads(SCHEMA_PATH.read_text())
    return pointer_get(data, pointer) if pointer else data


def examples() -> dict:
    return {
        "makeBusStopsBlue": {
            "operations": [{
                "op": "set",
                "path": "/locale/layers/Bus Stops/style/default/icon/fillColor",
                "value": "#2563eb",
            }],
            "explanation": "Changes only the Bus Stops default point-symbol fill colour.",
        },
        "workflow": [
            "config-cli describe",
            "config-cli layers get 'Bus Stops'",
            "config-cli proposals create --base-revision WORKSPACE_REVISION --set '/locale/layers/Bus Stops/style/default/icon/fillColor=\"#2563eb\"' --explanation 'Changes only the default Bus Stops fill colour.'",
            "config-cli proposals apply PROPOSAL_ID --confirm",
            "config-cli xyz status",
            "config-cli visual-test --layer 'Bus Stops'",
        ],
    }


def proposal_create(store, original: dict, original_revision: str, candidate: dict, operations: list, diff: list, actor: str, explanation: str | None = None) -> dict:
    proposal_id = (
        f"{int(time.time())}-{workspace_hash(candidate)[:12]}-"
        f"{secrets.token_hex(3)}"
    )
    proposal = {
        "id": proposal_id,
        "status": "pending",
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": actor,
        "originalRevision": original_revision,
        "originalHash": workspace_hash(original),
        "candidateHash": workspace_hash(candidate),
        "operations": operations,
        "diff": diff,
        "explanation": explanation or explain_diff(diff),
        "original": original,
        "candidate": candidate,
        "warnings": [
            "Visual tests render the live workspace; they do not preview this candidate before application."
        ],
    }
    path = store.proposals / proposal_id
    path.mkdir(mode=0o700)
    proposal_path = path / "proposal.json"
    _atomic_text(
        proposal_path,
        json.dumps(
            proposal,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
    )
    store.audit("proposal.created", actor=actor, details={"id": proposal_id, "candidateHash": proposal["candidateHash"]})
    return proposal


def explain_diff(diff: list[dict]) -> str:
    if len(diff) == 1:
        item = diff[0]
        return f"This proposal changes {item['path']} from {json.dumps(item.get('old'))} to {json.dumps(item.get('value'))}. All unrelated workspace properties are preserved."
    return f"This proposal makes {len(diff)} focused workspace changes and preserves unrelated properties."


def proposal_read(store, proposal_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", proposal_id):
        raise ValueError("Invalid proposal ID.")
    return strict_json_loads(
        (store.proposals / proposal_id / "proposal.json").read_text()
    )


def proposal_write(store, proposal: dict) -> None:
    path = store.proposals / proposal["id"] / "proposal.json"
    _atomic_text(
        path,
        json.dumps(
            proposal,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
    )


def proposal_list(store) -> list[dict]:
    output = []
    for path in sorted(store.proposals.glob("*/proposal.json"), reverse=True):
        item = strict_json_loads(path.read_text())
        output.append({key: item.get(key) for key in ("id", "status", "created", "actor", "explanation", "originalRevision", "candidateHash")})
    return output


def request_reload(expected_fingerprint: str | None = None) -> dict:
    if expected_fingerprint is not None and (
        not isinstance(expected_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint)
    ):
        raise ValueError(
            "Workspace fingerprint must be a lowercase SHA-256 digest."
        )
    with RELOAD_LOCK:
        RELOAD_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = RELOAD_DIR / ".request.lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            request_path = RELOAD_DIR / "requested"
            try:
                current = int(request_path.read_text().strip() or "0")
            except (FileNotFoundError, OSError, ValueError):
                current = 0
            generation = current + 1
            _atomic_text(
                RELOAD_DIR / "expected-workspace",
                (expected_fingerprint or "") + "\n",
            )
            # Publish the generation last so the supervisor cannot observe a
            # new request before the associated metadata is complete.
            _atomic_text(request_path, f"{generation}\n")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
    return {"requestedGeneration": generation, "expectedWorkspaceFingerprint": expected_fingerprint}


def reload_status() -> dict:
    def read(name: str, default: str = "0") -> str:
        path = RELOAD_DIR / name
        try:
            return path.read_text().strip()
        except (FileNotFoundError, OSError, UnicodeError):
            return default

    def generation(name: str) -> int:
        try:
            return int(read(name))
        except ValueError:
            return 0

    return {
        "requestedGeneration": generation("requested"),
        "appliedGeneration": generation("applied"),
        "workspaceFingerprint": read("workspace-fingerprint", ""),
        "startedAt": read("started-at", ""),
        "healthy": read("healthy", "false") == "true",
    }


def reload_timeout(value: Any = 30) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.1 <= value <= 120
    ):
        raise ValueError(
            "Reload timeout must be a finite number from 0.1 to 120 seconds."
        )
    return float(value)


def wait_reload(generation: int, fingerprint: str | None = None, timeout: float = 30) -> dict:
    timeout = reload_timeout(timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = reload_status()
        if (
            status["appliedGeneration"] >= generation
            and status["healthy"]
            and (not fingerprint or status["workspaceFingerprint"] == fingerprint)
        ):
            return {**status, "completed": True}
        time.sleep(.25)
    return {**reload_status(), "completed": False, "timeoutSeconds": timeout}


def apply_visual_override(plan: dict, payload: dict) -> dict:
    override = {}
    if "centre" in payload:
        centre = payload["centre"]
        if not isinstance(centre, list) or len(centre) != 2:
            raise ValueError("Visual centre must be [longitude, latitude].")
        lng, lat = centre
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (lng, lat)
        ):
            raise ValueError("Visual centre values must be finite numbers.")
        if not -180 <= lng <= 180 or not -90 <= lat <= 90:
            raise ValueError("Visual centre is outside longitude/latitude bounds.")
        override["centre"] = [lng, lat]
    if "zoom" in payload:
        zoom = payload["zoom"]
        if (
            isinstance(zoom, bool)
            or not isinstance(zoom, (int, float))
            or not math.isfinite(zoom)
            or not 0 <= zoom <= 22
        ):
            raise ValueError("Visual zoom must be a finite number from 0 to 22.")
        override["zoom"] = zoom
    if not override:
        return plan
    return {
        **plan,
        **override,
        "baseSource": plan.get("source"),
        "source": "explicit-view",
    }


def is_probeable_database_layer(layer: Any) -> bool:
    """Return true only for the concrete relation form we can safely probe.

    XYZ also supports templates, inline features, external renderers, and
    zoom-keyed table/geometry maps. Those valid advanced forms are preserved
    but cannot be represented by one bounded relation probe.
    """
    return (
        isinstance(layer, dict)
        and layer.get("format") in DATABASE_LAYER_FORMATS
        and not isinstance(layer.get("template"), str)
        and not isinstance(layer.get("features"), list)
        and not isinstance(layer.get("tables"), dict)
        and not isinstance(layer.get("geoms"), dict)
        and isinstance(layer.get("table"), str)
        and isinstance(layer.get("geom"), str)
        and isinstance(layer.get("qID"), str)
    )


def _xyz_array_includes(values: list, item: Any) -> bool:
    """Model JavaScript Array.includes for strict JSON values.

    XYZ's merge helper compares array entries with JavaScript identity
    semantics. Object and array entries from the independent source and target
    documents therefore never count as already included; scalar JSON values
    use type-aware value equality.
    """
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
    # JavaScript truthiness for values that can occur in strict JSON.
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return bool(value)
    return True


def deep_merge(base: Any, override: Any) -> Any:
    """Compose values with the pinned XYZ ``mod/utils/merge.js`` rules."""
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
                merged[key] = deep_merge(current, value)
            elif key not in merged or not _xyz_truthy(current):
                merged[key] = deep_merge({}, value)
            # XYZ leaves a truthy scalar or array target unchanged when the
            # corresponding source value is an object.
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def effective_locales(workspace: dict) -> dict[str, dict]:
    base = workspace.get("locale")
    named = workspace.get("locales")
    # XYZ cache.js synthesizes an empty default locale when the raw workspace
    # omits one, then uses it as the prototype for named alternatives.
    runtime_base = base if isinstance(base, dict) else {"layers": {}}
    output = {"locale": copy.deepcopy(runtime_base)}
    if isinstance(named, dict):
        for key, value in named.items():
            # getLocale('locale') resolves workspace.locale. A same-named
            # entry in workspace.locales is not a distinct rendered choice.
            if key == "locale" or not isinstance(value, dict):
                continue
            output[key] = deep_merge(runtime_base, value)
    return output


def select_locale(
    workspace: dict,
    requested: str | None = None,
) -> tuple[str, dict]:
    locales = effective_locales(workspace)
    if requested:
        locale = locales.get(requested)
        if not isinstance(locale, dict):
            raise ValueError(f"Unknown locale: {requested}")
        return requested, locale
    default = locales.get("locale")
    if isinstance(default, dict):
        return "locale", default
    usable = list(locales.items())
    if len(usable) == 1:
        return usable[0]
    if len(usable) > 1:
        raise ValueError("Workspace has multiple locales; select one explicitly.")
    raise ValueError("Workspace does not contain a usable locale.")


def visual_plan(
    workspace: dict,
    layer_key: str,
    db_connections: dict[str, str],
    locale_key: str | None = None,
) -> dict:
    selected_locale, locale = select_locale(workspace, locale_key)
    layer = (locale.get("layers") or {}).get(layer_key)
    if not isinstance(layer, dict):
        raise ValueError(
            f"Unknown layer in locale {selected_locale}: {layer_key}"
        )
    if not is_probeable_database_layer(layer):
        view = locale.get("view") or {}
        plan = {
            "layer": layer_key,
            "locale": selected_locale,
            "source": "workspace-view",
            "warnings": [
                "This layer uses an external or advanced XYZ source, so the "
                "visual check uses the configured workspace view."
            ],
        }
        if all(
            isinstance(view.get(key), (int, float))
            and not isinstance(view.get(key), bool)
            and math.isfinite(view[key])
            for key in ("lng", "lat")
        ):
            plan["centre"] = [view["lng"], view["lat"]]
        if (
            isinstance(view.get("z"), (int, float))
            and not isinstance(view.get("z"), bool)
            and math.isfinite(view["z"])
        ):
            plan["zoom"] = view["z"]
        return plan
    if psycopg is None or sql is None:
        raise RuntimeError("PostgreSQL support is unavailable.")
    db_name = layer.get("dbs") or workspace.get("dbs")
    database_url = db_connections.get(db_name)
    relation = str(layer.get("table", "")).split(".")
    if len(relation) == 1:
        relation.insert(0, "public")
    if not database_url or len(relation) != 2:
        raise ValueError("Layer database or relation is unavailable.")
    relation_sql = sql.SQL("{}.{}").format(sql.Identifier(relation[0]), sql.Identifier(relation[1]))
    geom = sql.Identifier(layer["geom"])
    query = sql.SQL("""
      SELECT feature_count,
             ST_XMin(extent), ST_YMin(extent), ST_XMax(extent), ST_YMax(extent),
             sample.geometry_type
      FROM (
        SELECT count(*)::bigint AS feature_count,
               ST_Extent(ST_Transform({geom}, 3857)) AS extent
        FROM {relation}
        WHERE {geom} IS NOT NULL
      ) bounds
      LEFT JOIN LATERAL (
        SELECT GeometryType({geom}) AS geometry_type
        FROM {relation}
        WHERE {geom} IS NOT NULL
        LIMIT 1
      ) sample ON TRUE
    """).format(geom=geom, relation=relation_sql)
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute(query)
            count, west, south, east, north, geometry_type = cur.fetchone()
    if not count or None in (west, south, east, north):
        raise ValueError("Layer has no non-null renderable geometry.")
    centre_x, centre_y = (west + east) / 2, (south + north) / 2
    width = max(east - west, 10.0)
    height = max(north - south, 10.0)
    resolution = max(width / (1280 * .7), height / (720 * .7))
    zoom = max(0, min(22, math.log2(156543.03392804097 / resolution)))
    upper_geometry = (geometry_type or "").upper()
    if "POINT" in upper_geometry:
        zoom = max(14, zoom)
    elif "LINE" in upper_geometry:
        zoom = max(13, zoom)
    elif "POLYGON" in upper_geometry:
        zoom = max(14, zoom)
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute(
                "SELECT ST_X(point), ST_Y(point) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s,%s),3857),4326) point) q",
                (centre_x, centre_y),
            )
            lng, lat = cur.fetchone()
    return {
        "layer": layer_key,
        "locale": selected_locale,
        "source": "postgis-extent",
        "database": db_name,
        "table": layer["table"],
        "geometry": layer["geom"],
        "geometryType": geometry_type,
        "featureCount": count,
        "bounds3857": [west, south, east, north],
        "centre": [lng, lat],
        "zoom": round(zoom, 2),
        "warnings": ["Large or outlier-heavy datasets may benefit from an explicit centre and zoom."],
    }
