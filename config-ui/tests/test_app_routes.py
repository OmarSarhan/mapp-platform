from __future__ import annotations

import io
import math
import threading
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPMessage
from http import HTTPStatus
from pathlib import Path
from unittest.mock import ANY, MagicMock, Mock, patch

import app
from control_plane import ControlStore, TOKEN_SCOPES
from semantic_sources import parse_exclusions


class FederationEnabledTests(unittest.TestCase):
    def test_requires_both_a_connection_url_and_bundled_mode(self):
        self.assertTrue(app.federation_enabled("postgresql://x", "bundled"))

    def test_disabled_without_a_federation_database_url(self):
        self.assertFalse(app.federation_enabled(None, "bundled"))
        self.assertFalse(app.federation_enabled("", "bundled"))

    def test_disabled_outside_bundled_mode(self):
        # The external handoff (docs/external-postgresql.md) grants
        # ownership of derived_layers alone — not the federation schema,
        # postgres_fdw, or the database-level CREATE that provisioning
        # needs, so an external deployment must never see federation
        # routes enabled even if it happens to set FEDERATION_DATABASE_URL.
        self.assertFalse(app.federation_enabled("postgresql://x", "external"))
        self.assertFalse(app.federation_enabled("postgresql://x", None))
        self.assertFalse(app.federation_enabled("postgresql://x", ""))


class FederationAliasActionRouteTests(unittest.TestCase):
    """POST /api/federation/aliases/<alias>/(observe|provision) must
    translate a raw local-database failure into the same
    "federation.registry_unavailable" 502 the sibling GET federation routes
    already use — not fall through to the generic, code-less 422
    psycopg.Error handler. FederationSchemaError is deliberately NOT
    caught here: it already carries its own status/code, correctly routed
    by the outer handler chain, and must keep passing through untouched."""

    @staticmethod
    def handler(action, *, actor="admin", payload=None):
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = f"/api/federation/aliases/leeds_ext/{action}"
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: actor
        handler._payload = lambda: {} if payload is None else payload
        handler._remote = lambda: "127.0.0.1"
        handler._json = lambda status, body: responses.append((status, body))
        handler.send_error = lambda status: responses.append((status, {}))
        return handler, responses

    def test_observe_reports_a_local_database_failure_as_registry_unavailable(
        self,
    ):
        import psycopg

        federation = MagicMock()
        federation.get.side_effect = psycopg.OperationalError(
            "could not connect to server"
        )
        handler, responses = self.handler("observe")

        with patch.object(app, "FEDERATION", federation):
            handler.do_POST()

        self.assertEqual(1, len(responses))
        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_GATEWAY, status)
        self.assertEqual("federation.registry_unavailable", body["code"])

    def test_provision_reports_a_local_database_failure_as_registry_unavailable(
        self,
    ):
        import psycopg

        federation = MagicMock()
        federation.get.side_effect = psycopg.OperationalError(
            "could not connect to server"
        )
        handler, responses = self.handler("provision")

        with patch.object(app, "FEDERATION", federation):
            handler.do_POST()

        self.assertEqual(1, len(responses))
        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_GATEWAY, status)
        self.assertEqual("federation.registry_unavailable", body["code"])

    def test_lock_contention_is_not_reported_as_a_dead_registry(self):
        # observe()/provision() take a blocking per-alias advisory lock and the
        # role carries lock_timeout, so contention arrives as a psycopg error.
        # Reporting it as "the registry is unavailable" sends an operator to
        # check a database that is working. The periodic verifier holds that
        # same lock across its probe, which is exactly when someone is likely
        # to be observing the alias by hand.
        import psycopg

        federation = MagicMock()
        federation.get.return_value = {
            "connectionRef": "LEEDS_EXT",
            "allowedRelations": ["leeds.smoke_control_orders"],
            "tlsPolicy": "require",
        }
        federation.observe.side_effect = psycopg.errors.LockNotAvailable(
            "canceling statement due to lock timeout"
        )
        handler, responses = self.handler("observe")

        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "CONTROL", MagicMock()
        ), patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": "postgresql://leeds"}
        ):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("federation.verification_in_progress", body["code"])
        # LockNotAvailable subclasses psycopg.Error, so the specific handler
        # has to come first; if it is ever reordered this fails.
        self.assertNotIn("registry", body["error"].lower())

    def test_retire_is_refused_while_a_derived_layer_still_reads_the_alias(
        self,
    ):
        # Revoking access underneath a dependent materialized view would
        # leave it refreshing against a source nobody believes is connected.
        federation = MagicMock()
        federation.get.return_value = {"connectionRef": "LEEDS_EXT"}
        derived = MagicMock()
        derived.source_schema_admission.return_value.__enter__.return_value = [
            "smoke_h3_r9",
        ]
        handler, responses = self.handler("retire")

        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "DERIVED", derived
        ), patch.object(app, "CONTROL", MagicMock()):
            handler.do_POST()

        federation.retire.assert_not_called()
        status, body = responses[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("federation.alias_in_use", body["code"])
        self.assertIn("smoke_h3_r9", body["error"])
        derived.source_schema_admission.assert_called_once_with(
            "source_leeds_ext"
        )

    def test_retire_proceeds_when_nothing_depends_on_the_alias(self):
        federation = MagicMock()
        federation.get.return_value = {"connectionRef": "LEEDS_EXT"}
        federation.retire.return_value = {
            "alias": "leeds_ext",
            "status": "retired",
            "archivedSchema": "retired_leeds_ext_20260811000000",
        }
        derived = MagicMock()
        derived.source_schema_admission.return_value.__enter__.return_value = []
        handler, responses = self.handler("retire")

        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "DERIVED", derived
        ), patch.object(app, "CONTROL", MagicMock()):
            handler.do_POST()

        federation.retire.assert_called_once_with("leeds_ext", "admin")
        status, body = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual("retired", body["alias"]["status"])

    def test_retire_refuses_when_a_derived_mutation_holds_admission(self):
        # The dependency check and the retirement DDL commit in separate
        # transactions, so admission is held across both to stop a layer
        # binding the schema in between. When a derived mutation already owns
        # it that guarantee is unavailable, and retirement must refuse rather
        # than act on a check it could not serialize.
        federation = MagicMock()
        federation.get.return_value = {"connectionRef": "LEEDS_EXT"}
        derived = MagicMock()
        # Raised from __enter__, not from the call: source_schema_admission is
        # a @contextmanager, so calling it only builds the generator and the
        # lock is taken when the block is entered.
        derived.source_schema_admission.return_value.__enter__.side_effect = (
            app.DerivedLayerContentionError("derived-mutation")
        )
        handler, responses = self.handler("retire")

        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "DERIVED", derived
        ), patch.object(app, "CONTROL", MagicMock()):
            handler.do_POST()

        federation.retire.assert_not_called()
        status, body = responses[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("federation.derived_layers_busy", body["code"])

    def test_retire_does_not_require_the_connection_reference_to_resolve(self):
        # A source whose credentials have already been removed from the
        # environment must still be retirable; resolving connectionRef would
        # raise federation.connection_ref_not_found and strand the alias.
        federation = MagicMock()
        federation.get.return_value = {"connectionRef": "ALREADY_REMOVED"}
        federation.retire.return_value = {
            "alias": "leeds_ext",
            "status": "retired",
            "archivedSchema": None,
        }
        derived = MagicMock()
        derived.source_schema_admission.return_value.__enter__.return_value = []
        handler, responses = self.handler("retire")

        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "DERIVED", derived
        ), patch.object(app, "CONTROL", MagicMock()), patch.object(
            app, "FEDERATION_CONNECTIONS", {}
        ):
            handler.do_POST()

        federation.retire.assert_called_once()
        self.assertEqual(HTTPStatus.OK, responses[0][0])

    def test_observe_route_calls_the_serialized_store_operation(self):
        federation = MagicMock()
        federation.get.return_value = {
            "connectionRef": "LEEDS_EXT",
            "allowedRelations": ["leeds.smoke_control_orders"],
            "tlsPolicy": "require",
        }
        federation.observe.return_value = {
            "lastObservationId": 42,
            "lastObservation": {"connectivity": "reachable"},
        }
        handler, responses = self.handler("observe")

        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "CONTROL", MagicMock()
        ), patch.object(
            app, "FEDERATION_CONNECTIONS",
            {"LEEDS_EXT": "postgresql://reader:secret@source-db:5432/sourcedb"},
        ):
            handler.do_POST()

        federation.observe.assert_called_once_with(
            "leeds_ext",
            "postgresql://reader:secret@source-db:5432/sourcedb",
            allowed_relations=("leeds.smoke_control_orders",),
            tls_policy="require",
        )
        self.assertEqual(1, len(responses))
        self.assertEqual(HTTPStatus.OK, responses[0][0])

    def test_provision_binds_approval_to_the_observed_revision(self):
        federation = MagicMock()
        federation.get.return_value = {
            "connectionRef": "LEEDS_EXT",
            "allowedRelations": ["leeds.smoke_control_orders"],
            "tlsPolicy": "require",
        }
        federation.provision.return_value = {"alias": "leeds_ext"}
        handler, responses = self.handler(
            "provision",
            payload={
                "expectedObservationId": 42,
                "schemaChangeAcknowledged": True,
            },
        )
        control = MagicMock()

        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "CONTROL", control
        ), patch.object(
            app, "FEDERATION_CONNECTIONS",
            {"LEEDS_EXT": "postgresql://reader:secret@source-db:5432/sourcedb"},
        ):
            handler.do_POST()

        federation.provision.assert_called_once_with(
            "leeds_ext",
            "postgresql://reader:secret@source-db:5432/sourcedb",
            "admin",
            expected_observation_id=42,
            acknowledge_row_level_security=False,
            acknowledge_schema_change=True,
            acknowledge_physical_rebind=False,
        )
        self.assertEqual(
            {
                "alias": "leeds_ext",
                "observationId": 42,
                "schemaChangeAcknowledged": True,
            },
            control.audit.call_args.kwargs["details"],
        )
        self.assertEqual(HTTPStatus.OK, responses[0][0])

    def test_provision_requires_a_positive_observation_id(self):
        federation = MagicMock()
        federation.get.return_value = {
            "connectionRef": "LEEDS_EXT",
            "allowedRelations": ["leeds.smoke_control_orders"],
            "tlsPolicy": "require",
        }
        handler, responses = self.handler("provision", payload={})

        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS",
            {"LEEDS_EXT": "postgresql://reader:secret@source-db:5432/sourcedb"},
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        self.assertEqual("federation.invalid_request", responses[0][1]["code"])
        federation.provision.assert_not_called()

    def test_federation_connections_are_isolated_from_normal_discovery(self):
        federation_url = "postgresql://federation-reader@source-db/source"
        ordinary_url = "postgresql://runtime-reader@db/mapp"
        with patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": federation_url}
        ), patch.object(
            app, "DB_CONNECTIONS", {"MAPP": ordinary_url}
        ), patch.object(app, "discover_connection", return_value=[]) as discover:
            self.assertEqual(
                federation_url,
                app.resolve_federation_connection_url("LEEDS_EXT"),
            )
            self.assertEqual([], app.discover())

        discover.assert_called_once_with("MAPP", ordinary_url)

    def test_normal_database_connection_cannot_satisfy_connection_ref(self):
        with patch.object(app, "FEDERATION_CONNECTIONS", {}), patch.object(
            app,
            "DB_CONNECTIONS",
            {"LEEDS_EXT": "postgresql://runtime-reader@source-db/source"},
        ):
            with self.assertRaises(app.FederationSchemaError) as raised:
                app.resolve_federation_connection_url("LEEDS_EXT")

        self.assertEqual(
            "federation.connection_ref_not_found", raised.exception.code
        )

    def test_a_federation_schema_error_still_carries_its_own_status_and_code(
        self,
    ):
        # Confirms the new try/except wraps only psycopg.Error — a
        # validation-style FederationSchemaError (e.g. alias not found)
        # must still be handled by the existing, unrelated
        # FederationSchemaError chain, not swallowed into a 502.
        from federation_schema import FederationSchemaError

        federation = MagicMock()
        federation.get.side_effect = FederationSchemaError(
            "boom", code="federation.some_validation_error", status=HTTPStatus.CONFLICT
        )
        handler, responses = self.handler("observe")

        with patch.object(app, "FEDERATION", federation):
            handler.do_POST()

        self.assertEqual(1, len(responses))
        status, body = responses[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("federation.some_validation_error", body["code"])


class FederationAliasReadRouteTests(unittest.TestCase):
    """GET /api/federation/aliases(/<alias>) must preserve a
    FederationSchemaError's own status/code (round 26 finding) — e.g.
    federation.not_configured, a permanent configuration fact when
    FEDERATION is unset outside bundled mode, must never be folded into
    the generic 502 federation.registry_unavailable a real psycopg.Error
    still gets, or a contract-driven client would retry a deployment mode
    that will never become available."""

    @staticmethod
    def handler(path, *, actor="admin"):
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: actor
        handler._json = lambda status, body: responses.append((status, body))
        return handler, responses

    def test_list_preserves_not_configured_status_and_code(self):
        handler, responses = self.handler("/api/federation/aliases")

        with patch.object(app, "FEDERATION", None):
            handler.do_GET()

        self.assertEqual(1, len(responses))
        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertEqual("federation.not_configured", body["code"])

    def test_list_still_reports_a_real_database_failure_as_unavailable(self):
        import psycopg

        federation = MagicMock()
        federation.list.side_effect = psycopg.OperationalError(
            "could not connect to server"
        )
        handler, responses = self.handler("/api/federation/aliases")

        with patch.object(app, "FEDERATION", federation):
            handler.do_GET()

        self.assertEqual(1, len(responses))
        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_GATEWAY, status)
        self.assertEqual("federation.registry_unavailable", body["code"])

    def test_list_uses_the_store_bound_without_cursor_surface(self):
        federation = MagicMock()
        federation.list.return_value = [
            {"alias": "alpha"},
            {"alias": "bravo"},
        ]
        handler, responses = self.handler("/api/federation/aliases")

        with patch.object(app, "FEDERATION", federation):
            handler.do_GET()

        federation.list.assert_called_once_with()
        self.assertEqual(
            [{"alias": "alpha"}, {"alias": "bravo"}],
            responses[0][1]["aliases"],
        )

    def test_get_by_name_preserves_not_configured_status_and_code(self):
        handler, responses = self.handler("/api/federation/aliases/leeds_ext")

        with patch.object(app, "FEDERATION", None):
            handler.do_GET()

        self.assertEqual(1, len(responses))
        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertEqual("federation.not_configured", body["code"])

    def test_get_by_name_still_reports_a_real_database_failure_as_unavailable(
        self,
    ):
        import psycopg

        federation = MagicMock()
        federation.get.side_effect = psycopg.OperationalError(
            "could not connect to server"
        )
        handler, responses = self.handler("/api/federation/aliases/leeds_ext")

        with patch.object(app, "FEDERATION", federation):
            handler.do_GET()

        self.assertEqual(1, len(responses))
        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_GATEWAY, status)
        self.assertEqual("federation.registry_unavailable", body["code"])


class DerivedFailureStateTests(unittest.TestCase):
    def test_exception_reclassification_cannot_downgrade_uncertainty(self):
        failure = RuntimeError("failed")
        failure.failure_phase = "preflight"
        response = app.derived_exception_response(
            {"error": "failed", "indeterminate": True},
            failure,
            "refresh",
            "preflight",
        )

        self.assertTrue(response["indeterminate"])
        self.assertNotIn("stateUnchanged", response)
        self.assertNotIn("safeState", response)

    def test_contradictory_rollback_claims_fail_closed(self):
        for phase in ("preflight", "transaction-commit", "result-reporting"):
            with self.subTest(phase=phase):
                response = app.derived_failure_state(
                    {"error": "failed"},
                    "refresh",
                    failure_phase=phase,
                    rolled_back=True,
                )

                self.assertTrue(response["indeterminate"])
                self.assertNotIn("stateUnchanged", response)
                self.assertNotIn("safeState", response)
                self.assertNotIn("rolledBack", response)

    def test_only_explicit_database_rollback_proves_unchanged_state(self):
        unconfirmed = app.derived_failure_state(
            {"error": "failed"},
            "refresh",
            failure_phase="database-transaction",
        )
        response = app.derived_failure_state(
            {"error": "failed"},
            "refresh",
            failure_phase="database-transaction",
            rolled_back=True,
        )

        self.assertTrue(unconfirmed["indeterminate"])
        self.assertEqual("transaction-rollback", unconfirmed["failurePhase"])
        self.assertNotIn("stateUnchanged", unconfirmed)
        self.assertTrue(response["stateUnchanged"])
        self.assertTrue(response["rolledBack"])
        self.assertNotIn("indeterminate", response)

    def test_indeterminate_lock_timeout_is_not_retryable(self):
        failure = app.psycopg.errors.LockNotAvailable(
            "SECRET commit-time lock context",
        )
        response = app.derived_database_error(
            failure,
            "create",
            failure_phase="transaction-commit",
            state_unchanged=False,
            indeterminate=True,
        )

        self.assertEqual("derived_layer.database_error", response["code"])
        self.assertTrue(response["indeterminate"])
        self.assertEqual(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            app.derived_failure_http_status(
                response,
                app.derived_database_error_status(response),
            ),
        )
        self.assertNotIn("retryable", response)
        self.assertNotIn("contentionScope", response)
        self.assertNotIn("stateUnchanged", response)
        self.assertNotIn("safeState", response)
        self.assertNotIn("rolledBack", response)
        self.assertNotIn("SECRET", repr(response))


class DerivedSemanticSourcePolicyTests(unittest.TestCase):
    @staticmethod
    def payload():
        return {
            "name": "roads_h3_r9",
            "kind": "view",
            "query": "SELECT id, geom_3857 FROM leeds.roads",
            "sources": ["leeds.roads"],
            "idColumn": "id",
            "geometryColumn": "geom_3857",
        }

    @staticmethod
    def catalog(*, status="ready"):
        return {"assets": [{
            "status": status,
            "generated": {"binding": {
                "adapter": "postgresql",
                "alias": "MAPP",
                "schema": "leeds",
                "relation": "roads",
            }},
        }]}

    def test_allows_declared_sources_with_ready_semantic_profiles(self):
        app.require_semantic_derived_sources(self.payload(), self.catalog())

    def test_rejects_declared_sources_without_semantic_profiles(self):
        with self.assertRaisesRegex(
            app.DerivedLayerError,
            "leeds.roads.*semantic source sync",
        ):
            app.require_semantic_derived_sources(self.payload(), {"assets": []})

    def test_rejects_archived_semantic_source_profiles(self):
        with self.assertRaisesRegex(app.DerivedLayerError, "leeds.roads"):
            app.require_semantic_derived_sources(
                self.payload(), self.catalog(status="archived")
            )


