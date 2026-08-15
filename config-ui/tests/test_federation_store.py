import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from federation_schema import FederationSchemaError
from federation_store import FederationAliasStore, MAX_ALIASES


OBSERVATION_ID = 41
OBSERVED_AT = datetime(2026, 8, 11, tzinfo=timezone.utc)
PHYSICAL_IDENTITY = "7672778953115078690/16384"
CONNECTION_IDENTITY = "reader@source-db:5432/sourcedb"
SERVER_OPTIONS = {
    "host": "source-db",
    "port": "5432",
    "dbname": "sourcedb",
    "use_remote_estimate": "true",
    "sslmode": "require",
    "gssencmode": "disable",
}
SOURCE_URL = (
    "postgresql://reader:secret@source-db:5432/sourcedb?"
    "sslmode=require&gssencmode=disable"
)
MATCHING_VERSIONS = {
    "postgis": "3.5.7",
    "postgisExtversion": "3.5.7",
    "proj": "9.8.1",
    "geos": "3.14.1",
}
DIFFERENT_VERSIONS = {
    "postgis": "3.0.0",
    "postgisExtversion": "3.0.0",
    "proj": "8.0.0",
    "geos": "3.9.0",
}
COLUMN_SHAPE_FINGERPRINT = "column-shape"
REMOTE_COLUMN_SHAPES = {
    "leeds.smoke_control_orders": COLUMN_SHAPE_FINGERPRINT
}
DEFAULT_COLLATION = ("UTF8", "default-collation")


def registration(**overrides):
    value = {
        "alias": "leeds_ext",
        "displayName": "Leeds external",
        "kind": "postgresql",
        "connectionRef": "LEEDS_EXT",
        "tlsPolicy": "require",
        "allowedRelations": ["leeds.smoke_control_orders"],
        "dataHandlingClassification": "Public open data.",
        "dataHandlingAcknowledged": True,
    }
    value.update(overrides)
    return value


def observation(
    *,
    connectivity="reachable",
    schema="current",
    fingerprint=None,
    versions=None,
    rls=False,
):
    value = {
        "connectivity": connectivity,
        "schema": schema,
        "sourceFreshness": "unknown",
        "lastConnected": None,
        "lastSchemaVerified": None,
        "sourceVersion": None,
        "rowLevelSecurityDetected": rls,
    }
    if fingerprint is not None:
        value["schemaFingerprint"] = fingerprint
    if versions is not None:
        value["extensionVersions"] = versions
    return value


def alias_row(**overrides):
    value = {
        "alias": "leeds_ext",
        "displayName": "Leeds external",
        "kind": "postgresql",
        "connectionRef": "LEEDS_EXT",
        "allowedRelations": ["leeds.smoke_control_orders"],
        "status": "pending",
        "freshnessStrategy": "manual",
        "dataHandlingClassification": "Public open data.",
        "registeredBy": "admin",
        "registeredAt": OBSERVED_AT,
        "lastObservation": observation(versions={}),
        "lastObservationId": OBSERVATION_ID,
        "tlsPolicy": "require",
        "provisionedAt": None,
        "approvedBy": None,
        "approvedAt": None,
        "rowLevelSecurityAcknowledged": False,
    }
    value.update(overrides)
    return value


def provision_row(
    *,
    last_observed_connection_identity=CONNECTION_IDENTITY,
    physical_identity=PHYSICAL_IDENTITY,
    accepted_schema_fingerprint=None,
    accepted_physical_identity=None,
    accepted_connection_identity=None,
    last_observation_id=OBSERVATION_ID,
    **overrides,
):
    value = alias_row(**overrides)
    value.update(
        last_observed_connection_identity=last_observed_connection_identity,
        physical_identity=physical_identity,
        accepted_schema_fingerprint=accepted_schema_fingerprint,
        accepted_physical_identity=accepted_physical_identity,
        accepted_connection_identity=accepted_connection_identity,
        last_observation_id=last_observation_id,
    )
    return value


