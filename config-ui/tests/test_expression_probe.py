import json
import unittest

import app
from infoj_types import info_value_error


class ExpressionProbeContractTests(unittest.TestCase):
    def test_sample_payload_is_json_serializable(self):
        payload = {
            "valid": True,
            "postgresType": "text",
            "sample": "Leeds Station — 45001234",
            "message": "Expression is compatible with the selected information type.",
        }
        self.assertIn("Leeds Station", json.dumps(payload))

    def test_probe_uses_renderer_compatibility_contract(self):
        self.assertIsNone(info_value_error("text", "text", "sample"))
        self.assertIn("boolean", info_value_error("boolean", "text", "sample"))

    def test_allowlisted_function_names_cannot_be_shadowed(self):
        class Cursor:
            def __init__(self, row):
                self.row = row
                self.calls = []

            def execute(self, statement, parameters):
                self.calls.append((statement, parameters))

            def fetchone(self):
                return self.row

        safe = Cursor(None)
        app.ensure_safe_expression_catalog(safe, "upper(name)")
        self.assertEqual((["upper"],), safe.calls[0][1])

        shadowed = Cursor(("public", "upper"))
        with self.assertRaisesRegex(ValueError, "shadowed"):
            app.ensure_safe_expression_catalog(shadowed, "upper(name)")

        no_function = Cursor(("public", "unused"))
        app.ensure_safe_expression_catalog(no_function, "name::text")
        self.assertEqual([], no_function.calls)


if __name__ == "__main__":
    unittest.main()
