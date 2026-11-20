"""Bounded control-role connections; keep the config-ui copy identical.

The deployed single OAuth process and single config API process each own three
slots, leaving two of the role's eight sessions for administration. Replicating
either service requires repartitioning this budget, not increasing the limit.
"""

from contextlib import contextmanager
from contextvars import ContextVar
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


@contextmanager
def request_scope(seconds=120.0, peer=None):
    try:
        seconds = float(seconds)
    except (ValueError, TypeError):
        seconds = 120.0
    if not math.isfinite(seconds):
        seconds = 120.0
    state = {"deadline": time.monotonic() + max(0, min(seconds, 120.0)),
             "queueSeconds": 0.0, "databaseSeconds": 0.0, "peer": peer}
    token = _REQUEST.set(state)
    try:
        yield state
    finally:
        _REQUEST.reset(token)


def remaining_time(default=120.0):
    state = _REQUEST.get()
    if state is not None and state["peer"] is not None:
        peer = state["peer"]
        try:
            readable, _, _ = select.select([peer], [], [], 0)
            if readable and peer.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                raise AdmissionTimeout("Request disconnected before database admission.")
        except (OSError, ValueError):
            raise AdmissionTimeout("Request disconnected before database admission.") from None
    remaining = default if state is None else min(default, state["deadline"] - time.monotonic())
    if remaining <= 0:
        raise AdmissionTimeout("Database operation deadline expired; retry later.")
    return remaining


def request_metrics():
    state = _REQUEST.get()
    return {} if state is None else {key: state[key] for key in ("queueSeconds", "databaseSeconds")}


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
            state["queueSeconds"] += time.monotonic() - start
    try:
        yield
    finally:
        slots.release()


class AdmissionTimeout(TimeoutError):
    """Retryable overload; no database work was started by this acquisition."""


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

    @contextmanager
    def connection(self, dsn, *, connect=psycopg.connect, deadline=None, cancelled=None):
        if getattr(self._local, "active", False):
            raise RuntimeError("Nested control acquisition must reuse its connection.")
        started = time.monotonic()
        state = _REQUEST.get()
        if state is not None:
            deadline = min(deadline if deadline is not None else float("inf"), state["deadline"])
        expires = min(deadline if deadline is not None else float("inf"), started + QUEUE_TIMEOUT)

        def expired():
            remaining_time()
            return time.monotonic() >= expires or (
                cancelled is not None and cancelled.is_set()
            )

        with self._condition:
            if self._waiters >= MAX_WAITERS:
                raise AdmissionTimeout("Control database queue is full; retry later.")
            self._waiters += 1
            try:
                while self._active >= CAPACITY:
                    if expired():
                        raise AdmissionTimeout("Control database admission expired; retry later.")
                    self._condition.wait(min(0.05, max(0, expires - time.monotonic())))
                if expired():
                    raise AdmissionTimeout("Control database admission expired; retry later.")
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
                    state["queueSeconds"] += time.monotonic() - started
        self._local.active = True
        execution_started = time.monotonic()
        try:
            if connection is None:
                connection = connect(
                    dsn, autocommit=True, row_factory=dict_row,
                    connect_timeout=2, prepare_threshold=None,
                )
                with self._condition:
                    self.created += 1
            if expired():
                raise AdmissionTimeout("Control database acquisition expired; retry later.")
            connection.execute("SET SESSION search_path = pg_catalog, control")
            connection.execute("SELECT set_config('statement_timeout', %s, false)",
                               (str(max(1, int(remaining_time(10) * 1000))),))
            yield connection
        finally:
            reusable = False
            if connection is not None:
                try:
                    # rollback() also clears explicit failed transactions in
                    # autocommit mode. DISCARD removes roles, GUCs, temporary
                    # objects, prepared statements, LISTEN and advisory locks.
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
                state["databaseSeconds"] += time.monotonic() - execution_started
            with self._condition:
                self.database_seconds += time.monotonic() - execution_started
                if reusable:
                    self._idle.append((dsn, connection))
                self._active -= 1
                self._condition.notify()

    def close(self):
        with self._condition:
            while self._idle:
                self._idle.pop()[1].close()


CONTROL_CONNECTIONS = ControlConnections()
