"""Every top-level module must actually ship in the built image.

Local tests run against the source tree directly (PYTHONPATH=.), so a module
that exists on disk but is missing from the Dockerfile's explicit COPY list
still imports fine in every test run while crashing the real container with
ModuleNotFoundError on startup. This is exactly the class of bug that broke
config-ui after relation_identity.py, federation_schema.py, and
federation_capability.py were added without updating the Dockerfile.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DockerfileModuleContractTests(unittest.TestCase):
    def test_every_top_level_module_is_copied_into_the_image(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        copy_line = next(
            line for line in dockerfile.splitlines() if line.startswith("COPY app.py")
        )
        copied = set(re.findall(r"[\w.]+\.py", copy_line))

        on_disk = {path.name for path in ROOT.glob("*.py")}

        missing = on_disk - copied
        self.assertEqual(
            set(),
            missing,
            f"{sorted(missing)} exist in config-ui/ but are not copied by the "
            "Dockerfile — the built image will crash with ModuleNotFoundError. "
            "Add them to the COPY line.",
        )


if __name__ == "__main__":
    unittest.main()
