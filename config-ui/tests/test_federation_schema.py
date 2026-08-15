import unittest
from math import inf, nan

from federation_schema import (
    FederationSchemaError,
    enforce_tls_policy,
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

    def test_rejects_a_hyphen(self):
        # An alias becomes the schema name source_<alias> and must also be
        # usable, unquoted, wherever it's referenced as a schema/relation
        # name component (semantic_sources.py, derived_layers.py) — both
        # reject hyphens, so a hyphenated alias could register and
        # provision but never be usable for semantic sync or a derived
        # layer.
        with self.assertRaises(FederationSchemaError):
            validate_alias("leeds-ext")

    def test_max_length_leaves_room_for_the_source_schema_prefix(self):
        # federation_store.py generates the schema name source_<alias> —
        # PostgreSQL silently truncates identifiers over 63 bytes, so two
        # long aliases sharing a truncated prefix would collide. The alias
        # bound must leave exactly enough room for "source_" (7 bytes).
        max_length_alias = "a" * 56
        self.assertEqual(max_length_alias, validate_alias(max_length_alias))
        self.assertLessEqual(len(f"source_{max_length_alias}"), 63)
        with self.assertRaises(FederationSchemaError):
            validate_alias("a" * 57)


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

    def test_accepts_the_only_currently_implemented_freshness_strategy(self):
        result = validate_registration(
            valid_registration(freshnessStrategy="manual")
        )
        self.assertEqual("manual", result["freshnessStrategy"])

    def test_rejects_freshness_strategies_with_no_evidence_collection_yet(self):
        # maximumAge/timestampColumn/versionRelation are documented, but
        # detect_capability() has no evidence-collection for any of them —
        # accepting one would silently promise freshness evidence the
        # platform can never produce.
        for strategy in ("maximumAge", "timestampColumn", "versionRelation"):
            with self.subTest(strategy=strategy):
                with self.assertRaises(FederationSchemaError):
                    validate_registration(
                        valid_registration(freshnessStrategy=strategy)
                    )

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
        for invalid in ("disable", "allow", "prefer", "", [], {}):
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

    def test_rejects_allowed_relations_with_colliding_basenames(self):
        # provision() imports every entry into one local source_<alias>
        # schema — two different remote schemas with a same-named table
        # would collide on the second IMPORT FOREIGN SCHEMA and could never
        # be provisioned, so this must fail at registration instead.
        with self.assertRaises(FederationSchemaError):
            validate_registration(
                valid_registration(
                    allowedRelations=["public.orders", "archive.orders"]
                )
            )

    def test_bounds_allowed_relation_count_and_identifier_lengths(self):
        with self.assertRaises(FederationSchemaError):
            validate_registration(
                valid_registration(
                    allowedRelations=[f"leeds.table_{index}" for index in range(101)]
                )
            )
        for relation in (f"{'s' * 64}.roads", f"leeds.{'r' * 64}"):
            with self.subTest(relation=relation):
                with self.assertRaises(FederationSchemaError):
                    validate_registration(
                        valid_registration(allowedRelations=[relation])
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
        for invalid in ("nightly", []):
            with self.subTest(invalid=invalid):
                with self.assertRaises(FederationSchemaError):
                    validate_registration(
                        valid_registration(freshnessStrategy=invalid)
                    )

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
        for field, value in (
            ("connectivity", "up"),
            ("schema", "ok"),
            ("sourceFreshness", "fresh"),
            ("connectivity", []),
            ("schema", {}),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(FederationSchemaError):
                    validate_observation(valid_observation(**{field: value}))

    def test_rejects_a_timestamp_without_timezone(self):
        with self.assertRaises(FederationSchemaError):
            validate_observation(
                valid_observation(lastConnected="2026-08-10T12:00:00")
            )

    def test_rejects_a_non_scalar_source_version(self):
        for invalid in (
            ["v1"], True, False, nan, inf, -inf, "x" * 201, int("1" * 201)
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(FederationSchemaError):
                    validate_observation(valid_observation(sourceVersion=invalid))

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

    def test_accepts_an_optional_schema_fingerprint(self):
        result = validate_observation(
            valid_observation(schemaFingerprint="abc123|def456")
        )
        self.assertEqual("abc123|def456", result["schemaFingerprint"])

    def test_omitted_schema_fingerprint_still_validates(self):
        # A pre-Discover, connectivity-only observation must still
        # validate — schemaFingerprint is only ever collected alongside
        # the RLS/extension-version evidence.
        result = validate_observation(valid_observation())
        self.assertNotIn("schemaFingerprint", result)

    def test_rejects_an_empty_schema_fingerprint(self):
        with self.assertRaises(FederationSchemaError):
            validate_observation(valid_observation(schemaFingerprint=""))

    def test_rejects_a_non_string_schema_fingerprint(self):
        with self.assertRaises(FederationSchemaError):
            validate_observation(valid_observation(schemaFingerprint=123))


class EnforceTlsPolicyTests(unittest.TestCase):
    def test_accepts_a_connection_meeting_the_policy(self):
        valid = (
            (
                "require",
                "postgresql://reader:secret@host/db?sslmode=require"
                "&gssencmode=disable",
            ),
            (
                "verify-full",
                "postgresql://reader:secret@host/db?sslmode=verify-full"
                "&gssencmode=disable&sslrootcert=system",
            ),
        )
        for policy, connection_url in valid:
            with self.subTest(policy=policy):
                enforce_tls_policy(policy, connection_url)

    def test_rejects_weak_or_unsupported_connection_contracts(self):
        invalid = (
            ("require", "sslmode=disable gssencmode=disable"),
            ("verify-full", "sslmode=require gssencmode=disable sslrootcert=system"),
            ("require", "gssencmode=disable"),
            ("require", "sslmode=typo gssencmode=disable"),
            ("require", "sslmode=require"),
            ("require", "sslmode=require gssencmode=prefer"),
            ("verify-ca", "sslmode=verify-ca gssencmode=disable"),
            (
                "require",
                "sslmode=require gssencmode=disable "
                "target_session_attrs=read-write",
            ),
            (
                "verify-full",
                "sslmode=verify-full gssencmode=disable "
                "sslrootcert=/run/secrets/ca.pem",
            ),
            (
                "verify-full",
                "sslmode=verify-full gssencmode=disable "
                "sslrootcert=system sslcert=/run/secrets/client.pem",
            ),
        )
        base = "host=host dbname=db user=reader password=secret"
        for policy, options in invalid:
            with self.subTest(policy=policy, options=options):
                with self.assertRaises(FederationSchemaError):
                    enforce_tls_policy(policy, f"{base} {options}")

    def test_rejects_implicit_or_non_tcp_endpoints(self):
        invalid = (
            "dbname=db user=reader password=secret",
            "host=/var/run/postgresql dbname=db user=reader password=secret",
            "host=one,two dbname=db user=reader password=secret",
            "host=host dbname=db user=reader",
        )
        for connection in invalid:
            with self.subTest(connection=connection):
                with self.assertRaises(FederationSchemaError):
                    enforce_tls_policy(
                        "require", f"{connection} sslmode=require gssencmode=disable"
                    )


if __name__ == "__main__":
    unittest.main()
