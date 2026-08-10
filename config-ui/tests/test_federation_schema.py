import unittest

from federation_schema import (
    FederationSchemaError,
    validate_alias,
    validate_observation,
    validate_registration,
)


def valid_registration(**updates):
    value = {
        "alias": "LEEDS",
        "displayName": "Leeds council read replica",
        "kind": "postgresql",
        "connectionRef": "secret:leeds-federation-reader",
        "tlsPolicy": "verify-full",
        "allowedRelations": ["leeds.bus_stops", "leeds.roads"],
        "dataHandlingClassification": "Open data, no personal information.",
        "dataHandlingAcknowledged": True,
    }
    value.update(updates)
    return value


def valid_observation(**updates):
    value = {
        "connectivity": "reachable",
        "schema": "current",
        "sourceFreshness": "current",
        "lastConnected": "2026-08-10T12:00:00+00:00",
        "lastSchemaVerified": "2026-08-10T12:00:00+00:00",
        "sourceVersion": "release-42",
    }
    value.update(updates)
    return value


class ValidateAliasTests(unittest.TestCase):
    def test_accepts_the_semantic_allowlist_grammar(self):
        self.assertEqual("MAPP", validate_alias("MAPP"))
        self.assertEqual("council_prod", validate_alias("council_prod"))

    def test_rejects_a_leading_digit_and_overlong_values(self):
        for invalid in ("9council", "a" * 64, "", "council prod", None, 7):
            with self.subTest(invalid=invalid):
                with self.assertRaises(FederationSchemaError):
                    validate_alias(invalid)


