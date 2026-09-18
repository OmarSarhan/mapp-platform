"""Token-B validation in the configuration API.

A token B is a credential, not a route: it is recognised inside the existing
Bearer branch of _actor, and spent at the end of _authorized -- the single gate
both routers already funnel through. So these tests drive those two functions
rather than any handler, because that is where every authenticated path passes.

The design claim being tested is narrow and worth stating: existing scope
enforcement applies unchanged and cannot be bypassed, and _required_scope's
`return "full"` catch-all means an unclassified route demands a scope the
broker will never issue -- so an unclassified route is refused by construction
rather than by an allowlist someone has to maintain.
"""

from __future__ import annotations

import base64
import datetime as dt
import http.client
import importlib.util
import io
import json
import sys
import threading
import unittest
import unittest.mock
import urllib.parse
from http import HTTPStatus
from http.client import HTTPMessage
from pathlib import Path

import app
import canonical
import execution_envelope
from mcp_token_client import McpTokenClient, McpTokenClientError

CONFIG_RESOURCE = "http://config.localhost/api"
MCP_RESOURCE = "http://mcp.localhost/mcp"
GRANT = "oauth:grant-1"
INSTANCE = "instance-under-test"


class StubTokens:
    """Stands in for the authorization component's control listener."""

    def __init__(self, *, record=None, redeem_error=None):
        self.record = record if record is not None else {
            "active": True,
            "scope": "apply",
            "sub": GRANT,
            "aud": CONFIG_RESOURCE,
            "client_id": "mapp-mcp-broker",
        }
        self.redeem_error = redeem_error
        self.introspected: list[str] = []
        self.redeemed: list[tuple[str, str, str]] = []

    def introspect(self, token):
        self.introspected.append(token)
        if isinstance(self.record, Exception):
            raise self.record
        return dict(self.record)

    def redeem(self, token, operation_id, request_digest):
        self.redeemed.append((token, operation_id, request_digest))
        if self.redeem_error is not None:
            raise self.redeem_error
        return {"redeemed": True, "single_use": True, "operation_id": operation_id}


