"""The metadata document and the challenge that points at it.

The property worth protecting is agreement: the document's `resource`, the
`resource_metadata` URL in a 401, and the entry in `authorization_servers` are
three strings a client compares, and a deployment where any two disagree is one
where discovery leads somewhere that cannot issue a usable token.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import protected_resource as pr  # noqa: E402
from app import build_app  # noqa: E402
from asgi_harness import call  # noqa: E402


class DocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = build_app(
            origin="https://mcp.example", issuer="https://mcp.example"
        )

    def fetch(self, path=pr.METADATA_PATH, method="GET"):
        return call(self.app, method=method, path=path)

    def test_the_document_matches_exactly(self) -> None:
        """Asserted whole, so it cannot quietly grow a field."""
        self.assertEqual(
            {
                "resource": "https://mcp.example/mcp",
                "authorization_servers": ["https://mcp.example"],
                "scopes_supported": ["mcp:connect", "inspect"],
                "bearer_methods_supported": ["header"],
            },
            self.fetch().json(),
        )

    def test_it_advertises_only_the_bootstrap_scopes(self) -> None:
        """A greedy client should not be able to read every permission the
        platform has out of a document served to anyone who asks."""
        for privileged in ("apply", "derive", "federation:provision", "full", "admin"):
            self.assertNotIn(privileged, self.fetch().json()["scopes_supported"])

    def test_both_well_known_paths_serve_it(self) -> None:
        self.assertEqual(self.fetch().json(), self.fetch(pr.ROOT_METADATA_PATH).json())

    def test_it_is_unauthenticated_and_cacheable(self) -> None:
        response = self.fetch()
        self.assertEqual(200, response.status)
        self.assertNotIn("www-authenticate", response.headers)
        self.assertIn("max-age", response.headers["cache-control"])

    def test_head_returns_the_headers_without_the_body(self) -> None:
        response = self.fetch(method="HEAD")
        self.assertEqual(200, response.status)
        self.assertEqual(b"", response.body)

    def test_it_is_read_only(self) -> None:
        response = self.fetch(method="POST")
        self.assertEqual(405, response.status)
        self.assertEqual("GET, HEAD", response.headers["allow"])


class AgreementTests(unittest.TestCase):
    """The three identifiers a client compares must be one derivation."""

    def test_the_resource_the_url_and_the_document_agree(self) -> None:
        _, resource = build_app(origin="https://mcp.example/", issuer="https://iss.example/")
        document = resource.document()
        self.assertEqual(document["resource"], resource.resource)
        self.assertIn(f'resource_metadata="{resource.metadata_url}"', resource.challenge())
        self.assertEqual([resource.issuer], document["authorization_servers"])
        # And a trailing slash on either input does not produce a doubled one.
        self.assertEqual("https://mcp.example/mcp", document["resource"])
        self.assertEqual("https://iss.example", document["authorization_servers"][0])

    def test_the_metadata_url_is_the_path_specific_form(self) -> None:
        """Preferred because the canonical resource ends in /mcp; a root
        document is unambiguous only when the origin serves one resource."""
        _, resource = build_app(origin="https://mcp.example", issuer="https://mcp.example")
        self.assertTrue(resource.metadata_url.endswith("/oauth-protected-resource/mcp"))

    def test_an_issuer_that_differs_from_the_origin_is_carried_through(self) -> None:
        """They are separate values: the issuer is compared, never fetched, and
        mapp-mcp cannot reach the public origin from its internal network."""
        _, resource = build_app(origin="https://mcp.example", issuer="https://auth.example")
        self.assertEqual(
            ["https://auth.example"], resource.document()["authorization_servers"]
        )


class ChallengeTests(unittest.TestCase):
    def setUp(self) -> None:
        _, self.resource = build_app(
            origin="https://mcp.example", issuer="https://mcp.example"
        )

    def test_a_missing_token_carries_no_bearer_error_code(self) -> None:
        """There is nothing wrong with the credential; there is not one.

        Telling a client holding no token that its token is invalid sends it
        looking for a credential to repair instead of one to obtain.
        """
        challenge = self.resource.challenge()
        self.assertNotIn("error=", challenge)
        self.assertIn('resource_metadata="https://mcp.example/.well-known', challenge)

    def test_an_invalid_token_says_so(self) -> None:
        challenge = self.resource.challenge(error="invalid_token")
        self.assertIn('error="invalid_token"', challenge)

    def test_insufficient_scope_names_every_required_scope_in_one_value(self) -> None:
        """One space-delimited value, so a client can ask once rather than
        discovering the next missing scope on the next refusal."""
        challenge = self.resource.challenge(
            error="insufficient_scope", scope="derive semantic:inspect"
        )
        self.assertIn('scope="derive semantic:inspect"', challenge)

    def test_every_challenge_names_the_metadata_url(self) -> None:
        for kwargs in ({}, {"error": "invalid_token"}, {"error": "insufficient_scope"}):
            with self.subTest(kwargs=kwargs):
                self.assertIn(
                    f'resource_metadata="{self.resource.metadata_url}"',
                    self.resource.challenge(**kwargs),
                )


if __name__ == "__main__":
    unittest.main()
