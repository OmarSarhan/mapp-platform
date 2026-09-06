"""The restricted RFC 8693 exchange: one happy path and every refusal.

This is the Phase 0 evidence artefact. RFC 8693 is a framework, and almost all
of its freedom is removed by this profile -- so most of the value is in what is
refused, not in what succeeds. A test suite that only proved the happy path
would prove almost nothing about the design.

The refusals are grouped by what an attacker would be attempting: replaying a
token at the wrong resource, widening scope, describing an operation
differently from the allowlist, or slipping a second value past a parser that
keeps the first.
"""

from __future__ import annotations

import base64
import datetime as dt
import http.client
import json
import os
import sys
import threading
import unittest
import urllib.parse
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import canonical
import exchange
import operations
from models import Client, Grant, Token
from server import ControlServer
from stub_store import StubStore

RESOURCE = "http://config.localhost/api"
MCP_RESOURCE = "http://mcp.localhost/mcp"
OPERATION = "proposals.apply"
READ_OPERATION = "layers.values"


def context(**overrides) -> str:
    """A well-formed operation context, before any tampering."""
    value = {
        "version": canonical.SCHEME,
        "operationId": OPERATION,
        "method": "POST",
        "pathTemplate": "/api/proposals/{proposalId}/apply",
        "requestDigest": canonical.digest({"proposalId": "p1"}),
    }
    value.update(overrides)
    return json.dumps(value)


def form(**overrides) -> defaultdict:
    """A valid exchange request as a parsed parameter list."""
    fields = {
        "grant_type": exchange.GRANT_TYPE,
        "subject_token": "mapp_a_subject",
        "subject_token_type": exchange.ACCESS_TOKEN_TYPE,
        "requested_token_type": exchange.ACCESS_TOKEN_TYPE,
        "resource": RESOURCE,
        "scope": "apply",
        exchange.CONTEXT_PARAMETER: context(),
    }
    fields.update(overrides)
    datalist: defaultdict = defaultdict(list)
    for name, value in fields.items():
        if value is None:
            continue
        datalist[name].extend(value if isinstance(value, list) else [value])
    return datalist


class ExchangeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = StubStore()
        self.broker = Client(
            client_id="mapp-mcp-broker",
            name="Broker",
            redirect_uris=(),
            scopes=(),
            token_endpoint_auth_method="client_secret_basic",
            client_secret="broker-secret",
        )
        self.store.add_client(self.broker)
        # The client the subject token belongs to. The exchange resolves it
        # now, because a grant held by a client that may no longer act should
        # not mint anything.
        self.store.add_client(
            Client(
                client_id="mcp-client",
                name="Claude Code",
                redirect_uris=("http://127.0.0.1:9/cb",),
                scopes=("apply", "derive", "semantic:inspect"),
                token_endpoint_auth_method="none",
            )
        )
        self.now = dt.datetime.now(dt.timezone.utc)
        self._seed_subject("mapp_a_subject", "apply derive semantic:inspect")

    def _seed_subject(self, raw: str, scope: str, *, expires_in: int = 900,
                      revoked: bool = False, audience: str = MCP_RESOURCE,
                      subject: str = "oauth:grant-1") -> None:
        # A grant behind the token, because the exchange resolves one now:
        # a subject with no grant is refused, which is what makes revocation
        # stop new exchanges.
        if self.store.query_grant(subject) is None:
            self.store.save_grant(
                Grant(grant_id=subject, client_id="mcp-client",
                      subject="admin", scopes=tuple(scope.split()))
            )
        self.store.save_token(
            raw,
            Token(
                token_hash="",
                client_id="mcp-client",
                scope=scope,
                subject=subject,
                issued_at=int(self.now.timestamp()),
                expires_in=expires_in,
                revoked=revoked,
                audience=audience,
            ),
        )

    def run_exchange(self, datalist=None):
        return exchange.exchange(
            datalist=datalist if datalist is not None else form(),
            broker_client=self.broker,
            store=self.store,
            resource=RESOURCE,
            mcp_resource=MCP_RESOURCE,
            now=self.now,
        )

    def assert_refused(self, error: str, datalist) -> str:
        with self.assertRaises(exchange.ExchangeError) as caught:
            self.run_exchange(datalist)
        self.assertEqual(error, caught.exception.error)
        return caught.exception.description