class ExcludedSemanticSourceArchivalTests(unittest.TestCase):
    def test_archives_only_ready_assets_matching_configured_exclusions(self):
        asset = {
            "id": "asset:census-datasets",
            "status": "ready",
            "generation": 4,
            "generated": {"binding": {
                "adapter": "postgresql",
                "alias": "MAPP",
                "schema": "leeds",
                "relation": "census_datasets",
            }},
        }
        semantic = Mock()
        semantic.request.side_effect = [
            {"assets": [asset]},
            {
                "catalogRevision": 8,
                "event": {
                    "eventId": ANY,
                    "payloadHash": ANY,
                    "idempotent": False,
                },
                "asset": {
                    "id": asset["id"],
                    "generation": 5,
                    "status": "archived",
                    "catalogRevision": 8,
                },
            },
        ]
        with patch.object(app, "SEMANTIC", semantic), patch.object(
            app,
            "SEMANTIC_SOURCE_EXCLUSIONS",
            parse_exclusions("MAPP:leeds.census_datasets"),
        ):
            archived = app.archive_excluded_semantic_sources("admin")

        self.assertEqual([{"id": asset["id"], "binding": asset["generated"]["binding"]}], archived)
        self.assertEqual(2, semantic.request.call_count)


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

    def test_rejects_filtering_panel_entries_without_real_table_columns(self):
        workspace = {"dbs": "MAPP", "locale": {"layers": {"Places": {
            "format": "mvt",
            "dbs": "MAPP",
            "table": "derived_layers.places",
            "geom": "geom",
            "srid": 3857,
            "qID": "id",
            "filter": {"includeAll": True},
            "infoj": [
                {
                    "type": "numeric",
                    "title": "Calculated score",
                    "field": "score_rounded",
                    "fieldfx": "round(score)::bigint",
                }
            ],
            "style": {"default": {"fillColor": "#eeeeee"}},
        }}}}
        tables = [{
            "dbs": "MAPP",
            "schema": "derived_layers",
            "table": "places",
            "columns": [
                {"name": "id", "geometryType": "", "srid": None},
                {"name": "score", "geometryType": "", "srid": None},
                {"name": "geom", "geometryType": "POLYGON", "srid": 3857},
            ],
        }]

        paths = {error["path"] for error in app.validate_catalog(workspace, tables)}

        self.assertIn("locale.layers.Places.infoj.0.filter", paths)

    def test_zoom_keyed_tables_map_is_checked_against_the_catalog(self):
        workspace = {"dbs": "MAPP", "locale": {"layers": {"Places": {
            "format": "mvt",
            "geom": "geom",
            "srid": 3857,
            "qID": "id",
            "tables": {"0": "public.low", "12": "public.missing"},
            "style": {"default": {"fillColor": "#eeeeee"}},
        }}}}
        tables = [{
            "dbs": "MAPP",
            "schema": "public",
            "table": "low",
            "columns": [
                {"name": "id", "geometryType": "", "srid": None},
                {"name": "geom", "geometryType": "POLYGON", "srid": 3857},
            ],
        }]

        errors = app.validate_catalog(workspace, tables)

        self.assertEqual(
            [{
                "path": "locale.layers.Places.tables.12",
                "message": (
                    "Table is not selectable through the configured "
                    "read-only connection."
                ),
            }],
            errors,
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


class TokenAdministrationRouteTests(unittest.TestCase):
    @staticmethod
    def handler(
        payload: dict,
        *,
        actor: str = "admin",
        path: str = "/api/admin/tokens",
    ) -> tuple[app.Handler, list[tuple[HTTPStatus, dict]]]:
        responses: list[tuple[HTTPStatus, dict]] = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: actor
        handler._payload = lambda: payload
        handler._remote = lambda: "127.0.0.1"
        handler._json = lambda status, body: responses.append((status, body))
        handler.send_error = lambda status: responses.append((status, {}))
        return handler, responses

    def test_explicit_empty_scopes_fail_closed_at_admin_route(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            handler, responses = self.handler({
                "name": "must not become full",
                "scopes": [],
            })

            with patch.object(app, "CONTROL", store):
                handler.do_POST()

            self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
            self.assertEqual([], store.list_tokens())

    def test_misspelled_or_null_scopes_never_expand_to_legacy_full(self):
        invalid_requests = (
            {
                "name": "misspelled scope",
                "scope": ["semantic:inspect"],
            },
            {
                "name": "explicit null scope",
                "scopes": None,
            },
            {
                "name": "explicit null expiry",
                "scopes": ["semantic:inspect"],
                "expires": None,
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")

            for payload in invalid_requests:
                with self.subTest(payload=payload):
                    handler, responses = self.handler(payload)
                    with patch.object(app, "CONTROL", store):
                        handler.do_POST()
                    self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])

            self.assertEqual([], store.list_tokens())

    def test_missing_expiry_defaults_to_thirty_days(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")
            handler, responses = self.handler({
                "name": "default lifetime",
                "scopes": ["semantic:inspect"],
            })
            before = datetime.now(timezone.utc)

            with patch.object(app, "CONTROL", store):
                handler.do_POST()

            self.assertEqual(HTTPStatus.CREATED, responses[0][0])
            expiry = datetime.fromisoformat(
                responses[0][1]["record"]["expires"].replace("Z", "+00:00")
            )
            self.assertGreaterEqual(expiry, before + timedelta(days=30))
            self.assertLess(expiry, before + timedelta(days=30, seconds=2))

    def test_extended_or_non_expiring_tokens_require_confirmation(self):
        expiry = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            store = ControlStore(Path(directory))
            store.initialize("correct horse battery staple", "instance")

            for requested_expiry in (expiry, None):
                with self.subTest(expires=requested_expiry):
                    handler, responses = self.handler({
                        "name": "unconfirmed extension",
                        "scopes": ["semantic:inspect"],
                        "expires": requested_expiry,
                    })
                    with patch.object(app, "CONTROL", store):
                        handler.do_POST()
                    self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])

            handler, responses = self.handler({
                "name": "confirmed extension",
                "scopes": ["semantic:inspect"],
                "expires": expiry,
                "extendedExpiryConfirmed": True,
            })
            with patch.object(app, "CONTROL", store):
                handler.do_POST()
            self.assertEqual(HTTPStatus.CREATED, responses[0][0])

            handler, responses = self.handler({
                "name": "confirmed non-expiring",
                "scopes": ["semantic:inspect"],
                "expires": None,
                "extendedExpiryConfirmed": True,
            })
            with patch.object(app, "CONTROL", store):
                handler.do_POST()
            self.assertEqual(HTTPStatus.CREATED, responses[0][0])
            self.assertIsNone(responses[0][1]["record"]["expires"])

    def test_extended_expiry_confirmation_must_be_boolean(self):
        handler, responses = self.handler({
            "name": "ambiguous confirmation",
            "scopes": ["semantic:inspect"],
            "extendedExpiryConfirmed": "yes",
        })
        control = MagicMock()

        with patch.object(app, "CONTROL", control):
            handler.do_POST()

        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        control.create_token.assert_not_called()

    def test_full_bearer_token_cannot_provision_more_tokens(self):
        handler, responses = self.handler(
            {"name": "nested administrator", "scopes": ["full"]},
            actor="token:existing",
        )
        control = MagicMock()

        with patch.object(app, "CONTROL", control):
            handler.do_POST()

        self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
        control.create_token.assert_not_called()

    def test_only_exact_admin_token_route_can_revoke(self):
        control = MagicMock()
        control.revoke_token.return_value = True

        handler, responses = self.handler(
            {},
            path="/api/admin/tokens/token-1/revoke",
        )
        with patch.object(app, "CONTROL", control):
            handler.do_POST()
        self.assertEqual(HTTPStatus.OK, responses[0][0])
        control.revoke_token.assert_called_once_with("token-1")

        control.reset_mock()
        for path in (
            "/api/not-admin/token-1/revoke",
            "/api/admin/tokens/token-1/extra/revoke",
            "/api/admin/tokens/token-1/revoke/extra",
        ):
            with self.subTest(path=path):
                handler, responses = self.handler({}, path=path)
                with patch.object(app, "CONTROL", control):
                    handler.do_POST()
                self.assertEqual(HTTPStatus.NOT_FOUND, responses[0][0])
                control.revoke_token.assert_not_called()

    def test_full_bearer_token_cannot_revoke_tokens(self):
        handler, responses = self.handler(
            {},
            actor="token:existing",
            path="/api/admin/tokens/token-1/revoke",
        )
        control = MagicMock()

        with patch.object(app, "CONTROL", control):
            handler.do_POST()

        self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
        control.revoke_token.assert_not_called()


class AdministratorSessionBoundaryTests(unittest.TestCase):
    @staticmethod
    def get_handler(
        path: str,
        actor: str,
    ) -> tuple[app.Handler, list[tuple[HTTPStatus, dict]]]:
        responses: list[tuple[HTTPStatus, dict]] = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: actor
        handler._json = lambda status, body: responses.append((status, body))
        return handler, responses

    def test_admin_reads_require_a_session_even_for_full_bearers(self):
        cases = (
            ("/api/admin/tokens", "list_tokens", "tokens"),
            (
                "/api/admin/device-authorizations",
                "list_device_authorizations",
                "authorizations",
            ),
            ("/api/admin/audit", "audit_tail", "events"),
        )
        for path, method_name, response_key in cases:
            with self.subTest(path=path, actor="token:full"):
                handler, responses = self.get_handler(
                    path,
                    "token:full",
                )
                control = MagicMock()
                with patch.object(app, "CONTROL", control):
                    handler.do_GET()
                self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
                getattr(control, method_name).assert_not_called()

            with self.subTest(path=path, actor="admin"):
                handler, responses = self.get_handler(path, "admin")
                control = MagicMock()
                getattr(control, method_name).return_value = []
                with patch.object(app, "CONTROL", control):
                    handler.do_GET()
                self.assertEqual(
                    (HTTPStatus.OK, {response_key: []}),
                    responses[0],
                )
                getattr(control, method_name).assert_called_once_with()

    def test_connect_reports_narrow_token_scopes_and_expiry(self):
        handler, responses = self.get_handler("/api/connect", "token:visual")
        handler._authentication = {
            "actor": "token:visual",
            "tokenId": "visual",
            "scopes": ["visual"],
            "expires": "2030-01-01T00:00:00Z",
        }

        handler.do_GET()

        self.assertEqual(
            (
                HTTPStatus.OK,
                {
                    "authenticated": True,
                    "actor": "token:visual",
                    "tokenId": "visual",
                    "scopes": ["visual"],
                    "expires": "2030-01-01T00:00:00Z",
                },
            ),
            responses[0],
        )


class AuthenticationHeaderTests(unittest.TestCase):
    def test_duplicate_authorization_headers_fail_closed(self):
        for values in (
            ("Bearer valid", "Bearer other"),
            ("Bearer invalid", "Bearer valid"),
        ):
            with self.subTest(values=values):
                handler = object.__new__(app.Handler)
                handler.headers = HTTPMessage()
                for value in values:
                    handler.headers.add_header("Authorization", value)
                handler.headers.add_header(
                    "Cookie",
                    "mapp_session=valid-session",
                )
                handler.client_address = ("127.0.0.1", 12345)
                control = MagicMock()

                with patch.object(app, "CONTROL", control):
                    actor = handler._actor()

                self.assertIsNone(actor)
                control.authenticate_token.assert_not_called()
                control.session.assert_not_called()

    def test_single_authorization_header_uses_the_bearer_identity(self):
        handler = object.__new__(app.Handler)
        handler.headers = HTTPMessage()
        handler.headers.add_header("Authorization", "Bearer valid")
        handler.client_address = ("127.0.0.1", 12345)
        control = MagicMock()
        control.authenticate_token.return_value = {
            "id": "token-1",
            "scopes": ["semantic:inspect"],
        }

        with patch.object(app, "CONTROL", control):
            actor = handler._actor()

        self.assertEqual("token:token-1", actor)
        self.assertEqual(
            {
                "actor": "token:token-1",
                "tokenId": "token-1",
                "scopes": ["semantic:inspect"],
                "expires": None,
            },
            handler._authentication,
        )
        control.authenticate_token.assert_called_once_with(
            "valid",
            "127.0.0.1",
        )
        control.session.assert_not_called()


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

    def test_new_noncanonical_layer_key_gets_actionable_warning(self):
        warnings = app.layer_key_diagnostics(
            {"locale": {"layers": {"Bus Stops": {"name": "Bus Stops"}}}},
            {"locale": {"layers": {}}},
        )

        self.assertEqual(1, len(warnings))
        self.assertEqual("Bus Stops", warnings[0]["configuredKey"])
        self.assertEqual("Bus Stops", warnings[0]["resolvedBrowserKey"])
        self.assertEqual("Bus_Stops", warnings[0]["recommendedKey"])
        self.assertEqual("warning", app.annotated(warnings)[0]["severity"])

    def test_inherited_noncanonical_layer_key_is_warned_once_at_source(self):
        candidate = {
            "locale": {"layers": {"Bus Stops": {"name": "Bus Stops"}}},
            "locales": {"cy": {"name": "Cymraeg"}},
        }

        warnings = app.layer_key_diagnostics(
            candidate,
            {"locale": {"layers": {}}, "locales": {"cy": {}}},
        )

        self.assertEqual(1, len(warnings))
        self.assertEqual("locale.layers.Bus Stops", warnings[0]["path"])

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

    def test_layer_values_returns_bounded_category_counts(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ("text", False),
            (12, 10, 3),
        ]
        cursor.fetchall.return_value = [("red", 6), ("blue", 3)]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect = MagicMock()
        connect.return_value.__enter__.return_value = connection
        workspace = {
            "dbs": "main",
            "locale": {"layers": {"Areas": {
                "format": "mvt",
                "table": "derived_layers.areas",
                "geom": "geom",
                "qID": "id",
                "filter": {"default": {"category": {"match": "red"}}},
            }}},
        }

        with (
            patch.object(app, "DB_CONNECTIONS", {"main": "postgresql://db"}),
            patch.object(app.psycopg, "connect", connect),
        ):
            result = app.aggregate_layer_values(
                workspace, None, "Areas", "category", 2
            )

        self.assertEqual(12, result["totalCount"])
        self.assertEqual(2, result["nullCount"])
        self.assertEqual(3, result["distinctCount"])
        self.assertEqual(
            [{"value": "red", "count": 6}, {"value": "blue", "count": 3}],
            result["values"],
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(
            {"category": {"match": "red"}},
            result["effectiveDataset"]["effectiveFilter"]["fixedFilter"],
        )
        self.assertEqual(
            ("red", 2),
            cursor.execute.call_args_list[-1].args[1],
        )

    def test_layer_statistics_returns_bounded_numeric_distribution(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ("numeric", "numeric", False),
            (12, 10, 9, 0.0, 1.0, [0.0, 0.1, 0.5, 0.9, 1.0]),
            (1, 8, 5, 4),
        ]
        cursor.fetchall.return_value = [(1, 4), (2, 5)]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect = MagicMock()
        connect.return_value.__enter__.return_value = connection
        workspace = {
            "dbs": "main",
            "locale": {"layers": {"Areas": {
                "format": "mvt",
                "table": "derived_layers.areas",
                "geom": "geom",
                "qID": "id",
                "filter": {"default": {"percent": {"gte": 0.05}}},
            }}},
        }

        with (
            patch.object(app, "DB_CONNECTIONS", {"main": "postgresql://db"}),
            patch.object(app.psycopg, "connect", connect),
        ):
            result = app.aggregate_layer_statistics(
                workspace,
                None,
                "Areas",
                "percent",
                2,
                [0.05],
                [0.5],
            )

        self.assertEqual(12, result["totalCount"])
        self.assertEqual(2, result["nullCount"])
        self.assertEqual(9, result["finiteCount"])
        self.assertEqual(1, result["nonFiniteCount"])
        self.assertEqual(0.5, result["quantiles"][2]["value"])
        self.assertEqual([4, 5], [item["count"] for item in result["histogram"]])
        self.assertEqual(1, result["thresholds"][0]["belowCount"])
        self.assertEqual([5, 4], [item["count"] for item in result["classes"]])
        self.assertFalse(result["classes"][0]["upperInclusive"])
        self.assertEqual(
            {"percent": {"gte": 0.05}},
            result["effectiveDataset"]["effectiveFilter"]["fixedFilter"],
        )
        summary_query, summary_params = cursor.execute.call_args_list[4].args
        self.assertIn('"percent" >= %s', summary_query.as_string(None))
        self.assertIn("percentile_disc", summary_query.as_string(None))
        self.assertIn(
            "'1.7976931348623157e308'::numeric",
            summary_query.as_string(None),
        )
        self.assertIn("CASE WHEN", summary_query.as_string(None))
        self.assertEqual(["0.05"], summary_params)

    def test_layer_statistics_keeps_extreme_histogram_bounds_finite(self) -> None:
        maximum_float = 1.7976931348623157e308
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ("double precision", "float8", False),
            (
                2,
                2,
                2,
                -maximum_float,
                maximum_float,
                [
                    -maximum_float,
                    -maximum_float,
                    -maximum_float,
                    maximum_float,
                    maximum_float,
                ],
            ),
        ]
        cursor.fetchall.return_value = [(1, 1), (2, 1)]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect = MagicMock()
        connect.return_value.__enter__.return_value = connection
        workspace = {
            "dbs": "main",
            "locale": {"layers": {"Areas": {
                "format": "mvt",
                "table": "derived_layers.areas",
                "geom": "geom",
                "qID": "id",
            }}},
        }

        with (
            patch.object(app, "DB_CONNECTIONS", {"main": "postgresql://db"}),
            patch.object(app.psycopg, "connect", connect),
        ):
            result = app.aggregate_layer_statistics(
                workspace, None, "Areas", "percent", 2, [], []
            )

        bounds = [
            value
            for item in result["histogram"]
            for value in (item["lower"], item["upper"])
        ]
        self.assertTrue(all(math.isfinite(value) for value in bounds))
        self.assertEqual(0.0, result["histogram"][0]["upper"])
        self.assertTrue(all(
            math.isfinite(item["value"]) for item in result["quantiles"]
        ))
        summary_query = cursor.execute.call_args_list[4].args[0]
        self.assertIn("percentile_disc", summary_query.as_string(None))

    def test_layer_statistics_route_parses_bounded_thresholds_and_breaks(self):
        handler, responses = self.handler(
            "/api/layers/Areas/statistics?field=percent&bins=8"
            "&threshold=0.05&break=0.5&break=1.0"
        )
        handler._authentication = {
            "actor": "token:test",
            "scopes": ["derive", "semantic:inspect"],
        }
        result = {
            "locale": "locale",
            "key": "Areas",
            "field": "percent",
        }
        with (
            patch.object(
                app,
                "read_workspace",
                return_value=(b"{}", {"locale": {}}, "revision-1"),
            ),
            patch.object(
                app, "aggregate_layer_statistics", return_value=result
            ) as aggregate,
        ):
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual("revision-1", responses[0][1]["revision"])
        aggregate.assert_called_once_with(
            {"locale": {}}, None, "Areas", "percent", 8,
            [0.05], [0.5, 1.0],
        )

    def test_layer_statistics_handles_an_empty_numeric_distribution(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ("double precision", "float8", False),
            (4, 0, 0, None, None, None),
        ]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value.__enter__.return_value = cursor
        workspace = {
            "dbs": "main",
            "locale": {"layers": {"Areas": {
                "format": "mvt",
                "table": "derived_layers.areas",
                "geom": "geom",
                "qID": "id",
            }}},
        }
        with (
            patch.object(app, "DB_CONNECTIONS", {"main": "postgresql://db"}),
            patch.object(app.psycopg, "connect", return_value=connection),
        ):
            result = app.aggregate_layer_statistics(
                workspace, None, "Areas", "percent", 10, [], [],
            )

        self.assertIsNone(result["min"])
        self.assertIsNone(result["max"])
        self.assertEqual([], result["quantiles"])
        self.assertEqual([], result["histogram"])
        self.assertEqual(0, result["binsReturned"])

    def test_layer_statistics_route_rejects_nonascending_breaks(self):
        handler, responses = self.handler(
            "/api/layers/Areas/statistics?field=percent&break=1&break=1"
        )
        handler._authentication = {
            "actor": "token:test",
            "scopes": ["derive", "semantic:inspect"],
        }
        with patch.object(app, "aggregate_layer_statistics") as aggregate:
            handler.do_GET()

        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        self.assertEqual("layer.statistics_invalid", responses[0][1]["code"])
        aggregate.assert_not_called()

    def test_layer_values_requires_and_accepts_derived_create_scopes(self) -> None:
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = "/api/layers/Areas/values?field=category"
        handler._host_allowed = lambda: True
        handler._actor = lambda state_change=False: "token:test"
        handler._authentication = {
            "actor": "token:test",
            "scopes": ["derive"],
        }
        handler._json = lambda status, payload: responses.append((status, payload))

        handler.do_GET()

        self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
        self.assertEqual("semantic:inspect", responses[0][1]["requiredScope"])
        self.assertEqual(
            "derive",
            app.Handler._required_scope(
                "/api/layers/Areas/values", "GET"
            ),
        )
        self.assertEqual(
            "derive",
            app.Handler._required_scope(
                "/api/layers/Areas/statistics", "GET"
            ),
        )

        responses.clear()
        handler._authentication["scopes"].append("semantic:inspect")
        with (
            patch.object(
                app,
                "read_workspace",
                return_value=(b"{}", {"locale": {}}, "revision-1"),
            ),
            patch.object(
                app,
                "aggregate_layer_values",
                return_value={"key": "Areas", "field": "category"},
            ) as aggregate,
        ):
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual("revision-1", responses[0][1]["revision"])
        aggregate.assert_called_once_with(
            {"locale": {}}, None, "Areas", "category", 100
        )


class LayerDependencyGuardSyncTests(unittest.TestCase):
    def test_syncs_grouped_relations_per_alias(self):
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect = MagicMock()
        connect.return_value.__enter__.return_value = connection

        with (
            patch.object(
                app,
                "platform_dependencies",
                return_value=[
                    {"alias": "MAPP", "relation": "leeds.roads"},
                    {"alias": "MAPP", "relation": "leeds.bus_stops"},
                ],
            ),
            patch.object(app, "DB_CONNECTIONS", {"MAPP": "postgresql://reader"}),
            patch.object(app.psycopg, "connect", connect),
        ):
            app.sync_layer_dependency_guard({})

        connect.assert_called_once_with(
            "postgresql://reader", connect_timeout=5
        )
        args = cursor.execute.call_args.args
        self.assertIn("mapp_sync_platform_layer_dependencies", args[0])
        self.assertEqual(
            ["MAPP", '["leeds.bus_stops", "leeds.roads"]'],
            args[1],
        )

    def test_skips_an_alias_with_no_configured_connection(self):
        connect = MagicMock()

        with (
            patch.object(
                app,
                "platform_dependencies",
                return_value=[{"alias": "UNCONFIGURED", "relation": "leeds.roads"}],
            ),
            patch.object(app, "DB_CONNECTIONS", {}),
            patch.object(app.psycopg, "connect", connect),
        ):
            app.sync_layer_dependency_guard({})

        connect.assert_not_called()

    def test_a_sync_failure_is_logged_and_does_not_raise(self):
        with (
            patch.object(
                app,
                "platform_dependencies",
                return_value=[{"alias": "MAPP", "relation": "leeds.roads"}],
            ),
            patch.object(app, "DB_CONNECTIONS", {"MAPP": "postgresql://reader"}),
            patch.object(
                app.psycopg, "connect", side_effect=RuntimeError("unreachable")
            ),
            self.assertLogs(app.LOGGER, level="ERROR") as logs,
        ):
            app.sync_layer_dependency_guard({})  # must not raise

        self.assertIn("MAPP", logs.output[0])
        self.assertIn("guard sync failed", logs.output[0])


class DependencyRouteTests(unittest.TestCase):
    @staticmethod
    def handler(
        path: str,
    ) -> tuple[app.Handler, list[tuple[HTTPStatus, dict]]]:
        responses: list[tuple[HTTPStatus, dict]] = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda: "token:test"
        handler._json = lambda status, payload: responses.append((status, payload))
        return handler, responses

    def test_dependency_route_returns_configured_references(self):
        workspace = {
            "dbs": "MAPP",
            "locale": {
                "layers": {
                    "Stops": {
                        "format": "mvt",
                        "table": "leeds.bus_stops",
                    },
                },
            },
        }
        with (
            patch.object(app, "DB_CONNECTIONS", {"MAPP": "postgresql://reader"}),
            patch.object(app, "DERIVED", None),
            patch.object(
                app,
                "read_workspace",
                return_value=(b"{}", workspace, "revision"),
            ),
        ):
            handler, responses = self.handler("/api/dependencies")
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual({
            "dependencies": [{
                "alias": "MAPP",
                "relation": "leeds.bus_stops",
                "workspaceLayers": ["locale:Stops"],
                "derivedLayers": [],
            }],
        }, responses[0][1])

    def test_dependency_route_returns_blocked_status_for_reference(self):
        workspace = {
            "dbs": "MAPP",
            "locale": {
                "layers": {
                    "Stops": {
                        "format": "mvt",
                        "table": "leeds.bus_stops",
                    },
                },
            },
        }
        with (
            patch.object(app, "DB_CONNECTIONS", {"MAPP": "postgresql://reader"}),
            patch.object(app, "DERIVED", None),
            patch.object(
                app,
                "read_workspace",
                return_value=(b"{}", workspace, "revision"),
            ),
        ):
            handler, responses = self.handler(
                "/api/dependencies?alias=MAPP&schema=leeds&relation=bus_stops",
            )
            handler.do_GET()

        status, payload = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertTrue(payload["blocked"])
        self.assertEqual("MAPP", payload["alias"])
        self.assertEqual("leeds", payload["schema"])
        self.assertEqual("bus_stops", payload["relation"])
        self.assertEqual(1, len(payload["matches"]))

    def test_dependency_route_requires_all_query_arguments(self):
        with (
            patch.object(app, "DB_CONNECTIONS", {"MAPP": "postgresql://reader"}),
            patch.object(app, "DERIVED", None),
            patch.object(
                app,
                "read_workspace",
                return_value=(b"{}", {"dbs": "MAPP", "locale": {}}, "revision"),
            ),
        ):
            handler, responses = self.handler("/api/dependencies?alias=MAPP&schema=leeds")
        handler.do_GET()
        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        self.assertEqual("dependencies.invalid_query", responses[0][1]["code"])


class DerivedMapExtentRouteTests(unittest.TestCase):
    @staticmethod
    def handler(path: str, payload: dict | None = None):
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: "token:test"
        handler._authentication = {"scopes": ["inspect", "derive"]}
        handler._payload = lambda: {**(payload or {})}
        handler._remote = lambda: "127.0.0.1"
        handler._json = lambda status, body: responses.append((status, body))
        handler.send_error = lambda status: responses.append((status, {}))
        return handler, responses

    @staticmethod
    def workspace():
        return {
            "locale": {
                "view": {"lng": -1.5, "lat": 53.8, "z": 11},
            },
            "locales": {
                "city-centre": {
                    "view": {"lng": -1.54, "lat": 53.81},
                },
            },
        }

    @staticmethod
    def catalog():
        return {"assets": [{
            "status": "ready",
            "generated": {"binding": {
                "adapter": "postgresql",
                "schema": "leeds",
                "relation": "roads",
            }},
        }]}

    @staticmethod
    def h3_catalog():
        return {"assets": [{
            "id": "asset-census",
            "version": 7,
            "status": "ready",
            "generated": {
                "qualifiedName": "census.areas",
                "binding": {
                    "adapter": "postgresql",
                    "alias": "MAPP",
                    "schema": "census",
                    "relation": "areas",
                },
                "fields": [
                    {
                        "id": "field-area-id",
                        "name": "area_id",
                        "type": "text",
                        "nullable": False,
                        "primaryKey": True,
                        "unique": True,
                    },
                    {
                        "id": "field-geometry",
                        "name": "source_geom",
                        "type": "geometry(MultiPolygon,4326)",
                        "nullable": False,
                        "geometryType": "MULTIPOLYGON",
                        "srid": 4326,
                    },
                    {
                        "id": "field-population",
                        "name": "population",
                        "type": "bigint",
                        "nullable": False,
                    },
                ],
            },
        }]}

    @staticmethod
    def h3_recipe():
        return {
            "name": "population_h3_r9",
            "kind": "materialized",
            "source": {
                "assetId": "asset-census",
                "relation": "census.areas",
                "idColumn": "area_id",
                "geometryColumn": "source_geom",
            },
            "resolution": 9,
            "measures": [{
                "sourceColumn": "population",
                "outputColumn": "population_estimate",
                "nullHandling": "zero",
            }],
            "spatialScope": {
                "type": "workspace-map-extent",
                "locale": "city-centre",
            },
        }

    @staticmethod
    def materialization_probe(*, estimated_bytes=2 * 1024 ** 3):
        return {
            "method": "postgresql-explain",
            "estimatedRows": 1_000_000,
            "planRowWidthBytes": 2048,
            "rowOverheadBytes": 32,
            "safetyMultiplier": 1.2,
            "estimatedBytes": estimated_bytes,
            "maxEstimatedBytes": 1024 ** 3,
        }

    def test_capabilities_advertise_background_job_capacity(self):
        handler, responses = self.handler("/api/derived-layers/capabilities")
        derived = Mock()
        derived.capabilities.return_value = {
            "configured": True,
            "schema": "derived_layers",
            "kinds": ["view", "materialized"],
        }
        with patch.object(app, "DERIVED", derived):
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        capacity = responses[0][1]["backgroundJobs"]
        self.assertEqual(app.DERIVED_MAX_BACKGROUND_JOBS, capacity["maxActiveJobs"])
        self.assertGreaterEqual(capacity["activeJobs"], 0)

    def test_unconfigured_capabilities_include_safe_h3_diagnostics(self):
        handler, responses = self.handler("/api/derived-layers/capabilities")

        with patch.object(app, "DERIVED", None):
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        capabilities = responses[0][1]
        self.assertFalse(capabilities["h3Available"])
        self.assertEqual({
            "version": "1",
            "method": "postgresql-explain-bounded-generator-pairs",
            "maxNestedLoopPairRows": 100_000_000,
            "reasonCodes": ["nested_loop_pair_work"],
        }, capabilities["queryPlanning"])
        self.assertEqual(
            {
                "method": "postgresql-catalog-and-execution",
                "ready": False,
                "code": "derived_layer.h3_not_ready",
                "stage": "extension-discovery",
                "reasons": [{
                    "code": "derived_layers_unconfigured",
                    "message": (
                        "H3 readiness cannot be checked because derived "
                        "layers are not configured."
                    ),
                    "suggestedAction": (
                        "Configure the derived-layer database, then retry "
                        "the readiness check."
                    ),
                }],
            },
            capabilities["h3Readiness"],
        )
        recipe = capabilities["recipes"]["areaWeightedH3"]
        self.assertFalse(recipe["available"])
        self.assertFalse(recipe["mutationAppliedByPlan"])
        self.assertEqual(
            "/api/derived-layers/recipes/area-weighted-h3/plan",
            recipe["planPath"],
        )

    def test_capabilities_database_error_does_not_expose_raw_context(self):
        class DetailedProgrammingError(app.psycopg.ProgrammingError):
            @property
            def sqlstate(self):
                return "08006"

            @property
            def diag(self):
                return type("Diagnostics", (), {
                    "message_primary": "database connection failed",
                })()

        handler, responses = self.handler(
            "/api/derived-layers/capabilities"
        )
        derived = Mock()
        derived.capabilities.side_effect = DetailedProgrammingError(
            "SECRET host and connection string",
        )
        with patch.object(app, "DERIVED", derived):
            handler.do_GET()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_GATEWAY, status)
        self.assertEqual(
            "derived_layer.capabilities_unavailable",
            body["code"],
        )
        self.assertEqual("08006", body["technicalDetail"]["sqlstate"])
        self.assertNotIn("SECRET", repr(body))

    def test_show_missing_layer_has_structured_next_action(self):
        handler, responses = self.handler(
            "/api/derived-layers/missing_layer"
        )
        derived = Mock()
        derived.get.side_effect = FileNotFoundError("missing_layer")
        with patch.object(app, "DERIVED", derived):
            handler.do_GET()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.NOT_FOUND, status)
        self.assertEqual("derived_layer.not_found", body["code"])
        self.assertEqual("missing_layer", body["name"])
        self.assertIn("List derived layers", body["suggestedAction"])
        self.assertNotIn("stateUnchanged", body)

    @staticmethod
    def query_plan_probe():
        return {
            "method": "postgresql-explain",
            "estimatedTotalCost": 25_000_000,
            "estimatedFinalRows": 1_000,
            "maxIntermediateRows": 80_000_000,
            "maxIntermediateBytes": 12 * 1024 ** 3,
            "planNodeCount": 12,
            "planDepth": 5,
            "plannedWorkers": 0,
            "recursivePlan": False,
            "h3Expansion": {
                "polygonToCellsCalls": 1,
                "resolutions": [12],
                "estimatedScopeCells": 20_000_000,
            },
            "limits": {
                "maxTotalCost": 10_000_000,
                "maxIntermediateRows": 50_000_000,
                "maxIntermediateBytes": 8 * 1024 ** 3,
            },
        }

    @staticmethod
    def definition(**updates):
        payload = {
            "name": "roads_visible",
            "kind": "view",
            "query": "SELECT id, geom_3857 FROM leeds.roads",
            "sources": ["leeds.roads"],
            "idColumn": "id",
            "geometryColumn": "geom_3857",
        }
        payload.update(updates)
        return payload

    def test_preview_uses_effective_named_locale_without_derived_database(self):
        handler, responses = self.handler(
            "/api/derived-layers/map-extent?locale=city-centre"
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", None):
            handler.do_GET()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        scope = body["spatialScope"]
        self.assertEqual("city-centre", scope["locale"])
        self.assertEqual(
            {"lng": -1.54, "lat": 53.81, "z": 11},
            scope["sourceView"],
        )

    def test_preview_reports_structured_unavailable_view(self):
        handler, responses = self.handler(
            "/api/derived-layers/map-extent"
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", {"locale": {}}, "revision"),
        ), patch.object(app, "DERIVED", None):
            handler.do_GET()

        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        self.assertEqual(
            "derived_layer.map_extent_unavailable",
            responses[0][1]["code"],
        )

    def test_area_weighted_h3_recipe_is_resolved_and_preflighted_without_mutation(self):
        handler, responses = self.handler(
            "/api/derived-layers/recipes/area-weighted-h3/plan",
            self.h3_recipe(),
        )
        handler._authentication["scopes"].append("semantic:inspect")
        handler._semantic_request = Mock(return_value={
            "asset": self.h3_catalog()["assets"][0],
        })
        derived = Mock()
        derived.preflight_definition.return_value = {
            "queryPlanProbe": {"method": "postgresql-explain"},
            "queryPlanningProbe": {"method": "bounded-pairs"},
            "materializationProbe": {"estimatedBytes": 1024},
        }

        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertFalse(body["mutationApplied"])
        plan = body["recipePlan"]
        self.assertEqual("area-weighted-h3", plan["recipe"]["name"])
        self.assertEqual(
            {
                "type": "workspace-map-extent",
                "locale": "city-centre",
            },
            plan["createRequest"]["spatialScope"],
        )
        self.assertIn("envelopes", plan["resolvedSpatialScope"])
        self.assertEqual(
            {"method": "postgresql-explain"},
            plan["queryPlanProbe"],
        )
        preflight_request = derived.preflight_definition.call_args.args[0]
        self.assertEqual(
            plan["resolvedSpatialScope"],
            preflight_request["spatialScope"],
        )
        self.assertEqual(
            plan["createRequest"]["query"],
            preflight_request["query"],
        )
        derived.create.assert_not_called()
        handler._semantic_request.assert_called_once_with(
            "token:test", "/v1/assets/asset-census"
        )

    def test_area_weighted_h3_recipe_requires_semantic_inspection_scope(self):
        handler, responses = self.handler(
            "/api/derived-layers/recipes/area-weighted-h3/plan",
            self.h3_recipe(),
        )
        handler._semantic_request = Mock()

        with patch.object(app, "DERIVED", Mock()):
            handler.do_POST()

        self.assertEqual(HTTPStatus.FORBIDDEN, responses[0][0])
        self.assertEqual("semantic:inspect", responses[0][1]["requiredScope"])
        handler._semantic_request.assert_not_called()

    def test_area_weighted_h3_recipe_reports_missing_semantic_source_without_mutation(self):
        request = self.h3_recipe()
        request["source"]["assetId"] = "missing-asset"
        handler, responses = self.handler(
            "/api/derived-layers/recipes/area-weighted-h3/plan",
            request,
        )
        handler._authentication["scopes"].append("semantic:inspect")
        handler._semantic_request = Mock(return_value={
            "asset": self.h3_catalog()["assets"][0],
        })
        derived = Mock()

        with patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertEqual("derived_layer.source_profile_required", body["code"])
        self.assertEqual("plan-area-weighted-h3", body["operation"])
        self.assertTrue(body["stateUnchanged"])
        self.assertEqual(
            "No derived-layer change was applied.",
            body["safeState"],
        )
        derived.preflight_definition.assert_not_called()
        derived.create.assert_not_called()

    def test_area_weighted_h3_recipe_wraps_semantic_404_as_safe_preflight(self):
        handler, responses = self.handler(
            "/api/derived-layers/recipes/area-weighted-h3/plan",
            self.h3_recipe(),
        )
        handler._authentication["scopes"].append("semantic:inspect")
        handler._semantic_request = Mock(side_effect=app.SemanticClientError(
            "asset not found",
            status=HTTPStatus.NOT_FOUND,
            payload={"code": "asset_not_found"},
        ))
        derived = Mock()

        with patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.NOT_FOUND, status)
        self.assertEqual("derived_layer.source_profile_required", body["code"])
        self.assertEqual("plan-area-weighted-h3", body["operation"])
        self.assertEqual("preflight", body["failurePhase"])
        self.assertTrue(body["stateUnchanged"])
        self.assertFalse(body["mutationApplied"])
        derived.preflight_definition.assert_not_called()
        derived.create.assert_not_called()

    def test_synchronous_create_resolves_scope_before_semantics_and_store(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(spatialScope={
                "type": "workspace-map-extent",
                "locale": "city-centre",
            }),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()

        def create(payload, actor):
            return {
                **payload,
                "semanticProfile": {
                    "assetId": "asset",
                    "generation": 1,
                    "status": "registering",
                    "revision": None,
                },
            }

        derived.create.side_effect = create
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived), patch.object(
            app, "schedule_semantic_outbox"
        ), patch.object(app, "CONTROL", Mock()):
            handler.do_POST()

        self.assertEqual(HTTPStatus.CREATED, responses[0][0])
        stored = derived.create.call_args.args[0]
        self.assertEqual("city-centre", stored["spatialScope"]["locale"])
        self.assertIn("envelopes", stored["spatialScope"])
        self.assertNotEqual(
            {"type": "workspace-map-extent", "locale": "city-centre"},
            stored["spatialScope"],
        )
        derived.preflight_definition.assert_called_once_with(stored)

    def test_create_defaults_to_the_workspace_map_extent(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        derived.create.side_effect = lambda payload, _actor: payload
        workspace = self.workspace()
        workspace["locale"]["extent"] = {
            "north": 54,
            "east": -1.2,
            "south": 53.65,
            "west": -1.85,
        }
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", workspace, "revision"),
        ), patch.object(app, "DERIVED", derived), patch.object(
            app, "schedule_semantic_outbox"
        ), patch.object(app, "CONTROL", Mock()):
            handler.do_POST()

        self.assertEqual(HTTPStatus.CREATED, responses[0][0])
        stored = derived.create.call_args.args[0]
        self.assertEqual("locale", stored["spatialScope"]["locale"])
        self.assertEqual(10, stored["spatialScope"]["scopeZoom"])
        self.assertEqual(
            [{"west": -1.85, "south": 53.65, "east": -1.2, "north": 54.0}],
            stored["spatialScope"]["envelopes"],
        )
        derived.preflight_definition.assert_called_once_with(stored)

    def test_oversized_materialization_is_blocked_before_background_create(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(kind="materialized", background=True),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        probe = self.materialization_probe()
        derived.preflight_definition.side_effect = (
            app.DerivedLayerMaterializationTooLarge("roads_visible", probe)
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived), patch.object(
            app, "start_derived_background"
        ) as start:
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("derived_layer.materialization_too_large", body["code"])
        self.assertTrue(body["blocked"])
        self.assertEqual("view", body["recommendedKind"])
        self.assertEqual(probe, body["probe"])
        self.assertNotIn("queryPlanningProbe", body)
        self.assertEqual("estimate", body["probeStage"])
        self.assertNotIn("rolledBack", body)
        self.assertEqual("create", body["operation"])
        self.assertEqual("No derived layer was created.", body["safeState"])
        start.assert_not_called()
        derived.create.assert_not_called()

    def test_expensive_view_query_is_blocked_before_background_create(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(kind="view", background=True),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        probe = self.query_plan_probe()
        reasons = [
            {"code": "total_cost", "message": "Planner cost exceeds the limit."},
            {
                "code": "intermediate_bytes",
                "message": "An intermediate plan node exceeds the byte limit.",
            },
        ]
        derived.preflight_definition.side_effect = (
            app.DerivedLayerQueryTooExpensive(
                "roads_visible",
                probe,
                reasons,
            )
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived), patch.object(
            app, "start_derived_background"
        ) as start:
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("derived_layer.query_too_expensive", body["code"])
        self.assertEqual("compute", body["category"])
        self.assertEqual("create", body["operation"])
        self.assertTrue(body["stateUnchanged"])
        self.assertEqual("No derived layer was created.", body["safeState"])
        self.assertTrue(body["blocked"])
        self.assertEqual(probe, body["probe"])
        self.assertNotIn("queryPlanningProbe", body)
        self.assertEqual(
            [reason["code"] for reason in reasons],
            [reason["code"] for reason in body["reasons"]],
        )
        self.assertTrue(all(
            reason.get("suggestedAction") for reason in body["reasons"]
        ))
        self.assertNotIn("recommendedKind", body)
        self.assertIn("ordinary view", body["suggestedAction"])
        start.assert_not_called()
        derived.create.assert_not_called()

    def test_nested_loop_pair_rejection_preserves_planning_guidance(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(kind="view", background=True),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        legacy_probe = self.query_plan_probe()
        planning_probe = {
            "version": "1",
            "method": "postgresql-explain-bounded-generator-pairs",
            "maxProvenGeneratedRows": 240_189,
            "nestedLoopCount": 1,
            "maxEstimatedNestedLoopPairRows": 42_898_956_345,
            "maxAllowedNestedLoopPairRows": 100_000_000,
        }
        derived.preflight_definition.side_effect = (
            app.DerivedLayerQueryTooExpensive(
                "roads_visible",
                legacy_probe,
                [{
                    "code": "nested_loop_pair_work",
                    "message": "The nested-loop pair budget is exceeded.",
                }],
                query_planning_probe=planning_probe,
            )
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived), patch.object(
            app, "start_derived_background"
        ) as start:
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("compute", body["category"])
        self.assertEqual(legacy_probe, body["probe"])
        self.assertEqual(planning_probe, body["queryPlanningProbe"])
        self.assertEqual(
            "nested_loop_pair_work",
            body["reasons"][0]["code"],
        )
        self.assertIn(
            "selective parameterized or indexed input",
            body["reasons"][0]["suggestedAction"],
        )
        start.assert_not_called()
        derived.create.assert_not_called()

    def test_invalid_query_has_a_syntax_specific_error(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(background=True),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        derived.preflight_definition.side_effect = (
            app.DerivedLayerQueryTooExpensive(
                "roads_visible",
                {"method": "postgresql-ast-guard"},
                [{
                    "code": "invalid_sql",
                    "message": "PostgreSQL could not parse the query.",
                }],
            )
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertEqual("derived_layer.query_invalid", body["code"])
        self.assertEqual("invalid", body["category"])
        self.assertIn("is invalid", body["userMessage"])
        self.assertNotIn("too expensive", body["userMessage"])
        self.assertNotIn("recommendedKind", body)

    def test_sql_contract_rejection_uses_the_same_invalid_query_code(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(query=(
                "SELECT id, geom_3857 FROM leeds.roads;"
            )),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", Mock()):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertEqual("derived_layer.query_invalid", body["code"])
        self.assertEqual("invalid", body["category"])
        self.assertEqual("invalid_sql", body["reasons"][0]["code"])
        self.assertTrue(body["stateUnchanged"])

    def test_forbidden_sql_keyword_uses_policy_code_and_status(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(query=(
                "WITH changed AS (UPDATE leeds.roads SET id = id RETURNING *) "
                "SELECT id, geom_3857 FROM changed"
            )),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", Mock()):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        self.assertEqual("derived_layer.query_not_allowed", body["code"])
        self.assertEqual("policy", body["category"])
        self.assertEqual("prohibited_sql", body["reasons"][0]["code"])
        self.assertNotIn("recommendedKind", body)

    def test_security_policy_query_has_policy_specific_guidance(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(background=True),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        derived.preflight_definition.side_effect = (
            app.DerivedLayerQueryTooExpensive(
                "roads_visible",
                {"method": "postgresql-catalog-guard"},
                [{
                    "code": "security_definer_routine",
                    "message": "Resolved routine private.wrapper is SECURITY DEFINER.",
                }],
            )
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        self.assertEqual("derived_layer.query_not_allowed", body["code"])
        self.assertEqual("policy", body["category"])
        self.assertIn("not allowed", body["userMessage"])
        self.assertIn("Remove the routine", body["suggestedAction"])
        self.assertIn(
            "approved pg_catalog",
            body["reasons"][0]["suggestedAction"],
        )
        self.assertNotIn("recommendedKind", body)

    def test_source_mismatch_identifies_missing_and_unused_declarations(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(background=True),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        derived.preflight_definition.side_effect = (
            app.DerivedLayerSourceMismatchError(
                ["leeds.roads", "leeds.unused"],
                ["leeds.roads", "leeds.boundaries"],
            )
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        self.assertEqual("derived_layer.source_mismatch", body["code"])
        self.assertEqual(["leeds.boundaries"], body["missingSources"])
        self.assertEqual(["leeds.unused"], body["extraSources"])
        self.assertIn("Add every relation", body["suggestedAction"])
        self.assertTrue(body["stateUnchanged"])

    def test_missing_semantic_source_profile_names_the_cli_remediation(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(),
        )
        handler._semantic_request = Mock(return_value={"assets": []})
        derived = Mock()
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertEqual(
            "derived_layer.source_profile_required",
            body["code"],
        )
        self.assertIn("semantic source sync", body["suggestedAction"])
        self.assertIn("leeds.roads", body["userMessage"])
        self.assertTrue(body["stateUnchanged"])
        derived.preflight_definition.assert_not_called()

    def test_database_error_exposes_only_sanitized_diagnostics(self):
        class DetailedProgrammingError(app.psycopg.ProgrammingError):
            @property
            def sqlstate(self):
                return "42703"

            @property
            def diag(self):
                return type("Diagnostics", (), {
                    "message_primary": "column score does not exist",
                })()

        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        derived.preflight_definition.side_effect = DetailedProgrammingError(
            "SECRET SELECT text and server context",
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        self.assertEqual("derived_layer.database_error", body["code"])
        self.assertEqual(
            {
                "sqlstate": "42703",
                "message": "column score does not exist",
            },
            body["technicalDetail"],
        )
        self.assertNotIn("SECRET SELECT", repr(body))
        self.assertTrue(body["stateUnchanged"])
        self.assertEqual("No derived layer was created.", body["safeState"])
        self.assertEqual("preflight", body["failurePhase"])
        self.assertNotIn("rolledBack", body)
        self.assertNotIn("indeterminate", body)
        self.assertNotIn("retryable", body)
        self.assertNotIn("contentionScope", body)

    def test_synchronous_create_contention_is_retryable_after_rollback(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        failure = app.DerivedLayerContentionError("derived-mutation")
        failure.failure_phase = "database-transaction"
        failure.rolled_back = True
        derived.create.side_effect = failure
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("derived_layer.database_contention", body["code"])
        self.assertEqual("contention", body["category"])
        self.assertEqual("derived-mutation", body["contentionScope"])
        self.assertTrue(body["retryable"])
        self.assertTrue(body["stateUnchanged"])
        self.assertTrue(body["rolledBack"])
        self.assertEqual("No derived layer was created.", body["safeState"])
        self.assertNotIn("Correct the query", body["suggestedAction"])

    def test_unexpected_preflight_failure_reports_unchanged_state(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        derived.preflight_definition.side_effect = RuntimeError(
            "SECRET internal path",
        )
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.INTERNAL_SERVER_ERROR, status)
        self.assertEqual("derived_layer.operation_failed", body["code"])
        self.assertNotIn("SECRET", repr(body))
        self.assertTrue(body["stateUnchanged"])
        self.assertEqual("No derived layer was created.", body["safeState"])
        self.assertEqual("preflight", body["failurePhase"])
        self.assertNotIn("indeterminate", body)

    def test_refresh_confirmation_error_names_the_unchanged_state(self):
        handler, responses = self.handler(
            "/api/derived-layers/roads_visible/refresh",
            {"confirmed": False},
        )
        derived = Mock()
        with patch.object(app, "DERIVED", derived):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("derived_layer.confirmation_required", body["code"])
        self.assertEqual("refresh", body["operation"])
        self.assertEqual(
            "The existing materialized data remains unchanged.",
            body["safeState"],
        )
        derived.preflight_refresh.assert_not_called()

    def test_oversized_refresh_is_blocked_before_background_start(self):
        handler, responses = self.handler(
            "/api/derived-layers/roads_visible/refresh",
            {"confirmed": True, "background": True},
        )
        derived = Mock()
        probe = self.materialization_probe()
        derived.preflight_refresh.side_effect = (
            app.DerivedLayerMaterializationTooLarge("roads_visible", probe)
        )
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "start_derived_background"
        ) as start:
            handler.do_POST()

        self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
        body = responses[0][1]
        self.assertEqual(probe, body["probe"])
        self.assertEqual("refresh", body["operation"])
        self.assertEqual("estimate", body["probeStage"])
        self.assertNotIn("rolledBack", body)
        self.assertEqual(
            "The existing materialized data remains unchanged.",
            body["safeState"],
        )
        self.assertIn("Convert this materialized layer", body["suggestedAction"])
        start.assert_not_called()
        derived.refresh.assert_not_called()

    def test_missing_refresh_target_returns_a_recoverable_not_found_error(self):
        handler, responses = self.handler(
            "/api/derived-layers/missing_layer/refresh",
            {"confirmed": True, "background": True},
        )
        derived = Mock()
        derived.preflight_refresh.side_effect = FileNotFoundError(
            "missing_layer"
        )
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "start_derived_background",
        ) as start:
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.NOT_FOUND, status)
        self.assertEqual("derived_layer.not_found", body["code"])
        self.assertEqual("missing_layer", body["name"])
        self.assertEqual("refresh", body["operation"])
        self.assertIn("List derived layers", body["suggestedAction"])
        start.assert_not_called()

    def test_background_capacity_returns_retryable_structured_rejection(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(background=True),
        )
        handler._semantic_request = Mock(return_value=self.catalog())
        derived = Mock()
        capacity = app.DerivedLayerBackgroundCapacityError(1, 1)
        with patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", self.workspace(), "revision"),
        ), patch.object(app, "DERIVED", derived), patch.object(
            app,
            "start_derived_background",
            side_effect=capacity,
        ):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, status)
        self.assertEqual("derived_layer.background_capacity", body["code"])
        self.assertTrue(body["blocked"])
        self.assertTrue(body["retryable"])
        self.assertEqual("create", body["operation"])
        self.assertTrue(body["stateUnchanged"])
        self.assertEqual("No derived layer was created.", body["safeState"])
        self.assertEqual(1, body["activeJobs"])
        self.assertEqual(1, body["maxActiveJobs"])
        derived.create.assert_not_called()

    def test_create_rejects_client_supplied_extent_bounds(self):
        handler, responses = self.handler(
            "/api/derived-layers",
            self.definition(spatialScope={
                "type": "workspace-map-extent",
                "envelopes": [{
                    "west": -2,
                    "south": 53,
                    "east": -1,
                    "north": 54,
                }],
            }),
        )
        derived = Mock()
        with patch.object(app, "DERIVED", derived):
            handler.do_POST()

        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        self.assertIn("Unknown spatialScope properties", responses[0][1]["error"])
        derived.create.assert_not_called()

    def test_background_create_and_replace_receive_resolved_scope(self):
        cases = (
            (
                "/api/derived-layers",
                self.definition(
                    background=True,
                    spatialScope={"type": "workspace-map-extent"},
                ),
                "create",
                None,
            ),
            (
                "/api/derived-layers/roads_visible/replace",
                self.definition(
                    confirmed=True,
                    background=True,
                    spatialScope={"type": "workspace-map-extent"},
                ),
                "replace",
                "roads_visible",
            ),
        )
        for path, payload, action, name in cases:
            with self.subTest(action=action):
                handler, responses = self.handler(path, payload)
                handler._semantic_request = Mock(return_value=self.catalog())
                with patch.object(
                    app,
                    "read_workspace",
                    return_value=(b"{}", self.workspace(), "revision"),
                ), patch.object(app, "DERIVED", Mock()), patch.object(
                    app,
                    "start_derived_background",
                    return_value={"id": "operation-1"},
                ) as start:
                    handler.do_POST()

                self.assertEqual(HTTPStatus.ACCEPTED, responses[0][0])
                args = start.call_args.args
                self.assertEqual(action, args[0])
                self.assertEqual("workspace-map-extent", args[1]["spatialScope"]["type"])
                self.assertIn("envelopes", args[1]["spatialScope"])
                if name is not None:
                    self.assertEqual(name, args[4])


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

    def test_server_catalog_offers_only_semantically_ready_derived_layers(self):
        discovered = [
            {"dbs": "MAPP", "schema": "etl", "table": "places"},
            {
                "dbs": "MAPP",
                "schema": "derived_layers",
                "table": "ready_places",
            },
            {
                "dbs": "MAPP",
                "schema": "derived_layers",
                "table": "pending_places",
            },
        ]
        derived = Mock()
        derived.list.return_value = [
            {
                "name": "ready_places",
                "kind": "view",
                "semanticProfile": {
                    "assetId": "1",
                    "generation": 1,
                    "status": "ready",
                    "revision": "4",
                },
            },
            {
                "name": "pending_places",
                "kind": "view",
                "semanticProfile": {
                    "assetId": "2",
                    "generation": 1,
                    "status": "registering",
                    "revision": None,
                },
            },
        ]

        with patch.object(app, "discover", return_value=discovered), patch.object(
            app, "DERIVED", derived
        ):
            result = app.discover_catalog()

        self.assertEqual(discovered[:2], result)

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
        self.assertIn("c.relkind IN ('r', 'p', 'v', 'm', 'f')", discovery_query)
        self.assertIn("postgis_typmod_type(a.atttypmod)", discovery_query)
        self.assertNotIn("JOIN geometry_columns", discovery_query)


class DerivedBackgroundOperationTests(unittest.TestCase):
    @staticmethod
    def failure_state(
        exc,
        *,
        phase="database-transaction",
        rolled_back=True,
    ):
        exc.failure_phase = phase
        exc.rolled_back = rolled_back
        return exc

    def test_background_job_admission_is_bounded(self):
        reserved = 0
        try:
            for _ in range(app.DERIVED_MAX_BACKGROUND_JOBS):
                app.reserve_derived_background_job()
                reserved += 1
            capacity = app.derived_background_capacity()
            self.assertEqual(
                app.DERIVED_MAX_BACKGROUND_JOBS,
                capacity["activeJobs"],
            )
            self.assertEqual(app.DERIVED_MAX_BACKGROUND_JOBS, capacity["maxActiveJobs"])
            with self.assertRaises(app.DerivedLayerBackgroundCapacityError):
                app.reserve_derived_background_job()
        finally:
            for _ in range(reserved):
                app.release_derived_background_job()

        self.assertEqual(0, app.derived_background_capacity()["activeJobs"])

    def test_started_worker_releases_its_admission_slot(self):
        control = Mock()
        control.create_operation.return_value = {
            "id": "a" * 32,
            "status": "running",
        }

        def immediate_thread(*, target, args, **_kwargs):
            thread = Mock()
            thread.start.side_effect = lambda: target(*args)
            return thread

        with patch.object(app, "CONTROL", control), patch.object(
            app.threading,
            "Thread",
            side_effect=immediate_thread,
        ), patch.object(app, "run_derived_background") as run:
            operation = app.start_derived_background(
                "create",
                {"name": "safe_places"},
                "admin",
                "127.0.0.1",
            )

        self.assertEqual("a" * 32, operation["id"])
        run.assert_called_once()
        self.assertEqual(0, app.derived_background_capacity()["activeJobs"])

    def test_worker_start_failure_persists_preflight_state(self):
        with tempfile.TemporaryDirectory() as directory:
            control = ControlStore(Path(directory))
            control.initialize("correct horse battery staple", "instance")
            thread = Mock()
            thread.start.side_effect = RuntimeError("thread unavailable")

            with patch.object(app, "CONTROL", control), patch.object(
                app.threading,
                "Thread",
                return_value=thread,
            ):
                with self.assertRaisesRegex(RuntimeError, "thread unavailable"):
                    app.start_derived_background(
                        "create",
                        {"name": "safe_places"},
                        "admin",
                        "127.0.0.1",
                    )

            operation_path = next(control.operations.glob("*.json"))
            stored = control.read_operation(operation_path.stem)
            self.assertEqual("failed", stored["status"])
            error = stored["error"]
            self.assertEqual(
                "derived_layer.background_start_failed",
                error["code"],
            )
            self.assertEqual("preflight", error["failurePhase"])
            self.assertTrue(error["stateUnchanged"])
            self.assertEqual("No derived layer was created.", error["safeState"])
            self.assertNotIn("indeterminate", error)
            self.assertFalse(error["suggestedAction"].startswith("Inspect"))
            self.assertEqual(0, app.derived_background_capacity()["activeJobs"])

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

    def test_confirmed_database_rollback_records_cancelled(self):
        derived = Mock()
        cancellation = app.DerivedLayerCancellation()

        def create(_payload, _actor, *, cancellation):
            cancellation.request()
            failure = app.DerivedLayerCancellationRequested("cancelled")
            failure.failure_phase = "database-transaction"
            failure.rolled_back = True
            raise failure

        derived.create.side_effect = create
        control = Mock()
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ):
            app.run_derived_background(
                "c" * 32,
                "create",
                {"name": "slow_places"},
                "admin",
                "127.0.0.1",
                cancellation=cancellation,
            )

        call = control.finish_operation.call_args
        self.assertEqual("cancelled", call.kwargs["status"])
        error = call.kwargs["error"]
        self.assertEqual("derived_layer.cancelled", error["code"])
        self.assertTrue(error["stateUnchanged"])
        self.assertTrue(error["rolledBack"])
        self.assertEqual("No derived layer was created.", error["safeState"])

    def test_postgresql_query_cancellation_records_cancelled_after_rollback(self):
        class CancelledQuery(app.psycopg.OperationalError):
            @property
            def sqlstate(self):
                return "57014"

        cancellation = app.DerivedLayerCancellation()
        self.assertTrue(cancellation.request())
        derived = Mock()
        derived.create.side_effect = app.DerivedLayerDatabaseOperationError(
            CancelledQuery("query cancelled"),
            failure_phase="database-transaction",
            state_unchanged=True,
            rolled_back=True,
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ):
            app.run_derived_background(
                "d" * 32,
                "create",
                {"name": "slow_places"},
                "admin",
                "127.0.0.1",
                cancellation=cancellation,
            )

        self.assertEqual(
            "cancelled", control.finish_operation.call_args.kwargs["status"],
        )

    def test_cancellation_during_commit_remains_indeterminate(self):
        cancellation = app.DerivedLayerCancellation()
        self.assertTrue(cancellation.request())
        derived = Mock()
        derived.create.side_effect = app.DerivedLayerDatabaseOperationError(
            app.psycopg.OperationalError("commit interrupted"),
            failure_phase="transaction-commit",
            state_unchanged=False,
            indeterminate=True,
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ):
            app.run_derived_background(
                "e" * 32,
                "create",
                {"name": "slow_places"},
                "admin",
                "127.0.0.1",
                cancellation=cancellation,
            )

        call = control.finish_operation.call_args
        self.assertEqual("indeterminate", call.kwargs["status"])
        self.assertEqual("transaction-commit", call.kwargs["error"]["failurePhase"])

    def test_cancel_route_requests_cancellation_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            control = ControlStore(Path(directory))
            control.initialize("correct horse battery staple", "instance")
            operation = control.create_operation(
                "derived-layer.create",
                "token:test",
                {"name": "slow_places", "action": "create"},
            )
            cancellation = app.DerivedLayerCancellation()
            handler, responses = DerivedMapExtentRouteTests.handler(
                f"/api/operations/{operation['id']}/cancel",
                {"confirmed": True},
            )

            with patch.object(app, "CONTROL", control), patch.object(
                app,
                "DERIVED_BACKGROUND_CANCELLATIONS",
                {operation["id"]: cancellation},
            ):
                handler.do_POST()

            self.assertEqual(HTTPStatus.ACCEPTED, responses[0][0])
            self.assertTrue(cancellation.requested)
            self.assertEqual(
                "cancelling", responses[0][1]["operation"]["status"],
            )
            self.assertEqual(
                "cancelling",
                control.read_operation(operation["id"])["status"],
            )

    def test_cancel_route_rejects_request_after_database_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            control = ControlStore(Path(directory))
            control.initialize("correct horse battery staple", "instance")
            operation = control.create_operation(
                "derived-layer.create", "token:test",
            )
            cancellation = app.DerivedLayerCancellation()
            cancellation.finish_database()
            handler, responses = DerivedMapExtentRouteTests.handler(
                f"/api/operations/{operation['id']}/cancel",
                {"confirmed": True},
            )

            with patch.object(app, "CONTROL", control), patch.object(
                app,
                "DERIVED_BACKGROUND_CANCELLATIONS",
                {operation["id"]: cancellation},
            ):
                handler.do_POST()

            self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
            self.assertEqual("operation.cancel_too_late", responses[0][1]["code"])
            self.assertEqual("running", control.read_operation(operation["id"])["status"])

    def test_post_commit_reporting_failure_is_indeterminate(self):
        derived = Mock()
        derived.create.return_value = {
            "name": "slow_places",
            "kind": "materialized",
            "sources": ["etl.places"],
        }
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ), patch.object(
            app,
            "schedule_semantic_outbox",
            side_effect=RuntimeError("private reporting failure"),
        ):
            app.run_derived_background(
                "9" * 32,
                "create",
                {"name": "slow_places"},
                "admin",
                "127.0.0.1",
            )

        call = control.finish_operation.call_args
        self.assertEqual("indeterminate", call.kwargs["status"])
        error = call.kwargs["error"]
        self.assertEqual("derived_layer.operation_failed", error["code"])
        self.assertTrue(error["indeterminate"])
        self.assertEqual("result-reporting", error["failurePhase"])
        self.assertNotIn("stateUnchanged", error)
        self.assertNotIn("private reporting failure", repr(error))

    def test_commit_failure_is_indeterminate(self):
        failure = app.psycopg.OperationalError("connection lost during commit")
        derived = Mock()
        derived.refresh.side_effect = app.DerivedLayerDatabaseOperationError(
            failure,
            failure_phase="transaction-commit",
            state_unchanged=False,
            indeterminate=True,
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ):
            app.run_derived_background(
                "8" * 32,
                "refresh",
                {},
                "admin",
                "127.0.0.1",
                "slow_places",
            )

        call = control.finish_operation.call_args
        self.assertEqual("indeterminate", call.kwargs["status"])
        error = call.kwargs["error"]
        self.assertEqual("derived_layer.database_error", error["code"])
        self.assertTrue(error["indeterminate"])
        self.assertEqual("transaction-commit", error["failurePhase"])
        self.assertNotIn("stateUnchanged", error)
        self.assertNotIn("rolledBack", error)

    def test_unexpected_failure_is_indeterminate_in_status_polling(self):
        derived = Mock()
        derived.refresh.side_effect = RuntimeError(
            "secret SQL and internal connection path"
        )
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
        self.assertEqual("indeterminate", call.kwargs["status"])
        error = call.kwargs["error"]
        self.assertEqual("derived_layer.operation_failed", error["code"])
        self.assertTrue(error["indeterminate"])
        self.assertEqual("transaction-rollback", error["failurePhase"])
        self.assertNotIn("secret SQL", error["message"])
        self.assertNotIn("stateUnchanged", error)
        self.assertNotIn("safeState", error)

    def test_background_database_failure_uses_sanitized_diagnostics(self):
        class DetailedProgrammingError(app.psycopg.ProgrammingError):
            @property
            def sqlstate(self):
                return "57014"

            @property
            def diag(self):
                return type("Diagnostics", (), {
                    "message_primary": "canceling statement due to timeout",
                })()

        derived = Mock()
        failure = DetailedProgrammingError("SECRET SQL query context")
        derived.refresh.side_effect = app.DerivedLayerDatabaseOperationError(
            failure,
            failure_phase="database-transaction",
            state_unchanged=True,
            rolled_back=True,
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ):
            app.run_derived_background(
                "1" * 32,
                "refresh",
                {},
                "admin",
                "127.0.0.1",
                "slow_places",
            )

        call = control.finish_operation.call_args
        self.assertEqual("failed", call.kwargs["status"])
        error = call.kwargs["error"]
        self.assertEqual("derived_layer.database_error", error["code"])
        self.assertEqual(
            {
                "sqlstate": "57014",
                "message": "canceling statement due to timeout",
            },
            error["technicalDetail"],
        )
        self.assertTrue(error["stateUnchanged"])
        self.assertEqual(
            "The existing materialized data remains unchanged.",
            error["safeState"],
        )
        self.assertTrue(error["rolledBack"])
        self.assertEqual("database-transaction", error["failurePhase"])
        self.assertNotIn("indeterminate", error)
        self.assertNotIn("SECRET SQL", repr(error))
        self.assertNotIn("retryable", error)
        self.assertNotIn("contentionScope", error)

    def test_background_mutation_contention_is_retryable_conflict(self):
        derived = Mock()
        derived.create.side_effect = self.failure_state(
            app.DerivedLayerContentionError("derived-mutation"),
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ):
            app.run_derived_background(
                "2" * 32,
                "create",
                {},
                "admin",
                "127.0.0.1",
            )

        call = control.finish_operation.call_args
        self.assertEqual("failed", call.kwargs["status"])
        error = call.kwargs["error"]
        self.assertEqual("derived_layer.database_contention", error["code"])
        self.assertEqual("contention", error["category"])
        self.assertEqual("derived-mutation", error["contentionScope"])
        self.assertEqual(HTTPStatus.CONFLICT, error["status"])
        self.assertTrue(error["retryable"])
        self.assertTrue(error["stateUnchanged"])
        self.assertTrue(error["rolledBack"])
        self.assertIn("active derived-layer operation", error["suggestedAction"])

    def test_background_postgres_lock_timeout_is_retryable_contention(self):
        failure = app.psycopg.errors.LockNotAvailable(
            "SECRET lock holder context",
        )
        derived = Mock()
        derived.create.side_effect = app.DerivedLayerDatabaseOperationError(
            failure,
            failure_phase="database-transaction",
            state_unchanged=True,
            rolled_back=True,
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ):
            app.run_derived_background(
                "3" * 32,
                "create",
                {},
                "admin",
                "127.0.0.1",
            )

        error = control.finish_operation.call_args.kwargs["error"]
        self.assertEqual("derived_layer.database_contention", error["code"])
        self.assertEqual("postgresql-lock", error["contentionScope"])
        self.assertEqual(HTTPStatus.CONFLICT, error["status"])
        self.assertTrue(error["retryable"])
        self.assertEqual("55P03", error["technicalDetail"]["sqlstate"])
        self.assertTrue(error["stateUnchanged"])
        self.assertTrue(error["rolledBack"])
        self.assertNotIn("SECRET", repr(error))

    def test_background_legacy_policy_rejection_keeps_422_status(self):
        derived = Mock()
        derived.create.side_effect = self.failure_state(
            app.DerivedLayerError("SQL keyword UPDATE is not allowed."),
            phase="preflight",
            rolled_back=False,
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ):
            app.run_derived_background(
                "2" * 32,
                "create",
                {"name": "slow_places"},
                "admin",
                "127.0.0.1",
            )

        error = control.finish_operation.call_args.kwargs["error"]
        self.assertEqual("derived_layer.query_not_allowed", error["code"])
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, error["status"])
        self.assertEqual("policy", error["category"])

    def test_materialization_growth_race_preserves_structured_failure(self):
        probe = DerivedMapExtentRouteTests.materialization_probe()
        derived = Mock()
        derived.create.side_effect = self.failure_state(
            app.DerivedLayerMaterializationTooLarge(
                "slow_places",
                probe,
            ),
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control
        ):
            app.run_derived_background(
                "c" * 32,
                "create",
                {"name": "slow_places"},
                "admin",
                "127.0.0.1",
            )

        error = control.finish_operation.call_args.kwargs["error"]
        self.assertEqual("derived_layer.materialization_too_large", error["code"])
        self.assertEqual(HTTPStatus.CONFLICT, error["status"])
        self.assertEqual("view", error["recommendedKind"])
        self.assertEqual(probe, error["probe"])
        self.assertEqual("create", error["operation"])
        self.assertEqual("estimate", error["probeStage"])
        self.assertTrue(error["rolledBack"])
        self.assertEqual("database-transaction", error["failurePhase"])

    def test_query_cost_race_preserves_structured_failure(self):
        probe = DerivedMapExtentRouteTests.query_plan_probe()
        reasons = [
            {"code": "total_cost", "message": "Planner cost exceeds the limit."},
            {
                "code": "intermediate_rows",
                "message": "An intermediate plan node exceeds the row limit.",
            },
        ]
        derived = Mock()
        derived.create.side_effect = self.failure_state(
            app.DerivedLayerQueryTooExpensive(
                "slow_places",
                probe,
                reasons,
            ),
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control
        ):
            app.run_derived_background(
                "d" * 32,
                "create",
                {"name": "slow_places"},
                "admin",
                "127.0.0.1",
            )

        error = control.finish_operation.call_args.kwargs["error"]
        self.assertEqual("derived_layer.query_too_expensive", error["code"])
        self.assertEqual(HTTPStatus.CONFLICT, error["status"])
        self.assertEqual(probe, error["probe"])
        self.assertEqual(
            [reason["code"] for reason in reasons],
            [reason["code"] for reason in error["reasons"]],
        )
        self.assertTrue(all(
            reason.get("suggestedAction") for reason in error["reasons"]
        ))
        self.assertNotIn("recommendedKind", error)

    def test_actual_materialization_growth_reports_transaction_rollback(self):
        probe = {
            **DerivedMapExtentRouteTests.materialization_probe(
                estimated_bytes=512 * 1024 ** 2,
            ),
            "actualBytes": 2 * 1024 ** 3,
        }
        derived = Mock()
        derived.refresh.side_effect = self.failure_state(
            app.DerivedLayerMaterializationTooLarge(
                "slow_places", probe,
            ),
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ):
            app.run_derived_background(
                "e" * 32,
                "refresh",
                {},
                "admin",
                "127.0.0.1",
                "slow_places",
            )

        error = control.finish_operation.call_args.kwargs["error"]
        self.assertEqual("actual", error["probeStage"])
        self.assertTrue(error["rolledBack"])
        self.assertEqual("refresh", error["operation"])
        self.assertEqual(
            "The existing materialized data remains unchanged.",
            error["safeState"],
        )

    def test_dependency_race_preserves_in_use_guidance(self):
        derived = Mock()
        derived.replace.side_effect = self.failure_state(
            app.DerivedLayerDependencyError(
                "slow_places",
                ["public.consumer"],
                removed_columns=["score"],
                dependent_columns=["score"],
            ),
        )
        control = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control,
        ), patch.object(
            app,
            "read_workspace",
            return_value=(b"{}", {"locale": {"layers": {}}}, "revision"),
        ):
            app.run_derived_background(
                "f" * 32,
                "replace",
                {"name": "slow_places"},
                "admin",
                "127.0.0.1",
                "slow_places",
            )

        error = control.finish_operation.call_args.kwargs["error"]
        self.assertEqual("derived_layer.in_use", error["code"])
        self.assertEqual(["public.consumer"], error["dependents"])
        self.assertEqual(["score"], error["removedColumns"])
        self.assertEqual("replace", error["operation"])
        self.assertTrue(error["stateUnchanged"])

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

    def test_result_serialization_failure_persists_indeterminate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            control = ControlStore(Path(directory))
            control.initialize("correct horse battery staple", "instance")
            operation = control.create_operation(
                "derived-layer.create",
                "admin",
                {"name": "safe_places"},
            )
            derived = Mock()
            derived.create.return_value = {
                "name": "safe_places",
                "kind": "view",
                "sources": ["etl.places"],
                "unsupported": {"not-json"},
            }

            with patch.object(app, "DERIVED", derived), patch.object(
                app, "CONTROL", control,
            ), patch.object(control, "audit"), patch.object(
                app, "schedule_semantic_outbox",
            ):
                app.run_derived_background(
                    operation["id"],
                    "create",
                    {"name": "safe_places"},
                    "admin",
                    "127.0.0.1",
                )

            stored = control.read_operation(operation["id"])
            self.assertEqual("indeterminate", stored["status"])
            self.assertIsNone(stored["result"])
            error = stored["error"]
            self.assertEqual("derived_layer.operation_failed", error["code"])
            self.assertTrue(error["indeterminate"])
            self.assertEqual("result-reporting", error["failurePhase"])
            self.assertNotIn("stateUnchanged", error)
            self.assertNotIn("safeState", error)


class SemanticDerivedIntegrationTests(unittest.TestCase):
    @staticmethod
    def workspace() -> dict:
        return {
            "dbs": "MAPP",
            "locale": {
                "layers": {
                    "Places": {
                        "dbs": "MAPP",
                        "table": "derived_layers.places",
                    },
                },
            },
        }

    @staticmethod
    def derived(status: str = "registering") -> Mock:
        derived = Mock()
        item = {
            "name": "places",
            "kind": "view",
            "semanticProfile": {
                "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "generation": 2,
                "status": status,
                "revision": "8" if status == "ready" else None,
            },
        }
        derived.list.return_value = [item]
        derived.list_page.return_value = [item]
        derived.get.return_value = item
        return derived

    def test_new_nonready_binding_is_blocked_but_existing_binding_warns(self):
        candidate = self.workspace()
        with patch.object(app, "DERIVED", self.derived()):
            errors, warnings = app.semantic_publication_diagnostics(candidate, {})
            existing_errors, existing_warnings = (
                app.semantic_publication_diagnostics(candidate, candidate)
            )

        self.assertEqual("semantic.derived_not_ready", errors[0]["code"])
        self.assertEqual([], warnings)
        self.assertEqual([], existing_errors)
        self.assertEqual("registering", existing_warnings[0]["status"])

    def test_ready_binding_has_no_publication_diagnostic(self):
        candidate = self.workspace()
        with patch.object(app, "DERIVED", self.derived("ready")):
            self.assertEqual(
                ([], []),
                app.semantic_publication_diagnostics(candidate, {}),
            )

    def test_outbox_delivery_atomically_completes_claimed_event(self):
        payload = {
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "register",
            "generation": 2,
            "actor": "token:test",
            "visibility": "inspect",
            "generated": {"name": "places"},
        }
        payload["payloadHash"] = app.semantic_event_payload_hash(payload)
        event = {
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "claimId": "cb39b58c-3487-49da-94ce-0a9633cc848a",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "register",
            "generation": 2,
            "attempts": 0,
            "payload": payload,
        }
        derived = Mock()
        derived.claim_semantic_events.side_effect = [[event], []]
        semantic = Mock()
        semantic.request.return_value = {
            "catalogRevision": 11,
            "event": {
                "eventId": event["eventId"],
                "payloadHash": payload["payloadHash"],
                "idempotent": False,
            },
            "asset": {
                "id": event["assetId"],
                "generation": 2,
                "status": "ready",
                "catalogRevision": 11,
            },
        }

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            result = app.drain_semantic_outbox()

        self.assertEqual({"delivered": 1, "retried": 0, "repairRequired": 0}, result)
        sent = semantic.request.call_args.kwargs["payload"]
        self.assertEqual(payload["payloadHash"], sent["payloadHash"])
        self.assertEqual(
            [
                "claim_semantic_events",
                "mark_semantic_delivered",
                "claim_semantic_events",
            ],
            [call[0] for call in derived.method_calls],
        )

    def test_mismatched_event_ack_never_marks_profile_ready(self):
        payload = {
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "register",
            "generation": 2,
            "actor": "token:test",
            "generated": {"name": "places"},
        }
        payload["payloadHash"] = app.semantic_event_payload_hash(payload)
        event = {
            **{
                key: payload[key]
                for key in ("eventId", "assetId", "type", "generation")
            },
            "claimId": "cb39b58c-3487-49da-94ce-0a9633cc848a",
            "attempts": 0,
            "payload": payload,
        }
        derived = Mock()
        derived.claim_semantic_events.side_effect = [[event], []]
        semantic = Mock()
        semantic.request.return_value = {
            "catalogRevision": 11,
            "event": {
                "eventId": "wrong-event",
                "payloadHash": payload["payloadHash"],
                "idempotent": False,
            },
            "asset": {
                "id": event["assetId"],
                "generation": 2,
                "status": "ready",
                "catalogRevision": 11,
            },
        }

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            result = app.drain_semantic_outbox()

        self.assertEqual(1, result["retried"])
        derived.mark_semantic_delivered.assert_not_called()

    def test_corrupt_outbox_hash_requires_repair_without_sending(self):
        event = {
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "claimId": "cb39b58c-3487-49da-94ce-0a9633cc848a",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "register",
            "generation": 2,
            "attempts": 0,
            "payload": {
                "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "type": "register",
                "generation": 2,
                "actor": "token:test",
                "payloadHash": "0" * 64,
            },
        }
        derived = Mock()
        derived.claim_semantic_events.side_effect = [[event], []]
        semantic = Mock()

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            result = app.drain_semantic_outbox()

        self.assertEqual(1, result["repairRequired"])
        semantic.request.assert_not_called()
        derived.mark_semantic_repair.assert_called_once()

    def test_semantic_event_integers_reject_bool_and_float_coercion(self):
        event = {
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "register",
            "generation": 1,
        }
        for invalid in (True, 1.0):
            with self.subTest(retained_generation=invalid):
                payload = {
                    **event,
                    "generation": invalid,
                    "actor": "token:test",
                    "generated": {"name": "places"},
                }
                payload["payloadHash"] = app.semantic_event_payload_hash(
                    payload
                )
                with self.assertRaises(app.SemanticClientError):
                    app.validate_semantic_outbox_event({
                        **event,
                        "payload": payload,
                    })

        payload = {**event, "payloadHash": "a" * 64}
        valid_ack = {
            "catalogRevision": 1,
            "event": {
                "eventId": event["eventId"],
                "payloadHash": payload["payloadHash"],
                "idempotent": False,
            },
            "asset": {
                "id": event["assetId"],
                "generation": 1,
                "status": "ready",
                "catalogRevision": 1,
            },
        }
        for field, invalid in (
            ("generation", True),
            ("generation", 1.0),
            ("catalogRevision", True),
            ("catalogRevision", 1.0),
        ):
            with self.subTest(ack_field=field, invalid=invalid):
                response = {
                    **valid_ack,
                    "event": dict(valid_ack["event"]),
                    "asset": {
                        **valid_ack["asset"],
                        field: invalid,
                    },
                }
                with self.assertRaises(app.SemanticClientError):
                    app.validate_semantic_event_ack(
                        event,
                        payload,
                        response,
                    )

    def test_permanent_semantic_rejection_requires_manual_repair(self):
        derived = Mock()
        event = {
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "claimId": "cb39b58c-3487-49da-94ce-0a9633cc848a",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "register",
            "generation": 2,
            "attempts": 0,
            "payload": {"actor": "token:test"},
        }
        derived.claim_semantic_events.side_effect = [[event], []]
        semantic = Mock()
        semantic.request.side_effect = app.SemanticClientError(
            "Invalid event.",
            status=HTTPStatus.UNPROCESSABLE_ENTITY,
            payload={"code": "semantic.invalid_event"},
        )

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            result = app.drain_semantic_outbox()

        self.assertEqual(1, result["repairRequired"])
        derived.mark_semantic_repair.assert_called_once()
        derived.mark_semantic_retry.assert_not_called()

    def test_confirmed_reset_archives_profiles_before_database_removal(self):
        reset_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        derived = self.derived("ready")
        semantic = Mock()
        archived = [{
            "name": "places",
            "relation": "derived_layers.places",
            "kind": "view",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "generation": 3,
            "status": "archived",
            "revision": "12",
        }]
        ready = [{**archived[0], "status": "ready", "revision": "11"}]
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ), patch.object(app, "drain_semantic_outbox") as drain, patch.object(
            app,
            "derived_semantic_profiles",
            side_effect=[ready, archived],
        ):
            derived.semantic_outbox_blockers.return_value = []
            result = app.archive_derived_semantics_before_reset(reset_owner)

        derived.begin_semantic_reset.assert_called_once_with(
            "system:reset-data",
            reset_owner,
        )
        derived.queue_semantic_archives.assert_called_once_with(
            "system:reset-data"
        )
        self.assertEqual(2, drain.call_count)
        self.assertEqual(1, result["archived"])
        self.assertEqual(12, result["catalogRevision"])

    def test_reset_refuses_orphaned_semantic_outbox_repair(self):
        reset_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        derived = self.derived("ready")
        derived.semantic_outbox_blockers.return_value = [{
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "archive",
            "generation": 4,
            "status": "repair_required",
            "name": "already_dropped",
            "lastError": "asset not found",
        }]
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", Mock()
        ), patch.object(app, "drain_semantic_outbox"), patch.object(
            app,
            "derived_semantic_profiles",
            return_value=[{
                "name": "places",
                "status": "ready",
            }],
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "already_dropped",
            ):
                app.archive_derived_semantics_before_reset(reset_owner)
        derived.queue_semantic_archives.assert_not_called()

    def test_explicit_force_recovery_rebinds_interrupted_reset_profiles(self):
        derived = Mock()
        derived.recover_reset_semantic_profiles.return_value = {
            "resetOwner": "289d495d-6642-4525-8a63-bb5e4f0c764c",
            "profiles": [{
                "name": "places",
                "assetId": "b22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "generation": 1,
                "status": "registering",
                "revision": None,
            }],
        }
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "schedule_semantic_outbox"
        ) as wake:
            result = app.recover_interrupted_reset_semantics(force=True)

        self.assertEqual(1, result["recovered"])
        derived.recover_reset_semantic_profiles.assert_called_once_with(
            "system:reset-recovery",
            None,
        )
        wake.assert_called_once_with()
        derived.complete_reset_semantic_recovery.assert_not_called()

    def test_force_recovery_pins_completion_to_the_observed_gate_owner(self):
        observed_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        derived = Mock()
        derived.recover_reset_semantic_profiles.return_value = {
            "resetOwner": observed_owner,
            "profiles": [{
                "name": "places",
                "assetId": "b22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "generation": 1,
                "status": "registering",
                "revision": None,
            }],
        }
        derived.semantic_outbox_blockers.return_value = []
        ready = [{
            "name": "places",
            "status": "ready",
            "revision": "14",
        }]
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", Mock()
        ), patch.object(app, "drain_semantic_outbox"), patch.object(
            app,
            "derived_semantic_profiles",
            return_value=ready,
        ):
            result = app.recover_interrupted_reset_semantics(
                force=True,
                wait_for_ready=True,
            )

        self.assertEqual(1, result["recovered"])
        derived.recover_reset_semantic_profiles.assert_called_once_with(
            "system:reset-recovery",
            None,
        )
        derived.complete_reset_semantic_recovery.assert_called_once_with(
            observed_owner
        )

    def test_empty_owned_recovery_completes_its_observed_gate(self):
        observed_owner = "289d495d-6642-4525-8a63-bb5e4f0c764c"
        derived = Mock()
        derived.recover_reset_semantic_profiles.return_value = {
            "resetOwner": observed_owner,
            "profiles": [],
        }
        derived.reset_recovery_names.return_value = []
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", Mock()
        ):
            result = app.recover_interrupted_reset_semantics(
                reset_owner=observed_owner,
                wait_for_ready=True,
            )

        self.assertEqual(0, result["recovered"])
        derived.complete_reset_semantic_recovery.assert_called_once_with(
            observed_owner
        )

    def test_reset_compensation_requires_old_archive_to_finish(self):
        derived = Mock()
        derived.recover_reset_semantic_profiles.return_value = {
            "resetOwner": "289d495d-6642-4525-8a63-bb5e4f0c764c",
            "profiles": [{
                "name": "places",
                "assetId": "b22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "generation": 1,
                "status": "registering",
                "revision": None,
            }],
        }
        derived.semantic_outbox_blockers.return_value = [{
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "archive",
            "generation": 3,
            "status": "repair_required",
            "name": "places",
            "lastError": "stale generation",
        }]
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", Mock()
        ), patch.object(app, "drain_semantic_outbox"), patch.object(
            app,
            "derived_semantic_profiles",
            return_value=[{
                "name": "places",
                "status": "registering",
            }],
        ):
            with self.assertRaisesRegex(RuntimeError, "places"):
                app.recover_interrupted_reset_semantics(
                    reset_owner=(
                        "289d495d-6642-4525-8a63-bb5e4f0c764c"
                    ),
                    wait_for_ready=True
                )
        derived.complete_reset_semantic_recovery.assert_not_called()

    def test_reset_compensation_waits_for_recovery_started_at_boot(self):
        derived = Mock()
        derived.recover_reset_semantic_profiles.return_value = {
            "resetOwner": "289d495d-6642-4525-8a63-bb5e4f0c764c",
            "profiles": [],
        }
        derived.reset_recovery_names.return_value = ["places"]
        derived.semantic_outbox_blockers.return_value = []
        ready = [{
            "name": "places",
            "status": "ready",
            "revision": "14",
        }]
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", Mock()
        ), patch.object(app, "drain_semantic_outbox"), patch.object(
            app,
            "derived_semantic_profiles",
            return_value=ready,
        ):
            result = app.recover_interrupted_reset_semantics(
                reset_owner="289d495d-6642-4525-8a63-bb5e4f0c764c",
                wait_for_ready=True
            )

        self.assertEqual(1, result["recovered"])
        self.assertEqual(ready, result["profiles"])
        derived.reset_recovery_names.assert_called_once_with()
        derived.complete_reset_semantic_recovery.assert_called_once_with(
            "289d495d-6642-4525-8a63-bb5e4f0c764c"
        )

    def test_losing_reset_compensation_does_not_touch_winning_gate(self):
        derived = Mock()
        derived.recover_reset_semantic_profiles.side_effect = (
            app.DerivedLayerResetOwnershipError(
                "The maintenance gate belongs to another reset operation."
            )
        )
        with patch.object(app, "DERIVED", derived):
            result = app.recover_interrupted_reset_semantics(
                reset_owner="ae7846cd-594b-4a42-91b7-06f533906b43",
                wait_for_ready=True,
            )

        self.assertEqual(0, result["recovered"])
        self.assertFalse(result["gateOwned"])
        self.assertEqual("foreign_gate", result["reason"])
        derived.reset_recovery_names.assert_not_called()
        derived.complete_reset_semantic_recovery.assert_not_called()


