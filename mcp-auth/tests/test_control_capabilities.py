"""What each confidential client may ask the control listener for.

The listener authenticated its callers and then treated them all alike: any
registered confidential client could introspect, exchange, revoke and redeem. So
the configuration API's credential could mint an execution token for an
allowlisted operation -- the exact privilege the exchange exists to gate -- and
the configuration API is the component with the largest attack surface on the
platform. Authenticating a caller without asking what it is for is most of an
access control, and most is not enough.

These drive the real endpoints over HTTP, because the property is about what the
listener answers, not about what a helper returns.
"""

from __future__ import annotations

import base64
import http.client
import json
import sys
import threading
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as server_module  # noqa: E402
from issuer import MappAuthorizationServer  # noqa: E402
from models import Client  # noqa: E402
from server import ControlServer  # noqa: E402
from stub_store import StubStore  # noqa: E402

SECRET = "control-secret"


class CapabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = StubStore()
        self.authorization = MappAuthorizationServer(
            self.store,
            issuer="http://mcp.localhost",
            resource="http://mcp.localhost/mcp",
            config_api_resource="http://config.localhost/api",
        )
        self.control = ControlServer(("127.0.0.1", 0), self.authorization)
        threading.Thread(target=self.control.serve_forever, daemon=True).start()
        self.addCleanup(self.control.server_close)
        self.addCleanup(self.control.shutdown)

    def register(self, client_id: str, capabilities: tuple[str, ...]) -> None:
        self.store.add_client(
            Client(
                client_id=client_id,
                name=client_id,
                redirect_uris=(),
                scopes=(),
                token_endpoint_auth_method="client_secret_basic",
                client_secret=SECRET,
                capabilities=capabilities,
            )
        )

    def call(self, path: str, client_id: str, body: dict | None = None):
        credentials = base64.b64encode(f"{client_id}:{SECRET}".encode()).decode()
        payload = urllib.parse.urlencode(body or {"token": "mapp_a_whatever"})
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.control.server_address[1], timeout=10
        )
        try:
            connection.request(
                "POST",
                path,
                body=payload,
                headers={
                    "Authorization": "Basic " + credentials,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(payload.encode())),
                },
            )
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()


class RefusalTests(CapabilityTestCase):
    PATHS = {
        "introspect": "/internal/oauth/introspect",
        "exchange": "/internal/oauth/exchange",
        "revoke": "/internal/oauth/revoke",
        "redeem": "/internal/oauth/redeem",
    }

    def test_a_client_with_no_capabilities_is_refused_everywhere(self) -> None:
        """The safe default, and what a client registered before this gets."""
        self.register("bare", ())
        for capability, path in self.PATHS.items():
            with self.subTest(capability=capability):
                status, body = self.call(path, "bare")
                self.assertEqual(403, status)
                self.assertEqual("unauthorized_client", json.loads(body)["error"])

    def test_each_capability_opens_only_its_own_endpoint(self) -> None:
        """Held one at a time, or a single over-broad grant would hide here."""
        for capability, path in self.PATHS.items():
            with self.subTest(capability=capability):
                client_id = f"only-{capability}"
                self.register(client_id, (capability,))
                status, _ = self.call(path, client_id)
                self.assertNotEqual(
                    403, status, f"{capability} did not open {path}"
                )
                for other, other_path in self.PATHS.items():
                    if other == capability:
                        continue
                    status, _ = self.call(other_path, client_id)
                    self.assertEqual(
                        403, status, f"{capability} also opened {other_path}"
                    )

    def test_the_refusal_is_403_not_401(self) -> None:
        """The credential is fine; it is not for this.

        A 401 would send a correctly configured component to go and repair the
        one thing about it that is not wrong.
        """
        self.register("bare", ())
        status, body = self.call("/internal/oauth/exchange", "bare")
        self.assertEqual(403, status)
        self.assertIn("not permitted", json.loads(body)["error_description"])


class PlatformClientTests(unittest.TestCase):
    """The two real clients, and the grants the platform gives them.

    Read from the code that registers them rather than restated, so a change
    there has to come past this test.
    """

    @staticmethod
    def granted(path: str, anchor: str) -> str:
        """The capabilities tuple alone, not the block around it.

        A first version read the whole call and matched "exchange" in a comment
        explaining why exchange is *not* granted -- the same way round as a
        test earlier in this project that asserted a flag was absent from a
        block where it appeared only in its own comment. Read the value.
        """
        source = (Path(__file__).resolve().parents[2] / path).read_text(
            encoding="utf-8"
        )
        start = source.index("capabilities=(", source.index(anchor))
        return source[start : source.index(")", start) + 1]

    def test_the_configuration_api_cannot_exchange(self) -> None:
        """The finding this whole change came from.

        Minting an execution credential is the runtime's job. The configuration
        API serves the dashboard and the public API, so it is the most exposed
        component on the platform and the last one that should be able to.
        """
        block = self.granted("config-ui/app.py", "CONTROL.ensure_oauth_client(")
        self.assertIn('"introspect"', block)
        self.assertIn('"redeem"', block)
        self.assertNotIn('"exchange"', block)

    def test_the_runtime_can_exchange_but_not_redeem(self) -> None:
        """It mints the credential; the configuration API spends it.

        A runtime that could redeem could spend a credential against a request
        the configuration API never saw, which is the binding the whole
        exchange design rests on.
        """
        block = self.granted("config-ui/config_admin.py", "MCP_RUNTIME_CLIENT_ID,")
        self.assertIn('"introspect"', block)
        self.assertIn('"exchange"', block)
        self.assertNotIn('"redeem"', block)


if __name__ == "__main__":
    unittest.main()
