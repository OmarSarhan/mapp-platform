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
        generated = set(
            re.findall(
                r's/(CHANGEME_[A-Z0-9_]+)/\$\(openssl rand -hex \d+\)/',
                init_env,
            )
        )
        self.assertEqual(placeholders, generated)

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
