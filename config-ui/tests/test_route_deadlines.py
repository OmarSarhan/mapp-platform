"""Route budget regressions. CONTROL_TEST_DATABASE_URL must be disposable."""

from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, closing
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import psycopg

import app
import control_connections as cc
from control_fixture import DATABASE_URL, requires_control_database, reset_control_schema
import control_plane
import control_schema
import runtime_database
import semantic_sources
from test_control_schema import reset_schema


@requires_control_database
class RouteDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, CONTROL_DATABASE_URL=DATABASE_URL))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = control_plane.ControlStore(Path(directory))
        self.addCleanup(os.close, self.store._process_lock_fd)
        pool = cc.ControlConnections()
        self.addCleanup(pool.close)
        self.enterContext(patch.object(cc, "CONTROL_CONNECTIONS", pool))
        self.observer = self.enterContext(psycopg.connect(DATABASE_URL, autocommit=True))
        # Join after the test's blocker has unwound, including on failure.
        self.executor = self.enterContext(ThreadPoolExecutor(1))

    def first_use(self):
        with self.store._db() as conn:
            return conn.execute("SELECT 1 AS ok").fetchone()["ok"]

    def while_blocked(self, operation, *, disconnect, entered):
        """Keep the caller's blocker held until both refusal and server exit."""
        local, peer = socket.socketpair()
        with local, peer:
            def run():
                with cc.request_scope(2 if disconnect else .1, local):
                    operation()

            until = time.monotonic() + .2  # 100 ms budget + 100 ms scheduler tolerance.
            future = self.executor.submit(run)
            while not entered():
                self.assertLess(time.monotonic(), until, "wait was never observed")
                time.sleep(.001)
            if disconnect:
                peer.close()
                # Wire cancellation has its own 250 ms libpq timeout.
                until = time.monotonic() + .25 + .1
            self.assertTrue(wait([future], timeout=max(0, until - time.monotonic())).done, "still blocked")
            return future.exception()

    def waiters(self, blocker):
        return self.observer.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
            (blocker.info.backend_pid,),
        ).fetchone()[0]

    def assert_no_ledger(self):
        self.assertFalse(self.store._migrated)
        self.assertIsNone(self.observer.execute(
            "SELECT to_regclass('control.schema_migrations')"
        ).fetchone()[0])

    def test_first_use_mutex_and_warm_checkout(self):
        reset_schema()
        # Signal an actual acquire attempt from the worker while we hold it.
        lock = self.store._db_lock
        entered = threading.Event()

        class ObservedLock:
            def __enter__(self):
                self.acquire()

            def __exit__(self, *_):
                self.release()

            def acquire(self, *args, **kwargs):
                entered.set()
                return lock.acquire(*args, **kwargs)

            def release(self):
                lock.release()

        with patch.object(self.store, "_db_lock", ObservedLock()), lock:
            for disconnect in (False, True):
                entered.clear()
                error = self.while_blocked(self.first_use, disconnect=disconnect, entered=entered.is_set)
                self.assertIsInstance(error, cc.AdmissionTimeout)
                self.assert_no_ledger()
        with cc.request_scope(2):
            self.assertEqual(1, self.first_use())
        self.assertTrue(self.store._migrated)

        def warm():
            with cc.request_scope(.1):
                return self.first_use()

        with lock, patch.object(control_schema, "migrate", side_effect=AssertionError("warm migration")):
            self.assertEqual(1, self.executor.submit(warm).result(.2))

    def test_first_use_advisory_wait(self):
        reset_schema()
        with psycopg.connect(DATABASE_URL) as blocker:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (control_schema.MIGRATION_LOCK,))
            for disconnect in (False, True):
                error = self.while_blocked(
                    self.first_use, disconnect=disconnect, entered=lambda: self.waiters(blocker),
                )
                self.assertIsInstance(error, (cc.CommandInterrupted, psycopg.errors.QueryCanceled))
                self.assertEqual(0, self.waiters(blocker))
                self.assert_no_ledger()
        with cc.request_scope(2):
            self.assertEqual(1, self.first_use())
        rows = self.observer.execute("SELECT version FROM control.schema_migrations ORDER BY version")
        self.assertEqual(sorted(control_schema.MIGRATIONS), [version for version, in rows])

    def test_partial_first_use_ladder_rolls_back_at_migration_ceiling(self):
        reset_schema()
        timeouts = []

        def blocked_second_step(conn):
            timeouts.append(int(conn.execute(
                "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'"
            ).fetchone()["setting"]))
            # Even a later change to a per-statement guard cannot restart
            # the operation budget after the first ledger INSERT.
            conn.execute("SET LOCAL statement_timeout = 0")
            conn.execute("SELECT pg_advisory_xact_lock(119)")

        with (
            psycopg.connect(DATABASE_URL) as blocker,
            patch.object(control_schema, "MIGRATION_TIMEOUT_MS", 200),
            patch.dict(control_schema.MIGRATIONS, {2: blocked_second_step}),
        ):
            blocker.execute("SELECT pg_advisory_xact_lock(119)")

            def migrate():
                with cc.request_scope(2):
                    self.first_use()

            future = self.executor.submit(migrate)
            self.assertTrue(wait([future], timeout=.3).done)
            self.assertIsInstance(future.exception(), cc.CommandInterrupted)
            self.assertEqual(1, len(timeouts))
            self.assertTrue(0 < timeouts[0] < 200)
            self.assertEqual(0, self.waiters(blocker))
            self.assert_no_ledger()
        with cc.request_scope(2):
            self.assertEqual(1, self.first_use())

    def test_generation_waits_exit_before_unlock(self):
        table = "generation_" + secrets.token_hex(8)
        self.observer.execute(f"CREATE TABLE control.{table} (id integer)")
        self.addCleanup(self.observer.execute, f"DROP TABLE control.{table}")
        sources = semantic_sources.PostgresSemanticSources(
            {"fixture": DATABASE_URL}, semantic_sources.parse_allowlist(f"fixture:control.{table}"),
        )
        kwargs = dict(schema="control", relation=table, fields=[{"name": "id", "type": "integer"}],
                      statistics=True, target_kind="table")
        for read in (lambda: sources.generation_context("fixture", **kwargs),
                     lambda: semantic_sources.postgres_generation_context(DATABASE_URL, **kwargs)):
            with psycopg.connect(DATABASE_URL) as blocker:
                blocker.execute(f"LOCK TABLE control.{table} IN ACCESS EXCLUSIVE MODE")
                for disconnect in (False, True):
                    error = self.while_blocked(read, disconnect=disconnect, entered=lambda: self.waiters(blocker))
                    self.assertIsInstance(error, cc.CommandInterrupted)
                    self.assertEqual(0, self.waiters(blocker))
            with cc.request_scope(1):
                self.assertEqual(1, read()["statistics"]["columnCount"])

    def test_late_derived_connector_holds_its_generation_slot_until_close(self):
        opened, release, closed = (threading.Event() for _ in range(3))
        slots = threading.BoundedSemaphore(1)
        borrower_sql = []

        class Tracked(psycopg.Connection):
            def cursor(self, *args, **kwargs):
                borrower_sql.append("cursor")
                return super().cursor(*args, **kwargs)

            def close(self):
                super().close()
                closed.set()

        def connect(*args, **kwargs):
            raw = Tracked.connect(*args, **kwargs)
            opened.set()
            if not release.wait(2):
                raw.close()
                raise AssertionError("late connector was never released")
            return raw

        def read():
            with cc.request_scope(.1):
                semantic_sources.postgres_generation_context(
                    DATABASE_URL, schema="control", relation="unused", fields=[],
                    target_kind="table", statistics=True,
                )

        with (
            patch.object(semantic_sources, "_GENERATION_CONTEXT_READ_SLOTS", slots),
            patch.object(semantic_sources.psycopg, "connect", connect),
        ):
            future = self.executor.submit(read)
            try:
                self.assertTrue(opened.wait(.2))
                self.assertTrue(wait([future], timeout=.2).done)
                self.assertIsInstance(future.exception(), cc.AdmissionTimeout)
                self.assertFalse(slots.acquire(blocking=False))
            finally:
                release.set()
            self.assertTrue(closed.wait(1))
            self.assertTrue(slots.acquire(timeout=.1))
            slots.release()
            self.assertEqual([], borrower_sql)

    def test_three_late_configured_connectors_hold_both_slots_through_close(self):
        opened, closing = threading.Semaphore(0), threading.Semaphore(0)
        finish_open, finish_close = threading.Event(), threading.Event()
        requests, borrower_cursors = [], []
        reader_slots = threading.BoundedSemaphore(8)
        sources = semantic_sources.PostgresSemanticSources(
            {"fixture": DATABASE_URL}, semantic_sources.parse_allowlist("fixture:control.unused"),
        )

        class Tracked(psycopg.Connection):
            def cursor(self, *args, **kwargs):
                borrower_cursors.append(True)
                return super().cursor(*args, **kwargs)

            def close(self):
                closing.release()
                finish_close.wait(3)
                super().close()

        def connect(*args, **kwargs):
            raw = Tracked.connect(*args, **kwargs)
            opened.release()
            finish_open.wait(3)
            return raw

        def read(seconds):
            with cc.request_scope(seconds) as request:
                requests.append(request)
                sources.generation_context("fixture", schema="control", relation="unused",
                                           fields=[], target_kind="table", statistics=True)

        with (ThreadPoolExecutor(3) as workers,
              patch.object(runtime_database, "_DBS_CONNECTION_SLOTS", reader_slots),
              patch.object(psycopg, "connect", side_effect=connect) as opener):
            callers = [workers.submit(read, 5) for _ in range(3)]
            try:
                for _ in callers:
                    self.assertTrue(opened.acquire(timeout=1))
                for request in requests:
                    request.cancel("test cancellation with opener held")
                for caller in callers:
                    self.assertIsInstance(caller.exception(1), cc.AdmissionTimeout)
                for phase in ("opening", "closing"):
                    if phase == "closing":
                        finish_open.set()
                        for _ in callers:
                            self.assertTrue(closing.acquire(timeout=1))
                    with ExitStack() as spare:
                        for _ in range(5):
                            self.assertTrue(reader_slots.acquire(blocking=False))
                            spare.callback(reader_slots.release)
                        self.assertFalse(reader_slots.acquire(blocking=False))
                    with self.assertRaises(cc.AdmissionTimeout):
                        read(.05)
                    self.assertEqual(3, opener.call_count)
            finally:
                finish_open.set()
                finish_close.set()
            with ExitStack() as recovered:
                for _ in range(8):
                    self.assertTrue(reader_slots.acquire(timeout=1))
                    recovered.callback(reader_slots.release)
                slots = semantic_sources._GENERATION_CONTEXT_READ_SLOTS
                for _ in range(3):
                    self.assertTrue(slots.acquire(timeout=1))
                    recovered.callback(slots.release)
            self.assertEqual([], borrower_cursors)

    def test_login_rechecks_after_password_verification(self):
        reset_control_schema()
        password = secrets.token_urlsafe(24)
        self.store.initialize(password)
        self.enterContext(patch.object(app, "CONTROL", self.store))
        self.enterContext(patch.object(app, "LOGIN_ATTEMPTS", {}))

        class QuietHandler(app.Handler):
            def log_message(self, *_):
                pass

        class Server(ThreadingHTTPServer):
            def handle_error(self, *args):
                if not isinstance(sys.exception(), cc.CommandNotSubmitted):
                    super().handle_error(*args)

        server = self.enterContext(Server(("127.0.0.1", 0), QuietHandler))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        verify = control_plane.verify_password
        login = self.store.login

        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect):
                if disconnect:
                    # Simulate first use after an upgrade. The committed
                    # migration survives a refused subsequent login INSERT.
                    with control_schema.connect(DATABASE_URL) as conn:
                        control_schema.rollback(conn, 10, accept_data_loss=True)
                    self.store._migrated = False
                verified, resume, finished = (threading.Event() for _ in range(3))
                errors = []

                def held(*args):
                    valid = verify(*args)
                    self.assertTrue(valid)
                    if not disconnect:
                        time.sleep(max(0, cc.current_request().deadline - time.monotonic()))
                    verified.set()
                    self.assertTrue(resume.wait(2))
                    return valid

                def observed(*args):
                    try:
                        return login(*args)
                    except Exception as error:
                        errors.append(type(error))
                        raise
                    finally:
                        finished.set()

                with (
                    patch.object(control_plane, "verify_password", held),
                    patch.object(self.store, "login", observed),
                    patch.object(secrets, "token_urlsafe", wraps=secrets.token_urlsafe) as new_token,
                    closing(http.client.HTTPConnection(*server.server_address, timeout=2)) as client,
                ):
                    # Allow HTTP/DB admission to finish on a loaded runner.
                    # Hold the verified result through the actual HTTP deadline.
                    client.request("POST", "/api/auth/login", json.dumps({"password": password}),
                                   {"X-Mapp-Timeout-Seconds": "2" if disconnect else ".3"})
                    try:
                        self.assertTrue(verified.wait(1))
                        if disconnect:
                            client.close()
                    finally:
                        resume.set()
                    self.assertTrue(finished.wait(1))
                    self.assertEqual([cc.CommandNotSubmitted], errors)
                    new_token.assert_not_called()
                    if not disconnect:
                        response = client.getresponse()
                        self.assertEqual(503, response.status)
                        self.assertIsNone(response.getheader("Set-Cookie"))
                        self.assertEqual("1", response.getheader("Retry-After"))
                        body = json.loads(response.read())
                        self.assertEqual("database.busy", body["code"])
                        self.assertFalse(body["mutation"]["targetStarted"])
                self.assertEqual(0, self.observer.execute("SELECT count(*) FROM control.sessions").fetchone()[0])
                self.assertEqual(max(control_schema.MIGRATIONS), self.observer.execute(
                    "SELECT max(version) FROM control.schema_migrations"
                ).fetchone()[0])

        with closing(http.client.HTTPConnection(*server.server_address, timeout=2)) as client:
            client.request("POST", "/api/auth/login", json.dumps({"password": password + "x"}))
            response = client.getresponse()
            self.assertEqual(401, response.status)
            self.assertIsNone(response.getheader("Set-Cookie"))
        with closing(http.client.HTTPConnection(*server.server_address, timeout=2)) as client:
            client.request("POST", "/api/auth/login", json.dumps({"password": password}))
            response = client.getresponse()
            self.assertEqual(200, response.status)
            self.assertIsNotNone(response.getheader("Set-Cookie"))
        self.assertEqual(1, self.observer.execute("SELECT count(*) FROM control.sessions").fetchone()[0])
