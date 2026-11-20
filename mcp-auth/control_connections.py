"""Bounded control-role connections; keep the config-ui copy identical.

The deployed single OAuth process and single config API process each own three
slots, leaving two of the role's eight sessions for administration. Replicating
either service requires repartitioning this budget, not increasing the limit.
"""

from concurrent.futures import Future, wait
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from functools import partial
from types import SimpleNamespace
import math
import select
import socket
import threading
import time

import psycopg
from psycopg.rows import dict_row

CAPACITY = 3
MAX_WAITERS = 12
QUEUE_TIMEOUT = 2.0
_REQUEST = ContextVar("control_request", default=None)
_POLL = 0.025


class AdmissionTimeout(TimeoutError):
    """A budget check failed before the next admission."""


class AcquisitionError(psycopg.OperationalError):
    """The backend could not be acquired or configured before borrower entry."""


class CommandNotSubmitted(psycopg.OperationalError):
    """This lease command did not start. Earlier effects may have committed."""


class CommandInterrupted(psycopg.OperationalError):
    """A command may have been submitted. Its effects require reconciliation."""


class RequestBudget:
    def __init__(self, seconds, peer):
        self.deadline = time.monotonic() + max(0, min(seconds, 120.0))
        self.peer = peer
        self.queue_seconds = self.database_seconds = 0.0
        self._condition = threading.Condition()
        self._reason = None
        self._watchers = set()
        self._late_aborts = 0

    def cancel(self, reason):
        with self._condition:
            if self._reason is None:
                self._reason = reason
                self._condition.notify_all()

    def remaining(self, cap=120.0):
        with self._condition:
            if self._reason is None and self.peer is not None:
                try:
                    readable, _, _ = select.select([self.peer], [], [], 0)
                    if readable and self.peer.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                        self.cancel("Request disconnected.")
                except BlockingIOError:
                    pass
                except (OSError, ValueError):
                    self.cancel("Request disconnected.")
            seconds = self.deadline - time.monotonic()
            if seconds <= 0:
                self.cancel("Database operation deadline expired.")
            if self._reason is not None:
                raise AdmissionTimeout(self._reason)
            if cap <= 0:
                raise AdmissionTimeout("Database operation deadline expired.")
            return min(seconds, cap)

    def arm(self, abort):
        """Register a zero-argument resource abort. Disarm joins a racing abort."""
        def watch():
            with self._condition:
                while watcher in self._watchers:
                    try:
                        remaining = self.remaining()
                    except AdmissionTimeout:
                        break
                    self._condition.wait(min(_POLL, remaining))
                else:
                    return
            abort()

        watcher = threading.Thread(target=watch, daemon=True)
        with self._condition:
            try:
                self.remaining()
            except AdmissionTimeout as exc:
                refusal = exc
                self._late_aborts += 1
            else:
                refusal = None
                self._watchers.add(watcher)
                try:
                    watcher.start()
                except BaseException:
                    self._watchers.remove(watcher)
                    raise
        if refusal is not None:
            try:
                abort()
            finally:
                with self._condition:
                    self._late_aborts -= 1
                    self._condition.notify_all()
            raise refusal

        def disarm():
            with self._condition:
                if self._reason is None:
                    self._watchers.discard(watcher)
                    self._condition.notify_all()
            watcher.join()
            with self._condition:
                self._watchers.discard(watcher)

        return disarm

    def finish(self):
        with self._condition:
            self.cancel("Request scope ended.")
            watchers = list(self._watchers)
        for watcher in watchers:
            watcher.join()
        with self._condition:
            self._condition.wait_for(lambda: self._late_aborts == 0)


def _remaining(deadline, cap=120.0):
    remaining = min(cap, deadline - time.monotonic())
    if remaining <= 0:
        raise AdmissionTimeout("Database operation deadline expired.")
    return remaining


