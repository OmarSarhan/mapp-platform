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
import canonical  # noqa: E402
from control_plane import ControlStore  # noqa: E402


#: Real digests, in the form execution_envelope.digest actually produces.
#: These were bare 64-character strings, and agreeing with the route's own
#: wrong assumption is how the approval gate shipped unreachable: every
#: request an agent made was refused `approval.digest_invalid`, and nothing
#: here could see it because nothing here went through the route.
DIGEST = f"{canonical.SCHEME}:{'a' * 64}"
OTHER_DIGEST = f"{canonical.SCHEME}:{'b' * 64}"


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
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        record = store.read_approval(handle)
        self.assertEqual("pending", record["status"])
        self.assertIsNone(record["decided_at"])
        self.assertIsNone(store.redeem_receipt(handle, request_digest=DIGEST))

    def test_the_decider_never_holds_the_spendable_secret(self) -> None:
        """Two names for one row. The asker holds the handle and can claim a
        receipt with it; the person deciding sees only the reference, which
        names the row and spends nothing. Nothing the decider is given could
        be carried back through the agent and used."""
        store = self.store()
        created = self.request(store)
        self.assertNotEqual(created["handle"], created["reference"])
        store.decide_approval(created["reference"], approved=True,
                              decided_by="admin")
        # The reference is not a handle: it claims nothing.
        self.assertIsNone(store.claim_receipt(created["reference"]))
        self.assertIsNotNone(store.claim_receipt(created["handle"]))

    def test_the_pending_list_shows_only_what_can_still_be_decided(self) -> None:
        store = self.store()
        live = self.request(store)
        decided = self.request(store)
        store.decide_approval(decided["reference"], approved=True,
                              decided_by="admin")
        pending = store.list_pending_approvals()
        self.assertEqual([live["reference"]],
                         [row["reference"] for row in pending])
        # The packet travels with the row, so a panel shows the request rather
        # than asking somebody to approve a word.
        self.assertEqual("Rename Bus Stops", pending[0]["packet"]["summary"])

    def test_the_packet_is_kept_so_a_decision_can_be_audited(self) -> None:
        """What the person was shown, not a reconstruction of it."""
        store = self.store()
        record = store.read_approval(self.request(store)["handle"])
        self.assertEqual({"summary": "Rename Bus Stops", "changes": 1},
                         record["packet"])

    def test_approval_mints_a_receipt_that_spends_once(self) -> None:
        store = self.store()
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        self.assertTrue(store.decide_approval(reference, approved=True,
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
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        store.decide_approval(reference, approved=True, decided_by="a")
        receipt = store.claim_receipt(handle)
        store.redeem_receipt(receipt, request_digest=DIGEST)
        self.assertEqual("consumed", store.read_approval(handle)["status"])

    def test_a_receipt_does_not_travel_to_another_request(self) -> None:
        """The digest is in the predicate, not checked afterwards. An approval
        carried to a different path, body or revision than the one a person saw
        does not match the row that would otherwise be spent."""
        store = self.store()
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        store.decide_approval(reference, approved=True, decided_by="admin")
        receipt = store.claim_receipt(handle)
        self.assertIsNone(
            store.redeem_receipt(receipt, request_digest=OTHER_DIGEST))
        # And is still spendable for the request it was actually issued for.
        self.assertIsNotNone(store.redeem_receipt(receipt, request_digest=DIGEST))

    def test_a_decline_mints_nothing(self) -> None:
        store = self.store()
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        self.assertTrue(store.decide_approval(reference, approved=False,
                                              decided_by="admin"))
        self.assertEqual("declined", store.read_approval(handle)["status"])
        # Nothing to claim: a decline mints no secret at all.
        self.assertIsNone(store.claim_receipt(handle))

    def test_a_decision_cannot_be_changed(self) -> None:
        """The predicate requires `pending`, which a decided row is not. Two
        operators deciding at once cannot both believe they did it."""
        store = self.store()
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        self.assertTrue(store.decide_approval(reference, approved=True,
                                              decided_by="first"))
        self.assertFalse(store.decide_approval(reference, approved=False,
                                               decided_by="second"))
        self.assertFalse(store.decide_approval(reference, approved=True,
                                               decided_by="third"))
        self.assertEqual("first", store.read_approval(handle)["decided_by"])

    def test_an_expired_request_cannot_be_decided(self) -> None:
        """A decision made about a workspace that has since changed is not a
        decision about this request."""
        store = self.store()
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        with store._db() as connection:
            connection.execute(
                "UPDATE control.approvals SET expires_at = %s",
                (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),),
            )
        self.assertFalse(store.decide_approval(reference, approved=True,
                                               decided_by="admin"))
        self.assertTrue(store.read_approval(handle)["expired"])

    def age_decision(self, store, seconds):
        """Push the decision back in time, leaving everything else alone."""
        with store._db() as connection:
            connection.execute(
                "UPDATE control.approvals SET decided_at = %s",
                (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds),),
            )

    def test_a_stale_decision_mints_nothing(self) -> None:
        """Without this a receipt is a standing authorisation: an agent could
        hold an approved one and spend it at a moment the person is not
        expecting, which is the thing the mechanism exists to prevent."""
        store = self.store()
        created = self.request(store)
        store.decide_approval(created["reference"], approved=True,
                              decided_by="admin")
        self.age_decision(
            store, store.RECEIPT_LIFETIME.total_seconds() + 1
        )
        self.assertIsNone(store.claim_receipt(created["handle"]))

    def test_a_receipt_claimed_in_time_cannot_be_held(self) -> None:
        """The bound is on the spend as well as the claim. Claiming early and
        spending late would otherwise buy back the standing authorisation."""
        store = self.store()
        created = self.request(store)
        store.decide_approval(created["reference"], approved=True,
                              decided_by="admin")
        receipt = store.claim_receipt(created["handle"])
        self.assertIsNotNone(receipt)
        self.age_decision(
            store, store.RECEIPT_LIFETIME.total_seconds() + 1
        )
        self.assertIsNone(store.redeem_receipt(receipt, request_digest=DIGEST))

    def test_the_spend_window_runs_from_the_decision_not_the_request(
        self,
    ) -> None:
        """Measured from `decided_at`, so somebody who takes fourteen minutes
        to decide does not leave the agent one minute to act."""
        store = self.store()
        created = self.request(store)
        with store._db() as connection:
            connection.execute(
                "UPDATE control.approvals SET created_at = %s",
                (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=14),),
            )
        store.decide_approval(created["reference"], approved=True,
                              decided_by="admin")
        receipt = store.claim_receipt(created["handle"])
        self.assertIsNotNone(receipt)
        self.assertIsNotNone(
            store.redeem_receipt(receipt, request_digest=DIGEST)
        )

    def test_confirming_by_handle_decides_the_same_row(self) -> None:
        """The MCP path names the row by the handle its requester holds; the
        dashboard names it by the reference. One row, two names."""
        store = self.store()
        created = self.request(store)
        self.assertTrue(store.confirm_approval(
            created["handle"], approved=True, decided_by="session:oauth:g",
        ))
        record = store.read_approval(created["handle"])
        self.assertEqual("approved", record["status"])
        self.assertEqual("session:oauth:g", record["decided_by"])

    def test_a_session_decision_is_distinguishable_from_an_operators(
        self,
    ) -> None:
        """Different assurances: an operator read the dashboard, this person
        answered a prompt their client rendered. They must not read the same
        afterwards."""
        store = self.store()
        confirmed = self.request(store)
        store.confirm_approval(confirmed["handle"], approved=True,
                               decided_by="session:oauth:g")
        decided = self.request(store, request_digest=OTHER_DIGEST)
        store.decide_approval(decided["reference"], approved=True,
                              decided_by="admin")
        self.assertTrue(
            store.read_approval(confirmed["handle"])["decided_by"]
            .startswith("session:")
        )
        self.assertEqual(
            "admin", store.read_approval(decided["handle"])["decided_by"]
        )

    def test_a_confirmed_decline_cannot_be_reversed_by_the_dashboard(
        self,
    ) -> None:
        store = self.store()
        created = self.request(store)
        store.confirm_approval(created["handle"], approved=False,
                               decided_by="session:oauth:g")
        self.assertFalse(store.decide_approval(
            created["reference"], approved=True, decided_by="admin",
        ))
        self.assertIsNone(store.claim_receipt(created["handle"]))

    def test_revoking_the_grant_kills_an_unspent_receipt(self) -> None:
        """The case that matters: a receipt outlives the decision that
        produced it for as long as nobody spends it."""
        store = self.store()
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        store.decide_approval(reference, approved=True, decided_by="admin")
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
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        store.decide_approval(reference, approved=True, decided_by="admin")
        receipt = store.claim_receipt(handle)

        self.assertTrue(store.revoke_oauth_grant(
            "oauth:grant-1", reason="operator withdrew consent"))

        self.assertIsNone(store.redeem_receipt(receipt, request_digest=DIGEST))

    def test_revocation_leaves_another_grants_approvals_alone(self) -> None:
        store = self.store()
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        store.decide_approval(reference, approved=True, decided_by="admin")
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
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]

        # Nothing to claim before a person has decided.
        self.assertIsNone(store.claim_receipt(handle))

        store.decide_approval(reference, approved=True, decided_by="admin")
        first = store.claim_receipt(handle)
        self.assertIsNotNone(first)

        # And never a second spendable secret for the same decision.
        self.assertIsNone(store.claim_receipt(handle))

    def test_a_revoked_approval_mints_nothing(self) -> None:
        store = self.store()
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        store.decide_approval(reference, approved=True, decided_by="admin")
        store.revoke_approvals_for_grant("oauth:grant-1", "withdrawn")
        self.assertIsNone(store.claim_receipt(handle))

    def test_the_handle_and_the_receipt_are_stored_only_as_hashes(self) -> None:
        store = self.store()
        created = self.request(store)
        handle = created["handle"]
        reference = created["reference"]
        store.decide_approval(reference, approved=True, decided_by="admin")
        receipt = store.claim_receipt(handle)
        with store._db() as connection:
            row = connection.execute(
                "SELECT id_hash, receipt_hash FROM control.approvals"
            ).fetchone()
        self.assertNotIn(handle, row.values())
        self.assertNotIn(receipt, row.values())
        self.assertEqual(64, len(row["id_hash"]))
        self.assertEqual(64, len(row["receipt_hash"]))


