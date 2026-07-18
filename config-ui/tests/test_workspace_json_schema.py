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
        bus_style = workspace["locale"]["layers"]["Bus Stops"]["style"]
        self.assertEqual(bus_style["default"]["icon"]["url"], "/instance/svg/bus.svg")
        self.assertEqual(bus_style["hover"]["field"], "stop_name")
        smoke = workspace["locale"]["layers"]["Smoke Control Orders"]
        self.assertEqual(smoke["table"], "leeds.smoke_control_orders")
        self.assertEqual(
            smoke["attribution"]["Leeds City Council source"],
            "https://mapservices.leeds.gov.uk/arcgis/rest/services/"
            "Public/Planning/MapServer/8",
        )
        self.assertTrue(
            {
                "locality",
                "council_reference",
                "description",
                "order_date",
                "area_square_metres",
            }
            <= {entry.get("field") for entry in smoke["infoj"]}
        )

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


if __name__ == "__main__":
    unittest.main()