class ValidateRegistrationTests(unittest.TestCase):
    def test_accepts_a_well_formed_registration_and_normalizes_it(self):
        result = validate_registration(valid_registration())

        self.assertEqual(
            {
                "alias": "LEEDS",
                "displayName": "Leeds council read replica",
                "kind": "postgresql",
                "connectionRef": "secret:leeds-federation-reader",
                "tlsPolicy": "verify-full",
                "allowedRelations": ("leeds.bus_stops", "leeds.roads"),
                "dataHandlingClassification": (
                    "Open data, no personal information."
                ),
                "dataHandlingAcknowledged": True,
                "freshnessStrategy": "manual",
                "status": "pending",
            },
            result,
        )

    def test_defaults_freshness_strategy_to_manual_when_omitted(self):
        result = validate_registration(valid_registration())
        self.assertEqual("manual", result["freshnessStrategy"])

    def test_accepts_an_explicit_freshness_strategy(self):
        result = validate_registration(
            valid_registration(freshnessStrategy="versionRelation")
        )
        self.assertEqual("versionRelation", result["freshnessStrategy"])

    def test_rejects_missing_required_fields(self):
        for field in (
            "alias",
            "displayName",
            "kind",
            "connectionRef",
            "tlsPolicy",
            "allowedRelations",
            "dataHandlingClassification",
            "dataHandlingAcknowledged",
        ):
            payload = valid_registration()
            del payload[field]
            with self.subTest(field=field):
                with self.assertRaises(FederationSchemaError):
                    validate_registration(payload)

    def test_rejects_unknown_properties(self):
        with self.assertRaises(FederationSchemaError):
            validate_registration(valid_registration(hostname="db.example.com"))

    def test_rejects_an_invalid_alias(self):
        with self.assertRaises(FederationSchemaError):
            validate_registration(valid_registration(alias="9-council"))

    def test_rejects_an_empty_or_overlong_display_name(self):
        with self.assertRaises(FederationSchemaError):
            validate_registration(valid_registration(displayName="  "))
        with self.assertRaises(FederationSchemaError):
            validate_registration(valid_registration(displayName="x" * 201))

    def test_rejects_a_kind_other_than_postgresql(self):
        with self.assertRaises(FederationSchemaError):
            validate_registration(valid_registration(kind="mysql"))

    def test_rejects_an_empty_or_overlong_connection_ref(self):
        with self.assertRaises(FederationSchemaError):
            validate_registration(valid_registration(connectionRef=""))
        with self.assertRaises(FederationSchemaError):
            validate_registration(valid_registration(connectionRef="x" * 201))

    def test_rejects_an_invalid_tls_policy(self):
        for invalid in ("disable", "allow", "prefer", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(FederationSchemaError):
                    validate_registration(valid_registration(tlsPolicy=invalid))

    def test_rejects_an_empty_or_malformed_allowed_relations_list(self):
        for invalid in ([], "leeds.roads", ["roads"], ["leeds..roads"], [7]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(FederationSchemaError):
                    validate_registration(
                        valid_registration(allowedRelations=invalid)
                    )

    def test_sorts_allowed_relations(self):
        result = validate_registration(
            valid_registration(allowedRelations=["leeds.roads", "leeds.bus_stops"])
        )
        self.assertEqual(
            ("leeds.bus_stops", "leeds.roads"), result["allowedRelations"]
        )

    def test_rejects_a_duplicate_allowed_relation(self):
        with self.assertRaises(FederationSchemaError):
            validate_registration(
                valid_registration(
                    allowedRelations=["leeds.roads", "leeds.roads"]
                )
            )

    def test_rejects_an_empty_or_overlong_data_handling_classification(self):
        with self.assertRaises(FederationSchemaError):
            validate_registration(valid_registration(dataHandlingClassification=""))
        with self.assertRaises(FederationSchemaError):
            validate_registration(
                valid_registration(dataHandlingClassification="x" * 2001)
            )

    def test_rejects_an_unacknowledged_data_handling_classification(self):
        for invalid in (False, None, "true", 1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(FederationSchemaError):
                    validate_registration(
                        valid_registration(dataHandlingAcknowledged=invalid)
                    )

    def test_rejects_an_invalid_freshness_strategy(self):
        with self.assertRaises(FederationSchemaError):
            validate_registration(valid_registration(freshnessStrategy="nightly"))

    def test_status_is_always_pending_regardless_of_input(self):
        result = validate_registration(valid_registration())
        self.assertEqual("pending", result["status"])


class ValidateObservationTests(unittest.TestCase):
    def test_accepts_a_well_formed_observation(self):
        self.assertEqual(valid_observation(), validate_observation(valid_observation()))

    def test_accepts_null_timestamps_and_source_version(self):
        result = validate_observation(
            valid_observation(
                lastConnected=None,
                lastSchemaVerified=None,
                sourceVersion=None,
                connectivity="unknown",
                schema="unknown",
                sourceFreshness="unknown",
            )
        )
        self.assertIsNone(result["lastConnected"])
        self.assertIsNone(result["sourceVersion"])

    def test_rejects_missing_required_fields(self):
        for field in (
            "connectivity",
            "schema",
            "sourceFreshness",
            "lastConnected",
            "lastSchemaVerified",
            "sourceVersion",
        ):
            payload = valid_observation()
            del payload[field]
            with self.subTest(field=field):
                with self.assertRaises(FederationSchemaError):
                    validate_observation(payload)

    def test_rejects_unknown_properties(self):
        with self.assertRaises(FederationSchemaError):
            validate_observation(valid_observation(healthy=True))

    def test_rejects_invalid_enum_values(self):
        with self.assertRaises(FederationSchemaError):
            validate_observation(valid_observation(connectivity="up"))
        with self.assertRaises(FederationSchemaError):
            validate_observation(valid_observation(schema="ok"))
        with self.assertRaises(FederationSchemaError):
            validate_observation(valid_observation(sourceFreshness="fresh"))

    def test_rejects_a_timestamp_without_timezone(self):
        with self.assertRaises(FederationSchemaError):
            validate_observation(
                valid_observation(lastConnected="2026-08-10T12:00:00")
            )

    def test_rejects_a_non_scalar_source_version(self):
        with self.assertRaises(FederationSchemaError):
            validate_observation(valid_observation(sourceVersion=["v1"]))

    def test_accepts_optional_extension_versions_and_rls_flag(self):
        result = validate_observation(
            valid_observation(
                extensionVersions={
                    "postgresql": "16.2",
                    "postgis": "3.4.2",
                    "proj": "9.3.1",
                    "geos": "3.12.1",
                },
                rowLevelSecurityDetected=True,
            )
        )
        self.assertEqual("16.2", result["extensionVersions"]["postgresql"])
        self.assertTrue(result["rowLevelSecurityDetected"])

    def test_rejects_an_unknown_extension_name(self):
        with self.assertRaises(FederationSchemaError):
            validate_observation(
                valid_observation(extensionVersions={"h3": "4.1.0"})
            )

    def test_rejects_a_non_boolean_rls_flag(self):
        with self.assertRaises(FederationSchemaError):
            validate_observation(valid_observation(rowLevelSecurityDetected="yes"))


if __name__ == "__main__":
    unittest.main()
