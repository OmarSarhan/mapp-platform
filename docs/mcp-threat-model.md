# MCP authorization threat model

Companion to [ADR 0001](adr/0001-mcp-authorization-and-topology.md). Covers the
authorization component, the credentials it issues and the configuration API's
validation of them, as built in Phase 0.

Every control below names where it is enforced and where that enforcement is
pinned. Where there is no control, the row says so rather than describing an
intention — a threat model whose mitigations are aspirations is worse than none,
because it stops people looking.

**Read this as covering an unmerged feasibility spike.** `mapp-mcp` now exists
and a real MCP client has driven it end to end, so the request digest and the
execution binding have a producer and are exercised rather than assumed. What
remains unbuilt is the approval flow: P8's receipts and review packets are Phase
1, so the approval-forgery row below still describes a control that is designed
and not yet built.

**Accepted by the owner**, most recently on 2026-09-16 covering ten abuse
cases, with the two unmitigated rows below — approval fatigue in full, client
attestation in part — carried knowingly. O20, carried as an unverified control
through Phase 0, is now measured and closed.

**Two further rows accepted on 2026-09-18**: disclosure through a read grant,
and reading data through an expression. Both were added after the read surface
grew from two tools to 37. The residual in the first was accepted knowingly: a
grant holding a scope sees everything that scope covers, with no per-layer or
row-level restriction designed, so an instance whose workspace or catalogue is
itself sensitive should not issue agent credentials for it.

The claims this document rests on are asserted in
`mcp-auth/tests/test_threat_model_claims.py`, not merely written here. A
threat model is read instead of the code, so a sentence in it that quietly
stops being true is worse than one never written. Writing those tests found
one such sentence already: this document claimed both wider scopes were kept
out of every preset, when `federation:observe` has its own.

## Assets

| Asset | Where it lives | Compromise means |
| --- | --- | --- |
| Operator credential | `control.admin_credential`, one row, pbkdf2-sha256 at 310,000 rounds | Consent can be granted for any client and scope |
| Grants | `control.oauth_grants` | An attacker acts as a consent that an operator approved |
| Token A | `control.oauth_tokens`, sha256 of the raw value | 15 minutes of MCP-audience access, exchangeable for token B |
| Token B | same table, with an operation and digest binding | One configuration-API operation on one request, ≤60s |
| Authorization codes | `control.oauth_authorization_codes` | A token A, if PKCE is also defeated |
| Operator session | `control.oauth_sessions` + `mapp_oauth_session` cookie | Consent on behalf of the operator |
| Client secrets | `control.oauth_clients`, sha256 | Impersonation of the broker or the configuration API to the control listener |
| Workspace and platform state | outside this component | The effect the whole design exists to gate |
| Workspace description | read through the configuration API by 37 tools | Disclosure of every layer's configuration and the relation it reads, the derived inventory, curated meaning, the source relation inventory, the federated source registry, and the full diff of every proposal |

Only hashes are persisted for every credential. Raw values are returned once and
never stored, never logged.

## Trust boundaries

1. **Public internet → Caddy.** TLS terminates here in production.
2. **Caddy → the component's edge listener**, over an exclusive Unix socket.
   Caddy publishes exactly four paths; the component's edge route table
   independently contains no `/internal/*` path.
3. **The `mcp-control` network.** The control listener, reachable only by
   `config-ui` and `mapp-mcp`. Every call is authenticated as a
   confidential OAuth client over HTTP Basic; network placement is not treated
   as authentication.
4. **Component → `control` schema**, as `mapp_control`, which can create nothing
   outside its own schema.
5. **Agent → configuration API.** Where a token B is presented and spent.

The socket peer at boundary 2 is Caddy alone, which is why the component trusts
`X-Forwarded-For` on that listener and only there — the TCP listener class sets
`trust_forwarded_for = False`. Caddy overwrites rather than appends the header.

## Abuse cases

### Replay

