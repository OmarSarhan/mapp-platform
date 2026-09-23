"""Bounded reads of screenshots retained by the browser runner.

Artifact names are not arbitrary paths. The runner's retained report must name
the image, and directory-relative opens reject symlinks at every untrusted
component, including if a file changes between validation and opening it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SCREENSHOT_FILENAMES = frozenset({
    "page.png", "map.png", "before-page.png", "before-map.png",
    "after-page.png", "after-map.png", "info-panel.png", "hover-tooltip.png",
    "filtering-panel.png", "styling-panel.png",
})


class VisualArtifactError(ValueError):
    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _read_file(directory: int, name: str, limit: int) -> bytes:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
    )
    with os.fdopen(descriptor, "rb") as stream:
        information = os.fstat(stream.fileno())
        if not stat.S_ISREG(information.st_mode):
            raise OSError("Artifact is not a regular file.")
        if information.st_size > limit:
            raise VisualArtifactError(
                "The retained visual artifact exceeds the retrieval size limit.",
                code="visual.artifact_too_large", status=413,
            )
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise VisualArtifactError(
                "The retained visual artifact exceeds the retrieval size limit.",
                code="visual.artifact_too_large", status=413,
            )
        return data


def read_visual_image(root: Path, relative: str, *, include_data: bool = True) -> dict:
    parts = relative.split("/")
    if (
        len(parts) != 2
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", parts[0]) is None
        or parts[1] not in SCREENSHOT_FILENAMES
    ):
        raise VisualArtifactError(
            "Use a retained screenshot path returned by a visual operation.",
            code="visual.artifact_path_invalid", status=400,
        )
    run_id, filename = parts
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            run_fd = os.open(
                run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            try:
                report = json.loads(_read_file(run_fd, "report.json", MAX_REPORT_BYTES))
                retained = report.get("artifacts") if isinstance(report, dict) else None
                if (
                    not isinstance(report, dict)
                    or report.get("runId") != run_id
                    or not isinstance(retained, dict)
                    or relative not in retained.values()
                ):
                    raise OSError("Screenshot is not retained in this visual report.")
                data = _read_file(run_fd, filename, MAX_IMAGE_BYTES)
                if not data.startswith(PNG_SIGNATURE):
                    raise OSError("Screenshot is not a PNG image.")
            finally:
                os.close(run_fd)
        finally:
            os.close(root_fd)
    except (OSError, ValueError) as exc:
        if isinstance(exc, VisualArtifactError):
            raise
        raise VisualArtifactError(
            "The retained visual screenshot is unavailable.",
            code="visual.artifact_not_found", status=404,
        ) from None
    result = {
        "path": relative, "mimeType": "image/png", "sizeBytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if len(data) >= 24 and data[12:16] == b"IHDR":
        result.update(width=int.from_bytes(data[16:20], "big"),
                      height=int.from_bytes(data[20:24], "big"))
    if include_data:
        result["data"] = base64.b64encode(data).decode("ascii")
    return result


DOWNLOAD_TTL_SECONDS = 300
DOWNLOAD_PREFIX = "/artifact-downloads/"


def download_origin(value: str) -> str:
    """Deployment-controlled origin, never inferred from forwarding headers."""
    if not value:
        return ""
    parsed = urlsplit(value)
    parsed.port  # Validate malformed ports before publishing a broken URL.
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or any(character.isspace() for character in value)
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not (parsed.scheme == "https" or (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("ARTIFACT_DOWNLOAD_ORIGIN must be an HTTPS origin or loopback HTTP origin.")
    return value.rstrip("/")


def _download_key(key: bytes) -> bytes:
    # Domain separation keeps signatures specific to this capability version.
    return hmac.digest(key, b"mapp-artifact-download-v1", "sha256")


def issue_download(artifact: dict, key: bytes, *, now=None) -> dict:
    expires = int(time.time() if now is None else now) + DOWNLOAD_TTL_SECONDS
    payload = json.dumps({"v": 1, "path": artifact["path"],
                          "sha256": artifact["sha256"], "exp": expires},
                         separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.digest(_download_key(key), encoded.encode(), "sha256").hex()
    return {"path": DOWNLOAD_PREFIX + encoded + "." + signature,
            "expiresAt": expires, "expiresInSeconds": DOWNLOAD_TTL_SECONDS}


def read_download(root: Path, ticket: str, key: bytes, *, now=None) -> dict:
    """Authenticate before touching files, then verify the exact retained bytes."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,800}\.[a-f0-9]{64}", ticket):
            raise ValueError()
        encoded, signature = ticket.split(".")
        expected = hmac.digest(_download_key(key), encoded.encode(), "sha256").hex()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        current = int(time.time() if now is None else now)
        if (not isinstance(payload, dict) or payload.get("v") != 1 or type(payload.get("exp")) is not int
                or not current < payload["exp"] <= current + DOWNLOAD_TTL_SECONDS
                or not isinstance(payload.get("path"), str)
                or re.fullmatch(r"[a-f0-9]{64}", str(payload.get("sha256", ""))) is None):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise VisualArtifactError("The download link is invalid or expired. Request a new link.",
                                  code="visual.download_invalid", status=403) from None
    artifact = read_visual_image(root, payload["path"])
    if not hmac.compare_digest(artifact["sha256"], payload["sha256"]):
        raise VisualArtifactError("The retained screenshot changed. Request a new link.",
                                  code="visual.download_changed", status=410)
    return artifact
