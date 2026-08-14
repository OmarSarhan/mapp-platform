import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import psycopg

import federation_capability
from federation_capability import detect_capability, verify_remote_state
from federation_schema import FederationSchemaError


class ScriptedFakeCursor:
    def __init__(self, fetchone_results, fetchall_results=None):
        self.fetchone_results = list(fetchone_results)
        self.fetchall_results = list(fetchall_results or [])
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None):
        rendered = (
            statement if isinstance(statement, str) else statement.as_string()
        )
        self.executed.append((rendered, params))

    def fetchone(self):
        return self.fetchone_results.pop(0)

    def fetchall(self):
        return self.fetchall_results.pop(0)


class ScriptedFakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


TLS_URL = (
    "postgresql://reader:secret@host/source?sslmode=require&gssencmode=disable"
)


def extension_version_results(*, postgis=True):
    results = [{"version": "16.2"}]
    if postgis:
        results.append({"extversion": "3.4.2"})
        results.append({"version": "3.4.2"})
        results.append({"version": "9.3.1"})
        results.append({"version": "3.12.1"})
    else:
        results.append(None)
    return results


def relation_check_results(*, rls=False, fingerprints=("fp-bus-stops",)):
    return [
        {"bypasses_per_user_access": rls, "definition_fingerprint": fp}
        for fp in fingerprints
    ]


def extension_and_rls_results(
    *, postgis=True, rls=False, fingerprints=("fp-bus-stops",)
):
    return extension_version_results(postgis=postgis) + relation_check_results(
        rls=rls, fingerprints=fingerprints
    )


def physical_identity_results(relation_oids=(24601,)):
    results = [
        {"system_identifier": 7672778953115078690},
        {"oid": 16384},
    ]
    results.extend(
        ({"oid": oid} if oid is not None else None) for oid in relation_oids
    )
    return results


