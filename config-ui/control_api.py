from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import heapq
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from plugin_registry import catalogue as external_plugin_catalogue, composed_schema
from relation_identity import parse_relation
from workspace_schema import expression_error

try:
    import psycopg
    from psycopg import sql
except ModuleNotFoundError:  # Allows pure contract/mutation tests without DB extras.
    psycopg = None
    sql = None


API_VERSION = "1.5"
CONTRACT_VERSION = "1.5"
RULES_VERSION = "1.6"
FIXED_FILTER_NUMBER_RE = re.compile(
    r"^[+-]?(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)(?:[eE][+-]?[0-9]+)?$"
)
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

DERIVED_ERROR_PRESENTATION = {
    "errorCodeField": "code",
    "errorCategoryField": "category",
    "reasonField": "reasons",
    "reasonActionField": "suggestedAction",
    "safeStateField": "safeState",
    "stateUnchangedField": "stateUnchanged",
    "rolledBackField": "rolledBack",
    "indeterminateField": "indeterminate",
    "failurePhaseField": "failurePhase",
    "retryableField": "retryable",
    "contentionErrorCode": "derived_layer.database_contention",
    "contentionScopeField": "contentionScope",
    "contentionScopes": ["derived-mutation", "postgresql-lock"],
    "failurePhases": [
        "preflight",
        "database-transaction",
        "transaction-rollback",
        "transaction-commit",
        "result-reporting",
        "request-response",
        "operation-polling",
        "service-recovery",
    ],
    "probeField": "probe",
    "queryPlanningProbeField": "queryPlanningProbe",
    "queryErrorCodes": {
        "invalid": "derived_layer.query_invalid",
        "policy": "derived_layer.query_not_allowed",
        "compute": "derived_layer.query_too_expensive",
    },
    "materializationFallbackCode": (
        "derived_layer.materialization_too_large"
    ),
}

RULES = [
    {"id": "workspace.structure", "category": "schema", "description": "Workspace values must satisfy the supported XYZ structure.", "remediation": "Inspect `config-cli schema` and correct the reported path."},
    {"id": "workspace.layer_order", "category": "schema", "description": "Layer group values create navigation drawers only; map drawing order is controlled by zIndex, where higher values render above lower values.", "remediation": "Set each layer's zIndex explicitly, or use promoteDisplay when a layer should move above currently displayed layers whenever it is shown."},
    {"id": "workspace.layer_key", "category": "schema", "description": "Layer keys are machine identifiers used in workspace paths and browser activation; display wording belongs in layer.name.", "remediation": "Prefer a stable key containing only ASCII letters, numbers, and underscores. Keep spaces, punctuation, and translated wording in layer.name."},
    {"id": "workspace.layer_group_colour", "category": "schema", "description": "XYZ styles a layer-group drawer with the first grouped layer's groupClassList. This is a deployed stylesheet class list, not a literal colour property.", "remediation": "Inspect every member of the exact group and use the same verified deployed class list on each one. Do not invent groupColor/groupColour or put a hex colour in groupClassList."},
    {"id": "workspace.layer_legend", "category": "schema", "description": "An optional basic theme exposes the layer symbology as a legend in XYZ's Styling panel.", "remediation": "Set style.theme to a basic theme whose style matches style.default, and include theme in style.elements when an explicit element list is present."},
    {"id": "workspace.categorized_symbology", "category": "schema", "description": "A categorized theme maps exact values from a feature field to labelled styles in XYZ's data-driven legend.", "remediation": "Set style.theme to a categorized theme with a valid field and category value/style entries; preserve unrelated style.elements and include theme when that array is explicit."},
    {"id": "workspace.theme_semantics", "category": "schema", "description": "Graduated themes require ordered unique numeric breaks; distributed themes require a stable identity field and a non-empty style palette; named and multi-field themes must resolve every referenced field.", "remediation": "Inspect the current layer fields and theme before choosing a corrective mode. Replace invalid references, reorder graduated breaks for the selected comparison, or refresh/reconcile the derived source before proposing the workspace change."},
    {"id": "workspace.infoj_geometry_symbol", "category": "schema", "description": "An optional geometry infoj style renders the same swatch or icon beside its checkbox and on the selected geometry.", "remediation": "Set the geometry infoj entry style to the effective layer symbol; use the dashboard ownership marker only when the dashboard should keep it synchronized with style.default."},
    {"id": "workspace.viewport_count", "category": "schema", "description": "An optional layer filter with viewport enabled scopes XYZ's feature count to the current map view.", "remediation": "Set filter.viewport to true and configure at least one compatible infoj entry as an interactive filter so XYZ creates the Filtering panel."},
    {"id": "workspace.catalog", "category": "catalog", "description": "Database-backed layers must use selectable relations and columns.", "remediation": "Use `config-cli catalog list` and select a reported table, geometry, and ID."},
    {"id": "workspace.feature_id", "category": "data", "description": "XYZ feature IDs must be non-null and unique.", "remediation": "Choose a primary or unique non-null column for qID."},
    {"id": "workspace.render", "category": "render", "description": "XYZ-equivalent bounded database reads must succeed.", "remediation": "Review the relation, SRID, expressions, and database permissions."},
    {"id": "sql.scalar_read_only", "category": "security", "description": "Calculated information values are one read-only scalar expression.", "remediation": "Remove statements, comments, subqueries, session functions, and system access."},
    {"id": "derived_layer.select_only", "category": "security", "description": "Managed derived layers are one dependency-checked SELECT materialized only inside derived_layers.", "remediation": "Declare every schema-qualified source and remove statements, comments, undeclared relations, or unsafe operations."},
    {"id": "derived_layer.query_cost", "category": "database", "description": "Every managed derived-layer query must pass bounded SQL-shape, H3-expansion, and recursive PostgreSQL plan checks before a view or materialized view is created, replaced, or refreshed.", "remediation": "Reduce joins, generated rows, H3 resolution or traversal distance, and intermediate work; changing an over-budget query to an ordinary view does not bypass this guard."},
    {"id": "derived_layer.materialization_size", "category": "database", "description": "Materialized derived layers must pass the server-side PostgreSQL plan-size probe and stay within the advertised 1 GiB estimated-storage limit.", "remediation": "Use an ordinary view, reduce the derived result, or choose a tighter source query; the materialized operation remains blocked while its estimate exceeds the limit."},
    {"id": "svg.safe", "category": "security", "description": "Dashboard SVGs must be bounded, parseable, and free of active content.", "remediation": "Use a safe SVG from `config-cli icons list`."},
    {"id": "plugin.catalogue", "category": "security", "description": "External XYZ plugins must be source-controlled, manifest-backed, compatible, contained, and schema-closed.", "remediation": "Run `config-cli plugins validate` and correct the deployment plugin package."},
    {"id": "proposal.plugin_catalogue", "category": "proposal", "description": "A proposal and its preview evidence apply only to the plugin catalogue fingerprint against which they were created.", "remediation": "Create and preview a new proposal after any plugin deployment change."},
    {"id": "proposal.revision", "category": "proposal", "description": "A proposal applies only to the revision from which it was created.", "remediation": "Create a new proposal against the current workspace."},
    {"id": "semantic.generated_read_only", "category": "semantic", "description": "Generated source facts are updated only by the managed derived-layer lifecycle.", "remediation": "Edit curated semantic annotations through a revision-bound semantic proposal."},
    {"id": "semantic.derived_ready", "category": "semantic", "description": "A managed derived layer must have a ready semantic profile before a new workspace reference can be published.", "remediation": "Wait for automatic delivery or ask a semantic administrator to resolve the failure and explicitly retry delivery."},
    {"id": "visual.data", "category": "visual", "description": "A database-backed visual test needs non-empty valid geometry.", "remediation": "Load data or provide an explicit centre and zoom."},
]
DATABASE_LAYER_FORMATS = {"cluster", "geojson", "mvt", "vector", "wkt"}
MAP_EXTENT_VIEWPORT_WIDTH = 1920
MAP_EXTENT_VIEWPORT_HEIGHT = 1080
MAP_EXTENT_TILE_SIZE = 256
WEB_MERCATOR_MAX_LATITUDE = 85.0511287798066
VISUAL_PLANNING_STATEMENT_TIMEOUT_MS = 5000
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 100
COLLECTION_PAGE_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
COLLECTION_PAGE_MAX_ITEMS_BYTES = 15 * 1024 * 1024
SEMANTIC_PAGE_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
SEMANTIC_PAGE_TOO_LARGE_CODE = "semantic.page_too_large"
_PAGE_CURSOR_RE = re.compile(
    r"^(?:[0-9a-f]{64}|[A-Za-z0-9_-]{1,2048}\.[0-9a-f]{64})$"
)


class CollectionPaginationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        details: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _bounded_collection_items(
    items: list[Any],
    *,
    maximum_items: int,
) -> tuple[list[Any], bool]:
    bounded: list[Any] = []
    used_bytes = 2
    has_more = len(items) > maximum_items
    for item in items[:maximum_items]:
        item_bytes = len(json.dumps(
            item,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"))
        separator_bytes = 1 if bounded else 0
        if item_bytes + 2 > COLLECTION_PAGE_MAX_ITEMS_BYTES:
            raise CollectionPaginationError(
                "pagination.page_too_large",
                "One collection item exceeds the bounded response limit.",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                details={
                    "maxPageBytes": COLLECTION_PAGE_MAX_RESPONSE_BYTES,
                    "maxItemsBytes": COLLECTION_PAGE_MAX_ITEMS_BYTES,
                },
            )
        if (
            used_bytes + separator_bytes + item_bytes
            > COLLECTION_PAGE_MAX_ITEMS_BYTES
        ):
            has_more = True
            break
        bounded.append(item)
        used_bytes += separator_bytes + item_bytes
    return bounded, has_more


def legacy_collection(items: list[Any]) -> list[Any]:
    if len(items) > MAX_PAGE_LIMIT:
        raise CollectionPaginationError(
            "pagination.required",
            "This collection has more than 100 items; retry with limit and "
            "follow pagination.nextCursor.",
            status=HTTPStatus.CONFLICT,
            details={"maxLegacyItems": MAX_PAGE_LIMIT},
        )
    bounded, has_more = _bounded_collection_items(
        items,
        maximum_items=MAX_PAGE_LIMIT,
    )
    if has_more:
        raise CollectionPaginationError(
            "pagination.required",
            "This legacy response exceeds the collection byte limit; retry "
            "with limit and follow pagination.nextCursor.",
            status=HTTPStatus.CONFLICT,
            details={
                "maxLegacyItems": MAX_PAGE_LIMIT,
                "maxPageBytes": COLLECTION_PAGE_MAX_RESPONSE_BYTES,
            },
        )
    return bounded


def enforce_collection_payload(
    payload: dict[str, Any],
    *,
    paginated: bool,
) -> None:
    encoded_bytes = len(json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8"))
    if encoded_bytes <= COLLECTION_PAGE_MAX_RESPONSE_BYTES:
        return
    if paginated:
        raise CollectionPaginationError(
            "pagination.page_too_large",
            "The bounded collection response exceeds the response byte limit; "
            "retry with a smaller limit.",
            status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            details={"maxPageBytes": COLLECTION_PAGE_MAX_RESPONSE_BYTES},
        )
    raise CollectionPaginationError(
        "pagination.required",
        "This legacy response exceeds the collection byte limit; retry with "
        "limit and follow pagination.nextCursor.",
        status=HTTPStatus.CONFLICT,
        details={
            "maxLegacyItems": MAX_PAGE_LIMIT,
            "maxPageBytes": COLLECTION_PAGE_MAX_RESPONSE_BYTES,
        },
    )


def pagination_requested(query: dict[str, list[str]]) -> bool:
    return "limit" in query or "cursor" in query


def pagination_parameters(
    query: dict[str, list[str]],
    *,
    allowed: set[str] | None = None,
) -> tuple[int, str | None]:
    allowed_keys = {"limit", "cursor"} | (allowed or set())
    unexpected = sorted(set(query) - allowed_keys)
    if unexpected:
        raise ValueError(
            "Unsupported query parameters: " + ", ".join(unexpected)
        )

    def one(name: str) -> str | None:
        values = query.get(name)
        if values is None:
            return None
        if len(values) != 1:
            raise ValueError(f"Query parameter {name} may be supplied only once.")
        return values[0]

    limit_text = one("limit")
    if limit_text is None:
        limit = DEFAULT_PAGE_LIMIT
    elif not re.fullmatch(r"[1-9][0-9]*", limit_text):
        raise ValueError("limit must be an integer from 1 to 100.")
    else:
        limit = int(limit_text, 10)
        if limit > MAX_PAGE_LIMIT:
            raise ValueError("limit must be an integer from 1 to 100.")

    cursor = one("cursor")
    if cursor is not None and _PAGE_CURSOR_RE.fullmatch(cursor) is None:
        raise ValueError("cursor is invalid or expired.")
    return limit, cursor


def _page_cursor(scope: str, item: Any) -> str:
    value = json.dumps(
        {"scope": scope, "position": item},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def paginate_collection(
    items: list[Any],
    *,
    limit: int,
    cursor: str | None,
    scope: str,
) -> tuple[list[Any], dict[str, Any]]:
    start = 0
    if cursor is not None:
        for index, item in enumerate(items):
            if _page_cursor(scope, item) == cursor:
                start = index + 1
                break
        else:
            raise ValueError("cursor is invalid or expired.")

    page_items = items[start : start + limit]
    has_more = start + len(page_items) < len(items)
    next_cursor = (
        _page_cursor(scope, page_items[-1])
        if has_more and page_items
        else None
    )
    return page_items, {"limit": limit, "nextCursor": next_cursor}


def encode_position_cursor(
    scope: str,
    position: Any,
    key: bytes,
) -> str:
    payload = json.dumps(
        {
            "version": 1,
            "scope": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
            "position": position,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def decode_position_cursor(
    cursor: str | None,
    scope: str,
    key: bytes,
) -> Any:
    if cursor is None:
        return None
    try:
        encoded, signature = cursor.rsplit(".", 1)
        padding = "=" * (-len(encoded) % 4)
        payload = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        value = json.loads(payload.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "scope", "position"}
            or value["version"] != 1
            or not hmac.compare_digest(
                str(value["scope"]),
                hashlib.sha256(scope.encode("utf-8")).hexdigest(),
            )
        ):
            raise ValueError("scope")
        return value["position"]
    except (TypeError, UnicodeError, ValueError):
        raise ValueError("cursor is invalid or expired.") from None


def paginate_keyset_page(
    fetched: list[Any],
    *,
    limit: int,
    scope: str,
    key: bytes,
    position,
) -> tuple[list[Any], dict[str, Any]]:
    page_items, has_more = _bounded_collection_items(
        fetched,
        maximum_items=limit,
    )
    next_cursor = (
        encode_position_cursor(scope, position(page_items[-1]), key)
        if has_more and page_items
        else None
    )
    return page_items, {"limit": limit, "nextCursor": next_cursor}


class VisualPlanningDatabaseError(RuntimeError):
    """A database failure with safe, structured visual-planning context."""

    def __init__(
        self,
        *,
        stage: str,
        query_purpose: str,
        timed_out: bool,
    ) -> None:
        self.stage = stage
        self.query_purpose = query_purpose
        self.timed_out = timed_out
        self.timeout_milliseconds = (
            VISUAL_PLANNING_STATEMENT_TIMEOUT_MS if timed_out else None
        )
        message = (
            "Visual planning timed out before browser validation began."
            if timed_out
            else "The database could not prepare this read-only visual check."
        )
        super().__init__(message)


class VisualPlanningNoMatchingFeatures(ValueError):
    """The effective rendered dataset has no usable feature geometry."""

    code = "visual.no_matching_features"

    def __init__(
        self,
        *,
        filter_applied: bool,
        effective_dataset: dict | None = None,
        reason: str = "no-matching-renderable-geometry",
        stage: str = "layer-summary",
    ) -> None:
        self.filter_applied = filter_applied
        self.effective_dataset = effective_dataset
        self.reason = reason
        self.stage = stage
        super().__init__(
            "The layer has no matching features with non-null renderable geometry"
            + (" after applying filter.default." if filter_applied else ".")
        )

PLUGIN_MANIFEST: list[dict[str, Any]] = [
    {"key": "admin", "configuration": "object", "execution": "locale", "purpose": "Administrator navigation for authenticated administrators.", "prerequisites": ["standard map button column", "authenticated administrator"]},
    {"key": "consent", "configuration": "object requiring text", "execution": "locale", "purpose": "Persisted user-consent confirmation.", "prerequisites": ["authenticated user", "user IndexedDB"]},
    {"key": "custom_theme", "configuration": "CSS colour string map", "execution": "locale", "purpose": "Apply locale CSS colour variables.", "prerequisites": ["XYZ cssColour utility"]},
    {"key": "dark_mode", "configuration": "object", "execution": "locale", "purpose": "Persisted light/dark mode toggle.", "prerequisites": ["standard map button column", "user IndexedDB for persistence"]},
    {"key": "feature_info", "configuration": "true or object with features/css", "execution": "locale", "purpose": "Raw clicked-feature popup interaction.", "prerequisites": ["standard map button column"]},
    {"key": "fullscreen", "configuration": "object", "execution": "locale", "purpose": "Fullscreen layout toggle and map resize.", "prerequisites": ["standard map button column"]},
    {"key": "layer_order", "configuration": "array of layer keys", "execution": "locale", "purpose": "Sort the decorated locale layer array.", "prerequisites": []},
    {"key": "link_button", "configuration": "link object or array", "execution": "locale", "purpose": "Add configured navigation links.", "prerequisites": ["standard map button column", "href and icon_name"]},
    {"key": "locator", "configuration": "object", "execution": "locale", "purpose": "Browser-geolocation button.", "prerequisites": ["standard map button column", "browser geolocation permission"]},
    {"key": "login", "configuration": "object", "execution": "locale", "purpose": "Login or logout navigation.", "prerequisites": ["standard map button column", "page login advertisement"]},
    {"key": "svg_templates", "configuration": "object mapping names to URLs", "execution": "locale-sync", "purpose": "Legacy SVG template loader; svgTemplates is preferred.", "prerequisites": ["fetchable SVG sources"]},
    {"key": "test", "configuration": "object with quiet/showSummary", "execution": "locale", "purpose": "Run requested core or integrity browser tests.", "prerequisites": ["test URL hook", "test framework import"]},
    {"key": "userIDB", "configuration": "object", "execution": "locale", "purpose": "Developer user-record JSON editor.", "prerequisites": ["standard map button column", "JSON editor"]},
    {"key": "userLayer", "configuration": "object", "execution": "locale", "purpose": "Developer unsaved-layer JSON editor.", "prerequisites": ["layer JSON editor UI"]},
    {"key": "userLocale", "configuration": "object", "execution": "locale", "purpose": "Store and remove personal composed locales.", "prerequisites": ["authenticated user", "standard map button column"]},
    {"key": "zoomBtn", "configuration": "object", "execution": "locale", "purpose": "Zoom buttons respecting effective view limits.", "prerequisites": ["standard map button column"]},
    {"key": "zoomToArea", "configuration": "object", "execution": "locale", "purpose": "Drag-box zoom interaction.", "prerequisites": ["standard map button column"]},
]


def plugin_manifest() -> dict[str, Any]:
    external = external_plugin_catalogue()
    return {
        "xyzVersion": XYZ_VERSION,
        "xyzCommit": external["xyzCommit"],
        "fingerprint": external["fingerprint"],
        "registrySource": "GEOLYTIX XYZ lib/plugins/_plugins.mjs",
        "loading": {
            "sources": "locale.plugins plus every layer.plugins array",
            "extensions": [".js", ".mjs"],
            "relativeResolution": "current document origin",
            "deduplication": "exact source string, first occurrence after layer sources are prepended",
            "failure": "dynamic import failures are logged and loading continues via Promise.allSettled",
            "registration": "imported modules must register functions on global mapp.plugins; exports are not automatically registered",
        },
        "dispatch": {
            "sync": "locale.syncPlugins keys execute sequentially and are awaited after dynamic imports",
            "async": "other locale keys matching mapp.plugins functions are invoked together and awaited with Promise.all",
            "layer": "during layer decoration, every layer property key matching mapp.plugins is invoked with the complete layer object and is not awaited",
            "missingKeys": "unknown sync keys and locale/layer properties without registered functions are ignored",
        },
        "security": [
            "Plugin modules execute arbitrary browser JavaScript in the XYZ origin.",
            "A schema-valid plugin URL is not proof that the module loads or registers the expected key.",
            "Plugin changes require source review, proposal review, reload confirmation, and focused browser evidence.",
        ],
        "bundled": copy.deepcopy(PLUGIN_MANIFEST),
        "external": external["external"],
        "valid": external["valid"],
    }

def _semantic_proposal_input_schema(*, require_fingerprint: bool) -> dict[str, Any]:
    curated_path = r"^/curated(?:/(?:[^/~]|~[01])+)*$"
    nested_curated_path = r"^/curated/(?:[^/~]|~[01])+(?:/(?:[^/~]|~[01])+)*$"
    properties: dict[str, Any] = {
        "assetId": {"type": "string", "minLength": 1, "maxLength": 200},
        "baseVersion": {"type": "integer", "minimum": 1},
        "operations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["op", "path", "value"],
                        "properties": {
                            "op": {"const": "set"},
                            "path": {
                                "type": "string",
                                "pattern": curated_path,
                            },
                            "value": {},
                        },
                        "additionalProperties": False,
                        "allOf": [
                            {
                                "if": {
                                    "properties": {
                                        "path": {"const": "/curated"},
                                    },
                                    "required": ["path"],
                                },
                                "then": {
                                    "properties": {
                                        "value": {"type": "object"},
                                    },
                                },
                            }
                        ],
                    },
                    {
                        "type": "object",
                        "required": ["op", "path"],
                        "properties": {
                            "op": {"const": "unset"},
                            "path": {
                                "type": "string",
                                "pattern": nested_curated_path,
                            },
                        },
                        "additionalProperties": False,
                    },
                ],
            },
        },
        "explanation": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
            "pattern": r"\S",
        },
    }
    required = ["assetId", "baseVersion", "operations"]
    if require_fingerprint:
        properties["fingerprint"] = {
            "type": "string",
            "pattern": r"^[0-9a-f]{64}$",
        }
        required.append("fingerprint")
    return {
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": False,
    }