**Token B replay.** A captured token B is presented again for the same request.
Refused: a mutating operation's token is single-use, and consumption is one
conditional `UPDATE` with the operation and digest as predicates, so two
presentations cannot both proceed. Measured under 16 simultaneous presenters
across 20 trials: exactly one winner every time. A read operation's token is not
single-use by design — there is nothing to spend, and the binding still confines
it to one request.

**Token B on a different request.** A token minted for one proposal presented
against another. Refused: the configuration API rebuilds the canonical envelope
from the request in front of it and the digest is a predicate of the spend. A
wrong presentation does not burn the credential, so the legitimate retry still
works.

**Authorization code replay.** Refused: the code is consumed by the statement
that reads it, so N concurrent redemptions yield exactly one token.

**Residual:** a token B captured *in flight* and raced to the API before the
legitimate caller wins the race and executes the operation once. The window is
under 60 seconds and the effect is the one the operator approved, but the
attacker chose the moment. Nothing prevents this; TLS and the internal network
are what stand between an attacker and the credential.

### Confused deputy

**The classic OAuth case** — an authorization server tricked into issuing a
credential for the wrong resource. Addressed by audience separation: token A
carries the MCP resource, token B the configuration API, and the component
refuses to start if the two values are equal. Introspection compares the
audience, and the configuration API checks it again on every call.

**Exchange as a deputy.** The broker holds credentials that can mint token B.
Restricted by: an allowlist of five operations rather than all fifty-two; a
requested scope that must be a *subset* of both the token's and the grant's,
never an intersection; and `full` on a hard deny-list so an unclassified route is
unreachable.

**The configuration API as a deputy.** It holds a client secret for the control
listener and could redeem any token B it was given. It only ever redeems against
a digest it computed itself from the request in front of it, so it cannot be
persuaded to authorise a request it did not receive.

**Residual:** the broker is trusted not to fabricate a digest. It cannot verify
one — it never sees the downstream body — so a compromised broker could mint a
token B bound to a digest of its choosing and then present the matching request.
The allowlist and the grant's scopes bound what that could reach. This is
inherent to the split and is the strongest argument for keeping the allowlist
small.

### SSRF

The component makes no outbound requests. The configuration API makes one, to a
fixed internal endpoint derived from `MCP_AUTH_URL`, which is validated at
construction as an internal HTTP root URL with no credentials, query or
fragment, and redirects are rejected by a custom opener.

**Residual:** `MCP_AUTH_URL` is operator-supplied. An operator who pointed it at
a hostile host would send it token values. O15 — whether the component gets a
dedicated egress proxy — remains open.

### Credential theft

Only hashes at rest. Raw tokens are returned once. The consent and login pages
carry a nonce-based CSP with `default-src 'none'`; the session cookie is
`SameSite=Lax`, `Path=/oauth`, `HttpOnly`, and `Secure` in production.
Never logged: authorization headers, raw tokens, codes, refresh identifiers,
broker assertions, cookies, CSRF values, approval state, raw SQL or workspace
content. The exception log lines added for token-B validation carry the
operation id and the failure reason, never the credential.

`hmac.compare_digest` guards the form tokens and client secrets. Hash lookups
are SQL equality on a sha256 of a high-entropy secret, which is a deliberate
move away from constant-time comparison and is documented at the call site.

**Residual:** `SameSite=Lax` is required — the specification's own diagnosis is
that `Strict` will not ride the agent's top-level handoff navigation — so it is
weaker than `Strict` by necessity.

**O20 is resolved, and it had failed.** Driven in Chromium, Firefox and WebKit,
`form-action 'self'` did not survive the cross-origin redirect the consent POST
answers with: Chromium and WebKit re-check the directive across the redirect and
refused to follow it. All three delivered the POST first, so the grant existed
and only the authorization code was lost — consent recorded, agent silent. The
directive now names the client's own redirect origin, taken from the pending
record the server has already matched exactly; re-measured, all three engines
reach the callback. WebKit is the engine Safari ships, driven here through
Playwright rather than Safari itself.

### Approval forgery

