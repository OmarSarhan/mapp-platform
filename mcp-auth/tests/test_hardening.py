"""Regressions for the second review pass: unconstrained controls and edge cases.

Everything here pins a control that a mutation survived, or a defect a probe
reproduced. The grouping follows the property rather than the module, because
several controls span the adapter, the store and the routes.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from authlib.oauth2.rfc7636 import create_s256_code_challenge  # noqa: E402

import passwords  # noqa: E402
from authlib_adapter import MappResponse  # noqa: E402
from issuer import MappAuthorizationServer  # noqa: E402
from models import AuthorizationCode, Client, PendingAuthorization, Session  # noqa: E402
from server import EdgeServer  # noqa: E402
from stub_store import PendingLimitReached, StubStore  # noqa: E402

ISSUER = "http://mcp.localhost"
REDIRECT_URI = "http://127.0.0.1:9/callback"
SECOND_REDIRECT_URI = "http://127.0.0.1:9/second"
VERIFIER = "a" * 64
PASSWORD = "correct horse battery staple"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"

    def setUp(self) -> None:
        self.store = StubStore()
        self.store.add_client(
            Client(
                client_id="mcp-client",
                name="Claude Code",
                # Two registered URIs, so a value taken from the payload and one
                # taken from the client default are distinguishable.
                redirect_uris=(REDIRECT_URI, SECOND_REDIRECT_URI),
                scopes=("mcp:connect", "inspect"),
                token_endpoint_auth_method="none",
            )
        )
        self.authorization = MappAuthorizationServer(
            self.store,
            issuer=ISSUER,
            resource=ISSUER + "/mcp",
            scopes_supported=("mcp:connect", "inspect", "propose"),
            advertised_scopes=("mcp:connect", "inspect"),
            admin_password_hash=passwords.password_hash(PASSWORD),
            secure_cookies=False,
        )
        self.httpd = EdgeServer(("127.0.0.1", 0), self.authorization)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, *, body=None, cookie=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            sent = dict(headers or {})
            payload = None
            if body is not None:
                payload = body if isinstance(body, str) else urllib.parse.urlencode(body)
                sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
                sent.setdefault("Content-Length", str(len(payload.encode())))
            if cookie:
                sent["Cookie"] = cookie
            connection.request(method, path, body=payload, headers=sent)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode(
                "utf-8", "replace"
            )
        finally:
            connection.close()

    def raw(self, request_bytes: bytes) -> bytes:
        client = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            client.sendall(request_bytes)
            chunks = []
            while True:
                try:
                    received = client.recv(65536)
                except socket.timeout:
                    break
                if not received:
                    break
                chunks.append(received)
            return b"".join(chunks)
        finally:
            client.close()

    def authorize_url(self, **overrides) -> str:
        params = {
            "response_type": "code",
            "client_id": "mcp-client",
            "redirect_uri": REDIRECT_URI,
            "scope": "mcp:connect inspect",
            "state": "client-state-123",
            "code_challenge": create_s256_code_challenge(VERIFIER),
            "code_challenge_method": "S256",
        }
        params.update(overrides)
        return "/oauth/authorize?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )

    @staticmethod
    def _hidden(page: str, name: str) -> str:
        marker = f'name="{name}" value="'
        start = page.index(marker) + len(marker)
        return page[start : page.index('"', start)]

    def sign_in(self, url=None):
        status, headers, _ = self.request("GET", url or self.authorize_url())
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]
        _, _, page = self.request("GET", headers["Location"])
        csrf = self._hidden(page, "csrf")
        _, headers, _ = self.request(
            "POST", "/oauth/login", body={"rid": rid, "csrf": csrf, "password": PASSWORD}
        )
        return headers["Set-Cookie"].split(";", 1)[0], rid

    def consent_page(self, cookie, rid, url=None):
        _, _, page = self.request(
            "GET", (url or self.authorize_url()) + f"&rid={rid}", cookie=cookie
        )
        return page


class EmptyChallengeMethodTests(Base):
    """An empty code_challenge_method must not reach authlib's verifier table."""

    def test_an_empty_method_is_refused_at_the_authorization_endpoint(self) -> None:
        status, headers, _ = self.request(
            "GET", self.authorize_url(code_challenge_method="")
        )
        # Whatever the shape of the refusal, it must be an OAuth error and not
        # a stored "" that later raises RuntimeError at the token endpoint.
        self.assertIn(status, (302, 400))
        if status == 302:
            self.assertIn("error=", headers["Location"])

    def test_a_stored_empty_method_never_reaches_the_token_endpoint(self) -> None:
        # Belt and braces: even if a record were somehow stored with "", the
        # exchange must answer rather than drop the connection.
        self.store.save_authorization_code(
            AuthorizationCode(
                code="blank-method",
                client_id="mcp-client",
                redirect_uri=REDIRECT_URI,
                scope="mcp:connect",
                subject="oauth:grant-1",
                code_challenge=create_s256_code_challenge(VERIFIER),
                # The value the comment is about: stored empty, not "S256".
                code_challenge_method="",
            )
        )
        status, _, _ = self.request(
            "POST",
            "/oauth/token",
            body={
                "grant_type": "authorization_code",
                "code": "blank-method",
                "redirect_uri": REDIRECT_URI,
                "client_id": "mcp-client",
                "code_verifier": VERIFIER,
            },
        )
        self.assertEqual(200, status)