class ApprovalWindowTests(ControlStoreTestCase):
    """P8's standing windows: the same receipt, decided without prompting.

    The governing sentence is "a window substitutes the decider, never the
    receipt", so most of what matters here is what does *not* change -- the
    digest binding, the single use, the atomic spend. What is new is the
    seven bounds, and each one is a way this could authorise more than
    somebody meant.
    """

    def store(self) -> ControlStore:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        store = ControlStore(Path(directory))
        store.initialize("correct horse battery staple")
        return store

    def window(self, store, **over):
        fields = {
            "grant_id": "oauth:grant-1",
            "client_id": "mcp-1",
            "instance": "instance-under-test",
            "action_class": "apply",
            "created_by": "admin",
            "session_created_at": dt.datetime.now(dt.UTC),
            "minutes": 30,
            "max_consumptions": 3,
        }
        fields.update(over)
        return store.open_approval_window(**fields)

    def request(self, store, **over):
        fields = {
            "grant_id": "oauth:grant-1",
            "client_id": "mcp-1",
            "instance": "instance-under-test",
            "operation_id": "proposals.apply",
            "tool": "proposals_apply",
            "request_digest": DIGEST,
            "risk": "apply",
            "scopes": ["apply"],
            "packet": {"summary": "Rename a layer"},
        }
        fields.update(over)
        return store.create_approval(**fields)

    # -- what a window may cover --------------------------------------------

    def test_a_window_cannot_cover_semantic_administration(self) -> None:
        """Excluded as a class, not as a scope. Every action class in it needs
        a per-action decision, always."""
        store = self.store()
        with self.assertRaises(ValueError):
            self.window(store, action_class="semantic-apply")

    def test_a_window_cannot_cover_federation_mutation(self) -> None:
        store = self.store()
        for action_class in ("federation-provision", "federation-register",
                             "federation-observe"):
            with self.subTest(action_class=action_class):
                with self.assertRaises(ValueError):
                    self.window(store, action_class=action_class)

    def test_a_window_cannot_cover_a_class_nobody_has_considered(self) -> None:
        """The allowlist is the opposite direction from the approval exemption
        list, so a new action class is un-windowable by default."""
        store = self.store()
        with self.assertRaises(ValueError):
            self.window(store, action_class="something-invented")

    # -- the bounds ---------------------------------------------------------

    def test_a_window_cannot_outlast_an_hour(self) -> None:
        store = self.store()
        with self.assertRaises(ValueError):
            self.window(store, minutes=61)

    def test_a_window_cannot_authorise_more_than_the_cap(self) -> None:
        """Time alone is not a bound: at the specification's rates an hour
        would auto-approve roughly three hundred mutations."""
        store = self.store()
        with self.assertRaises(ValueError):
            self.window(store, max_consumptions=21)

    def test_a_stale_session_cannot_open_one(self) -> None:
        """Recency is checked here and nowhere else. A window borrows the
        creator's authority, so the platform wants a recent authentication
        rather than a long-lived session."""
        store = self.store()
        stale = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=16)
        with self.assertRaises(ValueError) as raised:
            self.window(store, session_created_at=stale)
        self.assertIn("Sign in again", str(raised.exception))

    def test_recency_is_not_rechecked_at_consumption(self) -> None:
        """Deliberate, and the reason is arithmetic: re-checking would make
        the sixty-minute bound dead letter, since nothing would be approvable
        after the first fifteen."""
        store = self.store()
        self.window(store, minutes=60)
        with store._db() as connection:
            connection.execute(
                "UPDATE control.approval_windows SET creator_auth_time = %s,"
                " created_at = %s",
                (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=50),
                 dt.datetime.now(dt.UTC) - dt.timedelta(minutes=50)),
            )
        self.assertTrue(self.request(store)["decided"])

    # -- consumption --------------------------------------------------------

    def test_an_intent_inside_a_window_is_decided_on_arrival(self) -> None:
        store = self.store()
        opened = self.window(store)
        created = self.request(store)
        self.assertTrue(created["decided"])
        self.assertEqual(opened["id"], created["window"])
        record = store.read_approval(created["handle"])
        self.assertEqual("approved", record["status"])
        self.assertEqual("admin", record["decided_by"])

    def test_the_receipt_is_the_ordinary_one(self) -> None:
        """Everything downstream is unchanged: single use, bound to that one
        digest, spent atomically with the effect."""
        store = self.store()
        self.window(store)
        created = self.request(store)
        receipt = store.claim_receipt(created["handle"])
        self.assertIsNotNone(receipt)
        self.assertIsNotNone(
            store.redeem_receipt(receipt, request_digest=DIGEST)
        )
        self.assertIsNone(
            store.redeem_receipt(receipt, request_digest=DIGEST),
            "an auto-decided receipt must still spend exactly once",
        )

    def test_a_window_authorises_a_class_never_a_request(self) -> None:
        """The per-action invalidation set applies unchanged inside a live
        window: a different digest is a different intent."""
        store = self.store()
        self.window(store)
        created = self.request(store)
        receipt = store.claim_receipt(created["handle"])
        self.assertIsNone(
            store.redeem_receipt(receipt, request_digest=OTHER_DIGEST),
            "a window must not make a receipt portable between requests",
        )

    def test_consumptions_are_counted_and_the_window_closes(self) -> None:
        store = self.store()
        self.window(store, max_consumptions=2)
        for index in range(2):
            with self.subTest(consumption=index):
                self.assertTrue(self.request(store)["decided"])
        self.assertFalse(
            self.request(store)["decided"],
            "an exhausted window must stop deciding even with time left",
        )

    def test_an_exhausted_window_is_no_longer_live(self) -> None:
        store = self.store()
        self.window(store, max_consumptions=1)
        self.request(store)
        entry = store.list_approval_windows()[0]
        self.assertFalse(entry["live"])
        self.assertEqual(entry["consumed"], entry["max_consumptions"])

    def test_simultaneous_intents_cannot_overspend_the_last_consumption(
        self,
    ) -> None:
        """The predicate and the decrement are one statement, so two intents
        arriving together cannot both see the last consumption free."""
        import threading

        store = self.store()
        self.window(store, max_consumptions=1)
        decided, barrier = [], threading.Barrier(4)

        def ask():
            barrier.wait()
            decided.append(self.request(store)["decided"])

        threads = [threading.Thread(target=ask) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, decided.count(True), decided)

    # -- what closes one ----------------------------------------------------

    def test_an_expired_window_decides_nothing(self) -> None:
        store = self.store()
        self.window(store)
        # Both timestamps, because the row carries a CHECK that an expiry is
        # after its creation -- an expired window is one created earlier, not
        # one whose expiry was moved into the past.
        with store._db() as connection:
            connection.execute(
                "UPDATE control.approval_windows"
                "   SET created_at = %s, expires_at = %s",
                (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=31),
                 dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)),
            )
        self.assertFalse(self.request(store)["decided"])

    def test_a_revoked_window_decides_nothing(self) -> None:
        store = self.store()
        opened = self.window(store)
        self.assertTrue(store.revoke_approval_window(opened["id"], "changed my mind"))
        self.assertFalse(self.request(store)["decided"])

    def test_revoking_twice_reports_who_did_it(self) -> None:
        store = self.store()
        opened = self.window(store)
        self.assertTrue(store.revoke_approval_window(opened["id"], "first"))
        self.assertFalse(store.revoke_approval_window(opened["id"], "second"))

    def test_revoking_the_grant_closes_its_windows(self) -> None:
        """A window is authority to decide on a grant's behalf, so a grant
        that no longer exists cannot have one standing."""
        store = self.store()
        store.register_oauth_client(
            name="agent", redirect_uris=["http://127.0.0.1:9/cb"],
            scopes=["mcp:connect", "apply"],
        )
        self.window(store, grant_id="oauth:grant-9")
        self.assertEqual(
            1,
            store.revoke_approval_windows_for_grant("oauth:grant-9", "revoked"),
        )
        self.assertFalse(
            self.request(store, grant_id="oauth:grant-9")["decided"]
        )

    # -- what it is bound to ------------------------------------------------

    def test_a_window_does_not_reach_another_grant(self) -> None:
        store = self.store()
        self.window(store)
        self.assertFalse(
            self.request(store, grant_id="oauth:someone-else")["decided"]
        )

    def test_a_window_does_not_reach_another_client(self) -> None:
        store = self.store()
        self.window(store)
        self.assertFalse(self.request(store, client_id="mcp-2")["decided"])

    def test_a_window_does_not_reach_another_instance(self) -> None:
        store = self.store()
        self.window(store)
        self.assertFalse(
            self.request(store, instance="somewhere-else")["decided"]
        )

    def test_a_window_does_not_reach_another_action_class(self) -> None:
        """One class, never a set. A window covering several would be a second
        permission system growing beside the scopes."""
        store = self.store()
        self.window(store, action_class="apply")
        self.assertFalse(
            self.request(
                store, risk="database-definition",
                operation_id="derived-layers.drop", tool="derived_layers_drop",
                request_digest=OTHER_DIGEST,
            )["decided"]
        )

    def test_an_unwindowable_intent_is_never_auto_decided(self) -> None:
        """Even with a window open for something else, and even if one somehow
        existed for this class."""
        store = self.store()
        self.window(store)
        self.assertFalse(
            self.request(
                store, risk="semantic-apply",
                operation_id="semantic.proposals.apply",
                tool="semantic_proposals_apply",
                request_digest=OTHER_DIGEST,
            )["decided"]
        )

    def test_an_interactive_approval_still_has_no_window(self) -> None:
        store = self.store()
        created = self.request(store)
        self.assertFalse(created["decided"])
        self.assertIsNone(created["window"])
