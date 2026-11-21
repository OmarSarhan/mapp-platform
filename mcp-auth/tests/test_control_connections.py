import os
import multiprocessing
import secrets
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

import psycopg
from psycopg import sql

from control_connections import AcquisitionError, AdmissionTimeout, CAPACITY, ControlConnections, request_scope


def hold_process(dsn, ready, release):
    pool = ControlConnections()
    def hold():
        with pool.connection(dsn) as conn:
            ready.put(conn.info.backend_pid)
            release.wait(10)
    threads = [threading.Thread(target=hold) for _ in range(CAPACITY)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    pool.close()


class PackagingTests(unittest.TestCase):
    def test_service_copies_match(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual((root / "config-ui/control_connections.py").read_bytes(),
                         (root / "mcp-auth/control_connections.py").read_bytes())


@unittest.skipUnless(os.getenv("CONTROL_TEST_DATABASE_URL"), "requires disposable PostgreSQL")
class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.pool = ControlConnections()
        self.dsn = os.environ["CONTROL_TEST_DATABASE_URL"]

    def tearDown(self):
        self.pool.close()

    def terminate_idle_fixture(self, connect=psycopg.connect):
        with self.pool.connection(self.dsn, connect=connect) as conn:
            pid = conn.info.backend_pid
        with psycopg.connect(self.dsn) as observer:
            terminated = observer.execute(
                "SELECT pg_terminate_backend(pid, 1000) FROM pg_stat_activity "
                "WHERE pid=%s AND state='idle' AND usename=current_user AND datname=current_database()",
                (pid,),
            ).fetchone()
            self.assertEqual((True,), terminated)
        return pid

    def test_terminated_idle_backend_replaced_in_same_checkout(self):
        old = self.terminate_idle_fixture()
        entries = []
        with self.pool.connection(self.dsn) as conn:
            entries.append(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
        self.assertEqual(1, len(entries))
        self.assertNotEqual(old, entries[0])
        self.assertEqual(2, self.pool.created)
        with self.pool.connection(self.dsn) as conn:
            self.assertEqual(entries[0], conn.info.backend_pid)

    def test_failed_replacement_attempted_once_and_all_slots_recover(self):
        self.terminate_idle_fixture()
        attempts = []

        def fail(*args, **kwargs):
            attempts.append(1)
            raise psycopg.OperationalError("fixture replacement failed")

        with self.assertRaises(AcquisitionError), self.pool.connection(self.dsn, connect=fail):
            self.fail("failed replacement yielded")
        self.assertEqual([1], attempts)

        together = threading.Barrier(CAPACITY)

        def fresh():
            with self.pool.connection(self.dsn) as conn:
                together.wait(timeout=2)
                return conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]

        with ThreadPoolExecutor(CAPACITY) as executor:
            futures = [executor.submit(fresh) for _ in range(CAPACITY)]
            self.assertEqual(CAPACITY, len({f.result(3) for f in futures}))

    def test_expired_replacement_stays_counted_until_closed_without_sql(self):
        self.terminate_idle_fixture()
        opening, release, closed = (threading.Event() for _ in range(3))
        commands = []

        class Tracked(psycopg.Connection):
            def execute(self, query, *args, **kwargs):
                commands.append(query)
                return super().execute(query, *args, **kwargs)

            def close(self):
                super().close()
                closed.set()

        def delayed(*args, **kwargs):
            opening.set()
            self.assertTrue(release.wait(3))
            return Tracked.connect(*args, **kwargs)

        def checkout():
            with self.pool.connection(self.dsn, connect=delayed, deadline=time.monotonic() + .15):
                self.fail("expired replacement yielded")

        with ThreadPoolExecutor(1) as executor:
            result = executor.submit(checkout)
            try:
                self.assertTrue(opening.wait(1))
                with self.assertRaises(AdmissionTimeout):
                    result.result(.5)
                self.assertEqual(1, self.pool._active)
            finally:
                release.set()
        self.assertTrue(closed.wait(1))
        self.assertEqual([], commands)
        with self.pool.connection(self.dsn) as conn:
            self.assertEqual(1, conn.execute("SELECT 1 AS n").fetchone()["n"])

    def test_stale_cleanup_can_exhaust_acquisition_budget_before_fresh_connect(self):
        delayed = threading.Event()

        class SlowClose(psycopg.Connection):
            def close(self):
                super().close()
                if self.broken and not delayed.is_set():
                    delayed.set()
                    time.sleep(.12)

        self.terminate_idle_fixture(SlowClose.connect)
        fresh = Mock(side_effect=AssertionError("expired checkout started a fresh connector"))
        with patch("control_connections.QUEUE_TIMEOUT", .1), self.assertRaises(AdmissionTimeout):
            with self.pool.connection(self.dsn, connect=fresh):
                self.fail("expired setup yielded")
        self.assertTrue(delayed.is_set())
        fresh.assert_not_called()
        with self.pool.connection(self.dsn) as conn:
            self.assertEqual(1, conn.execute("SELECT 1 AS n").fetchone()["n"])

    def test_reset_and_failed_transaction(self):
        with self.pool.connection(self.dsn) as conn:
            pid = conn.info.backend_pid
            conn.execute("SET application_name = 'dirty'")
            conn.execute("CREATE TEMP TABLE dirty(value integer)")
            conn.execute("PREPARE dirty_plan AS SELECT 1")
            conn.execute("SELECT pg_advisory_lock(7654321)")
            conn.read_only = False
            conn.execute("BEGIN")
            with self.assertRaises(psycopg.Error):
                conn.execute("SELECT 1/0")
        with self.pool.connection(self.dsn) as conn:
            self.assertEqual(pid, conn.info.backend_pid)
            self.assertIsNone(conn.read_only)
            self.assertEqual("", conn.execute("SHOW application_name").fetchone()["application_name"])
            self.assertIsNone(conn.execute("SELECT to_regclass('pg_temp.dirty') AS relation").fetchone()["relation"])
            self.assertIsNone(conn.execute("SELECT name FROM pg_prepared_statements WHERE name='dirty_plan'").fetchone())
            self.assertEqual(0, conn.execute("SELECT count(*) AS n FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'").fetchone()["n"])
            self.assertEqual("pg_catalog, control", conn.execute("SHOW search_path").fetchone()["search_path"])
        self.assertEqual(1, self.pool.created)

    def test_connect_and_session_errors_release_capacity(self):
        def broken(*args, **kwargs):
            raise psycopg.OperationalError("fixture connect failure")
        with self.assertRaises(psycopg.OperationalError):
            with self.pool.connection(self.dsn, connect=broken):
                self.fail("failed connect yielded")
        with self.pool.connection(self.dsn) as conn:
            conn.close()
        with self.pool.connection(self.dsn) as conn:
            conn.execute("SELECT 1")
        self.assertEqual(2, self.pool.created)

    def test_saturation_expiry_cancellation_and_recovery(self):
        release = threading.Event()
        ready = threading.Barrier(CAPACITY + 1)
        errors = []

        def holder():
            try:
                with self.pool.connection(self.dsn):
                    ready.wait(timeout=3)
                    release.wait(3)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=holder) for _ in range(CAPACITY)]
        for thread in threads:
            thread.start()
        try:
            ready.wait(timeout=3)
            with self.assertRaises(AdmissionTimeout):
                with self.pool.connection(self.dsn, deadline=time.monotonic() + 0.02):
                    self.fail("Expired work ran")
            cancelled = threading.Event()
            timer = threading.Timer(0.02, cancelled.set)
            timer.start()
            with self.assertRaises(AdmissionTimeout):
                with self.pool.connection(self.dsn, cancelled=cancelled):
                    self.fail("Cancelled work ran")
            timer.join()
        finally:
            release.set()
            for thread in threads:
                thread.join(4)
        self.assertEqual([], errors)
        with self.pool.connection(self.dsn) as conn:
            conn.execute("SELECT 1")
        self.assertEqual(CAPACITY, self.pool.created)
        self.assertGreater(self.pool.queue_seconds, 0.01)

    def test_disconnected_and_expired_requests_do_not_connect(self):
        left, right = socket.socketpair()
        right.close()
        try:
            with request_scope(1, left), self.assertRaises(AdmissionTimeout):
                with self.pool.connection(self.dsn):
                    self.fail("Disconnected work ran")
        finally:
            left.close()
        with request_scope(0), self.assertRaises(AdmissionTimeout):
            with self.pool.connection(self.dsn):
                self.fail("Expired work ran")
        self.assertEqual(0, self.pool.created)

    def test_two_service_processes_leave_two_diagnostic_connections(self):
        # The launcher owns the scratch database as bootstrap. A standalone
        # run can instead supply an already limited test login.
        dsn = self.dsn
        if os.getenv("CONTROL_CAPACITY_TEST_CREATE_ROLE") == "1":
            admin = psycopg.connect(dsn, autocommit=True)
            self.addCleanup(admin.close)
            name = "mapp_capacity_test_" + secrets.token_hex(12)
            password = secrets.token_urlsafe(32)
            admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER CONNECTION LIMIT 8 PASSWORD {}")
                          .format(sql.Identifier(name), sql.Literal(password)))
            self.addCleanup(admin.execute, sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
            dsn = psycopg.conninfo.make_conninfo(dsn, user=name, password=password)

        with psycopg.connect(dsn) as check:
            role, superuser, login, limit = check.execute(
                "SELECT rolname, rolsuper, rolcanlogin, rolconnlimit FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            self.assertEqual((False, True, 8), (superuser, login, limit))
        context = multiprocessing.get_context("spawn")
        ready, release = context.Queue(), context.Event()
        processes = []
        try:
            for _ in range(2):
                process = context.Process(target=hold_process, args=(dsn, ready, release))
                process.start()
                processes.append(process)
            pids = {ready.get(timeout=10) for _ in range(2 * CAPACITY)}
            self.assertEqual(6, len(pids))
            with ExitStack() as diagnostics:
                first = diagnostics.enter_context(psycopg.connect(dsn))
                diagnostics.enter_context(psycopg.connect(dsn))
                with self.assertRaises(psycopg.OperationalError) as caught:
                    psycopg.connect(dsn).close()
                self.assertIn(f'too many connections for role "{role}"', str(caught.exception))
                first.close()
                with psycopg.connect(dsn) as replacement:
                    self.assertEqual((1,), replacement.execute("SELECT 1").fetchone())
        finally:
            release.set()
            for process in processes:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join()
            ready.close()
        self.assertTrue(all(process.exitcode == 0 for process in processes))
