"""Token A at the door: the three refusals, and the one acceptance.

The failure table gives three distinguishable answers, and the distinction is
the point. A client that presented nothing, a client whose credential no longer
resolves, and a client whose credential is fine but too narrow each have a
different next move, and a server that answers all three the same way tells two
of them to do the wrong thing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import era_guard  # noqa: E402
from app import build_app  # noqa: E402
from asgi_harness import StubIntrospection, active, call  # noqa: E402
from introspection_client import IntrospectionUnavailable  # noqa: E402

ORIGIN = "http://mcp.localhost"
RESOURCE = ORIGIN + "/mcp"
TOKEN = "mapp_a_live"


class Reached:
    def __init__(self) -> None:
        self.calls = 0
        self.caller = None

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.caller = scope.get("mapp.caller")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", b"0")],
        })
        await send({"type": "http.response.body", "body": b""})


def stack(records=None, *, raises=None):
    inner = Reached()
    introspection = StubIntrospection(records, raises=raises)
    app, resource = build_app(
        origin=ORIGIN, issuer=ORIGIN, inner=inner, introspection=introspection
    )
    return app, inner, introspection, resource


def rpc(app, *, token=None, body=b"{}"):
    headers = {"MCP-Protocol-Version": era_guard.MODERN_VERSION}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return call(app, headers=headers, body=body)


class AcceptanceTests(unittest.TestCase):
    def test_a_live_credential_reaches_the_runtime_with_its_grant(self) -> None:
        """The handler needs the grant, not the token: P3 makes the grant the
        actor, and a handler that saw the credential could pass it on."""
        app, inner, _, _ = stack({TOKEN: active()})
        self.assertEqual(200, rpc(app, token=TOKEN).status)
        self.assertEqual("oauth:test-grant", inner.caller.grant_id)
        self.assertEqual({"mcp:connect", "inspect"}, set(inner.caller.scopes))

    def test_the_scheme_is_case_insensitive_but_the_token_is_not(self) -> None:
        app, _, _, _ = stack({TOKEN: active()})
        self.assertEqual(200, call(
            app,
            headers={
                "MCP-Protocol-Version": era_guard.MODERN_VERSION,
                "Authorization": f"bEaReR {TOKEN}",
            },
            body=b"{}",
        ).status)
        self.assertEqual(401, rpc(app, token=TOKEN.upper()).status)


class CredentialCarriageTests(unittest.TestCase):
    """The raw token reaches a handler, and reaches nothing else.

    A tool that acts has to present the caller's token A as the subject of an
    exchange, so the runtime has to keep it. Keeping a live credential on the
    request scope is worth a test in both directions: that it is there, and that
    it does not come out anywhere it should not.
    """

    def test_the_handler_receives_the_credential_it_will_exchange(self) -> None:
        app, inner, _, _ = stack({TOKEN: active()})
        rpc(app, token=TOKEN)
        self.assertEqual(TOKEN, inner.caller.token)

    def test_the_caller_is_published_where_a_tool_can_read_it(self) -> None:
        """The middleware sets a contextvar; a tool is the only thing that reads it.

        Removing the set survived mutation until this existed, because the tool
        tests set it themselves -- which is right for testing a tool and leaves
        the publishing untested. In production every tool would then refuse
        with "requires an authenticated caller" on a request that was properly
        authenticated, and the suite would stay green.
        """
        from authentication import CURRENT_CALLER

        seen = {}

        class Reader:
            async def __call__(self, scope, receive, send):
                seen["caller"] = CURRENT_CALLER.get()
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"0")],
                })
                await send({"type": "http.response.body", "body": b""})

        app, resource = build_app(
            origin=ORIGIN, issuer=ORIGIN, inner=Reader(),
            introspection=StubIntrospection({TOKEN: active()}),
        )
        rpc(app, token=TOKEN)
        self.assertIsNotNone(seen["caller"], "nothing published the caller")
        self.assertEqual("oauth:test-grant", seen["caller"].grant_id)
        self.assertEqual(TOKEN, seen["caller"].token)

    def test_the_credential_is_not_in_the_repr(self) -> None:
        """Because this object lands in tracebacks and in anything logging scope.

        A default repr over __slots__ would print a live token A into both.
        Making the unsafe rendering impossible beats remembering not to log it.
        """
        app, inner, _, _ = stack({TOKEN: active()})
        rpc(app, token=TOKEN)
        rendered = repr(inner.caller)
        self.assertNotIn(TOKEN, rendered)
        # Still useful: the grant is what an operator needs to correlate.
        self.assertIn("oauth:test-grant", rendered)

    def test_the_credential_is_not_an_ordinary_attribute(self) -> None:
        """`vars()` and anything walking __dict__ finds nothing.

        __slots__ already means there is no __dict__, which is what stops a
        generic "log the object" helper from finding it.
        """
        app, inner, _, _ = stack({TOKEN: active()})
        rpc(app, token=TOKEN)
        with self.assertRaises(TypeError):
            vars(inner.caller)


class RefusalTests(unittest.TestCase):
    def test_no_credential_is_401_with_no_bearer_error_code(self) -> None:
        """Nothing is wrong with the credential; there is not one.

        Telling a client holding nothing that its token is invalid sends it
        looking for something to repair rather than something to obtain.
        """
        app, inner, _, _ = stack()
        response = rpc(app)
        self.assertEqual(401, response.status)
        self.assertNotIn("error=", response.headers["www-authenticate"])
        self.assertIn("resource_metadata=", response.headers["www-authenticate"])
        self.assertEqual(0, inner.calls)

    def test_a_malformed_authorization_header_counts_as_none(self) -> None:
        for value in ("", "Bearer", "Bearer   ", "Basic abc", "mapp_a_bare"):
            with self.subTest(value=value):
                app, _, _, _ = stack({TOKEN: active()})
                response = call(
                    app,
                    headers={
                        "MCP-Protocol-Version": era_guard.MODERN_VERSION,
                        "Authorization": value,
                    },
                    body=b"{}",
                )
                self.assertEqual(401, response.status)
                self.assertNotIn("error=", response.headers["www-authenticate"])

    def test_an_unresolvable_credential_is_401_invalid_token(self) -> None:
        app, inner, _, _ = stack({TOKEN: active()})
        response = rpc(app, token="mapp_a_revoked")
        self.assertEqual(401, response.status)
        self.assertIn('error="invalid_token"', response.headers["www-authenticate"])
        self.assertEqual(0, inner.calls)

    def test_an_inactive_record_is_refused_even_when_it_looks_right(self) -> None:
        """The active flag is the decision; the rest of the record is not.

        Removing the active check survived mutation until this existed, because
        an inactive record carries no audience and was refused by the audience
        test instead -- the right answer reached by the wrong route. A revoked
        or expired token whose record still names the correct resource and
        scopes is the case that would then have been admitted.
        """
        app, inner, _, _ = stack({
            TOKEN: {
                "active": False,
                "scope": "mcp:connect inspect",
                "sub": "oauth:test-grant",
                "client_id": "mcp-test-client",
                "aud": RESOURCE,
            }
        })
        response = rpc(app, token=TOKEN)
        self.assertEqual(401, response.status)
        self.assertIn('error="invalid_token"', response.headers["www-authenticate"])
        self.assertEqual(0, inner.calls)

    def test_a_credential_for_another_resource_is_refused(self) -> None:
        """The audience separation, checked here as well as at the component.

        An audience confusion is the one failure that silently accepts a
        credential minted for somewhere else, so it is worth asserting twice.
        """
        app, inner, _, _ = stack({
            TOKEN: active(audience="http://config.localhost/api")
        })
        response = rpc(app, token=TOKEN)
        self.assertEqual(401, response.status)
        self.assertIn('error="invalid_token"', response.headers["www-authenticate"])
        self.assertEqual(0, inner.calls)

    def test_a_live_credential_without_connect_is_403_insufficient_scope(self) -> None:
        app, inner, _, _ = stack({TOKEN: active(scopes="inspect")})
        response = rpc(app, token=TOKEN)
        self.assertEqual(403, response.status)
        challenge = response.headers["www-authenticate"]
        self.assertIn('error="insufficient_scope"', challenge)
        # Every required scope in one value, so a client asks once rather than
        # discovering the next missing scope on the next refusal.
        self.assertIn('scope="mcp:connect"', challenge)
        self.assertEqual(0, inner.calls)

    def test_the_three_refusals_are_distinguishable(self) -> None:
        """If they were not, the table would be describing one behaviour."""
        app, _, _, _ = stack({TOKEN: active(scopes="inspect")})
        none = rpc(app)
        bad = rpc(app, token="mapp_a_nope")
        narrow = rpc(app, token=TOKEN)
        self.assertEqual({401, 401, 403}, {none.status, bad.status, narrow.status})
        self.assertNotEqual(
            none.headers["www-authenticate"], bad.headers["www-authenticate"]
        )
        self.assertNotEqual(
            bad.headers["www-authenticate"], narrow.headers["www-authenticate"]
        )


class UnavailableTests(unittest.TestCase):
    def test_an_unreachable_authorizer_is_503_not_401(self) -> None:
        """The credential was never judged.

        Calling it invalid tells the client to go and obtain another one when
        the fault is entirely ours, and hides a deployment failure behind what
        reads as ordinary client error.
        """
        app, inner, _, _ = stack(raises=IntrospectionUnavailable("down"))
        response = rpc(app, token=TOKEN)
        self.assertEqual(503, response.status)
        self.assertNotIn("www-authenticate", response.headers)
        self.assertEqual(0, inner.calls)


class CacheTests(unittest.TestCase):
    def test_the_metadata_document_is_never_authenticated(self) -> None:
        """A client reads it before it can hold anything."""
        app, _, introspection, _ = stack()
        response = call(
            app, method="GET", path="/.well-known/oauth-protected-resource/mcp"
        )
        self.assertEqual(200, response.status)
        self.assertEqual(0, introspection.calls, "discovery must not introspect")

    def test_the_guard_refuses_before_a_credential_is_ever_resolved(self) -> None:
        """Ordering, asserted rather than assumed.

        A request in the wrong protocol era is refused whatever it carries, so
        introspecting first would spend a call -- and a revocation window -- on
        a request that was never going to be dispatched.
        """
        app, _, introspection, _ = stack({TOKEN: active()})
        response = call(
            app,
            headers={"MCP-Protocol-Version": "2025-06-18", "Authorization": f"Bearer {TOKEN}"},
            body=b"{}",
        )
        self.assertEqual(400, response.status)
        self.assertEqual(0, introspection.calls)


if __name__ == "__main__":
    unittest.main()
