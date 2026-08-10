from __future__ import annotations

import json
import unittest
from http import HTTPStatus
from types import MethodType
from unittest.mock import Mock, patch

import app
from control_plane import TOKEN_SCOPES


PROFILE = {
    "displayName": "Roads",
    "description": "Road centreline geometry.",
    "tags": ["transport"],
    "caveats": ["Metadata-only inference; review before use."],
}


def asset(*, status="ready", visibility="inspect"):
    return {
        "id": "asset:roads",
        "version": 7,
        "generation": 3,
        "status": status,
        "visibility": visibility,
        "generated": {
            "name": "roads",
            "kind": "view",
            "description": "Managed road geometry",
            "binding": {
                "adapter": "postgresql",
                "schema": "derived_layers",
                "relation": "roads",
                "private": "do-not-send",
            },
            "definitionDigest": "private-digest",
            "actor": "private-actor",
            "query": "SELECT secret FROM private",
            "sources": ["private.source"],
            "spatialScope": {
                "type": "workspace-map-extent",
                "locale": "locale",
                "sourceView": {"lng": -1.5491, "lat": 53.8008, "z": 11},
                "scopeZoom": 10,
                "zoomOffset": -1,
                "viewport": {
                    "width": 1920,
                    "height": 1080,
                    "tileSize": 256,
                },
                "crs": "EPSG:4326",
                "envelopes": [{
                    "west": -2.8,
                    "south": 53.0,
                    "east": -0.2,
                    "north": 54.6,
                }],
                "selection": "intersects-output-geometry",
                "clipsGeometry": False,
                "guidance": "This is a fixed output-row guard.",
            },
            "fields": [
                {
                    "id": "field:id",
                    "name": "id",
                    "type": "bigint",
                    "nullable": False,
                    "description": "Source column comment",
                    "private": "do-not-send",
                },
                {
                    "id": "field:label/with~escapes",
                    "name": "label",
                    "type": "text",
                    "nullable": True,
                },
            ],
        },
        "curated": {
            "displayName": "Existing roads",
            "description": "Existing description",
            "custom": "preserve and do not send",
            "fields": {
                "field:id": {
                    "description": "Stable identifier",
                    "custom": "private custom annotation",
                },
                "field:label/with~escapes": {
                    "description": "Existing label",
                    "tags": ["name"],
                    "custom": "preserve and do not send",
                },
            },
        },
        "orphans": [{"annotation": "do-not-send"}],
    }


