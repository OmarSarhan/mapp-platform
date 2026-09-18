import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.production_acceptance import (
    ROOT,
    compose_command,
    environment_checks,
    public_checks,
    rehearsal_check,
    write_evidence,
)


def valid_environment(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "MAPP_ENVIRONMENT=production",
                "PRODUCTION_MAP_SITE=https://maps.company.co.uk",
                "PRODUCTION_CONFIG_SITE=https://config.company.co.uk",
                "PRODUCTION_MCP_SITE=https://mcp.company.co.uk",
                "PRODUCTION_CONFIG_ALLOWED_HOSTS=config.company.co.uk,config-ui",
                "PRODUCTION_CADDY_EMAIL=operations@company.co.uk",
                "EDGE_BIND_ADDRESS=0.0.0.0",
                "HTTP_PORT=80",
                "HTTPS_PORT=443",
                "CONFIG_UID=1000",
                "CONFIG_GID=1000",
                f"SEMANTIC_INTERNAL_TOKEN={'a' * 64}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


class ProductionAcceptanceTests(unittest.TestCase):
    def test_the_bundled_database_is_always_in_the_verified_model(self):
        """An empty values mapping must still resolve the database overlay.

        This is the shape acceptance sees once MAPP_DATABASE_MODE is gone from
        .env: any mode lookup returns None, and a lookup-guarded overlay would
        drop out silently, leaving production acceptance reporting green over a
        model with no database service in it.
        """
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / ".env"
            environment.write_text("", encoding="utf-8")
            command = compose_command(environment, {})

        self.assertIn(str(ROOT / "compose.bundled-db.yaml"), command)

    def test_environment_is_validated_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / ".env"
            valid_environment(environment)
            values, checks = environment_checks(environment)
        self.assertEqual("production", values["MAPP_ENVIRONMENT"])
        self.assertTrue(all(item.status == "pass" for item in checks))
        serialized = json.dumps([item.__dict__ for item in checks])
        self.assertNotIn("operations@company.co.uk", serialized)
        self.assertNotIn("maps.company.co.uk", serialized)
        self.assertNotIn("a" * 64, serialized)

    def test_environment_permissions_are_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / ".env"
            valid_environment(environment)
            environment.chmod(0o640)
            _, checks = environment_checks(environment)
        permissions = next(item for item in checks if item.id == "environment.permissions")
        self.assertEqual("fail", permissions.status)

    def test_rehearsal_requires_explicit_execution(self):
        supplied = rehearsal_check("backup.create", Path("/tmp/hook"), False)
        missing = rehearsal_check("backup.create", None, True)
        self.assertEqual("pending", supplied.status)
        self.assertEqual("pending", missing.status)

    @patch("scripts.production_acceptance.run_quiet")
    def test_successful_hook_records_only_output_digest(self, run_quiet):
        with tempfile.TemporaryDirectory() as directory:
            hook = Path(directory) / "hook"
            hook.write_text("#!/bin/sh\n", encoding="utf-8")
            hook.chmod(0o700)
            run_quiet.return_value.returncode = 0
            run_quiet.return_value.stdout = b"sensitive output"
            run_quiet.return_value.stderr = b""
            result = rehearsal_check("backup.create", hook, True)
        self.assertEqual("pass", result.status)
        self.assertNotIn("sensitive output", result.reason)

    def test_evidence_is_written_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "evidence.json"
            write_evidence(target, {"schemaVersion": 1})
            self.assertEqual({"schemaVersion": 1}, json.loads(target.read_text()))
            self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(target.parent.stat().st_mode))


if __name__ == "__main__":
    unittest.main()


class PublishedOriginCoverageTests(unittest.TestCase):
    """Acceptance must cover every origin the platform publishes.

    It covered two of three. The MCP origin was the newest and the only one
    whose Caddy site proxies to a Unix socket rather than a container port --
    so the origin most likely to be misconfigured was the one with no live DNS
    or TLS evidence, while the report still read green.
    """

    VALUES = {
        "PRODUCTION_MAP_SITE": "https://maps.company.co.uk",
        "PRODUCTION_CONFIG_SITE": "https://config.company.co.uk",
        "PRODUCTION_MCP_SITE": "https://mcp.company.co.uk",
    }

    def test_all_three_origins_are_checked(self) -> None:
        names = {item.id for item in public_checks(self.VALUES, live=False)}
        for label in ("map", "config", "mcp"):
            for kind in ("dns", "tls"):
                with self.subTest(check=f"{kind}.{label}"):
                    self.assertIn(f"{kind}.{label}", names)

    def test_an_unchecked_origin_is_reported_pending_not_passing(self) -> None:
        """Without --live these are pending. Pending must never read as pass."""
        statuses = {
            item.id: item.status
            for item in public_checks(self.VALUES, live=False)
        }
        self.assertEqual("pending", statuses["dns.mcp"])
        self.assertEqual("pending", statuses["tls.mcp"])
