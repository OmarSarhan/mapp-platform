"""The approval record: one decision, about one request, spent once.

Driven against PostgreSQL rather than a fake, because every property here is a
property of a statement -- the predicate and the transition are the same
statement, which is what makes a second spend a miss rather than a race.
"""
from __future__ import annotations

import datetime as dt
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_fixture import ControlStoreTestCase  # noqa: E402
from control_plane import ControlStore  # noqa: E402


DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


class ApprovalTests(ControlStoreTestCase):
    def store(self) -> ControlStore:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        store = ControlStore(Path(directory))
        store.initialize("correct horse battery staple")
        return store

    def request(self, store, **over):
        fields = {
            "grant_id": "oauth:grant-1",
            "client_id": "mcp-1",
            "instance": "http://mcp.localhost/mcp",
            "operation_id": "proposals.apply",
            "tool": "proposals_apply",
            "request_digest": DIGEST,
            "risk": "apply",
            "scopes": ["apply"],
            "packet": {"summary": "Rename Bus Stops", "changes": 1},
        }
        fields.update(over)
        return store.create_approval(**fields)

    def test_a_new_request_is_pending_and_spends_nothing(self) -> None:
        """The handle names a row; it is not authority."""
        store = self.store()
        handle = self.request(store)
        record = store.read_approval(handle)
        self.assertEqual("pending", record["status"])
        self.assertIsNone(record["decided_at"])
        self.assertIsNone(store.redeem_receipt(handle, request_digest=DIGEST))

    def test_the_packet_is_kept_so_a_decision_can_be_audited(self) -> None:
        """What the person was shown, not a reconstruction of it."""
        store = self.store()
        record = store.read_approval(self.request(store))
        self.assertEqual({"summary": "Rename Bus Stops", "changes": 1},
                         record["packet"])

    def test_approval_mints_a_receipt_that_spends_once(self) -> None:
        store = self.store()
        handle = self.request(store)
        self.assertTrue(store.decide_approval(handle, approved=True,
                                              decided_by="admin"))
        receipt = store.claim_receipt(handle)
        self.assertIsNotNone(receipt)

        spent = store.redeem_receipt(receipt, request_digest=DIGEST)
        self.assertEqual("proposals.apply", spent["operation_id"])
        self.assertEqual("admin", spent["decided_by"])

        # The second attempt is a miss, not a second effect.
        self.assertIsNone(store.redeem_receipt(receipt, request_digest=DIGEST))

    def test_a_consumed_row_is_kept_so_a_replay_is_detectable(self) -> None:
        """Deleting it would make a replay indistinguishable from a receipt
        that never existed."""
        store = self.store()
        handle = self.request(store)
        store.decide_approval(handle, approved=True, decided_by="a")
        receipt = store.claim_receipt(handle)
        store.redeem_receipt(receipt, request_digest=DIGEST)
        self.assertEqual("consumed", store.read_approval(handle)["status"])

    def test_a_receipt_does_not_travel_to_another_request(self) -> None:
        """The digest is in the predicate, not checked afterwards. An approval
        carried to a different path, body or revision than the one a person saw
        does not match the row that would otherwise be spent."""
        store = self.store()
        handle = self.request(store)
        store.decide_approval(handle, approved=True, decided_by="admin")
        receipt = store.claim_receipt(handle)
        self.assertIsNone(
            store.redeem_receipt(receipt, request_digest=OTHER_DIGEST))
        # And is still spendable for the request it was actually issued for.
        self.assertIsNotNone(store.redeem_receipt(receipt, request_digest=DIGEST))

    def test_a_decline_mints_nothing(self) -> None:
        store = self.store()
        handle = self.request(store)
        self.assertTrue(store.decide_approval(handle, approved=False,
                                              decided_by="admin"))
        self.assertEqual("declined", store.read_approval(handle)["status"])
        # Nothing to claim: a decline mints no secret at all.
        self.assertIsNone(store.claim_receipt(handle))

    def test_a_decision_cannot_be_changed(self) -> None:
        """The predicate requires `pending`, which a decided row is not. Two
        operators deciding at once cannot both believe they did it."""
        store = self.store()
        handle = self.request(store)
        self.assertTrue(store.decide_approval(handle, approved=True,
                                              decided_by="first"))
        self.assertFalse(store.decide_approval(handle, approved=False,
                                               decided_by="second"))
        self.assertFalse(store.decide_approval(handle, approved=True,
                                               decided_by="third"))
        self.assertEqual("first", store.read_approval(handle)["decided_by"])

    def test_an_expired_request_cannot_be_decided(self) -> None:
        """A decision made about a workspace that has since changed is not a
        decision about this request."""
        store = self.store()
        handle = self.request(store)
        with store._db() as connection:
            connection.execute(
                "UPDATE control.approvals SET expires_at = %s",
                (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),),
            )
        self.assertFalse(store.decide_approval(handle, approved=True,
                                               decided_by="admin"))
        self.assertTrue(store.read_approval(handle)["expired"])

    def test_revoking_the_grant_kills_an_unspent_receipt(self) -> None:
        """The case that matters: a receipt outlives the decision that
        produced it for as long as nobody spends it."""
        store = self.store()
        handle = self.request(store)
        store.decide_approval(handle, approved=True, decided_by="admin")
        receipt = store.claim_receipt(handle)
        self.assertEqual(1, store.revoke_approvals_for_grant(
            "oauth:grant-1", "operator revoked the grant"))
        self.assertIsNone(store.redeem_receipt(receipt, request_digest=DIGEST))

    def test_revoking_the_grant_itself_reaches_the_receipt(self) -> None:
        """Through the lever an operator actually pulls, not the helper it
        calls. The revocation and the approval must move in one transaction:
        a revocation that reached the grant but not the receipt would leave
        behind the one credential most worth revoking.
        """
        store = self.store()
        with store._db() as connection:
            # The grant references a real client by foreign key.
            connection.execute(
                "INSERT INTO control.oauth_clients"
                " (client_id, name, redirect_uris, scopes, grant_types,"
                "  token_endpoint_auth_method)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                ("mcp-1", "Test agent", ["http://localhost:8484/callback"],
                 ["apply"], ["authorization_code"], "none"),
            )
            connection.execute(
                "INSERT INTO control.oauth_grants"
                " (grant_id, client_id, subject, scopes, created_at)"
                " VALUES (%s, %s, %s, %s, now())",
                ("oauth:grant-1", "mcp-1", "operator", ["apply"]),
            )
        handle = self.request(store)
        store.decide_approval(handle, approved=True, decided_by="admin")
        receipt = store.claim_receipt(handle)

        self.assertTrue(store.revoke_oauth_grant(
            "oauth:grant-1", reason="operator withdrew consent"))

        self.assertIsNone(store.redeem_receipt(receipt, request_digest=DIGEST))

    def test_revocation_leaves_another_grants_approvals_alone(self) -> None:
        store = self.store()
        handle = self.request(store)
        store.decide_approval(handle, approved=True, decided_by="admin")
        mine = store.claim_receipt(handle)
        store.revoke_approvals_for_grant("oauth:someone-else", "unrelated")
        self.assertIsNotNone(store.redeem_receipt(mine, request_digest=DIGEST))

    def test_a_receipt_is_minted_once_and_only_after_a_decision(self) -> None:
        """Deciding and minting are separate on purpose. The person who
        approves does so in a browser and is not the party that will spend it;
        a secret handed to them would have to travel back through the agent to
        be useful, which is the one route it must not take. So the holder of
        the handle mints it, and only once a decision exists."""
        store = self.store()
        handle = self.request(store)

        # Nothing to claim before a person has decided.
        self.assertIsNone(store.claim_receipt(handle))

        store.decide_approval(handle, approved=True, decided_by="admin")
        first = store.claim_receipt(handle)
        self.assertIsNotNone(first)

        # And never a second spendable secret for the same decision.
        self.assertIsNone(store.claim_receipt(handle))

    def test_a_revoked_approval_mints_nothing(self) -> None:
        store = self.store()
        handle = self.request(store)
        store.decide_approval(handle, approved=True, decided_by="admin")
        store.revoke_approvals_for_grant("oauth:grant-1", "withdrawn")
        self.assertIsNone(store.claim_receipt(handle))

    def test_the_handle_and_the_receipt_are_stored_only_as_hashes(self) -> None:
        store = self.store()
        handle = self.request(store)
        store.decide_approval(handle, approved=True, decided_by="admin")
        receipt = store.claim_receipt(handle)
        with store._db() as connection:
            row = connection.execute(
                "SELECT id_hash, receipt_hash FROM control.approvals"
            ).fetchone()
        self.assertNotIn(handle, row.values())
        self.assertNotIn(receipt, row.values())
        self.assertEqual(64, len(row["id_hash"]))
        self.assertEqual(64, len(row["receipt_hash"]))
