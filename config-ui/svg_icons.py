import re
import xml.etree.ElementTree as ET
from pathlib import Path


SVG_MAX_BYTES = 256 * 1024


def safe_svg(path: Path) -> bool:
    try:
        if path.suffix.lower() != ".svg" or not path.is_file():
            return False
        raw = path.read_bytes()
        if not raw or len(raw) > SVG_MAX_BYTES:
            return False
        lowered = raw.lower()
        if any(token in lowered for token in (
            b"<script", b"javascript:",
            b"<foreignobject", b"<!entity", b"<!doctype",
        )) or re.search(rb"\son[a-z0-9_-]+\s*=", lowered):
            return False
        root = ET.fromstring(raw)
        return root.tag.rsplit("}", 1)[-1].lower() == "svg"
    except (OSError, ET.ParseError):
        return False
