import json
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
from federation_schema import (
    ALIAS_PATTERN,
    MAX_GROUPS_PER_ALIAS,
    MAX_GROUP_DESCRIPTION,
    FederationSchemaError,
    validate_group_definition,
)
from control_api import (
    ACTION_SCHEMAS,
    CollectionPaginationError,
    RULES,
    VisualPlanningDatabaseError,
    VisualPlanningNoMatchingFeatures,
    apply_operations,
    apply_visual_override,
    capabilities,
    contract,
    decode_position_cursor,
    deep_merge,
    effective_layer_filter,
    effective_locales,
    enforce_collection_payload,
    examples,
    is_probeable_database_layer,
    unpaginated_collection,
    paginate_collection,
    paginate_keyset_page,
    pagination_parameters,
    pointer_get,
    plugin_manifest,
    proposal_create,
    proposal_check,
    proposal_list,
    reload_status,
    reload_timeout,
    request_reload,
    select_locale,
    strict_json_loads,
    visual_plan,
    workspace_fingerprint,
    workspace_map_extent,
)
from control_plane import ControlStore


class ControlApiTests(unittest.TestCase):
    def test_contract_advertises_background_job_inspection(self):
        advertised = contract("instance")
        advertised_capabilities = capabilities("instance")

        self.assertIn("derived-layers jobs", advertised["commands"])
        self.assertEqual(
            {
                "id": "derived-layers.background-jobs",
                "method": "GET",
                "path": "/api/derived-layers/background-jobs",
                "risk": "inspect",
                "scope": "inspect",
            },
            next(
                action for action in advertised_capabilities["actions"]
                if action["id"] == "derived-layers.background-jobs"
            ),
        )

    def test_contract_advertises_exactly_the_federation_cli_commands(self):
        # This assertion used to be the inverse: advertising a command the CLI
        # cannot run is a lie, and the CLI refuses anything unadvertised with
        # capability.missing, so the two repositories have to move together.
        # It stays an equality rather than a subset so a route added here
        # without a CLI command still fails.
        self.assertEqual(
            [
                "federation list",
                "federation show",
                "federation register",
                "federation observe",
                "federation provision",
                "federation retire",
                "federation groups",
                "federation group-define",
                "federation group-delete",
                "federation set-groups",
            ],
            [
                command
                for command in contract("instance")["commands"]
                if command.startswith("federation ")
            ],
        )

    def test_contract_advertises_every_stable_cli_exit_code(self):
        self.assertEqual(
            {
                "success": 0,
                "usage": 2,
                "validation": 3,
                "conflict": 4,
                "connectivity": 5,
                "visual": 6,
                "authentication": 7,
                "interrupted": 130,
            },
            contract("instance")["exitCodes"],
        )

    def test_plugin_manifest_describes_loader_dispatch_and_exact_registry(self):
        payload = plugin_manifest()
        keys = {item["key"] for item in payload["bundled"]}
        self.assertEqual("v4.23.4", payload["xyzVersion"])
        self.assertEqual(64, len(payload["fingerprint"]))
        external = {item["id"]: item for item in payload["external"]}
        self.assertTrue(external["viewport-layer-count"]["available"])
        self.assertTrue(external["tile-retry"]["available"])
        self.assertEqual(
            "/instance/plugins/viewport-layer-count/index.mjs",
            external["viewport-layer-count"]["entryUrl"],
        )
        self.assertEqual(
            "/instance/plugins/tile-retry/index.mjs",
            external["tile-retry"]["entryUrl"],
        )
        self.assertIn("allSettled", payload["loading"]["failure"])
        self.assertIn("not awaited", payload["dispatch"]["layer"])
        self.assertEqual({
            "admin", "consent", "custom_theme", "dark_mode", "feature_info",
            "fullscreen", "layer_order", "link_button", "locator", "login",
            "svg_templates", "test", "userIDB", "userLayer", "userLocale",
            "zoomBtn", "zoomToArea",
        }, keys)

    def test_capabilities_publish_stable_action_schemas_and_operation_contract(self):
        payload = capabilities("instance")
        actions = {item["id"]: item for item in payload["actions"]}
        self.assertEqual("instance", payload["instanceId"])
        self.assertEqual("1.6", payload["apiVersion"])
        self.assertEqual("1.6", payload["contractVersion"])
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
        self.assertNotIn("querySchema", actions["federation.aliases.list"])
        allowed_relations = actions["federation.aliases.register"][
            "inputSchema"
        ]["properties"]["allowedRelations"]
        self.assertEqual(100, allowed_relations["maxItems"])
        self.assertEqual(127, allowed_relations["items"]["maxLength"])
        self.assertEqual(
            "^[A-Za-z_][A-Za-z0-9_]{0,62}\\."
            "[A-Za-z_][A-Za-z0-9_]{0,62}$",
            allowed_relations["items"]["pattern"],
        )
        self.assertEqual(
            "^[A-Za-z][A-Za-z0-9_]{0,55}$",
            actions["federation.aliases.register"]["inputSchema"][
                "properties"
            ]["alias"]["pattern"],
        )
        provision_schema = actions["federation.aliases.provision"][
            "inputSchema"
        ]
        self.assertEqual(["expectedObservationId"], provision_schema["required"])
        self.assertEqual(
            {
                "type": "integer",
                "minimum": 1,
                "maximum": 9223372036854775807,
            },
            provision_schema["properties"]["expectedObservationId"],
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
            20,
            screenshot_properties["expectedInfoPanelText"]["maxItems"],
        )
        self.assertEqual(
            1000,
            screenshot_properties["expectedInfoPanelText"]["items"][
                "maxLength"
            ],
        )
        self.assertEqual(
            "proposal.screenshot",
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
        self.assertEqual({"type": "boolean"}, screenshot_schema["background"])
        self.assertIn("expectedInfoPanelText", screenshot_schema)
        self.assertIn(
            "workspaceFingerprint",
            actions["xyz.reload"]["inputSchema"]["properties"],
        )
        self.assertNotIn(
            "fingerprint",
            actions["xyz.reload"]["inputSchema"]["properties"],
        )
        semantic_check = actions["semantic.proposals.check"]["inputSchema"]
        semantic_create = actions["semantic.proposals.create"]["inputSchema"]
        self.assertFalse(semantic_check["additionalProperties"])
        self.assertFalse(semantic_create["additionalProperties"])
        self.assertEqual(
            ["assetId", "baseVersion", "operations"],
            semantic_check["required"],
        )
        self.assertEqual(
            ["assetId", "baseVersion", "operations", "fingerprint"],
            semantic_create["required"],
        )
        self.assertNotIn("fingerprint", semantic_check["properties"])
        self.assertEqual(
            r"^[0-9a-f]{64}$",
            semantic_create["properties"]["fingerprint"]["pattern"],
        )
        operations = semantic_check["properties"]["operations"]
        self.assertEqual((1, 100), (operations["minItems"], operations["maxItems"]))
        variants = operations["items"]["oneOf"]
        self.assertEqual({"set", "unset"}, {
            variant["properties"]["op"]["const"] for variant in variants
        })
        self.assertTrue(all(
            variant["additionalProperties"] is False for variant in variants
        ))
        self.assertEqual(
            2000,
            actions["semantic.proposals.decline"]["inputSchema"][
                "properties"
            ]["reason"]["maxLength"],
        )
        generation = actions["semantic.generate"]
        self.assertEqual(
            ["semantic:inspect", "semantic:generate"],
            generation["requiredScopes"],
        )
        self.assertEqual(
            [{
                "whenAnyTrue": [
                    "contextOptions.sampleRows",
                    "contextOptions.statistics",
                ],
                "requiredScopes": ["semantic:data"],
                "reason": (
                    "Optional row samples or data-derived statistics require "
                    "explicit data access."
                ),
            }],
            generation["conditionalScopes"],
        )
        source_sync = actions["semantic.source.sync"]
        self.assertEqual(
            ["semantic:inspect", "semantic:source"],
            source_sync["requiredScopes"],
        )
        self.assertEqual(
            ["alias", "schema", "relation"],
            source_sync["inputSchema"]["required"],
        )
        self.assertFalse(
            source_sync["inputSchema"]["additionalProperties"]
        )
        self.assertEqual(
            "^[A-Za-z][A-Za-z0-9_-]{0,62}$",
            source_sync["inputSchema"]["properties"]["alias"]["pattern"],
        )
        for action_id, path_key, path in (
            (
                "semantic.catalog.archive",
                "pathTemplate",
                "/api/semantic/catalog/objects/{assetId}/archive",
            ),
            (
                "semantic.source.archive-excluded",
                "path",
                "/api/semantic/source/archive-excluded",
            ),
        ):
            archive = actions[action_id]
            self.assertEqual(path, archive[path_key])
            self.assertEqual(
                ["semantic:inspect", "semantic:admin"],
                archive["requiredScopes"],
            )
            self.assertEqual(
                ["confirmed"],
                archive["inputSchema"]["required"],
            )
        self.assertEqual(
            "/api/semantic/catalog/objects/{assetId}/history",
            actions["semantic.catalog.history"]["pathTemplate"],
        )
        self.assertEqual(
            "/api/derived-layers/map-extent",
            actions["derived-layers.map-extent"]["path"],
        )
        spatial_scope = actions["derived-layers.create"]["inputSchema"][
            "properties"
        ]["spatialScope"]
        self.assertEqual(
            "workspace-map-extent",
            spatial_scope["properties"]["type"]["const"],
        )
        self.assertFalse(spatial_scope["additionalProperties"])
        self.assertEqual(
            {"type": "workspace-map-extent"},
            spatial_scope["default"],
        )
        for action in (
            "derived-layers.create",
            "derived-layers.refresh",
            "derived-layers.replace",
        ):
            presentation = actions[action]["presentation"]
            self.assertTrue(
                presentation["nextActionField"].endswith("suggestedAction")
            )
            self.assertIn(
                "materializationProbe",
                presentation["technicalFields"],
            )
            self.assertIn(
                "queryPlanProbe",
                presentation["technicalFields"],
            )
            self.assertIn(
                "queryPlanningProbe",
                presentation["technicalFields"],
            )
            self.assertEqual("reasons", presentation["reasonField"])
            self.assertEqual(
                "suggestedAction",
                presentation["reasonActionField"],
            )
            self.assertEqual("safeState", presentation["safeStateField"])
            self.assertEqual(
                "stateUnchanged",
                presentation["stateUnchangedField"],
            )
            self.assertEqual("rolledBack", presentation["rolledBackField"])
            self.assertEqual("retryable", presentation["retryableField"])
            self.assertEqual(
                "derived_layer.database_contention",
                presentation["contentionErrorCode"],
            )
            self.assertEqual(
                "contentionScope",
                presentation["contentionScopeField"],
            )
            self.assertEqual(
                ["derived-mutation", "postgresql-lock"],
                presentation["contentionScopes"],
            )
            self.assertEqual(
                "indeterminate",
                presentation["indeterminateField"],
            )
            self.assertEqual(
                "failurePhase",
                presentation["failurePhaseField"],
            )
            self.assertEqual(
                [
                    "preflight",
                    "database-transaction",
                    "transaction-rollback",
                    "transaction-commit",
                    "result-reporting",
                    "request-response",
                    "operation-polling",
                    "service-recovery",
                ],
                presentation["failurePhases"],
            )
            self.assertEqual("probe", presentation["probeField"])
            self.assertEqual(
                "queryPlanningProbe",
                presentation["queryPlanningProbeField"],
            )
            self.assertEqual(
                {
                    "invalid": "derived_layer.query_invalid",
                    "policy": "derived_layer.query_not_allowed",
                    "compute": "derived_layer.query_too_expensive",
                },
                presentation["queryErrorCodes"],
            )
        for action_id, operation_kind in (
            ("derived-layers.create", "derived-layer.create"),
            ("derived-layers.replace", "derived-layer.replace"),
            ("derived-layers.refresh", "derived-layer.refresh"),
        ):
            action = actions[action_id]
            self.assertEqual(operation_kind, action["operationKind"])
            self.assertEqual(
                {"type": "boolean"},
                action["inputSchema"]["properties"]["background"],
            )
        self.assertEqual(
            ["derive", "semantic:inspect"],
            actions["derived-layers.create"]["requiredScopes"],
        )
        self.assertEqual(
            ["derive", "semantic:inspect"],
            actions["derived-layers.replace"]["requiredScopes"],
        )
        statistics = actions["layers.statistics"]
        self.assertEqual(
            "/api/layers/{layerKey}/statistics",
            statistics["pathTemplate"],
        )
        self.assertEqual(
            ["derive", "semantic:inspect"],
            statistics["requiredScopes"],
        )
        self.assertEqual(
            50,
            statistics["querySchema"]["properties"]["bins"]["maximum"],
        )
        self.assertEqual(
            10,
            statistics["querySchema"]["properties"]["bins"]["default"],
        )
        recipe = actions["derived-layers.plan-area-weighted-h3"]
        self.assertEqual(
            "/api/derived-layers/recipes/area-weighted-h3/plan",
            recipe["path"],
        )
        self.assertEqual(
            ["derive", "semantic:inspect"],
            recipe["requiredScopes"],
        )
        self.assertEqual(
            32,
            recipe["inputSchema"]["properties"]["measures"]["maxItems"],
        )
        self.assertIn(
            "semantic catalog history",
            contract("instance")["commands"],
        )
        self.assertIn(
            "derived-layers map-extent",
            contract("instance")["commands"],
        )
        self.assertIn("layers statistics", contract("instance")["commands"])
        self.assertIn(
            "derived-layers plan-area-weighted-h3",
            contract("instance")["commands"],
        )
        self.assertIn(
            "semantic catalog archive",
            contract("instance")["commands"],
        )
        self.assertIn(
            "semantic source archive-excluded",
            contract("instance")["commands"],
        )
        self.assertIn("layers effective", contract("instance")["commands"])
        self.assertFalse(generation["inputSchema"]["additionalProperties"])
        self.assertEqual(
            {"table", "field"},
            {
                target["properties"]["kind"]["const"]
                for target in generation["inputSchema"]["properties"][
                    "target"
                ]["oneOf"]
            },
        )
        self.assertEqual("meta", payload["responseEnvelope"]["metadataField"])
        for action_id in ("visual.plan", "visual.test", "visual.screenshot"):
            self.assertIn(
                "expectedInfoPanelText",
                actions[action_id]["inputSchema"]["properties"],
            )
        self.assertIn(
            "expectedInfoPanelText",
            actions["proposals.preview-test"]["inputSchema"]["properties"],
        )
        self.assertEqual(
            ["confirmed"],
            actions["xyz.reload"]["inputSchema"]["required"],
        )

    def test_pagination_is_bounded_opaque_and_filter_bound(self):
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        limit, cursor = pagination_parameters({"limit": ["1"]})
        first, first_page = paginate_collection(
            items,
            limit=limit,
            cursor=cursor,
            scope="proposals-v1",
        )
        self.assertEqual([{"id": "a"}], first)
        self.assertRegex(first_page["nextCursor"], r"^[0-9a-f]{64}$")

        second, second_page = paginate_collection(
            items,
            limit=1,
            cursor=first_page["nextCursor"],
            scope="proposals-v1",
        )
        self.assertEqual([{"id": "b"}], second)
        self.assertRegex(second_page["nextCursor"], r"^[0-9a-f]{64}$")

        with self.assertRaisesRegex(ValueError, "invalid or expired"):
            paginate_collection(
                items,
                limit=1,
                cursor=first_page["nextCursor"],
                scope="different-filter",
            )
        for query in (
            {"limit": ["0"]},
            {"limit": ["101"]},
            {"cursor": ["readable-offset"]},
            {"limit": ["1", "2"]},
            {"unknown": ["1"]},
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                pagination_parameters(query)

    def test_keyset_cursor_is_integrity_and_scope_bound(self):
        key = b"k" * 32
        first, pagination = paginate_keyset_page(
            [{"id": "a"}, {"id": "b"}],
            limit=1,
            scope="catalog:revision-7:inspect",
            key=key,
            position=lambda item: item["id"],
        )
        self.assertEqual(first, [{"id": "a"}])
        cursor = pagination["nextCursor"]
        self.assertRegex(cursor, r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$")
        self.assertEqual(
            decode_position_cursor(
                cursor,
                "catalog:revision-7:inspect",
                key,
            ),
            "a",
        )
        with self.assertRaisesRegex(ValueError, "invalid or expired"):
            decode_position_cursor(
                cursor,
                "catalog:revision-8:inspect",
                key,
            )
        pagination_parameters({"cursor": [cursor]})

    def test_collection_pages_apply_count_and_byte_bounds(self):
        with self.assertRaises(CollectionPaginationError) as required:
            unpaginated_collection([{"id": index} for index in range(101)])
        self.assertEqual("pagination.required", required.exception.code)
        self.assertEqual(HTTPStatus.CONFLICT, required.exception.status)

        items = [
            {"id": "a", "explanation": "x" * 200},
            {"id": "b", "explanation": "y" * 200},
        ]
        first_size = len(json.dumps(
            items[0],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"))
        with patch(
            "control_api.COLLECTION_PAGE_MAX_ITEMS_BYTES",
            first_size + 2,
        ):
            page, pagination = paginate_keyset_page(
                items,
                limit=2,
                scope="workspace-proposals-v1",
                key=b"k" * 32,
                position=lambda item: item["id"],
            )
            with self.assertRaises(CollectionPaginationError) as legacy:
                unpaginated_collection(items)
        self.assertEqual([items[0]], page)
        self.assertIsNotNone(pagination["nextCursor"])
        self.assertEqual("pagination.required", legacy.exception.code)

        with patch(
            "control_api.COLLECTION_PAGE_MAX_ITEMS_BYTES",
            first_size + 1,
        ), self.assertRaises(CollectionPaginationError) as oversized:
            paginate_keyset_page(
                items,
                limit=1,
                scope="workspace-proposals-v1",
                key=b"k" * 32,
                position=lambda item: item["id"],
            )
        self.assertEqual(
            "pagination.page_too_large",
            oversized.exception.code,
        )

        combined = {"primary": items[:1], "diagnostics": items[1:]}
        complete_payload_size = len(json.dumps(
            combined,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"))
        with patch(
            "control_api.COLLECTION_PAGE_MAX_RESPONSE_BYTES",
            complete_payload_size,
        ):
            enforce_collection_payload(combined, paginated=True)
        with patch(
            "control_api.COLLECTION_PAGE_MAX_RESPONSE_BYTES",
            complete_payload_size - 1,
        ), self.assertRaises(CollectionPaginationError) as complete_payload:
            enforce_collection_payload(combined, paginated=True)
        self.assertEqual(
            "pagination.page_too_large",
            complete_payload.exception.code,
        )

    def test_workspace_proposal_page_parses_only_limit_plus_one_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            proposals = Path(directory)
            for name in ("103-c", "102-b", "101-a", "100-z"):
                path = proposals / name
                path.mkdir()
                (path / "proposal.json").write_text("{}")
            store = MagicMock()
            store.proposals = proposals

            def summary(path):
                return {"id": path.parent.name}

            with patch(
                "control_api._proposal_summary",
                side_effect=summary,
            ) as read_summary:
                first = proposal_list(store, fetch_limit=2)
                second = proposal_list(
                    store,
                    after_id=first[-1]["id"],
                    fetch_limit=2,
                )

            self.assertEqual(first, [{"id": "103-c"}, {"id": "102-b"}])
            self.assertEqual(second, [{"id": "101-a"}, {"id": "100-z"}])
            self.assertEqual(read_summary.call_count, 4)

    def test_contract_advertises_scoped_device_authorization(self):
        authentication = contract("instance")["authentication"]
        self.assertEqual(
            ["inspect", "propose", "visual", "semantic:inspect"],
            authentication["defaultDeviceScopes"],
        )
        self.assertEqual(
            {
                "full", "inspect", "propose", "visual", "apply", "reload",
                "derive", "semantic:inspect", "semantic:source",
                "semantic:generate", "semantic:data",
                "semantic:propose",
                "semantic:apply", "semantic:admin",
                "federation:register", "federation:provision",
                "federation:observe",
            },
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

    def test_checked_proposal_reuses_bound_plugin_catalogue_fingerprint(self):
        checked_fingerprint = "a" * 64
        changed_fingerprint = "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            original = {"key": "workspace", "locale": {"layers": {}}}
            operations = [
                {"op": "set", "path": "/title", "value": "Candidate"}
            ]
            candidate, diff = apply_operations(original, operations)
            with patch(
                "control_api.external_plugin_catalogue",
                side_effect=[
                    {"fingerprint": checked_fingerprint},
                    {"fingerprint": changed_fingerprint},
                ],
            ) as catalogue:
                checked = proposal_check(
                    original,
                    "revision",
                    candidate,
                    operations,
                    diff,
                )
                proposal = proposal_create(
                    store,
                    original,
                    "revision",
                    candidate,
                    operations,
                    diff,
                    "token:test",
                    plugin_catalogue_fingerprint=checked[
                        "pluginCatalogueFingerprint"
                    ],
                )

        self.assertEqual(
            checked_fingerprint,
            proposal["pluginCatalogueFingerprint"],
        )
        catalogue.assert_called_once_with()

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
        self.assertIn("semantic generate table", commands)
        self.assertIn("semantic generate field", commands)
        self.assertNotIn("semantic generate", commands)
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

    def test_layer_group_colour_rule_uses_framework_class_semantics(self):
        group_colour = next(
            rule
            for rule in RULES
            if rule["id"] == "workspace.layer_group_colour"
        )
        self.assertIn("first grouped layer", group_colour["description"])
        self.assertIn("groupClassList", group_colour["description"])
        self.assertIn("same verified deployed class", group_colour["remediation"])
        self.assertIn("hex colour", group_colour["remediation"])

    def test_examples_publish_optional_legend_and_viewport_count_operations(self):
        advertised = examples()
        legend = advertised["showLayerLegend"]
        categorized = advertised["setCategorizedSymbology"]
        graduated = advertised["setGraduatedSymbology"]
        distributed = advertised["setDistributedSymbology"]
        viewport = advertised["countLayerInViewport"]
        viewport_heading = advertised["showViewportCountBesideLayer"]
        info_symbol = advertised["showSymbolInFeatureInformation"]
        self.assertEqual("basic", legend["operations"][0]["value"]["type"])
        self.assertEqual(["theme"], legend["operations"][1]["value"])
        self.assertEqual(
            "categorized",
            categorized["operations"][0]["value"]["type"],
        )
        self.assertEqual(
            "town",
            categorized["operations"][0]["value"]["field"],
        )
        self.assertEqual(
            ["theme"],
            categorized["operations"][1]["value"],
        )
        self.assertEqual(
            "less_than",
            graduated["operations"][0]["value"]["graduated_breaks"],
        )
        self.assertEqual(
            "distributed",
            distributed["operations"][0]["value"]["type"],
        )
        self.assertTrue(viewport["operations"][0]["value"]["viewport"])
        self.assertTrue(viewport["operations"][1]["value"])
        self.assertEqual(
            ["/instance/plugins/viewport-layer-count.mjs"],
            viewport_heading["operations"][0]["value"],
        )
        self.assertEqual({}, viewport_heading["operations"][1]["value"])
        self.assertTrue(viewport_heading["operations"][2]["value"])
        self.assertIsNone(info_symbol["operations"][0]["value"]["fillColor"])
        self.assertTrue(
            info_symbol["operations"][1]["value"]["styleFromLayerDefault"]
        )
        rule_ids = {rule["id"] for rule in RULES}
        self.assertIn("workspace.layer_legend", rule_ids)
        self.assertIn("workspace.categorized_symbology", rule_ids)
        self.assertIn("workspace.theme_semantics", rule_ids)
        self.assertIn("workspace.infoj_geometry_symbol", rule_ids)
        self.assertIn("workspace.viewport_count", rule_ids)
        self.assertIn("derived_layer.query_cost", rule_ids)
        self.assertIn("derived_layer.materialization_size", rule_ids)

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

    def test_workspace_map_extent_uses_zoom_minus_one_and_fixed_viewport(self):
        scope = workspace_map_extent({
            "locale": {
                "view": {"lng": -1.5491, "lat": 53.8008, "z": 11},
            },
        })

        self.assertEqual("workspace-map-extent", scope["type"])
        self.assertEqual("locale", scope["locale"])
        self.assertEqual(
            {"lng": -1.5491, "lat": 53.8008, "z": 11},
            scope["sourceView"],
        )
        self.assertEqual(10, scope["scopeZoom"])
        self.assertEqual(
            {"width": 1920, "height": 1080, "tileSize": 256},
            scope["viewport"],
        )
        self.assertEqual("EPSG:4326", scope["crs"])
        self.assertFalse(scope["clipsGeometry"])
        self.assertEqual(1, len(scope["envelopes"]))
        envelope = scope["envelopes"][0]
        self.assertLess(envelope["west"], -1.5491)
        self.assertGreater(envelope["east"], -1.5491)
        self.assertLess(envelope["south"], 53.8008)
        self.assertGreater(envelope["north"], 53.8008)

    def test_workspace_map_extent_prefers_configured_locale_extent(self):
        scope = workspace_map_extent({
            "locale": {
                "extent": {
                    "north": 54,
                    "east": -1.2,
                    "south": 53.65,
                    "west": -1.85,
                    "mask": True,
                },
                "view": {"lng": -1.5491, "lat": 53.8008, "z": 11},
            },
        })

        self.assertEqual(
            [{"west": -1.85, "south": 53.65, "east": -1.2, "north": 54.0}],
            scope["envelopes"],
        )
        self.assertIn("configured locale extent", scope["guidance"])

    def test_workspace_map_extent_splits_antimeridian_and_covers_world(self):
        wrapped = workspace_map_extent({
            "locale": {
                "view": {"lng": 179, "lat": 0, "z": 5},
            },
        })
        self.assertEqual(2, len(wrapped["envelopes"]))
        self.assertEqual(180, wrapped["envelopes"][0]["east"])
        self.assertEqual(-180, wrapped["envelopes"][1]["west"])

        whole_world = workspace_map_extent({
            "locale": {
                "view": {"lng": 180, "lat": 0, "z": 0},
            },
        })
        self.assertEqual(
            [{"west": -180.0, "south": whole_world["envelopes"][0]["south"],
              "east": 180.0, "north": whole_world["envelopes"][0]["north"]}],
            whole_world["envelopes"],
        )
        self.assertEqual(0, whole_world["zoomOffset"])

    def test_workspace_map_extent_clamps_poles_for_web_mercator(self):
        north = workspace_map_extent({
            "locale": {
                "view": {"lng": 0, "lat": 90, "z": 10.5},
            },
        })
        south = workspace_map_extent({
            "locale": {
                "view": {"lng": 0, "lat": -90, "z": 10.5},
            },
        })

        self.assertEqual(90, north["sourceView"]["lat"])
        self.assertLessEqual(north["envelopes"][0]["north"], 85.05112878)
        self.assertEqual(-90, south["sourceView"]["lat"])
        self.assertGreaterEqual(south["envelopes"][0]["south"], -85.05112878)
        self.assertEqual(9.5, north["scopeZoom"])

    def test_workspace_map_extent_requires_a_complete_finite_view(self):
        for workspace, message in (
            ({"locale": {}}, "needs view.lng"),
            (
                {"locale": {"view": {"lng": 0, "lat": 0}}},
                "view.z must be a finite number",
            ),
            (
                {"locale": {"view": {"lng": 0, "lat": 0, "z": True}}},
                "view.z must be a finite number",
            ),
        ):
            with self.subTest(workspace=workspace):
                with self.assertRaisesRegex(ValueError, message):
                    workspace_map_extent(workspace)

    def test_advanced_layers_use_workspace_view_without_database_probe(self):
        workspace = {
            "locale": {
                "layers": {
                    "External": {
                        "name": "Friendly external layer",
                        "format": "maplibre",
                        "style": {"URL": "https://tiles.example.invalid/style"},
                        "filter": {"default": {"status": {"match": "open"}}},
                        "featureLookup": [],
                        "qID": "id",
                        "group": "External data",
                    }
                }
            }
        }
        plan = visual_plan(workspace, "External", {})
        self.assertEqual("workspace-view", plan["source"])
        self.assertEqual("External", plan["layer"])
        self.assertEqual("Friendly external layer", plan["layerTitle"])
        self.assertNotIn("centre", plan)
        self.assertNotIn("zoom", plan)
        dataset = plan["effectiveDataset"]
        self.assertEqual(
            {"status": {"match": "open"}},
            dataset["effectiveFilter"]["fixedFilter"],
        )
        self.assertEqual(
            ["filter.default", "featureLookup"],
            dataset["effectiveFilter"]["restrictions"],
        )
        self.assertEqual("External data", dataset["activation"]["group"])
        self.assertTrue(dataset["query"]["skipped"])
        self.assertFalse(
            is_probeable_database_layer(
                {"template": "OSM"}
            )
        )

    def test_visual_plan_marks_the_active_named_hover_for_evidence(self):
        workspace = {
            "locale": {
                "layers": {
                    "External": {
                        "format": "maplibre",
                        "style": {
                            "URL": "https://tiles.example.invalid/style",
                            "hover": "name",
                            "hovers": {
                                "name": {
                                    "display": True,
                                    "field": "display_name",
                                    "title": "Place name",
                                },
                            },
                        },
                    },
                },
            },
        }

        plan = visual_plan(workspace, "External", {})

        self.assertEqual(
            {
                "type": "hover-centre-feature",
                "field": "display_name",
                "title": "Place name",
            },
            plan["hover"],
        )

    def test_visual_plan_uses_configured_visible_tile_key_as_background(self):
        workspace = {
            "locale": {
                "layers": {
                    "Open_Street_Map": {
                        "format": "tiles",
                        "display": True,
                        "URI": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                    }
                }
            }
        }

        plan = visual_plan(workspace, "Open_Street_Map", {})

        self.assertEqual(["Open_Street_Map"], plan["backgroundLayers"])

    @staticmethod
    def database_visual_workspace() -> dict:
        return {
            "dbs": "MAPP",
            "locale": {
                "layers": {
                    "Arrivals 1951-1960": {
                        "name": "Arrivals 1951-1960",
                        "format": "mvt",
                        "table": "derived_layers.arrivals_1951_1960_oa",
                        "geom": "geom_3857",
                        "qID": "oa21cd",
                        "srid": "3857",
                        "style": {
                            "hover": {
                                "display": True,
                                "field": "percentage",
                                "title": "Percentage of all usual residents",
                            },
                        },
                    },
                },
            },
        }

    def test_complete_explicit_visual_view_skips_database_auto_framing(self):
        with patch("control_api.psycopg.connect") as connect:
            plan = visual_plan(
                self.database_visual_workspace(),
                "Arrivals 1951-1960",
                {"MAPP": "postgresql://example.invalid/mapp"},
                visual_request={
                    "centre": [-1.532, 53.814],
                    "zoom": 14,
                },
            )

        connect.assert_not_called()
        self.assertEqual("explicit-view", plan["source"])
        self.assertEqual("browser-centre-feature", plan["baseSource"])
        self.assertEqual([-1.532, 53.814], plan["centre"])
        self.assertEqual(14, plan["zoom"])
        self.assertNotIn("featureCount", plan)
        self.assertNotIn("bounds3857", plan)
        self.assertNotIn("expectedFeatureId", plan["interaction"])
        self.assertEqual(
            "hover-centre-feature",
            plan["hover"]["type"],
        )

    def test_explicit_advanced_view_keeps_effective_filter_diagnostics(self):
        workspace = {"locale": {"layers": {"External": {
            "name": "External areas",
            "format": "maplibre",
            "filter": {"default": {"kind": {"match": "park"}}},
            "qID": "id",
        }}}}

        plan = visual_plan(
            workspace,
            "External",
            {},
            visual_request={"centre": [-1.5, 53.8], "zoom": 12},
        )

        dataset = plan["effectiveDataset"]
        self.assertEqual(
            {"kind": {"match": "park"}},
            dataset["effectiveFilter"]["fixedFilter"],
        )
        self.assertEqual("complete-explicit-view", dataset["query"]["reason"])
        self.assertIsNone(dataset["filteredFeatureCount"])

    @staticmethod
    def failing_visual_connection(error: psycopg.Error) -> MagicMock:
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.execute.side_effect = [None, None, error]
        connection.cursor.return_value = cursor
        return connection

    def test_auto_visual_plan_identifies_summary_timeout_stage(self):
        connection = self.failing_visual_connection(
            psycopg.errors.QueryCanceled(
                "secret relation and query text must not be exposed"
            )
        )
        with (
            patch("control_api.psycopg.connect", return_value=connection),
            self.assertRaises(VisualPlanningDatabaseError) as raised,
        ):
            visual_plan(
                self.database_visual_workspace(),
                "Arrivals 1951-1960",
                {"MAPP": "postgresql://example.invalid/mapp"},
            )

        error = raised.exception
        self.assertTrue(error.timed_out)
        self.assertEqual("layer-summary", error.stage)
        self.assertEqual("feature-count-and-extent", error.query_purpose)
        self.assertNotIn("secret relation", str(error))

    def test_auto_visual_plan_identifies_feature_selection_timeout_stage(self):
        summary_connection = MagicMock()
        summary_connection.__enter__.return_value = summary_connection
        summary_cursor = MagicMock()
        summary_cursor.__enter__.return_value = summary_cursor
        summary_cursor.fetchone.return_value = (
            178605,
            -200000,
            7000000,
            -150000,
            7100000,
            "ST_Polygon",
        )
        summary_connection.cursor.return_value = summary_cursor
        feature_connection = self.failing_visual_connection(
            psycopg.errors.QueryCanceled("private query detail")
        )

        with (
            patch(
                "control_api.psycopg.connect",
                side_effect=[summary_connection, feature_connection],
            ),
            self.assertRaises(VisualPlanningDatabaseError) as raised,
        ):
            visual_plan(
                self.database_visual_workspace(),
                "Arrivals 1951-1960",
                {"MAPP": "postgresql://example.invalid/mapp"},
            )

        error = raised.exception
        self.assertTrue(error.timed_out)
        self.assertEqual("representative-feature", error.stage)
        self.assertEqual("centre-feature-selection", error.query_purpose)
        self.assertNotIn("private query detail", str(error))

    def test_visual_plan_applies_default_filter_to_summary_and_feature(self):
        workspace = self.database_visual_workspace()
        workspace["locale"]["layers"]["Arrivals 1951-1960"]["filter"] = {
            "default": "ts005_0017_count > 0",
        }
        summary_connection = MagicMock()
        summary_connection.__enter__.return_value = summary_connection
        summary_cursor = MagicMock()
        summary_cursor.__enter__.return_value = summary_cursor
        summary_cursor.fetchone.return_value = (
            34, -200000, 7000000, -150000, 7100000, "ST_Polygon",
        )
        summary_connection.cursor.return_value = summary_cursor
        feature_connection = MagicMock()
        feature_connection.__enter__.return_value = feature_connection
        feature_cursor = MagicMock()
        feature_cursor.__enter__.return_value = feature_cursor
        feature_cursor.fetchone.return_value = (
            "891942c4313ffff", "ST_Polygon",
            -180000, 7050000, -179000, 7051000, -1.55, 53.8,
        )
        feature_connection.cursor.return_value = feature_cursor

        with patch(
            "control_api.psycopg.connect",
            side_effect=[summary_connection, feature_connection],
        ):
            plan = visual_plan(
                workspace,
                "Arrivals 1951-1960",
                {"MAPP": "postgresql://example.invalid/mapp"},
            )

        summary_query, summary_params = summary_cursor.execute.call_args_list[2].args
        feature_query, feature_params = feature_cursor.execute.call_args_list[2].args
        self.assertIn("ts005_0017_count > 0", summary_query.as_string(None))
        self.assertIn("ts005_0017_count > 0", feature_query.as_string(None))
        self.assertIn("NOT ST_IsEmpty", summary_query.as_string(None))
        self.assertIn("NOT ST_IsEmpty", feature_query.as_string(None))
        self.assertIn(
            'pg_catalog.to_jsonb("oa21cd") AS feature_id',
            feature_query.as_string(None),
        )
        self.assertEqual([], summary_params)
        self.assertEqual((-175000.0, 7050000.0), feature_params)
        self.assertEqual(34, plan["featureCount"])
        self.assertEqual("891942c4313ffff", plan["featureId"])
        self.assertTrue(plan["defaultFilterApplied"])
        self.assertEqual(
            "ts005_0017_count > 0",
            plan["effectiveDataset"]["effectiveFilter"]["fixedFilter"],
        )
        self.assertEqual(
            34, plan["effectiveDataset"]["filteredFeatureCount"]
        )
        self.assertEqual(
            "891942c4313ffff",
            plan["effectiveDataset"]["representativeFeature"]["id"],
        )

    def test_effective_layer_filter_uses_boolean_literals_and_feature_scope(self):
        layer = self.database_visual_workspace()["locale"]["layers"][
            "Arrivals 1951-1960"
        ]
        layer["filter"] = {"default": {"published": {"boolean": True}}}
        layer["featureSet"] = ["cell-1", "cell-2"]

        predicate, params, descriptor = effective_layer_filter(layer)

        rendered = predicate.as_string(None)
        self.assertIn('"published" IS TRUE', rendered)
        self.assertIn(
            'pg_catalog.to_jsonb("oa21cd")', rendered
        )
        self.assertEqual(['["cell-1","cell-2"]'], params)
        self.assertEqual(
            ["filter.default", "featureSet"], descriptor["restrictions"]
        )

    def test_effective_layer_filter_safely_encodes_mixed_json_feature_ids(self):
        layer = self.database_visual_workspace()["locale"]["layers"][
            "Arrivals 1951-1960"
        ]
        layer["featureSet"] = [1, "1", {"unexpected": "object"}]

        predicate, params, descriptor = effective_layer_filter(layer)

        self.assertIn("%s::jsonb @>", predicate.as_string(None))
        self.assertEqual(['[1,"1"]'], params)
        self.assertEqual(
            {
                "source": "featureSet",
                "field": "oa21cd",
                "configuredCount": 3,
                "comparablePrimitiveCount": 2,
                "ignoredCount": 1,
            },
            descriptor["identifierRestrictions"][0],
        )

    def test_effective_layer_filter_preserves_xyz_scalar_coercion(self):
        layer = self.database_visual_workspace()["locale"]["layers"][
            "Arrivals 1951-1960"
        ]
        layer["filter"] = {"default": {
            "text_code": {"eq": 2},
            "numeric_code": {"in": ["01", 2, 3.0]},
            "enabled": {"ni": [True, "false"]},
            "text_pattern": {"in": ["A%"]},
        }}

        predicate, params, descriptor = effective_layer_filter(layer)

        rendered = predicate.as_string(None)
        self.assertIn('"text_code" = %s', rendered)
        self.assertIn('"numeric_code" = %s', rendered)
        self.assertIn('NOT (("enabled" = %s', rendered)
        self.assertEqual(
            ["2", "01", "2", "3", "true", "false", "A%"], params
        )
        self.assertEqual(
            layer["filter"]["default"], descriptor["fixedFilter"]
        )

    def test_empty_feature_set_is_inactive_but_empty_lookup_matches_none(self):
        layer = self.database_visual_workspace()["locale"]["layers"][
            "Arrivals 1951-1960"
        ]
        layer["featureSet"] = []

        predicate, params, descriptor = effective_layer_filter(layer)

        self.assertEqual("TRUE", predicate.as_string(None))
        self.assertEqual([], params)
        self.assertEqual([], descriptor["identifierRestrictions"])

        layer["featureLookup"] = []
        predicate, params, descriptor = effective_layer_filter(layer)
        self.assertIn("%s::jsonb @>", predicate.as_string(None))
        self.assertEqual(["[]"], params)
        self.assertEqual(
            ["featureLookup"], descriptor["restrictions"]
        )

    def test_raw_fixed_filter_escapes_psycopg_percent_placeholders(self):
        layer = self.database_visual_workspace()["locale"]["layers"][
            "Arrivals 1951-1960"
        ]
        layer["filter"] = {
            "default": "population % 2 = 0 AND name LIKE 'A%'",
        }
        layer["featureSet"] = ["cell-1"]

        predicate, params, descriptor = effective_layer_filter(layer)

        rendered = predicate.as_string(None)
        self.assertIn("population %% 2 = 0", rendered)
        self.assertIn("name LIKE 'A%%'", rendered)
        self.assertEqual(['["cell-1"]'], params)
        self.assertEqual(
            "population % 2 = 0 AND name LIKE 'A%'",
            descriptor["fixedFilter"],
        )

    def test_effective_layer_filter_rejects_unreliable_field_level_or(self):
        layer = self.database_visual_workspace()["locale"]["layers"][
            "Arrivals 1951-1960"
        ]
        layer["filter"] = {
            "default": {"score": [{"gte": 1}, {"null": True}]},
        }

        with self.assertRaisesRegex(ValueError, "field-level OR arrays"):
            effective_layer_filter(layer)

        layer["filter"] = {"default": {"name": {"like": "%FF"}}}
        with self.assertRaisesRegex(ValueError, "UTF-8 URL encoding"):
            effective_layer_filter(layer)

        layer["filter"] = {"default": {"score": {"in": [[1], 2]}}}
        with self.assertRaisesRegex(ValueError, "finite scalars"):
            effective_layer_filter(layer)

        layer["filter"] = {"default": {"score": {"gte": "many"}}}
        with self.assertRaisesRegex(ValueError, "numeric string"):
            effective_layer_filter(layer)

    def test_visual_plan_reports_no_matching_filtered_features(self):
        workspace = self.database_visual_workspace()
        workspace["locale"]["layers"]["Arrivals 1951-1960"]["filter"] = {
            "default": "ts005_0017_count > 0",
        }
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (0, None, None, None, None, None)
        connection.cursor.return_value = cursor

        with (
            patch("control_api.psycopg.connect", return_value=connection),
            self.assertRaises(VisualPlanningNoMatchingFeatures) as raised,
        ):
            visual_plan(
                workspace,
                "Arrivals 1951-1960",
                {"MAPP": "postgresql://example.invalid/mapp"},
            )

        self.assertTrue(raised.exception.filter_applied)
        self.assertIn("filter.default", str(raised.exception))
        self.assertEqual(
            0,
            raised.exception.effective_dataset["filteredFeatureCount"],
        )
        self.assertIsNone(
            raised.exception.effective_dataset["representativeFeature"]
        )


class FederationGroupSchemaParityTests(unittest.TestCase):
    """The advertised schema must refuse what the validator refuses.

    A schema-driven client reads inputSchema to decide whether a request is
    worth sending. When the advertised shape is looser than the runtime
    validator, that client approves requests the server then refuses
    deterministically -- the API stays secure and the client stays wrong.
    """

    @staticmethod
    def rejected_by_validator(payload):
        try:
            validate_group_definition(payload)
        except FederationSchemaError:
            return True
        return False

    def test_the_name_grammar_and_bounds_are_advertised(self):
        schema = ACTION_SCHEMAS["federation.groups.define"]["inputSchema"]

        self.assertEqual(
            ALIAS_PATTERN, schema["properties"]["name"]["pattern"]
        )
        self.assertEqual(
            MAX_GROUP_DESCRIPTION,
            schema["properties"]["description"]["maxLength"],
        )
        # Each of these is refused at runtime, so each must be refusable from
        # the advertised schema alone.
        for payload in (
            {"name": "has-a-hyphen"},
            {"name": "1leading_digit"},
            {"name": "a" * 57},
            {"name": "leeds", "description": ""},
            {"name": "leeds", "description": "x" * 201},
        ):
            with self.subTest(payload=payload):
                self.assertTrue(self.rejected_by_validator(payload))

    def test_membership_bounds_and_uniqueness_are_advertised(self):
        schema = ACTION_SCHEMAS["federation.aliases.set-groups"]["inputSchema"]
        membership = schema["properties"]["groups"]

        self.assertEqual(ALIAS_PATTERN, membership["items"]["pattern"])
        self.assertEqual(MAX_GROUPS_PER_ALIAS, membership["maxItems"])
        self.assertTrue(membership["uniqueItems"])
        # An empty set is valid: it is how a source's labels are cleared.
        self.assertNotIn("minItems", membership)
