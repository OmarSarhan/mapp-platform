"""Process-wide admission for configured ``DBS_*`` reader connections."""

from contextlib import ExitStack, contextmanager
from functools import partial
import threading
import time
from typing import Any, Callable, Iterator
from control_connections import (
    AdmissionTimeout, acquisition, admission_slot, checked_io, current_request,
    discard_connection, remaining_time,
)


# Live and preview XYZ can occupy forty sessions using the same runtime role.
# Admit at most eight configuration-service sessions so the database role can
# reserve two further reader sessions for probes and operator diagnostics.
MAX_CONCURRENT_DBS_CONNECTIONS = 8
DBS_ADMISSION_TIMEOUT_SECONDS = 5.0
_DBS_CONNECTION_SLOTS = threading.BoundedSemaphore(
    MAX_CONCURRENT_DBS_CONNECTIONS
)
_DBS_CONNECTION_STATE = threading.local()


@contextmanager
def dbs_connection(
    connect: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Iterator[Any]:
    """Open one ``DBS_*`` connection within the shared admission bound.

    The caller supplies its module-local ``psycopg.connect`` function so tests
    can keep patching that established seam.  Acquire before connecting and
    release only after the connection context closes; failed connects release
    the slot through normal context-manager unwinding.

    Callers must not open a second admitted connection while holding the first;
    pass an existing connection down instead.  Every current call site is
    single-connection or sequential, and the contract test guards that shape.
    """

    if getattr(_DBS_CONNECTION_STATE, "active", False):
        raise RuntimeError(
            "Nested DBS_* connections must reuse the admitted connection."
        )
    request = current_request()
    remaining = request.remaining if request else remaining_time
    deadline = time.monotonic() + remaining(DBS_ADMISSION_TIMEOUT_SECONDS)

    def acquisition_remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise AdmissionTimeout("Database reader admission timed out; retry later.")
        return remaining(seconds)

    with admission_slot(_DBS_CONNECTION_SLOTS, acquisition_remaining()) as slot:
        _DBS_CONNECTION_STATE.active = True
        try:
            with acquisition():
                raw = slot.connect(partial(connect, *args, **kwargs), acquisition_remaining)
            admitted = False

            def command_remaining():
                return remaining() if admitted else acquisition_remaining()

            try:
                with ExitStack() as stack:
                    with acquisition():
                        lease = stack.enter_context(checked_io(raw, command_remaining, request))
                        connection = stack.enter_context(raw)
                        acquisition_remaining()
                    admitted = True
                    yield connection if lease is raw else lease
            finally:
                # Driver __exit__ can itself raise while committing.
                if not getattr(raw, "closed", True):
                    discard_connection(raw)
        finally:
            _DBS_CONNECTION_STATE.active = False
