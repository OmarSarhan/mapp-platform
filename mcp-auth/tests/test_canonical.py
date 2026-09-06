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

    def test_an_integer_beyond_the_safe_range_is_refused(self) -> None:
        # Beyond 2^53 an integer cannot round-trip through a double, so two
        # components could canonicalize different values from the same bytes
        # and each believe the other agreed.
        canonical._format_number(canonical.MAX_SAFE_INTEGER)
        with self.assertRaises(canonical.CanonicalizationError):
            canonical._format_number(canonical.MAX_SAFE_INTEGER + 1)


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

    def test_an_unsafe_integer_is_refused_on_input(self) -> None:
        with self.assertRaises(canonical.CanonicalizationError):
            canonical.loads(b'{"n":9007199254740992}')

    def test_a_safe_integer_is_accepted(self) -> None:
        self.assertEqual(
            {"n": canonical.MAX_SAFE_INTEGER},
            canonical.loads(b'{"n":9007199254740991}'),
        )

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
