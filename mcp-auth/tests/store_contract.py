"""Rules both stores must obey, asserted against each of them.

sql_store.py's header said "the same suite runs against both". It did not.
Each store had its own hand-written suite with its own fixture, so the two
could disagree indefinitely while both stayed green -- and they did:
``exchanged_binding`` returned a different set of keys from each, and only one
of them enforced the grant. A double that is more permissive than the store it
stands in for is worse than no double, because every test written against it
reports a safety the real system does not have.

So these tests live once and both suites inherit them. Anything asserted here
is a property of the *contract*, not of either implementation.
"""

from __future__ import annotations

import datetime as dt

from models import Client, Grant, Token

#: Every key ``exchanged_binding`` returns, from either store. Frozen as a
#: literal rather than derived from one of them, because deriving it from
#: either would let that one define the contract it is supposed to satisfy.
BINDING_KEYS = frozenset(
    {
        "operation_id",
        "request_digest",
        "single_use",
        "audience",
        "actor_client_id",
        "broker_client_id",
        "consumed_at",
        "expires_at",
    }
)

AUDIENCE = "http://config.localhost/api"
DIGEST = "mapp-jcs-v1:" + "0" * 64


class StoreContractTests:
    """Mixin. The concrete class supplies ``self.store`` in setUp."""

    def _contract_client(self, client_id: str) -> None:
        self.store.add_client(
            Client(
                client_id=client_id,
                name=client_id,
                redirect_uris=("http://127.0.0.1:9/cb",),
                scopes=("apply",),
                token_endpoint_auth_method="none",
            )
        )

    def _mint_token_b(
        self, raw: str, *, subject: str = "oauth:contract", single_use: bool = True
    ) -> None:
        """Register a grant, its client and a broker, then mint a token B.

        Deliberately built from store calls only. A fixture that reached past
        the store would prove nothing about the store.
        """
        if self.store.query_client("contract-actor") is None:
            self._contract_client("contract-actor")
            self._contract_client("contract-broker")
        if subject != "oauth:missing" and self.store.query_grant(subject) is None:
            self.store.save_grant(
                Grant(
                    grant_id=subject,
                    client_id="contract-actor",
                    subject="operator",
                    scopes=("apply",),
                )
            )
        issued = dt.datetime.now(dt.timezone.utc)
        self.store.save_exchanged_token(
            raw,
            client_id="contract-broker",
            actor_client_id="contract-actor",
            subject=subject,
            scope="apply",
            audience=AUDIENCE,
            issued_at=issued,
            expires_at=issued + dt.timedelta(seconds=60),
            operation_id="proposals.apply",
            request_digest=DIGEST,
            single_use=single_use,
        )

    # -- shape -----------------------------------------------------------

    def test_the_binding_has_the_contract_shape(self) -> None:
        self._mint_token_b("mapp_b_shape")
        binding = self.store.exchanged_binding("mapp_b_shape")
        self.assertEqual(BINDING_KEYS, set(binding))
        self.assertIsNone(binding["consumed_at"])
        self.assertEqual(AUDIENCE, binding["audience"])
        self.assertTrue(binding["single_use"])

    def test_spending_records_when_it_was_spent(self) -> None:
        self._mint_token_b("mapp_b_spent")
        self.assertIsNotNone(
            self.store.consume_exchanged_token(
                "mapp_b_spent", "proposals.apply", DIGEST
            )
        )
        self.assertIsNotNone(
            self.store.exchanged_binding("mapp_b_spent")["consumed_at"]
        )

    # -- the grant governs every path that reads a token B ---------------

    def test_a_revoked_grant_makes_a_token_b_unspendable(self) -> None:
        """The claim M5 exists to make true.

        Introspection resolved the grant; the statement that actually spends
        the token did not, so a revoked grant reported inactive and its token
        still spent successfully. Reproduced against a real database before
        this test existed.
        """
        self._mint_token_b("mapp_b_revoked")
        self.assertTrue(self.store.revoke_grant("oauth:contract", "test"))
        self.assertIsNone(
            self.store.consume_exchanged_token(
                "mapp_b_revoked", "proposals.apply", DIGEST
            )
        )

    def test_a_revoked_grant_refuses_the_non_spending_read(self) -> None:
        """A read operation's token is not single-use, so this is its only gate.

        With no consume path for a read, exchanged_binding is the whole
        verification surface -- and it returned the binding unchanged after
        revocation.
        """
        self._mint_token_b("mapp_b_read", single_use=False)
        self.assertIsNotNone(self.store.exchanged_binding("mapp_b_read"))
        self.assertTrue(self.store.revoke_grant("oauth:contract", "test"))
        self.assertIsNone(self.store.exchanged_binding("mapp_b_read"))

    def test_a_token_b_whose_grant_never_existed_is_refused(self) -> None:
        """No foreign key stops such a row being written, so the check is here.

        control_schema.py declines to add one deliberately; that makes this an
        application-side guarantee, and an application-side guarantee needs a
        test or it is a comment.
        """
        self._mint_token_b("mapp_b_orphan", subject="oauth:missing")
        self.assertIsNone(self.store.exchanged_binding("mapp_b_orphan"))
        self.assertIsNone(
            self.store.consume_exchanged_token(
                "mapp_b_orphan", "proposals.apply", DIGEST
            )
        )

    # -- other divergences the two stores had ----------------------------

    def test_a_token_saved_revoked_reads_back_revoked(self) -> None:
        """SqlStore's INSERT omitted revoked_at; the stub kept the flag."""
        if self.store.query_client("revoked-token-client") is None:
            self._contract_client("revoked-token-client")
        self.store.save_token(
            "mapp_a_born_revoked",
            Token(
                token_hash="",
                client_id="revoked-token-client",
                scope="apply",
                subject="operator",
                issued_at=int(dt.datetime.now(dt.timezone.utc).timestamp()),
                expires_in=900,
                audience=AUDIENCE,
                revoked=True,
            ),
        )
        self.assertTrue(self.store.query_token("mapp_a_born_revoked").is_revoked())

    def test_a_duplicate_grant_id_is_refused(self) -> None:
        """The stub overwrote, which un-revoked a revoked grant."""
        self._mint_token_b("mapp_b_dup")
        grant = Grant(
            grant_id="oauth:contract",
            client_id="contract-actor",
            subject="operator",
            scopes=("apply",),
        )
        with self.assertRaises(Exception):
            self.store.save_grant(grant)

    def test_reads_return_snapshots_not_live_internals(self) -> None:
        """SqlStore cannot do otherwise; the double could, and did.

        A caller holding a Grant watched it change under them, and a caller
        mutating a returned binding mutated the store. Neither is expressible
        against a database, so a test relying on either would pass on the
        double and fail in production.
        """
        self._mint_token_b("mapp_b_snapshot")
        grant = self.store.query_grant("oauth:contract")
        self.assertTrue(self.store.revoke_grant("oauth:contract", "test"))
        self.assertFalse(
            grant.is_revoked(), "the Grant handed to the caller changed underneath it"
        )
        self.assertTrue(self.store.query_grant("oauth:contract").is_revoked())

    def test_the_exchange_budget_counts_this_grant_only(self) -> None:
        """The abuse budget's input, so it has to mean the same in both stores.

        One consent must not become an unbounded number of effects. The count
        is per grant, over a window, and a consumed token still counts -- it
        was still minted.
        """
        self._mint_token_b("mapp_b_budget_1")
        self._mint_token_b("mapp_b_budget_2")
        self.assertEqual(2, self.store.exchanged_token_count("oauth:contract", 60))

        # A different grant is counted separately.
        if self.store.query_grant("oauth:other") is None:
            self.store.save_grant(
                Grant(
                    grant_id="oauth:other",
                    client_id="contract-actor",
                    subject="operator",
                    scopes=("apply",),
                )
            )
        self._mint_token_b("mapp_b_budget_3", subject="oauth:other")
        self.assertEqual(2, self.store.exchanged_token_count("oauth:contract", 60))
        self.assertEqual(1, self.store.exchanged_token_count("oauth:other", 60))

        # Spending one does not return budget: a burst is a burst.
        self.assertIsNotNone(
            self.store.consume_exchanged_token(
                "mapp_b_budget_1", "proposals.apply", DIGEST
            )
        )
        self.assertEqual(2, self.store.exchanged_token_count("oauth:contract", 60))

    def test_a_token_a_is_not_counted_against_the_exchange_budget(self) -> None:
        """Only the exchange sets operation_id, and only its output is capped."""
        if self.store.query_client("budget-client") is None:
            self._contract_client("budget-client")
        self.store.save_token(
            "mapp_a_not_counted",
            Token(
                token_hash="",
                client_id="budget-client",
                scope="apply",
                subject="oauth:contract",
                issued_at=int(dt.datetime.now(dt.timezone.utc).timestamp()),
                expires_in=900,
                audience=AUDIENCE,
            ),
        )
        self.assertEqual(0, self.store.exchanged_token_count("oauth:contract", 60))

    # -- rotating refresh families ---------------------------------------

    def _open_family(self, raw="mapp_r_1", family="fam", **bounds) -> None:
        """A family on the contract's own grant, using only store calls."""
        if self.store.query_client("contract-actor") is None:
            self._contract_client("contract-actor")
            self._contract_client("contract-broker")
        if self.store.query_grant("oauth:contract") is None:
            self.store.save_grant(
                Grant(
                    grant_id="oauth:contract",
                    client_id="contract-actor",
                    subject="operator",
                    scopes=("apply",),
                )
            )
        self.store.start_refresh_family(
            raw,
            family_id=family,
            grant_id="oauth:contract",
            client_id="contract-actor",
            scope="apply",
            **bounds,
        )

    def test_a_refresh_token_rotates_once(self) -> None:
        self._open_family()
        result = self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")
        self.assertEqual("rotated", result["outcome"])
        self.assertEqual("oauth:contract", result["grant_id"])
        self.assertEqual("contract-actor", result["client_id"])
        self.assertEqual("apply", result["scope"])

    def test_rotation_records_the_chain(self) -> None:
        """Not needed to authorise anything; needed to read an audit after a replay."""
        self._open_family()
        self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")
        spent = self.store.refresh_token_state("mapp_r_1")
        successor = self.store.refresh_token_state("mapp_r_2")
        self.assertIsNotNone(spent["consumed_at"])
        self.assertIsNotNone(spent["replaced_by"])
        self.assertIsNone(successor["consumed_at"])
        self.assertEqual(spent["family_id"], successor["family_id"])

    def test_replaying_a_spent_token_revokes_the_family_and_the_grant(self) -> None:
        """Section 4's rule, and the whole reason spent tokens are kept.

        A deleted row would make a replay indistinguishable from a token that
        never existed -- and those two have opposite consequences.
        """
        self._open_family()
        self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")
        result = self.store.rotate_refresh_token("mapp_r_1", "mapp_r_evil")
        self.assertEqual("replayed", result["outcome"])
        self.assertEqual("oauth:contract", result["grant_revoked"])
        self.assertIsNotNone(self.store.query_refresh_family("fam")["revoked_at"])
        self.assertTrue(self.store.query_grant("oauth:contract").is_revoked())
        # And no replacement was issued for the replay.
        self.assertIsNone(self.store.refresh_token_state("mapp_r_evil"))

    def test_a_replay_also_kills_the_legitimate_live_token(self) -> None:
        """That is the containment, and it is also the cost O7 records.

        A client that retried after a timeout is indistinguishable from an
        attacker replaying, and loses the operator's consent either way.
        """
        self._open_family()
        self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")
        self.store.rotate_refresh_token("mapp_r_1", "mapp_r_evil")
        self.assertEqual(
            "grant-revoked",
            self.store.rotate_refresh_token("mapp_r_2", "mapp_r_3")["outcome"],
        )

    def test_an_unknown_refresh_token_is_not_a_replay(self) -> None:
        """It revokes nothing: a guess must not be able to destroy a consent."""
        self._open_family()
        self.assertEqual(
            "unknown",
            self.store.rotate_refresh_token("mapp_r_nosuch", "mapp_r_2")["outcome"],
        )
        self.assertIsNone(self.store.query_refresh_family("fam")["revoked_at"])
        self.assertFalse(self.store.query_grant("oauth:contract").is_revoked())

    def test_a_refresh_requires_a_live_grant(self) -> None:
        self._open_family()
        self.assertTrue(self.store.revoke_grant("oauth:contract", "test"))
        self.assertEqual(
            "grant-revoked",
            self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")["outcome"],
        )

    def test_a_revoked_family_stops_rotating(self) -> None:
        self._open_family()
        self.assertTrue(self.store.revoke_refresh_family("fam", "operator"))
        self.assertFalse(self.store.revoke_refresh_family("fam", "again"))
        self.assertEqual(
            "family-revoked",
            self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")["outcome"],
        )
        # Revoking a family does not withdraw the consent.
        self.assertFalse(self.store.query_grant("oauth:contract").is_revoked())

    def test_the_idle_deadline_stops_rotation(self) -> None:
        self._open_family(idle_seconds=-1)
        self.assertEqual(
            "idle-expired",
            self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")["outcome"],
        )

    def expire_family_absolutely(self, family_id: str) -> None:
        """Move a family's absolute deadline into the past.

        Store-specific because the schema forbids creating a family that is
        already dead -- `absolute_expires_at > created_at` is a CHECK, and
        rightly so. The state is reachable in production only by time passing,
        so the setup differs per store while the assertion below does not.
        """
        raise NotImplementedError

    def test_the_absolute_deadline_stops_rotation(self) -> None:
        """Rotation moves the idle deadline and never the absolute one.

        Otherwise an indefinitely-refreshed session would outlive the consent
        it came from.
        """
        self._open_family()
        self.expire_family_absolutely("fam")
        self.assertEqual(
            "family-expired",
            self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")["outcome"],
        )

    def test_rotation_does_not_extend_the_absolute_deadline(self) -> None:
        self._open_family()
        before = self.store.query_refresh_family("fam")["absolute_expires_at"]
        self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")
        self.assertEqual(before, self.store.query_refresh_family("fam")["absolute_expires_at"])

    def test_reopening_a_family_id_is_refused(self) -> None:
        """It is a primary key in SQL, so the double must not accept it either."""
        self._open_family()
        with self.assertRaises(Exception):
            self._open_family(raw="mapp_r_other")

    def test_revoking_a_grants_families_leaves_another_grants_alone(self) -> None:
        self._open_family()
        self.store.save_grant(
            Grant(
                grant_id="oauth:other",
                client_id="contract-actor",
                subject="operator",
                scopes=("apply",),
            )
        )
        self.store.start_refresh_family(
            "mapp_r_other",
            family_id="fam-other",
            grant_id="oauth:other",
            client_id="contract-actor",
            scope="apply",
        )
        self.assertEqual(
            1, self.store.revoke_refresh_families_for_grant("oauth:contract", "test")
        )
        self.assertIsNotNone(self.store.query_refresh_family("fam")["revoked_at"])
        self.assertIsNone(self.store.query_refresh_family("fam-other")["revoked_at"])

    def test_a_spent_token_after_a_revocation_is_not_reported_as_a_replay(self) -> None:
        """Revocation is checked before consumption, and the order matters.

        After a restore revokes a family, the client's already-spent token is
        re-presented on its next attempt. Classifying that as "replayed" raises
        a stolen-credential signal for something the operator's own restore
        caused -- and the containment it triggers has already happened.
        """
        self._open_family()
        self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")
        self.assertTrue(self.store.revoke_refresh_family("fam", "recovery-epoch"))
        result = self.store.rotate_refresh_token("mapp_r_1", "mapp_r_3")
        self.assertEqual("family-revoked", result["outcome"])

    def test_a_replay_on_a_live_family_is_still_a_replay(self) -> None:
        """The reordering must not blunt the signal it exists to raise."""
        self._open_family()
        self.store.rotate_refresh_token("mapp_r_1", "mapp_r_2")
        self.assertEqual(
            "replayed",
            self.store.rotate_refresh_token("mapp_r_1", "mapp_r_3")["outcome"],
        )
