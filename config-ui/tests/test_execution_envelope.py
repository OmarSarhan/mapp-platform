"""The canonical envelope: what a token B is actually bound to.

The broker never sees the downstream request, so it cannot check the digest it
stores -- it only validates the shape. Every rule that makes a token B
request-bound rather than merely scope-bound is enforced here, which is why
these tests are about refusals far more than about the happy path.

Each rejection below is a way two components could otherwise disagree about
which request they authorised, and every one of them is silent: a digest that
differs is just a refusal, and a digest that wrongly *agrees* is an
authorisation nobody asked for.
"""

from __future__ import annotations

import unittest

import canonical
import execution_envelope as envelope

INSTANCE = "instance-1"
TEMPLATE = "/api/proposals/{proposalId}/apply"


def digest(**overrides):
    fields = {
        "instance": INSTANCE,
        "method": "POST",
        "operation_id": "proposals.apply",
        "path_template": TEMPLATE,
        "path": "/api/proposals/p1/apply",
        "query": "",
        "body": {"approved": True},
        # P10's last three members. Null everywhere until the curated manifest
        # and the approval flow exist; build() requires them, so they are here
        # rather than defaulted away.
        "resolved_defaults": None,
        "confirmation_fields": None,
        "revision_binding": None,
    }
    fields.update(overrides)
    return envelope.digest(**fields)


class PathTemplateTests(unittest.TestCase):
    def test_a_matching_path_yields_its_typed_parameters(self) -> None:
        self.assertEqual(
            {"proposalId": "p1"},
            envelope.path_parameters(TEMPLATE, "/api/proposals/p1/apply"),
        )

    def test_a_multi_parameter_template_matches_each_segment(self) -> None:
        self.assertEqual(
            {"a": "one", "b": "two"},
            envelope.path_parameters("/x/{a}/y/{b}", "/x/one/y/two"),
        )

    def test_a_path_of_the_wrong_shape_is_refused(self) -> None:
        for path in (
            "/api/proposals/p1",
            "/api/proposals/p1/apply/extra",
            "/api/proposals/p1/decline",
            "/other/proposals/p1/apply",
        ):
            with self.subTest(path=path):
                with self.assertRaises(envelope.EnvelopeError):
                    envelope.path_parameters(TEMPLATE, path)

    def test_a_parameter_may_not_span_segments(self) -> None:
        """{proposalId} is one segment, so a slash cannot hide inside it."""
        with self.assertRaises(envelope.EnvelopeError):
            envelope.path_parameters(TEMPLATE, "/api/proposals/a/b/apply")

    def test_an_empty_parameter_is_refused(self) -> None:
        with self.assertRaises(envelope.EnvelopeError):
            envelope.path_parameters(TEMPLATE, "/api/proposals//apply")

    def test_a_relative_path_is_refused(self) -> None:
        with self.assertRaises(envelope.EnvelopeError):
            envelope.path_parameters(TEMPLATE, "api/proposals/p1/apply")


class PathEncodingTests(unittest.TestCase):
    """Refused, never normalized. Normalizing twice is how boundaries diverge."""

    def test_a_dot_segment_is_refused(self) -> None:
        for path in ("/api/proposals/./apply", "/api/proposals/../apply"):
            with self.subTest(path=path):
                with self.assertRaises(envelope.EnvelopeError):
                    envelope.path_parameters(TEMPLATE, path)

    def test_an_encoded_separator_is_refused(self) -> None:
        """%2F would let one path name two different resources."""
        with self.assertRaises(envelope.EnvelopeError):
            envelope.path_parameters(TEMPLATE, "/api/proposals/a%2Fb/apply")

    def test_a_lowercase_escape_is_refused(self) -> None:
        """One path must have one spelling, or it has two digests.

        The byte matters: %2f decodes to a separator and %41 to an unreserved
        character, so either would be refused by a *different* rule and this
        test would pass with the hex-case check deleted -- mutation confirmed
        it did. %3a is a colon: legal in a segment, not unreserved, so only the
        case rule can refuse it.
        """
        # The canonical spelling is accepted, so the pair isolates the rule.
        self.assertEqual(
            {"proposalId": "a:b"},
            envelope.path_parameters(TEMPLATE, "/api/proposals/a%3Ab/apply"),
        )
        for path in (
            "/api/proposals/a%3ab/apply",
            "/api/proposals/a%2cb/apply",
            "/api/proposals/a%7bb/apply",
        ):
            with self.subTest(path=path):
                with self.assertRaises(envelope.EnvelopeError):
                    envelope.path_parameters(TEMPLATE, path)

    def test_an_unreserved_character_may_not_be_encoded(self) -> None:
        # %41 is "A", which should have been written literally. Admitting both
        # spellings means two digests for the same request.
        with self.assertRaises(envelope.EnvelopeError):
            envelope.path_parameters(TEMPLATE, "/api/proposals/%41/apply")

    def test_a_backslash_is_refused(self) -> None:
        with self.assertRaises(envelope.EnvelopeError):
            envelope.path_parameters(TEMPLATE, "/api/proposals/a\\b/apply")

    def test_a_malformed_escape_is_refused(self) -> None:
        for path in ("/api/proposals/a%/apply", "/api/proposals/a%2/apply",
                     "/api/proposals/a%zz/apply"):
            with self.subTest(path=path):
                with self.assertRaises(envelope.EnvelopeError):
                    envelope.path_parameters(TEMPLATE, path)

    def test_a_legitimately_encoded_reserved_character_is_kept(self) -> None:
        # %20 is a space: reserved-ish, genuinely needs encoding, and decodes
        # to a value the digest must cover as a space.
        self.assertEqual(
            {"proposalId": "a b"},
            envelope.path_parameters(TEMPLATE, "/api/proposals/a%20b/apply"),
        )

    def test_percent_encoded_utf8_decodes(self) -> None:
        self.assertEqual(
            {"proposalId": "café"},
            envelope.path_parameters(TEMPLATE, "/api/proposals/caf%C3%A9/apply"),
        )

    def test_invalid_utf8_in_a_segment_is_refused(self) -> None:
        with self.assertRaises(envelope.EnvelopeError):
            envelope.path_parameters(TEMPLATE, "/api/proposals/%FF/apply")