class HappyPathTests(ExchangeTestCase):
    def test_a_valid_request_issues_token_b(self) -> None:
        token = self.run_exchange()
        self.assertTrue(token["access_token"].startswith(exchange.TOKEN_B_PREFIX))
        self.assertEqual("Bearer", token["token_type"])
        self.assertEqual(exchange.ACCESS_TOKEN_TYPE, token["issued_token_type"])
        self.assertEqual("apply", token["scope"])

    def test_no_refresh_token_is_returned(self) -> None:
        # By profile. A sixty-second credential that can be renewed is not a
        # sixty-second credential.
        self.assertNotIn("refresh_token", self.run_exchange())

    def test_the_lifetime_is_capped(self) -> None:
        self.assertLessEqual(
            self.run_exchange()["expires_in"], exchange.TOKEN_B_MAX_LIFETIME
        )

    def test_the_token_is_bound_to_the_operation_and_digest(self) -> None:
        token = self.run_exchange()
        binding = self.store.exchanged_binding(token["access_token"])
        self.assertEqual(OPERATION, binding["operation_id"])
        self.assertEqual(
            canonical.digest({"proposalId": "p1"}), binding["request_digest"]
        )

    def test_a_mutating_operation_yields_a_single_use_token(self) -> None:
        token = self.run_exchange()
        self.assertTrue(self.store.exchanged_binding(token["access_token"])["single_use"])
        # And it can be spent exactly once.
        digest = canonical.digest({"proposalId": "p1"})
        self.assertIsNotNone(
            self.store.consume_exchanged_token(
                token["access_token"], OPERATION, digest
            )
        )
        self.assertIsNone(
            self.store.consume_exchanged_token(
                token["access_token"], OPERATION, digest
            )
        )

    def test_a_read_operation_is_not_single_use(self) -> None:
        token = self.run_exchange(
            form(
                scope="derive semantic:inspect",
                **{
                    exchange.CONTEXT_PARAMETER: context(
                        operationId=READ_OPERATION,
                        method="GET",
                        pathTemplate="/api/layers/{layerKey}/values",
                    )
                },
            )
        )
        self.assertFalse(
            self.store.exchanged_binding(token["access_token"])["single_use"]
        )

    def test_the_originating_client_comes_from_the_subject_token(self) -> None:
        """Never from exchange input.

        A caller that could name its own originating client could borrow
        another client's authority, so the value is read off validated
        subject-token state and there is no parameter for it.
        """
        token = self.run_exchange()
        binding = self.store.exchanged_binding(token["access_token"])
        self.assertEqual("mcp-client", binding["actor_client_id"])
        self.assertEqual("mapp-mcp-broker", binding["broker_client_id"])


class ProfileRefusalTests(ExchangeTestCase):
    """The fixed profile: one grant type, one token type, no delegation."""

    def test_another_grant_type_is_refused(self) -> None:
        self.assert_refused("unsupported_grant_type", form(grant_type="authorization_code"))

    def test_a_missing_grant_type_is_refused(self) -> None:
        self.assert_refused("unsupported_grant_type", form(grant_type=None))

    def test_an_actor_token_is_refused(self) -> None:
        # Delegation is not part of this profile: an actor token would let the
        # broker act as a third party the grant never named.
        self.assert_refused("invalid_request", form(actor_token="mapp_a_other"))

    def test_an_actor_token_type_alone_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request", form(actor_token_type=exchange.ACCESS_TOKEN_TYPE)
        )

    def test_another_subject_token_type_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request",
            form(subject_token_type="urn:ietf:params:oauth:token-type:id_token"),
        )

    def test_another_requested_token_type_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request",
            form(requested_token_type="urn:ietf:params:oauth:token-type:refresh_token"),
        )

    def test_an_omitted_requested_token_type_defaults_to_access_token(self) -> None:
        self.assertIn("access_token", self.run_exchange(form(requested_token_type=None)))

    def test_a_duplicate_parameter_is_refused(self) -> None:
        # A parser keeping the first while a peer keeps the last is how two
        # components authorise different things from identical bytes.
        self.assert_refused("invalid_request", form(scope=["apply", "derive"]))

    def test_a_duplicate_grant_type_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request", form(grant_type=[exchange.GRANT_TYPE, exchange.GRANT_TYPE])
        )


