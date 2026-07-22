import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import plugin_registry


def manifest(**overrides):
    value = {
        "id": "sample-plugin",
        "name": "Sample plugin",
        "version": "1.0.0",
        "xyzVersion": ">=4.23.0 <5.0.0",
        "entry": "index.mjs",
        "registrationKey": "sample_plugin",
        "scope": ["layer"],
        "dispatch": ["layer"],
        "configurationKey": "sample_plugin",
        "configurationSchema": {
            "type": "object",
            "properties": {"delay": {"type": "number", "minimum": 0}},
            "additionalProperties": False,
        },
        "summary": "A test plugin.",
        "prerequisites": [],
        "dependencies": [],
        "previewAssertions": [{"type": "registration"}],
        "documentation": {"configuration": "Set delay."},
    }
    value.update(overrides)
    return value


class PluginRegistryTests(unittest.TestCase):
    def make_plugin(self, root: Path, value=None):
        directory = root / "sample-plugin"
        directory.mkdir()
        (directory / "index.mjs").write_text(
            "mapp.plugins.sample_plugin = () => {};", encoding="utf-8"
        )
        (directory / "plugin.json").write_text(
            json.dumps(value or manifest()), encoding="utf-8"
        )

    def test_discovers_hashes_and_composes_closed_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_plugin(root)
            with patch.object(plugin_registry, "PLUGIN_ROOT", root):
                first = plugin_registry.catalogue()
                second = plugin_registry.catalogue()
                schema = plugin_registry.composed_schema({
                    "$defs": {
                        "locale": {"properties": {"plugins": {"items": {}}}},
                        "layer": {"properties": {"plugins": {"items": {}}}},
                    }
                })
            self.assertTrue(first["valid"])
            self.assertEqual(first["fingerprint"], second["fingerprint"])
            self.assertEqual(len(first["external"][0]["files"][0]["sha256"]), 64)
            self.assertIs(
                schema["$defs"]["layer"]["properties"]["sample_plugin"]["additionalProperties"],
                False,
            )

    def test_rejects_incompatible_open_or_escaping_plugins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = manifest(xyzVersion=">=5.0.0", entry="../escape.mjs")
            value["configurationSchema"]["additionalProperties"] = True
            self.make_plugin(root, value)
            with patch.object(plugin_registry, "PLUGIN_ROOT", root):
                entry = plugin_registry.catalogue()["external"][0]
            self.assertFalse(entry["available"])
            self.assertTrue(any("pinned XYZ" in item for item in entry["diagnostics"]))
            self.assertTrue(any("additionalProperties" in item for item in entry["diagnostics"]))
            self.assertTrue(any("entry" in item for item in entry["diagnostics"]))

    def test_validates_module_pairing_scope_and_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_plugin(root)
            workspace = {
                "locale": {
                    "layers": {
                        "Places": {
                            "plugins": ["/instance/plugins/sample-plugin/index.mjs"],
                            "sample_plugin": {"delay": -1, "unknown": True},
                        }
                    }
                }
            }
            with patch.object(plugin_registry, "PLUGIN_ROOT", root):
                errors = plugin_registry.validate_workspace_plugins(workspace)
            paths = {error["path"] for error in errors}
            self.assertIn("locale.layers.Places.sample_plugin.delay", paths)
            self.assertIn("locale.layers.Places.sample_plugin.unknown", paths)


if __name__ == "__main__":
    unittest.main()
