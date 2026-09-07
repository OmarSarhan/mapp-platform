"""Per-test isolation for the control schema.

The store used to get its isolation from a temporary directory: every test
built a ControlStore on a fresh path and could not see any other test's state.
Backed by PostgreSQL they all share one schema, so isolation has to be explicit
or tests pollute each other -- observed directly, as a suite whose failure count
changed between identical runs.

Truncation rather than a schema per test: the control role deliberately cannot
CREATE ON DATABASE, so it could not make one, and a fixture needing a privilege
production denies would be testing a role that does not exist.
"""

from __future__ import annotations

import os
import unittest

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - exercised by the skip below
    psycopg = None

DATABASE_URL = os.getenv("CONTROL_TEST_DATABASE_URL", "")


requires_control_database = unittest.skipUnless(
    DATABASE_URL and psycopg is not None,
    "set CONTROL_TEST_DATABASE_URL to a scratch PostgreSQL database to run the"
    " control-plane tests; its control schema is emptied before every test",
)


def _control_schema():
    """Import control_schema with the package root on the path.

    Deferred, and it has to stay deferred: this module is imported by test
    discovery before any test has adjusted sys.path, and control_schema
    imports psycopg -- so resolving it at module scope breaks collection for
    every suite that does not touch the control plane at all.

    An earlier version of this file assigned `TABLES = _control_schema()...`
    at module level, above this definition. That was a NameError at import and
    broke seven test modules; only the one suite with its own fixture still
    ran, which is why it was not noticed immediately.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import control_schema

    return control_schema


def tables_in_delete_order() -> tuple[str, ...]:
    """The schema's own delete order, resolved at call time.

    One list, defined in control_schema. Six fixtures each held their own copy
    until migration 6 broke all of them at once -- its oauth_refresh_families
    references oauth_clients, so a list written before it existed deleted the
    clients first and hit a foreign key.
    """
    return _control_schema().TABLES_IN_DELETE_ORDER


def reset_control_schema() -> None:
    """Empty every control table, migrating first if the schema is bare."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import control_schema

    connection = control_schema.connect(DATABASE_URL)
    try:
        control_schema.migrate(connection)
        connection.execute(
            "TRUNCATE TABLE "
            + ", ".join(
                f"control.{name}" for name in tables_in_delete_order()
            )
            + " CASCADE"
        )
    finally:
        connection.close()


@requires_control_database
class ControlStoreTestCase(unittest.TestCase):
    """Base for any test that constructs a ControlStore.

    Also pins CONTROL_DATABASE_URL for the duration, because the store reads it
    from the environment -- which is what keeps ControlStore(root) constructible
    with one argument, and so keeps the eleven demo and federation harness call
    sites working without edits.
    """

    def setUp(self) -> None:
        super().setUp()
        reset_control_schema()
        previous = os.environ.get("CONTROL_DATABASE_URL")
        os.environ["CONTROL_DATABASE_URL"] = DATABASE_URL

        def restore() -> None:
            if previous is None:
                os.environ.pop("CONTROL_DATABASE_URL", None)
            else:
                os.environ["CONTROL_DATABASE_URL"] = previous

        self.addCleanup(restore)


def control_rows(table: str) -> list[dict]:
    """Read a control table directly.

    Replaces the tests' former reach into ControlStore._state(): the assertions
    that used it are about persisted state, and the store no longer has an
    in-memory document to inspect.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import control_schema

    connection = control_schema.connect(DATABASE_URL)
    try:
        return [
            dict(row)
            for row in connection.execute(f"SELECT * FROM control.{table}").fetchall()
        ]
    finally:
        connection.close()
