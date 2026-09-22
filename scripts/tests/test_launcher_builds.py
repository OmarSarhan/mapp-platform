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


def launcher_scripts():
    """Every shell script in this repository that drives compose.

    Globbed rather than listed: the console bug shipped a second time in
    `docker/demo-sources/seed.sh` because the guard read `bin/mapp` alone, and
    a hand-maintained list would have had the same gap.
    """
    scripts = [LAUNCHER]
    for pattern in ("scripts/*.sh", "docker/*/*.sh"):
        scripts.extend(sorted(ROOT.glob(pattern)))
    return [path for path in scripts if "compose" in path.read_text()]


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
        # Bounded by the function, not by a character count. A fixed window
        # silently stopped covering the run line the first time this function
        # grew.
        block = text[start : text.index("\ninit_env() {", start)]
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
        """`--build` on a run whose stdout does not reach the terminal fails
        from a terminal: BuildKit cannot render progress into a pipe while
        stderr is still a console, and dies with "failed to get console:
        provided file is not a console". It is invisible in CI and in any
        non-interactive shell, which is where this gets tested.

        Two shapes, both real. `bin/mapp init` captured the run to read a
        password out of it; `docker/demo-sources/seed.sh` redirected it to
        /dev/null. The first was fixed and the second shipped, because this
        test matched only command substitution and read only `bin/mapp`.
        """
        offending = []
        for path in launcher_scripts():
            text = path.read_text()
            for match in re.finditer(r'="\$\((?:[^()]|\([^()]*\))*\)"', text, re.S):
                if "2>&1" in match.group(0):
                    continue
                if "--build" in match.group(0) and " run " in match.group(0):
                    line = text[: match.start()].count("\n") + 1
                    offending.append(f"{path.name}:{line} (captured)")
            # The second shape: a run whose stdout is sent elsewhere. The
            # command spans lines, so the redirection is looked for in the
            # whole continued statement rather than on the `run` line.
            for match in re.finditer(
                r'^[^\n#]*\srun\s(?:[^\n]*\\\n)*[^\n]*$', text, re.M
            ):
                statement = match.group(0)
                if "--build" not in statement:
                    continue
                # Only when stderr still reaches the terminal. That is the
                # configuration both real failures had, and it is the one
                # that makes BuildKit choose a console renderer for a stream
                # that is not one. `>/dev/null 2>&1` and `2>&1 | tee` send
                # both streams away, leave no console to detect, and are
                # exercised constantly by `./bin/mapp test` without failing.
                if "2>&1" in statement or re.search(r'2>\s*\S', statement):
                    continue
                if re.search(r'>\s*(/dev/null|&?\d|"?\$)', statement) or "| " in statement:
                    line = text[: match.start()].count("\n") + 1
                    offending.append(f"{path.name}:{line} (redirected)")
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


class BuildVisibilityTests(unittest.TestCase):
    """A compose call that can build must not have its stderr discarded.

    BuildKit writes progress to stderr, so `2>&1` to /dev/null on a command
    that may build turns a multi-minute compile into silence. `./bin/mapp
    init` did exactly that: it folded the database build into a silenced
    `up --detach --wait db`, and on a fresh machine that compiles PostGIS and
    H3 from source. The first ten minutes of the first command a new user runs
    printed nothing, with no way to tell a long build from a hang.

    Discarding stdout alone is fine and several commands do it deliberately --
    that suppresses compose's container chatter and leaves the build visible.
    """

    #: `up` builds a service whose image is missing unless told not to.
    CAN_BUILD = (" up ", " build ")

    def test_no_build_has_its_progress_discarded(self) -> None:
        offending = []
        for number, line in enumerate(LAUNCHER.read_text().splitlines(), start=1):
            if "compose[@]}\"" not in line:
                continue
            if not any(token in line for token in self.CAN_BUILD):
                continue
            if "--no-build" in line:
                continue
            if "2>&1" in line and "/dev/null" in line:
                offending.append(f"{number}: {line.strip()}")
        self.assertEqual(
            [], offending,
            "these compose commands can build and send stderr to /dev/null,"
            " where BuildKit writes its progress. Build as a separate visible"
            " step and leave the silenced command unable to build.",
        )

    def test_init_says_what_it_is_doing_before_each_slow_step(self) -> None:
        """The database build, the configuration build and the credential are
        three distinct waits. Naming them is what makes the silence between
        them legible as progress rather than as a hang."""
        text = LAUNCHER.read_text()
        start = text.index("bootstrap_admin_credential() {")
        block = text[start : text.index("\ninit_env() {", start)]
        # Announcements only. Every one of these phrases also appears in the
        # matching failure message, so a test that searched the whole function
        # passed with the announcements deleted -- which is how the first
        # version of this test behaved when it was mutated.
        announcements = "\n".join(
            line for line in block.splitlines()
            if "printf " in line and ">&2" not in line
        )
        for expected in ("packaged database image", "configuration service image",
                         "administrator credential"):
            with self.subTest(step=expected):
                self.assertIn(
                    expected, announcements,
                    "init no longer announces this step before waiting on it",
                )