class DetectCapabilityTests(unittest.TestCase):
    def test_reports_reachable_with_full_extension_versions(self):
        cursor = ScriptedFakeCursor(
            extension_and_rls_results()
            + physical_identity_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ) as mock_connect:
            observation, observed_at, physical_id = detect_capability(
                TLS_URL,
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        self.assertIsInstance(observed_at, datetime)
        self.assertIsNotNone(observed_at.tzinfo)
        self.assertEqual("reachable", observation["connectivity"])
        self.assertEqual("current", observation["schema"])
        self.assertEqual("unknown", observation["sourceFreshness"])
        self.assertIsNone(observation["sourceVersion"])
        self.assertEqual(
            {
                "postgresql": "16.2",
                "postgis": "3.4.2",
                "postgisExtversion": "3.4.2",
                "proj": "9.3.1",
                "geos": "3.12.1",
            },
            observation["extensionVersions"],
        )
        self.assertFalse(observation["rowLevelSecurityDetected"])
        self.assertEqual("fp-bus-stops", observation["schemaFingerprint"])
        self.assertIsNotNone(observation["lastConnected"])
        self.assertIsNotNone(observation["lastSchemaVerified"])
        # The physical identity must come from this same probe's snapshot,
        # not a second connection — a relation dropped and recreated
        # between two separate connections would let a physical identity
        # for the *new* relation get paired with schema evidence that only
        # ever verified the *old* one.
        self.assertEqual("7672778953115078690/16384/24601", physical_id)
        self.assertEqual(1, mock_connect.call_count)

    def test_reports_no_postgis_without_probing_proj_or_geos(self):
        cursor = ScriptedFakeCursor(
            extension_and_rls_results(postgis=False)
            + physical_identity_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            observation, _, _ = detect_capability(
                TLS_URL,
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        self.assertEqual({"postgresql": "16.2"}, observation["extensionVersions"])
        executed_sql = "\n".join(query for query, _ in cursor.executed)
        self.assertNotIn("PostGIS_PROJ_Version", executed_sql)
        self.assertNotIn("PostGIS_GEOS_Version", executed_sql)

    def test_reports_schema_changed_when_a_relation_is_missing(self):
        # A relation that no longer exists (or the reader can no longer
        # SELECT) must not be silently skipped — the schema Discover
        # verified no longer matches what was registered.
        cursor = ScriptedFakeCursor([
            {"version": "16.2"},
            {"extversion": "3.4.2"},
            {"version": "3.4.2"},
            {"version": "9.3.1"},
            {"version": "3.12.1"},
            None,
        ] + physical_identity_results(relation_oids=(None,)))
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            observation, _, physical_id = detect_capability(
                TLS_URL,
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        self.assertEqual("reachable", observation["connectivity"])
        self.assertEqual("changed", observation["schema"])
        self.assertFalse(observation["rowLevelSecurityDetected"])
        # A relation gone missing must change the fingerprint too, not be
        # silently skipped — the same fail-closed direction as all_present.
        self.assertEqual("missing", observation["schemaFingerprint"])
        # Physical identity is still collected even when the schema check
        # itself found the relation missing — the identity's own "missing"
        # marker is what carries that fact forward, not a skipped call.
        self.assertEqual("7672778953115078690/16384/missing", physical_id)

    def test_detects_row_level_security(self):
        cursor = ScriptedFakeCursor(
            extension_and_rls_results(rls=True)
            + physical_identity_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            observation, _, _ = detect_capability(
                TLS_URL,
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        self.assertTrue(observation["rowLevelSecurityDetected"])

    def test_fingerprint_covers_schema_and_access_control(self):
        cursor = ScriptedFakeCursor(
            extension_and_rls_results()
            + physical_identity_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            detect_capability(
                TLS_URL,
                allowed_relations=("leeds.tenant_scoped_view",),
                tls_policy="require",
            )

        executed_sql = "\n".join(query for query, _ in cursor.executed)
        for fragment in (
            "jsonb_build_object",
            "sha256(convert_to",
            "format_type(a.atttypid, a.atttypmod)",
            "jsonb_build_array(cn.nspname, co.collname)",
            "c.relkind",
            "pg_get_userbyid(c.relowner)",
            "c.relrowsecurity",
            "c.relforcerowsecurity",
            "pg_catalog.row_security_active(c.oid)",
            "r.rolsuper OR r.rolbypassrls",
            "currentRoleOwnsRelation",
            "appliesToCurrentRole",
            "security_barrier=true",
            "security_invoker=true",
            "pg_catalog.pg_policy",
            "p.polcmd",
            "p.polpermissive",
            "p.polroles",
            "pg_get_expr(p.polqual",
            "p.polwithcheck",
            "c.relkind IN ('r', 'p', 'v', 'm')",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, executed_sql)

    def test_combines_multiple_relations_into_one_ordered_fingerprint(self):
        cursor = ScriptedFakeCursor(
            extension_and_rls_results(fingerprints=("fp-a", "fp-b"))
            + physical_identity_results(relation_oids=(24601, 24602)),
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            observation, _, _ = detect_capability(
                TLS_URL,
                allowed_relations=("leeds.bus_stops", "leeds.roads"),
                tls_policy="require",
            )

        # Ordered by the given allowed_relations sequence, matching
        # physical_identity's own ordering convention.
        self.assertEqual("fp-a|fp-b", observation["schemaFingerprint"])

    def test_connectivity_failure_is_reported_not_raised(self):
        with patch(
            "federation_capability.psycopg.connect",
            side_effect=psycopg.OperationalError("could not connect"),
        ):
            observation, observed_at, physical_id = detect_capability(
                "postgresql://reader:secret@unreachable/source?"
                "sslmode=require&gssencmode=disable",
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        self.assertEqual(
            {
                "connectivity": "unavailable",
                "schema": "unknown",
                "sourceFreshness": "unknown",
                "lastConnected": None,
                "lastSchemaVerified": None,
                "sourceVersion": None,
            },
            observation,
        )
        # Even a failed probe gets an ordering marker — captured at the
        # moment the connection was given up as failed. No server-side
        # clock is reachable here, so this is the one case where a
        # client-side timestamp is unavoidable.
        self.assertIsInstance(observed_at, datetime)
        self.assertIsNone(physical_id)

    def test_query_failure_preserves_successful_connectivity(self):
        cursor = ScriptedFakeCursor([])
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ), patch(
            "federation_capability.extension_versions",
            side_effect=psycopg.errors.InsufficientPrivilege("denied"),
        ):
            observation, _, physical_id = detect_capability(
                TLS_URL,
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        self.assertEqual("reachable", observation["connectivity"])
        self.assertEqual("unknown", observation["schema"])
        self.assertIsNotNone(observation["lastConnected"])
        self.assertIsNone(observation["lastSchemaVerified"])
        self.assertIsNone(physical_id)

    def test_authentication_failures_are_reported_distinctly_from_outages(self):
        failures = (
            psycopg.errors.InvalidAuthorizationSpecification(
                "certificate authentication rejected"
            ),
            psycopg.OperationalError("PAM authentication failed"),
            psycopg.OperationalError("no pg_hba.conf entry for host"),
        )
        for failure in failures:
            with self.subTest(failure=failure), patch(
                "federation_capability.psycopg.connect", side_effect=failure
            ):
                observation, _, _ = detect_capability(
                    TLS_URL,
                    allowed_relations=("leeds.bus_stops",),
                    tls_policy="require",
                )
            self.assertEqual("unauthorized", observation["connectivity"])

    def test_rejects_a_connection_weaker_than_the_registered_tls_policy(self):
        # Enforced before ever connecting — a weak connectionRef must not
        # even attempt to Observe, matching Provision's enforcement.
        with patch("federation_capability.psycopg.connect") as mock_connect:
            with self.assertRaises(FederationSchemaError):
                detect_capability(
                    "postgresql://reader:secret@host/source?"
                    "sslmode=disable&gssencmode=disable",
                    allowed_relations=("leeds.bus_stops",),
                    tls_policy="verify-full",
                )
        mock_connect.assert_not_called()


class VerifyRemoteStateTests(unittest.TestCase):
    """Provision's own live re-check — physical identity, extension
    versions, relation existence/selectability, RLS/security-barrier
    exposure, and schema fingerprint, all gathered from the one connection
    this opens."""

    def test_combines_the_cluster_database_and_relation_identity(self):
        cursor = ScriptedFakeCursor(
            physical_identity_results()
            + extension_version_results()
            + relation_check_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            (
                identity,
                versions,
                relations_verified,
                rls_detected,
                schema_fingerprint,
            ) = verify_remote_state("postgresql://reader", ("leeds.bus_stops",))

        self.assertEqual("7672778953115078690/16384/24601", identity)
        self.assertEqual(
            {
                "postgresql": "16.2",
                "postgis": "3.4.2",
                "postgisExtversion": "3.4.2",
                "proj": "9.3.1",
                "geos": "3.12.1",
            },
            versions,
        )
        self.assertTrue(relations_verified)
        self.assertFalse(rls_detected)
        self.assertEqual("fp-bus-stops", schema_fingerprint)

    def test_differs_when_the_database_was_dropped_and_recreated(self):
        # Same cluster (system_identifier unchanged), different database
        # oid — a DROP DATABASE + CREATE DATABASE within the same cluster.
        cursor = ScriptedFakeCursor(
            [
                {"system_identifier": 7672778953115078690},
                {"oid": 99999},
                {"oid": 24601},
            ]
            + extension_version_results()
            + relation_check_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            identity, *_ = verify_remote_state(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertNotEqual("7672778953115078690/16384/24601", identity)

    def test_differs_when_a_relation_was_recreated(self):
        # Same cluster and database — a physical/PITR restore, or an
        # in-place logical restore that never drops the database, changes
        # neither. Any restore recreating the relation itself (as
        # pg_restore --clean does) gives it a new oid, which this must
        # catch even when nothing at the database level changed.
        cursor = ScriptedFakeCursor(
            physical_identity_results(relation_oids=(99999,))
            + extension_version_results()
            + relation_check_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            identity, *_ = verify_remote_state(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertNotEqual("7672778953115078690/16384/24601", identity)

    def test_treats_a_missing_relation_as_a_changed_identity(self):
        # A relation that's gone entirely must not be silently skipped —
        # fail closed the same direction _verify_allowed_relations already
        # does for a missing/unselectable relation.
        cursor = ScriptedFakeCursor(
            physical_identity_results(relation_oids=(None,))
            + extension_version_results()
            + relation_check_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            identity, *_ = verify_remote_state(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertEqual("7672778953115078690/16384/missing", identity)

    def test_orders_multiple_relations_by_the_given_sequence(self):
        cursor = ScriptedFakeCursor(
            physical_identity_results(relation_oids=(111, 222))
            + extension_version_results()
            + relation_check_results(fingerprints=("fp-a", "fp-b"))
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            identity, *_ = verify_remote_state(
                "postgresql://reader",
                ("leeds.bus_stops", "leeds.roads"),
            )

        self.assertEqual("7672778953115078690/16384/111,222", identity)

    def test_reports_relations_not_verified_when_one_is_missing_or_unselectable(self):
        cursor = ScriptedFakeCursor(
            physical_identity_results()
            + extension_version_results()
            + [None]
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            _, _, relations_verified, _, schema_fingerprint = verify_remote_state(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertFalse(relations_verified)
        self.assertEqual("missing", schema_fingerprint)

    def test_reports_relations_not_verified_when_present_but_unselectable(self):
        # The relation exists in pg_class (a real row is returned, unlike
        # the "missing" case above) but _selectable()'s actual SELECT
        # fails — e.g. a security_invoker view whose underlying relation
        # the connecting role can't read. Patching _selectable() directly
        # (rather than re-deriving its SAVEPOINT mechanics here) proves
        # _verify_allowed_relations() actually wires its result into the
        # overall verdict, not just that _selectable() itself works in
        # isolation (see SelectableTests).
        cursor = ScriptedFakeCursor(
            physical_identity_results()
            + extension_version_results()
            + relation_check_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ), patch(
            "federation_capability._selectable", return_value=False
        ):
            _, _, relations_verified, _, schema_fingerprint = verify_remote_state(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertFalse(relations_verified)
        self.assertEqual("missing", schema_fingerprint)

    def test_reports_row_level_security_detection(self):
        cursor = ScriptedFakeCursor(
            physical_identity_results()
            + extension_version_results()
            + relation_check_results(rls=True)
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            *_, rls_detected, _ = verify_remote_state(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertTrue(rls_detected)

    def test_propagates_a_connection_failure(self):
        with patch(
            "federation_capability.psycopg.connect",
            side_effect=psycopg.OperationalError("could not connect"),
        ):
            with self.assertRaises(psycopg.Error):
                verify_remote_state("postgresql://unreachable", ())


class SelectableTests(unittest.TestCase):
    def test_returns_true_when_the_select_succeeds(self):
        cursor = MagicMock()
        self.assertTrue(
            federation_capability._selectable(cursor, "leeds", "bus_stops")
        )
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual("SAVEPOINT relation_selectable", statements[0])
        self.assertIn("SELECT * FROM", str(statements[1]))
        self.assertEqual("RELEASE SAVEPOINT relation_selectable", statements[-1])

    def test_returns_false_and_still_rolls_back_when_the_select_is_denied(self):
        cursor = MagicMock()

        def execute(statement, *args, **kwargs):
            if "SELECT * FROM" in str(statement):
                raise psycopg.errors.InsufficientPrivilege(
                    "permission denied for table bus_stops"
                )

        cursor.execute.side_effect = execute

        self.assertFalse(
            federation_capability._selectable(cursor, "leeds", "bus_stops")
        )
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual("SAVEPOINT relation_selectable", statements[0])
        self.assertEqual(
            "ROLLBACK TO SAVEPOINT relation_selectable", statements[-2]
        )
        self.assertEqual("RELEASE SAVEPOINT relation_selectable", statements[-1])


if __name__ == "__main__":
    unittest.main()