class ContentLengthTests(Base):
    """Content-Length is a bare run of digits or it is refused."""

    def _post_with_length(self, raw_length: bytes) -> bytes:
        body = b"grant_type=authorization_code"
        return self.raw(
            b"POST /oauth/token HTTP/1.1\r\nHost: x\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: " + raw_length + b"\r\n\r\n" + body
        )

    def test_a_signed_length_is_refused(self) -> None:
        self.assertIn(b"Malformed Content-Length", self._post_with_length(b"+29"))

    def test_an_underscored_length_is_refused(self) -> None:
        self.assertIn(b"Malformed Content-Length", self._post_with_length(b"2_9"))

    def test_a_padded_length_is_refused(self) -> None:
        self.assertIn(b"Malformed Content-Length", self._post_with_length(b"  29 "))

    def test_a_leading_zero_length_is_refused(self) -> None:
        # int() reads "029" as 29; an RFC-compliant intermediary may not.
        self.assertIn(b"Malformed Content-Length", self._post_with_length(b"029"))

    def test_a_plain_length_is_accepted(self) -> None:
        raw = self._post_with_length(b"29")
        self.assertNotIn(b"Malformed Content-Length", raw)


class QueryChannelTests(Base):
    """The token endpoint takes its parameters from the body only."""

    def test_a_query_string_on_the_token_endpoint_is_refused(self) -> None:
        # RFC 6749 s3.2. Merging would give request.form and request.payload
        # two different views of one request, and put the grant in proxy logs.
        status, _, raw = self.request(
            "POST",
            "/oauth/token?client_id=mcp-client&grant_type=authorization_code",
            body={"code": "x", "code_verifier": VERIFIER},
        )
        self.assertEqual(400, status)
        self.assertIn("Query parameters are not accepted", raw)

    def test_a_repeated_name_in_the_query_is_refused(self) -> None:
        # Two values for one name is ambiguous however it arrives; authlib's
        # own duplicate detection answers this one.
        status, _, raw = self.request(
            "GET", self.authorize_url() + "&client_id=mcp-client"
        )
        self.assertEqual(400, status)
        self.assertIn("Multiple 'client_id'", raw)


class MergeQueryBackstopTests(unittest.TestCase):
    """merge_query's both-channels refusal, tested where it now lives.

    build_request refuses a query string on the body-reading path, so this rule
    is no longer reachable over the wire. It is unit-tested directly rather
    than deleted, because it is the invariant any future endpoint reading both
    channels depends on -- and an untested rule is one that quietly rots.
    """

    def test_a_name_in_both_channels_is_refused(self) -> None:
        from collections import defaultdict

        from authlib_adapter import FormError, merge_query

        datalist = defaultdict(list)
        datalist["client_id"] = ["from-body"]
        with self.assertRaises(FormError) as caught:
            merge_query({"client_id": "from-body"}, datalist, "client_id=from-query")
        self.assertIn("supplied in both query and body", str(caught.exception.description))

    def test_disjoint_names_merge(self) -> None:
        from collections import defaultdict

        from authlib_adapter import merge_query

        datalist = defaultdict(list)
        datalist["code"] = ["c1"]
        data, merged = merge_query({"code": "c1"}, datalist, "state=s1")
        self.assertEqual({"code": "c1", "state": "s1"}, data)


