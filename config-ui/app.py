from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import secrets
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

import psycopg
from psycopg.rows import dict_row

from infoj_types import info_value_error
from derived_layers import (
    SCHEMA as DERIVED_SCHEMA,
    DerivedLayerCancellation,
    DerivedLayerCancellationRequested,
    DerivedLayerContentionError,
    DerivedLayerDatabaseOperationError,
    DerivedLayerDependencyError,
    DerivedLayerError,
    DerivedLayerMaterializationTooLarge,
    DerivedLayerMaintenanceError,
    DerivedLayerQueryTooExpensive,
    DerivedLayerResetOwnershipError,
    DerivedLayerSourceMismatchError,
    DerivedLayerStore,
    area_weighted_h3_recipe_capability,
    plan_area_weighted_h3_recipe,
    validate_definition,
    validate_spatial_scope,
)
from static_files import safe_static_path
from svg_icons import safe_svg
from semantic_client import SemanticClient, SemanticClientError
from semantic_sources import (
    DEFAULT_ALLOWLIST as DEFAULT_SEMANTIC_SOURCE_ALLOWLIST,
    GENERATION_SAMPLE_MAX_BYTES,
    GENERATION_SAMPLE_MAX_COLUMNS,
    GENERATION_SAMPLE_MAX_ROWS,
    GENERATION_SAMPLE_PERCENT,
    GENERATION_SAMPLE_VALUE_MAX_CHARS,
    GENERATION_STATISTICS_MAX_ROWS,
    PostgresSemanticSources,
    SemanticSourceError,
    parse_allowlist as parse_semantic_source_allowlist,
    parse_exclusions as parse_semantic_source_exclusions,
    postgres_generation_context,
    source_asset_id,
    source_generated,
    validate_source_selector,
)
from gemini_client import (
    DEFAULT_MODEL as DEFAULT_GEMINI_MODEL,
    GeminiClientError,
    GeminiSemanticClient,
)
from workspace_schema import expression_function_names, validate_workspace
from plugin_registry import catalogue as plugin_catalogue, plugin_usage, validate_workspace_plugins
from control_plane import ControlStore, parse_time
from control_api import (
    CONTRACT_VERSION, MAX_PAGE_LIMIT, PROPOSAL_LOCK, RULES,
    CollectionPaginationError, DATABASE_LAYER_FORMATS, VisualPlanningDatabaseError,
    VisualPlanningNoMatchingFeatures,
    apply_operations, capabilities, contract, examples, plugin_manifest,
    decode_position_cursor,
    effective_layer_filter, effective_locales, enforce_collection_payload,
    is_probeable_database_layer,
    legacy_collection, paginate_collection, paginate_keyset_page,
    pagination_parameters,
    pointer_get, pointer_parts, proposal_check, proposal_create, proposal_list, proposal_read, proposal_write,
    reload_status, reload_timeout, request_reload, visual_hover_plan,
    schema as contract_schema, select_locale, visual_plan,
    strict_json_loads, wait_reload, workspace_fingerprint, workspace_hash,
    workspace_map_extent,
)

ROOT = Path(__file__).parent
LOCAL_RUNTIME = Path(tempfile.gettempdir()) / "mapp-config"
WORKSPACE = Path(
    os.environ.get("WORKSPACE_PATH", str(LOCAL_RUNTIME / "workspace.json"))
)
STATIC = ROOT / "static"
STATIC_ROOT = STATIC.resolve()
SVG_DIR = Path(os.environ.get("SVG_DIR", str(LOCAL_RUNTIME / "public/svg")))
SVG_URL_PREFIX = "/instance/svg"
DB_CONNECTIONS = {
    key.removeprefix("DBS_"): value
    for key, value in os.environ.items()
    if key.startswith("DBS_") and value
}
LAYER_VALUES_DEFAULT_LIMIT = 100
LAYER_VALUES_MAX_LIMIT = 500
LAYER_STATISTICS_DEFAULT_BINS = 10
LAYER_STATISTICS_MAX_BINS = 50
LAYER_STATISTICS_MAX_THRESHOLDS = 20
SEMANTIC_SOURCE_ALLOWLIST = parse_semantic_source_allowlist(
    os.environ.get(
        "SEMANTIC_SOURCE_ALLOWLIST",
        DEFAULT_SEMANTIC_SOURCE_ALLOWLIST,
    )
)
SEMANTIC_SOURCE_EXCLUSIONS = parse_semantic_source_exclusions(
    os.environ.get("SEMANTIC_SOURCE_EXCLUSIONS", "")
)
SEMANTIC_SOURCES = PostgresSemanticSources(
    DB_CONNECTIONS,
    SEMANTIC_SOURCE_ALLOWLIST,
    SEMANTIC_SOURCE_EXCLUSIONS,
)
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = 5 * 1024 * 1024
DEFAULT_TOKEN_LIFETIME = timedelta(days=30)
_MISSING = object()


def requested_token_expiry(
    payload: dict,
    *,
    current_time: datetime | None = None,
) -> str | None:
    """Apply the dashboard token lifetime policy before token creation."""
    confirmed = payload.get("extendedExpiryConfirmed", False)
    if not isinstance(confirmed, bool):
        raise ValueError("Extended token expiry confirmation must be a boolean.")

    current_time = current_time or datetime.now(timezone.utc)
    if "expires" not in payload:
        return (current_time + DEFAULT_TOKEN_LIFETIME).isoformat()

    requested = payload["expires"]
    if requested is None:
        if not confirmed:
            raise ValueError(
                "Non-expiring tokens require explicit extended-expiry confirmation."
            )
        return None

    expiry = parse_time(requested)
    if expiry is None:
        raise ValueError("Token expiry must be an ISO-8601 timestamp.")
    if expiry > current_time + DEFAULT_TOKEN_LIFETIME and not confirmed:
        raise ValueError(
            "Token lifetimes longer than 30 days require explicit confirmation."
        )
    return requested
try:
    DERIVED_MAX_BACKGROUND_JOBS = int(
        os.environ.get("DERIVED_MAX_BACKGROUND_JOBS", "1")
    )
except ValueError:
    raise RuntimeError(
        "DERIVED_MAX_BACKGROUND_JOBS must be an integer between 1 and 4."
    ) from None
if not 1 <= DERIVED_MAX_BACKGROUND_JOBS <= 4:
    raise RuntimeError("DERIVED_MAX_BACKGROUND_JOBS must be between 1 and 4.")
try:
    VISUAL_BROWSER_TIMEOUT_SECONDS = int(
        os.environ.get("VISUAL_BROWSER_TIMEOUT_SECONDS", "90")
    )
    VISUAL_BACKGROUND_TIMEOUT_SECONDS = int(
        os.environ.get("VISUAL_BACKGROUND_TIMEOUT_SECONDS", "300")
    )
except ValueError:
    raise RuntimeError("Visual timeout settings must be whole seconds.") from None
if not 10 <= VISUAL_BROWSER_TIMEOUT_SECONDS <= 180:
    raise RuntimeError(
        "VISUAL_BROWSER_TIMEOUT_SECONDS must be between 10 and 180."
    )
if not 30 <= VISUAL_BACKGROUND_TIMEOUT_SECONDS <= 600:
    raise RuntimeError(
        "VISUAL_BACKGROUND_TIMEOUT_SECONDS must be between 30 and 600."
    )
SAVE_LOCK = threading.Lock()
SAVE_RELOAD_LOCK = threading.Lock()
PREVIEW_LOCK = threading.RLock()
DERIVED_BACKGROUND_JOB_LOCK = threading.Lock()
DERIVED_BACKGROUND_ACTIVE_JOBS = 0
DERIVED_BACKGROUND_CANCELLATIONS: dict[str, DerivedLayerCancellation] = {}
PREVIEW_SYNC_LOCK = threading.Lock()
PREVIEW_SYNC_STATE: dict[str, object] = {
    "pending": None,
    "running": False,
}
PREVIEW_WORKSPACE = Path(
    os.environ.get(
        "PREVIEW_WORKSPACE_PATH",
        str(LOCAL_RUNTIME / "preview/workspace.json"),
    )
)
PREVIEW_RELOAD_DIR = Path(
    os.environ.get(
        "PREVIEW_RELOAD_DIR",
        str(LOCAL_RUNTIME / "preview-reload"),
    )
)
CONTROL = ControlStore(
    Path(os.environ.get("CONTROL_DIR", str(LOCAL_RUNTIME / "control")))
)
CONTROL.recover_interrupted_operations()
DERIVED = (
    DerivedLayerStore(
        os.environ["DERIVED_DATABASE_URL"],
        os.environ["DERIVED_READER_ROLE"],
    )
    if os.environ.get("DERIVED_DATABASE_URL")
    else None
)


class DerivedLayerBackgroundCapacityError(DerivedLayerError):
    def __init__(self, active_jobs: int, max_active_jobs: int):
        self.active_jobs = active_jobs
        self.max_active_jobs = max_active_jobs
        super().__init__(
            "The derived-layer background worker is busy. Wait for the active "
            "operation to finish before retrying."
        )
SEMANTIC = (
    SemanticClient(
        os.environ["SEMANTIC_SERVICE_URL"],
        os.environ["SEMANTIC_INTERNAL_TOKEN"],
    )
    if (
        os.environ.get("SEMANTIC_SERVICE_URL")
        and os.environ.get("SEMANTIC_INTERNAL_TOKEN")
    )
    else None
)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
try:
    GEMINI = (
        GeminiSemanticClient(
            os.environ["GEMINI_APIKEY"],
            model=GEMINI_MODEL,
        )
        if os.environ.get("GEMINI_APIKEY")
        else None
    )
    GEMINI_CONFIGURATION_ERROR = None
except GeminiClientError as exc:
    GEMINI = None
    GEMINI_CONFIGURATION_ERROR = exc
SEMANTIC_OUTBOX_LOCK = threading.Lock()
SEMANTIC_OUTBOX_WAKE = threading.Event()
SEMANTIC_MAX_ATTEMPTS = 8
SEMANTIC_SOURCE_LOCK = threading.Lock()


def semantic_generation_capability() -> dict:
    return {
        "available": GEMINI is not None,
        "provider": "gemini",
        "model": GEMINI_MODEL,
        "targets": ["table", "field"],
        "metadataOnly": True,
        "contextOptions": {
            "sampleRows": {
                "available": True,
                "percent": GENERATION_SAMPLE_PERCENT,
                "maxRows": GENERATION_SAMPLE_MAX_ROWS,
                "maxBytes": GENERATION_SAMPLE_MAX_BYTES,
                "maxColumns": GENERATION_SAMPLE_MAX_COLUMNS,
                "maxValueCharacters": GENERATION_SAMPLE_VALUE_MAX_CHARS,
                "requiredScope": "semantic:data",
            },
            "statistics": {
                "available": True,
                "fieldSamplePercent": GENERATION_SAMPLE_PERCENT,
                "fieldMaxSampledRows": GENERATION_STATISTICS_MAX_ROWS,
                "requiredScope": "semantic:data",
            },
        },
    }


def _semantic_generation_request(
    payload: dict,
) -> tuple[str, dict, dict[str, bool]]:
    if (
        not isinstance(payload, dict)
        or not {"assetId", "target"} <= set(payload)
        or not set(payload) <= {"assetId", "target", "contextOptions"}
    ):
        raise GeminiClientError(
            "Semantic generation requires assetId, target, and optional "
            "contextOptions.",
            status=HTTPStatus.BAD_REQUEST,
            code="semantic.generation_invalid_request",
        )
    asset_id = payload.get("assetId")
    target = payload.get("target")
    if (
        not isinstance(asset_id, str)
        or not asset_id.strip()
        or len(asset_id) > 200
        or not isinstance(target, dict)
    ):
        raise GeminiClientError(
            "Semantic generation request is invalid.",
            status=HTTPStatus.BAD_REQUEST,
            code="semantic.generation_invalid_request",
        )
    raw_options = payload.get("contextOptions", {})
    if (
        not isinstance(raw_options, dict)
        or not set(raw_options) <= {"sampleRows", "statistics"}
        or any(
            key in raw_options and not isinstance(raw_options[key], bool)
            for key in ("sampleRows", "statistics")
        )
    ):
        raise GeminiClientError(
            "contextOptions accepts only boolean sampleRows and statistics.",
            status=HTTPStatus.BAD_REQUEST,
            code="semantic.generation_invalid_request",
        )
    context_options = {
        "sampleRows": raw_options.get("sampleRows", False),
        "statistics": raw_options.get("statistics", False),
    }
    kind = target.get("kind")
    if kind == "table" and set(target) == {"kind"}:
        return asset_id, {"kind": "table"}, context_options
    if kind == "field" and set(target) == {"kind", "fieldId"}:
        field_id = target.get("fieldId")
        if (
            isinstance(field_id, str)
            and field_id.strip()
            and len(field_id) <= 200
        ):
            return (
                asset_id,
                {"kind": "field", "fieldId": field_id},
                context_options,
            )
    raise GeminiClientError(
        "target must select a table or one stable fieldId.",
        status=HTTPStatus.BAD_REQUEST,
        code="semantic.generation_invalid_request",
    )


def _generated_table_identity(generated: dict) -> dict:
    identity = {}
    for key in (
        "name",
        "kind",
        "description",
        "idColumn",
        "geometryColumn",
        "geometryType",
        "srid",
    ):
        value = generated.get(key)
        if key in generated and (
            value is None or isinstance(value, (str, int, float, bool))
        ):
            identity[key] = value
    binding = generated.get("binding")
    if isinstance(binding, dict):
        filtered_binding = {
            key: binding[key]
            for key in ("adapter", "alias", "schema", "relation")
            if isinstance(binding.get(key), str)
        }
        if filtered_binding:
            identity["binding"] = filtered_binding
    spatial_scope = generated.get("spatialScope")
    if spatial_scope is not None:
        try:
            identity["spatialScope"] = validate_spatial_scope(spatial_scope)
        except DerivedLayerError:
            pass
    return identity


def _semantic_annotation(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("displayName", "description", "tags", "caveats")
        if key in value
    }


def _generated_field(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in (
            "name",
            "type",
            "nullable",
            "description",
            "geometryType",
            "srid",
        )
        if key in value
        and (
            value.get(key) is None
            or isinstance(value.get(key), (str, int, float, bool))
        )
    }


def semantic_generation_context(
    asset: dict,
    target: dict,
) -> tuple[dict, str | None]:
    generated = asset.get("generated")
    curated = asset.get("curated")
    if not isinstance(generated, dict):
        raise GeminiClientError(
            "Semantic asset generated metadata is invalid.",
            status=HTTPStatus.BAD_GATEWAY,
            code="semantic.generation_context_invalid",
        )
    if not isinstance(curated, dict):
        raise GeminiClientError(
            "Semantic asset curated metadata is invalid.",
            status=HTTPStatus.BAD_GATEWAY,
            code="semantic.generation_context_invalid",
        )
    identity = _generated_table_identity(generated)
    if target["kind"] == "table":
        fields = generated.get("fields")
        fields = fields if isinstance(fields, list) else []
        return {
            "target": {"kind": "table"},
            "table": {
                **identity,
                "fields": [
                    field
                    for raw in fields
                    if (field := _generated_field(raw))
                ],
            },
            "currentAnnotation": _semantic_annotation(curated),
        }, None

    fields = generated.get("fields")
    fields = fields if isinstance(fields, list) else []
    matches = [
        field
        for field in fields
        if isinstance(field, dict) and field.get("id") == target["fieldId"]
    ]
    if len(matches) != 1:
        raise GeminiClientError(
            "The selected semantic field was not found.",
            status=HTTPStatus.NOT_FOUND,
            code="semantic.field_not_found",
        )
    curated_fields = curated.get("fields")
    if "fields" in curated and not isinstance(curated_fields, dict):
        raise GeminiClientError(
            "Semantic field annotations are not an object.",
            status=HTTPStatus.BAD_GATEWAY,
            code="semantic.generation_context_invalid",
        )
    current_annotation = (
        curated_fields.get(target["fieldId"])
        if isinstance(curated_fields, dict)
        else None
    )
    return {
        "target": {"kind": "field"},
        "table": identity,
        "field": _generated_field(matches[0]),
        "currentAnnotation": _semantic_annotation(current_annotation),
    }, str(matches[0].get("name") or "")


def _semantic_generation_sample_seed(asset: dict) -> float:
    identity = (
        f"{asset.get('id', '')}:{asset.get('version', '')}"
    ).encode("utf-8")
    number = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")
    return (number / ((1 << 64) - 1)) * 2 - 1


def semantic_generation_optional_context(
    asset: dict,
    target: dict,
    context_options: dict[str, bool],
) -> dict:
    if not any(context_options.values()):
        return {}
    generated = asset.get("generated")
    if not isinstance(generated, dict):
        raise GeminiClientError(
            "Semantic asset generated metadata is invalid.",
            status=HTTPStatus.BAD_GATEWAY,
            code="semantic.generation_context_invalid",
        )
    binding = generated.get("binding")
    fields = generated.get("fields")
    if (
        not isinstance(binding, dict)
        or binding.get("adapter") != "postgresql"
        or not isinstance(fields, list)
    ):
        raise GeminiClientError(
            "Optional data context is unavailable for this semantic asset.",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="semantic.generation_context_unavailable",
        )
    schema = binding.get("schema")
    relation = binding.get("relation")
    if not isinstance(schema, str) or not isinstance(relation, str):
        raise GeminiClientError(
            "Optional data context is unavailable for this semantic asset.",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="semantic.generation_context_unavailable",
        )
    field_name = None
    if target["kind"] == "field":
        matches = [
            field
            for field in fields
            if (
                isinstance(field, dict)
                and field.get("id") == target["fieldId"]
            )
        ]
        if len(matches) != 1 or not isinstance(
            matches[0].get("name"), str
        ):
            raise GeminiClientError(
                "The selected semantic field was not found.",
                status=HTTPStatus.NOT_FOUND,
                code="semantic.field_not_found",
            )
        field_name = matches[0]["name"]
    arguments = {
        "schema": schema,
        "relation": relation,
        "fields": fields,
        "target_kind": target["kind"],
        "field_name": field_name,
        "sample_rows": context_options["sampleRows"],
        "statistics": context_options["statistics"],
        "sample_seed": _semantic_generation_sample_seed(asset),
    }
    try:
        alias = binding.get("alias")
        if isinstance(alias, str):
            if asset.get("id") != source_asset_id(alias, schema, relation):
                raise GeminiClientError(
                    "Semantic source binding does not match the asset.",
                    status=HTTPStatus.CONFLICT,
                    code="semantic.generation_context_invalid",
                )
            return SEMANTIC_SOURCES.generation_context(
                alias,
                **arguments,
            )
        if (
            schema != DERIVED_SCHEMA
            or generated.get("name") != relation
            or DERIVED is None
        ):
            raise GeminiClientError(
                "Optional data context is unavailable for this semantic asset.",
                status=HTTPStatus.UNPROCESSABLE_ENTITY,
                code="semantic.generation_context_unavailable",
            )
        definition = DERIVED.get(relation, include_query=False)
        profile = definition.get("semanticProfile")
        asset_generation = asset.get("generation")
        if (
            not isinstance(profile, dict)
            or profile.get("assetId") != asset.get("id")
            or profile.get("status") != "ready"
            or isinstance(asset_generation, bool)
            or not isinstance(asset_generation, int)
            or asset_generation < 1
            or isinstance(profile.get("generation"), bool)
            or profile.get("generation") != asset_generation
        ):
            raise GeminiClientError(
                "Derived-layer semantic profile is not current, ready, or "
                "matched to the semantic asset.",
                status=HTTPStatus.CONFLICT,
                code="semantic.generation_context_invalid",
            )
        return postgres_generation_context(
            DERIVED.connection_string,
            **arguments,
        )
    except GeminiClientError:
        raise
    except SemanticSourceError as exc:
        raise GeminiClientError(
            str(exc),
            status=exc.status,
            code=exc.code,
        ) from exc
    except (DerivedLayerError, FileNotFoundError, psycopg.Error) as exc:
        raise GeminiClientError(
            "Optional data context is unavailable for this semantic asset.",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            code="semantic.generation_context_unavailable",
        ) from exc


def _json_pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def semantic_generation_operations(
    target: dict,
    profile: dict,
    current_annotation: dict | None = None,
) -> list[dict]:
    prefix = "/curated"
    if target["kind"] == "field":
        prefix += f"/fields/{_json_pointer_part(target['fieldId'])}"
    current_annotation = (
        current_annotation
        if isinstance(current_annotation, dict)
        else {}
    )
    operations = [
        {
            "op": "set",
            "path": f"{prefix}/{key}",
            "value": profile[key],
        }
        for key in ("displayName", "description", "tags", "caveats")
        if current_annotation.get(key, _MISSING) != profile[key]
    ]
    if not operations:
        raise GeminiClientError(
            "Gemini returned the semantic annotation already stored.",
            status=HTTPStatus.CONFLICT,
            code="semantic.generation_no_change",
        )
    return operations


def semantic_proxy_path(path: str, query: str = "") -> str | None:
    static = {
        "/api/semantic/status": "/v1/status",
        "/api/semantic/catalog": "/v1/catalog",
        "/api/semantic/catalog/search": "/v1/search",
        "/api/semantic/proposals": "/v1/proposals",
        "/api/semantic/proposals/check": "/v1/proposals/check",
    }
    target = static.get(path)
    patterns = (
        (
            r"/api/semantic/catalog/objects/([^/]+)/history",
            "/v1/assets/{}/history",
        ),
        (
            r"/api/semantic/catalog/objects/([^/]+)",
            "/v1/assets/{}",
        ),
        (
            r"/api/semantic/proposals/([A-Za-z0-9._-]+)",
            "/v1/proposals/{}",
        ),
        (
            r"/api/semantic/proposals/([A-Za-z0-9._-]+)/(apply|decline)",
            "/v1/proposals/{}/{}",
        ),
    )
    if target is None:
        for pattern, template in patterns:
            match = re.fullmatch(pattern, path)
            if match:
                target = template.format(*match.groups())
                break
    if target is None:
        return None
    return target + (f"?{query}" if query else "")


def paginated_collection_payload(
    key: str,
    items: list,
    query: dict[str, list[str]],
    *,
    scope: str,
) -> dict:
    if not query:
        return {key: items}
    limit, cursor = pagination_parameters(query)
    page_items, pagination = paginate_collection(
        items,
        limit=limit,
        cursor=cursor,
        scope=scope,
    )
    return {key: page_items, "pagination": pagination}


def derived_semantic_profiles(
    *,
    include_delivery_diagnostics: bool = False,
    delivery_blockers: list[dict] | None = None,
    name: str | None = None,
    after_name: str | None = None,
    fetch_limit: int | None = None,
) -> list[dict]:
    if not DERIVED:
        raise DerivedLayerError(
            "Derived-layer database management is not configured."
        )
    if name is not None:
        definitions = [DERIVED.get(name, include_query=False)]
    elif fetch_limit is not None:
        definitions = DERIVED.list_page(
            after_name=after_name,
            fetch_limit=fetch_limit,
        )
    else:
        definitions = DERIVED.list()
    profiles = [
        {
            "name": item["name"],
            "relation": f"derived_layers.{item['name']}",
            "kind": item["kind"],
            **item["semanticProfile"],
        }
        for item in definitions
    ]
    if not include_delivery_diagnostics:
        return profiles
    blockers = (
        delivery_blockers
        if delivery_blockers is not None
        else DERIVED.semantic_outbox_blockers()
    )
    add_semantic_delivery_diagnostics(profiles, blockers)
    return profiles


def add_semantic_delivery_diagnostics(
    profiles: list[dict],
    blockers: list[dict],
) -> None:
    blockers_by_name = {}
    for blocker in blockers:
        name = blocker.get("name")
        if isinstance(name, str) and name and name not in blockers_by_name:
            blockers_by_name[name] = blocker
    for profile in profiles:
        blocker = blockers_by_name.get(profile["name"])
        if not blocker:
            continue
        error = blocker.get("lastError")
        profile["delivery"] = {
            "eventId": blocker["eventId"],
            "operation": blocker["type"],
            "generation": blocker["generation"],
            "status": blocker["status"],
            "attempts": blocker.get("attempts", 0),
            "lastError": (
                " ".join(str(error).split())[:1000]
                if error
                else None
            ),
        }


def semantic_delivery_blocker_page(
    profile_names: list[str],
    *,
    include_unmatched: bool,
) -> tuple[list[dict], list[dict], bool]:
    matched = (
        DERIVED.semantic_outbox_blockers(
            profile_names=profile_names,
            include_unmatched=False,
            one_per_profile=True,
            fetch_limit=len(profile_names),
        )
        if profile_names
        else []
    )
    if not include_unmatched:
        return matched, [], False
    unmatched = DERIVED.semantic_outbox_blockers(
        unmatched_only=True,
        fetch_limit=MAX_PAGE_LIMIT + 1,
    )
    return matched, unmatched[:MAX_PAGE_LIMIT], len(unmatched) > MAX_PAGE_LIMIT


def unmatched_semantic_delivery_blockers(
    profiles: list[dict],
    blockers: list[dict],
) -> list[dict]:
    current_names = {profile["name"] for profile in profiles}
    output = []
    for blocker in blockers:
        name = blocker.get("name")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name) is None
            or name in current_names
        ):
            continue
        error = blocker.get("lastError")
        output.append({
            "name": name,
            "relation": f"derived_layers.{name}",
            "assetId": blocker["assetId"],
            "eventId": blocker["eventId"],
            "operation": blocker["type"],
            "generation": blocker["generation"],
            "status": blocker["status"],
            "attempts": blocker.get("attempts", 0),
            "lastError": (
                " ".join(str(error).split())[:1000]
                if error
                else None
            ),
        })
    return output


def observed_semantic_revision(profiles: list[dict]) -> int:
    revisions = [
        int(profile["revision"])
        for profile in profiles
        if str(profile.get("revision") or "").isdigit()
    ]
    return max(revisions, default=0)


def current_semantic_revision(actor: str) -> int:
    if not SEMANTIC:
        raise SemanticClientError(
            "Semantic service is not configured.",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            payload={"code": "semantic.unavailable"},
        )
    status = SEMANTIC.request(
        "/v1/status",
        actor=actor,
        scopes=["semantic:inspect"],
    )
    revision = status.get("catalogRevision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise SemanticClientError(
            "Semantic service returned an invalid catalog revision.",
            status=HTTPStatus.BAD_GATEWAY,
            payload={"code": "semantic.invalid_response"},
        )
    return revision


def semantic_event_payload_hash(payload: dict) -> str:
    canonical = {
        key: value
        for key, value in payload.items()
        if key != "payloadHash"
    }
    return hashlib.sha256(json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()).hexdigest()


def validate_semantic_outbox_event(event: dict) -> dict:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise SemanticClientError(
            "Stored semantic event payload is invalid.",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            payload={"code": "semantic.outbox_corrupt"},
        )
    exact_fields = {
        "eventId": event.get("eventId"),
        "assetId": event.get("assetId"),
        "type": event.get("type"),
        "generation": event.get("generation"),
    }
    if (
        any(payload.get(key) != value for key, value in exact_fields.items())
        or not isinstance(event.get("eventId"), str)
        or not event["eventId"]
        or not isinstance(event.get("assetId"), str)
        or not event["assetId"]
        or event.get("type") not in {
            "register", "replace", "refresh", "archive"
        }
        or isinstance(event.get("generation"), bool)
        or not isinstance(event.get("generation"), int)
        or event["generation"] < 1
        or isinstance(payload.get("generation"), bool)
        or not isinstance(payload.get("generation"), int)
        or payload["generation"] < 1
    ):
        raise SemanticClientError(
            "Stored semantic event does not match its outbox envelope.",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            payload={"code": "semantic.outbox_corrupt"},
        )
    supplied_hash = payload.get("payloadHash")
    if (
        not isinstance(supplied_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", supplied_hash)
        or supplied_hash != semantic_event_payload_hash(payload)
    ):
        raise SemanticClientError(
            "Stored semantic event payload hash is invalid.",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            payload={"code": "semantic.outbox_corrupt"},
        )
    return dict(payload)


def validate_semantic_event_ack(
    event: dict,
    payload: dict,
    response: dict,
) -> int:
    revision = response.get("catalogRevision")
    acknowledged = response.get("event")
    asset = response.get("asset")
    expected_status = (
        "archived" if event["type"] == "archive" else "ready"
    )
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or not isinstance(acknowledged, dict)
        or acknowledged.get("eventId") != event["eventId"]
        or acknowledged.get("payloadHash") != payload["payloadHash"]
        or not isinstance(acknowledged.get("idempotent"), bool)
        or not isinstance(asset, dict)
        or asset.get("id") != event["assetId"]
        or isinstance(asset.get("generation"), bool)
        or not isinstance(asset.get("generation"), int)
        or asset.get("generation") != event["generation"]
        or asset.get("status") != expected_status
        or isinstance(asset.get("catalogRevision"), bool)
        or not isinstance(asset.get("catalogRevision"), int)
        or asset.get("catalogRevision") != revision
    ):
        raise SemanticClientError(
            "Semantic service returned a mismatched event acknowledgement.",
            status=HTTPStatus.BAD_GATEWAY,
            payload={"code": "semantic.invalid_event_ack"},
        )
    return revision


def drain_semantic_outbox(limit: int = 50) -> dict:
    result = {"delivered": 0, "retried": 0, "repairRequired": 0}
    if not DERIVED or not SEMANTIC:
        return result
    if not SEMANTIC_OUTBOX_LOCK.acquire(blocking=False):
        return result
    try:
        for _ in range(limit):
            claimed = DERIVED.claim_semantic_events(1)
            if not claimed:
                break
            event = claimed[0]
            try:
                payload = validate_semantic_outbox_event(event)
                response = SEMANTIC.request(
                    "/v1/events",
                    method="POST",
                    payload=payload,
                    actor=str(payload.get("actor") or "system"),
                    scopes=["semantic:admin"],
                )
                revision = validate_semantic_event_ack(
                    event,
                    payload,
                    response,
                )
                if DERIVED.mark_semantic_delivered(
                    event["eventId"],
                    event["claimId"],
                    revision,
                ):
                    result["delivered"] += 1
            except Exception as exc:
                attempts = int(event.get("attempts") or 0) + 1
                permanent = (
                    isinstance(exc, SemanticClientError)
                    and exc.status is not None
                    and 400 <= exc.status < 500
                )
                if permanent or attempts >= SEMANTIC_MAX_ATTEMPTS:
                    if DERIVED.mark_semantic_repair(
                        event["eventId"],
                        event["claimId"],
                        str(exc),
                    ):
                        result["repairRequired"] += 1
                else:
                    delay = min(300, 5 * (2 ** min(attempts - 1, 6)))
                    if DERIVED.mark_semantic_retry(
                        event["eventId"],
                        event["claimId"],
                        str(exc),
                        datetime.now(timezone.utc) + timedelta(seconds=delay),
                    ):
                        result["retried"] += 1
        return result
    finally:
        SEMANTIC_OUTBOX_LOCK.release()


def schedule_semantic_outbox() -> None:
    SEMANTIC_OUTBOX_WAKE.set()


def run_semantic_outbox() -> None:
    while True:
        try:
            drain_semantic_outbox()
        except Exception:
            # Event-specific errors are persisted by the drain. A connection
            # failure before events can be read is retried on the next wake.
            pass
        SEMANTIC_OUTBOX_WAKE.wait(10)
        SEMANTIC_OUTBOX_WAKE.clear()


def archive_derived_semantics_before_reset(
    reset_owner: str,
    timeout_seconds: int = 120,
) -> dict:
    if not DERIVED or not SEMANTIC:
        raise RuntimeError(
            "Derived-layer and semantic services must be configured before reset."
        )
    DERIVED.begin_semantic_reset("system:reset-data", reset_owner)
    deadline = time.monotonic() + timeout_seconds
    while True:
        drain_semantic_outbox()
        profiles = derived_semantic_profiles()
        blockers = DERIVED.semantic_outbox_blockers()
        repairs = [
            str(blocker.get("name") or blocker["assetId"])
            for blocker in blockers
            if blocker["status"] == "repair_required"
        ]
        if repairs:
            raise RuntimeError(
                "Semantic reset preflight found repair_required events for: "
                + ", ".join(repairs)
            )
        nonready = [
            profile for profile in profiles
            if profile["status"] != "ready"
        ]
        if not blockers and not nonready:
            break
        if time.monotonic() >= deadline:
            pending_items = [
                f'{profile["name"]} ({profile["status"]})'
                for profile in nonready
            ] + [
                f'{blocker.get("name") or blocker["assetId"]} '
                f'({blocker["type"]}:{blocker["status"]})'
                for blocker in blockers
            ]
            raise TimeoutError(
                "Timed out during semantic reset preflight: "
                + ", ".join(dict.fromkeys(pending_items))
            )
        time.sleep(1)
    DERIVED.queue_semantic_archives("system:reset-data")
    while True:
        drain_semantic_outbox()
        profiles = derived_semantic_profiles()
        blockers = DERIVED.semantic_outbox_blockers()
        incomplete = [
            profile for profile in profiles
            if profile["status"] != "archived"
        ]
        if not incomplete and not blockers:
            return {
                "archived": len(profiles),
                "catalogRevision": observed_semantic_revision(profiles),
                "profiles": profiles,
            }
        repairs = [
            str(blocker.get("name") or blocker["assetId"])
            for blocker in blockers
            if blocker["status"] == "repair_required"
        ]
        if repairs:
            raise RuntimeError(
                "Semantic archive has repair_required events for: "
                + ", ".join(repairs)
            )
        if time.monotonic() >= deadline:
            pending_items = [
                f'{profile["name"]} ({profile["status"]})'
                for profile in incomplete
            ] + [
                f'{blocker.get("name") or blocker["assetId"]} '
                f'({blocker["type"]}:{blocker["status"]})'
                for blocker in blockers
            ]
            raise TimeoutError(
                "Timed out before semantic archive completed: "
                + ", ".join(dict.fromkeys(pending_items))
            )
        time.sleep(1)


