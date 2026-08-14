import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import federation_store
from federation_schema import FederationSchemaError
from federation_store import FederationAliasStore

MATCHING_VERSIONS = {
    "postgis": "3.5.7", "postgisExtversion": "3.5.7", "proj": "9.8.1",
    "geos": "3.14.1",
}
DIFFERENT_VERSIONS = {
    "postgis": "3.0.0", "postgisExtversion": "3.0.0", "proj": "8.0.0",
    "geos": "3.9.0",
}
OBSERVED_AT = datetime(2026, 8, 11, 0, 0, 0, tzinfo=timezone.utc)

# Matches _connection_identity() of "postgresql://reader:secret@source-db
# :5432/sourcedb" — the connection_url every provision()-related fixture
# below uses unless it's specifically exercising a rotated endpoint.
# physical_identity matches SOURCE_DB_PHYSICAL_IDENTITY, the value every
# @patch("federation_store.verify_remote_state") below returns.
SOURCE_DB_PHYSICAL_IDENTITY = "7672778953115078690/16384"
SOURCE_DB_CONNECTION_IDENTITY = {
    "last_observed_connection_identity": "reader@source-db:5432/sourcedb",
    "physical_identity": SOURCE_DB_PHYSICAL_IDENTITY,
}


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
        "tlsPolicy": "require",
        "provisionedAt": None,
        "approvedBy": None,
        "approvedAt": None,
        "rowLevelSecurityAcknowledged": False,
    }
    row.update(overrides)
    return row


def provision_row(
    *,
    last_observed_connection_identity=None,
    physical_identity=None,
    accepted_schema_fingerprint=None,
    accepted_physical_identity=None,
    **overrides,
):
    """The single locked SELECT provision() issues combines the public
    alias_row() columns with the internal-only identity columns in one
    row — this is that combined shape. Defaults match
    SOURCE_DB_CONNECTION_IDENTITY/SOURCE_DB_PHYSICAL_IDENTITY, satisfying
    the connectionRef every fixture below uses unless overridden.
    accepted_schema_fingerprint defaults to None (nothing durably
    accepted yet — matches every fixture that doesn't care about the
    schema-fingerprint-acceptance gate specifically)."""
    row = alias_row(**overrides)
    row["last_observed_connection_identity"] = (
        last_observed_connection_identity
        if last_observed_connection_identity is not None
        else SOURCE_DB_CONNECTION_IDENTITY["last_observed_connection_identity"]
    )
    row["physical_identity"] = (
        physical_identity
        if physical_identity is not None
        else SOURCE_DB_PHYSICAL_IDENTITY
    )
    row["accepted_schema_fingerprint"] = accepted_schema_fingerprint
    row["accepted_physical_identity"] = accepted_physical_identity
    return row


