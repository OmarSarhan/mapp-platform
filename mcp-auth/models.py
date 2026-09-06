"""Record types implementing authlib's model mixins.

M1 keeps these as plain dataclasses over an in-memory store so the adapter can be
proven before the ``control`` schema exists. The same classes take a psycopg row
in M4 without changing the interface authlib sees.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field

from authlib.oauth2.rfc6749.models import AuthorizationCodeMixin
from authlib.oauth2.rfc6749.models import ClientMixin
from authlib.oauth2.rfc6749.models import TokenMixin


class PendingLimitReached(RuntimeError):
    """Too many live parked authorization requests.

    Defined here, not in a store, because both stores raise it and server.py
    catches it. Two identically named classes in two modules are not the same
    exception: the handler imported the stub's, so a capacity refusal from the
    SQL store escaped it and became a 500 instead of the 503 with Retry-After
    that P21 requires.
    """


@dataclass
class Client(ClientMixin):
    client_id: str
    name: str
    redirect_uris: tuple[str, ...]
    scopes: tuple[str, ...]
    #: 'none' for public MCP clients, which must use PKCE. 'client_secret_basic'
    #: for the confidential broker. P1 prefers private_key_jwt, but authlib's
    #: rfc7523 hard-codes the endpoint name it checks, which would stop the client
    #: model distinguishing the token endpoint from the exchange, so that is a
    #: Phase 1 item.
    token_endpoint_auth_method: str = "none"
    client_secret: str | None = None
    #: True when client_secret already holds the sha256 digest rather than the
    #: secret itself. The SQL store keeps only the digest -- a client secret at
    #: rest is a credential -- and check_client_secret hashes the supplied
    #: value before comparing, so both stores compare like with like.
    secret_is_hashed: bool = False
    #: Refresh is gated twice. authlib asks check_grant_type("refresh_token")
    #: and passes the answer to the token generator, so omitting the grant type
    #: here suppresses a refresh token -- but it is not currently the decisive
    #: condition: issuer.REFRESH_TOKENS_IMPLEMENTED is False, so adding the
    #: grant type alone changes nothing until the grant and its storage exist.
    grant_types: tuple[str, ...] = ("authorization_code",)
    response_types: tuple[str, ...] = ("code",)
    #: A disabled client authorises nothing. The SQL store already filtered on
    #: disabled_at; the model had no way to say it, so the in-memory store
    #: could not express a client state the real one enforces.
    disabled: bool = False

    def get_client_id(self) -> str:
        return self.client_id

    def get_default_redirect_uri(self) -> str:
        return self.redirect_uris[0]

    def get_allowed_scope(self, scope: str) -> str | None:
        """Narrow to what the client holds, or return None to refuse.

        Returning "" for a request whose scopes are all unheld is not a
        narrowing, it is a silent grant of nothing: authlib only raises
        InvalidScopeError on None (grants/authorization_code.py:390), so ""
        reached the consent screen as "none" and produced a token response with
        no ``scope`` member -- which RFC 6749 s5.1 tells the client to read as
        "identical to the one requested". The client would believe it had been
        granted exactly what it asked for.
        """
        if not scope:
            # No scope requested: refuse rather than guess. This grant has no
            # default scope, and inventing one here would be an escalation.
            return None
        allowed = set(self.scopes)
        # Preserve the requested order so the issued scope reads back predictably.
        kept = [s for s in scope.split() if s in allowed]
        return " ".join(kept) if kept else None

    def check_redirect_uri(self, redirect_uri: str) -> bool:
        # Exact match only. No prefix or wildcard matching, ever: a prefix test
        # would admit "<registered>.attacker.example/steal", which is a
        # working open redirect that delivers authorization codes.
        # test_a_suffix_extension_of_a_registered_uri_is_refused pins this.
        return redirect_uri in self.redirect_uris

    def check_client_secret(self, client_secret: str) -> bool:
        import hmac

        if not self.client_secret:
            return False
        supplied = client_secret or ""
        if self.secret_is_hashed:
            import hashlib

            supplied = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        # Encoded, for the same reason as the form tokens in server.py:
        # compare_digest raises TypeError on str operands holding non-ASCII,
        # and the supplied half is attacker-controlled.
        return hmac.compare_digest(
            self.client_secret.encode("utf-8"), supplied.encode("utf-8")
        )

    def check_endpoint_auth_method(self, method: str, endpoint: str) -> bool:
        # authlib passes the real endpoint name, which is what lets the token
        # endpoint and the exchange be authenticated differently later.
        return method == self.token_endpoint_auth_method

    def check_response_type(self, response_type: str) -> bool:
        return response_type in self.response_types

    def check_grant_type(self, grant_type: str) -> bool:
        return grant_type in self.grant_types


@dataclass
class AuthorizationCode(AuthorizationCodeMixin):
    code: str
    client_id: str
    redirect_uri: str
    scope: str
    subject: str
    code_challenge: str
    code_challenge_method: str = "S256"
    expires_at: float = field(default_factory=lambda: time.time() + 300)

    def get_redirect_uri(self) -> str:
        return self.redirect_uri

    def get_scope(self) -> str:
        return self.scope

    # Read by authlib's CodeChallenge extension.
    def get_nonce(self) -> None:
        return None

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


@dataclass
class Token(TokenMixin):
    token_hash: str
    client_id: str
    scope: str
    subject: str
    issued_at: int
    expires_in: int
    revoked: bool = False
    #: Which resource this token is for. Token A carries the MCP resource and
    #: token B the configuration API, and the exchange refuses a subject token
    #: that is not an A -- so a B cannot be exchanged again for another B.
    #: Deliberately empty rather than a plausible-looking placeholder. A
    #: default of "mcp" silently satisfied nothing and matched nothing: the
    #: exchange compares against the configured MCP resource, so a token that
    #: took the default was refused as "not active" with no hint that its
    #: audience had never been set. An empty value fails the same comparison
    #: but is obviously unset when read.
    audience: str = ""

    def check_client(self, client: ClientMixin) -> bool:
        return self.client_id == client.get_client_id()

    def get_scope(self) -> str:
        return self.scope

    def get_expires_in(self) -> int:
        return self.expires_in

    def is_expired(self) -> bool:
        return self.issued_at + self.expires_in < time.time()

    def is_revoked(self) -> bool:
        return self.revoked

    def get_client(self) -> str:
        return self.client_id

    def get_user(self) -> str:
        return self.subject


@dataclass
class PendingAuthorization:
    """A validated authorization request, parked while the operator signs in.

    ``query`` holds the *validated* query string. The consent POST rebuilds the
    OAuth request from this rather than from the submitted form, so the consent
    submission cannot alter scope, redirect_uri, client_id or resource — a
    tampered form body changes nothing that reaches the grant.

    The return target lives here rather than in the OAuth ``state`` parameter,
    which belongs to the client and must be echoed back untouched.
    """

    request_id: str
    query: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    csrf: str
    #: sha256 of the session cookie the consent page was rendered to. Until the
    #: operator is shown the page this is empty, and an empty value never
    #: matches, so a record that reached nobody cannot be submitted.
    #:
    #: Without this the form token is self-verifying: an unauthenticated caller
    #: mints its own pending record through GET /oauth/authorize, reads the csrf
    #: straight off the login page, and then submits that rid and csrf with a
    #: signed-in operator's cookie -- authorising a different client, with wider
    #: scopes, on a consent screen the operator never saw.
    session_hash: str = ""
    #: Throttle bucket of the caller that parked this record, so one source
    #: cannot fill the table that every other user shares.
    source: str = ""
    expires_at: float = field(default_factory=lambda: time.time() + 600)

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


@dataclass
class Grant:
    """One consent, as a thing that can be revoked.

    Revocation is grant-shaped, not token-shaped: an operator withdrawing
    consent means every credential derived from it stops working at once,
    including a token B already in flight. Revoking tokens one at a time could
    never achieve that, because the broker can mint another the moment before.
    """

    grant_id: str
    client_id: str
    subject: str
    scopes: tuple[str, ...]
    revoked_at: float | None = None

    def is_revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass
class Session:
    """An administrator browser session for the authorization component only.

    Deliberately separate from config-ui's ``mapp_session``: that cookie is
    host-only with SameSite=Strict, so it cannot ride the top-level navigation an
    agent's browser handoff produces. This one is scoped to /oauth and uses Lax,
    which sends on top-level GET navigations while still blocking cross-site POST.
    """

    subject: str
    auth_time: float
    expires_at: float

    def is_expired(self) -> bool:
        return time.time() > self.expires_at