The consent screen binds its CSRF token to the session (`session_hash`) and
rotates the token when the session binds, which closed a cross-session consent
forgery where the CSRF check was self-verifying. The consent POST rebuilds the
authorization request from the *stored* query string, so a submission cannot
alter `scope`, `redirect_uri` or `client_id`. A grant records exactly what was
approved, and the exchange checks the grant rather than only the token — a grant
that consented to nothing mints nothing.

Redirect URIs are matched exactly. No prefix or wildcard matching, ever: a prefix
test would admit `<registered>.attacker.example/steal`, which is a working open
redirect that delivers authorization codes.

Phase 1 wave 5 adds per-effect approval on top of that, so a grant with `apply`
no longer authorises every apply for its lifetime. The configuration API refuses
an operation that requires approval unless the request also carries a receipt
that spends against the same canonical digest the credential was bound to, and
the requirement is derived from the action's own risk class rather than declared
a fourth time. What "requires approval" means is stated as an exemption list, so
a risk class nobody has classified requires approval rather than silently
arriving unguarded.

The receipt cannot reach the model. It is minted by the platform for the holder
of a handle, returned inside the MCP server process, and put in a header by the
HTTP client; no tool takes it as an argument and no tool result contains it. The
person deciding never holds it either — they see a *reference*, which names the
row and spends nothing — so there is no route by which a decision made in a
browser travels back through the agent.

**A person is asked in the session they are working in, and the server holds a
session so that it can ask.** Three paths — URL elicitation, form elicitation,
and the dashboard — chosen by what the client declared.

This was not free, and the cost is recorded here rather than in a commit
message. Driving the deployed stack on 2026-09-20 showed that under the
original transport arrangement *neither* elicitation path could run, for two
different reasons:

- The modern era (2026-07-28) declares capabilities on every request, but the
  SDK serves it through a dispatch context that refuses server-initiated
  requests outright.
- The handshake era (2025-11-25) has the request's own stream, but
  `stateless_http=True` means no session is kept, so capabilities declared at
  `initialize` are gone by the time a tool runs. Forcing the capability on and
  retrying produced, verbatim: *"Cannot send 'elicitation/create': this
  transport context has no back-channel for server-initiated requests."*

What the shipped clients declare was measured separately, on 2026-09-18, and
is kept because it decides which path would be taken if the transport ever
carried one — today it decides nothing:

| Client | Declares | Path it would take |
| --- | --- | --- |
| Codex CLI 0.155.0 | `elicitation: {form, url}` | URL |
| Claude Code 2.1.276 | `elicitation: {}` | Form |
| Gemini CLI 0.58.0 | none | Dashboard |

**`stateless_http` was turned off at wave 7, and `era_guard` obligation 4 —
never mint or echo `Mcp-Session-Id` — was withdrawn with it.** That is the
trade, made deliberately: without a session there is no back-channel, and
without a back-channel approval happens at a separate dashboard over two tool
calls, which is the arrangement this design ruled out.

**What that obligation was actually worth, because it is easily overread.** It
never enforced the era decision. `era_guard` does that on the wire, holding
the served set to `SERVED_VERSIONS`, and it still does — an unserved revision
is still refused and `initialize` is still admitted only as the handshake era.
What the obligation bought was a smaller surface: no server-side state keyed
by an identifier a client presents. That is what was given up.

**What replaced it, checked rather than reasoned about.** A session identifier
authorises nothing: authentication is per request from the bearer token, so a
stolen identifier without a token A reaches nothing. And one session cannot
answer another's elicitation — probed against the deployed stack, where a
second session presenting a valid token and the correct request id was acked
`202` by the transport and never routed, leaving the first call waiting; and
pinned in process by `CrossSessionElicitationTests`, which carries a positive
control because the first version of that test passed while proving nothing.
An agent cannot approve a mutation it was not asked about.

**The dashboard path remains** for a client that declares no elicitation
capability — Gemini CLI 0.58.0, measured 2026-09-18 — as a two-call flow, and
its code and tests stay. What changed is that clients which can be asked now
are.