class TokenBTestCase(unittest.TestCase):
    def build(
        self,
        *,
        method="POST",
        path="/api/proposals/p1/apply",
        body=b'{"approved":true}',
        token="mapp_b_credential",
        tokens=None,
        resource=CONFIG_RESOURCE,
    ):
        handler = object.__new__(app.Handler)
        handler.path = path
        handler.command = method
        handler.headers = HTTPMessage()
        if token is not None:
            handler.headers["Authorization"] = f"Bearer {token}"
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler._remote = lambda: "127.0.0.1"
        handler._raw_body = None
        handler._exchanged_token = None
        handler._exchanged_token_redeemed = False
        self.responses: list = []
        handler._json = lambda status, payload, **kw: self.responses.append(
            (int(status), payload)
        )
        self.tokens = tokens if tokens is not None else StubTokens()
        self._patches = [
            unittest.mock.patch.object(app, "MCP_TOKENS", self.tokens),
            unittest.mock.patch.object(app, "CONFIG_API_RESOURCE", resource),
            unittest.mock.patch.object(
                app.CONTROL, "instance_id", lambda: INSTANCE
            ),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        return handler


class AuthenticationTests(TokenBTestCase):
    def test_a_valid_token_authenticates_as_its_grant(self) -> None:
        """P3: the actor is the grant, so the audit trail resolves to a consent."""
        handler = self.build()
        self.assertEqual(GRANT, handler._authorized(required_scope="apply"))
        self.assertEqual(["mapp_b_credential"], self.tokens.introspected)

    def test_the_scope_comes_from_the_token(self) -> None:
        handler = self.build()
        handler._authorized(required_scope="apply")
        self.assertEqual(["apply"], handler._authentication["scopes"])

    def test_an_inactive_token_is_refused(self) -> None:
        handler = self.build(tokens=StubTokens(record={"active": False}))
        self.assertIsNone(handler._authorized(required_scope="apply"))
        self.assertEqual(HTTPStatus.UNAUTHORIZED, self.responses[0][0])

    def test_a_token_for_the_mcp_audience_is_refused(self) -> None:
        """The audience separation, checked here as well as by the component.

        A token A is what carries the MCP resource. If it were spendable at the
        configuration API the two audiences would be decoration.
        """
        record = {
            "active": True, "scope": "apply", "sub": GRANT, "aud": MCP_RESOURCE,
        }
        handler = self.build(tokens=StubTokens(record=record))
        self.assertIsNone(handler._authorized(required_scope="apply"))
        self.assertEqual(HTTPStatus.UNAUTHORIZED, self.responses[0][0])

    def test_a_subject_that_is_not_a_grant_is_refused(self) -> None:
        record = {
            "active": True, "scope": "apply", "sub": "admin",
            "aud": CONFIG_RESOURCE,
        }
        handler = self.build(tokens=StubTokens(record=record))
        self.assertIsNone(handler._authorized(required_scope="apply"))

    def test_an_unreachable_component_refuses_rather_than_admits(self) -> None:
        handler = self.build(
            tokens=StubTokens(record=McpTokenClientError("unavailable"))
        )
        self.assertIsNone(handler._authorized(required_scope="apply"))

    def test_an_unconfigured_component_refuses_every_exchanged_token(self) -> None:
        """A credential nothing can check authorises whatever it claims."""
        handler = self.build()
        with unittest.mock.patch.object(app, "MCP_TOKENS", None):
            self.assertIsNone(handler._authorized(required_scope="apply"))

    def test_an_ordinary_bearer_token_is_untouched(self) -> None:
        """The prefix is the whole discriminator; nothing else changes path."""
        handler = self.build(token="mapp_a_something")
        with unittest.mock.patch.object(
            app.CONTROL, "authenticate_token", lambda *a: None
        ):
            self.assertIsNone(handler._authorized())
        self.assertEqual([], self.tokens.introspected)


class BindingTests(TokenBTestCase):
    def test_the_digest_is_computed_from_the_request(self) -> None:
        handler = self.build()
        handler._authorized(required_scope="apply")
        expected = execution_envelope.digest(
            instance=INSTANCE,
            method="POST",
            operation_id="proposals.apply",
            path_template="/api/proposals/{proposalId}/apply",
            path="/api/proposals/p1/apply",
            query="",
            body={"approved": True},
            resolved_defaults=None,
            confirmation_fields=None,
            revision_binding=None,
        )
        self.assertEqual(
            [("mapp_b_credential", "proposals.apply", expected)],
            self.tokens.redeemed,
        )

    def test_a_get_operation_binds_its_query(self) -> None:
        handler = self.build(
            method="GET",
            path="/api/layers/roads/values",
            body=b"",
        )
        handler.path = "/api/layers/roads/values?field=name&limit=10"
        self.assertEqual(GRANT, handler._authorized(required_scope="apply"))
        expected = execution_envelope.digest(
            instance=INSTANCE,
            method="GET",
            operation_id="layers.values",
            path_template="/api/layers/{layerKey}/values",
            path="/api/layers/roads/values",
            query="field=name&limit=10",
            body=None,
            resolved_defaults=None,
            confirmation_fields=None,
            revision_binding=None,
        )
        self.assertEqual(expected, self.tokens.redeemed[0][2])

    def test_a_refused_binding_refuses_the_request(self) -> None:
        handler = self.build(
            tokens=StubTokens(redeem_error=McpTokenClientError("no", refused=True))
        )
        self.assertIsNone(handler._authorized(required_scope="apply"))
        status, payload = self.responses[0]
        self.assertEqual(HTTPStatus.FORBIDDEN, status)
        self.assertEqual("auth.binding_refused", payload["code"])

    def test_an_unresolvable_route_is_refused(self) -> None:
        """No template matches, so there is no envelope to bind to."""
        handler = self.build(path="/api/workspace")
        self.assertIsNone(handler._authorized(required_scope="apply"))
        self.assertEqual("auth.operation_unresolved", self.responses[0][1]["code"])

    def test_an_ambiguous_route_is_refused(self) -> None:
        """Two action ids claim this method and template.

        Resolving by precedence would build the envelope one of two ways, and
        only one of them is what the broker digested.
        """
        handler = self.build(path="/api/proposals/p1/visual-test")
        self.assertIsNone(handler._authorized(required_scope="apply"))
        self.assertEqual("auth.operation_unresolved", self.responses[0][1]["code"])

    def test_a_body_with_a_duplicate_member_is_refused(self) -> None:
        """Caught on the raw bytes, before a parser can keep only the last."""
        handler = self.build(body=b'{"approved":false,"approved":true}')
        self.assertIsNone(handler._authorized(required_scope="apply"))
        status, payload = self.responses[0]
        self.assertEqual(HTTPStatus.BAD_REQUEST, status)
        self.assertEqual("auth.request_not_canonical", payload["code"])

    def test_an_uninitialized_control_plane_refuses_rather_than_raises(self) -> None:
        """instance_id raises when the control plane has not been initialized.

        The envelope needs an instance, so there is nothing to bind a digest
        to -- and an uncaught raise inside _authorized would answer 500 from
        the authorization gate instead of refusing.
        """
        handler = self.build()

        def unavailable():
            raise RuntimeError("Control-plane authentication is not initialized.")

        with unittest.mock.patch.object(app.CONTROL, "instance_id", unavailable):
            self.assertIsNone(handler._authorized(required_scope="apply"))
        status, payload = self.responses[0]
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, status)
        self.assertEqual("auth.binding_unavailable", payload["code"])
        self.assertEqual([], self.tokens.redeemed)

    def test_the_authentication_record_carries_no_marker_key(self) -> None:
        """Two routes spread this dict straight into a response body.

        /api/auth/me and /api/connect both do. A marker key nothing reads
        would be a field leaking out of the authorization layer into an API
        response, so the actor prefix carries that meaning instead.
        """
        handler = self.build()
        handler._authorized(required_scope="apply")
        self.assertEqual({"actor", "scopes"}, set(handler._authentication))
        self.assertTrue(handler._authentication["actor"].startswith("oauth:"))

    def test_a_non_canonical_path_is_refused(self) -> None:
        handler = self.build(path="/api/proposals/a%2fb/apply")
        self.assertIsNone(handler._authorized(required_scope="apply"))
        self.assertEqual(HTTPStatus.FORBIDDEN, self.responses[0][0])


