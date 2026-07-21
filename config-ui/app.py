from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import secrets
import tempfile
import threading
import time
from datetime import date, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import psycopg
from psycopg.rows import dict_row

from infoj_types import info_value_error
from derived_layers import (
    DerivedLayerDependencyError,
    DerivedLayerError,
    DerivedLayerStore,
)
from static_files import safe_static_path
from svg_icons import safe_svg
from workspace_schema import expression_function_names, validate_workspace
from control_plane import ControlStore
from control_api import (
    PROPOSAL_LOCK, RULES, apply_operations, apply_visual_override, capabilities, contract, examples,
    effective_locales, is_probeable_database_layer,
    pointer_get, pointer_parts, proposal_check, proposal_create, proposal_list, proposal_read, proposal_write,
    reload_status, reload_timeout, request_reload,
    schema as contract_schema, select_locale, visual_plan,
    strict_json_loads, wait_reload, workspace_fingerprint, workspace_hash,
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
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = 5 * 1024 * 1024
SAVE_LOCK = threading.Lock()
SAVE_RELOAD_LOCK = threading.Lock()
PREVIEW_LOCK = threading.RLock()
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
) -> None:
    """Complete a derived-layer database operation after HTTP has returned."""
    try:
        if not DERIVED:
            raise DerivedLayerError(
                "Derived-layer database management is not configured."
            )
        if action == "create":
            result = DERIVED.create(payload, actor)
        elif action == "replace" and name:
            result = DERIVED.replace(name, payload, actor)
            changes = result.get("columnChanges", {})
            result.update(derived_workspace_impact(
                name,
                changes.get("removed", []) + changes.get("changed", []),
            ))
        elif action == "refresh" and name:
            result = DERIVED.refresh(name)
        else:
            raise DerivedLayerError("Unsupported background operation.")
        CONTROL.audit(
            f"derived_layer.{action}d" if action != "refresh" else "derived_layer.refreshed",
            actor=actor,
            remote=remote,
            details={
                "name": result["name"],
                "kind": result["kind"],
                "sources": result["sources"],
                "operationId": operation_id,
            },
        )
        CONTROL.finish_operation(
            operation_id,
            status="succeeded",
            result={"derivedLayer": result},
        )
    except Exception as exc:
        CONTROL.finish_operation(
            operation_id,
            status="failed",
            error={
                "code": "derived_layer.background_failed",
                "message": str(exc),
                "type": type(exc).__name__,
            },
        )


def start_derived_background(
    action: str,
    payload: dict,
    actor: str,
    remote: str,
    name: str | None = None,
) -> dict:
    operation = CONTROL.create_operation(
        f"derived-layer.{action}",
        actor,
        {"name": name or payload.get("name"), "action": action},
    )
    threading.Thread(
        target=run_derived_background,
        args=(operation["id"], action, payload, actor, remote, name),
        name=f"derived-{action}-{operation['id'][:8]}",
        daemon=True,
    ).start()
    return operation


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


def proposal_changes_feature_info(
    proposal: dict,
    layer_key: str,
    locale_key: str,
) -> bool:
    """Whether this proposal changes feature information for the rendered layer."""
    resolved_layers = False
    try:
        _, original_locale = select_locale(proposal.get("original"), locale_key)
        _, candidate_locale = select_locale(proposal.get("candidate"), locale_key)
        original_layer = (original_locale.get("layers") or {}).get(layer_key)
        candidate_layer = (candidate_locale.get("layers") or {}).get(layer_key)
        resolved_layers = True
        if (
            isinstance(original_layer, dict)
            and isinstance(candidate_layer, dict)
            and original_layer.get("infoj") != candidate_layer.get("infoj")
        ):
            return True
    except (AttributeError, TypeError, ValueError):
        # Retain path-based detection for an older or incomplete proposal
        # record; preview integrity checks still gate rendering separately.
        pass
    if resolved_layers:
        return False
    prefixes = [["locale", "layers", layer_key, "infoj"]]
    if locale_key != "locale":
        prefixes.append(["locales", locale_key, "layers", layer_key, "infoj"])
    for item in proposal.get("diff") or []:
        try:
            parts = pointer_parts(item.get("path"))
        except (TypeError, ValueError):
            continue
        if any(parts[:len(prefix)] == prefix for prefix in prefixes):
            return True
    return False


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
    return proposal


