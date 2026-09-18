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

**Claude Code 2.1.272 could not connect to a specification-conformant mapp-mcp.
The spike existed to find exactly this, and it changed the specification: P2a
now admits the handshake revisions the target ecosystems speak
alongside 2026-07-28 — see "Every ecosystem sat behind the target" below.**

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

### What was decided, and why P2 was amended

P2 decided the response in advance (:2046): support "the ecosystems that pass
and document the remainder as unsupported -- do not add a legacy transport path
to accommodate it". That fallback rested on an assumption this spike disproved.

**Admitting 2025-11-25 adds no legacy transport path.** The SDK already serves
the handshake era; `era_guard` was written to refuse it. Measured directly, the
SDK completes a full legacy session under `stateless_http` -- initialize,
`notifications/initialized`, `tools/list`, `tools/call` -- and mints no
`Mcp-Session-Id` at any point. So admitting the era removes a refusal. It builds
no transport, adds no dependency, and opens no second path through the runtime.

**Under the original fallback the release gate was unreachable.** P2 gates the
production route on one pinned client from each of three ecosystems passing the
full authenticated journey. The shipped Claude client cannot negotiate
2026-07-28 at all, so "open the route for the ecosystems that pass" would have
opened it for none. A gate no client can pass is not a gate.

**Admitting the era made the guard stricter, not laxer.** This is the part worth
carrying forward. The SDK cannot be left to police the handshake: measured
against the installed version, an `initialize` is negotiated to *whatever the
client offers* -- `2024-11-05` and `2025-03-26` are accepted verbatim -- and an
unrecognisable offer such as `"zzz"` is silently counter-offered `2025-11-25`
rather than refused. Letting `initialize` through would therefore have admitted
four revisions and a fallback, not one. The guard now decodes the offered
revision out of the body and refuses everything that is not the single admitted
handshake revision, so the served set is what `era_guard.SERVED_VERSIONS` says
and nothing else.

Recorded as **P2a** in the scope document, superseding the fallback rather than
quietly relaxing it.

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

### Re-measured after P2a

The same container, the same client, the same server rebuilt:

| Before | After |
|---|---|
| `✘ Failed to connect — HTTP 400: Missing MCP-Protocol-Version` | `! Needs authentication` |

"Needs authentication" is the correct next state, not a remaining fault: the
client now completes the handshake, receives `401` with the RFC 9728 pointer
`WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp"`,
and wants an operator to sign in. Completing that needs a browser and a consent
decision, which is the design working rather than an obstacle to remove.

Every era decision, verified on the wire through the edge:

| Request | Answer |
|---|---|
| `initialize` offering 2025-11-25, no version header | `401` — admitted, then authentication |
| `initialize` offering 2025-06-18 / 2025-03-26 / 2024-11-05 | `400 unsupported-protocol-version` |
| `initialize` offering `zzz` | `400 unsupported-protocol-version` |
| `initialize` offering 2026-07-28 (handshake cannot reach the modern era) | `400 unsupported-protocol-version` |
| `tools/list` with header 2025-11-25 or 2026-07-28 | `401` — admitted, then authentication |
| `tools/list` with header 2025-06-18 | `400 unsupported-protocol-version` |
| `tools/list` with no header | `400 protocol-version-missing` |
| `initialize` inside a batch | `400 batched-initialize` |

The three middle rows are the ones that matter: each is a revision the SDK
behind the guard would have served.

### The authenticated journey, driven by the real client

Provisioned headlessly: a public client registered with
`./bin/mapp mcp-client-register`, the administrator password set to a known
value, then the authorization-code + PKCE flow driven with six HTTP calls
(authorize -> login -> consent -> code -> token). Claude Code was given the
resulting token A through `.mcp.json`'s `headers` key, which is the supported
way to hand an HTTP MCP server a static credential and avoids depending on the
undocumented shape of its OAuth credential store.

| Step | Result |
|---|---|
| `claude mcp list` | `✔ Connected` |
| `describe_instance` | `protocolVersions: ["2025-11-25","2026-07-28"]` at the time; `2025-06-18` was added later for Codex and Gemini |
| `layer_values` on a queryable layer | 6,147 output areas across 5 quintiles |
| `layer_values` with a narrow grant | refused, naming the scopes to request |
| same call after scope step-up | succeeds |

That is P2's release-gate journey for the Claude ecosystem, minus the approval
and elicitation legs, which apply to mutating operations this surface does not
expose.

Two properties checked while a live credential existed:

- **Audience separation holds.** A token A presented directly to the
  configuration API is refused `401 auth.authentication_required`. Only an
  exchange-minted token B, bound to one request, is spent there.
