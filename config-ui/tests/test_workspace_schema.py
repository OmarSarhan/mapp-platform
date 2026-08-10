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
                }
            },
        },
    }


class WorkspaceValidationTests(unittest.TestCase):
    def test_accepts_supported_workspace(self):
        self.assertEqual(validate_workspace(valid_workspace(), {"MAPP"}), [])

    def test_rejects_a_dbs_key_starting_with_a_digit(self):
        data = valid_workspace()
        data["dbs"] = "9council"

        errors = validate_workspace(data, {"9council", "MAPP"})

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["path"], "dbs")

    def test_rejects_a_dbs_key_over_63_characters(self):
        data = valid_workspace()
        data["dbs"] = "a" * 64

        errors = validate_workspace(data, {"a" * 64, "MAPP"})

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["path"], "dbs")

    def test_accepts_a_dbs_key_with_an_underscore(self):
        data = valid_workspace()
        data["dbs"] = "council_prod"
        data["locale"]["layers"]["Places"]["dbs"] = "council_prod"

        self.assertEqual(validate_workspace(data, {"council_prod"}), [])

    def test_rejects_layer_keys_xyz_cannot_register(self):
        data = valid_workspace()
        layer = data["locale"]["layers"].pop("Places")
        data["locale"]["layers"]["Passport holders — United Kingdom"] = layer

        errors = validate_workspace(data, {"MAPP"})

        self.assertEqual(len(errors), 1)
        self.assertEqual(
            errors[0]["path"],
            "locale.layers.Passport holders — United Kingdom",
        )
        self.assertIn("use name for display punctuation", errors[0]["message"])

    def test_accepts_native_templates_gazetteer_and_plugins(self):
        data = valid_workspace()
        data["templates"] = {
            "summary": {
                "template": "SELECT count(*) FROM places",
                "dbs": "MAPP",
                "value_only": True,
                "statement_timeout": 5000,
            },
            "remote_layer": {"src": "file:/instance/layer.json"},
        }
        data["locale"].update({
            "templates": ["base_locale", {"src": "file:/instance/extra.json"}],
            "syncPlugins": ["zoomBtn", "zoomToArea"],
            "keyvalue_dictionary": [{"key": "name", "value": "Places", "default": "Locations"}],
        })
        data["locale"]["layers"]["Places"]["gazetteer"] = {
            "datasets": [{"layer": "Places", "qterm": "name", "limit": 10}]
        }
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

    def test_rejects_unknown_properties_at_contract_boundaries(self):
        data = valid_workspace()
        data["futureRoot"] = True
        data["locale"]["measure_distance"] = {}
        data["locale"]["layers"]["Places"]["futureXYZProperty"] = {}
        data["locale"]["feature_info"] = {"features": True, "futureOption": True}
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertEqual(paths, {
            "futureRoot",
            "locale.measure_distance",
            "locale.layers.Places.futureXYZProperty",
            "locale.feature_info.futureOption",
        })

    def test_rejects_invalid_template_and_gazetteer_descriptors(self):
        data = valid_workspace()
        data["templates"] = {"bad": {"statement_timeout": -1, "module": "yes"}}
        data["locale"]["layers"]["Places"]["gazetteer"] = {
            "datasets": [{"layer": "Places", "qterm": "bad-name"}]
        }
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("templates.bad.statement_timeout", paths)
        self.assertIn("templates.bad.module", paths)
        self.assertIn("locale.layers.Places.gazetteer.datasets.0.qterm", paths)

    def test_validates_only_bundled_plugin_contracts(self):
        data = valid_workspace()
        data["locale"].update({
            "consent": {"text": "Allow required storage?", "title": "Consent"},
            "custom_theme": {"primary": "#123456"},
            "feature_info": {"features": True, "css": "max-width: 20rem"},
            "layer_order": ["Places"],
            "link_button": {"href": "/help", "icon_name": "help"},
            "test": {"quiet": True, "showSummary": False},
            "zoomBtn": {},
        })
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

        data["locale"]["consent"] = {}
        data["locale"]["link_button"] = {"href": "/help"}
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.consent.text", paths)
        self.assertIn("locale.link_button.icon_name", paths)

    def test_validates_xyz_layer_group_and_stylesheet_class_list(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"]["group"] = "Reference"
        data["locale"]["layers"]["Places"]["groupClassList"] = "reference-blue"
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

        data["locale"]["layers"]["Places"]["groupClassList"] = "#123456"
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.groupClassList", paths)

        data["locale"]["layers"]["Places"]["groupClassList"] = " "
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.groupClassList", paths)

        data["locale"]["layers"]["Places"]["groupClassList"] = "reference-blue"
        data["locale"]["layers"]["Places"].pop("group")
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.groupClassList", paths)

        data["locale"]["layers"]["Places"].pop("groupClassList")
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

    def test_validates_string_default_layer_filter_as_read_only_predicate(self):
        data = valid_workspace()
        layer = data["locale"]["layers"]["Places"]
        layer["filter"] = {"default": "population_count > 0"}
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

        layer["filter"]["default"] = "true; DELETE FROM public.places"
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.filter.default", paths)

    def test_validates_structured_default_layer_filter_deeply(self):
        data = valid_workspace()
        layer = data["locale"]["layers"]["Places"]
        layer["filter"] = {"default": [
            {"population_count": {"gte": 1, "lt": 10}},
            {"published": {"boolean": True}},
        ]}
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

        invalid_defaults = (
            {"published": {"boolean": "true"}},
            {"population_count": [{"gte": 1}, {"null": True}]},
            {"population_count": {"between": [1, 10]}},
            {"population_count": {"gte": "many"}},
            {"population$count": {"eq": 1}},
            {"population_count": {"in": [[1], 2]}},
            {"name": {"like": "%FF"}},
            [],
        )
        for default in invalid_defaults:
            with self.subTest(default=default):
                layer["filter"]["default"] = default
                paths = {
                    error["path"]
                    for error in validate_workspace(data, {"MAPP"})
                }
                self.assertTrue(any(
                    path.startswith("locale.layers.Places.filter.default")
                    for path in paths
                ))

    def test_rejects_invalid_filter_type_and_unknown_included_field(self):
        data = valid_workspace()
        layer = data["locale"]["layers"]["Places"]
        layer["infoj"][0]["filter"] = {"type": "unsupported"}
        layer["filter"] = {"include": ["missing_field"]}
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.infoj.0.filter.type", paths)
        self.assertIn("locale.layers.Places.filter.include", paths)

    def test_rejects_interactive_filters_on_calculated_info_fields(self):
        data = valid_workspace()
        layer = data["locale"]["layers"]["Places"]
        layer["infoj"][0]["fieldfx"] = "upper(name)"
        layer["infoj"][0]["filter"] = {"type": "like"}

        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}

        self.assertIn("locale.layers.Places.infoj.0.filter", paths)

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

    def test_accepts_all_xyz_theme_modes_and_named_theme_reference(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"]["style"] = {
            "default": {"fillColor": "#eeeeee", "strokeColor": "#111111"},
            "theme": "category",
            "themes": {
                "category": {
                    "type": "categorized",
                    "field": "kind",
                    "categories": [
                        {"value": "park", "label": "Park", "style": {"fillColor": "#00aa00"}},
                        {"value": "water", "label": "Water", "icon": {"type": "dot", "fillColor": "#0000ff"}},
                    ],
                    "futureThemeOption": True,
                },
                "multi": {
                    "type": "categorized",
                    "fields": ["kind", "status"],
                    "categories": [
                        {"field": "kind", "value": "park", "style": {"icon": {"type": "dot"}}},
                        {"field": "status", "value": "open", "style": {"icon": [{"type": "circle"}]}},
                    ],
                },
                "breaks": {
                    "type": "graduated",
                    "field": "score",
                    "graduated_breaks": "less_than",
                    "categories": [
                        {"value": 10, "label": "Low", "style": {"fillColor": "#eeeeee"}},
                        {"value": 20, "label": "High", "style": {"fillColor": "#111111"}},
                    ],
                },
                "palette": {
                    "type": "distributed",
                    "categories": [
                        {"style": {"fillColor": "#ff0000"}},
                        {"style": {"icon": {"url": "/instance/svg/bus.svg"}}},
                    ],
                },
                "one": {
                    "type": "basic",
                    "label": "Places",
                    "style": {"strokeColor": "#123456"},
                },
            },
        }
        self.assertEqual(validate_workspace(data, {"MAPP"}), [])

    def test_rejects_missing_duplicate_and_invalid_categorized_values(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"]["style"] = {
            "theme": {
                "type": "categorized",
                "field": "kind",
                "fields": ["kind"],
                "categories": [
                    {"value": "park", "label": " ", "style": {"fillColor": "#00aa00"}},
                    {"value": "park", "style": {"fillColor": "#00bb00"}},
                    {"value": ["not", "scalar"], "style": {}},
                    {"style": {}},
                ],
            }
        }
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.style.theme.field", paths)
        self.assertIn("locale.layers.Places.style.theme.categories.0.label", paths)
        self.assertIn("locale.layers.Places.style.theme.categories.1.value", paths)
        self.assertIn("locale.layers.Places.style.theme.categories.2.value", paths)
        self.assertIn("locale.layers.Places.style.theme.categories.3.value", paths)

    def test_rejects_unordered_or_duplicate_graduated_breaks(self):
        data = valid_workspace()
        theme = {
            "type": "graduated",
            "field": "score",
            "graduated_breaks": "less_than",
            "categories": [
                {"value": 20, "style": {}},
                {"value": 10, "style": {}},
                {"value": 10, "style": {}},
            ],
        }
        data["locale"]["layers"]["Places"]["style"] = {
            "default": {"fillColor": "#eeeeee"},
            "theme": theme,
        }
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.style.theme.categories", paths)
        self.assertIn("locale.layers.Places.style.theme.categories.2.value", paths)
        theme["graduated_breaks"] = "near"
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.style.theme.graduated_breaks", paths)

    def test_rejects_invalid_multi_field_distributed_and_named_themes(self):
        data = valid_workspace()
        data["locale"]["layers"]["Places"]["style"] = {
            "theme": "missing",
            "themes": {
                "multi": {
                    "type": "categorized",
                    "fields": ["kind", "kind"],
                    "categories": [
                        {"field": "other", "value": "park", "style": {"fillColor": "#00aa00"}},
                    ],
                },
                "distributed": {
                    "type": "distributed",
                    "field": "bad field",
                    "categories": [{}, {"style": None}],
                },
            },
        }
        paths = {error["path"] for error in validate_workspace(data, {"MAPP"})}
        self.assertIn("locale.layers.Places.style.theme", paths)
        self.assertIn("locale.layers.Places.style.themes.multi.fields", paths)
        self.assertIn("locale.layers.Places.style.themes.multi.categories.0.style.icon", paths)
        self.assertIn("locale.layers.Places.style.themes.distributed.field", paths)
        self.assertIn("locale.layers.Places.style.themes.distributed.categories.0.style", paths)

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
