import unittest

from scripts.validate_production_env import validate


def production_values() -> dict[str, str]:
    return {
        "PRODUCTION_MAP_SITE": "https://maps.company.co.uk",
        "PRODUCTION_CONFIG_SITE": "https://config.company.co.uk",
        "PRODUCTION_CONFIG_ALLOWED_HOSTS": "config.company.co.uk,config-ui",
        "PRODUCTION_CADDY_EMAIL": "operations@company.co.uk",
        "HTTPS_PORT": "443",
    }


class ProductionEnvironmentTests(unittest.TestCase):
    def test_accepts_distinct_https_origins(self):
        self.assertEqual([], validate(production_values()))

    def test_rejects_http_local_and_identical_origins(self):
        values = production_values()
        values["PRODUCTION_MAP_SITE"] = "http://localhost"
        values["PRODUCTION_CONFIG_SITE"] = "http://localhost"
        errors = validate(values)
        self.assertTrue(any("PRODUCTION_MAP_SITE" in error for error in errors))
        self.assertTrue(any("PRODUCTION_CONFIG_SITE" in error for error in errors))

        values = production_values()
        values["PRODUCTION_CONFIG_SITE"] = values["PRODUCTION_MAP_SITE"]
        values["PRODUCTION_CONFIG_ALLOWED_HOSTS"] = "maps.company.co.uk"
        self.assertTrue(any("distinct" in error for error in validate(values)))

    def test_rejects_same_canonical_hostname_regardless_of_case(self):
        values = production_values()
        values["PRODUCTION_MAP_SITE"] = "https://Maps.Company.CO.UK"
        values["PRODUCTION_CONFIG_SITE"] = "https://maps.company.co.uk"
        values["PRODUCTION_CONFIG_ALLOWED_HOSTS"] = "maps.company.co.uk"
        self.assertTrue(any("distinct hostnames" in error for error in validate(values)))

    def test_requires_config_host_and_real_contact(self):
        values = production_values()
        values["PRODUCTION_CONFIG_ALLOWED_HOSTS"] = "*"
        values["PRODUCTION_CADDY_EMAIL"] = "admin@example.invalid"
        errors = validate(values)
        self.assertTrue(any("wildcard" in error for error in errors))
        self.assertTrue(any("configuration hostname" in error for error in errors))
        self.assertTrue(any("non-placeholder" in error for error in errors))

    def test_rejects_private_single_label_and_reserved_hosts(self):
        for origin in (
            "https://10.0.0.2",
            "https://config",
            "https://maps.example.org",
        ):
            with self.subTest(origin=origin):
                values = production_values()
                values["PRODUCTION_MAP_SITE"] = origin
                self.assertTrue(
                    any(
                        "PRODUCTION_MAP_SITE" in error
                        for error in validate(values)
                    )
                )

    def test_rejects_malformed_dns_hostnames(self):
        long_label = "a" * 64
        overlong_hostname = ".".join(["a" * 63] * 4)
        for origin in (
            "https://bad_name.company.com",
            "https://-maps.company.com",
            "https://maps-.company.com",
            "https://maps..company.com",
            f"https://{long_label}.company.com",
            f"https://{overlong_hostname}",
            "https://999.999.999.999",
        ):
            with self.subTest(origin=origin):
                values = production_values()
                values["PRODUCTION_MAP_SITE"] = origin
                self.assertTrue(
                    any(
                        "PRODUCTION_MAP_SITE" in error
                        for error in validate(values)
                    )
                )

    def test_rejects_ip_nonstandard_port_and_trailing_dot_origins(self):
        for origin in (
            "https://8.8.8.8",
            "https://[2001:4860:4860::8888]",
            "https://maps.company.co.uk:8443",
            "https://maps.company.co.uk.",
        ):
            with self.subTest(origin=origin):
                values = production_values()
                values["PRODUCTION_MAP_SITE"] = origin
                self.assertTrue(
                    any(
                        "PRODUCTION_MAP_SITE" in error
                        for error in validate(values)
                    )
                )

        values = production_values()
        values["PRODUCTION_CONFIG_ALLOWED_HOSTS"] = (
            "config.company.co.uk.,config-ui"
        )
        self.assertTrue(any("trailing-dot" in error for error in validate(values)))

        values = production_values()
        values["HTTPS_PORT"] = "3443"
        self.assertTrue(any("HTTPS_PORT" in error for error in validate(values)))


if __name__ == "__main__":
    unittest.main()