class OrderingTests(TokenBTestCase):
    """What happens before what, and why it matters."""

    def test_a_scope_failure_does_not_spend_the_token(self) -> None:
        """A refused request must not burn a single-use credential.

        Redemption is last in _authorized for exactly this reason: the operator
        would otherwise have to re-exchange after every scope mistake, and a
        credential spent on a request that never ran is indistinguishable from
        one spent on a request that did.
        """
        handler = self.build()
        self.assertIsNone(handler._authorized(required_scope="federation:provision"))
        self.assertEqual(HTTPStatus.FORBIDDEN, self.responses[0][0])
        self.assertEqual("auth.scope_required", self.responses[0][1]["code"])
        self.assertEqual([], self.tokens.redeemed)

    def test_an_unclassified_route_demands_a_scope_never_issued(self) -> None:
        """_required_scope's catch-all is a safety asset, not a default.

        Any route the scope table does not classify requires `full`, `full` is
        on the broker's hard deny-list, so an unclassified route is refused by
        construction rather than by remembering to add it to a list.
        """
        self.assertEqual("full", app.Handler._required_scope("/api/anything", "POST"))
        handler = self.build(path="/api/anything")
        self.assertIsNone(handler._authorized(required_scope="full"))
        self.assertEqual("auth.scope_required", self.responses[0][1]["code"])
        self.assertEqual([], self.tokens.redeemed)

    def test_the_body_is_read_once_and_reused(self) -> None:
        """The digest needs the body before dispatch; _payload needs it after.

        A socket can only be read once, so the second reader has to be served
        from the first read or eleven call sites would get an empty body.
        """
        handler = self.build()
        self.assertEqual(GRANT, handler._authorized(required_scope="apply"))
        self.assertEqual({"approved": True}, handler._payload())
        self.assertEqual({"approved": True}, handler._payload())


