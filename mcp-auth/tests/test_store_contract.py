"""Runs the store contract against both implementations.

The stub half always runs. The SQL half runs only when
``CONTROL_TEST_DATABASE_URL`` points at a scratch database, and skips loudly
rather than passing on nothing -- the same gate test_sql_store.py uses.

Two concrete classes, one set of assertions. That is the whole point: a rule
asserted here cannot hold for one store and not the other without failing.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config-ui"))

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - exercised by the skip below
    psycopg = None

from store_contract import StoreContractTests  # noqa: E402
from stub_store import StubStore  # noqa: E402

DATABASE_URL = os.getenv("CONTROL_TEST_DATABASE_URL", "")

requires_database = unittest.skipUnless(
    DATABASE_URL and psycopg is not None,
    "set CONTROL_TEST_DATABASE_URL to a scratch PostgreSQL database to run the"
    " SqlStore half of the store contract",
)


class StubStoreContractTests(StoreContractTests, unittest.TestCase):
    def setUp(self) -> None:
        self.store = StubStore()


@requires_database
class SqlStoreContractTests(StoreContractTests, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import control_schema as cs

        connection = cs.connect(DATABASE_URL)
        try:
            cs.migrate(connection)
        finally:
            connection.close()

    def setUp(self) -> None:
        from sql_store import SqlStore

        self.store = SqlStore(DATABASE_URL)
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            # Child before parent: the only foreign keys in the control schema
            # are client_id -> oauth_clients, so clients must go last. Nothing
            # references oauth_grants -- that linkage is enforced in the
            # application, which is why the contract tests it.
            for table in (
                "oauth_authorization_codes",
                "oauth_pending_authorizations",
                "oauth_tokens",
                "oauth_sessions",
                "oauth_grants",
                "oauth_clients",
            ):
                connection.execute(f"DELETE FROM control.{table}")


if __name__ == "__main__":
    unittest.main()