class McpSurfaceSwitchTests(unittest.TestCase):
    """`bin/mapp` and `verify.sh` must decide the MCP surface the same way.

    One starts the services and the other probes them, so a disagreement means
    either services nobody verifies or a verification of services nobody
    started. Both halves of that have already shipped: `verify.sh` once
    required mcp-auth unconditionally, so a plain `./bin/mapp all` could never
    pass, and fixing it uncovered the identical bug in the metadata probe.

    The resolution reads the shell first and `.env` second, so the surface can
    be turned on for one command or set once for the deployment.
    """

    RESOLUTION = '"${MAPP_MCP:-$(dotenv_value MAPP_MCP)}"'
    VERIFY = ROOT / "scripts" / "verify.sh"

    def test_both_resolve_the_switch_identically(self) -> None:
        for path in (LAUNCHER, self.VERIFY):
            with self.subTest(file=path.name):
                self.assertIn(
                    self.RESOLUTION, path.read_text(),
                    "this file no longer resolves MAPP_MCP the same way as the"
                    " other, so the two can disagree about whether the agent"
                    " surface is running",
                )

    def test_neither_reads_the_shell_alone(self) -> None:
        """The old spelling. It ignored `.env` entirely, which made MAPP_MCP
        the odd one out beside MAPP_DEMO_SOURCES and meant setting it in
        `.env` silently started nothing."""
        for path in (LAUNCHER, self.VERIFY):
            with self.subTest(file=path.name):
                self.assertNotIn('"${MAPP_MCP:-0}"', path.read_text())

    def test_the_switch_is_in_the_env_template(self) -> None:
        """Otherwise it is only discoverable by reading the launcher."""
        keys = {
            line.split("=", 1)[0]
            for line in (ROOT / ".env.example").read_text().splitlines()
            if "=" in line and not line.startswith("#")
        }
        self.assertIn("MAPP_MCP", keys)

    def test_verify_resolves_it_once(self) -> None:
        """Two independent resolutions in one file is the same drift risk in
        miniature, and that is how the probe came to disagree with the
        required-services list."""
        text = self.VERIFY.read_text()
        self.assertEqual(
            1, text.count(self.RESOLUTION),
            "verify.sh resolves MAPP_MCP more than once; resolve it into"
            " mcp_surface and use that",
        )

    def test_all_registers_the_runtime_before_verification(self) -> None:
        """A fresh `all` must not leave every authenticated MCP call at 503."""
        text = LAUNCHER.read_text()
        branch = re.search(r"\n  all\)\n(.*?)\n    ;;", text, re.S)
        self.assertIsNotNone(branch, "the all command branch is missing")
        body = branch.group(1)
        started = body.index('up --detach --build "${runtime_services[@]}"')
        registration = re.search(
            r"^    ensure_runtime_client_registered$", body, re.M
        )
        self.assertIsNotNone(registration, "all does not register the MCP runtime")
        registered = registration.start()
        verified = body.index('"${ROOT_DIR}/scripts/verify.sh"')
        self.assertLess(started, registered)
        self.assertLess(registered, verified)
