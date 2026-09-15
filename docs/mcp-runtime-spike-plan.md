# mapp-mcp spike — plan and findings

Written for whoever implements the spike, which may be a different session or a
different model. It records what a scoping pass established so that work does
not have to be repeated, and what it found that would otherwise be discovered
by failure.

**Status: the authorization half is built and deployed; the tool surface is
not.** `mapp-mcp` runs as a service, Caddy routes `/mcp` and both RFC 9728
well-known paths to it, and a real token A obtained through the authorization
flow reaches it. What it answers with is a 501 placeholder, because no tool
exists yet.

Done: the protocol-era guard, the RFC 9728 document and challenge, token-A
authentication by introspection, the confidential client the runtime
authenticates with, the image, the compose service, the edge routes, and the
vendored canonicalizer and envelope checked against the other two copies and
the pinned golden vector.

Not done: JSON-RPC dispatch, `tools/list`, `tools/call`, and therefore any
tool. That needs the decision below.

**The open decision.** The specification says to use the official MCP SDK. It
resolves to 28 packages including `cryptography`, `pydantic-core` and `rpds-py`,
against a runtime that currently has three pure-Python ones. The counter-argument
is not only weight: the era guard exists *because* the SDK serves both handshake
eras and exposes no version allowlist, so the SDK is already being worked around
at the point where it matters most, and the read-only surface this spike needs is
small. Weigh it with the running service in front of you -- that was the reason
for deploying before deciding.

## What the spike is for

Phases 0 and 1 built an OAuth 2.1 authorization server with no resource behind
it. Every path through it is exercised by tests written against one reading of
the specification, and by a flow script driven by hand. No MCP client has ever
connected.

The spike exists to change that — to put a real client in front of the real
component and see what disagrees. It is deliberately wider than minimal,
because its value is in what it catches rather than in what it ships.

## Decisions taken

**Read-only tools only.** Phase 3 is scoped to "read-only tools/resources", and
its gate says consequential-effect flags stay disabled before Phase 7. The
spike does not cross that.

**`layers.values` is the wide part.** It is the only `GET` in the operation
allowlist (`mcp-auth/operations.py`), so it drives the whole exchange → canonical
digest → redemption path without any consequential effect. Its scopes, `derive`
and `semantic:inspect`, are not in the advertised discovery set, so it also
exercises P2's step-up. A spike that only reads through token A would leave the
riskiest half of the design untested.

**The issuer is an opaque string.** `mcp-control` is `internal: true`, so
`http://mcp.localhost` is neither resolvable nor routable from mapp-mcp. It is
compared, never fetched. All live work goes to `http://mcp-auth:8080`, the
control listener, as config-ui already does. An implementation that fetches
issuer metadata to validate a token hangs until its timeout.

**An operator registers mapp-mcp's client row**, with
`./bin/mapp mcp-runtime-register`. Without that row every
`/internal/oauth/exchange` call fails in `_authenticate_broker` and the whole
token-B path is dead with an opaque error.

It cannot register itself: it holds no database credential by design. The first
version had mcp-auth write the row from its own environment, which starts
without an operator step but costs three things that matter once more than one
person runs the platform -- the plaintext sits in a second service's
configuration, rotation means restarting the authorization server, and the act
has no author. The command mints the secret rather than accepting one, so it
never passes through a shell history, and stores only the digest: the component
that *verifies* the secret never holds it.

The trade is that mapp-mcp does not work until somebody registers it. That is
acceptable where the administrator credential was not, because this gates one
optional service rather than every way of signing in.

**mapp-mcp joins `mcp-control`**, as P12 requires. The specification asks for a
renewed transport threat model for any additional peer on that network; this
version records a delta rather than claiming a full renewal, which is
proportionate for a spike and at least visible.

## The finding the spike existed to produce

**Claude Code 2.1.272 cannot connect to a specification-conformant mapp-mcp,
and the specification already says what to do about it.**

