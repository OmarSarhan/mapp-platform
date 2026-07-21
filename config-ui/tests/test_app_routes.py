from __future__ import annotations

import io
import threading
import tempfile
import unittest
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import app
from control_plane import ControlStore


class CatalogSymbologyValidationTests(unittest.TestCase):
    def test_mixed_case_point_typmod_requires_an_icon(self):
        workspace = {"dbs": "MAPP", "locale": {"layers": {"Stops": {
            "format": "mvt", "table": "leeds.bus_stops", "geom": "geom_3857",
            "srid": 3857, "qID": "id", "style": {"default": {"fillColor": "#fff"}},
        }}}}
        tables = [{"dbs": "MAPP", "schema": "leeds", "table": "bus_stops", "columns": [
            {"name": "id", "geometryType": "", "srid": None},
            {"name": "geom", "geometryType": "Point", "srid": 4326},
            {"name": "geom_3857", "geometryType": "Geometry", "srid": 3857},
        ]}]
        paths = {error["path"] for error in app.validate_catalog(workspace, tables)}
        self.assertIn("locale.layers.Stops.style.default.icon", paths)

    def test_reports_removed_theme_and_category_fields_at_precise_paths(self):
        workspace = {
            "dbs": "MAPP",
            "locale": {
                "layers": {
                    "Places": {
                        "format": "mvt",
                        "dbs": "MAPP",
                        "table": "derived_layers.places",
                        "geom": "geom",
                        "srid": 3857,
                        "qID": "id",
                        "style": {
                            "default": {"fillColor": "#eeeeee"},
                            "theme": {
                                "type": "graduated",
                                "field": "removed_score",
                                "graduated_breaks": "less_than",
                                "categories": [{"value": 10, "style": {}}],
                            },
                            "themes": {
                                "multi": {
                                    "type": "categorized",
                                    "fields": ["kind", "removed_status"],
                                    "categories": [
                                        {
                                            "field": "removed_category_field",
                                            "value": "open",
                                            "style": {"icon": {"type": "dot"}},
                                        }
                                    ],
                                }
                            },
                        },
                    }
                }
            },
        }
        tables = [{
            "dbs": "MAPP",
            "schema": "derived_layers",
            "table": "places",
            "columns": [
                {"name": "id", "geometryType": "", "srid": None},
                {"name": "kind", "geometryType": "", "srid": None},
                {"name": "geom", "geometryType": "POLYGON", "srid": 3857},
            ],
        }]
        paths = {error["path"] for error in app.validate_catalog(workspace, tables)}
        self.assertIn("locale.layers.Places.style.theme.field", paths)
        self.assertIn("locale.layers.Places.style.themes.multi.fields.1", paths)
        self.assertIn(
            "locale.layers.Places.style.themes.multi.categories.0.field",
            paths,
        )


class JsonResponseTests(unittest.TestCase):
    def test_derived_layer_timestamps_are_serialized_as_iso_8601(self):
        handler = object.__new__(app.Handler)
        handler._request_id = "request-test"
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler._json(
            HTTPStatus.OK,
            {
                "derivedLayers": [
                    {
                        "name": "definitive_paths_h3_r9",
                        "createdAt": datetime(
                            2026, 7, 18, 12, 34, 56, tzinfo=timezone.utc
                        ),
                    }
                ]
            },
        )

        body = handler.wfile.getvalue().decode()
        self.assertIn('"createdAt":"2026-07-18T12:34:56+00:00"', body)
        self.assertIn('"requestId":"request-test"', body)
        handler.send_response.assert_called_once_with(HTTPStatus.OK)


