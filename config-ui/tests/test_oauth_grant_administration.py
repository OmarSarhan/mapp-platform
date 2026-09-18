"""Reading and withdrawing consents from the dashboard.

A grant is what an administrator actually approved: one client, one scope set,
at one moment. Registering a client grants nothing, and disabling one says only
that the software may no longer ask. Revoking is the separate act that withdraws
what an agent was already allowed to do, and until now the platform had no
surface for it at all -- `mcp-auth` could revoke through its control listener,
and nothing an operator could reach ever called it.

The correctness claim being tested is narrow and load-bearing: one conditional
write invalidates every credential derived from the grant, because every path
that reads a token resolves it through the grant. So these assert the write and
its exactly-once reporting, not a token-by-token sweep that does not happen.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from control_plane import ControlStore

from control_fixture import ControlStoreTestCase


class GrantTestCase(ControlStoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ControlStore(Path(self.directory.name) / "control")
        self.store.initialize("correct horse battery staple", "instance")
        self.client_id = self.store.register_oauth_client(
            name="Someone's laptop",
            redirect_uris=["http://localhost:8484/callback"],
            scopes=["mcp:connect", "inspect"],
        )

    def add_grant(self, grant_id: str, *, families: int = 0, revoked: bool = False):
        """A grant written directly.

        `save_grant` belongs to the authorization component's store, which this
        one deliberately does not import: they share a schema, not a module. So
        the row is written here as the broker would write it, which is also the
        only way to set up a state the dashboard can only ever observe.
        """
        with self.store._db() as connection:
            connection.execute(
                "INSERT INTO control.oauth_grants"
                "(grant_id, client_id, subject, scopes, revoked_at)"
                " VALUES(%s,%s,%s,%s, CASE WHEN %s THEN now() END)",
                (grant_id, self.client_id, "admin",
                 ["mcp:connect", "inspect"], revoked),
            )
            for index in range(families):
                connection.execute(
                    "INSERT INTO control.oauth_refresh_families"
                    "(family_id, grant_id, client_id, scope, absolute_expires_at)"
                    " VALUES(%s,%s,%s,%s, now() + interval '30 days')",
                    (f"{grant_id}-family-{index}", grant_id, self.client_id,
                     "mcp:connect inspect"),
                )

    def events(self, name: str) -> list[dict]:
        return [event for event in self.store.audit_tail() if event["event"] == name]


class ListingTests(GrantTestCase):
    def test_a_grant_is_reported_with_the_client_that_holds_it(self) -> None:
        """The client id alone is not something an operator recognises."""
        self.add_grant("grant-1")
        (grant,) = self.store.list_oauth_grants()
        self.assertEqual("grant-1", grant["grantId"])
        self.assertEqual(self.client_id, grant["clientId"])
        self.assertEqual("Someone's laptop", grant["clientName"])
        self.assertEqual(["mcp:connect", "inspect"], grant["scopes"])
        self.assertIsNone(grant["revoked"])

    def test_live_families_are_counted_and_revoked_ones_are_not(self) -> None:
        """The difference between "this consent exists" and "something is still
        using it". A grant with no live family cannot mint another access
        token, which is worth seeing before deciding to revoke."""
        self.add_grant("grant-1", families=3)
        with self.store._db() as connection:
            connection.execute(
                "UPDATE control.oauth_refresh_families SET revoked_at = now()"
                " WHERE family_id = %s",
                ("grant-1-family-0",),
            )
        (grant,) = self.store.list_oauth_grants()
        self.assertEqual(2, grant["liveFamilies"])

    def test_withdrawn_grants_are_still_listed(self) -> None:
        """A revoked grant is the evidence that a consent existed and was
        withdrawn. Dropping it would leave the audit log as the only record."""
        self.add_grant("grant-live")
        self.add_grant("grant-gone", revoked=True)
        listed = {grant["grantId"]: grant for grant in self.store.list_oauth_grants()}
        self.assertEqual({"grant-live", "grant-gone"}, set(listed))
        self.assertIsNone(listed["grant-live"]["revoked"])
        self.assertIsNotNone(listed["grant-gone"]["revoked"])

    def test_an_empty_registry_is_an_empty_list_not_an_error(self) -> None:
        self.assertEqual([], self.store.list_oauth_grants())


class RevocationTests(GrantTestCase):
    def test_revoking_is_reported_exactly_once(self) -> None:
        """The statement is conditional precisely so two operators revoking at
        once cannot both believe they acted."""
        self.add_grant("grant-1")
        self.assertTrue(self.store.revoke_oauth_grant("grant-1", actor="admin"))
        self.assertFalse(self.store.revoke_oauth_grant("grant-1", actor="admin"))

    def test_an_unknown_grant_is_reported_not_raised(self) -> None:
        self.assertFalse(self.store.revoke_oauth_grant("nosuch", actor="admin"))
        self.assertEqual([], self.events("oauth.grant_revoked"))

    def test_revoking_withdraws_the_refresh_families_too(self) -> None:
        """Not what makes revocation correct -- the rotation statement joins the
        grant and would refuse them anyway -- but leaving them live-looking
        gives an operator something to puzzle over."""
        self.add_grant("grant-1", families=2)
        self.assertTrue(self.store.revoke_oauth_grant("grant-1", actor="admin"))
        with self.store._db() as connection:
            live = connection.execute(
                "SELECT count(*) AS live FROM control.oauth_refresh_families"
                " WHERE grant_id = %s AND revoked_at IS NULL",
                ("grant-1",),
            ).fetchone()["live"]
        self.assertEqual(0, live)
        self.assertEqual(0, self.store.list_oauth_grants()[0]["liveFamilies"])

    def test_the_reason_is_recorded_against_the_grant(self) -> None:
        self.add_grant("grant-1")
        self.store.revoke_oauth_grant("grant-1", reason="laptop lost", actor="admin")
        self.assertEqual("laptop lost", self.store.list_oauth_grants()[0]["revokedReason"])

    def test_no_reason_leaves_the_column_null_rather_than_empty(self) -> None:
        """The schema's CHECK allows a null reason on a revoked grant; an empty
        string would be a reason that says nothing and reads as one."""
        self.add_grant("grant-1")
        self.store.revoke_oauth_grant("grant-1", actor="admin")
        self.assertIsNone(self.store.list_oauth_grants()[0]["revokedReason"])

    def test_one_grant_is_revoked_and_not_its_neighbours(self) -> None:
        self.add_grant("grant-1", families=1)
        self.add_grant("grant-2", families=1)
        self.store.revoke_oauth_grant("grant-1", actor="admin")
        listed = {grant["grantId"]: grant for grant in self.store.list_oauth_grants()}
        self.assertIsNotNone(listed["grant-1"]["revoked"])
        self.assertIsNone(listed["grant-2"]["revoked"])
        self.assertEqual(1, listed["grant-2"]["liveFamilies"])

    def test_revoking_does_not_disable_the_client(self) -> None:
        """They are different acts. A client may hold several consents, and one
        being withdrawn says nothing about whether the software may still ask."""
        self.add_grant("grant-1")
        self.store.revoke_oauth_grant("grant-1", actor="admin")
        (client,) = [
            item for item in self.store.list_oauth_clients()
            if item["clientId"] == self.client_id
        ]
        self.assertIsNone(client["disabled"])


class RevocationAuditTests(GrantTestCase):
    def test_a_withdrawal_records_who_did_it_and_what_it_covered(self) -> None:
        self.add_grant("grant-1", families=2)
        self.store.revoke_oauth_grant("grant-1", reason="laptop lost", actor="admin")
        (event,) = self.events("oauth.grant_revoked")
        self.assertEqual("admin", event["actor"])
        self.assertEqual("grant-1", event["details"]["grantId"])
        self.assertEqual(self.client_id, event["details"]["clientId"])
        self.assertEqual("laptop lost", event["details"]["reason"])
        self.assertEqual(2, event["details"]["refreshFamiliesRevoked"])

    def test_a_second_call_records_nothing(self) -> None:
        """An audit trail claiming two withdrawals of one consent would undo the
        exactly-once reporting the conditional statement exists to give."""
        self.add_grant("grant-1")
        self.store.revoke_oauth_grant("grant-1", actor="admin")
        self.store.revoke_oauth_grant("grant-1", actor="admin")
        self.assertEqual(1, len(self.events("oauth.grant_revoked")))

    def test_an_absent_reason_is_recorded_as_unspecified(self) -> None:
        """Rather than as an empty string, which reads as a reason that was
        given and said nothing."""
        self.add_grant("grant-1")
        self.store.revoke_oauth_grant("grant-1", actor="admin")
        (event,) = self.events("oauth.grant_revoked")
        self.assertEqual("unspecified", event["details"]["reason"])

    def test_the_entry_carries_no_credential(self) -> None:
        self.add_grant("grant-1")
        self.store.revoke_oauth_grant("grant-1", actor="admin")
        (event,) = self.events("oauth.grant_revoked")
        serialized = str(event).lower()
        for forbidden in ("secret", "password", "token", "hash"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
