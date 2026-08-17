from __future__ import annotations

import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from semantic_store import SemanticStore  # noqa: E402
from server import SemanticHTTPServer  # noqa: E402


class SemanticServerTest(unittest.TestCase):
    TOKEN = "test-internal-token"
    CALLER_TOKEN_SCOPES = (
        "full",
        "inspect",
        "propose",
        "visual",
        "apply",
        "reload",
        "derive",
        "semantic:inspect",
        "semantic:generate",
        "semantic:data",
        "semantic:propose",
        "semantic:apply",
        "semantic:admin",
    )

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        store = SemanticStore(
            Path(self.temporary_directory.name) / "semantic.sqlite3"
        )
        self.store = store
        self.server = SemanticHTTPServer(
            ("127.0.0.1", 0),
            store,
            self.TOKEN,
            max_body_bytes=1024,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: object | bytes | None = None,
        *,
        token: bool = True,
        scopes: str | None = "semantic:inspect",
        actor: str = "tester",
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {self.TOKEN}"
        if scopes is not None:
            headers["X-MAPP-Scopes"] = scopes
        headers["X-MAPP-Actor"] = actor
        if body is not None:
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(payload))
        else:
            payload = None
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)

    @staticmethod
    def event(
        *,
        event_id: str = "event-1",
        asset_id: str = "asset:derived/roads",
        visibility: str | None = None,
    ) -> dict:
        value = {
            "eventId": event_id,
            "assetId": asset_id,
            "type": "register",
            "generation": 1,
            "generated": {
                "kind": "managed-derived",
                "name": "roads",
                "binding": {"schema": "derived_layers", "relation": "roads"},
                "fields": [{"name": "id", "type": "integer"}],
            },
        }
        if visibility:
            value["visibility"] = visibility
        return value

    def test_health_is_public_but_every_v1_route_requires_token(self) -> None:
        status, body = self.request("GET", "/healthz", token=False, scopes=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})

        status, body = self.request(
            "GET", "/v1/status", token=False, scopes=None
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

        status, body = self.request("GET", "/v1/status", scopes=None)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

        status, body = self.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["schemaVersion"], 5)
        self.assertEqual(body["serviceVersion"], "1.2.0")
        self.assertEqual(
            body["capabilities"]["pagination"],
            {
                "version": "1",
                "defaultLimit": 100,
                "maxLimit": 100,
                "cursor": "opaque",
                "maxResponseBytes": 16 * 1024 * 1024,
                "oversizedItemError": "page_too_large",
                "legacyMaxItems": 100,
                "legacyOverflowError": "pagination_required",
            },
        )

    def test_every_caller_scope_is_isolated_on_every_private_route(self) -> None:
        read_routes = {
            "/v1/status": 200,
            "/v1/catalog": 200,
            "/v1/search?q=missing": 200,
            "/v1/assets/missing": 404,
            "/v1/assets/missing/history": 404,
            "/v1/derived-profiles": 200,
            "/v1/derived-profiles/missing": 404,
            "/v1/proposals": 200,
            "/v1/proposals/missing": 404,
        }
        for path, allowed_status in read_routes.items():
            for scope in self.CALLER_TOKEN_SCOPES:
                with self.subTest(method="GET", path=path, scope=scope):
                    status, body = self.request(
                        "GET",
                        path,
                        scopes=scope,
                    )
                    if scope == "semantic:inspect":
                        self.assertEqual(allowed_status, status)
                    else:
                        self.assertEqual(403, status)
                        self.assertEqual(
                            "forbidden",
                            body["error"]["code"],
                        )

        write_routes = {
            "/v1/events": ("semantic:admin", 400),
            "/v1/proposals/check": ("semantic:propose", 400),
            "/v1/proposals": ("semantic:propose", 400),
            "/v1/proposals/missing/apply": ("semantic:apply", 404),
            "/v1/proposals/missing/decline": ("semantic:propose", 404),
        }
        for path, (required_scope, allowed_status) in write_routes.items():
            for scope in self.CALLER_TOKEN_SCOPES:
                with self.subTest(method="POST", path=path, scope=scope):
                    status, body = self.request(
                        "POST",
                        path,
                        {},
                        scopes=scope,
                    )
                    if scope == required_scope:
                        self.assertEqual(allowed_status, status)
                    else:
                        self.assertEqual(403, status)
                        self.assertEqual(
                            "forbidden",
                            body["error"]["code"],
                        )

    def test_private_route_actions_require_exact_paths(self) -> None:
        cases = (
            ("/v1/events/", "semantic:admin"),
            ("/v1/proposals/check/", "semantic:propose"),
            ("/v1/proposals//apply", "semantic:apply"),
            ("/v1/proposals/proposal-1/apply/", "semantic:apply"),
            ("/v1/proposals/proposal-1/extra/apply", "semantic:apply"),
            ("/v1/proposals/proposal-1/decline/", "semantic:propose"),
            ("/v1/proposals/proposal-1/extra/decline", "semantic:propose"),
        )
        for path, scope in cases:
            with self.subTest(path=path, scope=scope):
                status, body = self.request(
                    "POST",
                    path,
                    {},
                    scopes=scope,
                )
                self.assertEqual(404, status)
                self.assertEqual("not_found", body["error"]["code"])

    def test_event_catalog_search_asset_history_and_derived_alias(self) -> None:
        status, created = self.request(
            "POST", "/v1/events", self.event(), scopes="semantic:admin"
        )
        self.assertEqual(status, 200)
        asset = created["asset"]
        self.assertEqual(asset["id"], "asset:derived/roads")
        self.assertEqual(asset["version"], 1)
        self.assertEqual(asset["status"], "ready")
        self.assertIsInstance(asset["generated"], dict)
        self.assertIsInstance(asset["curated"], dict)

        status, replay = self.request(
            "POST", "/v1/events", self.event(), scopes="semantic:admin"
        )
        self.assertEqual(status, 200)
        self.assertTrue(replay["event"]["idempotent"])

        status, catalog = self.request("GET", "/v1/catalog")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in catalog["assets"]], [asset["id"]])

        status, search = self.request("GET", "/v1/search?q=roads&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(search["results"][0]["id"], asset["id"])
        self.assertEqual(search["results"][0]["version"], 1)

        encoded = quote(asset["id"], safe="")
        status, shown = self.request("GET", f"/v1/assets/{encoded}")
        self.assertEqual(status, 200)
        self.assertEqual(shown["asset"]["id"], asset["id"])

        status, history = self.request(
            "GET", f"/v1/assets/{encoded}/history"
        )
        self.assertEqual(status, 200)
        self.assertEqual(history["history"][0]["eventId"], "event-1")

        status, profiles = self.request("GET", "/v1/derived-profiles")
        self.assertEqual(status, 200)
        self.assertEqual(profiles["derivedProfiles"][0]["id"], asset["id"])
        status, profile = self.request(
            "GET", f"/v1/derived-profiles/{quote('roads', safe='')}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(profile["derivedProfile"]["id"], asset["id"])

    def test_growing_collections_support_bounded_opaque_pages(self) -> None:
        assets = []
        for index in range(3):
            status, created = self.request(
                "POST",
                "/v1/events",
                self.event(
                    event_id=f"event-{index}",
                    asset_id=f"asset:derived/roads-{index}",
                ),
                scopes="semantic:admin",
            )
            self.assertEqual(status, 200)
            assets.append(created["asset"])

        status, first = self.request("GET", "/v1/catalog?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(first["assets"]), 1)
        cursor = first["pagination"]["nextCursor"]
        self.assertRegex(cursor, r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$")

        status, second = self.request(
            "GET", f"/v1/catalog?limit=1&cursor={cursor}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(second["assets"]), 1)
        self.assertNotEqual(first["assets"][0]["id"], second["assets"][0]["id"])

        status, search = self.request("GET", "/v1/search?q=roads&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(search["results"]), 1)
        self.assertRegex(
            search["pagination"]["nextCursor"],
            r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$",
        )

        status, profiles = self.request(
            "GET", "/v1/derived-profiles?limit=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(profiles["derivedProfiles"]), 1)
        self.assertRegex(
            profiles["pagination"]["nextCursor"],
            r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$",
        )

        asset = assets[0]
        status, refreshed = self.request(
            "POST",
            "/v1/events",
            {
                "eventId": "event-refresh",
                "assetId": asset["id"],
                "type": "refresh",
                "generation": 2,
                "generated": asset["generated"],
            },
            scopes="semantic:admin",
        )
        self.assertEqual(status, 200)
        asset = refreshed["asset"]
        encoded_asset = quote(asset["id"], safe="")
        status, history = self.request(
            "GET", f"/v1/assets/{encoded_asset}/history?limit=1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(history["history"]), 1)
        self.assertRegex(
            history["pagination"]["nextCursor"],
            r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$",
        )

        check_request = {
            "assetId": asset["id"],
            "baseVersion": asset["version"],
            "operations": [
                {
                    "op": "set",
                    "path": "/curated/description",
                    "value": "Bounded proposal",
                }
            ],
        }
        for _index in range(2):
            status, checked = self.request(
                "POST",
                "/v1/proposals/check",
                check_request,
                scopes="semantic:propose",
            )
            self.assertEqual(status, 200)
            status, _proposal = self.request(
                "POST",
                "/v1/proposals",
                {
                    **check_request,
                    "fingerprint": checked["check"]["fingerprint"],
                },
                scopes="semantic:propose",
            )
            self.assertEqual(status, 201)
        status, proposals = self.request("GET", "/v1/proposals?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(proposals["proposals"]), 1)
        self.assertRegex(
            proposals["pagination"]["nextCursor"],
            r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$",
        )

        status, invalid = self.request(
            "GET", "/v1/catalog?limit=1&cursor=" + "0" * 64
        )
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], "pagination_invalid")

        status, legacy = self.request("GET", "/v1/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(len(legacy["assets"]), 3)
        self.assertNotIn("pagination", legacy)

    def test_parameterless_collections_fetch_only_overflow_sentinel(self) -> None:
        cases = (
            ("list_assets", "/v1/catalog"),
            ("search_assets", "/v1/search?q=roads"),
            ("derived_profiles", "/v1/derived-profiles"),
            ("list_proposals", "/v1/proposals"),
            ("asset_history", "/v1/assets/asset%3Aroads/history"),
        )
        rows = [{"id": f"item-{index}"} for index in range(101)]
        for method_name, path in cases:
            with self.subTest(path=path), patch.object(
                self.store,
                method_name,
                return_value=rows,
            ) as read_collection:
                status, body = self.request("GET", path)

            self.assertEqual(HTTPStatus.CONFLICT, status)
            self.assertEqual("pagination_required", body["error"]["code"])
            self.assertEqual(101, read_collection.call_args.kwargs["fetch_limit"])

    def test_parameterless_search_retains_legacy_twenty_result_shape(self) -> None:
        rows = [{"id": f"item-{index:03d}"} for index in range(100)]
        with patch.object(
            self.store,
            "search_assets",
            return_value=rows,
        ) as search_assets:
            status, body = self.request("GET", "/v1/search?q=roads")

        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(rows[:20], body["results"])
        self.assertNotIn("pagination", body)
        self.assertIsNone(search_assets.call_args.kwargs["limit"])
        self.assertEqual(101, search_assets.call_args.kwargs["fetch_limit"])

    def test_catalog_page_pushes_keyset_and_limit_into_storage(self) -> None:
        for index in range(3):
            status, _created = self.request(
                "POST",
                "/v1/events",
                self.event(
                    event_id=f"event-storage-{index}",
                    asset_id=f"asset:storage/{index}",
                ),
                scopes="semantic:admin",
            )
            self.assertEqual(status, 200)

        with patch.object(
            self.store,
            "list_assets",
            wraps=self.store.list_assets,
        ) as list_assets:
            status, first = self.request("GET", "/v1/catalog?limit=1")
            self.assertEqual(status, 200)
            status, second = self.request(
                "GET",
                "/v1/catalog?limit=1&cursor="
                + first["pagination"]["nextCursor"],
            )
            self.assertEqual(status, 200)

        self.assertEqual(list_assets.call_count, 2)
        self.assertEqual(list_assets.call_args_list[0].kwargs["fetch_limit"], 2)
        self.assertIsNone(
            list_assets.call_args_list[0].kwargs["after_asset_id"]
        )
        self.assertEqual(list_assets.call_args_list[1].kwargs["fetch_limit"], 2)
        self.assertEqual(
            list_assets.call_args_list[1].kwargs["after_asset_id"],
            first["assets"][0]["id"],
        )
        self.assertNotEqual(first["assets"], second["assets"])

    def test_page_byte_budget_shortens_pages_and_rejects_one_large_item(
        self,
    ) -> None:
        for index in range(2):
            status, _created = self.request(
                "POST",
                "/v1/events",
                self.event(
                    event_id=f"event-budget-{index}",
                    asset_id=f"asset:budget/{index}",
                ),
                scopes="semantic:admin",
            )
            self.assertEqual(status, 200)
        first_asset = self.store.list_assets(is_admin=False)[0]
        item_bytes = len(json.dumps(
            first_asset,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"))

        with patch("server.MAX_PAGE_ITEMS_BYTES", item_bytes + 2):
            status, bounded = self.request("GET", "/v1/catalog?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(bounded["assets"]), 1)
        self.assertIsNotNone(bounded["pagination"]["nextCursor"])
        self.assertEqual(bounded["pagination"]["limit"], 2)

        with patch("server.MAX_PAGE_ITEMS_BYTES", item_bytes + 1):
            status, oversized = self.request("GET", "/v1/catalog?limit=1")
        self.assertEqual(status, 413)
        self.assertEqual(oversized["error"]["code"], "page_too_large")

        with patch("server.MAX_PAGE_ITEMS_BYTES", item_bytes + 2):
            status, legacy_bounded = self.request("GET", "/v1/catalog")
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(
            "pagination_required",
            legacy_bounded["error"]["code"],
        )

        with patch("server.MAX_PAGE_ITEMS_BYTES", item_bytes + 1):
            status, legacy_oversized = self.request("GET", "/v1/catalog")
        self.assertEqual(status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(
            "page_too_large",
            legacy_oversized["error"]["code"],
        )

    def test_cursor_is_invalidated_by_revision_or_visibility_scope(self) -> None:
        for index in range(2):
            status, _created = self.request(
                "POST",
                "/v1/events",
                self.event(
                    event_id=f"event-cursor-{index}",
                    asset_id=f"asset:cursor/{index}",
                ),
                scopes="semantic:admin",
            )
            self.assertEqual(status, 200)
        status, first = self.request("GET", "/v1/catalog?limit=1")
        self.assertEqual(status, 200)
        cursor = first["pagination"]["nextCursor"]

        status, wrong_visibility = self.request(
            "GET",
            f"/v1/catalog?limit=1&cursor={cursor}",
            scopes="semantic:inspect,semantic:admin",
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            wrong_visibility["error"]["code"], "pagination_invalid"
        )

        status, _created = self.request(
            "POST",
            "/v1/events",
            self.event(
                event_id="event-cursor-revision",
                asset_id="asset:cursor/revision",
            ),
            scopes="semantic:admin",
        )
        self.assertEqual(status, 200)
        status, changed = self.request(
            "GET", f"/v1/catalog?limit=1&cursor={cursor}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(changed["error"]["code"], "pagination_invalid")

    def test_encoded_slash_in_asset_id_is_decoded(self) -> None:
        event = self.event(asset_id="asset:derived/area/roads")
        self.request(
            "POST", "/v1/events", event, scopes="semantic:admin"
        )
        encoded = quote(event["assetId"], safe="")
        status, body = self.request("GET", f"/v1/assets/{encoded}")
        self.assertEqual(status, 200)
        self.assertEqual(body["asset"]["id"], event["assetId"])

    def test_admin_assets_are_not_disclosed_without_admin_scope(self) -> None:
        status, created = self.request(
            "POST",
            "/v1/events",
            self.event(visibility="admin"),
            scopes="semantic:admin",
        )
        self.assertEqual(status, 200)
        asset_id = created["asset"]["id"]

        status, catalog = self.request("GET", "/v1/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["assets"], [])
        status, body = self.request(
            "GET", f"/v1/assets/{quote(asset_id, safe='')}"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "asset_not_found")

        status, body = self.request(
            "GET", "/v1/catalog", scopes="semantic:admin"
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

        status, catalog = self.request(
            "GET",
            "/v1/catalog",
            scopes="semantic:inspect,semantic:admin",
        )
        self.assertEqual(status, 200)
        self.assertEqual(catalog["assets"][0]["id"], asset_id)

    def test_archived_assets_are_undiscoverable_but_admin_auditable(self) -> None:
        status, created = self.request(
            "POST",
            "/v1/events",
            self.event(),
            scopes="semantic:admin",
        )
        self.assertEqual(200, status)
        asset_id = created["asset"]["id"]
        encoded_asset_id = quote(asset_id, safe="")

        status, archived = self.request(
            "POST",
            "/v1/events",
            {
                "eventId": "event-archive",
                "assetId": asset_id,
                "type": "archive",
                "generation": 2,
                "visibility": "inspect",
            },
            scopes="semantic:admin",
        )
        self.assertEqual(200, status)
        self.assertEqual("archived", archived["asset"]["status"])
        self.assertEqual("admin", archived["asset"]["visibility"])

        for scopes in (
            "semantic:inspect",
            "semantic:inspect,semantic:admin",
        ):
            with self.subTest(route="catalog", scopes=scopes):
                status, body = self.request(
                    "GET",
                    "/v1/catalog",
                    scopes=scopes,
                )
                self.assertEqual(200, status)
                self.assertEqual([], body["assets"])
            with self.subTest(route="search", scopes=scopes):
                status, body = self.request(
                    "GET",
                    "/v1/search?q=roads",
                    scopes=scopes,
                )
                self.assertEqual(200, status)
                self.assertEqual([], body["results"])
            with self.subTest(route="derived-profiles", scopes=scopes):
                status, body = self.request(
                    "GET",
                    "/v1/derived-profiles",
                    scopes=scopes,
                )
                self.assertEqual(200, status)
                self.assertEqual([], body["derivedProfiles"])

        for suffix in ("", "/history"):
            with self.subTest(route=f"asset{suffix}", access="inspect"):
                status, body = self.request(
                    "GET",
                    f"/v1/assets/{encoded_asset_id}{suffix}",
                    scopes="semantic:inspect",
                )
                self.assertEqual(404, status)
                self.assertEqual("asset_not_found", body["error"]["code"])

        status, shown = self.request(
            "GET",
            f"/v1/assets/{encoded_asset_id}",
            scopes="semantic:inspect,semantic:admin",
        )
        self.assertEqual(200, status)
        self.assertEqual("archived", shown["asset"]["status"])
        status, history = self.request(
            "GET",
            f"/v1/assets/{encoded_asset_id}/history",
            scopes="semantic:inspect,semantic:admin",
        )
        self.assertEqual(200, status)
        self.assertEqual(
            ["event-1", "event-archive"],
            [item["eventId"] for item in history["history"]],
        )

    def test_hidden_asset_reads_and_proposal_actions_need_base_and_admin_scopes(
        self,
    ) -> None:
        status, created = self.request(
            "POST",
            "/v1/events",
            self.event(visibility="admin"),
            scopes="semantic:admin",
        )
        self.assertEqual(200, status)
        asset = created["asset"]
        asset_id = asset["id"]
        encoded_asset_id = quote(asset_id, safe="")

        hidden_reads = (
            ("/v1/catalog", "assets"),
            ("/v1/search?q=roads", "results"),
            ("/v1/derived-profiles", "derivedProfiles"),
            ("/v1/proposals", "proposals"),
        )
        for path, collection in hidden_reads:
            with self.subTest(path=path, access="inspect"):
                status, body = self.request(
                    "GET",
                    path,
                    scopes="semantic:inspect",
                )
                self.assertEqual(200, status)
                self.assertEqual([], body[collection])

        hidden_objects = (
            f"/v1/assets/{encoded_asset_id}",
            f"/v1/assets/{encoded_asset_id}/history",
            f"/v1/derived-profiles/{quote('roads', safe='')}",
        )
        for path in hidden_objects:
            with self.subTest(path=path, access="inspect"):
                status, _body = self.request(
                    "GET",
                    path,
                    scopes="semantic:inspect",
                )
                self.assertEqual(404, status)

        admin_reads = (
            ("/v1/catalog", "assets", asset_id),
            ("/v1/search?q=roads", "results", asset_id),
            ("/v1/derived-profiles", "derivedProfiles", asset_id),
        )
        for path, collection, expected_id in admin_reads:
            with self.subTest(path=path, access="inspect+admin"):
                status, body = self.request(
                    "GET",
                    path,
                    scopes="semantic:inspect,semantic:admin",
                )
                self.assertEqual(200, status)
                self.assertEqual(
                    [expected_id],
                    [item["id"] for item in body[collection]],
                )
        status, shown = self.request(
            "GET",
            f"/v1/assets/{encoded_asset_id}",
            scopes="semantic:inspect,semantic:admin",
        )
        self.assertEqual(200, status)
        self.assertEqual(asset_id, shown["asset"]["id"])
        status, history = self.request(
            "GET",
            f"/v1/assets/{encoded_asset_id}/history",
            scopes="semantic:inspect,semantic:admin",
        )
        self.assertEqual(200, status)
        self.assertEqual(asset_id, history["assetId"])
        status, derived = self.request(
            "GET",
            f"/v1/derived-profiles/{quote('roads', safe='')}",
            scopes="semantic:inspect,semantic:admin",
        )
        self.assertEqual(200, status)
        self.assertEqual(asset_id, derived["derivedProfile"]["id"])

        check_request = {
            "assetId": asset_id,
            "baseVersion": asset["version"],
            "operations": [{
                "op": "set",
                "path": "/curated/description",
                "value": "Hidden roads",
            }],
        }
        status, _body = self.request(
            "POST",
            "/v1/proposals/check",
            check_request,
            scopes="semantic:propose",
        )
        self.assertEqual(404, status)
        status, checked = self.request(
            "POST",
            "/v1/proposals/check",
            check_request,
            scopes="semantic:propose,semantic:admin",
        )
        self.assertEqual(200, status)

        create_request = {
            **check_request,
            "fingerprint": checked["check"]["fingerprint"],
        }
        status, _body = self.request(
            "POST",
            "/v1/proposals",
            create_request,
            scopes="semantic:propose",
        )
        self.assertEqual(404, status)
        status, created_proposal = self.request(
            "POST",
            "/v1/proposals",
            create_request,
            scopes="semantic:propose,semantic:admin",
            actor="hidden-author",
        )
        self.assertEqual(201, status)
        proposal_id = created_proposal["proposal"]["id"]
        encoded_proposal_id = quote(proposal_id, safe="")

        status, proposals = self.request(
            "GET",
            "/v1/proposals",
            scopes="semantic:inspect",
        )
        self.assertEqual(200, status)
        self.assertEqual([], proposals["proposals"])
        status, _body = self.request(
            "GET",
            f"/v1/proposals/{encoded_proposal_id}",
            scopes="semantic:inspect",
        )
        self.assertEqual(404, status)
        status, proposals = self.request(
            "GET",
            "/v1/proposals",
            scopes="semantic:inspect,semantic:admin",
        )
        self.assertEqual(200, status)
        self.assertEqual(
            [proposal_id],
            [item["id"] for item in proposals["proposals"]],
        )
        status, proposal = self.request(
            "GET",
            f"/v1/proposals/{encoded_proposal_id}",
            scopes="semantic:inspect,semantic:admin",
        )
        self.assertEqual(200, status)
        self.assertEqual(proposal_id, proposal["proposal"]["id"])

        status, _body = self.request(
            "POST",
            f"/v1/proposals/{encoded_proposal_id}/apply",
            {},
            scopes="semantic:apply",
        )
        self.assertEqual(404, status)
        status, applied = self.request(
            "POST",
            f"/v1/proposals/{encoded_proposal_id}/apply",
            {},
            scopes="semantic:apply,semantic:admin",
            actor="hidden-approver",
        )
        self.assertEqual(200, status)
        self.assertEqual("applied", applied["proposal"]["state"])

        decline_check = {
            **check_request,
            "baseVersion": applied["asset"]["version"],
            "operations": [{
                "op": "set",
                "path": "/curated/description",
                "value": "Decline this draft",
            }],
        }
        status, checked = self.request(
            "POST",
            "/v1/proposals/check",
            decline_check,
            scopes="semantic:propose,semantic:admin",
        )
        self.assertEqual(200, status)
        status, pending = self.request(
            "POST",
            "/v1/proposals",
            {
                **decline_check,
                "fingerprint": checked["check"]["fingerprint"],
            },
            scopes="semantic:propose,semantic:admin",
        )
        self.assertEqual(201, status)
        decline_id = quote(pending["proposal"]["id"], safe="")
        status, _body = self.request(
            "POST",
            f"/v1/proposals/{decline_id}/decline",
            {},
            scopes="semantic:propose",
        )
        self.assertEqual(404, status)
        status, declined = self.request(
            "POST",
            f"/v1/proposals/{decline_id}/decline",
            {},
            scopes="semantic:propose,semantic:admin",
        )
        self.assertEqual(200, status)
        self.assertEqual("declined", declined["proposal"]["state"])

    def test_read_and_proposal_scopes_are_enforced(self) -> None:
        status, body = self.request(
            "POST", "/v1/events", self.event(), scopes=None
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")
        _, created = self.request(
            "POST",
            "/v1/events",
            self.event(),
            scopes="semantic:admin",
        )
        asset = created["asset"]
        status, body = self.request("GET", "/v1/catalog", scopes=None)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

        check_body = {
            "assetId": asset["id"],
            "baseVersion": 1,
            "explanation": "Add a concise operator-facing description.",
            "operations": [
                {
                    "op": "set",
                    "path": "/curated",
                    "value": {"description": "Roads"},
                }
            ],
        }
        status, body = self.request(
            "POST",
            "/v1/proposals/check",
            check_body,
            scopes="semantic:inspect",
        )
        self.assertEqual(status, 403)
        status, body = self.request(
            "POST",
            "/v1/proposals/check",
            check_body,
            scopes="semantic:admin",
        )
        self.assertEqual(status, 403)
        status, body = self.request(
            "POST",
            "/v1/proposals/check",
            check_body,
            scopes="semantic:propose",
        )
        self.assertEqual(status, 200)
        check = body["check"]
        self.assertEqual(check["operations"], check_body["operations"])
        self.assertIn("diff", check)
        self.assertEqual(len(check["fingerprint"]), 64)

        status, body = self.request(
            "POST",
            "/v1/proposals",
            {**check_body, "fingerprint": check["fingerprint"]},
            scopes="semantic:propose",
            actor="author",
        )
        self.assertEqual(status, 201)
        proposal = body["proposal"]
        self.assertEqual(proposal["operations"], check_body["operations"])
        self.assertEqual(proposal["diff"], check["diff"])
        self.assertEqual(proposal["explanation"], check_body["explanation"])

        proposal_path = quote(proposal["id"], safe="")
        status, _ = self.request(
            "POST",
            f"/v1/proposals/{proposal_path}/apply",
            {},
            scopes="semantic:propose",
        )
        self.assertEqual(status, 403)
        status, applied = self.request(
            "POST",
            f"/v1/proposals/{proposal_path}/apply",
            {},
            scopes="semantic:apply",
            actor="approver",
        )
        self.assertEqual(status, 200)
        self.assertEqual(applied["proposal"]["state"], "applied")
        self.assertEqual(applied["proposal"]["decidedBy"], "approver")
        self.assertEqual(
            applied["proposal"]["decidedAt"],
            applied["proposal"]["updatedAt"],
        )
        self.assertEqual(
            applied["asset"]["curated"], {"description": "Roads"}
        )

    def test_decline_and_proposal_filters(self) -> None:
        _, created = self.request(
            "POST", "/v1/events", self.event(), scopes="semantic:admin"
        )
        asset = created["asset"]
        check_request = {
            "assetId": asset["id"],
            "baseVersion": 1,
            "operations": [
                {"op": "set", "path": "/curated/description", "value": "Draft"}
            ],
        }
        _, checked = self.request(
            "POST",
            "/v1/proposals/check",
            check_request,
            scopes="semantic:propose",
        )
        _, created_proposal = self.request(
            "POST",
            "/v1/proposals",
            {
                **check_request,
                "fingerprint": checked["check"]["fingerprint"],
            },
            scopes="semantic:propose",
        )
        proposal = created_proposal["proposal"]
        encoded = quote(proposal["id"], safe="")
        status, declined = self.request(
            "POST",
            f"/v1/proposals/{encoded}/decline",
            {"reason": "Superseded"},
            scopes="semantic:propose",
            actor="reviewer",
        )
        self.assertEqual(status, 200)
        self.assertEqual(declined["proposal"]["state"], "declined")
        self.assertEqual(declined["proposal"]["decidedBy"], "reviewer")
        self.assertEqual(
            declined["proposal"]["decidedAt"],
            declined["proposal"]["updatedAt"],
        )
        status, proposals = self.request(
            "GET",
            f"/v1/proposals?state=declined&assetId={quote(asset['id'], safe='')}",
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in proposals["proposals"]], [proposal["id"]])

    def test_strict_json_media_type_and_body_bound(self) -> None:
        status, body = self.request(
            "POST",
            "/v1/events",
            b'{"eventId":"one","eventId":"two"}',
            scopes=None,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        status, body = self.request(
            "POST",
            "/v1/events",
            b'{"eventId":NaN}',
            scopes=None,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")

        status, body = self.request(
            "POST",
            "/v1/events",
            b"{}",
            scopes=None,
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["error"]["code"], "unsupported_media_type")

        status, body = self.request(
            "POST",
            "/v1/events",
            b'{"padding":"' + b"x" * 1100 + b'"}',
            scopes=None,
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "body_too_large")

    def test_payload_hash_is_verified(self) -> None:
        event = self.event()
        event["payloadHash"] = "0" * 64
        status, body = self.request(
            "POST", "/v1/events", event, scopes="semantic:admin"
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "payload_hash_mismatch")
        self.assertEqual(len(body["error"]["details"]["computedPayloadHash"]), 64)

        event = self.event()
        event["actor"] = "tester"
        event["payloadHash"] = hashlib.sha256(
            json.dumps(
                event,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        status, body = self.request(
            "POST", "/v1/events", event, scopes="semantic:admin"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["event"]["payloadHash"], event["payloadHash"])

    def test_catalog_payload_and_revision_share_one_snapshot(self) -> None:
        first = self.store.apply_event(self.event())["asset"]
        original = self.store.list_assets

        def list_then_write(
            *,
            is_admin: bool,
            connection=None,
            after_asset_id=None,
            fetch_limit=None,
        ):
            assets = original(
                is_admin=is_admin,
                connection=connection,
                after_asset_id=after_asset_id,
                fetch_limit=fetch_limit,
            )
            self.store.apply_event(
                self.event(
                    event_id="event-2",
                    asset_id="asset:derived/rail",
                )
            )
            return assets

        with patch.object(
            self.store,
            "list_assets",
            side_effect=list_then_write,
        ):
            status, catalog = self.request("GET", "/v1/catalog")

        self.assertEqual(status, 200)
        self.assertEqual(catalog["catalogRevision"], 1)
        self.assertEqual(
            [asset["id"] for asset in catalog["assets"]],
            [first["id"]],
        )
        self.assertEqual(self.store.catalog_revision(), 2)
        self.assertEqual(len(self.store.list_assets(is_admin=False)), 2)

    def test_search_payload_and_revision_share_one_snapshot(self) -> None:
        asset = self.store.apply_event(self.event())["asset"]
        original = self.store.search_assets

        def search_then_write(
            query: str,
            *,
            limit: int | None,
            is_admin: bool,
            connection=None,
            after_asset_id: str | None = None,
            fetch_limit: int | None = None,
        ):
            results = original(
                query,
                limit=limit,
                is_admin=is_admin,
                connection=connection,
                after_asset_id=after_asset_id,
                fetch_limit=fetch_limit,
            )
            self.store.apply_event(
                {
                    "eventId": "event-refresh",
                    "assetId": asset["id"],
                    "type": "refresh",
                    "generation": 2,
                    "generated": asset["generated"],
                }
            )
            return results

        with patch.object(
            self.store,
            "search_assets",
            side_effect=search_then_write,
        ):
            status, search = self.request("GET", "/v1/search?q=roads")

        self.assertEqual(status, 200)
        self.assertEqual(search["catalogRevision"], 1)
        self.assertEqual(search["results"][0]["version"], 1)
        self.assertEqual(self.store.catalog_revision(), 2)
        self.assertEqual(
            self.store.get_asset(asset["id"], is_admin=False)["version"],
            2,
        )

    def test_asset_payload_and_revision_share_one_snapshot(self) -> None:
        asset = self.store.apply_event(self.event())["asset"]
        original = self.store.get_asset

        def asset_then_write(
            asset_id: str,
            *,
            is_admin: bool,
            connection=None,
        ):
            selected = original(
                asset_id,
                is_admin=is_admin,
                connection=connection,
            )
            self.store.apply_event(
                {
                    "eventId": "event-refresh",
                    "assetId": asset["id"],
                    "type": "refresh",
                    "generation": 2,
                    "generated": asset["generated"],
                }
            )
            return selected

        with patch.object(
            self.store,
            "get_asset",
            side_effect=asset_then_write,
        ):
            status, shown = self.request(
                "GET",
                f"/v1/assets/{quote(asset['id'], safe='')}",
            )

        self.assertEqual(status, 200)
        self.assertEqual(shown["catalogRevision"], 1)
        self.assertEqual(shown["asset"]["version"], 1)
        self.assertEqual(self.store.catalog_revision(), 2)
        self.assertEqual(
            self.store.get_asset(asset["id"], is_admin=False)["version"],
            2,
        )

    def test_history_payload_and_revision_share_one_snapshot(self) -> None:
        asset = self.store.apply_event(self.event())["asset"]
        original = self.store.asset_history

        def history_then_write(
            asset_id: str,
            *,
            is_admin: bool,
            connection=None,
            after_history_id=None,
            fetch_limit=None,
        ):
            history = original(
                asset_id,
                is_admin=is_admin,
                connection=connection,
                after_history_id=after_history_id,
                fetch_limit=fetch_limit,
            )
            self.store.apply_event(
                {
                    "eventId": "event-refresh",
                    "assetId": asset["id"],
                    "type": "refresh",
                    "generation": 2,
                    "generated": asset["generated"],
                }
            )
            return history

        with patch.object(
            self.store,
            "asset_history",
            side_effect=history_then_write,
        ):
            status, history = self.request(
                "GET",
                f"/v1/assets/{quote(asset['id'], safe='')}/history",
            )

        self.assertEqual(status, 200)
        self.assertEqual(history["catalogRevision"], 1)
        self.assertEqual(len(history["history"]), 1)
        self.assertEqual(self.store.catalog_revision(), 2)
        self.assertEqual(
            len(self.store.asset_history(asset["id"], is_admin=False)),
            2,
        )

    def test_proposal_payload_and_revision_share_one_snapshot(self) -> None:
        asset = self.store.apply_event(self.event())["asset"]
        request = {
            "assetId": asset["id"],
            "baseVersion": asset["version"],
            "operations": [
                {
                    "op": "set",
                    "path": "/curated/description",
                    "value": "Reviewed roads",
                }
            ],
        }
        check = self.store.check_proposal(request, is_admin=False)
        proposal = self.store.create_proposal(
            {**request, "fingerprint": check["fingerprint"]},
            actor="author",
            is_admin=False,
        )
        original = self.store.list_proposals

        def proposals_then_apply(
            *,
            state: str | None,
            asset_id: str | None,
            is_admin: bool,
            connection=None,
            after=None,
            fetch_limit=None,
        ):
            proposals = original(
                state=state,
                asset_id=asset_id,
                is_admin=is_admin,
                connection=connection,
                after=after,
                fetch_limit=fetch_limit,
            )
            self.store.apply_proposal(
                proposal["id"],
                actor="approver",
                is_admin=False,
            )
            return proposals

        with patch.object(
            self.store,
            "list_proposals",
            side_effect=proposals_then_apply,
        ):
            status, proposals = self.request("GET", "/v1/proposals")

        self.assertEqual(status, 200)
        self.assertEqual(proposals["catalogRevision"], 1)
        self.assertEqual(proposals["proposals"][0]["state"], "pending")
        self.assertEqual(self.store.catalog_revision(), 2)
        self.assertEqual(
            self.store.get_proposal(
                proposal["id"],
                is_admin=False,
            )["state"],
            "applied",
        )


if __name__ == "__main__":
    unittest.main()
