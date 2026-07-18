import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from control_api import (
    RULES,
    apply_operations,
    apply_visual_override,
    capabilities,
    contract,
    deep_merge,
    effective_locales,
    examples,
    is_probeable_database_layer,
    pointer_get,
    proposal_create,
    proposal_check,
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
    def test_capabilities_publish_stable_action_schemas_and_operation_contract(self):
        payload = capabilities("instance")
        actions = {item["id"]: item for item in payload["actions"]}
        self.assertEqual("instance", payload["instanceId"])
        self.assertEqual(
            ["revision", "operations"],
            actions["proposals.check"]["inputSchema"]["required"],
        )
        self.assertEqual("apply", actions["proposals.apply"]["risk"])
        self.assertEqual(
            "proposal.visual-test",
            actions["proposals.visual-test"]["operationKind"],
        )
        self.assertEqual(
            "proposal.screenshot",
            actions["proposals.screenshot"]["operationKind"],
        )
        screenshot_properties = actions["proposals.screenshot"]["inputSchema"][
            "properties"
        ]
        self.assertEqual(2560, screenshot_properties["viewport"]["properties"][
            "width"
        ]["maximum"])
        self.assertEqual(
            {"width": 1080, "height": 1080},
            screenshot_properties["viewport"]["default"],
        )
        self.assertEqual(3, screenshot_properties["deviceScaleFactor"]["maximum"])
        self.assertEqual(1, screenshot_properties["deviceScaleFactor"]["default"])
        self.assertEqual(
            "proposal.visual-test",
            actions["proposals.preview-screenshot"]["operationKind"],
        )
        screenshot_schema = actions[
            "proposals.preview-screenshot"
        ]["inputSchema"]["properties"]
        self.assertEqual(
            2560,
            screenshot_schema["viewport"]["properties"]["width"]["maximum"],
        )
        self.assertEqual(3, screenshot_schema["deviceScaleFactor"]["maximum"])
        self.assertIn(
            "workspaceFingerprint",
            actions["xyz.reload"]["inputSchema"]["properties"],
        )
        self.assertNotIn(
            "fingerprint",
            actions["xyz.reload"]["inputSchema"]["properties"],
        )
        self.assertEqual("meta", payload["responseEnvelope"]["metadataField"])

    def test_contract_advertises_scoped_device_authorization(self):
        authentication = contract("instance")["authentication"]
        self.assertEqual(
            ["inspect", "propose", "visual"],
            authentication["defaultDeviceScopes"],
        )
        self.assertEqual(
            {"full", "inspect", "propose", "visual", "apply", "reload", "derive"},
            set(authentication["scopes"]),
        )

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

    def test_proposal_check_returns_evidence_without_persisting(self):
        original = {"key": "workspace", "locale": {"layers": {}}}
        operations = [{"op": "set", "path": "/title", "value": "Candidate"}]
        candidate, diff = apply_operations(original, operations)
        checked = proposal_check(
            original, "revision", candidate, operations, diff, "A focused edit.",
        )
        self.assertTrue(checked["valid"])
        self.assertFalse(checked["proposalCreated"])
        self.assertEqual("revision", checked["originalRevision"])
        self.assertEqual(diff, checked["diff"])
        self.assertRegex(checked["checkFingerprint"], r"^[0-9a-f]{64}$")
        changed = proposal_check(
            original, "revision-2", candidate, operations, diff, "A focused edit.",
        )
        self.assertNotEqual(
            checked["checkFingerprint"], changed["checkFingerprint"]
        )
        changed_operations = [{"op": "set", "path": "/title", "value": "Other"}]
        other_candidate, other_diff = apply_operations(original, changed_operations)
        other = proposal_check(
            original, "revision", other_candidate, changed_operations, other_diff,
        )
        self.assertNotEqual(checked["checkFingerprint"], other["checkFingerprint"])
        self.assertNotIn("id", checked)
        self.assertNotIn("status", checked)

    def test_contract_lists_only_implemented_proposal_commands(self):
        advertised = contract("instance")
        commands = advertised["commands"]
        self.assertIn("proposals check", commands)
        self.assertNotIn("proposals delete", commands)
        self.assertNotIn("completion-spec", commands)
        self.assertIn("apply with managed reload", advertised["workflow"])
        self.assertIn("check reload status", advertised["workflow"])
        self.assertNotIn("reload", advertised["workflow"])

    def test_examples_preserve_revision_and_confirmation_guards(self):
        advertised_examples = examples()
        workflow = advertised_examples["workflow"]
        proposal_check_command = next(
            command for command in workflow if "proposals check" in command
        )
        proposal_create_command = next(
            command for command in workflow if "proposals create" in command
        )
        proposal_apply_command = next(
            command for command in workflow if "proposals apply" in command
        )
        self.assertIn("--base-revision", proposal_check_command)
        self.assertIn("--from-check", proposal_create_command)
        self.assertIn("--confirm", proposal_apply_command)
        self.assertNotIn(
            "<",
            proposal_check_command + proposal_create_command + proposal_apply_command,
        )
        drawing_order = advertised_examples["setLayerDrawingOrder"]
        self.assertEqual(
            [10, 20],
            [operation["value"] for operation in drawing_order["operations"]],
        )
        self.assertIn("navigation only", drawing_order["explanation"])

    def test_layer_order_rule_distinguishes_navigation_from_rendering(self):
        layer_order = next(
            rule for rule in RULES if rule["id"] == "workspace.layer_order"
        )
        self.assertIn("navigation drawers only", layer_order["description"])
        self.assertIn("higher values render above", layer_order["description"])
        self.assertIn("promoteDisplay", layer_order["remediation"])

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

    def test_reload_generation_advances_past_applied_when_requested_is_invalid_or_stale(self):
        cases = (None, "corrupt\n", "-1\n", "3\n")
        fingerprint = "a" * 64
        for requested in cases:
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as directory, patch(
                "control_api.RELOAD_DIR",
                Path(directory),
            ):
                reload_dir = Path(directory)
                if requested is not None:
                    (reload_dir / "requested").write_text(requested)
                (reload_dir / "applied").write_text("7\n")
                (reload_dir / "healthy").write_text("true\n")
                (reload_dir / "workspace-fingerprint").write_text(
                    f"{fingerprint}\n"
                )

                result = request_reload(fingerprint)
                status = reload_status()

                self.assertEqual(8, result["requestedGeneration"])
                self.assertEqual(8, status["requestedGeneration"])
                self.assertEqual(7, status["appliedGeneration"])
                self.assertTrue(status["healthy"])
                self.assertFalse(
                    status["appliedGeneration"] >= result["requestedGeneration"]
                    and status["healthy"]
                )

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
        with self.assertRaisesRegex(ValueError, "Unknown locale"):
            select_locale(workspace, "")

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