def _live_visual_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["layer"],
        "properties": {
            "layer": {"type": "string", "minLength": 1},
            "locale": {"type": "string"},
            "centre": {
                "type": "array",
                "prefixItems": [{"type": "number"}, {"type": "number"}],
                "minItems": 2,
                "maxItems": 2,
            },
            "zoom": {"type": "number", "minimum": 0, "maximum": 22},
            "background": {"type": "boolean"},
            "hover": {"type": "boolean"},
            "expectedHoverText": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                },
                "uniqueItems": True,
            },
            "expectedInfoPanelText": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                },
                "uniqueItems": True,
            },
        },
        "additionalProperties": True,
    }


ACTION_SCHEMAS: dict[str, dict[str, Any]] = {
    "layers.values": {
        "method": "GET",
        "pathTemplate": "/api/layers/{layerKey}/values",
        "risk": "aggregate-data-read",
        "scope": "derive",
        "requiredScopes": ["derive", "semantic:inspect"],
        "querySchema": {
            "type": "object",
            "required": ["field"],
            "properties": {
                "field": {"type": "string", "minLength": 1},
                "locale": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
    },
    "layers.statistics": {
        "method": "GET",
        "pathTemplate": "/api/layers/{layerKey}/statistics",
        "risk": "aggregate-data-read",
        "scope": "derive",
        "requiredScopes": ["derive", "semantic:inspect"],
        "querySchema": {
            "type": "object",
            "required": ["field"],
            "properties": {
                "field": {"type": "string", "minLength": 1},
                "locale": {"type": "string"},
                "bins": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                },
                "threshold": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {"type": "number"},
                },
                "break": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {"type": "number"},
                },
            },
            "additionalProperties": False,
        },
    },
    "derived-layers.plan-area-weighted-h3": {
        "method": "POST",
        "path": "/api/derived-layers/recipes/area-weighted-h3/plan",
        "risk": "database-plan",
        "scope": "derive",
        "requiredScopes": ["derive", "semantic:inspect"],
        "presentation": {
            **DERIVED_ERROR_PRESENTATION,
            "messageField": "userMessage",
            "nextActionField": "suggestedAction",
            "technicalFields": [
                "queryPlanProbe", "queryPlanningProbe",
                "materializationProbe", "technicalDetail",
            ],
        },
        "inputSchema": {
            "type": "object",
            "required": [
                "name", "kind", "source", "resolution", "measures",
                "spatialScope",
            ],
            "properties": {
                "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,62}$"},
                "kind": {"enum": ["view", "materialized"]},
                "source": {
                    "type": "object",
                    "required": [
                        "assetId", "relation", "idColumn", "geometryColumn",
                    ],
                    "properties": {
                        "assetId": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                        },
                        "relation": {
                            "type": "string",
                            "pattern": "^[A-Za-z_][A-Za-z0-9_]*\\.[A-Za-z_][A-Za-z0-9_]*$",
                        },
                        "idColumn": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 63,
                        },
                        "geometryColumn": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 63,
                        },
                    },
                    "additionalProperties": False,
                },
                "resolution": {"type": "integer", "minimum": 0, "maximum": 15},
                "measures": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {
                        "type": "object",
                        "required": [
                            "sourceColumn", "outputColumn", "nullHandling",
                        ],
                        "properties": {
                            "sourceColumn": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 63,
                            },
                            "outputColumn": {
                                "type": "string",
                                "pattern": "^[a-z][a-z0-9_]{0,62}$",
                            },
                            "nullHandling": {"enum": ["zero", "ignore"]},
                        },
                        "additionalProperties": False,
                    },
                },
                "description": {"type": "string", "maxLength": 2000},
                "spatialScope": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "type": {"const": "workspace-map-extent"},
                        "locale": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 200,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
    "semantic.status": {
        "method": "GET",
        "path": "/api/semantic/status",
        "risk": "inspect",
        "scope": "semantic:inspect",
    },
    "semantic.catalog.export": {
        "method": "GET",
        "path": "/api/semantic/catalog",
        "risk": "inspect",
        "scope": "semantic:inspect",
    },
    "semantic.catalog.search": {
        "method": "GET",
        "path": "/api/semantic/catalog/search",
        "risk": "inspect",
        "scope": "semantic:inspect",
    },
    "semantic.catalog.show": {
        "method": "GET",
        "pathTemplate": "/api/semantic/catalog/objects/{assetId}",
        "risk": "inspect",
        "scope": "semantic:inspect",
    },
    "semantic.catalog.history": {
        "method": "GET",
        "pathTemplate": "/api/semantic/catalog/objects/{assetId}/history",
        "risk": "inspect",
        "scope": "semantic:inspect",
    },
    "semantic.catalog.archive": {
        "method": "POST",
        "pathTemplate": "/api/semantic/catalog/objects/{assetId}/archive",
        "risk": "semantic-archive",
        "scope": "semantic:admin",
        "requiredScopes": ["semantic:inspect", "semantic:admin"],
        "inputSchema": {
            "type": "object",
            "required": ["confirmed"],
            "properties": {"confirmed": {"const": True}},
            "additionalProperties": False,
        },
    },
    "semantic.source.relations": {
        "method": "GET",
        "path": "/api/semantic/source/relations",
        "risk": "inspect",
        "scope": "semantic:source",
        "requiredScopes": ["semantic:inspect", "semantic:source"],
    },
    "semantic.source.sync": {
        "method": "POST",
        "path": "/api/semantic/source/sync",
        "risk": "semantic-source",
        "scope": "semantic:source",
        "requiredScopes": ["semantic:inspect", "semantic:source"],
        "inputSchema": {
            "type": "object",
            "required": ["alias", "schema", "relation"],
            "properties": {
                "alias": {
                    "type": "string",
                    # Ordinary DBS_* aliases follow semantic_sources.ALIAS_RE;
                    # federation aliases are intentionally narrower.
                    "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,62}$",
                },
                "schema": {
                    "type": "string",
                    "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,62}$",
                },
                "relation": {
                    "type": "string",
                    "pattern": "^[A-Za-z_][A-Za-z0-9_]{0,62}$",
                },
            },
            "additionalProperties": False,
        },
    },
    "semantic.source.archive-excluded": {
        "method": "POST",
        "path": "/api/semantic/source/archive-excluded",
        "risk": "semantic-archive",
        "scope": "semantic:admin",
        "requiredScopes": ["semantic:inspect", "semantic:admin"],
        "inputSchema": {
            "type": "object",
            "required": ["confirmed"],
            "properties": {"confirmed": {"const": True}},
            "additionalProperties": False,
        },
    },
    "semantic.derived-profiles.repair": {
        "method": "POST",
        "pathTemplate": "/api/semantic/derived-profiles/{name}/repair",
        "risk": "semantic-repair",
        "scope": "semantic:admin",
        "inputSchema": {
            "type": "object",
            "required": ["confirmed"],
            "properties": {"confirmed": {"const": True}},
            "additionalProperties": False,
        },
    },
    "semantic.derived-profiles.list": {
        "method": "GET",
        "path": "/api/semantic/derived-profiles",
        "risk": "inspect",
        "scope": "semantic:inspect",
    },
    "semantic.derived-profiles.show": {
        "method": "GET",
        "pathTemplate": "/api/semantic/derived-profiles/{name}",
        "risk": "inspect",
        "scope": "semantic:inspect",
    },
    "semantic.generate": {
        "method": "POST",
        "path": "/api/semantic/generate",
        "risk": "external-semantic-egress",
        "scope": "semantic:generate",
        "requiredScopes": ["semantic:inspect", "semantic:generate"],
        "conditionalScopes": [{
            "whenAnyTrue": [
                "contextOptions.sampleRows",
                "contextOptions.statistics",
            ],
            "requiredScopes": ["semantic:data"],
            "reason": (
                "Optional row samples or data-derived statistics require "
                "explicit data access."
            ),
        }],
        "inputSchema": {
            "type": "object",
            "required": ["assetId", "target"],
            "properties": {
                "assetId": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
                "target": {
                    "oneOf": [
                        {
                            "type": "object",
                            "required": ["kind"],
                            "properties": {
                                "kind": {"const": "table"},
                            },
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "required": ["kind", "fieldId"],
                            "properties": {
                                "kind": {"const": "field"},
                                "fieldId": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 200,
                                },
                            },
                            "additionalProperties": False,
                        },
                    ],
                },
                "contextOptions": {
                    "type": "object",
                    "properties": {
                        "sampleRows": {"type": "boolean"},
                        "statistics": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
    "semantic.proposals.check": {
        "method": "POST",
        "path": "/api/semantic/proposals/check",
        "risk": "inspect",
        "scope": "semantic:propose",
        "inputSchema": _semantic_proposal_input_schema(
            require_fingerprint=False
        ),
    },
    "semantic.proposals.create": {
        "method": "POST",
        "path": "/api/semantic/proposals",
        "risk": "propose",
        "scope": "semantic:propose",
        "inputSchema": _semantic_proposal_input_schema(
            require_fingerprint=True
        ),
    },
    "semantic.proposals.apply": {
        "method": "POST",
        "pathTemplate": "/api/semantic/proposals/{proposalId}/apply",
        "risk": "semantic-apply",
        "scope": "semantic:apply",
        "inputSchema": {
            "type": "object",
            "required": ["confirmed"],
            "properties": {"confirmed": {"const": True}},
            "additionalProperties": False,
        },
    },
    "semantic.proposals.list": {
        "method": "GET",
        "path": "/api/semantic/proposals",
        "risk": "inspect",
        "scope": "semantic:inspect",
    },
    "semantic.proposals.show": {
        "method": "GET",
        "pathTemplate": "/api/semantic/proposals/{proposalId}",
        "risk": "inspect",
        "scope": "semantic:inspect",
    },
    "semantic.proposals.decline": {
        "method": "POST",
        "pathTemplate": "/api/semantic/proposals/{proposalId}/decline",
        "risk": "propose",
        "scope": "semantic:propose",
        "inputSchema": {
            "type": "object",
            "required": ["confirmed"],
            "properties": {
                "confirmed": {"const": True},
                "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "additionalProperties": False,
        },
    },
    "derived-layers.map-extent": {
        "method": "GET",
        "path": "/api/derived-layers/map-extent",
        "risk": "inspect",
        "scope": "inspect",
    },
    "derived-layers.create": {
        "method": "POST",
        "path": "/api/derived-layers",
        "risk": "database-definition",
        "scope": "derive",
        "requiredScopes": ["derive", "semantic:inspect"],
        "operationKind": "derived-layer.create",
        "presentation": {
            **DERIVED_ERROR_PRESENTATION,
            "messageField": "userMessage",
            "nextActionField": "suggestedAction",
            "technicalFields": [
                "queryPlanProbe", "queryPlanningProbe",
                "materializationProbe", "technicalDetail",
            ],
        },
        "inputSchema": {
            "type": "object",
            "required": [
                "name", "query", "sources", "idColumn", "geometryColumn"
            ],
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9_]{0,62}$",
                },
                "kind": {"enum": ["view", "materialized"]},
                "query": {"type": "string", "minLength": 1},
                "sources": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "idColumn": {"type": "string"},
                "geometryColumn": {"type": "string"},
                "description": {"type": "string"},
                "background": {"type": "boolean"},
                "spatialScope": {
                    "type": "object",
                    "default": {"type": "workspace-map-extent"},
                    "required": ["type"],
                    "properties": {
                        "type": {"const": "workspace-map-extent"},
                        "locale": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
    "derived-layers.refresh": {
        "method": "POST",
        "pathTemplate": "/api/derived-layers/{name}/refresh",
        "risk": "database-refresh",
        "scope": "derive",
        "operationKind": "derived-layer.refresh",
        "presentation": {
            **DERIVED_ERROR_PRESENTATION,
            "messageField": "userMessage",
            "nextActionField": "suggestedAction",
            "technicalFields": [
                "queryPlanProbe", "queryPlanningProbe",
                "materializationProbe", "technicalDetail",
            ],
        },
        "inputSchema": {
            "type": "object",
            "required": ["confirmed"],
            "properties": {
                "confirmed": {"const": True},
                "background": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    "derived-layers.replace": {
        "method": "POST",
        "pathTemplate": "/api/derived-layers/{name}/replace",
        "risk": "database-definition",
        "scope": "derive",
        "requiredScopes": ["derive", "semantic:inspect"],
        "operationKind": "derived-layer.replace",
        "presentation": {
            **DERIVED_ERROR_PRESENTATION,
            "messageField": "derivedLayer.userMessage",
            "nextActionField": "derivedLayer.suggestedAction",
            "technicalFields": [
                "workspaceReferences", "fieldReferences", "dependents",
                "queryPlanProbe", "queryPlanningProbe",
                "materializationProbe", "technicalDetail",
            ],
        },
        "inputSchema": {
            "type": "object",
            "required": [
                "confirmed", "name", "kind", "query", "sources",
                "idColumn", "geometryColumn",
            ],
            "properties": {
                "confirmed": {"const": True},
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9_]{0,62}$",
                },
                "kind": {"enum": ["view", "materialized"]},
                "query": {"type": "string", "minLength": 1},
                "sources": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "idColumn": {"type": "string"},
                "geometryColumn": {"type": "string"},
                "description": {"type": "string"},
                "background": {"type": "boolean"},
                "spatialScope": {
                    "type": "object",
                    "default": {"type": "workspace-map-extent"},
                    "required": ["type"],
                    "properties": {
                        "type": {"const": "workspace-map-extent"},
                        "locale": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
    "derived-layers.drop": {
        "method": "POST",
        "pathTemplate": "/api/derived-layers/{name}/drop",
        "risk": "database-definition",
        "scope": "derive",
        "presentation": {
            **DERIVED_ERROR_PRESENTATION,
            "messageField": "userMessage",
            "nextActionField": "suggestedAction",
            "technicalFields": [
                "workspaceReferences", "dependents", "technicalDetail",
            ],
        },
        "inputSchema": {
            "type": "object",
            "required": ["confirmed"],
            "properties": {"confirmed": {"const": True}},
            "additionalProperties": False,
        },
    },
    "operations.cancel": {
        "method": "POST",
        "pathTemplate": "/api/operations/{operationId}/cancel",
        "risk": "database-definition",
        "scope": "derive",
        "inputSchema": {
            "type": "object",
            "required": ["confirmed"],
            "properties": {"confirmed": {"const": True}},
            "additionalProperties": False,
        },
    },
    "proposals.check": {
        "method": "POST",
        "path": "/api/proposals/check",
        "risk": "read",
        "scope": "propose",
        "inputSchema": {
            "type": "object",
            "required": ["revision", "operations"],
            "properties": {
                "revision": {"type": "string", "minLength": 1},
                "operations": {"type": "array", "minItems": 1},
                "explanation": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "proposals.create": {
        "method": "POST",
        "path": "/api/proposals",
        "risk": "propose",
        "scope": "propose",
        "inputSchema": {
            "type": "object",
            "required": ["revision", "operations"],
            "properties": {
                "revision": {"type": "string", "minLength": 1},
                "operations": {"type": "array", "minItems": 1},
                "explanation": {"type": "string"},
                "checkFingerprint": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "proposals.apply": {
        "method": "POST",
        "pathTemplate": "/api/proposals/{proposalId}/apply",
        "risk": "apply",
        "scope": "apply",
        "inputSchema": {
            "type": "object",
            "properties": {"approved": {"const": True}},
            "required": ["approved"],
            "additionalProperties": False,
        },
    },
    "visual.plan": {
        "method": "POST",
        "path": "/api/visual-plan",
        "risk": "visual",
        "scope": "visual",
        "inputSchema": _live_visual_input_schema(),
    },
    "visual.test": {
        "method": "POST",
        "path": "/api/visual-test",
        "risk": "visual",
        "scope": "visual",
        "operationKind": "visual.test",
        "inputSchema": _live_visual_input_schema(),
    },
    "visual.screenshot": {
        "method": "POST",
        "path": "/api/visual-test",
        "risk": "visual",
        "scope": "visual",
        "operationKind": "visual.test",
        "inputSchema": _live_visual_input_schema(),
    },
    "proposals.visual-test": {
        "method": "POST",
        "pathTemplate": "/api/proposals/{proposalId}/visual-test",
        "risk": "visual",
        "scope": "visual",
        "operationKind": "proposal.visual-test",
        "inputSchema": {
            "type": "object",
            "required": ["layer"],
            "properties": {
                "layer": {"type": "string", "minLength": 1},
                "locale": {"type": "string"},
                "centre": {"type": "array", "minItems": 2, "maxItems": 2},
                "zoom": {"type": "number", "minimum": 0, "maximum": 22},
                "background": {"type": "boolean"},
                "viewMode": {
                    "type": "string",
                    "enum": ["focus", "default"],
                    "default": "focus",
                },
                "hover": {"type": "boolean"},
                "expectedHoverText": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "uniqueItems": True,
                },
                "expectedInfoPanelText": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "uniqueItems": True,
                },
            },
            "additionalProperties": True,
        },
    },
    "proposals.screenshot": {
        "method": "POST",
        "pathTemplate": "/api/proposals/{proposalId}/screenshot",
        "risk": "visual",
        "scope": "visual",
        "operationKind": "proposal.screenshot",
        "inputSchema": {
            "type": "object",
            "required": ["layer"],
            "properties": {
                "layer": {"type": "string", "minLength": 1},
                "locale": {"type": "string"},
                "centre": {"type": "array", "minItems": 2, "maxItems": 2},
                "zoom": {"type": "number", "minimum": 0, "maximum": 22},
                "background": {"type": "boolean"},
                "viewMode": {
                    "type": "string",
                    "enum": ["focus", "default"],
                    "default": "focus",
                },
                "viewport": {
                    "type": "object",
                    "default": {"width": 1080, "height": 1080},
                    "properties": {
                        "width": {
                            "type": "number",
                            "minimum": 320,
                            "maximum": 2560,
                        },
                        "height": {
                            "type": "number",
                            "minimum": 240,
                            "maximum": 1440,
                        },
                    },
                    "additionalProperties": False,
                },
                "deviceScaleFactor": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 3,
                    "default": 1,
                },
                "panel": {
                    "type": "string",
                    "enum": ["filtering", "styling"],
                },
                "panels": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["filtering", "styling"],
                    },
                    "uniqueItems": True,
                },
                "expectedPanelText": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
                "hover": {"type": "boolean"},
                "expectedHoverText": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "uniqueItems": True,
                },
                "expectedInfoPanelText": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "uniqueItems": True,
                },
            },
            "additionalProperties": True,
        },
    },
    "proposals.preview-plan": {
        "method": "POST",
        "pathTemplate": "/api/proposals/{proposalId}/visual-plan",
        "risk": "visual",
        "scope": "visual",
        "inputSchema": {
            "type": "object",
            "required": ["layer"],
            "properties": {
                "layer": {"type": "string", "minLength": 1},
                "locale": {"type": "string"},
                "centre": {"type": "array", "minItems": 2, "maxItems": 2},
                "zoom": {"type": "number", "minimum": 0, "maximum": 22},
                "viewMode": {
                    "type": "string",
                    "enum": ["focus", "default"],
                    "default": "focus",
                },
                "viewport": {"type": "object"},
                "deviceScaleFactor": {"type": "number", "minimum": 1, "maximum": 3},
                "expectedInfoPanelText": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "uniqueItems": True,
                },
            },
            "additionalProperties": True,
        },
    },
    "proposals.preview-test": {
        "method": "POST",
        "pathTemplate": "/api/proposals/{proposalId}/visual-test",
        "risk": "visual",
        "scope": "visual",
        "operationKind": "proposal.visual-test",
        "inputSchema": {
            "type": "object",
            "required": ["layer"],
            "properties": {
                "layer": {"type": "string", "minLength": 1},
                "locale": {"type": "string"},
                "centre": {"type": "array", "minItems": 2, "maxItems": 2},
                "zoom": {"type": "number", "minimum": 0, "maximum": 22},
                "background": {"type": "boolean"},
                "viewMode": {
                    "type": "string",
                    "enum": ["focus", "default"],
                    "default": "focus",
                },
                "viewport": {"type": "object"},
                "deviceScaleFactor": {"type": "number", "minimum": 1, "maximum": 3},
                "hover": {"type": "boolean"},
                "expectedHoverText": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "uniqueItems": True,
                },
                "expectedInfoPanelText": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "uniqueItems": True,
                },
            },
            "additionalProperties": True,
        },
    },
    "proposals.preview-screenshot": {
        "method": "POST",
        "pathTemplate": "/api/proposals/{proposalId}/screenshot",
        "risk": "visual",
        "scope": "visual",
        "operationKind": "proposal.screenshot",
        "inputSchema": {
            "type": "object",
            "required": ["layer"],
            "properties": {
                "layer": {"type": "string", "minLength": 1},
                "locale": {"type": "string"},
                "centre": {"type": "array", "minItems": 2, "maxItems": 2},
                "zoom": {"type": "number", "minimum": 0, "maximum": 22},
                "background": {"type": "boolean"},
                "viewMode": {
                    "type": "string",
                    "enum": ["focus", "default"],
                    "default": "focus",
                },
                "viewport": {
                    "type": "object",
                    "properties": {
                        "width": {
                            "type": "integer",
                            "minimum": 320,
                            "maximum": 2560,
                        },
                        "height": {
                            "type": "integer",
                            "minimum": 240,
                            "maximum": 1440,
                        },
                    },
                    "additionalProperties": False,
                },
                "deviceScaleFactor": {"type": "number", "minimum": 1, "maximum": 3},
                "panel": {
                    "type": "string",
                    "enum": ["filtering", "styling"],
                },
                "panels": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["filtering", "styling"],
                    },
                    "uniqueItems": True,
                },
                "expectedPanelText": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
                "hover": {"type": "boolean"},
                "expectedHoverText": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "uniqueItems": True,
                },
                "expectedInfoPanelText": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                    },
                    "uniqueItems": True,
                },
            },
            "additionalProperties": True,
        },
    },
    "xyz.reload": {
        "method": "POST",
        "path": "/api/xyz/reload",
        "risk": "reload",
        "scope": "reload",
        "operationKind": "xyz.reload",
        "inputSchema": {
            "type": "object",
            "required": ["confirmed"],
            "properties": {
                "confirmed": {"const": True},
                "workspaceFingerprint": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
            },
            "additionalProperties": False,
        },
    },
    "federation.aliases.list": {
        "method": "GET",
        "path": "/api/federation/aliases",
        "risk": "inspect",
        "scope": "federation:observe",
    },
    "federation.aliases.show": {
        "method": "GET",
        "pathTemplate": "/api/federation/aliases/{alias}",
        "risk": "inspect",
        "scope": "federation:observe",
    },
    "federation.aliases.register": {
        "method": "POST",
        "path": "/api/federation/aliases",
        "risk": "federation-register",
        "scope": "federation:register",
        "inputSchema": {
            "type": "object",
            "required": [
                "alias", "displayName", "kind", "connectionRef", "tlsPolicy",
                "allowedRelations", "dataHandlingClassification",
                "dataHandlingAcknowledged",
            ],
            "properties": {
                "alias": {
                    "type": "string",
                    "pattern": "^[A-Za-z][A-Za-z0-9_]{0,55}$",
                },
                "displayName": {
                    "type": "string", "minLength": 1, "maxLength": 200,
                },
                "kind": {"const": "postgresql"},
                "connectionRef": {
                    "type": "string", "minLength": 1, "maxLength": 200,
                },
                "tlsPolicy": {"enum": ["require", "verify-ca", "verify-full"]},
                # Mirrors federation_schema._normalized_allowed_relations():
                # each entry must be schema-qualified with identifier-shaped
                # parts, and entries must be distinct. (That validator also
                # rejects two relations sharing a basename across different
                # schemas, since both would import into one local
                # source_<alias> table — not expressible in JSON Schema, so
                # it stays a server-side rejection.)
                "allowedRelations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "maxLength": 127,
                        "pattern": (
                            "^[A-Za-z_][A-Za-z0-9_]{0,62}\\."
                            "[A-Za-z_][A-Za-z0-9_]{0,62}$"
                        ),
                    },
                },
                "dataHandlingClassification": {
                    "type": "string", "minLength": 1, "maxLength": 2000,
                },
                "dataHandlingAcknowledged": {"const": True},
                "freshnessStrategy": {
                    # maximumAge/timestampColumn/versionRelation are a
                    # documented part of the architecture but have no
                    # evidence-collection implementation yet — see
                    # federation_schema.py's validate_registration(). The
                    # contract must not advertise support the server
                    # rejects.
                    "enum": ["manual"],
                },
            },
            "additionalProperties": False,
        },
    },
    "federation.aliases.observe": {
        "method": "POST",
        "pathTemplate": "/api/federation/aliases/{alias}/observe",
        "risk": "federation-observe",
        # Not federation:observe — Discover opens a live, credentialed
        # outbound connection, so it requires the same scope as Approve
        # exposure (see app.py's _required_scope).
        "scope": "federation:provision",
        "inputSchema": {"type": "object", "additionalProperties": False},
    },
    "federation.aliases.provision": {
        "method": "POST",
        "pathTemplate": "/api/federation/aliases/{alias}/provision",
        "risk": "federation-provision",
        "scope": "federation:provision",
        "inputSchema": {
            "type": "object",
            "required": ["expectedObservationId"],
            "properties": {
                "expectedObservationId": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 9223372036854775807,
                },
                "rowLevelSecurityAcknowledged": {"const": True},
                "schemaChangeAcknowledged": {"const": True},
                "physicalRebindAcknowledged": {"const": True},
            },
            "additionalProperties": False,
        },
    },
}


