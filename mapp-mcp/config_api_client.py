"""Spending a token B against the configuration API.

One credential, one request. The token was minted for exactly the request built
here, so this client takes the same path and query strings that were digested
rather than reassembling them -- those two members are bound byte for byte, and
a reassembled query that differs by a character is refused with no indication
that reassembly was the problem.

The credential goes in the Authorization header and nowhere else. It is
single-use for a mutating operation and short-lived for any, so there is nothing
to cache and nothing to retry with: a retry needs a new exchange, because the
old credential is either spent or bound to a request that has already happened.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ConfigApiRefused(RuntimeError):
    """The configuration API declined the request, and said why.

    Carries the platform's own error code, because those are the useful ones:
    `auth.binding_refused` means the digest did not match what the API
    recomputed, and `auth.operation_unresolved` means no template matched the
    path -- which is also what a path-encoding disagreement looks like.

    Also carries the platform's field-level `errors`, where it sent any. For a
    validation refusal those *are* the answer -- "Expression test failed" says
    only that something is wrong, while the entry beneath it names the field
    and what PostgreSQL said about it. Dropping them made sql_test, whose whole
    purpose is to report why an expression does not work, report nothing.
    """

    def __init__(self, message: str, *, status: int, code: str = "",
                 errors: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.errors = errors if isinstance(errors, list) else []


class ConfigApiUnavailable(RuntimeError):
    """The configuration API could not be reached."""


class ConfigApiClient:
    def __init__(self, *, endpoint: str, timeout: float = 15.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def get(self, *, path: str, query: str, token: str) -> Any:
        """Exactly the path and query the credential was bound to."""
        return self._send(path=path, query=query, token=token, method="GET",
                          body=None)

    def post(self, *, path: str, query: str, token: str, body: Any) -> Any:
        """The same, with the body the credential was bound to.

        The digest covers the body as a *parsed value*, canonicalised the same
        way on both sides, so what matters is that this serialises to something
        that parses back to what was digested. It is serialised once here and
        not rebuilt anywhere, for the reason the path and query are built once:
        two constructions of the same request is one chance for them to
        disagree, and the disagreement surfaces as a refusal naming no cause.
        """
        return self._send(path=path, query=query, token=token, method="POST",
                          body=body)

    def _send(self, *, path: str, query: str, token: str, method: str,
              body: Any) -> Any:
        url = self.endpoint + path + (f"?{query}" if query else "")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError, OSError):
                detail = {}
            raise ConfigApiRefused(
                str(detail.get("error") or f"HTTP {exc.code}"),
                status=exc.code,
                code=str(detail.get("code") or ""),
                errors=detail.get("errors"),
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ConfigApiUnavailable(
                f"the configuration API is unavailable: {exc}"
            ) from None


def layer_statistics_query(
    *, field: str, locale: str | None, bins: int | None
) -> str:
    """The statistics query, in one fixed order.

    Same contract as the other builders: digested at exchange time, sent at
    request time, built once so the two cannot disagree.

    `threshold` and `break` are accepted by the platform and deliberately not
    offered here. They are arrays, and an array in a digested query string is a
    repeated parameter whose order and encoding both have to match what the far
    side reconstructs -- a byte of disagreement surfaces as a refusal naming no
    cause. They can be added when something needs them, with a test that pins
    the encoding.
    """
    pairs = [("field", field)]
    if locale is not None:
        pairs.append(("locale", locale))
    if bins is not None:
        pairs.append(("bins", str(bins)))
    return "&".join(
        f"{urllib.parse.quote(name, safe='')}={urllib.parse.quote(value, safe='')}"
        for name, value in pairs
    )


def semantic_search_query(*, query: str, limit: int | None) -> str:
    """The semantic search query, in one fixed order.

    Same contract as the other builders: digested at exchange time, sent at
    request time, built once so the two cannot disagree.
    """
    pairs = [("q", query)]
    if limit is not None:
        pairs.append(("limit", str(limit)))
    return "&".join(
        f"{urllib.parse.quote(name, safe='')}={urllib.parse.quote(value, safe='')}"
        for name, value in pairs
    )


def limit_query(*, limit: int | None) -> str:
    """A bare `limit`, for the listings that paginate.

    Trivial, and separate rather than inlined for the reason every other builder
    is: the query is digested at exchange time and sent at request time, so it
    is built once in a place a test can reach.
    """
    if limit is None:
        return ""
    return f"limit={urllib.parse.quote(str(limit), safe='')}"


def layers_query(*, locale: str | None) -> str:
    """The query string for the layer listing, in the one fixed order.

    Same rule as `layer_values_query` and for the same reason: the query is
    digested at exchange time and sent at request time, so it is built once and
    used twice rather than constructed twice and hoped to match. An absent
    locale is an absent parameter, not an empty one -- `locale=` is a different
    request and the configuration API refuses it.
    """
    if locale is None:
        return ""
    return f"locale={urllib.parse.quote(locale, safe='')}"


def layer_values_query(*, field: str, locale: str | None, limit: int | None) -> str:
    """The query string, in a fixed order, built once.

    Order is part of the digest and MCP tool arguments arrive as an unordered
    JSON object, so it has to be decided somewhere deterministic. Here, and only
    here: the same string is digested at exchange time and sent at request time,
    so the two cannot disagree about what was authorised.

    ``quote`` rather than ``quote_plus``: a space must travel as ``%20``. The
    configuration API decodes the query with ``parse_qsl``, which reads ``+`` as
    a space, so encoding a literal plus as ``+`` would have it arrive as a space
    and change the request the digest covers.
    """
    pairs = [("field", field)]
    if locale is not None:
        pairs.append(("locale", locale))
    if limit is not None:
        pairs.append(("limit", str(limit)))
    return "&".join(
        f"{urllib.parse.quote(name, safe='')}={urllib.parse.quote(value, safe='')}"
        for name, value in pairs
    )