def run_browser_visual(layer_key: str | None, plan: dict, payload: dict, *,
                       target_url: str) -> tuple[int, dict]:
    runner_payload = json.dumps(
        {
            "url": target_url,
            "layer": layer_key,
            "layers": plan.get("layers", [layer_key]),
            "plan": plan,
            "viewport": payload.get("viewport", {"width": 1920, "height": 1080}),
            "deviceScaleFactor": payload.get("deviceScaleFactor", 2),
            "fullPage": payload.get("fullPage", True),
            "viewMode": payload.get("viewMode", "focus"),
            "metadata": payload.get("metadata"),
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
        ), timeout=60) as response:
            return HTTPStatus.OK, json.load(response)
    except HTTPError as exc:
        try:
            result = strict_json_loads(exc.read())
        except (OSError, UnicodeError, ValueError):
            result = None
        if exc.code == HTTPStatus.UNPROCESSABLE_ENTITY and isinstance(result, dict):
            return HTTPStatus.UNPROCESSABLE_ENTITY, result
        if exc.code == HTTPStatus.TOO_MANY_REQUESTS:
            return HTTPStatus.TOO_MANY_REQUESTS, {
                "error": (
                    result.get("error") if isinstance(result, dict) else None
                ) or "Visual runner is busy. Retry later."
            }
        return HTTPStatus.BAD_GATEWAY, {
            "error": f"Browser validation service returned HTTP {exc.code}."
        }
    except Exception as exc:
        return HTTPStatus.BAD_GATEWAY, {
            "error": f"Browser validation failed: {exc}"
        }


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
    return [table for table in discover() if table.get("schema") != "public"]


def layer_db(data: dict, layer: dict) -> str | None:
    return layer.get("dbs") or data.get("dbs")


def locale_items(data: dict) -> list[tuple[str, dict]]:
    return [
        (
            "locale" if key == "locale" else f"locales.{key}",
            locale,
        )
        for key, locale in effective_locales(data).items()
    ]


def validate_catalog(data: dict, tables: list[dict]) -> list[dict[str, str]]:
    errors = []
    index = {(table["dbs"], f'{table["schema"]}.{table["table"]}'): table for table in tables}
    for locale_path, locale in locale_items(data):
        if not isinstance(locale, dict):
            continue
        for key, layer in (locale.get("layers") or {}).items():
            if not is_probeable_database_layer(layer):
                continue
            path = f"{locale_path}.layers.{key}"
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
                if isinstance(entry, dict) and not entry.get("fieldfx") and entry.get("field") not in columns:
                    errors.append({"path": f"{path}.infoj.{index_number}.field", "message": "Must select a column from this table or provide a trusted SQL expression."})
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