**The model cannot answer on its own behalf.** On the path that actually runs
the answer is given in an authenticated dashboard session the agent has no
credential for, and what the agent holds afterwards is a receipt the platform
minted, bound to one digest. Where elicitation is available the same holds by a
different route: the prompt is rendered by the *client* and answered by a
person, and an `ElicitResult` is a transport message the model has no way to
fabricate, exactly as it cannot fabricate a tool result it did not receive. In
neither case is any of the four approval operations a tool, so nothing the
model can invoke reaches them. Two tests pin the last part — one reads the
registered tools and their declared operations out of the source, and one asserts
the gate operations are absent from the scope table the listing filter reads.
The prompt text is composed from the packet the tool built, not by the model, so
an agent cannot phrase its own request for permission persuasively.

**Division of trust, stated because it cannot be verified away.** For form-mode
clients the MCP server relays the person's answer to the platform through
`approvals.confirm`, and the platform cannot verify that an elicitation
happened any more than the broker can verify a request digest it never saw. A
compromised MCP server could therefore approve its own requests. That is the
same trust already placed in it to make the platform call at all, and it is
bounded by the exchange living on the control listener only — an external holder
of a token A cannot mint a token B, so this is not a route that opens to a
network attacker. The audit distinguishes the two assurances: a dashboard
decision records the operator, a relayed one records `session:<grant>`.

**A receipt is not a standing authorisation.** The decision window and the spend
window are separate and both bounded — fifteen minutes to decide, measured from
the request, and five minutes to claim and spend, measured from `decided_at`
rather than stored. Without the second bound an agent could hold an approved
receipt and spend it at a moment the person was not expecting, which is the
thing the mechanism exists to prevent. Revoking the grant reaches approved and
unspent approvals in the same transaction.

**Residual:** approval is a *confirmation* control, not a segregation-of-duties
control. The person approving in an MCP session is the same person who asked for
the change. It defends against an agent doing something the person did not
intend; it does not defend against a person doing something they should not. That
is the right trade for a single-operator system and is stated here rather than
left to be inferred — P20 already records that separation of duty is not
enforceable with one shared administrator identity.

**Residual:** P8's time-boxed standing windows are still Phase 1 wave 8. Today
every gated effect asks.

### Credential administration from a browser session

New in this revision, and a real change to what a web session can reach. An
administrator can now register an agent client and revoke a consent from the
dashboard; previously registration needed shell access to the host and
revocation had no operator surface at all. So the set of privileged actions
reachable by riding an authenticated session has grown.

What bounds it:

- **Administrator session only.** Every route checks the session actor and
  refuses a bearer token, including `full` — the widest credential the platform
  issues. Credential administration is deliberately not delegable to a token,
  and the check is written out per route with a test per route, plus one that
  derives the set of administrator reads from the dispatch so a new one cannot
  be added unguarded.
- **Registering a client grants nothing.** This is the property that keeps the
  new surface from being privilege escalation: a registered client can ask for
  consent and can do nothing until an operator signs in, is shown the scopes and
  approves them. An attacker who registers a client has created something that
  still needs the operator credential to become useful, and the registration is
  in the audit log.
- **`full` and `admin` are refused at registration**, so a client cannot be
  created carrying the scopes the broker's deny-list exists to stop.
- **Revocation fails safe.** The worst an attacker achieves by revoking is
  denial of service — consents stop working and operators re-consent. It removes
  authority and never grants it, which is the direction an attacker does not
  want and an operator under attack does.
- **Both actions are audited** with the acting surface distinguished: `admin`
  for the dashboard, `local-admin` for the command line, so the log answers
  which route was used and not merely that it happened.

**Residual:** these routes inherit the dashboard's CSRF and cookie posture
rather than adding their own, so they are exactly as strong as the session that
reaches them — `SameSite=Strict` on the configuration origin and an
`X-CSRF-Token` on every state change. A session compromise is already total for
the dashboard; what this adds is that it now also reaches agent registration.
The mitigation for that is not in this component but in the operator credential,
and P20 already records that separation of duty is not enforceable with a single
shared administrator identity.

