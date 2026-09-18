"""Adapter between stdlib ``http.server`` and authlib's OAuth 2 server.

authlib ships server integrations for Django and Flask only; ``starlette_client``
is a client integration. This module is the equivalent glue for the platform's
own stdlib HTTP layer, and it exists because P1 selects stdlib over adding a web
framework to a near-zero-dependency image.

Three requirements here are not obvious from authlib's documentation and were
found by reading the package:

* ``OAuth2Request.form`` raises ``NotImplementedError`` in the base class and is
  read by the authorization-code grant, PKCE, introspection and revocation. It
  must be supplied.
* RFC 9207 appends ``iss`` by reading and writing ``response.location``, so the
  response object cannot be a plain tuple.
* ``AuthorizationServer.send_signal`` raises in the base class, so it must be
  overridden or client authentication crashes.
"""

from __future__ import annotations

import urllib.parse
from collections import defaultdict

from authlib.oauth2.rfc6749.requests import OAuth2Payload
from authlib.oauth2.rfc6749.requests import OAuth2Request

#: A token, exchange or introspection request has no business being large, and a
#: small ceiling keeps the 5 MiB edge limit irrelevant to this service.
MAX_FORM_BYTES = 8 * 1024

FORM_MEDIA_TYPE = "application/x-www-form-urlencoded"
#: Applied to any response that does not carry its own policy.
DEFAULT_CSP = "default-src 'none'; frame-ancestors 'none'"


def _check_header(name: str, value: str) -> None:
    """Refuse any header that could split or abort the response.

    ``http.server.send_header`` validates nothing. A CR or LF in a value ends
    the header and lets the remainder be read as a second response; a character
    outside latin-1 raises UnicodeEncodeError *after* the status line is sent,
    so the client gets a bare connection close. Neither has a live path today,
    but this is the module's only response writer and M4 starts feeding it
    registered redirect URIs out of the database.
    """
    for text, label in ((name, "name"), (value, "value")):
        if any(character in text for character in ("\r", "\n", "\x00")):
            raise ValueError(f"Illegal character in header {label} {name!r}")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError(f"Non-latin-1 character in header {name!r}") from exc
ACCEPTED_CHARSETS = frozenset({"utf-8", "utf8"})


class FormError(ValueError):
    """A malformed request body. Carries the OAuth error code to report."""

    def __init__(self, description: str, error: str = "invalid_request") -> None:
        super().__init__(description)
        self.error = error
        self.description = description


class MappOAuth2Payload(OAuth2Payload):
    """Parameter access for one request.

    ``data`` returns the first occurrence of each name and ``datalist`` every
    occurrence, matching what authlib's validators expect. The base class
    derives ``client_id``, ``response_type``, ``grant_type``, ``redirect_uri``,
    ``scope`` and ``state`` from ``data``.
    """

    def __init__(self, data: dict[str, str], datalist: defaultdict[str, list[str]]) -> None:
        self._data = data
        self._datalist = datalist

    @property
    def data(self) -> dict[str, str]:
        return self._data

    @property
    def datalist(self) -> defaultdict[str, list[str]]:
        return self._datalist


class MappOAuth2Request(OAuth2Request):
    """An ``OAuth2Request`` backed by a stdlib handler.

    ``body`` is deliberately not passed to the base constructor: doing so emits a
    deprecation warning and routes ``form`` through the legacy path. ``form`` is
    overridden instead.
    """

    def __init__(
        self,
        method: str,
        uri: str,
        headers,
        form: dict[str, str],
        payload_data: dict[str, str],
        payload_datalist: defaultdict[str, list[str]],
    ) -> None:
        super().__init__(method, uri, headers=headers)
        self._form = form
        self.payload = MappOAuth2Payload(payload_data, payload_datalist)

    @property
    def form(self) -> dict[str, str]:
        return self._form

    # ``args`` is read nowhere in authlib.oauth2; only the Flask integration uses
    # it. The base class already raises NotImplementedError, which is the honest
    # answer for a request whose query string is merged into the payload.


