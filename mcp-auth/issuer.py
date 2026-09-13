"""The platform-hosted authorization server.

P1 makes the platform the canonical issuer and rejects off-the-shelf servers as
runtime broker dependencies. This wires authlib's ``AuthorizationServer`` to the
stdlib adapter and the store.

The RFC 8693 exchange is deliberately absent: authlib ships only a docstring for
it, and M6 implements it by hand on the internal listener, never here — putting
it on ``/oauth/token`` would place the most security-critical surface in the
design on an edge-routed path.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time

from authlib.oauth2.rfc6749 import AuthorizationServer
from authlib.oauth2.rfc6749.errors import InvalidGrantError
from authlib.oauth2.rfc6749.errors import InvalidRequestError
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant
from authlib.oauth2.rfc6749.grants import RefreshTokenGrant
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oauth2.rfc9207 import IssuerParameter

from authlib_adapter import MappResponse
from authlib_adapter import build_request
from models import AuthorizationCode
from models import RotatedRefresh
from models import Token

#: P6: token A lives 15 minutes.
ACCESS_TOKEN_EXPIRES_IN = 900

TOKEN_A_PREFIX = "mapp_a_"
REFRESH_TOKEN_PREFIX = "mapp_r_"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class MappAuthorizationCodeGrant(AuthorizationCodeGrant):
    #: Public MCP clients authenticate with PKCE and no secret; the confidential
    #: broker uses client_secret_basic. The client model decides which applies.
    TOKEN_ENDPOINT_AUTH_METHODS = ["none", "client_secret_basic"]

    def validate_authorization_request(self):
        """Validate, then enforce PKCE presence for *every* client.

        authlib's ``CodeChallenge(required=True)`` does NOT make ``code_challenge``
        required here: ``validate_code_challenge`` opens with
        ``if not challenge and not method: return``, and the ``required`` flag is
        only consulted at the token endpoint, where it additionally requires
        ``auth_method == "none"``. So without this check a client omitting PKCE
        entirely reaches the consent screen and is only refused after the operator
        has authorised it.

        The check deliberately does not exempt confidential clients. RFC 9700
        s2.1.1 requires PKCE for every client using the authorization-code grant,
        and an earlier version of this method tested
        ``token_endpoint_auth_method == "none"``, which let a secret-holding
        client complete the whole flow with no PKCE at all.
        """
        redirect_uri = super().validate_authorization_request()
        payload = self.request.payload.data
        if not payload.get("code_challenge"):
            raise InvalidRequestError(
                "Missing 'code_challenge'", redirect_uri=redirect_uri
            )
        if "code_challenge_method" in payload and not payload["code_challenge_method"]:
            # Present but empty. authlib's membership test skips a falsy
            # method, so this would otherwise sail through here and be stored,
            # and only the normalisation in save_authorization_code would stop
            # it becoming a RuntimeError at the token endpoint. Refuse the
            # ambiguity instead of quietly reading it as S256.
            raise InvalidRequestError(
                "Empty 'code_challenge_method'", redirect_uri=redirect_uri
            )
        return redirect_uri

    def save_authorization_code(self, code, request):
        payload = request.payload.data
        self.server.store.save_authorization_code(
            AuthorizationCode(
                code=code,
                client_id=request.client.get_client_id(),
                redirect_uri=request.payload.redirect_uri,
                # request.scope, not payload.scope: authlib resolves the default
                # through client.get_allowed_scope when the client omits it.
                scope=request.scope,
                subject=request.user,
                code_challenge=payload.get("code_challenge", ""),
                # `or`, not a dict default: the adapter parses with
                # keep_blank_values, so "code_challenge_method=" stores "".
                # authlib's validate_code_challenge skips its membership test
                # for a falsy method, and the token endpoint then raises a bare
                # RuntimeError rather than an OAuth2Error. Before _route
                # gained its catch-all that produced no response at all; today
                # it would be a 500. Normalised in three places now -- here, at
                # the authorization endpoint, and on read in
                # S256OnlyCodeChallenge -- because only the last of those
                # protects a record written by some other path.
                code_challenge_method=payload.get("code_challenge_method") or "S256",
            )
        )
        return code

    def query_authorization_code(self, code, client):
        """Consume the code here, because this is the only point that races.

        authlib queries the code, mints and saves a token, and only then calls
        ``delete_authorization_code`` (grants/authorization_code.py:229, :289,
        :290). Consuming at the delete step therefore leaves a window in which
        N concurrent requests all query the same live code and all receive
        tokens. The window is narrow under CPython's default 5 ms switch
        interval and often will not reproduce there; with
        ``sys.setswitchinterval(1e-6)`` the pre-fix ordering yields 2-3 tokens
        per 6 simultaneous exchanges, which is why the regression test sets it.
        Consuming *as* the query closes the window outright: exactly one caller
        is handed the record and every other gets None, which authlib turns
        into invalid_grant.

        A code consumed by a request that later fails validation is not
        restored. That is deliberate: RFC 6749 s4.1.2 treats a code presented
        more than once as compromised, so burning it on the first presentation
        is the safe direction.
        """
        record = self.server.store.consume_authorization_code_for(
            code, client.get_client_id()
        )
        return record

    def delete_authorization_code(self, authorization_code):
        # Already consumed by query_authorization_code, which is the only step
        # that can decide the race. Kept because authlib calls it unconditionally.
        return None

    def authenticate_user(self, authorization_code):
        # P3: the actor is the grant, not a directory user. The code's subject
        # is already the grant id -- consent writes it in server.authorize_post
        # -- so this returns it unchanged and the token inherits it.
        return authorization_code.subject


class MappRefreshTokenGrant(RefreshTokenGrant):
    """P6's rotating refresh families, over the store's atomic rotation.

    Public MCP clients present no secret, so the refresh token is a bearer
    credential and rotation is what limits what a stolen one is worth: every
    use spends the presented token and issues its successor, and a second
    presentation of a spent token revokes the whole family and the grant.
    """

    #: Same as the code grant: a public MCP client authenticates with nothing
    #: and the confidential broker with a secret. The base class allows only
    #: client_secret_basic, which would refuse every agent.
    TOKEN_ENDPOINT_AUTH_METHODS = ["none", "client_secret_basic"]

    #: False even though a successor *is* returned. The flag only decides
    #: whether authlib asks the token generator for a fresh value, and the
    #: successor is not generated here -- it is the one the rotation already
    #: wrote and which a row already matches. ``issue_token`` attaches that
    #: one, so asking the generator would mint a value only to discard it.
    INCLUDE_NEW_REFRESH_TOKEN = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._successor = ""

    def authenticate_refresh_token(self, refresh_token):
        """Spend the presented token here, because this is the only point that races.

        The same reasoning as ``query_authorization_code``: authlib validates,
        mints, saves and only then calls ``revoke_old_credential``, so
        consuming at that last step leaves a window in which N simultaneous
        presentations of one refresh token all validate and all receive tokens.
        The conditional UPDATE inside ``rotate_refresh_token`` is the authority,
        and exactly one caller can win it.

        A presentation that is spent here and then refused downstream -- a
        wrong client, a widened scope -- does not get the token back. The
        client's next attempt is therefore a replay, which costs it the grant.
        That is the specification's own open item O7, and it is the safe
        direction: a refresh token presented twice is indistinguishable from a
        stolen one.
        """
        successor = REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(32)
        result = self.server.store.rotate_refresh_token(refresh_token, successor)
        if result["outcome"] != "rotated":
            # One error for every refusal. Which of replayed, revoked, expired
            # or unknown applies is exactly what a holder of a stolen token
            # would like to learn, and RFC 6749 s5.2 has one code for all of
            # them. The store has already acted on a replay by the time this
            # is reached.
            raise InvalidGrantError()
        self._successor = successor
        # What save_token reads to know this refresh token already has a
        # family. The grant type would answer the same question today, but it
        # answers it by naming the *other* grant: a third grant issuing a
        # refresh token would silently get no family and hand the client an
        # inert credential. This says the thing itself -- the rotation wrote
        # the row, so nothing else should.
        self.request.rotated_family = result["family_id"]
        return RotatedRefresh(
            family_id=result["family_id"],
            grant_id=result["grant_id"],
            client_id=result["client_id"],
            scope=result["scope"],
        )

    def authenticate_user(self, refresh_token):
        # P3 again: the actor is the grant. The family records which grant it
        # belongs to, so the refreshed token A inherits the same subject the
        # original consent produced rather than anything the client supplied.
        return refresh_token.grant_id

    def revoke_old_credential(self, refresh_token):
        # Already spent by authenticate_refresh_token, which is the only step
        # that can decide the race. Kept because authlib calls it unconditionally.
        return None

    def issue_token(self, user, refresh_token):
        token = super().issue_token(user, refresh_token)
        token["refresh_token"] = self._successor
        return token


class S256OnlyCodeChallenge(CodeChallenge):
    """Refuse the ``plain`` challenge method.

    authlib's ``CodeChallenge.SUPPORTED_CODE_CHALLENGE_METHOD`` is
    ``["plain", "S256"]``, so without this the metadata document advertised
    ``code_challenge_methods_supported: ["S256"]`` while the endpoint happily
    accepted ``plain`` -- where the challenge *is* the verifier, so anything
    that sees the authorization URL (browser history, a proxy log, a Referer,
    the operator's own screen) recovers it and PKCE protects nothing. A client
    could not detect the downgrade, because the metadata claimed S256 only.
    """

    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]

    def get_authorization_code_challenge_method(self, authorization_code):
        """Normalise a stored method on the way out, as well as on the way in.

        authlib reads this straight off the record and then does
        ``CODE_CHALLENGE_METHODS.get(method)``, which returns None for an empty
        string and raises a bare RuntimeError -- not an OAuth2Error, so it
        escapes the endpoint's error handling entirely. Normalising only at
        save time left any record that already held "" able to trigger it.
        """
        method = super().get_authorization_code_challenge_method(authorization_code)
        return method or "S256"


class MappIssuerParameter(IssuerParameter):
    """RFC 9207. Appends ``iss`` to authorization responses.

    Registered on the server, not the grant: the server's ``create_authorization_response``
    is ``@hooked``, so ``after_create_authorization_response`` fires with the
    response for both the success and the error path. That is what makes P1's
    "iss in authorization success and error responses" hold, and it is why the
    response object exposes a writable ``location`` rather than being a tuple.
    """

    def __init__(self, issuer: str) -> None:
        self._issuer = issuer

    def get_issuer(self) -> str:
        return self._issuer


class MappAuthorizationServer(AuthorizationServer):
    def __init__(
        self,
        store,
        *,
        issuer: str,
        resource: str,
        config_api_resource: str | None = None,
        scopes_supported=None,
        advertised_scopes=None,
        admin_password_hash: str = "",
        secure_cookies: bool = False,
    ) -> None:
        # authlib uses scopes_supported as the *acceptance* allowlist:
        # validate_requested_scope refuses anything outside it. P2 needs a
        # different thing -- a narrower *advertised* set, so a greedy client
        # cannot auto-request every permission from discovery, while an
        # explicit step-up request for a privileged scope still succeeds.
        # Narrowing the single attribute would have refused step-up outright,
        # so the two roles are kept apart.
        supported = list(scopes_supported or ())
        super().__init__(scopes_supported=supported)
        self.advertised_scopes = (
            supported if advertised_scopes is None else list(advertised_scopes)
        )
        self.store = store
        self.issuer = issuer
        #: What token A is for.
        self.resource = resource
        #: What token B is for. Deliberately a separate value: if the two were
        #: ever equal, an A and a B would target the same resource and the
        #: audience separation the design rests on would be gone -- a token A
        #: could be replayed at the configuration API. assert_audiences_differ
        #: refuses that configuration outright rather than trusting deployment.
        self.config_api_resource = config_api_resource or (
            resource.rsplit("/", 1)[0] + "/api"
        )
        if self.config_api_resource == self.resource:
            raise ValueError(
                "The MCP resource and the configuration-API resource must differ;"
                " equal values collapse the audience separation between token A"
                " and token B."
            )
        #: Empty in deployment: the property below reads control.admin_credential,
        #: which is what ./bin/mapp init writes. A non-empty value here is a
        #: test override. passwords.py is byte-compatible with config-ui's
        #: hasher precisely so this is one credential and not two.
        self._admin_password_hash = admin_password_hash
        self.secure_cookies = secure_cookies
        self.register_token_generator("default", self._generate_token)
        self.register_grant(MappAuthorizationCodeGrant, [S256OnlyCodeChallenge(required=True)])
        self.register_grant(MappRefreshTokenGrant)
        self._issuer_parameter = MappIssuerParameter(issuer)
        self.register_extension(self._register_issuer_parameter)

    @property
    def admin_password_hash(self) -> str:
        """The operator credential, from the store unless one was supplied.

        Tests pass an explicit hash; the deployed component passes none and
        the store answers, so the credential ./bin/mapp init writes into
        control.admin_credential is the one the consent screen checks. Read
        per attempt rather than captured at start-up, so a password change
        needs no restart and a component that started before the credential
        existed does not cache its absence.
        """
        if self._admin_password_hash:
            return self._admin_password_hash
        reader = getattr(self.store, "admin_password_hash", None)
        return reader() if reader is not None else ""

    def _register_issuer_parameter(self, server):
        self._issuer_parameter(server)
        return self._issuer_parameter

    # -- abstract surface ------------------------------------------------

    def query_client(self, client_id):
        return self.store.query_client(client_id)

    def save_token(self, token, request):
        raw = token["access_token"]
        self.store.save_token(
            raw,
            Token(
                token_hash=_hash(raw),
                client_id=request.client.get_client_id(),
                scope=token.get("scope", ""),
                subject=request.user or "",
                issued_at=int(time.time()),
                expires_in=token["expires_in"],
                # The resource this token is for. Omitted, it took the model's
                # "mcp" placeholder, while the exchange compares against the
                # configured MCP resource -- so a genuinely issued token A
                # could never be exchanged. Every exchange test passed because
                # each seeded its own subject token with the audience it
                # wanted, and none drove the authorization-code flow.
                audience=self.resource,
            ),
        )
        refresh = token.get("refresh_token")
        # A rotation has already written its successor's row, and opening a
        # second family for it would give one consent two independent
        # families -- so revoking the replayed one would leave the other
        # alive. Every other path that mints a refresh token needs one.
        if refresh and getattr(request, "rotated_family", None) is None:
            self.store.start_refresh_family(
                refresh,
                family_id="fam-" + secrets.token_urlsafe(18),
                grant_id=request.user or "",
                client_id=request.client.get_client_id(),
                scope=token.get("scope", ""),
            )

    def create_oauth2_request(self, request):
        # The handler has already built it; authlib re-enters with the object.
        return request

    def create_json_request(self, request):  # pragma: no cover
        # No JSON endpoint is registered, and none should be: every endpoint this
        # server exposes is form-encoded per RFC 6749.
        raise NotImplementedError("No JSON endpoint is registered.")

    def handle_response(self, status, body, headers):
        payload = body if isinstance(body, str) else json.dumps(body)
        return MappResponse(status, payload, list(headers or []))

    def send_signal(self, name, *args, **kwargs):
        # Base class raises; overriding is mandatory or client authentication
        # fails. Phase 1 routes these into the audit log.
        return None

    # -- token generation ------------------------------------------------

    def _generate_token(
        self,
        grant_type,
        client,
        user=None,
        scope=None,
        expires_in=None,
        include_refresh_token=True,
    ):
        token = {
            "token_type": "Bearer",
            "access_token": TOKEN_A_PREFIX + secrets.token_urlsafe(32),
            "expires_in": expires_in or ACCESS_TOKEN_EXPIRES_IN,
        }
        if scope:
            token["scope"] = scope
        # authlib sets include_refresh_token from check_grant_type('refresh_token'),
        # so honouring it here makes the client's registered grant types the actual
        # control over whether a refresh token exists. Ignoring the flag would leave
        # this generator as the real control while the client record only looked
        # like it was -- and a test asserting "no refresh_token" would then pass for
        # the wrong reason.
        #
        # The value minted here is inert until save_token opens a family for it;
        # the two are one act, and nothing else in this class mints one. The
        # refresh grant deliberately does not come through here: its successor
        # is written by the rotation, not generated.
        if include_refresh_token:
            token["refresh_token"] = REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(32)
        return token

    # -- convenience -----------------------------------------------------

    def request_from(self, handler, **kwargs):
        return build_request(handler, issuer=self.issuer, **kwargs)

    def annotate_issuer(self, response) -> None:
        """Append ``iss`` to a redirect built outside create_authorization_response.

        The RFC 9207 extension rides the server's ``@hooked``
        ``after_create_authorization_response``, so an error raised earlier — during
        consent validation, before that method is entered — produces a redirect the
        hook never sees. P1 requires ``iss`` on error responses, so it is applied
        here too, through the same extension instance.
        """
        for extension in self._extensions:
            if hasattr(extension, "add_issuer_parameter"):
                extension.add_issuer_parameter(self, response)

    # -- discovery -------------------------------------------------------

    def metadata_document(self) -> dict:
        """RFC 8414 metadata.

        Served at ``/.well-known/oauth-authorization-server`` on the MCP origin.
        Without it a client has nothing to discover and the exact-issuer and
        RFC 9207 ``iss`` comparisons have no value to compare against.

        ``registration_endpoint`` is absent deliberately: Dynamic Client
        Registration is refused, and advertising it would also mean importing
        authlib's rfc7591, which pulls a native crypto stack into the image.
        """
        base = self.issuer.rstrip("/")
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
            # The advertised set, not the acceptance allowlist (P2).
            "scopes_supported": list(self.advertised_scopes or ()),
            "authorization_response_iss_parameter_supported": True,
        }
