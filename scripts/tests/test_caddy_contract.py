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
        self.block = site_block("{$MCP_SITE:http://mcp.localhost} {")

    def test_exactly_four_public_paths_reach_the_socket(self) -> None:
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