class MappResponse:
    """A response authlib can annotate.

    RFC 9207's ``add_issuer_parameter`` reads ``response.location`` and assigns a
    rewritten value back to it, so ``location`` is a live view over the
    ``Location`` header rather than a snapshot.
    """

    def __init__(self, status: int, body: str, headers: list[tuple[str, str]] | None = None) -> None:
        self.status = status
        self.body = body
        self.headers: list[tuple[str, str]] = list(headers or [])

    @property
    def location(self) -> str | None:
        for name, value in self.headers:
            if name.lower() == "location":
                return value
        return None

    @location.setter
    def location(self, value: str) -> None:
        self.headers = [(n, v) for n, v in self.headers if n.lower() != "location"]
        self.headers.append(("Location", value))

    def write_to(self, handler) -> None:
        encoded = self.body.encode("utf-8")
        # Validated before a single byte is sent, so a bad header cannot leave
        # a half-written response on the wire.
        for name, value in self.headers:
            _check_header(name, str(value))
        handler.send_response(self.status)
        seen = set()
        for name, value in self.headers:
            # Content-Length is computed here, never taken from the caller: a
            # wrong one silently breaks framing for every later request on the
            # connection.
            if name.lower() == "content-length":
                continue
            handler.send_header(name, value)
            seen.add(name.lower())
        handler.send_header("Content-Length", str(len(encoded)))
        if "cache-control" not in seen:
            handler.send_header("Cache-Control", "no-store")
        # Guarded like cache-control: sending both a caller's value and ours
        # puts two values on the wire, and browsers join them into one invalid
        # value, which defeats the header entirely.
        if "x-content-type-options" not in seen:
            handler.send_header("X-Content-Type-Options", "nosniff")
        if "referrer-policy" not in seen:
            handler.send_header("Referrer-Policy", "no-referrer")
        if "content-security-policy" not in seen:
            # Applied here so it covers every response the component writes,
            # including the ones authlib builds on the error path. Caddy sets
            # no CSP on the four proxied auth paths -- its header directive
            # replaces the upstream's, which would strip the nonce policy off
            # the consent and login pages -- so a response missing it here has
            # none at the edge. The HTML pages set their own and keep it.
            handler.send_header("Content-Security-Policy", DEFAULT_CSP)
        if getattr(handler, "close_connection", False) and "connection" not in seen:
            # Announce what the server is about to do, rather than closing an
            # apparently keep-alive connection under the client.
            handler.send_header("Connection", "close")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(encoded)


def _single_header(headers, name: str) -> str | None:
    """Return a header that must appear at most once, or raise."""
    values = headers.get_all(name)
    if not values:
        return None
    if len(values) > 1:
        raise FormError(f"Duplicate {name} header.")
    return values[0]