class ResourceRefusalTests(ExchangeTestCase):
    """The resource is what stops a token being replayed at the wrong API."""

    def test_a_missing_resource_is_refused(self) -> None:
        self.assert_refused("invalid_target", form(resource=None))

    def test_two_resources_are_refused(self) -> None:
        self.assert_refused("invalid_target", form(resource=[RESOURCE, RESOURCE]))

    def test_an_unknown_resource_is_refused(self) -> None:
        self.assert_refused("invalid_target", form(resource="http://evil.example/api"))

    def test_a_trailing_slash_is_not_normalised_away(self) -> None:
        self.assert_refused("invalid_target", form(resource=RESOURCE + "/"))

    def test_an_audience_alias_is_refused(self) -> None:
        self.assert_refused("invalid_target", form(audience="config-api"))


class SubjectTokenRefusalTests(ExchangeTestCase):
    def test_a_missing_subject_token_is_refused(self) -> None:
        self.assert_refused("invalid_request", form(subject_token=None))

    def test_an_unknown_subject_token_is_refused(self) -> None:
        self.assert_refused("invalid_grant", form(subject_token="mapp_a_nosuch"))

    def test_an_expired_subject_token_is_refused(self) -> None:
        self._seed_subject("mapp_a_stale", "apply", expires_in=-1)
        self.assert_refused("invalid_grant", form(subject_token="mapp_a_stale"))

    def test_a_revoked_subject_token_is_refused(self) -> None:
        self._seed_subject("mapp_a_dead", "apply", revoked=True)
        self.assert_refused("invalid_grant", form(subject_token="mapp_a_dead"))

    def test_a_placeholder_audience_is_refused(self) -> None:
        """The audience is compared positively, not denied by a list.

        It used to read `audience not in (None, "mcp")`, so a token whose
        audience had never been set -- or carried the model's old "mcp"
        placeholder -- passed as a token A. Both are refused now because the
        comparison is against the configured MCP resource and nothing else.
        """
        for placeholder in ("", "mcp"):
            with self.subTest(audience=placeholder):
                self._seed_subject(
                    "mapp_a_" + (placeholder or "empty"), "apply", audience=placeholder
                )
                self.assert_refused(
                    "invalid_grant",
                    form(subject_token="mapp_a_" + (placeholder or "empty")),
                )

    def test_a_token_for_another_resource_is_refused(self) -> None:
        self._seed_subject("mapp_a_elsewhere", "apply", audience="http://other/api")
        self.assert_refused("invalid_grant", form(subject_token="mapp_a_elsewhere"))

    def test_a_token_b_cannot_be_exchanged_again(self) -> None:
        """Audience separation, in the direction that matters.

        Without this a token B could be used as the subject of another
        exchange, chaining one sixty-second credential into an unbounded
        series of them.
        """
        self._seed_subject("mapp_b_already", "apply", audience=RESOURCE)
        self.assert_refused("invalid_grant", form(subject_token="mapp_b_already"))