class HeaderWritingTests(unittest.TestCase):
    """write_to applies one rule to every header it emits."""

    class _Handler:
        command = "GET"
        close_connection = False

        def __init__(self):
            self.written = []

        def send_response(self, status):
            self.written.append(("__status__", status))

        def send_header(self, name, value):
            self.written.append((name.lower(), value))

        def end_headers(self):
            self.written.append(("__end__", None))

        @property
        def wfile(self):
            handler = self

            class _W:
                @staticmethod
                def write(data):
                    handler.written.append(("__body__", data))

            return _W()

    def _emit(self, headers, body="body"):
        handler = self._Handler()
        MappResponse(200, body, list(headers)).write_to(handler)
        return handler.written

    def test_a_caller_supplied_content_length_is_ignored(self) -> None:
        written = self._emit([("Content-Length", "999")], body="body")
        lengths = [v for k, v in written if k == "content-length"]
        # Exactly one, and it is the real body length.
        self.assertEqual(["4"], lengths)

    def test_security_headers_are_not_emitted_twice(self) -> None:
        written = self._emit([("X-Content-Type-Options", "sniff-me")])
        values = [v for k, v in written if k == "x-content-type-options"]
        # Two values would be joined by browsers into one invalid value,
        # defeating nosniff entirely.
        self.assertEqual(1, len(values))

    def test_a_referrer_policy_is_not_emitted_twice(self) -> None:
        written = self._emit([("Referrer-Policy", "unsafe-url")])
        self.assertEqual(1, len([v for k, v in written if k == "referrer-policy"]))

    def test_a_non_latin1_header_value_is_refused(self) -> None:
        # Otherwise send_header raises UnicodeEncodeError after the status line
        # is already on the wire, and the client sees a bare close.
        with self.assertRaises(ValueError):
            self._emit([("Location", "http://x/cb?s=✓")])

    def test_a_carriage_return_in_a_header_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._emit([("Location", "http://x/cb\r\nSet-Cookie: pwned=1")])


class StoreAtRestTests(unittest.TestCase):
    """Only hashes are held at rest -- asserted on the keys, not the records."""

    def setUp(self) -> None:
        self.store = StubStore()

    def test_a_token_is_keyed_by_its_hash(self) -> None:
        raw = "mapp_a_secret-value"
        self.store.save_token(raw, _token(raw))
        self.assertNotIn(raw, self.store._tokens)
        self.assertIn(hashlib.sha256(raw.encode()).hexdigest(), self.store._tokens)

    def test_a_session_is_keyed_by_its_hash(self) -> None:
        raw = "cookie-secret-value"
        now = time.time()
        self.store.save_session(
            raw, Session(subject="admin", auth_time=now, expires_at=now + 600)
        )
        self.assertNotIn(raw, self.store._sessions)
        self.assertIn(hashlib.sha256(raw.encode()).hexdigest(), self.store._sessions)


def _token(raw: str):
    from models import Token

    return Token(
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        client_id="mcp-client",
        scope="mcp:connect",
        subject="oauth:grant-1",
        issued_at=int(time.time()),
        expires_in=900,
    )


class ExpiryTests(Base):
    """Expiry predicates are load-bearing, not decoration."""

    def test_an_expired_session_cannot_consent(self) -> None:
        cookie, rid = self.sign_in()
        page = self.consent_page(cookie, rid)
        csrf = self._hidden(page, "csrf")
        # Age the session past its expiry.
        for record in self.store._sessions.values():
            record.expires_at = 0.0
        status, _, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": csrf, "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(403, status)

    def test_an_expired_pending_record_is_refused(self) -> None:
        cookie, rid = self.sign_in()
        page = self.consent_page(cookie, rid)
        csrf = self._hidden(page, "csrf")
        self.store._pending[rid].expires_at = 0.0
        status, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": csrf, "decision": "allow"},
            cookie=cookie,
        )
        # Refused as unknown-or-expired by the lookup that precedes the
        # conditional consume; either way no authorization is issued.
        self.assertEqual(400, status)
        self.assertNotIn("Location", headers)


class PendingLifecycleTests(Base):
    """A rejected consent must not destroy the operator's parked request."""

    def test_a_wrong_form_token_leaves_the_record_intact(self) -> None:
        cookie, rid = self.sign_in()
        page = self.consent_page(cookie, rid)
        csrf = self._hidden(page, "csrf")
        status, _, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": "wrong", "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(403, status)
        # The whole point: the operator can still complete their own request.
        self.assertIn(rid, self.store._pending)
        status, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": csrf, "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(302, status)
        self.assertIn("code=", headers["Location"])

    def test_simultaneous_consents_produce_one_winner(self) -> None:
        """One consent is honoured; this does NOT prove the store is atomic.

        Splitting consume_pending into a read under one lock hold and a delete
        under another -- a genuine TOCTOU -- passes this test in 6 runs of 6,
        even with the switch interval lowered: each consent is a full HTTP
        round trip, so the window between the two acquisitions never lines up.
        What is pinned here is that a record is honoured once; the atomicity
        claim is pinned against the SQL store, where the conditional UPDATE is
        the mechanism and its mutation IS caught, and the SQL store is what the
        deployed component uses.
        """
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, previous)
        cookie, rid = self.sign_in()
        page = self.consent_page(cookie, rid)
        csrf = self._hidden(page, "csrf")
        results: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)

        def worker() -> None:
            barrier.wait()
            status, _, _ = self.request(
                "POST",
                "/oauth/authorize",
                body={"rid": rid, "csrf": csrf, "decision": "allow"},
                cookie=cookie,
            )
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(1, results.count(302), f"statuses={sorted(results)}")


