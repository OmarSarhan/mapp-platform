"""Bounded reads of screenshots retained by the browser runner.

Artifact names are not arbitrary paths. The runner's retained report must name
the image, and directory-relative opens reject symlinks at every untrusted
component, including if a file changes between validation and opening it.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from pathlib import Path


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


def read_visual_image(root: Path, relative: str) -> dict:
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
    return {
        "path": relative, "mimeType": "image/png", "sizeBytes": len(data),
        "data": base64.b64encode(data).decode("ascii"),
    }
