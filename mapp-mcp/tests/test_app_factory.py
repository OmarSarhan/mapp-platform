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

PLATFORM = Path(__file__).resolve().parents[2]


class EnvironmentWiringTests(unittest.TestCase):
    """The names, asserted against the ones the deployment actually sets."""

    #: Names the factory may read without compose setting them, because they
    #: have a working default and exist for tests and local runs. Anything else
    #: it reads must be something the deployment actually supplies.
    OPTIONAL = {"MAPP_MCP_CLIENT_ID"}

    @staticmethod
    def compose_environment() -> set:
        """The variable names compose gives this service, read from the file.

        Read rather than restated. The first version of this test kept a
        literal list, which is the shape that agrees with the code however
        wrong both are -- and it was already drifting, because the property
        being asserted is agreement with the *deployment*, not with a list
        somebody remembered to update.
        """
        import re

        compose = (PLATFORM / "compose.yaml").read_text(encoding="utf-8")
        service = compose[compose.index("\n  mapp-mcp:") :]
        service = service[: service.index("\n  config-ui:")]
        return set(re.findall(r"^      ([A-Z_]+):", service, re.MULTILINE))

    def test_the_factory_reads_only_names_the_deployment_sets(self) -> None:
        """Otherwise the runtime silently takes a default nobody configured.

        This is how the client secret was read under the wrong name for a whole
        commit: the factory asked for MCP_MCP_CLIENT_SECRET, compose set
        MAPP_MCP_CLIENT_SECRET, and the deployed runtime authenticated with an
        empty string and answered 503 to everything.
        """
        source = (Path(app_module.__file__)).read_text(encoding="utf-8")
        import re

        read = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', source))
        supplied = self.compose_environment() | self.OPTIONAL
        unexpected = read - supplied
        self.assertEqual(
            set(),
            unexpected,
            f"the factory reads {sorted(unexpected)}, which the mapp-mcp"
            f" compose service does not set (it sets"
            f" {sorted(self.compose_environment())})",
        )

    def test_the_deployment_sets_nothing_the_factory_ignores(self) -> None:
        """The other direction, which the first version missed entirely.

        A variable carefully plumbed through compose and never read is a
        setting an operator can change with no effect -- the quietest kind of
        wrong, because everything looks configured.
        """
        source = (Path(app_module.__file__)).read_text(encoding="utf-8")
        server = (PLATFORM / "mapp-mcp" / "server.py").read_text(encoding="utf-8")
        import re

        read = set(
            re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', source + server)
        )
        # Read by the SDK or by uvicorn rather than by our own code.
        indirect = {"MCP_RUNTIME_LOG_LEVEL"}
        ignored = self.compose_environment() - read - indirect
        self.assertEqual(
            set(),
            ignored,
            f"compose sets {sorted(ignored)} for mapp-mcp, and nothing reads it",
        )

    def test_the_secret_reaches_the_introspection_client(self) -> None:
        """Not merely present in the source: actually handed over."""
        import os

        saved = {
            k: os.environ.get(k)
            for k in self.compose_environment() | self.OPTIONAL
        }
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
