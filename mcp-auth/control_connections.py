"""Bounded control-role connections; keep the config-ui copy identical.

The deployed single OAuth process and single config API process each own three
slots, leaving two of the role's eight sessions for administration. Replicating
either service requires repartitioning this budget, not increasing the limit.
"""

from concurrent.futures import Future, wait
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
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
        try:
            with self._condition:
                self.remaining()
                self._watchers.add(watcher)
                try:
                    watcher.start()
                except BaseException:
                    self._watchers.remove(watcher)
                    raise
        except AdmissionTimeout:
            abort()
            raise

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
                        connection.close()
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


@contextmanager
def checked_io(connection, remaining):
    """Guard synchronous psycopg I/O, including implicit BEGIN and commit.

    The pinned driver's connection, cursor and transaction APIs all drive
    their protocol generators through Connection.wait. Check before starting
    and at every readiness poll. The borrower drives deadline cancellation.
    Serialize an explicit caller cancellation with eviction on scope exit.
    """
    if not isinstance(connection, psycopg.Connection):
        # Injected test connectors can supply a context manager without libpq.
        yield
        return
    psycopg.capabilities.has_cancel_safe(check=True)
    original_wait = connection.wait
    original_cancel = connection.cancel_safe
    original_close = connection.close
    cancel_lock = threading.RLock()
    armed = True
    evict = False

    def cancel_safe(*, timeout=0.25):
        nonlocal evict
        with cancel_lock:
            if armed and not evict:
                evict = True
                original_cancel(timeout=min(0.25, max(.001, timeout)))

    def close():
        nonlocal armed
        with cancel_lock:
            if armed:
                armed = False
                original_close()

    def checked_wait(gen, interval=_POLL):
        nonlocal evict

        def drive():
            ready = None
            started = False
            while True:
                try:
                    # The driver step submits/polls; it yields *before*
                    # waiting for readiness. Serialize it with a wire abort.
                    with cancel_lock:
                        if not armed or evict:
                            raise CommandInterrupted("Database connection was interrupted.")
                        remaining()
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
                started = True
                ready = yield interest

        try:
            return original_wait(drive(), interval=_POLL if interval <= 0 else min(interval, _POLL))
        except KeyboardInterrupt:
            # Connection.wait can itself issue a wire cancel on interrupt.
            evict = True
            raise

    # Preserve connector-supplied instance overrides as well as class methods.
    methods = ("wait", "cancel", "cancel_safe", "close")
    overrides = {key: vars(connection)[key] for key in methods
                 if key in vars(connection)}
    connection.wait = checked_wait
    connection.cancel = connection.cancel_safe = cancel_safe
    connection.close = close
    try:
        yield
    finally:
        with cancel_lock:
            armed = False
            if evict:
                original_close()
            for key in methods:
                if key in overrides:
                    setattr(connection, key, overrides[key])
                else:
                    delattr(connection, key)


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
                self._active += 1
                # One process-wide budget, even when tests or administration
                # construct multiple stores or point them at different DSNs.
                connection = None
                while self._idle:
                    key, candidate = self._idle.pop()
                    if key == dsn and not candidate.closed:
                        connection = candidate
                        break
                    candidate.close()
            finally:
                self._waiters -= 1
                self.queue_seconds += time.monotonic() - started
                if state is not None:
                    state.queue_seconds += time.monotonic() - started
        slot = _ConnectionSlot(self._release)
        self._local.active = True
        execution_started = time.monotonic()
        admitted = False
        try:
            if connection is None:
                opener = partial(connect, dsn, autocommit=True, row_factory=dict_row,
                                 connect_timeout=2, prepare_threshold=None)
                connection = slot.connect(opener, acquisition_remaining)
                with self._condition:
                    self.created += 1
            with checked_io(connection, acquisition_remaining):
                acquisition_remaining()
                connection.execute("SET SESSION search_path = pg_catalog, control")
                connection.execute("SELECT set_config('statement_timeout', %s, false)",
                                   (str(max(1, int(min(10, remaining()) * 1000))),))
                acquisition_remaining()
            with checked_io(connection, remaining):
                remaining()
                admitted = True
                yield connection
        finally:
            reusable = False
            if connection is not None and not admitted:
                connection.close()
            if connection is not None and not connection.closed:
                try:
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
                    connection.close()
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
                self._idle.pop()[1].close()


CONTROL_CONNECTIONS = ControlConnections()
