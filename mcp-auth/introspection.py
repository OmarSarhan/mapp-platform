"""Introspection, grant revocation and token-B redemption, for the control
listener.

Three endpoints that are really one idea: what makes a credential live, how to
make it stop being live, and how it is spent.

**Active means more than "the token exists and has not expired."** The scope
document requires a token be reported active only when the mapped local grant,
client and resource are *all* active. So introspection resolves the token to
its grant and refuses on any of: an unknown or revoked grant, a disabled
client, a resource that does not match, expiry, revocation or consumption.
Anything it cannot resolve is inactive -- a token with no grant fails closed,
which matters because rows predating the grant migration have none.

**Revocation is grant-shaped, not token-shaped.** Revoking tokens one at a
time could never mean what an operator withdrawing consent intends: the broker
can mint another the instant before. Revoking the grant invalidates every
credential derived from it at once, including a token B already issued and not
yet spent -- which is the difference between the design's claim and an
aspiration, because a token B lives only sixty seconds and expiring is not
revoking.

Both are authenticated and bound to the internal listener. RFC 7662 warns that
an unauthenticated introspection endpoint is an oracle for guessing tokens,
and this one would answer for the platform's own credentials. Being
authenticated is not on its own enough: every parameter is parsed before any
lookup, and the lookups are unconditional, because a request error or a
response time that varies with the outcome reveals exactly what the flat
inactive response is there to hide.
"""

from __future__ import annotations

import hmac
import re

import canonical

#: A fully formed ``mapp-jcs-v1`` digest. Matched entire rather than by prefix:
#: a prefix test accepts the scheme name with nothing after it, which is a
#: binding that matches whatever it is compared against.
DIGEST_PATTERN = re.compile(re.escape(canonical.SCHEME) + r":[0-9a-f]{64}")

#: Reported for every inactive token, with nothing else. RFC 7662 s2.2 is
#: explicit that an inactive response must not describe the token: saying why
#: it is inactive tells a caller holding a stolen token whether it was ever
#: valid, whose it was, and when it expired.
INACTIVE: dict = {"active": False}


