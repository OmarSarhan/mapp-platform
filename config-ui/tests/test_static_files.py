import tempfile
import unittest
from pathlib import Path

from static_files import safe_static_path


class StaticFileTests(unittest.TestCase):
    def test_root_and_nested_assets_resolve_inside_static_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(root / "index.html", safe_static_path(root, "/"))
            self.assertEqual(root / "assets/app.js", safe_static_path(root, "/assets/app.js"))

    def test_parent_and_encoded_parent_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (
                "/../app.py",
                "/../../etc/passwd",
                "/%2e%2e/app.py",
                "/assets/%2e%2e/%2e%2e/secret",
            ):
                with self.subTest(path=path):
                    self.assertIsNone(safe_static_path(root, path))

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "static"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "escape").symlink_to(outside, target_is_directory=True)
            self.assertIsNone(safe_static_path(root, "/escape/secret.txt"))


if __name__ == "__main__":
    unittest.main()
