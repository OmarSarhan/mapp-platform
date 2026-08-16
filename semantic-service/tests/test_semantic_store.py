from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from semantic_store import SemanticError, SemanticStore  # noqa: E402


class SemanticStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary_directory.name) / "semantic.sqlite3"
        self.store = SemanticStore(self.db_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @contextmanager
    def hide_asset_after_first_connection(self, asset_id: str):
        original_connection = self.store._connection
        connection_count = 0

        @contextmanager
        def connection_with_visibility_change():
            nonlocal connection_count
            connection_count += 1
            current_connection = connection_count
            with original_connection() as connection:
                yield connection
            if current_connection == 1:
                with original_connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        connection.execute(
                            """
                            UPDATE assets
                            SET visibility = 'admin'
                            WHERE asset_id = ?
                            """,
                            (asset_id,),
                        )
                        connection.execute("COMMIT")
                    except Exception:
                        connection.execute("ROLLBACK")
                        raise

        with patch.object(
            self.store,
            "_connection",
            connection_with_visibility_change,
        ):
            yield

    def register(
        self,
        *,
        event_id: str = "event-1",
        asset_id: str = "asset:derived/roads",
        visibility: str | None = None,
        generated: dict | None = None,
        predecessor_asset_id: str | None = None,
    ) -> dict:
        event = {
            "eventId": event_id,
            "assetId": asset_id,
            "type": "register",
            "generation": 1,
            "generated": generated
            if generated is not None
            else {
                "kind": "managed-derived",
                "name": "roads",
                "binding": {"schema": "derived_layers", "relation": "roads"},
                "fields": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "label", "type": "text", "nullable": True},
                ],
            },
        }
        if visibility is not None:
            event["visibility"] = visibility
        if predecessor_asset_id is not None:
            event["predecessorAssetId"] = predecessor_asset_id
        return self.store.apply_event(event)

    def test_database_is_migrated_and_restrictive(self) -> None:
        settings = self.store.database_settings()
        self.assertEqual(settings["schemaVersion"], 5)
        self.assertEqual(settings["journalMode"].lower(), "wal")
        self.assertEqual(settings["foreignKeys"], 1)
        self.assertEqual(settings["synchronous"], 2)  # FULL
        self.assertEqual(settings["busyTimeout"], 5000)
        self.assertEqual(os.stat(self.db_path).st_mode & 0o777, 0o600)
        self.assertEqual(
            os.stat(self.db_path.parent).st_mode & 0o777,
            0o700,
        )

        reopened = SemanticStore(self.db_path)
        self.assertEqual(reopened.status()["catalogRevision"], 0)

    def test_v1_database_is_upgraded_without_losing_proposals(self) -> None:
        legacy_path = (
            Path(self.temporary_directory.name) / "semantic-v1.sqlite3"
        )
        connection = sqlite3.connect(legacy_path, isolation_level=None)
        try:
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            SemanticStore._migration_1(connection)
            connection.execute(
                """
                INSERT INTO assets(
                    asset_id, version, generation, status, visibility,
                    generated_json, curated_json, orphans_json,
                    catalog_revision, created_at, updated_at, archived_at
                ) VALUES(?, 1, 1, 'ready', 'inspect', '{}', '{}', '[]',
                         1, ?, ?, NULL)
                """,
                (
                    "asset:legacy",
                    "2026-07-25T12:00:00.000Z",
                    "2026-07-25T12:00:00.000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, state, asset_id, base_version,
                    operations_json, fingerprint, diff_json, explanation,
                    actor, reason, created_at, updated_at, applied_version
                ) VALUES(?, 'declined', ?, 1, '[]', ?, '[]', ?,
                         ?, 'Legacy decision', ?, ?, NULL)
                """,
                (
                    "proposal:legacy",
                    "asset:legacy",
                    "f" * 64,
                    "Preserve this proposal.",
                    "legacy-author",
                    "2026-07-25T12:00:00.000Z",
                    "2026-07-25T12:00:00.000Z",
                ),
            )
        finally:
            connection.close()

        upgraded = SemanticStore(legacy_path)
        self.assertEqual(upgraded.database_settings()["schemaVersion"], 5)
        proposal = upgraded.get_proposal(
            "proposal:legacy",
            is_admin=False,
        )
        self.assertEqual(proposal["state"], "declined")
        self.assertEqual(proposal["reason"], "Legacy decision")
        self.assertEqual(proposal["explanation"], "Preserve this proposal.")
        self.assertEqual(proposal["actor"], "legacy-author")
        self.assertIsNone(proposal["decidedBy"])
        self.assertIsNone(proposal["decidedAt"])
        self.assertIsNone(
            upgraded.get_asset("asset:legacy", is_admin=False)[
                "predecessorAssetId"
            ]
        )

        reopened = SemanticStore(legacy_path)
        self.assertEqual(reopened.database_settings()["schemaVersion"], 5)

    def test_generated_event_lifecycle_is_idempotent(self) -> None:
        first = self.register()
        self.assertFalse(first["event"]["idempotent"])
        self.assertEqual(first["asset"]["status"], "ready")
        self.assertEqual(first["asset"]["version"], 1)
        self.assertEqual(first["asset"]["generation"], 1)
        field_ids = [
            field["id"] for field in first["asset"]["generated"]["fields"]
        ]
        self.assertEqual(len(set(field_ids)), 2)

        replay = self.register()
        self.assertTrue(replay["event"]["idempotent"])
        self.assertEqual(replay["event"]["payloadHash"], first["event"]["payloadHash"])
        self.assertEqual(self.store.catalog_revision(), 1)

        changed = {
            "eventId": "event-1",
            "assetId": "asset:derived/roads",
            "type": "archive",
            "generation": 2,
        }
        with self.assertRaisesRegex(SemanticError, "eventId"):
            self.store.apply_event(changed)

        with self.assertRaises(SemanticError) as stale:
            self.store.apply_event(
                {
                    "eventId": "event-stale",
                    "assetId": "asset:derived/roads",
                    "type": "refresh",
                    "generation": 1,
                    "generated": first["asset"]["generated"],
                }
            )
        self.assertEqual(stale.exception.code, "stale_generation")

    def _federated(self, asset_id: str, schema: str, *, event_id: str) -> dict:
        return self.register(
            event_id=event_id,
            asset_id=asset_id,
            generated={
                "kind": "managed-derived",
                "name": "orders",
                "binding": {
                    "adapter": "postgresql",
                    "schema": schema,
                    "relation": "orders",
                },
                "fields": [{"name": "id", "type": "integer", "nullable": False}],
            },
        )

    def test_source_state_flags_and_clears_by_binding_schema(self) -> None:
        # The whole point is reversibility: an asset keeps its identity so the
        # same source returning can turn it back on.
        self._federated("asset:a", "source_leeds", event_id="e-a")
        self._federated("asset:b", "source_leeds", event_id="e-b")
        self._federated("asset:c", "source_other", event_id="e-c")

        flagged = self.store.mark_source_state("source_leeds", available=False)
        self.assertEqual(["asset:a", "asset:b"], sorted(flagged))
        by_id = {
            a["id"]: a for a in self.store.assets_for_source_schema("source_leeds")
        }
        self.assertEqual("unavailable", by_id["asset:a"]["sourceState"])
        # An unrelated schema is untouched.
        other = self.store.assets_for_source_schema("source_other")
        self.assertIsNone(other[0]["sourceState"])

        cleared = self.store.mark_source_state("source_leeds", available=True)
        self.assertEqual(["asset:a", "asset:b"], sorted(cleared))
        restored = self.store.assets_for_source_schema("source_leeds")
        self.assertTrue(all(a["sourceState"] is None for a in restored))

    def test_source_state_reports_only_what_it_changed(self) -> None:
        # So a caller can log once rather than on every verification pass.
        self._federated("asset:a", "source_leeds", event_id="e-a")
        self.assertEqual(
            ["asset:a"], self.store.mark_source_state("source_leeds", available=False)
        )
        self.assertEqual(
            [], self.store.mark_source_state("source_leeds", available=False)
        )
        self.assertEqual(
            ["asset:a"], self.store.mark_source_state("source_leeds", available=True)
        )
        self.assertEqual(
            [], self.store.mark_source_state("source_leeds", available=True)
        )

    def test_a_healthy_asset_reports_no_source_state(self) -> None:
        asset = self.register()["asset"]
        self.assertIsNone(asset["sourceState"])

    def test_event_idempotency_survives_service_restart(self) -> None:
        first = self.register()
        self.store = SemanticStore(self.db_path)
        replay = self.register()
        self.assertTrue(replay["event"]["idempotent"])
        self.assertEqual(replay["asset"], first["asset"])
        self.assertEqual(self.store.catalog_revision(), 1)

    def test_identity_strings_reject_whitespace_only_values(self) -> None:
        events = [
            ("eventId", {"eventId": " "}),
            ("assetId", {"assetId": "\t"}),
            ("actor", {"actor": "\n"}),
            (
                "generated.fields[0].name",
                {
                    "generated": {
                        "kind": "managed-derived",
                        "name": "roads",
                        "fields": [{"name": "\u2003", "type": "integer"}],
                    }
                },
            ),
        ]
        for name, overrides in events:
            with self.subTest(name=name):
                event = {
                    "eventId": f"event-{name}",
                    "assetId": f"asset:{name}",
                    "type": "register",
                    "generation": 1,
                    "generated": {
                        "kind": "managed-derived",
                        "name": "roads",
                    },
                    **overrides,
                }
                with self.assertRaises(SemanticError) as error:
                    self.store.apply_event(event)
                self.assertEqual(error.exception.code, "invalid_request")

        with self.assertRaises(SemanticError) as error:
            self.store.get_proposal(" ", is_admin=False)
        self.assertEqual(error.exception.code, "invalid_request")

        retained = self.register(event_id=" event-with-padding ")
        self.assertEqual(retained["event"]["eventId"], " event-with-padding ")

    def test_replace_preserves_exact_field_ids_and_orphans_annotations(self) -> None:
        registered = self.register()["asset"]
        id_field, label_field = registered["generated"]["fields"]
        curated = {
            "description": "Road labels",
            "fields": {
                id_field["id"]: {"description": "Stable feature identifier"},
                label_field["id"]: {"description": "Human-readable label"},
            },
        }
        check = self.store.check_proposal(
            {
                "assetId": registered["id"],
                "baseVersion": 1,
                "operations": [
                    {"op": "set", "path": "/curated", "value": curated}
                ],
            },
            is_admin=False,
        )
        proposal = self.store.create_proposal(
            {
                "assetId": registered["id"],
                "baseVersion": 1,
                "operations": check["operations"],
                "fingerprint": check["fingerprint"],
            },
            actor="editor",
            is_admin=False,
        )
        _, curated_asset, _ = self.store.apply_proposal(
            proposal["id"], actor="approver", is_admin=False
        )
        self.assertEqual(curated_asset["curated"], curated)

        replaced = self.store.apply_event(
            {
                "eventId": "event-2",
                "assetId": registered["id"],
                "type": "replace",
                "generation": 2,
                "generated": {
                    "kind": "managed-derived",
                    "name": "roads",
                    "binding": {
                        "schema": "derived_layers",
                        "relation": "roads",
                    },
                    "fields": [
                        {"name": "id", "type": "bigint"},
                        # A supplied id is never trusted for a new physical field.
                        {
                            "id": label_field["id"],
                            "name": "display_name",
                            "type": "text",
                        },
                    ],
                },
            }
        )["asset"]

        fields = replaced["generated"]["fields"]
        self.assertEqual(fields[0]["id"], id_field["id"])
        self.assertNotEqual(fields[1]["id"], label_field["id"])
        self.assertEqual(
            replaced["curated"]["fields"][id_field["id"]],
            {"description": "Stable feature identifier"},
        )
        self.assertNotIn(label_field["id"], replaced["curated"]["fields"])
        self.assertEqual(
            replaced["orphans"],
            [
                {
                    "fieldId": label_field["id"],
                    "name": "label",
                    "annotation": {"description": "Human-readable label"},
                    "removedAtGeneration": 2,
                }
            ],
        )

    def test_curated_fields_require_current_stable_ids_and_object_shape(self) -> None:
        registered = self.register()["asset"]
        valid_id = registered["generated"]["fields"][0]["id"]
        invalid_values = (
            {
                "fields": {
                    "field:not-present": {
                        "description": "Must not become active metadata"
                    }
                }
            },
            {"fields": []},
            {"fields": {valid_id: "not an annotation object"}},
        )
        for curated in invalid_values:
            with self.subTest(curated=curated):
                with self.assertRaises(SemanticError) as error:
                    self.store.check_proposal(
                        {
                            "assetId": registered["id"],
                            "baseVersion": registered["version"],
                            "operations": [{
                                "op": "set",
                                "path": "/curated",
                                "value": curated,
                            }],
                        },
                        is_admin=False,
                    )
                self.assertEqual(
                    "invalid_curated_fields",
                    error.exception.code,
                )

    def test_curated_field_annotations_are_bounded(self) -> None:
        registered = self.register()["asset"]
        field_id = registered["generated"]["fields"][0]["id"]
        with self.assertRaises(SemanticError) as error:
            self.store.check_proposal(
                {
                    "assetId": registered["id"],
                    "baseVersion": registered["version"],
                    "operations": [{
                        "op": "set",
                        "path": f"/curated/fields/{field_id}/description",
                        "value": "x" * (16 * 1024),
                    }],
                },
                is_admin=False,
            )
        self.assertEqual("invalid_curated_fields", error.exception.code)

    def test_valid_stable_field_annotation_can_be_checked_and_applied(self) -> None:
        registered = self.register()["asset"]
        field_id = registered["generated"]["fields"][0]["id"]
        request = {
            "assetId": registered["id"],
            "baseVersion": registered["version"],
            "operations": [{
                "op": "set",
                "path": f"/curated/fields/{field_id}/description",
                "value": "Stable identifier",
            }],
        }
        check = self.store.check_proposal(request, is_admin=False)
        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="author",
            is_admin=False,
        )
        _, updated, _ = self.store.apply_proposal(
            proposal["id"],
            actor="curator",
            is_admin=False,
        )
        self.assertEqual(
            "Stable identifier",
            updated["curated"]["fields"][field_id]["description"],
        )

    def test_register_from_archived_predecessor_carries_curated_profile(self) -> None:
        registered = self.register(visibility="admin")["asset"]
        id_field, label_field = registered["generated"]["fields"]
        curated = {
            "description": "Reviewed road data",
            "fields": {
                id_field["id"]: {"description": "Stable feature identifier"},
                label_field["id"]: {"description": "Human-readable label"},
            },
        }
        request = {
            "assetId": registered["id"],
            "baseVersion": registered["version"],
            "operations": [
                {"op": "set", "path": "/curated", "value": curated}
            ],
        }
        check = self.store.check_proposal(request, is_admin=True)
        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="editor",
            is_admin=True,
        )
        self.store.apply_proposal(
            proposal["id"],
            actor="approver",
            is_admin=True,
        )

        replaced = self.store.apply_event(
            {
                "eventId": "event-replace",
                "assetId": registered["id"],
                "type": "replace",
                "generation": 2,
                "generated": {
                    "kind": "managed-derived",
                    "name": "roads",
                    "binding": {
                        "relation": "roads",
                        "schema": "derived_layers",
                    },
                    "fields": [{"name": "id", "type": "bigint"}],
                },
            }
        )["asset"]
        archived = self.store.apply_event(
            {
                "eventId": "event-archive",
                "assetId": registered["id"],
                "type": "archive",
                "generation": 3,
            }
        )["asset"]
        successor_event = {
            "eventId": "event-successor",
            "assetId": "asset:derived/roads-reset",
            "type": "register",
            "generation": 1,
            "predecessorAssetId": archived["id"],
            "generated": {
                "kind": "managed-derived",
                "name": "roads",
                "binding": {
                    "schema": "derived_layers",
                    "relation": "roads",
                },
                "fields": [{"name": "id", "type": "bigint"}],
            },
        }
        successor_response = self.store.apply_event(successor_event)
        successor = successor_response["asset"]

        self.assertEqual(successor["predecessorAssetId"], archived["id"])
        self.assertEqual(successor["visibility"], "admin")
        self.assertEqual(successor["curated"], archived["curated"])
        self.assertEqual(successor["orphans"], archived["orphans"])
        self.assertEqual(
            successor["generated"]["fields"][0]["id"],
            replaced["generated"]["fields"][0]["id"],
        )
        self.assertEqual(
            archived["orphans"],
            [
                {
                    "fieldId": label_field["id"],
                    "name": "label",
                    "annotation": {"description": "Human-readable label"},
                    "removedAtGeneration": 2,
                }
            ],
        )
        history = self.store.asset_history(
            successor["id"],
            is_admin=True,
        )
        self.assertEqual(
            history[0]["asset"]["predecessorAssetId"],
            archived["id"],
        )

        replay = self.store.apply_event(successor_event)
        self.assertTrue(replay["event"]["idempotent"])
        self.assertEqual(replay["asset"], successor)

    def test_predecessor_is_register_only_and_must_be_a_string(self) -> None:
        registered = self.register()["asset"]
        invalid_events = [
            {
                "eventId": "event-refresh",
                "assetId": registered["id"],
                "type": "refresh",
                "generation": 2,
                "generated": registered["generated"],
                "predecessorAssetId": "asset:old",
            },
            {
                "eventId": "event-null-predecessor",
                "assetId": "asset:derived/successor",
                "type": "register",
                "generation": 1,
                "generated": registered["generated"],
                "predecessorAssetId": None,
            },
            {
                "eventId": "event-self-predecessor",
                "assetId": "asset:derived/self",
                "type": "register",
                "generation": 1,
                "generated": registered["generated"],
                "predecessorAssetId": "asset:derived/self",
            },
        ]
        for event in invalid_events:
            with self.subTest(event_id=event["eventId"]):
                with self.assertRaises(SemanticError) as error:
                    self.store.apply_event(event)
                self.assertEqual(error.exception.code, "invalid_request")

    def test_predecessor_must_exist_and_be_archived(self) -> None:
        registered = self.register()["asset"]
        generated = {
            "kind": "managed-derived",
            "name": "roads",
            "binding": {
                "schema": "derived_layers",
                "relation": "roads",
            },
            "fields": [{"name": "id", "type": "integer"}],
        }
        with self.assertRaises(SemanticError) as missing:
            self.register(
                event_id="event-missing-predecessor",
                asset_id="asset:derived/missing-successor",
                generated=generated,
                predecessor_asset_id="asset:derived/missing",
            )
        self.assertEqual(missing.exception.code, "predecessor_not_found")

        with self.assertRaises(SemanticError) as active:
            self.register(
                event_id="event-active-predecessor",
                asset_id="asset:derived/active-successor",
                generated=generated,
                predecessor_asset_id=registered["id"],
            )
        self.assertEqual(active.exception.code, "predecessor_not_archived")

    def test_predecessor_name_and_binding_must_match(self) -> None:
        registered = self.register()["asset"]
        archived = self.store.apply_event(
            {
                "eventId": "event-archive",
                "assetId": registered["id"],
                "type": "archive",
                "generation": 2,
            }
        )["asset"]
        mismatches = [
            (
                "event-other-binding",
                "asset:derived/other-binding",
                "roads",
                "rail",
            ),
            (
                "event-other-name",
                "asset:derived/other-name",
                "renamed-roads",
                "roads",
            ),
        ]
        for event_id, asset_id, name, relation in mismatches:
            with self.subTest(event_id=event_id):
                with self.assertRaises(SemanticError) as error:
                    self.register(
                        event_id=event_id,
                        asset_id=asset_id,
                        predecessor_asset_id=archived["id"],
                        generated={
                            "kind": "managed-derived",
                            "name": name,
                            "binding": {
                                "schema": "derived_layers",
                                "relation": relation,
                            },
                            "fields": [
                                {"name": "id", "type": "integer"}
                            ],
                        },
                    )
                self.assertEqual(
                    error.exception.code,
                    "predecessor_binding_mismatch",
                )

    def test_archive_is_a_tombstone_and_retains_final_snapshot(self) -> None:
        registered = self.register()["asset"]
        archived = self.store.apply_event(
            {
                "eventId": "event-archive",
                "assetId": registered["id"],
                "type": "archive",
                "generation": 2,
                "visibility": "inspect",
                "generated": {
                    **registered["generated"],
                    "eventAt": "2026-07-26T10:00:00Z",
                },
            }
        )["asset"]
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(archived["visibility"], "admin")
        self.assertIsNotNone(archived["archivedAt"])
        self.assertEqual(
            archived["generated"]["eventAt"], "2026-07-26T10:00:00Z"
        )
        self.assertEqual(
            self.store.list_assets(is_admin=False),
            [],
        )
        self.assertEqual(
            self.store.list_assets(is_admin=True),
            [],
        )
        self.assertEqual(
            self.store.search_assets("roads", limit=20, is_admin=True),
            [],
        )
        with self.assertRaises(SemanticError) as hidden:
            self.store.get_asset(archived["id"], is_admin=False)
        self.assertEqual(hidden.exception.status, 404)
        self.assertEqual(
            self.store.get_asset(archived["id"], is_admin=True)["status"],
            "archived",
        )
        self.assertEqual(
            len(self.store.asset_history(archived["id"], is_admin=True)),
            2,
        )

        with self.assertRaises(SemanticError) as error:
            self.store.check_proposal(
                {
                    "assetId": archived["id"],
                    "baseVersion": archived["version"],
                    "operations": [
                        {
                            "op": "set",
                            "path": "/curated/description",
                            "value": "No",
                        }
                    ],
                },
                is_admin=True,
            )
        self.assertEqual(error.exception.code, "asset_archived")

    def test_legacy_archived_inspect_asset_is_hidden_but_admin_auditable(
        self,
    ) -> None:
        archived = self.store.apply_event(
            {
                "eventId": "event-archive",
                "assetId": self.register()["asset"]["id"],
                "type": "archive",
                "generation": 2,
            }
        )["asset"]
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE assets SET visibility = 'inspect' WHERE asset_id = ?",
                (archived["id"],),
            )

        self.assertEqual(self.store.list_assets(is_admin=False), [])
        self.assertEqual(self.store.list_assets(is_admin=True), [])
        with self.assertRaises(SemanticError) as hidden:
            self.store.get_asset(archived["id"], is_admin=False)
        self.assertEqual(hidden.exception.status, 404)
        self.assertEqual(
            self.store.get_asset(archived["id"], is_admin=True)["id"],
            archived["id"],
        )
        self.assertEqual(
            len(self.store.asset_history(archived["id"], is_admin=True)),
            2,
        )

    def test_admin_visibility_is_filtered_without_disclosing_identity(self) -> None:
        public = self.register(asset_id="asset:public")["asset"]
        hidden = self.register(
            event_id="event-hidden",
            asset_id="asset:hidden",
            visibility="admin",
        )["asset"]
        self.assertEqual(
            [asset["id"] for asset in self.store.list_assets(is_admin=False)],
            [public["id"]],
        )
        self.assertEqual(len(self.store.list_assets(is_admin=True)), 2)
        with self.assertRaises(SemanticError) as error:
            self.store.get_asset(hidden["id"], is_admin=False)
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(
            self.store.search_assets("hidden", limit=20, is_admin=False), []
        )

    def test_proposal_fingerprint_and_per_asset_concurrency(self) -> None:
        asset = self.register()["asset"]
        request = {
            "assetId": asset["id"],
            "baseVersion": asset["version"],
            "explanation": "Document this derived layer for operators.",
            "operations": [
                {
                    "op": "set",
                    "path": "/curated/description",
                    "value": "A governed road layer",
                },
                {
                    "op": "set",
                    "path": "/curated/tags",
                    "value": ["transport"],
                },
            ],
        }
        check = self.store.check_proposal(request, is_admin=False)
        self.assertEqual(check["diff"][0]["before"], {"exists": False})
        self.assertEqual(
            check["diff"][0]["after"],
            {"exists": True, "value": "A governed road layer"},
        )
        with self.assertRaises(SemanticError) as mismatch:
            self.store.create_proposal(
                {**request, "fingerprint": "0" * 64},
                actor="author",
                is_admin=False,
            )
        self.assertEqual(mismatch.exception.code, "fingerprint_mismatch")

        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="author",
            is_admin=False,
        )
        self.assertIsNone(proposal["decidedBy"])
        self.assertIsNone(proposal["decidedAt"])
        applied, updated, revision = self.store.apply_proposal(
            proposal["id"], actor="approver", is_admin=False
        )
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["decidedBy"], "approver")
        self.assertEqual(applied["decidedAt"], applied["updatedAt"])
        self.assertEqual(
            applied["explanation"],
            "Document this derived layer for operators.",
        )
        self.assertEqual(applied["operations"], request["operations"])
        self.assertEqual(applied["diff"], check["diff"])
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["catalogRevision"], revision)
        self.assertEqual(
            updated["curated"],
            {
                "description": "A governed road layer",
                "tags": ["transport"],
            },
        )

        with self.assertRaises(SemanticError) as stale:
            self.store.check_proposal(request, is_admin=False)
        self.assertEqual(stale.exception.code, "revision_conflict")

    def test_apply_response_is_pinned_to_its_write_transaction(self) -> None:
        asset = self.register()["asset"]
        request = {
            "assetId": asset["id"],
            "baseVersion": asset["version"],
            "operations": [
                {
                    "op": "set",
                    "path": "/curated/description",
                    "value": "Reviewed roads",
                }
            ],
        }
        check = self.store.check_proposal(request, is_admin=False)
        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="author",
            is_admin=False,
        )
        original_connection = self.store._connection
        connection_count = 0

        @contextmanager
        def connection_with_post_commit_refresh():
            nonlocal connection_count
            connection_count += 1
            current_connection = connection_count
            with original_connection() as connection:
                yield connection
            if current_connection == 2:
                self.store.apply_event(
                    {
                        "eventId": "event-after-apply-commit",
                        "assetId": asset["id"],
                        "type": "refresh",
                        "generation": 2,
                        "generated": asset["generated"],
                    }
                )

        with patch.object(
            self.store,
            "_connection",
            connection_with_post_commit_refresh,
        ):
            applied, returned_asset, revision = self.store.apply_proposal(
                proposal["id"],
                actor="approver",
                is_admin=False,
            )

        self.assertEqual(applied["appliedVersion"], 2)
        self.assertEqual(returned_asset["version"], 2)
        self.assertEqual(returned_asset["catalogRevision"], revision)
        self.assertEqual(revision, 2)
        self.assertEqual(
            self.store.get_asset(asset["id"], is_admin=False)["version"],
            3,
        )

    def test_create_rechecks_visibility_inside_its_write_transaction(self) -> None:
        asset = self.register()["asset"]
        request = {
            "assetId": asset["id"],
            "baseVersion": asset["version"],
            "operations": [{
                "op": "set",
                "path": "/curated/description",
                "value": "Draft",
            }],
        }
        check = self.store.check_proposal(request, is_admin=False)

        with self.hide_asset_after_first_connection(asset["id"]):
            with self.assertRaises(SemanticError) as error:
                self.store.create_proposal(
                    {**request, "fingerprint": check["fingerprint"]},
                    actor="author",
                    is_admin=False,
                )

        self.assertEqual("asset_not_found", error.exception.code)
        self.assertEqual(
            [],
            self.store.list_proposals(
                state=None,
                asset_id=asset["id"],
                is_admin=True,
            ),
        )

    def test_apply_rechecks_visibility_inside_its_write_transaction(self) -> None:
        asset = self.register()["asset"]
        request = {
            "assetId": asset["id"],
            "baseVersion": asset["version"],
            "operations": [{
                "op": "set",
                "path": "/curated/description",
                "value": "Reviewed",
            }],
        }
        check = self.store.check_proposal(request, is_admin=False)
        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="author",
            is_admin=False,
        )

        with self.hide_asset_after_first_connection(asset["id"]):
            with self.assertRaises(SemanticError) as error:
                self.store.apply_proposal(
                    proposal["id"],
                    actor="approver",
                    is_admin=False,
                )

        self.assertEqual("proposal_not_found", error.exception.code)
        self.assertEqual(
            "pending",
            self.store.get_proposal(
                proposal["id"],
                is_admin=True,
            )["state"],
        )
        self.assertEqual(
            1,
            self.store.get_asset(asset["id"], is_admin=True)["version"],
        )

    def test_decline_rechecks_visibility_inside_its_write_transaction(self) -> None:
        asset = self.register()["asset"]
        request = {
            "assetId": asset["id"],
            "baseVersion": asset["version"],
            "operations": [{
                "op": "set",
                "path": "/curated/description",
                "value": "Draft",
            }],
        }
        check = self.store.check_proposal(request, is_admin=False)
        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="author",
            is_admin=False,
        )

        with self.hide_asset_after_first_connection(asset["id"]):
            with self.assertRaises(SemanticError) as error:
                self.store.decline_proposal(
                    proposal["id"],
                    actor="reviewer",
                    reason="Superseded",
                    is_admin=False,
                )

        self.assertEqual("proposal_not_found", error.exception.code)
        self.assertEqual(
            "pending",
            self.store.get_proposal(
                proposal["id"],
                is_admin=True,
            )["state"],
        )

    def test_decline_records_decision_actor_and_time(self) -> None:
        asset = self.register()["asset"]
        request = {
            "assetId": asset["id"],
            "baseVersion": asset["version"],
            "operations": [
                {
                    "op": "set",
                    "path": "/curated/description",
                    "value": "Draft",
                }
            ],
        }
        check = self.store.check_proposal(request, is_admin=False)
        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="author",
            is_admin=False,
        )

        declined = self.store.decline_proposal(
            proposal["id"],
            actor="reviewer",
            reason="Superseded",
            is_admin=False,
        )

        self.assertEqual(declined["state"], "declined")
        self.assertEqual(declined["actor"], "author")
        self.assertEqual(declined["reason"], "Superseded")
        self.assertEqual(declined["decidedBy"], "reviewer")
        self.assertEqual(declined["decidedAt"], declined["updatedAt"])

    def test_proposal_is_invalidated_by_generated_asset_update(self) -> None:
        asset = self.register()["asset"]
        request = {
            "assetId": asset["id"],
            "baseVersion": 1,
            "operations": [
                {"op": "set", "path": "/curated/description", "value": "Draft"}
            ],
        }
        check = self.store.check_proposal(request, is_admin=False)
        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="author",
            is_admin=False,
        )
        self.store.apply_event(
            {
                "eventId": "refresh-2",
                "assetId": asset["id"],
                "type": "refresh",
                "generation": 2,
                "generated": asset["generated"],
            }
        )
        with self.assertRaises(SemanticError) as error:
            self.store.apply_proposal(
                proposal["id"], actor="approver", is_admin=False
            )
        self.assertEqual(error.exception.code, "revision_conflict")

    def test_unrelated_asset_update_does_not_invalidate_proposal(self) -> None:
        asset = self.register()["asset"]
        other = self.register(
            event_id="event-other",
            asset_id="asset:other",
        )["asset"]
        request = {
            "assetId": asset["id"],
            "baseVersion": asset["version"],
            "operations": [
                {"op": "set", "path": "/curated/description", "value": "Still valid"}
            ],
        }
        check = self.store.check_proposal(request, is_admin=False)
        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="author",
            is_admin=False,
        )
        self.store.apply_event(
            {
                "eventId": "event-other-refresh",
                "assetId": other["id"],
                "type": "refresh",
                "generation": 2,
                "generated": other["generated"],
            }
        )
        _, updated, _ = self.store.apply_proposal(
            proposal["id"], actor="approver", is_admin=False
        )
        self.assertEqual(updated["curated"]["description"], "Still valid")

    def test_only_curated_object_paths_are_accepted(self) -> None:
        asset = self.register()["asset"]
        invalid_operations = [
            {"op": "set", "path": "/generated/name", "value": "hijack"},
            {"op": "unset", "path": "/curated"},
            {"op": "set", "path": "/curated", "value": []},
            {"op": "unset", "path": "/curated/missing"},
        ]
        for operation in invalid_operations:
            with self.subTest(operation=operation):
                with self.assertRaises(SemanticError):
                    self.store.check_proposal(
                        {
                            "assetId": asset["id"],
                            "baseVersion": 1,
                            "operations": [operation],
                        },
                        is_admin=False,
                    )

    def test_derived_profile_aliases_use_binding_or_kind(self) -> None:
        asset = self.register()["asset"]
        self.store.apply_event(
            {
                "eventId": "ordinary",
                "assetId": "asset:ordinary",
                "type": "register",
                "generation": 1,
                "generated": {"kind": "external", "name": "ordinary"},
            }
        )
        profiles = self.store.derived_profiles(is_admin=False)
        self.assertEqual([profile["id"] for profile in profiles], [asset["id"]])
        self.assertEqual(
            self.store.get_derived_profile("roads", is_admin=False)["id"],
            asset["id"],
        )

    def test_growing_collections_use_bounded_keyset_fetches(self) -> None:
        assets = []
        for index, asset_id in enumerate(("asset:a", "asset:b", "asset:c")):
            assets.append(self.register(
                event_id=f"event-page-{index}",
                asset_id=asset_id,
                generated={
                    "kind": "managed-derived",
                    "name": f"roads_{index}",
                    "description": "Straße" if index == 1 else "Roads",
                    "binding": {
                        "schema": "derived_layers",
                        "relation": f"roads_{index}",
                    },
                },
            )["asset"])
        self.register(
            event_id="event-page-hidden",
            asset_id="asset:aa-hidden",
            visibility="admin",
        )

        first_assets = self.store.list_assets(
            is_admin=False,
            fetch_limit=2,
        )
        second_assets = self.store.list_assets(
            is_admin=False,
            after_asset_id=first_assets[-1]["id"],
            fetch_limit=2,
        )
        self.assertEqual(
            [item["id"] for item in first_assets + second_assets],
            ["asset:a", "asset:b", "asset:c"],
        )
        self.assertEqual(
            [item["id"] for item in self.store.search_assets(
                "STRASSE",
                limit=None,
                is_admin=False,
                fetch_limit=2,
            )],
            ["asset:b"],
        )

        first_profiles = self.store.derived_profiles(
            is_admin=False,
            fetch_limit=2,
        )
        second_profiles = self.store.derived_profiles(
            is_admin=False,
            after_asset_id=first_profiles[-1]["id"],
            fetch_limit=2,
        )
        self.assertEqual(
            [item["id"] for item in first_profiles + second_profiles],
            ["asset:a", "asset:b", "asset:c"],
        )

        current = assets[0]
        for generation in (2, 3):
            current = self.store.apply_event({
                "eventId": f"event-history-{generation}",
                "assetId": current["id"],
                "type": "refresh",
                "generation": generation,
                "generated": current["generated"],
            })["asset"]
        first_history = self.store.asset_history(
            current["id"],
            is_admin=False,
            fetch_limit=2,
        )
        second_history = self.store.asset_history(
            current["id"],
            is_admin=False,
            after_history_id=first_history[-1]["_historyId"],
            fetch_limit=2,
        )
        self.assertEqual(len(first_history + second_history), 3)
        self.assertNotIn(
            "_historyId",
            self.store.asset_history(current["id"], is_admin=False)[0],
        )

        request = {
            "assetId": current["id"],
            "baseVersion": current["version"],
            "operations": [{
                "op": "set",
                "path": "/curated/description",
                "value": "Keyset proposal",
            }],
        }
        checked = self.store.check_proposal(request, is_admin=False)
        for index in range(3):
            self.store.create_proposal(
                {**request, "fingerprint": checked["fingerprint"]},
                actor=f"author-{index}",
                is_admin=False,
            )
        all_proposals = self.store.list_proposals(
            state=None,
            asset_id=current["id"],
            is_admin=False,
        )
        first_proposals = self.store.list_proposals(
            state=None,
            asset_id=current["id"],
            is_admin=False,
            fetch_limit=2,
        )
        boundary = first_proposals[-1]
        second_proposals = self.store.list_proposals(
            state=None,
            asset_id=current["id"],
            is_admin=False,
            after=(boundary["createdAt"], boundary["id"]),
            fetch_limit=2,
        )
        self.assertEqual(
            [item["id"] for item in first_proposals + second_proposals],
            [item["id"] for item in all_proposals],
        )


if __name__ == "__main__":
    unittest.main()