# Every provision()-related fixture below uses this as its connectionRef,
# satisfying alias_row()'s default tlsPolicy="require" — sslmode=require
# is the floor of federation_schema.TLS_POLICIES.
SOURCE_DB_URL = "postgresql://reader:secret@source-db:5432/sourcedb?sslmode=require"


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
                "Public council open data.", "admin", "require",
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

        result = store.record_observation(
            "leeds_ext",
            observation,
            "postgresql://reader:secret@source-db:5432/sourcedb",
            SOURCE_DB_PHYSICAL_IDENTITY,
            OBSERVED_AT,
        )

        self.assertEqual("active", result["status"])
        history_insert = cursor.execute.call_args_list[0]
        self.assertIn("INSERT INTO", str(history_insert.args[0]))
        self.assertIn("_observations", str(history_insert.args[0]))
        history_params = history_insert.args[1]
        self.assertEqual("leeds_ext", history_params[0])
        self.assertEqual(observation, history_params[1].obj)  # unwrap Jsonb
        self.assertEqual(
            (
                "reader@source-db:5432/sourcedb",
                SOURCE_DB_PHYSICAL_IDENTITY,
                OBSERVED_AT,
                "leeds_ext",
            ),
            history_params[2:],
        )
        update = cursor.execute.call_args_list[1]
        self.assertEqual("reader@source-db:5432/sourcedb", update.args[1][1])
        self.assertEqual(SOURCE_DB_PHYSICAL_IDENTITY, update.args[1][2])
        self.assertEqual(OBSERVED_AT, update.args[1][3])
        self.assertEqual(True, update.args[1][4])

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

        result = store.record_observation(
            "leeds_ext",
            observation,
            "postgresql://reader:secret@source-db:5432/sourcedb",
            None,
            OBSERVED_AT,
        )

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

        result = store.record_observation(
            "leeds_ext",
            observation,
            "postgresql://reader:secret@source-db:5432/sourcedb",
            SOURCE_DB_PHYSICAL_IDENTITY,
            OBSERVED_AT,
        )

        self.assertEqual("pending", result["status"])
        update = cursor.execute.call_args_list[1]
        self.assertIn("WHEN provisioned_at IS NULL THEN status", str(update.args[0]))

    def test_record_observation_rejects_a_superseded_probe_as_a_no_op(self):
        # Two overlapping Observe calls can finish in either order — this
        # one's own probe (observed_at) is older than what's already
        # stored, meaning a newer Observe already committed while this
        # one's remote probe was still running. The conditional UPDATE's
        # WHERE clause matches no row (Postgres serializes concurrent
        # UPDATEs to the same row, so this always sees the fresher one
        # once it commits) — silently keep the fresher result rather than
        # overwrite it with this stale one.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            None,  # the conditional UPDATE matches no row (stale probe)
            {"exists": 1},  # the alias does exist
            alias_row(status="active"),  # self.get(alias)'s fresh read
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

        result = store.record_observation(
            "leeds_ext",
            observation,
            "postgresql://reader:secret@source-db:5432/sourcedb",
            SOURCE_DB_PHYSICAL_IDENTITY,
            OBSERVED_AT,
        )

        # No exception — the alias's already-fresher state is returned.
        self.assertEqual("active", result["status"])
        update = cursor.execute.call_args_list[1]
        rendered_update = str(update.args[0])
        self.assertIn("observed_at IS NULL", rendered_update)
        self.assertIn(
            "last_observation ->> 'connectivity' = 'reachable'", rendered_update
        )
        self.assertIn("observed_at <", rendered_update)
        # reachable is bound twice now: once for the status CASE, once
        # for the new connectivity-transition branch.
        self.assertEqual(2, update.args[1].count(True))

    def test_record_observation_binds_reachable_for_both_the_status_case_and_the_transition_guard(
        self,
    ):
        # A pure mock cannot prove Postgres evaluates the new WHERE-clause
        # boolean expression correctly (that requires a real database —
        # verified live separately) — but it can prove the Python side
        # binds the right values in the right positions, which is exactly
        # where a copy-paste slip (e.g. binding `reachable` only once, or
        # in the wrong slot) would hide from a passing test otherwise.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "leeds_ext", "provisioned_at": None},
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

        store.record_observation(
            "leeds_ext",
            observation,
            "postgresql://reader:secret@source-db:5432/sourcedb",
            SOURCE_DB_PHYSICAL_IDENTITY,
            OBSERVED_AT,
        )

        update = cursor.execute.call_args_list[1]
        params = update.args[1]
        # (observation, connection_identity, physical_identity,
        #  observed_at, reachable, alias, reachable, observed_at)
        self.assertEqual(8, len(params))
        self.assertIs(True, params[4])
        self.assertEqual("leeds_ext", params[5])
        self.assertIs(True, params[6])
        self.assertEqual(OBSERVED_AT, params[7])

    def test_record_observation_raises_not_found_when_alias_missing(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FileNotFoundError):
            store.record_observation(
                "leeds_ext",
                {
                    "connectivity": "unavailable",
                    "schema": "unknown",
                    "sourceFreshness": "unknown",
                    "lastConnected": None,
                    "lastSchemaVerified": None,
                    "sourceVersion": None,
                },
                "postgresql://reader:secret@source-db:5432/sourcedb",
                None,
                OBSERVED_AT,
            )

    @patch("federation_store.detect_capability")
    def test_observe_acquires_a_per_alias_advisory_lock_before_probing(
        self, mock_detect
    ):
        # round 25 finding: two overlapping Observe calls for the same
        # alias can otherwise commit out of order across a reachability
        # transition (record_observation() has no shared clock to compare
        # a reachable probe's remote-clock timestamp against an
        # unreachable probe's local-clock one, so it accepts every
        # transition unconditionally). observe() closes this by holding a
        # per-alias advisory lock across the whole probe-and-persist
        # cycle, so a second Observe for the same alias can't even start
        # probing until the first one's entire cycle has committed.
        mock_detect.return_value = (
            {
                "connectivity": "reachable",
                "schema": "current",
                "sourceFreshness": "unknown",
                "lastConnected": "2026-08-13T00:00:00+00:00",
                "lastSchemaVerified": "2026-08-13T00:00:00+00:00",
                "sourceVersion": None,
            },
            OBSERVED_AT,
            SOURCE_DB_PHYSICAL_IDENTITY,
        )
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "leeds_ext", "provisioned_at": None},
            alias_row(status="active"),
        ]
        store = self.store_with_cursor(cursor)

        store.observe(
            "leeds_ext",
            "postgresql://reader:secret@source-db:5432/sourcedb",
            allowed_relations=("leeds.smoke_control_orders",),
            tls_policy="require",
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertEqual(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", statements[0]
        )
        lock_params = cursor.execute.call_args_list[0].args[1]
        self.assertEqual(("federation:observe:leeds_ext",), lock_params)
        mock_detect.assert_called_once_with(
            "postgresql://reader:secret@source-db:5432/sourcedb",
            allowed_relations=("leeds.smoke_control_orders",),
            tls_policy="require",
            version_relation=None,
        )

    @patch("federation_store.detect_capability")
    def test_observe_uses_a_lock_key_scoped_to_its_own_alias(self, mock_detect):
        mock_detect.return_value = (
            {
                "connectivity": "unavailable",
                "schema": "unknown",
                "sourceFreshness": "unknown",
                "lastConnected": None,
                "lastSchemaVerified": None,
                "sourceVersion": None,
            },
            OBSERVED_AT,
            None,
        )
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"alias": "other_alias", "provisioned_at": None},
            alias_row(status="unavailable"),
        ]
        store = self.store_with_cursor(cursor)

        store.observe(
            "other_alias",
            "postgresql://reader:secret@source-db:5432/sourcedb",
            allowed_relations=("leeds.smoke_control_orders",),
            tls_policy="require",
        )

        lock_params = cursor.execute.call_args_list[0].args[1]
        self.assertEqual(("federation:observe:other_alias",), lock_params)

    def test_record_observation_reads_but_never_writes_the_accepted_schema_fingerprint(self):
        # accepted_schema_fingerprint is durable, written only by a
        # successful provision() call. Observe reads it read-only, purely
        # to report drift (see record_observation()'s docstring) — it must
        # never WRITE it, or the same self-healing bug this split was
        # meant to fix (comparing against a value Observe itself can
        # overwrite) would creep back in.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"accepted_schema_fingerprint": None},
            {"alias": "leeds_ext", "provisioned_at": None},
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
            "schemaFingerprint": "fp-1",
        }

        store.record_observation(
            "leeds_ext", observation,
            "postgresql://reader:secret@source-db:5432/sourcedb",
            SOURCE_DB_PHYSICAL_IDENTITY, OBSERVED_AT,
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        writes = [s for s in statements if "SET" in s or "INSERT INTO" in s]
        self.assertFalse(
            any("accepted_schema_fingerprint" in s for s in writes)
        )
        self.assertTrue(
            any("SELECT accepted_schema_fingerprint" in s for s in statements)
        )
        # FOR UPDATE — self-audit finding: an unlocked read here could see
        # a pre-commit value while a concurrent provision() call (which
        # takes its own FOR UPDATE on this row) is mid-transaction,
        # computing acceptedSchemaCurrent against a fingerprint
        # provision() is about to replace rather than the one it commits.
        self.assertTrue(
            any(
                "SELECT accepted_schema_fingerprint" in s and "FOR UPDATE" in s
                for s in statements
            )
        )
        insert = cursor.execute.call_args_list[1]
        self.assertEqual("current", insert.args[1][1].obj["schema"])
        self.assertTrue(insert.args[1][1].obj["acceptedSchemaCurrent"])

    def test_record_observation_reports_drift_from_the_accepted_fingerprint(self):
        # The read-only counterpart to provision()'s gate: Observe should
        # surface a drifted fingerprint as soon as it sees it, not leave
        # an operator to discover it only when someone next attempts to
        # Provision (round 22 finding). This must land in its own field,
        # not overwrite "schema" — schema's existing meaning (relation
        # present/selectable) feeds provision()'s stored-evidence fast-
        # fail, which has no acknowledgement escape; conflating the two
        # would make that fast-fail fire on every fingerprint drift and
        # permanently block the acknowledge_schema_change path (caught
        # live against the real FDW rig — see record_observation()'s
        # docstring).
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"accepted_schema_fingerprint": "fp-old"},
            {"alias": "leeds_ext", "provisioned_at": None},
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
            "schemaFingerprint": "fp-new",
        }

        store.record_observation(
            "leeds_ext", observation,
            "postgresql://reader:secret@source-db:5432/sourcedb",
            SOURCE_DB_PHYSICAL_IDENTITY, OBSERVED_AT,
        )

        insert = cursor.execute.call_args_list[1]
        self.assertEqual("current", insert.args[1][1].obj["schema"])
        self.assertFalse(insert.args[1][1].obj["acceptedSchemaCurrent"])
        update = cursor.execute.call_args_list[2]
        self.assertFalse(update.args[1][0].obj["acceptedSchemaCurrent"])
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        writes = [s for s in statements if "SET" in s or "INSERT INTO" in s]
        self.assertFalse(
            any("accepted_schema_fingerprint" in s for s in writes)
        )

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

        store.record_observation(
            "leeds_ext",
            observation,
            "postgresql://reader:secret@source-db:5432/sourcedb",
            SOURCE_DB_PHYSICAL_IDENTITY,
            OBSERVED_AT,
        )

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

        store.record_observation(
            "leeds_ext",
            observation,
            "postgresql://reader:secret@source-db:5432/sourcedb",
            SOURCE_DB_PHYSICAL_IDENTITY,
            OBSERVED_AT,
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("ALTER SERVER" in s for s in statements))

    def test_provision_rejects_a_malformed_alias_name(self):
        cursor = MagicMock()
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FederationSchemaError):
            store.provision("not a valid alias", SOURCE_DB_URL, "admin")
        cursor.execute.assert_not_called()

    def test_provision_rejects_a_missing_or_stale_observation(self):
        # IMPORT FOREIGN SCHEMA acts on whatever the remote looks like
        # right now — provisioning against evidence that was never taken,
        # or that already showed the schema had changed, could silently
        # produce an incomplete alias.
        for last_observation in (None, {"schema": "changed"}, {}):
            with self.subTest(last_observation=last_observation):
                cursor = MagicMock()
                cursor.fetchone.return_value = provision_row(
                    provisionedAt=None, lastObservation=last_observation
                )
                store = self.store_with_cursor(cursor)
                with self.assertRaises(FederationSchemaError):
                    store.provision(
                        "leeds_ext",
                        "postgresql://reader:secret@source-db:5432/sourcedb", "admin",
                    )

    def test_reprovision_also_rejects_a_stale_observation(self):
        # The reprovision branch never re-verifies or re-imports foreign
        # tables — without this, reprovisioning an alias whose latest
        # observation shows "changed" would still reconcile connection
        # settings and report success, silently ignoring the drift.
        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row(
            provisionedAt="2026-08-11T00:00:00+00:00",
            lastObservation={"schema": "changed"},
        )
        store = self.store_with_cursor(cursor)
        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb", "admin",
            )

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_rejects_an_incomplete_import(self, mock_versions, mock_physical_identity):
        # IMPORT FOREIGN SCHEMA ... LIMIT TO silently imports whatever of
        # the named relations actually exists — it does not error on one
        # that's missing. An alias must never go active with fewer
        # foreign tables than its allowedRelations declares.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(provisionedAt=None, lastObservation={"schema": "current"}),
        ]
        cursor.fetchall.return_value = []
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision("leeds_ext", SOURCE_DB_URL, "admin")

    def test_provision_requires_acknowledgement_of_detected_row_level_security(self):
        # All MAPP callers query through the same mapped remote user —
        # any per-user row-level security on the source is bypassed
        # entirely once federated. The registration-time acknowledgement
        # can't cover this: RLS is only discovered later, by Observe.
        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row(
            provisionedAt=None,
            lastObservation={
                "schema": "current",
                "rowLevelSecurityDetected": True,
            },
        )
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb", "admin",
            )

    @patch(
        # The stale lastObservation shows no RLS at all — only the LIVE
        # re-check (rls_detected=True) reveals it. This proves the
        # authoritative gate: RLS enabled on the remote *after* Observe
        # but *before* Provision must still be caught, not just RLS that
        # was already visible in the last Observe.
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, True, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_requires_acknowledgement_of_live_detected_row_level_security(
        self, mock_versions, mock_verify_remote_state
    ):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row(
            provisionedAt=None,
            lastObservation={"schema": "current"},
        )
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError) as raised:
            store.provision("leeds_ext", SOURCE_DB_URL, "admin")
        self.assertEqual(
            "federation.row_level_security_not_acknowledged",
            raised.exception.code,
        )
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("CREATE EXTENSION" in s for s in statements))

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, False, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_rejects_when_a_relation_is_no_longer_present_or_selectable(
        self, mock_versions, mock_verify_remote_state
    ):
        # Neither physical_identity (oid-only) nor the stored "schema":
        # "current" flag can detect a relation dropped, or a SELECT
        # revoked, since the last Observe — only a live re-check of
        # existence/selectability can.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row(
            provisionedAt=None,
            lastObservation={"schema": "current"},
        )
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError) as raised:
            store.provision("leeds_ext", SOURCE_DB_URL, "admin")
        self.assertEqual(
            "federation.observation_not_current", raised.exception.code
        )
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("CREATE EXTENSION" in s for s in statements))

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, "fp-first"),
    )
    @patch("federation_store.extension_versions")
    def test_provision_proceeds_on_first_ever_acceptance_when_it_matches_the_last_observation(
        self, mock_versions, mock_verify_remote_state
    ):
        # accepted_schema_fingerprint is None — nothing has ever been
        # durably accepted for this alias (its first provisioning) — so
        # there is nothing to compare the live fingerprint against for
        # THAT check specifically. It must still match what the last
        # Observe actually saw, though (see the test right below this one
        # for the case where it doesn't).
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt=None,
                lastObservation={
                    "schema": "current", "schemaFingerprint": "fp-first",
                },
                accepted_schema_fingerprint=None,
            ),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        result = store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        self.assertIsNotNone(result["provisionedAt"])
        activation = next(
            call for call in cursor.execute.call_args_list
            if "provisioned_at = clock_timestamp()" in str(call.args[0])
        )
        # The live fingerprint becomes the newly-accepted baseline.
        self.assertEqual(("admin", False, "fp-first", SOURCE_DB_PHYSICAL_IDENTITY, "leeds_ext"), activation.args[1])

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, "fp-drifted"),
    )
    @patch("federation_store.extension_versions")
    def test_first_ever_provisioning_rejects_a_fingerprint_drifted_since_the_last_observation(
        self, mock_versions, mock_verify_remote_state
    ):
        # round 25 finding: accepted_schema_fingerprint being None (first
        # provisioning) must not mean "no drift possible" — a column
        # added or a view redefined between Observe and this call changes
        # neither relations_verified nor physical_identity, so without
        # comparing against what the last Observe actually recorded, an
        # unreviewed definition would be imported on the very first
        # provisioning.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row(
            provisionedAt=None,
            lastObservation={
                "schema": "current", "schemaFingerprint": "fp-observed",
            },
            accepted_schema_fingerprint=None,
        )
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError) as raised:
            store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        self.assertEqual(
            "federation.observation_not_current", raised.exception.code
        )
        # No acknowledgement flag can override this — there is nothing
        # yet accepted to acknowledge past, only stale evidence to refresh.
        with self.assertRaises(FederationSchemaError) as raised_again:
            store.provision(
                "leeds_ext", SOURCE_DB_URL, "admin",
                acknowledge_schema_change=True,
            )
        self.assertEqual(
            "federation.observation_not_current", raised_again.exception.code
        )

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, "fp-accepted"),
    )
    @patch("federation_store.extension_versions")
    def test_provision_proceeds_when_the_fingerprint_still_matches_what_was_accepted(
        self, mock_versions, mock_verify_remote_state
    ):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt=None,
                lastObservation={"schema": "current"},
                accepted_schema_fingerprint="fp-accepted",
            ),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        result = store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        self.assertIsNotNone(result["provisionedAt"])

    @patch(
        # The live fingerprint ("fp-new") differs from what a PRIOR
        # successful provision() durably accepted ("fp-accepted") — an
        # ADD COLUMN or a CREATE OR REPLACE VIEW changes neither a
        # relation's oid nor its RLS flags, so only this comparison
        # catches it.
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, "fp-new"),
    )
    @patch("federation_store.extension_versions")
    def test_provision_rejects_when_the_fingerprint_has_drifted_since_acceptance(
        self, mock_versions, mock_verify_remote_state
    ):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row(
            provisionedAt=None,
            lastObservation={"schema": "current"},
            accepted_schema_fingerprint="fp-accepted",
        )
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError) as raised:
            store.provision("leeds_ext", SOURCE_DB_URL, "admin")
        self.assertEqual(
            "federation.schema_change_not_acknowledged", raised.exception.code
        )
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("CREATE EXTENSION" in s for s in statements))

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, "fp-new"),
    )
    @patch("federation_store.extension_versions")
    def test_provision_accepts_a_drifted_fingerprint_once_acknowledged(
        self, mock_versions, mock_verify_remote_state
    ):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt=None,
                lastObservation={"schema": "current"},
                accepted_schema_fingerprint="fp-accepted",
            ),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        result = store.provision(
            "leeds_ext", SOURCE_DB_URL, "admin",
            acknowledge_schema_change=True,
        )

        self.assertIsNotNone(result["provisionedAt"])
        activation = next(
            call for call in cursor.execute.call_args_list
            if "provisioned_at = clock_timestamp()" in str(call.args[0])
        )
        # The new fingerprint becomes the durably-accepted baseline.
        self.assertEqual(("admin", False, "fp-new", SOURCE_DB_PHYSICAL_IDENTITY, "leeds_ext"), activation.args[1])

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, "fp-new"),
    )
    @patch("federation_store.extension_versions")
    def test_repeated_rejected_provision_attempts_never_self_heal_without_acknowledgement(
        self, mock_versions, mock_verify_remote_state
    ):
        # The actual regression this whole mechanism exists for: a naive
        # design that compared against whatever Observe last *saw* (not
        # what a human explicitly *accepted*) would let the drift clear
        # itself the moment anyone retried — since accepted_schema_
        # fingerprint is only ever written by a successful provision()
        # call, retrying without acknowledgement must keep failing no
        # matter how many times it's retried.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row(
            provisionedAt=None,
            lastObservation={"schema": "current"},
            accepted_schema_fingerprint="fp-accepted",
        )
        store = self.store_with_cursor(cursor)

        for attempt in range(3):
            with self.subTest(attempt=attempt):
                with self.assertRaises(FederationSchemaError) as raised:
                    store.provision("leeds_ext", SOURCE_DB_URL, "admin")
                self.assertEqual(
                    "federation.schema_change_not_acknowledged",
                    raised.exception.code,
                )

    @patch(
        # rls_detected=True here — the live re-check must confirm RLS is
        # still actually present for the acknowledgement to be persisted
        # as True; a stale lastObservation claim alone is not enough.
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, True, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_proceeds_once_row_level_security_is_acknowledged(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
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
            SOURCE_DB_URL, "admin",
            acknowledge_row_level_security=True,
        )

        self.assertIsNotNone(result["provisionedAt"])
        # A durable record that this approval specifically accepted a
        # known RLS bypass — not just a transient gate that leaves no
        # trace once a later Observe replaces last_observation.
        activation = next(
            call for call in cursor.execute.call_args_list
            if "provisioned_at = clock_timestamp()" in str(call.args[0])
        )
        self.assertEqual(("admin", True, None, SOURCE_DB_PHYSICAL_IDENTITY, "leeds_ext"), activation.args[1])

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_issues_the_expected_fdw_ddl_and_marks_provisioned(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(provisionedAt=None, lastObservation={"schema": "current"}),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        result = store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        self.assertIsNotNone(result["provisionedAt"])
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        # The initial read locks the row and everything else — the
        # identity/policy checks, the FDW DDL, and the read of the row this
        # call returns — runs inside that same connection/transaction: a
        # concurrent Observe on this alias cannot commit a newer observation
        # until this call finishes, and the returned row is the one THIS
        # transaction produced rather than whatever a racing writer left
        # behind (it used to be a second, separate, post-commit read).
        self.assertIn("FOR UPDATE", statements[0])
        self.assertEqual(1, store._connect.call_count)
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
        activation = next(
            call for call in cursor.execute.call_args_list
            if "provisioned_at = clock_timestamp()" in str(call.args[0])
            and "status = 'active'" in str(call.args[0])
        )
        # Recorded atomically with activation, not as a separate write —
        # a crash or audit failure must never leave an exposed source
        # without durable approval attribution.
        self.assertIn("approved_by = %s", str(activation.args[0]))
        self.assertIn("approved_at = clock_timestamp()", str(activation.args[0]))
        # No RLS was detected in this fixture's observation, so nothing
        # was there to acknowledge.
        self.assertEqual(("admin", False, None, SOURCE_DB_PHYSICAL_IDENTITY, "leeds_ext"), activation.args[1])

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_records_the_approving_principal(self, mock_versions, mock_physical_identity):
        # Reprovisioning is itself an act of Approve exposure — an admin
        # explicitly re-approved by calling /provision again — so it must
        # record a fresh approvedBy/approvedAt too, not just the original
        # provision() call's.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
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

        store.provision("leeds_ext", SOURCE_DB_URL, "reviewer")

        approval = next(
            call for call in cursor.execute.call_args_list
            if "approved_by = %s" in str(call.args[0])
        )
        self.assertEqual(("reviewer", False, None, SOURCE_DB_PHYSICAL_IDENTITY, "leeds_ext"), approval.args[1])
        # provisioned_at is when the alias was first activated — untouched
        # on reprovision, unlike approved_by/approved_at.
        self.assertNotIn(
            "provisioned_at",
            str(approval.args[0]),
        )

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_forwards_ssl_options_from_the_connection_string(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(provisionedAt=None, lastObservation={"schema": "current"}),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext",
            "postgresql://reader:secret@source-db:5432/sourcedb"
            "?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca.pem", "admin",
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertIn("verify-full", create_server)
        self.assertIn("/etc/ssl/certs/ca.pem", create_server)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_omits_optional_ssl_options_when_absent(self, mock_versions, mock_physical_identity):
        # sslmode itself is always present once tlsPolicy is enforced (see
        # enforce_tls_policy below) — this covers the independently
        # optional sslrootcert/sslcert/sslkey, which forward only when the
        # connectionRef actually supplies them.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(provisionedAt=None, lastObservation={"schema": "current"}),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertIn("sslmode", create_server)
        self.assertNotIn("sslrootcert", create_server)
        self.assertNotIn("sslcert", create_server)
        self.assertNotIn("sslkey", create_server)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_does_not_mark_postgis_shippable_without_a_confirming_observation(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(provisionedAt=None, lastObservation={"schema": "current"}),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertNotIn("extensions", create_server)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, MATCHING_VERSIONS, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_marks_postgis_shippable_when_versions_match(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            # lastObservation's stored extensionVersions is stale
            # (DIFFERENT_VERSIONS, which would say "not shippable" if
            # used) — this proves the decision uses verify_remote_state's
            # live re-check above, not this stored value.
            provision_row(
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

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertIn("extensions", create_server)
        self.assertIn("postgis", create_server)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, DIFFERENT_VERSIONS, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_provision_does_not_mark_postgis_shippable_when_versions_mismatch(self, mock_versions, mock_physical_identity):
        # docs/federation-architecture-waypoint.md: pushdown is only safe
        # when PostGIS/PROJ/GEOS all match the federation database's own
        # versions — the remote merely *having* postgis is not enough.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            # lastObservation's stored extensionVersions is stale
            # (MATCHING_VERSIONS, which would say "shippable" if used) —
            # this proves the decision uses verify_remote_state's live
            # re-check above (DIFFERENT_VERSIONS), not this stored value:
            # an in-place remote extension upgrade since Observe changes
            # no OID, so the physical-identity check alone can't catch it.
            provision_row(
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

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertNotIn("extensions", create_server)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(
            SOURCE_DB_PHYSICAL_IDENTITY,
            {**MATCHING_VERSIONS, "postgisExtversion": "3.4.0"},
            True, False, None,
        ),
    )
    @patch("federation_store.extension_versions")
    def test_provision_does_not_mark_postgis_shippable_when_only_extversion_differs(
        self, mock_versions, mock_verify_remote_state
    ):
        # round 26 finding: a matching PostGIS_Lib_Version() (the linked
        # library, governing operator/function *behavior*) is not enough
        # on its own — pg_extension.extversion can still lag behind an
        # ALTER EXTENSION postgis UPDATE on one side, meaning its SQL
        # catalog may be missing a function or operator the other side's
        # newer script added. Pushing that down would fail at the SQL
        # level, not just evaluate differently, so this must also block
        # shippability even though the library versions alone match.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
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

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertNotIn("extensions", create_server)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, MATCHING_VERSIONS, True, False, "fp-new"),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_reimports_foreign_tables_when_accepting_schema_drift(
        self, mock_versions, mock_verify_remote_state
    ):
        # Accepting a drift must not just silence the warning — a foreign
        # table's columns/types are fixed at IMPORT time and never auto-
        # update, so persisting the new fingerprint without reconciling
        # the actual foreign table would leave every runtime query using
        # the stale local definition (round 22 finding).
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={"schema": "current"},
                accepted_schema_fingerprint="fp-old",
            ),
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", SOURCE_DB_URL, "admin",
            acknowledge_schema_change=True,
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertTrue(
            any(
                "DROP FOREIGN TABLE" in s and "smoke_control_orders" in s
                for s in statements
            )
        )
        self.assertTrue(any("IMPORT FOREIGN SCHEMA" in s for s in statements))
        self.assertTrue(
            any("GRANT SELECT ON ALL TABLES IN SCHEMA" in s for s in statements)
        )
        activation = next(
            call for call in cursor.execute.call_args_list
            if "accepted_schema_fingerprint = %s" in str(call.args[0])
            and "approved_by" in str(call.args[0])
        )
        self.assertEqual(("admin", False, "fp-new", SOURCE_DB_PHYSICAL_IDENTITY, "leeds_ext"), activation.args[1])

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, MATCHING_VERSIONS, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_does_not_repeat_create_ddl(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={"schema": "current"},
            ),
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

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

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_reconciles_rotated_connection_settings(self, mock_versions, mock_physical_identity):
        # The only reprovisioning path is /provision called again — it must
        # pick up a rotated password/host/database behind the same
        # connectionRef, not just re-decide the extensions option.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            # The rotation itself was already observed — this identity
            # matches the *new* endpoint, standing in for an Observe call
            # already made against it before this reprovision.
            provision_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": MATCHING_VERSIONS,
                },
                last_observed_connection_identity="reader@new-source-db:5433/sourcedb",
            ),
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext",
            "postgresql://reader:rotated-secret@new-source-db:5433/sourcedb"
            "?sslmode=require", "admin",
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

    def test_provision_rejects_an_observation_from_a_different_connection_target(self):
        # A "current" schema only proves *some* past Observe call was
        # current — not that it was taken against the endpoint this call
        # is about to provision. If the service restarted with connectionRef
        # rotated to a different host/database since that Observe, this
        # must not activate identically-named relations on an endpoint that
        # was never observed.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt=None,
                lastObservation={"schema": "current"},
                last_observed_connection_identity="reader@old-source-db:5432/sourcedb",
            ),
        ]
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext", "postgresql://reader:secret@source-db:5432/sourcedb"
            ,
                    "admin",
                )
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("CREATE EXTENSION" in s for s in statements))

    def test_reprovision_rejects_an_observation_from_a_different_connection_target(self):
        # The reprovision branch reconciles ALTER SERVER host/port/dbname
        # from whatever connectionRef resolves to right now — without this
        # check, reprovisioning right after a rotation would wire the live
        # FDW server up to a new endpoint using allowedRelations that were
        # only ever verified against the old one.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={"schema": "current"},
            ),
        ]
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext",
                "postgresql://reader:secret@new-source-db:5433/sourcedb", "admin",
            )
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("ALTER SERVER" in s for s in statements))

    def test_reprovision_rejects_an_observation_from_a_different_remote_role(self):
        # has_table_privilege and any row-level security the source
        # enforces are evaluated per connecting role — a connectionRef
        # rotated to a different remote username on the same host/port/
        # dbname is just as much a change of "what was actually observed"
        # as a host rotation, even though the endpoint string is identical.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={"schema": "current"},
            ),
        ]
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext",
                "postgresql://admin:secret@source-db:5432/sourcedb", "admin",
            )
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("ALTER SERVER" in s for s in statements))

    def test_provision_rejects_a_connection_weaker_than_the_registered_tls_policy(self):
        # tlsPolicy is validated at registration but was never enforced —
        # a registered "verify-full" alias could be provisioned over a
        # sslmode=disable connectionRef without ever being flagged.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt=None,
                lastObservation={"schema": "current"},
                tlsPolicy="verify-full",
            ),
        ]
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision(
                "leeds_ext",
                "postgresql://reader:secret@source-db:5432/sourcedb?sslmode=require", "admin",
            )
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("CREATE EXTENSION" in s for s in statements))

    def test_reprovision_rejects_a_connection_weaker_than_the_registered_tls_policy(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={"schema": "current"},
                tlsPolicy="verify-full",
            ),
        ]
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision("leeds_ext", SOURCE_DB_URL, "admin")
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("ALTER SERVER" in s for s in statements))

    @patch(
        "federation_store.verify_remote_state",
        return_value=("different-system-id/99999", {}, True, False, None),
    )
    def test_provision_rejects_a_replaced_physical_database(self, mock_physical_identity):
        # connection_identity (host/port/dbname/user) alone can't catch a
        # database dropped, restored, or replaced in place — the string
        # stays identical. This is the live re-fetch that closes that gap.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(provisionedAt=None, lastObservation={"schema": "current"}),
        ]
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision("leeds_ext", SOURCE_DB_URL, "admin")
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("CREATE EXTENSION" in s for s in statements))

    @patch(
        "federation_store.verify_remote_state",
        return_value=("different-system-id/99999", {}, True, False, None),
    )
    def test_reprovision_rejects_a_replaced_physical_database(self, mock_physical_identity):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={"schema": "current"},
            ),
        ]
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError):
            store.provision("leeds_ext", SOURCE_DB_URL, "admin")
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("ALTER SERVER" in s for s in statements))

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, "fp-1"),
    )
    @patch("federation_store.extension_versions")
    def test_provision_rejects_a_database_replaced_since_it_was_accepted(
        self, mock_versions, mock_verify_remote_state
    ):
        # The staleness check above compares against physical_identity, which
        # every Observe overwrites — so a source that was replaced and THEN
        # re-observed matches itself and passes it. Proven live: a
        # dropped-and-recreated relation kept an identical schema
        # fingerprint, cleared every other gate, and served replacement rows
        # through the existing foreign table. The durable
        # accepted_physical_identity is what actually catches it.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.return_value = provision_row(
            provisionedAt="2026-08-11T00:00:00+00:00",
            lastObservation={"schema": "current", "schemaFingerprint": "fp-1"},
            accepted_schema_fingerprint="fp-1",
            # what a prior successful provision() accepted...
            accepted_physical_identity="7672778953115078690/16384/OLD",
            # ...and what the latest Observe already recorded, so the
            # staleness check cannot see any discrepancy.
            physical_identity=SOURCE_DB_PHYSICAL_IDENTITY,
        )
        store = self.store_with_cursor(cursor)

        with self.assertRaises(FederationSchemaError) as raised:
            store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        self.assertEqual(
            "federation.physical_rebind_not_acknowledged", raised.exception.code
        )
        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertFalse(any("ALTER SERVER" in s for s in statements))

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, MATCHING_VERSIONS, True, False, "fp-1"),
    )
    @patch("federation_store.extension_versions")
    def test_provision_accepts_a_physical_rebind_once_acknowledged(
        self, mock_versions, mock_verify_remote_state
    ):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={"schema": "current", "schemaFingerprint": "fp-1"},
                accepted_schema_fingerprint="fp-1",
                accepted_physical_identity="7672778953115078690/16384/OLD",
                physical_identity=SOURCE_DB_PHYSICAL_IDENTITY,
            ),
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            {"srvoptions": ["host=source-db", "extensions=postgis"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext", SOURCE_DB_URL, "admin",
            acknowledge_physical_rebind=True,
        )

        approval = next(
            call for call in cursor.execute.call_args_list
            if "accepted_physical_identity = %s" in str(call.args[0])
        )
        # The rebound identity becomes the new durable baseline.
        self.assertIn(SOURCE_DB_PHYSICAL_IDENTITY, approval.args[1])

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, "fp-1"),
    )
    @patch("federation_store.extension_versions")
    def test_first_provisioning_has_no_accepted_identity_to_compare_against(
        self, mock_versions, mock_verify_remote_state
    ):
        # accepted_physical_identity is None until a provision() sets it, so
        # the rebind gate must be vacuous on the first call rather than
        # blocking every new alias.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt=None,
                lastObservation={"schema": "current", "schemaFingerprint": "fp-1"},
                accepted_physical_identity=None,
            ),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        result = store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        self.assertIsNotNone(result["provisionedAt"])

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, "fp-1"),
    )
    @patch("federation_store.extension_versions")
    def test_provision_forwards_hostaddr_to_the_foreign_server(
        self, mock_versions, mock_verify_remote_state
    ):
        # When a connectionRef pins hostaddr, Observe and verify_remote_state
        # connect to THAT address. A foreign server built from host alone can
        # resolve somewhere else entirely, so the identity, schema, and
        # privileges that were just verified would describe a different
        # database than the one runtime queries actually reach.
        mock_versions.return_value = {}
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
                provisionedAt=None,
                lastObservation={"schema": "current", "schemaFingerprint": "fp-1"},
                last_observed_connection_identity="reader@source-db:5432/sourcedb",
            ),
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        cursor.fetchall.return_value = [{"relname": "smoke_control_orders"}]
        store = self.store_with_cursor(cursor)

        store.provision(
            "leeds_ext",
            "postgresql://reader:secret@source-db:5432/sourcedb"
            "?sslmode=require&hostaddr=10.1.2.3",
            "admin",
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        create_server = next(s for s in statements if "CREATE SERVER" in s)
        self.assertIn("hostaddr", create_server)
        self.assertIn("10.1.2.3", create_server)
        # host is still sent: with hostaddr present libpq uses host purely
        # for TLS name verification.
        self.assertIn("source-db", create_server)

    def test_provision_wraps_a_physical_identity_connection_failure(self):
        # A source that goes unreachable in the narrow window between the
        # connection-identity check and this live re-fetch must fail
        # closed with a clear, actionable error — not a raw psycopg
        # exception leaking out of the store.
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(provisionedAt=None, lastObservation={"schema": "current"}),
        ]
        store = self.store_with_cursor(cursor)

        with patch(
            "federation_store.verify_remote_state",
            side_effect=federation_store.psycopg.OperationalError("connection refused"),
        ):
            with self.assertRaises(FederationSchemaError):
                store.provision("leeds_ext", SOURCE_DB_URL, "admin")

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_adds_a_newly_configured_ssl_option(self, mock_versions, mock_physical_identity):
        # An operator tightening sslmode from disable to require/verify-
        # full behind the same connectionRef must reach the live server,
        # not just host/port/dbname/credentials.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
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
            "postgresql://reader:secret@source-db:5432/sourcedb?sslmode=verify-full", "admin",
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        ssl_alter = next(s for s in statements if "ALTER SERVER" in s and "sslmode" in s)
        self.assertIn("ADD", ssl_alter)
        self.assertIn("verify-full", ssl_alter)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_updates_a_changed_ssl_option(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
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
            "postgresql://reader:secret@source-db:5432/sourcedb?sslmode=verify-full", "admin",
        )

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        ssl_alter = next(s for s in statements if "ALTER SERVER" in s and "sslmode" in s)
        self.assertIn("SET", ssl_alter)
        self.assertIn("verify-full", ssl_alter)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_drops_a_removed_ssl_option(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
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

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        ssl_alter = next(s for s in statements if "ALTER SERVER" in s and "sslrootcert" in s)
        self.assertIn("DROP", ssl_alter)

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, {}, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_creates_a_missing_reader_mapping(self, mock_versions, mock_physical_identity):
        # An alias provisioned before the reader-mapping fix existed has no
        # mapping for mapp_xyz yet — ALTER would fail on it, so this must
        # be DROP IF EXISTS + CREATE, not ALTER, to converge such an alias
        # the next time it's reprovisioned.
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            provision_row(
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

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

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

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, MATCHING_VERSIONS, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_enables_pushdown_once_versions_now_match(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            # lastObservation's stored extensionVersions is stale
            # (DIFFERENT_VERSIONS, i.e. mismatched as of the last Observe)
            # — verify_remote_state's live re-check above is what actually
            # says they now match, proving the decision uses that, not
            # this stored value.
            provision_row(
                provisionedAt="2026-08-11T00:00:00+00:00",
                lastObservation={
                    "schema": "current",
                    "extensionVersions": DIFFERENT_VERSIONS,
                },
            ),
            {"srvoptions": ["host=source-db"]},
            {"srvoptions": ["host=source-db"]},
            alias_row(provisionedAt="2026-08-11T00:00:00+00:00"),
        ]
        store = self.store_with_cursor(cursor)

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertTrue(any("ALTER SERVER" in s and "ADD extensions" in s and "postgis" in s for s in statements))
        self.assertFalse(any("DROP extensions" in s for s in statements))

    @patch(
        "federation_store.verify_remote_state",
        return_value=(SOURCE_DB_PHYSICAL_IDENTITY, DIFFERENT_VERSIONS, True, False, None),
    )
    @patch("federation_store.extension_versions")
    def test_reprovision_disables_pushdown_when_versions_now_mismatch(self, mock_versions, mock_physical_identity):
        mock_versions.return_value = MATCHING_VERSIONS
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            # lastObservation's stored extensionVersions is stale
            # (MATCHING_VERSIONS, i.e. matched as of the last Observe) —
            # verify_remote_state's live re-check above (an in-place
            # remote extension upgrade changes no OID, so physical
            # identity alone wouldn't catch this) is what actually says
            # they no longer match.
            provision_row(
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

        store.provision("leeds_ext", SOURCE_DB_URL, "admin")

        statements = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertTrue(any("ALTER SERVER" in s and "DROP extensions" in s for s in statements))
        self.assertFalse(any("ADD extensions" in s for s in statements))

    def test_affected_derived_layer_names_queries_by_local_fdw_schema(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {"oid": 12345}
        cursor.fetchall.return_value = [{"name": "smoke_control_area_h3_r9_ext"}]
        store = self.store_with_cursor(cursor)

        result = store.affected_derived_layer_names("leeds_ext")

        self.assertEqual(["smoke_control_area_h3_r9_ext"], result)
        query = cursor.execute.call_args_list[1]
        self.assertIn("derived_layers", str(query.args[0]))
        self.assertEqual(("source_leeds_ext",), query.args[1])

    def test_affected_derived_layer_names_is_empty_when_definitions_table_is_absent(self):
        # derived_layers._definitions is created lazily on first use — a
        # fresh deployment that registered an alias but never touched a
        # derived-layer endpoint has no such table yet. No table means no
        # rows could possibly depend on this alias, not an error.
        cursor = MagicMock()
        cursor.fetchone.return_value = {"oid": None}
        store = self.store_with_cursor(cursor)

        result = store.affected_derived_layer_names("leeds_ext")

        self.assertEqual([], result)
        cursor.fetchall.assert_not_called()


if __name__ == "__main__":
    unittest.main()
