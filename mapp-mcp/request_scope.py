"""Local lifetime of one MCP POST. Cancellation registrations belong to its loop."""

import asyncio
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import partial
import math
import threading
import time


CURRENT_REQUEST = ContextVar("mapp_request", default=None)
_CHILD_DEADLINE = ContextVar("mapp_child_deadline", default=math.inf)


class RequestStopped(TimeoutError):
    pass


class RequestScope:
    def __init__(self, cap=None):
        self.start = time.monotonic()
        self.loop = asyncio.get_running_loop()
        self.caller_deadline = math.inf
        if cap is not None:
            try:
                seconds = float(cap)
            except ValueError:
                seconds = 0.0
            self.caller_deadline = self.start + (seconds if math.isfinite(seconds) else 0.0)
        self.deadline = min(self.caller_deadline, self.start + 120.0)
        self.closed = False
        self._active = set()
        self._lock = threading.Lock()
        self._hops = set()

    def tighten(self, seconds):
        self.deadline = min(self.deadline, self.start + seconds)
        self.remaining()

    def remaining(self, cap=math.inf):
        remaining = min(cap, min(self.deadline, _CHILD_DEADLINE.get()) - time.monotonic())
        if self.closed or remaining <= 0:
            raise RequestStopped("The MCP request ended or its operation deadline expired.")
        return remaining

    def close(self):
        with self._lock:
            self.closed = True
            hops = tuple(self._hops)
        for hop in hops:
            hop.abort()
        for scope in tuple(self._active):
            scope.cancel()

    @contextmanager
    def own_hop(self, hop):
        with self._lock:
            self.remaining()
            self._hops.add(hop)
        try:
            yield
        finally:
            with self._lock:
                self._hops.remove(hop)

    @contextmanager
    def active(self, cap=math.inf):
        """Arm a loop-local wait, including admission to an AnyIO worker."""
        import anyio

        with anyio.CancelScope(deadline=anyio.current_time() + self.remaining(cap)) as scope:
            self._active.add(scope)
            try:
                yield
            finally:
                self._active.remove(scope)
        if scope.cancel_called:
            raise RequestStopped("The MCP request ended or its operation deadline expired.")

    @contextmanager
    def during_decision(self, operation_seconds):
        self.remaining()
        self.deadline = self.caller_deadline
        try:
            with self.active():
                yield
        finally:
            self.deadline = min(self.caller_deadline, time.monotonic() + operation_seconds)


def human_wait(operation_seconds):
    # Keep origin cancellation and an explicit caller limit live through the
    # existing elicitation/poll policy. Bound post-decision work separately.
    request = CURRENT_REQUEST.get()
    return request.during_decision(operation_seconds) if request else nullcontext()


@contextmanager
def child_operation(seconds):
    token = _CHILD_DEADLINE.set(min(_CHILD_DEADLINE.get(), time.monotonic() + seconds))
    try:
        yield
    finally:
        _CHILD_DEADLINE.reset(token)


def checked(fn, *args, **kwargs):
    request = CURRENT_REQUEST.get()
    if request is not None:
        request.remaining()
    result = fn(*args, **kwargs)
    if request is not None:
        request.remaining()
    return result


async def run_worker(fn, *args, **kwargs):
    import anyio

    work = partial(checked, fn, *args, **kwargs)
    request = CURRENT_REQUEST.get()
    if request is None:
        return await anyio.to_thread.run_sync(work)
    with request.active():
        return await anyio.to_thread.run_sync(work)