### More than one protocol revision is served

P2a admits two handshake revisions alongside the modern one, because that is
what the three target ecosystems speak: `2025-11-25` for Claude Code 2.1.272 and
`2025-06-18` for Codex CLI 0.154.0 and Gemini CLI 0.60.0. Serving several
revisions is the kind of change that usually widens a surface, and here it did
not, for reasons worth recording rather than re-deriving later:

- **The allowlist got stricter, not laxer.** The SDK behind the guard negotiates
  an `initialize` to whatever the client offers — 2024-11-05 verbatim, and an
  unrecognisable offer counter-offered the newest handshake revision — so
  admitting the method without policing it would have admitted every revision
  the SDK has ever spoken, plus a fallback. The guard reads the offered revision
  out of the body and admits only the listed set; `2024-11-05` and `2025-03-26`
  are refused because no target ecosystem needs them. The relevant property is
  not how many revisions are listed but that the list is this project's and is
  enforced before dispatch.
- **No session identifier is minted.** The handshake era is session-based, so
  the obvious cost would have been accepting `Mcp-Session-Id` and the state
  behind it. Measured across a full legacy session — initialize, notification,
  list, call — none is emitted, and the guard strips the header from anything
  the inner application sends regardless.
- **The standalone event stream is refused.** A GET to the RPC path is where the
  handshake era puts its server-to-client channel, and the SDK serves one: an
  authenticated GET returned `200 text/event-stream` and held the connection
  open. With `stateless_http` there is no session for it to belong to, so it
  carries nothing and costs a held connection per caller — a cheap way to occupy
  the runtime. The guard answers `405`. No target client needs it: all three
  completed a session through a proxy that implemented POST and nothing else.
- **Authorization is untouched by era.** The guard runs before authentication,
  every scope check is era-independent, and no credential, audience or binding
  rule differs between them.
- **A header-less request reaches the handshake era only.** The guard stopped
  requiring `MCP-Protocol-Version` after the handshake because Gemini CLI 0.60.0
  sends it on nothing after `initialize`, contrary to the specification it
  speaks. The relaxation is bounded by the modern era's own shape rather than by
  the guard: the modern revision carries its version and the client's
  capabilities in `params._meta` and requires the header to agree with them, so
  a request with no header cannot be served as modern. That is asserted against
  the real SDK rather than reasoned about, because the relaxation rests on it
  — and the corollary is asserted too: a modern request without its envelope is
  refused, so the two are required together rather than the envelope being
  quietly optional.

### State exhaustion

Parked authorization records are capped at 64 per source with a global backstop
of 10,000, and a refusal answers `503` with `Retry-After` rather than a server
error. This replaced a global-only cap that measured worse than none. Measured:
64 admitted, the next 10 refused, and a second source still admitted throughout.
Expiry cleanup runs opportunistically on write; under 16 concurrent writers and
4 concurrent sweeps it removed 100 expired records, removed no live record, and
did not deadlock.

Token B minted per grant is capped at 60 in a sliding 60-second window, which
closes the one unbounded multiplier: every other bound was per credential, so a
grant minting them in a loop was the only way one consent became an unlimited
number of consequential effects. Per grant rather than global, for the reason
the parked-request cap is per source — a global bound lets one busy actor deny
everybody. Refuses with `slow_down`, so a legitimate burst is delayed.

Login attempts are throttled per remote address. Request bodies are bounded at
16 KiB on the control endpoints and 512 KiB at the MCP origin in Caddy.

**Residual:** `control.tokens` is never purged, deliberately — a revoked token's
case-insensitive name must stay reserved. And the connection ceiling of 8 with no
pooling means a burst of concurrent authorization requests is refused by
PostgreSQL rather than queued; measured to recover as soon as a connection is
released, but it is a ceiling and not headroom.

### Approval fatigue

**No control.** P8's time-boxed standing approval and its seven bounds are
Phase 1, and O19 records that neither the capabilities response nor the manifest
can currently express which action classes a standing window may cover. Phase 0
prompts for consent per authorization request, which is the safe end of the
trade-off and also the one most likely to train an operator to click through.

