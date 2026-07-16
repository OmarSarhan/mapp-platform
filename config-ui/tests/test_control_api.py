import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from control_api import (
    apply_operations,
    apply_visual_override,
    contract,
    deep_merge,
    effective_locales,
    examples,
    is_probeable_database_layer,
    pointer_get,
    proposal_create,
    reload_status,
    reload_timeout,
    request_reload,
    select_locale,
    strict_json_loads,
    visual_plan,
    workspace_fingerprint,
)
from control_plane import ControlStore


class ControlApiTests(unittest.TestCase):
    def test_pointer_and_operations_preserve_unrelated_values(self):
        source = {"locale": {"layers": {"Bus Stops": {"style": {"default": {"icon": {"fillColor": "#0f0", "scale": 1}}}}}}, "plugin": {"x": 1}}
        candidate, diff = apply_operations(source, [{
            "op": "set",
            "path": "/locale/layers/Bus Stops/style/default/icon/fillColor",
            "value": "#2563eb",
        }])
        self.assertEqual("#2563eb", pointer_get(candidate, "/locale/layers/Bus Stops/style/default/icon/fillColor"))
        self.assertEqual({"x": 1}, candidate["plugin"])
        self.assertEqual("#0f0", diff[0]["old"])

    def test_pointer_escaping(self):
        self.assertEqual(1, pointer_get({"a/b": {"x~y": 1}}, "/a~1b/x~0y"))
        self.assertEqual(2, pointer_get({"": 2}, "/"))
        for pointer in ("/bad~2escape", "/trailing~"):
            with self.subTest(pointer=pointer), self.assertRaises(ValueError):
                pointer_get({}, pointer)

    def test_pointer_failures_are_explicit_validation_errors(self):
        invalid_operations = (
            [{"op": "set", "path": "/missing/child", "value": 1}],
            [{"op": "set", "path": "/items/not-a-number", "value": 1}],
            [{"op": "set", "path": "/items/01", "value": 1}],
            [{"op": "set", "path": "/items/2", "value": 1}],
            [{"op": "unset", "path": "/missing"}],
        )
        source = {"items": [0]}
        for operations in invalid_operations:
            with self.subTest(operations=operations), self.assertRaises(ValueError):
                apply_operations(source, operations)

    def test_array_set_can_replace_or_append_only(self):
        replaced, _ = apply_operations(
            {"items": [0]},
            [{"op": "set", "path": "/items/0", "value": 1}],
        )
        appended, _ = apply_operations(
            {"items": [0]},
            [{"op": "set", "path": "/items/1", "value": 1}],
        )
        self.assertEqual([1], replaced["items"])
        self.assertEqual([0, 1], appended["items"])

    def test_operation_evidence_is_not_mutated_by_later_operations(self):
        operations = [
            {"op": "set", "path": "/value", "value": {"nested": 1}},
            {"op": "set", "path": "/value/nested", "value": 2},
        ]
        candidate, diff = apply_operations({}, operations)
        self.assertEqual({"nested": 2}, candidate["value"])
        self.assertEqual({"nested": 1}, operations[0]["value"])
        self.assertEqual({"nested": 1}, diff[0]["value"])

    def test_unset(self):
        candidate, diff = apply_operations({"a": {"b": 1, "c": 2}}, [{"op": "unset", "path": "/a/b"}])
        self.assertEqual({"a": {"c": 2}}, candidate)
        self.assertEqual("remove", diff[0]["op"])

    def test_repeated_proposals_receive_unique_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            original = {"key": "workspace", "locale": {"layers": {}}}
            candidate = {**original, "title": "Candidate"}
            arguments = (
                store,
                original,
                "revision",
                candidate,
                [{"op": "set", "path": "/title", "value": "Candidate"}],
                [{"op": "add", "path": "/title", "old": None, "value": "Candidate"}],
                "token:test",
            )
            first = proposal_create(*arguments)
            second = proposal_create(*arguments)
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(
                0o600,
                (store.proposals / first["id"] / "proposal.json").stat().st_mode
                & 0o777,
            )

    def test_contract_lists_only_implemented_proposal_commands(self):
        commands = contract("instance")["commands"]
        self.assertNotIn("proposals delete", commands)
        self.assertNotIn("completion-spec", commands)

    def test_examples_preserve_revision_and_confirmation_guards(self):
        workflow = examples()["workflow"]
        proposal_create_command = next(
            command for command in workflow if "proposals create" in command
        )
        proposal_apply_command = next(
            command for command in workflow if "proposals apply" in command
        )
        self.assertIn("--base-revision", proposal_create_command)
        self.assertIn("--confirm", proposal_apply_command)
        self.assertNotIn("<", proposal_create_command + proposal_apply_command)

    def test_visual_override_is_bounded_and_preserves_base_evidence(self):
        plan = {
            "source": "postgis-extent",
            "centre": [-1.5, 53.8],
            "zoom": 14,
        }
        overridden = apply_visual_override(
            plan,
            {"centre": [-1.55, 53.81], "zoom": 12.5},
        )
        self.assertEqual("explicit-view", overridden["source"])
        self.assertEqual("postgis-extent", overridden["baseSource"])
        self.assertEqual([-1.55, 53.81], overridden["centre"])
        self.assertEqual(12.5, overridden["zoom"])
        with self.assertRaises(ValueError):
            apply_visual_override(plan, {"centre": [181, 53.8]})
        with self.assertRaises(ValueError):
            apply_visual_override(plan, {"zoom": float("nan")})

    def test_workspace_fingerprint_hashes_exact_saved_bytes(self):
        first = b'{"value":1.0}\n'
        second = b'{"value":1}\n'
        self.assertNotEqual(
            workspace_fingerprint(first),
            workspace_fingerprint(second),
        )

    def test_strict_json_rejects_nonstandard_numeric_constants(self):
        for raw in ('{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json_loads(raw)

    def test_reload_requests_are_atomic_and_receive_unique_generations(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "control_api.RELOAD_DIR",
            Path(directory),
        ):
            generations = []
            lock = threading.Lock()

            def issue(index):
                result = request_reload(f"{index:064x}")
                with lock:
                    generations.append(result["requestedGeneration"])

            threads = [threading.Thread(target=issue, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(list(range(1, 13)), sorted(generations))
            self.assertEqual(12, reload_status()["requestedGeneration"])
            self.assertEqual([], list(Path(directory).glob("*.tmp")))

    def test_reload_input_is_validated_before_requesting(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "control_api.RELOAD_DIR",
            Path(directory),
        ):
            with self.assertRaises(ValueError):
                request_reload("not-a-fingerprint")
            self.assertFalse((Path(directory) / "requested").exists())
        for value in (True, 0, 121, float("inf"), "30"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reload_timeout(value)

    def test_locale_selection_defaults_to_top_level_and_merges_named_choices(self):
        workspace = {
            "locale": {
                "layers": {
                    "Stops": {
                        "format": "mvt",
                        "style": {"default": {"strokeWidth": 2}},
                    }
                }
            },
            "locales": {
                "en-GB": {},
                "cy-GB": {
                    "layers": {
                        "Stops": {
                            "name": "Safleoedd",
                            "style": {
                                "default": {"strokeColor": "#123456"}
                            },
                        }
                    }
                },
            }
        }
        default_name, default_locale = select_locale(workspace)
        self.assertEqual("locale", default_name)
        self.assertEqual(
            {"strokeWidth": 2},
            default_locale["layers"]["Stops"]["style"]["default"],
        )
        name, locale = select_locale(workspace, "cy-GB")
        self.assertEqual("cy-GB", name)
        self.assertEqual("mvt", locale["layers"]["Stops"]["format"])
        self.assertEqual("Safleoedd", locale["layers"]["Stops"]["name"])
        self.assertEqual(
            {
                "strokeWidth": 2,
                "strokeColor": "#123456",
            },
            locale["layers"]["Stops"]["style"]["default"],
        )
        self.assertEqual(
            {"locale", "en-GB", "cy-GB"},
            set(effective_locales(workspace)),
        )

    def test_locale_merge_matches_xyz_array_rules(self):
        merged = deep_merge(
            {
                "controls": ["zoom", "scale"],
                "infoj": [{"field": "name"}],
            },
            {
                "controls": ["scale"],
                "infoj": [{"field": "name"}],
            },
        )
        # A scalar-only source subset replaces the target array.
        self.assertEqual(["scale"], merged["controls"])
        # Distinct JSON objects have JavaScript identity semantics in XYZ, so
        # separately parsed object entries are concatenated.
        self.assertEqual(
            [{"field": "name"}, {"field": "name"}],
            merged["infoj"],
        )
        self.assertEqual(
            {
                "truthy": "keep",
                "array": [1],
                "falsy": {"added": True},
            },
            deep_merge(
                {"truthy": "keep", "array": [1], "falsy": ""},
                {
                    "truthy": {"ignored": True},
                    "array": {"ignored": True},
                    "falsy": {"added": True},
                },
            ),
        )

    def test_missing_default_locale_is_synthesized_like_xyz_cache(self):
        workspace = {
            "locales": {
                "alternative": {"name": "Alternative"},
            },
        }
        name, locale = select_locale(workspace)
        self.assertEqual("locale", name)
        self.assertEqual({"layers": {}}, locale)
        self.assertEqual(
            {
                "layers": {},
                "name": "Alternative",
            },
            effective_locales(workspace)["alternative"],
        )

    def test_advanced_layers_use_workspace_view_without_database_probe(self):
        workspace = {
            "locale": {
                "layers": {
                    "External": {
                        "format": "maplibre",
                        "style": {"URL": "https://tiles.example.invalid/style"},
                    }
                }
            }
        }
        plan = visual_plan(workspace, "External", {})
        self.assertEqual("workspace-view", plan["source"])
        self.assertNotIn("centre", plan)
        self.assertNotIn("zoom", plan)
        self.assertFalse(
            is_probeable_database_layer(
                {"template": "OSM"}
            )
        )