class ContextRefusalTests(ExchangeTestCase):
    """The operation binding: the broker does not infer it from scope."""

    def test_a_missing_context_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request", form(**{exchange.CONTEXT_PARAMETER: None})
        )

    def test_malformed_context_json_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request", form(**{exchange.CONTEXT_PARAMETER: "{"})
        )

    def test_a_context_with_duplicate_members_is_refused(self) -> None:
        # json.loads would keep the last silently, so a second operationId
        # could hide behind the first.
        raw = '{"version":"%s","operationId":"layers.values","operationId":"%s"}' % (
            canonical.SCHEME,
            OPERATION,
        )
        self.assert_refused("invalid_request", form(**{exchange.CONTEXT_PARAMETER: raw}))

    def test_a_context_that_is_not_an_object_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request", form(**{exchange.CONTEXT_PARAMETER: "[1,2]"})
        )

    def test_an_unknown_context_version_is_refused(self) -> None:
        # Never interpreted under this version: a new algorithm requires a new
        # version, and an approval issued under one is never read under another.
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: context(version="mapp-jcs-v2")}),
        )

    def test_a_missing_context_version_is_refused(self) -> None:
        value = json.loads(context())
        del value["version"]
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: json.dumps(value)}),
        )

    def test_an_operation_outside_the_allowlist_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: context(operationId="tokens.create")}),
        )

    def test_a_missing_request_digest_is_refused(self) -> None:
        value = json.loads(context())
        del value["requestDigest"]
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: json.dumps(value)}),
        )

    def test_a_digest_from_another_scheme_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: context(requestDigest="sha256:abc")}),
        )

    def test_a_method_that_disagrees_with_the_allowlist_is_refused(self) -> None:
        # The caller does not get to describe the operation differently from
        # the allowlist; the API recomputes against the real request.
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: context(method="GET")}),
        )

    def test_a_path_that_disagrees_with_the_allowlist_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: context(pathTemplate="/api/other")}),
        )


class DigestFormatTests(ExchangeTestCase):
    """The digest must be a digest, not merely something with the prefix."""

    def test_a_prefix_only_digest_is_refused(self) -> None:
        # The case that survived every earlier check: bound to the empty
        # digest, which matches whatever it is later compared against.
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: context(requestDigest=canonical.SCHEME + ":")}),
        )

    def test_a_short_digest_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: context(requestDigest=canonical.SCHEME + ":abcd")}),
        )

    def test_a_non_hex_digest_is_refused(self) -> None:
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: context(requestDigest=canonical.SCHEME + ":" + "z" * 64)}),
        )

    def test_an_uppercase_digest_is_refused(self) -> None:
        # One spelling only: two components must not disagree about case.
        self.assert_refused(
            "invalid_request",
            form(**{exchange.CONTEXT_PARAMETER: context(
                requestDigest=canonical.digest({"proposalId": "p1"}).upper()
            )}),
        )


