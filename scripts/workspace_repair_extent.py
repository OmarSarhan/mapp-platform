#!/usr/bin/env python3
"""The extent the demo repairs census geometry within, printed once.

Two commands need this and they must not disagree. ``docker/demo-sources/seed.sh``
uses it to bound what the census ETL repairs; ``scripts/verify.sh`` uses it to
bound what it then asserts is valid. When only the seeder knew the extent,
verify asserted validity across all 178,605 England output areas while the ETL
had repaired only the ones the demo actually maps -- so a run that did exactly
what it was told still failed, naming geometries in Devon and Norfolk that no
part of the demo reads.

Printing nothing means "no extent", and both callers read that the same way:
the ETL repairs everything and verify asserts everything. The two halves stay
symmetric because there is one definition of what the extent is.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The versioned demo workspace, not whichever mutable workspace happens to be
#: live when a command starts. A custom live extent could otherwise produce
#: source geometry that does not cover the map the same demo run publishes.
CANDIDATES = (
    ROOT / "docker/demo-sources/workspace-demo.json",
    ROOT / "instance/workspace.seed.json",
)


def repair_extent() -> str:
    """``west,south,east,north`` in EPSG:4326, or "" when there is no usable one."""
    for candidate in CANDIDATES:
        if candidate.is_file():
            workspace_path = candidate
            break
    else:
        return ""
    try:
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    extent = ((workspace.get("locale") or {}).get("extent") or {})
    try:
        west = float(extent["west"])
        south = float(extent["south"])
        east = float(extent["east"])
        north = float(extent["north"])
    except (KeyError, TypeError, ValueError):
        return ""
    # Refuse a nonsensical box rather than passing it on. A caller that turned
    # it into an envelope would get an empty or inverted one, and "nothing is
    # invalid inside an empty box" is a check that passes by saying nothing.
    if (
        all(math.isfinite(value) for value in (west, south, east, north))
        and -180 <= west <= east <= 180
        and -90 <= south <= north <= 90
    ):
        return f"{west},{south},{east},{north}"
    return ""


if __name__ == "__main__":
    extent = repair_extent()
    if extent:
        print(extent)
    sys.exit(0)