def contract(instance_id: str) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "rulesVersion": RULES_VERSION,
        "xyzVersion": XYZ_VERSION,
        "instanceId": instance_id,
        "authentication": {
            "type": "bearer",
            "tokens": "issued by config UI or approved device authorization",
            "scopes": [
                "full", "inspect", "propose", "visual", "apply", "reload",
                "derive", "semantic:inspect", "semantic:source",
                "semantic:generate", "semantic:data",
                "semantic:propose",
                "semantic:apply", "semantic:admin",
                "federation:register", "federation:provision",
                "federation:observe",
            ],
            "defaultDeviceScopes": [
                "inspect", "propose", "visual", "semantic:inspect",
            ],
        },
        "commands": [
            "describe", "schema", "rules", "examples",
            "plugins list", "plugins show", "plugins validate", "plugins usage",
            "capabilities list", "capabilities show",
            "dependencies list", "dependencies check",
            "workspace get", "layers list", "layers get", "layers values",
            "layers statistics",
            "layers style-elements", "layers filters", "layers effective",
            "catalog list", "icons list",
            "derived-layers capabilities", "derived-layers list",
            "derived-layers show", "derived-layers create",
            "derived-layers plan-area-weighted-h3",
            "derived-layers map-extent",
            "derived-layers refresh", "derived-layers replace",
            "derived-layers drop",
            "semantic status",
            "semantic catalog export", "semantic catalog search",
            "semantic catalog show", "semantic catalog history",
            "semantic catalog archive",
            "semantic source relations", "semantic source sync",
            "semantic source archive-excluded",
            "semantic derived-profiles list",
            "semantic derived-profiles show",
            "semantic derived-profiles repair",
            "semantic generate table", "semantic generate field",
            "semantic proposals check", "semantic proposals create",
            "semantic proposals list", "semantic proposals show",
            "semantic proposals apply", "semantic proposals decline",
            "validate", "set", "unset", "amend", "sql capabilities", "sql test",
            "visual-plan", "visual-test", "screenshot",
            "proposals preview-plan", "proposals preview-test",
            "proposals preview-screenshot",
            "proposals check", "proposals create", "proposals show", "proposals list",
            "proposals apply", "proposals decline",
            "xyz status", "xyz reload",
            "operations show", "operations wait", "operations cancel",
            "auth status", "auth device",
        ],
        "workflow": [
            "inspect",
            "propose",
            "validate",
            "review evidence",
            "approve",
            "apply with managed reload",
            "check reload status",
            "verify",
        ],
        "exitCodes": {"success": 0, "usage": 2, "validation": 3, "conflict": 4, "connectivity": 5, "visual": 6, "authentication": 7, "interrupted": 130},
        "capabilities": {
            "discovery": "/api/capabilities",
            "operations": {
                "statusTemplate": "/api/operations/{operationId}",
                "cancelTemplate": "/api/operations/{operationId}/cancel",
                "terminalStatuses": [
                    "succeeded", "failed", "cancelled", "indeterminate",
                ],
            },
            "responseMetadata": "meta",
        },
        "pagination": {
            "version": "1",
            "defaultLimit": DEFAULT_PAGE_LIMIT,
            "maxLimit": MAX_PAGE_LIMIT,
            "cursor": "opaque",
            "pageMaxResponseBytes": COLLECTION_PAGE_MAX_RESPONSE_BYTES,
            "pageTooLargeCode": "pagination.page_too_large",
            "legacyMaxItems": MAX_PAGE_LIMIT,
            "legacyOverflowCode": "pagination.required",
            "semanticPageMaxResponseBytes": (
                SEMANTIC_PAGE_MAX_RESPONSE_BYTES
            ),
            "semanticPageTooLargeCode": SEMANTIC_PAGE_TOO_LARGE_CODE,
            "derivedDeliveryBlockers": {
                "itemsField": "deliveryBlockers",
                "moreField": "deliveryBlockersMore",
                "maxItems": MAX_PAGE_LIMIT,
                "firstPageOnly": True,
            },
            "compatibilityArtifact": "contracts/api-compatibility-v1.5.json",
        },
    }


