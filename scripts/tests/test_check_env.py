import tempfile
import unittest
from pathlib import Path

from scripts.check_env import add_missing_defaults, keys


class CheckEnvironmentTests(unittest.TestCase):
    def test_add_missing_defaults_generates_secret_placeholders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / ".env.example"
            environment = root / ".env"
            example.write_text(
                "PLAIN=value\nPASSWORD=CHANGEME_PASSWORD\n",
                encoding="utf-8",
            )
            environment.write_text("", encoding="utf-8")

            added = add_missing_defaults(example, environment, keys(environment))
            assignments = dict(
                line.split("=", 1)
                for line in environment.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")
            )

            self.assertEqual(added, 2)
            self.assertEqual(assignments["PLAIN"], "value")
            self.assertEqual(len(assignments["PASSWORD"]), 48)
            self.assertNotIn("CHANGEME", assignments["PASSWORD"])


if __name__ == "__main__":
    unittest.main()