class ScopeRefusalTests(ExchangeTestCase):
    """Refuse when requested is not a subset. Never intersect."""

    def _subject_under_a_grant_of(self, *scopes: str) -> None:
        """Seed a subject token whose grant consented to exactly these.

        Both of these tests used to narrow the grant by assigning through
        `store.query_grant(...).scopes`, which reached into the double's
        internals. SqlStore returns a snapshot and cannot be written that way,
        so the assertion held only against the stand-in -- precisely the class
        of divergence tests/store_contract.py now forbids. Seeding a second
        grant uses nothing but the store's real interface.
        """
        self.store.save_grant(
            Grant(
                grant_id="oauth:narrow",
                client_id="mcp-client",
                subject="operator",
                scopes=scopes,
            )
        )
        self._seed_subject(
            "mapp_a_narrow", "apply derive semantic:inspect", subject="oauth:narrow"
        )

    def test_a_scope_the_grant_never_consented_to_is_refused(self) -> None:
        """The consent screen is binding, not decorative.

        Token A's scope is derived from the grant, but nothing reconciled the
        two, so a grant that consented to nothing still minted an `apply`
        token B. The grant is the stronger statement: it is what the operator
        actually approved.
        """
        self._subject_under_a_grant_of()
        self.assert_refused("invalid_scope", form(subject_token="mapp_a_narrow"))

    def test_a_narrower_grant_narrows_the_token(self) -> None:
        self._subject_under_a_grant_of("inspect")
        self.assert_refused("invalid_scope", form(subject_token="mapp_a_narrow"))

    def test_a_permitted_but_unrequired_scope_is_refused(self) -> None:
        """Exactly the operation's scopes: no more, no fewer.

        The subject holds `derive`, so asking for `apply derive` passes the
        subset test -- but issuing it would hand a mutation token unrelated
        authority for its whole lifetime.
        """
        self.assert_refused("invalid_scope", form(scope="apply derive"))

    def test_a_scope_the_subject_lacks_is_refused_not_narrowed(self) -> None:
        """The single most important refusal in this file.

        Intersecting would return a working `apply` token to a caller that
        asked for `apply derive`, and nothing in the response would say the
        request had been altered.
        """
        self._seed_subject("mapp_a_narrow", "apply")
        description = self.assert_refused(
            "invalid_scope",
            form(subject_token="mapp_a_narrow", scope="apply derive"),
        )
        self.assertIn("derive", description)

    def test_no_token_is_issued_when_scope_is_refused(self) -> None:
        self._seed_subject("mapp_a_narrow", "apply")
        before = self.store.token_count()
        self.assert_refused(
            "invalid_scope", form(subject_token="mapp_a_narrow", scope="apply derive")
        )
        self.assertEqual(before, self.store.token_count())

    def test_an_empty_scope_is_refused(self) -> None:
        self.assert_refused("invalid_scope", form(scope=None))

    def test_duplicate_scope_values_are_refused(self) -> None:
        self.assert_refused("invalid_scope", form(scope="apply apply"))

    def test_a_request_that_does_not_name_the_operations_scopes_is_refused(self) -> None:
        """Renamed to say what actually fires.

        This was called "a subject missing the operation's own scope", after a
        branch that checked exactly that -- and which was unreachable, because
        `requested` must equal the operation's scopes and had already been
        tested against the subject. The branch is gone; the refusal here comes
        from the equality check.
        """
        self._seed_subject("mapp_a_reader", "derive")
        self.assert_refused(
            "invalid_scope", form(subject_token="mapp_a_reader", scope="derive")
        )

    def test_a_request_omitting_a_required_scope_is_refused(self) -> None:
        # layers.values needs both derive and semantic:inspect; asking for one
        # must not yield a token that the API would then refuse anyway.
        self.assert_refused(
            "invalid_scope",
            form(
                scope="derive",
                **{
                    exchange.CONTEXT_PARAMETER: context(
                        operationId=READ_OPERATION,
                        method="GET",
                        pathTemplate="/api/layers/{layerKey}/values",
                    )
                },
            ),
        )