class PendingGrowthTests(unittest.TestCase):
    """The pending table is reclaimed and capped."""

    def setUp(self) -> None:
        self.store = StubStore()

    def _pending(self, request_id: str, expires_at: float) -> PendingAuthorization:
        return PendingAuthorization(
            request_id=request_id,
            query="",
            client_id="mcp-client",
            redirect_uri=REDIRECT_URI,
            scopes=("mcp:connect",),
            csrf="c",
            expires_at=expires_at,
        )

    def test_expired_records_are_swept_on_write(self) -> None:
        for index in range(50):
            self.store._pending[f"old-{index}"] = self._pending(f"old-{index}", 0.0)
        self.store.save_pending(self._pending("live", time.time() + 600))
        self.assertEqual(["live"], list(self.store._pending))

    def test_one_source_cannot_fill_the_shared_table(self) -> None:
        """A global cap alone would let a flood deny everyone else.

        Parking a record is unauthenticated and costs the caller nothing, so a
        purely global limit converts an unbounded-memory problem into a total
        lockout of /oauth/authorize for every legitimate user.
        """
        future = time.time() + 600
        flooder = "198.51.100.7"
        refused = 0
        for index in range(StubStore.MAX_PENDING_PER_SOURCE + 20):
            record = self._pending(f"flood-{index}", future)
            record.source = flooder
            try:
                self.store.save_pending(record)
            except PendingLimitReached:
                refused += 1
        self.assertGreater(refused, 0, "the flooder was never refused")
        # Another source is unaffected.
        victim = self._pending("legitimate", future)
        victim.source = "203.0.113.5"
        self.store.save_pending(victim)
        self.assertIn("legitimate", self.store._pending)

    def test_the_table_is_capped(self) -> None:
        future = time.time() + 600
        for index in range(StubStore.MAX_PENDING):
            self.store._pending[f"k{index}"] = self._pending(f"k{index}", future)
        with self.assertRaises(PendingLimitReached):
            self.store.save_pending(self._pending("one-too-many", future))


class FormTokenRotationTests(Base):
    """Binding a record to a session invalidates the token read before it."""

    def test_the_token_read_from_the_login_page_dies_on_bind(self) -> None:
        # An attacker can mint a record and read its csrf with no session at
        # all, so the binding alone would not stop them submitting it once a
        # lured GET had bound it to the operator. Rotation kills that copy.
        _, headers, _ = self.request("GET", self.authorize_url())
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]
        _, _, login_page = self.request("GET", f"/oauth/login?rid={rid}")
        token_before_binding = self._hidden(login_page, "csrf")

        cookie, _ = self.sign_in()
        page = self.consent_page(cookie, rid)
        token_after_binding = self._hidden(page, "csrf")
        self.assertNotEqual(token_before_binding, token_after_binding)

        status, _, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": token_before_binding, "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(403, status)

    def test_the_rotated_token_still_completes_the_flow(self) -> None:
        cookie, rid = self.sign_in()
        page = self.consent_page(cookie, rid)
        status, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": self._hidden(page, "csrf"), "decision": "allow"},
            cookie=cookie,
        )
        self.assertEqual(302, status)
        self.assertIn("code=", headers["Location"])


class ConstructorDefaultTests(unittest.TestCase):
    """The server must be constructable with its own documented defaults."""

    def test_both_scope_arguments_may_be_omitted(self) -> None:
        # The advertised/acceptance split briefly made list(None) reachable
        # through the constructor's own defaults.
        server = MappAuthorizationServer(
            StubStore(), issuer=ISSUER, resource=ISSUER + "/mcp"
        )
        self.assertEqual([], server.advertised_scopes)
        self.assertEqual([], list(server.scopes_supported or ()))

    def test_advertised_defaults_to_the_accepted_set(self) -> None:
        server = MappAuthorizationServer(
            StubStore(), issuer=ISSUER, resource=ISSUER + "/mcp",
            scopes_supported=("mcp:connect", "inspect"),
        )
        self.assertEqual(["mcp:connect", "inspect"], server.advertised_scopes)


