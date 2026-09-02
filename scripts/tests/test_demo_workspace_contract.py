from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEMO_ROOT = ROOT / "docker" / "demo-sources"
DEMO_WORKSPACE = DEMO_ROOT / "workspace-demo.json"
SEED_WORKSPACE = ROOT / "instance" / "workspace.seed.json"
DERIVED_FIXTURES = DEMO_ROOT / "derived-layers"
OLD_RECIPES = DEMO_ROOT / "recipes"
SEED_SCRIPT = DEMO_ROOT / "seed.sh"
EXPECTED_FIXTURE_STEMS = frozenset({
    "census-oa-population-quintiles",
    "definitive-paths-length-cost",
    "census-oa-country-birth-categories",
    "foreign-birth-categories-h3-r9",
})
EXPECTED_DERIVED_NAMES = frozenset(
    stem.replace("-", "_") for stem in EXPECTED_FIXTURE_STEMS
)

sys.path.insert(0, str(ROOT / "config-ui"))

from workspace_schema import validate_workspace


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def workspace_relations(workspace: dict) -> set[str]:
    relations: set[str] = set()
    locales = []
    if isinstance(workspace.get("locale"), dict):
        locales.append(workspace["locale"])
    if isinstance(workspace.get("locales"), dict):
        locales.extend(
            locale
            for locale in workspace["locales"].values()
            if isinstance(locale, dict)
        )
    for locale in locales:
        layers = locale.get("layers")
        if not isinstance(layers, dict):
            continue
        for layer in layers.values():
            if not isinstance(layer, dict):
                continue
            table = layer.get("table")
            if isinstance(table, str):
                relations.add(table)
            tables = layer.get("tables")
            if isinstance(tables, dict):
                relations.update(
                    relation
                    for relation in tables.values()
                    if isinstance(relation, str)
                )
    return relations


