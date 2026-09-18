"""The one definition of the extent the demo repairs and verify asserts within.

It exists because the two disagreed: verify asserted census geometry valid
across all of England while the ETL repaired only what the demo maps, so a
correct run failed naming output areas in Devon and Norfolk. Both now read this.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from workspace_repair_extent import repair_extent  # noqa: E402


class ExtentTests(unittest.TestCase):
    def test_the_packaged_demo_workspace_yields_an_ordered_box(self) -> None:
        """Read from the tree, not restated here: a literal would drift."""
        extent = repair_extent()
        self.assertTrue(extent, "the packaged demo workspace must carry an extent")
        west, south, east, north = (float(part) for part in extent.split(","))
        self.assertLess(west, east)
        self.assertLess(south, north)
        self.assertGreaterEqual(west, -180)
        self.assertLessEqual(north, 90)

    def test_the_command_prints_exactly_what_the_function_returns(self) -> None:
        """seed.sh and verify.sh both call it as a command, not as an import."""
        printed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/workspace_repair_extent.py")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(repair_extent(), printed)


class MalformedExtentTests(unittest.TestCase):
    """A bad box must read as "no extent", never as a box.

    An inverted or non-finite envelope would make the bounded half of verify's
    filter match nothing, and "nothing is invalid inside an empty box" is a
    check that passes by asserting nothing at all.
    """

    def extent_for(self, locale) -> str:
        import workspace_repair_extent as module

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            path.write_text(json.dumps({"locale": locale}), encoding="utf-8")
            original = module.CANDIDATES
            module.CANDIDATES = (path,)
            try:
                return module.repair_extent()
            finally:
                module.CANDIDATES = original

    def test_a_well_formed_extent_is_returned(self) -> None:
        self.assertEqual(
            "-1.85,53.65,-1.2,54.0",
            self.extent_for(
                {"extent": {"west": -1.85, "south": 53.65, "east": -1.2, "north": 54.0}}
            ),
        )

    def test_an_inverted_or_out_of_range_box_is_refused(self) -> None:
        for label, extent in {
            "east west of west": {"west": 1.0, "south": 0.0, "east": -1.0, "north": 1.0},
            "north south of south": {"west": 0.0, "south": 1.0, "east": 1.0, "north": 0.0},
            "beyond the antimeridian": {"west": -181.0, "south": 0.0, "east": 1.0, "north": 1.0},
            "beyond the pole": {"west": 0.0, "south": 0.0, "east": 1.0, "north": 91.0},
            "not a number": {"west": "x", "south": 0.0, "east": 1.0, "north": 1.0},
            "missing a side": {"west": 0.0, "south": 0.0, "east": 1.0},
        }.items():
            with self.subTest(label):
                self.assertEqual("", self.extent_for({"extent": extent}))

    def test_a_workspace_without_a_locale_is_refused(self) -> None:
        for locale in ({}, {"extent": {}}, None):
            with self.subTest(repr(locale)):
                self.assertEqual("", self.extent_for(locale))


if __name__ == "__main__":
    unittest.main()