class IntrospectionError(Exception):
    """A malformed request, as distinct from an inactive token."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def _single(datalist, name: str) -> str | None:
    values = datalist.get(name) or []
    if len(values) > 1:
        raise IntrospectionError(
            "invalid_request", f"Duplicate {name!r} parameter."
        )
    return values[0] if values else None


def introspect(*, datalist, store) -> dict:
    """Answer RFC 7662 for one token.

    Returns the inactive response rather than raising for anything about the
    token itself, including a token that does not exist. Raising would let a
    caller distinguish "unknown" from "revoked", which is the oracle the
    endpoint must not be.
    """
    raw = _single(datalist, "token")
    if not raw:
        raise IntrospectionError("invalid_request", "Missing 'token'.")

    # token_type_hint is accepted and deliberately ignored: RFC 7662 s2.1 makes
    # it a hint, and honouring it would let a caller probe one token space at a
    # time. Every token lives in one table here anyway.
    _single(datalist, "token_type_hint")

    # Every request parameter is parsed before anything is looked up. `resource`
    # used to be read after the inactive short-circuits below, which turned a
    # malformed request into a liveness oracle: a duplicated `resource` returned
    # 400 for a live token and 200 {"active": false} for every other token,
    # because only a live one reached the line that raised.
    resource = _single(datalist, "resource")

    # The three lookups are unconditional, so an unknown token, a revoked grant
    # and a live token all cost the same three round trips. Short-circuiting
    # made the outcome readable from the response time alone -- measured at
    # roughly 1:2:3 against a real database, which is not a subtle signal.
    # Equal *round trips*, not constant time; a single joined statement would
    # be better still and is the obvious follow-up.
    record = store.query_token(raw)
    grant = store.query_grant(record.subject if record is not None else "")
    # The grant's client, not the token's. For a token B, record.client_id is
    # the broker that performed the exchange, so checking that one applied the
    # "active client" rule to token A and to the exchange but never to token B:
    # disabling a client left its already-issued token B active for the whole
    # sixty seconds. query_client already excludes disabled clients, so this
    # covers "unknown" and "disabled" without asking twice.
    client = store.query_client(grant.client_id if grant is not None else "")

    if record is None or record.is_expired() or record.is_revoked():
        return dict(INACTIVE)
    if grant is None or grant.is_revoked():
        # Fails closed for a token with no resolvable grant, which includes any
        # row written before grants existed.
        return dict(INACTIVE)
    if client is None:
        return dict(INACTIVE)
    if resource is not None and resource != record.audience:
        # Exact equality. A prefix test would introspect a token for
        # ".../mcp" as active for the resource ".../m".
        return dict(INACTIVE)

    return {
        "active": True,
        "scope": record.get_scope(),
        "client_id": record.client_id,
        "token_type": "Bearer",
        "exp": record.issued_at + record.expires_in,
        "iat": record.issued_at,
        # The grant, not a person: P3 makes the grant the actor, and a shared
        # administrator identity would say nothing useful here anyway.
        "sub": grant.grant_id,
        "aud": record.audience,
    }


def revoke(*, datalist, store) -> dict:
    """Revoke the grant behind a presented token.

    RFC 7009 describes revoking the presented token. This revokes its whole
    grant, which is a deliberate widening: the token is a leaf of the consent,
    and leaving the consent live would let the next exchange mint a
    replacement immediately. The response is the RFC's -- an empty 200 whether
    or not anything was revoked -- so a caller cannot use it to discover
    whether a token was real.
    """
    raw = _single(datalist, "token")
    if not raw:
        raise IntrospectionError("invalid_request", "Missing 'token'.")
    _single(datalist, "token_type_hint")

    record = store.query_token(raw)
    if record is not None and record.subject:
        store.revoke_grant(record.subject, "token-revocation")
    # Nothing is reported either way, per RFC 7009 s2.2.
    return {}


def redeem(*, datalist, store) -> dict:
    """Spend or verify a token B against the request actually being executed.

    This is the half of the operation binding that lives outside the broker.
    The broker validates the *shape* of the digest it is handed at exchange
    time and stores it; it never sees the downstream request and so cannot
    recompute anything. The configuration API rebuilds the canonical envelope
    from the request in front of it, digests that, and presents the result
    here. A token minted for one proposal therefore cannot be spent on
    another, which is the entire point of binding it.

    For a mutating operation the token is single-use and
    ``consume_exchanged_token`` is the authority: one conditional statement,
    with the operation and digest as predicates, so two presentations of the
    same token cannot both proceed. The binding is read first only to learn
    whether the token is single-use; the consuming statement re-checks
    everything atomically, so the read cannot become a stale decision.

    A read operation's token is not single-use -- there is nothing to spend --
    so the binding comparison is the whole check, and it happens here rather
    than in a statement.

    Both paths answer with the same three members. Neither returns the scope or
    the subject: the caller already has both from introspection, and inventing
    empty values for the branch that cannot produce them would make one
    endpoint answer in two shapes.
    """
    raw = _single(datalist, "token")
    operation_id = _single(datalist, "operation_id")
    request_digest = _single(datalist, "request_digest")
    if not raw:
        raise IntrospectionError("invalid_request", "Missing 'token'.")
    if not operation_id:
        raise IntrospectionError("invalid_request", "Missing 'operation_id'.")
    if not request_digest or not DIGEST_PATTERN.fullmatch(request_digest):
        raise IntrospectionError(
            "invalid_request", "The request digest is missing or malformed."
        )

    binding = store.exchanged_binding(raw)
    if binding is None:
        # Covers an unknown token, a token that is not a B, an expired one and
        # one whose grant is gone or revoked -- the store resolves the grant.
        raise IntrospectionError("invalid_grant", "The token is not redeemable.")

    if binding["single_use"]:
        if store.consume_exchanged_token(raw, operation_id, request_digest) is None:
            raise IntrospectionError(
                "invalid_grant", "The token is not redeemable for this request."
            )
        return {"redeemed": True, "single_use": True, "operation_id": operation_id}

    # Compared as digests, so compare_digest rather than ==. Both halves are
    # hex from a fixed alphabet, so this is hygiene rather than a live timing
    # defence -- but the operation id is caller-supplied and the habit is
    # cheaper to keep than to reason about each time.
    if not hmac.compare_digest(
        str(binding["operation_id"]), operation_id
    ) or not hmac.compare_digest(str(binding["request_digest"]), request_digest):
        raise IntrospectionError(
            "invalid_grant", "The token is not redeemable for this request."
        )
    return {"redeemed": True, "single_use": False, "operation_id": operation_id}
