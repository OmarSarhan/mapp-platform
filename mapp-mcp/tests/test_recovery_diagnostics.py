"""Recovery must distinguish a refused request from an unknown write outcome."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from config_api_client import ConfigApiRefused, ConfigApiUnavailable
from mcp.server.mcpserver.exceptions import ToolError
from test_layers_tools import FakeConfigApi, FakeExchange, ToolTestCase, caller


class RecoveryDiagnosticsTests(ToolTestCase):
    def test_exchange_time_consumes_the_operation_deadline(self):
        self.as_caller(caller())
        api = FakeConfigApi(answer={"derivedProfiles": []})
        fn = self.build_named("semantic_derived_profiles_list", config_api=api)
        ticks = iter((100.0, 105.0))
        with patch("runtime.time", SimpleNamespace(monotonic=lambda: next(ticks))):
            fn()
        self.assertEqual(20.0, api.calls[0]["timeout"])
        api.calls.clear()
        ticks = iter((100.0, 126.0))
        with patch("runtime.time", SimpleNamespace(monotonic=lambda: next(ticks))):
            with self.assertRaisesRegex(ToolError, "deadline expired"):
                fn()
        self.assertEqual([], api.calls)

    def test_transport_failure_does_not_invite_duplicate_writes(self):
        for name, kwargs, retryable in (
            ("semantic_derived_profiles_list", {}, True),
            ("proposals_create", {"operations": [], "revision": "rev-1",
                                  "check_fingerprint": "sha256:checked"}, False),
        ):
            with self.subTest(tool=name):
                self.as_caller(caller(scopes="mcp:connect inspect propose semantic:inspect"))
                api = FakeConfigApi(raises=ConfigApiUnavailable(
                    "private transport detail", reason="request_timeout",
                    request_id="a" * 32, timeout=25,
                ))
                fn = self.build_named(name, config_api=api)
                with self.assertRaises(ToolError) as caught:
                    fn(**kwargs)
                message = str(caught.exception)
                self.assertNotIn("private transport detail", message)
                detail = json.loads(message.split("Diagnostics: ")[1])
                self.assertEqual(retryable, detail["retryable"])
                self.assertEqual(not retryable, detail["indeterminate"])
                self.assertEqual("a" * 32, detail["requestId"])
                self.assertEqual("config_api.request_timeout", detail["code"])
                self.assertEqual(1, len(api.calls))

    def test_refusal_retains_replanning_and_rollback_facts_only(self):
        self.as_caller(caller())
        api = FakeConfigApi(raises=ConfigApiRefused(
            "Re-plan the definition.", status=409, code="derived_layer.plan_stale",
            request_id="b" * 32, body={
                "stateUnchanged": True, "rolledBack": True,
                "expectedPlanFingerprint": "sha256:old",
                "actualPlanFingerprint": "sha256:new",
                "technicalDetail": {"sqlstate": "57014", "query": "PRIVATE SQL"},
                "mutation": {"tokenB": "spent", "receipt": "consumed", "targetStarted": True,
                             "published": {"workspaceRevision": "c" * 64},
                             "recovery": {"approvalReference": "a" * 64, "operationId": "b" * 32,
                                          "proposalId": "public-id", "handle": "PRIVATE HANDLE"}},
                "query": "PRIVATE SQL",
            },
        ))
        fn = self.build_named("derived_layers_list", config_api=api)
        with self.assertRaises(ToolError) as caught:
            fn()
        message = str(caught.exception)
        detail = json.loads(message.split("Diagnostics: ")[1])
        self.assertTrue(detail["stateUnchanged"])
        self.assertTrue(detail["rolledBack"])
        self.assertEqual("sha256:new", detail["actualPlanFingerprint"])
        self.assertEqual("57014", detail["sqlstate"])
        self.assertEqual("c" * 64, detail["mutation"]["published"]["workspaceRevision"])
        self.assertEqual({"approvalReference": "a" * 64, "operationId": "b" * 32, "proposalId": "public-id"},
                         detail["mutation"]["recovery"])
        self.assertNotIn("PRIVATE HANDLE", message)
        self.assertNotIn("PRIVATE SQL", message)

    def test_poll_retains_failure_phase_and_draft_identity(self):
        self.as_caller(caller())
        draft = {"assetId": "asset-1", "generation": 2, "disposable": True}
        api = FakeConfigApi(answer={"operation": {
            "id": "op-1", "status": "failed",
            "diagnostics": {"databasePhase": "output-validation", "query": "PRIVATE SQL"},
            "error": {"code": "derived_layer.query_cancelled", "rolledBack": True,
                      "retryable": False, "technicalDetail": {"sqlstate": "57014"}},
            "result": {"derivedLayer": {"name": "draft_a", "draft": draft,
                                        "query": "PRIVATE SQL"}},
        }})
        detail = self.build_named("operations_show", config_api=api)(operation_id="op-1")
        self.assertEqual("output-validation", detail["databasePhase"])
        self.assertEqual("57014", detail["error"]["sqlstate"])
        self.assertTrue(detail["error"]["rolledBack"])
        self.assertFalse(detail["error"]["retryable"])
        self.assertEqual(draft, detail["result"]["derivedLayer"]["draft"])
        self.assertNotIn("PRIVATE SQL", json.dumps(detail))

    def test_profile_pagination_is_bound_to_the_exact_request(self):
        self.as_caller(caller())
        exchange = FakeExchange()
        api = FakeConfigApi(answer={"derivedProfiles": [], "pagination": {"nextCursor": "next"}})
        detail = self.build_named("semantic_derived_profiles_list", config_api=api,
                                  exchange=exchange)(limit=10, cursor="a+b/c=")
        self.assertEqual("next", detail["pagination"]["nextCursor"])
        self.assertEqual("limit=10&cursor=a%2Bb%2Fc%3D", api.calls[0]["query"])
        self.assertEqual(api.calls[0]["query"], exchange.calls[0]["query"])
        self.assertGreater(api.calls[0]["timeout"], 0)
        self.assertLessEqual(api.calls[0]["timeout"], 25)
