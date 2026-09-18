"""This runtime's envelope must agree with the configuration API's, byte for byte.

The asymmetry is the whole reason this file exists. This component builds the
envelope and claims its digest; the configuration API rebuilds it from the
request that actually arrives and compares; the broker in between records
whatever it is handed and never recomputes anything.

So a one-byte disagreement between the two copies fails *nowhere near here*. It
fails at the configuration API, as a refusal of every token B, with no
indication of which of the twelve members disagreed -- and the component that
computed the wrong one reports a successful exchange. This test is what turns
that into a failure with a name.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canonical  # noqa: E402
import execution_envelope as envelope  # noqa: E402

PLATFORM = Path(__file__).resolve().parents[2]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class AgreementTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api_canonical = load(
            "api_canonical", PLATFORM / "config-ui" / "canonical.py"
        )
        cls.broker_canonical = load(
            "broker_canonical", PLATFORM / "mcp-auth" / "canonical.py"
        )
        # The envelope exists in two copies, not three: the broker never builds
        # one. It validates the shape of the digest it is handed and stores it.
        cls.api_envelope = load(
            "api_envelope", PLATFORM / "config-ui" / "execution_envelope.py"
        )


class CanonicalizerTests(AgreementTestCase):
    """All three copies of the canonicalizer, on values chosen to drift."""

    CORPUS = [
        {},
        [],
        {"a": 1, "b": 2},
        {"b": 2, "a": 1},
        {"": "empty key"},
        {"é": "latin", "中": "han", "\U0001f600": "astral"},
        {"n": [0, -0.0, 1e21, 1e-7, 9007199254740991]},
        {"s": "quote\" backslash\\ tab\t newline\n control"},
        [[[{"deep": True}]]],
        {"nested": {"a": [1, {"b": None}], "c": False}},
    ]

    def test_every_copy_serialises_the_corpus_identically(self) -> None:
        for index, value in enumerate(self.CORPUS):
            with self.subTest(index=index):
                mine = canonical.digest(value)
                self.assertEqual(mine, self.api_canonical.digest(value))
                self.assertEqual(mine, self.broker_canonical.digest(value))

    def test_every_copy_refuses_the_same_values(self) -> None:
        """Agreement on what is *rejected* matters as much as on what is emitted.

        A value one copy serialises and another refuses is a request the runtime
        will happily bind and the configuration API will not, which surfaces as
        a 403 that names nothing.

        2**53 + 1 is the first integer that does not survive a double round
        trip. Note 2**53 itself is accepted even though MAX_SAFE_INTEGER is
        2**53 - 1: the check is a round trip rather than a comparison against
        the bound, and 2**53 is exactly representable. Harmless, because all
        three copies are the same code and agree, which is what this asserts.
        """
        for value in (
            {"n": 9007199254740993},
            {"n": float("nan")},
            {"n": float("inf")},
        ):
            with self.subTest(value=repr(value)):
                for name, module in (
                    ("runtime", canonical),
                    ("api", self.api_canonical),
                    ("broker", self.broker_canonical),
                ):
                    with self.assertRaises(Exception, msg=f"{name} accepted it"):
                        module.digest(value)

    def test_the_scheme_name_matches_everywhere(self) -> None:
        """The one drift that fails loudly, and only because the digest carries it."""
        self.assertEqual(canonical.SCHEME, self.api_canonical.SCHEME)
        self.assertEqual(canonical.SCHEME, self.broker_canonical.SCHEME)

    def test_the_safe_integer_bound_matches_everywhere(self) -> None:
        self.assertEqual(canonical.MAX_SAFE_INTEGER, self.api_canonical.MAX_SAFE_INTEGER)
        self.assertEqual(
            canonical.MAX_SAFE_INTEGER, self.broker_canonical.MAX_SAFE_INTEGER
        )


class EnvelopeTests(AgreementTestCase):
    """The two that build envelopes, across the cases most likely to diverge."""

    CASES = [
        dict(
            instance="instance-1", method="GET", operation_id="layers.values",
            path_template="/api/layers/{layerKey}/values",
            path="/api/layers/roads/values", query="field=name&limit=10", body=None,
        ),
        # A plus in a query value: decodes to a space here, and a strict RFC 3986
        # reimplementation would disagree on exactly this.
        dict(
            instance="instance-1", method="GET", operation_id="layers.values",
            path_template="/api/layers/{layerKey}/values",
            path="/api/layers/roads/values", query="field=a+b", body=None,
        ),
        # The same bytes encoded: a different request, and a different digest.
        dict(
            instance="instance-1", method="GET", operation_id="layers.values",
            path_template="/api/layers/{layerKey}/values",
            path="/api/layers/roads/values", query="field=a%2Bb", body=None,
        ),
        # Query order is load-bearing, and tool arguments arrive unordered.
        dict(
            instance="instance-1", method="GET", operation_id="layers.values",
            path_template="/api/layers/{layerKey}/values",
            path="/api/layers/roads/values", query="limit=10&field=name", body=None,
        ),
        dict(
            instance="instance-1", method="POST", operation_id="proposals.apply",
            path_template="/api/proposals/{proposalId}/apply",
            path="/api/proposals/p1/apply", query="", body={"approved": True},
        ),
        # An empty body is distinguishable from a null one.
        dict(
            instance="instance-1", method="POST", operation_id="proposals.apply",
            path_template="/api/proposals/{proposalId}/apply",
            path="/api/proposals/p1/apply", query="", body={},
        ),
    ]

    NULLS = dict(resolved_defaults=None, confirmation_fields=None, revision_binding=None)

    def test_both_copies_build_the_same_envelope(self) -> None:
        for index, case in enumerate(self.CASES):
            with self.subTest(index=index):
                self.assertEqual(
                    envelope.build(**case, **self.NULLS),
                    self.api_envelope.build(**case, **self.NULLS),
                )

    def test_both_copies_digest_to_the_same_value(self) -> None:
        for index, case in enumerate(self.CASES):
            with self.subTest(index=index):
                self.assertEqual(
                    envelope.digest(**case, **self.NULLS),
                    self.api_envelope.digest(**case, **self.NULLS),
                )

    def test_the_cases_are_actually_distinct(self) -> None:
        """Otherwise agreement above could be agreement on one value repeated."""
        digests = {envelope.digest(**case, **self.NULLS) for case in self.CASES}
        self.assertEqual(len(self.CASES), len(digests))

    def test_the_pinned_golden_vector_matches_the_other_copy(self) -> None:
        """The literal the configuration API's suite records, recomputed here.

        Differential agreement proves the two copies match each other; it cannot
        prove either is right. This ties both to a value written down.
        """
        expected = (
            "mapp-jcs-v1:"
            "8d6e7d1699c903b06892e5c499350a241c7505a2bd1cf629162a79f439fd6c4e"
        )
        vector = dict(self.CASES[0], **self.NULLS)
        self.assertEqual(expected, envelope.digest(**vector))
        self.assertEqual(expected, self.api_envelope.digest(**vector))

    def test_both_copies_refuse_the_same_inputs(self) -> None:
        """A refusal only one copy makes is a request one will bind and the
        other will not, which reads as an unexplained 403."""
        bad = [
            dict(self.CASES[0], method="get"),
            dict(self.CASES[0], instance=""),
            dict(self.CASES[0], path="/api/layers/../values"),
            dict(self.CASES[0], query="field=one&field=two"),
        ]
        for index, case in enumerate(bad):
            with self.subTest(index=index):
                with self.assertRaises(Exception):
                    envelope.build(**case, **self.NULLS)
                with self.assertRaises(Exception):
                    self.api_envelope.build(**case, **self.NULLS)


if __name__ == "__main__":
    unittest.main()
