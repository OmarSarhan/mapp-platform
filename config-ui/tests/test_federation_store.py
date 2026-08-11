import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from federation_schema import FederationSchemaError
from federation_store import FederationAliasStore


def valid_registration(**overrides):
    value = {
        "alias": "leeds_ext",
        "displayName": "Leeds (external copy)",
        "kind": "postgresql",
        "connectionRef": "LEEDS_EXT",
        "tlsPolicy": "require",
        "allowedRelations": ["leeds.smoke_control_orders"],
        "dataHandlingClassification": "Public council open data.",
        "dataHandlingAcknowledged": True,
    }
    value.update(overrides)
    return value


def alias_row(**overrides):
    row = {
        "alias": "leeds_ext",
        "displayName": "Leeds (external copy)",
        "kind": "postgresql",
        "connectionRef": "LEEDS_EXT",
        "allowedRelations": ["leeds.smoke_control_orders"],
        "status": "pending",
        "freshnessStrategy": "manual",
        "dataHandlingClassification": "Public council open data.",
        "registeredBy": "admin",
        "registeredAt": "2026-08-11T00:00:00+00:00",
        "lastObservation": None,
        "provisionedAt": None,
    }
    row.update(overrides)
    return row


class FederationAliasStoreTests(unittest.TestCase):
    @staticmethod
    def store_with_cursor(cursor):
        store = FederationAliasStore("postgresql://database", "mapp_xyz")
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection.cursor.return_value = cursor_context
        store._connect = MagicMock(return_value=connection)
        store._initialize = MagicMock()
        return store

    def test_register_rejects_invalid_payload_before_touching_the_database(self):
        cursor = MagicMock()
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FederationSchemaError):
            store.register(valid_registration(tlsPolicy="not-a-policy"), "admin")
        cursor.execute.assert_not_called()

    def test_register_rejects_duplicate_alias(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"exists": True}
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FileExistsError):
            store.register(valid_registration(), "admin")

    def test_register_inserts_a_pending_row_and_returns_it(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [None, alias_row()]
        store = self.store_with_cursor(cursor)

        result = store.register(valid_registration(), "admin")

        self.assertEqual(alias_row(), result)
        insert = cursor.execute.call_args_list[1]
        self.assertIn("INSERT INTO", str(insert.args[0]))
        self.assertEqual(
            (
                "leeds_ext", "Leeds (external copy)", "postgresql", "LEEDS_EXT",
                ["leeds.smoke_control_orders"], "pending", "manual",
                "Public council open data.", "admin",
            ),
            insert.args[1],
        )

    def test_get_raises_not_found_when_missing(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FileNotFoundError):
            store.get("leeds_ext")

    def test_get_rejects_a_malformed_alias_name(self):
        cursor = MagicMock()
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FederationSchemaError):
            store.get("not a valid alias")

    def test_list_returns_every_row(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [alias_row(), alias_row(alias="census_ext")]
        store = self.store_with_cursor(cursor)

        result = store.list()

        self.assertEqual(2, len(result))
        self.assertEqual("census_ext", result[1]["alias"])

    def test_record_observation_marks_alias_active_when_reachable(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "leeds_ext"},
            alias_row(status="active"),
        ]
        store = self.store_with_cursor(cursor)
        observation = {
            "connectivity": "reachable",
            "schema": "current",
            "sourceFreshness": "unknown",
            "lastConnected": "2026-08-11T00:00:00+00:00",
            "lastSchemaVerified": "2026-08-11T00:00:00+00:00",
            "sourceVersion": None,
        }

        result = store.record_observation("leeds_ext", observation)

        self.assertEqual("active", result["status"])
        update = cursor.execute.call_args_list[0]
        self.assertEqual("active", update.args[1][1])

    def test_record_observation_marks_alias_unavailable_when_not_reachable(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "leeds_ext"},
            alias_row(status="unavailable"),
        ]
        store = self.store_with_cursor(cursor)
        observation = {
            "connectivity": "unavailable",
            "schema": "unknown",
            "sourceFreshness": "unknown",
            "lastConnected": None,
            "lastSchemaVerified": None,
            "sourceVersion": None,
        }

        result = store.record_observation("leeds_ext", observation)

        self.assertEqual("unavailable", result["status"])

    def test_record_observation_raises_not_found_when_alias_missing(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FileNotFoundError):
            store.record_observation("leeds_ext", {
                "connectivity": "unavailable",
                "schema": "unknown",
                "sourceFreshness": "unknown",
                "lastConnected": None,
                "lastSchemaVerified": None,
                "sourceVersion": None,
            })

    def test_provision_rejects_an_already_provisioned_alias(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = alias_row(
            provisionedAt="2026-08-11T00:00:00+00:00"
        )
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
            )

    def test_provision_issues_the_expected_fdw_ddl_and_marks_provisioned(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(provisionedAt=None),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        result = store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        self.assertIsNotNone(result["provisionedAt"])
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertTrue(any("CREATE EXTENSION IF NOT EXISTS postgres_fdw" in s for s in statements))
        self.assertTrue(any("CREATE SERVER" in s and "leeds_ext_srv" in s for s in statements))
        self.assertTrue(any("CREATE USER MAPPING" in s for s in statements))
        self.assertTrue(any("IMPORT FOREIGN SCHEMA" in s and "smoke_control_orders" in s for s in statements))
        self.assertTrue(any("GRANT SELECT ON ALL TABLES IN SCHEMA" in s and "mapp_xyz" in s for s in statements))

    def test_affected_derived_layer_names_queries_by_local_fdw_schema(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{"name": "smoke_control_area_h3_r9_ext"}]
        store = self.store_with_cursor(cursor)

        result = store.affected_derived_layer_names("leeds_ext")

        self.assertEqual(["smoke_control_area_h3_r9_ext"], result)
        query = cursor.execute.call_args_list[0]
        self.assertIn("derived_layers", str(query.args[0]))
        self.assertEqual(("source_leeds_ext",), query.args[1])


if __name__ == "__main__":
    unittest.main()