class DemoWorkspaceContractTests(unittest.TestCase):
    def manifests(self) -> dict[Path, dict]:
        return {
            path: read_json(path)
            for path in sorted(DERIVED_FIXTURES.glob("*.json"))
        }

    def test_full_demo_workspace_satisfies_the_live_workspace_contract(self):
        workspace = read_json(DEMO_WORKSPACE)

        self.assertEqual([], validate_workspace(workspace, {"MAPP"}))
        locale = workspace["locale"]
        self.assertEqual(10, len(locale["layers"]))
        self.assertEqual(set(locale["layers"]), set(locale["layer_order"]))
        self.assertEqual(
            ["/instance/plugins/tile-retry/index.mjs"],
            locale.get("plugins"),
        )
        self.assertEqual(["tile_retry"], locale.get("syncPlugins"))
        self.assertEqual({}, locale.get("tile_retry"))

    def test_every_derived_reference_has_one_manifest_and_adjacent_query(self):
        workspace = read_json(DEMO_WORKSPACE)
        manifests = self.manifests()
        manifest_stems = {path.stem for path in manifests}
        query_stems = {path.stem for path in DERIVED_FIXTURES.glob("*.sql")}
        derived_references = {
            relation.removeprefix("derived_layers.")
            for relation in workspace_relations(workspace)
            if relation.startswith("derived_layers.")
        }
        manifest_names = {manifest.get("name") for manifest in manifests.values()}

        self.assertEqual(EXPECTED_FIXTURE_STEMS, manifest_stems)
        self.assertEqual(EXPECTED_FIXTURE_STEMS, query_stems)
        self.assertEqual(EXPECTED_DERIVED_NAMES, manifest_names)
        self.assertEqual(EXPECTED_DERIVED_NAMES, derived_references)

        for manifest_path, manifest in manifests.items():
            with self.subTest(manifest=manifest_path.name):
                query_file = manifest.get("queryFile")
                self.assertIsInstance(query_file, str)
                self.assertEqual(Path(query_file).name, query_file)
                self.assertEqual(manifest_path.with_suffix(".sql").name, query_file)
                self.assertTrue(
                    (manifest_path.parent / query_file).is_file(),
                    f"Missing query file for {manifest_path.name}: {query_file}",
                )
                legacy_hashes = manifest.get("legacyQuerySha256")
                self.assertIsInstance(legacy_hashes, list)
                self.assertTrue(all(
                    isinstance(value, str)
                    and re.fullmatch(r"[0-9a-f]{64}", value)
                    for value in legacy_hashes
                ))

    def test_seed_and_demo_use_the_same_default_spatial_scope(self):
        seed = read_json(SEED_WORKSPACE)
        demo = read_json(DEMO_WORKSPACE)
        seed_locale = seed["locale"]
        demo_locale = demo["locale"]

        self.assertEqual("MAPP", seed.get("dbs"))
        self.assertEqual(seed.get("dbs"), demo.get("dbs"))
        self.assertEqual(seed_locale.get("extent"), demo_locale.get("extent"))
        self.assertEqual(seed_locale.get("view"), demo_locale.get("view"))
        for manifest_path, manifest in self.manifests().items():
            with self.subTest(manifest=manifest_path.name):
                self.assertEqual(
                    {"type": "workspace-map-extent", "locale": "locale"},
                    manifest.get("spatialScope"),
                )

    def test_global_h3_fixture_uses_geography_spheroid_and_multipolygon(self):
        manifests = self.manifests()
        fixture_paths = [DEMO_WORKSPACE, SEED_WORKSPACE]
        fixture_paths.extend(manifests)
        fixture_paths.extend(sorted(DERIVED_FIXTURES.glob("*.sql")))
        for path in fixture_paths:
            with self.subTest(no_uk_area_crs=path.relative_to(ROOT)):
                self.assertNotIn("27700", path.read_text(encoding="utf-8"))

        h3_manifest_path = (
            DERIVED_FIXTURES / "foreign-birth-categories-h3-r9.json"
        )
        h3_manifest = manifests[h3_manifest_path]
        h3_sql = (
            h3_manifest_path.parent / h3_manifest["queryFile"]
        ).read_text(encoding="utf-8")
        normalized_sql = re.sub(r"\s+", " ", h3_sql).lower()

        self.assertEqual("foreign_birth_categories_h3_r9", h3_manifest["name"])
        self.assertEqual("materialized", h3_manifest["kind"])
        self.assertEqual("geom_3857", h3_manifest["geometryColumn"])
        self.assertIn("::public.geography", normalized_sql)
        self.assertIn("public.st_intersection(", normalized_sql)
        self.assertIn("source.geom_3857 && source_scope.geom_source", normalized_sql)
        self.assertIn(
            "public.st_transform(source_geom, 4326)::public.geography",
            normalized_sql,
        )
        self.assertGreaterEqual(normalized_sql.count("public.st_area("), 2)
        self.assertGreaterEqual(
            len(re.findall(r",\s*true\s*\)", normalized_sql)),
            2,
            "Every geodetic area calculation must explicitly request spheroid use.",
        )
        self.assertIn("public.st_multi(", normalized_sql)
        self.assertRegex(
            normalized_sql,
            r"geometry\s*\(\s*multipolygon\s*,\s*4326\s*\)",
        )
        self.assertRegex(
            normalized_sql,
            r"geometry\s*\(\s*multipolygon\s*,\s*3857\s*\)",
        )

    def test_no_legacy_or_unreferenced_demo_recipes_remain(self):
        self.assertFalse(
            OLD_RECIPES.exists(),
            "The superseded recipe directory must be removed, not retained.",
        )
        manifest_names = {
            manifest["name"] for manifest in self.manifests().values()
        }
        derived_references = {
            relation.removeprefix("derived_layers.")
            for relation in workspace_relations(read_json(DEMO_WORKSPACE))
            if relation.startswith("derived_layers.")
        }
        self.assertEqual(derived_references, manifest_names)

    def test_retained_source_reader_credentials_are_reconciled(self):
        source = SEED_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'READER_PASSWORD="$(dotenv_value SOURCE_READER_PASSWORD)"',
            source,
        )
        self.assertIn(
            'ALTER ROLE :"reader_user" LOGIN PASSWORD :\'reader_password\'',
            source,
        )


if __name__ == "__main__":
    unittest.main()