def recover_interrupted_reset_semantics(
    *,
    reset_owner: str | None = None,
    force: bool = False,
    wait_for_ready: bool = False,
    timeout_seconds: int = 120,
) -> dict:
    if not DERIVED:
        return {"recovered": 0, "profiles": []}
    if reset_owner is None and not force:
        raise RuntimeError(
            "Reset recovery requires the owning operation UUID or explicit "
            "force confirmation."
        )
    try:
        recovered_result = DERIVED.recover_reset_semantic_profiles(
            "system:reset-recovery",
            reset_owner,
        )
    except DerivedLayerResetOwnershipError:
        return {
            "recovered": 0,
            "profiles": [],
            "gateOwned": False,
            "reason": "foreign_gate",
        }
    gate_owned = recovered_result is not None
    gate_owner = (
        recovered_result["resetOwner"]
        if recovered_result is not None
        else None
    )
    recovered = (
        recovered_result["profiles"]
        if recovered_result is not None
        else []
    )
    if not wait_for_ready:
        if recovered:
            schedule_semantic_outbox()
        return {
            "recovered": len(recovered),
            "profiles": recovered,
            "gateOwned": gate_owned,
        }
    if not SEMANTIC:
        raise RuntimeError(
            "Semantic service must be configured to complete reset recovery."
        )
    recovered_names = {
        item["name"] for item in recovered
    } or set(DERIVED.reset_recovery_names())
    if not recovered_names:
        if gate_owner is not None:
            DERIVED.complete_reset_semantic_recovery(gate_owner)
        return {
            "recovered": 0,
            "profiles": [],
            "gateOwned": gate_owned,
        }
    deadline = time.monotonic() + timeout_seconds
    while True:
        drain_semantic_outbox()
        profiles = [
            item
            for item in derived_semantic_profiles()
            if item["name"] in recovered_names
        ]
        blockers = [
            item
            for item in DERIVED.semantic_outbox_blockers()
            if item.get("name") in recovered_names
        ]
        repairs = [
            str(item.get("name") or item["assetId"])
            for item in blockers
            if item["status"] == "repair_required"
        ]
        if repairs:
            raise RuntimeError(
                "Semantic reset recovery has repair_required events for: "
                + ", ".join(repairs)
            )
        if not blockers and len(profiles) == len(recovered_names) and all(
            item["status"] == "ready" for item in profiles
        ):
            if gate_owner is not None:
                DERIVED.complete_reset_semantic_recovery(gate_owner)
            return {
                "recovered": len(profiles),
                "profiles": profiles,
                "gateOwned": gate_owned,
            }
        if time.monotonic() >= deadline:
            states = ", ".join(
                f'{item["name"]} ({item["status"]})' for item in profiles
            )
            raise TimeoutError(
                "Timed out before semantic reset recovery completed: "
                + (states or "profiles unavailable")
            )
        time.sleep(1)


def normalized_host(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    try:
        hostname = urlparse(f"//{candidate}").hostname
    except ValueError:
        return None
    return hostname.lower().rstrip(".") if hostname else None


INTERNAL_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "config-ui"}
ALLOWED_HOSTS = INTERNAL_ALLOWED_HOSTS | {
    host
    for value in os.environ.get("CONFIG_ALLOWED_HOSTS", "").split(",")
    if (host := normalized_host(value))
}
SECURE_COOKIES = os.environ.get("CONFIG_SECURE_COOKIES", "false").lower() == "true"
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_LOCK = threading.Lock()


def revision(raw: bytes, modified_ns: int) -> str:
    # Include the file generation so an identical intervening save is still
    # detected as a stale browser revision.
    generation = str(modified_ns).encode()
    return hashlib.sha256(raw + b":" + generation).hexdigest()


def read_workspace() -> tuple[bytes, dict, str]:
    with WORKSPACE.open("rb") as stream:
        raw = stream.read()
        modified_ns = os.fstat(stream.fileno()).st_mtime_ns
    return raw, strict_json_loads(raw), revision(raw, modified_ns)


def derived_workspace_references(name: str) -> list[str]:
    _, workspace, _ = read_workspace()
    relation = f"derived_layers.{name}"
    references = []
    for locale_key, locale in effective_locales(workspace).items():
        locale_path = "locale" if locale_key == "locale" else f"locales.{locale_key}"
        if not isinstance(locale, dict):
            continue
        for layer_key, layer in (locale.get("layers") or {}).items():
            if isinstance(layer, dict) and layer.get("table") == relation:
                references.append(f"{locale_path}.layers.{layer_key}")
    return references


def _normalize_relation(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    relation = value.strip()
    if not relation or relation.count(".") > 1:
        return None
    if "." in relation:
        schema, table = relation.split(".", 1)
    else:
        schema, table = "public", relation
    schema = schema.strip()
    table = table.strip()
    if not schema or not table:
        return None
    return schema, table


def _database_layer_relations(layer: dict) -> list[tuple[str, str]]:
    if (
        not isinstance(layer, dict)
        or layer.get("format") not in DATABASE_LAYER_FORMATS
    ):
        return []
    table_map = layer.get("tables")
    if isinstance(table_map, dict) and table_map:
        relations = []
        for relation in table_map.values():
            normalized = _normalize_relation(relation)
            if normalized:
                relations.append(normalized)
        return relations
    normalized = _normalize_relation(layer.get("table"))
    return [normalized] if normalized else []


def platform_dependencies(workspace: dict) -> list[dict]:
    locales = effective_locales(workspace)
    dependencies: dict[str, dict] = {}

    def add_reference(
        *,
        alias: str,
        schema: str,
        relation: str,
        key: str,
        value: str,
    ) -> None:
        compound = f"{alias}:{schema}.{relation}"
        record = dependencies.get(compound)
        if record is None:
            record = {
                "alias": alias,
                "relation": f"{schema}.{relation}",
                "workspace": [],
                "derived": [],
            }
            dependencies[compound] = record
        record[key].append(value)

    for locale_key, locale in locales.items():
        if not isinstance(locale, dict):
            continue
        for layer_key, layer in (locale.get("layers") or {}).items():
            if not isinstance(layer_key, str) or not isinstance(layer, dict):
                continue
            alias = layer.get("dbs") or workspace.get("dbs")
            if not isinstance(alias, str) or alias not in DB_CONNECTIONS:
                continue
            for schema, relation in _database_layer_relations(layer):
                add_reference(
                    alias=alias,
                    schema=schema,
                    relation=relation,
                    key="workspace",
                    value=f"{locale_key}:{layer_key}",
                )

    if DERIVED is not None:
        try:
            for definition in DERIVED.list():
                name = definition.get("name")
                if not name:
                    continue
                for source_relation in definition.get("sources", ()):
                    normalized = _normalize_relation(source_relation)
                    if not normalized:
                        continue
                    schema, table = normalized
                    add_reference(
                        alias="derived",
                        schema=schema,
                        relation=table,
                        key="derived",
                        value=name,
                    )
        except (psycopg.Error, DerivedLayerError):
            # If the derived catalog is unavailable, only workspace references
            # should be reported.
            pass

    return sorted(
        (
            {
                "alias": record["alias"],
                "relation": record["relation"],
                "workspaceLayers": sorted(set(record["workspace"])),
                "derivedLayers": sorted(set(record["derived"])),
            }
            for record in dependencies.values()
        ),
        key=lambda item: (item["alias"], item["relation"]),
    )


def sync_layer_dependency_guard(workspace: dict) -> None:
    """Sync workspace+derived-layer references into database guard rows.

    The best-effort write keeps per-database protections close to the live
    workspace configuration. Synchronization failures do not block proposal
    apply/save operations because platform-level safety is advisory and the
    manual-drop path must fail open only if explicitly configured for that DB.
    """
    grouped: dict[str, set[str]] = {}
    for item in platform_dependencies(workspace):
        grouped.setdefault(item["alias"], set()).add(item["relation"])

    for alias, relations in grouped.items():
        database_url = DB_CONNECTIONS.get(alias)
        if not database_url:
            continue
        try:
            with psycopg.connect(database_url, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT public.mapp_sync_platform_layer_dependencies(%s, %s)",
                        [alias, json.dumps(sorted(relations))],
                    )
        except Exception:
            continue


def derived_workspace_impact(name: str, affected_columns: list[str]) -> dict:
    _, workspace, _ = read_workspace()
    relation, affected = f"derived_layers.{name}", set(affected_columns)
    references, field_references, consumer_labels = [], [], []
    for locale_key, locale in effective_locales(workspace).items():
        locale_path = "locale" if locale_key == "locale" else f"locales.{locale_key}"
        if not isinstance(locale, dict):
            continue
        for layer_key, layer in (locale.get("layers") or {}).items():
            if not isinstance(layer, dict) or layer.get("table") != relation:
                continue
            layer_path = f"{locale_path}.layers.{layer_key}"
            references.append(layer_path)
            locale_label = (
                "default map"
                if locale_key == "locale"
                else str(locale.get("name") or locale_key)
            )
            layer_label = str(layer.get("name") or layer_key)
            consumer_labels.append(f"{layer_label} ({locale_label})")
            hover = (layer.get("style") or {}).get("hover")
            style = layer.get("style") or {}
            theme_key = style.get("theme")
            theme_path = "style.theme"
            theme = theme_key
            if isinstance(theme_key, str):
                theme = (style.get("themes") or {}).get(theme_key)
                theme_path = f"style.themes.{theme_key}"
            candidates = [
                ("qID", "feature ID", layer.get("qID")),
                ("geom", "map geometry", layer.get("geom")),
            ]
            if isinstance(hover, dict):
                candidates.append((
                    "style.hover.field", "hover text", hover.get("field")
                ))
            if isinstance(theme, dict):
                candidates.append((
                    f"{theme_path}.field", "symbology field",
                    theme.get("field"),
                ))
                for index, field in enumerate(theme.get("fields") or []):
                    candidates.append((
                        f"{theme_path}.fields.{index}",
                        "multi-field symbology", field,
                    ))
                for index, category in enumerate(theme.get("categories") or []):
                    if isinstance(category, dict):
                        candidates.append((
                            f"{theme_path}.categories.{index}.field",
                            "symbology category field", category.get("field"),
                        ))
            for index, entry in enumerate(layer.get("infoj") or []):
                if isinstance(entry, dict):
                    label = str(
                        entry.get("title") or entry.get("label")
                        or f"information field {index + 1}"
                    )
                    candidates.append((
                        f"infoj.{index}.field", f'information value “{label}”',
                        entry.get("field"),
                    ))
            field_references.extend(
                {
                    "path": f"{layer_path}.{path}",
                    "column": value,
                    "consumer": f"{layer_label} ({locale_label})",
                    "usage": usage,
                    "label": (
                        f'{layer_label} ({locale_label}) uses “{value}” '
                        f"for its {usage}"
                    ),
                }
                for path, usage, value in candidates if value in affected
            )
    return {
        "workspaceReferences": references,
        "consumerLabels": consumer_labels,
        "fieldReferences": field_references,
        "requiresSecondOrderChanges": bool(field_references),
    }


def run_derived_background(
    operation_id: str,
    action: str,
    payload: dict,
    actor: str,
    remote: str,
    name: str | None = None,
    cancellation: DerivedLayerCancellation | None = None,
) -> None:
    """Complete a derived-layer database operation after HTTP has returned."""
    failure_phase = "database-transaction"
    try:
        if not DERIVED:
            raise DerivedLayerError(
                "Derived-layer database management is not configured."
            )
        if action == "create":
            result = DERIVED.create(
                payload,
                actor,
                **({"cancellation": cancellation} if cancellation else {}),
            )
            failure_phase = "result-reporting"
        elif action == "replace" and name:
            result = DERIVED.replace(
                name,
                payload,
                actor,
                **({"cancellation": cancellation} if cancellation else {}),
            )
            failure_phase = "result-reporting"
            changes = result.get("columnChanges", {})
            result.update(derived_workspace_impact(
                name,
                changes.get("removed", []) + changes.get("changed", []),
            ))
        elif action == "refresh" and name:
            result = DERIVED.refresh(
                name,
                actor,
                **({"cancellation": cancellation} if cancellation else {}),
            )
            failure_phase = "result-reporting"
        else:
            raise DerivedLayerError("Unsupported background operation.")
        schedule_semantic_outbox()
        CONTROL.audit(
            f"derived_layer.{action}d" if action != "refresh" else "derived_layer.refreshed",
            actor=actor,
            remote=remote,
            details={
                "name": result["name"],
                "kind": result["kind"],
                "sources": result["sources"],
                "spatialScope": result.get("spatialScope"),
                "operationId": operation_id,
            },
        )
        CONTROL.finish_operation(
            operation_id,
            status="succeeded",
            result={"derivedLayer": result},
        )
    except DerivedLayerCancellationRequested as exc:
        finish_derived_background_cancellation(operation_id, action, exc)
    except DerivedLayerQueryTooExpensive as exc:
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            derived_query_too_expensive_error(exc, action),
            derived_query_error_status(exc),
            failure_phase,
        )
    except DerivedLayerMaterializationTooLarge as exc:
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            derived_materialization_too_large_error(exc, action),
            HTTPStatus.CONFLICT,
            failure_phase,
        )
    except DerivedLayerDependencyError as exc:
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            derived_dependency_error(exc, action),
            HTTPStatus.CONFLICT,
            failure_phase,
        )
    except DerivedLayerSourceMismatchError as exc:
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            derived_source_mismatch_error(exc, action),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            failure_phase,
        )
    except DerivedLayerContentionError as exc:
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            derived_contention_error(exc, action),
            HTTPStatus.CONFLICT,
            failure_phase,
        )
    except DerivedLayerMaintenanceError as exc:
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            derived_maintenance_error(exc, action),
            HTTPStatus.CONFLICT,
            failure_phase,
        )
    except FileExistsError as exc:
        finish_derived_background_failure(
            operation_id,
            "create",
            exc,
            derived_already_exists_error(str(exc)),
            HTTPStatus.CONFLICT,
            failure_phase,
        )
    except FileNotFoundError as exc:
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            derived_not_found_error(str(exc) or str(name or ""), action),
            HTTPStatus.NOT_FOUND,
            failure_phase,
        )
    except DerivedLayerError as exc:
        response = derived_validation_error(exc, action)
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            response,
            derived_validation_error_status(response),
            failure_phase,
        )
    except DerivedLayerDatabaseOperationError as exc:
        if (
            cancellation is not None
            and cancellation.requested
            and exc.rolled_back
            and getattr(exc.cause, "sqlstate", None) == "57014"
        ):
            finish_derived_background_cancellation(
                operation_id, action, exc.cause,
            )
        else:
            response = derived_database_error(
                exc.cause,
                action,
                failure_phase=exc.failure_phase,
                state_unchanged=exc.state_unchanged,
                rolled_back=exc.rolled_back,
                indeterminate=exc.indeterminate,
            )
            finish_derived_background_failure(
                operation_id,
                action,
                exc,
                response,
                derived_database_error_status(response),
                failure_phase,
                type_exc=exc.cause,
            )
    except psycopg.Error as exc:
        phase, _ = derived_exception_failure_state(exc, failure_phase)
        response = derived_database_error(
            exc,
            action,
            failure_phase=phase,
            state_unchanged=False,
            indeterminate=True,
        )
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            response,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            failure_phase,
        )
    except Exception as exc:
        phase, rolled_back = derived_exception_failure_state(
            exc, failure_phase,
        )
        state_unchanged = rolled_back and phase == "database-transaction"
        response = derived_operation_failed_error(
            action,
            failure_phase=phase,
            state_unchanged=state_unchanged,
            rolled_back=rolled_back,
        )
        finish_derived_background_failure(
            operation_id,
            action,
            exc,
            response,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            failure_phase,
        )


def derived_background_capacity() -> dict:
    with DERIVED_BACKGROUND_JOB_LOCK:
        return {
            "activeJobs": DERIVED_BACKGROUND_ACTIVE_JOBS,
            "maxActiveJobs": DERIVED_MAX_BACKGROUND_JOBS,
        }


def reserve_derived_background_job() -> None:
    global DERIVED_BACKGROUND_ACTIVE_JOBS
    with DERIVED_BACKGROUND_JOB_LOCK:
        if DERIVED_BACKGROUND_ACTIVE_JOBS >= DERIVED_MAX_BACKGROUND_JOBS:
            raise DerivedLayerBackgroundCapacityError(
                DERIVED_BACKGROUND_ACTIVE_JOBS,
                DERIVED_MAX_BACKGROUND_JOBS,
            )
        DERIVED_BACKGROUND_ACTIVE_JOBS += 1


def release_derived_background_job() -> None:
    global DERIVED_BACKGROUND_ACTIVE_JOBS
    with DERIVED_BACKGROUND_JOB_LOCK:
        if DERIVED_BACKGROUND_ACTIVE_JOBS <= 0:
            raise RuntimeError("No derived-layer background job is reserved.")
        DERIVED_BACKGROUND_ACTIVE_JOBS -= 1


def run_reserved_derived_background(*args) -> None:
    try:
        run_derived_background(*args)
    finally:
        with DERIVED_BACKGROUND_JOB_LOCK:
            DERIVED_BACKGROUND_CANCELLATIONS.pop(args[0], None)
        release_derived_background_job()


INVALID_QUERY_REASON_CODES = frozenset({
    "invalid_sql", "multiple_statements", "not_select",
})
COMPUTE_QUERY_REASON_CODES = frozenset({
    "cartesian_join",
    "final_rows",
    "h3_composed_expansion",
    "h3_scope_expansion",
    "intermediate_bytes",
    "intermediate_rows",
    "join_expansion",
    "nested_loop_pair_work",
    "plan_depth",
    "plan_nodes",
    "planned_workers",
    "recursive_plan",
    "too_many_ctes",
    "too_many_grouping_sets",
    "too_many_joins",
    "too_many_set_operations",
    "total_cost",
    "unbounded_aggregate_state",
    "unbounded_geometry_expansion",
    "unbounded_row_generator",
    "unbounded_scalar_output",
    "unbounded_set_function",
})
DERIVED_FAILURE_PHASES = frozenset({
    "preflight",
    "database-transaction",
    "transaction-rollback",
    "transaction-commit",
    "result-reporting",
    "request-response",
    "service-recovery",
})


def derived_operation_safe_state(operation: str | None) -> str:
    return {
        "create": "No derived layer was created.",
        "replace": "The existing derived layer remains active and unchanged.",
        "refresh": "The existing materialized data remains unchanged.",
        "drop": "Nothing was deleted.",
    }.get(operation, "No derived-layer change was applied.")


def derived_request_operation(request_path: str, derived_action_path) -> str | None:
    if request_path == "/api/derived-layers/recipes/area-weighted-h3/plan":
        return "plan-area-weighted-h3"
    if request_path == "/api/derived-layers":
        return "create"
    if derived_action_path:
        return derived_action_path.group(2)
    return None


def derived_failure_state(
    response: dict,
    operation: str | None,
    *,
    failure_phase: str,
    rolled_back: bool = False,
    indeterminate: bool = False,
) -> dict:
    if failure_phase not in DERIVED_FAILURE_PHASES:
        raise ValueError("Invalid derived-layer failure phase.")
    indeterminate = indeterminate or response.get("indeterminate") is True
    if failure_phase == "database-transaction" and not rolled_back:
        failure_phase = "transaction-rollback"
    elif rolled_back and failure_phase != "database-transaction":
        if failure_phase == "preflight":
            failure_phase = "transaction-rollback"
        rolled_back = False
    response["failurePhase"] = failure_phase
    preflight_is_proven = failure_phase == "preflight" and not rolled_back
    rollback_is_proven = (
        failure_phase == "database-transaction" and rolled_back
    )
    unchanged_is_proven = preflight_is_proven or rollback_is_proven
    if indeterminate or not unchanged_is_proven:
        response["indeterminate"] = True
        response.pop("retryable", None)
        response.pop("stateUnchanged", None)
        response.pop("safeState", None)
        response.pop("rolledBack", None)
        return response
    response["stateUnchanged"] = True
    response["safeState"] = derived_operation_safe_state(operation)
    response.pop("indeterminate", None)
    if rolled_back:
        response["rolledBack"] = True
    else:
        response.pop("rolledBack", None)
    return response


def derived_exception_failure_state(
    exc: Exception,
    default_phase: str,
) -> tuple[str, bool]:
    phase = getattr(exc, "failure_phase", default_phase)
    if phase not in DERIVED_FAILURE_PHASES:
        phase = default_phase
    rolled_back = getattr(exc, "rolled_back", False) is True
    if phase == "database-transaction" and not rolled_back:
        phase = "transaction-rollback"
    return phase, rolled_back


def derived_exception_response(
    response: dict,
    exc: Exception,
    operation: str | None,
    default_phase: str,
) -> dict:
    phase, rolled_back = derived_exception_failure_state(exc, default_phase)
    return derived_failure_state(
        response,
        operation,
        failure_phase=phase,
        rolled_back=rolled_back,
        indeterminate=response.get("indeterminate") is True,
    )


def derived_failure_http_status(
    response: dict,
    expected_status: HTTPStatus,
) -> HTTPStatus:
    return (
        HTTPStatus.INTERNAL_SERVER_ERROR
        if response.get("indeterminate") is True
        else expected_status
    )


def derived_failure_operation_status(response: dict) -> str:
    return (
        "indeterminate"
        if response.get("indeterminate") is True
        else "failed"
    )


def finish_derived_background_failure(
    operation_id: str,
    action: str,
    exc: Exception,
    response: dict,
    expected_status: HTTPStatus,
    default_phase: str,
    *,
    type_exc: Exception | None = None,
) -> None:
    response = derived_exception_response(
        response,
        exc,
        action,
        default_phase,
    )
    CONTROL.finish_operation(
        operation_id,
        status=derived_failure_operation_status(response),
        error={
            **response,
            "status": int(derived_failure_http_status(
                response, expected_status,
            )),
            "type": type(type_exc or exc).__name__,
        },
    )


def finish_derived_background_cancellation(
    operation_id: str,
    action: str,
    exc: Exception,
) -> None:
    message = f"Derived-layer {action} was cancelled and rolled back."
    error = derived_failure_state(
        {
            "error": message,
            "message": message,
            "userMessage": message,
            "suggestedAction": (
                "Inspect the operation record before submitting another "
                "derived-layer change."
            ),
            "code": "derived_layer.cancelled",
            "operation": action,
            "cancelled": True,
        },
        action,
        failure_phase="database-transaction",
        rolled_back=True,
    )
    CONTROL.finish_operation(
        operation_id,
        status="cancelled",
        error={**error, "type": type(exc).__name__},
    )


def request_derived_background_cancellation(operation_id: str) -> bool:
    with DERIVED_BACKGROUND_JOB_LOCK:
        cancellation = DERIVED_BACKGROUND_CANCELLATIONS.get(operation_id)
    return cancellation.request() if cancellation is not None else False


def derived_blocked_error(
    *,
    code: str,
    message: str,
    suggested_action: str,
    operation: str | None,
    **fields,
) -> dict:
    return derived_failure_state({
        "error": message,
        "message": message,
        "userMessage": message,
        "suggestedAction": suggested_action,
        "code": code,
        "operation": operation,
        "blocked": True,
        **fields,
    }, operation, failure_phase="preflight")


def derived_materialization_too_large_error(
    exc: DerivedLayerMaterializationTooLarge,
    operation: str | None = None,
) -> dict:
    actual = "actualBytes" in exc.probe
    if operation == "refresh":
        suggested_action = (
            "Convert this materialized layer to an ordinary view, or reduce "
            "its output before refreshing again."
        )
    elif operation == "replace":
        suggested_action = (
            "Replace it with an ordinary view, or reduce the replacement "
            "query output."
        )
    else:
        suggested_action = (
            "Create this derived layer as an ordinary view, or reduce its "
            "output before trying again."
        )
    message = str(exc)
    response = derived_blocked_error(
        code="derived_layer.materialization_too_large",
        message=message,
        suggested_action=suggested_action,
        operation=operation,
        recommendedKind="view",
        name=exc.name,
        probe=exc.probe,
        probeStage="actual" if actual else "estimate",
    )
    return response


def derived_query_classification(
    exc: DerivedLayerQueryTooExpensive,
) -> tuple[str, str, HTTPStatus]:
    codes = {
        reason.get("code")
        for reason in exc.reasons
        if isinstance(reason, dict)
    }
    if codes & INVALID_QUERY_REASON_CODES:
        return "invalid", "derived_layer.query_invalid", HTTPStatus.BAD_REQUEST
    if codes and codes <= COMPUTE_QUERY_REASON_CODES:
        return (
            "compute",
            "derived_layer.query_too_expensive",
            HTTPStatus.CONFLICT,
        )
    return (
        "policy",
        "derived_layer.query_not_allowed",
        HTTPStatus.UNPROCESSABLE_ENTITY,
    )


def derived_query_reason_action(code: str) -> str:
    if code in INVALID_QUERY_REASON_CODES:
        return "Submit exactly one PostgreSQL SELECT statement with valid syntax."
    if code in {"unqualified_relation", "unapproved_relation_schema"}:
        return (
            "Use only declared, permitted source relations and qualify each "
            "one as schema.table."
        )
    if code in {"h3_unscoped_polygon_expansion", "h3_scope_binding"}:
        return (
            "Generate H3 cells directly from _mapp_h3_scope.geom_4326 in the "
            "query scope."
        )
    if code == "h3_missing_scope":
        return "Resolve a workspace map extent before using H3 polygon expansion."
    if code in {"h3_dynamic_resolution", "h3_dynamic_grid_distance"}:
        return "Use the literal H3 resolution or grid-distance bounds in the reason."
    if code == "h3_polygon_mode":
        return (
            "Use h3_polygon_to_cells_experimental with the literal "
            "'overlapping' containment mode."
        )
    if code in {"h3_scope_expansion", "h3_composed_expansion"}:
        return "Use a coarser H3 resolution or a smaller literal traversal distance."
    if code in {"h3_unbounded_expansion", "h3_unbounded_child_expansion"}:
        return "Replace the H3 expansion with a server-bounded supported H3 operation."
    if code in {
        "unbounded_aggregate_state", "unapproved_aggregate",
        "unapproved_window_routine",
    }:
        return "Use a fixed-state aggregate such as count, sum, avg, min, or max."
    if code in {
        "unbounded_geometry_expansion", "unbounded_row_generator",
        "unbounded_scalar_output", "unbounded_set_function",
    }:
        return "Remove the unbounded expansion or replace it with a provably bounded input."
    if code == "cartesian_join":
        return "Add an explicit bounded join predicate and split OR alternatives."
    if code in {
        "hazardous_function", "dangerous_catalog_function",
        "security_definer_routine", "configured_routine", "volatile_routine",
    } or code.startswith("unapproved_"):
        return (
            "Remove the routine, operator, type, or cast and use only approved "
            "pg_catalog, PostGIS, or H3 functionality."
        )
    if code in {
        "modifying_cte", "recursive_cte", "row_locking", "select_into",
    }:
        return "Rewrite the query as a read-only, non-recursive SELECT."
    if code in {
        "h3_scope_shadowed", "reserved_alias", "reserved_cte",
        "reserved_relation",
    }:
        return "Rename the reserved query alias, CTE, or relation."
    if code == "natural_join":
        return "Replace NATURAL JOIN with an explicit bounded join condition."
    if code == "nested_loop_pair_work":
        return (
            "Rewrite the join so each outer row reaches a selective "
            "parameterized or indexed input; keep whole-input aggregates and "
            "windows outside that row-matching path."
        )
    return (
        "Reduce output rows, joins, generated rows, and intermediate work; "
        "add selective predicates or source indexes where appropriate."
    )


def derived_query_too_expensive_error(
    exc: DerivedLayerQueryTooExpensive,
    operation: str | None = None,
) -> dict:
    category, code, _ = derived_query_classification(exc)
    reasons = []
    for reason in exc.reasons:
        item = dict(reason)
        item["suggestedAction"] = derived_query_reason_action(
            str(item.get("code", ""))
        )
        reasons.append(item)
    details = "; ".join(
        str(reason.get("message", "")) for reason in reasons
    )
    if category == "invalid":
        lead = f'The derived-layer query for “{exc.name}” is invalid.'
        suggested_action = (
            "Correct the SQL described below, then submit exactly one SELECT."
        )
    elif category == "policy":
        lead = (
            f'The derived-layer query for “{exc.name}” uses SQL that is not '
            "allowed."
        )
        suggested_action = (
            reasons[0]["suggestedAction"]
            if reasons
            else "Remove the prohibited SQL and try again."
        )
    else:
        lead = (
            f'The derived-layer query for “{exc.name}” exceeds the server '
            "compute budget."
        )
        suggested_action = (
            "Address each compute-limit reason below. Changing to an ordinary "
            "view does not bypass this guard."
        )
    message = lead + (" " + details if details else "")
    planning_probe = getattr(exc, "query_planning_probe", None)
    return derived_blocked_error(
        code=code,
        message=message,
        suggested_action=suggested_action,
        operation=operation,
        category=category,
        name=exc.name,
        probe=exc.probe,
        reasons=reasons,
        **(
            {"queryPlanningProbe": planning_probe}
            if isinstance(planning_probe, dict)
            else {}
        ),
    )


def derived_query_error_status(
    exc: DerivedLayerQueryTooExpensive,
) -> HTTPStatus:
    return derived_query_classification(exc)[2]


def derived_source_mismatch_error(
    exc: DerivedLayerSourceMismatchError,
    operation: str | None,
) -> dict:
    message = (
        "The declared source list does not match the relations PostgreSQL "
        "resolved from the query."
    )
    return derived_blocked_error(
        code="derived_layer.source_mismatch",
        message=message,
        suggested_action=(
            "Add every relation used by the query to sources and remove any "
            "declared source the query does not use."
        ),
        operation=operation,
        declaredSources=exc.declared_sources,
        resolvedSources=exc.resolved_sources,
        missingSources=exc.missing_sources,
        extraSources=exc.extra_sources,
    )


def derived_in_use_reasons(
    *,
    has_workspace_references: bool,
    has_postgresql_dependents: bool,
) -> list[dict[str, str]]:
    reasons = []
    if has_workspace_references:
        reasons.append({
            "code": "workspace_references",
            "message": (
                "One or more workspace map layers still reference this "
                "derived layer."
            ),
            "suggestedAction": (
                "Remove or replace the listed workspace references first."
            ),
        })
    if has_postgresql_dependents:
        reasons.append({
            "code": "postgresql_dependents",
            "message": (
                "One or more PostgreSQL views or objects depend on this "
                "derived layer."
            ),
            "suggestedAction": (
                "Update or remove the listed PostgreSQL dependents first."
            ),
        })
    return reasons


def derived_dependency_error(
    exc: DerivedLayerDependencyError,
    operation: str,
) -> dict:
    action = "edited" if operation == "replace" else "deleted"
    columns = sorted(
        set(exc.removed_columns) & set(exc.dependent_columns)
    )
    column_message = (
        " The database uses these affected fields: "
        + ", ".join(f"“{column}”" for column in columns) + "."
        if columns else ""
    )
    message = (
        f'The derived layer “{exc.name}” cannot be {action} because other '
        f"database views or objects use it.{column_message}"
    )
    return derived_blocked_error(
        code="derived_layer.in_use",
        message=message,
        suggested_action=(
            "Update or remove the dependent database views first, then try "
            "again."
        ),
        operation=operation,
        reasons=derived_in_use_reasons(
            has_workspace_references=False,
            has_postgresql_dependents=True,
        ),
        name=exc.name,
        dependents=exc.dependents,
        removedColumns=exc.removed_columns,
        dependentColumns=exc.dependent_columns,
        workspaceReferences=derived_workspace_references(exc.name),
        requiresSecondOrderChanges=bool(
            exc.removed_columns or exc.dependent_columns
        ),
        dropped=False,
    )


def derived_validation_error(exc: Exception, operation: str | None) -> dict:
    message = str(exc)
    normalized_message = message.lower()
    invalid_query = (
        message == "A SELECT query is required."
        or message == "Derived-layer SQL must be one SELECT query."
        or message.startswith("Derived-layer SQL is limited to ")
        or message == "SQL terminators and comments are not allowed."
    )
    policy_query = (
        message.startswith("SQL keyword ")
        or message == (
            "A managed derived layer cannot depend on another derived layer."
        )
    )
    if invalid_query or policy_query:
        category = "invalid" if invalid_query else "policy"
        code = (
            "derived_layer.query_invalid"
            if invalid_query
            else "derived_layer.query_not_allowed"
        )
        suggested_action = (
            "Submit exactly one PostgreSQL SELECT statement with valid syntax."
            if invalid_query
            else "Remove the prohibited SQL or managed-source dependency."
        )
        return derived_blocked_error(
            code=code,
            message=message,
            suggested_action=suggested_action,
            operation=operation,
            category=category,
            reasons=[{
                "code": "invalid_sql" if invalid_query else "prohibited_sql",
                "message": message,
                "suggestedAction": suggested_action,
            }],
        )
    if (
        "semantic profile" in normalized_message
        or "semantic asset" in normalized_message
        or "semantic catalog" in normalized_message
    ):
        code = "derived_layer.source_profile_required"
        suggested_action = (
            "Synchronize every listed source with `semantic source sync`, "
            "then retry the derived-layer request."
        )
    elif "spatialScope" in message or "map extent" in message.lower():
        code = "derived_layer.spatial_scope_invalid"
        suggested_action = (
            "Select a valid workspace locale and let the server resolve its "
            "map extent."
        )
    else:
        code = "derived_layer.invalid_request"
        suggested_action = "Correct the derived-layer request described above and retry."
    return derived_blocked_error(
        code=code,
        message=message,
        suggested_action=suggested_action,
        operation=operation,
    )