class RouteGateAlignmentTests(unittest.TestCase):
    """The route gate and the exchanged scope set must agree.

    Three separate tables decide whether an allowlisted operation can actually
    be executed: the broker's allowlist (what scopes a token B is minted with),
    ACTION_SCHEMAS (what the platform says the action needs) and
    _required_scope (what this router demands for the concrete path). The
    broker already has a drift test against ACTION_SCHEMAS. Nothing compared
    either of them against the router, which is the table that actually
    refuses -- and a mismatch there is invisible: the exchange succeeds, and
    the request is then refused for want of a scope no one could have asked
    for.

    layers.values is why this matters concretely. Its handler imposes a second
    gate of its own (semantic:inspect, beyond the route's `derive`), which is
    exactly why the allowlist mints both scopes for it.
    """

    #: One concrete path per allowlisted operation. Concrete on purpose: the
    #: router matches paths, not templates, so a template would not exercise it.
    PATHS = {
        "layers.values": ("GET", "/api/layers/roads/values"),
        "derived-layers.refresh": ("POST", "/api/derived-layers/d1/refresh"),
        "federation.aliases.observe": (
            "POST", "/api/federation/aliases/a1/observe",
        ),
        "proposals.apply": ("POST", "/api/proposals/p1/apply"),
        "semantic.proposals.apply": (
            "POST", "/api/semantic/proposals/p1/apply",
        ),
    }

    def exchanged_scopes(self, name):
        from control_api import ACTION_SCHEMAS

        schema = ACTION_SCHEMAS[name]
        return set(schema.get("requiredScopes") or [schema["scope"]])

    def test_the_router_demands_a_scope_the_exchange_grants(self) -> None:
        for name, (method, path) in self.PATHS.items():
            with self.subTest(operation=name):
                gate = app.Handler._required_scope(path, method)
                self.assertIsNotNone(
                    gate, "an allowlisted operation must be scope-gated"
                )
                self.assertIn(gate, self.exchanged_scopes(name))

    def test_no_allowlisted_route_falls_through_to_full(self) -> None:
        """`full` is on the broker's deny-list, so that would be unreachable."""
        for name, (method, path) in self.PATHS.items():
            with self.subTest(operation=name):
                self.assertNotEqual(
                    "full", app.Handler._required_scope(path, method)
                )

    def test_each_path_resolves_to_the_operation_it_belongs_to(self) -> None:
        handler = object.__new__(app.Handler)
        for name, (method, path) in self.PATHS.items():
            with self.subTest(operation=name):
                resolved = app.Handler._resolve_operation(handler, method, path)
                self.assertIsNotNone(resolved, f"{name} does not resolve")
                self.assertEqual(name, resolved[0])


