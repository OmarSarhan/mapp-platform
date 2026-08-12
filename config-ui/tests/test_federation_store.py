import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from federation_schema import FederationSchemaError
from federation_store import FederationAliasStore

MATCHING_VERSIONS = {"postgis": "3.5.7", "proj": "9.8.1", "geos": "3.14.1"}
DIFFERENT_VERSIONS = {"postgis": "3.0.0", "proj": "8.0.0", "geos": "3.9.0"}


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

    @patch("federation_store.extension_versions")
    def test_record_observation_marks_alias_active_when_reachable(self, mock_versions):
        # Status only ever reflects connectivity once the alias has passed
        # Approve exposure (provision()) — this fixture is already
        # provisioned, matching that.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "leeds_ext", "provisioned_at": "2026-08-11T00:00:00+00:00"},
            {"srvoptions": None},
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
        self.assertEqual(True, update.args[1][1])

    @patch("federation_store.extension_versions")
    def test_record_observation_marks_alias_unavailable_when_not_reachable(self, mock_versions):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "leeds_ext", "provisioned_at": "2026-08-11T00:00:00+00:00"},
            {"srvoptions": None},
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

    def test_record_observation_leaves_status_pending_before_provisioning(self):
        # Discover (observe) is documented to run before Approve exposure
        # (provision) in the ordinary lifecycle — a reachable, unprovisioned
        # alias is evidence, not proof of usability, so status must not
        # jump to "active" just because it's reachable.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "leeds_ext", "provisioned_at": None},
            alias_row(status="pending"),
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

        self.assertEqual("pending", result["status"])
        update = cursor.execute.call_args_list[0]
        self.assertIn("WHEN provisioned_at IS NULL THEN status", str(update.args[0]))

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

    @patch("federation_store.extension_versions")
    def test_record_observation_auto_disables_pushdown_on_version_drift(self, mock_versions):
        # Fail-safe direction: once provisioned, a drift away from a
        # version match must disable pushdown immediately at observe
        # time — re-enabling it is a deliberate reprovisioning action.
        mock_versions.return_value = DIFFERENT_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "leeds_ext", "provisioned_at": "2026-08-11T00:00:00+00:00"},
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)
        observation = {
            "connectivity": "reachable",
            "schema": "current",
            "sourceFreshness": "unknown",
            "lastConnected": "2026-08-11T00:00:00+00:00",
            "lastSchemaVerified": "2026-08-11T00:00:00+00:00",
            "sourceVersion": None,
            "extensionVersions": MATCHING_VERSIONS,
        }

        store.record_observation("leeds_ext", observation)

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertTrue(any("ALTER SERVER" in s and "DROP extensions" in s for s in statements))
        self.assertFalse(any("ADD extensions" in s for s in statements))

    @patch("federation_store.extension_versions")
    def test_record_observation_does_not_auto_enable_pushdown(self, mock_versions):
        # The doc requires re-enabling to go through an explicit
        # reprovisioning call, never automatically from an observation —
        # even one that now shows matching versions.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "leeds_ext", "provisioned_at": "2026-08-11T00:00:00+00:00"},
            {"srvoptions": ["host=source-db"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)
        observation = {
            "connectivity": "reachable",
            "schema": "current",
            "sourceFreshness": "unknown",
            "lastConnected": "2026-08-11T00:00:00+00:00",
            "lastSchemaVerified": "2026-08-11T00:00:00+00:00",
            "sourceVersion": None,
            "extensionVersions": MATCHING_VERSIONS,
        }

        store.record_observation("leeds_ext", observation)

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("ALTER SERVER" in s for s in statements))

    def test_provision_rejects_a_missing_or_stale_observation(self):
        # IMPORT FOREIGN SCHEMA acts on whatever the remote looks like
        # right now — provisioning against evidence that was never taken,
        # or that already showed the schema had changed, could silently
        # produce an incomplete alias.
        for last_observation in (None, {"schema": "changed"}, {}):
            with self.subTest(last_observation=last_observation):
                cursor = MagicMock()
                cursor.fetchone.return_value = alias_row(
                    provisionedAt=None, lastObservation=last_observation
                )
                store = self.store_with_cursor(cursor)
                with self.assertRaises(FederationSchemaError):
                    store.provision(
                        "leeds_ext",
                        "postgresql://reader:secret@source-db:5432/sourcedb",
                    )

    def test_reprovision_also_rejects_a_stale_observation(self):
        # The reprovision branch never re-verifies or re-imports foreign
        # tables — without this, reprovisioning an alias whose latest
        # observation shows "changed" would still reconcile connection
        # settings and report success, silently ignoring the drift.
        cursor = MagicMock()
        cursor.fetchone.return_value = alias_row(
            provisionedAt="2026-08-11T00:00:00+00:00",
            lastObservation={"schema": "changed"},
        )
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
            )

    @patch("federation_store.extension_versions")
    def test_provision_rejects_an_incomplete_import(self, mock_versions):
        # IMPORT FOREIGN SCHEMA ... LIMIT TO silently imports whatever of
        # the named relations actually exists — it does not error on one
        # that's missing. An alias must never go active with fewer
        # foreign tables than its allowedRelations declares.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(provisionedAt=None, lastObservation={"schema": "current"}),
        ]
        cursor.fetchall.return_value = []
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
            )

    def test_provision_requires_acknowledgement_of_detected_row_level_security(self):
        # All MAPP callers query through the same mapped remote user —
        # any per-user row-level security on the source is bypassed
        # entirely once federated. The registration-time acknowledgement
        # can't cover this: RLS is only discovered later, by Observe.
        cursor = MagicMock()
        cursor.fetchone.return_value = alias_row(
            provisionedAt=None,
            lastObservation={
                "schema": "current",
                "rowLevelSecurityDetected": True,
            },
        )
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
            )

    @patch("federation_store.extension_versions")
    def test_provision_proceeds_once_row_level_security_is_acknowledged(self, mock_versions):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt=None,
                lastObservation={
                    "schema": "current",
                    "rowLevelSecurityDetected": True,
                },
            ),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        result = store.provision(
            "leeds_ext",
            "postgresql://reader:secret@source-db:5432/sourcedb",
            acknowledge_row_level_security=True,
        )

        self.assertIsNotNone(result["provisionedAt"])

    @patch("federation_store.extension_versions")
    def test_provision_issues_the_expected_fdw_ddl_and_marks_provisioned(self, mock_versions):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(provisionedAt=None, lastObservation={"schema": "current"}),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        result = store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        self.assertIsNotNone(result["provisionedAt"])
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertTrue(any("CREATE EXTENSION IF NOT EXISTS postgres_fdw" in s for s in statements))
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertIn("leeds_ext_srv", create_server)
        user_mappings = [s for s in statements if "CREATE USER MAPPING" in s]
        self.assertTrue(any("CURRENT_USER" in s for s in user_mappings))
        self.assertTrue(any("mapp_xyz" in s for s in user_mappings))
        create_schema = next(
            s for s in statements
            if "CREATE SCHEMA" in s and "source_leeds_ext" in s
        )
        self.assertNotIn("IF NOT EXISTS", create_schema)
        self.assertTrue(any("IMPORT FOREIGN SCHEMA" in s and "smoke_control_orders" in s for s in statements))
        self.assertTrue(any("GRANT SELECT ON ALL TABLES IN SCHEMA" in s and "mapp_xyz" in s for s in statements))
        self.assertTrue(any("provisioned_at = clock_timestamp()" in s and "status = 'active'" in s for s in statements))

    @patch("federation_store.extension_versions")
    def test_provision_forwards_ssl_options_from_the_connection_string(self, mock_versions):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(provisionedAt=None, lastObservation={"schema": "current"}),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext",
            "postgresql://reader:secret@source-db:5432/sourcedb"
            "?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca.pem",
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertIn("verify-full", create_server)
        self.assertIn("/etc/ssl/certs/ca.pem", create_server)

    @patch("federation_store.extension_versions")
    def test_provision_omits_ssl_options_when_the_connection_string_has_none(self, mock_versions):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(provisionedAt=None, lastObservation={"schema": "current"}),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertNotIn("sslmode", create_server)

    @patch("federation_store.extension_versions")
    def test_provision_does_not_mark_postgis_shippable_without_a_confirming_observation(self, mock_versions):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(provisionedAt=None, lastObservation={"schema": "current"}),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertNotIn("extensions", create_server)

    @patch("federation_store.extension_versions")
    def test_provision_marks_postgis_shippable_when_versions_match(self, mock_versions):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt=None,
                lastObservation={
                    "schema": "current",
                    "extensionVersions": MATCHING_VERSIONS,
                },
            ),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertIn("extensions", create_server)
        self.assertIn("postgis", create_server)

    @patch("federation_store.extension_versions")
    def test_provision_does_not_mark_postgis_shippable_when_versions_mismatch(self, mock_versions):
        # docs/federation-architecture-waypoint.md: pushdown is only safe
        # when PostGIS/PROJ/GEOS all match the federation database's own
        # versions — the remote merely *having* postgis is not enough.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt=None,
                lastObservation={
                    "schema": "current",
                    "extensionVersions": DIFFERENT_VERSIONS,
                },
            ),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertNotIn("extensions", create_server)

    @patch("federation_store.extension_versions")
    def test_reprovision_does_not_repeat_create_ddl(self, mock_versions):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": MATCHING_VERSIONS,
                },
            ),
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("CREATE SERVER" in s for s in statements))
        self.assertFalse(any("CREATE SCHEMA" in s for s in statements))
        self.assertFalse(any("IMPORT FOREIGN SCHEMA" in s for s in statements))
        # The CURRENT_USER mapping is reconciled by ALTER, not repeated —
        # only the reader mapping's DROP+CREATE reconciliation (always run,
        # to converge an alias provisioned before that mapping existed)
        # legitimately issues CREATE USER MAPPING on every reprovision call.
        self.assertFalse(
            any(
                "CREATE USER MAPPING" in s and "CURRENT_USER" in s
                for s in statements
            )
        )
        # Reprovisioning still reconciles connection settings (see below)
        # even when nothing changed — but must never touch the extensions
        # option when it's already correct.
        self.assertFalse(any("ADD extensions" in s or "DROP extensions" in s for s in statements))

    @patch("federation_store.extension_versions")
    def test_reprovision_reconciles_rotated_connection_settings(self, mock_versions):
        # The only reprovisioning path is /provision called again — it must
        # pick up a rotated password/host/database behind the same
        # connectionRef, not just re-decide the extensions option.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": MATCHING_VERSIONS,
                },
            ),
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext",
            "postgresql://reader:rotated-secret@new-source-db:5433/sourcedb",
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        alter_server = next(s for s in statements if "ALTER SERVER" in s and "SET host" in s)
        self.assertIn("new-source-db", alter_server)
        self.assertIn("5433", alter_server)
        alter_mapping = next(s for s in statements if "ALTER USER MAPPING" in s)
        self.assertIn("rotated-secret", alter_mapping)
        # The reader mapping is reconciled by DROP + CREATE, not ALTER (see
        # test_reprovision_creates_a_missing_reader_mapping for why), but
        # must still pick up the rotated credential.
        reader_mapping = next(
            s for s in statements
            if "CREATE USER MAPPING" in s and "mapp_xyz" in s
        )
        self.assertIn("rotated-secret", reader_mapping)

    @patch("federation_store.extension_versions")
    def test_reprovision_adds_a_newly_configured_ssl_option(self, mock_versions):
        # An operator tightening sslmode from disable to require/verify-
        # full behind the same connectionRef must reach the live server,
        # not just host/port/dbname/credentials.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": MATCHING_VERSIONS,
                },
            ),
            {"srvoptions": ["host=source-db"]},
            {"srvoptions": ["host=source-db"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext",
            "postgresql://reader:secret@source-db:5432/sourcedb?sslmode=verify-full",
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        ssl_alter = next(s for s in statements if "ALTER SERVER" in s and "sslmode" in s)
        self.assertIn("ADD", ssl_alter)
        self.assertIn("verify-full", ssl_alter)

    @patch("federation_store.extension_versions")
    def test_reprovision_updates_a_changed_ssl_option(self, mock_versions):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": MATCHING_VERSIONS,
                },
            ),
            {"srvoptions": ["host=source-db", "sslmode=require"]},
            {"srvoptions": ["host=source-db", "sslmode=require"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext",
            "postgresql://reader:secret@source-db:5432/sourcedb?sslmode=verify-full",
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        ssl_alter = next(s for s in statements if "ALTER SERVER" in s and "sslmode" in s)
        self.assertIn("SET", ssl_alter)
        self.assertIn("verify-full", ssl_alter)

    @patch("federation_store.extension_versions")
    def test_reprovision_drops_a_removed_ssl_option(self, mock_versions):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": MATCHING_VERSIONS,
                },
            ),
            {"srvoptions": ["host=source-db", "sslrootcert=/etc/ssl/old-ca.pem"]},
            {"srvoptions": ["host=source-db", "sslrootcert=/etc/ssl/old-ca.pem"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        ssl_alter = next(s for s in statements if "ALTER SERVER" in s and "sslrootcert" in s)
        self.assertIn("DROP", ssl_alter)

    @patch("federation_store.extension_versions")
    def test_reprovision_creates_a_missing_reader_mapping(self, mock_versions):
        # An alias provisioned before the reader-mapping fix existed has no
        # mapping for mapp_xyz yet — ALTER would fail on it, so this must
        # be DROP IF EXISTS + CREATE, not ALTER, to converge such an alias
        # the next time it's reprovisioned.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": MATCHING_VERSIONS,
                },
            ),
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertTrue(
            any(
                "DROP USER MAPPING IF EXISTS" in s and "mapp_xyz" in s
                for s in statements
            )
        )
        self.assertTrue(
            any(
                "CREATE USER MAPPING" in s and "mapp_xyz" in s
                and "ALTER" not in s
                for s in statements
            )
        )

    @patch("federation_store.extension_versions")
    def test_reprovision_enables_pushdown_once_versions_now_match(self, mock_versions):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": MATCHING_VERSIONS,
                },
            ),
            {"srvoptions": ["host=source-db"]},
            {"srvoptions": ["host=source-db"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertTrue(any("ALTER SERVER" in s and "ADD extensions" in s and "postgis" in s for s in statements))
        self.assertFalse(any("DROP extensions" in s for s in statements))

    @patch("federation_store.extension_versions")
    def test_reprovision_disables_pushdown_when_versions_now_mismatch(self, mock_versions):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            alias_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": DIFFERENT_VERSIONS,
                },
            ),
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertTrue(any("ALTER SERVER" in s and "DROP extensions" in s for s in statements))
        self.assertFalse(any("ADD extensions" in s for s in statements))

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
