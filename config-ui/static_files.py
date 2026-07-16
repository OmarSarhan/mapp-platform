from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse


def safe_static_path(root: Path, url_path: str) -> Path | None:
    """Resolve a URL path beneath ``root`` and reject every escape attempt."""

    root = root.resolve()
    raw_path = unquote(urlparse(url_path).path)
    if "\x00" in raw_path:
        return None
    relative = raw_path.lstrip("/") or "index.html"
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate
