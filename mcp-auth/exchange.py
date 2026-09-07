"""The restricted RFC 8693 token exchange that issues token B.

authlib ships no implementation of RFC 8693 -- in 1.8.0 the package is a single
docstring -- so this is project code end to end. That is not a gap being filled
generically: RFC 8693 is a framework, and almost all of its freedom is
*removed* here. One grant type, one subject token type, one requested token
type, one resource value, no actor token, no refresh token, and a private
context extension the RFC does not define.

The design rule that matters most, because getting it wrong looks like
success:

    Refuse when requested is not a subset of permitted.
    Never compute granted = requested & permitted.

Silent intersection turns a widening attempt into a working token. A client
asking for `apply` against an `inspect` grant would receive a usable `inspect`
token, never learn its request had been altered, and no test comparing "did I
get a token" could tell that apart from correct narrowing.

This endpoint is registered on the internal control listener only. Putting the
most security-critical surface in the design on an edge-routed path would make
the Caddy allowlist the only thing standing between the internet and it.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets

import canonical
import operations

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

TOKEN_B_PREFIX = "mapp_b_"

#: P6: at most sixty seconds. Token B exists to cross one trust boundary for
#: one call; anything longer is a credential lying around.
TOKEN_B_MAX_LIFETIME = 60

#: The only canonicalization version this broker will accept context under. A
#: token issued under one scheme is never interpreted under another, so an
#: unknown version is refused rather than assumed compatible.
CONTEXT_VERSION = canonical.SCHEME

#: The private extension carrying the operation binding. Namespaced so it
#: cannot collide with a future registered RFC 8693 parameter.
CONTEXT_PARAMETER = "mapp_operation_context"

#: How many token B may be minted for one grant inside the window below.
#: Provisional (P11): generous for interactive agent work -- one per second
#: sustained -- and low enough that a runaway loop is bounded rather than
#: unbounded. A legitimate burst that hits it is delayed, not refused
#: permanently: the window slides.
EXCHANGE_BUDGET_PER_GRANT = 60

#: Seconds the budget is counted over.
EXCHANGE_BUDGET_WINDOW = 60

#: scheme name, then exactly one sha256 in lower-case hex.
DIGEST_PATTERN = re.compile(re.escape(canonical.SCHEME) + r":[0-9a-f]{64}")


class ExchangeError(Exception):
    """A refusal, carrying the RFC 6749 error code to report."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def _single(datalist, name: str) -> str | None:
    """Exactly one value, or a refusal.

    RFC 8693 says nothing about repeated parameters, and a parser that keeps
    the first while a peer keeps the last is how two components authorise
    different things from identical bytes. Duplicates are refused outright.
    """
    values = datalist.get(name) or []
    if len(values) > 1:
        raise ExchangeError(
            "invalid_request", f"Duplicate {name!r} parameter."
        )
    return values[0] if values else None