@contextmanager
def request_scope(seconds=120.0, peer=None):
    try:
        seconds = float(seconds)
    except (ValueError, TypeError):
        seconds = 0.0
    if not math.isfinite(seconds):
        seconds = 0.0
    state = RequestBudget(seconds, peer)
    token = _REQUEST.set(state)
    try:
        yield state
    finally:
        state.finish()
        _REQUEST.reset(token)


def current_request():
    return _REQUEST.get()


def remaining_time(default=120.0):
    state = current_request()
    return state.remaining(default) if state is not None else _remaining(math.inf, default)


def request_metrics():
    state = _REQUEST.get()
    return {} if state is None else {"queueSeconds": state.queue_seconds,
                                     "databaseSeconds": state.database_seconds}


class _ConnectionSlot:
    """Hand off the reserved slot to a late synchronous connector until close."""
    def __init__(self, release):
        self.release = release
        self.transferred = False

    def connect(self, opener, remaining):
        future = Future()
        handoff = threading.Event()

        def open_connection():
            connection = None
            try:
                connection = opener()
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(connection)
            handoff.wait()
            if self.transferred:
                try:
                    if connection is not None:
                        discard_connection(connection)
                finally:
                    self.release()

        remaining()
        worker = threading.Thread(target=open_connection, daemon=True)
        worker.start()
        try:
            while not future.done():
                wait([future], timeout=min(_POLL, remaining()))
            remaining()
            return future.result()
        except BaseException:
            self.transferred = True
            raise
        finally:
            handoff.set()
            if not self.transferred:
                worker.join()


class _EagerCursor(psycopg.Cursor):
    # psycopg 3.3.4 _execute_gen converts the query (including user dumpers)
    # before these synchronous libpq calls. Preparation can itself yield
    # between sending the statement text and executing the prepared query.
    def _execute_send(self, *args, **kwargs):
        self.check_send()
        return super()._execute_send(*args, **kwargs)

    def _send_prepare(self, *args, **kwargs):
        self.check_send()
        return super()._send_prepare(*args, **kwargs)

    def _send_query_prepared(self, *args, **kwargs):
        self.check_send()
        return super()._send_query_prepared(*args, **kwargs)


class _LeaseCursor:
    def __init__(self, lease, cursor):
        self.connection = lease
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._cursor.close()

    def execute(self, *args, **kwargs):
        self.connection._active_connection()
        self._cursor.execute(*args, **kwargs)
        return self

    def stream(self, *args, **kwargs):
        raise psycopg.NotSupportedError("Database leases support eager execution only.")

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._cursor)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchmany(self, *args):
        return self._cursor.fetchmany(*args)

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount


class _Lease:
    """Expose the services' eager API. Cancellation belongs to this checkout."""
    def __init__(self, connection, cancel, close):
        self._connection = connection
        self.cancel = self.cancel_safe = cancel
        self.close = close

    def _active_connection(self):
        if self._connection is None:
            raise CommandNotSubmitted("Database lease has ended.")
        return self._connection

    @property
    def info(self):
        info = self._active_connection().info
        return SimpleNamespace(backend_pid=info.backend_pid, server_version=info.server_version,
                               transaction_status=info.transaction_status)

    @property
    def closed(self):
        return self._connection is None or self._connection.closed

    @property
    def read_only(self):
        return self._active_connection().read_only

    @read_only.setter
    def read_only(self, value):
        self._active_connection().read_only = value

    def execute(self, *args, **kwargs):
        return _LeaseCursor(self, self._active_connection().execute(*args, **kwargs))

    def cursor(self, *, binary=False, row_factory=None):
        return _LeaseCursor(self, self._active_connection().cursor(binary=binary, row_factory=row_factory))

    def commit(self):
        self._active_connection().commit()

    def rollback(self):
        self._active_connection().rollback()

    @contextmanager
    def transaction(self, *args, **kwargs):
        transaction = self._active_connection().transaction(*args, **kwargs)
        transaction.__enter__()
        try:
            yield
        except BaseException as exc:
            try:
                suppressed = transaction.__exit__(type(exc), exc, exc.__traceback__)
            except psycopg.transaction.OutOfOrderTransactionNesting:
                # Preserve the driver's structural error even during unwind.
                raise
            except Exception:
                # The deadline guard can refuse rollback before the driver's
                # own cleanup handler starts. Evict without erasing the cause.
                with suppress(Exception):
                    self.close()
                suppressed = False
            if not suppressed:
                raise
        else:
            closed_before_commit = self.closed
            transaction.__exit__(None, None, None)
            if closed_before_commit:
                # psycopg skips transaction exit on a closed connection. An
                # inner rollback failure must not turn that into outer success.
                raise CommandNotSubmitted("Transaction commit refused: the lease was closed.")


