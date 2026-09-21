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
            if "--build" in line:
                continue
            # Or the image was built as its own step just before. That is the
            # right shape wherever the run's stdout is captured: BuildKit
            # rendering progress into a captured pipe from an interactive
            # terminal fails with "failed to get console", so the build has to
            # happen outside the capture.
            preceding = "\n".join(
                text.splitlines()[max(0, line_number - 12) : line_number]
            )
            if re.search(r'\}" build [a-z]', preceding):
                continue
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
        # Either spelling, so long as the image exists before it is run. init
        # builds as its own step because its run's stdout is captured.
        self.assertTrue(
            "--build" in run_line.group(0)
            or re.search(r'\$\{init_compose\[@\]\}" build config-ui', block),
            "init runs config-ui without ensuring the image is built",
        )

    def test_a_captured_run_does_not_build_inside_the_capture(self) -> None:
        """`--build` on a run whose stdout is captured fails from a terminal:
        BuildKit cannot render progress into a pipe and dies with "failed to
        get console: provided file is not a console". It is invisible in CI and
        in any non-interactive shell, which is where this was tested."""
        text = LAUNCHER.read_text()
        offending = []
        for match in re.finditer(r'="\$\((?:[^()]|\([^()]*\))*\)"', text, re.S):
            if "--build" in match.group(0) and " run " in match.group(0):
                line = text[: match.start()].count("\n") + 1
                offending.append(line)
        self.assertEqual(
            [], offending,
            "these capture a compose `run --build`, so the build renders into"
            " a pipe and fails on an interactive terminal",
        )