class LayersRouteTests(unittest.TestCase):
    @staticmethod
    def handler(path: str) -> tuple[app.Handler, list[tuple[HTTPStatus, dict]]]:
        responses: list[tuple[HTTPStatus, dict]] = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda: "token:test"
        handler._json = lambda status, payload: responses.append((status, payload))
        return handler, responses

    def test_derived_workspace_references_include_effective_named_locales(self):
        workspace = {
            "locale": {
                "layers": {
                    "Paths": {"table": "derived_layers.paths_h3_r9"},
                },
            },
            "locales": {
                "cy": {"name": "Cymraeg"},
            },
        }
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", workspace, "revision"),
        ):
            references = app.derived_workspace_references("paths_h3_r9")
        self.assertEqual(
            references,
            ["locale.layers.Paths", "locales.cy.layers.Paths"],
        )

    def test_derived_workspace_impact_reports_removed_fields(self):
        workspace = {
            "locale": {"layers": {"Paths": {
                "table": "derived_layers.paths_h3_r9",
                "qID": "path_id",
                "geom": "geom_3857",
                "infoj": [{"field": "status"}],
                "style": {
                    "theme": {
                        "type": "categorized",
                        "fields": ["status", "class"],
                        "categories": [
                            {"field": "class", "value": "A", "style": {}},
                        ],
                    },
                },
            }}},
        }
        with patch.object(
            app, "read_workspace", return_value=(b"{}", workspace, "revision")
        ):
            impact = app.derived_workspace_impact(
                "paths_h3_r9", ["path_id", "status"]
            )
        self.assertTrue(impact["requiresSecondOrderChanges"])
        self.assertEqual(
            [item["column"] for item in impact["fieldReferences"]],
            ["path_id", "status", "status"],
        )
        self.assertEqual(impact["consumerLabels"], ["Paths (default map)"])
        self.assertEqual(
            impact["fieldReferences"][0]["label"],
            "Paths (default map) uses “path_id” for its feature ID",
        )
        symbology = [
            item for item in impact["fieldReferences"]
            if "symbology" in item["usage"]
        ]
        self.assertEqual(symbology[0]["path"], (
            "locale.layers.Paths.style.theme.fields.0"
        ))

    def test_layers_route_returns_server_composed_locale(self) -> None:
        workspace = {
            "locale": {
                "layers": {
                    "Stops": {"format": "mvt", "style": {"width": 2}},
                },
            },
            "locales": {
                "cy-GB": {
                    "layers": {
                        "Stops": {"name": "Safleoedd"},
                    },
                },
            },
        }
        handler, responses = self.handler("/api/layers?locale=cy-GB")
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", workspace, "revision-1"),
        ):
            handler.do_GET()

        self.assertEqual(1, len(responses))
        status, payload = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual("revision-1", payload["revision"])
        self.assertEqual("cy-GB", payload["locale"])
        self.assertEqual("mvt", payload["layers"]["Stops"]["format"])
        self.assertEqual("Safleoedd", payload["layers"]["Stops"]["name"])

    def test_layers_route_rejects_an_empty_explicit_locale(self) -> None:
        handler, responses = self.handler("/api/layers?locale=")
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", {"locale": {"layers": {}}}, "revision-1"),
        ):
            handler.do_GET()

        self.assertEqual(
            (
                HTTPStatus.BAD_REQUEST,
                {"error": "Unknown locale: ", "code": "locale.not_found"},
            ),
            responses[0],
        )


class CatalogDiscoveryTests(unittest.TestCase):
    def test_server_catalog_omits_public_schema_without_hiding_it_from_validation(self):
        discovered = [
            {"dbs": "MAPP", "schema": "public", "table": "old_places"},
            {"dbs": "MAPP", "schema": "etl", "table": "places"},
        ]

        with patch.object(app, "discover", return_value=discovered):
            self.assertEqual([discovered[1]], app.discover_catalog())

        # Full discovery remains available to workspace validation.
        self.assertEqual("public", discovered[0]["schema"])

    def test_materialized_geometry_uses_relation_typmod_metadata(self):
        connection = MagicMock()
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchall.return_value = []
        connector = MagicMock()
        connector.return_value.__enter__.return_value = connection

        with patch.object(app.psycopg, "connect", connector):
            self.assertEqual([], app.discover_connection("MAPP", "postgresql://db"))

        discovery_query = cursor.execute.call_args_list[1].args[0]
        self.assertIn("c.relkind IN ('r', 'p', 'v', 'm')", discovery_query)
        self.assertIn("postgis_typmod_type(a.atttypmod)", discovery_query)
        self.assertNotIn("JOIN geometry_columns", discovery_query)


class DerivedBackgroundOperationTests(unittest.TestCase):
    def test_create_records_success_only_after_store_returns(self):
        derived = Mock()
        derived.create.return_value = {
            "name": "slow_places",
            "kind": "materialized",
            "sources": ["etl.places"],
        }
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(app, "CONTROL", control):
            app.run_derived_background(
                "a" * 32,
                "create",
                {"name": "slow_places"},
                "admin",
                "127.0.0.1",
            )

        derived.create.assert_called_once_with({"name": "slow_places"}, "admin")
        control.finish_operation.assert_called_once_with(
            "a" * 32,
            status="succeeded",
            result={"derivedLayer": derived.create.return_value},
        )

    def test_database_failure_is_available_to_status_polling(self):
        derived = Mock()
        derived.refresh.side_effect = RuntimeError("statement timed out")
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(app, "CONTROL", control):
            app.run_derived_background(
                "b" * 32,
                "refresh",
                {},
                "admin",
                "127.0.0.1",
                "slow_places",
            )

        call = control.finish_operation.call_args
        self.assertEqual("failed", call.kwargs["status"])
        self.assertEqual(
            "statement timed out",
            call.kwargs["error"]["message"],
        )

    def test_background_create_with_datetime_metadata_records_success(self):
        with tempfile.TemporaryDirectory() as directory:
            control = ControlStore(Path(directory))
            control.initialize("correct horse battery staple", "instance")
            operation = control.create_operation(
                "derived-layer.create",
                "admin",
                {"name": "definitive_paths_h3_r9"},
            )
            derived = Mock()
            derived.create.return_value = {
                "name": "definitive_paths_h3_r9",
                "kind": "materialized",
                "sources": ["leeds.definitive_paths"],
                "createdAt": datetime(
                    2026, 7, 21, 11, 11, 52, 489807,
                    tzinfo=timezone.utc,
                ),
                "refreshedAt": datetime(
                    2026, 7, 21, 11, 11, 52, 489807,
                    tzinfo=timezone.utc,
                ),
            }

            with patch.object(app, "DERIVED", derived), patch.object(
                app, "CONTROL", control
            ), patch.object(control, "audit"):
                app.run_derived_background(
                    operation["id"],
                    "create",
                    {"name": "definitive_paths_h3_r9"},
                    "admin",
                    "127.0.0.1",
                )

            stored = control.read_operation(operation["id"])
            self.assertEqual("succeeded", stored["status"])
            self.assertIsNone(stored["error"])
            layer = stored["result"]["derivedLayer"]
            self.assertEqual(
                "2026-07-21T11:11:52.489807+00:00",
                layer["createdAt"],
            )
            self.assertEqual(
                "2026-07-21T11:11:52.489807+00:00",
                layer["refreshedAt"],
            )


