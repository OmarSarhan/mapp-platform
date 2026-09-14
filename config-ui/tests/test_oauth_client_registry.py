"""The agent-client registry, and the validation that makes it safe.

Registration is the only way an agent client comes to exist, so what it refuses
is the platform's whole admission policy for agents. The redirect-URI rules
matter most: the authorization server matches them exactly, with no prefix or
wildcard allowance, so a permissive entry here is a working way to have
authorization codes delivered to somebody else.

`mcp-auth/tests/test_registered_client.py` drives a registered client through
the real flow. This file is about what never gets registered in the first place.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_plane import ControlStore

from control_fixture import ControlStoreTestCase


class RegistryTestCase(ControlStoreTestCase):
    """An initialised store. Registration requires one, as every method does.

    The fixture truncates `metadata` and `admin_credential` too, so a store is
    uninitialised until a test says otherwise -- which is the correct default:
    an uninitialised control plane must refuse rather than half-work.
    """

    def setUp(self) -> None:
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ControlStore(Path(self.directory.name) / "control")
        self.store.initialize("correct horse battery staple", "instance")


class RegistrationTests(RegistryTestCase):
    def register(self, **overrides):
        fields = {
            "name": "Claude Code",
            "redirect_uris": ["http://127.0.0.1:33418/callback"],
            "scopes": ["mcp:connect", "apply"],
        }
        fields.update(overrides)
        return self.store.register_oauth_client(**fields)

    def test_a_registered_client_is_public_and_has_no_secret(self) -> None:
        """An agent cannot keep a secret, so it is never issued one.

        A secret would have to live unprotected on the operator's machine. PKCE
        is what binds the authorization code instead, and the server requires
        S256 of every client including confidential ones.
        """
        client_id = self.register()
        self.assertTrue(client_id.startswith("mcp-"))
        client = next(
            item
            for item in self.store.list_oauth_clients()
            if item["clientId"] == client_id
        )
        self.assertFalse(client["confidential"])
        self.assertIsNone(client["disabled"])
        self.assertEqual(["mcp:connect", "apply"], client["scopes"])

    def test_each_registration_gets_its_own_id(self) -> None:
        self.assertNotEqual(self.register(), self.register())

    def test_a_name_is_required(self) -> None:
        for name in ("", "   "):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.register(name=name)

    def test_at_least_one_redirect_uri_and_scope_are_required(self) -> None:
        with self.assertRaises(ValueError):
            self.register(redirect_uris=[])
        with self.assertRaises(ValueError):
            self.register(scopes=[])

    def test_duplicates_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.register(redirect_uris=["https://a.example/cb"] * 2)
        with self.assertRaises(ValueError):
            self.register(scopes=["apply", "apply"])


class RedirectUriTests(RegistryTestCase):
    """The server matches these exactly, so registration is the only filter."""

    def test_https_is_accepted_for_any_host(self) -> None:
        self.assertEqual(
            "https://agent.example/callback",
            self.store.check_redirect_uri("https://agent.example/callback"),
        )

    def test_http_is_accepted_only_for_loopback(self) -> None:
        for value in (
            "http://127.0.0.1:33418/callback",
            "http://localhost:8080/cb",
            "http://[::1]:9/cb",
        ):
            with self.subTest(value=value):
                self.assertEqual(value, self.store.check_redirect_uri(value))

    def test_http_to_any_other_host_is_refused(self) -> None:
        """Plaintext would carry an authorization code over the network."""
        for value in (
            "http://agent.example/cb",
            "http://127.0.0.1.attacker.example/cb",
            "http://localhost.attacker.example/cb",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.check_redirect_uri(value)

    def test_a_relative_or_schemeless_uri_is_refused(self) -> None:
        for value in ("/callback", "callback", "//agent.example/cb"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.check_redirect_uri(value)

    def test_a_fragment_is_refused(self) -> None:
        # RFC 6749 s3.1.2: the endpoint URI must not include a fragment.
        with self.assertRaises(ValueError):
            self.store.check_redirect_uri("https://agent.example/cb#part")

    def test_userinfo_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.check_redirect_uri("https://user:pw@agent.example/cb")

    def test_whitespace_and_newlines_are_refused(self) -> None:
        for value in (
            "https://agent.example/cb extra",
            "https://agent.example/cb\nLocation: elsewhere",
            "https://agent.example/cb\r\n",
            "",
            "   ",
        ):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    self.store.check_redirect_uri(value)

    def test_a_custom_native_scheme_is_accepted(self) -> None:
        """RFC 8252 private-use schemes are how a desktop agent gets called back."""
        self.assertEqual(
            "com.example.agent:/oauth",
            self.store.check_redirect_uri("com.example.agent:/oauth"),
        )

    def test_a_scheme_that_is_not_reverse_dns_is_refused(self) -> None:
        """The "any other scheme" allowance admitted javascript:, data: and file:.

        RFC 8252 s7.1 says a private-use scheme is a reverse-DNS name the
        application controls. Without that rule these registered successfully,
        and the authorization server redirects to a registered URI exactly as
        stored.
        """
        for value in (
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "vbscript:msgbox(1)",
            "about:blank",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.check_redirect_uri(value)

    def test_the_scheme_is_normalised_to_lower_case(self) -> None:
        """The server matches byte for byte, so casing has to be settled here.

        A stored mixed-case scheme would only match a client that repeated the
        same casing. RFC 3986 s3.1 makes the scheme case-insensitive, so the
        client is not wrong to send either.
        """
        self.assertEqual(
            "https://agent.example/CB",
            self.store.check_redirect_uri("HtTpS://agent.example/CB"),
        )
        self.assertEqual(
            "com.example.agent:/oauth",
            self.store.check_redirect_uri("COM.Example.Agent:/oauth"),
        )
        # The path keeps its casing: only the scheme is case-insensitive.
        self.assertEqual(
            "https://agent.example/Callback",
            self.store.check_redirect_uri("https://agent.example/Callback"),
        )


class ScopeTests(RegistryTestCase):
    def test_the_reserved_scopes_are_never_issued_to_an_agent(self) -> None:
        """`full` is on the broker's deny-list and reaches unclassified routes.

        _required_scope returns "full" for anything its table does not
        classify, which is what makes an unclassified route unreachable. An
        agent holding it would reach all of them.
        """
        for reserved in ("full", "admin"):
            with self.subTest(scope=reserved):
                with self.assertRaises(ValueError):
                    self.store.register_oauth_client(
                        name="Greedy",
                        redirect_uris=["https://a.example/cb"],
                        scopes=["apply", reserved],
                    )

    def test_a_malformed_scope_is_refused(self) -> None:
        for value in ("", "   ", "two words", "with\ttab", "with\nnewline"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    self.store.check_scope(value)

    def test_an_unknown_scope_is_accepted_here(self) -> None:
        """Deliberately not checked against a vocabulary.

        The authorization server decides what it will issue and derives that
        from the operation allowlist. Restating the vocabulary in a third place
        is a drift this platform has already been bitten by, and a scope the
        server will not issue produces invalid_scope at the authorization
        request -- a clear failure rather than a silent one.
        """
        self.assertEqual("not:a:real:scope", self.store.check_scope("not:a:real:scope"))


class DisableTests(RegistryTestCase):
    def test_disabling_is_reported_once(self) -> None:
        client_id = self.store.register_oauth_client(
            name="Agent",
            redirect_uris=["https://a.example/cb"],
            scopes=["apply"],
        )
        self.assertTrue(self.store.disable_oauth_client(client_id))
        self.assertFalse(self.store.disable_oauth_client(client_id))
        client = next(
            item
            for item in self.store.list_oauth_clients()
            if item["clientId"] == client_id
        )
        self.assertIsNotNone(client["disabled"])

    def test_an_unknown_client_is_reported_not_raised(self) -> None:
        self.assertFalse(self.store.disable_oauth_client("mcp-nosuch"))

    def test_a_confidential_service_client_is_refused(self) -> None:
        """Otherwise disabling it would quietly undo itself at the next restart.

        ensure_oauth_client re-enables the configuration API's own row on every
        start-up from the deployment's secret, so this command would appear to
        work and then stop working. Clearing MCP_AUTH_CLIENT_SECRET is the
        lever, and it turns the feature off rather than leaving it half on.
        """
        self.store.ensure_oauth_client(
            "mapp-config-api", "a-secret", name="MAPP configuration API"
        )
        with self.assertRaises(ValueError) as caught:
            self.store.disable_oauth_client("mapp-config-api")
        self.assertIn("MCP_AUTH_CLIENT_SECRET", str(caught.exception))

    def test_disabling_does_not_revoke_the_grants(self) -> None:
        """Two different operator intentions, so two different acts.

        Withdrawing a client is not necessarily withdrawing the consents, and
        revoking a grant has its own audit meaning. Disabling still stops the
        client acting, because every check resolves the grant's client.
        """
        client_id = self.store.register_oauth_client(
            name="Agent",
            redirect_uris=["https://a.example/cb"],
            scopes=["apply"],
        )
        self.store.disable_oauth_client(client_id)
        clients = {item["clientId"] for item in self.store.list_oauth_clients()}
        self.assertIn(client_id, clients, "the row must survive being disabled")


class RuntimeClientTests(RegistryTestCase):
    """The MCP runtime's confidential client, registered by an operator.

    It cannot register itself: it holds no database credential by design and
    reaches platform state only through authenticated API calls. The
    authorization component could have written the row from its own environment
    and did for one commit, but that put the plaintext in a second service's
    configuration, made rotation a restart of the authorization server, and left
    the act without an author -- which matters once more than one person
    operates the platform.
    """

    RUNTIME = "mapp-mcp"

    def register(self, secret="runtime-secret"):
        self.store.ensure_oauth_client(
            self.RUNTIME, secret, name="MAPP MCP runtime"
        )
        return next(
            item
            for item in self.store.list_oauth_clients()
            if item["clientId"] == self.RUNTIME
        )

    def test_it_is_confidential_and_holds_nothing_else(self) -> None:
        """No redirect URI, no scopes: it never appears in an authorization
        request, so either would be a capability with no purpose."""
        client = self.register()
        self.assertTrue(client["confidential"])
        self.assertEqual([], client["scopes"])
        self.assertEqual([], client["redirectUris"])
        self.assertIsNone(client["disabled"])

    def test_registering_again_rotates_the_secret(self) -> None:
        """The reason an operator command beats an environment variable.

        Rotation is this call, and it takes effect for the authorization
        component immediately -- no restart, and nothing else holds the old
        value to get out of step with.
        """
        self.register("first")
        self.register("second")
        with self.store._db() as connection:
            row = connection.execute(
                "SELECT client_secret_hash FROM control.oauth_clients"
                " WHERE client_id = %s",
                (self.RUNTIME,),
            ).fetchone()
        import hashlib

        self.assertEqual(
            hashlib.sha256(b"second").hexdigest(), row["client_secret_hash"]
        )

    def test_the_plaintext_is_never_stored(self) -> None:
        self.register("runtime-secret")
        with self.store._db() as connection:
            row = connection.execute(
                "SELECT client_secret_hash FROM control.oauth_clients"
                " WHERE client_id = %s",
                (self.RUNTIME,),
            ).fetchone()
        self.assertNotIn("runtime-secret", str(row))


class EmptyRegistryTests(RegistryTestCase):
    def test_an_empty_registry_lists_nothing(self) -> None:
        """The state a correct deployment starts in, before an operator acts."""
        self.assertEqual([], self.store.list_oauth_clients())


if __name__ == "__main__":
    unittest.main()