def discard_connection(connection, close=None):
    try:
        if close is None:
            close = connection.close
        close()
    except Exception:
        if not connection.closed:
            raise


def rollback_after_error(connection):
    """Best-effort cleanup while the original transaction failure unwinds."""
    try:
        if not connection.closed:
            connection.execute("ROLLBACK")
    except Exception:
        with suppress(Exception):
            connection.close()


@contextmanager
def acquisition():
    try:
        yield
    except (CommandNotSubmitted, CommandInterrupted) as exc:
        if isinstance(exc.__cause__, AdmissionTimeout):
            raise AdmissionTimeout("Database lease setup expired before borrower entry.") from exc
        raise AcquisitionError("Database lease setup failed before borrower entry.") from exc
    except psycopg.NotSupportedError:
        raise
    except psycopg.Error as exc:
        raise AcquisitionError("Database acquisition failed before borrower entry.") from exc


@contextmanager
def checked_io(connection, remaining, request=None):
    """Guard synchronous psycopg I/O, including implicit BEGIN and commit.

    The pinned driver's connection, cursor and transaction APIs all drive
    their protocol generators through Connection.wait. Gate the eager cursor
    sends after adaptation, poll active waits, and join the request-owned
    wire abort before releasing the backend.
    """
    if not isinstance(connection, psycopg.Connection):
        # Injected test connectors can supply a context manager without libpq.
        yield connection
        return
    psycopg.capabilities.has_cancel_safe(check=True)
    original_wait = connection.wait
    original_cancel = connection.cancel_safe
    original_close = connection.close
    cancel_lock = threading.RLock()
    cancel_requested = threading.Event()
    armed = True
    evict = False

    def cancel_safe(*, timeout=0.25):
        nonlocal evict
        # Parameter adaptation holds the driver lock. Latch before waiting so
        # the post-adaptation gate can refuse the still-unsubmitted command.
        cancel_requested.set()
        with cancel_lock:
            if armed and not evict:
                evict = True
                original_cancel(timeout=min(0.25, max(.001, timeout)))

    def abort_active():
        with cancel_lock:
            if armed and connection.info.transaction_status == psycopg.pq.TransactionStatus.ACTIVE:
                # A failed wire attempt still evicts. The borrower checks the
                # same cancellation on its next readiness poll.
                with suppress(psycopg.Error):
                    cancel_safe()

    def close():
        nonlocal armed, evict
        with cancel_lock:
            if armed:
                armed = False
                try:
                    discard_connection(connection, original_close)
                finally:
                    if not connection.closed:
                        evict = True

    def before_send():
        # Called inside the serialized driver step, after parameter adaptation.
        if not armed or evict or cancel_requested.is_set():
            raise CommandNotSubmitted("Database command refused before submission.")
        try:
            remaining()
        except AdmissionTimeout as exc:
            close()
            raise CommandNotSubmitted("Database command refused before submission.") from exc

    def cursor_factory(*args, **kwargs):
        cursor = _EagerCursor(*args, **kwargs)
        cursor.check_send = before_send
        return cursor

    def checked_wait(gen, interval=_POLL):
        nonlocal evict

        def drive():
            started = False
            ready = None
            while True:
                try:
                    # The driver step submits/polls; it yields *before*
                    # waiting for readiness. Serialize it with a wire abort.
                    with cancel_lock:
                        remaining()
                        if not armed or evict or cancel_requested.is_set():
                            if not started:
                                raise CommandNotSubmitted("Database command refused before submission.")
                            raise CommandInterrupted("Database connection was interrupted.")
                        # Even a first step can send and consume a reply
                        # without yielding. Only our pre-send gates can prove
                        # refusal once control passes to the driver.
                        started = True
                        try:
                            interest = gen.send(ready)
                        except StopIteration as done:
                            return done.value
                except AdmissionTimeout as exc:
                    if not started:
                        close()
                        raise CommandNotSubmitted("Database command refused before submission.") from exc
                    try:
                        cancel_safe()
                    except psycopg.Error as cancel_error:
                        raise CommandInterrupted("Wire cancel failed; command outcome uncertain.") from cancel_error
                    finally:
                        close()
                    raise CommandInterrupted("Database command interrupted; outcome uncertain.") from exc
                ready = yield interest

        driver = drive()
        try:
            return original_wait(driver, interval=_POLL if interval <= 0 else min(interval, _POLL))
        except (CommandNotSubmitted, CommandInterrupted):
            raise
        except (psycopg.OperationalError, OSError) as exc:
            sqlstate = getattr(exc, "sqlstate", None)
            # The waiter can lose its fd between polls, before libpq marks
            # the connection broken. A local dumper can also raise OSError,
            # but in that case the driver has unwound, not yielded for I/O.
            lost_wait = driver.gi_suspended and sqlstate is None
            if connection.broken or (sqlstate or "").startswith("08") or lost_wait:
                evict = True
                raise CommandInterrupted("Database reply lost; command outcome uncertain.") from exc
            raise
        except KeyboardInterrupt:
            # Connection.wait can itself issue a wire cancel on interrupt.
            evict = True
            raise
        finally:
            driver.close()
            gen.close()

    # Preserve connector-supplied instance overrides as well as class methods.
    methods = ("wait", "cancel_safe", "cursor_factory", "close")
    overrides = {key: vars(connection)[key] for key in methods
                 if key in vars(connection)}
    connection.wait = checked_wait
    connection.cancel_safe = cancel_safe
    connection.cursor_factory = cursor_factory
    connection.close = close
    lease = _Lease(connection, cancel_safe, close)
    try:
        disarm = request.arm(abort_active) if request is not None else None
        try:
            yield lease
        finally:
            if disarm is not None:
                disarm()
    finally:
        with cancel_lock:
            armed = False
            lease._connection = None
            for key in methods:
                if key in overrides:
                    setattr(connection, key, overrides[key])
                else:
                    delattr(connection, key)
            if evict:
                discard_connection(connection)


