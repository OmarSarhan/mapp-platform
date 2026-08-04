from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CaddyContractTests(unittest.TestCase):
    def test_request_body_limit_matches_the_api_binary_limit(self) -> None:
        caddyfile = (ROOT / "docker/caddy/Caddyfile").read_text(encoding="utf-8")

        self.assertIn("max_size 5MiB", caddyfile)
        self.assertNotIn("max_size 5MB", caddyfile)


if __name__ == "__main__":
    unittest.main()
