import unittest

from derived_query_guard import (
    QualifiedCastType,
    QueryGuardViolation,
    inspect_query_ast,
    inspect_relation_types_and_casts,
    validate_qualified_cast_types,
    validate_relation_routines,
)


class Cursor:
    def __init__(self, rows, type_rows=()):
        self.rows = rows
        self.type_rows = type_rows
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))

    def fetchall(self):
        return (
            self.type_rows
            if "type_objects AS" in self.calls[-1][0]
            else self.rows
        )


def dependency(**updates):
    value = {
        "kind": "function",
        "object_oid": 100,
        "identity": "public.wrapper(integer)",
        "schema": "public",
        "name": "wrapper",
        "extension": None,
        "extension_schema": None,
        "implementation_schema": "public",
        "implementation_extension": None,
        "implementation_extension_schema": None,
        "volatility": "i",
        "returns_set": False,
        "routine_kind": "f",
        "security_definer": False,
        "routine_config": None,
        "language": "sql",
        "object_builtin": False,
        "implementation_builtin": False,
        "approved_extension_search_path": "search_path=pg_catalog, public",
    }
    value.update(updates)
    return value


def type_dependency(**updates):
    value = {
        "kind": "type",
        "object_oid": 20000,
        "identity": "evil.payload",
        "schema": "evil",
        "name": "payload",
        "extension": None,
        "object_builtin": False,
        "implementation_schema": None,
        "implementation_extension": None,
        "implementation_builtin": False,
        "volatility": None,
        "security_definer": None,
        "routine_config": None,
        "language": None,
    }
    value.update(updates)
    return value


def qualified_cast_type(**updates):
    value = {
        "schema": "public",
        "name": "geometry",
        "object_oid": 30000,
        "type_defined": True,
        "type_kind": "b",
        "extension": "postgis",
        "extension_schema": "public",
    }
    value.update(updates)
    return value


