"""Private HTTP API for the semantic metadata store."""

from __future__ import annotations

import base64
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
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from semantic_store import SemanticError, SemanticStore, canonical_json


DEFAULT_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 100
LEGACY_SEARCH_LIMIT = 20
UNPAGINATED_FETCH = MAX_PAGE_LIMIT + 1
MAX_PAGE_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PAGE_ITEMS_BYTES = 15 * 1024 * 1024
SERVICE_VERSION = (
    Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
)
READ_SCOPES = {"semantic:inspect"}
_PAGE_CURSOR_RE = re.compile(
    r"^[A-Za-z0-9_-]{1,2048}\.[0-9a-f]{64}$"
)


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
    def _pagination_scope(
        collection: str,
        revision: int,
        is_admin: bool,
        **filters: str,
    ) -> str:
        return canonical_json({
            "collection": collection,
            "revision": revision,
            "visibility": "admin" if is_admin else "inspect",
            "filters": filters,
        })

    def _encode_cursor(self, scope: str, position: Any) -> str:
        payload = canonical_json({
            "version": 1,
            "scope": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
            "position": position,
        }).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(
            self.server.internal_token.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return f"{encoded}.{signature}"

    def _decode_cursor(self, cursor: str | None, scope: str) -> Any:
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
            expected = hmac.new(
                self.server.internal_token.encode("utf-8"),
                payload,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            value = strict_json_loads(payload)
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
        except (SemanticError, TypeError, UnicodeError, ValueError):
            raise SemanticError(
                "pagination_invalid",
                "cursor is invalid or expired.",
            ) from None

    @staticmethod
    def _string_position(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 200:
            raise SemanticError(
                "pagination_invalid", "cursor is invalid or expired."
            )
        return value

    @staticmethod
    def _history_position(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SemanticError(
                "pagination_invalid", "cursor is invalid or expired."
            )
        return value

    @staticmethod
    def _proposal_position(value: Any) -> tuple[str, str] | None:
        if value is None:
            return None
        if (
            not isinstance(value, list)
            or len(value) != 2
            or any(not isinstance(item, str) or not item for item in value)
            or len(value[0]) > 200
            or len(value[1]) > 200
        ):
            raise SemanticError(
                "pagination_invalid", "cursor is invalid or expired."
            )
        return value[0], value[1]

    def _bounded_page(
        self,
        fetched: list[Any],
        *,
        limit: int,
        scope: str,
        position_of: Callable[[Any], Any],
        public_item: Callable[[Any], Any] | None = None,
    ) -> tuple[list[Any], dict[str, Any]]:
        has_more = len(fetched) > limit
        page_items: list[Any] = []
        last_position: Any = None
        used_bytes = 2
        for stored_item in fetched[:limit]:
            item = public_item(stored_item) if public_item else stored_item
            item_bytes = len(canonical_json(item).encode("utf-8"))
            separator_bytes = 1 if page_items else 0
            if item_bytes + 2 > MAX_PAGE_ITEMS_BYTES:
                raise SemanticError(
                    "page_too_large",
                    "One collection item exceeds the bounded page response limit.",
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    details={
                        "maxPageBytes": MAX_PAGE_RESPONSE_BYTES,
                        "maxItemsBytes": MAX_PAGE_ITEMS_BYTES,
                    },
                )
            if used_bytes + separator_bytes + item_bytes > MAX_PAGE_ITEMS_BYTES:
                has_more = True
                break
            page_items.append(item)
            used_bytes += separator_bytes + item_bytes
            last_position = position_of(stored_item)
        next_cursor = (
            self._encode_cursor(scope, last_position)
            if has_more and last_position is not None
            else None
        )
        return page_items, {"limit": limit, "nextCursor": next_cursor}

    def _unpaginated_collection(
        self,
        fetched: list[Any],
        *,
        public_item: Callable[[Any], Any] | None = None,
    ) -> list[Any]:
        """Bound a response for a caller that asked for no page.

        A supported request shape, not a deprecated one: without limit or
        cursor this returns the whole collection when it fits and refuses,
        naming pagination, when it does not. The `maxLegacyItems` detail key
        keeps the older word because it rides in an error body clients may
        branch on.
        """
        if len(fetched) > MAX_PAGE_LIMIT:
            raise SemanticError(
                "pagination_required",
                "This collection has more than 100 items; retry with limit "
                "and follow pagination.nextCursor.",
                status=HTTPStatus.CONFLICT,
                details={"maxLegacyItems": MAX_PAGE_LIMIT},
            )
        items: list[Any] = []
        used_bytes = 2
        for stored_item in fetched:
            item = public_item(stored_item) if public_item else stored_item
            item_bytes = len(canonical_json(item).encode("utf-8"))
            separator_bytes = 1 if items else 0
            if item_bytes + 2 > MAX_PAGE_ITEMS_BYTES:
                raise SemanticError(
                    "page_too_large",
                    "One collection item exceeds the bounded response limit.",
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    details={
                        "maxPageBytes": MAX_PAGE_RESPONSE_BYTES,
                        "maxItemsBytes": MAX_PAGE_ITEMS_BYTES,
                    },
                )
            if used_bytes + separator_bytes + item_bytes > MAX_PAGE_ITEMS_BYTES:
                raise SemanticError(
                    "pagination_required",
                    "This legacy response exceeds the collection byte limit; "
                    "retry with limit and follow pagination.nextCursor.",
                    status=HTTPStatus.CONFLICT,
                    details={
                        "maxLegacyItems": MAX_PAGE_LIMIT,
                        "maxPageBytes": MAX_PAGE_RESPONSE_BYTES,
                    },
                )
            items.append(item)
            used_bytes += separator_bytes + item_bytes
        return items

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
                "maxResponseBytes": MAX_PAGE_RESPONSE_BYTES,
                "oversizedItemError": "page_too_large",
                "legacyMaxItems": MAX_PAGE_LIMIT,
                "legacyOverflowError": "pagination_required",
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
                scope = self._pagination_scope(
                    "catalog-v1", revision, is_admin
                )
                after = (
                    self._string_position(self._decode_cursor(cursor, scope))
                    if paginated
                    else None
                )
                assets = self.server.store.list_assets(
                    is_admin=is_admin,
                    connection=connection,
                    after_asset_id=after,
                    fetch_limit=(
                        limit + 1 if paginated else UNPAGINATED_FETCH
                    ),
                )
            payload = {"catalogRevision": revision, "assets": assets}
            if paginated:
                assets, pagination = self._bounded_page(
                    assets,
                    limit=limit,
                    scope=scope,
                    position_of=lambda item: item["id"],
                )
                payload.update({"assets": assets, "pagination": pagination})
            else:
                payload["assets"] = self._unpaginated_collection(assets)
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
                scope = self._pagination_scope(
                    "search-v1",
                    revision,
                    is_admin,
                    q=search_query or "",
                )
                after = (
                    self._string_position(self._decode_cursor(cursor, scope))
                    if paginated
                    else None
                )
                results = self.server.store.search_assets(
                    search_query or "",
                    limit=None,
                    is_admin=is_admin,
                    connection=connection,
                    after_asset_id=after,
                    fetch_limit=(
                        limit + 1 if paginated else UNPAGINATED_FETCH
                    ),
                )
            payload = {
                "catalogRevision": revision,
                "query": search_query,
                "results": results,
            }
            if paginated:
                results, pagination = self._bounded_page(
                    results,
                    limit=limit,
                    scope=scope,
                    position_of=lambda item: item["id"],
                )
                payload.update({"results": results, "pagination": pagination})
            else:
                if len(results) > MAX_PAGE_LIMIT:
                    # Detect growth past the platform-wide legacy threshold,
                    # while retaining search's established 20-result shape.
                    self._unpaginated_collection(results)
                payload["results"] = self._unpaginated_collection(
                    results[:LEGACY_SEARCH_LIMIT]
                )
            self._send_json(HTTPStatus.OK, payload)
            return
        if path == "/v1/derived-profiles":
            limit, cursor, paginated = self._pagination_parameters(query)
            with self.server.store.read_snapshot() as (
                connection,
                revision,
            ):
                scope = self._pagination_scope(
                    "derived-profiles-v1", revision, is_admin
                )
                after = (
                    self._string_position(self._decode_cursor(cursor, scope))
                    if paginated
                    else None
                )
                profiles = self.server.store.derived_profiles(
                    is_admin=is_admin,
                    connection=connection,
                    after_asset_id=after,
                    fetch_limit=(
                        limit + 1 if paginated else UNPAGINATED_FETCH
                    ),
                )
            payload = {
                "catalogRevision": revision,
                "derivedProfiles": profiles,
            }
            if paginated:
                profiles, pagination = self._bounded_page(
                    profiles,
                    limit=limit,
                    scope=scope,
                    position_of=lambda item: item["id"],
                )
                payload.update(
                    {"derivedProfiles": profiles, "pagination": pagination}
                )
            else:
                payload["derivedProfiles"] = self._unpaginated_collection(profiles)
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
                scope = self._pagination_scope(
                    "proposals-v1",
                    revision,
                    is_admin,
                    state=state or "",
                    assetId=asset_id or "",
                )
                after = (
                    self._proposal_position(self._decode_cursor(cursor, scope))
                    if paginated
                    else None
                )
                proposals = self.server.store.list_proposals(
                    state=state,
                    asset_id=asset_id,
                    is_admin=is_admin,
                    connection=connection,
                    after=after,
                    fetch_limit=(
                        limit + 1 if paginated else UNPAGINATED_FETCH
                    ),
                )
            payload = {
                "catalogRevision": revision,
                "proposals": proposals,
            }
            if paginated:
                proposals, pagination = self._bounded_page(
                    proposals,
                    limit=limit,
                    scope=scope,
                    position_of=lambda item: [item["createdAt"], item["id"]],
                )
                payload.update({"proposals": proposals, "pagination": pagination})
            else:
                payload["proposals"] = self._unpaginated_collection(proposals)
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
                    scope = self._pagination_scope(
                        "asset-history-v1",
                        revision,
                        is_admin,
                        assetId=asset_id,
                    )
                    after = (
                        self._history_position(
                            self._decode_cursor(cursor, scope)
                        )
                        if paginated
                        else None
                    )
                    history_items = self.server.store.asset_history(
                        asset_id,
                        is_admin=is_admin,
                        connection=connection,
                        after_history_id=after,
                        fetch_limit=(
                            limit + 1
                            if paginated
                            else UNPAGINATED_FETCH
                        ),
                    )
                payload = {
                    "assetId": asset_id,
                    "catalogRevision": revision,
                    "history": history_items,
                }
                if paginated:
                    history_items, pagination = self._bounded_page(
                        history_items,
                        limit=limit,
                        scope=scope,
                        position_of=lambda item: item["_historyId"],
                        public_item=lambda item: {
                            key: value
                            for key, value in item.items()
                            if key != "_historyId"
                        },
                    )
                    payload.update(
                        {"history": history_items, "pagination": pagination}
                    )
                else:
                    payload["history"] = self._unpaginated_collection(
                        history_items,
                        public_item=lambda item: {
                            key: value
                            for key, value in item.items()
                            if key != "_historyId"
                        },
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
        if path == "/v1/source-state":
            # semantic:admin, the same scope /v1/events needs. This changes
            # whether assets are treated as usable, so it belongs with the
            # mutating routes rather than the reading ones -- even though the
            # caller is the platform's own verifier rather than a person.
            self._require_any_scope({"semantic:admin"})
            schema = body.get("schema")
            if not isinstance(schema, str) or not schema.strip():
                raise SemanticError(
                    "invalid_request",
                    "schema must be a non-empty string.",
                    status=HTTPStatus.BAD_REQUEST,
                )
            available = body.get("available")
            if not isinstance(available, bool):
                # Explicitly not truthiness. A missing or misspelled property
                # would otherwise read as false and quietly mark a healthy
                # source unusable.
                raise SemanticError(
                    "invalid_request",
                    "available must be a boolean.",
                    status=HTTPStatus.BAD_REQUEST,
                )
            unexpected = sorted(set(body) - {"schema", "available", "actor"})
            if unexpected:
                raise SemanticError(
                    "invalid_request",
                    "Unknown properties: " + ", ".join(unexpected),
                    status=HTTPStatus.BAD_REQUEST,
                )
            changed = self.server.store.mark_source_state(
                schema.strip(), available=available
            )
            # The changed list, not a count, so a caller can log which assets
            # moved and stay silent when a pass changes nothing.
            self._send_json(
                HTTPStatus.OK, {"schema": schema.strip(), "changed": changed}
            )
            return
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
    # Both DSNs are required: the store reads as the read-only semantic role
    # and writes as the role owning the schema, and neither identity has a
    # usable default to fall back to.
    database_url = os.environ.get("SEMANTIC_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("SEMANTIC_DATABASE_URL is required")
    reader_database_url = os.environ.get("SEMANTIC_READER_DATABASE_URL", "")
    if not reader_database_url:
        raise SystemExit("SEMANTIC_READER_DATABASE_URL is required")
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
    store = SemanticStore(database_url, reader_database_url)
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
