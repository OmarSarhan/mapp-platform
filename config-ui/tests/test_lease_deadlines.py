"""Real driver boundary tests. CONTROL_TEST_DATABASE_URL must be disposable."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import socket
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql

import control_connections as cc
import runtime_database as rd


@unittest.skipUnless(os.getenv("CONTROL_TEST_DATABASE_URL"), "requires disposable PostgreSQL")
class LeaseDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["CONTROL_TEST_DATABASE_URL"]
        self.pool = cc.ControlConnections()
        self.addCleanup(self.pool.close)

    @contextmanager
    def rows(self, expected=0):
        table = sql.Identifier("control", "lease_" + uuid4().hex)
        with psycopg.connect(self.dsn, autocommit=True) as observer:
            observer.execute(sql.SQL("CREATE TABLE {} (n int)").format(table))
            try:
                yield sql.SQL("INSERT INTO {} VALUES (1)").format(table)
                count, = observer.execute(sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone()
                self.assertEqual(expected, count)
            finally:
                observer.execute(sql.SQL("DROP TABLE {}").format(table))

    def test_blocked_connectors_keep_capacity_until_late_close(self):
        with patch.object(cc, "CAPACITY", 1), patch.object(
            rd, "_DBS_CONNECTION_SLOTS", threading.BoundedSemaphore(1)
        ):
            for reader in (False, True):
                with self.subTest(reader=reader):
                    self.check_late_connector(reader)

    def check_late_connector(self, reader):
        entered, release, closed = (threading.Event() for _ in range(3))
        closing, allow_close = threading.Event(), threading.Event()
        commands = []

        class Tracked(psycopg.Connection):
            def execute(self, query, *args, **kwargs):
                commands.append(query)
                return super().execute(query, *args, **kwargs)

            def close(self):
                closing.set()
                if not allow_close.wait(3):
                    raise AssertionError("test did not release late close")
                super().close()
                closed.set()

        def connect(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release the connector")
            return Tracked.connect(*args, **kwargs)

        def lease(opener):
            if reader:
                return rd.dbs_connection(opener, self.dsn)
            return self.pool.connection(self.dsn, connect=opener)

        def caller():
            with cc.request_scope(.05), lease(connect) as conn:
                conn.execute("SELECT 1")
                self.fail("late connector reached borrower")

        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(caller)
            try:
                self.assertTrue(entered.wait(1))
                # Refusal must arrive while the connector is still blocked.
                with self.assertRaises(cc.AdmissionTimeout):
                    future.result(timeout=.3)
                with cc.request_scope(.04), self.assertRaises(cc.AdmissionTimeout):
                    with lease(lambda *a, **kw: self.fail("slot released early")):
                        self.fail("over capacity")
            finally:
                release.set()
                try:
                    self.assertTrue(closing.wait(1))
                    with cc.request_scope(.04), self.assertRaises(cc.AdmissionTimeout):
                        with lease(lambda *a, **kw: self.fail("slot released during close")):
                            self.fail("over capacity")
                finally:
                    allow_close.set()
        self.assertTrue(closed.wait(1))
        self.assertEqual([], commands)
        with lease(psycopg.connect) as conn:
            self.assertIsNotNone(conn.execute("SELECT 1").fetchone())

    def test_chained_autocommit_commands_share_deadline(self):
        completed = 0
        with self.rows() as insert, self.assertRaises(cc.CommandInterrupted):
            with cc.request_scope(.18), self.pool.connection(self.dsn) as conn:
                conn.execute("SET statement_timeout = 0")
                for _ in range(3):
                    conn.execute("SELECT pg_sleep(.08)")
                    completed += 1
                conn.execute(insert)
        self.assertGreater(completed, 0)

    def test_reader_disconnect_interrupts_active_read_while_lock_is_held(self):
        entered = threading.Event()
        pids = []
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        def read():
            with cc.request_scope(10, left), rd.dbs_connection(psycopg.connect, self.dsn) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    cursor.execute("SHOW transaction_read_only")
                    self.assertEqual(("on",), cursor.fetchone())
                    pids.append(conn.info.backend_pid)
                    entered.set()
                    cursor.execute("SELECT pg_advisory_lock(543220)")

        with psycopg.connect(self.dsn, autocommit=True) as locker, ThreadPoolExecutor(1) as executor:
            locker.execute("SELECT pg_advisory_lock(543220)")
            future = executor.submit(read)
            try:
                self.assertTrue(entered.wait(1))
                deadline = time.monotonic() + 1
                while not locker.execute(
                    "SELECT EXISTS (SELECT FROM pg_locks WHERE pid=%s AND NOT granted)", pids
                ).fetchone()[0]:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.005)
                right.close()
                with self.assertRaises(cc.CommandInterrupted):
                    future.result(timeout=.5)
                # Connection.close() alone can leave the query waiting on the
                # server. Check cancellation before releasing the blocker.
                deadline = time.monotonic() + .5
                while locker.execute("SELECT EXISTS (SELECT FROM pg_locks WHERE pid=%s)", pids).fetchone()[0]:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.005)
            finally:
                locker.execute("SELECT pg_advisory_unlock_all()")

    def test_expired_cursor_and_commits(self):
        for mode in ("cursor", "sql_commit", "driver_commit", "transaction_exit", "reader_exit"):
            with self.subTest(mode=mode), self.rows() as insert:
                reader = mode == "reader_exit"
                lease = rd.dbs_connection(psycopg.connect, self.dsn) if reader else self.pool.connection(self.dsn)
                with self.assertRaises(cc.CommandNotSubmitted), cc.request_scope(.10), lease as conn:
                    if mode == "transaction_exit":
                        with conn.transaction():
                            conn.execute(insert)
                            time.sleep(.12)
                    elif mode == "cursor":
                        with conn.cursor() as cursor:
                            time.sleep(.12)
                            cursor.execute(insert)
                    else:
                        if not reader:
                            conn.execute("BEGIN")
                        conn.execute(insert)
                        time.sleep(.12)
                        if mode == "sql_commit":
                            conn.execute("COMMIT")
                        elif mode == "driver_commit":
                            conn.commit()

    def test_cancel_before_driver_submission_refuses_autocommit_insert(self):
        entered, resume = threading.Event(), threading.Event()

        class PausedDriver(psycopg.Connection):
            pause = False

            def wait(self, gen, **kwargs):
                if self.pause:
                    entered.set()
                    if not resume.wait(2):
                        raise AssertionError("test did not release driver")
                return super().wait(gen, **kwargs)

        def cancel(request):
            try:
                self.assertTrue(entered.wait(1))
                request.cancel("peer closed")
            finally:
                resume.set()

        with self.rows() as insert, cc.request_scope(5) as request, ThreadPoolExecutor(1) as executor:
            with self.pool.connection(self.dsn, connect=PausedDriver.connect) as conn:
                future = executor.submit(cancel, request)
                conn.pause = True
                with self.assertRaises(cc.CommandNotSubmitted):
                    conn.execute(insert)
                future.result(timeout=1)

    def test_commit_reply_interrupted_after_publication_is_indeterminate(self):
        # Hold delivery of readiness to the driver until a second connection
        # has actually observed the commit. This is a lost acknowledgement
        # with an independently known database outcome.
        table = sql.Identifier("control", "lease_" + uuid4().hex)
        count = sql.SQL("SELECT count(*) FROM {} WHERE n = 2").format(table)
        submitted, resume = threading.Event(), threading.Event()

        class PausedReply(psycopg.Connection):
            pause = False

            def wait(self, gen, **kwargs):
                if not self.pause:
                    return super().wait(gen, **kwargs)

                def paused():
                    try:
                        interest = next(gen)
                    except StopIteration as done:
                        return done.value
                    submitted.set()
                    if not resume.wait(2):
                        raise AssertionError("test did not release reply")
                    ready = yield interest
                    try:
                        while True:
                            ready = yield gen.send(ready)
                    except StopIteration as done:
                        return done.value

                return super().wait(paused(), **kwargs)

        def commit(request):
            with self.pool.connection(self.dsn, connect=PausedReply.connect, cancelled=request) as conn:
                conn.execute("BEGIN")
                conn.execute(sql.SQL("INSERT INTO {} VALUES (2, 1)").format(table))
                conn.pause = True
                conn.commit()

        cancelled = threading.Event()
        with psycopg.connect(self.dsn, autocommit=True) as observer, ThreadPoolExecutor(1) as executor:
            observer.execute(sql.SQL("CREATE TABLE {} (n int PRIMARY KEY, parent int REFERENCES {}"
                                     " DEFERRABLE INITIALLY DEFERRED)").format(table, table))
            try:
                observer.execute(sql.SQL("INSERT INTO {} VALUES (1, NULL)").format(table))
                try:
                    # A deferred FK needs a key-share lock during COMMIT.
                    # The driver must yield, even on a fast local server.
                    with observer.transaction():
                        observer.execute(sql.SQL("SELECT n FROM {} WHERE n = 1 FOR UPDATE").format(table))
                        future = executor.submit(commit, cancelled)
                        self.assertTrue(submitted.wait(1))
                    deadline = time.monotonic() + 1
                    while observer.execute(count).fetchone()[0] == 0:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.005)
                    cancelled.set()
                finally:
                    resume.set()
                with self.assertRaises(cc.CommandInterrupted):
                    future.result(timeout=1)
                self.assertEqual((1,), observer.execute(count).fetchone())
            finally:
                observer.execute(sql.SQL("DROP TABLE {}").format(table))

    def test_refused_next_command_preserves_prior_autocommit_effect(self):
        with self.rows(expected=1) as insert, cc.request_scope(5) as request:
            with self.pool.connection(self.dsn) as conn:
                conn.execute(insert)
                request.cancel("peer closed")
                with self.assertRaises(cc.CommandNotSubmitted):
                    conn.execute(insert)

    def test_delayed_wire_cancel_cannot_reach_next_borrower(self):
        cancel_entered, allow_cancel, executing = (threading.Event() for _ in range(3))
        pids = []

        class DelayedCancel(psycopg.Connection):
            def cancel_safe(self, **kwargs):
                cancel_entered.set()
                if not allow_cancel.wait(3):
                    raise AssertionError("test did not release wire cancellation")
                return super().cancel_safe(**kwargs)

        def borrower():
            with cc.request_scope(.3), self.pool.connection(self.dsn, connect=DelayedCancel.connect) as conn:
                pids.append(conn.info.backend_pid)
                conn.execute("SET statement_timeout = 0")
                executing.set()
                conn.execute("SELECT pg_advisory_lock(543219)")

        with patch.object(cc, "CAPACITY", 1), psycopg.connect(self.dsn) as locker, ThreadPoolExecutor(1) as executor:
            locker.execute("SELECT pg_advisory_lock(543219)")
            future = executor.submit(borrower)
            try:
                self.assertTrue(executing.wait(1))
                self.assertTrue(cancel_entered.wait(1))
                # The server can finish the command now, before cancel arrives.
                locker.execute("SELECT pg_advisory_unlock(543219)")
                with cc.request_scope(.05), self.assertRaises(cc.AdmissionTimeout):
                    with self.pool.connection(self.dsn):
                        self.fail("slot released with wire cancel in flight")
            finally:
                locker.execute("SELECT pg_advisory_unlock_all()")
                allow_cancel.set()
            with self.assertRaises(cc.CommandInterrupted):
                future.result(timeout=1)
            with self.pool.connection(self.dsn) as healthy:
                self.assertNotEqual(pids[0], healthy.info.backend_pid)
                self.assertEqual(1, healthy.execute("SELECT 1 AS ok").fetchone()["ok"])

    def test_release_joins_external_cancel_and_disarms_captured_callback(self):
        cancelling, resume, leaving = (threading.Event() for _ in range(3))
        attempts = []

        class DelayedCancel(psycopg.Connection):
            def cancel_safe(self, **kwargs):
                attempts.append(self.info.backend_pid)
                cancelling.set()
                if not resume.wait(3):
                    raise AssertionError("test did not release cancel")
                return super().cancel_safe(**kwargs)

        def owner(executor):
            with self.pool.connection(self.dsn, connect=DelayedCancel.connect) as conn:
                callback = conn.cancel
                future = executor.submit(callback)
                self.assertTrue(cancelling.wait(1))
                leaving.set()
            future.result(timeout=1)
            return callback

        with patch.object(cc, "CAPACITY", 1), ThreadPoolExecutor(2) as executor:
            released = executor.submit(owner, executor)
            try:
                self.assertTrue(leaving.wait(1))
                with cc.request_scope(.05), self.assertRaises(cc.AdmissionTimeout):
                    with self.pool.connection(self.dsn):
                        self.fail("returned backend before cancellation finished")
            finally:
                resume.set()
            stale_callback = released.result(timeout=1)
            with self.pool.connection(self.dsn) as healthy:
                stale_callback()
                self.assertEqual(1, len(attempts))
                self.assertNotEqual(attempts[0], healthy.info.backend_pid)
                healthy.execute("SELECT 1")