def derived_validation_error_status(response: dict) -> HTTPStatus:
    if response.get("code") == "derived_layer.query_not_allowed":
        return HTTPStatus.UNPROCESSABLE_ENTITY
    return HTTPStatus.BAD_REQUEST


def derived_maintenance_error(
    exc: DerivedLayerMaintenanceError,
    operation: str | None,
) -> dict:
    message = str(exc)
    return derived_blocked_error(
        code="derived_layer.maintenance",
        message=message,
        suggested_action=(
            "Wait for reset-data maintenance to finish, then retry the same "
            "request."
        ),
        operation=operation,
        retryable=True,
    )


def derived_contention_error(
    exc: DerivedLayerContentionError,
    operation: str | None,
) -> dict:
    return derived_blocked_error(
        code="derived_layer.database_contention",
        message=str(exc),
        suggested_action=(
            "Wait for the active derived-layer operation to finish or cancel "
            "it, then retry the same reviewed request. If no operation is "
            "active, ask a database operator to inspect derived-layer lock "
            "holders before retrying."
        ),
        operation=operation,
        category="contention",
        contentionScope=exc.contention_scope,
        retryable=True,
    )


def derived_not_found_error(name: str, operation: str | None) -> dict:
    message = (
        f'The derived layer “{name}” does not exist.'
    )
    return derived_blocked_error(
        code="derived_layer.not_found",
        message=message,
        suggested_action=(
            "List derived layers and retry with an existing layer name."
        ),
        operation=operation,
        name=name,
    )


def derived_already_exists_error(name: str) -> dict:
    message = (
        f'A derived layer named “{name}” already exists.'
    )
    return derived_blocked_error(
        code="derived_layer.already_exists",
        message=message,
        suggested_action=(
            "Choose a different name or replace the existing derived layer."
        ),
        operation="create",
        name=name,
    )


def sanitized_postgres_detail(exc: psycopg.Error) -> dict:
    technical_detail = {}
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
        technical_detail["sqlstate"] = sqlstate
    diagnostics = getattr(exc, "diag", None)
    primary = getattr(diagnostics, "message_primary", None)
    if isinstance(primary, str) and primary.strip():
        technical_detail["message"] = primary.strip()[:500]
    return technical_detail


def derived_database_error(
    exc: psycopg.Error,
    operation: str | None,
    *,
    failure_phase: str,
    state_unchanged: bool,
    rolled_back: bool = False,
    indeterminate: bool = False,
) -> dict:
    uncertain = indeterminate or not state_unchanged
    lock_contention = (
        getattr(exc, "sqlstate", None) == "55P03"
        and not uncertain
        and (
            failure_phase == "preflight"
            or (failure_phase == "database-transaction" and rolled_back)
        )
    )
    message = (
        "The database could not acquire a required lock before the "
        "derived-layer lock timeout."
        if lock_contention
        else "The database could not apply this derived-layer change."
    )
    if lock_contention:
        suggested_action = (
            "Wait for the blocking database transaction or source-table "
            "maintenance to finish, then retry the same reviewed request. "
            "If this repeats, ask a database operator to inspect PostgreSQL "
            "lock holders."
        )
    elif uncertain:
        suggested_action = (
            "Inspect the operation, managed derived layer, and catalog before "
            "retrying; the requested change may have committed."
        )
    else:
        suggested_action = (
            "Correct the query, declared source tables, or selected ID and "
            "geometry fields, then retry."
        )
    response = {
        "error": message,
        "message": message,
        "userMessage": message,
        "suggestedAction": suggested_action,
        "code": (
            "derived_layer.database_contention"
            if lock_contention
            else "derived_layer.database_error"
        ),
        "operation": operation,
        "blocked": True,
    }
    if lock_contention:
        response.update({
            "category": "contention",
            "contentionScope": "postgresql-lock",
            "retryable": True,
        })
    technical_detail = sanitized_postgres_detail(exc)
    if technical_detail:
        response["technicalDetail"] = technical_detail
    return derived_failure_state(
        response,
        operation,
        failure_phase=failure_phase,
        rolled_back=rolled_back,
        indeterminate=indeterminate or not state_unchanged,
    )


def derived_database_error_status(response: dict) -> HTTPStatus:
    return (
        HTTPStatus.CONFLICT
        if response.get("code") == "derived_layer.database_contention"
        else HTTPStatus.UNPROCESSABLE_ENTITY
    )


def derived_read_error(
    *,
    code: str,
    message: str,
    suggested_action: str,
    exc: Exception,
) -> dict:
    response = {
        "error": message,
        "message": message,
        "userMessage": message,
        "suggestedAction": suggested_action,
        "code": code,
    }
    if isinstance(exc, psycopg.Error):
        technical_detail = sanitized_postgres_detail(exc)
        if technical_detail:
            response["technicalDetail"] = technical_detail
    return response


def derived_operation_failed_error(
    operation: str | None,
    *,
    failure_phase: str,
    state_unchanged: bool = False,
    rolled_back: bool = False,
) -> dict:
    message = (
        "The derived-layer operation ended without a confirmed result."
    )
    return derived_failure_state({
        "error": message,
        "message": message,
        "userMessage": message,
        "suggestedAction": (
            (
                "Review the request and safe diagnostics, then retry; no "
                "derived-layer change was applied."
            )
            if state_unchanged
            else (
                "Inspect the operation, managed derived layer, and catalog "
                "before retrying; the requested change may have committed."
            )
        ),
        "code": "derived_layer.operation_failed",
        "operation": operation,
        "blocked": True,
    }, operation, failure_phase=failure_phase, rolled_back=rolled_back,
       indeterminate=not state_unchanged)


def require_semantic_derived_sources(
    payload: dict,
    catalog: dict,
) -> None:
    """Require every declared relation source to have a ready semantic profile."""
    definition = validate_definition(payload)
    assets = catalog.get("assets")
    if not isinstance(assets, list):
        raise DerivedLayerError("Semantic catalog returned invalid source profiles.")
    profiled_sources = set()
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("status") != "ready":
            continue
        generated = asset.get("generated")
        if not isinstance(generated, dict):
            continue
        binding = generated.get("binding")
        if not isinstance(binding, dict) or binding.get("adapter") != "postgresql":
            continue
        schema = binding.get("schema")
        relation = binding.get("relation")
        if isinstance(schema, str) and isinstance(relation, str):
            profiled_sources.add(f"{schema}.{relation}")
    missing = sorted(set(definition["sources"]) - profiled_sources)
    if missing:
        raise DerivedLayerError(
            "Derived-layer sources need ready semantic profiles: "
            + ", ".join(missing)
            + ". Synchronize each source with `semantic source sync` before "
            "creating or replacing a derived layer."
        )


def recipe_source_asset_id(payload: dict) -> str:
    source = payload.get("source")
    asset_id = source.get("assetId") if isinstance(source, dict) else None
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
    return asset_id


def resolve_derived_spatial_scope(payload: dict) -> dict:
    resolved = {**payload}
    requested = resolved.get(
        "spatialScope",
        {"type": "workspace-map-extent"},
    )
    if not isinstance(requested, dict):
        raise DerivedLayerError(
            "spatialScope must be an object with type workspace-map-extent."
        )
    unknown = sorted(set(requested) - {"type", "locale"})
    if unknown:
        raise DerivedLayerError(
            "Unknown spatialScope properties: " + ", ".join(unknown)
        )
    if requested.get("type") != "workspace-map-extent":
        raise DerivedLayerError(
            "spatialScope.type must be workspace-map-extent."
        )
    locale_key = requested.get("locale")
    if locale_key is not None and (
        not isinstance(locale_key, str) or not locale_key
    ):
        raise DerivedLayerError(
            "spatialScope.locale must be a non-empty locale key."
        )
    _, workspace, _ = read_workspace()
    try:
        resolved["spatialScope"] = workspace_map_extent(
            workspace,
            locale_key,
        )
    except ValueError as exc:
        raise DerivedLayerError(str(exc)) from exc
    return resolved


def archive_excluded_semantic_sources(actor: str) -> list[dict]:
    if not SEMANTIC:
        raise SemanticClientError(
            "Semantic service is not configured.",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            payload={"code": "semantic.not_configured"},
        )
    catalog = SEMANTIC.request(
        "/v1/catalog",
        actor=actor,
        scopes=["semantic:inspect"],
    )
    assets = catalog.get("assets")
    if not isinstance(assets, list):
        raise SemanticClientError(
            "Semantic service returned an invalid catalog.",
            status=HTTPStatus.BAD_GATEWAY,
            payload={"code": "semantic.invalid_response"},
        )
    archived = []
    for asset in assets:
        generated = asset.get("generated") if isinstance(asset, dict) else None
        binding = generated.get("binding") if isinstance(generated, dict) else None
        if (
            not isinstance(asset, dict)
            or asset.get("status") != "ready"
            or not isinstance(binding, dict)
            or binding.get("adapter") != "postgresql"
            or not all(isinstance(binding.get(key), str) for key in ("alias", "schema", "relation"))
            or not any(pattern.permits(
                binding["alias"], binding["schema"], binding["relation"],
            ) for pattern in SEMANTIC_SOURCE_EXCLUSIONS)
        ):
            continue
        generation = asset.get("generation")
        asset_id = asset.get("id")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1 or not isinstance(asset_id, str):
            raise SemanticClientError(
                "Semantic service returned an invalid source asset.",
                status=HTTPStatus.BAD_GATEWAY,
                payload={"code": "semantic.invalid_response"},
            )
        event = {
            "eventId": str(uuid.uuid4()),
            "assetId": asset_id,
            "type": "archive",
            "generation": generation + 1,
            "actor": actor,
        }
        event["payloadHash"] = semantic_event_payload_hash(event)
        response = SEMANTIC.request(
            "/v1/events",
            method="POST",
            payload=event,
            actor=actor,
            scopes=["semantic:admin"],
        )
        validate_semantic_event_ack(event, event, response)
        archived.append({
            "id": asset_id,
            "binding": binding,
        })
    return archived


def start_derived_background(
    action: str,
    payload: dict,
    actor: str,
    remote: str,
    name: str | None = None,
) -> dict:
    reserve_derived_background_job()
    operation = None
    try:
        operation = CONTROL.create_operation(
            f"derived-layer.{action}",
            actor,
            {
                "name": name or payload.get("name"),
                "action": action,
                "spatialScope": payload.get("spatialScope"),
            },
        )
        cancellation = DerivedLayerCancellation()
        with DERIVED_BACKGROUND_JOB_LOCK:
            DERIVED_BACKGROUND_CANCELLATIONS[operation["id"]] = cancellation
        threading.Thread(
            target=run_reserved_derived_background,
            args=(
                operation["id"], action, payload, actor, remote, name,
                cancellation,
            ),
            name=f"derived-{action}-{operation['id'][:8]}",
            daemon=True,
        ).start()
        return operation
    except Exception:
        if operation is not None:
            with DERIVED_BACKGROUND_JOB_LOCK:
                DERIVED_BACKGROUND_CANCELLATIONS.pop(operation["id"], None)
        release_derived_background_job()
        if operation is not None:
            CONTROL.finish_operation(
                operation["id"],
                status="failed",
                error=derived_blocked_error(
                    code="derived_layer.background_start_failed",
                    message="The background worker could not be started.",
                    suggested_action=(
                        "Retry after the local worker-start problem is "
                        "resolved; no derived-layer change was started."
                    ),
                    operation=action,
                ),
            )
        raise


