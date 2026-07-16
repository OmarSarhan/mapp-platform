import os
import unittest
from unittest.mock import patch

import config_admin


class ConfigAdminTests(unittest.TestCase):
    def test_default_control_root_matches_container_mount(self):
        with patch.dict(os.environ, {"CONTROL_DIR": "/control"}):
            args = config_admin.parser().parse_args(["revoke-tokens"])
        self.assertEqual("/control", args.root)


if __name__ == "__main__":
    unittest.main()
