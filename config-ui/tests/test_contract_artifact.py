from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from control_api import (
    API_VERSION,
    CONTRACT_VERSION,
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    RULES_VERSION,
    contract,
)


PLATFORM_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = PLATFORM_ROOT / "contracts/api-compatibility-v1.4.json"


class ContractArtifactTests(unittest.TestCase):
    def test_versioned_artifact_matches_the_runtime_contract(self) -> None:
        value = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        runtime = contract("test-instance")

        self.assertRegex(value["artifactVersion"], r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertEqual(API_VERSION, value["apiVersion"])
        self.assertEqual(CONTRACT_VERSION, value["contractVersion"])
        self.assertEqual(RULES_VERSION, value["rulesVersion"])
        self.assertEqual(runtime["pagination"], {
            "version": value["pagination"]["version"],
            "defaultLimit": value["pagination"]["defaultLimit"],
            "maxLimit": value["pagination"]["maxLimit"],
            "cursor": "opaque",
            "pageMaxResponseBytes": value["pagination"][
                "pageMaxResponseBytes"
            ],
            "pageTooLargeCode": value["pagination"]["pageTooLargeCode"],
            "legacyMaxItems": value["pagination"]["legacyMaxItems"],
            "legacyOverflowCode": value["pagination"][
                "legacyOverflowCode"
            ],
            "semanticPageMaxResponseBytes": value["pagination"][
                "semanticPageMaxResponseBytes"
            ],
            "semanticPageTooLargeCode": value["pagination"][
                "semanticPageTooLargeCode"
            ],
            "derivedDeliveryBlockers": value["pagination"][
                "derivedDeliveryBlockers"
            ],
            "compatibilityArtifact": "contracts/api-compatibility-v1.4.json",
        })
        self.assertEqual("1.1.0", value["artifactVersion"])
        self.assertEqual(DEFAULT_PAGE_LIMIT, value["pagination"]["defaultLimit"])
        self.assertEqual(MAX_PAGE_LIMIT, value["pagination"]["maxLimit"])
        self.assertLess(
            value["pagination"]["semanticPageMaxResponseBytes"],
            20 * 1024 * 1024,
        )
        self.assertEqual(
            "semantic.page_too_large",
            value["pagination"]["semanticPageTooLargeCode"],
        )
        self.assertEqual(
            {
                "itemsField": "deliveryBlockers",
                "moreField": "deliveryBlockersMore",
                "maxItems": 100,
                "firstPageOnly": True,
            },
            value["pagination"]["derivedDeliveryBlockers"],
        )
        self.assertEqual(
            value["semanticServiceVersion"],
            (PLATFORM_ROOT / "semantic-service/VERSION")
            .read_text(encoding="utf-8")
            .strip(),
        )

    def test_paginated_endpoint_declarations_are_unique_and_closed(self) -> None:
        value = json.loads(ARTIFACT.read_text(encoding="utf-8"))
        endpoints = value["pagination"]["endpoints"]
        identities = [(item["method"], item["path"]) for item in endpoints]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertTrue(all(item["method"] == "GET" for item in endpoints))
        self.assertTrue(all(re.fullmatch(r"/[A-Za-z0-9_{}./-]+", item["path"])
                            for item in endpoints))
        self.assertTrue(all(isinstance(item["filters"], list) for item in endpoints))


if __name__ == "__main__":
    unittest.main()