def capabilities(instance_id: str) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "instanceId": instance_id,
        "actions": [
            {"id": action_id, **copy.deepcopy(schema)}
            for action_id, schema in sorted(ACTION_SCHEMAS.items())
        ],
        "responseEnvelope": {
            "metadataField": "meta",
            "requestIdField": "requestId",
            "operationIdField": "operationId",
        },
        "pagination": {
            "version": "1",
            "defaultLimit": DEFAULT_PAGE_LIMIT,
            "maxLimit": MAX_PAGE_LIMIT,
            "cursor": "opaque",
            "pageMaxResponseBytes": COLLECTION_PAGE_MAX_RESPONSE_BYTES,
            "pageTooLargeCode": "pagination.page_too_large",
            "legacyMaxItems": MAX_PAGE_LIMIT,
            "legacyOverflowCode": "pagination.required",
            "semanticPageMaxResponseBytes": (
                SEMANTIC_PAGE_MAX_RESPONSE_BYTES
            ),
            "semanticPageTooLargeCode": SEMANTIC_PAGE_TOO_LARGE_CODE,
            "derivedDeliveryBlockers": {
                "itemsField": "deliveryBlockers",
                "moreField": "deliveryBlockersMore",
                "maxItems": MAX_PAGE_LIMIT,
                "firstPageOnly": True,
            },
        },
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
    data = composed_schema(strict_json_loads(SCHEMA_PATH.read_text()))
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
        "setLayerDrawingOrder": {
            "operations": [
                {
                    "op": "set",
                    "path": "/locale/layers/Boundaries/zIndex",
                    "value": 10,
                },
                {
                    "op": "set",
                    "path": "/locale/layers/Labels/zIndex",
                    "value": 20,
                },
            ],
            "explanation": "Draws Labels above Boundaries. Layer group values affect navigation only.",
        },
        "showLayerLegend": {
            "operations": [
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/style/theme",
                    "value": {
                        "type": "basic",
                        "label": "Bus stop",
                        "style": {
                            "icon": {
                                "url": "/instance/svg/bus.svg",
                                "scale": 1,
                            },
                        },
                    },
                },
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/style/elements",
                    "value": ["theme"],
                },
            ],
            "explanation": "Optionally shows the Bus Stops map symbol as a basic legend in the XYZ layer Styling panel.",
        },
        "setCategorizedSymbology": {
            "operations": [
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/style/theme",
                    "value": {
                        "type": "categorized",
                        "title": "Bus stops by town",
                        "field": "town",
                        "categories": [
                            {
                                "value": "Leeds",
                                "label": "Leeds",
                                "style": {
                                    "icon": {
                                        "type": "dot",
                                        "fillColor": "#176b4d",
                                        "scale": 1,
                                    },
                                },
                            },
                            {
                                "value": "Wetherby",
                                "label": "Wetherby",
                                "style": {
                                    "icon": {
                                        "type": "dot",
                                        "fillColor": "#277da1",
                                        "scale": 1,
                                    },
                                },
                            },
                        ],
                    },
                },
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/style/elements",
                    "value": ["theme"],
                },
            ],
            "explanation": "Uses exact town values to drive Bus Stops symbols and the XYZ theme legend; preserve unrelated style element keys from the inspected revision.",
        },
        "setGraduatedSymbology": {
            "operations": [
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/style/theme",
                    "value": {
                        "type": "graduated",
                        "title": "Bus stops by score",
                        "field": "score",
                        "graduated_breaks": "less_than",
                        "categories": [
                            {"value": 10, "label": "Up to 10", "style": {"icon": {"type": "dot", "fillColor": "#a8d5ec"}}},
                            {"value": 50, "label": "Up to 50", "style": {"icon": {"type": "dot", "fillColor": "#277da1"}}},
                        ],
                    },
                },
            ],
            "explanation": "Uses ordered numeric less-than breaks; verify that score is numeric and preserve the inspected layer's unrelated style configuration.",
        },
        "setDistributedSymbology": {
            "operations": [
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/style/theme",
                    "value": {
                        "type": "distributed",
                        "title": "Distributed bus stop palette",
                        "field": "object_id",
                        "categories": [
                            {"label": "Green", "style": {"icon": {"type": "dot", "fillColor": "#176b4d"}}},
                            {"label": "Blue", "style": {"icon": {"type": "dot", "fillColor": "#277da1"}}},
                        ],
                    },
                },
            ],
            "explanation": "Lets XYZ distribute a reusable palette by stable feature identity while avoiding repeated styles on intersecting features where possible.",
        },
        "countLayerInViewport": {
            "operations": [
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/filter",
                    "value": {
                        "viewport": True,
                        "includeAll": False,
                        "count_meta": "features currently visible",
                    },
                },
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/infoj/2/filter",
                    "value": True,
                },
            ],
            "explanation": "Optionally creates the Filtering panel and scopes its feature count to the current viewport.",
        },
        "showViewportCountBesideLayer": {
            "operations": [
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/plugins",
                    "value": ["/instance/plugins/viewport-layer-count.mjs"],
                },
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/viewport_layer_count",
                    "value": {},
                },
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/filter/viewport",
                    "value": True,
                },
            ],
            "explanation": "Optionally shows the visible feature count in brackets beside the Bus Stops layer name.",
        },
        "showSymbolInFeatureInformation": {
            "operations": [
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/infoj/0/style",
                    "value": {
                        "fillColor": None,
                        "strokeColor": None,
                        "icon": {
                            "url": "/instance/svg/bus.svg",
                            "scale": 1,
                        },
                    },
                },
                {
                    "op": "set",
                    "path": "/locale/layers/Bus Stops/infoj/0/_dashboard",
                    "value": {"styleFromLayerDefault": True},
                },
            ],
            "explanation": "Optionally shows the Bus Stops map icon beside its geometry control in clicked-feature information.",
        },
        "workflow": [
            "config-cli describe",
            "config-cli layers get 'Bus Stops'",
            "config-cli proposals check --base-revision WORKSPACE_REVISION --set '/locale/layers/Bus Stops/style/default/icon/fillColor=\"#2563eb\"' --explanation 'Changes only the default Bus Stops fill colour.'",
            "config-cli proposals create --from-check CHECK_FINGERPRINT",
            "config-cli proposals apply PROPOSAL_ID --confirm",
            "config-cli xyz status",
            "config-cli visual-test --layer 'Bus Stops'",
        ],
    }