def save_workspace(candidate: dict, expected: str) -> tuple[bytes, str]:
    encoded = (
        json.dumps(
            candidate,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n"
    ).encode()
    with SAVE_LOCK:
        _, _, current_revision = read_workspace()
        if not isinstance(expected, str) or expected != current_revision:
            raise FileExistsError("Workspace changed on disk. Reload before saving.")
        backup = WORKSPACE.with_suffix(WORKSPACE.suffix + ".bak")
        shutil.copyfile(WORKSPACE, backup)
        fd, temporary = tempfile.mkstemp(prefix=".workspace-", suffix=".json", dir=WORKSPACE.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o644)
                next_revision = revision(
                    encoded,
                    os.fstat(stream.fileno()).st_mtime_ns,
                )
            os.replace(temporary, WORKSPACE)
            directory = os.open(
                WORKSPACE.parent,
                os.O_RDONLY | os.O_DIRECTORY,
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return encoded, next_revision


def save_and_reload(
    candidate: dict,
    expected: str,
    *,
    timeout: float = 30,
) -> tuple[bytes, str, str, dict]:
    # Keep a saved generation paired with its reload request and completion
    # check. Otherwise a concurrent save could replace the workspace between
    # the write and the supervisor fingerprinting it.
    with SAVE_RELOAD_LOCK:
        encoded, next_revision = save_workspace(candidate, expected)
        sync_layer_dependency_guard(candidate)
        fingerprint = workspace_fingerprint(encoded)
        try:
            reload_result = request_reload(fingerprint)
            reload_result["status"] = wait_reload(
                reload_result["requestedGeneration"],
                fingerprint,
                timeout,
            )
        except Exception as exc:
            reload_result = {
                "expectedWorkspaceFingerprint": fingerprint,
                "status": {"completed": False},
                "error": f"Reload coordination failed: {type(exc).__name__}",
            }
    return encoded, next_revision, fingerprint, reload_result


def reload_completed(reload_result: dict) -> bool:
    status = reload_result.get("status")
    return isinstance(status, dict) and status.get("completed") is True


def _atomic_preview_text(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def prepare_workspace_preview(
    workspace: dict,
    expected_hash: str,
    *,
    source: str,
    timeout: float = 30,
    wait: bool = True,
) -> dict:
    """Publish one integrity-checked workspace to the isolated preview XYZ."""
    actual_hash = workspace_hash(workspace)
    if actual_hash != expected_hash:
        raise RuntimeError(f"Stored proposal {source} failed its integrity check.")
    encoded = json.dumps(
        workspace, indent=2, ensure_ascii=False, allow_nan=False
    ) + "\n"
    fingerprint = workspace_fingerprint(encoded.encode())
    with PREVIEW_LOCK:
        _atomic_preview_text(PREVIEW_WORKSPACE, encoded, 0o600)
        PREVIEW_RELOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            current = int(
                (PREVIEW_RELOAD_DIR / "requested").read_text().strip() or "0"
            )
        except (FileNotFoundError, OSError, ValueError):
            current = 0
        generation = current + 1
        _atomic_preview_text(
            PREVIEW_RELOAD_DIR / "expected-workspace", fingerprint + "\n"
        )
        _atomic_preview_text(
            PREVIEW_RELOAD_DIR / "requested", f"{generation}\n"
        )
        if not wait:
            return {
                "source": source,
                "generation": generation,
                "workspaceFingerprint": fingerprint,
                "workspaceHash": actual_hash,
                "queued": True,
            }
        deadline = time.monotonic() + timeout
        status = {}
        while time.monotonic() < deadline:
            def read(name: str, default: str = "") -> str:
                try:
                    return (PREVIEW_RELOAD_DIR / name).read_text().strip()
                except (FileNotFoundError, OSError, UnicodeError):
                    return default
            status = {
                "appliedGeneration": read("applied", "0"),
                "workspaceFingerprint": read("workspace-fingerprint"),
                "healthy": read("healthy", "false") == "true",
            }
            if (
                status["appliedGeneration"] == str(generation)
                and status["workspaceFingerprint"] == fingerprint
                and status["healthy"]
            ):
                return {
                    "source": source,
                    "generation": generation,
                    "workspaceFingerprint": fingerprint,
                    "workspaceHash": actual_hash,
                }
            time.sleep(0.1)
    raise TimeoutError("Candidate preview process did not become ready.")


def prepare_candidate_preview(proposal: dict, timeout: float = 30) -> dict:
    """Publish a proposal candidate only to the isolated preview XYZ process."""
    return prepare_workspace_preview(
        proposal.get("candidate"),
        proposal.get("candidateHash"),
        source="candidate",
        timeout=timeout,
    )


def prepare_original_preview(proposal: dict, timeout: float = 30) -> dict:
    """Publish the proposal's integrity-checked original to preview XYZ."""
    return prepare_workspace_preview(
        proposal.get("original"),
        proposal.get("originalHash"),
        source="original",
        timeout=timeout,
    )


def sync_live_preview(encoded: bytes | None = None, timeout: float = 30) -> dict:
    """Publish the current committed workspace as the preview baseline."""
    if encoded is None:
        encoded, workspace, _ = read_workspace()
    else:
        workspace = strict_json_loads(encoded)
    return prepare_workspace_preview(
        workspace,
        workspace_hash(workspace),
        source="live",
        timeout=timeout,
        wait=False,
    )


def _live_preview_sync_worker() -> None:
    while True:
        with PREVIEW_SYNC_LOCK:
            encoded = PREVIEW_SYNC_STATE["pending"]
            PREVIEW_SYNC_STATE["pending"] = None
            if encoded is None:
                PREVIEW_SYNC_STATE["running"] = False
                return
        try:
            sync_live_preview(encoded)
        except Exception as exc:
            CONTROL.audit(
                "preview.sync_failed",
                actor="system",
                details={"error": type(exc).__name__},
            )


def schedule_live_preview_sync(encoded: bytes | None = None) -> None:
    """Coalesce preview refreshes so the newest committed workspace wins."""
    if encoded is None:
        encoded, _, _ = read_workspace()
    with PREVIEW_SYNC_LOCK:
        PREVIEW_SYNC_STATE["pending"] = bytes(encoded)
        if PREVIEW_SYNC_STATE["running"]:
            return
        PREVIEW_SYNC_STATE["running"] = True
    threading.Thread(
        target=_live_preview_sync_worker,
        name="live-preview-sync",
        daemon=True,
    ).start()


_STATIC_INFO_LITERAL = re.compile(
    r"\A\s*'((?:[^']|'')*)'\s*(?:::\s*(?:text|varchar|character\s+varying))?\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _static_info_text(entry: dict) -> str | None:
    """Return visible text only for a simple, constant information expression."""
    expression = entry.get("fieldfx")
    if not isinstance(expression, str):
        return None
    match = _STATIC_INFO_LITERAL.fullmatch(expression)
    if match is None:
        return None
    text = match.group(1).replace("''", "'")
    if entry.get("type") == "html":
        text = html.unescape(re.sub(r"<[^<>]*>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1000] or None


def _info_expectations(entries: list, changed_indexes: set[int]) -> list[str]:
    expected = []
    for index in sorted(changed_indexes):
        if index >= len(entries):
            continue
        entry = entries[index]
        if not isinstance(entry, dict) or entry.get("display") is False:
            continue
        for value in (
            entry.get("title"),
            entry.get("label"),
            _static_info_text(entry),
        ):
            if (
                isinstance(value, str)
                and value.strip()
                and value.strip() not in expected
            ):
                expected.append(value.strip())
    return expected[:20]


def proposal_feature_info_evidence(
    proposal: dict,
    layer_key: str,
    locale_key: str,
) -> dict:
    """Plan clicked-feature evidence independently for each proposal side."""
    try:
        _, original_locale = select_locale(proposal.get("original"), locale_key)
        _, candidate_locale = select_locale(proposal.get("candidate"), locale_key)
        original_layer = (original_locale.get("layers") or {}).get(layer_key)
        candidate_layer = (candidate_locale.get("layers") or {}).get(layer_key)
    except (AttributeError, TypeError, ValueError):
        prefixes = [["locale", "layers", layer_key, "infoj"]]
        if locale_key != "locale":
            prefixes.append(
                ["locales", locale_key, "layers", layer_key, "infoj"]
            )
        changed = False
        for item in proposal.get("diff") or []:
            try:
                parts = pointer_parts(item.get("path"))
            except (TypeError, ValueError):
                continue
            if any(parts[:len(prefix)] == prefix for prefix in prefixes):
                changed = True
                break
        return {
            "changed": changed,
            "original": {"requested": changed, "expectedText": []},
            "candidate": {"requested": changed, "expectedText": []},
        }
    original_entries = (
        original_layer.get("infoj")
        if isinstance(original_layer, dict)
        and isinstance(original_layer.get("infoj"), list)
        else []
    )
    candidate_entries = (
        candidate_layer.get("infoj")
        if isinstance(candidate_layer, dict)
        and isinstance(candidate_layer.get("infoj"), list)
        else []
    )
    changed = original_entries != candidate_entries
    changed_indexes = {
        index
        for index in range(max(len(original_entries), len(candidate_entries)))
        if (
            original_entries[index] if index < len(original_entries) else None
        ) != (
            candidate_entries[index] if index < len(candidate_entries) else None
        )
    }

    def side(layer: object, entries: list) -> dict:
        visible = any(
            isinstance(entry, dict) and entry.get("display") is not False
            for entry in entries
        )
        return {
            "requested": bool(changed and isinstance(layer, dict) and visible),
            "expectedText": _info_expectations(entries, changed_indexes),
        }

    return {
        "changed": changed,
        "original": side(original_layer, original_entries),
        "candidate": side(candidate_layer, candidate_entries),
    }


def proposal_changes_feature_info(
    proposal: dict,
    layer_key: str,
    locale_key: str,
) -> bool:
    """Whether this proposal changes feature information for the rendered layer."""
    return proposal_feature_info_evidence(
        proposal,
        layer_key,
        locale_key,
    )["changed"]


def expected_info_panel_text(payload: dict) -> list[str]:
    raw = payload.get("expectedInfoPanelText")
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError(
            "expectedInfoPanelText must be an array containing at most 20 strings."
        )
    expected = []
    for value in raw:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > 1000
        ):
            raise ValueError(
                "expectedInfoPanelText entries must be non-empty strings of "
                "at most 1000 characters."
            )
        if value.strip() not in expected:
            expected.append(value.strip())
    return expected


def plan_expected_info_panel(plan: dict, payload: dict) -> None:
    expected = expected_info_panel_text(payload)
    if not expected:
        return
    interaction = plan.get("interaction")
    if not isinstance(interaction, dict) or not interaction.get("type"):
        raise ValueError(
            "Clicked-feature information evidence is unavailable for this layer."
        )
    plan["interaction"] = {
        **interaction,
        "requireInfoPanel": True,
        "expectedInfoPanelText": expected,
    }


def feature_info_observation(result: dict, evidence: dict) -> dict:
    interaction = result.get("interaction")
    interaction = interaction if isinstance(interaction, dict) else {}
    found = interaction.get("expectedInfoPanelTextFound")
    found = found if isinstance(found, dict) else {}
    expected = evidence.get("expectedText") or []
    captured = interaction.get("infoPanelExpanded") is True
    return {
        **evidence,
        "captured": captured,
        "artifact": (result.get("artifacts") or {}).get("infoPanel"),
        "expectedTextFound": {
            text: found.get(text) is True
            for text in expected
        },
        "passed": (
            not evidence.get("requested")
            or (
                captured
                and all(found.get(text) is True for text in expected)
            )
        ),
    }


def proposal_group_preview(
    proposal: dict,
    requested_layer: str,
    locale_key: str | None,
) -> dict:
    """Resolve original/candidate group membership for proposal rendering."""
    selected_locale, candidate_locale = select_locale(
        proposal["candidate"],
        locale_key,
    )
    _, original_locale = select_locale(
        proposal["original"],
        selected_locale,
    )

    def layer_map(locale: dict) -> dict:
        layers = locale.get("layers") or {}
        return layers if isinstance(layers, dict) else {}

    original_layers = layer_map(original_locale)
    candidate_layers = layer_map(candidate_locale)
    original_layer = original_layers.get(requested_layer)
    candidate_layer = candidate_layers.get(requested_layer)
    original_present = isinstance(original_layer, dict)
    candidate_present = isinstance(candidate_layer, dict)
    original_group = (
        original_layer.get("group") if original_present else None
    )
    candidate_group = (
        candidate_layer.get("group") if candidate_present else None
    )
    change_kind = (
        "added"
        if not original_present and candidate_present
        else "removed"
        if original_present and not candidate_present
        else "moved"
        if original_present
        and candidate_present
        and original_group != candidate_group
        else "edited"
    )
    groups = []
    for layer in (original_layer, candidate_layer):
        group = layer.get("group") if isinstance(layer, dict) else None
        if isinstance(group, str) and group and group not in groups:
            groups.append(group)

    def side_selection(layers: dict) -> dict:
        requested_present = isinstance(layers.get(requested_layer), dict)
        selected = (
            [requested_layer] if requested_present else []
        ) if change_kind in {"added", "removed", "moved"} else [
                key
                for key, layer in layers.items()
                if (
                    isinstance(key, str)
                    and isinstance(layer, dict)
                    and layer.get("group") in groups
                )
            ]
        if not groups and requested_present and requested_layer not in selected:
            selected.append(requested_layer)
        anchor_candidates = [
            key
            for key in selected
            if is_probeable_database_layer(layers[key])
        ] or selected
        if not anchor_candidates:
            anchor_candidates = [
                key
                for key in ("OpenStreetMap", *layers)
                if key in layers and isinstance(layers[key], dict)
            ]
        if not anchor_candidates:
            raise ValueError(
                "The proposal workspace has no layer available for visual planning."
            )
        return {
            "anchorLayer": (
                requested_layer
                if requested_layer in anchor_candidates
                else anchor_candidates[0]
            ),
            "renderLayer": (
                requested_layer
                if requested_layer in selected
                else None
            ),
            "layers": selected,
            "backgroundLayers": [
                key
                for key, layer in layers.items()
                if (
                    isinstance(key, str)
                    and isinstance(layer, dict)
                    and layer.get("format") == "tiles"
                    and layer.get("display") is True
                )
            ],
            "groups": [
                group
                for group in groups
                if any(
                    isinstance(layer, dict)
                    and layer.get("group") == group
                    for layer in layers.values()
                )
            ],
            "requestedLayerPresent": requested_present,
            "configuredLayerKeys": list(layers),
            "groupMembership": {
                key: layer.get("group")
                for key, layer in layers.items()
                if isinstance(key, str) and isinstance(layer, dict)
            },
        }

    return {
        "locale": selected_locale,
        "requestedLayer": requested_layer,
        "changeKind": change_kind,
        "groups": groups,
        "original": side_selection(original_layers),
        "candidate": side_selection(candidate_layers),
    }


def preview_proposal(proposal_id: str) -> dict:
    proposal = proposal_read(CONTROL, proposal_id)
    if proposal.get("status") != "pending":
        raise ValueError(f"Proposal is {proposal.get('status')}.")
    candidate = proposal.get("candidate")
    if not isinstance(candidate, dict) or workspace_hash(candidate) != proposal.get(
        "candidateHash"
    ):
        raise RuntimeError("Stored proposal candidate failed its integrity check.")
    _, _, current_revision = read_workspace()
    if current_revision != proposal.get("originalRevision"):
        raise FileExistsError(
            "Proposal has been superseded by a newer workspace revision."
        )
    if proposal.get("pluginCatalogueFingerprint") != plugin_catalogue()["fingerprint"]:
        raise FileExistsError(
            "Proposal plugin catalogue changed; create and preview a new proposal."
        )
    return proposal


def plugin_preview_checks(workspace: dict, locale_key: str, layers: list[str]) -> list[dict]:
    catalogue = plugin_catalogue()
    by_id = {entry["id"]: entry for entry in catalogue["external"] if entry.get("available")}
    usage = plugin_usage(workspace)
    selected_paths = {
        f"{locale_key}.layers.{layer}" for layer in layers
    } | {locale_key}
    checks = []
    for item in usage:
        if item["path"] not in selected_paths:
            continue
        plugin = by_id[item["pluginId"]]
        checks.append({
            "id": plugin["id"],
            "registrationKey": plugin["registrationKey"],
            "entryUrl": plugin["entryUrl"],
            "entryHash": plugin["files"][0]["sha256"] if plugin.get("files") else None,
            "scope": item["scope"],
            "path": item["path"],
            "assertions": plugin["previewAssertions"],
        })
    return checks


def run_browser_visual(layer_key: str | None, plan: dict, payload: dict, *,
                       target_url: str) -> tuple[int, dict]:
    panels = visual_panels(payload)
    metadata = payload.get("metadata")
    operation_id = (
        metadata.get("operationId")
        if isinstance(metadata, dict)
        else None
    )
    if isinstance(operation_id, str):
        update_visual_operation_progress(operation_id, "browser-execution")
    runner_payload = json.dumps(
        {
            "url": target_url,
            "layer": layer_key,
            "layerTitle": plan.get("layerTitle") if layer_key else None,
            "layers": plan.get("layers", [layer_key]),
            "plan": plan,
            "viewport": payload.get("viewport", {"width": 1920, "height": 1080}),
            "deviceScaleFactor": payload.get("deviceScaleFactor", 2),
            "fullPage": payload.get("fullPage", True),
            "viewMode": payload.get("viewMode", "focus"),
            "hover": requested_hover(payload),
            "expectedHoverText": expected_hover_text(payload),
            "panels": panels,
            "expectedPanelText": payload.get("expectedPanelText", []),
            "metadata": metadata,
            "pluginChecks": plan.get("pluginChecks", []),
            "runTimeout": VISUAL_BROWSER_TIMEOUT_SECONDS * 1000,
        },
        allow_nan=False,
    ).encode()
    try:
        with urlopen(Request(
            os.environ.get(
                "BROWSER_RUNNER_URL", "http://browser-runner:8080/run"
            ),
            data=runner_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        ), timeout=VISUAL_BROWSER_TIMEOUT_SECONDS + 15) as response:
            result = json.load(response)
            if isinstance(operation_id, str):
                update_visual_operation_progress(
                    operation_id,
                    "artifact-binding",
                    result.get("diagnostics")
                    if isinstance(result, dict)
                    else None,
                )
            return HTTPStatus.OK, result
    except HTTPError as exc:
        try:
            result = strict_json_loads(exc.read())
        except (OSError, UnicodeError, ValueError):
            result = None
        if isinstance(result, dict):
            if isinstance(operation_id, str):
                update_visual_operation_progress(
                    operation_id,
                    result.get("failedStage") or "browser-response",
                    result.get("diagnostics"),
                )
            return exc.code, result
        return HTTPStatus.BAD_GATEWAY, {
            "error": f"Browser validation service returned HTTP {exc.code}."
        }
    except TimeoutError as exc:
        diagnostics = {
            "exceptionType": type(exc).__name__,
            "browserRunnerUrl": "configured-internal-runner",
        }
        if isinstance(operation_id, str):
            update_visual_operation_progress(
                operation_id, "browser-transport", diagnostics
            )
        return HTTPStatus.GATEWAY_TIMEOUT, {
            "error": "Browser validation did not return before its deadline.",
            "code": "visual.browser_transport_timeout",
            "failedStage": "browser-transport",
            "diagnostics": diagnostics,
            "metadata": metadata,
        }
    except Exception as exc:
        diagnostics = {"exceptionType": type(exc).__name__}
        if isinstance(operation_id, str):
            update_visual_operation_progress(
                operation_id, "browser-transport", diagnostics
            )
        return HTTPStatus.BAD_GATEWAY, {
            "error": f"Browser validation failed: {exc}",
            "code": "visual.browser_transport_failed",
            "failedStage": "browser-transport",
            "diagnostics": diagnostics,
            "metadata": metadata,
        }


def browser_result_has_evidence(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    if isinstance(result.get("runId"), str) and result["runId"]:
        return True
    artifacts = result.get("artifacts")
    return isinstance(artifacts, dict) and any(
        isinstance(path, str) and path for path in artifacts.values()
    )


def visual_failure_code(status: int) -> str:
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return "visual.busy"
    if status in {HTTPStatus.GATEWAY_TIMEOUT, HTTPStatus.REQUEST_TIMEOUT}:
        return "visual.upstream_timeout"
    return "visual.failed"


def visual_failure_error(status: int, result: dict, message: str) -> dict:
    error = {
        "code": result.get("code") or visual_failure_code(status),
        "message": message,
    }
    for key in ("failedStage", "timeoutMilliseconds", "diagnostics"):
        if result.get(key) is not None:
            error[key] = result[key]
    if result.get("diagnosis") is not None:
        error["diagnosis"] = result["diagnosis"]
    return error


def visual_panels(payload: dict) -> list[str]:
    raw = payload.get("panels")
    if raw is None:
        raw = payload.get("panel")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("Visual panels must be 'filtering', 'styling', or a list.")
    panels: list[str] = []
    for item in raw:
        if item not in {"filtering", "styling"}:
            raise ValueError("Visual panels must be 'filtering' or 'styling'.")
        if item not in panels:
            panels.append(item)
    return panels


def expected_hover_text(payload: dict) -> list[str]:
    raw = payload.get("expectedHoverText")
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError(
            "expectedHoverText must be an array containing at most 20 strings."
        )
    expected = []
    for value in raw:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > 1000
        ):
            raise ValueError(
                "expectedHoverText entries must be non-empty strings of at "
                "most 1000 characters."
            )
        if value.strip() not in expected:
            expected.append(value.strip())
    return expected


def requested_hover(payload: dict) -> bool | None:
    if "hover" not in payload:
        return None
    if not isinstance(payload["hover"], bool):
        raise ValueError("hover must be true or false.")
    return payload["hover"]


def apply_proposal_and_reload(
    store: ControlStore,
    proposal: dict,
    *,
    actor: str,
    timeout: float = 30,
) -> tuple[dict, dict]:
    """Apply a revision-bound proposal with recoverable on-disk transitions.

    ``applying`` is persisted before the workspace write. If the process exits
    after the atomic workspace replacement but before the proposal is marked
    applied, a repeated approved apply request can recognize the exact
    candidate hash, finish the proposal record, and request XYZ reload again.
    """
    with SAVE_RELOAD_LOCK:
        raw, current_workspace, current_revision = read_workspace()
        candidate = proposal["candidate"]
        candidate_hash = proposal["candidateHash"]
        recovered = False

        if proposal["status"] == "pending":
            if current_revision != proposal["originalRevision"]:
                raise FileExistsError(
                    "Workspace changed on disk. Reload before saving."
                )
            proposal["status"] = "applying"
            proposal["applyingStarted"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            )
            proposal["applyingActor"] = actor
            proposal_write(store, proposal)
        elif proposal["status"] == "applying":
            if workspace_hash(current_workspace) == candidate_hash:
                # The prior process committed the candidate but did not finish
                # recording the proposal transition.
                encoded = raw
                next_revision = current_revision
                recovered = True
            elif current_revision != proposal["originalRevision"]:
                proposal["status"] = "conflicted"
                proposal["conflictingRevision"] = current_revision
                proposal["conflictedAt"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(),
                )
                proposal_write(store, proposal)
                raise FileExistsError(
                    "Workspace changed while proposal application was "
                    "interrupted. Create a new proposal."
                )
        else:
            raise FileExistsError(f"Proposal is {proposal['status']}.")

        if not recovered:
            try:
                encoded, next_revision = save_workspace(
                    candidate,
                    proposal["originalRevision"],
                )
                sync_layer_dependency_guard(candidate)
            except FileExistsError:
                raw, current_workspace, current_revision = read_workspace()
                if workspace_hash(current_workspace) == candidate_hash:
                    encoded = raw
                    next_revision = current_revision
                    recovered = True
                else:
                    proposal["status"] = "conflicted"
                    proposal["conflictingRevision"] = current_revision
                    proposal["conflictedAt"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(),
                    )
                    proposal_write(store, proposal)
                    raise

        fingerprint = workspace_fingerprint(encoded)
        try:
            reload_result = request_reload(fingerprint)
        except Exception as exc:
            reload_result = {
                "expectedWorkspaceFingerprint": fingerprint,
                "status": {"completed": False},
                "error": f"Reload coordination failed: {type(exc).__name__}",
            }

        # Persist the committed proposal immediately after the reload request,
        # before waiting for XYZ. A crash or timeout cannot leave a committed
        # workspace represented as a pending proposal.
        proposal["status"] = "applied"
        proposal["appliedRevision"] = next_revision
        proposal["appliedFingerprint"] = fingerprint
        proposal["appliedAt"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        )
        if recovered:
            proposal["applicationRecovered"] = True
        if isinstance(reload_result.get("requestedGeneration"), int):
            proposal["requestedGeneration"] = reload_result[
                "requestedGeneration"
            ]
        proposal_write(store, proposal)
        schedule_live_preview_sync(encoded)

        generation = reload_result.get("requestedGeneration")
        if isinstance(generation, int):
            try:
                reload_result["status"] = wait_reload(
                    generation,
                    fingerprint,
                    timeout,
                )
            except Exception as exc:
                reload_result["status"] = {"completed": False}
                reload_result["error"] = (
                    f"Reload completion check failed: {type(exc).__name__}"
                )
    return proposal, reload_result


def discover_icons() -> list[dict[str, str]]:
    SVG_DIR.mkdir(parents=True, exist_ok=True)
    return [
        {
            "name": path.stem.replace("_", " ").replace("-", " ").title(),
            "filename": path.name,
            "url": f"{SVG_URL_PREFIX}/{path.name}",
        }
        for path in sorted(SVG_DIR.iterdir(), key=lambda item: item.name.lower())
        if safe_svg(path)
    ]


def icon_path(url_path: str) -> Path | None:
    prefix = f"{SVG_URL_PREFIX}/"
    if not url_path.startswith(prefix):
        return None
    filename = url_path.removeprefix(prefix)
    if not filename or filename != Path(filename).name:
        return None
    path = SVG_DIR / filename
    return path if safe_svg(path) else None


def discover_connection(db_name: str, database_url: str) -> list[dict]:
    query = """
      SELECT n.nspname AS schema, c.relname AS table, a.attname AS column,
             format_type(a.atttypid, a.atttypmod) AS data_type,
             CASE WHEN a.atttypid = 'geometry'::regtype
               THEN postgis_typmod_type(a.atttypmod)
               ELSE ''
             END AS geometry_type,
             CASE WHEN a.atttypid = 'geometry'::regtype
               THEN postgis_typmod_srid(a.atttypmod)
               ELSE NULL
             END AS srid,
             NOT a.attnotnull AS nullable,
             COALESCE(ix.is_primary, false) AS primary_key,
             COALESCE(ix.is_unique, false) AS unique_key
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
      LEFT JOIN (
        SELECT i.indrelid, unnest(i.indkey) AS attnum,
               bool_or(i.indisprimary) AS is_primary,
               bool_or(i.indisunique AND i.indnkeyatts = 1) AS is_unique
        FROM pg_index i GROUP BY i.indrelid, unnest(i.indkey)
      ) ix ON ix.indrelid = c.oid AND ix.attnum = a.attnum
      WHERE c.relkind IN ('r', 'p', 'v', 'm')
        AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        AND has_schema_privilege(n.oid, 'USAGE')
        AND has_table_privilege(c.oid, 'SELECT')
      ORDER BY n.nspname, c.relname, a.attnum
    """
    with psycopg.connect(database_url, connect_timeout=5, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute(query)
            rows = cur.fetchall()
    tables: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["schema"], row["table"])
        table = tables.setdefault(key, {"dbs": db_name, "schema": key[0], "table": key[1], "columns": []})
        table["columns"].append({
            "name": row["column"], "type": row["data_type"],
            "geometryType": row["geometry_type"], "srid": row["srid"],
            "nullable": row["nullable"], "primaryKey": row["primary_key"],
            "unique": row["unique_key"],
        })
    # XYZ map layers need geometry; hide metadata and ordinary relational tables.
    return [table for table in tables.values() if any(column["geometryType"] for column in table["columns"])]


def discover() -> list[dict]:
    tables = []
    for db_name, database_url in sorted(DB_CONNECTIONS.items()):
        tables.extend(discover_connection(db_name, database_url))
    return tables


def discover_catalog() -> list[dict]:
    """Return tables offered for layer discovery in the dashboard/API.

    Keep ``discover()`` complete because workspace validation must continue to
    recognise an explicitly configured legacy ``public.*`` layer.  The public
    schema is omitted only from the server catalog used to add new layers.
    """
    ready_derived = {
        profile["name"]
        for profile in derived_semantic_profiles()
        if profile["status"] == "ready"
    } if DERIVED else set()
    return [
        table
        for table in discover()
        if table.get("schema") != "public"
        and (
            table.get("schema") != "derived_layers"
            or table.get("table") in ready_derived
        )
    ]


def layer_db(data: dict, layer: dict) -> str | None:
    return layer.get("dbs") or data.get("dbs")


def aggregate_layer_values(
    data: dict,
    requested_locale: str | None,
    layer_key: str,
    field: str,
    limit: int,
) -> dict:
    """Return bounded category counts for one stored layer column."""
    locale_key, locale = select_locale(data, requested_locale)
    layer = (locale.get("layers") or {}).get(layer_key)
    if not isinstance(layer, dict):
        raise FileNotFoundError(layer_key)
    if not is_probeable_database_layer(layer):
        raise ValueError(
            "The selected layer does not use a queryable database relation."
        )

    db_name = layer_db(data, layer)
    database_url = DB_CONNECTIONS.get(db_name)
    if not database_url:
        raise ValueError("The selected layer database is not configured.")
    relation_name = layer.get("table")
    if (
        not isinstance(relation_name, str)
        or not relation_name
        or relation_name.count(".") > 1
    ):
        raise ValueError(
            "The selected layer does not use a valid database relation."
        )
    relation_parts = relation_name.split(".")
    if len(relation_parts) == 1:
        relation_parts.insert(0, "public")
    schema_name, table_name = relation_parts
    relation = psycopg.sql.SQL("{}.{}").format(
        psycopg.sql.Identifier(schema_name),
        psycopg.sql.Identifier(table_name),
    )
    column = psycopg.sql.Identifier(field)
    effective_filter, filter_params, filter_descriptor = (
        effective_layer_filter(layer)
    )

    with psycopg.connect(database_url, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute("SET LOCAL search_path = pg_catalog, public")
            cur.execute(
                """
                SELECT format_type(attribute.atttypid, attribute.atttypmod),
                       type.typname = 'geometry'
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_attribute AS attribute
                  ON attribute.attrelid = relation.oid
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
                JOIN pg_catalog.pg_type AS type
                  ON type.oid = attribute.atttypid
                WHERE namespace.nspname = %s
                  AND relation.relname = %s
                  AND attribute.attname = %s
                  AND relation.relkind IN ('r', 'p', 'v', 'm')
                  AND has_schema_privilege(namespace.oid, 'USAGE')
                  AND has_table_privilege(relation.oid, 'SELECT')
                """,
                (schema_name, table_name, field),
            )
            field_metadata = cur.fetchone()
            if not field_metadata:
                raise ValueError(
                    f'The field “{field}” is not a selectable column on this layer.'
                )
            field_type, is_geometry = field_metadata
            if is_geometry:
                raise ValueError(
                    "Geometry columns cannot be aggregated as style values."
                )

            cur.execute(
                psycopg.sql.SQL(
                    "SELECT count(*)::bigint, count({column})::bigint, "
                    "count(DISTINCT {column})::bigint FROM {relation} "
                    "WHERE {effective_filter}"
                ).format(
                    column=column,
                    relation=relation,
                    effective_filter=effective_filter,
                ),
                filter_params,
            )
            total_count, non_null_count, distinct_count = cur.fetchone()
            cur.execute(
                psycopg.sql.SQL(
                    "SELECT to_jsonb({column}), count(*)::bigint "
                    "FROM {relation} WHERE {effective_filter} "
                    "AND {column} IS NOT NULL "
                    "GROUP BY {column} "
                    "ORDER BY count(*) DESC, to_jsonb({column})::text "
                    "LIMIT %s"
                ).format(
                    column=column,
                    relation=relation,
                    effective_filter=effective_filter,
                ),
                (*filter_params, limit),
            )
            values = [
                {"value": value, "count": count}
                for value, count in cur.fetchall()
            ]

    return {
        "locale": locale_key,
        "key": layer_key,
        "field": field,
        "fieldType": field_type,
        "effectiveDataset": {
            "scope": "effective-locale-layer",
            "relation": relation_name,
            "effectiveFilter": filter_descriptor,
        },
        "totalCount": total_count,
        "nonNullCount": non_null_count,
        "nullCount": total_count - non_null_count,
        "distinctCount": distinct_count,
        "values": values,
        "limit": limit,
        "truncated": distinct_count > len(values),
    }


def aggregate_layer_statistics(
    data: dict,
    requested_locale: str | None,
    layer_key: str,
    field: str,
    bins: int,
    thresholds: list[float],
    breaks: list[float],
) -> dict:
    """Return bounded numeric distribution metadata without returning rows."""
    locale_key, locale = select_locale(data, requested_locale)
    layer = (locale.get("layers") or {}).get(layer_key)
    if not isinstance(layer, dict):
        raise FileNotFoundError(layer_key)
    if not is_probeable_database_layer(layer):
        raise ValueError(
            "The selected layer does not use a queryable database relation."
        )

    db_name = layer_db(data, layer)
    database_url = DB_CONNECTIONS.get(db_name)
    if not database_url:
        raise ValueError("The selected layer database is not configured.")
    relation_name = layer.get("table")
    if (
        not isinstance(relation_name, str)
        or not relation_name
        or relation_name.count(".") > 1
    ):
        raise ValueError(
            "The selected layer does not use a valid database relation."
        )
    relation_parts = relation_name.split(".")
    if len(relation_parts) == 1:
        relation_parts.insert(0, "public")
    schema_name, table_name = relation_parts
    relation = psycopg.sql.SQL("{}.{}").format(
        psycopg.sql.Identifier(schema_name),
        psycopg.sql.Identifier(table_name),
    )
    column = psycopg.sql.Identifier(field)
    effective_filter, filter_params, filter_descriptor = (
        effective_layer_filter(layer)
    )
    quantile_probabilities = (0.0, 0.25, 0.5, 0.75, 1.0)

    with psycopg.connect(database_url, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute("SET LOCAL search_path = pg_catalog, public")
            cur.execute(
                """
                SELECT format_type(attribute.atttypid, attribute.atttypmod),
                       type.typname,
                       type.typname = 'geometry'
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                JOIN pg_catalog.pg_attribute AS attribute
                  ON attribute.attrelid = relation.oid
                 AND attribute.attnum > 0
                 AND NOT attribute.attisdropped
                JOIN pg_catalog.pg_type AS type
                  ON type.oid = attribute.atttypid
                WHERE namespace.nspname = %s
                  AND relation.relname = %s
                  AND attribute.attname = %s
                  AND relation.relkind IN ('r', 'p', 'v', 'm')
                  AND has_schema_privilege(namespace.oid, 'USAGE')
                  AND has_table_privilege(relation.oid, 'SELECT')
                """,
                (schema_name, table_name, field),
            )
            field_metadata = cur.fetchone()
            if not field_metadata:
                raise ValueError(
                    f'The field “{field}” is not a selectable column on this layer.'
                )
            field_type, type_name, is_geometry = field_metadata
            if is_geometry or type_name not in {
                "int2", "int4", "int8", "float4", "float8", "numeric",
            }:
                raise ValueError(
                    "Layer statistics require a stored numeric field."
                )
            finite_value = (
                psycopg.sql.SQL(
                    "{column} BETWEEN "
                    "'-1.7976931348623157e308'::numeric AND "
                    "'1.7976931348623157e308'::numeric"
                ).format(column=column)
                if type_name == "numeric"
                else psycopg.sql.SQL(
                    "{column}::double precision NOT IN "
                    "('NaN'::double precision, 'Infinity'::double precision, "
                    "'-Infinity'::double precision)"
                ).format(column=column)
            )
            safe_value = psycopg.sql.SQL(
                "CASE WHEN {finite} THEN {column}::double precision END"
            ).format(finite=finite_value, column=column)

            cur.execute(
                psycopg.sql.SQL(
                    "SELECT count(*)::bigint, count({column})::bigint, "
                    "count({column}) FILTER (WHERE {finite}), "
                    "min({safe_value}), max({safe_value}), "
                    "percentile_disc(ARRAY[0, 0.25, 0.5, 0.75, 1]"
                    "::double precision[]) WITHIN GROUP "
                    "(ORDER BY {safe_value}) "
                    "FILTER (WHERE {finite}) "
                    "FROM {relation} WHERE {effective_filter}"
                ).format(
                    column=column,
                    finite=finite_value,
                    safe_value=safe_value,
                    relation=relation,
                    effective_filter=effective_filter,
                ),
                filter_params,
            )
            (
                total_count,
                non_null_count,
                finite_count,
                minimum,
                maximum,
                quantile_values,
            ) = cur.fetchone()

            histogram_counts: dict[int, int] = {}
            if finite_count and minimum != maximum:
                cur.execute(
                    psycopg.sql.SQL(
                        "WITH rendered AS ("
                        " SELECT {safe_value} AS value"
                        " FROM {relation}"
                        " WHERE {effective_filter}"
                        " AND {column} IS NOT NULL AND {finite}"
                        ") "
                        "SELECT LEAST(%s, width_bucket(value, %s, %s, %s)) "
                        "AS bucket, count(*)::bigint "
                        "FROM rendered GROUP BY bucket ORDER BY bucket"
                    ).format(
                        column=column,
                        safe_value=safe_value,
                        relation=relation,
                        effective_filter=effective_filter,
                        finite=finite_value,
                    ),
                    (*filter_params, bins, minimum, maximum, bins),
                )
                histogram_counts = dict(cur.fetchall())

            count_expressions = []
            count_params: list[float] = []
            for threshold in thresholds:
                count_expressions.extend([
                    psycopg.sql.SQL("count(*) FILTER (WHERE value < %s)"),
                    psycopg.sql.SQL("count(*) FILTER (WHERE value >= %s)"),
                ])
                count_params.extend([threshold, threshold])
            if breaks:
                count_expressions.append(
                    psycopg.sql.SQL("count(*) FILTER (WHERE value < %s)")
                )
                count_params.append(breaks[0])
                for lower, upper in zip(breaks, breaks[1:]):
                    count_expressions.append(psycopg.sql.SQL(
                        "count(*) FILTER "
                        "(WHERE value >= %s AND value < %s)"
                    ))
                    count_params.extend([lower, upper])
                count_expressions.append(
                    psycopg.sql.SQL("count(*) FILTER (WHERE value >= %s)")
                )
                count_params.append(breaks[-1])
            requested_counts = ()
            if count_expressions:
                cur.execute(
                    psycopg.sql.SQL(
                        "SELECT {counts} FROM ("
                        " SELECT {safe_value} AS value"
                        " FROM {relation}"
                        " WHERE {effective_filter}"
                        " AND {column} IS NOT NULL AND {finite}"
                        ") AS rendered"
                    ).format(
                        counts=psycopg.sql.SQL(", ").join(count_expressions),
                        column=column,
                        safe_value=safe_value,
                        relation=relation,
                        effective_filter=effective_filter,
                        finite=finite_value,
                    ),
                    (*count_params, *filter_params),
                )
                requested_counts = cur.fetchone()

    minimum = float(minimum) if minimum is not None else None
    maximum = float(maximum) if maximum is not None else None
    quantiles = [] if not finite_count else [
        {"probability": probability, "value": float(value)}
        for probability, value in zip(
            quantile_probabilities, quantile_values or (),
        )
    ]
    if not finite_count:
        histogram = []
    elif minimum == maximum:
        histogram = [{
            "index": 1,
            "lower": minimum,
            "upper": maximum,
            "count": finite_count,
            "lowerInclusive": True,
            "upperInclusive": True,
        }]
    else:
        def boundary(index: int) -> float:
            if index == 0:
                return minimum
            if index == bins:
                return maximum
            ratio = index / bins
            return minimum * (1.0 - ratio) + maximum * ratio

        histogram = [
            {
                "index": index,
                "lower": boundary(index - 1),
                "upper": boundary(index),
                "count": histogram_counts.get(index, 0),
                "lowerInclusive": True,
                "upperInclusive": index == bins,
            }
            for index in range(1, bins + 1)
        ]

    offset = 0
    threshold_counts = []
    for threshold in thresholds:
        threshold_counts.append({
            "value": threshold,
            "belowCount": requested_counts[offset],
            "atOrAboveCount": requested_counts[offset + 1],
        })
        offset += 2
    classes = []
    if breaks:
        class_counts = requested_counts[offset:]
        bounds = [(None, breaks[0]), *zip(breaks, breaks[1:]), (breaks[-1], None)]
        classes = [
            {
                "index": index,
                "lower": lower,
                "upper": upper,
                "count": class_counts[index],
                "lowerInclusive": lower is not None,
                "upperInclusive": False,
            }
            for index, (lower, upper) in enumerate(bounds)
        ]

    return {
        "locale": locale_key,
        "key": layer_key,
        "field": field,
        "fieldType": field_type,
        "effectiveDataset": {
            "scope": "effective-locale-layer",
            "relation": relation_name,
            "effectiveFilter": filter_descriptor,
        },
        "totalCount": total_count,
        "nonNullCount": non_null_count,
        "nullCount": total_count - non_null_count,
        "finiteCount": finite_count,
        "nonFiniteCount": non_null_count - finite_count,
        "min": minimum,
        "max": maximum,
        "quantiles": quantiles,
        "histogram": histogram,
        "thresholds": threshold_counts,
        "classes": classes,
        "binsRequested": bins,
        "binsReturned": len(histogram),
    }


def locale_items(data: dict) -> list[tuple[str, dict]]:
    return [
        (
            "locale" if key == "locale" else f"locales.{key}",
            locale,
        )
        for key, locale in effective_locales(data).items()
    ]


def workspace_derived_bindings(data: dict) -> list[dict]:
    bindings = []
    for locale_path, locale in locale_items(data):
        if not isinstance(locale, dict):
            continue
        for layer_key, layer in (locale.get("layers") or {}).items():
            if (
                not isinstance(layer, dict)
                or not str(layer.get("table") or "").startswith("derived_layers.")
            ):
                continue
            bindings.append({
                "identity": (
                    locale_path,
                    layer_key,
                    layer_db(data, layer),
                    layer["table"],
                ),
                "path": f"{locale_path}.layers.{layer_key}.table",
                "relation": layer["table"],
                "name": layer["table"].removeprefix("derived_layers."),
            })
    return bindings


def semantic_publication_diagnostics(
    candidate: dict,
    original: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    candidate_bindings = workspace_derived_bindings(candidate)
    if not candidate_bindings:
        return [], []
    original_bindings = {
        binding["identity"]
        for binding in workspace_derived_bindings(original or {})
    }
    try:
        profiles = {
            profile["name"]: profile
            for profile in derived_semantic_profiles()
        }
    except (DerivedLayerError, psycopg.Error):
        profiles = {}
    errors, warnings = [], []
    for binding in candidate_bindings:
        profile = profiles.get(binding["name"])
        status = profile.get("status") if profile else "unavailable"
        if status == "ready":
            continue
        diagnostic = {
            "path": binding["path"],
            "code": "semantic.derived_not_ready",
            "status": status,
            "relation": binding["relation"],
            "message": (
                f'{binding["relation"]} has semantic profile status '
                f"“{status}”. Resolve the cause and retry semantic delivery."
            ),
        }
        if binding["identity"] in original_bindings:
            warnings.append(diagnostic)
        else:
            errors.append(diagnostic)
    return errors, warnings


def layer_key_diagnostics(
    candidate: dict,
    original: dict | None = None,
) -> list[dict]:
    """Warn when an accepted XYZ key is less stable than a machine key."""
    warnings = []
    original_locales = dict(locale_items(original or {}))

    configured_locales = []
    default_locale = candidate.get("locale")
    if isinstance(default_locale, dict):
        configured_locales.append(("locale", default_locale))
    named_locales = candidate.get("locales")
    if isinstance(named_locales, dict):
        configured_locales.extend(
            (f"locales.{key}", locale)
            for key, locale in named_locales.items()
            if key != "locale" and isinstance(locale, dict)
        )

    for locale_path, locale in configured_locales:
        if not isinstance(locale, dict):
            continue
        original_layers = (
            original_locales.get(locale_path, {}).get("layers") or {}
        )
        for key in (locale.get("layers") or {}):
            if (
                key in original_layers
                or not isinstance(key, str)
                or re.fullmatch(r"[A-Za-z0-9_]+", key)
            ):
                continue
            recommended = re.sub(r"[^A-Za-z0-9_]+", "_", key).strip("_")
            warnings.append({
                "path": f"{locale_path}.layers.{key}",
                "code": "workspace.layer_key_noncanonical",
                "configuredKey": key,
                "resolvedBrowserKey": key,
                "recommendedKey": recommended or "layer",
                "severity": "warning",
                "message": (
                    "This accepted XYZ layer key contains display-oriented "
                    "characters. Prefer ASCII letters, numbers, and underscores "
                    "for stable URL activation; keep display wording in name."
                ),
            })
    return warnings


def validate_catalog(data: dict, tables: list[dict]) -> list[dict[str, str]]:
    errors = []
    index = {(table["dbs"], f'{table["schema"]}.{table["table"]}'): table for table in tables}
    for locale_path, locale in locale_items(data):
        if not isinstance(locale, dict):
            continue
        for key, layer in (locale.get("layers") or {}).items():
            path = f"{locale_path}.layers.{key}"
            if (
                isinstance(layer, dict)
                and layer.get("format") in DATABASE_LAYER_FORMATS
                and not isinstance(layer.get("template"), str)
                and not isinstance(layer.get("features"), list)
                and isinstance(layer.get("tables"), dict)
            ):
                db_name = layer_db(data, layer)
                for zoom, relation in layer["tables"].items():
                    if relation is not None and not index.get((db_name, relation)):
                        errors.append({
                            "path": f"{path}.tables.{zoom}",
                            "message": "Table is not selectable through the configured read-only connection.",
                        })
                continue
            if not is_probeable_database_layer(layer):
                continue
            table = index.get((layer_db(data, layer), layer.get("table")))
            if not table:
                errors.append({"path": f"{path}.table", "message": "Table is not selectable through the configured read-only connection."})
                continue
            columns = {column["name"]: column for column in table["columns"]}
            geom = columns.get(layer.get("geom"))
            if not geom or not geom["geometryType"]:
                errors.append({"path": f"{path}.geom", "message": "Must select a geometry column from this table."})
            elif str(geom["srid"]) != str(layer.get("srid")):
                errors.append({"path": f"{path}.srid", "message": f'Must match the geometry column SRID ({geom["srid"]}).'})
            else:
                geometry_type = str(geom["geometryType"])
                if geometry_type.upper() == "GEOMETRY":
                    geometry_type = next(
                        (column["geometryType"] for column in table["columns"]
                         if column["geometryType"]
                         and str(column["geometryType"]).upper() != "GEOMETRY"),
                        geometry_type,
                    )
                geometry_type = str(geometry_type).upper()
                default_style = (layer.get("style") or {}).get("default") or {}
                if "POINT" in geometry_type and not default_style.get("icon"):
                    errors.append({"path": f"{path}.style.default.icon", "message": "Point layers require an XYZ icon style."})
                if ("LINE" in geometry_type or "POLYGON" in geometry_type) and default_style.get("icon"):
                    errors.append({"path": f"{path}.style.default.icon", "message": "XYZ only applies icon styles to point geometry."})
            if layer.get("qID") not in columns:
                errors.append({"path": f"{path}.qID", "message": "Must select an ID column from this table."})
            hover = (layer.get("style") or {}).get("hover")
            if isinstance(hover, dict) and hover.get("field") not in columns:
                errors.append({"path": f"{path}.style.hover.field", "message": "Must select a column from this table."})
            style = layer.get("style") or {}
            themes = []
            if isinstance(style.get("theme"), dict):
                themes.append((f"{path}.style.theme", style["theme"]))
            if isinstance(style.get("themes"), dict):
                themes.extend(
                    (f"{path}.style.themes.{name}", theme)
                    for name, theme in style["themes"].items()
                    if isinstance(theme, dict)
                )
            for theme_path, theme in themes:
                if theme.get("field") is not None and theme["field"] not in columns:
                    errors.append({
                        "path": f"{theme_path}.field",
                        "message": "Must select a column from this table.",
                    })
                for field_index, field in enumerate(theme.get("fields") or []):
                    if field not in columns:
                        errors.append({
                            "path": f"{theme_path}.fields.{field_index}",
                            "message": "Must select a column from this table.",
                        })
                for category_index, category in enumerate(theme.get("categories") or []):
                    if (
                        isinstance(category, dict)
                        and category.get("field") is not None
                        and category["field"] not in columns
                    ):
                        errors.append({
                            "path": f"{theme_path}.categories.{category_index}.field",
                            "message": "Must select a column from this table.",
                        })
            for index_number, entry in enumerate(layer.get("infoj") or []):
                if not isinstance(entry, dict):
                    continue
                field = entry.get("field")
                if not entry.get("fieldfx") and field not in columns:
                    errors.append({"path": f"{path}.infoj.{index_number}.field", "message": "Must select a column from this table or provide a trusted SQL expression."})
                filter_enabled = entry.get("filter") not in (None, False)
                auto_filtered = (
                    (layer.get("filter") or {}).get("includeAll") is True
                    and entry.get("type") in {"numeric", "integer", "text", "date", "datetime", "boolean"}
                    and field not in set((layer.get("filter") or {}).get("exclude") or [])
                )
                included = field in set((layer.get("filter") or {}).get("include") or [])
                if (filter_enabled or auto_filtered or included) and field not in columns:
                    errors.append({
                        "path": f"{path}.infoj.{index_number}.filter",
                        "message": (
                            "XYZ Filtering panel entries must use a real table "
                            "column. Calculated fieldfx aliases are not safe "
                            "for filter SQL or min/max statistics."
                        ),
                    })
    return errors


def ensure_safe_expression_catalog(cur, expression: str) -> None:
    function_names = sorted(expression_function_names(expression))
    if not function_names:
        return
    cur.execute(
        """
        SELECT namespace.nspname, procedure.proname
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        LEFT JOIN pg_catalog.pg_depend AS dependency
          ON dependency.classid = 'pg_proc'::regclass
         AND dependency.objid = procedure.oid
         AND dependency.deptype = 'e'
        LEFT JOIN pg_catalog.pg_extension AS extension
          ON extension.oid = dependency.refobjid
         AND dependency.refclassid = 'pg_extension'::regclass
        WHERE lower(procedure.proname) = ANY(%s)
          AND namespace.nspname <> 'pg_catalog'
          AND COALESCE(extension.extname, '') NOT IN ('postgis', 'postgis_raster')
        LIMIT 1
        """,
        (function_names,),
    )
    unsafe = cur.fetchone()
    if unsafe:
        raise ValueError(
            f"Allowlisted function name is shadowed by an untrusted database function: "
            f"{unsafe[0]}.{unsafe[1]}."
        )


def validate_renderable(data: dict, tables: list[dict]) -> list[dict[str, str]]:
    """Execute bounded equivalents of XYZ's MVT and infoj database reads."""
    errors = []
    table_index = {(table["dbs"], f'{table["schema"]}.{table["table"]}'): table for table in tables}
    for db_name, database_url in DB_CONNECTIONS.items():
        with psycopg.connect(database_url, connect_timeout=5) as conn:
          with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute("SET LOCAL search_path = pg_catalog, public")
            for locale_path, locale in locale_items(data):
                if not isinstance(locale, dict):
                    continue
                for key, layer in (locale.get("layers") or {}).items():
                    if not is_probeable_database_layer(layer):
                        continue
                    path = f"{locale_path}.layers.{key}"
                    if layer_db(data, layer) != db_name:
                        continue
                    table = table_index.get((db_name, layer.get("table")))
                    if not table:
                        continue
                    relation = psycopg.sql.SQL("{}.{}").format(
                        psycopg.sql.Identifier(table["schema"]), psycopg.sql.Identifier(table["table"]))
                    geom = psycopg.sql.Identifier(layer["geom"])
                    qid = psycopg.sql.Identifier(layer["qID"])
                    try:
                        expressions = [qid, geom]
                        for index_number, entry in enumerate(layer.get("infoj") or []):
                            if not isinstance(entry, dict):
                                continue
                            if entry.get("fieldfx"):
                                try:
                                    ensure_safe_expression_catalog(
                                        cur,
                                        entry["fieldfx"],
                                    )
                                except Exception as exc:
                                    raise ValueError(
                                        f"infoj.{index_number}.fieldfx: {exc}"
                                    ) from exc
                            expressions.append(
                                psycopg.sql.SQL(entry["fieldfx"])
                                if entry.get("fieldfx")
                                else psycopg.sql.Identifier(entry["field"])
                            )
                        cur.execute(psycopg.sql.SQL("""
                          SELECT
                            EXISTS(SELECT 1 FROM {relation} WHERE {qid} IS NULL),
                            EXISTS(
                              SELECT 1 FROM {relation}
                              WHERE {qid} IS NOT NULL
                              GROUP BY {qid} HAVING count(*) > 1 LIMIT 1
                            )
                        """).format(relation=relation, qid=qid))
                        has_null, has_duplicate = cur.fetchone()
                        if has_null:
                            errors.append({"path": f"{path}.qID", "message": "XYZ feature IDs must be non-null; the selected column contains null values."})
                        if has_duplicate:
                            errors.append({"path": f"{path}.qID", "message": "XYZ feature IDs must be unique; the selected column contains duplicates."})
                        cur.execute(psycopg.sql.SQL("SELECT {} FROM {} LIMIT 1").format(
                            psycopg.sql.SQL(", ").join(expressions), relation))
                        cur.fetchone()
                        for index_number, entry in enumerate(layer.get("infoj") or []):
                            if not isinstance(entry, dict) or not entry.get("field"):
                                continue
                            expression = (
                                psycopg.sql.SQL(entry["fieldfx"])
                                if entry.get("fieldfx")
                                else psycopg.sql.Identifier(entry["field"])
                            )
                            cur.execute(psycopg.sql.SQL(
                                "SELECT pg_typeof({expr})::text, to_jsonb({expr}) "
                                "FROM {relation} WHERE {expr} IS NOT NULL LIMIT 1"
                            ).format(expr=expression, relation=relation))
                            sample = cur.fetchone()
                            if sample:
                                message = info_value_error(entry.get("type", "text"), sample[0], sample[1])
                                if message:
                                    locale_pointer = (
                                        "/locale" if locale_path == "locale"
                                        else "/locales/" + locale_path.removeprefix("locales.").replace("~", "~0").replace("/", "~1")
                                    )
                                    errors.append({
                                        "path": f"{path}.infoj.{index_number}.{'fieldfx' if entry.get('fieldfx') else 'field'}",
                                        "pointer": f"{locale_pointer}/layers/{key.replace('~', '~0').replace('/', '~1')}/infoj/{index_number}/{'fieldfx' if entry.get('fieldfx') else 'field'}",
                                        "message": message,
                                        "locale": locale_path.removeprefix("locales."),
                                        "layer": key,
                                        "field": entry.get("field"),
                                        "expectedType": entry.get("type", "text"),
                                        "actualType": sample[0],
                                    })
                        if layer.get("format") == "mvt":
                            if str(layer.get("srid")) != "3857":
                                errors.append({"path": f"{path}.srid", "message": "XYZ's MVT template requires an EPSG:3857 geometry column."})
                                continue
                            cur.execute(psycopg.sql.SQL("""
                              SELECT ST_AsMVT(tile, %s, 4096, 'geom') FROM (
                                SELECT {qid} AS id, ST_AsMVTGeom({geom}, ST_TileEnvelope(0,0,0), 4096,1024,false) AS geom
                                FROM {relation} WHERE {geom} IS NOT NULL LIMIT 1
                              ) tile
                            """).format(qid=qid, geom=geom, relation=relation), (key,))
                            cur.fetchone()
                    except Exception as exc:
                        conn.rollback()
                        cur.execute("SET TRANSACTION READ ONLY")
                        cur.execute("SET statement_timeout = '5000ms'")
                        cur.execute("SET LOCAL search_path = pg_catalog, public")
                        match = re.match(r"infoj\.(\d+)\.fieldfx: (.*)", str(exc))
                        if match:
                            locale_pointer = (
                                "/locale" if locale_path == "locale"
                                else "/locales/" + locale_path.removeprefix("locales.").replace("~", "~0").replace("/", "~1")
                            )
                            errors.append({
                                "path": f"{path}.infoj.{match.group(1)}.fieldfx",
                                "pointer": f"{locale_pointer}/layers/{key.replace('~', '~0').replace('/', '~1')}/infoj/{match.group(1)}/fieldfx",
                                "message": match.group(2),
                                "locale": locale_path.removeprefix("locales."),
                                "layer": key,
                                "field": (layer.get("infoj") or [])[int(match.group(1))].get("field"),
                            })
                        else:
                            # Database exceptions can include query fragments or
                            # connection details. Keep proposal diagnostics safe
                            # for terminals and automation logs.
                            errors.append({
                                "path": path,
                                "message": "XYZ database render probe failed during the bounded read.",
                            })
    return errors


def test_info_expression(candidate: dict, locale_key: str, layer_key: str, index: int) -> dict:
    structural = validate_workspace(candidate, set(DB_CONNECTIONS))
    expression_errors = [
        error for error in structural
        if error["path"].endswith(f".infoj.{index}.fieldfx")
    ]
    if expression_errors:
        raise ValueError(expression_errors[0]["message"])
    locale = effective_locales(candidate).get(locale_key)
    layer = (locale.get("layers") or {}).get(layer_key) if isinstance(locale, dict) else None
    entries = layer.get("infoj") if isinstance(layer, dict) else None
    if not isinstance(entries, list) or not 0 <= index < len(entries):
        raise ValueError("The selected feature-information entry no longer exists.")
    entry = entries[index]
    expression = entry.get("fieldfx") if isinstance(entry, dict) else None
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("Enter a SQL expression before testing it.")
    db_name = layer_db(candidate, layer)
    database_url = DB_CONNECTIONS.get(db_name)
    if not database_url:
        raise ValueError(f"No DBS_{db_name} connection is configured.")
    relation_name = layer.get("table")
    if not isinstance(relation_name, str) or relation_name.count(".") > 1:
        raise ValueError("Select a valid database table before testing the expression.")
    relation_parts = relation_name.split(".")
    if len(relation_parts) == 1:
        relation_parts.insert(0, "public")
    relation = psycopg.sql.SQL("{}.{}").format(
        psycopg.sql.Identifier(relation_parts[0]),
        psycopg.sql.Identifier(relation_parts[1]),
    )
    sql_expression = psycopg.sql.SQL(expression)
    with psycopg.connect(database_url, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute("SET LOCAL search_path = pg_catalog, public")
            ensure_safe_expression_catalog(cur, expression)
            cur.execute(psycopg.sql.SQL(
                "SELECT pg_typeof({expr})::text, to_jsonb({expr}) "
                "FROM {relation} WHERE {expr} IS NOT NULL LIMIT 1"
            ).format(expr=sql_expression, relation=relation))
            sample = cur.fetchone()
    if not sample:
        return {
            "valid": True,
            "postgresType": "unknown",
            "sample": None,
            "message": "Expression is valid but returned no non-null sample value.",
        }
    message = info_value_error(entry.get("type", "text"), sample[0], sample[1])
    if message:
        raise ValueError(message)
    return {
        "valid": True,
        "postgresType": sample[0],
        "sample": sample[1],
        "message": "Expression is compatible with the selected information type.",
    }


def validate_candidate(
    candidate,
    original: dict | None = None,
) -> list[dict[str, str]]:
    errors = validate_workspace(candidate, set(DB_CONNECTIONS))
    semantic_errors, _ = semantic_publication_diagnostics(candidate, original)
    errors.extend(semantic_errors)
    icon_urls = {entry["url"] for entry in discover_icons()}
    has_probeable_layers = False
    if not errors:
        for locale_name, locale in locale_items(candidate):
            if not isinstance(locale, dict):
                continue
            for layer_name, layer in (locale.get("layers") or {}).items():
                if not isinstance(layer, dict):
                    continue
                has_probeable_layers = (
                    has_probeable_layers
                    or is_probeable_database_layer(layer)
                )
                for state in ("default", "highlight"):
                    icon = ((layer.get("style") or {}).get(state) or {}).get("icon")
                    icons = icon if isinstance(icon, list) else [icon]
                    for item in icons:
                        url = item.get("url") if isinstance(item, dict) else None
                        if isinstance(url, str) and url.startswith(f"{SVG_URL_PREFIX}/") and url not in icon_urls:
                            errors.append({
                                "path": f"{locale_name}.layers.{layer_name}.style.{state}.icon.url",
                                "message": "Select an SVG currently available in instance/public/svg.",
                            })
    tables = discover() if not errors and has_probeable_layers else []
    if not errors and has_probeable_layers:
        errors.extend(validate_catalog(candidate, tables))
    if not errors and has_probeable_layers:
        errors.extend(validate_renderable(candidate, tables))
    return errors


def annotated(errors):
    output = []
    for error in errors:
        path = error.get("path", "")
        message = error.get("message", "")
        if error.get("code") == "semantic.derived_not_ready":
            rule, phase = "semantic.derived_ready", "semantic"
        elif error.get("code") == "workspace.layer_key_noncanonical":
            rule, phase = "workspace.layer_key", "schema"
        elif "fieldfx" in path:
            rule, phase = "sql.scalar_read_only", "security"
        elif ".plugins" in path or "Plugin" in message or "plugin" in message:
            rule, phase = "plugin.catalogue", "plugin"
        elif path.endswith(".qID") and ("null" in message or "unique" in message or "duplicate" in message):
            rule, phase = "workspace.feature_id", "data"
        elif "render probe" in message:
            rule, phase = "workspace.render", "render"
        elif any(token in message for token in ("select", "column", "table", "geometry", "SRID")):
            rule, phase = "workspace.catalog", "catalog"
        else:
            rule, phase = "workspace.structure", "schema"
        pointer = error.get("pointer") or "/" + "/".join(
            part.replace("~", "~0").replace("/", "~1")
            for part in path.split(".")
        ) if path else ""
        diagnostic = {
            **error,
            "pointer": pointer,
            "ruleId": rule,
            "phase": phase,
            "severity": error.get("severity", "error"),
        }
        type_match = re.search(
            r"XYZ ([A-Za-z]+) entries require (?:a )?([^;]+); PostgreSQL returned ([^.]+)\.?$",
            message,
        )
        if type_match and "expectedType" not in diagnostic:
            diagnostic["expectedType"] = type_match.group(2).strip()
        if type_match and "actualType" not in diagnostic:
            diagnostic["actualType"] = type_match.group(3).strip()
        output.append(diagnostic)
    return output


_VISUAL_BACKGROUND_OPERATION = object()


def update_visual_operation_progress(
    operation_id: str,
    stage: str,
    diagnostics: dict | None = None,
) -> None:
    """Best-effort heartbeat for a durable visual operation."""
    try:
        CONTROL.update_operation_progress(
            operation_id,
            stage=stage,
            diagnostics=diagnostics,
        )
    except (FileNotFoundError, OSError, ValueError):
        # Progress is advisory. Terminal persistence below remains authoritative.
        return


def finish_visual_operation(
    operation_id: str,
    *,
    status: str,
    result: dict | None = None,
    error: dict | None = None,
) -> dict:
    """Persist a terminal visual result, retrying transient atomic-write errors."""
    update_visual_operation_progress(operation_id, "result-persistence")
    last_error: OSError | None = None
    for attempt in range(3):
        try:
            return CONTROL.finish_operation(
                operation_id,
                status=status,
                result=result,
                error=error,
            )
        except OSError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    assert last_error is not None
    raise last_error


def visual_planning_failure_response(
    operation: dict,
    response: dict,
) -> dict:
    """Persist a known pre-browser rejection and return its durable envelope."""
    result = dict(response)
    error = {
        "code": response.get("code", "visual.planning_failed"),
        "message": response.get("error", "Visual planning failed."),
        "failedStage": "planning",
    }
    diagnostics = {
        key: response[key]
        for key in (
            "planningStage",
            "queryPurpose",
            "reason",
            "timeoutMilliseconds",
        )
        if response.get(key) is not None
    }
    if diagnostics:
        error["diagnostics"] = diagnostics
    terminal = finish_visual_operation(
        operation["id"],
        status="failed",
        result=result,
        error=error,
    )
    return {**result, "operation": terminal}


def watch_visual_background(
    operation_id: str,
    worker: threading.Thread,
    timeout_seconds: float,
) -> None:
    """Force a durable terminal state if a visual worker misses its deadline."""
    worker.join(timeout_seconds)
    timed_out = worker.is_alive()
    try:
        operation = CONTROL.read_operation(operation_id)
    except (FileNotFoundError, OSError, ValueError):
        return
    if operation.get("status") != "running":
        return
    stage = operation.get("stage") or "background-execution"
    diagnostics = operation.get("diagnostics")
    terminal_error = {
        "code": (
            "visual.operation_timeout"
            if timed_out
            else "visual.result_persistence_failed"
        ),
        "message": (
            (
                "Visual validation exceeded its end-to-end deadline while "
                f"running stage '{stage}'."
            )
            if timed_out
            else (
                "The visual worker exited without persisting a terminal "
                "result."
            )
        ),
        "failedStage": stage,
        **({"timeoutSeconds": timeout_seconds} if timed_out else {}),
        **({"diagnostics": diagnostics} if diagnostics is not None else {}),
    }
    try:
        finish_visual_operation(
            operation_id,
            status="failed",
            result={
                "operationId": operation_id,
                "timedOut": timed_out,
                "failedStage": stage,
                **(
                    {"diagnostics": diagnostics}
                    if diagnostics is not None
                    else {}
                ),
            },
            error=terminal_error,
        )
    except OSError:
        # A permanently unavailable operation store cannot serve status either;
        # a transient failure was already retried by finish_visual_operation.
        return


def run_visual_background(
    request_path: str,
    payload: dict,
    actor: str,
    remote: str,
    authentication: dict,
    operation_id: str,
) -> None:
    """Replay one visual request after returning its durable operation ID."""
    update_visual_operation_progress(operation_id, "request-dispatch")
    responses: list[tuple[int, dict]] = []
    handler = object.__new__(Handler)
    handler.path = request_path
    handler._host_allowed = lambda: True
    handler._authorized = lambda state_change=False: actor
    handler._authentication = authentication
    handler._payload = lambda: {
        **payload,
        _VISUAL_BACKGROUND_OPERATION: CONTROL.read_operation(operation_id),
    }
    handler._remote = lambda: remote
    handler._json = lambda status, body, headers=None: responses.append(
        (status, body)
    )
    try:
        handler.do_POST()
    except Exception as exc:
        responses.append((HTTPStatus.INTERNAL_SERVER_ERROR, {
            "error": f"Visual background execution was interrupted: {exc}",
            "code": "visual.background_interrupted",
        }))

    operation = CONTROL.read_operation(operation_id)
    if operation.get("status") != "running":
        return
    status, response = responses[-1] if responses else (
        HTTPStatus.INTERNAL_SERVER_ERROR,
        {
            "error": "Visual background execution recorded no response.",
            "code": "visual.background_interrupted",
        },
    )
    result = {
        key: value for key, value in response.items() if key != "operation"
    }
    finish_visual_operation(
        operation_id,
        status=(
            "failed"
            if HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
            else "indeterminate"
        ),
        result=result,
        error={
            "code": response.get("code", "visual.background_interrupted"),
            "message": response.get(
                "error", "Visual background execution did not complete."
            ),
            "httpStatus": int(status),
        },
    )


def start_visual_background(
    request_path: str,
    payload: dict,
    actor: str,
    remote: str,
    authentication: dict,
    kind: str,
    target: dict,
) -> dict:
    operation = CONTROL.create_operation(kind, actor, target)
    try:
        operation = CONTROL.update_operation_progress(
            operation["id"], stage="accepted"
        )
        worker = threading.Thread(
            target=run_visual_background,
            args=(
                request_path,
                dict(payload),
                actor,
                remote,
                dict(authentication),
                operation["id"],
            ),
            name=f"visual-{operation['id'][:8]}",
            daemon=True,
        )
        worker.start()
        threading.Thread(
            target=watch_visual_background,
            args=(
                operation["id"],
                worker,
                VISUAL_BACKGROUND_TIMEOUT_SECONDS,
            ),
            name=f"visual-watchdog-{operation['id'][:8]}",
            daemon=True,
        ).start()
    except Exception:
        finish_visual_operation(
            operation["id"],
            status="failed",
            error={
                "code": "visual.background_start_failed",
                "message": "The visual background worker could not be started.",
            },
        )
        raise
    return operation


class Handler(SimpleHTTPRequestHandler):
    server_version = "MAPPConfig/1.0"

    def translate_path(self, path):
        candidate = safe_static_path(STATIC_ROOT, path)
        return str(candidate or STATIC_ROOT / ".not-found")

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}")

    def _json(self, status, payload, *, headers=None):
        payload = dict(payload)
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        meta["requestId"] = getattr(self, "_request_id", secrets.token_hex(16))
        operation = payload.get("operation")
        if isinstance(operation, dict) and isinstance(operation.get("id"), str):
            meta["operationId"] = operation["id"]
        payload["meta"] = meta

        def json_default(value):
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            raise TypeError(
                f"Object of type {type(value).__name__} is not JSON serializable"
            )

        body = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=json_default,
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Request-ID", meta["requestId"])
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _remote(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

    def _cookies(self):
        output = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                output[key] = value
        return output

    def _actor(self, *, state_change=False):
        self._authentication = None
        authorization_values = self.headers.get_all("Authorization", [])
        if len(authorization_values) > 1:
            return None
        authorization = (
            authorization_values[0]
            if authorization_values
            else ""
        )
        if authorization.startswith("Bearer "):
            token = CONTROL.authenticate_token(authorization[7:], self._remote())
            if not token:
                return None
            actor = f"token:{token['id']}"
            self._authentication = {
                "actor": actor,
                "tokenId": token["id"],
                "scopes": token.get("scopes") or [],
                "expires": token.get("expires"),
            }
            return actor
        cookies = self._cookies()
        if CONTROL.session(
            cookies.get("mapp_session"),
            self.headers.get("X-CSRF-Token"),
            require_csrf=state_change,
        ):
            self._authentication = {"actor": "admin", "scopes": ["admin"]}
            return "admin"
        return None

    def _authorized(self, *, state_change=False, required_scope: str | None = None):
        actor = self._actor(state_change=state_change)
        if not actor:
            self._json(HTTPStatus.UNAUTHORIZED, {
                "error": "Authentication required.",
                "code": "auth.authentication_required",
            })
            return None
        required_scope = required_scope or getattr(self, "_authorization_scope", None)
        scopes = set((self._authentication or {}).get("scopes") or [])
        if (
            required_scope
            and actor != "admin"
            and "full" not in scopes
            and required_scope not in scopes
        ):
            self._json(HTTPStatus.FORBIDDEN, {
                "error": "The credential does not grant the required scope.",
                "code": "auth.scope_required",
                "requiredScope": required_scope,
                "grantedScopes": sorted(scopes),
            })
            return None
        return actor

    def _scope_granted(self, actor: str, required_scope: str) -> bool:
        scopes = set((self._authentication or {}).get("scopes") or [])
        if actor == "admin" or "full" in scopes or required_scope in scopes:
            return True
        self._json(HTTPStatus.FORBIDDEN, {
            "error": "The credential does not grant the required scope.",
            "code": "auth.scope_required",
            "requiredScope": required_scope,
            "grantedScopes": sorted(scopes),
        })
        return False

    @staticmethod
    def _required_scope(path: str, method: str) -> str | None:
        if method == "GET" and path in {
            "/api/capabilities",
            "/api/contract",
            "/api/connect",
            "/api/auth/me",
        }:
            # Contract discovery and a credential's own identity do not expose
            # workspace state. Any authenticated narrow-scope token may read
            # them before invoking its domain-specific API.
            return None
        if path.startswith("/api/semantic/"):
            if method == "GET":
                return "semantic:inspect"
            if path == "/api/semantic/source/sync":
                return "semantic:source"
            if path == "/api/semantic/generate":
                return "semantic:generate"
            if re.fullmatch(
                r"/api/semantic/derived-profiles/"
                r"[a-z][a-z0-9_]{0,62}/repair",
                path,
            ):
                return "semantic:admin"
            semantic_proposal_action = re.fullmatch(
                r"/api/semantic/proposals/[A-Za-z0-9._-]+/(apply|decline)",
                path,
            )
            if (
                semantic_proposal_action
                and semantic_proposal_action.group(1) == "apply"
            ):
                return "semantic:apply"
            if (
                path in {
                    "/api/semantic/proposals",
                    "/api/semantic/proposals/check",
                }
                or (
                    semantic_proposal_action
                    and semantic_proposal_action.group(1) == "decline"
                )
            ):
                return "semantic:propose"
            return "semantic:admin"
        if method == "GET":
            if re.fullmatch(r"/api/layers/[^/]+/(values|statistics)", path):
                return "derive"
            if path.startswith("/api/operations/"):
                return None
            if path.startswith("/api/artifacts/"):
                return "visual"
            return "inspect"
        if re.fullmatch(
            r"/api/operations/[0-9a-f]{32}/cancel",
            path,
        ):
            return "derive"
        if path in {"/api/visual-plan", "/api/visual-test"} or re.fullmatch(
            r"/api/proposals/[^/]+/(visual-plan|visual-test|screenshot)", path
        ):
            return "visual"
        proposal_action = re.fullmatch(
            r"/api/proposals/[A-Za-z0-9._-]+/(apply|decline)",
            path,
        )
        if proposal_action and proposal_action.group(1) == "apply":
            return "apply"
        if path == "/api/xyz/reload":
            return "reload"
        if path == "/api/derived-layers" or path.startswith("/api/derived-layers/"):
            return "derive" if method != "GET" else "inspect"
        if (
            path in {"/api/proposals", "/api/proposals/check"}
            or proposal_action
            and proposal_action.group(1) == "decline"
        ):
            return "propose"
        return "full"

    def _semantic_scopes(self, actor: str) -> list[str]:
        semantic_scopes = [
            "semantic:inspect",
            "semantic:source",
            "semantic:generate",
            "semantic:data",
            "semantic:propose",
            "semantic:apply",
            "semantic:admin",
        ]
        if actor == "admin":
            return semantic_scopes
        scopes = list((self._authentication or {}).get("scopes") or [])
        return list(dict.fromkeys(
            scopes + (semantic_scopes if "full" in scopes else [])
        ))

    def _semantic_request(
        self,
        actor: str,
        internal_path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
    ) -> dict:
        if not SEMANTIC:
            raise SemanticClientError(
                "Semantic service is not configured.",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                payload={"code": "semantic.not_configured"},
            )
        return SEMANTIC.request(
            internal_path,
            method=method,
            payload=payload,
            actor=actor,
            scopes=self._semantic_scopes(actor),
        )

    def _semantic_error(self, error: SemanticClientError) -> None:
        status = error.status or HTTPStatus.SERVICE_UNAVAILABLE
        if status < 400 or status > 599:
            status = HTTPStatus.BAD_GATEWAY
        if status == HTTPStatus.UNAUTHORIZED:
            self._json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "Semantic service authentication is misconfigured.",
                    "code": "semantic.internal_auth_failed",
                },
            )
            return
        upstream = dict(error.payload)
        nested = upstream.get("error")
        payload = {
            key: value
            for key, value in upstream.items()
            if key != "error"
        }
        if isinstance(nested, dict):
            payload.setdefault("code", nested.get("code"))
            payload.setdefault("details", nested.get("details"))
            payload["error"] = nested.get("message") or str(error)
        else:
            payload["error"] = nested or str(error)
        code = payload.get("code")
        if isinstance(code, str) and code:
            if code == "pagination_invalid":
                payload["code"] = "pagination.invalid"
            elif code == "pagination_required":
                payload["code"] = "pagination.required"
            else:
                payload["code"] = (
                    code if code.startswith("semantic.") else f"semantic.{code}"
                )
        else:
            payload["code"] = (
                "semantic.unavailable"
                if status >= 500
                else "semantic.request_failed"
            )
        if payload.get("details") is None:
            payload.pop("details", None)
        self._json(status, payload)

    def _collection_pagination_error(
        self,
        error: CollectionPaginationError,
    ) -> None:
        self._json(
            error.status,
            {
                "error": str(error),
                "code": error.code,
                "details": error.details,
            },
        )

    def _gemini_error(self, error: GeminiClientError) -> None:
        status = error.status
        if status < 400 or status > 599:
            status = HTTPStatus.BAD_GATEWAY
        self._json(status, {
            "error": str(error),
            "code": error.code,
        })

    def _semantic_source_error(self, error: SemanticSourceError) -> None:
        status = error.status
        if status < 400 or status > 599:
            status = HTTPStatus.BAD_GATEWAY
        self._json(status, {
            "error": str(error),
            "code": error.code,
        })

    def _payload(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > MAX_BODY:
            raise ValueError("Request body must be between 1 byte and 5 MiB.")
        payload = strict_json_loads(self.rfile.read(size))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _host_allowed(self):
        host = normalized_host(self.headers.get("Host", ""))
        return not ALLOWED_HOSTS or host in ALLOWED_HOSTS

    def do_OPTIONS(self):
        self._request_id = secrets.token_hex(16)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        self._request_id = secrets.token_hex(16)
        path = urlparse(self.path).path
        if not self._host_allowed():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Unrecognized Host header."})
            return
        if path == "/api/public/identity":
            self._json(HTTPStatus.OK, {
                "instanceId": CONTROL.instance_id(),
                "authentication": "bearer-or-session",
                "contractVersion": CONTRACT_VERSION,
                "xyzVersion": os.environ.get("XYZ_VERSION", "v4.23.4"),
            })
            return
        if path.startswith("/api/") and path not in {
            "/api/auth/login",
            "/api/auth/device",
            "/api/auth/device/token",
        }:
            self._authorization_scope = self._required_scope(path, "GET")
            actor = self._authorized()
            if not actor:
                return
        derived_semantic_path = re.fullmatch(
            r"/api/semantic/derived-profiles(?:/([a-z][a-z0-9_]{0,62}))?",
            path,
        )
        if derived_semantic_path:
            try:
                query = parse_qs(
                    urlparse(self.path).query,
                    keep_blank_values=True,
                )
                authentication = self._authentication or {}
                granted = set(authentication.get("scopes") or [])
                include_delivery_diagnostics = (
                    actor == "admin"
                    or "full" in granted
                    or "semantic:admin" in granted
                )
                revision = current_semantic_revision(actor)
                name = derived_semantic_path.group(1)
                if name:
                    if query:
                        raise ValueError(
                            "Derived-profile show does not accept query parameters."
                        )
                    delivery_blockers = []
                    if include_delivery_diagnostics and DERIVED:
                        delivery_blockers, _, _ = (
                            semantic_delivery_blocker_page(
                                [name],
                                include_unmatched=False,
                            )
                        )
                    profile = derived_semantic_profiles(
                        name=name,
                        include_delivery_diagnostics=(
                            include_delivery_diagnostics
                        ),
                        delivery_blockers=delivery_blockers,
                    )[0]
                    self._json(HTTPStatus.OK, {
                        "catalogRevision": revision,
                        "derivedProfile": profile,
                    })
                else:
                    unmatched_blockers = []
                    delivery_blockers_more = False
                    if query:
                        limit, cursor = pagination_parameters(query)
                        scope = json.dumps(
                            {
                                "collection": "semantic-derived-profiles-v1",
                                "catalogRevision": revision,
                                "diagnostics": include_delivery_diagnostics,
                                "instanceId": CONTROL.instance_id(),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        after_name = decode_position_cursor(
                            cursor,
                            scope,
                            CONTROL.pagination_key(),
                        )
                        if after_name is not None and (
                            not isinstance(after_name, str)
                            or re.fullmatch(
                                r"[a-z][a-z0-9_]{0,62}", after_name
                            )
                            is None
                        ):
                            raise ValueError("cursor is invalid or expired.")
                        profiles = derived_semantic_profiles(
                            after_name=after_name,
                            fetch_limit=limit + 1,
                        )
                        delivery_blockers = []
                        if include_delivery_diagnostics and DERIVED:
                            (
                                delivery_blockers,
                                unmatched_blockers,
                                delivery_blockers_more,
                            ) = semantic_delivery_blocker_page(
                                [
                                    profile["name"]
                                    for profile in profiles[:limit]
                                ],
                                include_unmatched=cursor is None,
                            )
                        if include_delivery_diagnostics:
                            add_semantic_delivery_diagnostics(
                                profiles,
                                delivery_blockers,
                            )
                        profiles, pagination = paginate_keyset_page(
                            profiles,
                            limit=limit,
                            scope=scope,
                            key=CONTROL.pagination_key(),
                            position=lambda item: item["name"],
                        )
                        payload = {
                            "derivedProfiles": profiles,
                            "pagination": pagination,
                        }
                    else:
                        profiles = derived_semantic_profiles(
                            fetch_limit=MAX_PAGE_LIMIT + 1,
                        )
                        delivery_blockers = []
                        if include_delivery_diagnostics:
                            (
                                delivery_blockers,
                                unmatched_blockers,
                                delivery_blockers_more,
                            ) = semantic_delivery_blocker_page(
                                [
                                    profile["name"]
                                    for profile in profiles[:MAX_PAGE_LIMIT]
                                ],
                                include_unmatched=True,
                            )
                            add_semantic_delivery_diagnostics(
                                profiles,
                                delivery_blockers,
                            )
                        profiles = legacy_collection(profiles)
                        payload = {"derivedProfiles": profiles}
                    payload["catalogRevision"] = revision
                    if include_delivery_diagnostics:
                        payload["deliveryBlockers"] = legacy_collection(
                            unmatched_semantic_delivery_blockers(
                                profiles, unmatched_blockers,
                            )
                        )
                        payload["deliveryBlockersMore"] = (
                            delivery_blockers_more
                        )
                    enforce_collection_payload(
                        payload,
                        paginated=bool(query),
                    )
                    self._json(HTTPStatus.OK, payload)
                schedule_semantic_outbox()
            except CollectionPaginationError as exc:
                self._collection_pagination_error(exc)
            except ValueError as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": str(exc), "code": "pagination.invalid"},
                )
            except FileNotFoundError as exc:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": str(exc), "code": "semantic.derived_not_found"},
                )
            except SemanticClientError as exc:
                self._semantic_error(exc)
            except (DerivedLayerError, psycopg.Error) as exc:
                self._json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": str(exc), "code": "semantic.derived_unavailable"},
                )
            return
        if path == "/api/semantic/source/relations":
            if not self._scope_granted(actor, "semantic:source"):
                return
            try:
                query = parse_qs(
                    urlparse(self.path).query,
                    keep_blank_values=True,
                )
                if query:
                    limit, cursor = pagination_parameters(query)
                    key = CONTROL.pagination_key()
                    scope = json.dumps(
                        {
                            "collection": "semantic-source-relations-v1",
                            "instanceId": CONTROL.instance_id(),
                            "sourceConfiguration": (
                                SEMANTIC_SOURCES.configuration_fingerprint(key)
                            ),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    position = decode_position_cursor(cursor, scope, key)
                    if (
                        position is not None
                        and (
                            not isinstance(position, list)
                            or len(position) != 3
                        )
                    ):
                        raise ValueError("cursor is invalid or expired.")
                    if position is None:
                        after = None
                    else:
                        after = tuple(position)
                    relations = SEMANTIC_SOURCES.discover_page(
                        after=after,
                        fetch_limit=limit + 1,
                    )
                    relations, pagination = paginate_keyset_page(
                        relations,
                        limit=limit,
                        scope=scope,
                        key=key,
                        position=lambda item: [
                            item["alias"],
                            item["schema"],
                            item["relation"],
                        ],
                    )
                    payload = {
                        "relations": relations,
                        "pagination": pagination,
                    }
                else:
                    relations = SEMANTIC_SOURCES.discover_page(
                        after=None,
                        fetch_limit=MAX_PAGE_LIMIT + 1,
                    )
                    payload = {"relations": legacy_collection(relations)}
                enforce_collection_payload(
                    payload,
                    paginated=bool(query),
                )
                self._json(HTTPStatus.OK, payload)
            except CollectionPaginationError as exc:
                self._collection_pagination_error(exc)
            except ValueError as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": str(exc), "code": "pagination.invalid"},
                )
            except SemanticSourceError as exc:
                self._semantic_source_error(exc)
            except psycopg.Error:
                self._json(
                    HTTPStatus.BAD_GATEWAY,
                    {
                        "error": "Semantic source discovery is unavailable.",
                        "code": "semantic.source_unavailable",
                    },
                )
            return
        if path.startswith("/api/semantic/"):
            internal = semantic_proxy_path(
                path,
                urlparse(self.path).query,
            )
            if not internal:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                result = self._semantic_request(actor, internal)
                if path == "/api/semantic/status":
                    result = dict(result)
                    semantic_capabilities = result.get("capabilities")
                    semantic_capabilities = (
                        dict(semantic_capabilities)
                        if isinstance(semantic_capabilities, dict)
                        else {}
                    )
                    semantic_capabilities["generation"] = (
                        semantic_generation_capability()
                    )
                    result["capabilities"] = semantic_capabilities
                self._json(
                    HTTPStatus.OK,
                    result,
                )
            except SemanticClientError as exc:
                self._semantic_error(exc)
            return
        if path == "/healthz":
            try:
                if not DB_CONNECTIONS:
                    raise RuntimeError("No DBS_* database connections are configured.")
                read_workspace()
                self._json(HTTPStatus.OK, {"status": "ok"})
            except Exception as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        elif path == "/api/workspace":
            try:
                _, data, current_revision = read_workspace()
                _, semantic_warnings = semantic_publication_diagnostics(
                    data,
                    data,
                )
                self._json(
                    HTTPStatus.OK,
                    {
                        "workspace": data,
                        "revision": current_revision,
                        "semanticWarnings": annotated(semantic_warnings),
                    },
                )
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        elif layer_statistics_path := re.fullmatch(
            r"/api/layers/([^/]+)/statistics", path
        ):
            if not self._scope_granted(actor, "semantic:inspect"):
                return
            try:
                query = parse_qs(
                    urlparse(self.path).query,
                    keep_blank_values=True,
                )
                if (
                    set(query) - {"field", "locale", "bins", "threshold", "break"}
                    or any(
                        len(query.get(key, [])) != 1
                        for key in ("field", "locale", "bins")
                        if key in query
                    )
                ):
                    raise ValueError(
                        "Use one field, optional locale and bins, and repeated "
                        "threshold or break values."
                    )
                field = query.get("field", [None])[0]
                if not isinstance(field, str) or not field:
                    raise ValueError("field is required.")
                try:
                    bins = int(query.get(
                        "bins", [str(LAYER_STATISTICS_DEFAULT_BINS)]
                    )[0])
                except (TypeError, ValueError) as exc:
                    raise ValueError("bins must be an integer.") from exc
                if not 1 <= bins <= LAYER_STATISTICS_MAX_BINS:
                    raise ValueError(
                        f"bins must be between 1 and {LAYER_STATISTICS_MAX_BINS}."
                    )

                def requested_numbers(key: str) -> list[float]:
                    raw = query.get(key, [])
                    if len(raw) > LAYER_STATISTICS_MAX_THRESHOLDS:
                        raise ValueError(
                            f"At most {LAYER_STATISTICS_MAX_THRESHOLDS} {key} "
                            "values may be requested."
                        )
                    try:
                        values = [float(value) for value in raw]
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"{key} values must be numbers.") from exc
                    if not all(math.isfinite(value) for value in values):
                        raise ValueError(f"{key} values must be finite numbers.")
                    return values

                thresholds = requested_numbers("threshold")
                breaks = requested_numbers("break")
                if any(lower >= upper for lower, upper in zip(breaks, breaks[1:])):
                    raise ValueError(
                        "break values must be unique and strictly increasing."
                    )
                _, data, current_revision = read_workspace()
                result = aggregate_layer_statistics(
                    data,
                    query.get("locale", [None])[0],
                    unquote(layer_statistics_path.group(1)),
                    field,
                    bins,
                    thresholds,
                    breaks,
                )
                self._json(
                    HTTPStatus.OK,
                    {"revision": current_revision, **result},
                )
            except FileNotFoundError as exc:
                self._json(HTTPStatus.NOT_FOUND, {
                    "error": f"Unknown layer: {exc}",
                    "code": "layer.not_found",
                })
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {
                    "error": str(exc),
                    "code": "layer.statistics_invalid",
                })
            except psycopg.Error:
                self._json(HTTPStatus.BAD_GATEWAY, {
                    "error": "Layer statistics are unavailable.",
                    "code": "layer.statistics_unavailable",
                })
        elif layer_values_path := re.fullmatch(
            r"/api/layers/([^/]+)/values", path
        ):
            if not self._scope_granted(actor, "semantic:inspect"):
                return
            try:
                query = parse_qs(
                    urlparse(self.path).query,
                    keep_blank_values=True,
                )
                if (
                    set(query) - {"field", "locale", "limit"}
                    or any(len(values) != 1 for values in query.values())
                ):
                    raise ValueError(
                        "Use one field, optional locale, and optional limit."
                    )
                field = query.get("field", [None])[0]
                if not isinstance(field, str) or not field:
                    raise ValueError("field is required.")
                raw_limit = query.get(
                    "limit", [str(LAYER_VALUES_DEFAULT_LIMIT)]
                )[0]
                try:
                    limit = int(raw_limit)
                except (TypeError, ValueError) as exc:
                    raise ValueError("limit must be an integer.") from exc
                if not 1 <= limit <= LAYER_VALUES_MAX_LIMIT:
                    raise ValueError(
                        f"limit must be between 1 and {LAYER_VALUES_MAX_LIMIT}."
                    )
                _, data, current_revision = read_workspace()
                result = aggregate_layer_values(
                    data,
                    query.get("locale", [None])[0],
                    unquote(layer_values_path.group(1)),
                    field,
                    limit,
                )
                self._json(
                    HTTPStatus.OK,
                    {"revision": current_revision, **result},
                )
            except FileNotFoundError as exc:
                self._json(HTTPStatus.NOT_FOUND, {
                    "error": f"Unknown layer: {exc}",
                    "code": "layer.not_found",
                })
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {
                    "error": str(exc),
                    "code": "layer.values_invalid",
                })
            except psycopg.Error:
                self._json(HTTPStatus.BAD_GATEWAY, {
                    "error": "Layer value aggregation is unavailable.",
                    "code": "layer.values_unavailable",
                })
        elif path == "/api/layers":
            requested_locale = parse_qs(
                urlparse(self.path).query,
                keep_blank_values=True,
            ).get("locale", [None])[0]
            try:
                _, data, current_revision = read_workspace()
                locale_key, locale = select_locale(data, requested_locale)
                layers = locale.get("layers")
                self._json(HTTPStatus.OK, {
                    "revision": current_revision,
                    "locale": locale_key,
                    "layers": layers if isinstance(layers, dict) else {},
                })
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {
                    "error": str(exc),
                    "code": "locale.not_found",
                })
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        elif path == "/api/catalog":
            try:
                profiles = derived_semantic_profiles() if DERIVED else []
                self._json(HTTPStatus.OK, {
                    "databases": sorted(DB_CONNECTIONS),
                    "tables": discover_catalog(),
                    "derivedProfiles": profiles,
                })
            except Exception as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Database discovery failed: {exc}"})
        elif path == "/api/derived-layers/capabilities":
            try:
                result = (
                    DERIVED.capabilities()
                    if DERIVED
                    else {
                        "configured": False,
                        "schema": "derived_layers",
                        "kinds": ["view", "materialized"],
                        "spatialScopeTypes": ["workspace-map-extent"],
                        "queryPlanning": (
                            DerivedLayerStore.query_planning_capability()
                        ),
                        "recipes": {
                            "areaWeightedH3": (
                                area_weighted_h3_recipe_capability(
                                    available=False,
                                )
                            ),
                        },
                        "h3Available": False,
                        "h3Readiness": {
                            "method": "postgresql-catalog-and-execution",
                            "ready": False,
                            "code": "derived_layer.h3_not_ready",
                            "stage": "extension-discovery",
                            "reasons": [{
                                "code": "derived_layers_unconfigured",
                                "message": (
                                    "H3 readiness cannot be checked because "
                                    "derived layers are not configured."
                                ),
                                "suggestedAction": (
                                    "Configure the derived-layer database, "
                                    "then retry the readiness check."
                                ),
                            }],
                        },
                    }
                )
                result["backgroundJobs"] = derived_background_capacity()
                self._json(HTTPStatus.OK, result)
            except (DerivedLayerError, psycopg.Error) as exc:
                self._json(
                    HTTPStatus.BAD_GATEWAY,
                    derived_read_error(
                        code="derived_layer.capabilities_unavailable",
                        message="Derived-layer capabilities are unavailable.",
                        suggested_action=(
                            "Check derived-layer database configuration and "
                            "connectivity, then retry."
                        ),
                        exc=exc,
                    ),
                )
        elif path == "/api/derived-layers/map-extent":
            requested_locale = parse_qs(
                urlparse(self.path).query,
                keep_blank_values=True,
            ).get("locale", [None])[0]
            try:
                _, workspace, _ = read_workspace()
                self._json(HTTPStatus.OK, {
                    "spatialScope": workspace_map_extent(
                        workspace,
                        requested_locale,
                    ),
                })
            except ValueError as exc:
                message = str(exc)
                self._json(HTTPStatus.BAD_REQUEST, {
                    "error": message,
                    "userMessage": message,
                    "suggestedAction": (
                        "Select a workspace locale with a valid map view, then "
                        "request the derived-layer extent again."
                    ),
                    "code": "derived_layer.map_extent_unavailable",
                })
            except Exception:
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "error": "The workspace map extent is unavailable.",
                        "userMessage": (
                            "The workspace map extent could not be resolved."
                        ),
                        "suggestedAction": (
                            "Check the workspace map view and retry."
                        ),
                        "code": "derived_layer.map_extent_unavailable",
                    },
                )
        elif path == "/api/derived-layers":
            try:
                if not DERIVED:
                    raise DerivedLayerError(
                        "Derived-layer database management is not configured."
                    )
                self._json(HTTPStatus.OK, {"derivedLayers": DERIVED.list()})
            except (DerivedLayerError, psycopg.Error) as exc:
                self._json(
                    HTTPStatus.BAD_GATEWAY,
                    derived_read_error(
                        code="derived_layer.catalog_unavailable",
                        message="The derived-layer catalog is unavailable.",
                        suggested_action=(
                            "Check derived-layer database connectivity and "
                            "retry the list request."
                        ),
                        exc=exc,
                    ),
                )
        elif path.startswith("/api/derived-layers/"):
            try:
                if not DERIVED:
                    raise DerivedLayerError(
                        "Derived-layer database management is not configured."
                    )
                name = path.removeprefix("/api/derived-layers/")
                if "/" in name:
                    raise FileNotFoundError(name)
                self._json(HTTPStatus.OK, {"derivedLayer": DERIVED.get(name)})
            except FileNotFoundError as exc:
                name = str(exc)
                message = f'The derived layer “{name}” does not exist.'
                self._json(HTTPStatus.NOT_FOUND, {
                    "error": message,
                    "message": message,
                    "userMessage": message,
                    "suggestedAction": (
                        "List derived layers and retry with an existing name."
                    ),
                    "code": "derived_layer.not_found",
                    "name": name,
                })
            except (DerivedLayerError, psycopg.Error) as exc:
                self._json(
                    HTTPStatus.BAD_GATEWAY,
                    derived_read_error(
                        code="derived_layer.read_unavailable",
                        message="The derived layer could not be read.",
                        suggested_action=(
                            "Check derived-layer database connectivity and "
                            "retry."
                        ),
                        exc=exc,
                    ),
                )
        elif path == "/api/icons":
            self._json(HTTPStatus.OK, {"icons": discover_icons()})
        elif path == "/api/contract":
            self._json(HTTPStatus.OK, contract(CONTROL.instance_id()))
        elif path == "/api/connect":
            authentication = self._authentication or {
                "actor": actor,
                "scopes": ["admin"] if actor == "admin" else [],
                "expires": None,
            }
            self._json(HTTPStatus.OK, {
                "authenticated": True,
                "actor": authentication["actor"],
                "scopes": authentication.get("scopes") or [],
                "expires": authentication.get("expires"),
                **(
                    {"tokenId": authentication["tokenId"]}
                    if authentication.get("tokenId")
                    else {}
                ),
            })
        elif path == "/api/capabilities":
            self._json(HTTPStatus.OK, capabilities(CONTROL.instance_id()))
        elif path == "/api/schema":
            query = parse_qs(urlparse(self.path).query)
            try:
                self._json(HTTPStatus.OK, {"schema": contract_schema(query.get("pointer", [None])[0])})
            except (KeyError, IndexError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        elif path == "/api/plugins":
            _, workspace, _ = read_workspace()
            manifest = plugin_manifest()
            manifest["usage"] = plugin_usage(workspace)
            manifest["workspaceErrors"] = validate_workspace_plugins(workspace)
            self._json(HTTPStatus.OK, {"plugins": manifest})
        elif path == "/api/rules":
            category = parse_qs(urlparse(self.path).query).get("category", [None])[0]
            self._json(HTTPStatus.OK, {"rules": [rule for rule in RULES if not category or rule["category"] == category]})
        elif path == "/api/dependencies":
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            alias = query.get("alias", [None])[0]
            schema = query.get("schema", [None])[0]
            relation = query.get("relation", [None])[0]
            if any(value is not None and not isinstance(value, str) for value in (alias, schema, relation)):
                self._json(HTTPStatus.BAD_REQUEST, {
                    "error": "dependency query values must be strings.",
                })
                return
            if relation is not None or alias is not None or schema is not None:
                if alias is None or schema is None or relation is None:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "alias, schema, and relation must be supplied together.",
                            "code": "dependencies.invalid_query",
                        },
                    )
                    return
                if alias not in DB_CONNECTIONS and alias != "derived":
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": (
                                f"Alias {alias!r} is not configured in DBS_ env vars."
                            ),
                            "code": "dependencies.invalid_alias",
                        },
                    )
                    return
            try:
                dependencies = platform_dependencies((read_workspace())[1])
            except (ValueError, RuntimeError, FileNotFoundError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {
                    "error": str(exc),
                    "code": "dependencies.invalid_workspace",
                })
                return
            if relation is not None and schema is not None and alias is not None:
                normalized = _normalize_relation(f"{schema}.{relation}")
                if not normalized:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "relation must be schema.table or table.",
                            "code": "dependencies.invalid_relation",
                        },
                    )
                    return
                matches = [
                    item for item in dependencies
                    if item["alias"] == alias
                    and item["relation"] == f"{normalized[0]}.{normalized[1]}"
                ]
                self._json(HTTPStatus.OK, {
                    "alias": alias,
                    "schema": normalized[0],
                    "relation": normalized[1],
                    "matches": matches,
                    "blocked": bool(matches),
                    "message": (
                        "Delete is blocked by active platform references."
                        if matches else
                        "No active platform references were found for this relation."
                    ),
                })
                return
            self._json(HTTPStatus.OK, {"dependencies": dependencies})
            return
        elif path == "/api/examples":
            self._json(HTTPStatus.OK, examples())
        elif path == "/api/sql/capabilities":
            self._json(HTTPStatus.OK, {
                "mode": "one read-only scalar PostgreSQL expression",
                "supports": ["columns", "casts", "arithmetic", "CASE", "safe scalar functions", "selected PostGIS functions"],
                "prohibits": ["statements", "comments", "subqueries", "DDL", "DML", "session changes", "system/file access", "sleep", "notifications", "database links"],
                "statementTimeoutMs": 5000,
            })
        elif path == "/api/auth/me":
            self._json(HTTPStatus.OK, {
                **(self._authentication or {"actor": actor, "scopes": []}),
                "sessions": CONTROL.sessions() if actor == "admin" else [],
            })
        elif path == "/api/admin/tokens":
            if actor != "admin":
                self._json(HTTPStatus.FORBIDDEN, {"error": "Administrator session required."})
            else:
                self._json(HTTPStatus.OK, {"tokens": CONTROL.list_tokens()})
        elif path == "/api/admin/device-authorizations":
            if actor != "admin":
                self._json(HTTPStatus.FORBIDDEN, {"error": "Administrator session required."})
            else:
                self._json(HTTPStatus.OK, {"authorizations": CONTROL.list_device_authorizations()})
        elif path == "/api/admin/audit":
            if actor != "admin":
                self._json(HTTPStatus.FORBIDDEN, {"error": "Administrator session required."})
            else:
                self._json(HTTPStatus.OK, {"events": CONTROL.audit_tail()})
        elif path == "/api/proposals":
            try:
                query = parse_qs(
                    urlparse(self.path).query,
                    keep_blank_values=True,
                )
                if query:
                    limit, cursor = pagination_parameters(query)
                    scope = json.dumps(
                        {
                            "collection": "workspace-proposals-v1",
                            "instanceId": CONTROL.instance_id(),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    after_id = decode_position_cursor(
                        cursor,
                        scope,
                        CONTROL.pagination_key(),
                    )
                    if after_id is not None and (
                        not isinstance(after_id, str)
                        or re.fullmatch(r"[A-Za-z0-9._-]+", after_id) is None
                    ):
                        raise ValueError("cursor is invalid or expired.")
                    proposals = proposal_list(
                        CONTROL,
                        after_id=after_id,
                        fetch_limit=limit + 1,
                    )
                    proposals, pagination = paginate_keyset_page(
                        proposals,
                        limit=limit,
                        scope=scope,
                        key=CONTROL.pagination_key(),
                        position=lambda item: item["id"],
                    )
                    payload = {
                        "proposals": proposals,
                        "pagination": pagination,
                    }
                else:
                    proposals = proposal_list(
                        CONTROL,
                        fetch_limit=MAX_PAGE_LIMIT + 1,
                    )
                    payload = {"proposals": legacy_collection(proposals)}
                enforce_collection_payload(
                    payload,
                    paginated=bool(query),
                )
                self._json(HTTPStatus.OK, payload)
            except CollectionPaginationError as exc:
                self._collection_pagination_error(exc)
            except ValueError as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": str(exc), "code": "pagination.invalid"},
                )
        elif path.startswith("/api/proposals/"):
            try:
                self._json(HTTPStatus.OK, {"proposal": proposal_read(CONTROL, path.rsplit("/", 1)[1])})
            except (FileNotFoundError, ValueError) as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        elif path == "/api/xyz/status":
            self._json(HTTPStatus.OK, reload_status())
        elif path.startswith("/api/operations/"):
            try:
                operation = CONTROL.read_operation(path.rsplit("/", 1)[1])
                required_scope = {
                    "visual.test": "visual",
                    "proposal.visual-test": "visual",
                    "proposal.screenshot": "visual",
                    "xyz.reload": "reload",
                    "proposal.apply": "apply",
                    "derived-layer.create": "derive",
                    "derived-layer.replace": "derive",
                    "derived-layer.refresh": "derive",
                }.get(operation.get("kind"), "full")
                scopes = set((self._authentication or {}).get("scopes") or [])
                if actor != "admin" and "full" not in scopes and required_scope not in scopes:
                    self._json(HTTPStatus.FORBIDDEN, {
                        "error": "The credential cannot inspect this operation.",
                        "code": "auth.scope_required",
                        "requiredScope": required_scope,
                    })
                    return
                self._json(HTTPStatus.OK, {"operation": operation})
            except FileNotFoundError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc), "code": "operation.not_found"})
        elif path.startswith("/api/artifacts/"):
            relative = path.removeprefix("/api/artifacts/")
            artifact = (CONTROL.root / "artifacts" / relative).resolve()
            root = (CONTROL.root / "artifacts").resolve()
            if root not in artifact.parents or not artifact.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                body = artifact.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png" if artifact.suffix == ".png" else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, no-store")
                self.end_headers()
                self.wfile.write(body)
        elif path.startswith(f"{SVG_URL_PREFIX}/"):
            icon = icon_path(path)
            if not icon:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = icon.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def do_POST(self):
        self._request_id = secrets.token_hex(16)
        request_path = urlparse(self.path).path
        if not self._host_allowed():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Unrecognized Host header."})
            return
        if request_path == "/api/auth/device":
            try:
                payload = self._payload()
                result = CONTROL.start_device_authorization(
                    payload.get("deviceName", ""),
                    payload.get(
                        "scopes",
                        [
                            "inspect", "propose", "visual",
                            "semantic:inspect",
                        ],
                    ),
                    self._remote(),
                )
                result["verificationUri"] = "/"
                self._json(HTTPStatus.CREATED, result)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc), "code": "device.invalid_request"})
            return
        if request_path == "/api/auth/device/token":
            try:
                result = CONTROL.poll_device_authorization(self._payload().get("deviceId", ""))
                status = HTTPStatus.OK if result["status"] == "authorized" else HTTPStatus.ACCEPTED
                self._json(status, result)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc), "code": "device.invalid_request"})
            return
        if request_path == "/api/auth/login":
            try:
                with LOGIN_LOCK:
                    cutoff = time.time() - 300
                    attempts = [item for item in LOGIN_ATTEMPTS.get(self._remote(), []) if item > cutoff]
                    if len(attempts) >= 8:
                        self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many login attempts. Try again later."})
                        return
                    LOGIN_ATTEMPTS[self._remote()] = attempts + [time.time()]
                result = CONTROL.login(self._payload().get("password", ""), self._remote())
                if not result:
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid administrator password."})
                    return
                session, csrf = result
                with LOGIN_LOCK:
                    LOGIN_ATTEMPTS.pop(self._remote(), None)
                secure = "; Secure" if SECURE_COOKIES else ""
                self._json(
                    HTTPStatus.OK,
                    {"authenticated": True, "csrfToken": csrf},
                    headers={
                        "Set-Cookie": (
                            f"mapp_session={session}; Path=/; HttpOnly; "
                            f"SameSite=Strict{secure}; Max-Age=43200"
                        )
                    },
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._authorization_scope = self._required_scope(request_path, "POST")
        actor = self._authorized(state_change=True)
        if not actor:
            return
        operation_cancel_path = re.fullmatch(
            r"/api/operations/([0-9a-f]{32})/cancel",
            request_path,
        )
        if operation_cancel_path:
            operation_id = operation_cancel_path.group(1)
            try:
                payload = self._payload()
                if payload.get("confirmed") is not True:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "Confirm cancellation before stopping this operation.",
                        "code": "operation.confirmation_required",
                    })
                    return
                operation = CONTROL.read_operation(operation_id)
                if operation.get("kind") not in {
                    "derived-layer.create",
                    "derived-layer.replace",
                    "derived-layer.refresh",
                }:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "Only background derived-layer operations can be cancelled.",
                        "code": "operation.not_cancellable",
                        "operation": operation,
                    })
                    return
                if operation.get("status") in {
                    "succeeded", "failed", "cancelled", "indeterminate",
                }:
                    self._json(HTTPStatus.OK, {"operation": operation})
                    return
                if not request_derived_background_cancellation(operation_id):
                    operation = CONTROL.read_operation(operation_id)
                    if operation.get("status") in {
                        "succeeded", "failed", "cancelled", "indeterminate",
                    }:
                        self._json(HTTPStatus.OK, {"operation": operation})
                    else:
                        self._json(HTTPStatus.CONFLICT, {
                            "error": (
                                "The database phase has already finished; "
                                "wait for the terminal operation result."
                            ),
                            "code": "operation.cancel_too_late",
                            "operation": operation,
                        })
                    return
                operation = CONTROL.request_operation_cancellation(operation_id)
                CONTROL.audit(
                    "derived_layer.cancellation_requested",
                    actor=actor,
                    remote=self._remote(),
                    details={
                        "operationId": operation_id,
                        "name": (operation.get("target") or {}).get("name"),
                        "action": (operation.get("target") or {}).get("action"),
                    },
                )
                status = (
                    HTTPStatus.OK
                    if operation.get("status") in {
                        "succeeded", "failed", "cancelled", "indeterminate",
                    }
                    else HTTPStatus.ACCEPTED
                )
                self._json(status, {
                    "operation": operation,
                    "statusUrl": f"/api/operations/{operation_id}",
                })
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {
                    "error": str(exc),
                    "code": "operation.invalid_cancellation",
                })
            except FileNotFoundError as exc:
                self._json(HTTPStatus.NOT_FOUND, {
                    "error": str(exc),
                    "code": "operation.not_found",
                })
            return
        if request_path == "/api/semantic/generate":
            if not self._scope_granted(actor, "semantic:inspect"):
                return
            audit_details = None
            try:
                asset_id, target, context_options = _semantic_generation_request(
                    self._payload()
                )
                audit_details = {
                    "assetId": asset_id,
                    "target": target["kind"],
                    "provider": "gemini",
                    "model": GEMINI_MODEL,
                    "contextOptions": dict(context_options),
                }
                if target["kind"] == "field":
                    audit_details["fieldId"] = target["fieldId"]
                if (
                    any(context_options.values())
                    and not self._scope_granted(actor, "semantic:data")
                ):
                    return
                if GEMINI is None:
                    if GEMINI_CONFIGURATION_ERROR is not None:
                        raise GEMINI_CONFIGURATION_ERROR
                    raise GeminiClientError(
                        "Gemini semantic generation is not configured.",
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                        code="semantic.generation_not_configured",
                    )
                response = self._semantic_request(
                    actor,
                    f"/v1/assets/{quote(asset_id, safe='')}",
                )
                asset = response.get("asset")
                if (
                    not isinstance(asset, dict)
                    or asset.get("id") != asset_id
                    or isinstance(asset.get("version"), bool)
                    or not isinstance(asset.get("version"), int)
                    or asset["version"] < 1
                ):
                    raise GeminiClientError(
                        "Semantic service returned an invalid asset.",
                        status=HTTPStatus.BAD_GATEWAY,
                        code="semantic.generation_context_invalid",
                    )
                if asset.get("status") == "archived":
                    raise GeminiClientError(
                        "Archived semantic assets cannot receive new drafts.",
                        status=HTTPStatus.CONFLICT,
                        code="semantic.asset_archived",
                    )
                context, field_name = semantic_generation_context(
                    asset,
                    target,
                )
                optional_context = semantic_generation_optional_context(
                    asset,
                    target,
                    context_options,
                )
                if optional_context:
                    context["dataContext"] = optional_context
                    sample_context = optional_context.get("sampleRows")
                    statistics_context = optional_context.get("statistics")
                    audit_details["dataContext"] = {
                        **(
                            {
                                "sampleRowsReturned": sample_context.get(
                                    "returnedRows", 0
                                )
                            }
                            if isinstance(sample_context, dict)
                            else {}
                        ),
                        **(
                            {
                                "statisticsScope": statistics_context.get(
                                    "scope"
                                )
                            }
                            if isinstance(statistics_context, dict)
                            else {}
                        ),
                    }
                profile = GEMINI.generate(
                    context,
                    target_kind=target["kind"],
                )
                operations = semantic_generation_operations(
                    target,
                    profile,
                    context.get("currentAnnotation"),
                )
                subject = (
                    f"field {field_name!r}"
                    if target["kind"] == "field"
                    else "table"
                )
                draft = {
                    "assetId": asset_id,
                    "baseVersion": asset["version"],
                    "target": target,
                    "operations": operations,
                    "explanation": (
                        (
                            f"Gemini draft for {subject}, using semantic "
                            "metadata and the explicitly selected bounded "
                            "data context. "
                            if any(context_options.values())
                            else f"Gemini metadata-only draft for {subject}. "
                        )
                        + "Review every generated value before checking or "
                        "creating a semantic proposal."
                    ),
                }
                CONTROL.audit(
                    "semantic.draft.generated",
                    actor=actor,
                    remote=self._remote(),
                    details=audit_details,
                )
                self._json(HTTPStatus.OK, {
                    "draft": draft,
                    "generation": {
                        "provider": "gemini",
                        "model": GEMINI_MODEL,
                        "metadataOnly": not any(context_options.values()),
                        "contextOptions": context_options,
                        "proposalCreated": False,
                    },
                })
            except (ValueError, json.JSONDecodeError) as exc:
                error = GeminiClientError(
                    str(exc),
                    status=HTTPStatus.BAD_REQUEST,
                    code="semantic.generation_invalid_request",
                )
                self._gemini_error(error)
            except SemanticClientError as exc:
                if audit_details is not None:
                    details = {
                        **audit_details,
                        "code": "semantic.asset_lookup_failed",
                    }
                    if (
                        isinstance(exc.status, int)
                        and 400 <= exc.status <= 599
                    ):
                        details["status"] = exc.status
                    CONTROL.audit(
                        "semantic.draft.generation_failed",
                        actor=actor,
                        remote=self._remote(),
                        details=details,
                    )
                self._semantic_error(exc)
            except GeminiClientError as exc:
                if audit_details is not None:
                    CONTROL.audit(
                        "semantic.draft.generation_failed",
                        actor=actor,
                        remote=self._remote(),
                        details={
                            **audit_details,
                            "code": exc.code,
                        },
                    )
                self._gemini_error(exc)
            return
        if request_path == "/api/semantic/source/archive-excluded":
            if not self._scope_granted(actor, "semantic:inspect"):
                return
            try:
                if self._payload() != {"confirmed": True}:
                    raise ValueError(
                        "Archiving excluded semantic sources requires confirmed: true."
                    )
                archived = archive_excluded_semantic_sources(actor)
                CONTROL.audit(
                    "semantic.source.exclusions_archived",
                    actor=actor,
                    remote=self._remote(),
                    details={"assetIds": [item["id"] for item in archived]},
                )
                self._json(HTTPStatus.OK, {"archived": archived})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except SemanticClientError as exc:
                self._semantic_error(exc)
            return
        semantic_archive_path = re.fullmatch(
            r"/api/semantic/catalog/objects/([^/]+)/archive",
            request_path,
        )
        if semantic_archive_path:
            if not self._scope_granted(actor, "semantic:inspect"):
                return
            try:
                if self._payload() != {"confirmed": True}:
                    raise ValueError("Archiving a semantic profile requires confirmed: true.")
                asset_id = semantic_archive_path.group(1)
                response = self._semantic_request(
                    actor,
                    f"/v1/assets/{quote(asset_id, safe='')}",
                )
                asset = response.get("asset")
                generation = asset.get("generation") if isinstance(asset, dict) else None
                if (
                    not isinstance(asset, dict)
                    or asset.get("id") != asset_id
                    or asset.get("status") != "ready"
                    or isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation < 1
                ):
                    raise ValueError("Only a ready semantic profile can be archived.")
                event = {
                    "eventId": str(uuid.uuid4()),
                    "assetId": asset_id,
                    "type": "archive",
                    "generation": generation + 1,
                    "actor": actor,
                }
                event["payloadHash"] = semantic_event_payload_hash(event)
                archived = SEMANTIC.request(
                    "/v1/events", method="POST", payload=event,
                    actor=actor, scopes=["semantic:admin"],
                )
                validate_semantic_event_ack(event, event, archived)
                CONTROL.audit("semantic.asset.archived", actor=actor, remote=self._remote(), details={"assetId": asset_id})
                self._json(HTTPStatus.OK, {"asset": archived["asset"]})
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except SemanticClientError as exc:
                self._semantic_error(exc)
            return
        if request_path == "/api/semantic/source/sync":
            if not self._scope_granted(actor, "semantic:inspect"):
                return
            audit_details = None
            try:
                alias, schema, relation = validate_source_selector(
                    self._payload()
                )
                audit_details = {
                    "alias": alias,
                    "schema": schema,
                    "relation": relation,
                }
                if not SEMANTIC:
                    raise SemanticClientError(
                        "Semantic service is not configured.",
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                        payload={"code": "semantic.not_configured"},
                    )
                with SEMANTIC_SOURCE_LOCK, SEMANTIC_SOURCES.locked_relation(
                    alias,
                    schema,
                    relation,
                ) as source:
                    asset_id = source["assetId"]
                    existing = None
                    try:
                        existing_response = self._semantic_request(
                            actor,
                            f"/v1/assets/{quote(asset_id, safe='')}",
                        )
                        existing = existing_response.get("asset")
                    except SemanticClientError as exc:
                        if exc.status != HTTPStatus.NOT_FOUND:
                            raise
                    if existing is None:
                        operation = "register"
                        generation = 1
                    else:
                        binding = (
                            existing.get("generated", {}).get("binding")
                            if isinstance(existing, dict)
                            and isinstance(existing.get("generated"), dict)
                            else None
                        )
                        expected_binding = {
                            "adapter": "postgresql",
                            "alias": alias,
                            "schema": schema,
                            "relation": relation,
                        }
                        if (
                            not isinstance(existing, dict)
                            or existing.get("id") != asset_id
                            or existing.get("status") != "ready"
                            or binding != expected_binding
                            or isinstance(existing.get("generation"), bool)
                            or not isinstance(existing.get("generation"), int)
                            or existing["generation"] < 1
                        ):
                            raise SemanticSourceError(
                                "The existing semantic source identity is invalid.",
                                status=HTTPStatus.CONFLICT,
                                code="semantic.source_identity_conflict",
                            )
                        operation = "refresh"
                        generation = existing["generation"] + 1
                    generated = source_generated(source)
                    if (
                        existing is not None
                        and existing["generated"].get("definitionDigest")
                        == generated["definitionDigest"]
                    ):
                        operation = "unchanged"
                        asset = existing
                        revision = existing_response.get("catalogRevision")
                        if (
                            isinstance(revision, bool)
                            or not isinstance(revision, int)
                            or revision < 0
                        ):
                            raise SemanticSourceError(
                                "The semantic catalog revision is invalid.",
                                status=HTTPStatus.BAD_GATEWAY,
                                code="semantic.source_invalid_response",
                            )
                    else:
                        event = {
                            "eventId": str(uuid.uuid4()),
                            "assetId": asset_id,
                            "type": operation,
                            "generation": generation,
                            "generated": generated,
                            "visibility": "inspect",
                            "actor": actor,
                        }
                        event["payloadHash"] = semantic_event_payload_hash(event)
                        response = SEMANTIC.request(
                            "/v1/events",
                            method="POST",
                            payload=event,
                            actor=actor,
                            scopes=["semantic:admin"],
                        )
                        revision = validate_semantic_event_ack(
                            event,
                            event,
                            response,
                        )
                        asset = response["asset"]
                source_response = {
                    key: source[key]
                    for key in (
                        "alias",
                        "schema",
                        "relation",
                        "kind",
                        "assetId",
                    )
                }
                CONTROL.audit(
                    "semantic.source.synced",
                    actor=actor,
                    remote=self._remote(),
                    details={
                        **audit_details,
                        "assetId": asset_id,
                        "operation": operation,
                        "generation": asset["generation"],
                        "catalogRevision": revision,
                    },
                )
                self._json(
                    HTTPStatus.CREATED if operation == "register" else HTTPStatus.OK,
                    {
                        "catalogRevision": revision,
                        "operation": operation,
                        "source": source_response,
                        "asset": asset,
                    },
                )
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": str(exc),
                        "code": "semantic.source_invalid_request",
                    },
                )
            except SemanticSourceError as exc:
                if audit_details is not None:
                    CONTROL.audit(
                        "semantic.source.sync_failed",
                        actor=actor,
                        remote=self._remote(),
                        details={
                            **audit_details,
                            "code": exc.code,
                        },
                    )
                self._semantic_source_error(exc)
            except SemanticClientError as exc:
                if audit_details is not None:
                    details = {
                        **audit_details,
                        "code": "semantic.source_delivery_failed",
                    }
                    if (
                        isinstance(exc.status, int)
                        and 400 <= exc.status <= 599
                    ):
                        details["status"] = exc.status
                    CONTROL.audit(
                        "semantic.source.sync_failed",
                        actor=actor,
                        remote=self._remote(),
                        details=details,
                    )
                self._semantic_error(exc)
            except psycopg.Error:
                if audit_details is not None:
                    CONTROL.audit(
                        "semantic.source.sync_failed",
                        actor=actor,
                        remote=self._remote(),
                        details={
                            **audit_details,
                            "code": "semantic.source_unavailable",
                        },
                    )
                self._json(
                    HTTPStatus.BAD_GATEWAY,
                    {
                        "error": "Semantic source synchronization is unavailable.",
                        "code": "semantic.source_unavailable",
                    },
                )
            return
        semantic_repair_path = re.fullmatch(
            r"/api/semantic/derived-profiles/([a-z][a-z0-9_]{0,62})/repair",
            request_path,
        )
        if semantic_repair_path:
            try:
                payload = self._payload()
                if set(payload) != {"confirmed"}:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": (
                                "Semantic repair accepts only confirmed."
                            ),
                            "code": "semantic.invalid_request",
                        },
                    )
                    return
                if payload["confirmed"] is not True:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": (
                            "Explicit confirmation is required before a "
                            "semantic delivery retry is queued."
                        ),
                        "code": "semantic.confirmation_required",
                    })
                    return
                if not DERIVED:
                    raise DerivedLayerError(
                        "Derived-layer database management is not configured."
                    )
                catalog_revision = current_semantic_revision(actor)
                profile = DERIVED.repair_semantic_profile(
                    semantic_repair_path.group(1)
                )
                schedule_semantic_outbox()
                CONTROL.audit(
                    "semantic.derived_repair_requested",
                    actor=actor,
                    remote=self._remote(),
                    details={
                        "name": profile["name"],
                        "assetId": profile["assetId"],
                        "generation": profile["generation"],
                    },
                )
                self._json(HTTPStatus.ACCEPTED, {
                    "catalogRevision": catalog_revision,
                    "derivedProfile": profile,
                })
            except DerivedLayerMaintenanceError as exc:
                self._json(HTTPStatus.CONFLICT, {
                    "error": str(exc),
                    "code": "derived_layer.maintenance",
                    "operation": "reset-data",
                    "blocked": True,
                })
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except DerivedLayerError as exc:
                self._json(
                    HTTPStatus.CONFLICT,
                    {"error": str(exc), "code": "semantic.repair_not_available"},
                )
            except SemanticClientError as exc:
                self._semantic_error(exc)
            except psycopg.Error as exc:
                self._json(
                    HTTPStatus.BAD_GATEWAY,
                    {"error": str(exc), "code": "semantic.derived_unavailable"},
                )
            return
        if (
            request_path.startswith("/api/semantic/")
            and not request_path.endswith("/repair")
        ):
            internal = semantic_proxy_path(
                request_path,
                urlparse(self.path).query,
            )
            if not internal:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                payload = self._payload()
                if internal.endswith(("/apply", "/decline")):
                    if payload.get("confirmed") is not True:
                        self._json(HTTPStatus.CONFLICT, {
                            "error": (
                                "Explicit confirmation is required before a "
                                "semantic proposal decision is recorded."
                            ),
                            "code": "semantic.confirmation_required",
                        })
                        return
                    payload.pop("confirmed", None)
                result = self._semantic_request(
                    actor,
                    internal,
                    method="POST",
                    payload=payload,
                )
                event = None
                if request_path == "/api/semantic/proposals":
                    event = "semantic.proposal.created"
                elif request_path.endswith("/apply"):
                    event = "semantic.proposal.applied"
                elif request_path.endswith("/decline"):
                    event = "semantic.proposal.declined"
                if event:
                    proposal = result.get("proposal")
                    proposal = proposal if isinstance(proposal, dict) else {}
                    details = {}
                    for input_key, output_key in (
                        ("id", "id"),
                        ("assetId", "assetId"),
                        ("state", "status"),
                    ):
                        value = proposal.get(input_key)
                        if isinstance(value, str):
                            details[output_key] = value
                    CONTROL.audit(
                        event,
                        actor=actor,
                        remote=self._remote(),
                        details=details,
                    )
                self._json(HTTPStatus.OK, result)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": str(exc), "code": "semantic.invalid_request"},
                )
            except SemanticClientError as exc:
                self._semantic_error(exc)
            return
        allowed = {
            "/api/workspace", "/api/validate", "/api/expression-test", "/api/mutate",
            "/api/proposals", "/api/proposals/check", "/api/xyz/reload", "/api/visual-plan",
            "/api/visual-test",
            "/api/admin/tokens", "/api/admin/password", "/api/auth/logout",
            "/api/admin/device-authorizations/approve",
            "/api/sql/test",
            "/api/derived-layers",
            "/api/derived-layers/recipes/area-weighted-h3/plan",
        }
        derived_action_path = re.fullmatch(
            r"/api/derived-layers/([a-z][a-z0-9_]{0,62})/(refresh|replace|drop)",
            request_path,
        )
        proposal_action_path = re.fullmatch(
            r"/api/proposals/([A-Za-z0-9._-]+)/(apply|decline)",
            request_path,
        )
        token_revoke_path = re.fullmatch(
            r"/api/admin/tokens/([A-Za-z0-9._-]+)/revoke",
            request_path,
        )
        proposal_visual_path = re.fullmatch(
            r"/api/proposals/([A-Za-z0-9._-]+)/(visual-plan|visual-test|screenshot)",
            request_path,
        )
        if (
            request_path not in allowed
            and not proposal_action_path
            and not token_revoke_path
            and not proposal_visual_path
            and not derived_action_path
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        derived_failure_phase: str | None = None
        visual_operation: dict | None = None
        visual_planning_active = False
        try:
            payload = self._payload()
            background_operation = payload.pop(
                _VISUAL_BACKGROUND_OPERATION, None
            )
            if isinstance(background_operation, dict):
                visual_operation = background_operation
            visual_background_kind = None
            visual_background_target = None
            if request_path == "/api/visual-test":
                visual_background_kind = "visual.test"
                visual_background_target = {
                    "source": "live",
                    "layer": payload.get("layer"),
                }
            elif (
                proposal_visual_path
                and proposal_visual_path.group(2) in {"visual-test", "screenshot"}
            ):
                proposal_id, visual_action = proposal_visual_path.groups()
                visual_background_kind = (
                    "proposal.screenshot"
                    if visual_action == "screenshot"
                    else "proposal.visual-test"
                )
                visual_background_target = {
                    "source": "candidate",
                    "proposalId": proposal_id,
                    "layer": payload.get("layer"),
                }
            if visual_background_kind is not None:
                background = payload.pop("background", False)
                if not isinstance(background, bool):
                    raise ValueError("Visual background must be true or false.")
                if background and background_operation is None:
                    layer_key = payload.get("layer")
                    if not isinstance(layer_key, str) or not layer_key.strip():
                        raise ValueError("Visual requests require a layer key.")
                    if proposal_visual_path:
                        proposal = preview_proposal(
                            proposal_visual_path.group(1)
                        )
                        visual_background_target["candidateHash"] = proposal[
                            "candidateHash"
                        ]
                    operation = start_visual_background(
                        request_path,
                        payload,
                        actor,
                        self._remote(),
                        getattr(self, "_authentication", {}) or {},
                        visual_background_kind,
                        visual_background_target or {},
                    )
                    self._json(HTTPStatus.ACCEPTED, {
                        "operation": operation,
                        "statusUrl": f"/api/operations/{operation['id']}",
                    })
                    return
            if (
                request_path
                == "/api/derived-layers/recipes/area-weighted-h3/plan"
            ):
                if not self._scope_granted(actor, "semantic:inspect"):
                    return
                derived_failure_phase = "preflight"
                if not DERIVED:
                    raise DerivedLayerError(
                        "Derived-layer database management is not configured."
                    )
                asset_id = recipe_source_asset_id(payload)
                asset_response = self._semantic_request(
                    actor,
                    f"/v1/assets/{quote(asset_id, safe='')}",
                )
                source_asset = asset_response.get("asset")
                if not isinstance(source_asset, dict):
                    raise DerivedLayerError(
                        "Semantic asset lookup returned an invalid source profile."
                    )
                recipe_plan = plan_area_weighted_h3_recipe(
                    payload,
                    source_asset,
                )
                create_request = recipe_plan["createRequest"]
                resolved_create_request = resolve_derived_spatial_scope(
                    recipe_plan["createRequest"]
                )
                probes = DERIVED.preflight_definition(resolved_create_request)
                recipe_plan = {
                    **recipe_plan,
                    "createRequest": create_request,
                    "resolvedSpatialScope": resolved_create_request[
                        "spatialScope"
                    ],
                    **probes,
                }
                self._json(HTTPStatus.OK, {
                    "recipePlan": recipe_plan,
                    "mutationApplied": False,
                })
                return
            if request_path == "/api/derived-layers":
                derived_failure_phase = "preflight"
                if not DERIVED:
                    raise DerivedLayerError(
                        "Derived-layer database management is not configured."
                    )
                background = payload.pop("background", False)
                payload = resolve_derived_spatial_scope(payload)
                require_semantic_derived_sources(
                    payload,
                    self._semantic_request(actor, "/v1/catalog"),
                )
                DERIVED.preflight_definition(payload)
                if background is True:
                    operation = start_derived_background(
                        "create", payload, actor, self._remote()
                    )
                    derived_failure_phase = "request-response"
                    self._json(HTTPStatus.ACCEPTED, {
                        "operation": operation,
                        "statusUrl": f"/api/operations/{operation['id']}",
                    })
                    return
                derived_failure_phase = "database-transaction"
                result = DERIVED.create(payload, actor)
                derived_failure_phase = "result-reporting"
                schedule_semantic_outbox()
                CONTROL.audit(
                    "derived_layer.created",
                    actor=actor,
                    remote=self._remote(),
                    details={
                        "name": result["name"],
                        "kind": result["kind"],
                        "sources": result["sources"],
                        "spatialScope": result.get("spatialScope"),
                    },
                )
                self._json(HTTPStatus.CREATED, {"derivedLayer": result})
                return
            if derived_action_path:
                derived_failure_phase = "preflight"
                if not DERIVED:
                    raise DerivedLayerError(
                        "Derived-layer database management is not configured."
                    )
                name, action = derived_action_path.groups()
                if payload.get("confirmed") is not True:
                    self._json(
                        HTTPStatus.CONFLICT,
                        derived_blocked_error(
                            code="derived_layer.confirmation_required",
                            message=(
                                "Please confirm this derived-layer change "
                                "before it is applied."
                            ),
                            suggested_action=(
                                "Review the change, then retry with confirmation."
                            ),
                            operation=action,
                            name=name,
                        ),
                    )
                    return
                background = payload.pop("background", False)
                replacement = None
                if action == "replace":
                    replacement = {**payload}
                    replacement.pop("confirmed", None)
                    replacement = resolve_derived_spatial_scope(replacement)
                    require_semantic_derived_sources(
                        replacement,
                        self._semantic_request(actor, "/v1/catalog"),
                    )
                    DERIVED.preflight_definition(replacement)
                elif action == "refresh":
                    DERIVED.preflight_refresh(name)
                if background is True and action in {"refresh", "replace"}:
                    operation = start_derived_background(
                        action,
                        replacement if action == "replace" else {},
                        actor,
                        self._remote(),
                        name,
                    )
                    derived_failure_phase = "request-response"
                    self._json(HTTPStatus.ACCEPTED, {
                        "operation": operation,
                        "statusUrl": f"/api/operations/{operation['id']}",
                    })
                    return
                if action == "refresh":
                    derived_failure_phase = "database-transaction"
                    result = DERIVED.refresh(name, actor)
                    derived_failure_phase = "result-reporting"
                    event = "derived_layer.refreshed"
                elif action == "replace":
                    derived_failure_phase = "database-transaction"
                    result = DERIVED.replace(name, replacement, actor)
                    derived_failure_phase = "result-reporting"
                    changes = result.get("columnChanges", {})
                    result.update(derived_workspace_impact(
                        name,
                        changes.get("removed", []) + changes.get("changed", []),
                    ))
                    if result["requiresSecondOrderChanges"]:
                        affected = "; ".join(
                            item["label"] for item in result["fieldReferences"]
                        )
                        symbology_affected = any(
                            "symbology" in item["usage"]
                            for item in result["fieldReferences"]
                        )
                        result["userMessage"] = (
                            f'The derived layer “{name}” was saved, but these '
                            f"map settings now need attention: {affected}."
                        )
                        result["suggestedAction"] = (
                            "Update those map settings before publishing. In "
                            "the CLI, you can include the follow-on changes in "
                            "the same proposal."
                        )
                        if symbology_affected:
                            result["correctionOptions"] = [
                                {
                                    "id": "select_replacement_field",
                                    "label": "Select a valid replacement symbology field",
                                },
                                {
                                    "id": "change_symbology_mode",
                                    "label": "Choose another symbology mode and rebuild its legend",
                                },
                                {
                                    "id": "restore_derived_output",
                                    "label": "Edit the derived query to restore the required output field",
                                },
                                {
                                    "id": "abandon_workspace_change",
                                    "label": "Leave the workspace unchanged",
                                },
                            ]
                    elif result["consumerLabels"]:
                        result["userMessage"] = (
                            f'The derived layer “{name}” was saved. It is used '
                            "by: " + ", ".join(result["consumerLabels"]) + "."
                        )
                        result["suggestedAction"] = (
                            "Review those map layers before publishing."
                        )
                    else:
                        result["userMessage"] = (
                            f'The derived layer “{name}” was saved successfully.'
                        )
                    event = "derived_layer.replaced"
                else:
                    workspace_impact = derived_workspace_impact(name, [])
                    workspace_references = workspace_impact["workspaceReferences"]
                    dependents = DERIVED.dependents(name)
                    if workspace_references or dependents:
                        reasons = derived_in_use_reasons(
                            has_workspace_references=bool(workspace_references),
                            has_postgresql_dependents=bool(dependents),
                        )
                        self._json(
                            HTTPStatus.CONFLICT,
                            derived_blocked_error(
                                code="derived_layer.in_use",
                                message=(
                                    f'The derived layer “{name}” cannot be '
                                    "deleted because it is still in use."
                                ),
                                suggested_action=(
                                    "Remove it from the listed map layers and "
                                    "database views, then try again."
                                ),
                                operation="drop",
                                reasons=reasons,
                                name=name,
                                dependents=dependents,
                                workspaceReferences=workspace_references,
                                consumerLabels=(
                                    workspace_impact["consumerLabels"]
                                ),
                                dropped=False,
                            ),
                        )
                        return
                    derived_failure_phase = "database-transaction"
                    result = DERIVED.drop(name, actor)
                    derived_failure_phase = "result-reporting"
                    event = "derived_layer.dropped"
                schedule_semantic_outbox()
                CONTROL.audit(
                    event,
                    actor=actor,
                    remote=self._remote(),
                    details={
                        "name": name,
                        "kind": result["kind"],
                        "spatialScope": result.get("spatialScope"),
                    },
                )
                self._json(HTTPStatus.OK, {"derivedLayer": result})
                return
            if request_path == "/api/auth/logout":
                CONTROL.logout(self._cookies().get("mapp_session"))
                self._json(HTTPStatus.OK, {"authenticated": False})
                return
            if request_path == "/api/admin/tokens":
                if actor != "admin":
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Administrator session required."})
                    return
                unexpected = sorted(
                    set(payload)
                    - {
                        "name",
                        "expires",
                        "scopes",
                        "extendedExpiryConfirmed",
                    }
                )
                if unexpected:
                    raise ValueError(
                        "Token request contains unsupported properties: "
                        + ", ".join(unexpected)
                    )
                if "scopes" in payload and payload["scopes"] is None:
                    raise ValueError(
                        "Token scopes must be a non-empty array."
                    )
                expires = requested_token_expiry(payload)
                raw, token = CONTROL.create_token(
                    payload.get("name", "CLI token"),
                    expires,
                    payload.get("scopes"),
                )
                self._json(HTTPStatus.CREATED, {"token": raw, "record": token, "warning": "Copy this token now; it will not be shown again."})
                return
            if request_path == "/api/admin/device-authorizations/approve":
                if actor != "admin":
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Administrator session required."})
                    return
                approved = CONTROL.approve_device_authorization(payload.get("userCode", ""))
                self._json(
                    HTTPStatus.OK if approved else HTTPStatus.NOT_FOUND,
                    {"approved": approved},
                )
                return
            if request_path == "/api/admin/password":
                if actor != "admin" or not CONTROL.change_password(payload.get("current", ""), payload.get("replacement", "")):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "Current password is invalid."})
                else:
                    self._json(HTTPStatus.OK, {"message": "Password changed; existing sessions were revoked."})
                return
            if token_revoke_path:
                if actor != "admin":
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Administrator session required."})
                    return
                token_id = token_revoke_path.group(1)
                self._json(HTTPStatus.OK if CONTROL.revoke_token(token_id) else HTTPStatus.NOT_FOUND, {"revoked": token_id})
                return
            if proposal_visual_path:
                proposal_id, action = proposal_visual_path.groups()
                try:
                    proposal = preview_proposal(proposal_id)
                except FileNotFoundError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                except FileExistsError as exc:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": str(exc),
                        "ruleId": "proposal.revision",
                    })
                    return
                except ValueError as exc:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": str(exc),
                        "ruleId": "proposal.preview_status",
                    })
                    return
                except RuntimeError as exc:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": str(exc),
                        "ruleId": "proposal.integrity",
                    })
                    return
                layer_key = payload.get("layer")
                if not isinstance(layer_key, str) or not layer_key.strip():
                    raise ValueError("Visual requests require a layer key.")
                hover_request = requested_hover(payload)
                hover_expectations = expected_hover_text(payload)
                group_preview = proposal_group_preview(
                    proposal,
                    layer_key,
                    payload.get("locale"),
                )
                view_mode = payload.get("viewMode", "focus")
                if view_mode not in {"focus", "default"}:
                    raise ValueError(
                        "Visual viewMode must be 'focus' or 'default'."
                    )
                panels = (
                    visual_panels(payload) if action == "screenshot" else []
                )
                if action != "visual-plan":
                    visual_operation = (
                        visual_operation
                        or CONTROL.create_operation(
                            (
                                "proposal.screenshot"
                                if action == "screenshot"
                                else "proposal.visual-test"
                            ),
                            actor,
                            {
                                "source": "candidate",
                                "proposalId": proposal_id,
                                "candidateHash": proposal["candidateHash"],
                                "layer": layer_key,
                                "groups": group_preview["groups"],
                                "originalLayers": group_preview[
                                    "original"
                                ]["layers"],
                                "candidateLayers": group_preview[
                                    "candidate"
                                ]["layers"],
                                "panels": panels,
                                "pluginCatalogueFingerprint": proposal[
                                    "pluginCatalogueFingerprint"
                                ],
                            },
                        )
                    )
                    visual_planning_active = True
                    update_visual_operation_progress(
                        visual_operation["id"], "planning"
                    )
                plan_source = (
                    "candidate"
                    if group_preview["candidate"]["requestedLayerPresent"]
                    else "original"
                )
                plan_selection = group_preview[plan_source]
                plan = visual_plan(
                    proposal[plan_source],
                    plan_selection["anchorLayer"],
                    DB_CONNECTIONS,
                    group_preview["locale"],
                    visual_request=payload,
                )
                plan.update({
                    "layer": layer_key,
                    "anchorLayer": group_preview["candidate"]["anchorLayer"],
                    "layers": group_preview["candidate"]["layers"],
                    "groups": group_preview["groups"],
                    "activeGroups": group_preview["candidate"]["groups"],
                    "changeKind": group_preview["changeKind"],
                    "requestedLayerDeleted": not group_preview[
                        "candidate"
                    ]["requestedLayerPresent"],
                    "candidateLayerDiagnostics": {
                        "configuredLayerKeys": group_preview[
                            "candidate"
                        ]["configuredLayerKeys"],
                        "groupMembership": group_preview[
                            "candidate"
                        ]["groupMembership"],
                    },
                    "evidenceApplicability": {
                        side: group_preview[side]["requestedLayerPresent"]
                        for side in ("original", "candidate")
                    },
                    "viewSource": plan_source,
                    "viewMode": view_mode,
                    "pluginCatalogueFingerprint": proposal["pluginCatalogueFingerprint"],
                })
                feature_info_evidence = proposal_feature_info_evidence(
                    proposal,
                    layer_key,
                    plan.get("locale", "locale"),
                )
                explicit_info_text = expected_info_panel_text(payload)
                candidate_evidence = feature_info_evidence["candidate"]
                if (
                    explicit_info_text
                    and group_preview["candidate"]["renderLayer"] is not None
                ):
                    candidate_evidence["requested"] = True
                    candidate_evidence["expectedText"] = list(dict.fromkeys([
                        *candidate_evidence["expectedText"],
                        *explicit_info_text,
                    ]))
                base_interaction = plan.get("interaction")
                for side in ("original", "candidate"):
                    feature_info_evidence[side]["planned"] = bool(
                        feature_info_evidence[side]["requested"]
                        and group_preview[side]["renderLayer"] is not None
                        and isinstance(base_interaction, dict)
                        and base_interaction.get("type")
                    )
                plan["featureInfoEvidence"] = feature_info_evidence
                binding = {
                    "source": "candidate",
                    "proposalId": proposal_id,
                    "candidateHash": proposal["candidateHash"],
                }
                if action == "visual-plan":
                    self._json(HTTPStatus.OK, {**binding, "plan": plan})
                    return
                feature_info_comparison = (
                    action == "screenshot"
                    and any(
                        feature_info_evidence[side]["requested"]
                        for side in ("original", "candidate")
                    )
                )
                render_plan = dict(plan)
                original_render_plan = {
                    **render_plan,
                    "anchorLayer": group_preview["original"]["anchorLayer"],
                    "layers": group_preview["original"]["layers"],
                    "backgroundLayers": group_preview[
                        "original"
                    ]["backgroundLayers"],
                    "activeGroups": group_preview["original"]["groups"],
                    "candidateLayerDiagnostics": {
                        "configuredLayerKeys": group_preview[
                            "original"
                        ]["configuredLayerKeys"],
                        "groupMembership": group_preview[
                            "original"
                        ]["groupMembership"],
                    },
                    "pluginChecks": plugin_preview_checks(
                        proposal["original"],
                        group_preview["locale"],
                        group_preview["original"]["layers"],
                    ),
                }
                candidate_render_plan = {
                    **render_plan,
                    "anchorLayer": group_preview["candidate"]["anchorLayer"],
                    "layers": group_preview["candidate"]["layers"],
                    "backgroundLayers": group_preview[
                        "candidate"
                    ]["backgroundLayers"],
                    "activeGroups": group_preview["candidate"]["groups"],
                    "candidateLayerDiagnostics": {
                        "configuredLayerKeys": group_preview[
                            "candidate"
                        ]["configuredLayerKeys"],
                        "groupMembership": group_preview[
                            "candidate"
                        ]["groupMembership"],
                    },
                    "pluginChecks": plugin_preview_checks(
                        proposal["candidate"],
                        group_preview["locale"],
                        group_preview["candidate"]["layers"],
                    ),
                }
                for side, side_plan in (
                    ("original", original_render_plan),
                    ("candidate", candidate_render_plan),
                ):
                    side_plan.pop("hover", None)
                    render_layer = group_preview[side]["renderLayer"]
                    if not isinstance(render_layer, str):
                        continue
                    _, side_locale = select_locale(
                        proposal[side],
                        group_preview["locale"],
                    )
                    side_layer = (
                        side_locale.get("layers") or {}
                    ).get(render_layer)
                    if isinstance(side_layer, dict):
                        hover_plan = visual_hover_plan(side_layer)
                        if hover_plan:
                            side_plan["hover"] = hover_plan
                if group_preview["original"]["renderLayer"] is None:
                    original_render_plan.pop("interaction", None)
                if group_preview["candidate"]["renderLayer"] is None:
                    candidate_render_plan.pop("interaction", None)
                render_payload = dict(payload)
                render_payload["viewMode"] = view_mode
                render_payload["hover"] = hover_request
                render_payload["expectedHoverText"] = hover_expectations
                if action == "screenshot":
                    render_payload.setdefault(
                        "viewport",
                        {"width": 1080, "height": 1080},
                    )
                    render_payload.setdefault("deviceScaleFactor", 1)
                    # Keep approval images at the requested viewport dimensions
                    # instead of extending them to the document's full height.
                    render_payload["fullPage"] = False
                    if feature_info_comparison:
                        for side, side_plan in (
                            ("original", original_render_plan),
                            ("candidate", candidate_render_plan),
                        ):
                            evidence = feature_info_evidence[side]
                            if evidence["planned"]:
                                side_plan["interaction"] = {
                                    **(render_plan.get("interaction") or {}),
                                    "requireInfoPanel": True,
                                    "expectedInfoPanelText": evidence[
                                        "expectedText"
                                    ],
                                }
                            else:
                                side_plan.pop("interaction", None)
                    else:
                        # A proposal screenshot compares configuration states.
                        # Selection would obscure point-style changes, so it is
                        # reserved for feature-information comparisons.
                        original_render_plan.pop("interaction", None)
                        candidate_render_plan.pop("interaction", None)
                elif feature_info_evidence["candidate"]["planned"]:
                    candidate_render_plan["interaction"] = {
                        **(candidate_render_plan.get("interaction") or {}),
                        "requireInfoPanel": True,
                        "expectedInfoPanelText": feature_info_evidence[
                            "candidate"
                        ]["expectedText"],
                    }
                operation = visual_operation
                if operation is None:
                    raise RuntimeError(
                        "Visual operation was not created before planning."
                    )
                binding["operationId"] = operation["id"]
                comparison_binding_valid = True
                visual_planning_active = False
                try:
                    # Keep the isolated workspace pinned throughout the
                    # original/candidate comparison so another proposal cannot
                    # replace either side while Chromium is rendering it.
                    update_visual_operation_progress(
                        operation["id"], "preview-lock"
                    )
                    with PREVIEW_LOCK:
                        original_status = None
                        original_result = None
                        original_preview = None
                        target_url = os.environ.get(
                            "BROWSER_PREVIEW_XYZ_URL",
                            "http://xyz-preview:3000",
                        )
                        if action == "screenshot":
                            original_binding = {
                                "source": "original",
                                "proposalId": proposal_id,
                                "originalHash": proposal["originalHash"],
                                "operationId": operation["id"],
                            }
                            original_render_payload = {
                                **render_payload,
                                "metadata": original_binding,
                            }
                            if not group_preview["original"]["requestedLayerPresent"]:
                                original_render_payload["panels"] = []
                            if (
                                render_payload.get("hover") is True
                                and plan_source != "original"
                                and "hover" not in original_render_plan
                            ):
                                original_render_payload["hover"] = False
                            update_visual_operation_progress(
                                operation["id"], "original-page-readiness"
                            )
                            original_preview = prepare_original_preview(proposal)
                            original_status, original_result = run_browser_visual(
                                group_preview["original"]["renderLayer"],
                                original_render_plan,
                                original_render_payload,
                                target_url=target_url,
                            )
                        update_visual_operation_progress(
                            operation["id"], "candidate-page-readiness"
                        )
                        candidate_preview = prepare_candidate_preview(proposal)
                        candidate_render_payload = {
                            **render_payload,
                            "metadata": binding,
                        }
                        if not group_preview["candidate"]["requestedLayerPresent"]:
                            candidate_render_payload["panels"] = []
                        if (
                            render_payload.get("hover") is True
                            and plan_source != "candidate"
                            and "hover" not in candidate_render_plan
                        ):
                            candidate_render_payload["hover"] = False
                        status, candidate_result = run_browser_visual(
                            group_preview["candidate"]["renderLayer"],
                            candidate_render_plan,
                            candidate_render_payload,
                            target_url=target_url,
                        )
                    if proposal["pluginCatalogueFingerprint"] != plugin_catalogue()["fingerprint"]:
                        raise FileExistsError(
                            "Plugin catalogue changed during preview; run a new proposal preview."
                        )
                    if action == "screenshot":
                        if (
                            not isinstance(original_result, dict)
                            or not isinstance(candidate_result, dict)
                        ):
                            result = None
                        else:
                            comparison_binding_valid = all((
                                not browser_result_has_evidence(side_result)
                                or side_result.get("metadata") == side_binding
                            ) for side_result, side_binding in (
                                (original_result, original_binding),
                                (candidate_result, binding),
                            ))
                            original_artifacts = original_result.get("artifacts") or {}
                            candidate_artifacts = candidate_result.get("artifacts") or {}
                            original_page_key = (
                                "afterPage"
                                if feature_info_evidence["original"]["planned"]
                                else "beforePage"
                            )
                            candidate_page_key = (
                                "afterPage"
                                if feature_info_evidence["candidate"]["planned"]
                                else "beforePage"
                            )
                            original_map_key = (
                                "afterMap"
                                if feature_info_evidence["original"]["planned"]
                                else "beforeMap"
                            )
                            candidate_map_key = (
                                "afterMap"
                                if feature_info_evidence["candidate"]["planned"]
                                else "beforeMap"
                            )
                            comparison_artifacts = {
                                "beforePage": original_artifacts.get(
                                    original_page_key
                                ),
                                "beforeMap": original_artifacts.get(
                                    original_map_key
                                ),
                                "afterPage": candidate_artifacts.get(
                                    candidate_page_key
                                ),
                                "afterMap": candidate_artifacts.get(
                                    candidate_map_key
                                ),
                                "beforeReport": original_artifacts.get("report"),
                                "afterReport": candidate_artifacts.get("report"),
                                "beforeHoverTooltip": original_artifacts.get(
                                    "hoverTooltip"
                                ),
                                "afterHoverTooltip": candidate_artifacts.get(
                                    "hoverTooltip"
                                ),
                            }
                            if feature_info_comparison:
                                comparison_artifacts.update({
                                    "beforeInfoPanel": original_artifacts.get(
                                        "infoPanel"
                                    ),
                                    "afterInfoPanel": candidate_artifacts.get(
                                        "infoPanel"
                                    ),
                                })
                            feature_info_observations = {
                                side: feature_info_observation(
                                    (
                                        original_result
                                        if side == "original"
                                        else candidate_result
                                    ),
                                    feature_info_evidence[side],
                                )
                                for side in ("original", "candidate")
                            }
                            for panel in panels:
                                key = (
                                    "filteringPanel"
                                    if panel == "filtering"
                                    else "stylingPanel"
                                )
                                artifact_name = (
                                    "FilteringPanel"
                                    if panel == "filtering"
                                    else "StylingPanel"
                                )
                                comparison_artifacts.update({
                                    f"before{artifact_name}": (
                                        original_artifacts.get(key)
                                    ),
                                    f"after{artifact_name}": (
                                        candidate_artifacts.get(key)
                                    ),
                                })
                            candidate_status = status
                            statuses = {original_status, candidate_status}
                            status = (
                                HTTPStatus.OK
                                if statuses == {HTTPStatus.OK}
                                else HTTPStatus.UNPROCESSABLE_ENTITY
                                if HTTPStatus.UNPROCESSABLE_ENTITY in statuses
                                else next(
                                    (
                                        item for item in statuses
                                        if item != HTTPStatus.OK
                                    ),
                                    HTTPStatus.BAD_GATEWAY,
                                )
                            )
                            if (
                                status == HTTPStatus.OK
                                and not all(
                                    observation["passed"]
                                    for observation
                                    in feature_info_observations.values()
                                )
                            ):
                                status = HTTPStatus.UNPROCESSABLE_ENTITY
                            failure_result = next((
                                side_result
                                for side_status, side_result in (
                                    (candidate_status, candidate_result),
                                    (original_status, original_result),
                                )
                                if side_status != HTTPStatus.OK
                            ), {})
                            result = {
                                "runId": candidate_result.get("runId"),
                                "passed": status == HTTPStatus.OK,
                                "metadata": binding,
                                "error": next((
                                    side_result.get("error")
                                    for side_result in (
                                        candidate_result,
                                        original_result,
                                    )
                                    if side_result.get("error")
                                ), None),
                                **{
                                    key: failure_result[key]
                                    for key in (
                                        "code",
                                        "failedStage",
                                        "timeoutMilliseconds",
                                        "diagnostics",
                                    )
                                    if failure_result.get(key) is not None
                                },
                                "comparison": {
                                    "before": "original",
                                    "after": "candidate",
                                    "featureInfoPanel": feature_info_comparison,
                                    "featureInfoEvidence": (
                                        feature_info_observations
                                    ),
                                    "original": original_result,
                                    "candidate": candidate_result,
                                },
                                "artifacts": comparison_artifacts,
                                "capture": {
                                    "original": original_result.get("capture"),
                                    "candidate": candidate_result.get("capture"),
                                },
                                "diagnosis": {
                                    "outcome": (
                                        "passed"
                                        if status == HTTPStatus.OK
                                        else "failed"
                                    ),
                                    "original": original_result.get("diagnosis"),
                                    "candidate": candidate_result.get("diagnosis"),
                                    "featureInfoEvidence": (
                                        feature_info_observations
                                    ),
                                },
                            }
                        preview = {
                            "original": original_preview,
                            "candidate": candidate_preview,
                        }
                    else:
                        result = candidate_result
                        preview = candidate_preview
                except FileExistsError as exc:
                    operation = finish_visual_operation(
                        operation["id"],
                        status="failed",
                        error={"code": "visual.plugin_catalogue_stale", "message": str(exc)},
                    )
                    self._json(HTTPStatus.CONFLICT, {
                        **binding,
                        "error": str(exc),
                        "operation": operation,
                    })
                    return
                except TimeoutError as exc:
                    operation = finish_visual_operation(
                        operation["id"],
                        status="failed",
                        error={"code": "visual.preview_timeout", "message": str(exc)},
                    )
                    self._json(HTTPStatus.GATEWAY_TIMEOUT, {
                        **binding,
                        "error": str(exc),
                        "operation": operation,
                    })
                    return
                except Exception as exc:
                    operation = finish_visual_operation(
                        operation["id"],
                        status="indeterminate",
                        error={
                            "code": "visual.preview_interrupted",
                            "message": (
                                "Proposal visual validation did not record a "
                                f"terminal result: {exc}"
                            ),
                        },
                    )
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                        **binding,
                        "error": (
                            "Proposal visual validation did not record a "
                            "terminal result."
                        ),
                        "operation": operation,
                    })
                    return
                if not isinstance(result, dict):
                    partial_response = {
                        **binding,
                        "plan": plan,
                        "preview": preview,
                    }
                    operation = finish_visual_operation(
                        operation["id"],
                        status="failed",
                        result=partial_response,
                        error={
                            "code": "visual.invalid_response",
                            "message": (
                                "Browser validation returned an invalid response."
                            ),
                        },
                    )
                    self._json(HTTPStatus.BAD_GATEWAY, {
                        **partial_response,
                        "error": "Browser validation returned an invalid response.",
                        "operation": operation,
                    })
                    return
                result_binding = result.get("metadata")
                if (
                    browser_result_has_evidence(result)
                    and (
                        result_binding != binding
                        or not comparison_binding_valid
                    )
                ):
                    partial_response = {
                        **binding,
                        "plan": plan,
                        "preview": preview,
                        "visual": result,
                    }
                    operation = finish_visual_operation(
                        operation["id"],
                        status="failed",
                        result=partial_response,
                        error={
                            "code": "visual.binding_mismatch",
                            "message": "Browser artifact binding was not preserved.",
                        },
                    )
                    self._json(HTTPStatus.BAD_GATEWAY, {
                        **partial_response,
                        "error": "Browser artifact binding was not preserved.",
                        "operation": operation,
                    })
                    return
                response = {
                    **binding,
                    "plan": plan,
                    "preview": preview,
                    "visual": result,
                }
                if status == HTTPStatus.UNPROCESSABLE_ENTITY:
                    response["error"] = "Browser validation did not pass."
                elif status != HTTPStatus.OK:
                    response["error"] = result.get("error", "Browser validation failed.")
                operation = finish_visual_operation(
                    operation["id"],
                    status="succeeded" if status == HTTPStatus.OK else "failed",
                    result=response,
                    error=(
                        None
                        if status == HTTPStatus.OK
                        else visual_failure_error(
                            status, result, response["error"]
                        )
                    ),
                )
                CONTROL.audit(
                    (
                        "proposal.screenshot_completed"
                        if action == "screenshot"
                        else "proposal.visual_completed"
                    ),
                    actor=actor,
                    remote=self._remote(),
                    details={
                        **binding,
                        "action": action,
                        "layer": layer_key,
                        "runId": result.get("runId"),
                        "passed": result.get("passed"),
                    },
                )
                # The durable operation stores `response` as its result. Build
                # a separate outer envelope instead of inserting the operation
                # back into that same object, which would create
                # response -> operation -> result -> response.
                self._json(status, {**response, "operation": operation})
                return
            if request_path == "/api/xyz/reload":
                unexpected = sorted(
                    set(payload)
                    - {"confirmed", "workspaceFingerprint", "timeout"}
                )
                if unexpected:
                    raise ValueError(
                        "XYZ reload contains unsupported properties: "
                        + ", ".join(unexpected)
                    )
                if payload.get("confirmed") is not True:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": (
                            "Explicit confirmation is required before XYZ reload."
                        ),
                        "code": "xyz.confirmation_required",
                    })
                    return
                timeout = reload_timeout(payload.get("timeout", 30))
                with SAVE_RELOAD_LOCK:
                    supplied_fingerprint = payload.get("workspaceFingerprint")
                    if supplied_fingerprint is not None and (
                        not isinstance(supplied_fingerprint, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", supplied_fingerprint)
                    ):
                        raise ValueError(
                            "Workspace fingerprint must be a lowercase SHA-256 digest."
                        )
                    raw, _, _ = read_workspace()
                    fingerprint = workspace_fingerprint(raw)
                    if (
                        supplied_fingerprint is not None
                        and supplied_fingerprint != fingerprint
                    ):
                        self._json(HTTPStatus.CONFLICT, {
                            "error": "Workspace fingerprint is stale.",
                            "code": "workspace.fingerprint_conflict",
                            "currentWorkspaceFingerprint": fingerprint,
                        })
                        return
                    operation = CONTROL.create_operation(
                        "xyz.reload",
                        actor,
                        {"workspaceFingerprint": fingerprint},
                    )
                    try:
                        result = request_reload(fingerprint)
                        result["status"] = wait_reload(
                            result["requestedGeneration"],
                            fingerprint,
                            timeout,
                        )
                    except Exception as exc:
                        operation = CONTROL.finish_operation(
                            operation["id"],
                            status="indeterminate",
                            error={
                                "code": "xyz.reload_interrupted",
                                "message": (
                                    "Reload did not record a terminal result: "
                                    f"{exc}"
                                ),
                            },
                        )
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                            "error": (
                                "XYZ reload did not record a terminal result. "
                                "Inspect XYZ status before retrying."
                            ),
                            "operation": operation,
                        })
                        return
                completed = result["status"]["completed"]
                operation = CONTROL.finish_operation(
                    operation["id"],
                    status="succeeded" if completed else "indeterminate",
                    result=result if completed else None,
                    error=(
                        None
                        if completed
                        else {
                            "code": "xyz.reload_timeout",
                            "message": "Reload completion was not observed before timeout.",
                        }
                    ),
                )
                CONTROL.audit(
                    "xyz.reload_requested",
                    actor=actor,
                    remote=self._remote(),
                    details=result,
                )
                self._json(
                    HTTPStatus.OK if completed else HTTPStatus.GATEWAY_TIMEOUT,
                    {**result, "operation": operation},
                )
                return
            if request_path == "/api/visual-plan":
                layer_key = payload.get("layer")
                if not isinstance(layer_key, str) or not layer_key.strip():
                    raise ValueError("Visual requests require a layer key.")
                _, current_workspace, _ = read_workspace()
                plan = visual_plan(
                    payload.get("workspace", current_workspace),
                    layer_key,
                    DB_CONNECTIONS,
                    payload.get("locale"),
                    visual_request=payload,
                )
                plan_expected_info_panel(plan, payload)
                workspace_for_plan = payload.get("workspace", current_workspace)
                plan["pluginChecks"] = plugin_preview_checks(
                    workspace_for_plan,
                    plan.get("locale", "locale"),
                    plan.get("layers", [layer_key]),
                )
                plan["pluginCatalogueFingerprint"] = plugin_catalogue()["fingerprint"]
                self._json(
                    HTTPStatus.OK,
                    {"plan": plan},
                )
                return
            if request_path == "/api/visual-test":
                layer_key = payload.get("layer")
                if not isinstance(layer_key, str) or not layer_key.strip():
                    raise ValueError("Visual requests require a layer key.")
                _, current_workspace, _ = read_workspace()
                visual_operation = (
                    visual_operation
                    or CONTROL.create_operation(
                        "visual.test",
                        actor,
                        {
                            "source": "live",
                            "layer": layer_key,
                            "locale": payload.get("locale"),
                        },
                    )
                )
                visual_planning_active = True
                update_visual_operation_progress(
                    visual_operation["id"], "planning"
                )
                plan = visual_plan(
                    payload.get("workspace", current_workspace),
                    layer_key,
                    DB_CONNECTIONS,
                    payload.get("locale"),
                    visual_request=payload,
                )
                plan_expected_info_panel(plan, payload)
                workspace_for_plan = payload.get("workspace", current_workspace)
                plan["pluginChecks"] = plugin_preview_checks(
                    workspace_for_plan,
                    plan.get("locale", "locale"),
                    plan.get("layers", [layer_key]),
                )
                plan["pluginCatalogueFingerprint"] = plugin_catalogue()["fingerprint"]
                operation = visual_operation
                binding = {
                    "source": "live",
                    "operationId": operation["id"],
                }
                runner_payload = json.dumps(
                    {
                        "url": os.environ.get(
                            "BROWSER_XYZ_URL",
                            "http://caddy:8081",
                        ),
                        "layer": layer_key,
                        "plan": plan,
                        "viewport": payload.get(
                            "viewport",
                            {"width": 1920, "height": 1080},
                        ),
                        "deviceScaleFactor": payload.get("deviceScaleFactor"),
                        "hover": requested_hover(payload),
                        "expectedHoverText": expected_hover_text(payload),
                        "metadata": binding,
                        "runTimeout": VISUAL_BROWSER_TIMEOUT_SECONDS * 1000,
                    },
                    allow_nan=False,
                ).encode()
                visual_planning_active = False
                try:
                    update_visual_operation_progress(
                        operation["id"], "browser-execution"
                    )
                    with urlopen(Request(
                        os.environ.get("BROWSER_RUNNER_URL", "http://browser-runner:8080/run"),
                        data=runner_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ), timeout=VISUAL_BROWSER_TIMEOUT_SECONDS + 15) as response:
                        result = json.load(response)
                    if (
                        browser_result_has_evidence(result)
                        and result.get("metadata") != binding
                    ):
                        response_payload = {
                            **binding,
                            "error": "Browser artifact binding was not preserved.",
                            "plan": plan,
                            "visual": result,
                        }
                        operation = finish_visual_operation(
                            operation["id"],
                            status="failed",
                            result=response_payload,
                            error={
                                "code": "visual.binding_mismatch",
                                "message": (
                                    "Browser artifact binding was not preserved."
                                ),
                            },
                        )
                        self._json(HTTPStatus.BAD_GATEWAY, {
                            **response_payload,
                            "operation": operation,
                        })
                        return
                    CONTROL.audit("visual.completed", actor=actor, remote=self._remote(), details={"layer": layer_key, "runId": result.get("runId"), "passed": result.get("passed")})
                    response_payload = {**binding, "plan": plan, "visual": result}
                    operation = finish_visual_operation(
                        operation["id"],
                        status="succeeded",
                        result=response_payload,
                    )
                    self._json(HTTPStatus.OK, {**response_payload, "operation": operation})
                except HTTPError as exc:
                    try:
                        result = strict_json_loads(exc.read())
                    except (OSError, UnicodeError, ValueError):
                        result = None
                    if (
                        browser_result_has_evidence(result)
                        and result.get("metadata") != binding
                    ):
                        response_payload = {
                            **binding,
                            "error": "Browser artifact binding was not preserved.",
                            "plan": plan,
                            "visual": result,
                        }
                        operation = finish_visual_operation(
                            operation["id"],
                            status="failed",
                            result=response_payload,
                            error={
                                "code": "visual.binding_mismatch",
                                "message": (
                                    "Browser artifact binding was not preserved."
                                ),
                            },
                        )
                        self._json(HTTPStatus.BAD_GATEWAY, {
                            **response_payload,
                            "operation": operation,
                        })
                        return
                    if exc.code == HTTPStatus.UNPROCESSABLE_ENTITY and isinstance(result, dict):
                        CONTROL.audit(
                            "visual.completed",
                            actor=actor,
                            remote=self._remote(),
                            details={
                                "layer": layer_key,
                                "runId": result.get("runId"),
                                "passed": False,
                            },
                        )
                        response_payload = {
                            **binding,
                            "error": "Browser validation did not pass.",
                            "plan": plan,
                            "visual": result,
                        }
                        operation = finish_visual_operation(
                            operation["id"],
                            status="failed",
                            result=response_payload,
                            error=visual_failure_error(
                                exc.code,
                                result,
                                "Browser validation did not pass.",
                            ),
                        )
                        self._json(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            {
                                **response_payload,
                                "operation": operation,
                            },
                        )
                    elif exc.code == HTTPStatus.TOO_MANY_REQUESTS:
                        response_payload = {
                            **binding,
                            "error": (
                                result.get("error")
                                if isinstance(result, dict)
                                else None
                            ) or "Visual runner is busy. Retry later.",
                            "plan": plan,
                            **({"visual": result} if isinstance(result, dict) else {}),
                        }
                        operation = finish_visual_operation(
                            operation["id"],
                            status="failed",
                            result=response_payload,
                            error={"code": "visual.busy", "message": "Visual runner is busy."},
                        )
                        self._json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {
                                **response_payload,
                                "operation": operation,
                            },
                        )
                    else:
                        response_payload = {
                            **binding,
                            "error": (
                                result.get("error")
                                if isinstance(result, dict)
                                else None
                            ) or (
                                "Browser validation service returned "
                                f"HTTP {exc.code}."
                            ),
                            "plan": plan,
                            **({"visual": result} if isinstance(result, dict) else {}),
                        }
                        operation = finish_visual_operation(
                            operation["id"],
                            status="failed",
                            result=response_payload,
                            error=visual_failure_error(
                                exc.code,
                                result if isinstance(result, dict) else {},
                                response_payload["error"],
                            ),
                        )
                        self._json(
                            (
                                HTTPStatus.GATEWAY_TIMEOUT
                                if exc.code in {
                                    HTTPStatus.GATEWAY_TIMEOUT,
                                    HTTPStatus.REQUEST_TIMEOUT,
                                }
                                else HTTPStatus.BAD_GATEWAY
                            ),
                            {
                                **response_payload,
                                "operation": operation,
                            },
                        )
                except TimeoutError as exc:
                    diagnostics = {"exceptionType": type(exc).__name__}
                    response_payload = {
                        **binding,
                        "error": (
                            "Browser validation did not return before its "
                            "deadline."
                        ),
                        "plan": plan,
                    }
                    operation = finish_visual_operation(
                        operation["id"],
                        status="failed",
                        result=response_payload,
                        error={
                            "code": "visual.browser_transport_timeout",
                            "message": response_payload["error"],
                            "failedStage": "browser-transport",
                            "diagnostics": diagnostics,
                        },
                    )
                    self._json(HTTPStatus.GATEWAY_TIMEOUT, {
                        **response_payload,
                        "operation": operation,
                    })
                except Exception as exc:
                    response_payload = {
                        **binding,
                        "error": f"Browser validation failed: {exc}",
                        "plan": plan,
                    }
                    operation = finish_visual_operation(
                        operation["id"],
                        status="failed",
                        result=response_payload,
                        error={
                            "code": "visual.upstream",
                            "message": f"Browser validation failed: {exc}",
                        },
                    )
                    self._json(HTTPStatus.BAD_GATEWAY, {
                        **response_payload,
                        "operation": operation,
                    })
                return
            if request_path == "/api/sql/test":
                _, candidate, _ = read_workspace()
                locale_key, locale = select_locale(
                    candidate,
                    payload.get("locale"),
                )
                layer = (locale.get("layers") or {}).get(payload.get("layer")) if isinstance(locale, dict) else None
                if not isinstance(layer, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "Unknown layer."})
                    return
                entries = list(layer.get("infoj") or [])
                entries.append({
                    "field": payload.get("field", "calculated_value"),
                    "fieldfx": payload.get("expression"),
                    "type": payload.get("type", "text"),
                    "display": True,
                })
                layer["infoj"] = entries
                try:
                    result = test_info_expression(candidate, locale_key, payload["layer"], len(entries) - 1)
                    self._json(HTTPStatus.OK, result)
                except (TypeError, ValueError, psycopg.Error) as exc:
                    self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Expression test failed.", "errors": annotated([{"path": "fieldfx", "message": str(exc)}])})
                return
            if request_path in {"/api/mutate", "/api/proposals", "/api/proposals/check"}:
                _, current_workspace, current_revision = read_workspace()
                expected = payload.get("revision") or current_revision
                if expected != current_revision:
                    self._json(HTTPStatus.CONFLICT, {"error": "Workspace changed on disk. Reload before continuing."})
                    return
                candidate, diff = apply_operations(current_workspace, payload.get("operations") or [])
                errors = validate_candidate(candidate, current_workspace)
                if errors:
                    self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Validation failed.", "errors": annotated(errors)})
                    return
                supplied_check = payload.get("checkFingerprint")
                validated_check = None
                if request_path == "/api/proposals" and supplied_check is not None:
                    validated_check = proposal_check(
                        current_workspace, expected, candidate,
                        payload.get("operations") or [], diff,
                        payload.get("explanation"),
                    )
                    if (
                        not isinstance(supplied_check, str)
                        or supplied_check != validated_check["checkFingerprint"]
                    ):
                        self._json(HTTPStatus.CONFLICT, {
                            "error": "Checked operations no longer match this proposal request.",
                            "ruleId": "proposal.check_fingerprint",
                            "remediation": "Run proposals check again and create from the new check fingerprint.",
                        })
                        return
                if request_path == "/api/proposals/check":
                    checked = proposal_check(
                        current_workspace, expected, candidate,
                        payload.get("operations") or [], diff,
                        payload.get("explanation"),
                    )
                    _, warnings = semantic_publication_diagnostics(
                        candidate,
                        current_workspace,
                    )
                    warnings.extend(layer_key_diagnostics(
                        candidate, current_workspace,
                    ))
                    checked["warnings"] = annotated(warnings)
                    self._json(HTTPStatus.OK, {"check": checked})
                elif request_path == "/api/proposals":
                    proposal = proposal_create(
                        CONTROL,
                        current_workspace,
                        expected,
                        candidate,
                        payload.get("operations") or [],
                        diff,
                        actor,
                        payload.get("explanation"),
                        plugin_catalogue_fingerprint=(
                            validated_check["pluginCatalogueFingerprint"]
                            if validated_check is not None
                            else None
                        ),
                    )
                    if supplied_check is not None:
                        proposal["checkFingerprint"] = supplied_check
                    _, warnings = semantic_publication_diagnostics(
                        candidate,
                        current_workspace,
                    )
                    warnings.extend(layer_key_diagnostics(
                        candidate, current_workspace,
                    ))
                    proposal["warnings"] = annotated(warnings)
                    proposal_write(CONTROL, proposal)
                    self._json(HTTPStatus.CREATED, {"proposal": proposal})
                elif payload.get("save"):
                    try:
                        encoded, next_revision, fingerprint, reload_result = save_and_reload(
                            candidate,
                            expected,
                        )
                    except FileExistsError as exc:
                        _, _, current_revision = read_workspace()
                        self._json(HTTPStatus.CONFLICT, {
                            "error": str(exc),
                            "currentRevision": current_revision,
                        })
                        return
                    completed = reload_completed(reload_result)
                    CONTROL.audit(
                        "workspace.saved",
                        actor=actor,
                        remote=self._remote(),
                        details={
                            "changeCount": len(diff),
                            "paths": [item["path"] for item in diff],
                            "reloadCompleted": completed,
                        },
                    )
                    self._json(
                        HTTPStatus.OK if completed else HTTPStatus.GATEWAY_TIMEOUT,
                        {
                            **(
                                {}
                                if completed
                                else {
                                    "error": "Workspace saved, but XYZ reload did not complete."
                                }
                            ),
                            "workspace": candidate,
                            "revision": next_revision,
                            "fingerprint": fingerprint,
                            "diff": diff,
                            "saved": True,
                            "reload": reload_result,
                        },
                    )
                else:
                    self._json(HTTPStatus.OK, {"workspace": candidate, "revision": expected, "fingerprint": workspace_hash(candidate), "diff": diff, "saved": False})
                return
            if (
                proposal_action_path
                and proposal_action_path.group(2) == "apply"
            ):
                if payload.get("approved") is not True:
                    self._json(HTTPStatus.BAD_REQUEST, {
                        "error": "Proposal application requires explicit approval.",
                        "code": "proposal.approval_required",
                    })
                    return
                with PROPOSAL_LOCK:
                    proposal = proposal_read(
                        CONTROL,
                        proposal_action_path.group(1),
                    )
                    if proposal["status"] not in {"pending", "applying"}:
                        self._json(HTTPStatus.CONFLICT, {"error": f"Proposal is {proposal['status']}."})
                        return
                    if (
                        workspace_hash(proposal.get("candidate"))
                        != proposal.get("candidateHash")
                    ):
                        self._json(HTTPStatus.CONFLICT, {
                            "error": "Stored proposal candidate failed its integrity check.",
                            "ruleId": "proposal.integrity",
                        })
                        return
                    current_plugin_fingerprint = plugin_catalogue()["fingerprint"]
                    if proposal.get("pluginCatalogueFingerprint") != current_plugin_fingerprint:
                        self._json(HTTPStatus.CONFLICT, {
                            "error": "Plugin catalogue changed; create and preview a new proposal.",
                            "ruleId": "proposal.plugin_catalogue",
                            "expectedPluginCatalogueFingerprint": proposal.get("pluginCatalogueFingerprint"),
                            "actualPluginCatalogueFingerprint": current_plugin_fingerprint,
                        })
                        return
                    validate_before_apply = True
                    if proposal["status"] == "applying":
                        _, current_workspace, current_revision = read_workspace()
                        current_matches_candidate = (
                            workspace_hash(current_workspace)
                            == proposal["candidateHash"]
                        )
                        current_matches_original_revision = (
                            current_revision
                            == proposal["originalRevision"]
                        )
                        # A committed candidate must be reconciled even if
                        # current catalog validation is transiently
                        # unavailable or rules changed after the write. A
                        # different revision is handled as a conflict by the
                        # crash-recovery helper without probing the database.
                        validate_before_apply = (
                            not current_matches_candidate
                            and current_matches_original_revision
                        )
                    if validate_before_apply:
                        errors = validate_candidate(
                            proposal["candidate"],
                            proposal.get("original"),
                        )
                        if errors:
                            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                                "error": "Proposal no longer passes current validation.",
                                "errors": annotated(errors),
                                "ruleId": "proposal.validation",
                            })
                            return
                    operation = CONTROL.create_operation(
                        "proposal.apply",
                        actor,
                        {
                            "proposalId": proposal["id"],
                            "candidateHash": proposal["candidateHash"],
                        },
                    )
                    try:
                        proposal, reload_result = apply_proposal_and_reload(
                            CONTROL,
                            proposal,
                            actor=actor,
                        )
                        completed = reload_completed(reload_result)
                    except FileExistsError as exc:
                        operation = CONTROL.finish_operation(
                            operation["id"],
                            status="failed",
                            error={"code": "proposal.revision", "message": str(exc)},
                        )
                        _, _, current_revision = read_workspace()
                        self._json(HTTPStatus.CONFLICT, {
                            "error": str(exc),
                            "ruleId": "proposal.revision",
                            "currentRevision": current_revision,
                            "remediation": "Create a new proposal from the current workspace revision.",
                            "operation": operation,
                        })
                        return
                    except Exception as exc:
                        operation = CONTROL.finish_operation(
                            operation["id"],
                            status="indeterminate",
                            error={
                                "code": "proposal.apply_interrupted",
                                "message": (
                                    "Proposal apply did not record a terminal "
                                    f"result: {exc}"
                                ),
                            },
                        )
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                            "error": (
                                "Proposal apply did not record a terminal result. "
                                "Inspect the proposal, workspace revision, and XYZ "
                                "status before retrying."
                            ),
                            "operation": operation,
                        })
                        return
                operation = CONTROL.finish_operation(
                    operation["id"],
                    status="succeeded" if completed else "indeterminate",
                    result=(
                        {"proposal": proposal, "reload": reload_result}
                        if completed
                        else None
                    ),
                    error=(
                        None
                        if completed
                        else {
                            "code": "proposal.apply_reload_timeout",
                            "message": "Proposal committed but reload completion was not observed.",
                        }
                    ),
                )
                CONTROL.audit(
                    "proposal.applied",
                    actor=actor,
                    remote=self._remote(),
                    details={"id": proposal["id"], "reloadCompleted": completed},
                )
                self._json(
                    HTTPStatus.OK if completed else HTTPStatus.GATEWAY_TIMEOUT,
                    {
                        **(
                            {}
                            if completed
                            else {
                                "error": "Proposal applied, but XYZ reload did not complete."
                            }
                        ),
                        "proposal": proposal,
                        "reload": reload_result,
                        "operation": operation,
                    },
                )
                return
            if (
                proposal_action_path
                and proposal_action_path.group(2) == "decline"
            ):
                with PROPOSAL_LOCK:
                    proposal = proposal_read(
                        CONTROL,
                        proposal_action_path.group(1),
                    )
                    if proposal["status"] != "pending":
                        self._json(HTTPStatus.CONFLICT, {"error": f"Proposal is {proposal['status']}."})
                        return
                    proposal["status"] = "declined"
                    proposal["declineReason"] = payload.get("reason")
                    proposal_write(CONTROL, proposal)
                CONTROL.audit("proposal.declined", actor=actor, remote=self._remote(), details={"id": proposal["id"]})
                self._json(HTTPStatus.OK, {"proposal": proposal})
                return
            candidate = payload.get("workspace")
            if request_path == "/api/expression-test":
                try:
                    result = test_info_expression(
                        candidate,
                        payload.get("locale", "locale"),
                        payload.get("layer"),
                        int(payload.get("index")),
                    )
                    self._json(HTTPStatus.OK, result)
                except (TypeError, ValueError, psycopg.Error) as exc:
                    self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                        "error": "Expression test failed.",
                        "errors": [{"path": "fieldfx", "message": str(exc)}],
                    })
                return
            expected = payload.get("revision")
            _, current_workspace, _ = read_workspace()
            errors = validate_candidate(candidate, current_workspace)
            if errors:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Validation failed.", "errors": annotated(errors)})
                return
            if request_path == "/api/validate":
                _, warnings = semantic_publication_diagnostics(
                    candidate,
                    current_workspace,
                )
                self._json(HTTPStatus.OK, {
                    "message": "Configuration is valid.",
                    "semanticWarnings": annotated(warnings),
                })
                return
            try:
                encoded, next_revision, fingerprint, reload_result = save_and_reload(
                    candidate,
                    expected,
                )
            except FileExistsError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            schedule_live_preview_sync(encoded)
            completed = reload_completed(reload_result)
            CONTROL.audit(
                "workspace.saved",
                actor=actor,
                remote=self._remote(),
                details={"reloadCompleted": completed},
            )
            self._json(
                HTTPStatus.OK if completed else HTTPStatus.GATEWAY_TIMEOUT,
                {
                    **(
                        {"message": "Workspace saved and XYZ reloaded."}
                        if completed
                        else {
                            "error": "Workspace saved, but XYZ reload did not complete."
                        }
                    ),
                    "workspace": candidate,
                    "revision": next_revision,
                    "fingerprint": fingerprint,
                    "saved": True,
                    "reload": reload_result,
                    "semanticWarnings": annotated(
                        semantic_publication_diagnostics(
                            candidate,
                            current_workspace,
                        )[1]
                    ),
                },
            )
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Request body is not valid JSON."})
        except DerivedLayerDependencyError as exc:
            operation = (
                "replace"
                if derived_action_path and derived_action_path.group(2) == "replace"
                else "drop"
            )
            response = derived_exception_response(
                derived_dependency_error(exc, operation),
                exc,
                operation,
                derived_failure_phase or "preflight",
            )
            self._json(
                derived_failure_http_status(response, HTTPStatus.CONFLICT),
                response,
            )
        except DerivedLayerMaintenanceError as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            response = derived_exception_response(
                derived_maintenance_error(exc, derived_operation),
                exc,
                derived_operation,
                derived_failure_phase or "preflight",
            )
            self._json(
                derived_failure_http_status(response, HTTPStatus.CONFLICT),
                response,
            )
        except DerivedLayerContentionError as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            response = derived_exception_response(
                derived_contention_error(exc, derived_operation),
                exc,
                derived_operation,
                derived_failure_phase or "preflight",
            )
            self._json(
                derived_failure_http_status(response, HTTPStatus.CONFLICT),
                response,
            )
        except DerivedLayerBackgroundCapacityError as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            message = str(exc)
            self._json(
                HTTPStatus.TOO_MANY_REQUESTS,
                derived_blocked_error(
                    code="derived_layer.background_capacity",
                    message=message,
                    suggested_action=(
                        "Wait for the active derived-layer operation to finish, "
                        "then retry the same request."
                    ),
                    operation=derived_operation,
                    retryable=True,
                    activeJobs=exc.active_jobs,
                    maxActiveJobs=exc.max_active_jobs,
                ),
            )
        except DerivedLayerQueryTooExpensive as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            response = derived_exception_response(
                derived_query_too_expensive_error(exc, derived_operation),
                exc,
                derived_operation,
                derived_failure_phase or "preflight",
            )
            self._json(
                derived_failure_http_status(
                    response, derived_query_error_status(exc),
                ),
                response,
            )
        except DerivedLayerMaterializationTooLarge as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            response = derived_exception_response(
                derived_materialization_too_large_error(
                    exc, derived_operation,
                ),
                exc,
                derived_operation,
                derived_failure_phase or "preflight",
            )
            self._json(
                derived_failure_http_status(response, HTTPStatus.CONFLICT),
                response,
            )
        except DerivedLayerSourceMismatchError as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            response = derived_exception_response(
                derived_source_mismatch_error(exc, derived_operation),
                exc,
                derived_operation,
                derived_failure_phase or "preflight",
            )
            self._json(
                derived_failure_http_status(
                    response, HTTPStatus.UNPROCESSABLE_ENTITY,
                ),
                response,
            )
        except DerivedLayerDatabaseOperationError as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            if derived_operation is not None:
                response = derived_database_error(
                    exc.cause,
                    derived_operation,
                    failure_phase=exc.failure_phase,
                    state_unchanged=exc.state_unchanged,
                    rolled_back=exc.rolled_back,
                    indeterminate=exc.indeterminate,
                )
                self._json(
                    derived_failure_http_status(
                        response, derived_database_error_status(response),
                    ),
                    response,
                )
            else:
                self._json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"error": "The database could not complete the request."},
                )
        except DerivedLayerError as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            if derived_operation is not None:
                response = derived_validation_error(exc, derived_operation)
                response = derived_exception_response(
                    response,
                    exc,
                    derived_operation,
                    derived_failure_phase or "preflight",
                )
                self._json(
                    derived_failure_http_status(
                        response, derived_validation_error_status(response),
                    ),
                    response,
                )
            else:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except VisualPlanningNoMatchingFeatures as exc:
            response = {
                "error": str(exc),
                "code": exc.code,
                "planningStage": exc.stage,
                "queryPurpose": (
                    "filtered-feature-count-and-extent"
                    if exc.stage == "layer-summary"
                    else "representative-feature-selection"
                ),
                "defaultFilterApplied": exc.filter_applied,
                "filteredFeatureCount": 0,
                "representativeFeature": None,
                "reason": exc.reason,
            }
            if exc.effective_dataset is not None:
                response["effectiveDataset"] = exc.effective_dataset
                response["filteredFeatureCount"] = exc.effective_dataset.get(
                    "filteredFeatureCount", 0
                )
            if visual_planning_active and visual_operation is not None:
                response = visual_planning_failure_response(
                    visual_operation, response,
                )
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, response)
        except ValueError as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            if derived_operation is not None:
                phase, rolled_back = derived_exception_failure_state(
                    exc, derived_failure_phase or "preflight",
                )
                response = derived_operation_failed_error(
                    derived_operation,
                    failure_phase=phase,
                    state_unchanged=phase == "preflight" or rolled_back,
                    rolled_back=rolled_back,
                )
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, response)
            elif visual_planning_active and visual_operation is not None:
                response = visual_planning_failure_response(
                    visual_operation,
                    {
                        "error": str(exc),
                        "code": "visual.planning_invalid",
                        "planningStage": "request-planning",
                    },
                )
                self._json(HTTPStatus.BAD_REQUEST, response)
            else:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except FileExistsError as exc:
            if request_path == "/api/derived-layers":
                response = derived_exception_response(
                    derived_already_exists_error(str(exc)),
                    exc,
                    "create",
                    derived_failure_phase or "preflight",
                )
                self._json(
                    derived_failure_http_status(
                        response, HTTPStatus.CONFLICT,
                    ),
                    response,
                )
            elif visual_planning_active and visual_operation is not None:
                response = visual_planning_failure_response(
                    visual_operation,
                    {
                        "error": str(exc),
                        "code": "visual.planning_conflict",
                        "planningStage": "request-planning",
                    },
                )
                self._json(HTTPStatus.CONFLICT, response)
            else:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except FileNotFoundError as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            if derived_operation is not None:
                response = derived_exception_response(
                    derived_not_found_error(str(exc), derived_operation),
                    exc,
                    derived_operation,
                    derived_failure_phase or "preflight",
                )
                self._json(
                    derived_failure_http_status(
                        response, HTTPStatus.NOT_FOUND,
                    ),
                    response,
                )
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except VisualPlanningDatabaseError as exc:
            response = {
                "error": str(exc),
                "code": (
                    "visual.planning_timeout"
                    if exc.timed_out
                    else "visual.planning_database_error"
                ),
                "planningStage": exc.stage,
                "queryPurpose": exc.query_purpose,
            }
            if exc.timed_out:
                response["timeoutMilliseconds"] = exc.timeout_milliseconds
            if visual_planning_active and visual_operation is not None:
                response = visual_planning_failure_response(
                    visual_operation, response,
                )
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, response)
        except psycopg.Error as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            if derived_operation is not None:
                phase, _ = derived_exception_failure_state(
                    exc, derived_failure_phase or "preflight",
                )
                state_unchanged = phase == "preflight"
                response = derived_database_error(
                    exc,
                    derived_operation,
                    failure_phase=phase,
                    state_unchanged=state_unchanged,
                    indeterminate=not state_unchanged,
                )
                self._json(
                    derived_failure_http_status(
                        response,
                        (
                            derived_database_error_status(response)
                            if state_unchanged
                            else HTTPStatus.INTERNAL_SERVER_ERROR
                        ),
                    ),
                    response,
                )
            else:
                self._json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"error": "The database could not complete the request."},
                )
        except SemanticClientError as exc:
            if (
                request_path
                == "/api/derived-layers/recipes/area-weighted-h3/plan"
            ):
                status = exc.status or HTTPStatus.SERVICE_UNAVAILABLE
                if status < 400 or status > 599:
                    status = HTTPStatus.BAD_GATEWAY
                if status == HTTPStatus.UNAUTHORIZED:
                    status = HTTPStatus.BAD_GATEWAY
                missing = status == HTTPStatus.NOT_FOUND
                response = derived_blocked_error(
                    code=(
                        "derived_layer.source_profile_required"
                        if missing
                        else "derived_layer.source_profile_unavailable"
                    ),
                    message=(
                        "The requested semantic source profile does not exist."
                        if missing
                        else "The semantic source profile could not be verified."
                    ),
                    suggested_action=(
                        "Synchronize the source with `semantic source sync`, "
                        "then retry the recipe plan."
                        if missing
                        else "Restore semantic profile access, then retry the recipe plan."
                    ),
                    operation="plan-area-weighted-h3",
                    mutationApplied=False,
                )
                self._json(status, response)
            else:
                self._semantic_error(exc)
        except Exception as exc:
            derived_operation = derived_request_operation(
                request_path, derived_action_path,
            )
            if derived_operation is not None:
                phase, rolled_back = derived_exception_failure_state(
                    exc, derived_failure_phase or "preflight",
                )
                state_unchanged = phase == "preflight" or rolled_back
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    derived_operation_failed_error(
                        derived_operation,
                        failure_phase=phase,
                        state_unchanged=state_unchanged,
                        rolled_back=rolled_back,
                    ),
                )
            elif visual_planning_active and visual_operation is not None:
                response = visual_planning_failure_response(
                    visual_operation,
                    {
                        "error": "Visual planning was interrupted.",
                        "code": "visual.planning_interrupted",
                        "planningStage": "request-planning",
                    },
                )
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, response)
            else:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


if __name__ == "__main__":
    schedule_live_preview_sync()
    threading.Thread(
        target=run_semantic_outbox,
        name="semantic-outbox",
        daemon=True,
    ).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
