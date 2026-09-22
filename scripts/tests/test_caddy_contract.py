from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = (ROOT / "docker/caddy/Caddyfile").read_text(encoding="utf-8")


def site_block(opening: str) -> str:
    """The body of one site block, by brace matching.

    Substring assertions over the whole file would pass no matter which site a
    directive landed in, which is the one thing these tests exist to check.
    """
    # Count from the brace that OPENS the block, not from the start of the
    # marker: a site address like {$MCP_SITE:...} contains braces of its own.
    start = CADDYFILE.index(opening) + len(opening) - 1
    depth = 0
    for index in range(start, len(CADDYFILE)):
        if CADDYFILE[index] == "{":
            depth += 1
        elif CADDYFILE[index] == "}":
            depth -= 1
            if depth == 0:
                return CADDYFILE[start : index + 1]
    raise AssertionError(f"unterminated site block for {opening!r}")


def mcp_site_opening() -> str:
    """The `{$MCP_SITE:...} {` line that opens the MCP site block."""
    match = re.search(r"\{\$MCP_SITE:[^}]*\} \{", CADDYFILE)
    assert match, "the Caddyfile no longer has an MCP site block"
    return match.group(0)


#: Hosts an MCP client will send credentials to over plain http. Everything
#: else must be https. The Claude extension states the rule in its own
#: refusal, and authlib applies the same one server-side, which is why the
#: development deployment needs AUTHLIB_INSECURE_TRANSPORT at all.
TLS_EXEMPT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class HttpOriginTests(unittest.TestCase):
    """An http MCP origin must be one a client will actually use.

    `mcp.localhost` is a *subdomain* of localhost and is not exempt, so the
    Claude extension completed consent, received the authorization code and
    then refused to exchange it:

        Refusing to send credentials to non-https token endpoint
        'http://mcp.localhost/oauth/token'. OAuth token requests MUST use TLS
        (localhost / 127.0.0.1 / ::1 are exempt).

    Nothing on this side reported a fault -- the grant was live and the server
    was waiting -- so the failure was only visible in the client's log. A port
    keeps it a distinct origin from the map without leaving the exempt set.
    """

    def origins(self):
        """Every default MCP origin this repository ships."""
        template = (ROOT / ".env.example").read_text()
        found = {
            "env.example": re.search(r"^MCP_SITE=(\S+)", template, re.M).group(1),
            "Caddyfile": re.search(
                r"\{\$MCP_SITE:([^}]*)\}", CADDYFILE
            ).group(1),
        }
        return found

    def test_an_http_origin_uses_a_host_clients_exempt_from_tls(self) -> None:
        for where, origin in self.origins().items():
            with self.subTest(source=where, origin=origin):
                if not origin.startswith("http://"):
                    continue
                host = origin[len("http://"):].split("/")[0].rsplit(":", 1)[0]
                self.assertIn(
                    host, TLS_EXEMPT_HOSTS,
                    f"{origin} is plain http on a host no MCP client will send"
                    " credentials to. Use localhost, 127.0.0.1 or ::1 -- a"
                    " port keeps it a separate origin -- or serve it over TLS.",
                )

    def test_the_published_port_is_the_one_the_origin_names(self) -> None:
        """Caddy listens on the port inside MCP_SITE, so the port Compose
        publishes must be that same number. A mapping onto a fixed container
        port works until somebody changes MCP_SITE, and then publishes a port
        nothing is listening on -- with every container healthy."""
        template = (ROOT / ".env.example").read_text()
        site = re.search(r"^MCP_SITE=(\S+)", template, re.M).group(1)
        port = re.search(r"^MCP_PORT=(\S+)", template, re.M).group(1)
        self.assertTrue(
            site.endswith(f":{port}"),
            f"MCP_SITE ({site}) and MCP_PORT ({port}) name different ports",
        )
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn(
            "${MCP_PORT:-" + port + "}:${MCP_PORT:-" + port + '}"', compose,
            "Compose must publish the MCP port onto itself, because Caddy"
            " listens on whatever port MCP_SITE names",
        )

    def test_the_two_defaults_agree(self) -> None:
        """The Caddyfile default is what a deployment with no MCP_SITE gets,
        and .env.example is what every `./bin/mapp init` writes. If they
        disagree, one of them is never exercised."""
        origins = self.origins()
        self.assertEqual(origins["env.example"], origins["Caddyfile"])


class CaddyContractTests(unittest.TestCase):
    def test_request_body_limit_matches_the_api_binary_limit(self) -> None:
        self.assertIn("max_size 5MiB", CADDYFILE)
        self.assertNotIn("max_size 5MB", CADDYFILE)


