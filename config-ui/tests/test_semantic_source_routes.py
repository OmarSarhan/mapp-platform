from __future__ import annotations

import unittest
from contextlib import contextmanager
from http import HTTPStatus
from unittest.mock import Mock, patch

import app
from semantic_sources import SemanticSourceError, source_generated


SOURCE = {
    "alias": "MAPP",
    "schema": "leeds",
    "relation": "census_2021_england_oa",
    "kind": "table",
    "assetId": "8cabd3f8-9668-56de-b920-cf01d0256718",
    "description": "Official 2021 Census OA variables.",
    "fields": [{
        "name": "oa21cd",
        "type": "text",
        "nullable": False,
        "primaryKey": True,
        "unique": True,
        "description": "2021 Output Area code",
    }],
}


class FakeSources:
    def __init__(self, source=SOURCE, *, error=None):
        self.source = source
        self.error = error
        self.discover = Mock(return_value=[{
            key: source[key]
            for key in ("alias", "schema", "relation", "kind", "assetId")
        }])
        self.locked_calls = []

    @contextmanager
    def locked_relation(self, alias, schema, relation):
        self.locked_calls.append((alias, schema, relation))
        if self.error:
            raise self.error
        yield self.source


class SemanticSourceRouteTests(unittest.TestCase):
    @staticmethod
    def handler(path, *, method="GET", scopes=None, actor="token:test", payload=None):
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authentication = {"scopes": list(scopes or [])}
        handler._actor = lambda state_change=False: actor
        handler._payload = lambda: payload
        handler._remote = lambda: "127.0.0.1"
        handler._json = lambda status, body: responses.append((status, body))
        handler.send_error = lambda status: responses.append((status, {}))
        return handler, responses

    def test_source_discovery_requires_inspect_and_source(self):
        cases = (
            (["semantic:inspect"], "semantic:source"),
            (["semantic:source"], "semantic:inspect"),
            (["semantic:generate"], "semantic:inspect"),
        )
        for scopes, required in cases:
            with self.subTest(scopes=scopes):
                handler, responses = self.handler(
                    "/api/semantic/source/relations",
                    scopes=scopes,
                )
                with patch.object(app, "SEMANTIC_SOURCES", FakeSources()):
                    handler.do_GET()
                self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
                self.assertEqual(required, responses[0][1]["requiredScope"])

        sources = FakeSources()
        handler, responses = self.handler(
            "/api/semantic/source/relations",
            scopes=["semantic:inspect", "semantic:source"],
        )
        with patch.object(app, "SEMANTIC_SOURCES", sources):
            handler.do_GET()
        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual(SOURCE["assetId"], responses[0][1]["relations"][0]["assetId"])
        sources.discover.assert_called_once_with()

    def test_admin_and_full_authority_include_source_scope(self):
        for actor, scopes in (
            ("admin", []),
            ("token:full", ["full"]),
        ):
            with self.subTest(actor=actor):
                handler, responses = self.handler(
                    "/api/semantic/source/relations",
                    actor=actor,
                    scopes=scopes,
                )
                with patch.object(app, "SEMANTIC_SOURCES", FakeSources()):
                    handler.do_GET()
                self.assertEqual(HTTPStatus.OK, responses[0][0])
                self.assertIn(
                    "semantic:source",
                    handler._semantic_scopes(actor),
                )

    def test_source_sync_registers_metadata_through_trusted_event(self):
        handler, responses = self.handler(
            "/api/semantic/source/sync",
            method="POST",
            scopes=["semantic:inspect", "semantic:source"],
            payload={
                "alias": "MAPP",
                "schema": "leeds",
                "relation": "census_2021_england_oa",
            },
        )
        handler._semantic_request = Mock(side_effect=app.SemanticClientError(
            "not found",
            status=HTTPStatus.NOT_FOUND,
        ))
        generated = source_generated(SOURCE)
        asset = {
            "id": SOURCE["assetId"],
            "generation": 1,
            "version": 1,
            "status": "ready",
            "catalogRevision": 9,
            "generated": {
                **generated,
                "fields": [{
                    **generated["fields"][0],
                    "id": "field:oa21cd",
                }],
            },
            "curated": {},
        }
        semantic = Mock()
        semantic.request.return_value = {
            "catalogRevision": 9,
            "event": {
                "eventId": "filled-from-request",
                "payloadHash": "filled-from-request",
                "idempotent": False,
            },
            "asset": asset,
        }

        def acknowledge(_path, *, method, payload, actor, scopes):
            response = semantic.request.return_value
            response["event"]["eventId"] = payload["eventId"]
            response["event"]["payloadHash"] = payload["payloadHash"]
            return response

        semantic.request.side_effect = acknowledge
        sources = FakeSources()
        control = Mock()
        with patch.object(app, "SEMANTIC", semantic), patch.object(
            app, "SEMANTIC_SOURCES", sources
        ), patch.object(app, "CONTROL", control):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.CREATED, status)
        self.assertEqual("register", body["operation"])
        self.assertEqual(9, body["catalogRevision"])
        self.assertEqual(SOURCE["assetId"], body["asset"]["id"])
        call = semantic.request.call_args
        self.assertEqual("/v1/events", call.args[0])
        self.assertEqual(["semantic:admin"], call.kwargs["scopes"])
        event = call.kwargs["payload"]
        self.assertEqual(generated, event["generated"])
        self.assertNotIn("query", event["generated"])
        self.assertNotIn("default", event["generated"]["fields"][0])
        control.audit.assert_called_once()

    def test_unchanged_sync_is_a_revision_preserving_noop(self):
        generated = source_generated(SOURCE)
        existing = {
            "id": SOURCE["assetId"],
            "generation": 4,
            "version": 6,
            "status": "ready",
            "catalogRevision": 17,
            "generated": {
                **generated,
                "fields": [{
                    **generated["fields"][0],
                    "id": "field:oa21cd",
                }],
            },
            "curated": {},
        }
        handler, responses = self.handler(
            "/api/semantic/source/sync",
            method="POST",
            scopes=["semantic:inspect", "semantic:source"],
            payload={
                "alias": "MAPP",
                "schema": "leeds",
                "relation": "census_2021_england_oa",
            },
        )
        handler._semantic_request = Mock(return_value={
            "catalogRevision": 23,
            "asset": existing,
        })
        semantic = Mock()
        control = Mock()
        with patch.object(app, "SEMANTIC", semantic), patch.object(
            app, "SEMANTIC_SOURCES", FakeSources()
        ), patch.object(app, "CONTROL", control):
            handler.do_POST()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual("unchanged", responses[0][1]["operation"])
        self.assertEqual(23, responses[0][1]["catalogRevision"])
        self.assertEqual(4, responses[0][1]["asset"]["generation"])
        semantic.request.assert_not_called()

    def test_source_sync_scope_and_closed_payload_fail_before_introspection(self):
        for scopes, required in (
            (["semantic:source"], "semantic:inspect"),
            (["semantic:inspect"], "semantic:source"),
        ):
            with self.subTest(scopes=scopes):
                handler, responses = self.handler(
                    "/api/semantic/source/sync",
                    method="POST",
                    scopes=scopes,
                    payload={
                        "alias": "MAPP",
                        "schema": "leeds",
                        "relation": "roads",
                    },
                )
                sources = FakeSources()
                with patch.object(app, "SEMANTIC_SOURCES", sources):
                    handler.do_POST()
                self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
                self.assertEqual(required, responses[0][1]["requiredScope"])
                self.assertEqual([], sources.locked_calls)

        handler, responses = self.handler(
            "/api/semantic/source/sync",
            method="POST",
            scopes=["semantic:inspect", "semantic:source"],
            payload={
                "alias": "MAPP",
                "schema": "leeds",
                "relation": "roads",
                "sampleRows": True,
            },
        )
        sources = FakeSources()
        with patch.object(app, "SEMANTIC_SOURCES", sources):
            handler.do_POST()
        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        self.assertEqual([], sources.locked_calls)

    def test_privilege_loss_is_redacted_and_does_not_reach_service(self):
        handler, responses = self.handler(
            "/api/semantic/source/sync",
            method="POST",
            scopes=["semantic:inspect", "semantic:source"],
            payload={
                "alias": "MAPP",
                "schema": "leeds",
                "relation": "roads",
            },
        )
        semantic = Mock()
        control = Mock()
        sources = FakeSources(error=SemanticSourceError(
            "The semantic source was not found or is not selectable.",
            status=HTTPStatus.NOT_FOUND,
            code="semantic.source_not_found",
        ))
        with patch.object(app, "SEMANTIC", semantic), patch.object(
            app, "SEMANTIC_SOURCES", sources
        ), patch.object(
            app, "CONTROL", control
        ):
            handler.do_POST()
        self.assertEqual(HTTPStatus.NOT_FOUND, responses[0][0])
        self.assertEqual("semantic.source_not_found", responses[0][1]["code"])
        semantic.request.assert_not_called()
        self.assertNotIn(
            "postgresql://",
            str(control.audit.call_args),
        )
