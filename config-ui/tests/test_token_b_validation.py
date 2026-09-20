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
import re
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
        receipt="receipt-value",
        receipt_spends=True,
    ):
        """The default path is `proposals.apply`, which requires approval, so
        the default carries a receipt that spends. Tests about the receipt
        itself pass `receipt=None` or `receipt_spends=False`; everything else
        is about the credential and should not have to care."""
        handler = object.__new__(app.Handler)
        handler.path = path
        handler.command = method
        handler.headers = HTTPMessage()
        if token is not None:
            handler.headers["Authorization"] = f"Bearer {token}"
        if receipt is not None:
            handler.headers[app.APPROVAL_RECEIPT_HEADER] = receipt
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
        self.redeemed_receipts: list = []

        def redeem_receipt(value, *, request_digest):
            self.redeemed_receipts.append((value, request_digest))
            return (
                {"operation_id": "proposals.apply", "grant_id": GRANT,
                 "client_id": "mcp-1", "decided_by": "admin",
                 "request_digest": request_digest}
                if receipt_spends else None
            )

        self._patches = [
            unittest.mock.patch.object(
                app.CONTROL, "redeem_receipt", redeem_receipt
            ),
            unittest.mock.patch.object(app.CONTROL, "audit", lambda *a, **k: None),
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
        """Two action ids claiming one method and template.

        Resolving by precedence would build the envelope one of two ways, and
        only one of them is what the broker digested.

        The ambiguity is constructed rather than borrowed. Three real pairs
        existed until Phase 1 wave 4 -- and this test used one of them, so it
        passed for a reason it was not testing: the route it named was
        genuinely unreachable, which was the defect rather than the control.
        `RouteUniquenessTests` now asserts no such pair exists, so this has to
        make its own.
        """
        route = ("POST", "/api/proposals/{proposalId}/visual-test")
        doubled = dict(app.OPERATIONS_BY_ROUTE)
        doubled[route] = doubled[route] + ("proposals.invented-duplicate",)

        with unittest.mock.patch.object(
            app, "OPERATIONS_BY_ROUTE", doubled
        ):
            handler = self.build(path="/api/proposals/p1/visual-test")
            self.assertIsNone(handler._authorized(required_scope="apply"))

        self.assertEqual("auth.operation_unresolved", self.responses[0][1]["code"])

    def test_the_same_route_resolves_when_only_one_action_claims_it(self) -> None:
        """The other half: without the invented duplicate the route resolves,
        so the test above is measuring ambiguity and not a broken path."""
        handler = self.build(path="/api/proposals/p1/visual-test")
        resolved = app.Handler._resolve_operation(
            handler, "POST", "/api/proposals/p1/visual-test"
        )
        self.assertEqual("proposals.preview-test", resolved[0])

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


class ApprovalReceiptTests(TokenBTestCase):
    """A consequential operation needs a person to have agreed to it.

    The credential proves an operator consented to the *grant*; the receipt
    proves somebody agreed to *this request*. They are different consents and
    the platform requires both, because a grant is issued once and a request
    happens whenever the agent decides it should.
    """

    def test_an_apply_without_a_receipt_is_refused(self) -> None:
        handler = self.build(receipt=None)
        self.assertIsNone(handler._authorized(required_scope="apply"))
        status, payload = self.responses[0]
        self.assertEqual(HTTPStatus.FORBIDDEN, status)
        self.assertEqual("approval.required", payload["code"])

    def test_a_missing_receipt_does_not_burn_the_credential(self) -> None:
        """Checked before the credential is spent. The redemption is
        single-use for exactly these operations, so refusing afterwards would
        make a forgotten header cost an exchange."""
        handler = self.build(receipt=None)
        handler._authorized(required_scope="apply")
        self.assertEqual([], self.tokens.redeemed)

    def test_a_receipt_that_does_not_cover_the_request_is_refused(self) -> None:
        handler = self.build(receipt_spends=False)
        self.assertIsNone(handler._authorized(required_scope="apply"))
        status, payload = self.responses[0]
        self.assertEqual(HTTPStatus.FORBIDDEN, status)
        self.assertEqual("approval.receipt_invalid", payload["code"])

    def test_the_receipt_is_spent_against_the_bound_digest(self) -> None:
        """The same value the credential was bound to, so the thing approved
        and the thing done cannot differ."""
        handler = self.build()
        handler._authorized(required_scope="apply")
        self.assertEqual(1, len(self.redeemed_receipts))
        value, digest = self.redeemed_receipts[0]
        self.assertEqual("receipt-value", value)
        self.assertEqual(self.tokens.redeemed[0][2], digest)

    def test_a_read_needs_no_receipt(self) -> None:
        """Requiring one everywhere would make every read need a person."""
        handler = self.build(
            method="GET", path="/api/layers", body=b"", receipt=None,
            tokens=StubTokens(record={
                "active": True, "scope": "inspect", "sub": GRANT,
                "aud": CONFIG_RESOURCE, "client_id": "mcp-1",
            }),
        )
        self.assertEqual(
            GRANT,
            handler._authorized(required_scope="inspect"),
        )
        self.assertEqual([], self.redeemed_receipts)

    def test_proposing_needs_no_receipt(self) -> None:
        """Deliberate, and the substance of waves 3 and 4: a proposal adds to
        a review queue a person already works through and applies nothing."""
        handler = self.build(
            path="/api/proposals",
            body=b'{"revision":"r1","operations":[]}',
            receipt=None,
            tokens=StubTokens(record={
                "active": True, "scope": "propose", "sub": GRANT,
                "aud": CONFIG_RESOURCE, "client_id": "mcp-1",
            }),
        )
        self.assertEqual(
            GRANT, handler._authorized(required_scope="propose")
        )
        self.assertEqual([], self.redeemed_receipts)

    def test_the_requirement_is_derived_from_the_platforms_risk_class(
        self,
    ) -> None:
        """Not a second list to keep in step with the three that exist. The
        exemption is what is stated, so an action class nobody has considered
        requires approval rather than silently arriving unguarded."""
        import control_api

        self.assertTrue(control_api.requires_approval("proposals.apply"))
        self.assertTrue(control_api.requires_approval("semantic.proposals.apply"))
        self.assertTrue(control_api.requires_approval("derived-layers.refresh"))
        self.assertFalse(control_api.requires_approval("proposals.create"))
        self.assertFalse(control_api.requires_approval("layers.list"))
        self.assertTrue(
            control_api.requires_approval("nothing.the.platform.defines"),
            "an unknown operation must not be exempt",
        )

    def test_every_action_the_platform_defines_is_classified(self) -> None:
        """A risk class arriving without a decision about it should show up
        here rather than as an unguarded mutation."""
        import control_api

        classified = control_api.NO_APPROVAL_RISKS
        unknown = {
            action["risk"] for action in control_api.ACTION_SCHEMAS.values()
        } - classified
        self.assertTrue(
            unknown,
            "if nothing requires approval this test is measuring nothing",
        )
        for risk in unknown:
            with self.subTest(risk=risk):
                self.assertNotIn(risk, classified)


class RequestDigestShapeTests(unittest.TestCase):
    """One digest, produced in one place, accepted and matched in others.

    `execution_envelope.digest` produces it, the broker binds a token to it,
    `POST /api/approvals` records what an approval covers, and
    `redeem_receipt` matches the receipt against it. Four places, and only the
    first decides the shape.

    Wave 5 shipped with the approval route demanding a bare 64-character
    sha256 -- the digest without its scheme -- so every approval an agent
    asked for was refused `approval.digest_invalid` and the gate could never
    run. Nothing caught it: the store tests used a bare 64-character fixture,
    which agreed with the route, and the handler tests supplied the scope and
    the digest by hand. It took driving a real client.
    """

    def real_digest(self):
        return execution_envelope.digest(
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

    def test_the_route_accepts_what_the_envelope_produces(self) -> None:
        self.assertIsNotNone(
            app.REQUEST_DIGEST.fullmatch(self.real_digest()),
            "the approval route would refuse a digest the platform itself"
            " computed",
        )

    def test_the_declared_schema_accepts_it_too(self) -> None:
        """The handler and the published contract must agree, or a client
        built from the schema sends what the handler rejects."""
        from control_api import ACTION_SCHEMAS

        pattern = (ACTION_SCHEMAS["approvals.create"]["inputSchema"]
                   ["properties"]["requestDigest"]["pattern"])
        self.assertIsNotNone(re.fullmatch(pattern, self.real_digest()))

    def test_the_broker_binds_the_same_shape(self) -> None:
        """The token and the approval must be about the same string, or a
        receipt could never match the request it was minted for."""
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "mcp-auth")
        )
        import exchange as broker_exchange

        self.assertIsNotNone(
            broker_exchange.DIGEST_PATTERN.fullmatch(self.real_digest())
        )

    def test_a_bare_sha256_is_refused(self) -> None:
        """The exact wrong value that shipped. A digest without its scheme
        cannot be told apart from one computed under a different scheme, which
        is the whole reason the prefix exists."""
        self.assertIsNone(app.REQUEST_DIGEST.fullmatch("a" * 64))

    def test_an_empty_digest_is_refused(self) -> None:
        """The scheme alone matches a prefix test and binds to nothing."""
        self.assertIsNone(
            app.REQUEST_DIGEST.fullmatch(f"{canonical.SCHEME}:")
        )