class AllowlistDriftTests(unittest.TestCase):
    """The allowlist must agree with the configuration API's own actions.

    operations.py restates a contract owned by another service that this
    component cannot import at runtime. A restated contract nobody compares is
    one that has already drifted, so it is compared here.
    """

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "config-ui")
        )
        import control_api

        cls.actions = control_api.ACTION_SCHEMAS

    def test_every_allowlisted_scope_can_actually_be_issued(self) -> None:
        """The allowlist and the issuer must share one vocabulary.

        They did not. server.py restated a list that omitted `derive`,
        `semantic:inspect`, `federation:provision` and `semantic:apply`, so
        authlib refused the authorization request before the exchange was
        ever reached and four of these five operations were unreachable in
        the deployed configuration. This file compared operations.py against
        the *platform* and never against what the server can issue, which is
        the gap that let it stand.
        """
        import server

        supported = set(server.SUPPORTED_SCOPES)
        for name, operation in operations.OPERATIONS.items():
            with self.subTest(operation=name):
                self.assertLessEqual(set(operation.required_scopes), supported)

    def test_every_allowlisted_operation_exists_in_the_platform(self) -> None:
        for name in operations.OPERATIONS:
            with self.subTest(operation=name):
                self.assertIn(name, self.actions)

    def test_method_and_path_match_the_platform(self) -> None:
        for name, operation in operations.OPERATIONS.items():
            action = self.actions[name]
            with self.subTest(operation=name):
                self.assertEqual(action["method"], operation.method)
                self.assertEqual(action["pathTemplate"], operation.path_template)

    #: Risk classes that describe a pure read. Everything else writes, probes
    #: or produces an artifact, and its token must not be replayable.
    READ_RISKS = frozenset({"aggregate-data-read", "inspect", "read"})

    def test_each_key_equals_its_entrys_operation_id(self) -> None:
        """The key selects the operation; the field is what gets bound.

        exchange.py stamps `operation.operation_id` into the token, while
        lookup happens by dict key, so a one-token drift between them would
        bind a token to a different operation than the one validated.
        """
        for key, operation in operations.OPERATIONS.items():
            with self.subTest(key=key):
                self.assertEqual(key, operation.operation_id)

    def test_the_mutating_flag_is_derived_from_the_platforms_risk_class(self) -> None:
        """The one field that decides whether a token B is single-use.

        Nothing else constrains it: flipping `mutating` to False on three of
        the five entries left the whole suite green, and a non-single-use
        token is replayable for its full sixty seconds. Derived from `risk`
        rather than from `method`, because semantic.proposals.check is a POST
        whose risk is `inspect` -- method alone would mark reads as mutations
        and, worse, invite the inverse mistake.
        """
        for name, operation in operations.OPERATIONS.items():
            action = self.actions[name]
            with self.subTest(operation=name, risk=action["risk"]):
                self.assertEqual(
                    action["risk"] not in self.READ_RISKS,
                    operation.mutating,
                )

    def test_every_platform_risk_class_is_classified(self) -> None:
        """A risk class nobody has classified must not default to "read".

        A new action class arriving in the platform should fail here rather
        than silently become non-mutating the first time someone allowlists it.
        """
        known = self.READ_RISKS | {
            "apply", "database-definition", "database-plan", "database-refresh",
            "external-semantic-egress", "federation-observe",
            "federation-provision", "federation-register", "propose", "reload",
            "semantic-apply", "semantic-archive", "semantic-repair",
            "semantic-source", "visual",
        }
        seen = {spec.get("risk") for spec in self.actions.values()}
        self.assertEqual(
            set(), seen - known, "unclassified platform risk class"
        )

    def test_required_scopes_cover_the_actions_declared_scope(self) -> None:
        """The action's `scope` authorises it, so it must be required.

        federation.aliases.observe is the case that makes this worth pinning:
        its risk class is federation-observe but its scope is
        federation:provision, and requiring the risk class would admit a
        weaker grant than the API accepts.
        """
        for name, operation in operations.OPERATIONS.items():
            action = self.actions[name]
            with self.subTest(operation=name):
                self.assertIn(action["scope"], operation.required_scopes)
                for extra in action.get("requiredScopes") or ():
                    self.assertIn(extra, operation.required_scopes)


class AudienceSeparationTests(unittest.TestCase):
    """The two resources must never be the same value.

    If they were, a token A would target the configuration API too, and the
    separation the whole design rests on -- A never reaches /api, B never
    reaches /mcp -- would be a comment rather than a control.
    """

    def test_equal_resources_are_refused_at_construction(self) -> None:
        from issuer import MappAuthorizationServer

        with self.assertRaises(ValueError):
            MappAuthorizationServer(
                StubStore(),
                issuer="http://mcp.localhost",
                resource="http://x/api",
                config_api_resource="http://x/api",
                scopes_supported=(),
            )

    def test_distinct_resources_are_accepted(self) -> None:
        from issuer import MappAuthorizationServer

        server = MappAuthorizationServer(
            StubStore(),
            issuer="http://mcp.localhost",
            resource="http://mcp.localhost/mcp",
            config_api_resource="http://config.localhost/api",
            scopes_supported=(),
        )
        self.assertNotEqual(server.resource, server.config_api_resource)

    def test_the_default_configuration_keeps_them_distinct(self) -> None:
        import server as server_module

        authorization = server_module.build_authorization(StubStore())
        self.assertNotEqual(
            authorization.resource, authorization.config_api_resource
        )