class DiscoveryRouteTests(TokenBTestCase):
    """A skipped scope check is not an open route.

    _required_scope returns None for the discovery routes, so the scope check
    does not run. What refuses an exchanged credential is the binding gate: a
    token B authorises exactly one allowlisted operation, and a route no
    operation names matches no template.

    That distinction is the point of these tests, and it is why the list below
    shrank rather than the property changing. `/api/capabilities` and
    `/api/derived-layers/capabilities` are now allowlisted operations --
    `capabilities.list` and `derived-layers.capabilities` -- so they resolve
    and are reachable, which is a decision about what an agent may read and
    not a hole. The routes that remain here are refused for the original
    reason: nothing names them. `/api/auth/me` is the one that matters most,
    being a credential's own identity.

    Documented by a test because the alternative reading -- that a skipped
    scope check means an open route -- is the dangerous one.
    """

    def test_an_unnamed_discovery_route_refuses_an_exchanged_token(self) -> None:
        for path in (
            "/api/contract",
            "/api/connect",
            "/api/auth/me",
        ):
            with self.subTest(path=path):
                self.assertIsNone(app.Handler._required_scope(path, "GET"))
                handler = self.build(method="GET", path=path, body=b"")
                self.assertIsNone(handler._authorized())
                self.assertEqual(
                    "auth.operation_unresolved", self.responses[-1][1]["code"]
                )

    def test_an_allowlisted_discovery_route_is_reachable(self) -> None:
        """The other half, stated rather than implied. These two are readable
        by an agent on purpose: the contract says what actions exist and what
        each costs, which is what an agent composing a change reads first."""
        for path, operation in (
            ("/api/capabilities", "capabilities.list"),
            ("/api/derived-layers/capabilities", "derived-layers.capabilities"),
        ):
            with self.subTest(path=path):
                self.assertIsNone(app.Handler._required_scope(path, "GET"))
                handler = self.build(method="GET", path=path, body=b"")
                resolved = app.Handler._resolve_operation(handler, "GET", path)
                self.assertIsNotNone(resolved, f"{path} resolves no operation")
                self.assertEqual(operation, resolved[0])


class FailClosedGuardTests(TokenBTestCase):
    """_json is the only response helper, so it can hold the invariant."""

    def make(self):
        handler = object.__new__(app.Handler)
        handler.path = "/api/proposals/p1/apply"
        handler.command = "POST"
        handler._raw_body = None
        handler._exchanged_token = "mapp_b_credential"
        handler._exchanged_token_redeemed = False
        handler._request_id = "r"
        self.written: list = []
        handler.send_response = lambda status: self.written.append(status)
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        return handler

    def test_a_success_without_redemption_becomes_a_refusal(self) -> None:
        handler = self.make()
        handler._json(HTTPStatus.OK, {"applied": True})
        self.assertEqual([HTTPStatus.FORBIDDEN], self.written)
        body = json.loads(handler.wfile.getvalue().decode())
        self.assertEqual("auth.binding_not_redeemed", body["code"])
        self.assertNotIn("applied", body)

    def test_a_redeemed_request_passes_through(self) -> None:
        handler = self.make()
        handler._exchanged_token_redeemed = True
        handler._json(HTTPStatus.OK, {"applied": True})
        self.assertEqual([HTTPStatus.OK], self.written)
        self.assertTrue(json.loads(handler.wfile.getvalue().decode())["applied"])

    def test_a_request_with_no_exchanged_token_passes_through(self) -> None:
        handler = self.make()
        handler._exchanged_token = None
        handler._json(HTTPStatus.OK, {"applied": True})
        self.assertEqual([HTTPStatus.OK], self.written)

    def test_raw_response_paths_are_unreachable(self) -> None:
        """_json is not literally every response, so the guard is not the control.

        /api/artifacts/ and the SVG prefix write to wfile directly and would
        bypass this guard entirely. What actually stops an exchanged credential
        reaching them is the binding gate: no manifest template matches, so
        _resolve_operation refuses before dispatch. Pinned here because the
        comfortable reading -- "the guard covers everything" -- is false, and a
        future raw-write handler on an allowlisted route would need the gate,
        not this.
        """
        handler = object.__new__(app.Handler)
        for path in ("/api/artifacts/shot.png", "/instance/svg/bus.svg"):
            with self.subTest(path=path):
                self.assertIsNone(
                    app.Handler._resolve_operation(handler, "GET", path)
                )
        # And the SVG prefix is outside /api/, so it is never authorized at
        # all -- an exchanged credential presented there is simply ignored.
        self.assertFalse("/instance/svg/bus.svg".startswith("/api/"))

    def test_an_error_response_is_not_rewritten(self) -> None:
        """Only a 2xx is a claim that the effect happened."""
        handler = self.make()
        handler._json(HTTPStatus.BAD_REQUEST, {"error": "no", "code": "x"})
        self.assertEqual([HTTPStatus.BAD_REQUEST], self.written)
        self.assertEqual("x", json.loads(handler.wfile.getvalue().decode())["code"])