Driven for real: the CLI installed in an ephemeral container on the host
network, this session's credential mounted, the server registered with a fixed
callback port and both loopback spellings. It reached the era guard and was
refused by it, with the guard's own error --
`400 Missing MCP-Protocol-Version {"reason": "protocol-version-missing"}`.

Its first request, captured verbatim from an echo server:

```json
{"method":"initialize",
 "params":{"protocolVersion":"2025-11-25",
           "clientInfo":{"name":"claude-code","version":"2.1.272"}},
 "jsonrpc":"2.0","id":0}
```

Three refusals, each correct:

- no `MCP-Protocol-Version` header at all -- the revision is negotiated in the
  `initialize` body, which is the older handshake;
- the method is `initialize`, which the guard refuses as a method that does not
  exist in the modern era;
- the revision it offers is **2025-11-25**, and this server accepts 2026-07-28
  and nothing else.

So the platform targets a revision *newer* than the shipped client speaks. This
is not a defect in either: it is the gap the spike was built to measure, and it
was invisible until a real client was put in front of the real server.

P2 decided the response in advance (:2046): support "the ecosystems that pass
and document the remainder as unsupported -- do not add a legacy transport path
to accommodate it". Taken literally, Claude Code is unsupported until it speaks
2026-07-28, and that is a decision to reaffirm knowingly rather than discover
later. The alternative -- admitting 2025-11-25 -- is the legacy path P2 refuses,
and the era guard exists precisely because the SDK would otherwise serve it.

Two smaller things the same run established:

- **`*.localhost` is resolved to loopback by the client regardless of
  `/etc/hosts`.** A container that mapped `mcp.localhost` to a bridge address
  could not reach the server at all; on the host network, where the name really
  is loopback, it connected immediately. Anyone testing from a container needs
  the host network or a hostname that is not `*.localhost`.
- The registration side is right: a **public** client with `--client-id`, no
  secret, and a fixed `callbackPort` is accepted in `.mcp.json`, and the server
  is discovered, approved and health-checked without complaint. Everything up to
  the protocol revision works.

## Traps that cost hours if unknown

Each was established by reading the code, not inferred.

**Contract 1.7 does not exist.** The specification says mapp-mcp "pins a minimum
of 1.7 and exposes no tool against an older contract"; the live platform is 1.6.
Exposing any tool is therefore a knowing deviation to record. The conformance
fixture needs no manifest, so the transport, the era guard, RFC 9728 and the
authentication middleware are all exercisable today.

**`tools/list` is empty on first connect.** It is filtered by the active grant's
scopes, and discovery advertises only `mcp:connect` and `inspect`. A grant
holding just those lists zero exchangeable tools. This looks like a broken
server and is not.

**The era guard has four separable obligations**, and the specification
preemptively rejects the convenient proof: "enabling `stateless_http` is not
accepted as evidence that the legacy era is disabled". It must accept only an
exact `MCP-Protocol-Version: 2026-07-28`; distinguish a *missing or malformed*
header (validation error) from a *declared older* revision (unsupported-version
error) rather than fabricating a version error for both; reject `initialize` as
an unsupported modern method, which forces it to inspect the decoded body before
dispatch; and never mint or echo `Mcp-Session-Id`. It must not intercept the
unauthenticated RFC 9728 GET.

**Three identifiers must agree exactly**: the RFC 9728 `resource`, the 401's
`resource_metadata` URL, and the single entry in `authorization_servers`. Derive
all three from one configured origin rather than composing them separately.

**The edge ceiling is 512KiB, not 5MiB.** `docker/caddy/Caddyfile` sets
`max_size 512KiB` on the MCP origin, and `test_caddy_contract.py` asserts that
literal *and* asserts `{$MCP_MAX_REQUEST_BODY` appears nowhere. The
specification's template says otherwise; the deployed file wins, and pasting the
template breaks three tests and silently strips the consent page's CSP.