class ContentSecurityPolicyBindingTests(Base):
    """The nonce in the header and the nonce in the page must be the same."""

    def test_the_page_nonce_matches_the_header_nonce(self) -> None:
        cookie, rid = self.sign_in()
        _, headers, page = self.request(
            "GET", self.authorize_url() + f"&rid={rid}", cookie=cookie
        )
        policy = headers["Content-Security-Policy"]
        start = policy.index("'nonce-") + len("'nonce-")
        nonce = policy[start : policy.index("'", start)]
        # Unbound, the two drift apart and every page renders unstyled, which
        # no substring assertion on the policy would notice.
        self.assertIn(f'<style nonce="{nonce}">', page)

    def test_the_login_page_nonce_matches_too(self) -> None:
        _, headers, _ = self.request("GET", self.authorize_url())
        rid = urllib.parse.parse_qs(
            urllib.parse.urlsplit(headers["Location"]).query
        )["rid"][0]
        _, headers, page = self.request("GET", f"/oauth/login?rid={rid}")
        policy = headers["Content-Security-Policy"]
        start = policy.index("'nonce-") + len("'nonce-")
        nonce = policy[start : policy.index("'", start)]
        self.assertIn(f'<style nonce="{nonce}">', page)


class RedirectUriProvenanceTests(Base):
    """The stored redirect_uri comes from the request, not the client default."""

    def test_the_second_registered_uri_is_recorded(self) -> None:
        url = self.authorize_url(redirect_uri=SECOND_REDIRECT_URI)
        cookie, rid = self.sign_in(url)
        page = self.consent_page(cookie, rid, url)
        csrf = self._hidden(page, "csrf")
        _, headers, _ = self.request(
            "POST",
            "/oauth/authorize",
            body={"rid": rid, "csrf": csrf, "decision": "allow"},
            cookie=cookie,
        )
        location = headers["Location"]
        self.assertTrue(location.startswith(SECOND_REDIRECT_URI))
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["code"][0]
        record = self.store.query_authorization_code(code, "mcp-client")
        # A client-default source would silently record the first URI.
        self.assertEqual(SECOND_REDIRECT_URI, record.redirect_uri)


class MetadataDocumentTests(Base):
    """The document is asserted whole, so it cannot advertise the unimplemented."""

    def test_the_document_matches_exactly(self) -> None:
        _, _, raw = self.request("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(
            {
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/oauth/authorize",
                "token_endpoint": ISSUER + "/oauth/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
                # The advertised set, narrower than what is accepted.
                "scopes_supported": ["mcp:connect", "inspect"],
                "authorization_response_iss_parameter_supported": True,
            },
            json.loads(raw),
        )

    def test_a_privileged_scope_is_accepted_though_not_advertised(self) -> None:
        # P2's step-up: absent from discovery, still grantable on request.
        self.store.add_client(
            Client(
                client_id="stepup",
                name="Step-up",
                redirect_uris=(REDIRECT_URI,),
                scopes=("mcp:connect", "propose"),
                token_endpoint_auth_method="none",
            )
        )
        status, headers, _ = self.request(
            "GET", self.authorize_url(client_id="stepup", scope="propose")
        )
        # Reaches the login redirect rather than being refused as out of scope.
        self.assertEqual(302, status)
        self.assertTrue(headers["Location"].startswith("/oauth/login?rid="))


class PasswordInteropTests(unittest.TestCase):
    """passwords.py and config-ui/control_plane.py must stay byte-compatible.

    The rule was previously enforced only by a comment. P4 copies the stored
    administrator credential from one to the other, so a divergence would not
    surface until it locked the operator out.
    """

    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).resolve().parents[2] / "config-ui" / "control_plane.py"
        spec = importlib.util.spec_from_file_location("_control_plane", path)
        cls.control_plane = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.control_plane)

    def test_the_round_counts_agree(self) -> None:
        self.assertEqual(self.control_plane.PBKDF2_ROUNDS, passwords.PBKDF2_ROUNDS)

    def test_each_verifies_the_others_hash(self) -> None:
        secret = "a shared administrator password"
        self.assertTrue(
            self.control_plane.verify_password(secret, passwords.password_hash(secret))
        )
        self.assertTrue(
            passwords.verify_password(secret, self.control_plane.password_hash(secret))
        )

    def test_a_wrong_password_fails_in_both_directions(self) -> None:
        encoded = passwords.password_hash("right")
        self.assertFalse(self.control_plane.verify_password("wrong", encoded))
        self.assertFalse(passwords.verify_password("wrong", encoded))


if __name__ == "__main__":
    unittest.main()
