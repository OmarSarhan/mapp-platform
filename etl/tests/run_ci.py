"""Run ETL checks, permitting only explicitly optional external-service skips."""
import sys
import unittest
from pathlib import Path


OPTIONAL_EXTERNAL_TESTS = {
    "test_live_source.LiveSourceTests.test_every_configured_layer_has_valid_metadata_and_geojson",
    "test_live_census_source.LiveCensusSourceTests.test_geometry_contract_and_sample_features_are_current",
    "test_live_census_source.LiveCensusSourceTests.test_representative_and_metadata_exception_topics_are_current",
}


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    unexpected = [(test.id(), reason) for test, reason in result.skipped
                  if test.id() not in OPTIONAL_EXTERNAL_TESTS]
    for test_id, reason in unexpected:
        print(f"Required ETL test skipped: {test_id}: {reason}", file=sys.stderr)
    sys.exit(0 if result.wasSuccessful() and not unexpected else 1)