class McpOriginTests(unittest.TestCase):
    """The public surface of the authorization component, as Caddy publishes it.

    None of this was pinned. The component's own route tables make the control
    endpoints absent from the edge listener independently -- that is the
    structural control, and it is tested in mcp-auth -- but Caddy is what
    decides which of the *edge* paths reach the socket at all, and what the
    component is told the client's address was.
    """

    def setUp(self) -> None:
        # Derived, not spelled out: the default origin moved once already
        # (mcp.localhost -> a loopback host and port) and this line was the
        # sibling that broke.
        self.block = site_block(mcp_site_opening())

    def test_exactly_four_authorization_paths_reach_that_socket(self) -> None:
        matcher = re.search(r"@auth_public path ([^\n]+)", self.block)
        self.assertIsNotNone(matcher, "the public path allowlist is gone")
        self.assertEqual(
            [
                "/.well-known/oauth-authorization-server",
                "/oauth/authorize",
                "/oauth/token",
                "/oauth/login",
            ],
            matcher.group(1).split(),
        )

    def test_exactly_three_runtime_paths_reach_the_runtime_socket(self) -> None:
        """The resource, and the two spellings of the document that names it.

        Held to an exact list for the same reason the authorization set is: a
        matcher that grew a path would publish a surface nobody reviewed, and
        this origin's whole design is that everything not named here is a 404.
        """
        matcher = re.search(r"@mcp_public path ([^\n]+)", self.block)
        self.assertIsNotNone(matcher, "the runtime path allowlist is gone")
        self.assertEqual(
            [
                "/mcp",
                "/.well-known/oauth-protected-resource",
                "/.well-known/oauth-protected-resource/mcp",
            ],
            matcher.group(1).split(),
        )

    def test_the_two_sockets_are_not_confused(self) -> None:
        """Each allowlist reaches its own component.

        Routing the RPC path to the authorization socket would answer 404 from
        a component that does not serve it, which reads as "the runtime is
        down" rather than "the edge is misrouted".
        """
        auth_handle = self.block.index("handle @auth_public")
        mcp_handle = self.block.index("handle @mcp_public")
        self.assertIn(
            "unix//run/mapp-auth/mapp-auth.sock",
            self.block[auth_handle:mcp_handle],
        )
        self.assertIn(
            "unix//run/mapp-mcp/mapp-mcp.sock",
            self.block[mcp_handle:self.block.index("handle {")],
        )

    def test_everything_else_on_this_origin_is_a_404(self) -> None:
        fallback = self.block[self.block.index("handle {"):]
        self.assertIn("respond 404", fallback)
        # And the catch-all comes last, or it would shadow the allowlist.
        self.assertGreater(
            self.block.index("handle {"), self.block.index("handle @auth_public")
        )

    def test_the_forwarded_address_is_overwritten_not_appended(self) -> None:
        """The component trusts this header unconditionally.

        Over an exclusive Unix socket the only peer is Caddy, so a
        client-supplied forwarding header would be indistinguishable from the
        real address -- and the login throttle is keyed on it, so an attacker
        who could set it would never be throttled.
        """
        for directive in (
            "header_up -Forwarded",
            "header_up -X-Real-IP",
            "header_up X-Forwarded-For {remote_host}",
        ):
            with self.subTest(directive=directive):
                self.assertIn(directive, self.block)

    def test_the_site_sets_no_content_security_policy(self) -> None:
        """Caddy's header directive REPLACES the upstream value.

        A site-level CSP would discard the nonce-based policy the consent and
        login pages emit, leaving them with default-src 'none' and no styling.
        Caddy may set CSP only where Caddy itself responds -- the 404 handler.
        """
        header_block = self.block[self.block.index("header {"):]
        header_block = header_block[: header_block.index("}")]
        self.assertNotIn("Content-Security-Policy", header_block)
        self.assertIn(
            "Content-Security-Policy", self.block[self.block.index("handle {"):]
        )

    def test_the_socket_path_is_the_one_compose_mounts(self) -> None:
        self.assertIn("reverse_proxy unix//run/mapp-auth/mapp-auth.sock", self.block)
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("./var/mcp-auth:/run/mapp-auth", compose)

    def test_no_unsupplied_placeholder_governs_the_body_limit(self) -> None:
        """MCP_MAX_REQUEST_BODY was a knob no compose file ever supplied."""
        self.assertIn("max_size 512KiB", self.block)
        # The placeholder form, not the name -- the comment explaining its
        # removal mentions it, and should not have to avoid doing so.
        self.assertNotIn("{$MCP_MAX_REQUEST_BODY", CADDYFILE)


if __name__ == "__main__":
    unittest.main()
