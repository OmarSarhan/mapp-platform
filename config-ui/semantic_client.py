from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


def _reject_constant(value: str):
    raise ValueError(f"{value} is not valid JSON")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _scrub(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, dict):
        return {key: _scrub(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secret) for item in value]
    return value


class SemanticClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        payload: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class SemanticClient:
    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        timeout: float = 10,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise SemanticClientError(
                "Semantic service endpoint must be an internal HTTP root URL."
            )
        if not isinstance(token, str) or not token:
            raise SemanticClientError("Semantic service token is required.")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.opener = urllib.request.build_opener(_RejectRedirects())

    def _safe_path(self, path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or path.startswith("//")
            or any(character in path for character in "\r\n")
        ):
            raise SemanticClientError("Semantic service path is invalid.")
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or ".." in parsed.path.split("/"):
            raise SemanticClientError("Semantic service path is invalid.")
        return path

    def _decode(self, body: bytes) -> dict[str, Any]:
        if len(body) > self.max_response_bytes:
            raise SemanticClientError("Semantic service response is too large.")
        try:
            value = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_constant,
                object_pairs_hook=_strict_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise SemanticClientError(
                "Semantic service returned invalid JSON."
            ) from exc
        if not isinstance(value, dict):
            raise SemanticClientError(
                "Semantic service returned an invalid response object."
            )
        return _scrub(value, self.token)

    @staticmethod
    def _require_json_content_type(response) -> None:
        headers = getattr(response, "headers", None)
        value = headers.get("Content-Type") if headers is not None else None
        media_type = str(value or "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise SemanticClientError(
                "Semantic service response Content-Type must be application/json."
            )

    def _read(self, response) -> bytes:
        body = response.read(self.max_response_bytes + 1)
        if len(body) > self.max_response_bytes:
            raise SemanticClientError("Semantic service response is too large.")
        return body

    def _decode_response(self, response) -> dict[str, Any]:
        self._require_json_content_type(response)
        return self._decode(self._read(response))

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        actor: str = "system",
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        safe_path = self._safe_path(path)
        if any(character in actor for character in "\r\n"):
            raise SemanticClientError("Semantic service actor is invalid.")
        normalized_scopes = sorted(set(scopes or []))
        if any(
            not isinstance(scope, str)
            or not scope
            or any(character in scope for character in "\r\n,")
            for scope in normalized_scopes
        ):
            raise SemanticClientError("Semantic service scopes are invalid.")
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "X-MAPP-Actor": actor,
            "X-MAPP-Scopes": ",".join(normalized_scopes),
        }
        if payload is not None:
            try:
                body = json.dumps(
                    payload,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise SemanticClientError(
                    "Semantic service request is not valid JSON."
                ) from exc
            if len(body) > MAX_BODY_BYTES:
                raise SemanticClientError(
                    "Semantic service request is too large."
                )
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.endpoint + safe_path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return self._decode_response(response)
        except urllib.error.HTTPError as exc:
            try:
                decoded = self._decode_response(exc)
            except SemanticClientError as response_error:
                decoded = {"error": str(response_error)}
            decoded = _scrub(decoded, self.token)
            message = str(decoded.get("error") or "Semantic service request failed.")
            raise SemanticClientError(
                message.replace(self.token, "[redacted]"),
                status=exc.code,
                payload=decoded,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SemanticClientError(
                "Semantic service is unavailable."
            ) from exc
