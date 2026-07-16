from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import psycopg
from psycopg.rows import dict_row

from infoj_types import info_value_error
from static_files import safe_static_path
from svg_icons import safe_svg
from workspace_schema import expression_function_names, validate_workspace
from control_plane import ControlStore
from control_api import (
    PROPOSAL_LOCK, RULES, apply_operations, apply_visual_override, contract, examples,
    effective_locales, is_probeable_database_layer,
    pointer_get, proposal_create, proposal_list, proposal_read, proposal_write,
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
CONTROL = ControlStore(
    Path(os.environ.get("CONTROL_DIR", str(LOCAL_RUNTIME / "control")))
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


def revision(raw: bytes) -> str:
    # Include the file generation so an identical intervening save is still
    # detected as a stale browser revision.
    generation = str(WORKSPACE.stat().st_mtime_ns).encode()
    return hashlib.sha256(raw + b":" + generation).hexdigest()


def read_workspace() -> tuple[bytes, dict]:
    raw = WORKSPACE.read_bytes()
    return raw, strict_json_loads(raw)


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
        current, _ = read_workspace()
        if not isinstance(expected, str) or expected != revision(current):
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
    return encoded, revision(encoded)


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
        raw, current_workspace = read_workspace()
        current_revision = revision(raw)
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
                raw, current_workspace = read_workspace()
                current_revision = revision(raw)
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
             COALESCE(gc.type, '') AS geometry_type,
             gc.srid,
             NOT a.attnotnull AS nullable,
             COALESCE(ix.is_primary, false) AS primary_key,
             COALESCE(ix.is_unique, false) AS unique_key
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
      LEFT JOIN geometry_columns gc
        ON gc.f_table_schema = n.nspname AND gc.f_table_name = c.relname
       AND gc.f_geometry_column = a.attname
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
                geometry_type = geom["geometryType"]
                if geometry_type == "GEOMETRY":
                    geometry_type = next(
                        (column["geometryType"] for column in table["columns"]
                         if column["geometryType"] not in ("", "GEOMETRY")),
                        geometry_type,
                    )
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
                                    errors.append({
                                        "path": f"{path}.infoj.{index_number}.{'fieldfx' if entry.get('fieldfx') else 'field'}",
                                        "message": message,
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
                            errors.append({
                                "path": f"{path}.infoj.{match.group(1)}.fieldfx",
                                "message": match.group(2),
                            })
                        else:
                            errors.append({"path": path, "message": f"XYZ database render probe failed: {exc}"})
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
        output.append({**error, "ruleId": rule, "phase": phase})
    return output


class Handler(SimpleHTTPRequestHandler):
    server_version = "MAPPConfig/1.0"

    def translate_path(self, path):
        candidate = safe_static_path(STATIC_ROOT, path)
        return str(candidate or STATIC_ROOT / ".not-found")

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}")

    def _json(self, status, payload):
        body = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
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

    def _authorized(self, *, state_change=False):
        actor = self._actor(state_change=state_change)
        if not actor:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required."})
            return None
        return actor

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
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
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
        if path.startswith("/api/") and path != "/api/auth/login":
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
                raw, data = read_workspace()
                self._json(HTTPStatus.OK, {"workspace": data, "revision": revision(raw)})
            except Exception as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
        elif path == "/api/catalog":
            try:
                self._json(HTTPStatus.OK, {"databases": sorted(DB_CONNECTIONS), "tables": discover()})
            except Exception as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Database discovery failed: {exc}"})
        elif path == "/api/icons":
            self._json(HTTPStatus.OK, {"icons": discover_icons()})
        elif path == "/api/contract":
            self._json(HTTPStatus.OK, contract(CONTROL.instance_id()))
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
        request_path = urlparse(self.path).path
        if not self._host_allowed():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Unrecognized Host header."})
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
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                secure = "; Secure" if SECURE_COOKIES else ""
                self.send_header("Set-Cookie", f"mapp_session={session}; Path=/; HttpOnly; SameSite=Strict{secure}; Max-Age=43200")
                body = json.dumps(
                    {"authenticated": True, "csrfToken": csrf},
                    allow_nan=False,
                ).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        actor = self._authorized(state_change=True)
        if not actor:
            return
        allowed = {
            "/api/workspace", "/api/validate", "/api/expression-test", "/api/mutate",
            "/api/proposals", "/api/xyz/reload", "/api/visual-plan",
            "/api/visual-test",
            "/api/admin/tokens", "/api/admin/password", "/api/auth/logout",
            "/api/sql/test",
        }
        if request_path not in allowed and not request_path.endswith(("/apply", "/decline", "/revoke")):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._payload()
            if request_path == "/api/auth/logout":
                CONTROL.logout(self._cookies().get("mapp_session"))
                self._json(HTTPStatus.OK, {"authenticated": False})
                return
            if request_path == "/api/admin/tokens":
                if actor != "admin":
                    self._json(HTTPStatus.FORBIDDEN, {"error": "Administrator session required."})
                    return
                raw, token = CONTROL.create_token(payload.get("name", "CLI token"), payload.get("expires"))
                self._json(HTTPStatus.CREATED, {"token": raw, "record": token, "warning": "Copy this token now; it will not be shown again."})
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
            if request_path == "/api/xyz/reload":
                fingerprint = payload.get("workspaceFingerprint")
                timeout = reload_timeout(payload.get("timeout", 30))
                result = request_reload(fingerprint)
                result["status"] = wait_reload(
                    result["requestedGeneration"],
                    fingerprint,
                    timeout,
                )
                CONTROL.audit("xyz.reload_requested", actor=actor, remote=self._remote(), details=result)
                self._json(HTTPStatus.OK if result["status"]["completed"] else HTTPStatus.GATEWAY_TIMEOUT, result)
                return
            if request_path == "/api/visual-plan":
                layer_key = payload.get("layer")
                if not isinstance(layer_key, str) or not layer_key.strip():
                    raise ValueError("Visual requests require a layer key.")
                _, current_workspace = read_workspace()
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
                _, current_workspace = read_workspace()
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
                            {"width": 1280, "height": 720},
                        ),
                    },
                    allow_nan=False,
                ).encode()
                try:
                    with urlopen(Request(
                        os.environ.get("BROWSER_RUNNER_URL", "http://browser-runner:8080/run"),
                        data=runner_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ), timeout=60) as response:
                        result = json.load(response)
                    CONTROL.audit("visual.completed", actor=actor, remote=self._remote(), details={"layer": layer_key, "runId": result.get("runId"), "passed": result.get("passed")})
                    self._json(HTTPStatus.OK, {"plan": plan, "visual": result})
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
                        self._json(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            {
                                "error": "Browser validation did not pass.",
                                "plan": plan,
                                "visual": result,
                            },
                        )
                    elif exc.code == HTTPStatus.TOO_MANY_REQUESTS:
                        self._json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {
                                "error": (
                                    result.get("error")
                                    if isinstance(result, dict)
                                    else None
                                ) or "Visual runner is busy. Retry later.",
                                "plan": plan,
                            },
                        )
                    else:
                        self._json(
                            HTTPStatus.BAD_GATEWAY,
                            {
                                "error": (
                                    "Browser validation service returned "
                                    f"HTTP {exc.code}."
                                ),
                                "plan": plan,
                            },
                        )
                except Exception as exc:
                    self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Browser validation failed: {exc}", "plan": plan})
                return
            if request_path == "/api/sql/test":
                raw, candidate = read_workspace()
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
            if request_path in {"/api/mutate", "/api/proposals"}:
                raw, current_workspace = read_workspace()
                expected = payload.get("revision") or revision(raw)
                if expected != revision(raw):
                    self._json(HTTPStatus.CONFLICT, {"error": "Workspace changed on disk. Reload before continuing."})
                    return
                candidate, diff = apply_operations(current_workspace, payload.get("operations") or [])
                errors = validate_candidate(candidate)
                if errors:
                    self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Validation failed.", "errors": annotated(errors)})
                    return
                if request_path == "/api/proposals":
                    proposal = proposal_create(CONTROL, current_workspace, expected, candidate, payload.get("operations") or [], diff, actor, payload.get("explanation"))
                    self._json(HTTPStatus.CREATED, {"proposal": proposal})
                elif payload.get("save"):
                    try:
                        encoded, next_revision, fingerprint, reload_result = save_and_reload(
                            candidate,
                            expected,
                        )
                    except FileExistsError as exc:
                        raw, _ = read_workspace()
                        self._json(HTTPStatus.CONFLICT, {
                            "error": str(exc),
                            "currentRevision": revision(raw),
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
                        current_raw, current_workspace = read_workspace()
                        current_matches_candidate = (
                            workspace_hash(current_workspace)
                            == proposal["candidateHash"]
                        )
                        current_matches_original_revision = (
                            revision(current_raw)
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
                    try:
                        proposal, reload_result = apply_proposal_and_reload(
                            CONTROL,
                            proposal,
                            actor=actor,
                        )
                    except FileExistsError as exc:
                        raw, _ = read_workspace()
                        self._json(HTTPStatus.CONFLICT, {
                            "error": str(exc),
                            "ruleId": "proposal.revision",
                            "currentRevision": revision(raw),
                            "remediation": "Create a new proposal from the current workspace revision.",
                        })
                        return
                completed = reload_completed(reload_result)
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
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
