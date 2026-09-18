"""What an advanced recovery epoch means for the credentials this component issues.

The requirement is one line of Phase 1's tested list: "a restore advances the
recovery epoch and cannot make a pre-restore grant/A mapping, refresh family or
B usable". The advance itself lives in the configuration service, which owns the
schema; the credentials live here. So this is the join, and the join is the only
place the requirement can actually be demonstrated.

It is also the payoff for the design choice. Nothing in this component knows the
epoch exists: the advance writes revocations, and every read here already
resolves through one. If that were not true, the epoch would need a predicate on
every credential read in both components -- which is what an earlier analysis
costed, and why it was nearly not built.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config-ui"))

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - exercised by the skip below
    psycopg = None

# Imported at module scope because the fixtures below read its shared
# table order; a method-local import left it invisible to setUp.
try:
    import control_schema as cs  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - covered by the skip
    cs = None  # type: ignore[assignment]

import canonical  # noqa: E402
import introspection  # noqa: E402
from models import Client, Grant, Token  # noqa: E402

DATABASE_URL = os.getenv("CONTROL_TEST_DATABASE_URL", "")
AUDIENCE = "http://config.localhost/api"
DIGEST = canonical.digest({"restore": True})


@unittest.skipUnless(
    DATABASE_URL and psycopg is not None,
    "set CONTROL_TEST_DATABASE_URL to a scratch PostgreSQL database",
)
class RecoveryEpochEffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["CONTROL_DATABASE_URL"] = DATABASE_URL
        connection = cs.connect(DATABASE_URL)
        try:
            cs.migrate(connection)
        finally:
            connection.close()

    def setUp(self) -> None:
        import control_plane
        from sql_store import SqlStore

        os.environ["CONTROL_DATABASE_URL"] = DATABASE_URL
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            for table in cs.TABLES_IN_DELETE_ORDER:
                connection.execute(f"DELETE FROM control.{table}")

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.control = control_plane.ControlStore(Path(directory.name) / "control")
        if not self.control.initialize("correct horse battery staple", "instance"):
            self.control.reset_password("correct horse battery staple")

        self.store = SqlStore(DATABASE_URL)
        self.store.add_client(
            Client(
                client_id="agent",
                name="Agent",
                redirect_uris=("https://agent.example/cb",),
                scopes=("apply",),
                token_endpoint_auth_method="none",
            )
        )
        self.store.save_grant(
            Grant(
                grant_id="oauth:before",
                client_id="agent",
                subject="operator",
                scopes=("apply",),
            )
        )

    def mint_token_a(self, raw: str, subject: str = "oauth:before") -> None:
        now = dt.datetime.now(dt.timezone.utc)
        self.store.save_token(
            raw,
            Token(
                token_hash="",
                client_id="agent",
                scope="apply",
                subject=subject,
                issued_at=int(now.timestamp()),
                expires_in=900,
                audience="http://mcp.localhost/mcp",
            ),
        )

    def mint_token_b(self, raw: str, subject: str = "oauth:before") -> None:
        issued = dt.datetime.now(dt.timezone.utc)
        self.store.save_exchanged_token(
            raw,
            client_id="agent",
            actor_client_id="agent",
            subject=subject,
            scope="apply",
            audience=AUDIENCE,
            issued_at=issued,
            expires_at=issued + dt.timedelta(seconds=60),
            operation_id="proposals.apply",
            request_digest=DIGEST,
            single_use=True,
        )

    # -- the requirement -------------------------------------------------

    def test_a_pre_restore_grant_is_not_usable(self) -> None:
        self.assertFalse(self.store.query_grant("oauth:before").is_revoked())
        self.control.advance_recovery_epoch(reason="restore")
        self.assertTrue(self.store.query_grant("oauth:before").is_revoked())

    def test_a_pre_restore_token_a_is_not_usable(self) -> None:
        self.mint_token_a("mapp_a_before")
        self.assertFalse(self.store.query_token("mapp_a_before").is_revoked())
        self.control.advance_recovery_epoch(reason="restore")
        self.assertTrue(self.store.query_token("mapp_a_before").is_revoked())

    def test_a_pre_restore_token_b_cannot_be_spent(self) -> None:
        """The one that matters most: a spendable credential for a real effect."""
        self.mint_token_b("mapp_b_before")
        self.control.advance_recovery_epoch(reason="restore")
        self.assertIsNone(
            self.store.consume_exchanged_token(
                "mapp_b_before", "proposals.apply", DIGEST
            )
        )

    def test_a_pre_restore_token_b_cannot_be_verified_either(self) -> None:
        """A read operation's token has no consume path, so this is its only gate."""
        self.mint_token_b("mapp_b_read")
        self.assertIsNotNone(self.store.exchanged_binding("mapp_b_read"))
        self.control.advance_recovery_epoch(reason="restore")
        self.assertIsNone(self.store.exchanged_binding("mapp_b_read"))

    def test_introspection_reports_a_pre_restore_token_inactive(self) -> None:
        from collections import defaultdict

        self.mint_token_a("mapp_a_introspect")
        self.control.advance_recovery_epoch(reason="restore")
        datalist: defaultdict = defaultdict(list)
        datalist["token"].append("mapp_a_introspect")
        self.assertEqual(
            {"active": False},
            introspection.introspect(datalist=datalist, store=self.store),
        )

    # -- and it does not overreach ---------------------------------------

    def test_a_credential_minted_after_the_advance_still_works(self) -> None:
        """Otherwise the platform would be unusable after every restore.

        The column default stamps a new row with the current epoch, which is
        the whole reason no INSERT in this component had to change.
        """
        self.control.advance_recovery_epoch(reason="restore")
        self.store.save_grant(
            Grant(
                grant_id="oauth:after",
                client_id="agent",
                subject="operator",
                scopes=("apply",),
            )
        )
        self.mint_token_b("mapp_b_after", subject="oauth:after")
        self.assertFalse(self.store.query_grant("oauth:after").is_revoked())
        self.assertIsNotNone(
            self.store.consume_exchanged_token(
                "mapp_b_after", "proposals.apply", DIGEST
            )
        )

    def test_the_client_registration_survives_a_restore(self) -> None:
        """An operator should not have to re-register every agent."""
        self.control.advance_recovery_epoch(reason="restore")
        self.assertIsNotNone(self.store.query_client("agent"))

    def test_a_second_restore_invalidates_what_the_first_left(self) -> None:
        """Each advance invalidates what predates *it*, not only the first one."""
        self.control.advance_recovery_epoch(reason="first restore")
        self.store.save_grant(
            Grant(
                grant_id="oauth:between",
                client_id="agent",
                subject="operator",
                scopes=("apply",),
            )
        )
        self.assertFalse(self.store.query_grant("oauth:between").is_revoked())
        self.control.advance_recovery_epoch(reason="second restore")
        self.assertTrue(self.store.query_grant("oauth:between").is_revoked())


if __name__ == "__main__":
    unittest.main()
