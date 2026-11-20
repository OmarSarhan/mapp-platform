"""Real driver boundary tests. CONTROL_TEST_DATABASE_URL must be disposable."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

import control_connections as cc
import control_schema
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

    def test_late_failed_openers_hold_capacity_until_completion(self):
        for reader in (False, True):
            entered, release = threading.Event(), threading.Event()

            def fail(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2))
                raise psycopg.OperationalError("late test opener")

            def lease(opener):
                return (rd.dbs_connection(opener, self.dsn) if reader
                        else self.pool.connection(self.dsn, connect=opener))

            def caller():
                with cc.request_scope(.05), lease(fail):
                    self.fail("failed opener yielded")

            with self.subTest(reader=reader), patch.object(cc, "CAPACITY", 1), patch.object(
                rd, "_DBS_CONNECTION_SLOTS", threading.BoundedSemaphore(1)
            ), ThreadPoolExecutor(1) as executor:
                result = executor.submit(caller)
                try:
                    self.assertTrue(entered.wait(1))
                    with self.assertRaises(cc.AdmissionTimeout):
                        result.result(.4)
                    with cc.request_scope(.04), self.assertRaises(cc.AdmissionTimeout), lease(psycopg.connect):
                        self.fail("slot released before late failure")
                finally:
                    release.set()
                with lease(psycopg.connect) as healthy:
                    healthy.execute("SELECT 1")

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
                with self.assertRaises(cc.CommandNotSubmitted), cc.request_scope(5) as request, lease as conn:
                    # This case tests command/commit expiry, not connector speed.
                    request.deadline = time.monotonic() + .10
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
                PausedDriver.pause = True
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
                PausedReply.pause = True
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
            with self.assertRaises(cc.AdmissionTimeout), self.pool.connection(self.dsn):
                self.fail("cancelled scope acquired a second lease")

    def test_transport_loss_after_observed_commit(self):
        # The bridge forwards one client session. Withhold the first byte
        # of the COMMIT reply until the observer has seen its durable effect.
        with psycopg.connect(self.dsn, autocommit=True) as observer:
            host, port = observer.info.host, observer.info.port
            table = sql.Identifier("control", "lease_" + uuid4().hex)
            observer.execute(sql.SQL("CREATE TABLE {} (n int)").format(table))
            self.addCleanup(self.drop_table, table)
            commit, held, release = (threading.Event() for _ in range(3))
            name = f".s.PGSQL.{port}"
            with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as listener:
                listener.bind(os.path.join(directory, name))
                listener.settimeout(4)
                listener.listen(1)

                def forward(source, target, server_reply=False):
                    while chunk := source.recv(65536):
                        if server_reply and commit.is_set():
                            held.set()
                            self.assertTrue(release.wait(3))
                            target.shutdown(socket.SHUT_RDWR)
                            return
                        target.sendall(chunk)

                def bridge(executor):
                    client, _ = listener.accept()
                    if os.path.isabs(host):
                        server = socket.socket(socket.AF_UNIX)
                        server.settimeout(4)
                        server.connect(os.path.join(host, name))
                    else:
                        server = socket.create_connection((host, port), timeout=4)
                    with client, server:
                        client.settimeout(4)
                        server.settimeout(4)
                        writer = executor.submit(forward, client, server)
                        try:
                            forward(server, client, True)
                        finally:
                            client.shutdown(socket.SHUT_RDWR)
                        writer.result(2)

                entries = []
                dsn = psycopg.conninfo.make_conninfo(self.dsn, host=directory)

                def borrower():
                    with self.pool.connection(dsn) as conn, control_schema.transaction(conn):
                        entries.append(conn.info.backend_pid)
                        conn.execute(sql.SQL("INSERT INTO {} VALUES (1)").format(table))
                        commit.set()

                with ThreadPoolExecutor(3) as executor:
                    proxy = executor.submit(bridge, executor)
                    with self.pool.connection(dsn) as warm:
                        pid = warm.info.backend_pid
                    result = executor.submit(borrower)
                    try:
                        self.assertTrue(held.wait(2))
                        self.assertEqual((1,), observer.execute(
                            sql.SQL("SELECT count(*) FROM {}").format(table)).fetchone())
                    finally:
                        release.set()
                    with self.assertRaises(cc.CommandInterrupted):
                        result.result(2)
                    proxy.result(2)
                self.assertEqual([pid], entries)
                self.assertEqual(1, self.pool.created)
                with self.pool.connection(self.dsn) as fresh:
                    self.assertNotEqual(pid, fresh.info.backend_pid)
                    self.assertEqual(1, fresh.execute("SELECT 1 AS n").fetchone()["n"])

    def drop_table(self, table):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP TABLE {}").format(table))

    def test_json_adapter_cannot_submit_after_cancellation_or_deadline(self):
        for cancel in (False, True):
            for prepared in (False, True):
                with self.subTest(cancel=cancel, prepared=prepared), self.rows() as insert:
                    with cc.request_scope(5) as request, self.pool.connection(self.dsn) as conn:
                        if not cancel:
                            request.deadline = time.monotonic() + .15
                        def dump(value):
                            if cancel:
                                request.cancel("fixture")
                            else:
                                time.sleep(max(0, request.deadline - time.monotonic()) + .01)
                            return str(value)

                        # A RETURNING expression is adapted before PostgreSQL
                        # executes the INSERT. The observer checks the table.
                        with conn.cursor() as cur, self.assertRaises(cc.CommandNotSubmitted):
                            execute = cur.execute if prepared else conn.execute
                            execute(insert + sql.SQL(" RETURNING %s::jsonb"),
                                    (Jsonb(1, dumps=dump),), prepare=prepared)

    def test_stream_rejected_before_insert(self):
        with self.rows() as insert, self.pool.connection(self.dsn) as conn, conn.cursor() as cursor:
            with self.assertRaises(psycopg.NotSupportedError):
                next(cursor.stream(insert + sql.SQL(" RETURNING n")))

    def test_local_dumper_failure_preserves_original_error(self):
        def fail(_):
            raise OSError("fixture dumper")

        with self.rows() as insert, self.pool.connection(self.dsn) as conn:
            with self.assertRaisesRegex(OSError, "fixture dumper"):
                conn.execute(insert + sql.SQL(" RETURNING %s::jsonb"), (Jsonb(1, dumps=fail),))
            conn.execute("SELECT 1")

    def test_transaction_helper_preserves_refused_and_interrupted_errors(self):
        for active in (False, True):
            error = cc.CommandInterrupted if active else cc.CommandNotSubmitted
            with self.subTest(active=active), self.rows() as insert, self.assertRaises(error):
                with cc.request_scope(.15) as req, self.pool.connection(self.dsn) as conn:
                    with control_schema.transaction(conn):
                        conn.execute(insert)
                        if active:
                            conn.execute("SET statement_timeout=0")
                            conn.execute("SELECT pg_sleep(.5)")
                        else:
                            req.cancel("fixture")

    def test_lease_transaction_preserves_primary_exception_during_unwind(self):
        for active in (False, True):
            expected = cc.CommandInterrupted if active else ValueError
            with self.subTest(active=active), self.rows() as insert, self.assertRaises(expected):
                with cc.request_scope(.15) as request, self.pool.connection(self.dsn) as conn:
                    with conn.transaction():
                        conn.execute(insert)
                        if active:
                            conn.execute("SET statement_timeout=0")
                            conn.execute("SELECT pg_sleep(.5)")
                        else:
                            request.cancel("fixture unwind")
                            raise ValueError("original borrower failure")

    def test_direct_cancel_latches_while_parameter_adapter_holds_driver(self):
        adapting, resume, cancelling = (threading.Event() for _ in range(3))
        with self.rows() as insert, self.pool.connection(self.dsn) as conn, ThreadPoolExecutor(2) as executor:
            def dump(value):
                adapting.set()
                self.assertTrue(resume.wait(2))
                return str(value)

            def cancel():
                def entered(frame, event, arg):
                    if event == "call" and frame.f_code is conn.cancel_safe.__code__:
                        cancelling.set()
                    return None
                sys.settrace(entered)
                try:
                    conn.cancel_safe()
                finally:
                    sys.settrace(None)

            command = executor.submit(conn.execute, insert + sql.SQL(" RETURNING %s::jsonb"),
                                      (Jsonb(1, dumps=dump),))
            try:
                self.assertTrue(adapting.wait(1))
                abort = executor.submit(cancel)
                self.assertTrue(cancelling.wait(1))
                with self.assertRaises(TimeoutError):
                    abort.result(.05)
            finally:
                resume.set()
            with self.assertRaises(cc.CommandNotSubmitted):
                command.result(1)
            abort.result(1)

    def test_caught_inner_failure_cannot_acknowledge_rolled_back_outer_write(self):
        for direct in (False, True):
            with self.subTest(direct=direct), self.rows() as insert:
                with cc.request_scope(5) as request, self.pool.connection(self.dsn) as conn:
                    with self.assertRaises(cc.CommandNotSubmitted):
                        with conn.transaction():
                            conn.execute(insert)
                            try:
                                with conn.transaction():
                                    if direct:
                                        conn.cancel_safe()
                                    else:
                                        request.cancel("inner unwind")
                                    raise ValueError("caught inner failure")
                            except ValueError:
                                pass

    def test_out_of_order_transaction_exit_preserves_driver_error(self):
        with self.pool.connection(self.dsn) as conn:
            outer = conn.transaction()
            inner = conn.transaction()
            outer.__enter__()
            inner.__enter__()
            error = ValueError("outer failure")
            with self.assertRaises(psycopg.transaction.OutOfOrderTransactionNesting):
                outer.__exit__(type(error), error, None)
            with self.assertRaises(psycopg.transaction.OutOfOrderTransactionNesting):
                inner.__exit__(None, None, None)

    def test_late_and_bound_cancel_cannot_interrupt_reused_backend(self):
        with self.pool.connection(self.dsn) as previous:
            pid = previous.info.backend_pid
            cursor = previous.execute("SELECT 1")
            callbacks = [previous.cancel, lambda: previous.cancel(), lambda: cursor.connection.cancel_safe()]
        with self.pool.connection(self.dsn) as current, psycopg.connect(self.dsn, autocommit=True) as observer:
            self.assertEqual(pid, current.info.backend_pid)
            with ThreadPoolExecutor(1) as executor:
                running = executor.submit(current.execute, "SELECT pg_sleep(.2), 1 AS n")
                deadline = time.monotonic() + 1
                while observer.execute("SELECT wait_event FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone() != ("PgSleep",):
                    self.assertLess(time.monotonic(), deadline)
                for callback in callbacks:
                    callback()
                self.assertEqual(1, running.result(1).fetchone()["n"])

    def test_explicit_wire_abort_before_command_evicts_and_refuses_send(self):
        with self.rows() as insert, self.pool.connection(self.dsn) as conn:
            pid = conn.info.backend_pid
            conn.cancel_safe()
            with self.assertRaises(cc.CommandNotSubmitted):
                conn.execute(insert)
        with self.pool.connection(self.dsn) as healthy:
            self.assertNotEqual(pid, healthy.info.backend_pid)
            healthy.execute("SELECT 1")

    def test_interruption_closes_suspended_driver_generator(self):
        finalized = threading.Event()
        # Instrument the driver's generator at its source, leaving the normal
        # borrower execute and interrupt paths intact.
        execute = psycopg.Cursor._execute_gen

        def tracking(cursor, query, *args, **kwargs):
            try:
                return (yield from execute(cursor, query, *args, **kwargs))
            finally:
                if query == "SELECT pg_sleep(.5)":
                    finalized.set()

        with patch.object(psycopg.Cursor, "_execute_gen", tracking):
            with cc.request_scope(.15), self.pool.connection(self.dsn) as conn:
                conn.execute("SET statement_timeout=0")
                try:
                    conn.execute("SELECT pg_sleep(.5)")
                except cc.CommandInterrupted as retained:
                    self.assertIsNotNone(retained.__traceback__)
                    self.assertTrue(finalized.is_set())
                else:
                    self.fail("expected an active wait to be interrupted")

    def test_setup_refused_before_or_during_send(self):
        for active in (False, True):
            class PausedSetup(psycopg.Connection):
                def execute(self, query, *args, **kwargs):
                    if query == "SET SESSION search_path = pg_catalog, control":
                        if active:
                            return super().execute("SELECT pg_sleep(.5)")
                        cc.current_request().cancel("setup")
                    return super().execute(query, *args, **kwargs)

            with self.subTest(active=active), cc.request_scope(.15), self.assertRaises(cc.AdmissionTimeout):
                with self.pool.connection(self.dsn, connect=PausedSetup.connect):
                    self.fail("borrower entered")
        with self.pool.connection(self.dsn) as healthy:
            self.assertEqual(1, healthy.execute("SELECT 1 AS n").fetchone()["n"])

    def test_close_fault_after_setup_or_reset_failure_releases_slot(self):
        for setup in (False, True):
            fail_at = "SET SESSION search_path = pg_catalog, control" if setup else "DISCARD ALL"
            closed = threading.Event()

            class Broken(psycopg.Connection):
                def execute(self, query, *args, **kwargs):
                    if query == fail_at:
                        raise psycopg.OperationalError("injected")
                    return super().execute(query, *args, **kwargs)

                def close(self):
                    super().close()
                    closed.set()
                    raise OSError("injected after close")

            with self.subTest(setup=setup), patch.object(cc, "CAPACITY", 1):
                pool = cc.ControlConnections()
                self.addCleanup(pool.close)
                if setup:
                    with self.assertRaises(cc.AcquisitionError), pool.connection(self.dsn, connect=Broken.connect):
                        self.fail("borrower entered")
                else:
                    with pool.connection(self.dsn, connect=Broken.connect) as conn:
                        conn.execute("SELECT 1")
                self.assertTrue(closed.is_set())
                with pool.connection(self.dsn) as healthy:
                    self.assertEqual(1, healthy.execute("SELECT 1 AS n").fetchone()["n"])

    def test_reader_setup_failure_and_close_fault_release_slot(self):
        class Broken(psycopg.Connection):
            def __enter__(self):
                raise psycopg.OperationalError("reader setup failed")

            def close(self):
                super().close()
                raise OSError("injected after close")

        with patch.object(rd, "_DBS_CONNECTION_SLOTS", threading.BoundedSemaphore(1)):
            with self.assertRaises(cc.AcquisitionError), rd.dbs_connection(Broken.connect, self.dsn):
                self.fail("reader borrower entered")
            with rd.dbs_connection(psycopg.connect, self.dsn) as healthy:
                self.assertEqual((1,), healthy.execute("SELECT 1").fetchone())

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

    def test_lease_joins_budget_registration_before_idle_backend_reuse(self):
        aborting, leaving, release = (threading.Event() for _ in range(3))

        def borrower():
            with cc.request_scope(5) as req:
                arm = req.arm

                def observe(abort):
                    def held():
                        abort()
                        aborting.set()
                        self.assertTrue(release.wait(2))
                    return arm(held)

                with patch.object(req, "arm", observe), self.pool.connection(self.dsn) as conn:
                    pid = conn.info.backend_pid
                    req.cancel("idle")
                    self.assertTrue(aborting.wait(1))
                    leaving.set()
                return pid

        with patch.object(cc, "CAPACITY", 1), ThreadPoolExecutor(1) as executor:
            owner = executor.submit(borrower)
            try:
                self.assertTrue(leaving.wait(1))
                with cc.request_scope(.05), self.assertRaises(cc.AdmissionTimeout), self.pool.connection(self.dsn):
                    self.fail("acquired while registered abort was still running")
            finally:
                release.set()
            pid = owner.result(1)
            with self.pool.connection(self.dsn) as healthy:
                self.assertEqual(pid, healthy.info.backend_pid)
                healthy.execute("SELECT 1")
