"""Draft lifecycle tests; optional PostgreSQL tests exercise actual DDL/rollback.

Set MAPP_DRAFT_TEST_DSN to a disposable PostgreSQL database whose name starts
with mapp_draft_test, or to the launcher's mapp_control_test scratch database.
These tests do not validate PostGIS or spatial planning.
"""
import os
import unittest
import uuid
from unittest.mock import MagicMock

import psycopg
from psycopg.conninfo import conninfo_to_dict

from derived_layers import (
    DerivedLayerDependencyError,
    DerivedLayerError,
    DerivedLayerStore,
    derived_layer_plan_fingerprint,
    validate_definition,
)


def payload(**overrides):
    return {
        "name": "preview_percent", "kind": "view",
        "query": "SELECT id, geom FROM public.draft_source",
        "sources": ["public.draft_source"], "idColumn": "id",
        "geometryColumn": "geom", **overrides,
    }


class DraftValidationTests(unittest.TestCase):
    def test_capability_distinguishes_cleanup_from_non_mutating_preview(self):
        planning = DerivedLayerStore.definition_planning_capability()
        self.assertFalse(planning["draftPreview"]["supported"])
        lifecycle = planning["draftLifecycle"]
        self.assertTrue(lifecycle["supported"])
        self.assertEqual("permanent", lifecycle["default"])
        self.assertTrue(lifecycle["requiresDatabaseCreation"])
        self.assertTrue(lifecycle["requiresCleanupApproval"])
        self.assertEqual(168, lifecycle["createFields"]["expiresInHours"]["maximum"])

    def test_closed_explicit_cleanup_consent_and_bounded_expiry(self):
        for invalid in (
            None, [], {}, {"expiresInHours": 24},
            {"expiresInHours": 24, "cleanupApproved": False},
            {"expiresInHours": 24, "cleanupApproved": 1},
            {"expiresInHours": True, "cleanupApproved": True},
            {"expiresInHours": 0, "cleanupApproved": True},
            {"expiresInHours": 169, "cleanupApproved": True},
            {"expiresInHours": 1.5, "cleanupApproved": True},
            {"expiresInHours": 24, "cleanupApproved": True, "owner": "guess"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(DerivedLayerError):
                validate_definition(payload(draft=invalid))
        for hours in (1, 24, 168):
            request = payload(draft={"expiresInHours": hours, "cleanupApproved": True})
            self.assertEqual(request["draft"], validate_definition(request)["draft"])

    def test_permanent_definition_shape_and_review_fingerprint(self):
        permanent = validate_definition(payload())
        self.assertNotIn("draft", permanent)
        draft = validate_definition(payload(draft={"expiresInHours": 24, "cleanupApproved": True}))
        self.assertNotEqual(
            derived_layer_plan_fingerprint(permanent, {}),
            derived_layer_plan_fingerprint(draft, {}),
        )
        other_expiry = {**draft, "draft": {"expiresInHours": 48, "cleanupApproved": True}}
        self.assertNotEqual(
            derived_layer_plan_fingerprint(draft, {}),
            derived_layer_plan_fingerprint(other_expiry, {}),
        )

    def test_replacement_cannot_introduce_draft_ownership(self):
        store = DerivedLayerStore("unused", "mapp_xyz")
        store._connect = MagicMock()
        with self.assertRaisesRegex(DerivedLayerError, "only.*creating"):
            store.replace("preview_percent", payload(
                draft={"expiresInHours": 24, "cleanupApproved": True},
            ), "creator")
        store._connect.assert_not_called()

    def test_cleanup_and_binding_require_exact_identity(self):
        store = DerivedLayerStore("unused", "mapp_xyz")
        store._connect = MagicMock()
        identity = {"name": "preview_percent", "assetId": str(uuid.uuid4()), "generation": 1}
        for invalid in (
            {}, {**identity, "assetId": "not-a-uuid"},
            {**identity, "generation": True}, {**identity, "generation": 0},
            {**identity, "name": "public.somewhere"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(DerivedLayerError):
                store.cleanup_draft(invalid)
        with self.assertRaises(DerivedLayerError):
            store.bind_drafts([identity, identity], "proposal", "creator")
        store._connect.assert_not_called()

    def test_publication_lookup_is_bounded_before_opening_database(self):
        store = DerivedLayerStore("unused", "mapp_xyz")
        store._connect = MagicMock()
        self.assertEqual([], store.drafts_for_names(set()))
        for invalid in ("preview_percent", None, ["public.invalid"], ["valid"] * 1001):
            with self.subTest(invalid=type(invalid)), self.assertRaises(DerivedLayerError):
                store.drafts_for_names(invalid)
        store._connect.assert_not_called()


@unittest.skipUnless(os.environ.get("MAPP_DRAFT_TEST_DSN"), "requires disposable PostgreSQL database")
class DraftPostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["MAPP_DRAFT_TEST_DSN"]
        database = conninfo_to_dict(self.dsn).get("dbname", "")
        if database != "mapp_control_test" and not database.startswith("mapp_draft_test"):
            self.fail("Draft integration tests require mapp_control_test or mapp_draft_test...")
        with psycopg.connect(self.dsn) as connection:
            connection.execute("DROP SCHEMA IF EXISTS derived_layers CASCADE")
            connection.execute("DROP VIEW IF EXISTS public.draft_consumer")
            connection.execute("DROP TABLE IF EXISTS public.draft_source")
            connection.execute("CREATE SCHEMA derived_layers")
            connection.execute("CREATE TABLE public.draft_source (id bigint, geom text)")
            connection.execute("INSERT INTO public.draft_source VALUES (1, 'geometry fixture')")
            role = connection.execute("SELECT current_user").fetchone()[0]
        self.store = DerivedLayerStore(self.dsn, role)
        # This suite isolates database lifecycle; the unchanged spatial guards
        # are covered by test_derived_layers and test_derived_query_guard.
        self.store._query_probe = MagicMock(return_value=({}, {}, {}, None))
        self.store._require_resolved_spatial_scope = MagicMock()
        self.store._validate_catalog_dependencies = MagicMock()
        self.store._validate_output = MagicMock(return_value={})
        self.store._semantic_fields = MagicMock(return_value=[])

    def create(self, *, name="preview_percent", actor="creator", draft=True):
        request = payload(name=name)
        if draft:
            request["draft"] = {"expiresInHours": 24, "cleanupApproved": True}
        return self.store.create(request, actor)

    def sql(self, statement, values=None):
        with psycopg.connect(self.dsn) as connection:
            cursor = connection.execute(statement, values)
            return cursor.fetchall() if cursor.description else None

    def test_migration_is_idempotent_and_never_enrolls_existing_relations(self):
        permanent = self.create(draft=False)
        draft = self.create(name="draft_relation")
        with self.store._connect() as connection, connection.cursor() as cur:
            self.store._initialize(cur)
            self.store._initialize(cur)
        self.assertNotIn("draft", self.store.get(permanent["name"]))
        self.assertEqual(draft["draft"], self.store.get("draft_relation")["draft"])
        self.assertEqual(1, len(self.store.list_drafts()))
        self.assertEqual("active", self.store.list_page(after_name=None, fetch_limit=10)[0]["draft"]["state"])

    def test_create_registers_draft_atomically_and_failed_create_leaves_nothing(self):
        original = self.store._enqueue_semantic_event
        self.store._enqueue_semantic_event = MagicMock(side_effect=RuntimeError("injected failure"))
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertEqual([], self.store.list_drafts())
        self.assertEqual([(None,)], self.sql("SELECT to_regclass('derived_layers.preview_percent')"))
        self.assertEqual([(0,)], self.sql("SELECT count(*) FROM derived_layers._definitions"))
        self.store._enqueue_semantic_event = original
        item = self.create()
        self.assertEqual(item["semanticProfile"]["assetId"], item["draft"]["assetId"])
        self.assertEqual("creator", item["draft"]["createdBy"])
        self.assertIsInstance(item["draft"]["expiresAt"], str)

    def test_bind_is_owned_atomic_and_rejects_expired_or_other_proposal(self):
        one, two = self.create()["draft"], self.create(name="other_draft")["draft"]
        with self.assertRaises(DerivedLayerError):
            self.store.bind_drafts([one], "proposal-one", "other-creator")
        with self.assertRaises(DerivedLayerError):
            self.store.bind_drafts([one, {**two, "generation": 2}], "proposal-one", "creator")
        self.assertIsNone(self.store.get(one["name"])["draft"]["proposalId"])
        self.store.bind_drafts([one], "proposal-one", "admin", allow_other_owner=True)
        with self.assertRaises(DerivedLayerError):
            self.store.bind_drafts([one], "proposal-two", "creator")
        self.sql("UPDATE derived_layers._drafts SET expires_at=clock_timestamp()-interval '1 hour' WHERE asset_id=%s", (two["assetId"],))
        with self.assertRaisesRegex(DerivedLayerError, "Expired"):
            self.store.bind_drafts([two], "proposal-two", "creator")

    def test_adoption_is_permanent_idempotent_and_checks_proposal(self):
        draft = self.create()["draft"]
        self.store.bind_drafts([draft], "proposal-one", "creator")
        with self.assertRaises(DerivedLayerError):
            self.store.adopt_drafts([draft], "proposal-two")
        first = self.store.adopt_drafts([draft], "proposal-one")
        self.assertEqual(first, self.store.adopt_drafts([draft], "proposal-one"))
        self.assertEqual("adopted", first[0]["state"])
        with self.assertRaisesRegex(DerivedLayerError, "permanent"):
            self.store.cleanup_draft(draft)
        self.assertEqual([], self.store.list_drafts())
        self.assertEqual("adopted", self.store.list_drafts(include_terminal=True)[0]["state"])

    def test_active_draft_cannot_refresh_or_replace(self):
        item = self.create()
        for operation in (
            lambda: self.store.refresh(item["name"]),
            lambda: self.store.preflight_refresh(item["name"]),
            lambda: self.store.replace(item["name"], payload(), "creator"),
        ):
            with self.assertRaisesRegex(DerivedLayerError, "Active draft"):
                operation()
        self.assertEqual(1, self.store.get(item["name"])["semanticProfile"]["generation"])

    def test_cleanup_drops_and_archives_atomically_with_durable_history(self):
        draft = self.create()["draft"]
        dropped = self.store.cleanup_draft(draft)
        self.assertEqual("dropped", dropped["state"])
        self.assertEqual(dropped, self.store.cleanup_draft(draft))
        self.assertEqual([], self.store.list())
        self.assertEqual([(None,)], self.sql("SELECT to_regclass('derived_layers.preview_percent')"))
        self.assertEqual([("register", 1), ("archive", 2)], self.sql(
            "SELECT event_type,generation FROM derived_layers._semantic_outbox ORDER BY generation",
        ))
        self.assertEqual([dropped], self.store.list_drafts(include_terminal=True))

    def test_cleanup_dependency_failure_retains_relation_and_no_archive(self):
        draft = self.create()["draft"]
        self.sql("CREATE VIEW public.draft_consumer AS SELECT * FROM derived_layers.preview_percent")
        with self.assertRaises(DerivedLayerDependencyError):
            self.store.cleanup_draft(draft)
        self.assertEqual("active", self.store.get(draft["name"])["draft"]["state"])
        self.assertEqual([("register",)], self.sql("SELECT event_type FROM derived_layers._semantic_outbox"))
        # Also exercise PostgreSQL's final DROP RESTRICT race protection rather
        # than relying solely on the earlier catalog dependency check.
        self.store._incoming_dependents = MagicMock(return_value=[])
        with self.assertRaises(DerivedLayerDependencyError):
            self.store.cleanup_draft(draft)
        self.assertEqual([("register",)], self.sql("SELECT event_type FROM derived_layers._semantic_outbox"))

    def test_cleanup_generation_drift_and_reused_name_fail_closed(self):
        draft = self.create()["draft"]
        self.sql("UPDATE derived_layers._definitions SET semantic_generation=2")
        with self.assertRaisesRegex(DerivedLayerError, "identity changed"):
            self.store.cleanup_draft(draft)
        self.sql("UPDATE derived_layers._definitions SET semantic_generation=1")
        self.store.drop(draft["name"], "creator")
        replacement = self.create(draft=False)
        self.assertNotEqual(draft["assetId"], replacement["semanticProfile"]["assetId"])
        self.assertEqual("dropped", self.store.cleanup_draft(draft)["state"])
        self.assertEqual(replacement["semanticProfile"], self.store.get(draft["name"])["semanticProfile"])

    def test_cleanup_records_round_robin_retry_without_changing_ownership(self):
        first = self.create()["draft"]
        second = self.create(name="other_draft")["draft"]
        self.store.record_draft_cleanup(first, "active-preview", "retry later")
        self.assertEqual(second["assetId"], self.store.list_drafts(limit=1)[0]["assetId"])
        recorded = self.store.get(first["name"])["draft"]
        self.assertEqual("retry later", recorded["lastCleanupError"])
        self.assertEqual("active", recorded["state"])
        self.assertIsNone(recorded["proposalId"])

    def test_publication_lookup_returns_only_active_exact_references(self):
        active = self.create()["draft"]
        adopted = self.create(name="adopted_relation")["draft"]
        self.store.adopt_drafts([adopted])
        dropped = self.create(name="dropped_relation")["draft"]
        self.store.drop(dropped["name"])
        self.create(name="dropped_relation", draft=False)
        self.create(name="permanent_relation", draft=False)
        self.assertEqual([active], self.store.drafts_for_names({
            active["name"], adopted["name"], dropped["name"], "permanent_relation", "absent_relation",
        }))

    def test_publication_lookup_blocks_missing_or_changed_draft_identity(self):
        draft = self.create()["draft"]
        self.sql("UPDATE derived_layers._definitions SET semantic_generation=2")
        with self.assertRaisesRegex(DerivedLayerError, "identity changed"):
            self.store.drafts_for_names([draft["name"]])
        self.sql("UPDATE derived_layers._definitions SET semantic_generation=1,semantic_asset_id=%s", (str(uuid.uuid4()),))
        with self.assertRaisesRegex(DerivedLayerError, "identity changed"):
            self.store.drafts_for_names([draft["name"]])
        self.sql("DELETE FROM derived_layers._definitions")
        with self.assertRaisesRegex(DerivedLayerError, "identity changed"):
            self.store.drafts_for_names([draft["name"]])


if __name__ == "__main__":
    unittest.main()