- **Disabling a client withdraws credentials already issued, not merely future
  ones.** `introspect` resolves the token to its *grant's* client, and
  `query_client` excludes disabled rows, so `client is None` returns the flat
  inactive response (mcp-auth/introspection.py:100-113,
  mcp-auth/sql_store.py:177). An already-issued token A therefore stops working
  without waiting for its 15-minute expiry. Refresh is refused too: an
  *unspent* refresh token against a disabled client gets `400 invalid_client`.

  The only delay is `mapp-mcp`'s positive introspection cache, bounded hard at
  30 seconds (mapp-mcp/introspection_client.py:25-27). Measured: a live token
  answered `200` immediately after the disable and `401` four seconds later,
  when its cache entry lapsed.

  Both halves of this were measured wrong the first time, and the way they were
  wrong is worth keeping. The refresh check was run *before* the disable, which
  rotated the token, so the later refusal could equally have been replay
  detection -- indistinguishable from the status code alone. And the token-A
  check was made inside the cache window and read as "disable does not affect
  live tokens", which would have understated the control badly. Re-run past the
  cache, with an unspent refresh token, both answers reverse.

### The defect the live client found

`layer_values` raised `ValueError` for every anticipated refusal -- a grant
missing a scope, a broker refusal, a binding refusal -- and `RuntimeError` for
unavailability. The SDK treats **only** `ToolError` as an anticipated failure
and puts its text in the result the model reads; everything else is a crash,
replaced with `Error executing tool layer_values` and logged at ERROR with a
traceback.

So every message written here to be acted on was discarded before reaching the
caller, and routine scope refusals were logged as crashes. The agent saw four
words and could do nothing with them.

The unit tests passed throughout, because they call the registered function and
assert on the exception it raises -- true of the function, and silent about what
crosses the wire. This is the same shape as the handshake failure recorded
above, and the same lesson: the assertion has to be made where the client reads
the answer. `ToolFailureVisibilityTests` in `tests/test_legacy_session.py` is
that assertion, and it discriminates on the exact signature of the bug -- the
SDK prefixes both kinds with `Error executing tool <name>`, and only a crash
stops there with nothing after it.

After the fix, driven against the deployed stack, the client is told: *"This
grant does not carry derive and semantic:inspect. Re-authorize requesting derive
semantic:inspect to use this tool."* -- and acts on it.

`tests/test_legacy_session.py` drives the whole sequence through the composed
application -- guard, authentication, real SDK runtime, real tool registry --
because the failure this section records was invisible to every per-layer test
in the directory. Each layer was correct and the client still could not connect.
A test per layer cannot see a handshake.

## Every ecosystem sat behind the target

The Claude finding above repeated itself twice, which turns a one-off into a
pattern worth stating: **no shipped client of any target ecosystem speaks the
specification's target revision.**

| Client | Offers | Measured how |
|---|---|---|
| Claude Code 2.1.272 | `2025-11-25` | echo server capturing the raw `initialize` |
| Codex CLI 0.154.0 | `2025-06-18` | the guard's own `-32022` refusal, in Codex's log |
| Gemini CLI 0.60.0 | `2025-06-18` | echo server capturing the raw `initialize` |

Codex and Gemini agree on the revision, so one allowlist entry served both. The
served set is now `2025-06-18`, `2025-11-25` and `2026-07-28`; `2024-11-05` and
`2025-03-26` remain refused, because no target ecosystem needs them.

Gemini needed one thing more. Its `initialize` succeeded and its very next
request was refused:

```
-> {"method":"notifications/initialized","jsonrpc":"2.0"}
<- 400 Missing MCP-Protocol-Version
```

It sends that header on nothing after the handshake, which the 2025-06-18
specification requires of clients. Proved to be the *only* remaining gap by
putting a proxy in front that injected the header and nothing else: with it,
Gemini completed initialize, the notification and `prompts/list` cleanly. So the
guard now admits a header-less request to the handshake era, bounded by the fact
that the modern era cannot be entered that way — it carries its version in
`params._meta` and requires the header to agree.

All three ecosystems then listed the tools and called them against the running
platform.

Two things are worth carrying forward.

**The refusal was actionable because the error code was right.** Codex logged
`-32022: Unsupported MCP protocol version 2025-06-18` *and the supported list*,
because the guard uses the code the MCP ecosystem defines rather than the
project-chosen number it originally had. A client that cannot parse the refusal
reports "connection failed", and the next hour goes into the wrong question.

**Reading the documentation would not have answered it.** Each revision was
established by putting the client in front of the server, and in Gemini's case
by capturing the raw handshake — its bundle contains half a dozen revision
strings, and picking the right one by inspection would have been a guess.

The list is expected to shrink rather than grow. Each entry exists for a client
that has not caught up, and should be removed when its ecosystem does.

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

**The era guard has five separable obligations**, and the specification
preemptively rejects the convenient proof: "enabling `stateless_http` is not
accepted as evidence that the legacy era is disabled" -- a rule that now cuts
both ways, since the no-session claim is asserted on the wire across a full
legacy session rather than inferred from configuration. It must admit only the
revisions in `SERVED_VERSIONS`; distinguish a *missing or malformed* header
(validation error) from a *declared unserved* revision (unsupported-version
error) rather than fabricating a version error for both; admit `initialize` only
as the handshake era and only when the revision offered *in the body* is the one
admitted handshake revision, refusing it as a method under the modern revision,
which forces it to inspect the decoded body before dispatch; never mint or echo
`Mcp-Session-Id`; and admit a header-less request only when it is a lone
handshake `initialize`, since that is the one request that cannot declare a
revision -- it is what decides one. It must not intercept the unauthenticated
RFC 9728 GET.

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
