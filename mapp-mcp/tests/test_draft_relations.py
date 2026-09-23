"""Draft lifecycle opt-in stays inside approval and exact-request binding."""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_approval_gate as approvals  # noqa: E402
import test_layers_tools as layer_tools  # noqa: E402
from test_approval_gate import (  # noqa: E402
    APPROVALS_CREATE, Answer, Capability, FakeContext,
)
from test_layers_tools import (  # noqa: E402
    AUTHORING, CHECK, CREATED, FakeConfigApi, FakeExchange, caller,
)
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from runtime import _proposal_summary, build_runtime  # noqa: E402


BINDINGS = [{
    "name": "new_h3",
    "assetId": "440c4b84-4c84-4452-acff-7f4cbb9ca1bd",
    "generation": 1,
}]


class DraftApprovalTests(unittest.IsolatedAsyncioTestCase):
    Api = approvals.DerivedLifecycleTests.Api
    ENTRY = approvals.DerivedLifecycleTests.ENTRY
    agreeing = approvals.DerivedLifecycleTests.agreeing

    async def test_slow_creation_keeps_the_event_loop_responsive(self):
        entered, release = threading.Event(), threading.Event()
        api = self.Api(self)
        original_post = api.post
        observed_callers = []

        def slow_post(**kwargs):
            if kwargs["path"] == "/api/derived-layers":
                observed_callers.append(approvals.CURRENT_CALLER.get())
                entered.set()
                release.wait(2)
            return original_post(**kwargs)

        api.post = slow_post
        create = self.build("derived_layers_create", api)
        expected_caller = approvals.CURRENT_CALLER.get()
        task = asyncio.create_task(create(
            self.agreeing(), "new_h3", "SELECT 1", ["source_census.oa"],
            "oa_id", "geom_3857",
        ))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            # Before the worker bridge this cannot run until the blocking
            # HTTP call has finished, including across unrelated sessions.
            self.assertFalse(task.done())
            self.assertEqual([expected_caller], observed_callers)
        finally:
            release.set()
            await task
        self.assertTrue(api.posted_to("/api/derived-layers")[0]["body"]["background"])

    async def test_background_admission_returns_polling_guidance(self):
        api = self.Api(self)
        original_post = api.post

        def post(**kwargs):
            if kwargs["path"] == "/api/derived-layers":
                return {"operation": {"id": "op", "status": "running"}}
            return original_post(**kwargs)

        api.post = post
        create = self.build("derived_layers_create", api)
        result = await create(self.agreeing(), "new_h3", "SELECT 1",
                              ["source_census.oa"], "oa_id", "geom_3857")
        self.assertEqual("operations_show", result["pollTool"])
        self.assertEqual(2, result["pollAfterSeconds"])

    async def test_explicit_synchronous_create_is_still_approval_bound(self):
        api = self.Api(self)
        create = self.build("derived_layers_create", api)
        await create(self.agreeing(), "new_h3", "SELECT 1",
                     ["source_census.oa"], "oa_id", "geom_3857", background=False)
        body = api.posted_to("/api/derived-layers")[0]["body"]
        self.assertIs(False, body["background"])
        self.assertEqual(body, self.exchange.digests[0]["body"])

    def build(self, name, api):
        self.exchange = approvals.FakeExchange()
        server = build_runtime(
            resource=approvals.ProtectedResource(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"),
            exchange=self.exchange, config_api=api,
        )
        token = approvals.CURRENT_CALLER.set(caller(
            scopes="mcp:connect inspect derive:manage semantic:inspect"))
        self.addCleanup(approvals.CURRENT_CALLER.reset, token)
        return server._tool_manager._tools[name].fn

    async def test_draft_plan_and_create_send_identical_retention(self):
        api = self.Api(self)
        plan = self.build("derived_layers_plan", api)
        create = self.build("derived_layers_create", api)
        args = ("new_h3", "SELECT 1", ["source_census.oa"], "oa_id", "geom_3857")
        plan(*args, draft_expires_in_hours=24)
        self.assertEqual([], api.posted_to(APPROVALS_CREATE["path_template"]))
        await create(self.agreeing(), *args, draft_expires_in_hours=24)
        planned = api.posted_to("/api/derived-layers/plan")[0]["body"]
        created = api.posted_to("/api/derived-layers")[0]["body"]
        self.assertEqual({**planned, "background": True}, created)
        self.assertEqual(
            {"expiresInHours": 24, "cleanupApproved": True}, created["draft"])
        self.assertEqual(created, self.exchange.digests[0]["body"])
        packet = api.packet()
        self.assertEqual(created["draft"], packet["definition"]["draft"])
        self.assertIn("Authorize automatic cleanup", packet["summary"])
        self.assertIn("declined", packet["summary"])
        self.assertIn("24 hours", packet["summary"])
        self.assertIn("Publication makes it permanent", packet["note"])
        self.assertIn("separate approval", packet["note"])

    async def test_nested_draft_plan_and_create_send_identical_retention(self):
        api = self.Api(self)
        plan = self.build("derived_layers_plan", api)
        create = self.build("derived_layers_create", api)
        args = ("new_h3", "SELECT 1", ["source_census.oa"], "oa_id", "geom_3857")
        draft = {"expiresInHours": 24, "cleanupApproved": True}
        plan(*args, draft=draft)
        await create(self.agreeing(), *args, draft=draft)
        planned = api.posted_to("/api/derived-layers/plan")[0]["body"]
        created = api.posted_to("/api/derived-layers")[0]["body"]
        self.assertEqual({**planned, "background": True}, created)
        self.assertEqual(draft, created["draft"])
        self.assertIn("24 hours", api.packet()["summary"])

    async def test_conflicting_and_invalid_nested_draft_is_refused(self):
        args = ("new_h3", "SELECT 1", ["source_census.oa"], "oa_id", "geom_3857")
        for draft in (
            {"expiresInHours": 24, "cleanupApproved": False},
            {"expiresInHours": 24, "cleanupApproved": True, "extra": 1},
            {"expiresInHours": 0, "cleanupApproved": True},
            {"expiresInHours": 24},
        ):
            with self.subTest(draft=draft):
                api = self.Api(self)
                plan = self.build("derived_layers_plan", api)
                with self.assertRaises(ToolError):
                    plan(*args, draft=draft)
                self.assertEqual([], api.calls)
        api = self.Api(self)
        plan = self.build("derived_layers_plan", api)
        with self.assertRaises(ToolError):
            plan(*args, draft={"expiresInHours": 24, "cleanupApproved": True},
                 draft_expires_in_hours=24)
        self.assertEqual([], api.calls)

    def test_derived_tool_schema_rejects_unknown_arguments(self):
        api = self.Api(self)
        self.build("derived_layers_plan", api)
        server = build_runtime(
            resource=approvals.ProtectedResource(
                origin="http://mcp.localhost", issuer="http://mcp.localhost"),
            exchange=approvals.FakeExchange(), config_api=api,
        )
        arguments = {
            "name": "new_h3", "query": "SELECT 1",
            "sources": ["source_census.oa"], "id_column": "oa_id",
            "geometry_column": "geom_3857",
        }
        for name in ("derived_layers_plan", "derived_layers_create"):
            with self.subTest(name=name):
                tool = server._tool_manager._tools[name]
                self.assertFalse(tool.parameters.get("additionalProperties", True))
                self.assertIn("draft", tool.parameters["properties"])
                with self.assertRaises(ValidationError):
                    tool.fn_metadata.arg_model.model_validate({
                        **arguments, "draft_expiry_hours": 24,
                    })

    async def test_declining_draft_creation_creates_nothing(self):
        api = self.Api(self)
        create = self.build("derived_layers_create", api)
        ctx = FakeContext(elicitation=Capability(form={}), form=Answer("decline"))
        with self.assertRaises(ToolError):
            await create(ctx, "new_h3", "SELECT 1", ["source_census.oa"],
                         "oa_id", "geom_3857", draft_expires_in_hours=24)
        self.assertEqual([], api.posted_to("/api/derived-layers"))

    async def test_invalid_retention_is_refused_before_approval_or_plan(self):
        for value in (0, 169, -1, True, 1.5, "24"):
            with self.subTest(value=value):
                api = self.Api(self)
                plan = self.build("derived_layers_plan", api)
                with self.assertRaises(ToolError):
                    plan("new_h3", "SELECT 1", ["source_census.oa"],
                         "oa_id", "geom_3857", draft_expires_in_hours=value)
                self.assertEqual([], api.calls)


class DraftBindingTests(layer_tools.ToolTestCase):
    def test_decline_is_bound_to_exact_proposal_and_does_not_claim_cleanup_completed(self):
        self.as_caller(caller(scopes=AUTHORING))
        payload = {"proposal": {"id": "p/1", "status": "declined",
                                "draftRelations": BINDINGS, "declineReason": "Do not use it."}}
        api, exchange = FakeConfigApi(answer=payload), FakeExchange()
        result = self.build_named("proposals_decline", config_api=api,
                                  exchange=exchange)("p/1", reason="Do not use it.")
        self.assertEqual("/api/proposals/p%2F1/decline", api.calls[0]["path"])
        self.assertEqual({"reason": "Do not use it."}, api.calls[0]["body"])
        self.assertEqual(api.calls[0]["body"], exchange.calls[0]["body"])
        self.assertEqual("propose", exchange.calls[0]["scope"])
        self.assertEqual("proposals.decline", exchange.calls[0]["operation_id"])
        self.assertEqual(1, len(api.calls))
        self.assertEqual("declined", result["status"])
        self.assertEqual(BINDINGS, result["draftRelations"])
        self.assertIn("eligible", result["cleanupNote"])

    def test_decline_requires_propose_scope_before_exchange(self):
        self.as_caller(caller(scopes="mcp:connect inspect"))
        exchange = FakeExchange()
        tool = self.build_named("proposals_decline", exchange=exchange)
        with self.assertRaises(ToolError):
            tool("p1")
        self.assertEqual([], exchange.calls)

    def test_decline_without_owned_drafts_has_no_cleanup_claim(self):
        self.as_caller(caller(scopes=AUTHORING))
        api = FakeConfigApi(answer={"proposal": {"status": "declined"}})
        result = self.build_named("proposals_decline", config_api=api)("p1")
        self.assertEqual({}, api.calls[0]["body"])
        self.assertNotIn("cleanupNote", result)

    def test_check_and_create_bind_the_same_identity_in_exchanged_request(self):
        self.as_caller(caller(scopes=AUTHORING))
        for name, payload, extras in (
            ("proposals_check", CHECK, {}),
            ("proposals_create", CREATED, {"check_fingerprint": "fp-7"}),
        ):
            with self.subTest(name=name):
                key = "check" if name.endswith("check") else "proposal"
                answer = {**payload, key: {**payload[key], "draftRelations": BINDINGS}}
                api, exchange = FakeConfigApi(answer=answer), FakeExchange()
                tool = self.build_named(name, config_api=api, exchange=exchange)
                result = tool(operations=[], revision="r", draft_relations=BINDINGS,
                              **extras)
                self.assertEqual(BINDINGS, api.calls[0]["body"]["draftRelations"])
                self.assertEqual(api.calls[0]["body"], exchange.calls[0]["body"])
                self.assertEqual(BINDINGS, result["draftRelations"])

    def test_legacy_request_does_not_opt_in(self):
        self.as_caller(caller(scopes=AUTHORING))
        api = FakeConfigApi(answer=CHECK)
        result = self.build_named("proposals_check", config_api=api)(
            operations=[], revision="r")
        self.assertNotIn("draftRelations", api.calls[0]["body"])
        self.assertNotIn("draftRelations", result)

    def test_draft_cleanup_is_visible_in_proposal_summaries(self):
        cleanup = {"status": "blocked", "reason": "active-preview"}
        result = _proposal_summary({"draftRelations": BINDINGS, "draftCleanup": cleanup})
        self.assertEqual(BINDINGS, result["draftRelations"])
        self.assertEqual(cleanup, result["draftCleanup"])

    def test_draft_status_read_only_costs_inspect_and_preserves_cleanup_evidence(self):
        self.as_caller(caller(scopes="mcp:connect inspect"))
        answer = {"drafts": [{**BINDINGS[0], "status": "blocked",
                              "cleanup": {"reason": "dependency"}}]}
        api, exchange = FakeConfigApi(answer=answer), FakeExchange()
        result = self.build_named("derived_layers_drafts", config_api=api,
                                  exchange=exchange)()
        self.assertEqual(answer, result)
        self.assertEqual("inspect", exchange.calls[0]["scope"])
        self.assertEqual("derived-layers.drafts", exchange.calls[0]["operation_id"])
        self.assertEqual("GET", api.calls[0]["method"])
        self.assertEqual("/api/derived-layers/drafts", api.calls[0]["path"])


if __name__ == "__main__":
    unittest.main()