Wave 5 makes this worse before wave 8 makes it better, and that is worth saying
plainly: every gated effect now asks, so the number of prompts an operator sees
goes up. What is done about it is small and deliberate — approving takes two
clicks in the dashboard and names the operation, declining takes one, and a
request that carries no packet says so rather than looking routine, because
approving a bare operation name is how the habit forms. None of that is a
control. P8's standing windows and their seven bounds remain the answer, and
O19 still records that neither the capabilities response nor the manifest can
express which action classes such a window may cover.

### Disclosure through a read grant

What a single `inspect` grant discloses is not self-evident from the scope's
name: every layer's configuration including the relation and columns it reads,
the derived-layer inventory, the review queue with the full diff of every
proposal ever made, the platform contract, and the workspace JSON schema.
Adding `semantic:inspect` discloses curated meaning and its change history;
`derive` discloses aggregate values and distribution summaries over a layer's
own relation, and nothing more — creating, replacing, refreshing and dropping
a managed relation moved to `derive:manage` in Phase 1 wave 1, because a scope
in the default read-only preset must not authorise a write; `federation:observe` names the third-party databases behind the
instance; `semantic:source` names every table and view in the database,
including ones no layer uses.

**Partly controlled, and the control is scope separation rather than
redaction.** The four disclosures above sit behind four different scopes, none
implied by another, so an operator grants each deliberately. The dashboard's
default `analysis` preset stops at `derive` and `semantic:inspect`;
`federation:observe` is offered only through a separate `analysis-federated`
preset, so naming the third-party databases is a different choice from reading
the workspace; and `semantic:source` is in no preset at all, so the database's
relation inventory can only be granted by ticking it. `full` and `admin` are never issued for this resource, and the
configuration API withholds credential identifiers from an exchanged
credential at the response layer, so `actor` fields arrive as `[withheld]`
whichever tool returns them.

Since 2026-09-18 `tools/list` is filtered to what the grant can call, so a
narrow grant is not shown the surface it cannot reach. That is a usability
property first -- an agent was previously told no one tool at a time -- and
disclosure reduction second: it does not change what any credential may read,
only what it is told about. The call check is unchanged and remains the
boundary.

**Not controlled:** a grant that legitimately holds a scope sees everything
that scope covers. There is no per-layer, per-asset or row-level restriction,
and none is designed. An instance whose workspace or catalogue is itself
sensitive should not issue agent credentials for it.

### Reading data through an expression

`sql_test` evaluates a caller-supplied SQL expression against a configured
layer's own relation and returns a sample value, which is a data read spelled
as a validation check.

**Controlled at the platform.** The transaction is `READ ONLY`, the statement
timeout is five seconds, `search_path` is pinned to `pg_catalog, public`, and
function names are allowlisted and checked against being shadowed by an
untrusted database function. Driven against the deployed stack on 2026-09-18:
statement injection is refused as a syntax error because the expression is
composed as one scalar, and `pg_sleep` and aggregate subqueries are refused by
name. It costs `derive` -- the scope `layers_values` already needs, and for the
same reason: both return values from the same relation.

**Residual:** an expression can read any column of a relation a layer is
already configured to read, which `layers_values` can do too. The bound is the
set of configured layers, not the database.

### Compromised client

A registered agent client that turns hostile. Bounded by: scopes it was granted
and no more; `full` and `admin` never issued for the MCP resource; no direct
saves, no credential administration, no token issuance or revocation, and no
dashboard or audit administration exposed; every consequential operation
requiring a fresh token B bound to one request.

**What changed at Phase 1 wave 7.** `derive:manage` became grantable, which
puts DDL on the surface: a hostile client holding it can create, replace,
refresh and drop managed relations. This is the widest thing an operator can
hand over and the preset says so. Three things bound it. Every one of those
operations requires a receipt, so the reach is again "it can ask". A drop is
refused outright while any layer or other derived relation still reads the
relation — by the platform, and separately by the tool before it asks. And a
replace, which is the one that does *not* announce itself, carries the current
definition and the dependent list into the approval, so the person is shown
that layers will keep working and start returning different numbers.