class SemanticGenerationRouteTests(unittest.TestCase):
    @staticmethod
    def handler(payload, scopes=None):
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = "/api/semantic/generate"
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: "token:test"
        handler._authentication = {
            "scopes": scopes
            if scopes is not None
            else ["semantic:inspect", "semantic:generate"]
        }
        handler._payload = lambda: payload
        handler._remote = lambda: "127.0.0.1"
        handler._json = lambda status, body: responses.append((status, body))
        handler.send_error = lambda status: responses.append((status, {}))
        return handler, responses

    def test_table_generation_returns_review_only_targeted_operations(self):
        handler, responses = self.handler({
            "assetId": "asset:roads",
            "target": {"kind": "table"},
        })
        handler._semantic_request = Mock(return_value={"asset": asset()})
        gemini = Mock()
        gemini.generate.return_value = PROFILE
        control = Mock()

        with patch.object(app, "GEMINI", gemini), patch.object(
            app, "CONTROL", control
        ):
            handler.do_POST()

        status, response = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        draft = response["draft"]
        self.assertEqual("asset:roads", draft["assetId"])
        self.assertEqual(7, draft["baseVersion"])
        self.assertEqual({"kind": "table"}, draft["target"])
        self.assertEqual(
            [
                "/curated/displayName",
                "/curated/description",
                "/curated/tags",
                "/curated/caveats",
            ],
            [operation["path"] for operation in draft["operations"]],
        )
        self.assertEqual(
            {
                "provider": "gemini",
                "model": app.GEMINI_MODEL,
                "metadataOnly": True,
                "contextOptions": {
                    "sampleRows": False,
                    "statistics": False,
                },
                "proposalCreated": False,
            },
            response["generation"],
        )
        handler._semantic_request.assert_called_once_with(
            "token:test",
            "/v1/assets/asset%3Aroads",
        )
        context = gemini.generate.call_args.args[0]
        serialized = json.dumps(context)
        for forbidden in (
            "private-digest",
            "private-actor",
            "SELECT secret",
            "private.source",
            "do-not-send",
            "private custom annotation",
            "orphans",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(
            {"displayName": "Existing roads", "description": "Existing description"},
            context["currentAnnotation"],
        )
        self.assertEqual(
            "Source column comment",
            context["table"]["fields"][0]["description"],
        )
        self.assertEqual(
            "workspace-map-extent",
            context["table"]["spatialScope"]["type"],
        )
        control.audit.assert_called_once()
        self.assertNotIn(
            "Road centreline geometry",
            json.dumps(control.audit.call_args.kwargs["details"]),
        )

    def test_field_generation_discloses_only_selected_field_and_annotation(self):
        handler, responses = self.handler({
            "assetId": "asset:roads",
            "target": {
                "kind": "field",
                "fieldId": "field:label/with~escapes",
            },
        })
        handler._semantic_request = Mock(return_value={"asset": asset()})
        gemini = Mock()
        gemini.generate.return_value = PROFILE

        with patch.object(app, "GEMINI", gemini), patch.object(
            app, "CONTROL", Mock()
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        context = gemini.generate.call_args.args[0]
        serialized = json.dumps(context)
        self.assertEqual("label", context["field"]["name"])
        self.assertNotIn('"name": "id"', serialized)
        self.assertNotIn("field:id", serialized)
        self.assertEqual(
            {
                "description": "Existing label",
                "tags": ["name"],
            },
            context["currentAnnotation"],
        )
        paths = [
            operation["path"]
            for operation in responses[0][1]["draft"]["operations"]
        ]
        self.assertTrue(all(
            path.startswith(
                "/curated/fields/field:label~1with~0escapes/"
            )
            for path in paths
        ))
        self.assertNotIn(
            "/curated/fields/field:label~1with~0escapes",
            paths,
        )

    def test_generation_adds_only_explicitly_requested_data_context(self):
        request = {
            "assetId": "asset:roads",
            "target": {
                "kind": "field",
                "fieldId": "field:label/with~escapes",
            },
            "contextOptions": {
                "sampleRows": True,
                "statistics": True,
            },
        }
        handler, responses = self.handler(
            request,
            ["semantic:inspect", "semantic:generate", "semantic:data"],
        )
        handler._semantic_request = Mock(return_value={"asset": asset()})
        optional_context = {
            "sampleRows": {
                "percent": 5,
                "returnedRows": 1,
                "rows": [{"label": "A sample value"}],
            },
            "statistics": {
                "scope": "field",
                "sampledRows": 20,
                "nonNullCount": 19,
            },
        }
        gemini = Mock()
        gemini.generate.return_value = PROFILE
        control = Mock()

        with (
            patch.object(app, "GEMINI", gemini),
            patch.object(app, "CONTROL", control),
            patch.object(
                app,
                "semantic_generation_optional_context",
                return_value=optional_context,
            ) as context_reader,
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        context_reader.assert_called_once_with(
            asset(),
            request["target"],
            request["contextOptions"],
        )
        context = gemini.generate.call_args.args[0]
        self.assertEqual(optional_context, context["dataContext"])
        self.assertEqual(
            {
                "provider": "gemini",
                "model": app.GEMINI_MODEL,
                "metadataOnly": False,
                "contextOptions": {
                    "sampleRows": True,
                    "statistics": True,
                },
                "proposalCreated": False,
            },
            responses[0][1]["generation"],
        )
        audit = control.audit.call_args.kwargs["details"]
        self.assertTrue(audit["contextOptions"]["sampleRows"])
        self.assertTrue(audit["contextOptions"]["statistics"])
        self.assertNotIn("A sample value", json.dumps(audit))
        self.assertNotIn(
            "metadata-only",
            responses[0][1]["draft"]["explanation"],
        )

    def test_optional_context_reauthorizes_source_binding_and_selected_field(self):
        current = asset()
        current["generated"]["binding"] = {
            "adapter": "postgresql",
            "alias": "MAPP",
            "schema": "leeds",
            "relation": "roads",
        }
        current["id"] = app.source_asset_id("MAPP", "leeds", "roads")
        sources = Mock()
        sources.generation_context.return_value = {
            "statistics": {"scope": "field"}
        }
        options = {"sampleRows": False, "statistics": True}

        with patch.object(app, "SEMANTIC_SOURCES", sources):
            context = app.semantic_generation_optional_context(
                current,
                {
                    "kind": "field",
                    "fieldId": "field:label/with~escapes",
                },
                options,
            )

        self.assertEqual("field", context["statistics"]["scope"])
        call = sources.generation_context.call_args
        self.assertEqual("MAPP", call.args[0])
        self.assertEqual("leeds", call.kwargs["schema"])
        self.assertEqual("roads", call.kwargs["relation"])
        self.assertEqual("field", call.kwargs["target_kind"])
        self.assertEqual("label", call.kwargs["field_name"])
        self.assertFalse(call.kwargs["sample_rows"])
        self.assertTrue(call.kwargs["statistics"])
        self.assertGreaterEqual(call.kwargs["sample_seed"], -1)
        self.assertLessEqual(call.kwargs["sample_seed"], 1)

        current["id"] = "forged-asset"
        with patch.object(app, "SEMANTIC_SOURCES", sources):
            with self.assertRaises(app.GeminiClientError) as raised:
                app.semantic_generation_optional_context(
                    current,
                    {"kind": "table"},
                    {"sampleRows": True, "statistics": False},
                )
        self.assertEqual(
            "semantic.generation_context_invalid",
            raised.exception.code,
        )

    def test_optional_context_uses_runtime_reader_dsn_after_profile_match(self):
        current = asset()
        current["id"] = "b08f4fb6-e4ac-5963-982f-843ee00d21f3"
        derived = Mock()
        derived.connection_string = "postgresql://derived-owner"
        derived.reader_role = "xyz_reader"
        derived.get.return_value = {
            "semanticProfile": {
                "assetId": current["id"],
                "generation": current["generation"],
                "status": "ready",
            }
        }
        expected = {"sampleRows": {"returnedRows": 0, "rows": []}}

        with (
            patch.object(app, "DERIVED", derived),
            patch.object(
                app,
                "postgres_generation_context",
                return_value=expected,
            ) as reader,
        ):
            context = app.semantic_generation_optional_context(
                current,
                {"kind": "table"},
                {"sampleRows": True, "statistics": False},
            )

        self.assertEqual(expected, context)
        derived.get.assert_called_once_with("roads", include_query=False)
        reader.assert_called_once_with(
            "postgresql://derived-owner",
            schema="derived_layers",
            relation="roads",
            fields=current["generated"]["fields"],
            target_kind="table",
            field_name=None,
            sample_rows=True,
            statistics=False,
            sample_seed=app._semantic_generation_sample_seed(current),
        )

    def test_optional_context_rejects_non_ready_derived_profile(self):
        current = asset()
        current["id"] = "b08f4fb6-e4ac-5963-982f-843ee00d21f3"
        derived = Mock()
        derived.connection_string = "postgresql://derived-owner"
        derived.reader_role = "xyz_reader"

        for status in (None, "pending", "failed"):
            with self.subTest(status=status):
                profile = {
                    "assetId": current["id"],
                    "generation": current["generation"],
                }
                if status is not None:
                    profile["status"] = status
                derived.get.return_value = {"semanticProfile": profile}
                with (
                    patch.object(app, "DERIVED", derived),
                    patch.object(
                        app,
                        "postgres_generation_context",
                    ) as reader,
                    self.assertRaises(app.GeminiClientError) as raised,
                ):
                    app.semantic_generation_optional_context(
                        current,
                        {"kind": "table"},
                        {"sampleRows": True, "statistics": False},
                    )

                self.assertEqual(
                    "semantic.generation_context_invalid",
                    raised.exception.code,
                )
                self.assertEqual(HTTPStatus.CONFLICT, raised.exception.status)
                reader.assert_not_called()

    def test_optional_context_rejects_stale_derived_profile_generation(self):
        derived = Mock()
        derived.connection_string = "postgresql://derived-owner"
        derived.reader_role = "xyz_reader"

        for asset_generation, profile_generation in (
            (None, 3),
            (True, True),
            (0, 0),
            (3, None),
            (3, 2),
        ):
            with self.subTest(
                asset_generation=asset_generation,
                profile_generation=profile_generation,
            ):
                current = asset()
                current["id"] = (
                    "b08f4fb6-e4ac-5963-982f-843ee00d21f3"
                )
                if asset_generation is None:
                    current.pop("generation")
                else:
                    current["generation"] = asset_generation
                profile = {
                    "assetId": current["id"],
                    "status": "ready",
                }
                if profile_generation is not None:
                    profile["generation"] = profile_generation
                derived.get.return_value = {"semanticProfile": profile}
                with (
                    patch.object(app, "DERIVED", derived),
                    patch.object(
                        app,
                        "postgres_generation_context",
                    ) as reader,
                    self.assertRaises(app.GeminiClientError) as raised,
                ):
                    app.semantic_generation_optional_context(
                        current,
                        {"kind": "table"},
                        {"sampleRows": True, "statistics": False},
                    )

                self.assertEqual(
                    "semantic.generation_context_invalid",
                    raised.exception.code,
                )
                self.assertEqual(HTTPStatus.CONFLICT, raised.exception.status)
                reader.assert_not_called()

    def test_request_is_closed_and_missing_field_never_calls_gemini(self):
        invalid_requests = (
            {},
            {"assetId": "asset:roads", "target": {"kind": "row"}},
            {
                "assetId": "asset:roads",
                "target": {"kind": "table", "fieldId": "field:id"},
            },
            {
                "assetId": "asset:roads",
                "target": {"kind": "field"},
            },
            {
                "assetId": "asset:roads",
                "target": {"kind": "table"},
                "extra": True,
            },
            {
                "assetId": "asset:roads",
                "target": {"kind": "table"},
                "contextOptions": {"sampleRows": "yes"},
            },
            {
                "assetId": "asset:roads",
                "target": {"kind": "table"},
                "contextOptions": {
                    "sampleRows": False,
                    "statistics": False,
                    "unexpected": True,
                },
            },
        )
        for payload in invalid_requests:
            with self.subTest(payload=payload):
                handler, responses = self.handler(payload)
                handler._semantic_request = Mock()
                with patch.object(app, "GEMINI", Mock()), patch.object(
                    app, "CONTROL", Mock()
                ):
                    handler.do_POST()
                self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
                self.assertEqual(
                    "semantic.generation_invalid_request",
                    responses[0][1]["code"],
                )
                handler._semantic_request.assert_not_called()

        handler, responses = self.handler({
            "assetId": "asset:roads",
            "target": {"kind": "field", "fieldId": "field:missing"},
        })
        handler._semantic_request = Mock(return_value={"asset": asset()})
        gemini = Mock()
        with patch.object(app, "GEMINI", gemini), patch.object(
            app, "CONTROL", Mock()
        ):
            handler.do_POST()
        self.assertEqual(HTTPStatus.NOT_FOUND, responses[0][0])
        self.assertEqual("semantic.field_not_found", responses[0][1]["code"])
        gemini.generate.assert_not_called()

        malformed = asset()
        malformed["curated"]["fields"] = []
        handler, responses = self.handler({
            "assetId": "asset:roads",
            "target": {"kind": "field", "fieldId": "field:id"},
        })
        handler._semantic_request = Mock(return_value={"asset": malformed})
        with patch.object(app, "GEMINI", gemini), patch.object(
            app, "CONTROL", Mock()
        ):
            handler.do_POST()
        self.assertEqual(HTTPStatus.BAD_GATEWAY, responses[0][0])
        self.assertEqual(
            "semantic.generation_context_invalid",
            responses[0][1]["code"],
        )
        gemini.generate.assert_not_called()

    def test_missing_configuration_and_archived_assets_do_not_generate(self):
        handler, responses = self.handler({
            "assetId": "asset:roads",
            "target": {"kind": "table"},
        })
        handler._semantic_request = Mock()
        with patch.object(app, "GEMINI", None), patch.object(
            app, "GEMINI_CONFIGURATION_ERROR", None
        ), patch.object(app, "CONTROL", Mock()):
            handler.do_POST()
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, responses[0][0])
        self.assertEqual(
            "semantic.generation_not_configured",
            responses[0][1]["code"],
        )
        handler._semantic_request.assert_not_called()

        handler, responses = self.handler({
            "assetId": "asset:roads",
            "target": {"kind": "table"},
        })
        handler._semantic_request = Mock(
            return_value={"asset": asset(status="archived")}
        )
        gemini = Mock()
        with patch.object(app, "GEMINI", gemini), patch.object(
            app, "CONTROL", Mock()
        ):
            handler.do_POST()
        self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
        self.assertEqual("semantic.asset_archived", responses[0][1]["code"])
        gemini.generate.assert_not_called()

    def test_both_generate_and_inspect_scopes_are_required(self):
        def auth_handler(scopes):
            handler, responses = self.handler(
                {
                    "assetId": "asset:roads",
                    "target": {"kind": "table"},
                },
                scopes,
            )
            handler._authorized = MethodType(app.Handler._authorized, handler)

            def actor(*, state_change=False):
                handler._authentication = {"scopes": scopes}
                return "token:test"

            handler._actor = actor
            handler._semantic_request = Mock()
            return handler, responses

        inspect_only, responses = auth_handler(["semantic:inspect"])
        inspect_only.do_POST()
        self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
        self.assertEqual(
            "semantic:generate",
            responses[0][1]["requiredScope"],
        )

        generate_only, responses = auth_handler(["semantic:generate"])
        generate_only.do_POST()
        self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
        self.assertEqual(
            "semantic:inspect",
            responses[0][1]["requiredScope"],
        )

    def test_data_context_requires_the_additive_data_scope(self):
        handler, responses = self.handler(
            {
                "assetId": "asset:roads",
                "target": {"kind": "table"},
                "contextOptions": {
                    "sampleRows": False,
                    "statistics": True,
                },
            },
            ["semantic:inspect", "semantic:generate"],
        )
        handler._semantic_request = Mock()
        gemini = Mock()
        with patch.object(app, "GEMINI", gemini), patch.object(
            app, "CONTROL", Mock()
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
        self.assertEqual("semantic:data", responses[0][1]["requiredScope"])
        handler._semantic_request.assert_not_called()
        gemini.generate.assert_not_called()

    def test_generation_scope_matrix_has_no_implicit_grants(self):
        request = {
            "assetId": "asset:roads",
            "target": {"kind": "table"},
        }
        cases = [
            ([scope], "token:test", (
                None
                if scope == "full"
                else (
                    "semantic:inspect"
                    if scope == "semantic:generate"
                    else "semantic:generate"
                )
            ))
            for scope in sorted(TOKEN_SCOPES)
        ]
        cases.extend([
            (
                ["semantic:inspect", "semantic:generate"],
                "token:test",
                None,
            ),
            (
                [
                    "semantic:inspect",
                    "semantic:generate",
                    "semantic:admin",
                ],
                "token:test",
                None,
            ),
            (
                ["semantic:generate", "semantic:admin"],
                "token:test",
                "semantic:inspect",
            ),
            (
                ["semantic:inspect", "semantic:admin"],
                "token:test",
                "semantic:generate",
            ),
            (
                [
                    "semantic:propose",
                    "semantic:apply",
                    "semantic:admin",
                ],
                "token:test",
                "semantic:generate",
            ),
            ([], "admin", None),
        ])

        for scopes, actor, required_scope in cases:
            with self.subTest(scopes=scopes, actor=actor):
                handler, responses = self.handler(request, scopes)
                handler._authorized = MethodType(
                    app.Handler._authorized,
                    handler,
                )

                def authenticate(*, state_change=False):
                    handler._authentication = {"scopes": scopes}
                    return actor

                handler._actor = authenticate
                handler._semantic_request = Mock(
                    return_value={"asset": asset()}
                )
                gemini = Mock()
                gemini.generate.return_value = PROFILE
                with patch.object(app, "GEMINI", gemini), patch.object(
                    app, "CONTROL", Mock()
                ):
                    handler.do_POST()

                if required_scope is None:
                    self.assertEqual(HTTPStatus.OK, responses[0][0])
                    handler._semantic_request.assert_called_once()
                    gemini.generate.assert_called_once()
                else:
                    self.assertEqual(
                        HTTPStatus.FORBIDDEN,
                        responses[0][0],
                    )
                    self.assertEqual(
                        required_scope,
                        responses[0][1]["requiredScope"],
                    )
                    handler._semantic_request.assert_not_called()
                    gemini.generate.assert_not_called()

    def test_hidden_assets_require_admin_visibility_without_identity_leak(self):
        request = {
            "assetId": "asset:roads",
            "target": {"kind": "table"},
        }

        def private_request(
            path,
            *,
            method="GET",
            payload=None,
            actor,
            scopes,
        ):
            if "semantic:admin" not in scopes:
                raise app.SemanticClientError(
                    "not found",
                    status=HTTPStatus.NOT_FOUND,
                    payload={
                        "error": {
                            "code": "asset_not_found",
                            "message": "Semantic asset was not found.",
                        }
                    },
                )
            return {"asset": asset(visibility="admin")}

        semantic = Mock()
        semantic.request.side_effect = private_request
        gemini = Mock()
        gemini.generate.return_value = PROFILE

        handler, responses = self.handler(
            request,
            ["semantic:inspect", "semantic:generate"],
        )
        with patch.object(app, "SEMANTIC", semantic), patch.object(
            app, "GEMINI", gemini
        ), patch.object(app, "CONTROL", Mock()):
            handler.do_POST()
        self.assertEqual(HTTPStatus.NOT_FOUND, responses[0][0])
        self.assertEqual("semantic.asset_not_found", responses[0][1]["code"])
        gemini.generate.assert_not_called()

        handler, responses = self.handler(
            request,
            ["semantic:inspect", "semantic:generate", "semantic:admin"],
        )
        with patch.object(app, "SEMANTIC", semantic), patch.object(
            app, "GEMINI", gemini
        ), patch.object(app, "CONTROL", Mock()):
            handler.do_POST()
        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertIn(
            "semantic:admin",
            semantic.request.call_args.kwargs["scopes"],
        )

        for scopes, actor in ((["full"], "token:legacy"), ([], "admin")):
            with self.subTest(scopes=scopes, actor=actor):
                semantic = Mock()
                semantic.request.side_effect = private_request
                handler, responses = self.handler(request, scopes)
                handler._authorized = (
                    lambda state_change=False, actor=actor: actor
                )
                with patch.object(app, "SEMANTIC", semantic), patch.object(
                    app, "GEMINI", gemini
                ), patch.object(app, "CONTROL", Mock()):
                    handler.do_POST()
                self.assertEqual(HTTPStatus.OK, responses[0][0])
                self.assertIn(
                    "semantic:admin",
                    semantic.request.call_args.kwargs["scopes"],
                )

    def test_upstream_generation_failure_is_sanitized_and_audited(self):
        handler, responses = self.handler({
            "assetId": "asset:roads",
            "target": {"kind": "table"},
        })
        handler._semantic_request = Mock(return_value={"asset": asset()})
        gemini = Mock()
        gemini.generate.side_effect = app.GeminiClientError(
            "Gemini semantic generation failed."
        )
        control = Mock()
        with patch.object(app, "GEMINI", gemini), patch.object(
            app, "CONTROL", control
        ):
            handler.do_POST()
        self.assertEqual(HTTPStatus.BAD_GATEWAY, responses[0][0])
        self.assertEqual("semantic.generation_failed", responses[0][1]["code"])
        self.assertEqual(
            "semantic.draft.generation_failed",
            control.audit.call_args.args[0],
        )
        self.assertNotIn("currentAnnotation", json.dumps(
            control.audit.call_args.kwargs["details"]
        ))

    def test_asset_lookup_failure_is_sanitized_and_audited_without_egress(self):
        handler, responses = self.handler({
            "assetId": "asset:missing",
            "target": {"kind": "table"},
        })
        handler._semantic_request = Mock(side_effect=app.SemanticClientError(
            "private upstream detail",
            status=HTTPStatus.NOT_FOUND,
            payload={
                "error": {
                    "code": "asset_not_found",
                    "message": "Semantic asset was not found.",
                },
            },
        ))
        gemini = Mock()
        control = Mock()
        with patch.object(app, "GEMINI", gemini), patch.object(
            app, "CONTROL", control
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.NOT_FOUND, responses[0][0])
        self.assertEqual("semantic.asset_not_found", responses[0][1]["code"])
        gemini.generate.assert_not_called()
        control.audit.assert_called_once_with(
            "semantic.draft.generation_failed",
            actor="token:test",
            remote="127.0.0.1",
            details={
                "assetId": "asset:missing",
                "target": "table",
                "provider": "gemini",
                "model": app.GEMINI_MODEL,
                "contextOptions": {
                    "sampleRows": False,
                    "statistics": False,
                },
                "code": "semantic.asset_lookup_failed",
                "status": HTTPStatus.NOT_FOUND,
            },
        )
        self.assertNotIn(
            "private upstream detail",
            json.dumps(control.audit.call_args.kwargs["details"]),
        )

    def test_exact_existing_annotation_returns_no_change(self):
        current = asset()
        current["curated"].update(PROFILE)
        handler, responses = self.handler({
            "assetId": "asset:roads",
            "target": {"kind": "table"},
        })
        handler._semantic_request = Mock(return_value={"asset": current})
        gemini = Mock()
        gemini.generate.return_value = PROFILE
        with patch.object(app, "GEMINI", gemini), patch.object(
            app, "CONTROL", Mock()
        ):
            handler.do_POST()
        self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
        self.assertEqual(
            "semantic.generation_no_change",
            responses[0][1]["code"],
        )

    def test_status_advertises_generation_without_exposing_key(self):
        handler, responses = self.handler({})
        handler.path = "/api/semantic/status"
        handler._semantic_request = Mock(return_value={
            "catalogRevision": 3,
            "capabilities": {"catalog": True},
        })
        gemini = Mock()
        with patch.object(app, "GEMINI", gemini):
            handler.do_GET()
        self.assertEqual(HTTPStatus.OK, responses[0][0])
        capability = responses[0][1]["capabilities"]["generation"]
        self.assertEqual({
            "available": True,
            "provider": "gemini",
            "model": app.GEMINI_MODEL,
            "targets": ["table", "field"],
            "metadataOnly": True,
            "contextOptions": {
                "sampleRows": {
                    "available": True,
                    "percent": 5,
                    "maxRows": 100,
                    "maxBytes": 98304,
                    "maxColumns": 20,
                    "maxValueCharacters": 512,
                    "requiredScope": "semantic:data",
                },
                "statistics": {
                    "available": True,
                    "fieldSamplePercent": 5,
                    "fieldMaxSampledRows": 1000,
                    "requiredScope": "semantic:data",
                },
            },
        }, capability)
        self.assertNotIn("api", json.dumps(capability).lower())


if __name__ == "__main__":
    unittest.main()