def proposal_create(
    store,
    original: dict,
    original_revision: str,
    candidate: dict,
    operations: list,
    diff: list,
    actor: str,
    explanation: str | None = None,
    *,
    plugin_catalogue_fingerprint: str | None = None,
) -> dict:
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
        "pluginCatalogueFingerprint": (
            plugin_catalogue_fingerprint
            if plugin_catalogue_fingerprint is not None
            else external_plugin_catalogue()["fingerprint"]
        ),
        "operations": operations,
        "diff": diff,
        "explanation": explanation or explain_diff(diff),
        "original": original,
        "candidate": candidate,
        "warnings": [],
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


def proposal_check(original: dict, original_revision: str, candidate: dict,
                   operations: list, diff: list,
                   explanation: str | None = None) -> dict:
    """Return proposal evidence without allocating or persisting a proposal."""
    candidate_hash = workspace_hash(candidate)
    plugin_fingerprint = external_plugin_catalogue()["fingerprint"]
    fingerprint = hashlib.sha256(json.dumps({
        "revision": original_revision,
        "candidateHash": candidate_hash,
        "operations": operations,
        "pluginCatalogueFingerprint": plugin_fingerprint,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
       allow_nan=False).encode()).hexdigest()
    return {
        "valid": True,
        "proposalCreated": False,
        "originalRevision": original_revision,
        "originalHash": workspace_hash(original),
        "candidateHash": candidate_hash,
        "pluginCatalogueFingerprint": plugin_fingerprint,
        "checkFingerprint": fingerprint,
        "operations": operations,
        "diff": diff,
        "explanation": explanation or explain_diff(diff),
        "warnings": [],
    }


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


def _proposal_summary(path: Path) -> dict:
    item = strict_json_loads(path.read_text())
    return {
        key: item.get(key)
        for key in (
            "id",
            "status",
            "created",
            "actor",
            "explanation",
            "originalRevision",
            "candidateHash",
            "pluginCatalogueFingerprint",
        )
    }


def proposal_list(
    store,
    *,
    after_id: str | None = None,
    fetch_limit: int | None = None,
) -> list[dict]:
    if after_id is not None and not re.fullmatch(r"[A-Za-z0-9._-]+", after_id):
        raise ValueError("cursor is invalid or expired.")
    if fetch_limit is None:
        return [
            _proposal_summary(path)
            for path in sorted(
                store.proposals.glob("*/proposal.json"),
                reverse=True,
            )
        ]
    if (
        isinstance(fetch_limit, bool)
        or not isinstance(fetch_limit, int)
        or not 1 <= fetch_limit <= MAX_PAGE_LIMIT + 1
    ):
        raise ValueError("Proposal fetch limit is invalid.")

    # Proposal IDs begin with a Unix timestamp and retain the legacy reverse
    # lexical ordering. Keep only the next bounded set of names while scanning
    # the directory, then parse only those limit+1 JSON documents.
    candidate_names: list[str] = []
    with os.scandir(store.proposals) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            name = entry.name
            if after_id is not None and name >= after_id:
                continue
            proposal_path = Path(entry.path) / "proposal.json"
            if not proposal_path.is_file() or proposal_path.is_symlink():
                continue
            if len(candidate_names) < fetch_limit:
                heapq.heappush(candidate_names, name)
            elif name > candidate_names[0]:
                heapq.heapreplace(candidate_names, name)
    return [
        _proposal_summary(store.proposals / name / "proposal.json")
        for name in sorted(candidate_names, reverse=True)
    ]


def _reload_generation(path: Path) -> int | None:
    try:
        generation = int(path.read_text().strip())
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return None
    return generation if generation >= 0 else None


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
            requested = _reload_generation(request_path)
            applied = _reload_generation(RELOAD_DIR / "applied")
            generation = max(
                requested if requested is not None else 0,
                applied if applied is not None else 0,
            ) + 1
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
        value = _reload_generation(RELOAD_DIR / name)
        return value if value is not None else 0

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


def _visual_override(payload: dict) -> dict:
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
    return override


def apply_visual_override(plan: dict, payload: dict) -> dict:
    override = _visual_override(payload)
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
    if requested is not None:
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


def workspace_map_extent(
    workspace: dict,
    locale_key: str | None = None,
) -> dict[str, Any]:
    selected_locale, locale = select_locale(workspace, locale_key)
    view = locale.get("view")
    if not isinstance(view, dict):
        raise ValueError(
            f"Locale {selected_locale!r} needs view.lng, view.lat, and view.z "
            "before a workspace map extent can be calculated."
        )

    values: dict[str, float] = {}
    for key in ("lng", "lat", "z"):
        value = view.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(
                f"Locale {selected_locale!r} view.{key} must be a finite number "
                "before a workspace map extent can be calculated."
            )
        values[key] = float(value)

    longitude = values["lng"]
    latitude = values["lat"]
    zoom = values["z"]
    if longitude < -180 or longitude > 180:
        raise ValueError(
            f"Locale {selected_locale!r} view.lng must be between -180 and 180."
        )
    if latitude < -90 or latitude > 90:
        raise ValueError(
            f"Locale {selected_locale!r} view.lat must be between -90 and 90."
        )
    if zoom < 0 or zoom > 30:
        raise ValueError(
            f"Locale {selected_locale!r} view.z must be between 0 and 30."
        )

    scope_zoom = max(0.0, zoom - 1.0)

    def coordinate(value: float) -> float:
        rounded = round(value, 12)
        return 0.0 if rounded == 0 else rounded

    configured_extent = locale.get("extent")
    extent_keys = ("west", "south", "east", "north")
    if isinstance(configured_extent, dict) and all(
        key in configured_extent for key in extent_keys
    ):
        extent_values: dict[str, float] = {}
        for key in extent_keys:
            value = configured_extent[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(
                    f"Locale {selected_locale!r} extent.{key} must be a finite "
                    "number before a workspace map extent can be calculated."
                )
            extent_values[key] = float(value)
        west = extent_values["west"]
        south = extent_values["south"]
        east = extent_values["east"]
        north = extent_values["north"]
        if (
            west < -180
            or west > 180
            or east < -180
            or east > 180
            or west == east
            or south < -90
            or north > 90
            or south >= north
        ):
            raise ValueError(
                f"Locale {selected_locale!r} extent bounds are invalid."
            )
        if west < east:
            longitude_ranges = [(west, east)]
        else:
            longitude_ranges = []
            if west < 180:
                longitude_ranges.append((west, 180.0))
            if east > -180:
                longitude_ranges.append((-180.0, east))
            if not longitude_ranges:
                raise ValueError(
                    f"Locale {selected_locale!r} extent bounds are invalid."
                )
        envelopes = [
            {"west": west, "south": south, "east": east, "north": north}
            for west, east in longitude_ranges
        ]
        scope_source = "configured locale extent"
    else:
        world_size = MAP_EXTENT_TILE_SIZE * (2.0 ** scope_zoom)
        half_longitude_span = (
            MAP_EXTENT_VIEWPORT_WIDTH / world_size * 360.0 / 2.0
        )
        projected_latitude = max(
            -WEB_MERCATOR_MAX_LATITUDE,
            min(WEB_MERCATOR_MAX_LATITUDE, latitude),
        )
        latitude_radians = math.radians(projected_latitude)
        centre_y = (
            1.0
            - math.asinh(math.tan(latitude_radians)) / math.pi
        ) / 2.0 * world_size
        north_y = max(0.0, centre_y - MAP_EXTENT_VIEWPORT_HEIGHT / 2.0)
        south_y = min(world_size, centre_y + MAP_EXTENT_VIEWPORT_HEIGHT / 2.0)

        def latitude_at(pixel_y: float) -> float:
            return math.degrees(math.atan(math.sinh(
                math.pi * (1.0 - 2.0 * pixel_y / world_size)
            )))

        north = coordinate(latitude_at(north_y))
        south = coordinate(latitude_at(south_y))
        longitude_span = half_longitude_span * 2.0
        if longitude_span >= 360.0:
            longitude_ranges = [(-180.0, 180.0)]
        else:
            west = longitude - half_longitude_span
            east = longitude + half_longitude_span
            if west < -180.0:
                longitude_ranges = [
                    (west + 360.0, 180.0),
                    (-180.0, east),
                ]
            elif east > 180.0:
                longitude_ranges = [
                    (west, 180.0),
                    (-180.0, east - 360.0),
                ]
            else:
                longitude_ranges = [(west, east)]
        envelopes = [
            {
                "west": coordinate(west),
                "south": south,
                "east": coordinate(east),
                "north": north,
            }
            for west, east in longitude_ranges
        ]
        scope_source = "startup-view fallback envelope"

    def json_number(value: float) -> int | float:
        return int(value) if value.is_integer() else value

    return {
        "type": "workspace-map-extent",
        "locale": selected_locale,
        "sourceView": {
            "lng": json_number(longitude),
            "lat": json_number(latitude),
            "z": json_number(zoom),
        },
        "scopeZoom": json_number(scope_zoom),
        "zoomOffset": json_number(scope_zoom - zoom),
        "viewport": {
            "width": MAP_EXTENT_VIEWPORT_WIDTH,
            "height": MAP_EXTENT_VIEWPORT_HEIGHT,
            "tileSize": MAP_EXTENT_TILE_SIZE,
        },
        "crs": "EPSG:4326",
        "envelopes": envelopes,
        "selection": "intersects-output-geometry",
        "clipsGeometry": False,
        "guidance": (
            "This output-row guard uses the selected locale's "
            f"{scope_source}; it keeps complete output features intersecting "
            "that fixed extent. It is not a security "
            "boundary and does not scope source-side aggregates, clip "
            "geometry, or follow later map movements. Add the envelope inside "
            "source-side SQL before aggregation when metrics must be map-scoped."
        ),
    }


def visual_hover_plan(layer: dict) -> dict | None:
    style = layer.get("style")
    if not isinstance(style, dict):
        return None
    hovers = style.get("hovers")
    hover = style.get("hover")
    if hover is None and isinstance(hovers, dict) and hovers:
        hover = next(iter(hovers.values()))
    elif isinstance(hover, str) and isinstance(hovers, dict):
        hover = hovers.get(hover)
    if not isinstance(hover, dict) or hover.get("display") is not True:
        return None
    return {
        "type": "hover-centre-feature",
        "field": hover.get("field"),
        "title": hover.get("title"),
    }


def effective_layer_filter_descriptor(layer: dict) -> tuple[dict, list[tuple]]:
    """Describe configured render restrictions without assuming a SQL source."""
    configured = layer.get("filter")
    predicate = configured.get("default") if isinstance(configured, dict) else None
    qid = layer.get("qID")
    identifier_sets = []

    def comparable_identifiers(values):
        return [
            value for value in values
            if value is None or isinstance(value, (str, int, float, bool))
        ]

    feature_set = layer.get("featureSet")
    # XYZ gates featureSet through Set.size; an explicitly empty array is not
    # a restriction, while an empty featureLookup array matches no feature.
    if isinstance(feature_set, list) and feature_set:
        identifier_sets.append((
            "featureSet",
            comparable_identifiers(feature_set),
            len(feature_set),
        ))
    feature_lookup = layer.get("featureLookup")
    if isinstance(feature_lookup, list):
        lookup_id = layer.get("featureLookupId") or "id"
        identifiers = [
            item[lookup_id]
            for item in feature_lookup
            if isinstance(item, dict) and lookup_id in item
        ]
        identifier_sets.append((
            "featureLookup",
            comparable_identifiers(identifiers),
            len(feature_lookup),
        ))

    restrictions = []
    if predicate is not None:
        restrictions.append("filter.default")
    restrictions.extend(source for source, _, _ in identifier_sets)
    return (
        {
            "fixedFilter": copy.deepcopy(predicate),
            "filterApplied": predicate is not None,
            "identifierRestrictions": [
                {
                    "source": source,
                    "field": qid,
                    "configuredCount": configured_count,
                    "comparablePrimitiveCount": len(identifiers),
                    "ignoredCount": configured_count - len(identifiers),
                }
                for source, identifiers, configured_count in identifier_sets
            ],
            "restrictions": restrictions,
        },
        identifier_sets,
    )


def effective_layer_filter(layer: dict):
    """Compile the static restrictions that decide which features can render."""
    configured = layer.get("filter")
    predicate = configured.get("default") if isinstance(configured, dict) else None
    descriptor, identifier_sets = effective_layer_filter_descriptor(layer)
    clauses = []
    params = []

    def compile_mapping(mapping: dict):
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("Layer filter.default objects must be non-empty.")
        mapping_clauses = []
        mapping_params = []
        operations = {
            "eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
        }

        def xyz_scalar_text(value):
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)

        for field, tests in mapping.items():
            if not isinstance(field, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", field
            ):
                raise ValueError("Layer filter.default contains an invalid field name.")
            if isinstance(tests, list):
                raise ValueError(
                    "Layer filter.default field-level OR arrays are not supported; "
                    "use a top-level OR-array or a reviewed predicate string."
                )
            if not isinstance(tests, dict) or not tests:
                raise ValueError("Layer filter.default field tests must be non-empty objects.")
            unsupported = set(tests) - {
                *operations, "boolean", "null", "in", "ni", "match", "like",
            }
            if unsupported:
                name = sorted(unsupported)[0]
                raise ValueError(
                    f"Layer filter.default uses unsupported operation: {name}."
                )
            field_sql = sql.Identifier(field)
            field_clauses = []
            for operation, value in tests.items():
                if operation in operations:
                    try:
                        numeric_value = float(value)
                    except (OverflowError, TypeError, ValueError):
                        numeric_value = math.nan
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (str, int, float))
                        or isinstance(value, str)
                        and not FIXED_FILTER_NUMBER_RE.fullmatch(value)
                        or not math.isfinite(numeric_value)
                    ):
                        raise ValueError(
                            "Layer filter.default comparisons require a "
                            "finite number or numeric string."
                        )
                    field_clauses.append(sql.SQL("{} {} %s").format(
                        field_sql,
                        sql.SQL(operations[operation]),
                    ))
                    mapping_params.append(xyz_scalar_text(value))
                elif operation == "boolean":
                    if not isinstance(value, bool):
                        raise ValueError(
                            "Layer filter.default boolean values must be true or false."
                        )
                    field_clauses.append(sql.SQL("{} IS {}").format(
                        field_sql,
                        sql.SQL("TRUE" if value else "FALSE"),
                    ))
                elif operation == "null":
                    if not isinstance(value, bool):
                        raise ValueError(
                            "Layer filter.default null values must be true or false."
                        )
                    field_clauses.append(sql.SQL("{} IS {}NULL").format(
                        field_sql, sql.SQL("") if value else sql.SQL("NOT "),
                    ))
                elif operation in {"in", "ni"}:
                    values = value if isinstance(value, list) else [value]
                    if not values or any(
                        item is None
                        or isinstance(item, (dict, list))
                        or not isinstance(item, (str, int, float, bool))
                        or isinstance(item, float) and not math.isfinite(item)
                        for item in values
                    ):
                        raise ValueError(
                            "Layer filter.default in/ni values must be a "
                            "non-empty scalar or array of finite scalars."
                        )
                    # XYZ sends a JavaScript array through node-postgres, so
                    # PostgreSQL coerces each textual array member to the
                    # compared column type. Expand safe text parameters here
                    # to preserve that behaviour without asking psycopg to
                    # adapt a potentially heterogeneous Python array.
                    comparison = sql.SQL("({})").format(sql.SQL(" OR ").join(
                        sql.SQL("{} = %s").format(field_sql)
                        for _ in values
                    ))
                    field_clauses.append(
                        comparison if operation == "in"
                        else sql.SQL("NOT ({})").format(comparison)
                    )
                    mapping_params.extend(
                        xyz_scalar_text(item) for item in values
                    )
                elif operation == "match":
                    if not isinstance(value, str):
                        raise ValueError(
                            "Layer filter.default match values must be strings."
                        )
                    field_clauses.append(sql.SQL("{}::text = %s").format(field_sql))
                    mapping_params.append(value)
                elif operation == "like":
                    if not isinstance(value, str):
                        raise ValueError(
                            "Layer filter.default like values must be strings."
                        )
                    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
                        raise ValueError(
                            "Layer filter.default like values contain invalid URL encoding."
                        )
                    try:
                        decoded = unquote(value, errors="strict")
                    except UnicodeDecodeError as exc:
                        raise ValueError(
                            "Layer filter.default like values contain invalid "
                            "UTF-8 URL encoding."
                        ) from exc
                    values = [item for item in decoded.split(",") if item]
                    if not values:
                        raise ValueError("Layer filter.default like values must be non-empty.")
                    field_clauses.append(sql.SQL("({})").format(sql.SQL(" OR ").join(
                        sql.SQL("{} ILIKE %s").format(field_sql) for _ in values
                    )))
                    mapping_params.extend(f"{item}%" for item in values)
            mapping_clauses.append(
                sql.SQL("({})").format(sql.SQL(" AND ").join(field_clauses))
            )
        return (
            sql.SQL("({})").format(sql.SQL(" AND ").join(mapping_clauses)),
            mapping_params,
        )

    if predicate is not None:
        if isinstance(predicate, str):
            error = expression_error(predicate)
            if error:
                raise ValueError(f"Invalid layer filter.default: {error}")
            # The predicate is a validated XYZ SQL fragment, but psycopg uses
            # percent signs for client-side placeholders whenever a parameter
            # sequence is supplied. Double literal percents so modulo and
            # LIKE patterns reach PostgreSQL unchanged.
            clauses.append(sql.SQL("({})").format(
                sql.SQL(predicate.replace("%", "%%"))
            ))
        elif isinstance(predicate, list):
            if not predicate:
                raise ValueError("Layer filter.default arrays must be non-empty.")
            compiled = [compile_mapping(item) for item in predicate]
            clauses.append(sql.SQL("({})").format(
                sql.SQL(" OR ").join(item[0] for item in compiled)
            ))
            params.extend(value for item in compiled for value in item[1])
        elif isinstance(predicate, dict):
            compiled_filter, compiled_params = compile_mapping(predicate)
            clauses.append(compiled_filter)
            params.extend(compiled_params)
        else:
            raise ValueError(
                "Layer filter.default must be a predicate string, object, or array."
            )
    qid = layer.get("qID")
    for source, identifiers, _ in identifier_sets:
        try:
            encoded_identifiers = json.dumps(
                identifiers,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Layer {source} must contain valid JSON feature IDs."
            ) from exc
        clauses.append(sql.SQL(
            "%s::jsonb @> pg_catalog.jsonb_build_array("
            "pg_catalog.to_jsonb({}))"
        ).format(sql.Identifier(qid)))
        params.append(encoded_identifiers)

    return (
        (
            sql.SQL("({})").format(sql.SQL(" AND ").join(clauses))
            if clauses
            else sql.SQL("TRUE")
        ),
        params,
        descriptor,
    )


