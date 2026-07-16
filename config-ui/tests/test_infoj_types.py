import unittest

from infoj_types import info_value_error


class InfojTypeTests(unittest.TestCase):
    def test_accepts_compatible_values(self):
        self.assertIsNone(info_value_error("numeric", "numeric", 12.5))
        self.assertIsNone(info_value_error("boolean", "boolean", True))
        self.assertIsNone(info_value_error("pin", "double precision[]", [-1.5, 53.8]))
        self.assertIsNone(info_value_error("geometry", "text", '{"type":"Point","coordinates":[0,0]}'))
        self.assertIsNone(info_value_error("pills", "text[]", ["A", "B"]))

    def test_rejects_incompatible_values(self):
        self.assertIn("numeric", info_value_error("numeric", "text", "twelve"))
        self.assertIn("boolean", info_value_error("boolean", "text", "yes"))
        self.assertIn("two numeric", info_value_error("pin", "text[]", ["x", "y"]))
        self.assertIn("GeoJSON", info_value_error("geometry", "text", "POINT(0 0)"))
        self.assertIn("array", info_value_error("pills", "text", "A"))


if __name__ == "__main__":
    unittest.main()