class RequestStateTests(unittest.TestCase):
    """Per-request state must not survive into the next request.

    HTTP/1.0 closes each connection today, so one handler serves one request --
    but that is BaseHTTPRequestHandler's default protocol_version, not a
    decision recorded anywhere. If it were raised to 1.1, a stale
    _exchanged_token_redeemed would let a second request on the same connection
    pass the guard without ever redeeming.
    """

    def test_the_class_defaults_are_the_unset_state(self) -> None:
        self.assertIsNone(app.Handler._raw_body)
        self.assertIsNone(app.Handler._exchanged_token)
        self.assertFalse(app.Handler._exchanged_token_redeemed)

    def test_handle_one_request_clears_the_previous_request(self) -> None:
        handler = object.__new__(app.Handler)
        handler._raw_body = b'{"stale":true}'
        handler._exchanged_token = "mapp_b_previous"
        handler._exchanged_token_redeemed = True
        with unittest.mock.patch.object(
            app.SimpleHTTPRequestHandler, "handle_one_request", lambda self: None
        ):
            handler.handle_one_request()
        self.assertIsNone(handler._raw_body)
        self.assertIsNone(handler._exchanged_token)
        self.assertFalse(handler._exchanged_token_redeemed)


class RealComponentLoopTests(unittest.TestCase):
    """The whole binding against the real component, over real HTTP.

    Everything above stubs the client. This class does not: it stands up the
    authorization component's control listener, mints a token B through its own
    store, and drives the real McpTokenClient against it. The digest is built
    by the configuration API's envelope and compared by the component -- which
    is the one thing no stub can prove, because a stub agrees with whatever it
    is handed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[2] / "mcp-auth"
        if not (root / "server.py").exists():  # pragma: no cover
            raise unittest.SkipTest("the authorization component is not in this tree")
        sys.path.insert(0, str(root))
        try:
            import server as auth_server
            from issuer import MappAuthorizationServer
            from models import Client, Grant
        except ModuleNotFoundError as exc:  # pragma: no cover - authlib absent
            raise unittest.SkipTest(f"the component's dependencies are absent: {exc}")
        spec = importlib.util.spec_from_file_location(
            "broker_stub_store", root / "tests" / "stub_store.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.auth_server = auth_server
        cls.MappAuthorizationServer = MappAuthorizationServer
        cls.Client = Client
        cls.Grant = Grant
        cls.StubStore = module.StubStore

    def setUp(self) -> None:
        self.store = self.StubStore()
        self.store.add_client(
            self.Client(
                client_id="mapp-config-api", name="Configuration API",
                redirect_uris=(), scopes=(),
                token_endpoint_auth_method="client_secret_basic",
                client_secret="config-secret",
                # What the deployed configuration API is granted. Notably not
                # "exchange": minting an execution credential is the runtime's
                # job, and this suite drives the real control listener, which
                # now refuses a capability the client does not hold.
                capabilities=("introspect", "redeem", "revoke"),
            )
        )
        self.store.add_client(
            self.Client(
                client_id="mcp-client", name="Agent",
                redirect_uris=("http://127.0.0.1:9/cb",), scopes=("apply",),
                token_endpoint_auth_method="none",
            )
        )
        self.store.save_grant(
            self.Grant(grant_id=GRANT, client_id="mcp-client", subject="operator",
                       scopes=("apply",))
        )
        authorization = self.MappAuthorizationServer(
            self.store, issuer="http://mcp.localhost", resource=MCP_RESOURCE,
            config_api_resource=CONFIG_RESOURCE, scopes_supported=("apply",),
        )
        self.httpd = self.auth_server.ControlServer(("127.0.0.1", 0), authorization)
        port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.client = McpTokenClient(
            f"http://127.0.0.1:{port}", "mapp-config-api", "config-secret",
            resource=CONFIG_RESOURCE,
        )

    def mint(self, raw: str, digest: str, *, single_use=True,
             operation_id="proposals.apply") -> None:
        issued = dt.datetime.now(dt.timezone.utc)
        self.store.save_exchanged_token(
            raw, client_id="mapp-config-api", actor_client_id="mcp-client",
            subject=GRANT, scope="apply", audience=CONFIG_RESOURCE,
            issued_at=issued, expires_at=issued + dt.timedelta(seconds=60),
            operation_id=operation_id, request_digest=digest,
            single_use=single_use,
        )

    def envelope_digest(self, path="/api/proposals/p1/apply", body=None):
        return execution_envelope.digest(
            instance=INSTANCE, method="POST", operation_id="proposals.apply",
            path_template="/api/proposals/{proposalId}/apply", path=path,
            query="", body={"approved": True} if body is None else body,
            resolved_defaults=None, confirmation_fields=None,
            revision_binding=None,
        )

    def test_introspection_resolves_the_grant_over_http(self) -> None:
        self.mint("mapp_b_loop", self.envelope_digest())
        record = self.client.introspect("mapp_b_loop")
        self.assertTrue(record["active"])
        self.assertEqual(GRANT, record["sub"])
        self.assertEqual(CONFIG_RESOURCE, record["aud"])

    def test_the_envelope_digest_is_what_the_component_accepts(self) -> None:
        """The claim that cannot be stubbed: both sides compute the same value."""
        digest = self.envelope_digest()
        self.mint("mapp_b_loop2", digest)
        self.assertTrue(
            self.client.redeem("mapp_b_loop2", "proposals.apply", digest)["redeemed"]
        )

    def test_the_same_token_cannot_be_redeemed_twice(self) -> None:
        digest = self.envelope_digest()
        self.mint("mapp_b_loop3", digest)
        self.client.redeem("mapp_b_loop3", "proposals.apply", digest)
        with self.assertRaises(McpTokenClientError) as caught:
            self.client.redeem("mapp_b_loop3", "proposals.apply", digest)
        self.assertTrue(caught.exception.refused)

    def test_a_different_proposal_is_refused(self) -> None:
        """One token, one request. This is the binding, end to end."""
        self.mint("mapp_b_loop4", self.envelope_digest())
        other = self.envelope_digest(path="/api/proposals/p2/apply")
        with self.assertRaises(McpTokenClientError):
            self.client.redeem("mapp_b_loop4", "proposals.apply", other)

    def test_a_changed_body_is_refused(self) -> None:
        self.mint("mapp_b_loop5", self.envelope_digest())
        other = self.envelope_digest(body={"approved": True, "force": True})
        with self.assertRaises(McpTokenClientError):
            self.client.redeem("mapp_b_loop5", "proposals.apply", other)

    def test_a_revoked_grant_stops_redemption_immediately(self) -> None:
        digest = self.envelope_digest()
        self.mint("mapp_b_loop6", digest)
        self.assertTrue(self.store.revoke_grant(GRANT, "operator withdrew consent"))
        with self.assertRaises(McpTokenClientError):
            self.client.redeem("mapp_b_loop6", "proposals.apply", digest)
        self.assertEqual({"active": False}, self.client.introspect("mapp_b_loop6"))

    def test_a_token_a_does_not_resolve_at_this_audience(self) -> None:
        from models import Token

        now = dt.datetime.now(dt.timezone.utc)
        self.store.save_token(
            "mapp_a_live",
            Token(token_hash="", client_id="mcp-client", scope="apply",
                  subject=GRANT, issued_at=int(now.timestamp()), expires_in=900,
                  audience=MCP_RESOURCE),
        )
        self.assertEqual({"active": False}, self.client.introspect("mapp_a_live"))

    def test_wrong_client_credentials_are_refused(self) -> None:
        bad = McpTokenClient(
            self.client.endpoint, "mapp-config-api", "not-the-secret",
            resource=CONFIG_RESOURCE,
        )
        with self.assertRaises(McpTokenClientError) as caught:
            bad.introspect("mapp_b_anything")
        self.assertEqual(401, caught.exception.status)


if __name__ == "__main__":
    unittest.main()