class SemanticGatewayRouteTests(unittest.TestCase):
    @staticmethod
    def handler(path: str, payload: dict | None = None):
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: "token:test"
        handler._authentication = {}
        handler._payload = lambda: payload or {}
        handler._remote = lambda: "127.0.0.1"
        handler._json = lambda status, body: responses.append((status, body))
        return handler, responses

    def test_derived_profile_readiness_is_served_from_local_outbox_state(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles/places"
        )
        derived = SemanticDerivedIntegrationTests.derived("registering")
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 14}

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            handler.do_GET()

        status, payload = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(14, payload["catalogRevision"])
        self.assertEqual("places", payload["derivedProfile"]["name"])
        self.assertEqual(
            "registering",
            payload["derivedProfile"]["status"],
        )

    def test_admin_profile_read_surfaces_name_level_delivery_blocker(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles/places"
        )
        handler._authentication = {
            "scopes": ["semantic:inspect", "semantic:admin"],
        }
        derived = SemanticDerivedIntegrationTests.derived("registering")
        derived.semantic_outbox_blockers.return_value = [{
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "archive",
            "generation": 3,
            "status": "repair_required",
            "attempts": 8,
            "name": "places",
            "lastError": "stale\n generation",
        }]
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 15}

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            handler.do_GET()

        status, payload = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(
            {
                "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "operation": "archive",
                "generation": 3,
                "status": "repair_required",
                "attempts": 8,
                "lastError": "stale generation",
            },
            payload["derivedProfile"]["delivery"],
        )

    def test_admin_list_surfaces_unmatched_dropped_archive_blocker(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles"
        )
        handler._authentication = {
            "scopes": ["semantic:inspect", "semantic:admin"],
        }
        derived = SemanticDerivedIntegrationTests.derived("ready")
        derived.semantic_outbox_blockers.return_value = [{
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "archive",
            "generation": 3,
            "status": "repair_required",
            "attempts": 8,
            "name": "already_dropped",
            "lastError": "private\n failure",
        }]
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 15}

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            handler.do_GET()

        status, payload = responses[0]
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(
            [{
                "name": "already_dropped",
                "relation": "derived_layers.already_dropped",
                "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "eventId": (
                    derived.semantic_outbox_blockers.return_value[0]["eventId"]
                ),
                "operation": "archive",
                "generation": 3,
                "status": "repair_required",
                "attempts": 8,
                "lastError": "private failure",
            }],
            payload["deliveryBlockers"],
        )

    def test_inspect_only_list_omits_dropped_archive_blockers(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles"
        )
        handler._authentication = {"scopes": ["semantic:inspect"]}
        derived = SemanticDerivedIntegrationTests.derived("ready")
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 15}

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertNotIn("deliveryBlockers", responses[0][1])
        derived.semantic_outbox_blockers.assert_not_called()

    def test_legacy_derived_profiles_require_pagination_above_100(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles"
        )
        handler._authentication = {"scopes": ["semantic:inspect"]}
        derived = Mock()
        derived.list_page.return_value = [
            {
                "name": f"profile_{index}",
                "kind": "view",
                "semanticProfile": {
                    "assetId": f"asset-{index}",
                    "generation": 1,
                    "status": "ready",
                    "revision": "15",
                },
            }
            for index in range(101)
        ]
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 15}

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ), patch.object(app, "schedule_semantic_outbox"):
            handler.do_GET()

        self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
        self.assertEqual("pagination.required", responses[0][1]["code"])
        derived.list_page.assert_called_once_with(
            after_name=None,
            fetch_limit=101,
        )
        derived.semantic_outbox_blockers.assert_not_called()

    def test_derived_profile_cursor_is_storage_revision_and_visibility_bound(
        self,
    ):
        first_item = {
            "name": "places",
            "kind": "view",
            "semanticProfile": {
                "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "generation": 2,
                "status": "ready",
                "revision": "15",
            },
        }
        second_item = {
            "name": "roads",
            "kind": "view",
            "semanticProfile": {
                "assetId": "b22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
                "generation": 1,
                "status": "ready",
                "revision": "15",
            },
        }
        derived = Mock()
        derived.list_page.return_value = [first_item, second_item]
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 15}
        control = Mock()
        control.instance_id.return_value = "instance-1"
        control.pagination_key.return_value = b"p" * 32

        first_handler, first_responses = self.handler(
            "/api/semantic/derived-profiles?limit=1"
        )
        first_handler._authentication = {"scopes": ["semantic:inspect"]}
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ), patch.object(app, "CONTROL", control), patch.object(
            app, "schedule_semantic_outbox"
        ):
            first_handler.do_GET()

        self.assertEqual(HTTPStatus.OK, first_responses[0][0])
        cursor = first_responses[0][1]["pagination"]["nextCursor"]
        self.assertRegex(cursor, r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$")
        derived.list_page.assert_called_once_with(
            after_name=None,
            fetch_limit=2,
        )

        derived.list_page.reset_mock()
        derived.list_page.return_value = [second_item]
        second_handler, second_responses = self.handler(
            "/api/semantic/derived-profiles?limit=1&cursor=" + cursor
        )
        second_handler._authentication = {"scopes": ["semantic:inspect"]}
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ), patch.object(app, "CONTROL", control), patch.object(
            app, "schedule_semantic_outbox"
        ):
            second_handler.do_GET()
        self.assertEqual(HTTPStatus.OK, second_responses[0][0])
        self.assertEqual(
            "roads",
            second_responses[0][1]["derivedProfiles"][0]["name"],
        )
        derived.list_page.assert_called_once_with(
            after_name="places",
            fetch_limit=2,
        )

        semantic.request.return_value = {"catalogRevision": 16}
        changed_handler, changed_responses = self.handler(
            "/api/semantic/derived-profiles?limit=1&cursor=" + cursor
        )
        changed_handler._authentication = {"scopes": ["semantic:inspect"]}
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ), patch.object(app, "CONTROL", control):
            changed_handler.do_GET()
        self.assertEqual(HTTPStatus.BAD_REQUEST, changed_responses[0][0])
        self.assertEqual("pagination.invalid", changed_responses[0][1]["code"])

        semantic.request.return_value = {"catalogRevision": 15}
        admin_handler, admin_responses = self.handler(
            "/api/semantic/derived-profiles?limit=1&cursor=" + cursor
        )
        admin_handler._authentication = {
            "scopes": ["semantic:inspect", "semantic:admin"],
        }
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ), patch.object(app, "CONTROL", control):
            admin_handler.do_GET()
        self.assertEqual(HTTPStatus.BAD_REQUEST, admin_responses[0][0])
        self.assertEqual("pagination.invalid", admin_responses[0][1]["code"])

    def test_admin_derived_page_batches_unmatched_blockers_only_once(self):
        first_item = {
            "name": "places",
            "kind": "view",
            "semanticProfile": {
                "assetId": "asset-places",
                "generation": 2,
                "status": "ready",
                "revision": "15",
            },
        }
        second_item = {
            "name": "roads",
            "kind": "view",
            "semanticProfile": {
                "assetId": "asset-roads",
                "generation": 1,
                "status": "ready",
                "revision": "15",
            },
        }
        matching = [{
            "eventId": "event-places",
            "assetId": "asset-places",
            "type": "register",
            "generation": 2,
            "status": "repair_required",
            "attempts": 3,
            "name": "places",
            "lastError": "delivery failed",
        }]
        unmatched = [
            {
                "eventId": f"event-dropped-{index}",
                "assetId": f"asset-dropped-{index}",
                "type": "archive",
                "generation": 3,
                "status": "repair_required",
                "attempts": 8,
                "name": f"dropped_{index}",
                "lastError": "archive failed",
            }
            for index in range(101)
        ]
        derived = Mock()
        derived.list_page.return_value = [first_item, second_item]
        derived.semantic_outbox_blockers.side_effect = [matching, unmatched]
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 15}
        control = Mock()
        control.instance_id.return_value = "instance-1"
        control.pagination_key.return_value = b"p" * 32
        handler, responses = self.handler(
            "/api/semantic/derived-profiles?limit=1"
        )
        handler._authentication = {
            "scopes": ["semantic:inspect", "semantic:admin"],
        }

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ), patch.object(app, "CONTROL", control), patch.object(
            app, "schedule_semantic_outbox"
        ):
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        payload = responses[0][1]
        self.assertEqual(100, len(payload["deliveryBlockers"]))
        self.assertTrue(payload["deliveryBlockersMore"])
        self.assertEqual(
            "repair_required",
            payload["derivedProfiles"][0]["delivery"]["status"],
        )
        cursor = payload["pagination"]["nextCursor"]
        self.assertEqual(
            [
                {
                    "profile_names": ["places"],
                    "include_unmatched": False,
                    "one_per_profile": True,
                    "fetch_limit": 1,
                },
                {"unmatched_only": True, "fetch_limit": 101},
            ],
            [call.kwargs for call in derived.semantic_outbox_blockers.call_args_list],
        )

        derived.list_page.return_value = [second_item]
        derived.semantic_outbox_blockers.reset_mock()
        derived.semantic_outbox_blockers.side_effect = [[{
            **matching[0],
            "eventId": "event-roads",
            "assetId": "asset-roads",
            "name": "roads",
        }]]
        handler, responses = self.handler(
            "/api/semantic/derived-profiles?limit=1&cursor=" + cursor
        )
        handler._authentication = {
            "scopes": ["semantic:inspect", "semantic:admin"],
        }
        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ), patch.object(app, "CONTROL", control), patch.object(
            app, "schedule_semantic_outbox"
        ):
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual([], responses[0][1]["deliveryBlockers"])
        self.assertFalse(responses[0][1]["deliveryBlockersMore"])
        derived.semantic_outbox_blockers.assert_called_once_with(
            profile_names=["roads"],
            include_unmatched=False,
            one_per_profile=True,
            fetch_limit=1,
        )

    def test_inspect_only_profile_read_redacts_delivery_diagnostics(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles/places"
        )
        handler._authentication = {"scopes": ["semantic:inspect"]}
        derived = SemanticDerivedIntegrationTests.derived("registering")
        derived.semantic_outbox_blockers.return_value = [{
            "eventId": "e22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "type": "archive",
            "generation": 3,
            "status": "repair_required",
            "attempts": 8,
            "name": "places",
            "lastError": "private database detail",
        }]
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 15}

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertNotIn("delivery", responses[0][1]["derivedProfile"])
        derived.semantic_outbox_blockers.assert_not_called()

    def test_derived_profile_read_never_fabricates_catalog_revision(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles/places"
        )
        derived = SemanticDerivedIntegrationTests.derived("ready")
        semantic = Mock()
        semantic.request.side_effect = app.SemanticClientError(
            "semantic service unavailable",
        )

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            handler.do_GET()

        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, responses[0][0])
        self.assertEqual("semantic.unavailable", responses[0][1]["code"])
        self.assertNotIn("catalogRevision", responses[0][1])

    def test_semantic_apply_requires_confirmation_and_strips_gateway_field(self):
        handler, responses = self.handler(
            "/api/semantic/proposals/sem-1/apply",
            {"confirmed": True},
        )
        handler._semantic_request = Mock(
            return_value={"proposal": {
                "id": "sem-1",
                "assetId": "asset-1",
                "state": "applied",
            }}
        )
        control = Mock()

        with patch.object(app, "CONTROL", control):
            handler.do_POST()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual(
            {},
            handler._semantic_request.call_args.kwargs["payload"],
        )
        control.audit.assert_called_once_with(
            "semantic.proposal.applied",
            actor="token:test",
            remote="127.0.0.1",
            details={
                "id": "sem-1",
                "assetId": "asset-1",
                "status": "applied",
            },
        )

        unconfirmed, unconfirmed_responses = self.handler(
            "/api/semantic/proposals/sem-1/apply",
            {},
        )
        unconfirmed._semantic_request = Mock()
        unconfirmed.do_POST()
        self.assertEqual(HTTPStatus.CONFLICT, unconfirmed_responses[0][0])
        unconfirmed._semantic_request.assert_not_called()

    def test_semantic_create_and_decline_audit_metadata_only(self):
        cases = (
            (
                "/api/semantic/proposals",
                {
                    "assetId": "asset-1",
                    "baseVersion": 2,
                    "operations": [{
                        "op": "set",
                        "path": "/curated/description",
                        "value": "sensitive curated value",
                    }],
                    "fingerprint": "a" * 64,
                },
                "pending",
                "semantic.proposal.created",
            ),
            (
                "/api/semantic/proposals/sem-1/decline",
                {"confirmed": True, "reason": "Superseded"},
                "declined",
                "semantic.proposal.declined",
            ),
        )
        for path, request, state, event in cases:
            with self.subTest(path=path):
                handler, responses = self.handler(path, request)
                handler._semantic_request = Mock(return_value={
                    "proposal": {
                        "id": "sem-1",
                        "assetId": "asset-1",
                        "state": state,
                        "operations": request.get("operations"),
                    }
                })
                control = Mock()

                with patch.object(app, "CONTROL", control):
                    handler.do_POST()

                self.assertEqual(HTTPStatus.OK, responses[0][0])
                control.audit.assert_called_once_with(
                    event,
                    actor="token:test",
                    remote="127.0.0.1",
                    details={
                        "id": "sem-1",
                        "assetId": "asset-1",
                        "status": state,
                    },
                )

    def test_semantic_errors_namespace_private_service_envelopes(self):
        cases = (
            (
                HTTPStatus.NOT_FOUND,
                {
                    "error": {
                        "code": "asset_not_found",
                        "message": "Semantic asset was not found.",
                        "details": {"assetId": "asset-1"},
                    }
                },
                {
                    "error": "Semantic asset was not found.",
                    "code": "semantic.asset_not_found",
                    "details": {"assetId": "asset-1"},
                },
            ),
            (
                HTTPStatus.CONFLICT,
                {
                    "error": {
                        "code": "semantic.revision_conflict",
                        "message": "Asset version changed.",
                    }
                },
                {
                    "error": "Asset version changed.",
                    "code": "semantic.revision_conflict",
                },
            ),
            (
                HTTPStatus.BAD_REQUEST,
                {
                    "error": {
                        "code": "pagination_invalid",
                        "message": "cursor is invalid or expired.",
                    }
                },
                {
                    "error": "cursor is invalid or expired.",
                    "code": "pagination.invalid",
                },
            ),
            (
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {
                    "error": {
                        "code": "page_too_large",
                        "message": "One collection item is too large.",
                        "details": {"maxPageBytes": 16 * 1024 * 1024},
                    }
                },
                {
                    "error": "One collection item is too large.",
                    "code": "semantic.page_too_large",
                    "details": {"maxPageBytes": 16 * 1024 * 1024},
                },
            ),
            (
                HTTPStatus.CONFLICT,
                {
                    "error": {
                        "code": "pagination_required",
                        "message": "Retry this collection with limit.",
                        "details": {"maxLegacyItems": 100},
                    }
                },
                {
                    "error": "Retry this collection with limit.",
                    "code": "pagination.required",
                    "details": {"maxLegacyItems": 100},
                },
            ),
        )
        for status, envelope, expected in cases:
            with self.subTest(status=status, envelope=envelope):
                handler, responses = self.handler("/api/semantic/catalog")
                handler._semantic_error(app.SemanticClientError(
                    "Semantic service request failed.",
                    status=status,
                    payload=envelope,
                ))
                self.assertEqual([(status, expected)], responses)

    def test_private_semantic_unauthorized_is_not_caller_authentication(self):
        handler, responses = self.handler("/api/semantic/catalog")

        handler._semantic_error(app.SemanticClientError(
            "Semantic service request failed.",
            status=HTTPStatus.UNAUTHORIZED,
            payload={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid internal service token.",
                }
            },
        ))

        self.assertEqual(
            [(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "Semantic service authentication is misconfigured.",
                    "code": "semantic.internal_auth_failed",
                },
            )],
            responses,
        )

    def test_admin_repair_requeues_and_wakes_delivery(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles/places/repair",
            {"confirmed": True},
        )
        derived = SemanticDerivedIntegrationTests.derived("registering")
        derived.repair_semantic_profile.return_value = {
            "name": "places",
            "assetId": "a22c52cb-f1d2-4146-bb1b-8f6e3c59fa61",
            "generation": 2,
            "status": "registering",
            "revision": None,
            "operation": "replace",
        }
        control = Mock()
        wake = Mock()
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 16}

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "CONTROL", control
        ), patch.object(app, "SEMANTIC", semantic), patch.object(
            app, "schedule_semantic_outbox", wake
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.ACCEPTED, responses[0][0])
        derived.repair_semantic_profile.assert_called_once_with("places")
        wake.assert_called_once_with()
        control.audit.assert_called_once()

    def test_admin_repair_rejects_properties_outside_closed_contract(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles/places/repair",
            {"confirmed": True, "retryAll": True},
        )
        derived = SemanticDerivedIntegrationTests.derived("repair_required")

        with patch.object(app, "DERIVED", derived):
            handler.do_POST()

        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        self.assertEqual("semantic.invalid_request", responses[0][1]["code"])
        derived.repair_semantic_profile.assert_not_called()

    def test_admin_retry_reports_reset_maintenance_as_conflict(self):
        handler, responses = self.handler(
            "/api/semantic/derived-profiles/places/repair",
            {"confirmed": True},
        )
        derived = SemanticDerivedIntegrationTests.derived("repair_required")
        derived.repair_semantic_profile.side_effect = (
            app.DerivedLayerMaintenanceError(
                "Derived-layer changes are paused while reset-data archives "
                "semantic profiles."
            )
        )
        semantic = Mock()
        semantic.request.return_value = {"catalogRevision": 16}

        with patch.object(app, "DERIVED", derived), patch.object(
            app, "SEMANTIC", semantic
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
        self.assertEqual(
            "derived_layer.maintenance",
            responses[0][1]["code"],
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

    def test_reload_requires_explicit_confirmation(self) -> None:
        handler, responses = self.handler({})
        with (
            patch.object(app, "read_workspace") as read_workspace,
            patch.object(app.CONTROL, "create_operation") as create_operation,
        ):
            handler.do_POST()

        read_workspace.assert_not_called()
        create_operation.assert_not_called()
        self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
        self.assertEqual("xyz.confirmation_required", responses[0][1]["code"])

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


class CollectionPaginationRouteTests(unittest.TestCase):
    @staticmethod
    def handler(path: str) -> tuple[app.Handler, list]:
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = path
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: "token:test"
        handler._authentication = {"scopes": ["inspect"]}
        handler._json = lambda status, body: responses.append((status, body))
        handler.send_error = lambda status: responses.append((status, {}))
        return handler, responses

    def test_workspace_proposal_list_returns_one_bounded_page(self):
        proposals = [
            {"id": "proposal-1", "status": "pending"},
            {"id": "proposal-2", "status": "applied"},
        ]
        handler, responses = self.handler("/api/proposals?limit=1")
        control = Mock()
        control.instance_id.return_value = "instance-1"
        control.pagination_key.return_value = b"p" * 32
        with patch.object(
            app,
            "proposal_list",
            return_value=proposals,
        ) as proposal_page, patch.object(app, "CONTROL", control):
            handler.do_GET()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertEqual([proposals[0]], responses[0][1]["proposals"])
        self.assertEqual(1, responses[0][1]["pagination"]["limit"])
        self.assertRegex(
            responses[0][1]["pagination"]["nextCursor"],
            r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$",
        )
        proposal_page.assert_called_once_with(
            control,
            after_id=None,
            fetch_limit=2,
        )

    def test_workspace_proposal_list_rejects_invalid_cursor(self):
        handler, responses = self.handler(
            "/api/proposals?limit=1&cursor=readable-offset"
        )
        with patch.object(app, "proposal_list", return_value=[]):
            handler.do_GET()

        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][0])
        self.assertEqual("pagination.invalid", responses[0][1]["code"])

    def test_legacy_workspace_proposals_require_pagination_above_100(self):
        proposals = [{"id": f"proposal-{index}"} for index in range(101)]
        handler, responses = self.handler("/api/proposals")
        control = Mock()
        with patch.object(
            app,
            "proposal_list",
            return_value=proposals,
        ) as proposal_page, patch.object(app, "CONTROL", control):
            handler.do_GET()

        self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
        self.assertEqual("pagination.required", responses[0][1]["code"])
        proposal_page.assert_called_once_with(control, fetch_limit=101)


