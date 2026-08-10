import re
import unittest

from relation_identity import parse_relation

IDENTIFIER_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ParseRelationTests(unittest.TestCase):
    def test_splits_a_qualified_relation(self):
        self.assertEqual(
            ("MAPP", "leeds", "bus_stops"),
            parse_relation("leeds.bus_stops", alias="MAPP"),
        )

    def test_defaults_a_bare_relation_to_the_given_schema(self):
        self.assertEqual(
            ("MAPP", "public", "places"),
            parse_relation("places", alias="MAPP", default_schema="public"),
        )

    def test_rejects_a_bare_relation_without_a_default_schema(self):
        self.assertIsNone(parse_relation("places", alias="MAPP"))

    def test_rejects_more_than_one_dot(self):
        self.assertIsNone(
            parse_relation("a.b.c", alias="MAPP", default_schema="public")
        )

    def test_rejects_an_empty_schema_or_relation_part(self):
        self.assertIsNone(
            parse_relation(".places", alias="MAPP", default_schema="public")
        )
        self.assertIsNone(
            parse_relation("public.", alias="MAPP", default_schema="public")
        )

    def test_rejects_non_string_and_empty_values(self):
        self.assertIsNone(parse_relation(None, alias="MAPP"))
        self.assertIsNone(parse_relation("", alias="MAPP", default_schema="public"))
        self.assertIsNone(parse_relation(42, alias="MAPP"))

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(
            ("MAPP", "leeds", "bus_stops"),
            parse_relation(" leeds.bus_stops ", alias="MAPP"),
        )

    def test_alias_may_be_none(self):
        self.assertEqual(
            (None, "leeds", "bus_stops"),
            parse_relation("leeds.bus_stops", alias=None),
        )

    def test_part_pattern_accepts_identifier_shaped_parts(self):
        self.assertEqual(
            (None, "leeds", "bus_stops"),
            parse_relation(
                "leeds.bus_stops", alias=None, part_pattern=IDENTIFIER_PART_RE
            ),
        )

    def test_part_pattern_rejects_a_non_identifier_part(self):
        self.assertIsNone(
            parse_relation(
                "leeds.bus stops; drop table x",
                alias=None,
                part_pattern=IDENTIFIER_PART_RE,
            )
        )
        self.assertIsNone(
            parse_relation(
                "leeds-council.bus_stops",
                alias=None,
                part_pattern=IDENTIFIER_PART_RE,
            )
        )


if __name__ == "__main__":
    unittest.main()