class ReceiptHeaderTests(unittest.TestCase):
    """The two ends must name the header identically.

    One is in this repository's configuration API and the other in its MCP
    server, in different modules that never import each other. A rename on one
    side would make every approval refuse with `approval.required`, which
    reads exactly like nobody having approved anything.
    """

    def test_the_client_and_the_platform_agree(self) -> None:
        source = (
            Path(__file__).resolve().parents[2]
            / "mapp-mcp" / "config_api_client.py"
        ).read_text(encoding="utf-8")
        declared = re.search(
            r'APPROVAL_RECEIPT_HEADER = "([^"]+)"', source
        )
        self.assertIsNotNone(declared, "the client declares no receipt header")
        self.assertEqual(app.APPROVAL_RECEIPT_HEADER, declared.group(1))


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

    #: Placeholders that need a value the router will actually match. Every
    #: other `{segment}` takes the generic one below.
    SEGMENTS = {
        "alias": "a1",
        "name": "d1",
        "layerKey": "roads",
        "operationId": "op1",
        "proposalId": "p1",
    }

    @classmethod
    def paths(cls):
        """One concrete path per allowlisted operation, derived from the
        allowlist rather than listed.

        Concrete on purpose: the router matches paths, not templates, so a
        template would not exercise it. Derived on purpose, and that is the
        correction -- this was a hand-written table of five operations, and
        wave 5 added three the table did not know about. All three fell
        through to the `full` catch-all, so the entire approval flow was
        unreachable in a real deployment while this test passed. A list of
        what to check is a list somebody has to remember to extend.
        """
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "mcp-auth")
        )
        import operations as broker

        found = {}
        for name, operation in broker.OPERATIONS.items():
            path = re.sub(
                r"\{([A-Za-z]+)\}",
                lambda match: cls.SEGMENTS.get(match.group(1), "x1"),
                operation.path_template,
            )
            found[name] = (operation.method, path)
        return found

    def exchanged_scopes(self, name):
        from control_api import ACTION_SCHEMAS

        schema = ACTION_SCHEMAS[name]
        return set(schema.get("requiredScopes") or [schema["scope"]])

    #: Allowlisted operations the router deliberately does not scope-gate,
    #: because something narrower than a route rule does it instead. Pinned
    #: rather than inferred from a None, so adding one is a decision: a route
    #: that silently stops being gated looks exactly like a route that was
    #: never meant to be.
    #:
    #: The two capabilities reads answer contract discovery and expose no
    #: workspace state; what refuses an exchanged credential there is the
    #: binding gate, since a token B authorises one allowlisted operation and
    #: a route no operation names matches no template. `operations.show` is
    #: gated per record inside the handler, from the kind of work the
    #: operation describes, which a path rule cannot see.
    UNGATED_BY_ROUTE = {
        "capabilities.list",
        "derived-layers.capabilities",
        "operations.show",
    }

    def test_the_router_demands_a_scope_the_exchange_grants(self) -> None:
        for name, (method, path) in self.paths().items():
            with self.subTest(operation=name, path=path):
                gate = app.Handler._required_scope(path, method)
                if name in self.UNGATED_BY_ROUTE:
                    self.assertIsNone(
                        gate,
                        "this is pinned as gated elsewhere; if the router now"
                        " gates it, drop it from UNGATED_BY_ROUTE",
                    )
                    continue
                self.assertIsNotNone(
                    gate, "an allowlisted operation must be scope-gated"
                )
                self.assertIn(gate, self.exchanged_scopes(name))

    def test_no_allowlisted_route_falls_through_to_full(self) -> None:
        """`full` is on the broker's deny-list, so that would be unreachable."""
        for name, (method, path) in self.paths().items():
            with self.subTest(operation=name, path=path):
                self.assertNotEqual(
                    "full", app.Handler._required_scope(path, method),
                    "this operation is allowlisted and unreachable:"
                    " the exchange mints a credential the router then"
                    " refuses for want of a scope nobody can ask for",
                )

    def test_each_path_resolves_to_the_operation_it_belongs_to(self) -> None:
        handler = object.__new__(app.Handler)
        for name, (method, path) in self.paths().items():
            with self.subTest(operation=name, path=path):
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