class ProposalCreationRouteTests(unittest.TestCase):
    @staticmethod
    def handler(check_fingerprint: str) -> tuple[app.Handler, list]:
        responses = []
        handler = object.__new__(app.Handler)
        handler.path = "/api/proposals"
        handler._host_allowed = lambda: True
        handler._authorized = lambda state_change=False: "token:test"
        handler._payload = lambda: {
            "revision": "revision-1",
            "operations": [],
            "checkFingerprint": check_fingerprint,
        }
        handler._json = lambda status, body: responses.append((status, body))
        handler.send_error = lambda status: responses.append((status, {}))
        return handler, responses

    def test_verified_check_fingerprint_is_persisted_and_returned(self):
        fingerprint = "a" * 64
        plugin_fingerprint = "b" * 64
        current = {"locale": {"layers": {}}}
        candidate = {"locale": {"layers": {}}}
        proposal = {"id": "proposal-1", "status": "pending"}
        handler, responses = self.handler(fingerprint)

        with (
            patch.object(
                app,
                "read_workspace",
                return_value=(b"{}", current, "revision-1"),
            ),
            patch.object(
                app,
                "apply_operations",
                return_value=(candidate, []),
            ),
            patch.object(app, "validate_candidate", return_value=[]),
            patch.object(
                app,
                "proposal_check",
                return_value={
                    "checkFingerprint": fingerprint,
                    "pluginCatalogueFingerprint": plugin_fingerprint,
                },
            ),
            patch.object(
                app,
                "proposal_create",
                return_value=proposal,
            ) as create_proposal,
            patch.object(
                app,
                "semantic_publication_diagnostics",
                return_value=([], []),
            ),
            patch.object(app, "proposal_write") as proposal_write,
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.CREATED, responses[0][0])
        self.assertEqual(
            fingerprint,
            responses[0][1]["proposal"]["checkFingerprint"],
        )
        self.assertEqual(
            fingerprint,
            proposal_write.call_args.args[1]["checkFingerprint"],
        )
        self.assertEqual(
            plugin_fingerprint,
            create_proposal.call_args.kwargs[
                "plugin_catalogue_fingerprint"
            ],
        )

    def test_stale_check_fingerprint_is_not_persisted(self):
        handler, responses = self.handler("a" * 64)

        with (
            patch.object(
                app,
                "read_workspace",
                return_value=(b"{}", {}, "revision-1"),
            ),
            patch.object(app, "apply_operations", return_value=({}, [])),
            patch.object(app, "validate_candidate", return_value=[]),
            patch.object(
                app,
                "proposal_check",
                return_value={"checkFingerprint": "b" * 64},
            ),
            patch.object(app, "proposal_create") as proposal_create,
            patch.object(app, "proposal_write") as proposal_write,
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.CONFLICT, responses[0][0])
        proposal_create.assert_not_called()
        proposal_write.assert_not_called()


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
            "pluginCatalogueFingerprint": app.plugin_catalogue()["fingerprint"],
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
            "pluginCatalogueFingerprint": app.plugin_catalogue()["fingerprint"],
            "diff": [],
        }

    @staticmethod
    def planning_timeout() -> app.VisualPlanningDatabaseError:
        return app.VisualPlanningDatabaseError(
            stage="layer-summary",
            query_purpose="feature-count-and-extent",
            timed_out=True,
        )

    def test_live_visual_timeout_reports_read_only_planning_stage(self):
        payload = {
            "layer": "Stops",
            "centre": [-1.532, 53.814],
            "zoom": 14,
        }
        handler, responses = self.handler("/api/visual-test", payload)
        running = {"id": "f" * 32, "status": "running"}

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
                return_value=(b"{}", self.proposal()["candidate"], "revision-1"),
            ),
            patch.object(
                app,
                "visual_plan",
                side_effect=self.planning_timeout(),
            ) as visual_plan,
            patch.object(app, "urlopen") as browser,
            patch.object(
                app.CONTROL, "create_operation", return_value=running,
            ) as create_operation,
            patch.object(
                app.CONTROL, "finish_operation", side_effect=finish,
            ) as finish_operation,
        ):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        self.assertEqual("visual.planning_timeout", body["code"])
        self.assertEqual("layer-summary", body["planningStage"])
        self.assertEqual("feature-count-and-extent", body["queryPurpose"])
        self.assertEqual(5000, body["timeoutMilliseconds"])
        self.assertNotIn("derived-layer", body["error"])
        self.assertNotIn("technicalDetail", body)
        self.assertEqual(payload, visual_plan.call_args.kwargs["visual_request"])
        browser.assert_not_called()
        create_operation.assert_called_once()
        self.assertEqual("failed", body["operation"]["status"])
        self.assertEqual(
            "visual.planning_timeout",
            finish_operation.call_args.kwargs["error"]["code"],
        )
        self.assertEqual(
            "planning",
            finish_operation.call_args.kwargs["error"]["failedStage"],
        )

    def test_live_visual_reports_no_matching_filtered_features(self):
        handler, responses = self.handler(
            "/api/visual-plan", {"layer": "Stops"}
        )
        with (
            patch.object(
                app,
                "read_workspace",
                return_value=(b"{}", self.proposal()["candidate"], "revision-1"),
            ),
            patch.object(
                app,
                "visual_plan",
                side_effect=app.VisualPlanningNoMatchingFeatures(
                    filter_applied=True,
                ),
            ),
        ):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        self.assertEqual("visual.no_matching_features", body["code"])
        self.assertEqual("layer-summary", body["planningStage"])
        self.assertEqual(
            "filtered-feature-count-and-extent", body["queryPurpose"]
        )
        self.assertTrue(body["defaultFilterApplied"])
        self.assertEqual(0, body["filteredFeatureCount"])
        self.assertIsNone(body["representativeFeature"])
        self.assertEqual("no-matching-renderable-geometry", body["reason"])

    def test_live_visual_test_sends_requested_information_evidence(self):
        payload = {
            "layer": "Stops",
            "expectedInfoPanelText": ["Source: ONS"],
        }
        handler, responses = self.handler("/api/visual-test", payload)
        browser_response = MagicMock()
        browser_response.__enter__.return_value = io.BytesIO(app.json.dumps({
            "runId": "run-live",
            "passed": True,
            "metadata": {
                "source": "live",
                "operationId": "a" * 32,
            },
            "interaction": {
                "infoPanelExpanded": True,
                "expectedInfoPanelTextFound": {"Source: ONS": True},
            },
            "artifacts": {"infoPanel": "run-live/info-panel.png"},
        }).encode())
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
                return_value=(b"{}", self.proposal()["candidate"], "revision-1"),
            ),
            patch.object(app, "visual_plan", return_value={
                "layer": "Stops",
                "locale": "locale",
                "interaction": {"type": "click-centre-feature"},
            }),
            patch.object(app, "plugin_preview_checks", return_value=[]),
            patch.object(app, "urlopen", return_value=browser_response) as browser,
            patch.object(
                app.CONTROL,
                "create_operation",
                return_value=running,
            ),
            patch.object(
                app.CONTROL,
                "finish_operation",
                side_effect=finish,
            ),
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        runner_request = browser.call_args.args[0]
        runner_payload = app.json.loads(runner_request.data)
        interaction = runner_payload["plan"]["interaction"]
        self.assertEqual(
            {"source": "live", "operationId": running["id"]},
            runner_payload["metadata"],
        )
        self.assertTrue(interaction["requireInfoPanel"])
        self.assertEqual(
            ["Source: ONS"],
            interaction["expectedInfoPanelText"],
        )
        self.assertEqual(HTTPStatus.OK, responses[0][0])
        self.assertTrue(
            responses[0][1]["visual"]["interaction"]
            ["expectedInfoPanelTextFound"]["Source: ONS"]
        )

    def test_proposal_visual_timeout_uses_same_safe_planning_error(self):
        payload = {"layer": "Stops"}
        handler, responses = self.handler(
            "/api/proposals/proposal-1/visual-plan",
            payload,
        )
        proposal = self.proposal()

        with (
            patch.object(app, "preview_proposal", return_value=proposal),
            patch.object(
                app,
                "visual_plan",
                side_effect=self.planning_timeout(),
            ) as visual_plan,
        ):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        self.assertEqual("visual.planning_timeout", body["code"])
        self.assertEqual("layer-summary", body["planningStage"])
        self.assertEqual("feature-count-and-extent", body["queryPurpose"])
        self.assertNotIn("technicalDetail", body)
        self.assertEqual(payload, visual_plan.call_args.kwargs["visual_request"])

    def test_proposal_visual_test_persists_planning_timeout(self):
        payload = {"layer": "Stops"}
        handler, responses = self.handler(
            "/api/proposals/proposal-1/visual-test",
            payload,
        )
        proposal = self.proposal()
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
            patch.object(
                app, "visual_plan", side_effect=self.planning_timeout(),
            ),
            patch.object(
                app.CONTROL, "create_operation", return_value=running,
            ),
            patch.object(
                app.CONTROL, "finish_operation", side_effect=finish,
            ) as finish_operation,
            patch.object(app, "run_browser_visual") as browser,
        ):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        self.assertEqual("failed", body["operation"]["status"])
        self.assertEqual(
            "visual.planning_timeout",
            finish_operation.call_args.kwargs["error"]["code"],
        )
        self.assertEqual(
            "planning",
            finish_operation.call_args.kwargs["error"]["failedStage"],
        )
        browser.assert_not_called()

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
        self.assertEqual(
            app.VISUAL_BROWSER_TIMEOUT_SECONDS * 1000,
            payload["runTimeout"],
        )

    def test_browser_transport_timeout_is_structured(self) -> None:
        with patch.object(app, "urlopen", side_effect=TimeoutError("hung")):
            status, result = app.run_browser_visual(
                "Stops",
                {"centre": [1, 2]},
                {"metadata": {"operationId": "a" * 32}},
                target_url="http://xyz-preview:3000",
            )

        self.assertEqual(HTTPStatus.GATEWAY_TIMEOUT, status)
        self.assertEqual(
            "visual.browser_transport_timeout", result["code"]
        )
        self.assertEqual("browser-transport", result["failedStage"])
        self.assertEqual("TimeoutError", result["diagnostics"]["exceptionType"])

    def test_browser_http_error_preserves_structured_runner_result(self) -> None:
        expected = {
            "error": "Visual runner is busy.",
            "metadata": {"source": "candidate", "operationId": "a" * 32},
            "state": "rejected",
        }
        error = app.HTTPError(
            "http://browser-runner:8080/run",
            HTTPStatus.TOO_MANY_REQUESTS,
            "busy",
            {},
            io.BytesIO(app.json.dumps(expected).encode()),
        )

        with patch.object(app, "urlopen", side_effect=error):
            status, result = app.run_browser_visual(
                "Stops",
                {"centre": [1, 2]},
                {"metadata": expected["metadata"]},
                target_url="http://xyz-preview:3000",
            )

        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, status)
        self.assertEqual(expected, result)

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

    def test_browser_request_omits_title_when_comparison_side_has_no_layer(self):
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"passed": true}')
        with patch.object(app, "urlopen", return_value=response) as urlopen:
            app.run_browser_visual(
                None,
                {"layerTitle": "Candidate-only title", "layers": []},
                {},
                target_url="http://xyz-preview:3000",
            )

        request = urlopen.call_args.args[0]
        payload = app.json.loads(request.data)
        self.assertIsNone(payload["layer"])
        self.assertIsNone(payload["layerTitle"])

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

    def test_feature_info_evidence_extracts_only_constant_visible_text(self):
        proposal = self.proposal()
        proposal["candidate"]["locale"]["layers"]["Stops"]["infoj"] = [
            {
                "type": "html",
                "title": "Data source and calculation",
                "field": "source_note",
                "fieldfx": (
                    "'<div><strong>Source:</strong> ONS Census "
                    "2021.</div>'::text"
                ),
            },
            {
                "type": "text",
                "title": "Dynamic",
                "field": "dynamic_note",
                "fieldfx": "concat('Source: ', source_name)",
            },
        ]

        evidence = app.proposal_feature_info_evidence(
            proposal,
            "Stops",
            "locale",
        )

        self.assertTrue(evidence["changed"])
        self.assertFalse(evidence["original"]["requested"])
        self.assertTrue(evidence["candidate"]["requested"])
        self.assertEqual(
            [
                "Data source and calculation",
                "Source: ONS Census 2021.",
                "Dynamic",
            ],
            evidence["candidate"]["expectedText"],
        )
        self.assertNotIn(
            "Source: ",
            evidence["candidate"]["expectedText"],
        )

    def test_expected_info_panel_text_is_bounded(self):
        self.assertEqual(
            ["Source", "Calculation"],
            app.expected_info_panel_text({
                "expectedInfoPanelText": [
                    " Source ",
                    "Calculation",
                    "Source",
                ],
            }),
        )
        with self.assertRaisesRegex(ValueError, "at most 20"):
            app.expected_info_panel_text({
                "expectedInfoPanelText": ["text"] * 21,
            })

    def test_feature_info_observation_requires_capture_and_expected_text(self):
        evidence = {
            "requested": True,
            "planned": True,
            "expectedText": ["Source: ONS"],
        }
        missing = app.feature_info_observation(
            {"interaction": None, "artifacts": {}},
            evidence,
        )
        not_found = app.feature_info_observation(
            {
                "interaction": {
                    "infoPanelExpanded": True,
                    "expectedInfoPanelTextFound": {"Source: ONS": False},
                },
                "artifacts": {"infoPanel": "run/info-panel.png"},
            },
            evidence,
        )

        self.assertFalse(missing["passed"])
        self.assertFalse(missing["captured"])
        self.assertFalse(not_found["passed"])
        self.assertTrue(not_found["captured"])
        self.assertEqual(
            {"Source: ONS": False},
            not_found["expectedTextFound"],
        )

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
                side_effect=lambda _layer, _plan, request, **_kwargs: (
                    HTTPStatus.OK,
                    {
                        "runId": "run-candidate",
                        "passed": True,
                        "metadata": request["metadata"],
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
        proposal["original"]["locale"]["layers"]["OriginalOnly"] = {
            "format": "tiles",
            "display": True,
        }
        proposal["originalHash"] = app.workspace_hash(proposal["original"])
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
        candidate_metadata = runner.call_args.args[2]["metadata"]
        original_metadata = runner.call_args_list[0].args[2]["metadata"]
        original_diagnostics = runner.call_args_list[0].args[1][
            "candidateLayerDiagnostics"
        ]
        candidate_diagnostics = runner.call_args_list[1].args[1][
            "candidateLayerDiagnostics"
        ]
        self.assertEqual(
            ["Stops", "OriginalOnly"],
            original_diagnostics["configuredLayerKeys"],
        )
        self.assertEqual(
            ["Stops"], candidate_diagnostics["configuredLayerKeys"]
        )
        self.assertEqual(
            ["OriginalOnly"],
            runner.call_args_list[0].args[1]["backgroundLayers"],
        )
        self.assertEqual(
            [], runner.call_args_list[1].args[1]["backgroundLayers"]
        )
        self.assertEqual(binding, {
            key: candidate_metadata[key] for key in binding
        })
        self.assertEqual(original_binding, {
            key: original_metadata[key] for key in original_binding
        })
        self.assertEqual(
            responses[0][1]["operation"]["id"],
            candidate_metadata["operationId"],
        )
        self.assertEqual(
            candidate_metadata["operationId"],
            original_metadata["operationId"],
        )
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
        binding["operationId"] = running["id"]
        original_binding["operationId"] = running["id"]

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

    def test_busy_screenshot_is_structured_and_not_a_binding_mismatch(self):
        handler, responses = self.handler(
            "/api/proposals/proposal-1/screenshot", {"layer": "Stops"}
        )
        proposal = self.proposal()
        running = {"id": "f" * 32, "status": "running"}

        def run(_layer, _plan, _payload, **_kwargs):
            return HTTPStatus.TOO_MANY_REQUESTS, {
                "error": "Visual runner is busy.",
                "state": "rejected",
            }

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
            patch.object(app, "prepare_original_preview", return_value={}),
            patch.object(app, "prepare_candidate_preview", return_value={}),
            patch.object(app, "run_browser_visual", side_effect=run),
            patch.object(
                app.CONTROL, "create_operation", return_value=running
            ),
            patch.object(
                app.CONTROL, "finish_operation", side_effect=finish
            ),
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.TOO_MANY_REQUESTS, status)
        self.assertEqual("visual.busy", body["operation"]["error"]["code"])
        self.assertNotEqual(
            "visual.binding_mismatch", body["operation"]["error"]["code"]
        )
        self.assertEqual(running["id"], body["operationId"])
        self.assertEqual(body["visual"], body["operation"]["result"]["visual"])

    def test_failed_preview_retains_bound_report_in_operation(self):
        handler, responses = self.handler(
            "/api/proposals/proposal-1/visual-test", {"layer": "Stops"}
        )
        proposal = self.proposal()
        running = {"id": "9" * 32, "status": "running"}

        def run(_layer, _plan, payload, **_kwargs):
            return HTTPStatus.UNPROCESSABLE_ENTITY, {
                "runId": "run-failed",
                "passed": False,
                "metadata": payload["metadata"],
                "diagnosis": {"outcome": "failed"},
                "artifacts": {"report": "run-failed/report.json"},
            }

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
            patch.object(app, "prepare_candidate_preview", return_value={}),
            patch.object(app, "run_browser_visual", side_effect=run),
            patch.object(
                app.CONTROL, "create_operation", return_value=running
            ),
            patch.object(
                app.CONTROL, "finish_operation", side_effect=finish
            ),
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        status, body = responses[0]
        self.assertEqual(HTTPStatus.UNPROCESSABLE_ENTITY, status)
        retained = body["operation"]["result"]
        self.assertEqual(running["id"], retained["operationId"])
        self.assertEqual(
            "run-failed/report.json",
            retained["visual"]["artifacts"]["report"],
        )

    def test_background_preview_returns_pollable_operation_before_browser_finishes(self):
        handler, responses = self.handler(
            "/api/proposals/proposal-1/visual-test",
            {"layer": "Stops", "background": True},
        )
        proposal = self.proposal()
        browser_started = threading.Event()
        release_browser = threading.Event()

        def run(_layer, _plan, payload, **_kwargs):
            browser_started.set()
            self.assertTrue(release_browser.wait(2))
            return HTTPStatus.OK, {
                "runId": "run-background",
                "passed": True,
                "metadata": payload["metadata"],
                "artifacts": {"report": "run-background/report.json"},
            }

        with tempfile.TemporaryDirectory() as directory:
            control = ControlStore(Path(directory))
            with (
                patch.object(app, "CONTROL", control),
                patch.object(app, "preview_proposal", return_value=proposal),
                patch.object(app, "visual_plan", return_value={"centre": [1, 2]}),
                patch.object(app, "prepare_candidate_preview", return_value={}),
                patch.object(app, "run_browser_visual", side_effect=run),
                patch.object(control, "audit"),
            ):
                handler.do_POST()
                self.assertEqual(HTTPStatus.ACCEPTED, responses[0][0])
                operation_id = responses[0][1]["operation"]["id"]
                self.assertEqual(
                    f"/api/operations/{operation_id}",
                    responses[0][1]["statusUrl"],
                )
                self.assertTrue(browser_started.wait(2))
                self.assertEqual(
                    "running", control.read_operation(operation_id)["status"]
                )
                release_browser.set()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    operation = control.read_operation(operation_id)
                    if operation["status"] != "running":
                        break
                    time.sleep(0.01)

            self.assertEqual("succeeded", operation["status"])
            self.assertEqual(
                "run-background/report.json",
                operation["result"]["visual"]["artifacts"]["report"],
            )
            self.assertEqual(
                operation_id, operation["result"]["operationId"]
            )

    def test_visual_watchdog_persists_terminal_stage_diagnostics(self):
        release_worker = threading.Event()
        worker = threading.Thread(target=release_worker.wait, daemon=True)

        with tempfile.TemporaryDirectory() as directory:
            control = ControlStore(Path(directory))
            operation = control.create_operation(
                "proposal.visual-test",
                "token:test",
                {"proposalId": "proposal-1"},
            )
            control.update_operation_progress(
                operation["id"],
                stage="screenshot-capture",
                diagnostics={"pageErrors": ["Chromium exited"]},
            )
            worker.start()
            with patch.object(app, "CONTROL", control):
                app.watch_visual_background(operation["id"], worker, 0.01)

            terminal = control.read_operation(operation["id"])
            self.assertEqual("failed", terminal["status"])
            self.assertEqual(
                "visual.operation_timeout", terminal["error"]["code"]
            )
            self.assertEqual(
                "screenshot-capture", terminal["error"]["failedStage"]
            )
            self.assertEqual(
                ["Chromium exited"],
                terminal["error"]["diagnostics"]["pageErrors"],
            )
            release_worker.set()
            worker.join(1)

            abandoned = control.create_operation(
                "proposal.visual-test",
                "token:test",
                {"proposalId": "proposal-2"},
            )
            control.update_operation_progress(
                abandoned["id"], stage="result-persistence"
            )
            exited_worker = threading.Thread(target=lambda: None)
            exited_worker.start()
            exited_worker.join(1)
            with patch.object(app, "CONTROL", control):
                app.watch_visual_background(
                    abandoned["id"], exited_worker, 0.01
                )
            abandoned_terminal = control.read_operation(abandoned["id"])
            self.assertEqual("failed", abandoned_terminal["status"])
            self.assertEqual(
                "visual.result_persistence_failed",
                abandoned_terminal["error"]["code"],
            )

    def test_visual_terminal_persistence_retries_transient_write_failure(self):
        terminal = {"id": "a" * 32, "status": "failed"}
        control = Mock()
        control.finish_operation.side_effect = [
            OSError("temporary write failure"),
            terminal,
        ]

        with (
            patch.object(app, "CONTROL", control),
            patch.object(app.time, "sleep"),
        ):
            result = app.finish_visual_operation(
                "a" * 32,
                status="failed",
                error={"code": "visual.failed"},
            )

        self.assertEqual(terminal, result)
        self.assertEqual(2, control.finish_operation.call_count)

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
            expected = plan["interaction"]["expectedInfoPanelText"]
            return HTTPStatus.OK, {
                "runId": f"run-{source}",
                "passed": True,
                "metadata": payload["metadata"],
                "interaction": {
                    "infoPanelExpanded": True,
                    "expectedInfoPanelTextFound": {
                        text: True for text in expected
                    },
                },
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

    def test_added_layer_screenshot_captures_candidate_static_source_note(self):
        handler, responses = self.handler(
            "/api/proposals/proposal-1/screenshot",
            {
                "layer": "Added",
                "expectedInfoPanelText": ["ONS Census 2021"],
            },
        )
        proposal = self.proposal()
        proposal["candidate"]["locale"]["layers"]["Added"] = {
            "name": "Added layer",
            "infoj": [{
                "type": "html",
                "title": "Data source and calculation",
                "field": "source_note",
                "fieldfx": (
                    "'<div><strong>Source:</strong> ONS Census "
                    "2021.</div>'::text"
                ),
            }],
        }
        proposal["candidateHash"] = app.workspace_hash(proposal["candidate"])

        def run(layer, plan, payload, **kwargs):
            source = payload["metadata"]["source"]
            if source == "original":
                self.assertIsNone(layer)
                self.assertNotIn("interaction", plan)
                return HTTPStatus.OK, {
                    "runId": "run-original",
                    "passed": True,
                    "metadata": payload["metadata"],
                    "interaction": None,
                    "artifacts": {
                        "beforePage": "run-original/before-page.png",
                        "beforeMap": "run-original/before-map.png",
                        "report": "run-original/report.json",
                    },
                }
            self.assertEqual("Added", layer)
            self.assertTrue(plan["interaction"]["requireInfoPanel"])
            self.assertEqual(
                [
                    "Data source and calculation",
                    "Source: ONS Census 2021.",
                    "ONS Census 2021",
                ],
                plan["interaction"]["expectedInfoPanelText"],
            )
            found = {
                text: True
                for text in plan["interaction"]["expectedInfoPanelText"]
            }
            return HTTPStatus.OK, {
                "runId": "run-candidate",
                "passed": True,
                "metadata": payload["metadata"],
                "interaction": {
                    "infoPanelExpanded": True,
                    "expectedInfoPanelTextFound": found,
                },
                "artifacts": {
                    "afterPage": "run-candidate/after-page.png",
                    "afterMap": "run-candidate/after-map.png",
                    "infoPanel": "run-candidate/info-panel.png",
                    "report": "run-candidate/report.json",
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
            patch.object(app, "run_browser_visual", side_effect=run),
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        self.assertEqual(HTTPStatus.OK, responses[0][0])
        visual = responses[0][1]["visual"]
        evidence = visual["comparison"]["featureInfoEvidence"]
        self.assertFalse(evidence["original"]["requested"])
        self.assertTrue(evidence["original"]["passed"])
        self.assertTrue(evidence["candidate"]["requested"])
        self.assertTrue(evidence["candidate"]["captured"])
        self.assertTrue(evidence["candidate"]["passed"])
        self.assertEqual(
            "run-original/before-page.png",
            visual["artifacts"]["beforePage"],
        )
        self.assertIsNone(visual["artifacts"]["beforeInfoPanel"])
        self.assertEqual(
            "run-candidate/after-page.png",
            visual["artifacts"]["afterPage"],
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

    def test_preview_rejects_a_changed_plugin_catalogue(self) -> None:
        proposal = self.proposal()
        proposal["pluginCatalogueFingerprint"] = "stale"
        with (
            patch.object(app, "proposal_read", return_value=proposal),
            patch.object(app, "read_workspace", return_value=(b"{}", {}, "revision-1")),
        ):
            with self.assertRaisesRegex(FileExistsError, "plugin catalogue changed"):
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

    def test_browser_request_propagates_hover_evidence_options(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"passed": true}')
        with patch.object(app, "urlopen", return_value=response) as urlopen:
            status, result = app.run_browser_visual(
                "Stops",
                {
                    "centre": [1, 2],
                    "hover": {
                        "type": "hover-centre-feature",
                        "field": "stop_name",
                        "title": "Stop name",
                    },
                },
                {
                    "hover": True,
                    "expectedHoverText": ["City Square"],
                },
                target_url="http://xyz-preview:3000",
            )

        request = urlopen.call_args.args[0]
        payload = app.json.loads(request.data)
        self.assertEqual(HTTPStatus.OK, status)
        self.assertTrue(result["passed"])
        self.assertIs(payload["hover"], True)
        self.assertEqual(["City Square"], payload["expectedHoverText"])

    def test_proposal_screenshot_plans_hover_for_each_workspace_side(self) -> None:
        handler, responses = self.handler(
            "/api/proposals/proposal-1/screenshot",
            {"layer": "Stops", "hover": True},
        )
        proposal = self.proposal()
        for side, field in (
            ("original", "old_name"),
            ("candidate", "new_name"),
        ):
            proposal[side]["locale"]["layers"]["Stops"]["style"] = {
                "hover": {
                    "display": True,
                    "field": field,
                    "title": "Stop name",
                },
            }
            proposal[f"{side}Hash"] = app.workspace_hash(proposal[side])
        observed = {}

        def run(layer, plan, payload, **kwargs):
            source = payload["metadata"]["source"]
            observed[source] = plan["hover"]["field"]
            return HTTPStatus.OK, {
                "runId": f"run-{source}",
                "passed": True,
                "metadata": payload["metadata"],
                "artifacts": {
                    "beforePage": f"run-{source}/before-page.png",
                    "beforeMap": f"run-{source}/before-map.png",
                    "hoverTooltip": f"run-{source}/hover-tooltip.png",
                    "report": f"run-{source}/report.json",
                },
            }

        with (
            patch.object(app, "preview_proposal", return_value=proposal),
            patch.object(app, "visual_plan", return_value={
                "locale": "locale",
                "hover": {
                    "type": "hover-centre-feature",
                    "field": "new_name",
                    "title": "Stop name",
                },
            }),
            patch.object(
                app, "prepare_original_preview",
                return_value={"source": "original", "generation": 1},
            ),
            patch.object(
                app, "prepare_candidate_preview",
                return_value={"source": "candidate", "generation": 2},
            ),
            patch.object(app, "run_browser_visual", side_effect=run),
            patch.object(app.CONTROL, "audit"),
        ):
            handler.do_POST()

        self.assertEqual(
            {"original": "old_name", "candidate": "new_name"},
            observed,
        )
        artifacts = responses[0][1]["visual"]["artifacts"]
        self.assertEqual(
            "run-original/hover-tooltip.png",
            artifacts["beforeHoverTooltip"],
        )
        self.assertEqual(
            "run-candidate/hover-tooltip.png",
            artifacts["afterHoverTooltip"],
        )

    def test_hover_evidence_options_are_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "hover must be true or false"):
            app.requested_hover({"hover": "yes"})
        with self.assertRaisesRegex(ValueError, "at most 20 strings"):
            app.expected_hover_text({
                "expectedHoverText": [str(index) for index in range(21)],
            })


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
        semantic = self.handler(["semantic:inspect"])
        self.assertEqual(
            "token:test",
            semantic._authorized(required_scope="semantic:inspect"),
        )
        self.assertIsNone(
            semantic._authorized(required_scope="semantic:propose")
        )

    def test_capabilities_route_returns_runtime_pagination_contract(self):
        handler = self.handler(["semantic:inspect"])
        handler.path = "/api/capabilities"
        handler._host_allowed = lambda: True
        control = Mock()
        control.instance_id.return_value = "instance-1"

        with patch.object(app, "CONTROL", control):
            handler.do_GET()

        status, payload = handler._json.call_args.args
        self.assertEqual(HTTPStatus.OK, status)
        expected = dict(app.contract("instance-1")["pagination"])
        expected.pop("compatibilityArtifact")
        self.assertEqual(expected, payload["pagination"])

    def test_semantic_routes_use_narrow_scopes(self):
        cases = {
            ("GET", "/api/semantic/status"): "semantic:inspect",
            ("GET", "/api/semantic/catalog"): "semantic:inspect",
            (
                "GET",
                "/api/semantic/catalog/search",
            ): "semantic:inspect",
            (
                "GET",
                "/api/semantic/catalog/objects/asset%3Aderived%2Froads",
            ): "semantic:inspect",
            (
                "GET",
                "/api/semantic/catalog/objects/"
                "asset%3Aderived%2Froads/history",
            ): "semantic:inspect",
            (
                "GET",
                "/api/semantic/derived-profiles",
            ): "semantic:inspect",
            (
                "GET",
                "/api/semantic/derived-profiles/example",
            ): "semantic:inspect",
            ("GET", "/api/semantic/proposals"): "semantic:inspect",
            (
                "GET",
                "/api/semantic/proposals/proposal-1",
            ): "semantic:inspect",
            ("POST", "/api/semantic/generate"): "semantic:generate",
            (
                "POST",
                "/api/semantic/source/archive-excluded",
            ): "semantic:admin",
            (
                "POST",
                "/api/semantic/catalog/objects/asset-1/archive",
            ): "semantic:admin",
            ("POST", "/api/semantic/proposals/check"): "semantic:propose",
            ("POST", "/api/semantic/proposals"): "semantic:propose",
            (
                "POST",
                "/api/semantic/proposals/proposal-1/apply",
            ): "semantic:apply",
            (
                "POST",
                "/api/semantic/proposals/proposal-1/decline",
            ): "semantic:propose",
            (
                "POST",
                "/api/semantic/derived-profiles/example/repair",
            ): "semantic:admin",
        }
        for (method, path), expected in cases.items():
            with self.subTest(method=method, path=path):
                self.assertEqual(
                    expected,
                    app.Handler._required_scope(path, method),
                )
                allowed = self.handler([expected])
                self.assertEqual(
                    "token:test",
                    allowed._authorized(required_scope=expected),
                )
                denied = self.handler(["inspect"])
                self.assertIsNone(
                    denied._authorized(required_scope=expected)
                )
                self.assertEqual(
                    expected,
                    denied._json.call_args.args[1]["requiredScope"],
                )

    def test_every_token_scope_is_isolated_at_each_narrow_route_gate(self):
        required_scopes = TOKEN_SCOPES - {"full"}
        for required in sorted(required_scopes):
            for granted in sorted(TOKEN_SCOPES):
                with self.subTest(required=required, granted=granted):
                    handler = self.handler([granted])
                    result = handler._authorized(required_scope=required)
                    if granted in {required, "full"}:
                        self.assertEqual("token:test", result)
                        handler._json.assert_not_called()
                    else:
                        self.assertIsNone(result)
                        self.assertEqual(
                            required,
                            handler._json.call_args.args[1]["requiredScope"],
                        )

    def test_administrator_session_can_cross_each_narrow_route_gate(self):
        for required in sorted(TOKEN_SCOPES - {"full"}):
            with self.subTest(required=required):
                handler = self.handler([])
                handler._actor = lambda state_change=False: "admin"
                self.assertEqual(
                    "admin",
                    handler._authorized(required_scope=required),
                )
                handler._json.assert_not_called()

    def test_unauthenticated_actor_cannot_cross_any_narrow_route_gate(self):
        for required in sorted(TOKEN_SCOPES - {"full"}):
            with self.subTest(required=required):
                handler = self.handler([])
                handler._actor = lambda state_change=False: None
                self.assertIsNone(
                    handler._authorized(required_scope=required)
                )
                self.assertEqual(
                    HTTPStatus.UNAUTHORIZED,
                    handler._json.call_args.args[0],
                )
                self.assertEqual(
                    "Authentication required.",
                    handler._json.call_args.args[1]["error"],
                )
                self.assertEqual(
                    "auth.authentication_required",
                    handler._json.call_args.args[1]["code"],
                )

    def test_narrow_scopes_are_forwarded_without_implicit_expansion(self):
        semantic_scopes = {
            "semantic:inspect",
            "semantic:source",
            "semantic:generate",
            "semantic:data",
            "semantic:propose",
            "semantic:apply",
            "semantic:admin",
        }
        for granted in sorted(TOKEN_SCOPES - {"full"}):
            with self.subTest(granted=granted):
                handler = self.handler([granted])
                self.assertEqual(
                    [granted],
                    handler._semantic_scopes("token:test"),
                )

        combined = self.handler([
            "semantic:inspect",
            "semantic:generate",
            "semantic:admin",
        ])
        self.assertEqual(
            [
                "semantic:inspect",
                "semantic:generate",
                "semantic:admin",
            ],
            combined._semantic_scopes("token:test"),
        )

        administrator = self.handler([])
        self.assertEqual(
            semantic_scopes,
            set(administrator._semantic_scopes("admin")),
        )

    def test_semantic_action_scopes_require_exact_routes(self):
        cases = {
            "/api/semantic/generate/": "semantic:admin",
            "/api/semantic/not-generate": "semantic:admin",
            "/api/semantic/proposals//apply": "semantic:admin",
            "/api/semantic/proposals/proposal-1/apply/": "semantic:admin",
            "/api/semantic/proposals/proposal-1/extra/apply": "semantic:admin",
            "/api/semantic/proposals/proposal-1/extra/decline": "semantic:admin",
            "/api/semantic/derived-profiles/example/repair/": "semantic:admin",
            "/api/semantic/derived-profiles/example/extra/repair": (
                "semantic:admin"
            ),
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(
                    expected,
                    app.Handler._required_scope(path, "POST"),
                )
                self.assertIsNone(app.semantic_proxy_path(path))

    def test_workspace_proposal_action_scopes_require_exact_routes(self):
        self.assertEqual(
            "apply",
            app.Handler._required_scope(
                "/api/proposals/proposal-1/apply",
                "POST",
            ),
        )
        self.assertEqual(
            "propose",
            app.Handler._required_scope(
                "/api/proposals/proposal-1/decline",
                "POST",
            ),
        )
        for path in (
            "/api/not-proposals/proposal-1/apply",
            "/api/proposals/proposal-1/extra/apply",
            "/api/not-proposals/proposal-1/decline",
            "/api/proposals/proposal-1/extra/decline",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    "full",
                    app.Handler._required_scope(path, "POST"),
                )

    def test_workspace_proposal_actions_reject_suffix_aliases(self):
        for path in (
            "/api/not-proposals/proposal-1/apply",
            "/api/proposals/proposal-1/extra/apply",
            "/api/not-proposals/proposal-1/decline",
            "/api/proposals/proposal-1/extra/decline",
        ):
            with self.subTest(path=path):
                responses = []
                handler = object.__new__(app.Handler)
                handler.path = path
                handler._host_allowed = lambda: True
                handler._authorized = (
                    lambda state_change=False: "admin"
                )
                handler._json = (
                    lambda status, body: responses.append((status, body))
                )
                handler.send_error = (
                    lambda status: responses.append((status, {}))
                )

                handler.do_POST()

                self.assertEqual(
                    [(HTTPStatus.NOT_FOUND, {})],
                    responses,
                )

    def test_semantic_only_tokens_can_discover_contract_and_identity(self):
        self.assertIsNone(
            app.Handler._required_scope("/api/capabilities", "GET")
        )
        self.assertIsNone(
            app.Handler._required_scope("/api/contract", "GET")
        )
        self.assertIsNone(
            app.Handler._required_scope("/api/auth/me", "GET")
        )
        semantic = self.handler(["semantic:inspect"])
        self.assertEqual("token:test", semantic._authorized())

    def test_every_nonempty_token_scope_can_discover_api_identity(self):
        for granted in sorted(TOKEN_SCOPES):
            with self.subTest(granted=granted):
                handler = self.handler([granted])
                self.assertEqual("token:test", handler._authorized())
                handler._json.assert_not_called()

    def test_legacy_full_scope_expands_at_private_semantic_boundary(self):
        handler = object.__new__(app.Handler)
        handler._authentication = {"scopes": ["full"]}

        scopes = handler._semantic_scopes("token:legacy")

        self.assertIn("full", scopes)
        self.assertIn("semantic:inspect", scopes)
        self.assertIn("semantic:generate", scopes)
        self.assertIn("semantic:data", scopes)
        self.assertIn("semantic:propose", scopes)
        self.assertIn("semantic:apply", scopes)
        self.assertIn("semantic:admin", scopes)

    def test_semantic_proxy_paths_are_closed_and_exact(self):
        self.assertEqual(
            "/v1/status",
            app.semantic_proxy_path("/api/semantic/status"),
        )
        self.assertEqual(
            "/v1/search?q=bus%20stops",
            app.semantic_proxy_path(
                "/api/semantic/catalog/search",
                "q=bus%20stops",
            ),
        )
        self.assertEqual(
            "/v1/assets/asset%3Atransport.bus",
            app.semantic_proxy_path(
                "/api/semantic/catalog/objects/asset%3Atransport.bus"
            ),
        )
        self.assertEqual(
            "/v1/assets/asset%3Atransport.bus/history",
            app.semantic_proxy_path(
                "/api/semantic/catalog/objects/"
                "asset%3Atransport.bus/history"
            ),
        )
        for path in (
            "/api/semantic",
            "/api/semantic/unknown",
            "/api/semantic/catalog/objects/a/b",
            "/api/semantic/proposals/x/unknown",
        ):
            with self.subTest(path=path):
                self.assertIsNone(app.semantic_proxy_path(path))


class FederationVerificationTickTests(unittest.TestCase):
    """The tick that the periodic verifier will call.

    Kept separate from any loop or thread so the behaviour that matters is
    reachable from a test: run_semantic_outbox, the pattern this follows, has
    no tests at all because its logic lives inside a while True.
    """

    @staticmethod
    def alias(name, **overrides):
        record = {
            "alias": name,
            "provisionedAt": "2026-08-11T00:00:00Z",
            "connectionRef": "LEEDS_EXT",
            "allowedRelations": ["leeds.smoke_control_orders"],
            "tlsPolicy": "require",
            "acceptedEvidenceComplete": True,
            "lastObservationId": 1,
        }
        record.update(overrides)
        return record

    def test_observes_each_provisioned_source_with_its_own_settings(self):
        federation = MagicMock()
        federation.list.return_value = [
            self.alias("leeds_ext"),
            self.alias("bristol_ext", connectionRef="BRISTOL", tlsPolicy="verify-full"),
        ]
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS",
            {"LEEDS_EXT": "postgresql://leeds", "BRISTOL": "postgresql://bristol"},
        ):
            summary = app.verify_federation_sources()

        self.assertEqual({"observed": 2, "failed": 0, "skipped": 0, "deferred": 0}, summary)
        observed = {
            call.args[0]: call for call in federation.observe.call_args_list
        }
        self.assertEqual({"leeds_ext", "bristol_ext"}, set(observed))
        # Each alias must be probed with its own reference and policy; reusing
        # the first alias's settings would silently verify the wrong thing.
        self.assertEqual("postgresql://bristol", observed["bristol_ext"].args[1])
        self.assertEqual(
            "verify-full", observed["bristol_ext"].kwargs["tls_policy"]
        )

    def test_skips_sources_that_expose_nothing_yet(self):
        # A pending alias has no access to keep honest, so probing it would be
        # an outbound connection to a third party for no reason.
        federation = MagicMock()
        federation.list.return_value = [self.alias("pending_ext", provisionedAt=None)]
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": "postgresql://leeds"}
        ):
            summary = app.verify_federation_sources()

        federation.observe.assert_not_called()
        self.assertEqual({"observed": 0, "failed": 0, "skipped": 1, "deferred": 0}, summary)

    def test_never_observes_a_retired_alias(self):
        # Retired exclusion comes from list(). Pinning it here means a change
        # to that filter fails a test rather than quietly putting a
        # decommissioned source back on a timer.
        federation = MagicMock()
        federation.list.return_value = []
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {}
        ):
            app.verify_federation_sources()

        statement = str(federation.list.call_args)
        self.assertTrue(federation.list.called, statement)
        federation.observe.assert_not_called()

    def test_never_revokes_an_alias_it_could_never_restore(self):
        # An alias approved before the accepted-evidence columns existed can
        # never satisfy the currency test, so observing it on a timer revokes
        # every pass and only an operator provision can undo it. The interval
        # bounds a false revoke only for sources that can recover; this class
        # cannot, so the timer must leave it alone.
        federation = MagicMock()
        federation.list.return_value = [
            self.alias("pre_migration", acceptedEvidenceComplete=False),
            self.alias("leeds_ext"),
        ]
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": "postgresql://leeds"}
        ):
            summary = app.verify_federation_sources()

        self.assertEqual(
            ["leeds_ext"],
            [call.args[0] for call in federation.observe.call_args_list],
        )
        self.assertEqual({"observed": 1, "failed": 0, "skipped": 1, "deferred": 0}, summary)

    def test_only_scopes_a_pass_to_a_single_alias(self):
        # The timer never passes this. A test that stops a shared source must
        # not revoke every other alias pointing at it, since its teardown only
        # knows about its own probe.
        federation = MagicMock()
        federation.list.return_value = [
            self.alias("e2e_probe"),
            self.alias("someone_elses"),
        ]
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": "postgresql://leeds"}
        ):
            summary = app.verify_federation_sources(only="e2e_probe")

        self.assertEqual(
            ["e2e_probe"],
            [call.args[0] for call in federation.observe.call_args_list],
        )
        self.assertEqual(1, summary["observed"])

    def test_a_vanished_connection_reference_withdraws_access(self):
        # No observation can reach this alias, but its foreign tables keep
        # working because the user mapping still holds the credential. Without
        # this, both consumer roles read a source nothing can verify, and every
        # other revoke path runs from an observation that cannot happen.
        federation = MagicMock()
        federation.list.return_value = [self.alias("gone", connectionRef="REMOVED")]
        federation.mark_unverifiable.return_value = True
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {}
        ), self.assertLogs(app.LOGGER, level="WARNING") as logs:
            summary = app.verify_federation_sources()

        federation.mark_unverifiable.assert_called_once_with("gone")
        federation.observe.assert_not_called()
        self.assertEqual(1, summary["failed"])
        self.assertIn("REMOVED", "\n".join(logs.output))

    def test_a_vanished_reference_logs_only_when_something_changed(self):
        # mark_unverifiable reports whether it changed anything, so a source
        # left de-configured does not warn every fifteen minutes forever.
        federation = MagicMock()
        federation.list.return_value = [self.alias("gone", connectionRef="REMOVED")]
        federation.mark_unverifiable.return_value = False
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {}
        ):
            with self.assertNoLogs(app.LOGGER, level="WARNING"):
                summary = app.verify_federation_sources()

        federation.mark_unverifiable.assert_called_once_with("gone")
        self.assertEqual(1, summary["failed"])

    def test_a_probe_that_outruns_its_transaction_withdraws_access(self):
        # observe() holds its local transaction across the probe, so this means
        # the probe outran the transaction budget rather than a remote
        # statement exceeding its limit. Nothing was persisted, so without this
        # the source too slow to verify keeps access while one merely down
        # loses it in five seconds.
        import psycopg

        federation = MagicMock()
        federation.list.return_value = [self.alias("slow")]
        federation.observe.side_effect = (
            psycopg.errors.IdleInTransactionSessionTimeout("terminating")
        )
        federation.mark_unverifiable.return_value = True
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": "postgresql://leeds"}
        ), self.assertLogs(app.LOGGER, level="WARNING"):
            summary = app.verify_federation_sources()

        federation.mark_unverifiable.assert_called_once_with("slow")
        self.assertEqual(1, summary["failed"])

    def test_a_pass_stops_starting_work_once_its_budget_is_spent(self):
        # The traversal is serial and one alias can consume the whole
        # idle-transaction allowance, so without a budget the interval is not a
        # staleness bound: an alias near the end of a hundred could wait most
        # of a day for its first check.
        federation = MagicMock()
        federation.list.return_value = [
            self.alias("first", lastObservationId=1),
            self.alias("second", lastObservationId=2),
            self.alias("third", lastObservationId=3),
        ]
        clock = iter([0.0, 0.0, 10_000.0, 10_000.0])
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": "postgresql://leeds"}
        ), patch.object(app.time, "monotonic", lambda: next(clock)),                 self.assertLogs(app.LOGGER, level="WARNING") as logs:
            summary = app.verify_federation_sources()

        # One observed before the budget ran out; the rest deferred, not lost.
        self.assertEqual(1, summary["observed"])
        self.assertEqual(2, summary["deferred"])
        self.assertEqual(
            ["first"],
            [call.args[0] for call in federation.observe.call_args_list],
        )
        self.assertIn("deferred", "\n".join(logs.output))

    def test_least_recently_verified_aliases_go_first(self):
        # Ordering by alias would let a slow source early in the alphabet
        # starve everything after it forever, because a deferred tail is only
        # reached by a pass that happens to run fast enough.
        federation = MagicMock()
        federation.list.return_value = [
            self.alias("aaa_recent", lastObservationId=99),
            self.alias("zzz_stale", lastObservationId=2),
            self.alias("mmm_never", lastObservationId=None),
        ]
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": "postgresql://leeds"}
        ):
            app.verify_federation_sources()

        self.assertEqual(
            ["mmm_never", "zzz_stale", "aaa_recent"],
            [call.args[0] for call in federation.observe.call_args_list],
        )

    def test_one_failing_alias_does_not_strand_the_others(self):
        # A single removed connectionRef would otherwise leave every source
        # after it in the list unverified until someone noticed.
        federation = MagicMock()
        federation.list.return_value = [
            self.alias("gone_ext", connectionRef="REMOVED"),
            self.alias("leeds_ext"),
        ]
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": "postgresql://leeds"}
        ), self.assertLogs(app.LOGGER, level="WARNING") as logs:
            summary = app.verify_federation_sources()

        self.assertEqual({"observed": 1, "failed": 1, "skipped": 0, "deferred": 0}, summary)
        self.assertEqual(
            ["leeds_ext"],
            [call.args[0] for call in federation.observe.call_args_list],
        )
        self.assertIn("gone_ext", "\n".join(logs.output))

    def test_a_source_going_unreachable_is_not_counted_as_a_failure(self):
        # detect_capability returns connectivity 'unavailable' rather than
        # raising, so this is an ordinary observation -- the exact condition
        # the verifier exists to notice, not an error to log.
        federation = MagicMock()
        federation.list.return_value = [self.alias("leeds_ext")]
        federation.observe.return_value = {
            "alias": "leeds_ext",
            "status": "unavailable",
            "lastObservation": {"connectivity": "unavailable"},
        }
        with patch.object(app, "FEDERATION", federation), patch.object(
            app, "FEDERATION_CONNECTIONS", {"LEEDS_EXT": "postgresql://leeds"}
        ):
            summary = app.verify_federation_sources()

        self.assertEqual({"observed": 1, "failed": 0, "skipped": 0, "deferred": 0}, summary)

    def test_is_inert_when_federation_is_not_configured(self):
        # Outside bundled mode FEDERATION is None; the tick must be safe to
        # call rather than raising into a background thread.
        with patch.object(app, "FEDERATION", None):
            self.assertEqual(
                {"observed": 0, "failed": 0, "skipped": 0, "deferred": 0},
                app.verify_federation_sources(),
            )


