"""Provisioning mapp-mcp's row, and refusing to when there is nothing to store.

The MCP runtime authenticates to the control listener to introspect a token A
and to exchange one, so it needs a confidential client row. It cannot write that
row itself: it holds no database credential, deliberately, and reaches platform
state only through authenticated API calls. This component holds the DSN, so it
writes the row.

Storage is not re-asserted here. Provisioning goes through ``add_client``, and
``test_sql_store`` already proves that stores a digest rather than the
plaintext -- against the real store, which is the only one where "at rest"
means anything. StubStore keeps the secret in memory by design, so asserting
the hashing property against it would assert something the double does not do.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402
from stub_store import StubStore  # noqa: E402


class ProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = StubStore()
        self._saved = server.os.environ.get("MAPP_MCP_CLIENT_SECRET")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            server.os.environ.pop("MAPP_MCP_CLIENT_SECRET", None)
        else:
            server.os.environ["MAPP_MCP_CLIENT_SECRET"] = self._saved

    def set_secret(self, value) -> None:
        if value is None:
            server.os.environ.pop("MAPP_MCP_CLIENT_SECRET", None)
        else:
            server.os.environ["MAPP_MCP_CLIENT_SECRET"] = value

    def test_a_supplied_secret_writes_a_confidential_client(self) -> None:
        self.set_secret("runtime-secret")
        self.assertTrue(server.provision_runtime_client(self.store))
        client = self.store.query_client(server.MCP_RUNTIME_CLIENT_ID)
        self.assertIsNotNone(client)
        self.assertEqual("client_secret_basic", client.token_endpoint_auth_method)
        self.assertTrue(client.check_client_secret("runtime-secret"))

    def test_it_holds_no_redirect_uri_scope_or_grant_type(self) -> None:
        """It never appears in an authorization request.

        A redirect URI would be a capability with no purpose, and a grant type
        would let it ask for a token at the edge rather than only authenticate
        to the control listener.
        """
        self.set_secret("runtime-secret")
        server.provision_runtime_client(self.store)
        client = self.store.query_client(server.MCP_RUNTIME_CLIENT_ID)
        self.assertEqual((), client.redirect_uris)
        self.assertEqual((), client.scopes)
        self.assertEqual((), client.grant_types)

    def test_no_secret_writes_nothing(self) -> None:
        """Off, rather than on with an empty credential.

        A blank secret that provisioned a row would be a confidential client
        authenticated by the empty string -- which is worse than no client,
        because something would appear to be configured.
        """
        for value in (None, "", "   "):
            with self.subTest(value=repr(value)):
                store = StubStore()
                self.set_secret(value)
                self.assertFalse(server.provision_runtime_client(store))
                self.assertIsNone(store.query_client(server.MCP_RUNTIME_CLIENT_ID))

    def test_a_rotated_secret_replaces_the_old_one(self) -> None:
        """The deployment supplies it, so a restart is the deployment asserting
        what the secret now is."""
        self.set_secret("first")
        server.provision_runtime_client(self.store)
        self.set_secret("second")
        server.provision_runtime_client(self.store)
        client = self.store.query_client(server.MCP_RUNTIME_CLIENT_ID)
        self.assertTrue(client.check_client_secret("second"))
        self.assertFalse(client.check_client_secret("first"))


if __name__ == "__main__":
    unittest.main()
