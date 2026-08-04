"""Private HTTP API for the semantic metadata store."""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import re
import signal
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from semantic_store import SemanticError, SemanticStore, canonical_json


DEFAULT_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 100
SERVICE_VERSION = (
    Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
)
READ_SCOPES = {"semantic:inspect"}
_PAGE_CURSOR_RE = re.compile(r"^[0-9a-f]{64}$")


def strict_json_loads(raw: bytes) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate property: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SemanticError(
            "invalid_json", "Request body must be strict UTF-8 JSON."
        ) from exc


class SemanticHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: SemanticStore,
        internal_token: str,
        *,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        if not internal_token:
            raise ValueError("internal_token must not be empty")
        super().__init__(address, SemanticHandler)
        self.store = store
        self.internal_token = internal_token
        self.max_body_bytes = max_body_bytes


class SemanticHandler(BaseHTTPRequestHandler):
    server: SemanticHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the standard useful request log while avoiding request headers/bodies.
        super().log_message(format, *args)

    def _send_json(self, status: int, value: Any) -> None:
        payload = canonical_json(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, error: SemanticError) -> None:
        # Some failures (unsupported media type or excessive Content-Length) are
        # returned before consuming the body.  Closing prevents those bytes from
        # being parsed and logged as a second HTTP request.
        self.close_connection = True
        body: dict[str, Any] = {
            "error": {"code": error.code, "message": error.message}
        }
        if error.details:
            body["error"]["details"] = error.details
        self._send_json(error.status, body)

    def _authenticate(self) -> None:
        authorization_values = self.headers.get_all("Authorization", [])
        expected = f"Bearer {self.server.internal_token}"
        if len(authorization_values) != 1 or not hmac.compare_digest(
            authorization_values[0], expected
        ):
            raise SemanticError(
                "unauthorized",
                "A valid internal bearer token is required.",
                status=HTTPStatus.UNAUTHORIZED,
            )

    def _scopes(self) -> set[str]:
        values = self.headers.get_all("X-MAPP-Scopes", [])
        scopes: set[str] = set()
        for value in values:
            scopes.update(value.replace(",", " ").split())
        return scopes

    def _actor(self) -> str:
        values = self.headers.get_all("X-MAPP-Actor", [])
        if len(values) > 1:
            raise SemanticError(
                "invalid_request", "X-MAPP-Actor may be supplied only once."
            )
        actor = values[0].strip() if values else "system"
        if not actor or len(actor) > 256:
            raise SemanticError(
                "invalid_request",
                "X-MAPP-Actor must contain 1 to 256 characters.",
            )
        return actor

    def _require_any_scope(self, allowed: set[str]) -> set[str]:
        scopes = self._scopes()
        if scopes.isdisjoint(allowed):
            raise SemanticError(
                "forbidden",
                "The caller does not have the required semantic scope.",
                status=HTTPStatus.FORBIDDEN,
            )
        return scopes

    def _read_json(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise SemanticError(
                "invalid_request",
                "Transfer-Encoding is not accepted.",
            )
        content_type = self.headers.get("Content-Type", "")
        media_type, _, parameters = content_type.partition(";")
        if media_type.strip().lower() != "application/json":
            raise SemanticError(
                "unsupported_media_type",
                "Content-Type must be application/json.",
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        if parameters:
            parameter = parameters.strip().replace(" ", "").lower()
            if parameter not in {"charset=utf-8", "charset=utf8"}:
                raise SemanticError(
                    "unsupported_media_type",
                    "JSON request charset must be UTF-8.",
                    status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
        content_length_values = self.headers.get_all("Content-Length", [])
        if len(content_length_values) != 1:
            raise SemanticError(
                "length_required",
                "A single Content-Length header is required.",
                status=HTTPStatus.LENGTH_REQUIRED,
            )
        try:
            content_length = int(content_length_values[0], 10)
        except ValueError as exc:
            raise SemanticError(
                "invalid_request", "Content-Length must be an integer."
            ) from exc
        if content_length < 2:
            raise SemanticError(
                "invalid_json", "Request body must contain a JSON object."
            )
        if content_length > self.server.max_body_bytes:
            raise SemanticError(
                "body_too_large",
                "Request body exceeds the configured limit.",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                details={"maxBytes": self.server.max_body_bytes},
            )
        raw = self.rfile.read(content_length)
        if len(raw) != content_length:
            raise SemanticError(
                "invalid_request", "Request body ended before Content-Length."
            )
        value = strict_json_loads(raw)
        if not isinstance(value, dict):
            raise SemanticError("invalid_request", "Request body must be an object.")
        return value

    @staticmethod
    def _one_query(
        query: dict[str, list[str]], name: str, *, required: bool = False
    ) -> str | None:
        values = query.get(name)
        if values is None:
            if required:
                raise SemanticError(
                    "invalid_request", f"Query parameter {name} is required."
                )
            return None
        if len(values) != 1:
            raise SemanticError(
                "invalid_request",
                f"Query parameter {name} may be supplied only once.",
            )
        return values[0]

    @staticmethod
    def _validate_query_keys(
        query: dict[str, list[str]], allowed: set[str]
    ) -> None:
        unexpected = sorted(set(query) - allowed)
        if unexpected:
            raise SemanticError(
                "invalid_request",
                "Unsupported query parameters.",
                details={"parameters": unexpected},
            )

    def _pagination_parameters(
        self,
        query: dict[str, list[str]],
        *,
        allowed: set[str] | None = None,
    ) -> tuple[int, str | None, bool]:
        pagination_requested = "limit" in query or "cursor" in query
        self._validate_query_keys(
            query,
            {"limit", "cursor"} | (allowed or set()),
        )
        limit_text = self._one_query(query, "limit")
        if limit_text is None:
            limit = DEFAULT_PAGE_LIMIT
        elif not re.fullmatch(r"[1-9][0-9]*", limit_text):
            raise SemanticError(
                "pagination_invalid",
                "limit must be an integer from 1 to 100.",
            )
        else:
            limit = int(limit_text, 10)
            if limit > MAX_PAGE_LIMIT:
                raise SemanticError(
                    "pagination_invalid",
                    "limit must be an integer from 1 to 100.",
                )
        cursor = self._one_query(query, "cursor")
        if cursor is not None and _PAGE_CURSOR_RE.fullmatch(cursor) is None:
            raise SemanticError(
                "pagination_invalid",
                "cursor is invalid or expired.",
            )
        return limit, cursor, pagination_requested

    @staticmethod
    def _page_cursor(scope: str, item: Any) -> str:
        value = canonical_json(
            {"scope": scope, "position": item}
        ).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    @classmethod
    def _paginate(
        cls,
        items: list[Any],
        *,
        limit: int,
        cursor: str | None,
        scope: str,
    ) -> tuple[list[Any], dict[str, Any]]:
        start = 0
        if cursor is not None:
            for index, item in enumerate(items):
                if cls._page_cursor(scope, item) == cursor:
                    start = index + 1
                    break
            else:
                raise SemanticError(
                    "pagination_invalid",
                    "cursor is invalid or expired.",
                )
        page_items = items[start : start + limit]
        has_more = start + len(page_items) < len(items)
        next_cursor = (
            cls._page_cursor(scope, page_items[-1])
            if has_more and page_items
            else None
        )
        return page_items, {"limit": limit, "nextCursor": next_cursor}

    def do_GET(self) -> None:
        try:
            split = urlsplit(self.path)
            path = split.path
            if path == "/healthz":
                if split.query:
                    raise SemanticError(
                        "invalid_request",
                        "Health route does not accept query parameters.",
                    )
                self.server.store.catalog_revision()
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            self._authenticate()
            query = parse_qs(split.query, keep_blank_values=True, strict_parsing=True)
            self._route_get(path, query)
        except SemanticError as error:
            self._send_error(error)
        except ValueError:
            self._send_error(
                SemanticError("invalid_request", "Query string is invalid.")
            )
        except Exception:
            traceback.print_exc()
            self._send_error(
                SemanticError(
                    "internal_error",
                    "The semantic service could not complete the request.",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            )

    def _route_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/v1/status":
            self._validate_query_keys(query, set())
            self._require_any_scope(READ_SCOPES)
            status = self.server.store.status()
            status["serviceVersion"] = SERVICE_VERSION
            status["capabilities"]["pagination"] = {
                "version": "1",
                "defaultLimit": DEFAULT_PAGE_LIMIT,
                "maxLimit": MAX_PAGE_LIMIT,
                "cursor": "opaque",
            }
            self._send_json(HTTPStatus.OK, status)
            return

        scopes = self._require_any_scope(READ_SCOPES)
        is_admin = "semantic:admin" in scopes
        if path == "/v1/catalog":
            limit, cursor, paginated = self._pagination_parameters(query)
            with self.server.store.read_snapshot() as (
                connection,
                revision,
            ):
                assets = self.server.store.list_assets(
                    is_admin=is_admin,
                    connection=connection,
                )
            payload = {"catalogRevision": revision, "assets": assets}
            if paginated:
                assets, pagination = self._paginate(
                    assets,
                    limit=limit,
                    cursor=cursor,
                    scope=f"catalog-v1:{revision}:{is_admin}",
                )
                payload.update({"assets": assets, "pagination": pagination})
            self._send_json(HTTPStatus.OK, payload)
            return
        if path == "/v1/search":
            limit, cursor, paginated = self._pagination_parameters(
                query,
                allowed={"q"},
            )
            search_query = self._one_query(query, "q", required=True)
            with self.server.store.read_snapshot() as (
                connection,
                revision,
            ):
                results = self.server.store.search_assets(
                    search_query or "",
                    limit=None if paginated else 20,
                    is_admin=is_admin,
                    connection=connection,
                )
            payload = {
                "catalogRevision": revision,
                "query": search_query,
                "results": results,
            }
            if paginated:
                results, pagination = self._paginate(
                    results,
                    limit=limit,
                    cursor=cursor,
                    scope=(
                        f"search-v1:{revision}:{is_admin}:"
                        f"{search_query or ''}"
                    ),
                )
                payload.update({"results": results, "pagination": pagination})
            self._send_json(HTTPStatus.OK, payload)
            return
        if path == "/v1/derived-profiles":
            limit, cursor, paginated = self._pagination_parameters(query)
            with self.server.store.read_snapshot() as (
                connection,
                revision,
            ):
                profiles = self.server.store.derived_profiles(
                    is_admin=is_admin,
                    connection=connection,
                )
            payload = {
                "catalogRevision": revision,
                "derivedProfiles": profiles,
            }
            if paginated:
                profiles, pagination = self._paginate(
                    profiles,
                    limit=limit,
                    cursor=cursor,
                    scope=f"derived-profiles-v1:{revision}:{is_admin}",
                )
                payload.update(
                    {"derivedProfiles": profiles, "pagination": pagination}
                )
            self._send_json(HTTPStatus.OK, payload)
            return
        derived_prefix = "/v1/derived-profiles/"
        if path.startswith(derived_prefix):
            self._validate_query_keys(query, set())
            name = self._decode_path_identifier(path[len(derived_prefix) :])
            with self.server.store.read_snapshot() as (
                connection,
                revision,
            ):
                profile = self.server.store.get_derived_profile(
                    name,
                    is_admin=is_admin,
                    connection=connection,
                )
            self._send_json(
                HTTPStatus.OK,
                {
                    "catalogRevision": revision,
                    "derivedProfile": profile,
                },
            )
            return
        if path == "/v1/proposals":
            limit, cursor, paginated = self._pagination_parameters(
                query,
                allowed={"state", "assetId"},
            )
            state = self._one_query(query, "state")
            asset_id = self._one_query(query, "assetId")
            with self.server.store.read_snapshot() as (
                connection,
                revision,
            ):
                proposals = self.server.store.list_proposals(
                    state=state,
                    asset_id=asset_id,
                    is_admin=is_admin,
                    connection=connection,
                )
            payload = {
                "catalogRevision": revision,
                "proposals": proposals,
            }
            if paginated:
                proposals, pagination = self._paginate(
                    proposals,
                    limit=limit,
                    cursor=cursor,
                    scope=(
                        f"proposals-v1:{revision}:{is_admin}:"
                        f"{state or ''}:{asset_id or ''}"
                    ),
                )
                payload.update({"proposals": proposals, "pagination": pagination})
            self._send_json(HTTPStatus.OK, payload)
            return
        asset_prefix = "/v1/assets/"
        if path.startswith(asset_prefix):
            suffix = path[len(asset_prefix) :]
            history = suffix.endswith("/history")
            encoded_id = suffix[: -len("/history")] if history else suffix
            asset_id = self._decode_path_identifier(encoded_id)
            if history:
                limit, cursor, paginated = self._pagination_parameters(query)
                with self.server.store.read_snapshot() as (
                    connection,
                    revision,
                ):
                    history_items = self.server.store.asset_history(
                        asset_id,
                        is_admin=is_admin,
                        connection=connection,
                    )
                payload = {
                    "assetId": asset_id,
                    "catalogRevision": revision,
                    "history": history_items,
                }
                if paginated:
                    history_items, pagination = self._paginate(
                        history_items,
                        limit=limit,
                        cursor=cursor,
                        scope=(
                            f"asset-history-v1:{revision}:{is_admin}:"
                            f"{asset_id}"
                        ),
                    )
                    payload.update(
                        {"history": history_items, "pagination": pagination}
                    )
                self._send_json(HTTPStatus.OK, payload)
            else:
                self._validate_query_keys(query, set())
                with self.server.store.read_snapshot() as (
                    connection,
                    revision,
                ):
                    asset = self.server.store.get_asset(
                        asset_id,
                        is_admin=is_admin,
                        connection=connection,
                    )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "asset": asset,
                        "catalogRevision": revision,
                    },
                )
            return
        proposal_prefix = "/v1/proposals/"
        if path.startswith(proposal_prefix):
            self._validate_query_keys(query, set())
            proposal_id = self._decode_path_identifier(path[len(proposal_prefix) :])
            with self.server.store.read_snapshot() as (
                connection,
                revision,
            ):
                proposal = self.server.store.get_proposal(
                    proposal_id,
                    is_admin=is_admin,
                    connection=connection,
                )
            self._send_json(
                HTTPStatus.OK,
                {
                    "proposal": proposal,
                    "catalogRevision": revision,
                },
            )
            return
        raise SemanticError("not_found", "Route was not found.", status=404)

    @staticmethod
    def _decode_path_identifier(encoded: str) -> str:
        if not encoded or "/" in encoded:
            raise SemanticError("not_found", "Route was not found.", status=404)
        value = unquote(encoded)
        if not value or "\x00" in value:
            raise SemanticError("not_found", "Route was not found.", status=404)
        return value

    def do_POST(self) -> None:
        try:
            split = urlsplit(self.path)
            if split.query:
                raise SemanticError(
                    "invalid_request", "POST routes do not accept query parameters."
                )
            self._authenticate()
            body = self._read_json()
            self._route_post(split.path, body)
        except SemanticError as error:
            self._send_error(error)
        except Exception:
            traceback.print_exc()
            self._send_error(
                SemanticError(
                    "internal_error",
                    "The semantic service could not complete the request.",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            )

    def _route_post(self, path: str, body: dict[str, Any]) -> None:
        if path == "/v1/events":
            self._require_any_scope({"semantic:admin"})
            body["actor"] = self._actor()
            self._send_json(HTTPStatus.OK, self.server.store.apply_event(body))
            return
        if path == "/v1/proposals/check":
            scopes = self._require_any_scope({"semantic:propose"})
            with self.server.store.read_snapshot() as (
                connection,
                revision,
            ):
                check = self.server.store.check_proposal(
                    body,
                    is_admin="semantic:admin" in scopes,
                    connection=connection,
                )
            self._send_json(
                HTTPStatus.OK,
                {
                    "check": check,
                    "catalogRevision": revision,
                },
            )
            return
        if path == "/v1/proposals":
            scopes = self._require_any_scope({"semantic:propose"})
            proposal = self.server.store.create_proposal(
                body,
                actor=self._actor(),
                is_admin="semantic:admin" in scopes,
            )
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "proposal": proposal,
                    "catalogRevision": self.server.store.catalog_revision(),
                },
            )
            return
        proposal_prefix = "/v1/proposals/"
        if path.startswith(proposal_prefix):
            suffix = path[len(proposal_prefix) :]
            if suffix.endswith("/apply"):
                proposal_id = self._decode_path_identifier(suffix[: -len("/apply")])
                if set(body):
                    raise SemanticError(
                        "invalid_request", "Apply request body must be empty."
                    )
                scopes = self._require_any_scope({"semantic:apply"})
                proposal, asset, revision = self.server.store.apply_proposal(
                    proposal_id,
                    actor=self._actor(),
                    is_admin="semantic:admin" in scopes,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "proposal": proposal,
                        "asset": asset,
                        "catalogRevision": revision,
                    },
                )
                return
            if suffix.endswith("/decline"):
                proposal_id = self._decode_path_identifier(
                    suffix[: -len("/decline")]
                )
                unexpected = sorted(set(body) - {"reason"})
                if unexpected:
                    raise SemanticError(
                        "invalid_request",
                        "Decline request contains unsupported properties.",
                        details={"properties": unexpected},
                    )
                scopes = self._require_any_scope({"semantic:propose"})
                proposal = self.server.store.decline_proposal(
                    proposal_id,
                    actor=self._actor(),
                    reason=body.get("reason"),
                    is_admin="semantic:admin" in scopes,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "proposal": proposal,
                        "catalogRevision": self.server.store.catalog_revision(),
                    },
                )
                return
        raise SemanticError("not_found", "Route was not found.", status=404)


def main() -> None:
    state_dir = Path(os.environ.get("STATE_DIR", "/state"))
    db_path = Path(
        os.environ.get("SEMANTIC_DB_PATH", str(state_dir / "semantic.sqlite3"))
    )
    token = os.environ.get("SEMANTIC_INTERNAL_TOKEN", "")
    if not token:
        raise SystemExit("SEMANTIC_INTERNAL_TOKEN is required")
    try:
        port = int(os.environ.get("PORT", "8080"))
        max_body = int(
            os.environ.get("SEMANTIC_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES))
        )
    except ValueError as exc:
        raise SystemExit("PORT and SEMANTIC_MAX_BODY_BYTES must be integers") from exc
    if not 1 <= port <= 65535 or max_body < 1024:
        raise SystemExit("PORT or SEMANTIC_MAX_BODY_BYTES is out of range")
    store = SemanticStore(db_path)
    server = SemanticHTTPServer(
        ("0.0.0.0", port), store, token, max_body_bytes=max_body
    )

    def request_shutdown(_signum: int, _frame: Any) -> None:
        # BaseServer.shutdown must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