def registry_state(**overrides):
    value = {
        "provisioned_at": None,
        "allowed_relations": ["leeds.smoke_control_orders"],
        "accepted_schema_fingerprint": None,
        "accepted_physical_identity": None,
        "accepted_connection_identity": None,
        "row_level_security_acknowledged": False,
    }
    value.update(overrides)
    return value


def local_binding(**overrides):
    value = {
        "relname": "smoke_control_orders",
        "relkind": "f",
        "owned": True,
        "srvname": "leeds_ext_srv",
        "remote_schema": "leeds",
        "remote_table": "smoke_control_orders",
        "column_shape_fingerprint": COLUMN_SHAPE_FINGERPRINT,
    }
    value.update(overrides)
    return value


def server_row(*, options=None, **overrides):
    value = {
        "fdwname": "postgres_fdw",
        "owned": True,
        "srvoptions": [
            f"{name}={option}"
            for name, option in (
                SERVER_OPTIONS if options is None else options
            ).items()
        ],
    }
    value.update(overrides)
    return value


def mapping_rows(current_options=("user=reader", "password=secret")):
    return [
        {
            "role_name": "mapp_federation",
            "is_current": True,
            "umoptions": list(current_options),
        },
        {"role_name": "mapp_derived", "is_current": False, "umoptions": None},
        {"role_name": "mapp_reader", "is_current": False, "umoptions": None},
    ]


def statements(cursor):
    return [str(call.args[0]) for call in cursor.execute.call_args_list]


class FederationAliasStoreTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "federation_store._database_default_collation_identity",
            return_value=DEFAULT_COLLATION,
        )
        self.default_collation_identity = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def store_with_cursor(cursor):
        store = FederationAliasStore(
            "postgresql://database", "mapp_reader", "mapp_derived"
        )
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection.cursor.return_value = cursor_context
        store._connect = MagicMock(return_value=connection)
        return store

    @staticmethod
    def provision(store, **kwargs):
        return store.provision(
            "leeds_ext",
            SOURCE_URL,
            "admin",
            expected_observation_id=OBSERVATION_ID,
            **kwargs,
        )

    def test_register_is_atomic_and_returns_its_own_row(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"count": 0, "exists": False},
            {"alias": "leeds_ext"},
            alias_row(lastObservation=None, lastObservationId=None),
        ]
        store = self.store_with_cursor(cursor)

        result = store.register(registration(), "admin")

        self.assertEqual("leeds_ext", result["alias"])
        sql_text = "\n".join(statements(cursor))
        self.assertIn("federation:register", cursor.execute.call_args_list[0].args[1][0])
        self.assertIn("ON CONFLICT (alias) DO NOTHING", sql_text)
        self.assertIn("RETURNING alias", sql_text)

    def test_register_preserves_duplicate_semantics_and_enforces_ceiling(self):
        cases = (
            ({"count": 1, "exists": True}, FileExistsError),
            ({"count": MAX_ALIASES, "exists": False}, FederationSchemaError),
        )
        for registry, error in cases:
            with self.subTest(registry=registry):
                cursor = MagicMock()
                cursor.fetchone.return_value = registry
                store = self.store_with_cursor(cursor)
                with self.assertRaises(error):
                    store.register(registration(), "admin")
                self.assertFalse(
                    any("INSERT INTO" in text for text in statements(cursor))
                )

    def test_list_is_bounded_by_the_registry_ceiling(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [alias_row(alias="m")]
        store = self.store_with_cursor(cursor)

        result = store.list()

        self.assertEqual("m", result[0]["alias"])
        query = str(cursor.execute.call_args.args[0])
        self.assertIn("ORDER BY alias", query)
        self.assertIn("LIMIT %s", query)
        self.assertEqual(
            (MAX_ALIASES + 1,), cursor.execute.call_args.args[1]
        )

    def test_connection_identity_binds_an_explicit_host_address(self):
        base = (
            "postgresql://reader:secret@source:5432/maps?"
            "sslmode=require&gssencmode=disable"
        )
        addressed = base + "&hostaddr=10.0.0.8"

        self.assertEqual(
            "reader@source:5432/maps",
            FederationAliasStore._connection_identity(base),
        )
        self.assertEqual(
            "reader@source[10.0.0.8]:5432/maps",
            FederationAliasStore._connection_identity(addressed),
        )

    def test_persist_observation_uses_local_sequence_not_remote_clock(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            registry_state(),
            {"id": OBSERVATION_ID},
        ]
        store = self.store_with_cursor(cursor)

        store._persist_observation(
            cursor,
            "leeds_ext",
            observation(),
            SOURCE_URL,
            PHYSICAL_IDENTITY,
            OBSERVED_AT,
            REMOTE_COLUMN_SHAPES,
        )

        update = next(
            text for text in statements(cursor) if "SET last_observation" in text
        )
        self.assertIn("last_observation_id = %s", update)
        self.assertNotIn("observed_at <", update)

    def test_reachable_drift_suspends_runtime_schema_access(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            registry_state(
                provisioned_at=OBSERVED_AT,
                accepted_schema_fingerprint="old",
                accepted_physical_identity=PHYSICAL_IDENTITY,
                accepted_connection_identity=CONNECTION_IDENTITY,
            ),
            {"id": OBSERVATION_ID},
            {"owned": True},
        ]
        store = self.store_with_cursor(cursor)

        store._persist_observation(
            cursor,
            "leeds_ext",
            observation(fingerprint="new"),
            SOURCE_URL,
            PHYSICAL_IDENTITY,
            OBSERVED_AT,
            REMOTE_COLUMN_SHAPES,
        )

        sql_text = "\n".join(statements(cursor))
        self.assertIn("REVOKE USAGE ON SCHEMA", sql_text)
        self.assertIn("REVOKE SELECT ON ALL TABLES IN SCHEMA", sql_text)
        self.assertIn("mapp_derived", sql_text)
        self.assertIn("mapp_reader", sql_text)
        update = next(
            call
            for call in cursor.execute.call_args_list
            if "SET last_observation" in str(call.args[0])
        )
        self.assertIs(False, update.args[1][-2])

    def test_matching_evidence_restores_runtime_schema_access(self):
        cases = (
            (server_row(), local_binding(), True, "GRANT"),
            (
                server_row(options={**SERVER_OPTIONS, "host": "other-source"}),
                local_binding(),
                False,
                "REVOKE",
            ),
            (
                server_row(),
                local_binding(column_shape_fingerprint="drifted"),
                False,
                "REVOKE",
            ),
        )
        for local_server, binding, active, action in cases:
            with self.subTest(action=action):
                cursor = MagicMock()
                cursor.fetchone.side_effect = [
                    registry_state(
                        provisioned_at=OBSERVED_AT,
                        accepted_schema_fingerprint="same",
                        accepted_physical_identity=PHYSICAL_IDENTITY,
                        accepted_connection_identity=CONNECTION_IDENTITY,
                    ),
                    {"id": OBSERVATION_ID},
                    local_server,
                    {"owned": True},
                    {"owned": True},
                ]
                cursor.fetchall.side_effect = [
                    mapping_rows(),
                    [binding],
                ]
                store = self.store_with_cursor(cursor)

                store._persist_observation(
                    cursor, "leeds_ext", observation(fingerprint="same"),
                    SOURCE_URL, PHYSICAL_IDENTITY, OBSERVED_AT,
                    REMOTE_COLUMN_SHAPES,
                )

                update = next(
                    call for call in cursor.execute.call_args_list
                    if "SET last_observation" in str(call.args[0])
                )
                self.assertIs(active, update.args[1][-2])
                sql_text = "\n".join(statements(cursor))
                self.assertIn(f"{action} USAGE ON SCHEMA", sql_text)
                self.assertIn(f"{action} SELECT ON ALL TABLES", sql_text)

    def test_local_state_gate_requires_exact_owned_fdw_objects(self):
        store = self.store_with_cursor(MagicMock())

        def matches(
            server=server_row(), schema={"owned": True},
            mappings=None, bindings=(local_binding(),), shippable=(),
        ):
            cursor = MagicMock()
            cursor.fetchone.side_effect = [server, schema]
            cursor.fetchall.side_effect = [
                mapping_rows() if mappings is None else mappings,
                list(bindings),
            ]
            return store._local_state_matches(
                cursor, "leeds_ext", ["leeds.smoke_control_orders"],
                REMOTE_COLUMN_SHAPES, SOURCE_URL, list(shippable),
            )

        self.assertTrue(matches())
        for server in (
            None,
            server_row(fdwname="file_fdw"),
            server_row(owned=False),
            server_row(options={**SERVER_OPTIONS, "host": "other-source"}),
        ):
            self.assertFalse(matches(server=server))
        for schema in (None, {"owned": False}):
            self.assertFalse(matches(schema=schema))
        valid_mappings = mapping_rows()
        for mappings in (
            valid_mappings[1:],
            valid_mappings[:-1],
            valid_mappings + [{
                "role_name": "intruder", "is_current": False,
                "umoptions": None,
            }],
            mapping_rows(("user=other", "password=secret")),
            mapping_rows(("user=reader", "password=wrong")),
        ):
            self.assertFalse(matches(mappings=mappings))
        for bindings in (
            (),
            (local_binding(), local_binding(relname="extra")),
            (local_binding(relkind="r"),),
            (local_binding(owned=False),),
            (local_binding(srvname="other_srv"),),
            (local_binding(remote_schema="archive"),),
            (local_binding(remote_table="other"),),
            (local_binding(column_shape_fingerprint="drifted"),),
        ):
            self.assertFalse(matches(bindings=bindings))

        self.assertTrue(matches(shippable=("postgis",)))
        postgis = server_row(options={**SERVER_OPTIONS, "extensions": "postgis"})
        self.assertTrue(matches(server=postgis, shippable=("postgis",)))
        self.assertFalse(matches(server=postgis))
        unexpected = server_row(
            options={**SERVER_OPTIONS, "extensions": "postgis_topology"}
        )
        self.assertFalse(matches(server=unexpected, shippable=("postgis",)))

    def test_local_column_shape_uses_effective_remote_column_names(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [local_binding()]

        FederationAliasStore._local_relation_bindings(cursor, "source_leeds_ext")

        query = str(cursor.execute.call_args.args[0])
        for fragment in (
            "column_shape_fingerprint",
            "a.attname",
            "a.attfdwoptions",
            "option_name = 'column_name'",
            "t.typnamespace",
            "a.atttypmod",
            "a.attnotnull",
            "ORDER BY a.attnum",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, query)

    @patch("federation_store.extension_versions")
    def test_outage_does_not_change_pushdown_without_version_evidence(self, versions):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            registry_state(provisioned_at=OBSERVED_AT),
            {"id": OBSERVATION_ID},
            {"owned": True},
        ]
        store = self.store_with_cursor(cursor)

        store._persist_observation(
            cursor,
            "leeds_ext",
            observation(connectivity="unavailable", schema="unknown"),
            SOURCE_URL,
            None,
            OBSERVED_AT,
            {},
        )

        versions.assert_not_called()
        self.assertFalse(any("ALTER SERVER" in text for text in statements(cursor)))
        self.assertTrue(
            any("REVOKE USAGE ON SCHEMA" in text for text in statements(cursor))
        )
        self.assertTrue(
            any(
                "REVOKE SELECT ON ALL TABLES IN SCHEMA" in text
                for text in statements(cursor)
            )
        )

    @patch("federation_store.extension_versions", return_value=DIFFERENT_VERSIONS)
    def test_observed_version_drift_disables_pushdown(self, _versions):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            registry_state(
                provisioned_at=OBSERVED_AT,
                accepted_schema_fingerprint="same",
                accepted_physical_identity=PHYSICAL_IDENTITY,
                accepted_connection_identity=CONNECTION_IDENTITY,
            ),
            {"id": OBSERVATION_ID},
            {"srvoptions": ["extensions=postgis"]},
            server_row(),
            {"owned": True},
            {"owned": True},
        ]
        cursor.fetchall.side_effect = [mapping_rows(), [local_binding()]]
        store = self.store_with_cursor(cursor)

        store._persist_observation(
            cursor,
            "leeds_ext",
            observation(fingerprint="same", versions=MATCHING_VERSIONS),
            SOURCE_URL,
            PHYSICAL_IDENTITY,
            OBSERVED_AT,
            REMOTE_COLUMN_SHAPES,
        )

        self.assertTrue(
            any("DROP extensions" in text for text in statements(cursor))
        )
        update = next(
            call for call in cursor.execute.call_args_list
            if "SET last_observation" in str(call.args[0])
        )
        self.assertIs(True, update.args[1][-2])
        self.assertIn("GRANT USAGE ON SCHEMA", "\n".join(statements(cursor)))

    @patch("federation_store.detect_capability")
    def test_observe_locks_before_probe_and_has_no_dead_freshness_route(self, detect):
        detect.return_value = (
            observation(), OBSERVED_AT, PHYSICAL_IDENTITY, REMOTE_COLUMN_SHAPES
        )
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            registry_state(),
            {"id": OBSERVATION_ID},
            alias_row(),
        ]
        store = self.store_with_cursor(cursor)

        store.observe(
            "leeds_ext",
            SOURCE_URL,
            allowed_relations=("leeds.smoke_control_orders",),
            tls_policy="require",
        )

        self.assertIn("advisory_xact_lock", statements(cursor)[0])
        self.assertNotIn("version_relation", detect.call_args.kwargs)
        self.assertEqual(
            DEFAULT_COLLATION,
            detect.call_args.kwargs["local_default_collation"],
        )
        self.default_collation_identity.assert_called_once_with(cursor)

    def test_provision_requires_the_exact_observation_id(self):
        for value in (None, 0, True, "41"):
            with self.subTest(value=value):
                store = self.store_with_cursor(MagicMock())
                with self.assertRaises(FederationSchemaError):
                    store.provision(
                        "leeds_ext",
                        SOURCE_URL,
                        "admin",
                        expected_observation_id=value,
                    )

        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row(last_observation_id=40)
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FederationSchemaError) as raised:
            self.provision(store)
        self.assertEqual("federation.observation_not_current", raised.exception.code)

    @patch("federation_store.verify_remote_state")
    def test_provision_rejects_live_evidence_different_from_observation(self, verify):
        cases = (
            (
                observation(fingerprint="old", versions={}),
                (PHYSICAL_IDENTITY, {}, True, False, "new", REMOTE_COLUMN_SHAPES),
            ),
            (
                observation(versions={}, rls=False),
                (PHYSICAL_IDENTITY, {}, True, True, None, REMOTE_COLUMN_SHAPES),
            ),
            (
                observation(versions={}),
                ("replacement/1", {}, True, False, None, REMOTE_COLUMN_SHAPES),
            ),
            (
                observation(versions={"postgresql": "16"}),
                (
                    PHYSICAL_IDENTITY, {"postgresql": "17"}, True, False,
                    None, REMOTE_COLUMN_SHAPES,
                ),
            ),
        )
        for observed, live in cases:
            with self.subTest(observed=observed, live=live):
                verify.reset_mock()
                verify.return_value = live
                cursor = MagicMock()
                cursor.fetchone.return_value = provision_row(lastObservation=observed)
                store = self.store_with_cursor(cursor)
                with self.assertRaises(FederationSchemaError) as raised:
                    self.provision(store)
                self.assertEqual(
                    "federation.observation_not_current", raised.exception.code
                )

    @patch("federation_store.verify_remote_state")
    def test_provision_acknowledgement_gates_are_independent(self, verify):
        cases = (
            (
                provision_row(lastObservation=observation(versions={}, rls=True)),
                (PHYSICAL_IDENTITY, {}, True, True, None, REMOTE_COLUMN_SHAPES),
                "federation.row_level_security_not_acknowledged",
            ),
            (
                provision_row(
                    lastObservation=observation(fingerprint="new", versions={}),
                    accepted_schema_fingerprint="old",
                ),
                (PHYSICAL_IDENTITY, {}, True, False, "new", REMOTE_COLUMN_SHAPES),
                "federation.schema_change_not_acknowledged",
            ),
            (
                provision_row(
                    accepted_physical_identity="old/1",
                    lastObservation=observation(versions={}),
                ),
                (PHYSICAL_IDENTITY, {}, True, False, None, REMOTE_COLUMN_SHAPES),
                "federation.physical_rebind_not_acknowledged",
            ),
            (
                provision_row(accepted_connection_identity="other@host:5432/db"),
                (PHYSICAL_IDENTITY, {}, True, False, None, REMOTE_COLUMN_SHAPES),
                "federation.physical_rebind_not_acknowledged",
            ),
        )
        for row, live, code in cases:
            with self.subTest(code=code):
                verify.return_value = live
                cursor = MagicMock()
                cursor.fetchone.return_value = row
                store = self.store_with_cursor(cursor)
                with self.assertRaises(FederationSchemaError) as raised:
                    self.provision(store)
                self.assertEqual(code, raised.exception.code)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(
            PHYSICAL_IDENTITY, {}, True, False, None, REMOTE_COLUMN_SHAPES
        ),
    )
    @patch("federation_store.extension_versions", return_value={})
    def test_first_provision_reconciles_roles_and_appends_approval(
        self, _versions, verify
    ):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(),
            alias_row(status="active", provisionedAt=OBSERVED_AT),
        ]
        cursor.fetchall.return_value = [local_binding()]
        store = self.store_with_cursor(cursor)

        result = self.provision(store)

        self.assertEqual("active", result["status"])
        sql_text = "\n".join(statements(cursor))
        self.assertIn("CREATE SERVER", sql_text)
        self.assertIn("IMPORT FOREIGN SCHEMA", sql_text)
        self.assertEqual(3, sql_text.count("CREATE USER MAPPING"))
        self.assertIn("mapp_derived", sql_text)
        self.assertIn("mapp_reader", sql_text)
        self.assertNotIn("GRANT USAGE ON FOREIGN SERVER", sql_text)
        self.assertIn("INSERT INTO", sql_text)
        self.assertIn("_approvals", sql_text)
        self.assertIn("accepted_connection_identity", sql_text)
        self.assertEqual(2, verify.call_count)
        self.default_collation_identity.assert_called_once_with(cursor)
        self.assertTrue(all(
            call.kwargs["local_default_collation"] == DEFAULT_COLLATION
            for call in verify.call_args_list
        ))

    @patch("federation_store.verify_remote_state")
    @patch("federation_store.extension_versions", return_value={})
    def test_import_change_rolls_back_before_approval(self, _versions, verify):
        verify.side_effect = [
            (PHYSICAL_IDENTITY, {}, True, False, None, REMOTE_COLUMN_SHAPES),
            (PHYSICAL_IDENTITY, {}, True, True, None, REMOTE_COLUMN_SHAPES),
        ]
        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row()
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError) as raised:
            self.provision(store)

        self.assertEqual("federation.observation_not_current", raised.exception.code)
        self.assertFalse(any("_approvals" in text for text in statements(cursor)))

    @patch(
        "federation_store.verify_remote_state",
        return_value=(
            PHYSICAL_IDENTITY, {}, True, False, None, REMOTE_COLUMN_SHAPES
        ),
    )
    @patch("federation_store.extension_versions", return_value={})
    def test_incomplete_or_mismatched_import_never_activates(
        self, _versions, _verify
    ):
        cases = (
            ([], [], "federation.import_incomplete"),
            (
                [local_binding()],
                [local_binding(column_shape_fingerprint="drifted")],
                "federation.local_state_invalid",
            ),
        )
        for imported, final_bindings, code in cases:
            with self.subTest(code=code):
                cursor = MagicMock()
                cursor.fetchone.return_value = provision_row()
                cursor.fetchall.side_effect = [imported, final_bindings]
                store = self.store_with_cursor(cursor)

                with self.assertRaises(FederationSchemaError) as raised:
                    self.provision(store)

                self.assertEqual(code, raised.exception.code)
                sql_text = "\n".join(statements(cursor))
                self.assertNotIn("GRANT SELECT", sql_text)
                self.assertNotIn("_approvals", sql_text)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(
            PHYSICAL_IDENTITY, {}, True, False, None, REMOTE_COLUMN_SHAPES
        ),
    )
    @patch("federation_store.extension_versions", return_value={})
    def test_reprovision_repairs_missing_local_foreign_table(
        self, _versions, verify
    ):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt=OBSERVED_AT,
                accepted_physical_identity=PHYSICAL_IDENTITY,
                accepted_connection_identity=CONNECTION_IDENTITY,
            ),
            {
                "fdwname": "postgres_fdw",
                "owned": True,
                "srvoptions": [
                    "host=source-db",
                    "port=5432",
                    "dbname=sourcedb",
                    "use_remote_estimate=true",
                    "sslmode=require",
                    "gssencmode=disable",
                ],
            },
            {"owned": True},
            alias_row(status="active", provisionedAt=OBSERVED_AT),
        ]
        cursor.fetchall.side_effect = [
            [],
            [local_binding()],
            [local_binding()],
        ]
        store = self.store_with_cursor(cursor)

        self.provision(store)

        self.assertTrue(any("IMPORT FOREIGN SCHEMA" in text for text in statements(cursor)))
        self.assertEqual(2, verify.call_count)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(
            PHYSICAL_IDENTITY, {}, True, False, "same", REMOTE_COLUMN_SHAPES
        ),
    )
    @patch("federation_store.extension_versions", return_value={})
    def test_reprovision_reimports_only_without_a_schema_baseline(
        self, _versions, verify
    ):
        for accepted_fingerprint, should_import in ((None, True), ("same", False)):
            with self.subTest(accepted_fingerprint=accepted_fingerprint):
                cursor = MagicMock()
                cursor.fetchone.side_effect = [
                    provision_row(
                        provisionedAt=OBSERVED_AT,
                        accepted_schema_fingerprint=accepted_fingerprint,
                        accepted_physical_identity=PHYSICAL_IDENTITY,
                        accepted_connection_identity=CONNECTION_IDENTITY,
                        lastObservation=observation(
                            fingerprint="same", versions={}
                        ),
                    ),
                    {
                        "fdwname": "postgres_fdw",
                        "owned": True,
                        "srvoptions": [
                            "host=source-db",
                            "port=5432",
                            "dbname=sourcedb",
                            "use_remote_estimate=true",
                            "sslmode=require",
                            "gssencmode=disable",
                        ],
                    },
                    {"owned": True},
                    alias_row(status="active", provisionedAt=OBSERVED_AT),
                ]
                cursor.fetchall.side_effect = (
                    [[local_binding()], [local_binding()], [local_binding()]]
                    if should_import
                    else [[local_binding()], [local_binding()]]
                )
                store = self.store_with_cursor(cursor)
                verify.reset_mock()

                self.provision(store)

                imported = any(
                    "IMPORT FOREIGN SCHEMA" in text
                    for text in statements(cursor)
                )
                self.assertEqual(should_import, imported)
                self.assertEqual(2 if should_import else 1, verify.call_count)
        self.assertTrue(any("_approvals" in text for text in statements(cursor)))

    @patch(
        "federation_store.verify_remote_state",
        return_value=(
            PHYSICAL_IDENTITY, {}, True, False, "same", REMOTE_COLUMN_SHAPES
        ),
    )
    @patch("federation_store.extension_versions", return_value={})
    def test_reprovision_repairs_local_column_drift(self, _versions, verify):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt=OBSERVED_AT,
                accepted_schema_fingerprint="same",
                accepted_physical_identity=PHYSICAL_IDENTITY,
                accepted_connection_identity=CONNECTION_IDENTITY,
                lastObservation=observation(fingerprint="same", versions={}),
            ),
            server_row(),
            {"owned": True},
            alias_row(status="active", provisionedAt=OBSERVED_AT),
        ]
        cursor.fetchall.side_effect = [
            [local_binding(column_shape_fingerprint="drifted")],
            [local_binding()],
            [local_binding()],
        ]
        store = self.store_with_cursor(cursor)

        self.provision(store)

        sql_text = "\n".join(statements(cursor))
        self.assertIn("DROP FOREIGN TABLE", sql_text)
        self.assertIn("IMPORT FOREIGN SCHEMA", sql_text)
        self.assertEqual(2, verify.call_count)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(
            PHYSICAL_IDENTITY, {}, True, False, None, REMOTE_COLUMN_SHAPES
        ),
    )
    @patch("federation_store.extension_versions", return_value={})
    def test_reprovision_fails_closed_for_missing_or_wrong_server(
        self, _versions, _verify
    ):
        for server in (
            None,
            server_row(fdwname="file_fdw"),
            server_row(owned=False),
        ):
            with self.subTest(server=server):
                cursor = MagicMock()
                cursor.fetchone.side_effect = [
                    provision_row(
                        provisionedAt=OBSERVED_AT,
                        accepted_physical_identity=PHYSICAL_IDENTITY,
                        accepted_connection_identity=CONNECTION_IDENTITY,
                    ),
                    server,
                ]
                store = self.store_with_cursor(cursor)
                with self.assertRaises(FederationSchemaError) as raised:
                    self.provision(store)
                self.assertEqual("federation.local_state_invalid", raised.exception.code)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(
            PHYSICAL_IDENTITY, {}, True, False, None, REMOTE_COLUMN_SHAPES
        ),
    )
    @patch("federation_store.extension_versions", return_value={})
    def test_provision_does_not_expose_an_extra_table_or_view(
        self, _versions, _verify
    ):
        for relkind in ("r", "v"):
            with self.subTest(relkind=relkind):
                cursor = MagicMock()
                cursor.fetchone.return_value = provision_row()
                cursor.fetchall.side_effect = [
                    [local_binding()],
                    [
                        local_binding(),
                        local_binding(
                            relname="unmanaged",
                            relkind=relkind,
                            srvname=None,
                            remote_schema=None,
                            remote_table=None,
                        ),
                    ],
                ]
                store = self.store_with_cursor(cursor)

                with self.assertRaises(FederationSchemaError) as raised:
                    self.provision(store)

                self.assertEqual(
                    "federation.local_state_invalid", raised.exception.code
                )
                sql_text = "\n".join(statements(cursor))
                self.assertNotIn("GRANT SELECT", sql_text)
                self.assertNotIn("_approvals", sql_text)

    def test_server_option_reconciliation_is_exact(self):
        cursor = MagicMock()
        FederationAliasStore._reconcile_server_options(
            cursor,
            "leeds_ext_srv",
            {
                "host": "old",
                "sslmode": "require",
                "sslcert": "/stale/client.crt",
            },
            {
                "host": "source-db",
                "sslmode": "verify-full",
                "gssencmode": "disable",
            },
        )

        query = str(cursor.execute.call_args.args[0])
        self.assertIn("SET", query)
        self.assertIn("ADD", query)
        self.assertIn("DROP", query)
        self.assertIn("sslcert", query)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(
            PHYSICAL_IDENTITY, {}, True, False, None, REMOTE_COLUMN_SHAPES
        ),
    )
    @patch("federation_store.extension_versions", return_value={})
    def test_first_server_uses_only_the_closed_connection_options(
        self, _versions, _verify
    ):
        url = (
            "postgresql://reader:secret@source-db:5432/sourcedb?"
            "hostaddr=10.0.0.8&sslmode=verify-full&sslrootcert=system&"
            "gssencmode=disable"
        )
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                last_observed_connection_identity=(
                    "reader@source-db[10.0.0.8]:5432/sourcedb"
                )
            ),
            alias_row(status="active"),
        ]
        cursor.fetchall.return_value = [local_binding()]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext",
            url,
            "admin",
            expected_observation_id=OBSERVATION_ID,
        )

        create_server = next(
            text for text in statements(cursor) if "CREATE SERVER" in text
        )
        for option in ("hostaddr", "sslmode", "sslrootcert", "gssencmode"):
            self.assertIn(option, create_server)
        self.assertNotIn("sslcert", create_server)
        self.assertNotIn("sslkey", create_server)


if __name__ == "__main__":
    unittest.main()