class QueryTests(unittest.TestCase):
    def test_pairs_keep_the_order_they_arrived_in(self) -> None:
        """Sorted order would digest two materially different requests alike."""
        self.assertEqual(
            [["b", "1"], ["a", "2"]], envelope.query_pairs("b=1&a=2")
        )
        self.assertNotEqual(
            envelope.query_pairs("b=1&a=2"), envelope.query_pairs("a=2&b=1")
        )

    def test_an_empty_query_is_an_empty_list(self) -> None:
        self.assertEqual([], envelope.query_pairs(""))

    def test_a_blank_value_is_kept_and_differs_from_absence(self) -> None:
        self.assertEqual([["field", ""]], envelope.query_pairs("field="))
        self.assertNotEqual(
            canonical.digest(envelope.query_pairs("field=")),
            canonical.digest(envelope.query_pairs("")),
        )

    def test_a_duplicate_scalar_is_refused(self) -> None:
        with self.assertRaises(envelope.EnvelopeError):
            envelope.query_pairs("field=a&field=b")

    def test_a_duplicate_is_allowed_only_where_the_schema_models_one(self) -> None:
        self.assertEqual(
            [["f", "a"], ["f", "b"]],
            envelope.query_pairs("f=a&f=b", repeatable=frozenset({"f"})),
        )

    def test_a_malformed_query_is_refused(self) -> None:
        with self.assertRaises(envelope.EnvelopeError):
            envelope.query_pairs("field")

    def test_percent_decoding_happens_once(self) -> None:
        self.assertEqual([["f", "a b"]], envelope.query_pairs("f=a%20b"))
        self.assertEqual([["f", "a%20b"]], envelope.query_pairs("f=a%2520b"))


class BodyTests(unittest.TestCase):
    """The three states the digest has to keep apart."""

    def test_an_absent_body_differs_from_an_empty_object(self) -> None:
        self.assertNotEqual(digest(body=None), digest(body={}))

    def test_an_omitted_member_differs_from_an_explicit_null(self) -> None:
        self.assertNotEqual(
            digest(body={"approved": True}),
            digest(body={"approved": True, "note": None}),
        )

    def test_a_string_digit_differs_from_the_number(self) -> None:
        self.assertNotEqual(digest(body={"n": 1}), digest(body={"n": "1"}))

    def test_member_order_does_not_change_the_digest(self) -> None:
        self.assertEqual(
            digest(body={"a": 1, "b": 2}), digest(body={"b": 2, "a": 1})
        )

    def test_a_body_with_a_duplicate_member_never_reaches_the_envelope(self) -> None:
        """Caught on the raw bytes, before a parser can collapse it.

        canonical.loads is what app.py parses a token-B body with, precisely so
        a second "approved" cannot hide behind the first and leave the digest
        covering only the survivor.
        """
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'{"approved":false,"approved":true}')

    def test_a_body_outside_the_numeric_domain_never_reaches_the_envelope(self) -> None:
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'{"n":9007199254740993}')