def parse_form(handler, *, max_bytes: int = MAX_FORM_BYTES) -> tuple[dict[str, str], defaultdict[str, list[str]]]:
    """Read and strictly parse an ``application/x-www-form-urlencoded`` body.

    The configuration service has no form parser — ``_payload`` there is
    JSON-only — but RFC 6749 requires this encoding on the token endpoint, so
    this is new code. It is deliberately stricter than ``urllib``'s defaults:
    a body that is ambiguous is refused rather than guessed at.
    """
    # _single_header, not .get: .get returns only the FIRST occurrence and
    # treats an empty value as absent, so "Transfer-Encoding:\r\nTransfer-
    # Encoding: chunked" slipped past this check and was then framed by
    # Content-Length -- precisely the TE/CL disagreement the check exists to
    # prevent. Any Transfer-Encoding header at all is refused.
    if _single_header(handler.headers, "Transfer-Encoding") is not None:
        raise FormError("Chunked request bodies are not accepted.")

    raw_length = _single_header(handler.headers, "Content-Length")
    if raw_length is None:
        raise FormError("Missing Content-Length.")
    # int() accepts "+29", "029", "2_9" and surrounding whitespace, each of
    # which an RFC-compliant intermediary frames differently or rejects. A
    # parser that guesses where a proxy refuses is a framing-disagreement
    # primitive, so the token is validated before conversion.
    if not raw_length.isascii() or not raw_length.isdigit():
        raise FormError("Malformed Content-Length.")
    if len(raw_length) > 1 and raw_length.startswith("0"):
        # "029" is legal ABNF but no real client sends it, and intermediaries
        # differ on it. Refusing costs nothing and removes the disagreement.
        raise FormError("Malformed Content-Length.")
    length = int(raw_length, 10)
    if length > max_bytes:
        raise FormError("Request body is too large.")

    content_type = _single_header(handler.headers, "Content-Type")
    if content_type is None:
        raise FormError("Missing Content-Type.")
    media_type, _, parameters = content_type.partition(";")
    if media_type.strip().lower() != FORM_MEDIA_TYPE:
        raise FormError(f"Content-Type must be {FORM_MEDIA_TYPE}.")
    for parameter in (p for p in parameters.split(";") if p.strip()):
        key, _, value = parameter.partition("=")
        if key.strip().lower() != "charset":
            raise FormError("Unsupported Content-Type parameter.")
        if value.strip().strip('"').lower() not in ACCEPTED_CHARSETS:
            raise FormError("Unsupported charset.")

    raw = handler.rfile.read(length) if length else b""
    # Whatever happens after this point, the declared body is off the socket,
    # so the connection can safely carry another request.
    handler.body_consumed = True
    if len(raw) != length:
        raise FormError("Truncated request body.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormError("Request body is not valid UTF-8.") from exc

    if not text:
        return {}, defaultdict(list)
    try:
        # strict_parsing rejects a bare name with no '='. Since Python 3.10
        # parse_qsl splits on '&' only, so ';' carries no separator meaning.
        pairs = urllib.parse.parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except ValueError as exc:
        raise FormError("Malformed form body.") from exc

    datalist: defaultdict[str, list[str]] = defaultdict(list)
    for name, value in pairs:
        if not name:
            raise FormError("Empty parameter name.")
        datalist[name].append(value)
    data = {name: values[0] for name, values in datalist.items()}
    return data, datalist


def merge_query(
    data: dict[str, str],
    datalist: defaultdict[str, list[str]],
    query: str,
) -> tuple[dict[str, str], defaultdict[str, list[str]]]:
    """Merge query-string parameters into a parsed body.

    Flask's integration lets the body win when a name appears in both. That is
    the wrong default for an authorization server: a name supplied twice through
    two channels is ambiguous, so it is refused instead.

    In the current routing this refusal is a backstop rather than the live
    control: ``build_request`` refuses a query string outright on the body-
    reading path, and the authorization endpoint reads no body, so the two
    channels never both carry a name. It is kept, and unit-tested directly,
    because it is the rule that must hold if a future endpoint does read both.
    """
    if not query:
        return data, datalist
    try:
        pairs = urllib.parse.parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except ValueError as exc:
        raise FormError("Malformed query string.") from exc

    merged_list: defaultdict[str, list[str]] = defaultdict(list)
    for name, values in datalist.items():
        merged_list[name].extend(values)
    for name, value in pairs:
        if not name:
            raise FormError("Empty parameter name.")
        if name in datalist:
            raise FormError(f"Parameter {name!r} supplied in both query and body.")
        merged_list[name].append(value)
    merged = {name: values[0] for name, values in merged_list.items()}
    return merged, merged_list


def build_request(
    handler,
    *,
    issuer: str,
    read_body: bool = True,
    query_override: str | None = None,
) -> MappOAuth2Request:
    """Build an ``OAuth2Request`` from a stdlib handler.

    The absolute URI is composed from the *configured* issuer, never from ``Host``
    or ``X-Forwarded-Host``. Behind a Unix socket the request line carries no
    authority, and deriving one from a header would let a caller choose the
    origin against which the secure-transport check and every exact-issuer
    comparison are evaluated.
    """
    split = urllib.parse.urlsplit(handler.path)
    #: query_override lets the consent POST and the post-login redirect rebuild
    #: the request from the *stored*, already-validated query string, so a
    #: tampered form body cannot alter scope, redirect_uri, client_id or resource.
    query = split.query if query_override is None else query_override
    uri = issuer.rstrip("/") + split.path
    if query:
        uri = f"{uri}?{query}"

    if read_body and handler.command in {"POST", "PUT", "PATCH"}:
        # RFC 6749 s3.2: token-endpoint parameters are carried in the body. A
        # merged query would give one request two disagreeing views -- authlib
        # reads `code` and `code_verifier` from request.form (body only) but
        # `client_id` and `grant_type` from request.payload (merged) -- so a
        # grant could be steered from the URL, where proxies log it.
        if split.query and query_override is None:
            raise FormError("Query parameters are not accepted on this endpoint.")
        data, datalist = parse_form(handler)
    else:
        data, datalist = {}, defaultdict(list)
    form = dict(data)
    data, datalist = merge_query(data, datalist, query)

    return MappOAuth2Request(
        handler.command,
        uri,
        handler.headers,
        form,
        data,
        datalist,
    )