**Path-shaped canonicalization refusals do not say so.** `_resolve_operation`
catches `EnvelopeError` and continues, so a dot segment, a lower-case escape or
an encoded separator becomes "no template matched" and answers 403
`auth.operation_unresolved`, "This route does not accept an exchanged
credential". Only query and body faults reach the 400
`auth.request_not_canonical` branch. A path-encoding mismatch reads as a routing
bug.

**`+` is a space in a query and a literal plus in a path.** The query decoder is
`parse_qsl`, which is unquote_*plus*; the path decoder is not. Pinned by
`QueryPlusDecodingTests`. An implementation written from "percent-decoded exactly
once" disagrees on every query value containing one, and the symptom is a blanket
403 with no diagnostic.

**The body is bound by value; path and query are bound by bytes.** config-ui
digests the *parsed* body, so mapp-mcp may re-serialize JSON freely between
exchange and dispatch. `path` and `query` are digested from the raw wire strings
and are byte-exact. Query pair *order* is load-bearing, and MCP tool arguments
arrive as an unordered JSON object — so the ordering must be decided somewhere
deterministic before the digest is computed.

**One digest is written down**, and the third implementation is now checked
against it. `GoldenVectorTests` in `config-ui/tests/test_execution_envelope.py`
pins a fixed envelope to a literal value, and
`mapp-mcp/tests/test_envelope_agreement.py` recomputes it and compares all three
canonicalizer copies on what they emit and on what they refuse. Every other test
is differential, and two implementations wrong in the same way agree perfectly.

## Files that must change together

Adding a second internal service follows what mcp-auth did on this branch. A
missed entry is a red suite, not a silent gap.

- `compose.yaml` and every overlay — service, `mcp-control` network, `expose`,
  `read_only`, tmpfs, `cap_drop: ALL`, healthcheck. `ci.yml` asserts config-ui's
  `depends_on` equals exactly `{"semantic-service"}`.
- `docker/caddy/Caddyfile` — route `/mcp` and both `/.well-known/oauth-protected-resource`
  forms; everything else on that origin 404s. CSP stays out of the site header
  block: Caddy's `header` directive replaces rather than merges.
- `scripts/tests/test_caddy_contract.py` — the public matcher is pinned with
  `assertEqual` on exactly four paths today.
- `scripts/tests/test_compose_isolation.py` — asserts
  `{"config-ui", "mcp-auth"} == control_members`. Update deliberately.
- `scripts/tests/test_supply_chain.py` and `.github/workflows/supply-chain.yml` —
  `EXPECTED_BASES` and the pinned-package matrix. The MCP SDK resolves to **28
  packages** on `python:3.12-alpine`, installing from musllinux wheels with no
  build toolchain, but including `cryptography`, `pydantic_core`, `rpds-py` and
  `cffi`. That is a real change of posture for an image that currently has
  almost none, and it needs a deliberate decision rather than a pip install.
- `scripts/tests/test_dockerfile_contract.py`, `bin/mapp` (`runtime_services`,
  the test target), and `ci.yml`'s `grep -q "skipped="` guard for any new suite.

## Order to build in

1. The era guard and RFC 9728. Neither needs the manifest, the broker row, or
   any tool. Prove the guard on the wire rather than through SDK configuration.
2. The application factory. The conformance fixture is specified as "created by
   the same application factory … only its handler registry and test
   authorization policy differ", so building the factory first is what makes
   that fixture possible later. It is also A4's containment rule — the transport
   stays behind one module so an SDK bump is a contained change.
3. Authentication middleware: token A by authenticated RFC 7662 introspection
   per call, against `http://mcp-auth:8080`.
4. A read-only tool needing only token A.
5. `layers.values` through the full exchange, with mapp-mcp's own envelope
   implementation checked against the golden vector.

## Open items this raised

- The envelope's `resolvedDefaults`, `confirmationFields` and `revisionBinding`
  carry explicit nulls until the curated manifest and approval flow exist. The
  call site in `config-ui/app.py` is where values must arrive.
- Contract 1.7 is unbuilt, so any exposed tool deviates from the phase gate.
- The `mcp-control` fourth-peer threat-model delta is recorded, not renewed.