class EnvelopeMemberTests(unittest.TestCase):
    """Every member must change the digest, or it is not bound by it."""

    def test_each_member_is_load_bearing(self) -> None:
        baseline = digest()
        variants = {
            "instance": digest(instance="instance-2"),
            "operationId": digest(operation_id="proposals.decline"),
            "path": digest(path="/api/proposals/p2/apply"),
            "query": digest(query="x=1"),
            "body": digest(body={"approved": True, "extra": 1}),
        }
        for member, value in variants.items():
            with self.subTest(member=member):
                self.assertNotEqual(baseline, value)

    def test_the_scheme_version_is_a_member(self) -> None:
        built = envelope.build(
            instance=INSTANCE, method="POST", operation_id="proposals.apply",
            path_template=TEMPLATE, path="/api/proposals/p1/apply",
            query="", body=None,
            resolved_defaults=None, confirmation_fields=None,
            revision_binding=None,
        )
        self.assertEqual(canonical.SCHEME, built["version"])

    def test_the_digest_names_its_scheme(self) -> None:
        self.assertTrue(digest().startswith(canonical.SCHEME + ":"))

    def test_a_lower_case_method_is_refused(self) -> None:
        """Uppercase is specified, so accepting both would give two digests."""
        with self.assertRaises(envelope.EnvelopeError):
            digest(method="post")

    def test_a_missing_instance_is_refused(self) -> None:
        with self.assertRaises(envelope.EnvelopeError):
            digest(instance="")

    def test_the_credential_is_not_a_member(self) -> None:
        """Non-circular control-field boundary.

        A token cannot be an input to the digest that authorises it, so the
        envelope has no member that could carry one. This asserts the shape
        rather than a behaviour, which is the only way to catch someone adding
        one later.
        """
        built = envelope.build(
            instance=INSTANCE, method="POST", operation_id="proposals.apply",
            path_template=TEMPLATE, path="/api/proposals/p1/apply",
            query="", body=None,
            resolved_defaults=None, confirmation_fields=None,
            revision_binding=None,
        )
        self.assertEqual(
            {
                "version", "instance", "method", "operationId", "pathTemplate",
                "pathParameters", "path", "query", "body",
                # P10: "the envelope is the twelve-member definition normative
                # in section 7, not a shorter restatement". This set was the
                # nine the module built, so the test that exists to pin the
                # membership pinned the shortfall instead of catching it.
                "resolvedDefaults", "confirmationFields", "revisionBinding",
            },
            set(built),
        )


class GoldenVectorTests(unittest.TestCase):
    """One digest written down, because every other test here is differential.

    The rest compute two digests and compare them, which proves the envelope
    reacts to a change but cannot prove it produces the *right* value. Two
    implementations wrong in the same way agree with each other perfectly, and
    mapp-mcp is about to become the third -- so a vector nobody recomputes is
    the only thing that can catch a shared mistake.

    If a change to the envelope or the canonicalizer fails this test, that is
    the test working: the digest is a wire contract, and moving it means
    already-minted credentials no longer match. Move the scheme version with
    it, do not edit the constant to match the new output.
    """

    #: GET with a query, because the query decoder is the subtle half, and no
    #: body, because a null body is distinguishable from {} in the digest.
    VECTOR = dict(
        instance="instance-1",
        method="GET",
        operation_id="layers.values",
        path_template="/api/layers/{layerKey}/values",
        path="/api/layers/roads/values",
        query="field=name&limit=10",
        body=None,
        resolved_defaults=None,
        confirmation_fields=None,
        revision_binding=None,
    )
    EXPECTED = (
        "mapp-jcs-v1:"
        "8d6e7d1699c903b06892e5c499350a241c7505a2bd1cf629162a79f439fd6c4e"
    )

    def test_the_pinned_vector_still_digests_to_its_recorded_value(self) -> None:
        self.assertEqual(self.EXPECTED, envelope.digest(**self.VECTOR))

    def test_the_vector_envelope_has_all_twelve_members(self) -> None:
        self.assertEqual(12, len(envelope.build(**self.VECTOR)))


class QueryPlusDecodingTests(unittest.TestCase):
    """`+` means a space in a query and a literal plus in a path.

    Nothing recorded this and nothing tested it, and it is the likeliest way a
    second implementation of this envelope disagrees with the first. The query
    decoder is ``urllib.parse.parse_qsl``, which is unquote_*plus*; the path
    decoder is not. A reimplementation written from the phrase "percent-decoded
    exactly once" produces a literal plus for the query, disagrees on every
    value containing one, and the only symptom is a blanket 403 with no
    diagnostic.

    Pinned rather than fixed: changing the rule would move every digest for
    every request carrying a `+`, and the two sides agree today. This says what
    the rule *is* so the next implementation can match it.
    """

    def test_a_plus_in_a_query_value_decodes_to_a_space(self) -> None:
        self.assertEqual([["f", "a b"]], envelope.query_pairs("f=a+b"))

    def test_an_encoded_plus_in_a_query_value_decodes_to_a_plus(self) -> None:
        self.assertEqual([["f", "a+b"]], envelope.query_pairs("f=a%2Bb"))

    def test_a_plus_in_a_path_segment_stays_a_plus(self) -> None:
        """The asymmetry itself: the same byte, two rules, one envelope."""
        self.assertEqual(
            {"layerKey": "a+b"},
            envelope.path_parameters(
                "/api/layers/{layerKey}/values", "/api/layers/a+b/values"
            ),
        )

    def test_the_two_spellings_do_not_share_a_digest(self) -> None:
        """Because they are different requests, and the binding must say so."""
        self.assertNotEqual(digest(query="f=a+b"), digest(query="f=a%2Bb"))


if __name__ == "__main__":
    unittest.main()
