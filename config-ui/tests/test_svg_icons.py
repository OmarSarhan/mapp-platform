import tempfile
import unittest
from pathlib import Path

from svg_icons import safe_svg


class SvgIconTests(unittest.TestCase):
    def write(self, directory, name, body):
        path = Path(directory) / name
        path.write_text(body)
        return path

    def test_accepts_bounded_svg(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                "bus.svg",
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                '<path d="M1 1h8v8H1z"/></svg>',
            )
            self.assertTrue(safe_svg(path))

    def test_rejects_script_and_event_handlers(self):
        with tempfile.TemporaryDirectory() as directory:
            script = self.write(
                directory, "script.svg", "<svg><script>alert(1)</script></svg>"
            )
            event = self.write(
                directory, "event.svg", '<svg><path onmouseover="x()"/></svg>'
            )
            self.assertFalse(safe_svg(script))
            self.assertFalse(safe_svg(event))

    def test_rejects_non_svg_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self.write(directory, "icon.txt", "<svg/>")
            large = self.write(
                directory, "large.svg", "<svg>" + (" " * (256 * 1024)) + "</svg>"
            )
            self.assertFalse(safe_svg(text))
            self.assertFalse(safe_svg(large))


if __name__ == "__main__":
    unittest.main()