The residual worth naming: `refresh` is the cheapest of the four to ask for
and the most expensive to serve, since it reads every source row again. It
asks a person every time, which is what stops it being a way to spend the
database's time unattended — and is another reason wave 8's standing windows
should not cover it casually.

**What changed at Phase 1 wave 6.** `apply`, `semantic:apply` and `reload`
became grantable, so a hostile client holding them can reach operations that
write the workspace, write curated meaning and tell the tile service to serve
the result. Until wave 6 those scopes were unofferable and that unofferability
*was* the control. It no longer is, and the control that replaced it is wave
5's receipt: every one of those operations is refused by the configuration API
without a receipt bound to that exact request, and a receipt exists only
because a person answered a prompt their own client rendered. So a hostile
client's reach into the irreversible half is exactly "it can ask", and what it
can do unattended is unchanged — the proposal creates and the previews, none of
which alter what the map serves.

The honest residual is that asking is itself an attack surface. A hostile
client can ask repeatedly, and it composes the *operation* even though it does
not compose the prompt: the summary the person reads is read back from the
platform's own record of the proposal, not from the tool's arguments, so the
description cannot be made to disagree with the change. What it can do is
choose a moment, and phrase nothing. That is approval fatigue, which is
recorded above as having no control until wave 8.

What it gets is disclosure, at the speed of a script. The surface is 37 reads,
so a hostile client holding an ordinary analysis grant can enumerate the whole
workspace description in a few dozen calls — every layer and the relation it
reads, the catalogue, the proposal history — and nothing rate-limits reading.
The bound is which scopes the grant carries, not how many tools exist or how
fast they are called. This is the same disclosure the row above describes,
reached through a client rather than a person; it is listed separately because
the mitigations differ, and for a compromised client the only ones that bite
are the scope the operator chose and disabling it afterwards. Which is why the
preset an operator reaches for matters: `analysis` is the default and excludes
both of the scopes that disclose anything beyond the configured workspace.

Disabling a client takes effect immediately at introspection, at the exchange,
and for an already-issued token B — the last of which required checking the
*grant's* client rather than the token's, because a token B names the broker.

Revoking the grant invalidates every credential derived from it at once,
including a token B issued and not yet spent.

A client is registered by an operator, never by itself. Registration validates
the redirect URIs the authorization server will later match exactly, and
refuses `full` and `admin` outright.

**Residual, and accepted:** there is no client attestation. An agent is a
public client — it holds no secret at all, and PKCE binds the code to the
requesting instance rather than proving which application it is.
`private_key_jwt` is deferred for a concrete reason recorded in the ADR. So a
hostile application that can register itself as an agent, or take over a
registered agent's redirect URI on the operator's own machine, is bounded only
by the scopes that agent was granted. P2's client acceptance matrix is 1 of 3,
so nothing can be said about how Codex/OpenAI or Gemini behave.

## Delta: a fourth peer on `mcp-control`

The specification asks for a renewed transport threat model whenever another
service joins that network. This is a recorded delta rather than a renewal, and
saying which is the point: the original analysis is not re-derived here, so a
reader should treat the two together rather than this alone.

`mapp-mcp` has joined, as P12 requires. This was written before it did; the
claims below are now checked against the running deployment rather than
predicted, and each is marked where that changed the evidence. What changes:

- **One more peer can reach the control listener.** It authenticates as a
  confidential client like config-ui, so the listener's admission rule is
  unchanged; what grows is the number of processes holding a credential that
  passes it. The credential is registered by an operator and stored as a digest,
  so compromising the authorization component does not yield it and compromising
  `mapp-mcp` yields only its own.
- **It reaches nothing else.** No `backend`, so no database, and no `edge`. It
  holds no database credential by design and reaches platform state only through
  authenticated API calls, which is the constraint the whole topology rests on.
  *Checked on the running container: it is attached to `mcp-control` and to no
  other network, and its environment carries no database URL of any kind.*
