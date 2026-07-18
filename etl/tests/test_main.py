from __future__ import annotations

import argparse
import logging
import unittest
from unittest.mock import MagicMock, patch

import leeds_arcgis_etl.__main__ as etl_main
from leeds_arcgis_etl.arcgis import ArcGISError
from leeds_arcgis_etl.config import AppConfig
from test_core import sample_layer


class ArgumentTests(unittest.TestCase):
    def test_env_layer_is_comma_separated(self) -> None:
        with patch.dict(
            "os.environ",
            {"ETL_LAYER": "bus_stops, definitive_paths"},
            clear=False,
        ), patch("sys.argv", ["leeds_arcgis_etl"]):
            args = etl_main._parse_args()
        self.assertEqual(args.layers, ["bus_stops", "definitive_paths"])

    def test_cli_layers_take_precedence_over_env(self) -> None:
        with patch.dict("os.environ", {"ETL_LAYER": "bus_stops"}, clear=False), patch(
            "sys.argv",
            [
                "leeds_arcgis_etl",
                "--layer",
                "definitive_paths",
                "--layer",
                "smoke_control_orders",
            ],
        ):
            args = etl_main._parse_args()
        self.assertEqual(
            args.layers,
            ["definitive_paths", "smoke_control_orders"],
        )


class ErrorReportingTests(unittest.TestCase):
    def _run_with_error(
        self,
        error: Exception,
    ) -> tuple[int, list[logging.LogRecord], MagicMock]:
        args = argparse.Namespace(config="unused.json", layers=None, check_source=False)
        config = AppConfig(
            target_schema="test",
            page_size=2,
            http_timeout_seconds=1,
            http_retries=0,
            layers=(sample_layer(),),
        )
        store = MagicMock()

        with (
            patch.object(etl_main, "_parse_args", return_value=args),
            patch.object(etl_main, "load_config", return_value=config),
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://unused"}),
            patch(
                "leeds_arcgis_etl.database.connect_with_retry",
                return_value=object(),
            ),
            patch("leeds_arcgis_etl.database.PostgresStore", return_value=store),
            patch("leeds_arcgis_etl.pipeline.run_layer", side_effect=error),
            self.assertLogs("leeds_arcgis_etl.__main__", level="ERROR") as logs,
        ):
            result = etl_main.main()

        return result, logs.records, store

    def test_arcgis_error_is_logged_without_traceback_and_returns_one(self) -> None:
        result, records, store = self._run_with_error(
            ArcGISError(
                "ArcGIS service error from https://example.test/query (code 400): "
                "Unable to complete operation."
            )
        )

        self.assertEqual(result, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0].getMessage(),
            "sample failed safely: "
            "ArcGIS service error from https://example.test/query (code 400): "
            "Unable to complete operation. "
            "Stale-row reconciliation was skipped.",
        )
        self.assertIsNone(records[0].exc_info)
        store.initialize.assert_called_once_with()
        store.close.assert_called_once_with()

    def test_unexpected_error_keeps_traceback_and_returns_one(self) -> None:
        result, records, store = self._run_with_error(RuntimeError("unexpected"))

        self.assertEqual(result, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0].getMessage(),
            "sample failed",
        )
        self.assertIsNotNone(records[0].exc_info)
        store.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
