from __future__ import annotations

import unittest

from scripts.validate_database_url import DatabaseUrlError
from scripts.validate_database_url import validate_external_database_url


class ValidateExternalDatabaseUrlTests(unittest.TestCase):
    def test_accepts_remote_postgresql_uri(self) -> None:
        validate_external_database_url(
            "postgresql://reader:encoded@example.internal:5432/maps?sslmode=require"
        )

    def test_accepts_remote_ipv6_uri(self) -> None:
        validate_external_database_url("postgresql://reader@[2001:db8::10]:5432/maps")

    def test_rejects_bundled_service_hostname(self) -> None:
        for value in (
            "postgresql://reader@db:5432/maps",
            "postgresql://reader@%64%62:5432/maps",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(DatabaseUrlError, "bundled Compose hostname"):
                    validate_external_database_url(value)

    def test_rejects_localhost(self) -> None:
        for host in ("localhost", "postgres.localhost", "127.0.0.1", "[::1]"):
            with self.subTest(host=host):
                with self.assertRaises(DatabaseUrlError):
                    validate_external_database_url(
                        f"postgresql://reader@{host}:5432/maps"
                    )

    def test_rejects_query_host_and_service_overrides(self) -> None:
        for query in (
            "host=db",
            "h%6fst=127.0.0.1",
            "hostaddr=127.0.0.1",
            "service=local",
            "servicefile=%2Ftmp%2Fpg_service.conf",
        ):
            with self.subTest(query=query):
                with self.assertRaisesRegex(DatabaseUrlError, "must not override"):
                    validate_external_database_url(
                        f"postgresql://reader@remote.example:5432/maps?{query}"
                    )

    def test_rejects_ambiguous_numeric_loopback_hosts(self) -> None:
        for host in ("2130706433", "127.1", "0177.0.0.1", "0x7f000001"):
            with self.subTest(host=host):
                with self.assertRaises(DatabaseUrlError):
                    validate_external_database_url(
                        f"postgresql://reader@{host}:5432/maps"
                    )

    def test_rejects_malformed_or_encoded_control_hostnames(self) -> None:
        for host in ("bad_name.example", "%0alocalhost", "bad..example"):
            with self.subTest(host=host):
                with self.assertRaises(DatabaseUrlError):
                    validate_external_database_url(
                        f"postgresql://reader@{host}:5432/maps"
                    )

    def test_rejects_missing_or_non_postgresql_uri(self) -> None:
        for value in (
            "",
            "maps.internal",
            "https://maps.internal/database",
            "postgresql://reader@remote.example/maps#",
            "postgresql://reader@remote.example/maps#fragment",
        ):
            with self.subTest(value=value):
                with self.assertRaises(DatabaseUrlError):
                    validate_external_database_url(value)


if __name__ == "__main__":
    unittest.main()