- **Its public surface is a Unix socket**, mode 0660, in a directory shared with
  Caddy alone. *Checked: `srw-rw----`. Worth checking rather than asserting,
  because uvicorn hard-codes `uds_perms = 0o666`; the component binds the socket
  itself to avoid that.* That is the same shape as the authorization component's edge
  socket, and it is why `mapp-mcp` needs no edge network: nothing can reach it
  from a network at all, so a misconfigured route cannot expose it.
- **What it can do with its credential is bounded by what the listener offers**:
  introspect a token A, and exchange one for a token B against an allowlisted
  operation. It cannot mint a token A, revoke a grant belongs to the operator
  surface, and the exchange refuses anything the presented grant does not
  already carry.

One of those questions has since been answered rather than left open. The
listener used to permit any authenticated confidential client everything it
offered, so the configuration API's credential could mint an execution token
for an allowlisted operation -- the privilege the exchange exists to gate, held
by the component with the largest attack surface on the platform. Each client
now carries the capabilities it needs and is refused the rest: the configuration
API may introspect, redeem and revoke; the runtime may introspect and exchange.
Neither can do the other's job.

What this delta still does **not** cover, and a renewal would: whether three
peers on one internal network is the right shape at all. That becomes worth
asking if a fourth arrives.

## Attacker capabilities the design does not defend against

Stated plainly, because a threat model that implies otherwise is misleading:

- **A compromised host or database.** Every credential hash, the operator
  credential and all grant state are readable there.
- **A compromised broker.** It can mint a token B for any allowlisted operation
  within a grant's scopes and fabricate the matching digest.
- **A compromised MCP server.** It can relay a form-mode approval that nobody
  gave, because the platform cannot see the elicitation. Bounded by the exchange
  being reachable only on the control listener; see *Approval forgery*.
- **A malicious operator.** P20 records that separation of duty is not
  enforceable with a single shared administrator identity, so it is not claimed.
- **Traffic interception inside the deployment.** The control listener is plain
  HTTP on an internal Docker network. The specification's own transport threat
  model raises mTLS for this; it is not implemented.
- ~~**Restore-time credential invalidation.**~~ **No longer true — this was
  closed and this document said otherwise for a revision.** The deferral was
  recorded against a cost estimate — an epoch predicate on 46 statements across
  two components — that was for the wrong design. Every credential read already
  filters on a revocation, so the epoch is applied once, at restore, by revoking
  what predates it; reads never changed, and not one `INSERT` changed either,
  because rows are stamped by a function-backed column default.
  `./bin/mapp advance-recovery-epoch --confirm` is step 5 of the restore
  procedure, before the stack starts, because a pre-restore credential is usable
  until it has run. The sweep invalidates every *live* credential rather than
  only those below the counter, which is what makes a newer snapshot restored
  over an older database behave correctly.

## Review status

The abuse cases the Phase 0 gate names are covered above: replay, confused
deputy, SSRF, credential theft, approval forgery, state exhaustion, approval
fatigue and compromised client. Two carry no mitigation (approval fatigue in
full, client attestation in part) and one carries an unverified control (O20).

Four further cases were added after the runtime and the dashboard surfaces were
built, beyond the list the gate names: credential administration from a browser
session, serving a second protocol era, disclosure through a read grant, and
reading data through an expression. None is unmitigated, and all are here
because the surface changed rather than because the gate asked — a threat model
that only ever answers its original checklist stops describing the system it is
about.

The last two are the clearest case of that. This document was written when the
runtime had two tools; it now has 37, and what one `inspect` grant discloses
was not a question the original checklist could have asked.

**Both were accepted by the owner on 2026-09-16**, together with the rest of
this revision. The acceptance line at the top of this document now covers ten
cases rather than eight.

The owner has accepted this document for this version with those two rows
carried knowingly. That acceptance is not a claim they are mitigated — it is a
decision to proceed with them open, and each has a revisit condition in the
ADR. O20 still needs one manual check in three browser engines before any
public route.
