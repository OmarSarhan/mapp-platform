import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.check_env import add_missing_defaults, keys


ROOT = Path(__file__).resolve().parents[2]


class CheckEnvironmentTests(unittest.TestCase):
    def test_init_generates_every_template_secret(self):
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        script = (ROOT / "bin/mapp").read_text(encoding="utf-8")
        start = script.index("init_env() {")
        init_env = script[start : script.index("\n}\n", start)]

        placeholders = set(
            re.findall(r"=(CHANGEME_[A-Z0-9_]+)$", template, re.MULTILINE)
        )
        # The trailing $ is required, not incidental: CHANGEME_SEMANTIC is a
        # prefix of CHANGEME_SEMANTIC_CRUD and CHANGEME_SEMANTIC_READER, and
        # an unanchored expression rewrote their heads and left a password of
        # <hex>_CRUD. Requiring the anchor here means a new placeholder that
        # collides with an existing one fails this test rather than shipping.
        generated = set(
            re.findall(
                r's/(CHANGEME_[A-Z0-9_]+)\$/\$\(openssl rand -hex \d+\)/',
                init_env,
            )
        )
        self.assertEqual(placeholders, generated)

    def test_reset_builds_every_runtime_image_before_it_destroys_anything(self):
        """A build failure must not be discovered after the volume is gone.

        The rebuild after deletion used to be the first time several images
        were built, so an unrelated build failure -- a withdrawn distribution
        package, which is what actually happened -- left the deployment
        deleted and half-rebuilt.
        """
        script = (ROOT / "bin/mapp").read_text(encoding="utf-8")
        start = script.index("  reset-data)")
        reset = script[start : script.index("\n  upgrade-derived)", start)]

        build_all = reset.index('build "${runtime_services[@]}"')
        removal = reset.index('docker volume rm "${database_volume}"')
        self.assertLess(build_all, removal)
        # And nothing after the boundary may build, or the guarantee is void.
        self.assertNotIn("--build", reset[removal:])

    def test_every_documented_wrapper_command_exists(self):
        """Copy-pasteable commands must be commands.

        Removing the packaged ETL left `./bin/mapp etl` and `census-etl` in
        the README and three other documents, through several documentation
        sweeps, because each sweep corrected the places it was looking at
        rather than the places the claim appeared. An operator following the
        README got "unknown command".

        usage() is the source of truth here rather than the dispatch table:
        it is what the wrapper tells an operator it supports.
        """
        script = (ROOT / "bin/mapp").read_text(encoding="utf-8")
        usage = script[script.index("usage() {"):]
        usage = usage[: usage.index("\n}\n")]
        advertised = set()
        for match in re.finditer(r'"  ([a-z][a-z0-9|-]*)', usage):
            advertised.update(match.group(1).split("|"))
        self.assertIn("verify", advertised, "usage parsing found nothing")

        documents = sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").glob("*.md"))
        documents.append(ROOT / "etl/README.md")
        unknown: dict[str, set[str]] = {}
        for document in documents:
            if not document.exists():
                continue
            # Historical records state what was true when they were written.
            if document.name in {"CHANGELOG.md", "validation-log.md"}:
                continue
            if "Superseded" in document.read_text(encoding="utf-8")[:400]:
                continue
            for match in re.finditer(
                r"\./bin/mapp ([a-z][a-z0-9-]*)",
                document.read_text(encoding="utf-8"),
            ):
                if match.group(1) not in advertised:
                    unknown.setdefault(match.group(1), set()).add(document.name)

        self.assertEqual(
            {},
            {name: sorted(files) for name, files in unknown.items()},
        )

    def test_reset_data_names_what_it_destroys_before_asking(self):
        """The warning is where consent is obtained, so it must be true.

        It said semantic history was preserved. That was correct while the
        catalogue was a SQLite file under var; it stopped being correct the
        moment the catalogue moved into the packaged database, which is the
        volume this command removes. An operator reading it would have agreed
        to something other than what happens.
        """
        result = subprocess.run(
            [ROOT / "bin/mapp", "reset-data"],
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, result.returncode)
        warning = result.stderr.lower()
        for destroyed in (
            "semantic catalogue",
            "curated meaning",
            "semantic proposals",
            "federation registry",
            "derived layers",
        ):
            with self.subTest(destroyed=destroyed):
                self.assertIn(destroyed, warning)
        # The claim that started this: semantic state must never be listed
        # among what survives.
        self.assertNotIn("semantic history, and public", warning)
        self.assertNotIn("semantic-history", warning)
        for preserved in ("source databases", "audit log", "artifacts"):
            with self.subTest(preserved=preserved):
                self.assertIn(preserved, warning)

    def test_init_rejects_an_unknown_argument_without_writing_anything(self):
        """init took no arguments and silently ignored any it was given.

        Now that --demo changes what the file says, a typo must not be
        swallowed: an operator who writes --dem0 should be told, not handed a
        non-demo install that looks like a demo one.
        """
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / ".env"
            process_environment = os.environ.copy()
            process_environment["MAPP_ENV_FILE"] = str(environment)

            result = subprocess.run(
                [ROOT / "bin/mapp", "init", "--dem0"],
                cwd=ROOT,
                env=process_environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("init [--demo]", result.stderr)
        self.assertFalse(environment.exists())

    def test_add_missing_defaults_generates_secret_placeholders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / ".env.example"
            environment = root / ".env"
            example.write_text(
                "PLAIN=value\nPASSWORD=CHANGEME_PASSWORD\n",
                encoding="utf-8",
            )
            environment.write_text("", encoding="utf-8")

            added = add_missing_defaults(example, environment, keys(environment))
            assignments = dict(
                line.split("=", 1)
                for line in environment.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")
            )

            self.assertEqual(added, 2)
            self.assertEqual(assignments["PLAIN"], "value")
            self.assertEqual(len(assignments["PASSWORD"]), 48)
            self.assertNotIn("CHANGEME", assignments["PASSWORD"])

    def test_start_reports_missing_federation_settings_before_docker(self):
        # An empty env file: every deployment needs the federation settings
        # now, so there is no mode to opt into the requirement.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = root / ".env"
            environment.write_text("", encoding="utf-8")
            docker_marker = root / "docker-invoked"
            docker = root / "docker"
            docker.write_text(
                '#!/bin/sh\ntouch "$DOCKER_MARKER"\nexit 99\n',
                encoding="utf-8",
            )
            docker.chmod(0o700)
            process_environment = os.environ.copy()
            process_environment.update(
                {
                    "DOCKER_MARKER": str(docker_marker),
                    "MAPP_ENV_FILE": str(environment),
                    "PATH": f"{root}:{process_environment['PATH']}",
                }
            )

            result = subprocess.run(
                [ROOT / "bin/mapp", "config"],
                cwd=ROOT,
                env=process_environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("FEDERATION_DB_USER", result.stderr)
            self.assertIn("FEDERATION_DB_PASSWORD", result.stderr)
            self.assertIn("FEDERATION_DATABASE_URL", result.stderr)
            self.assertIn("./bin/mapp doctor --add-missing", result.stderr)
            self.assertFalse(docker_marker.exists())


if __name__ == "__main__":
    unittest.main()