class FederationVerifierLoopTests(unittest.TestCase):
    def test_runs_a_pass_before_it_ever_waits(self):
        # A source that broke while the service was down should be caught at
        # startup, not one interval later.
        order = []

        def sleep(seconds):
            order.append(("wait", seconds))
            raise StopIteration

        with patch.object(app.time, "sleep", sleep), patch.object(
            app, "verify_federation_sources",
            side_effect=lambda: order.append(("pass", None)) or {
                "observed": 1, "failed": 0, "skipped": 0
            },
        ):
            with self.assertRaises(StopIteration):
                app.run_federation_verifier()

        self.assertEqual(
            [("pass", None), ("wait", app.FEDERATION_VERIFY_INTERVAL_SECONDS)],
            order,
        )

    def test_a_failed_pass_does_not_end_the_thread(self):
        # One unreachable registry must not silently stop verification for the
        # lifetime of the process.
        import psycopg

        calls = []
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            if len(waits) >= 2:
                raise StopIteration

        def pass_then_raise():
            calls.append(1)
            if len(calls) == 1:
                raise psycopg.OperationalError("registry down")
            return {"observed": 1, "failed": 0, "skipped": 0, "deferred": 0}

        with patch.object(app.time, "sleep", sleep), patch.object(
            app, "verify_federation_sources", side_effect=pass_then_raise
        ), self.assertLogs(app.LOGGER, level="WARNING") as logs:
            with self.assertRaises(StopIteration):
                app.run_federation_verifier()

        self.assertEqual(2, len(calls), "the loop kept going after a failure")
        # The type, not the text: exc_info renders the raising source line, so
        # asserting on the message passes even when the real exception is a
        # NameError from a missing import.
        self.assertIs(psycopg.OperationalError, logs.records[0].exc_info[0])

    def test_readiness_is_not_signalled_when_the_pass_never_ran(self):
        # An unreachable registry raises before the first alias. Reporting that
        # as verification complete would let startup proceed having checked
        # nothing at all, silently.
        import psycopg

        waits = []

        def sleep(seconds):
            waits.append(seconds)
            raise StopIteration

        app.FEDERATION_FIRST_PASS_DONE.clear()
        try:
            with patch.object(app.time, "sleep", sleep), patch.object(
                app, "verify_federation_sources",
                side_effect=psycopg.OperationalError("registry down"),
            ), self.assertLogs(app.LOGGER, level="WARNING"):
                with self.assertRaises(StopIteration):
                    app.run_federation_verifier()

            self.assertFalse(app.FEDERATION_FIRST_PASS_DONE.is_set())
        finally:
            app.FEDERATION_FIRST_PASS_DONE.clear()

    def test_readiness_is_signalled_once_a_pass_completes(self):
        def sleep(seconds):
            raise StopIteration

        app.FEDERATION_FIRST_PASS_DONE.clear()
        try:
            with patch.object(app.time, "sleep", sleep), patch.object(
                app, "verify_federation_sources",
                return_value={"observed": 1, "failed": 0, "skipped": 0, "deferred": 0},
            ):
                with self.assertRaises(StopIteration):
                    app.run_federation_verifier()

            self.assertTrue(app.FEDERATION_FIRST_PASS_DONE.is_set())
        finally:
            app.FEDERATION_FIRST_PASS_DONE.clear()

    def test_interval_is_long_enough_to_be_polite_to_a_third_party(self):
        # Each pass opens a connection to a database somebody else operates.
        # This is also the window a false revoke persists for, so it is
        # bounded on both sides deliberately.
        self.assertGreaterEqual(app.FEDERATION_VERIFY_INTERVAL_SECONDS, 300)
        self.assertLessEqual(app.FEDERATION_VERIFY_INTERVAL_SECONDS, 3600)


if __name__ == "__main__":
    unittest.main()
