"""RFC 8785 JSON Canonicalization, and the ``mapp-jcs-v1`` request digest.

P10 makes this a versioned cross-component security contract rather than a
serialization convenience: mapp-mcp, this broker and the configuration API each
compute the digest independently, and a token B is bound to the value they
agree on. A defect here does not produce a wrong answer in one place, it
produces a *consistent* wrong answer everywhere, which is why the scheme is
vendored and checked against the RFC's own published vectors instead of being
taken from a library.

Two rules are easy to state and easy to get wrong. Both are enforced during
parsing rather than after it, which matters because a completed parse destroys
the evidence -- but note they are not raw-byte scans: duplicates are caught by
json's object_pairs_hook as members are assembled, and the numeric domain is
checked over the parsed tree. The hook is the stricter of the two possible
duplicate checks, because it sees names after escape processing, so
``{"a":1,"\u0061":2}`` is caught where a regex over the bytes would not see a
collision at all.

*   Duplicate object member names are rejected. Python's json silently keeps
    the last, so a caller could hide a second ``scope`` behind the first and
    the digest would cover only what survived the collapse.
*   Numbers outside the I-JSON domain are rejected. NaN and Infinity have no
    JSON representation, and an integer beyond 2^53 cannot survive a float
    round trip, so both sides would canonicalize different values while
    believing they agreed.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import math
from typing import Any

#: The canonicalization version. A new algorithm requires a new version: an
#: approval or a token issued under one is never interpreted under another.
SCHEME = "mapp-jcs-v1"

#: Named for the ECMAScript constant, and still the value the docstrings cite,
#: but the rule enforced is the round-trip property below rather than this
#: magnitude -- see survives_round_trip.
MAX_SAFE_INTEGER = 2**53 - 1


def survives_round_trip(value: int) -> bool:
    """True when an integer survives a double and comes back unchanged.

    The rule has to be about the *value*, not about the Python type. Written
    as a magnitude on `int` it left the scheme not closed: 1e16 parsed to a
    float, passed the check, and canonicalized to the literal
    10000000000000000 -- which the very same loader then rejected. One trust
    boundary accepted what the next refused, which is precisely the
    disagreement this scheme exists to prevent.

    Large round-numbered integers such as 1e20 do survive a double exactly, so
    they are admitted; 2**53 + 1 does not, so it is refused.
    """
    try:
        return int(float(value)) == value
    except (OverflowError, ValueError):
        return False


class CanonicalizationError(ValueError):
    """The input cannot be canonicalized under this scheme."""


def _format_number(value: float | int) -> str:
    """Serialize a number the way ECMAScript's Number::toString does.

    RFC 8785 defers to ECMAScript here, and Python does not agree with it out
    of the box: repr(1.0) is '1.0' where ECMAScript gives '1', and Python
    writes exponents as '1e+30' where ECMAScript writes '1e+30' only above
    1e21. Getting this wrong changes the digest for perfectly ordinary
    numbers, so the cases are handled explicitly rather than trusted to repr.
    """
    if isinstance(value, bool):  # bool is an int subclass; JSON says otherwise
        raise CanonicalizationError("Booleans are not numbers.")
    if isinstance(value, int):
        if not survives_round_trip(value):
            raise CanonicalizationError(
                f"Integer {value} does not survive a double round trip."
            )
        return str(value)
    if math.isnan(value) or math.isinf(value):
        raise CanonicalizationError("NaN and Infinity have no JSON form.")
    if value == 0:
        # -0.0 and 0.0 are the same JSON number; ECMAScript prints "0".
        return "0"
    # ECMAScript switches between fixed and exponential notation at fixed
    # bounds: fixed for 1e-6 <= |x| < 1e21, exponential outside. Python's repr
    # switches at a different point -- repr(0.000001) is '1e-06' where
    # ECMAScript gives '0.000001' -- so the choice is made here rather than
    # inherited. repr is still the source of the digits, because it already
    # produces the shortest round-trip form both specifications require.
    shortest = repr(float(value))
    if 1e-6 <= abs(value) < 1e21:
        text = format(decimal.Decimal(shortest), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    mantissa, _, exponent = shortest.partition("e")
    if not exponent:
        # In the exponential range but written plainly: normalise it.
        text = format(decimal.Decimal(shortest), "e")
        mantissa, _, exponent = text.partition("e")
    if "." in mantissa:
        mantissa = mantissa.rstrip("0").rstrip(".")
    sign = "-" if exponent.startswith("-") else "+"
    digits = exponent.lstrip("+-").lstrip("0") or "0"
    return f"{mantissa}e{sign}{digits}"


#: Characters JSON requires be escaped, with the two-character forms RFC 8785
#: mandates where they exist. Everything else below 0x20 uses \\u.
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _format_string(value: str) -> str:
    _reject_lone_surrogates(value)
    out = ['"']
    for character in value:
        if character in _ESCAPES:
            out.append(_ESCAPES[character])
        elif character < "\x20":
            out.append(f"\\u{ord(character):04x}")
        else:
            # Everything else is emitted as itself, including non-ASCII: the
            # output is UTF-8 and RFC 8785 does not escape above 0x1f.
            out.append(character)
    out.append('"')
    return "".join(out)


def _serialize(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _format_string(value)
    if isinstance(value, (int, float)):
        return _format_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_serialize(item) for item in value) + "]"
    if isinstance(value, dict):
        # Sorted by UTF-16 code units, which is what RFC 8785 specifies and is
        # NOT the same as Python's default string ordering above the BMP.
        members = sorted(value.items(), key=lambda item: _utf16_key(item[0]))
        return "{" + ",".join(
            _format_string(name) + ":" + _serialize(member)
            for name, member in members
        ) + "}"
    raise CanonicalizationError(f"{type(value).__name__} has no JSON form.")


def _utf16_key(name: str) -> tuple[int, ...]:
    """Sort key over UTF-16 code units.

    Python compares strings by code point, so anything above the BMP sorts
    after every BMP character; UTF-16 puts surrogates at U+D800..U+DFFF and
    orders them below U+E000. The two disagree for astral characters, and RFC
    8785 requires the UTF-16 ordering.
    """
    return tuple(name.encode("utf-16-be"))




def loads(raw: bytes) -> Any:
    """Parse JSON, refusing what this scheme cannot canonicalize.

    Both checks run *during* parsing rather than after it, because a completed
    parse destroys the evidence: duplicate names collapse to the last value and
    a huge integer silently becomes a float, and neither is recoverable
    afterwards. Duplicates are caught by json's object_pairs_hook as members
    are assembled -- which sees names after escape processing, so
    ``{"a":1,"\u0061":2}`` is caught -- and the numeric domain is walked over
    the parsed tree by _check_domain.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise CanonicalizationError("Canonical input must be raw bytes.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalizationError("Canonical input must be UTF-8.") from exc


    def _no_duplicates(pairs):
        seen: set[str] = set()
        for name, value in pairs:
            if name in seen:
                raise CanonicalizationError(
                    f"Duplicate object member {name!r}."
                )
            seen.add(name)
        return dict(pairs)

    def _reject_constant(literal):
        raise CanonicalizationError(f"{literal} is outside the JSON number domain.")

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except CanonicalizationError:
        raise
    except ValueError as exc:
        raise CanonicalizationError(f"Malformed JSON: {exc}") from exc
    _check_domain(parsed)
    return parsed


def _reject_lone_surrogates(text: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError(
            "Unpaired surrogates cannot be canonicalized."
        ) from exc


def _check_domain(value: Any) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, str):
        # Checked here rather than on the input text: a \ud800 escape is
        # perfectly good ASCII in the raw bytes and only becomes an unpaired
        # surrogate once json decodes it. It has no UTF-8 encoding, so
        # canonicalize would raise UnicodeEncodeError -- outside this module's
        # error contract, and not something a peer could agree with either.
        _reject_lone_surrogates(value)
        return
    if isinstance(value, int) and not survives_round_trip(value):
        raise CanonicalizationError(
            f"Integer {value} does not survive a double round trip."
        )
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise CanonicalizationError("NaN and Infinity have no JSON form.")
    if isinstance(value, list):
        for item in value:
            _check_domain(item)
    elif isinstance(value, dict):
        for item in value.values():
            _check_domain(item)


def canonicalize(value: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 form of an already-parsed value."""
    return _serialize(value).encode("utf-8")


def digest(value: Any) -> str:
    """The ``mapp-jcs-v1`` digest: sha256 over the canonical form, hex.

    Prefixed with the scheme name so a digest computed under a future version
    can never be mistaken for one computed under this.
    """
    return SCHEME + ":" + hashlib.sha256(canonicalize(value)).hexdigest()