class ExchangeOverHttpTests(unittest.TestCase):
    """The endpoint, over a real socket, on the control listener."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

    def setUp(self) -> None:
        from issuer import MappAuthorizationServer

        self.store = StubStore()
        self.store.add_client(
            Client(
                client_id="mapp-mcp-broker",
                name="Broker",
                redirect_uris=(),
                scopes=(),
                token_endpoint_auth_method="client_secret_basic",
                client_secret="broker-secret",
            )
        )
        now = dt.datetime.now(dt.timezone.utc)
        self.store.add_client(
            Client(client_id="mcp-client", name="Claude Code",
                   redirect_uris=("http://127.0.0.1:9/cb",), scopes=("apply",),
                   token_endpoint_auth_method="none")
        )
        self.store.save_grant(
            Grant(grant_id="oauth:grant-1", client_id="mcp-client",
                  subject="admin", scopes=("apply",))
        )
        self.store.save_token(
            "mapp_a_subject",
            Token(
                token_hash="",
                client_id="mcp-client",
                scope="apply",
                subject="oauth:grant-1",
                issued_at=int(now.timestamp()),
                expires_in=900,
                audience=MCP_RESOURCE,
            ),
        )
        self.authorization = MappAuthorizationServer(
            self.store,
            issuer="http://mcp.localhost",
            # The two resources are distinct on purpose: token A is for the
            # MCP endpoint and token B for the configuration API, and the
            # server refuses to start if they are equal.
            resource=MCP_RESOURCE,
            config_api_resource=RESOURCE,
            scopes_supported=("apply",),
        )
        self.httpd = ControlServer(("127.0.0.1", 0), self.authorization)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, fields, *, auth=("mapp-mcp-broker", "broker-secret")):
        body = urllib.parse.urlencode(fields)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body.encode())),
        }
        if auth is not None:
            raw = f"{auth[0]}:{auth[1]}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", "/internal/oauth/exchange", body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode())
        finally:
            connection.close()

    def _fields(self, **overrides):
        fields = {
            "grant_type": exchange.GRANT_TYPE,
            "subject_token": "mapp_a_subject",
            "subject_token_type": exchange.ACCESS_TOKEN_TYPE,
            "resource": RESOURCE,
            "scope": "apply",
            exchange.CONTEXT_PARAMETER: context(),
        }
        fields.update(overrides)
        return fields

    def test_an_authenticated_broker_receives_token_b(self) -> None:
        status, body = self.post(self._fields())
        self.assertEqual(200, status, body)
        self.assertTrue(body["access_token"].startswith(exchange.TOKEN_B_PREFIX))

    def test_an_unauthenticated_request_is_refused_before_anything_else(self) -> None:
        # It must not learn whether the subject token exists.
        status, body = self.post(self._fields(subject_token="mapp_a_nosuch"), auth=None)
        self.assertEqual(401, status)
        self.assertEqual("invalid_client", body["error"])

    def test_a_wrong_client_secret_is_refused(self) -> None:
        status, body = self.post(self._fields(), auth=("mapp-mcp-broker", "wrong"))
        self.assertEqual(401, status)
        self.assertEqual("invalid_client", body["error"])

    def test_an_unknown_client_is_refused(self) -> None:
        status, _ = self.post(self._fields(), auth=("nosuch", "broker-secret"))
        self.assertEqual(401, status)

    def test_a_public_client_cannot_broker(self) -> None:
        self.store.add_client(
            Client(
                client_id="public-client",
                name="Public",
                redirect_uris=(),
                scopes=(),
                token_endpoint_auth_method="none",
            )
        )
        status, _ = self.post(self._fields(), auth=("public-client", ""))
        self.assertEqual(401, status)

    def test_a_refusal_reports_an_oauth_error_code(self) -> None:
        status, body = self.post(self._fields(resource="http://evil.example"))
        self.assertEqual(400, status)
        self.assertEqual("invalid_target", body["error"])


if __name__ == "__main__":
    unittest.main()
