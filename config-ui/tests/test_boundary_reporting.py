import unittest
from boundary_reporting import boundary_report


class BoundaryTests(unittest.TestCase):
    def test_mask_and_circle_names_are_not_proof_of_clipping(self):
        report = boundary_report({'extent': {'mask': True, 'north': 54}}, {'name': 'Stops inside circle', 'table': 'public.stops'})
        self.assertEqual('configured', report['visualMask']['status'])
        self.assertEqual('unknown', report['geometryClipping']['status'])
        self.assertEqual('unknown', report['studyBoundary']['status'])
        self.assertEqual('unknown', report['featureSelection']['status'])

    def test_zoom_sources_report_only_verified_platform_selection(self):
        scope = {'selection': 'intersects-output-geometry', 'clipsGeometry': False, 'envelopes': [[1, 2, 3, 4]]}
        report = boundary_report({}, {'table': None, 'tables': {'0': None, '15': 'derived_layers.stops'}}, {'derived_layers.stops': scope})
        self.assertEqual(['derived_layers.stops'], report['relations'])
        self.assertEqual('configured', report['featureSelection']['status'])
        self.assertFalse(report['featureSelection']['platformScopes'][0]['clipsGeometry'])
        self.assertEqual('unknown', report['geometryClipping']['status'])
        self.assertEqual('not-configured', report['visualMask']['status'])
