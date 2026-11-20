"""Request-local authority and target facts for config mutation admission."""

from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass, field
import fcntl
import time

from control_connections import (
    AcquisitionError, AdmissionTimeout, CommandInterrupted, CommandNotSubmitted, current_request, remaining_time,
)


class ReceiptRefused(Exception):
    """No approved receipt covers this effect."""


@dataclass
class MutationState:
    token_b: str = "not_submitted"
    receipt: str = "not_required"
    target_started: bool = False
    recovery: dict = field(default_factory=dict)
    published: dict = field(default_factory=dict)
    admission_record_id: str | None = None
    _spend: Callable | None = field(default=None, repr=False)

    @property
    def retryable(self):
        return (not self.target_started and not self.published and self.admission_record_id is None
                and self.receipt in {"not_required", "pending"}
                and self.token_b not in {"unconfirmed", "refused"})

    def facts(self):
        """Credential-free snapshot for error mapping and authorized recovery."""
        recovery = dict(self.recovery)
        if self.receipt not in {"not_required", "consumed"}:
            recovery = {key: recovery[key] for key in ("approvalReference", "requestId", "operationId")
                        if key in recovery}
        return {"tokenB": self.token_b, "receipt": self.receipt,
                "targetStarted": self.target_started, "published": dict(self.published),
                "recovery": recovery}

    def require_receipt(self, reference, spend):
        self.recovery["approvalReference"] = reference
        self.receipt = "pending"
        self._spend = spend

    def admit(self, connection=None):
        remaining_time()
        if self.receipt == "pending":
            try:
                self._spend(connection)
            except (AcquisitionError, CommandInterrupted, CommandNotSubmitted) as exc:
                # Derived and federation adapters translate driver errors into
                # target failures. This command belongs to the control store.
                raise AdmissionTimeout("Receipt admission interrupted.") from exc
            finally:
                if self.receipt != "pending":
                    self._spend = None
        if self.receipt not in {"not_required", "consumed"}:
            raise ReceiptRefused()
        remaining_time()

    def begin(self, connection=None):
        self.admit(connection)
        self.target_started = True


def target_command(connection, statement, params):
    """Preserve earlier effects when this specific command did not write."""
    state = mutation_state()
    previous = state.target_started
    state.begin(connection)
    try:
        cursor = connection.execute(statement, params)
    except CommandNotSubmitted:
        state.target_started = previous
        raise
    if cursor.rowcount == 0:
        state.target_started = previous
    return cursor


def mutation_state():
    request = current_request()
    if request is None:
        return MutationState()
    if not hasattr(request, "mutation"):
        request.mutation = MutationState()
    return request.mutation


@contextmanager
def request_lock(lock):
    if current_request() is None:
        with lock:
            yield
        return
    while not lock.acquire(timeout=remaining_time(.025)):
        pass
    try:
        remaining_time()
        yield
    finally:
        lock.release()


def request_flock(fd, mode):
    if current_request() is None:
        fcntl.flock(fd, mode)
        return
    while True:
        remaining_time()
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            time.sleep(remaining_time(.025))
    try:
        remaining_time()
    except AdmissionTimeout:
        fcntl.flock(fd, fcntl.LOCK_UN)
        raise