class ReloadRouteTests(unittest.TestCase):
    @staticmethod
    def handler(payload: dict) -> tuple[app.Handler, list]:
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = "/api/xyz/reload"
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: "token:test"
        handler._payload = lambda: payload
        handler._remote = lambda: "127.0.0.1"
        handler._json = lambda status, body: responses.append((status, body))
        return handler, responses

    def test_reload_uses_and_waits_for_the_current_workspace_fingerprint(self) -> None:
        raw = b'{"key":"demo"}\n'
        fingerprint = app.workspace_fingerprint(raw)
        status = {
            "requestedGeneration": 7,
            "appliedGeneration": 7,
            "workspaceFingerprint": fingerprint,
            "healthy": True,
            "completed": True,
        }
        handler, responses = self.handler({"confirmed": True})
        with (
            patch.object(
                app,
                "read_workspace",
                return_value=(raw, {"key": "demo"}, "revision-1"),
            ),
            patch.object(
                app,
                "request_reload",
                return_value={
                    "requestedGeneration": 7,
                    "expectedWorkspaceFingerprint": fingerprint,
                },
            ) as request_reload,
            patch.object(app, "wait_reload", return_value=status) as wait_reload,
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        request_reload.assert_called_once_with(fingerprint)
        wait_reload.assert_called_once_with(7, fingerprint, 30.0)
        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual(fingerprint, responses[0][1]["status"]["workspaceFingerprint"])

    def test_reload_rejects_a_stale_fingerprint_before_requesting(self) -> None:
        raw = b'{"key":"current"}\n'
        current = app.workspace_fingerprint(raw)
        handler, responses = self.handler({
            "confirmed": True,
            "workspaceFingerprint": "b" * 64,
        })
        with (
            patch.object(
                app,
                "read_workspace",
                return_value=(raw, {"key": "current"}, "revision-1"),
            ),
            patch.object(app, "request_reload") as request_reload,
            patch.object(app.CONTROL, "create_operation") as create_operation,
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        request_reload.assert_not_called()
        create_operation.assert_not_called()
        self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
        self.assertEqual(
            "workspace.fingerprint_conflict",
            responses[0][1]["code"],
        )
        self.assertEqual(
            current,
            responses[0][1]["currentWorkspaceFingerprint"],
        )

    def test_reload_exception_finishes_the_operation_as_indeterminate(self) -> None:
        raw = b'{"key":"current"}\n'
        handler, responses = self.handler({"confirmed": True})
        running = {"id": "a" * 32, "status": "running"}

        def finish(operation_id, *, status, result=None, error=None):
            return {
                **running,
                "status": status,
                "result": result,
                "error": error,
            }

        with (
            patch.object(
                app,
                "read_workspace",
                return_value=(raw, {"key": "current"}, "revision-1"),
            ),
            patch.object(
                app,
                "request_reload",
                return_value={"requestedGeneration": 8},
            ),
            patch.object(app, "wait_reload", side_effect=OSError("mailbox failed")),
            patch.object(
                app.CONTROL,
                "create_operation",
                return_value=running,
            ),
            patch.object(
                app.CONTROL,
                "finish_operation",
                side_effect=finish,
            ) as finish_operation,
        ):
            handler.do_POST()

        self.assertEqual(
            "indeterminate",
            finish_operation.call_args.kwargs["status"],
        )
        self.assertEqual(
            "xyz.reload_interrupted",
            finish_operation.call_args.kwargs["error"]["code"],
        )
        self.assertEqual(HTTPStatus.INTERNAL_SERVER_ERROR, responses[0][0])
        self.assertEqual("indeterminate", responses[0][1]["operation"]["status"])


class ApplyRouteTests(unittest.TestCase):
    @staticmethod
    def handler(payload: dict) -> tuple[app.Handler, list]:
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = "/api/proposals/proposal-1/apply"
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: "token:test"
        handler._payload = lambda: payload
        handler._remote = lambda: "127.0.0.1"
        handler._json = lambda status, body: responses.append((status, body))
        return handler, responses

    @staticmethod
    def proposal() -> dict:
        candidate = {"locale": {"layers": {}}}
        return {
            "id": "proposal-1",
            "status": "pending",
            "originalRevision": "revision-1",
            "candidate": candidate,
            "candidateHash": app.workspace_hash(candidate),
        }

    def test_apply_requires_explicit_approval_before_operation_creation(self):
        handler, responses = self.handler({})
        with patch.object(app.CONTROL, "create_operation") as create_operation:
            handler.do_POST()

        create_operation.assert_not_called()
        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        self.assertEqual("proposal.approval_required", responses[0][1]["code"])

    def test_apply_exception_finishes_the_operation_as_indeterminate(self):
        handler, responses = self.handler({"approved": True})
        running = {"id": "b" * 32, "status": "running"}

        def finish(operation_id, *, status, result=None, error=None):
            return {
                **running,
                "status": status,
                "result": result,
                "error": error,
            }

        with (
            patch.object(app, "proposal_read", return_value=self.proposal()),
            patch.object(app, "validate_candidate", return_value=[]),
            patch.object(
                app,
                "apply_proposal_and_reload",
                side_effect=OSError("reload channel failed"),
            ),
            patch.object(
                app.CONTROL,
                "create_operation",
                return_value=running,
            ),
            patch.object(
                app.CONTROL,
                "finish_operation",
                side_effect=finish,
            ) as finish_operation,
        ):
            handler.do_POST()

        self.assertEqual(
            "indeterminate",
            finish_operation.call_args.kwargs["status"],
        )
        self.assertEqual(
            "proposal.apply_interrupted",
            finish_operation.call_args.kwargs["error"]["code"],
        )
        self.assertEqual(HTTPStatus.INTERNAL_SERVER_ERROR, responses[0][0])
        self.assertEqual("indeterminate", responses[0][1]["operation"]["status"])


class CandidatePreviewRouteTests(unittest.TestCase):
    @staticmethod
    def handler(path: str, payload: dict) -> tuple[app.Handler, list]:
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: "token:test"
        handler._payload = lambda: payload
        handler._remote = lambda: "127.0.0.1"
        handler._json = lambda status, body: responses.append((status, body))
        return handler, responses

    def proposal(self) -> dict:
        original = {"locale": {"layers": {"Stops": {}}}}
        candidate = {"locale": {"layers": {"Stops": {}}}}
        return {
            "id": "proposal-1",
            "status": "pending",
            "originalRevision": "revision-1",
            "original": original,
            "originalHash": app.workspace_hash(original),
            "candidate": candidate,
            "candidateHash": app.workspace_hash(candidate),
            "diff": [],
        }

    def test_browser_request_defaults_to_high_resolution_capture(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"passed": true}')
        with patch.object(app, "urlopen", return_value=response) as urlopen:
            status, result = app.run_browser_visual(
                "Stops",
                {
                    "centre": [1, 2],
                    "layers": ["Stops", "Rail Stations"],
                },
                {},
                target_url="http://xyz-preview:3000",
            )

        request = urlopen.call_args.args[0]
        payload = app.json.loads(request.data)
        self.assertEqual(HTTPStatus.OK, status)
        self.assertTrue(result["passed"])
        self.assertEqual({"width": 1920, "height": 1080}, payload["viewport"])
        self.assertEqual(2, payload["deviceScaleFactor"])
        self.assertEqual(["Stops", "Rail Stations"], payload["layers"])
        self.assertEqual("focus", payload["viewMode"])

    def test_browser_request_propagates_default_view_mode(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"passed": true}')
        with patch.object(app, "urlopen", return_value=response) as urlopen:
            status, result = app.run_browser_visual(
                "Stops",
                {"centre": [1, 2], "layers": ["Stops"]},
                {"viewMode": "default"},
                target_url="http://xyz-preview:3000",
            )

        request = urlopen.call_args.args[0]
        payload = app.json.loads(request.data)
        self.assertEqual(HTTPStatus.OK, status)
        self.assertTrue(result["passed"])
        self.assertEqual("default", payload["viewMode"])

    def test_browser_request_propagates_panel_capture_options(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"passed": true}')
        with patch.object(app, "urlopen", return_value=response) as urlopen:
            status, result = app.run_browser_visual(
                "Stops",
                {"centre": [1, 2], "layers": ["Stops"]},
                {
                    "panel": "filtering",
                    "expectedPanelText": ["Cost", "Length"],
                },
                target_url="http://xyz-preview:3000",
            )

        request = urlopen.call_args.args[0]
        payload = app.json.loads(request.data)
        self.assertEqual(HTTPStatus.OK, status)
        self.assertTrue(result["passed"])
        self.assertEqual(["filtering"], payload["panels"])
        self.assertEqual(["Cost", "Length"], payload["expectedPanelText"])

    def test_group_preview_isolates_added_and_deleted_layers(self):
        existing = {"group": "Transport", "format": "mvt"}
        added = {"group": "Transport", "format": "mvt"}
        original = {
            "locale": {
                "layers": {
                    "Existing": existing,
                    "Deleted": {"group": "Transport", "format": "mvt"},
                    "Other": {"group": "Planning", "format": "mvt"},
                },
            },
        }
        candidate = {
            "locale": {
                "layers": {
                    "Existing": existing,
                    "Added": added,
                    "Other": {"group": "Planning", "format": "mvt"},
                },
            },
        }
        proposal = {"original": original, "candidate": candidate}

        added_preview = app.proposal_group_preview(
            proposal,
            "Added",
            None,
        )
        deleted_preview = app.proposal_group_preview(
            proposal,
            "Deleted",
            None,
        )

        self.assertEqual([], added_preview["original"]["layers"])
        self.assertEqual(["Added"], added_preview["candidate"]["layers"])
        self.assertEqual(["Transport"], added_preview["candidate"]["groups"])
        self.assertEqual(["Deleted"], deleted_preview["original"]["layers"])
        self.assertEqual([], deleted_preview["candidate"]["layers"])
        self.assertEqual("Existing", deleted_preview["candidate"]["anchorLayer"])
        self.assertIsNone(deleted_preview["candidate"]["renderLayer"])
        self.assertEqual("added", added_preview["changeKind"])
        self.assertEqual("removed", deleted_preview["changeKind"])
        self.assertFalse(
            deleted_preview["candidate"]["requestedLayerPresent"],
        )

    def test_group_preview_retains_configured_tile_background_key(self):
        osm = {"format": "tiles", "display": True}
        proposal = {
            "original": {"locale": {"layers": {"Open_Street_Map": osm}}},
            "candidate": {"locale": {"layers": {"Open_Street_Map": osm}}},
        }

        preview = app.proposal_group_preview(
            proposal, "Open_Street_Map", None
        )

        self.assertEqual(
            ["Open_Street_Map"],
            preview["candidate"]["backgroundLayers"],
        )

    def test_group_move_isolates_moved_layer_across_affected_groups(self):
        proposal = {
            "original": {
                "locale": {
                    "layers": {
                        "Moved": {"group": "Old"},
                        "Old peer": {"group": "Old"},
                        "New peer": {"group": "New"},
                        "Unrelated": {"group": "Other"},
                    },
                },
            },
            "candidate": {
                "locale": {
                    "layers": {
                        "Moved": {"group": "New"},
                        "Old peer": {"group": "Old"},
                        "New peer": {"group": "New"},
                        "Unrelated": {"group": "Other"},
                    },
                },
            },
        }

        preview = app.proposal_group_preview(proposal, "Moved", None)

        self.assertEqual(["Old", "New"], preview["groups"])
        self.assertEqual(["Moved"], preview["original"]["layers"])
        self.assertEqual(["Moved"], preview["candidate"]["layers"])
        self.assertEqual("moved", preview["changeKind"])

    def test_non_membership_edit_retains_group_context(self):
        proposal = {
            "original": {
                "locale": {
                    "layers": {
                        "Edited": {"group": "Transport"},
                        "Peer": {"group": "Transport"},
                    },
                },
            },
            "candidate": {
                "locale": {
                    "layers": {
                        "Edited": {
                            "group": "Transport",
                            "name": "Edited name",
                        },
                        "Peer": {"group": "Transport"},
                    },
                },
            },
        }

        preview = app.proposal_group_preview(proposal, "Edited", None)

        self.assertEqual("edited", preview["changeKind"])
        self.assertEqual(
            ["Edited", "Peer"],
            preview["candidate"]["layers"],
        )

    def test_deleted_group_layer_plans_original_and_activates_none_after(self):
        handler, responses = self.handler(
            "/api/proposals/proposal-1/visual-test",
            {"layer": "Deleted"},
        )
        proposal = self.proposal()
        proposal["original"]["locale"]["layers"] = {
            "Deleted": {"group": "Transport"},
            "Remaining": {"group": "Transport"},
        }
        proposal["candidate"]["locale"]["layers"] = {
            "Remaining": {"group": "Transport"},
        }
        proposal["originalHash"] = app.workspace_hash(proposal["original"])
        proposal["candidateHash"] = app.workspace_hash(proposal["candidate"])
        binding = {
            "source": "candidate",
            "proposalId": "proposal-1",
            "candidateHash": proposal["candidateHash"],
        }

        with (
            patch.object(app, "preview_proposal", return_value=proposal),
            patch.object(
                app,
                "visual_plan",
                return_value={"locale": "locale", "centre": [1, 2]},
            ) as visual_plan,
            patch.object(
                app,
                "prepare_candidate_preview",
                return_value={"source": "candidate", "generation": 2},
            ),
            patch.object(
                app,
                "run_browser_visual",
                return_value=(
                    HTTPStatus.OK,
                    {
                        "runId": "run-candidate",
                        "passed": True,
                        "metadata": binding,
                    },
                ),
            ) as runner,
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        self.assertIs(proposal["original"], visual_plan.call_args.args[0])
        self.assertEqual("Deleted", visual_plan.call_args.args[1])
        self.assertIsNone(runner.call_args.args[0])
        self.assertEqual([], runner.call_args.args[1]["layers"])
        self.assertEqual(
            ["Transport"],
            runner.call_args.args[1]["activeGroups"],
        )
        self.assertTrue(responses[0][1]["plan"]["requestedLayerDeleted"])
        self.assertEqual("original", responses[0][1]["plan"]["viewSource"])
        self.assertEqual("removed", responses[0][1]["plan"]["changeKind"])

    def test_plan_is_bound_to_stored_candidate_without_publishing(self) -> None:
        handler, responses = self.handler(
            "/api/proposals/proposal-1/visual-plan", {"layer": "Stops"}
        )
        proposal = self.proposal()
        with (
            patch.object(app, "preview_proposal", return_value=proposal),
            patch.object(app, "visual_plan", return_value={"centre": [1, 2]}),
            patch.object(app, "prepare_candidate_preview") as prepare,
        ):
            handler.do_POST()
        self.assertFalse(prepare.called)
        status, body = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual("candidate", body["source"])
        self.assertEqual("proposal-1", body["proposalId"])
        self.assertEqual(proposal["candidateHash"], body["candidateHash"])

    def test_visual_run_preserves_binding_and_uses_preview_origin(self) -> None:
        handler, responses = self.handler(
            "/api/proposals/proposal-1/screenshot", {"layer": "Stops"}
        )
        proposal = self.proposal()
        binding = {
            "source": "candidate",
            "proposalId": "proposal-1",
            "candidateHash": proposal["candidateHash"],
        }
        original_binding = {
            "source": "original",
            "proposalId": "proposal-1",
            "originalHash": proposal["originalHash"],
        }
        lock_observations = []

        def run(*args, **kwargs):
            def probe():
                acquired = app.PREVIEW_LOCK.acquire(blocking=False)
                lock_observations.append(acquired)
                if acquired:
                    app.PREVIEW_LOCK.release()
            worker = threading.Thread(target=probe)
            worker.start()
            worker.join()
            metadata = args[2]["metadata"]
            return HTTPStatus.OK, {
                "runId": f"run-{metadata['source']}",
                "passed": True,
                "metadata": metadata,
                "artifacts": {
                    "beforePage": f"run-{metadata['source']}/before-page.png",
                    "beforeMap": f"run-{metadata['source']}/before-map.png",
                    "report": f"run-{metadata['source']}/report.json",
                },
            }

        with (
            patch.object(app, "preview_proposal", return_value=proposal),
            patch.object(app, "visual_plan", return_value={"centre": [1, 2]}),
            patch.object(
                app, "prepare_original_preview",
                return_value={"source": "original", "generation": 1},
            ),
            patch.object(
                app, "prepare_candidate_preview",
                return_value={"source": "candidate", "generation": 2},
            ),
            patch.object(
                app, "run_browser_visual",
                side_effect=run,
            ) as runner,
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()
        self.assertEqual("http://xyz-preview:3000", runner.call_args.kwargs["target_url"])
        self.assertEqual(binding, runner.call_args.args[2]["metadata"])
        self.assertEqual(original_binding, runner.call_args_list[0].args[2]["metadata"])
        self.assertEqual([False, False], lock_observations)
        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual(binding["candidateHash"], responses[0][1]["candidateHash"])
        self.assertEqual(
            "run-original/before-page.png",
            responses[0][1]["visual"]["artifacts"]["beforePage"],
        )
        self.assertEqual(
            "run-candidate/before-page.png",
            responses[0][1]["visual"]["artifacts"]["afterPage"],
        )

    def test_screenshot_preserves_high_resolution_capture_metadata(self) -> None:
        handler, responses = self.handler(
            "/api/proposals/proposal-1/screenshot",
            {
                "layer": "Stops",
                "panels": ["filtering", "styling"],
                "expectedPanelText": ["Cost"],
            },
        )
        proposal = self.proposal()
        binding = {
            "source": "candidate",
            "proposalId": "proposal-1",
            "candidateHash": proposal["candidateHash"],
        }
        original_binding = {
            "source": "original",
            "proposalId": "proposal-1",
            "originalHash": proposal["originalHash"],
        }
        running = {"id": "e" * 32, "status": "running"}

        def finish(operation_id, *, status, result=None, error=None):
            return {
                **running,
                "status": status,
                "result": result,
                "error": error,
            }

        with (
            patch.object(app, "preview_proposal", return_value=proposal),
            patch.object(app, "visual_plan", return_value={"centre": [1, 2]}),
            patch.object(
                app,
                "prepare_original_preview",
                return_value={"source": "original", "generation": 1},
            ),
            patch.object(
                app,
                "prepare_candidate_preview",
                return_value={"source": "candidate", "generation": 2},
            ),
            patch.object(
                app,
                "run_browser_visual",
                side_effect=[
                    (
                        HTTPStatus.OK,
                        {
                            "runId": "run-original",
                            "passed": True,
                            "metadata": original_binding,
                            "artifacts": {
                                "beforePage": "run-original/before-page.png",
                                "beforeMap": "run-original/before-map.png",
                                "filteringPanel": (
                                    "run-original/filtering-panel.png"
                                ),
                                "stylingPanel": "run-original/styling-panel.png",
                            },
                            "capture": {
                                "viewport": {"width": 1080, "height": 1080},
                                "deviceScaleFactor": 1,
                            },
                        },
                    ),
                    (
                        HTTPStatus.OK,
                        {
                            "runId": "run-candidate",
                            "passed": True,
                            "metadata": binding,
                            "artifacts": {
                                "beforePage": "run-candidate/before-page.png",
                                "beforeMap": "run-candidate/before-map.png",
                                "filteringPanel": (
                                    "run-candidate/filtering-panel.png"
                                ),
                                "stylingPanel": "run-candidate/styling-panel.png",
                            },
                            "capture": {
                                "viewport": {"width": 1080, "height": 1080},
                                "deviceScaleFactor": 1,
                                "images": {
                                    "page": {"width": 1080, "height": 1080},
                                },
                            },
                        },
                    ),
                ],
            ) as runner,
            patch.object(
                app.CONTROL, "create_operation", return_value=running
            ) as create_operation,
            patch.object(
                app.CONTROL,
                "finish_operation",
                side_effect=finish,
            ),
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        self.assertEqual(
            "proposal.screenshot",
            create_operation.call_args.args[0],
        )
        for call in runner.call_args_list:
            self.assertEqual(
                {"width": 1080, "height": 1080},
                call.args[2]["viewport"],
            )
            self.assertEqual(1, call.args[2]["deviceScaleFactor"])
            self.assertFalse(call.args[2]["fullPage"])
            self.assertEqual(["filtering", "styling"], call.args[2]["panels"])
            self.assertEqual(["Cost"], call.args[2]["expectedPanelText"])
        self.assertEqual(
            ["filtering", "styling"],
            create_operation.call_args.args[2]["panels"],
        )
        artifacts = responses[0][1]["visual"]["artifacts"]
        self.assertEqual(
            "run-original/filtering-panel.png",
            artifacts["beforeFilteringPanel"],
        )
        self.assertEqual(
            "run-candidate/filtering-panel.png",
            artifacts["afterFilteringPanel"],
        )
        self.assertEqual(
            "run-original/styling-panel.png",
            artifacts["beforeStylingPanel"],
        )
        self.assertEqual(
            "run-candidate/styling-panel.png",
            artifacts["afterStylingPanel"],
        )
        self.assertEqual(
            {"width": 1080, "height": 1080},
            responses[0][1]["visual"]["capture"]["candidate"]["images"]["page"],
        )
        serialized = app.json.dumps(responses[0][1])
        self.assertIn('"afterMap"', serialized)
        self.assertNotIn(
            "operation",
            responses[0][1]["operation"]["result"],
        )

    def test_information_screenshot_selects_feature_in_both_comparison_images(self):
        handler, responses = self.handler(
            "/api/proposals/proposal-1/screenshot", {"layer": "Stops"}
        )
        proposal = self.proposal()
        proposal["original"]["locale"]["layers"]["Stops"]["infoj"] = [
            {"title": "Name"},
        ]
        proposal["candidate"]["locale"]["layers"]["Stops"]["infoj"] = [
            {"title": "Stop name"},
        ]
        proposal["originalHash"] = app.workspace_hash(proposal["original"])
        proposal["candidateHash"] = app.workspace_hash(proposal["candidate"])
        proposal["diff"] = [{
            "op": "replace",
            "path": "/locale/layers/Stops/infoj/0/title",
            "old": "Name",
            "value": "Stop name",
        }]

        def run(layer, plan, payload, **kwargs):
            source = payload["metadata"]["source"]
            self.assertTrue(plan["interaction"]["requireInfoPanel"])
            return HTTPStatus.OK, {
                "runId": f"run-{source}",
                "passed": True,
                "metadata": payload["metadata"],
                "artifacts": {
                    "afterPage": f"run-{source}/after-page.png",
                    "afterMap": f"run-{source}/after-map.png",
                    "infoPanel": f"run-{source}/info-panel.png",
                    "report": f"run-{source}/report.json",
                },
            }

        with (
            patch.object(app, "preview_proposal", return_value=proposal),
            patch.object(app, "visual_plan", return_value={
                "locale": "locale",
                "interaction": {"type": "click-centre-feature"},
            }),
            patch.object(
                app, "prepare_original_preview",
                return_value={"source": "original", "generation": 1},
            ),
            patch.object(
                app, "prepare_candidate_preview",
                return_value={"source": "candidate", "generation": 2},
            ),
            patch.object(app, "run_browser_visual", side_effect=run) as runner,
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        self.assertEqual(2, runner.call_count)
        visual = responses[0][1]["visual"]
        self.assertTrue(visual["comparison"]["featureInfoPanel"])
        self.assertEqual(
            "run-original/after-page.png",
            visual["artifacts"]["beforePage"],
        )
        self.assertEqual(
            "run-candidate/after-page.png",
            visual["artifacts"]["afterPage"],
        )
        self.assertEqual(
            "run-original/info-panel.png",
            visual["artifacts"]["beforeInfoPanel"],
        )
        self.assertEqual(
            "run-candidate/info-panel.png",
            visual["artifacts"]["afterInfoPanel"],
        )

    def test_visual_exception_finishes_the_operation_as_indeterminate(self):
        handler, responses = self.handler(
            "/api/proposals/proposal-1/visual-test",
            {"layer": "Stops"},
        )
        proposal = self.proposal()
        running = {"id": "c" * 32, "status": "running"}

        def finish(operation_id, *, status, result=None, error=None):
            return {
                **running,
                "status": status,
                "result": result,
                "error": error,
            }

        with (
            patch.object(app, "preview_proposal", return_value=proposal),
            patch.object(app, "visual_plan", return_value={"centre": [1, 2]}),
            patch.object(
                app,
                "prepare_candidate_preview",
                side_effect=OSError("preview mailbox failed"),
            ),
            patch.object(
                app.CONTROL,
                "create_operation",
                return_value=running,
            ),
            patch.object(
                app.CONTROL,
                "finish_operation",
                side_effect=finish,
            ) as finish_operation,
        ):
            handler.do_POST()

        self.assertEqual(
            "indeterminate",
            finish_operation.call_args.kwargs["status"],
        )
        self.assertEqual(
            "visual.preview_interrupted",
            finish_operation.call_args.kwargs["error"]["code"],
        )
        self.assertEqual(HTTPStatus.INTERNAL_SERVER_ERROR, responses[0][0])
        self.assertEqual("indeterminate", responses[0][1]["operation"]["status"])

    def test_visual_invalid_runner_response_finishes_the_operation(self):
        handler, responses = self.handler(
            "/api/proposals/proposal-1/visual-test",
            {"layer": "Stops"},
        )
        proposal = self.proposal()
        running = {"id": "d" * 32, "status": "running"}

        def finish(operation_id, *, status, result=None, error=None):
            return {
                **running,
                "status": status,
                "result": result,
                "error": error,
            }

        with (
            patch.object(app, "preview_proposal", return_value=proposal),
            patch.object(app, "visual_plan", return_value={"centre": [1, 2]}),
            patch.object(
                app,
                "prepare_candidate_preview",
                return_value={"generation": 2},
            ),
            patch.object(app, "run_browser_visual", return_value=(200, None)),
            patch.object(
                app.CONTROL,
                "create_operation",
                return_value=running,
            ),
            patch.object(
                app.CONTROL,
                "finish_operation",
                side_effect=finish,
            ) as finish_operation,
        ):
            handler.do_POST()

        self.assertEqual("failed", finish_operation.call_args.kwargs["status"])
        self.assertEqual(
            "visual.invalid_response",
            finish_operation.call_args.kwargs["error"]["code"],
        )
        self.assertEqual(HTTPStatus.BAD_GATEWAY, responses[0][0])

    def test_preview_rejects_non_pending_and_corrupt_proposals(self) -> None:
        declined = self.proposal()
        declined["status"] = "declined"
        with patch.object(app, "proposal_read", return_value=declined):
            with self.assertRaisesRegex(ValueError, "declined"):
                app.preview_proposal("proposal-1")
        corrupt = self.proposal()
        corrupt["candidateHash"] = "0" * 64
        with patch.object(app, "proposal_read", return_value=corrupt):
            with self.assertRaisesRegex(RuntimeError, "integrity"):
                app.preview_proposal("proposal-1")

    def test_preview_lock_is_reentrant_for_publish_and_render_scope(self) -> None:
        self.assertIsInstance(app.PREVIEW_LOCK, type(threading.RLock()))

    def test_live_preview_sync_publishes_exact_committed_workspace(self) -> None:
        encoded = b'{"key":"demo","locale":{"layers":{}}}\n'
        workspace = app.json.loads(encoded)
        with patch.object(
            app,
            "prepare_workspace_preview",
            return_value={"source": "live"},
        ) as prepare:
            result = app.sync_live_preview(encoded)

        self.assertEqual({"source": "live"}, result)
        prepare.assert_called_once_with(
            workspace,
            app.workspace_hash(workspace),
            source="live",
            timeout=30,
            wait=False,
        )


class AuthorizationScopeTests(unittest.TestCase):
    def handler(self, scopes):
        handler = object.__new__(app.Handler)
        handler._actor = lambda state_change=False: "token:test"
        handler._authentication = {
            "actor": "token:test",
            "scopes": scopes,
        }
        handler._json = Mock()
        return handler

    def test_narrow_scope_is_enforced_and_full_remains_compatible(self):
        visual = self.handler(["visual"])
        self.assertEqual(
            "token:test",
            visual._authorized(required_scope="visual"),
        )
        self.assertIsNone(visual._authorized(required_scope="apply"))
        self.assertEqual(
            "apply",
            visual._json.call_args.args[1]["requiredScope"],
        )
        full = self.handler(["full"])
        self.assertEqual(
            "token:test",
            full._authorized(required_scope="reload"),
        )


if __name__ == "__main__":
    unittest.main()
