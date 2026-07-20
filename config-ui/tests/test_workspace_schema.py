import unittest

from workspace_schema import _merge, validate_workspace


def valid_workspace():
    return {
        "key": "example",
        "dbs": "MAPP",
        "locale": {
            "extent": {"north": 54, "east": -1, "south": 53, "west": -2, "mask": True},
            "view": {"lat": 53.5, "lng": -1.5, "z": 11},
            "ScaleLine": "metric",
            "layers": {
                "Places": {
                    "format": "mvt", "display": True, "dbs": "MAPP",
                    "table": "public.places", "geom": "geom_3857",
                    "srid": "3857", "qID": "id",
                    "infoj": [{"title": "Name", "field": "name", "inline": True}],
                    "futureXYZProperty": {"is": "preserved"},
                }
            },
        },
    }


class WorkspaceValidationTests(unittest.TestCase):
    def test_accepts_supported_workspace_and_unknown_properties(self):
        self.assertEqual(validate_workspace(valid_workspace(), {"MAPP"}), [])

    def test_accepts_non_empty_xyz_layer_group_and_rejects_empty_group(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"]["group"] = "Reference"
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])
        data["locale"]["layers"]["Places"]["group"] = " "
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.group", paths)

    def test_validates_style_panel_visibility_and_ordered_elements(self):
        data = valid_workspace()
        style = data["locale"]["layers"]["Places"].setdefault("style", {})
        style.update({
            "hidden": False,
            "elements": ["hover", "opacitySlider", "customPluginControl"],
            "opacitySlider": True,
        })
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])
        style["elements"] = ["hover", "hover"]
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.style.elements", paths)

    def test_accepts_xyz_layer_and_infoj_filter_configuration(self):
        data = valid_workspace()
        layer = data["locale"]["layers"]["Places"]
        layer["filter"] = {
            "include": ["name"],
            "exclude": ["id"],
            "includeAll": False,
            "viewport": True,
            "hidden": False,
            "default": {"active": {"boolean": True}},
        }
        layer["infoj"][0]["filter"] = {
            "type": "like",
            "leading_wildcard": True,
        }
        layer["infoj"].extend([
            {
                "title": "ID",
                "field": "id",
                "type": "integer",
                "filter": True,
            },
        ])
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

    def test_rejects_invalid_filter_type_and_unknown_included_field(self):
        data = valid_workspace()
        layer = data["locale"]["layers"]["Places"]
        layer["infoj"][0]["filter"] = {"type": "unsupported"}
        layer["filter"] = {"include": ["missing_field"]}
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.infoj.0.filter.type", paths)
        self.assertIn("locale.layers.Places.filter.include", paths)

    def test_rejects_unknown_database(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"]["dbs"] = "OTHER"
        errors = validate_workspace(data, {"MAPP"})
        self.assertIn("No DBS_OTHER", errors[0]["message"])

    def test_rejects_invalid_extent_and_relation(self):
        data = valid_workspace()
        data["locale"]["extent"]["north"] = -50
        data["locale"]["layers"]["Places"]["table"] = "public.places; DROP TABLE x"
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.extent", paths)
        self.assertIn("locale.layers.Places.table", paths)

    def test_tile_layer_requires_uri(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"] = {"format": "tiles", "display": True}
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.URI", paths)

    def test_accepts_template_advanced_external_and_icon_array_layers(self):
        data = {
            "key": "advanced",
            "locale": {
                "extent": {"west": -2, "east": -1},
                "layers": {
                    "Template": {"template": "OSM"},
                    "Tiles": {"format": "tiles", "template": "raster"},
                    "External": {
                        "format": "maplibre",
                        "style": {"URL": "https://tiles.example.invalid/style"},
                    },
                    "Inline": {
                        "format": "geojson",
                        "features": [],
                        "qID": "id",
                        "srid": 4326,
                    },
                    "Zoomed": {
                        "format": "mvt",
                        "tables": {"0": "public.low", "12": "public.high"},
                        "geoms": {"0": "geom_low", "12": "geom_high"},
                        "qID": "id",
                        "srid": 3857,
                        "style": {
                            "default": {
                                "icon": [
                                    {"type": "dot", "fillColor": "#123456"},
                                    {"type": "circle", "strokeColor": "#654321"},
                                ]
                            }
                        },
                    },
                },
            },
        }
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

    def test_named_locales_inherit_and_deep_merge_the_base_locale(self):
        data = {
            "key": "translated",
            "locale": {
                "extent": {"west": -2},
                "layers": {
                    "Places": {
                        "format": "mvt",
                        "dbs": "MAPP",
                        "table": "public.places",
                        "geom": "geom_3857",
                        "srid": 3857,
                        "qID": "id",
                        "style": {
                            "default": {
                                "strokeWidth": 2,
                                "strokeColor": "#111111",
                            }
                        },
                    }
                },
            },
            "locales": {
                "en-GB": {"name": "English"},
                "cy-GB": {
                    "name": "Cymraeg",
                    "layers": {
                        "Places": {
                            "name": "Lleoedd",
                            "style": {
                                "default": {"strokeColor": "#123456"}
                            },
                        }
                    },
                },
            },
        }
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

    def test_workspace_locale_merge_uses_xyz_array_semantics(self):
        merged = _merge(
            {"controls": ["zoom", "scale"], "items": [{"key": "base"}]},
            {"controls": ["scale"], "items": [{"key": "base"}]},
        )
        self.assertEqual(["scale"], merged["controls"])
        self.assertEqual(
            [{"key": "base"}, {"key": "base"}],
            merged["items"],
        )
        self.assertEqual(
            {"truthy": "keep", "array": [1], "falsy": {"added": True}},
            _merge(
                {"truthy": "keep", "array": [1], "falsy": 0},
                {
                    "truthy": {"ignored": True},
                    "array": {"ignored": True},
                    "falsy": {"added": True},
                },
            ),
        )

    def test_top_level_locale_is_validated_when_named_locales_exist(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"]["table"] = "invalid;relation"
        data["locales"] = {"alternative": {}}
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.table", paths)

    def test_rejects_multi_statement_field_expression(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"]["infoj"][0]["fieldfx"] = "name; SELECT 1"
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.infoj.0.fieldfx", paths)

    def test_rejects_statement_or_system_function_expression(self):
        for expression in (
            "(SELECT name)",
            "pg_read_file('/etc/passwd')",
            "version()",
            "current_user",
            "pg_backend_pid()",
            "pg_advisory_lock(1)",
            "public.user_function(name)",
            "name::regclass",
            "$tag$unsafe$tag$",
        ):
            data = valid_workspace()
            data["locale"]["layers"]["Places"]["infoj"][0]["fieldfx"] = expression
            paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
            self.assertIn("locale.layers.Places.infoj.0.fieldfx", paths)

    def test_accepts_allowlisted_scalar_and_postgis_expressions(self):
        expressions = (
            "upper(name)",
            "CASE WHEN name IS NULL THEN 'Unknown' ELSE concat(name, ' stop') END",
            "ST_asGeoJSON(geom_3857)",
            "ARRAY[ST_X(ST_PointOnSurface(geom_3857)), ST_Y(ST_PointOnSurface(geom_3857))]",
            "length(name)::text",
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                data = valid_workspace()
                data["locale"]["layers"]["Places"]["infoj"][0]["fieldfx"] = expression
                self.assertEqual(validate_workspace(data, {"MAPP"}), [])

    def test_accepts_xyz_symbol_and_vector_style_properties(self):
        data = valid_workspace()
        style = data["locale"]["layers"]["Places"].setdefault("style", {})
        style["default"] = {
            "strokeColor": "#123456",
            "strokeOpacity": 0.75,
            "strokeWidth": 3,
            "lineDash": [5, 4],
        }
        style["highlight"] = {
            "icon": {
                "type": "markerLetter",
                "color": "#176b4d",
                "letter": "A",
                "scale": 1.2,
            }
        }
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

    def test_rejects_unknown_symbol_and_invalid_marker_letter(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"]["style"] = {"default": {
            "icon": {"type": "hexagon", "letter": "AB"}
        }}
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.style.default.icon.type", paths)

    def test_validates_optional_viewport_count_text(self):
        data = valid_workspace()
        layer = data["locale"]["layers"]["Places"]
        layer["filter"] = {
            "viewport": True,
            "count_meta": "features currently visible",
            "viewport_description": "Counted in the current map view.",
        }
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])
        layer["filter"]["count_meta"] = " "
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.filter.count_meta", paths)

    def test_validates_optional_layer_heading_viewport_count(self):
        data = valid_workspace()
        layer = data["locale"]["layers"]["Places"]
        layer["plugins"] = ["/instance/plugins/viewport-layer-count.mjs"]
        layer["viewport_layer_count"] = {"debounce": 300}
        layer["filter"] = {"viewport": True}
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])
        layer["viewport_layer_count"]["debounce"] = -1
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn(
            "locale.layers.Places.viewport_layer_count.debounce",
            paths,
        )

    def test_validates_optional_geometry_info_symbol_metadata(self):
        data = valid_workspace()
        entry = data["locale"]["layers"]["Places"]["infoj"][0]
        entry.update({
            "type": "geometry",
            "style": {
                "fillColor": None,
                "strokeColor": "#ff007b",
                "strokeWidth": 3,
            },
            "_dashboard": {"styleFromLayerDefault": True},
        })
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])
        entry["_dashboard"]["styleFromLayerDefault"] = "yes"
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn(
            "locale.layers.Places.infoj.0._dashboard.styleFromLayerDefault",
            paths,
        )

    def test_rejects_non_3857_mvt_and_unsupported_scale_units(self):
        data = valid_workspace()
        data["locale"]["ScaleLine"] = "nautical"
        data["locale"]["layers"]["Places"]["srid"] = "4326"
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.ScaleLine", paths)
        self.assertIn("locale.layers.Places.srid", paths)


if __name__ == "__main__":
    unittest.main()
