"""RFC 8785 canonicalization, checked against the specification's own vectors.

P10 makes this a cross-component security contract: mapp-mcp, the broker and
the configuration API each compute the digest independently, and a token is
bound to the value they agree on. A defect here yields a *consistent* wrong
answer everywhere, which no amount of agreement between the components would
reveal -- so the vectors come from the RFC rather than from this
implementation.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canonical


class NumberSerializationTests(unittest.TestCase):
    """ECMAScript Number::toString, which RFC 8785 defers to.

    Python disagrees with it out of the box -- repr(1.0) is '1.0' where
    ECMAScript gives '1' -- so every one of these is a case the digest would
    get wrong if the formatting were left to repr.
    """

    VECTORS = [
        (0.0, "0"),
        (-0.0, "0"),
        (1.0, "1"),
        (100.0, "100"),
        (-1.5, "-1.5"),
        (4.50, "4.5"),
        (2e-3, "0.002"),
        (0.000001, "0.000001"),
        (1e-7, "1e-7"),
        (1e-27, "1e-27"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1e30, "1e+30"),
        (333333333.33333329, "333333333.3333333"),
        (5e-324, "5e-324"),
    ]

    def test_every_published_number_vector(self) -> None:
        for value, expected in self.VECTORS:
            with self.subTest(value=value):
                self.assertEqual(expected, canonical._format_number(value))

    def test_integers_serialize_without_a_decimal_point(self) -> None:
        self.assertEqual("42", canonical._format_number(42))
        self.assertEqual("-42", canonical._format_number(-42))

    def test_a_boolean_is_not_a_number(self) -> None:
        # bool subclasses int in Python, so without an explicit guard True
        # would canonicalize as 1 and change the digest of any envelope
        # carrying a flag.
        with self.assertRaises(canonical.CanonicalizationError):
            canonical._format_number(True)

    def test_values_outside_the_domain_are_refused(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(canonical.CanonicalizationError):
                    canonical._format_number(value)

    def test_an_integer_that_cannot_round_trip_is_refused(self) -> None:
        """The rule is the round trip, not a magnitude.

        Stated as `abs(value) > 2**53 - 1` it refused 2**53 itself, which is a
        power of two and survives a double exactly -- and, worse, it was a
        rule about the Python *type*, so the same number written as 1e16 was
        admitted while its own canonical form was not.
        """
        canonical._format_number(canonical.MAX_SAFE_INTEGER)
        # 2**53 is representable, so it is admitted.
        canonical._format_number(2**53)
        # 2**53 + 1 is not.
        with self.assertRaises(canonical.CanonicalizationError):
            canonical._format_number(2**53 + 1)
        with self.assertRaises(canonical.CanonicalizationError):
            canonical._format_number(-(2**53 + 1))
        with self.assertRaises(canonical.CanonicalizationError):
            canonical._format_number(123456789012345678901)


class CanonicalFormTests(unittest.TestCase):
    def test_the_specification_worked_example(self) -> None:
        """RFC 8785 section 3.2.3, verbatim."""
        value = {
            "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
            "string": "€$\nA'B\"\\\\\"/",
            "literals": [None, True, False],
        }
        expected = (
            '{"literals":[null,true,false],'
            '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
        )
        self.assertEqual(expected, canonical.canonicalize(value).decode())

    def test_members_are_ordered_by_utf16_code_unit(self) -> None:
        """Not by code point, which is what Python would do unaided.

        An astral character encodes as a surrogate pair beginning U+D800, so
        UTF-16 sorts it *below* U+E000 while Python sorts it above. The two
        orders disagree, and RFC 8785 specifies the UTF-16 one.
        """
        value = {"\U0001f600": 1, "": 2}
        self.assertEqual(
            '{"\U0001f600":1,"":2}',
            canonical.canonicalize(value).decode(),
        )

    def test_ordinary_keys_sort_lexicographically(self) -> None:
        self.assertEqual(
            '{"a":1,"b":2,"c":3}',
            canonical.canonicalize({"c": 3, "a": 1, "b": 2}).decode(),
        )

    def test_non_ascii_is_not_escaped(self) -> None:
        # The output is UTF-8 and RFC 8785 escapes nothing above 0x1f.
        self.assertEqual(
            '{"k":"café"}',
            canonical.canonicalize({"k": "café"}).decode(),
        )

    def test_control_characters_use_the_short_escape_where_one_exists(self) -> None:
        self.assertEqual(
            '"\\b\\f\\n\\r\\t\\u0000\\u001f"',
            canonical.canonicalize("\b\f\n\r\t\x00\x1f").decode(),
        )

    def test_no_insignificant_whitespace_is_emitted(self) -> None:
        out = canonical.canonicalize({"a": [1, 2], "b": {"c": 3}}).decode()
        self.assertEqual('{"a":[1,2],"b":{"c":3}}', out)

    def test_an_unserializable_type_is_refused(self) -> None:
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.canonicalize({"k": {1, 2}})


class RawByteRejectionTests(unittest.TestCase):
    """Checks that must run before a parser destroys the evidence."""

    def test_duplicate_member_names_are_refused(self) -> None:
        # json.loads keeps the last silently, so a second "scope" could hide
        # behind the first and the digest would cover only the survivor.
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'{"scope":"inspect","scope":"apply"}')

    def test_a_duplicate_nested_member_is_refused(self) -> None:
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'{"outer":{"a":1,"a":2}}')

    def test_json_constants_are_refused(self) -> None:
        for literal in (b'{"n":NaN}', b'{"n":Infinity}', b'{"n":-Infinity}'):
            with self.subTest(literal=literal):
                with self.assertRaises(canonical.CanonicalizationError):
                    canonical.loads(literal)

    def test_the_domain_check_reaches_inside_arrays(self) -> None:
        """Objects were covered and arrays were not.

        _check_domain recurses into both, but only the object branch had a
        test, so the list branch was guarded by nothing.
        """
        for raw in (
            b'[9007199254740993]',
            b'{"a":[{"b":9007199254740993}]}',
            b'[[[9007199254740993]]]',
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(canonical.CanonicalizationError):
                    canonical.loads(raw)

    def test_a_lone_surrogate_raises_this_modules_error(self) -> None:
        """Not UnicodeEncodeError, which a caller would not be catching.

        RFC 8785 has no encoding for an unpaired surrogate, and a peer
        computing the same digest would not agree about one either.
        """
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'["\\ud800"]')

    def test_a_duplicate_hidden_behind_an_escape_is_refused(self) -> None:
        # "a" and "\u0061" are the same member name after escape processing.
        # The parser hook sees that; a scan over the raw bytes would not.
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'{"a":1,"\\u0061":2}')

    def test_an_integer_that_cannot_round_trip_is_refused_on_input(self) -> None:
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'{"n":9007199254740993}')

    def test_a_representable_integer_is_accepted(self) -> None:
        self.assertEqual(
            {"n": canonical.MAX_SAFE_INTEGER},
            canonical.loads(b'{"n":9007199254740991}'),
        )


class ClosureTests(unittest.TestCase):
    """canonicalize must produce something loads accepts.

    The scheme is evaluated independently at each trust boundary, so a value
    one boundary admits and the next refuses is a disagreement by
    construction. It happened: 1e16 was accepted, canonicalized to
    10000000000000000, and that literal was then rejected by the same loader.
    """

    VECTORS = [
        b'[1e16]',
        b'[1e20]',
        b'[1e21]',
        b'[10000000000000000]',
        b'[-1e16]',
        b'[0.000001]',
        b'[1e-7]',
        b'{"a":[1,2.5,"x"],"b":{"n":null}}',
        b'[9007199254740991]',
        b'[1.7976931348623157e308]',
    ]

    def test_every_canonical_form_is_itself_acceptable(self) -> None:
        for raw in self.VECTORS:
            with self.subTest(raw=raw):
                once = canonical.canonicalize(canonical.loads(raw))
                twice = canonical.canonicalize(canonical.loads(once))
                self.assertEqual(once, twice)

    def test_the_digest_is_stable_across_a_round_trip(self) -> None:
        for raw in self.VECTORS:
            with self.subTest(raw=raw):
                first = canonical.digest(canonical.loads(raw))
                again = canonical.digest(
                    canonical.loads(canonical.canonicalize(canonical.loads(raw)))
                )
                self.assertEqual(first, again)

    def test_non_utf8_input_is_refused(self) -> None:
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'{"k":"\xff"}')

    def test_text_input_is_refused(self) -> None:
        # The duplicate and domain checks are defined over bytes; accepting a
        # str would mean accepting something already decoded by someone else.
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads('{"a":1}')

    def test_malformed_json_is_refused(self) -> None:
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'{"a":}')


class DigestTests(unittest.TestCase):
    def test_the_digest_names_its_scheme(self) -> None:
        value = canonical.digest({"a": 1})
        self.assertTrue(value.startswith(canonical.SCHEME + ":"))

    def test_member_order_does_not_change_the_digest(self) -> None:
        self.assertEqual(
            canonical.digest({"a": 1, "b": 2}),
            canonical.digest({"b": 2, "a": 1}),
        )

    def test_a_changed_value_changes_the_digest(self) -> None:
        self.assertNotEqual(
            canonical.digest({"a": 1}), canonical.digest({"a": 2})
        )

    def test_an_omitted_field_differs_from_an_explicit_null(self) -> None:
        # The scheme has to distinguish these: "no body" and "a body whose
        # field is null" authorise different requests.
        self.assertNotEqual(
            canonical.digest({"a": 1}), canonical.digest({"a": 1, "b": None})
        )

    def test_a_string_digit_differs_from_the_number(self) -> None:
        self.assertNotEqual(
            canonical.digest({"a": 1}), canonical.digest({"a": "1"})
        )

    def test_round_tripping_through_loads_preserves_the_digest(self) -> None:
        raw = b'{"b":[1,2.5,"x"],"a":{"n":null}}'
        self.assertEqual(
            canonical.digest(canonical.loads(raw)),
            canonical.digest(json.loads(raw.decode())),
        )


if __name__ == "__main__":
    unittest.main()
