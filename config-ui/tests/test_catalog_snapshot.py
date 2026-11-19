import unittest
from unittest.mock import Mock, patch

import app


class CatalogSnapshotTests(unittest.TestCase):
    def test_route_uses_one_profile_snapshot(self):
        handler = object.__new__(app.Handler)
        handler.path = '/api/catalog'
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: 'admin'
        handler._json = Mock()
        profiles = [{'name': 'ready', 'status': 'ready'}]
        tables = [{'schema': 'derived_layers', 'table': 'ready'},
                  {'schema': 'derived_layers', 'table': 'pending'}]
        with patch.object(app, 'DERIVED', Mock()), patch.object(
            app, 'derived_semantic_profiles', return_value=profiles,
        ) as enumerate_profiles, patch.object(app, 'discover', return_value=tables):
            handler.do_GET()
        enumerate_profiles.assert_called_once_with()
        body = handler._json.call_args.args[1]
        self.assertEqual(body['derivedProfiles'], profiles)
        self.assertEqual(body['tables'], [tables[0]])

    def test_empty_snapshot_does_not_enumerate_again(self):
        with patch.object(app, 'DERIVED', Mock()), patch.object(
            app, 'derived_semantic_profiles', side_effect=AssertionError('duplicate read'),
        ), patch.object(app, 'discover', return_value=[]):
            self.assertEqual(app.discover_catalog([]), [])
