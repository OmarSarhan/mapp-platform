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


def compose_services(path: Path):
    """Every service in a compose file, with its image and whether it builds.

    Deliberately a parser rather than `docker compose config`: this has to run
    without Docker, without `.env`, and over the overlays individually.
    """
    text = path.read_text()
    start = re.search(r"^services:\n", text, re.M)
    if not start:
        return
    body = text[start.end() :]
    end = re.search(r"^[a-zA-Z]", body, re.M)
    if end:
        body = body[: end.start()]
    blocks = list(re.finditer(r"^  ([a-z][a-z0-9-]*):\s*$", body, re.M))
    for index, match in enumerate(blocks):
        stop = blocks[index + 1].start() if index + 1 < len(blocks) else len(body)
        block = body[match.end() : stop]
        image = re.search(r"^    image:\s*(\S+)", block, re.M)
        yield (
            match.group(1),
            image.group(1) if image else None,
            bool(re.search(r"^    build:", block, re.M)),
        )


def repository(image: str) -> str:
    """The repository part of an image reference, with `${VAR:-default}`
    resolved to its default -- the value a developer with no override gets,
    and the only one this repository can make promises about."""
    resolved = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", image)
    return resolved.split("@")[0].rsplit(":", 1)[0] if ":" in resolved.split("@")[0] \
        else resolved.split("@")[0]


class ComposeImageOriginTests(unittest.TestCase):
    """A service that names an image this repository builds must build it.

    Compose resolves an `image:` with no `build:` by pulling. Our images are
    published nowhere, so such a service works only on a machine that happens
    to hold the tag already -- which every development machine does, because
    some *other* service built it. `xyz-preview` shipped that way: it reuses
    `xyz`'s image by tag, so a fresh machine running `./bin/mapp all` got

        Image mapp-xyz:v4.23.4-a6f03c0 Error pull access denied for mapp-xyz

    while the same command on a warm machine printed `Built` and passed.

    An image whose repository has no `/` is one of ours: a bare name resolves
    to Docker Hub's `library/` namespace, where none of these exist. Anything
    with a registry or namespace (`postgis/postgis`) is genuinely pullable.
    """

    def test_no_service_pulls_an_image_this_repository_builds(self) -> None:
        offending = []
        for path in sorted(ROOT.glob("compose*.yaml")):
            for name, image, builds in compose_services(path):
                if image is None or builds:
                    continue
                if "/" in repository(image):
                    continue
                offending.append(f"{path.name}: {name} -> {image}")
        self.assertEqual(
            [], offending,
            "these services name an image built by this repository and"
            " published nowhere, without a build section, so compose pulls"
            " them and they fail on a fresh machine. Give the service the"
            " same build as whichever service builds that tag.",
        )

    def test_the_parser_sees_the_services_it_is_guarding(self) -> None:
        """A parser that silently matched nothing would pass forever."""
        base = dict(
            (name, (image, builds))
            for name, image, builds in compose_services(COMPOSE)
        )
        self.assertIn("xyz-preview", base)
        self.assertIn("caddy", base)
        self.assertNotIn("backend", base, "networks are not services")
        self.assertEqual("mapp-caddy", repository(base["caddy"][0]))
        self.assertEqual(
            "postgis/postgis",
            repository(
                "postgis/postgis:17-3.5-alpine@sha256:"
                + "978a2e6671c956d650d1f240dba7c73b8519a5f5af8685165fca616cc4ae3568"
            ),
        )
        self.assertEqual(
            "mapp-postgis-h3",
            repository("${POSTGIS_IMAGE:-mapp-postgis-h3:17-3.5-4.2.3}"),
        )


class StateDirectoryTests(unittest.TestCase):
    """Every `./var` bind mount must be a directory the launcher creates.

    Docker creates a missing bind-mount source itself, as root. The launcher
    then refuses to run, because it checks that writable state is owned by
    CONFIG_UID -- correctly, but the message names a directory the user never
    made and cannot explain.

    `./var/mapp-mcp` shipped that way. It is mounted by `caddy`, which starts
    whether or not the MCP surface is enabled, so `./bin/mapp all` created it
    as root on every fresh machine and the next command -- `./bin/mapp demo`
    -- died with an ownership error naming a path nothing had written to.
    `all` itself passed, because the check runs before the `up` that creates
    the directory.

    Derived from the compose files rather than listed: the failure mode is
    adding a mount and forgetting the directory, so a list maintained by hand
    would be wrong in exactly the case that matters.
    """

    def test_every_var_mount_is_created_by_the_launcher(self) -> None:
        mounted = set()
        for path in sorted(ROOT.glob("compose*.yaml")):
            mounted.update(re.findall(r"\./var/([a-z0-9-]+)", path.read_text()))
        self.assertTrue(mounted, "no ./var bind mounts found; parser is wrong")

        launcher = LAUNCHER.read_text()
        # The directories init_state actually makes, resolved through the
        # variables it makes them under.
        block = re.search(r"\n  mkdir -p \\\n(.*?)\n  chmod", launcher, re.S)
        self.assertIsNotNone(block, "init_state no longer creates directories")
        variables = dict(
            re.findall(r'^([A-Z_]+)="\$\{STATE_DIR\}/([a-z0-9-]+)"', launcher, re.M)
        )
        created = {
            variables[name]
            for name in re.findall(r"\$\{([A-Z_]+)\}", block.group(1))
            if name in variables
        }
        self.assertEqual(
            set(), mounted - created,
            "these directories are bind-mounted from compose but not created"
            " by init_state, so Docker creates them as root and the next"
            " ./bin/mapp command refuses to run",
        )