def exchange(
    *,
    datalist,
    broker_client,
    store,
    resource: str,
    mcp_resource: str,
    now: dt.datetime | None = None,
) -> dict:
    """Validate an exchange request and issue token B, or refuse.

    `broker_client` is the already-authenticated confidential client. Client
    authentication happens before this is called, because an unauthenticated
    caller must never reach the point of naming a subject token.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    # -- the fixed profile -------------------------------------------------
    if _single(datalist, "grant_type") != GRANT_TYPE:
        raise ExchangeError("unsupported_grant_type", "Unsupported grant type.")

    if _single(datalist, "actor_token") or _single(datalist, "actor_token_type"):
        # Delegation is not part of this profile. Accepting an actor token
        # would let the broker act as a third party the grant never named.
        raise ExchangeError("invalid_request", "Actor tokens are not accepted.")

    subject_token_type = _single(datalist, "subject_token_type")
    if subject_token_type != ACCESS_TOKEN_TYPE:
        raise ExchangeError("invalid_request", "Unsupported subject token type.")

    requested_token_type = _single(datalist, "requested_token_type")
    if requested_token_type not in (None, ACCESS_TOKEN_TYPE):
        raise ExchangeError("invalid_request", "Unsupported requested token type.")

    # Exactly one resource, matching exactly. An audience alias, a trailing
    # slash or a second value are all refused rather than normalised: the
    # resource is what stops a token minted for the configuration API being
    # replayed at the MCP endpoint.
    resources = datalist.get("resource") or []
    if len(resources) != 1:
        raise ExchangeError(
            "invalid_target", "Exactly one resource must be requested."
        )
    if resources[0] != resource:
        raise ExchangeError("invalid_target", "Unknown resource.")
    if datalist.get("audience"):
        raise ExchangeError(
            "invalid_target", "Audience aliases are not accepted; use resource."
        )

    subject_token = _single(datalist, "subject_token")
    if not subject_token:
        raise ExchangeError("invalid_request", "Missing 'subject_token'.")

    # -- the subject: token A ---------------------------------------------
    record = store.query_token(subject_token)
    if record is None or record.is_expired() or record.is_revoked():
        raise ExchangeError("invalid_grant", "The subject token is not active.")
    # Compared positively against the configured MCP resource rather than
    # denied by magic string. `not in (None, "mcp")` spread a literal across
    # three files with three different ideas of what an audience is, and an
    # absent audience passed it.
    if record.audience != mcp_resource:
        raise ExchangeError("invalid_grant", "The subject token is not active.")
    # The grant, not just the token. The scope document requires revocation to
    # prevent new exchanges immediately, and a token A stays live for its full
    # fifteen minutes after consent is withdrawn -- so checking only the token
    # would keep minting token Bs from a grant that no longer exists.
    grant = store.query_grant(record.subject) if record.subject else None
    if grant is None or grant.is_revoked():
        raise ExchangeError("invalid_grant", "The subject token is not active.")
    # And the client that holds the grant must still be permitted to act.
    if store.query_client(record.client_id) is None:
        raise ExchangeError("invalid_grant", "The subject token is not active.")

    # -- the operation binding --------------------------------------------
    raw_context = _single(datalist, CONTEXT_PARAMETER)
    if not raw_context:
        raise ExchangeError(
            "invalid_request", f"Missing {CONTEXT_PARAMETER!r}."
        )
    try:
        context = canonical.loads(raw_context.encode("utf-8"))
    except canonical.CanonicalizationError as exc:
        raise ExchangeError("invalid_request", f"Malformed context: {exc}") from exc
    if not isinstance(context, dict):
        raise ExchangeError("invalid_request", "Context must be a JSON object.")

    version = context.get("version")
    if version != CONTEXT_VERSION:
        # An unknown version is refused, never interpreted under this one.
        raise ExchangeError("invalid_request", "Unknown context version.")

    operation_id = context.get("operationId")
    try:
        operation = operations.lookup(operation_id)
    except operations.UnknownOperation:
        raise ExchangeError(
            "invalid_request", "The operation is not allowlisted for exchange."
        ) from None

    request_digest = context.get("requestDigest")
    # Fully matched, not merely prefixed: a prefix test admitted
    # "mapp-jcs-v1:" with nothing after it, so a token could be bound to the
    # empty digest -- a binding that matches whatever it is later compared to.
    if not isinstance(request_digest, str) or not DIGEST_PATTERN.fullmatch(
        request_digest
    ):
        raise ExchangeError(
            "invalid_request", "The request digest is missing or malformed."
        )

    method = context.get("method")
    path_template = context.get("pathTemplate")
    if method != operation.method or path_template != operation.path_template:
        # The caller does not get to describe the operation differently from
        # the allowlist. NOTE the digest's *shape* is checked above but its
        # value is never recomputed against the real downstream request: that is
        # the configuration API's job and that half does not exist yet (M7).
        # Until it lands, the binding is recorded but not enforced downstream.
        raise ExchangeError(
            "invalid_request", "The context does not match the allowlisted operation."
        )

    # -- scope: subset, never intersection ---------------------------------
    requested_raw = _single(datalist, "scope")
    requested = tuple(requested_raw.split()) if requested_raw else ()
    if not requested:
        raise ExchangeError("invalid_scope", "A scope must be requested.")
    if len(set(requested)) != len(requested):
        raise ExchangeError("invalid_scope", "Duplicate scope values.")

    # Both the token and the grant bound it. The grant is the stronger of the
    # two: it is what the operator actually saw and approved on the consent
    # screen, and a token is only ever derived from it. Checking the token
    # alone let a grant that consented to nothing mint an `apply` token B,
    # because nothing downstream reconciled the two.
    permitted = set((record.get_scope() or "").split()) & set(grant.scopes)
    missing = [scope for scope in requested if scope not in permitted]
    if missing:
        # Refused, not narrowed. Narrowing here would hand back a working
        # token for a request the caller never made.
        raise ExchangeError(
            "invalid_scope",
            "Requested scopes exceed the subject token or its grant: "
            + " ".join(sorted(missing)),
        )

    # No separate "does the grant hold the operation's scopes" branch: the
    # subset test above already ran over `requested`, and `requested` must
    # equal the operation's scopes, so that check was unreachable. It used to
    # exist, and the test named for it passed because a different check fired.
    # Exactly the operation's scopes: no more, no fewer. Issuing whatever was
    # asked for would hand a mutation token unrelated authority for its
    # lifetime, which is the opposite of binding it to one operation.
    if set(requested) != set(operation.required_scopes):
        raise ExchangeError(
            "invalid_scope",
            "The request must ask for exactly the scopes this operation requires: "
            + " ".join(sorted(operation.required_scopes)),
        )

    # -- abuse budget ------------------------------------------------------
    # One consent must not become an unbounded number of effects. Every other
    # bound here is per credential -- a token B is single-use and lives sixty
    # seconds -- but nothing stopped a grant minting them in a loop, and each
    # one authorises a consequential operation. The cap is per grant rather
    # than per client or per source, because the grant is what the operator
    # approved and is the unit revocation acts on.
    #
    # Provisional, as P11 requires: replaced by measured values in Phase 6.
    # Counted from the tokens already issued rather than a counter column, so
    # nothing new has to be written, expired or reconciled.
    minted = store.exchanged_token_count(record.subject, EXCHANGE_BUDGET_WINDOW)
    if minted >= EXCHANGE_BUDGET_PER_GRANT:
        raise ExchangeError(
            "slow_down",
            "This grant has reached its exchange budget. Retry shortly.",
        )

    # -- issue -------------------------------------------------------------
    raw = TOKEN_B_PREFIX + secrets.token_urlsafe(32)
    lifetime = TOKEN_B_MAX_LIFETIME
    store.save_exchanged_token(
        raw,
        client_id=broker_client.get_client_id(),
        # Derived from validated subject-token state, never from exchange
        # input: a caller that could name its own originating client could
        # borrow another client's authority.
        actor_client_id=record.client_id,
        subject=record.subject,
        scope=" ".join(requested),
        audience=resource,
        issued_at=now,
        expires_at=now + dt.timedelta(seconds=lifetime),
        operation_id=operation.operation_id,
        request_digest=request_digest,
        single_use=operation.mutating,
    )
    return {
        "access_token": raw,
        "issued_token_type": ACCESS_TOKEN_TYPE,
        "token_type": "Bearer",
        "expires_in": lifetime,
        "scope": " ".join(requested),
        # No refresh_token, by profile. A sixty-second credential that can be
        # renewed is not a sixty-second credential.
    }
