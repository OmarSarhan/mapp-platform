"""What the factory reads from the environment.

Every other test here injects a stub introspection client, which is right for
testing the middleware and wrong for testing the wiring: it means nothing
exercised the names `build_app` actually reads. It read
MCP_MCP_CLIENT_SECRET while every other file, compose and .env.example said
MAPP_MCP_CLIENT_SECRET, so the deployed runtime authenticated with an empty
secret, was refused by the authorization component, and answered 503 to every
call -- fail-closed, correct, and completely silent about why.

Found by driving a real token through the deployed stack. These are the tests
that would have found it sooner.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module  # noqa: E402


class EnvironmentWiringTests(unittest.TestCase):
    """The names, asserted against the ones the deployment actually sets."""

    #: Kept literal rather than imported. A constant shared with the code under
    #: test would agree with it however wrong both were, and agreement with
    #: compose is the whole property.
    EXPECTED = {
        "MCP_SITE",
        "MCP_ISSUER",
        "MCP_AUTH_URL",
        "MAPP_MCP_CLIENT_ID",
        "MAPP_MCP_CLIENT_SECRET",
    }

    def test_the_factory_reads_only_names_the_deployment_sets(self) -> None:
        source = (Path(app_module.__file__)).read_text(encoding="utf-8")
        import re

        read = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', source))
        unexpected = read - self.EXPECTED
        self.assertEqual(
            set(),
            unexpected,
            f"the factory reads {sorted(unexpected)}, which nothing sets;"
            " compose, .env.example and bin/mapp all use MAPP_MCP_*",
        )

    def test_the_secret_reaches_the_introspection_client(self) -> None:
        """Not merely present in the source: actually handed over."""
        import os

        saved = {k: os.environ.get(k) for k in self.EXPECTED}
        os.environ["MAPP_MCP_CLIENT_SECRET"] = "wired-secret"
        os.environ["MAPP_MCP_CLIENT_ID"] = "mapp-mcp"
        try:
            _, resource = app_module.build_app(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"
            )
            from introspection_client import IntrospectionClient

            client = IntrospectionClient(
                "http://mcp-auth:8080",
                client_id="mapp-mcp",
                client_secret="wired-secret",
                resource=resource.resource,
            )
            # The factory's client must authenticate identically to one built
            # by hand from the same values.
            built, _ = app_module.build_app(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"
            )
            guard = built._app
            self.assertEqual(
                client._authorization,
                guard._app._introspection._authorization,
                "the factory did not pass the deployment's credential through",
            )
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_the_introspection_resource_is_the_mcp_resource(self) -> None:
        """A token minted for the configuration API must not resolve here."""
        built, resource = app_module.build_app(
            origin="http://mcp.localhost", issuer="http://mcp.localhost"
        )
        introspection = built._app._app._introspection
        self.assertEqual(resource.resource, introspection.resource)
        self.assertTrue(introspection.resource.endswith("/mcp"))


if __name__ == "__main__":
    unittest.main()
