"""Turning a token A into a credential for one configuration-API request.

This is the asserting side of the binding. It builds the canonical envelope for
the request it is about to make, digests it, and hands that digest to the broker
at exchange time. The configuration API then rebuilds the envelope from the
request that actually arrives and compares. The broker in between records what
it is given and recomputes nothing -- it never sees the body.

So a mistake here does not fail here. It fails at the configuration API, as a
403 naming neither the member that disagreed nor the component that got it
wrong, while this side reports a successful exchange. That asymmetry is why the
envelope is vendored rather than reimplemented, and why the copies are compared
by test.

The instance identifier is fetched rather than configured. It lives in the
control schema, which this component deliberately cannot read, and a value
copied into an environment variable would be a second source that drifts -- and
drift here is invisible until every call is refused. The configuration API
publishes it unauthenticated at `/api/public/identity`, which is the same
database and needs no credential to read.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import canonical
import execution_envelope

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
CONTEXT_PARAMETER = "mapp_operation_context"

#: How long an instance identifier is reused before it is fetched again. It
#: changes only when the platform is reinitialised, so this is about not making
#: a second HTTP call per tool invocation rather than about freshness.
IDENTITY_CACHE_SECONDS = 300


class ExchangeRefused(RuntimeError):
    """The broker declined to mint a credential, and said why.

    Distinct from being unable to ask. A refusal is about this request -- the
    grant does not carry the scope, the operation is not allowlisted, the budget
    is spent -- and is worth telling the caller. An unreachable broker is an
    operational fault and says nothing about the request.
    """

    def __init__(self, message: str, *, error: str = "") -> None:
        super().__init__(message)
        self.error = error


class ExchangeUnavailable(RuntimeError):
    """The broker could not be reached, or did not answer usefully."""


class ExchangeClient:
    def __init__(
        self,
        *,
        broker_endpoint: str,
        config_api_endpoint: str,
        config_api_resource: str,
        client_id: str,
        client_secret: str,
        timeout: float = 10.0,
        clock=time.monotonic,
    ) -> None:
        self.broker_endpoint = broker_endpoint.rstrip("/")
        self.config_api_endpoint = config_api_endpoint.rstrip("/")
        #: What the minted credential is for. Must differ from the MCP resource,
        #: or a token A and a token B would target the same audience and the
        #: separation the design rests on would be gone.
        self.config_api_resource = config_api_resource
        self.timeout = timeout
        self._clock = clock
        self._secret = client_secret
        self._authorization = "Basic " + base64.b64encode(
            f"{client_id}:{client_secret}".encode()
        ).decode()
        self._instance: tuple[float, str] | None = None

    # -- the platform's identity -----------------------------------------

    def instance_id(self) -> str:
        cached = self._instance
        now = self._clock()
        if cached is not None and cached[0] > now:
            return cached[1]
        request = urllib.request.Request(
            self.config_api_endpoint + "/api/public/identity",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ExchangeUnavailable(
                f"the configuration API's identity is unreadable: {exc}"
            ) from None
        instance = str(payload.get("instanceId") or "")
        if not instance:
            raise ExchangeUnavailable(
                "the configuration API published no instanceId"
            )
        self._instance = (now + IDENTITY_CACHE_SECONDS, instance)
        return instance

    # -- the exchange ----------------------------------------------------

    def request_digest(
        self,
        *,
        operation_id: str,
        method: str,
        path_template: str,
        path: str,
        query: str,
        body: Any,
    ) -> str:
        """The canonical digest of one request, as the platform will recompute it.

        Extracted from `exchange` because an approval is bound to this value
        and so is the credential that spends it. Two computations of the same
        digest is one chance for them to differ, and the difference would
        surface as a person approving a request whose receipt then buys
        nothing -- a refusal naming no cause, at the most expensive possible
        moment.

        Every member the configuration API will recompute is supplied in the
        same shapes: the raw path and query strings rather than anything
        reassembled, because those two are digested byte for byte.
        """
        return execution_envelope.digest(
            instance=self.instance_id(),
            method=method,
            operation_id=operation_id,
            path_template=path_template,
            path=path,
            query=query,
            body=body,
            # Null until the curated manifest and the approval flow exist.
            # Supplied explicitly because the builder requires them: the day
            # either exists, this is a call site that has to be updated rather
            # than a default that silently stays empty.
            resolved_defaults=None,
            confirmation_fields=None,
            revision_binding=None,
        )

    def exchange(
        self,
        *,
        subject_token: str,
        operation_id: str,
        method: str,
        path_template: str,
        path: str,
        query: str,
        body: Any,
        scope: str,
    ) -> str:
        """Mint a token B bound to exactly this request, or raise."""
        digest = self.request_digest(
            operation_id=operation_id,
            method=method,
            path_template=path_template,
            path=path,
            query=query,
            body=body,
        )
        context = json.dumps(
            {
                "version": canonical.SCHEME,
                "operationId": operation_id,
                "method": method,
                "pathTemplate": path_template,
                "requestDigest": digest,
            },
            separators=(",", ":"),
        )
        payload = self._post({
            "grant_type": GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "resource": self.config_api_resource,
            "scope": scope,
            CONTEXT_PARAMETER: context,
        })
        token = str(payload.get("access_token") or "")
        if not token:
            raise ExchangeUnavailable("the broker minted no credential")
        return token

    def _post(self, fields: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            self.broker_endpoint + "/internal/oauth/exchange",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": self._authorization,
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A refusal carries an OAuth error code and is about the request; a
            # 401 or 403 is about *this component's* credential and is not.
            try:
                detail = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError, OSError):
                detail = {}
            error = str(detail.get("error") or "")
            if exc.code in (401, 403):
                raise ExchangeUnavailable(
                    self._scrub(
                        f"the broker refused this component: HTTP {exc.code} {error}"
                    )
                ) from None
            raise ExchangeRefused(
                self._scrub(
                    str(detail.get("error_description") or error or "refused")
                ),
                error=error,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ExchangeUnavailable(
                self._scrub(f"the broker is unavailable: {exc}")
            ) from None

    def _scrub(self, text: str) -> str:
        return text.replace(self._secret, "[redacted]") if self._secret else text
