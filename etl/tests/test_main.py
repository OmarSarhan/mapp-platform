from __future__ import annotations

import unittest
from unittest.mock import patch

from leeds_arcgis_etl.__main__ import _parse_args


class ArgumentTests(unittest.TestCase):
    def test_env_layer_is_comma_separated(self) -> None:
        with patch.dict(
            "os.environ",
            {"ETL_LAYER": "bus_stops, definitive_paths"},
            clear=False,
        ), patch("sys.argv", ["leeds_arcgis_etl"]):
            args = _parse_args()
        self.assertEqual(args.layers, ["bus_stops", "definitive_paths"])

    def test_cli_layers_take_precedence_over_env(self) -> None:
        with patch.dict("os.environ", {"ETL_LAYER": "bus_stops"}, clear=False), patch(
            "sys.argv",
            [
                "leeds_arcgis_etl",
                "--layer",
                "definitive_paths",
                "--layer",
                "planning_applications_recent",
            ],
        ):
            args = _parse_args()
        self.assertEqual(
            args.layers,
            ["definitive_paths", "planning_applications_recent"],
        )


if __name__ == "__main__":
    unittest.main()
