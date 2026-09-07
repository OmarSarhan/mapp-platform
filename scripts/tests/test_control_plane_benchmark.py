"""The benchmark's invariants, run small enough for CI.

The benchmark itself is evidence: its throughput figures depend on the machine
and nothing asserts them. But the properties it checks along the way are not
machine-dependent -- exactly one winner per single-use record, a per-source
flood that denies only the flooder -- and those belong in a suite rather than
in a script somebody remembers to run.

Kept deliberately small. The point is that the scenarios still hold, not that
they hold at scale; scale is what the script is for.
"""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.getenv("CONTROL_TEST_DATABASE_URL", "")

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - exercised by the skip below
    psycopg = None


def load_benchmark():
    spec = importlib.util.spec_from_file_location(
        "control_plane_benchmark", ROOT / "scripts" / "control_plane_benchmark.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchmarkImportTests(unittest.TestCase):
    """It must at least import, or nobody discovers it is broken until the gate."""

    def test_the_script_imports_and_declares_its_scenarios(self) -> None:
        try:
            benchmark = load_benchmark()
        except ModuleNotFoundError as exc:  # pragma: no cover - deps absent
            self.skipTest(f"benchmark dependencies are absent: {exc}")
        for name in (
            "replay_contention",
            "exchange_throughput",
            "approval_admission",
            "cleanup_under_load",
            "connection_ceiling",
        ):
            with self.subTest(scenario=name):
                self.assertTrue(callable(getattr(benchmark, name)))

    def test_the_documented_connection_limit_matches_the_role_definition(self) -> None:
        """The benchmark measures the deployed ceiling, so it must be the same one.

        A benchmark that saturates a limit the platform does not use would
        report a capacity nobody has.
        """
        try:
            benchmark = load_benchmark()
        except ModuleNotFoundError as exc:  # pragma: no cover
            self.skipTest(f"benchmark dependencies are absent: {exc}")
        roles = (ROOT / "docker/postgis/init/10-roles.sh").read_text()
        self.assertIn(
            f'CONNECTION LIMIT {benchmark.DEPLOYED_CONNECTION_LIMIT};',
            roles.replace('ALTER ROLE :"control_db_user" ', ""),
        )


@unittest.skipUnless(
    DATABASE_URL and psycopg is not None,
    "set CONTROL_TEST_DATABASE_URL to a scratch PostgreSQL database",
)
class BenchmarkInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.benchmark = load_benchmark()
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"benchmark dependencies are absent: {exc}")
        connection = cls.benchmark.cs.connect(DATABASE_URL)
        try:
            cls.benchmark.cs.migrate(connection)
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.benchmark.truncate(DATABASE_URL)

    def test_it_refuses_an_initialized_platform(self) -> None:
        """The DSN comes from an environment variable, so this must fail loudly.

        Every scenario deletes all rows from six control tables. Against a live
        platform that destroys every grant, token and OAuth client it holds.
        An initialized platform has an administrator credential; a scratch
        database does not.
        """
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute(
                "INSERT INTO control.admin_credential(id, encoded)"
                " VALUES(1, 'pbkdf2-sha256$1$x$y')"
                " ON CONFLICT (id) DO UPDATE SET encoded = EXCLUDED.encoded"
            )
        try:
            with self.assertRaises(self.benchmark.RefusedDestructiveRun):
                self.benchmark.guard_scratch_database(DATABASE_URL)
            # --force-destructive is the deliberate override, and nothing else is.
            self.benchmark.guard_scratch_database(DATABASE_URL, force=True)
        finally:
            with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
                connection.execute("DELETE FROM control.admin_credential")

    def test_it_permits_a_scratch_database(self) -> None:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            connection.execute("DELETE FROM control.admin_credential")
        self.benchmark.guard_scratch_database(DATABASE_URL)

    def test_the_guard_never_touches_the_credential_tables(self) -> None:
        """truncate must not be the thing that makes the guard pass next time.

        A benchmark that cleared admin_credential would disarm its own guard,
        and would also break the deployment-helper suite that shares this
        scratch database and asserts on what init writes.
        """
        self.assertNotIn("admin_credential", self.benchmark.TABLES)
        self.assertNotIn("metadata", self.benchmark.TABLES)

    def test_simultaneous_presentations_yield_one_winner(self) -> None:
        result = self.benchmark.replay_contention(
            DATABASE_URL, presenters=4, trials=2
        )
        self.assertEqual([1], result["winners_per_trial"])

    def test_a_flood_denies_only_the_flooder(self) -> None:
        result = self.benchmark.approval_admission(DATABASE_URL)
        self.assertEqual(result["per_source_cap"], result["admitted"])
        self.assertTrue(result["other_source_still_admitted"])

    def test_distinct_tokens_each_spend_once(self) -> None:
        result = self.benchmark.exchange_throughput(
            DATABASE_URL, workers=4, tokens=8
        )
        self.assertEqual(0, result["failures"])

    def test_cleanup_removes_no_live_record(self) -> None:
        result = self.benchmark.cleanup_under_load(
            DATABASE_URL, writers=2, sweeps=2
        )
        self.assertGreaterEqual(result["live_survivors"], 100)

    def test_the_connection_ceiling_refuses_and_recovers(self) -> None:
        """Measured against a role capped the way the deployed one is.

        A connection-per-operation store turns a burst past the cap into
        refusals rather than queuing, and the refusal is PostgreSQL's. Worth
        pinning because the alternative reading -- that the budget is headroom
        above two pools -- was in a comment for a while and was wrong.
        """
        result = self.benchmark.connection_ceiling(DATABASE_URL, limit=3)
        self.assertEqual(3, result["connections_opened"])
        self.assertIn("too many connections", result["refusal"])
        self.assertTrue(result["recovers_after_release"])


if __name__ == "__main__":
    unittest.main()
