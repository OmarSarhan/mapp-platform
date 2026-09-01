"""Process-wide admission for configured ``DBS_*`` reader connections."""

from contextlib import contextmanager
import threading
from typing import Any, Callable, Iterator


# Live and preview XYZ can occupy forty sessions using the same runtime role.
# Admit at most eight configuration-service sessions so the database role can
# reserve two further reader sessions for probes and operator diagnostics.
MAX_CONCURRENT_DBS_CONNECTIONS = 8
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
    with _DBS_CONNECTION_SLOTS:
        _DBS_CONNECTION_STATE.active = True
        try:
            with connect(*args, **kwargs) as connection:
                yield connection
        finally:
            _DBS_CONNECTION_STATE.active = False
