import unittest
from datetime import datetime
from unittest.mock import patch

import psycopg

from federation_capability import detect_capability, physical_identity
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


def extension_and_rls_results(*, postgis=True, rls=False, relation_count=1):
    results = [{"version": "16.2"}]
    if postgis:
        results.append({"exists": 1})
        results.append({"version": "3.4.2"})
        results.append({"version": "9.3.1"})
        results.append({"version": "3.12.1"})
    else:
        results.append(None)
    results.extend({"bypasses_per_user_access": rls} for _ in range(relation_count))
    return results


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
            extension_and_rls_results() + physical_identity_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ) as mock_connect:
            observation, observed_at, physical_id = detect_capability(
                "postgresql://reader?sslmode=require",
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        # Captured once the connection succeeds, fixing this probe's
        # REPEATABLE READ snapshot — not before the connection attempt,
        # which record_observation()'s race protection depends on.
        self.assertIsInstance(observed_at, datetime)
        self.assertEqual("reachable", observation["connectivity"])
        self.assertEqual("current", observation["schema"])
        self.assertEqual("unknown", observation["sourceFreshness"])
        self.assertIsNone(observation["sourceVersion"])
        self.assertEqual(
            {
                "postgresql": "16.2",
                "postgis": "3.4.2",
                "proj": "9.3.1",
                "geos": "3.12.1",
            },
            observation["extensionVersions"],
        )
        self.assertFalse(observation["rowLevelSecurityDetected"])
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
            extension_and_rls_results(postgis=False) + physical_identity_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            observation, _, _ = detect_capability(
                "postgresql://reader?sslmode=require",
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
            {"exists": 1},
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
                "postgresql://reader?sslmode=require",
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        self.assertEqual("reachable", observation["connectivity"])
        self.assertEqual("changed", observation["schema"])
        self.assertFalse(observation["rowLevelSecurityDetected"])
        # Physical identity is still collected even when the schema check
        # itself found the relation missing — the identity's own "missing"
        # marker is what carries that fact forward, not a skipped call.
        self.assertEqual("7672778953115078690/16384/missing", physical_id)

    def test_detects_row_level_security(self):
        cursor = ScriptedFakeCursor(
            extension_and_rls_results(rls=True) + physical_identity_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            observation, _, _ = detect_capability(
                "postgresql://reader?sslmode=require",
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        self.assertTrue(observation["rowLevelSecurityDetected"])

    def test_detects_a_security_barrier_view_without_native_rls(self):
        # docs/federation-architecture-waypoint.md: the same per-user
        # bypass risk applies to a security-barrier view (a common way to
        # implement per-user row filtering without native RLS) as to
        # relrowsecurity — the query combines both signals.
        cursor = ScriptedFakeCursor(
            extension_and_rls_results(rls=True) + physical_identity_results()
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            observation, _, _ = detect_capability(
                "postgresql://reader?sslmode=require",
                allowed_relations=("leeds.tenant_scoped_view",),
                tls_policy="require",
            )

        self.assertTrue(observation["rowLevelSecurityDetected"])
        executed_sql = "\n".join(query for query, _ in cursor.executed)
        self.assertIn("security_barrier", executed_sql)

    def test_connectivity_failure_is_reported_not_raised(self):
        with patch(
            "federation_capability.psycopg.connect",
            side_effect=psycopg.OperationalError("could not connect"),
        ):
            observation, observed_at, physical_id = detect_capability(
                "postgresql://unreachable?sslmode=require",
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
        # moment the connection was given up as failed.
        self.assertIsInstance(observed_at, datetime)
        self.assertIsNone(physical_id)

    def test_authentication_failure_is_reported_distinctly_from_an_outage(self):
        # A rejected credential needs MAPP-side secret rotation, not a
        # wait, and must never be conflated with "unreachable" (docs/
        # federation-architecture-waypoint.md, "Drift and retirement").
        # Postgres uses the exact same FATAL message for a wrong password
        # and for a nonexistent role, to avoid confirming which usernames
        # exist — both cases are "unauthorized" here.
        with patch(
            "federation_capability.psycopg.connect",
            side_effect=psycopg.OperationalError(
                'connection failed: connection to server at "10.0.0.5", '
                'port 5432 failed: FATAL:  password authentication failed '
                'for user "reader"'
            ),
        ):
            observation, _, _ = detect_capability(
                "postgresql://reader?sslmode=require",
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
            )

        self.assertEqual(
            {
                "connectivity": "unauthorized",
                "schema": "unknown",
                "sourceFreshness": "unknown",
                "lastConnected": None,
                "lastSchemaVerified": None,
                "sourceVersion": None,
            },
            observation,
        )

    def test_rejects_a_connection_weaker_than_the_registered_tls_policy(self):
        # Enforced before ever connecting — a weak connectionRef must not
        # even attempt to Observe, matching Provision's enforcement.
        with patch("federation_capability.psycopg.connect") as mock_connect:
            with self.assertRaises(FederationSchemaError):
                detect_capability(
                    "postgresql://reader?sslmode=disable",
                    allowed_relations=("leeds.bus_stops",),
                    tls_policy="verify-full",
                )
        mock_connect.assert_not_called()

    def test_reads_a_configured_version_relation_scalar(self):
        cursor = ScriptedFakeCursor(
            extension_and_rls_results(relation_count=2)
            + physical_identity_results(relation_oids=(24601, 24602)),
            fetchall_results=[[{"release_id": "release-42"}]],
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            observation, _, _ = detect_capability(
                "postgresql://reader?sslmode=require",
                allowed_relations=("leeds.bus_stops", "leeds.dataset_publication"),
                tls_policy="require",
                version_relation="leeds.dataset_publication",
            )

        self.assertEqual("release-42", observation["sourceVersion"])

    def test_rejects_a_version_relation_not_on_the_allowlist(self):
        with self.assertRaises(FederationSchemaError):
            detect_capability(
                "postgresql://reader?sslmode=require",
                allowed_relations=("leeds.bus_stops",),
                tls_policy="require",
                version_relation="leeds.dataset_publication",
            )

    def test_rejects_a_version_relation_returning_more_than_one_row(self):
        cursor = ScriptedFakeCursor(
            extension_and_rls_results(relation_count=2),
            fetchall_results=[[{"release_id": "a"}, {"release_id": "b"}]],
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            with self.assertRaises(FederationSchemaError):
                detect_capability(
                    "postgresql://reader?sslmode=require",
                    allowed_relations=(
                        "leeds.bus_stops",
                        "leeds.dataset_publication",
                    ),
                    tls_policy="require",
                    version_relation="leeds.dataset_publication",
                )

    def test_rejects_a_version_relation_returning_more_than_one_column(self):
        cursor = ScriptedFakeCursor(
            extension_and_rls_results(relation_count=2),
            fetchall_results=[[{"release_id": "a", "schema_version": 1}]],
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            with self.assertRaises(FederationSchemaError):
                detect_capability(
                    "postgresql://reader?sslmode=require",
                    allowed_relations=(
                        "leeds.bus_stops",
                        "leeds.dataset_publication",
                    ),
                    tls_policy="require",
                    version_relation="leeds.dataset_publication",
                )


class PhysicalIdentityTests(unittest.TestCase):
    def test_combines_the_cluster_database_and_relation_identity(self):
        cursor = ScriptedFakeCursor(physical_identity_results())
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            identity = physical_identity(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertEqual("7672778953115078690/16384/24601", identity)

    def test_differs_when_the_database_was_dropped_and_recreated(self):
        # Same cluster (system_identifier unchanged), different database
        # oid — a DROP DATABASE + CREATE DATABASE within the same cluster.
        cursor = ScriptedFakeCursor([
            {"system_identifier": 7672778953115078690},
            {"oid": 99999},
            {"oid": 24601},
        ])
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            identity = physical_identity(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertNotEqual("7672778953115078690/16384/24601", identity)

    def test_differs_when_a_relation_was_recreated(self):
        # Same cluster and database — a physical/PITR restore, or an
        # in-place logical restore that never drops the database, changes
        # neither. Any restore recreating the relation itself (as
        # pg_restore --clean does) gives it a new oid, which this must
        # catch even when nothing at the database level changed.
        cursor = ScriptedFakeCursor(physical_identity_results(relation_oids=(99999,)))
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            identity = physical_identity(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertNotEqual("7672778953115078690/16384/24601", identity)

    def test_treats_a_missing_relation_as_a_changed_identity(self):
        # A relation that's gone entirely must not be silently skipped —
        # fail closed the same direction _verify_allowed_relations already
        # does for a missing/unselectable relation.
        cursor = ScriptedFakeCursor(physical_identity_results(relation_oids=(None,)))
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            identity = physical_identity(
                "postgresql://reader", ("leeds.bus_stops",)
            )

        self.assertEqual("7672778953115078690/16384/missing", identity)

    def test_orders_multiple_relations_by_the_given_sequence(self):
        cursor = ScriptedFakeCursor(
            physical_identity_results(relation_oids=(111, 222))
        )
        with patch(
            "federation_capability.psycopg.connect",
            return_value=ScriptedFakeConnection(cursor),
        ):
            identity = physical_identity(
                "postgresql://reader",
                ("leeds.bus_stops", "leeds.roads"),
            )

        self.assertEqual("7672778953115078690/16384/111,222", identity)

    def test_propagates_a_connection_failure(self):
        with patch(
            "federation_capability.psycopg.connect",
            side_effect=psycopg.OperationalError("could not connect"),
        ):
            with self.assertRaises(psycopg.Error):
                physical_identity("postgresql://unreachable", ())


if __name__ == "__main__":
    unittest.main()