def visual_plan(
    workspace: dict,
    layer_key: str,
    db_connections: dict[str, str],
    locale_key: str | None = None,
    *,
    visual_request: dict | None = None,
) -> dict:
    visual_request = visual_request or {}
    override = _visual_override(visual_request)
    selected_locale, locale = select_locale(workspace, locale_key)
    background_layers = [
        key
        for key, candidate in (locale.get("layers") or {}).items()
        if (
            isinstance(key, str)
            and isinstance(candidate, dict)
            and candidate.get("format") == "tiles"
            and candidate.get("display") is True
        )
    ]
    layer = (locale.get("layers") or {}).get(layer_key)
    if not isinstance(layer, dict):
        raise ValueError(
            f"Unknown layer in locale {selected_locale}: {layer_key}"
        )
    layer_title = layer.get("name") if isinstance(layer.get("name"), str) else layer_key
    layer_title = layer_title.strip() or layer_key
    hover_plan = visual_hover_plan(layer)
    probeable = is_probeable_database_layer(layer)
    filter_descriptor, _ = effective_layer_filter_descriptor(layer)
    activation = {
        "configuredKey": layer_key,
        "displayName": layer_title,
        "group": layer.get("group"),
        "mode": "focused-url",
    }
    dataset_source = (
        {
            "database": layer.get("dbs") or workspace.get("dbs"),
            "relation": layer["table"],
            "geometryField": layer["geom"],
            "featureIdField": layer["qID"],
        }
        if probeable
        else {"type": "browser-managed"}
    )
    if "centre" in override and "zoom" in override:
        plan = {
            "layer": layer_key,
            "layerTitle": layer_title,
            "locale": selected_locale,
            "source": "browser-centre-feature",
            "backgroundLayers": background_layers,
            "centre": override["centre"],
            "zoom": override["zoom"],
            "warnings": [
                "The complete explicit view skips database-wide feature-count "
                "and extent queries; browser interaction targets the map centre."
            ],
            "effectiveDataset": {
                "locale": selected_locale,
                "layerKey": layer_key,
                "layerName": layer_title,
                "source": dataset_source,
                "effectiveFilter": filter_descriptor,
                "query": {
                    "scope": "explicit-browser-view",
                    "skipped": True,
                    "reason": "complete-explicit-view",
                },
                "activation": activation,
                "filteredFeatureCount": None,
                "representativeFeature": None,
            },
        }
        if probeable:
            # Keep fixed-filter validation even when an explicit browser view
            # intentionally skips the database summary query.
            effective_layer_filter(layer)
            plan.update({
                "database": layer.get("dbs") or workspace.get("dbs"),
                "table": layer["table"],
                "geometry": layer["geom"],
                "featureIdField": layer["qID"],
                "interaction": {
                    "type": "click-centre-feature",
                    "expectedLayer": layer_key,
                    "expectedLayerTitle": layer_title,
                },
            })
        if hover_plan:
            plan["hover"] = hover_plan
        return apply_visual_override(plan, visual_request)
    if not probeable:
        view = locale.get("view") or {}
        plan = {
            "layer": layer_key,
            "layerTitle": layer_title,
            "locale": selected_locale,
            "source": "workspace-view",
            "backgroundLayers": background_layers,
            "warnings": [
                "This layer uses an external or advanced XYZ source, so the "
                "visual check uses the configured workspace view."
            ],
            "effectiveDataset": {
                "locale": selected_locale,
                "layerKey": layer_key,
                "layerName": layer_title,
                "source": dataset_source,
                "effectiveFilter": filter_descriptor,
                "query": {
                    "scope": "browser-runtime",
                    "skipped": True,
                    "reason": "non-probeable-layer-source",
                },
                "activation": activation,
                "filteredFeatureCount": None,
                "representativeFeature": None,
            },
        }
        if hover_plan:
            plan["hover"] = hover_plan
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
        return apply_visual_override(plan, visual_request)
    if psycopg is None or sql is None:
        raise RuntimeError("PostgreSQL support is unavailable.")
    db_name = layer.get("dbs") or workspace.get("dbs")
    database_url = db_connections.get(db_name)
    parsed = parse_relation(layer.get("table"), alias=None, default_schema="public")
    if not database_url or parsed is None:
        raise ValueError("Layer database or relation is unavailable.")
    _, schema_name, table_name = parsed
    relation_sql = sql.SQL("{}.{}").format(sql.Identifier(schema_name), sql.Identifier(table_name))
    geom = sql.Identifier(layer["geom"])
    default_filter, default_filter_params, filter_descriptor = (
        effective_layer_filter(layer)
    )
    filter_applied = filter_descriptor["filterApplied"]
    effective_dataset = {
        "locale": selected_locale,
        "layerKey": layer_key,
        "layerName": layer_title,
        "source": {
            "database": db_name,
            "relation": layer["table"],
            "geometryField": layer["geom"],
            "featureIdField": layer["qID"],
        },
        "effectiveFilter": filter_descriptor,
        "query": {
            "scope": "effective-locale-layer",
            "conditions": [
                *filter_descriptor["restrictions"],
                "geometry IS NOT NULL",
                "geometry IS NOT EMPTY",
            ],
            "summary": "filtered-feature-count-and-extent",
            "representative": "nearest-feature-to-filtered-extent-centre",
        },
        "activation": activation,
    }
    query = sql.SQL("""
      WITH rendered AS (
        SELECT *
        FROM {relation}
        WHERE {default_filter}
      )
      SELECT feature_count,
             ST_XMin(extent), ST_YMin(extent), ST_XMax(extent), ST_YMax(extent),
             sample.geometry_type
      FROM (
        SELECT count(*)::bigint AS feature_count,
               ST_Extent(ST_Transform({geom}, 3857)) AS extent
        FROM rendered
        WHERE {geom} IS NOT NULL
          AND NOT ST_IsEmpty({geom})
      ) bounds
      LEFT JOIN LATERAL (
        SELECT GeometryType({geom}) AS geometry_type
        FROM rendered
        WHERE {geom} IS NOT NULL
          AND NOT ST_IsEmpty({geom})
        LIMIT 1
      ) sample ON TRUE
    """).format(
        geom=geom,
        relation=relation_sql,
        default_filter=default_filter,
    )
    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    "SET statement_timeout = "
                    f"'{VISUAL_PLANNING_STATEMENT_TIMEOUT_MS}ms'"
                )
                cur.execute(query, default_filter_params)
                count, west, south, east, north, geometry_type = cur.fetchone()
    except psycopg.Error as exc:
        raise VisualPlanningDatabaseError(
            stage="layer-summary",
            query_purpose="feature-count-and-extent",
            timed_out=getattr(exc, "sqlstate", None) == "57014",
        ) from exc
    if not count or None in (west, south, east, north):
        raise VisualPlanningNoMatchingFeatures(
            filter_applied=filter_applied,
            effective_dataset={
                **effective_dataset,
                "filteredFeatureCount": 0,
                "representativeFeature": None,
            },
        )
    centre_x, centre_y = (west + east) / 2, (south + north) / 2
    sample_query = sql.SQL("""
      WITH rendered AS (
        SELECT *
        FROM {relation}
        WHERE {default_filter}
      ), candidate AS (
        SELECT pg_catalog.to_jsonb({qid}) AS feature_id,
               {geom} AS geom
        FROM rendered
        WHERE {geom} IS NOT NULL
          AND NOT ST_IsEmpty({geom})
        ORDER BY ST_Transform({geom}, 3857) <-> ST_SetSRID(ST_MakePoint(%s, %s), 3857)
        LIMIT 1
      ), prepared AS (
        SELECT feature_id,
               GeometryType(geom) AS geometry_type,
               ST_Extent(ST_Transform(geom, 3857)) AS extent,
               ST_Transform(ST_PointOnSurface(geom), 4326) AS target
        FROM candidate
        GROUP BY feature_id, GeometryType(geom), ST_Transform(ST_PointOnSurface(geom), 4326)
      )
      SELECT feature_id,
             geometry_type,
             ST_XMin(extent), ST_YMin(extent), ST_XMax(extent), ST_YMax(extent),
             ST_X(target), ST_Y(target)
      FROM prepared
    """).format(
        geom=geom,
        qid=sql.Identifier(layer["qID"]),
        relation=relation_sql,
        default_filter=default_filter,
    )
    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(
                    "SET statement_timeout = "
                    f"'{VISUAL_PLANNING_STATEMENT_TIMEOUT_MS}ms'"
                )
                cur.execute(
                    sample_query,
                    (*default_filter_params, centre_x, centre_y),
                )
                sample = cur.fetchone()
    except psycopg.Error as exc:
        raise VisualPlanningDatabaseError(
            stage="representative-feature",
            query_purpose="centre-feature-selection",
            timed_out=getattr(exc, "sqlstate", None) == "57014",
        ) from exc
    if sample is None:
        raise VisualPlanningNoMatchingFeatures(
            filter_applied=filter_applied,
            effective_dataset={
                **effective_dataset,
                "filteredFeatureCount": count,
                "representativeFeature": None,
            },
            reason="representative-feature-unavailable",
            stage="representative-feature",
        )
    (
        feature_id,
        sample_geometry_type,
        sample_west,
        sample_south,
        sample_east,
        sample_north,
        target_lng,
        target_lat,
    ) = sample
    focus_west = sample_west if sample_west is not None else west
    focus_south = sample_south if sample_south is not None else south
    focus_east = sample_east if sample_east is not None else east
    focus_north = sample_north if sample_north is not None else north
    width = max(focus_east - focus_west, 25.0)
    height = max(focus_north - focus_south, 25.0)
    resolution = max(width / (1920 * .45), height / (1080 * .45))
    zoom = max(0, min(22, math.log2(156543.03392804097 / resolution)))
    geometry_type = sample_geometry_type or geometry_type
    upper_geometry = (geometry_type or "").upper()
    if "POINT" in upper_geometry:
        zoom = max(16, zoom)
    elif "LINE" in upper_geometry:
        zoom = max(16, zoom)
    elif "POLYGON" in upper_geometry:
        zoom = max(14, zoom)
    plan = {
        "layer": layer_key,
        "layerTitle": layer_title,
        "locale": selected_locale,
        "source": "postgis-feature",
        "backgroundLayers": background_layers,
        "database": db_name,
        "table": layer["table"],
        "geometry": layer["geom"],
        "featureIdField": layer["qID"],
        "featureId": feature_id,
        "geometryType": geometry_type,
        "featureCount": count,
        "defaultFilterApplied": filter_applied,
        "effectiveDataset": {
            **effective_dataset,
            "filteredFeatureCount": count,
            "representativeFeature": {
                "id": feature_id,
                "geometryType": geometry_type,
                "bounds3857": [
                    focus_west, focus_south, focus_east, focus_north,
                ],
                "target": [target_lng, target_lat],
            },
        },
        "bounds3857": [west, south, east, north],
        "focusBounds3857": [focus_west, focus_south, focus_east, focus_north],
        "centre": [target_lng, target_lat],
        "zoom": round(zoom, 2),
        "interaction": {
            "type": "click-centre-feature",
            "expectedLayer": layer_key,
            "expectedLayerTitle": layer_title,
            "expectedFeatureId": feature_id,
        },
        "warnings": ["The visual check is focused on one representative feature."],
    }
    if hover_plan:
        plan["hover"] = hover_plan
    return apply_visual_override(plan, visual_request)