class QueryAstGuardTests(unittest.TestCase):
    def reason_codes(self, query):
        with self.assertRaises(QueryGuardViolation) as raised:
            inspect_query_ast(query)
        return {reason.code for reason in raised.exception.reasons}

    def test_requires_exactly_one_read_only_select(self):
        cases = {
            "DELETE FROM source.rows": "not_select",
            "SELECT 1; SELECT 2": "multiple_statements",
            "WITH changed AS (DELETE FROM source.rows RETURNING *) "
            "SELECT * FROM changed": "modifying_cte",
            "WITH RECURSIVE walk AS (SELECT 1) SELECT * FROM walk": "recursive_cte",
            "SELECT 1 INTO unsafe_table": "select_into",
            "SELECT * FROM source.rows FOR UPDATE": "row_locking",
        }
        for query, code in cases.items():
            with self.subTest(query=query):
                self.assertIn(code, self.reason_codes(query))

    def test_base_relations_must_be_schema_qualified(self):
        self.assertIn(
            "unqualified_relation",
            self.reason_codes("SELECT * FROM source_rows"),
        )
        inspect_query_ast("SELECT * FROM source.source_rows")
        inspect_query_ast(
            "WITH selected AS (SELECT * FROM source.source_rows) "
            "SELECT * FROM selected"
        )

    def test_system_and_managed_relation_schemas_are_rejected(self):
        for relation in (
            "pg_catalog.pg_class",
            "pg_temp.injected",
            "information_schema.tables",
            "derived_layers.existing_layer",
        ):
            with self.subTest(relation=relation):
                self.assertIn(
                    "unapproved_relation_schema",
                    self.reason_codes(f"SELECT * FROM {relation}"),
                )

    def test_explicit_custom_cast_types_and_operator_schemas_are_rejected(self):
        self.assertIn(
            "unapproved_cast_type",
            self.reason_codes("SELECT 'payload'::evil.bomb"),
        )
        self.assertIn(
            "unapproved_operator_schema",
            self.reason_codes(
                "SELECT left_value OPERATOR(evil.##) right_value "
                "FROM source.values"
            ),
        )
        inspect_query_ast(
            "SELECT geom::geometry(Point, 3857), value::text "
            "FROM source.values"
        )

    def test_qualified_postgis_geometry_cast_is_recorded_for_catalog_validation(self):
        inspection = inspect_query_ast(
            "SELECT geom::public.geometry(Polygon, 3857) FROM source.values"
        )

        self.assertEqual(
            (QualifiedCastType(schema="public", name="geometry"),),
            inspection.qualified_cast_types,
        )

    def test_lower_level_h3_wkb_query_records_qualified_geometry_cast(self):
        inspection = inspect_query_ast(
            "WITH scope_cells AS ("
            "SELECT h3_polygon_to_cells(_mapp_h3_scope.geom_4326, 9) AS h3_id "
            "FROM _mapp_h3_scope"
            ") "
            "SELECT h3_id::text AS h3_id, "
            "public.ST_Transform("
            "public.ST_GeomFromWKB(h3_cell_to_boundary_wkb(h3_id), 4326), "
            "3857"
            ")::public.geometry(Polygon, 3857) AS geom_3857 "
            "FROM scope_cells"
        )

        self.assertEqual(
            (QualifiedCastType(schema="public", name="geometry"),),
            inspection.qualified_cast_types,
        )

    def test_qualified_non_extension_cast_type_is_rejected(self):
        self.assertIn(
            "unapproved_cast_type",
            self.reason_codes("SELECT value::public.text FROM source.values"),
        )

    def test_qualified_geometry_cast_requires_static_valid_positive_typmods(self):
        queries = (
            "SELECT geom::public.geometry(NotGeometry, 3857) FROM source.values",
            "SELECT geom::public.geometry(Polygon) FROM source.values",
            "SELECT geom::public.geometry(Polygon, srid) FROM source.values",
            "SELECT geom::public.geometry(Polygon, 0) FROM source.values",
            "SELECT geom::public.geometry(Polygon, -1) FROM source.values",
        )
        for query in queries:
            with self.subTest(query=query):
                self.assertIn("unapproved_cast_typmod", self.reason_codes(query))

    def test_reserved_bindings_include_quoted_unicode_identifiers(self):
        queries = (
            "SELECT 1 FROM source.rows AS _mapp_h3_scope",
            'SELECT 1 FROM source.rows AS "_mapp_h3_scope"',
            r'SELECT 1 FROM source.rows AS U&"_mapp_h3_sc\006Fpe"',
            "WITH _mapp_private AS (SELECT 1) SELECT * FROM _mapp_private",
            "SELECT 1 FROM source._mapp_h3_scope",
        )
        for query in queries:
            with self.subTest(query=query):
                codes = self.reason_codes(query)
                self.assertTrue(
                    codes & {"h3_scope_shadowed", "reserved_alias", "reserved_cte",
                             "reserved_relation"}
                )

    def test_h3_polygon_expansion_proves_direct_scope_binding(self):
        safe = inspect_query_ast(
            "SELECT cell FROM _mapp_h3_scope CROSS JOIN LATERAL "
            "h3_polygon_to_cells(_mapp_h3_scope.geom_4326, 9) AS cell"
        )
        polygon = safe.calls_named({"h3_polygon_to_cells"})[0]
        self.assertTrue(polygon.bounded_set)
        self.assertEqual("server-scope-polygon", polygon.bound_kind)

        unsafe = (
            "SELECT h3_polygon_to_cells(_mapp_h3_scope.geom_4326, 9) "
            "FROM source.rows",
            "SELECT h3_polygon_to_cells(area.geom_4326, 9) FROM source.areas area",
            "SELECT h3_polygon_to_cells((_mapp_h3_scope.geom_4326), level) "
            "FROM _mapp_h3_scope",
        )
        for query in unsafe:
            with self.subTest(query=query):
                self.assertTrue(
                    self.reason_codes(query)
                    & {"h3_scope_binding", "h3_unscoped_polygon_expansion",
                       "h3_dynamic_resolution"}
                )

    def test_safe_h3_aggregation_and_bounded_traversal_remain_available(self):
        queries = (
            "SELECT h3_latlng_to_cell(geom, 9), count(*) FROM source.points GROUP BY 1",
            "SELECT h3_cell_to_parent(cell, 8) FROM source.cells",
            "SELECT h3_grid_disk(cell, 25) FROM source.cells",
            "SELECT h3_cell_to_children(cell) FROM source.cells",
            "SELECT h3_cell_to_children(cell, h3_get_resolution(cell) + 1) "
            "FROM source.cells",
        )
        for query in queries:
            with self.subTest(query=query):
                inspect_query_ast(query)

        for query in (
            "SELECT h3_grid_disk(cell, distance) FROM source.cells",
            "SELECT h3_cell_to_children(cell, 15) FROM source.cells",
            "SELECT h3_grid_path_cells(a, b) FROM source.cells",
            r'SELECT U&"h3_grid_path_cells_recurs\0069ve"(a, b) FROM source.cells',
        ):
            with self.subTest(query=query):
                self.assertTrue(
                    self.reason_codes(query)
                    & {"h3_dynamic_grid_distance", "h3_unbounded_child_expansion",
                       "h3_unbounded_expansion"}
                )

    def test_cartesian_and_implicit_joins_are_structurally_rejected(self):
        queries = (
            "SELECT * FROM source.a, source.b",
            "SELECT * FROM source.a CROSS JOIN source.b",
            "SELECT * FROM source.a JOIN source.b ON TRUE",
            "SELECT * FROM source.a a JOIN source.b b ON 1 = 1",
            "SELECT * FROM source.a a JOIN source.b b ON a.id = a.id",
            "SELECT * FROM source.a a JOIN source.b b ON b.id IS NOT NULL",
            "SELECT * FROM source.a a JOIN source.b b ON a_id = b_id",
            "SELECT * FROM source.a a JOIN source.b b "
            "ON a.id = b.id OR TRUE",
            "SELECT * FROM source.a a JOIN source.b b "
            "ON a.id = b.id OR 1 = 1",
            "SELECT * FROM source.a NATURAL JOIN source.b",
            "SELECT * FROM source.a CROSS JOIN LATERAL (SELECT * FROM source.b) b",
        )
        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(
                    self.reason_codes(query) & {"cartesian_join", "natural_join"}
                )
        inspect_query_ast(
            "SELECT * FROM source.a a JOIN source.b b ON a.id = b.id"
        )
        inspect_query_ast(
            "SELECT * FROM source.a JOIN source.b "
            "ON source.a.id = source.b.id"
        )
        inspect_query_ast(
            "SELECT * FROM source.a a JOIN source.b b USING (id)"
        )

    def test_generic_and_postgis_row_expanders_are_rejected(self):
        functions = (
            "unnest(values)",
            "jsonb_array_elements(payload)",
            "regexp_split_to_table(value, ',')",
            "generate_subscripts(values, 1)",
            "ST_DumpPoints(geom)",
            "ST_Subdivide(geom)",
            "ST_HexagonGrid(0.001, geom)",
            "ST_SquareGrid(1, geom)",
        )
        for function in functions:
            query = f"SELECT {function} FROM source.features"
            with self.subTest(query=query):
                self.assertIn("unbounded_set_function", self.reason_codes(query))

        for query in (
            "SELECT * FROM XMLTABLE('/x' PASSING payload COLUMNS x text PATH '.') t",
            "SELECT * FROM JSON_TABLE(payload, '$[*]' COLUMNS (x int PATH '$')) jt",
        ):
            with self.subTest(query=query):
                self.assertIn("unbounded_set_function", self.reason_codes(query))

    def test_unbounded_value_aggregates_are_rejected_but_numeric_ones_are_safe(self):
        for function in (
            "array_agg(value)",
            "jsonb_agg(value)",
            "string_agg(value, ',')",
            "xmlagg(value)",
        ):
            query = f"SELECT {function} FROM source.values"
            with self.subTest(query=query):
                self.assertIn("unbounded_aggregate_state", self.reason_codes(query))

        inspect_query_ast(
            "SELECT h3, count(*), sum(value), avg(value), min(value), max(value) "
            "FROM source.values GROUP BY h3"
        )
        inspect_query_ast(
            "SELECT ST_Transform(ST_Centroid(geom), 3857) FROM source.features"
        )

    def test_dynamic_query_file_and_server_catalog_functions_are_rejected(self):
        for expression in (
            "query_to_xml(query_text, true, false, '')",
            "database_to_xml(true, false, '')",
            "pg_read_file(path)",
            "current_setting(setting_name)",
            "pg_cancel_backend(pid)",
            "nextval(sequence_name)",
        ):
            query = f"SELECT {expression} FROM source.values"
            with self.subTest(query=query):
                self.assertIn(
                    "dangerous_catalog_function",
                    self.reason_codes(query),
                )

    def test_scalar_and_geometry_growth_is_rejected_per_source_row(self):
        unsafe = (
            "SELECT repeat(value, 10) FROM source.values",
            "SELECT repeat(value, 1000000000) FROM source.values",
            "SELECT lpad(value, dynamic_size) FROM source.values",
            "SELECT space(10) FROM source.values",
            "SELECT format('%1000000000s', value) FROM source.values",
            "SELECT array_fill(1, ARRAY[10])",
            "SELECT array_fill(1, ARRAY[1000000000])",
            "SELECT ST_GeneratePoints(geom, 10) FROM source.features",
            "SELECT ST_GeneratePoints(geom, 1000000000) FROM source.features",
            "SELECT ST_Segmentize(geom, 0.000001) FROM source.features",
            "SELECT ST_Buffer(geom, 10, 8) FROM source.features",
            "SELECT ST_Buffer(geom, 10, 'quad_segs=100000000') FROM source.features",
            "SELECT ST_VoronoiPolygons(geom) FROM source.features",
            "SELECT ST_DelaunayTriangles(geom) FROM source.features",
        )
        for query in unsafe:
            with self.subTest(query=query):
                self.assertTrue(
                    self.reason_codes(query)
                    & {"unbounded_scalar_output", "unbounded_geometry_expansion"}
                )

    def test_grouping_sets_are_counted_by_actual_expansion(self):
        self.assertEqual(
            8,
            inspect_query_ast(
                "SELECT a FROM source.values GROUP BY CUBE(a,b,c)"
            ).grouping_set_count,
        )
        self.assertEqual(
            4,
            inspect_query_ast(
                "SELECT a FROM source.values GROUP BY ROLLUP(a,b,c)"
            ).grouping_set_count,
        )
        self.assertEqual(
            12,
            inspect_query_ast(
                "SELECT a FROM source.values GROUP BY CUBE(a,b), ROLLUP(c,d)"
            ).grouping_set_count,
        )