_READER_WAITERS = threading.BoundedSemaphore(32)


@contextmanager
def admission_slot(slots, timeout=2.0):
    start = time.monotonic()
    deadline = start + remaining_time(timeout)
    acquired = False
    if not _READER_WAITERS.acquire(blocking=False):
        raise AdmissionTimeout("Database admission queue is full; retry later.")
    try:
        while not acquired:
            remaining = min(remaining_time(), deadline - time.monotonic())
            if remaining <= 0:
                raise AdmissionTimeout("Database reader admission timed out; retry later.")
            acquired = slots.acquire(timeout=min(0.05, remaining))
        remaining_time()
    except BaseException:
        if acquired:
            slots.release()
        raise
    finally:
        _READER_WAITERS.release()
        state = _REQUEST.get()
        if state is not None:
            state.queue_seconds += time.monotonic() - start
    slot = _ConnectionSlot(slots.release)
    try:
        yield slot
    finally:
        if not slot.transferred:
            slots.release()


class ControlConnections:
    def __init__(self):
        self._condition = threading.Condition()
        self._idle = []
        self._active = 0
        self._waiters = 0
        self._local = threading.local()
        self.created = 0
        self.queue_seconds = 0.0
        self.database_seconds = 0.0

    def _release(self):
        with self._condition:
            self._active -= 1
            self._condition.notify()

    @contextmanager
    def connection(self, dsn, *, connect=psycopg.connect, deadline=None, cancelled=None):
        if getattr(self._local, "active", False):
            raise RuntimeError("Nested control acquisition must reuse its connection.")
        started = time.monotonic()
        state = _REQUEST.get()
        operation_deadline = math.inf if deadline is None else deadline
        expires = min(operation_deadline, started + QUEUE_TIMEOUT)

        def remaining():
            if cancelled is not None and cancelled.is_set():
                raise AdmissionTimeout("Control database operation cancelled.")
            return _remaining(operation_deadline, state.remaining() if state else 120.0)

        def acquisition_remaining():
            return _remaining(expires, remaining())

        with self._condition:
            if self._waiters >= MAX_WAITERS:
                raise AdmissionTimeout("Control database queue is full; retry later.")
            self._waiters += 1
            try:
                while self._active >= CAPACITY:
                    self._condition.wait(min(_POLL, acquisition_remaining()))
                acquisition_remaining()
                # One process-wide budget, even when tests or administration
                # construct multiple stores or point them at different DSNs.
                connection = None
                while self._idle:
                    key, candidate = self._idle.pop()
                    if key == dsn and not candidate.closed:
                        connection = candidate
                        break
                    discard_connection(candidate)
                self._active += 1
            finally:
                self._waiters -= 1
                self.queue_seconds += time.monotonic() - started
                if state is not None:
                    state.queue_seconds += time.monotonic() - started
        slot = _ConnectionSlot(self._release)
        self._local.active = True
        execution_started = time.monotonic()
        admitted = False

        def command_remaining():
            return remaining() if admitted else acquisition_remaining()

        try:
            with acquisition():
                if connection is None:
                    opener = partial(connect, dsn, autocommit=True, row_factory=dict_row,
                                     connect_timeout=2, prepare_threshold=None)
                    connection = slot.connect(opener, acquisition_remaining)
                    with self._condition:
                        self.created += 1
            with checked_io(connection, command_remaining, state) as lease:
                with acquisition():
                    acquisition_remaining()
                    connection.execute("SET SESSION search_path = pg_catalog, control")
                    connection.execute("SELECT set_config('statement_timeout', %s, false)",
                                       (str(max(1, int(min(10, remaining()) * 1000))),))
                    acquisition_remaining()
                admitted = True
                yield lease
        finally:
            reusable = False
            try:
                if connection is not None and not admitted:
                    discard_connection(connection)
                elif connection is not None and not connection.closed:
                    # rollback() also clears explicit failed transactions in
                    # autocommit mode. DISCARD removes roles, GUCs, temporary
                    # objects, prepared statements, LISTEN and advisory locks.
                    with checked_io(connection, partial(_remaining, time.monotonic() + QUEUE_TIMEOUT)):
                        connection.rollback()
                        connection.autocommit = True
                        connection.isolation_level = None
                        connection.read_only = None
                        connection.deferrable = None
                        connection.row_factory = dict_row
                        connection.prepare_threshold = None
                        connection.execute("DISCARD ALL")
                    reusable = not connection.closed
            except BaseException:
                discard_connection(connection)
            finally:
                self._local.active = False
                if state is not None:
                    state.database_seconds += time.monotonic() - execution_started
                with self._condition:
                    self.database_seconds += time.monotonic() - execution_started
                    if reusable:
                        self._idle.append((dsn, connection))
                    if not slot.transferred:
                        self._release()

    def close(self):
        with self._condition:
            while self._idle:
                discard_connection(self._idle.pop()[1])


CONTROL_CONNECTIONS = ControlConnections()
