import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class WorkspaceJsonSchemaTests(unittest.TestCase):
    def test_schema_and_workspace_are_json(self):
        schema = json.loads(
            (ROOT / "config-ui/schema/workspace.schema.json").read_text()
        )
        workspace = json.loads((ROOT / "instance/workspace.seed.json").read_text())
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        layers = workspace["locale"]["layers"]
        self.assertEqual({"Open_Street_Map"}, set(layers))
        self.assertEqual("tiles", layers["Open_Street_Map"]["format"])
        self.assertEqual("Open Street Map", layers["Open_Street_Map"]["name"])

    def test_schema_covers_dashboard_xyz_symbols(self):
        schema = json.loads(
            (ROOT / "config-ui/schema/workspace.schema.json").read_text()
        )
        icon_types = set(schema["$defs"]["iconStyle"]["properties"]["type"]["enum"])
        self.assertEqual(
            icon_types,
            {
                "dot", "target", "triangle", "square", "diamond",
                "semiCircle", "markerLetter", "markerColor", "circle",
                "template",
            },
        )
        feature = schema["$defs"]["featureStyle"]["properties"]
        self.assertIn("highlightScale", feature)
        self.assertIn("lineDash", feature)
        self.assertEqual(
            schema["$defs"]["iconStyle"]["properties"]["url"]["minLength"], 1
        )

    def test_schema_covers_xyz_layer_formats_and_runtime_fields(self):
        schema = json.loads(
            (ROOT / "config-ui/schema/workspace.schema.json").read_text()
        )
        defs = schema["$defs"]
        formats = set(defs["layer"]["properties"]["format"]["enum"])
        self.assertEqual(
            formats,
            {
                "cluster", "geojson", "googleMapTiles", "mapboxStyle",
                "maplibre", "mvt", "tiles", "vector", "wkt",
            },
        )
        layer_fields = set(defs["layer"]["properties"])
        self.assertTrue({
            "tables", "geoms", "z_field", "cluster", "featureFormat",
            "featureLookup", "featureSet", "wkt_properties", "cacheSize",
            "transition", "vectorImage", "infoj_skip", "infoj_order",
        } <= layer_fields)
        self.assertIn("_dashboard", layer_fields)

    def test_schema_covers_templates_gazetteer_and_only_bundled_plugins(self):
        schema = json.loads(
            (ROOT / "config-ui/schema/workspace.schema.json").read_text()
        )
        defs = schema["$defs"]
        template_fields = set(defs["templateDefinition"]["properties"])
        self.assertTrue({
            "src", "template", "dbs", "module", "nonblocking",
            "statement_timeout", "value_only", "reduce",
        } <= template_fields)
        locale_fields = set(defs["locale"]["properties"])
        self.assertTrue({
            "template", "templates", "keyvalue_dictionary",
            "svgTemplates", "svg_templates", "admin", "consent",
            "custom_theme", "dark_mode", "feature_info", "fullscreen",
            "layer_order", "link_button", "locator", "login", "test",
            "userIDB", "userLayer", "userLocale", "zoomBtn", "zoomToArea",
        } <= locale_fields)
        self.assertFalse({
            "googleMaps", "measure_distance", "query_features", "posthog",
            "userSettings", "info_panel", "screenshot", "coordinates",
            "streetview",
        } & locale_fields)
        self.assertIn("gazetteer", defs["layer"]["properties"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertIs(defs["locale"]["additionalProperties"], False)
        self.assertIs(defs["layer"]["additionalProperties"], False)
        self.assertIs(
            defs["layer"]["properties"]["gazetteer"]["additionalProperties"],
            False,
        )
        self.assertIs(defs["gazetteerDataset"]["additionalProperties"], False)

    def test_schema_covers_xyz_infoj_and_style_registries(self):
        schema = json.loads(
            (ROOT / "config-ui/schema/workspace.schema.json").read_text()
        )
        defs = schema["$defs"]
        entry_types = set(defs["infoEntry"]["properties"]["type"]["enum"])
        self.assertTrue({
            "boolean", "dataview", "date", "datetime", "documents",
            "geometry", "html", "image", "images", "integer", "json",
            "key", "layer", "link", "numeric", "pills", "pin", "ping",
            "query_button", "report", "tab", "text", "textarea", "time",
            "title", "mvt_clone", "vector_layer",
        } <= entry_types)
        style_fields = set(defs["layerStyle"]["properties"])
        self.assertTrue({
            "default", "highlight", "selected", "cluster", "theme",
            "themes", "hover", "hovers", "label", "labels",
            "icon_scaling", "cache", "contextFilter",
        } <= style_fields)
        hover_fields = set(defs["hover"]["properties"])
        self.assertTrue({"display", "field", "title", "dynamic", "hidden", "query"} <= hover_fields)
        info_fields = set(defs["infoEntry"]["properties"])
        self.assertTrue({"style", "_dashboard"} <= info_fields)


if __name__ == "__main__":
    unittest.main()