class CatalogRoutineGuardTests(unittest.TestCase):
    def inspection(self, query="SELECT wrapper(value) FROM source.values"):
        return inspect_query_ast(query)

    def test_rejects_user_wrappers_and_operators_by_resolved_oid(self):
        for row in (
            dependency(),
            dependency(
                kind="operator",
                identity="public.@@(integer,integer)",
                name="@@",
            ),
        ):
            with self.subTest(row=row):
                with self.assertRaises(QueryGuardViolation) as raised:
                    validate_relation_routines(
                        Cursor([row]), "derived_layers", "probe", self.inspection()
                    )
                self.assertTrue(
                    {reason.code for reason in raised.exception.reasons}
                    & {"unapproved_function", "unapproved_operator"}
                )

    def test_allows_exact_pg_catalog_and_approved_extension_membership(self):
        rows = [
            dependency(
                identity="pg_catalog.abs(integer)",
                schema="pg_catalog",
                name="abs",
                implementation_schema="pg_catalog",
                object_builtin=True,
                implementation_builtin=True,
            ),
            dependency(
                object_oid=101,
                identity="geo.st_transform(geometry,integer)",
                schema="geo",
                name="st_transform",
                extension="postgis",
                implementation_schema="geo",
                implementation_extension="postgis",
            ),
            dependency(
                object_oid=102,
                identity="hex.h3_latlng_to_cell(geometry,integer)",
                schema="hex",
                name="h3_latlng_to_cell",
                extension="h3_postgis",
                implementation_schema="hex",
                implementation_extension="h3_postgis",
            ),
        ]
        result = validate_relation_routines(
            Cursor(rows),
            "derived_layers",
            "probe",
            inspect_query_ast(
                "SELECT abs(value), ST_Transform(geom, 3857), "
                "h3_latlng_to_cell(geom, 9) FROM source.values"
            ),
        )
        self.assertEqual(3, len(result))

    def test_allows_only_catalog_derived_path_for_h3_sql_wrappers(self):
        inspection = inspect_query_ast(
            "SELECT h3_polygon_to_cells("
            "_mapp_h3_scope.geom_4326, 9) FROM _mapp_h3_scope"
        )
        rows = (
            dependency(
                identity="public.h3_polygon_to_cells(geometry,integer)",
                name="h3_polygon_to_cells",
                extension="h3_postgis",
                extension_schema="public",
                implementation_extension="h3_postgis",
                implementation_extension_schema="public",
                routine_config=("search_path=pg_catalog, public",),
                returns_set=True,
            ),
            dependency(
                identity="hex.h3_polygon_to_cells(geo.geometry,integer)",
                schema="hex",
                name="h3_polygon_to_cells",
                extension="h3_postgis",
                extension_schema="hex",
                implementation_schema="hex",
                implementation_extension="h3_postgis",
                implementation_extension_schema="hex",
                routine_config=("search_path=pg_catalog, geo, hex",),
                approved_extension_search_path=(
                    "search_path=pg_catalog, geo, hex"
                ),
                returns_set=True,
            ),
        )

        for row in rows:
            with self.subTest(identity=row["identity"]):
                cursor = Cursor([row])
                validate_relation_routines(
                    cursor, "derived_layers", "probe", inspection
                )
                query, parameters = cursor.calls[0]
                self.assertIn(
                    "approved_extension_namespaces AS", query
                )
                self.assertEqual(
                    ["h3", "h3_postgis", "postgis"], parameters[0]
                )

    def test_rejects_unpinned_or_unproven_h3_sql_wrappers(self):
        inspection = inspect_query_ast(
            "SELECT h3_polygon_to_cells("
            "_mapp_h3_scope.geom_4326, 9) FROM _mapp_h3_scope"
        )
        approved = {
            "identity": "public.h3_polygon_to_cells(geometry,integer)",
            "name": "h3_polygon_to_cells",
            "extension": "h3_postgis",
            "extension_schema": "public",
            "implementation_extension": "h3_postgis",
            "implementation_extension_schema": "public",
            "returns_set": True,
        }
        rows = (
            dependency(**approved),
            dependency(
                **approved,
                routine_config=("search_path=pg_catalog, public, unsafe",),
            ),
            dependency(**{
                **approved,
                "extension_schema": "hex",
                "routine_config": ("search_path=pg_catalog, public",),
            }),
            dependency(**{
                **approved,
                "implementation_extension": "postgis",
                "routine_config": ("search_path=pg_catalog, public",),
            }),
            dependency(
                **approved,
                routine_config=(
                    "search_path=pg_catalog, public",
                    "work_mem=1GB",
                ),
            ),
        )

        for row in rows:
            with self.subTest(row=row):
                with self.assertRaises(QueryGuardViolation) as raised:
                    validate_relation_routines(
                        Cursor([row]), "derived_layers", "probe", inspection
                    )
                self.assertIn(
                    "configured_routine",
                    {reason.code for reason in raised.exception.reasons},
                )

    def test_catalog_path_does_not_approve_arbitrary_public_routine(self):
        row = dependency(
            routine_config=("search_path=pg_catalog, public",),
        )

        with self.assertRaises(QueryGuardViolation) as raised:
            validate_relation_routines(
                Cursor([row]),
                "derived_layers",
                "probe",
                self.inspection(),
            )

        self.assertIn(
            "unapproved_function",
            {reason.code for reason in raised.exception.reasons},
        )

    def test_catalog_path_does_not_approve_unrelated_extension_config(self):
        row = dependency(
            identity="public.extension_helper(integer)",
            extension="postgis",
            extension_schema="public",
            implementation_extension="postgis",
            implementation_extension_schema="public",
            routine_config=("search_path=pg_catalog, public",),
        )

        with self.assertRaises(QueryGuardViolation) as raised:
            validate_relation_routines(
                Cursor([row]),
                "derived_layers",
                "probe",
                self.inspection(),
            )

        self.assertIn(
            "configured_routine",
            {reason.code for reason in raised.exception.reasons},
        )

    def test_allows_qualified_cast_type_in_authoritative_extension_schema(self):
        inspection = inspect_query_ast(
            "SELECT geom::public.geometry(Polygon, 3857) FROM source.values"
        )
        cursor = Cursor([qualified_cast_type()])

        validate_qualified_cast_types(
            cursor,
            inspection,
        )
        query, params = cursor.calls[0]
        self.assertEqual(2, query.count("%s"))
        self.assertEqual(2, query.count("WITH ORDINALITY"))
        self.assertEqual((["public"], ["geometry"]), params)

    def test_rejects_qualified_cast_type_without_exact_extension_catalog_match(self):
        inspection = inspect_query_ast(
            "SELECT geom::public.geometry(Polygon, 3857) FROM source.values"
        )
        catalog_results = (
            (),
            (qualified_cast_type(object_oid=None),),
            (qualified_cast_type(type_defined=False),),
            (qualified_cast_type(type_kind="c"),),
            (qualified_cast_type(extension=None),),
            (qualified_cast_type(extension="h3"),),
            (qualified_cast_type(extension_schema="postgis"),),
            (qualified_cast_type(schema="other"),),
        )
        for rows in catalog_results:
            with self.subTest(rows=rows):
                with self.assertRaises(QueryGuardViolation) as raised:
                    validate_qualified_cast_types(Cursor(rows), inspection)
                self.assertIn(
                    "unapproved_cast_type",
                    {reason.code for reason in raised.exception.reasons},
                )

        non_public = inspect_query_ast(
            "SELECT geom::geo.geometry(Polygon, 3857) FROM source.values"
        )
        with self.assertRaises(QueryGuardViolation):
            validate_qualified_cast_types(
                Cursor([
                    qualified_cast_type(
                        schema="geo",
                        extension_schema="geo",
                    )
                ]),
                non_public,
            )

    def test_rejects_volatile_unproved_set_and_geometry_aggregates(self):
        cases = (
            (
                dependency(
                    identity="pg_catalog.random()",
                    schema="pg_catalog",
                    name="random",
                    implementation_schema="pg_catalog",
                    object_builtin=True,
                    implementation_builtin=True,
                    volatility="v",
                ),
                "SELECT random() FROM source.values",
                "volatile_routine",
            ),
            (
                dependency(
                    identity="pg_catalog.unnest(anyarray)",
                    schema="pg_catalog",
                    name="unnest",
                    implementation_schema="pg_catalog",
                    object_builtin=True,
                    implementation_builtin=True,
                    returns_set=True,
                ),
                "SELECT safe_name(values) FROM source.values",
                "unbounded_set_function",
            ),
            (
                dependency(
                    identity="geo.st_union(geometry)",
                    schema="geo",
                    name="st_union",
                    extension="postgis",
                    implementation_schema="geo",
                    implementation_extension="postgis",
                    routine_kind="a",
                ),
                "SELECT safe_name(geom) FROM source.values",
                "unbounded_aggregate_state",
            ),
        )
        for row, query, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(QueryGuardViolation) as raised:
                    validate_relation_routines(
                        Cursor([row]),
                        "derived_layers",
                        "probe",
                        self.inspection(query),
                    )
                self.assertIn(code, {reason.code for reason in raised.exception.reasons})

    def test_allows_only_ast_proved_set_functions(self):
        inspection = inspect_query_ast(
            "SELECT value FROM generate_series(1, 10) AS value"
        )
        row = dependency(
            identity="pg_catalog.generate_series(integer,integer)",
            schema="pg_catalog",
            name="generate_series",
            implementation_schema="pg_catalog",
            object_builtin=True,
            implementation_builtin=True,
            returns_set=True,
        )
        cursor = Cursor([row])
        validate_relation_routines(
            cursor, "derived_layers", "probe", inspection
        )
        self.assertIn("generate_series", cursor.calls[0][1][2])

    def test_rejects_security_definer_configured_and_untrusted_languages(self):
        cases = (
            (dependency(
                schema="geo", extension="postgis",
                implementation_schema="geo",
                implementation_extension="postgis",
                security_definer=True,
            ), "security_definer_routine"),
            (dependency(
                schema="geo", extension="postgis",
                implementation_schema="geo",
                implementation_extension="postgis",
                routine_config=("search_path=evil",),
            ), "configured_routine"),
            (dependency(
                schema="geo", extension="postgis",
                implementation_schema="geo",
                implementation_extension="postgis",
                language="plpgsql",
            ), "unapproved_routine_language"),
            (dependency(
                identity="pg_catalog.pg_read_file(text)",
                schema="pg_catalog", name="pg_read_file",
                implementation_schema="pg_catalog",
                object_builtin=True, implementation_builtin=True,
            ), "dangerous_catalog_function"),
        )
        for row, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(QueryGuardViolation) as raised:
                    validate_relation_routines(
                        Cursor([row]),
                        "derived_layers",
                        "probe",
                        self.inspection(),
                    )
                self.assertIn(code, {reason.code for reason in raised.exception.reasons})

    def test_rejects_custom_types_and_casts_by_catalog_oid(self):
        cases = (
            type_dependency(),
            type_dependency(
                kind="cast",
                identity="cast from integer to text",
                schema="",
                name="integer AS text",
                implementation_schema="evil",
                language="sql",
            ),
        )
        for row in cases:
            with self.subTest(kind=row["kind"]):
                with self.assertRaises(QueryGuardViolation) as raised:
                    validate_relation_routines(
                        Cursor([], [row]),
                        "derived_layers",
                        "probe",
                        self.inspection(),
                    )
                self.assertIn(
                    f"unapproved_{row['kind']}",
                    {reason.code for reason in raised.exception.reasons},
                )

        approved_geometry = type_dependency(
            identity="geo.geometry",
            schema="geo",
            name="geometry",
            extension="postgis",
        )
        validate_relation_routines(
            Cursor([], [approved_geometry]),
            "derived_layers",
            "probe",
            self.inspection(),
        )

    def test_type_probe_checks_output_columns_with_bound_parameters(self):
        cursor = Cursor([], [type_dependency()])

        inspect_relation_types_and_casts(cursor, "derived_layers", "probe")

        query, params = cursor.calls[0]
        self.assertIn("FROM pg_attribute AS attribute", query)
        self.assertEqual(2, query.count("%s"))
        self.assertEqual(
            ('derived_layers."probe"', 'derived_layers."probe"'),
            params,
        )


if __name__ == "__main__":
    unittest.main()
