from __future__ import annotations

import unittest
from http import HTTPStatus
from unittest.mock import MagicMock, patch

import app


class VisualOperationRoutesTests(unittest.TestCase):
    @staticmethod
    def handler(path, scopes=("visual",)):
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: "oauth:visual-reader"
        handler._authentication = {"scopes": list(scopes)}
        handler._json = lambda status, body: responses.append((status, body))
        return handler, responses

    def test_visual_read_route_has_its_own_bound_action_and_scope(self):
        handler, _ = self.handler("/api/visual-operations/" + "a" * 32)
        self.assertEqual("visual", handler._required_scope(handler.path, "GET"))
        resolved = handler._resolve_operation("GET", handler.path)
        self.assertEqual("visual.operations.show", resolved[0])

    def test_visual_only_reader_can_poll_running_and_finished_previews(self):
        for kind in ("visual.test", "proposal.visual-test", "proposal.screenshot"):
            for status in ("running", "succeeded", "failed", "indeterminate"):
                with self.subTest(kind=kind, status=status):
                    handler, responses = self.handler(
                        "/api/visual-operations/" + "a" * 32)
                    operation = {
                        "id": "a" * 32, "kind": kind, "status": status,
                        "stage": "candidate-page-readiness",
                        "result": {"visual": {"artifacts": {
                            "afterMap": "run/after-map.png"}}},
                    }
                    control = MagicMock()
                    control.read_operation.return_value = operation
                    with patch.object(app, "CONTROL", control):
                        handler.do_GET()
                    self.assertEqual((HTTPStatus.OK, {"operation": operation}),
                                     responses[0])

    def test_visual_endpoint_cannot_read_derived_or_apply_operations(self):
        for kind in ("derived-layer.create", "proposal.apply", "xyz.reload"):
            with self.subTest(kind=kind):
                handler, responses = self.handler(
                    "/api/visual-operations/" + "a" * 32, scopes=("full",))
                control = MagicMock()
                control.read_operation.return_value = {"kind": kind}
                with patch.object(app, "CONTROL", control):
                    handler.do_GET()
                self.assertEqual(HTTPStatus.NOT_FOUND, responses[0][0])
                self.assertNotIn("operation", responses[0][1])

    def test_derived_only_reader_cannot_read_visual_operation(self):
        handler, responses = self.handler(
            "/api/visual-operations/" + "a" * 32, scopes=("derive",))
        control = MagicMock()
        control.read_operation.return_value = {"kind": "proposal.screenshot"}
        with patch.object(app, "CONTROL", control):
            handler.do_GET()
        self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
        self.assertEqual("visual", responses[0][1]["requiredScope"])


if __name__ == "__main__":
    unittest.main()
