"""`docker compose run` must build, because it does not by default.

`up` builds a service whose image is missing; `run` treats the `image:` name
as a registry reference and tries to pull it. Every service this launcher runs
one-off is built from this repository and published nowhere, so without
`--build` the command works on any machine that happens to have built the
image already and fails on a fresh clone with:

    pull access denied for mapp-config-ui, repository does not exist

That is the first command a new user types. It was reported from a fresh
container after `./bin/mapp init --demo`, and no amount of testing on a
machine with a warm image cache would have found it -- including a
`reset-system` rebuild, which keeps the images.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "bin" / "mapp"
COMPOSE = ROOT / "compose.yaml"


def locally_built_services():
    """Services compose builds here rather than pulling, read from the file."""
    text = COMPOSE.read_text()
    found = set()
    for match in re.finditer(r"\n  ([a-z][a-z0-9-]*):\n", text):
        name = match.group(1)
        block = text[match.end() : match.end() + 4000]
        nxt = re.search(r"\n  [a-z][a-z0-9-]*:\n", block)
        if nxt:
            block = block[: nxt.start()]
        if re.search(r"^\s+build:", block, re.M):
            found.add(name)
    return found


class LauncherBuildTests(unittest.TestCase):
    def test_every_one_off_run_builds_its_image(self) -> None:
        text = LAUNCHER.read_text()
        built = locally_built_services()
        self.assertTrue(built, "no locally built services found in compose.yaml")

        missing = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if "compose[@]}\" run" not in line and "compose[@]}\" run" not in line:
                if "} run " not in line and "]} run" not in line:
                    continue
            if " run " not in line:
                continue
            if "docker run" in line:
                # Not compose: a plain container with an explicit image.
                continue
            if "--build" not in line:
                missing.append(f"{line_number}: {line.strip()}")
        self.assertEqual(
            [], missing,
            "these compose `run` invocations do not build, so they pull an"
            " image this repository builds and fail on a fresh clone",
        )

    def test_the_first_command_a_new_user_types_builds(self) -> None:
        """`./bin/mapp init` is the entry point in every document. Checked by
        name so a refactor that moves it still has to keep this true."""
        text = LAUNCHER.read_text()
        start = text.index("init_compose=(")
        block = text[start : start + 2500]
        run_line = re.search(r'\$\{init_compose\[@\]\}" run [^\n]*', block)
        self.assertIsNotNone(run_line, "init no longer runs a one-off container")
        self.assertIn("--build", run_line.group(0))