def validate_candidate(candidate) -> list[dict[str, str]]:
    errors = validate_workspace(candidate, set(DB_CONNECTIONS))
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
        if "fieldfx" in path:
            rule, phase = "sql.scalar_read_only", "security"
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
            "severity": "error",
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
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            token = CONTROL.authenticate_token(authorization[7:], self._remote())
            if not token:
                return None
            actor = f"token:{token['id']}"
            self._authentication = {
                "actor": actor,
                "scopes": token.get("scopes") or [],
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
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required."})
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

    @staticmethod
    def _required_scope(path: str, method: str) -> str | None:
        if method == "GET":
            if path.startswith("/api/operations/"):
                return None
            if path.startswith("/api/artifacts/"):
                return "visual"
            return "inspect"
        if path in {"/api/visual-plan", "/api/visual-test"} or re.fullmatch(
            r"/api/proposals/[^/]+/(visual-plan|visual-test|screenshot)", path
        ):
            return "visual"
        if path.endswith("/apply"):
            return "apply"
        if path == "/api/xyz/reload":
            return "reload"
        if path == "/api/derived-layers" or path.startswith("/api/derived-layers/"):
            return "derive" if method != "GET" else "inspect"
        if path in {"/api/proposals", "/api/proposals/check"} or path.endswith("/decline"):
            return "propose"
        return "full"

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
                "contractVersion": "1.0",
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
                self._json(
                    HTTPStatus.OK,
                    {"workspace": data, "revision": current_revision},
                )
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
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
                self._json(HTTPStatus.OK, {
                    "databases": sorted(DB_CONNECTIONS),
                    "tables": discover_catalog(),
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
                        "h3Available": False,
                    }
                )
                self._json(HTTPStatus.OK, result)
            except (DerivedLayerError, psycopg.Error) as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
        elif path == "/api/derived-layers":
            try:
                if not DERIVED:
                    raise DerivedLayerError(
                        "Derived-layer database management is not configured."
                    )
                self._json(HTTPStatus.OK, {"derivedLayers": DERIVED.list()})
            except (DerivedLayerError, psycopg.Error) as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
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
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except (DerivedLayerError, psycopg.Error) as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
        elif path == "/api/icons":
            self._json(HTTPStatus.OK, {"icons": discover_icons()})
        elif path == "/api/contract":
            self._json(HTTPStatus.OK, contract(CONTROL.instance_id()))
        elif path == "/api/capabilities":
            self._json(HTTPStatus.OK, capabilities(CONTROL.instance_id()))
        elif path == "/api/schema":
            query = parse_qs(urlparse(self.path).query)
            try:
                self._json(HTTPStatus.OK, {"schema": contract_schema(query.get("pointer", [None])[0])})
            except (KeyError, IndexError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        elif path == "/api/rules":
            category = parse_qs(urlparse(self.path).query).get("category", [None])[0]
            self._json(HTTPStatus.OK, {"rules": [rule for rule in RULES if not category or rule["category"] == category]})
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
            self._json(HTTPStatus.OK, {"proposals": proposal_list(CONTROL)})
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
                    payload.get("scopes", ["inspect", "propose", "visual"]),
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
        allowed = {
            "/api/workspace", "/api/validate", "/api/expression-test", "/api/mutate",
            "/api/proposals", "/api/proposals/check", "/api/xyz/reload", "/api/visual-plan",
            "/api/visual-test",
            "/api/admin/tokens", "/api/admin/password", "/api/auth/logout",
            "/api/admin/device-authorizations/approve",
            "/api/sql/test",
            "/api/derived-layers",
        }
        derived_action_path = re.fullmatch(
            r"/api/derived-layers/([a-z][a-z0-9_]{0,62})/(refresh|replace|drop)",
            request_path,
        )
        proposal_visual_path = re.fullmatch(
            r"/api/proposals/([A-Za-z0-9._-]+)/(visual-plan|visual-test|screenshot)",
            request_path,
        )
        if (
            request_path not in allowed
            and not request_path.endswith(("/apply", "/decline", "/revoke"))
            and not proposal_visual_path
            and not derived_action_path
        ):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._payload()
            if request_path == "/api/derived-layers":
                if not DERIVED:
                    raise DerivedLayerError(
                        "Derived-layer database management is not configured."
                    )
                background = payload.pop("background", False)
                if background is True:
                    operation = start_derived_background(
                        "create", payload, actor, self._remote()
                    )
                    self._json(HTTPStatus.ACCEPTED, {
                        "operation": operation,
                        "statusUrl": f"/api/operations/{operation['id']}",
                    })
                    return
                result = DERIVED.create(payload, actor)
                CONTROL.audit(
                    "derived_layer.created",
                    actor=actor,
                    remote=self._remote(),
                    details={
                        "name": result["name"],
                        "kind": result["kind"],
                        "sources": result["sources"],
                    },
                )
                self._json(HTTPStatus.CREATED, {"derivedLayer": result})
                return
            if derived_action_path:
                if not DERIVED:
                    raise DerivedLayerError(
                        "Derived-layer database management is not configured."
                    )
                name, action = derived_action_path.groups()
                if payload.get("confirmed") is not True:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": (
                            "Please confirm this derived-layer change before "
                            "it is applied."
                        ),
                        "code": "derived_layer.confirmation_required",
                        "suggestedAction": (
                            "Review the change, then retry with confirmation."
                        ),
                    })
                    return
                background = payload.pop("background", False)
                if background is True and action in {"refresh", "replace"}:
                    replacement = {**payload}
                    replacement.pop("confirmed", None)
                    operation = start_derived_background(
                        action,
                        replacement if action == "replace" else {},
                        actor,
                        self._remote(),
                        name,
                    )
                    self._json(HTTPStatus.ACCEPTED, {
                        "operation": operation,
                        "statusUrl": f"/api/operations/{operation['id']}",
                    })
                    return
                if action == "refresh":
                    result = DERIVED.refresh(name)
                    event = "derived_layer.refreshed"
                elif action == "replace":
                    replacement = {**payload}
                    replacement.pop("confirmed", None)
                    result = DERIVED.replace(name, replacement, actor)
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
                        reasons = []
                        if workspace_references:
                            reasons.append("workspace_references")
                        if dependents:
                            reasons.append("postgresql_dependents")
                        self._json(HTTPStatus.CONFLICT, {
                            "error": (
                                f'The derived layer “{name}” cannot be deleted '
                                "because it is still in use."
                            ),
                            "userMessage": (
                                f'The derived layer “{name}” cannot be deleted '
                                "because it is still in use. Nothing was changed."
                            ),
                            "suggestedAction": (
                                "Remove it from the listed map layers and "
                                "database views, then try again."
                            ),
                            "code": "derived_layer.in_use",
                            "operation": "drop",
                            "blocked": True,
                            "reasons": reasons,
                            "name": name,
                            "dependents": dependents,
                            "workspaceReferences": workspace_references,
                            "consumerLabels": workspace_impact["consumerLabels"],
                            "dropped": False,
                        })
                        return
                    result = DERIVED.drop(name)
                    event = "derived_layer.dropped"
                CONTROL.audit(
                    event,
                    actor=actor,
                    remote=self._remote(),
                    details={"name": name, "kind": result["kind"]},
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
                raw, token = CONTROL.create_token(
                    payload.get("name", "CLI token"),
                    payload.get("expires"),
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
            if request_path.endswith("/revoke"):
                if actor != "admin":
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Administrator session required."})
                    return
                token_id = request_path.split("/")[-2]
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
                plan_source = (
                    "candidate"
                    if group_preview["candidate"]["requestedLayerPresent"]
                    else "original"
                )
                plan_selection = group_preview[plan_source]
                plan = apply_visual_override(
                    visual_plan(
                        proposal[plan_source],
                        plan_selection["anchorLayer"],
                        DB_CONNECTIONS,
                        group_preview["locale"],
                    ),
                    payload,
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
                    "viewSource": plan_source,
                    "viewMode": view_mode,
                })
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
                    and proposal_changes_feature_info(
                        proposal,
                        layer_key,
                        plan.get("locale", "locale"),
                    )
                )
                render_plan = dict(plan)
                original_render_plan = {
                    **render_plan,
                    "anchorLayer": group_preview["original"]["anchorLayer"],
                    "layers": group_preview["original"]["layers"],
                    "activeGroups": group_preview["original"]["groups"],
                }
                candidate_render_plan = {
                    **render_plan,
                    "anchorLayer": group_preview["candidate"]["anchorLayer"],
                    "layers": group_preview["candidate"]["layers"],
                    "activeGroups": group_preview["candidate"]["groups"],
                }
                if group_preview["changeKind"] != "edited":
                    original_render_plan.pop("interaction", None)
                    candidate_render_plan.pop("interaction", None)
                render_payload = dict(payload)
                render_payload["viewMode"] = view_mode
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
                        interaction = {
                            **(render_plan.get("interaction") or {}),
                            "requireInfoPanel": True,
                        }
                        original_render_plan["interaction"] = interaction
                        candidate_render_plan["interaction"] = interaction
                    else:
                        # A proposal screenshot compares configuration states.
                        # Selection would obscure point-style changes, so it is
                        # reserved for feature-information comparisons.
                        original_render_plan.pop("interaction", None)
                        candidate_render_plan.pop("interaction", None)
                operation = CONTROL.create_operation(
                    (
                        "proposal.screenshot"
                        if action == "screenshot"
                        else "proposal.visual-test"
                    ),
                    actor,
                    {
                        **binding,
                        "layer": layer_key,
                        "groups": group_preview["groups"],
                        "originalLayers": group_preview["original"]["layers"],
                        "candidateLayers": group_preview["candidate"]["layers"],
                        "featureInfoComparison": feature_info_comparison,
                    },
                )
                comparison_binding_valid = True
                try:
                    # Keep the isolated workspace pinned throughout the
                    # original/candidate comparison so another proposal cannot
                    # replace either side while Chromium is rendering it.
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
                            }
                            original_preview = prepare_original_preview(proposal)
                            original_status, original_result = run_browser_visual(
                                group_preview["original"]["renderLayer"],
                                original_render_plan,
                                {
                                    **render_payload,
                                    "metadata": original_binding,
                                },
                                target_url=target_url,
                            )
                        candidate_preview = prepare_candidate_preview(proposal)
                        status, candidate_result = run_browser_visual(
                            group_preview["candidate"]["renderLayer"],
                            candidate_render_plan,
                            {
                                **render_payload,
                                "metadata": binding,
                            },
                            target_url=target_url,
                        )
                    if action == "screenshot":
                        if (
                            not isinstance(original_result, dict)
                            or not isinstance(candidate_result, dict)
                        ):
                            result = None
                        else:
                            comparison_binding_valid = (
                                original_result.get("metadata")
                                == original_binding
                                and candidate_result.get("metadata") == binding
                            )
                            original_artifacts = original_result.get("artifacts") or {}
                            candidate_artifacts = candidate_result.get("artifacts") or {}
                            page_key = (
                                "afterPage"
                                if feature_info_comparison
                                else "beforePage"
                            )
                            map_key = (
                                "afterMap"
                                if feature_info_comparison
                                else "beforeMap"
                            )
                            comparison_artifacts = {
                                "beforePage": original_artifacts.get(page_key),
                                "beforeMap": original_artifacts.get(map_key),
                                "afterPage": candidate_artifacts.get(page_key),
                                "afterMap": candidate_artifacts.get(map_key),
                                "beforeReport": original_artifacts.get("report"),
                                "afterReport": candidate_artifacts.get("report"),
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
                            statuses = {original_status, status}
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
                            result = {
                                "runId": candidate_result.get("runId"),
                                "passed": status == HTTPStatus.OK,
                                "metadata": binding,
                                "comparison": {
                                    "before": "original",
                                    "after": "candidate",
                                    "featureInfoPanel": feature_info_comparison,
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
                                },
                            }
                        preview = {
                            "original": original_preview,
                            "candidate": candidate_preview,
                        }
                    else:
                        result = candidate_result
                        preview = candidate_preview
                except TimeoutError as exc:
                    operation = CONTROL.finish_operation(
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
                    operation = CONTROL.finish_operation(
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
                    operation = CONTROL.finish_operation(
                        operation["id"],
                        status="failed",
                        error={
                            "code": "visual.invalid_response",
                            "message": (
                                "Browser validation returned an invalid response."
                            ),
                        },
                    )
                    self._json(HTTPStatus.BAD_GATEWAY, {
                        **binding,
                        "error": "Browser validation returned an invalid response.",
                        "operation": operation,
                    })
                    return
                result_binding = result.get("metadata")
                if (
                    (result.get("runId") or result.get("artifacts"))
                    and (
                        result_binding != binding
                        or not comparison_binding_valid
                    )
                ):
                    operation = CONTROL.finish_operation(
                        operation["id"],
                        status="failed",
                        error={
                            "code": "visual.binding_mismatch",
                            "message": "Browser artifact binding was not preserved.",
                        },
                    )
                    self._json(HTTPStatus.BAD_GATEWAY, {
                        **binding,
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
                operation = CONTROL.finish_operation(
                    operation["id"],
                    status="succeeded" if status == HTTPStatus.OK else "failed",
                    result=response if status == HTTPStatus.OK else None,
                    error=(
                        None
                        if status == HTTPStatus.OK
                        else {
                            "code": "visual.failed",
                            "message": response["error"],
                            "diagnosis": result.get("diagnosis"),
                        }
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
                )
                self._json(
                    HTTPStatus.OK,
                    {"plan": apply_visual_override(plan, payload)},
                )
                return
            if request_path == "/api/visual-test":
                layer_key = payload.get("layer")
                if not isinstance(layer_key, str) or not layer_key.strip():
                    raise ValueError("Visual requests require a layer key.")
                _, current_workspace, _ = read_workspace()
                plan = apply_visual_override(
                    visual_plan(
                        payload.get("workspace", current_workspace),
                        layer_key,
                        DB_CONNECTIONS,
                        payload.get("locale"),
                    ),
                    payload,
                )
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
                    },
                    allow_nan=False,
                ).encode()
                operation = CONTROL.create_operation(
                    "visual.test",
                    actor,
                    {"layer": layer_key, "locale": plan.get("locale")},
                )
                try:
                    with urlopen(Request(
                        os.environ.get("BROWSER_RUNNER_URL", "http://browser-runner:8080/run"),
                        data=runner_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ), timeout=60) as response:
                        result = json.load(response)
                    CONTROL.audit("visual.completed", actor=actor, remote=self._remote(), details={"layer": layer_key, "runId": result.get("runId"), "passed": result.get("passed")})
                    response_payload = {"plan": plan, "visual": result}
                    operation = CONTROL.finish_operation(
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
                        operation = CONTROL.finish_operation(
                            operation["id"],
                            status="failed",
                            error={
                                "code": "visual.failed",
                                "message": "Browser validation did not pass.",
                                "diagnosis": result.get("diagnosis"),
                            },
                        )
                        self._json(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            {
                                "error": "Browser validation did not pass.",
                                "plan": plan,
                                "visual": result,
                                "operation": operation,
                            },
                        )
                    elif exc.code == HTTPStatus.TOO_MANY_REQUESTS:
                        operation = CONTROL.finish_operation(
                            operation["id"],
                            status="failed",
                            error={"code": "visual.busy", "message": "Visual runner is busy."},
                        )
                        self._json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {
                                "error": (
                                    result.get("error")
                                    if isinstance(result, dict)
                                    else None
                                ) or "Visual runner is busy. Retry later.",
                                "plan": plan,
                                "operation": operation,
                            },
                        )
                    else:
                        operation = CONTROL.finish_operation(
                            operation["id"],
                            status="failed",
                            error={
                                "code": "visual.upstream",
                                "message": f"Browser runner returned HTTP {exc.code}.",
                            },
                        )
                        self._json(
                            HTTPStatus.BAD_GATEWAY,
                            {
                                "error": (
                                    "Browser validation service returned "
                                    f"HTTP {exc.code}."
                                ),
                                "plan": plan,
                                "operation": operation,
                            },
                        )
                except Exception as exc:
                    operation = CONTROL.finish_operation(
                        operation["id"],
                        status="failed",
                        error={
                            "code": "visual.upstream",
                            "message": f"Browser validation failed: {exc}",
                        },
                    )
                    self._json(HTTPStatus.BAD_GATEWAY, {
                        "error": f"Browser validation failed: {exc}",
                        "plan": plan,
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
                errors = validate_candidate(candidate)
                if errors:
                    self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Validation failed.", "errors": annotated(errors)})
                    return
                supplied_check = payload.get("checkFingerprint")
                if request_path == "/api/proposals" and supplied_check is not None:
                    checked = proposal_check(
                        current_workspace, expected, candidate,
                        payload.get("operations") or [], diff,
                        payload.get("explanation"),
                    )
                    if (
                        not isinstance(supplied_check, str)
                        or supplied_check != checked["checkFingerprint"]
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
                    self._json(HTTPStatus.OK, {"check": checked})
                elif request_path == "/api/proposals":
                    proposal = proposal_create(CONTROL, current_workspace, expected, candidate, payload.get("operations") or [], diff, actor, payload.get("explanation"))
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
            if request_path.endswith("/apply"):
                if payload.get("approved") is not True:
                    self._json(HTTPStatus.BAD_REQUEST, {
                        "error": "Proposal application requires explicit approval.",
                        "code": "proposal.approval_required",
                    })
                    return
                with PROPOSAL_LOCK:
                    proposal = proposal_read(CONTROL, request_path.split("/")[-2])
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
                        errors = validate_candidate(proposal["candidate"])
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
            if request_path.endswith("/decline"):
                with PROPOSAL_LOCK:
                    proposal = proposal_read(CONTROL, request_path.split("/")[-2])
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
            errors = validate_candidate(candidate)
            if errors:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Validation failed.", "errors": annotated(errors)})
                return
            if request_path == "/api/validate":
                self._json(HTTPStatus.OK, {"message": "Configuration is valid."})
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
            action = "edited" if operation == "replace" else "deleted"
            columns = sorted(set(exc.removed_columns) & set(exc.dependent_columns))
            column_message = (
                " The database uses these affected fields: "
                + ", ".join(f"“{column}”" for column in columns) + "."
                if columns else ""
            )
            message = (
                f'The derived layer “{exc.name}” cannot be {action} because '
                f"other database views or objects use it.{column_message} "
                "Nothing was changed."
            )
            self._json(HTTPStatus.CONFLICT, {
                "error": message,
                "userMessage": message,
                "suggestedAction": (
                    "Update or remove the dependent database views first, "
                    "then try again."
                ),
                "code": "derived_layer.in_use",
                "operation": operation,
                "blocked": True,
                "reasons": ["postgresql_dependents"],
                "name": exc.name,
                "dependents": exc.dependents,
                "removedColumns": exc.removed_columns,
                "dependentColumns": exc.dependent_columns,
                "workspaceReferences": derived_workspace_references(exc.name),
                "requiresSecondOrderChanges": bool(
                    exc.removed_columns or exc.dependent_columns
                ),
                "dropped": False,
            })
        except (DerivedLayerError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except FileExistsError as exc:
            if request_path == "/api/derived-layers":
                self._json(HTTPStatus.CONFLICT, {
                    "error": (
                        f'A derived layer named “{exc}” already exists. '
                        "Choose a different name or edit the existing layer."
                    ),
                    "code": "derived_layer.already_exists",
                })
            else:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except psycopg.Error as exc:
            self._json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "error": (
                        "The database could not apply this derived-layer "
                        "change. Check the query, source tables, and selected "
                        "ID and geometry fields, then try again."
                    ),
                    "code": "derived_layer.database_error",
                    "technicalDetail": str(exc),
                },
            )
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


if __name__ == "__main__":
    schedule_live_preview_sync()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
