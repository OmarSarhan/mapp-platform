"""The canonical execution request a token B is bound to.

A token B authorises **one operation on one request**, and this is where that
becomes true rather than recorded. The broker validates the shape of the digest
it is handed and stores it; it never sees the downstream request and cannot
recompute anything. So this module is the only place the binding is actually
checked, and a defect here turns a request-bound credential back into a plain
scoped bearer token.

The envelope members come from the scope document's canonical-execution-request
section: canonicalization version, target instance, uppercase method,
allowlisted operation ID, manifest path template, typed path parameters,
normalized absolute path, ordered query pairs and the exact downstream JSON
body.

**Not yet modelled, and deliberately absent rather than silently empty:**
resolved defaults, server-generated confirmation fields and revision/preflight
bindings. None of the five allowlisted operations has any, so there is nothing
to bind. Adding one is a change to the envelope, and a change to the envelope
is a new canonicalization version -- which is the whole reason the version is a
member.

The control-field boundary is non-circular: the Authorization header, the token
itself, CSRF values and trace headers are *not* members. A credential cannot be
an input to the digest that authorises it.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, unquote

import canonical

#: Unreserved characters (RFC 3986 s2.3). A percent-escape whose decoded byte
#: is one of these is non-canonical: it should have been written literally.
_UNRESERVED = re.compile(r"[A-Za-z0-9\-._~]")

#: A percent-escape in canonical form: uppercase hex, as RFC 3986 s6.2.2.1
#: requires. Accepting lowercase would give one path two spellings and so two
#: digests.
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")

_TEMPLATE_PARAMETER = re.compile(r"^\{([A-Za-z][A-Za-z0-9]*)\}$")


class EnvelopeError(ValueError):
    """The request cannot be canonicalized, so it cannot be authorised."""


def _check_segment(segment: str) -> str:
    """Validate one already-encoded path segment and return its decoded value.

    Rejects rather than normalizes. Normalizing twice is how two boundaries
    end up disagreeing about which request they authorised: the scope document
    asks for dot segments, encoded separators, backslashes and non-canonical
    encodings to be refused, not repaired.
    """
    if segment in (".", ".."):
        raise EnvelopeError("Path contains a dot segment.")
    for escape in _ESCAPE.finditer(segment):
        if escape.group(1) != escape.group(1).upper():
            raise EnvelopeError("Percent-escape must use uppercase hex digits.")
        decoded = bytes.fromhex(escape.group(1))
        if _UNRESERVED.fullmatch(decoded.decode("latin-1")):
            raise EnvelopeError(
                "Percent-escape encodes an unreserved character."
            )
    if "%" in _ESCAPE.sub("", segment):
        raise EnvelopeError("Path contains a malformed percent-escape.")
    try:
        value = unquote(segment, errors="strict")
    except UnicodeDecodeError as exc:
        raise EnvelopeError("Path segment is not valid UTF-8.") from exc
    if any(character in value for character in ("/", "\\", "\x00")):
        # An encoded separator would let one path spell two different
        # resources, and the template match would see the wrong one.
        raise EnvelopeError("Path segment decodes to a separator.")
    return value


def path_parameters(path_template: str, path: str) -> dict[str, str]:
    """Match a request path against its manifest template, or refuse.

    The template drives, and every segment must correspond: a request whose
    path does not match the template of the operation it claims to be is not
    that operation.
    """
    if not path.startswith("/") or "\\" in path:
        raise EnvelopeError("Path must be absolute and free of backslashes.")
    template_parts = path_template.split("/")
    actual_parts = path.split("/")
    if len(template_parts) != len(actual_parts):
        raise EnvelopeError("Path does not match the operation's template.")
    parameters: dict[str, str] = {}
    for template_part, actual_part in zip(template_parts, actual_parts):
        parameter = _TEMPLATE_PARAMETER.match(template_part)
        if parameter is None:
            if template_part != actual_part:
                raise EnvelopeError("Path does not match the operation's template.")
            continue
        if not actual_part:
            raise EnvelopeError("Path parameter is empty.")
        parameters[parameter.group(1)] = _check_segment(actual_part)
    return parameters


def query_pairs(query: str, *, repeatable: frozenset[str] = frozenset()) -> list:
    """Ordered [name, value] pairs, with duplicates refused by default.

    Order is preserved rather than sorted: the scope document specifies
    *ordered* query pairs, and sorting would make two materially different
    requests -- ones a handler reads positionally -- digest identically.

    A duplicate scalar is refused unless the operation's schema models a
    repeated value. None of the five allowlisted operations does, so in
    practice every duplicate is refused; the parameter exists so that adding
    one is a deliberate act rather than a silent widening.
    """
    if not query:
        return []
    if "\x00" in query:
        raise EnvelopeError("Query contains a NUL byte.")
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            errors="strict",
            separator="&",
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise EnvelopeError(f"Query is not well formed: {exc}") from exc
    seen: set[str] = set()
    for name, _ in pairs:
        if name in seen and name not in repeatable:
            raise EnvelopeError(f"Duplicate query parameter {name!r}.")
        seen.add(name)
    # Values stay exactly as decoded, as strings. Typing them here would mean
    # coercing on one side of a comparison whose other side is computed by a
    # different component -- the schema validates types separately, and it
    # does so after the binding has already been checked.
    return [[name, value] for name, value in pairs]


def build(
    *,
    instance: str,
    method: str,
    operation_id: str,
    path_template: str,
    path: str,
    query: str,
    body: Any,
    resolved_defaults: Any,
    confirmation_fields: Any,
    revision_binding: Any,
    repeatable_query: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Assemble the envelope. Every member is required; none defaults silently.

    ``body`` is the value parsed from the request's raw bytes by
    ``canonical.loads`` -- which rejects duplicate member names and numbers
    outside the I-JSON domain on the way in, at this trust boundary, before a
    normal parser can collapse them. ``None`` means the request had no body at
    all, which the digest distinguishes from a body of ``{}``.

    The last three members carry ``None`` everywhere today, and they are
    keyword-only *required* arguments rather than defaulting to it. P10 is
    explicit that "the envelope is the twelve-member definition normative in
    section 7, not a shorter restatement", because dropping resolved defaults
    or confirmation fields "would let two materially different requests share
    one digest". This module built nine and so had exactly that weakness.

    Their values come from the curated manifest and the approval flow, neither
    of which exists yet, and section 7 says the binding field then "carries its
    action-specific preflight value or an explicit null". An explicit null is
    not the same as an absent member: the member is in the digest now, so the
    day a manifest supplies a value it changes the digest rather than the
    envelope's shape. Requiring the argument is what makes that wiring a
    visible edit at every call site instead of a default nobody notices.

    The scheme version does not move for this. ``mapp-jcs-v1`` always denoted
    the twelve-member envelope; nine was the implementation falling short of
    it, not an earlier version of it.
    """
    if method != method.upper():
        raise EnvelopeError("Method must be upper case.")
    if not instance:
        raise EnvelopeError("The target instance is required.")
    return {
        "version": canonical.SCHEME,
        # Binds the digest to this deployment, so a token B minted against one
        # instance cannot be spent against another that shares an issuer.
        "instance": instance,
        "method": method,
        "operationId": operation_id,
        "pathTemplate": path_template,
        "pathParameters": path_parameters(path_template, path),
        "path": path,
        "query": query_pairs(query, repeatable=repeatable_query),
        "body": body,
        # The three P10 members that were missing. Present with an explicit
        # null rather than omitted -- see the docstring.
        "resolvedDefaults": resolved_defaults,
        "confirmationFields": confirmation_fields,
        "revisionBinding": revision_binding,
    }


def digest(**kwargs) -> str:
    """The digest a token B is bound to."""
    return canonical.digest(build(**kwargs))
