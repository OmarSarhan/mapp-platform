# ADR 0001 — MCP authorization and deployment topology

- **Status:** accepted for Phase 0. The owner accepted this record and the
  threat model as functional and safe for this version, with the two
  unmitigated threat-model rows (approval fatigue in full, client attestation
  in part) knowingly carried as accepted risks. Phase 1 implementation remains
  gated on the conditions in the evidence bundle.
- **Revised 2026-09-16**, after `mapp-mcp` was built and a real MCP client drove
  it end to end. Four amendments are new (P2a, the dashboard registration
  surface, the consent-withdrawal surface, and the consent CSP widened after
  O20 failed) and three accepted risks are closed.
- **Accepted by the owner, 2026-09-16**, covering all of the above:
  refresh-token semantics as built (30-day families, rotation with replay
  detection, 30-second retry grace); P2a's two-revision support; dashboard
  credential administration; the two threat-model rows added this revision; and
  advancing past Phase 0 into Phase 1 design. Merge approved **on the condition
  that the MCP feature stays off `main`** — see the merge note in the evidence
  bundle.
- **Scope:** the authorization design for `mapp-mcp`, its deployment topology,
  and the state it owns
- **Supersedes:** nothing. First ADR in this repository.
- **Evidence:** [`mcp-phase0-evidence.md`](../mcp-phase0-evidence.md)
- **Threat model:** [`mcp-threat-model.md`](../mcp-threat-model.md)
- **Specification:** `mapp-mcp-scope.md` (outside this repository), decisions
  A1–A8 and P1–P21

## Context

`mapp-mcp-scope.md` records 28 settled decisions and a gate that forbids Phase 1
until Phase 0 produces working prototypes plus an approved ADR and threat model.
Phase 0 existed because specification had stopped paying: two rounds of closing
gaps in prose failed adversarial verification, and the dominant failure mode was
asserting platform behaviour that does not exist. Every one of those was caught
by a reviewer who read the code, which is the error class a prototype eliminates
on contact and a review cannot.

This ADR records what the decisions became when they were built. It deliberately
does **not** restate the specification. Where a decision survived contact
unchanged, it is listed and left alone; the value here is in the deltas, because
those are what a reader cannot get from the spec.

## Decision

Build the authorization component as specified, with the amendments below, and
treat the gaps in the final section as blocking for Phase 1 rather than as
Phase 0 failures.

The shape that resulted:

- A platform-hosted authorization component (`mcp-auth`) on its own public
  origin, serving four public paths over a Unix socket that Caddy proxies to,
  and an internal control listener on the `mcp-control` network only.
- Two token audiences. Token A is for the MCP resource and lives 15 minutes.
  Token B is for the configuration API, lives at most 60 seconds, is bound to
  one allowlisted operation and one canonical request digest, and is single-use
  when that operation mutates.
- A grant as the unit of consent and of revocation. The actor is
  `oauth:{grant_id}`, so every credential resolves back to the consent that
  produced it.
- All authorization state in a PostgreSQL `control` schema owned by
  `mapp_control`, replacing the previous file-backed store.

## What Phase 0 changed

These are the amendments. Each was discovered by building, not by review.

### Retired objections

**P1's dependency objection to `authlib` does not hold for this slice.** All 40
modules under the RFCs used (6749, 6750, 7009, 7636, 7662, 8414, 9207) are pure
Python and import neither `joserfc` nor `cryptography`. `pip install --no-deps
Authlib` therefore adds no native dependency — which matters because every
platform image is Alpine/musl and the vendored `cryptography` wheel is manylinux,
so it could not have been installed regardless. Pinned by a test.

### Amendments

**`rfc8693` in `authlib` is a 162-byte docstring.** No grant, no endpoint, no
validation. The exchange is entirely project code. The specification's phrasing
implied an integration point that does not exist.

**`private_key_jwt` is deferred to Phase 1, for a concrete reason.** `rfc7523`'s
`authenticate_client` hard-codes `check_endpoint_auth_method(..., "token")`, so
under it a client model cannot distinguish the token endpoint from the exchange.
Phase 0 uses `client_secret_basic`, which receives the real endpoint name. P1
states a preference for `private_key_jwt`; this records why it was not taken yet.

**The exchange refuses rather than narrows.** The specification says "narrowing";
the implementation refuses when the requested scope is not a subset of what the
grant permits, and never computes `granted = requested ∩ permitted`. Silent
intersection makes widening look like success: a client asking for `apply`
against an `inspect` grant would get a working `inspect` token and never learn
its request was altered. This is a deliberate reading of P1, not a deviation
from its intent.

**Revocation is enforced at every read, not only at introspection.** The
specification's claim — that revoking a grant invalidates an already-issued
token B — was true of introspection alone for one milestone. Both statements
that spend or verify a token B now resolve the grant. A token B lives sixty
seconds, and expiring is not revoking.

**The canonicalizer is vendored three times, not shared.** The components ship
as separate images with no shared package, and the specification asks for each
trust boundary to evaluate the scheme independently so a defective shared helper
cannot make a bad digest look correct everywhere. Each copy runs the RFC's own
vectors, and a further test compares the copies directly. The third copy arrived
with `mapp-mcp`, which also carries the envelope builder; one envelope digest is
pinned literally and recomputed independently by both builders, which is what
differential agreement alone cannot give — two copies wrong in the same way
agree perfectly.

**PostgreSQL is 17, not 13.** The repository's general guidance assumes 13; the
packaged image is 17. Nothing in the control schema depends on the difference,
but the assumption was wrong and is recorded so no one plans around it.

**One abuse budget exists; P11's full budgets do not.** The plan expected M4 to
create quota columns with enforcement deferred, and it created none. Rather
than add columns, Phase 0 added the one bound whose absence was a multiplier:
**token B minted per grant per window**, capped at 60 in 60 seconds and counted
from the token rows themselves, so nothing new has to be written, expired or
reconciled. Every other bound was per credential — a token B is single-use and
lives sixty seconds — so a grant minting them in a loop was the only unbounded
path, and each one authorises a consequential operation. The cap is per grant
because that is what the operator approved and what revocation acts on; a
global cap would let one busy grant deny a quiet one, which is the mistake P21
already corrected for parked requests. Refuses with `slow_down` on a sliding
window, so a legitimate burst is delayed rather than denied. Provisional, as
P11 requires: replaced by measured values in Phase 6.

**`recovery_epoch` was reserved storage, not a mechanism** — through Phase 0.
Phase 1 wired it; see the note below. Through Phase 0 the column was
declared on eight tables and never written or read. A restore today invalidates
nothing. The schema previously described it as working.

**The per-source parked-request cap (P21) was decided by measurement.** A global
cap alone proved worse than none: parking a record is unauthenticated and free,
so one caller filled the shared table in about 24 seconds and locked every other
user out of `/oauth/authorize` for the record TTL. The bound is now 64 per
source, so a flood denies only the flooder. Accepted with a revisit condition:
operators behind a shared egress IP could legitimately reach 64 concurrent
in-flight authorizations.

**The audit record and the authorization decision do not share a transaction.**
The audit log deliberately remains at `var/control/audit.jsonl` while
authorization state moved into the database. P19 asks for an append-only audit
table committed with the effect; that is Phase 1. The current arrangement is
consistent with the ownership matrix and is recorded as an accepted gap.

**Agent clients are registered by a person, from either of two operator
surfaces.** `./bin/mapp mcp-client-register` and the dashboard's Access and
audit panel write the same *public* client — PKCE and no secret, because an
agent is a native or desktop application that cannot keep one, and issuing a
secret would create a credential that has to live on the operator's machine
unprotected. Redirect URIs are validated at registration (absolute, no fragment,
no userinfo, https unless loopback) because the authorization server matches them
exactly and a permissive entry is a working way to have authorization codes
delivered elsewhere. `full` and `admin` are refused outright. RFC 7591 dynamic
registration is rejected rather than deferred: P2 requires one *pinned* client
per ecosystem, and a person decides which agent may ask for consent.

*Amended after Phase 0.* The command was originally the only surface, which made
the person who runs the platform and the person who runs the agent the same
person, or required them to exchange a client id out of band. The dashboard
route is administrator-session only and refuses a bearer token including `full`;
it composes the configuration the operator hands over, because a client id
retyped by hand gets the byte-exact redirect URI wrong. Registration still
grants nothing by itself — the operator signs in and consents before the client
can do anything — which is what keeps a second registration surface from being a
second privilege.

**Consent has an operator surface, and until Phase 0 ended it did not.** A grant
is the unit of revocation in this design, and revoking one was reachable only
from `mcp-auth`'s control listener, which no operator-facing path called. The
strongest claim the design makes — that withdrawing a consent invalidates every
credential derived from it, including an exchanged token already issued and not
yet spent — was therefore reachable only from a test. The dashboard now lists
every grant with the client holding it and a live refresh-family count, and
revokes on one conditional write. Measured against the running platform:
the agent's next call answered `401` and its refresh answered `invalid_grant`.

**The consent page's `form-action` names the client's redirect origin, not just
`'self'` (O20).** This was carried as an open risk through Phase 0 because the
test harness drives `http.client`, which enforces no CSP. Driven in three real
engines it failed in two: Chromium and WebKit re-check `form-action` across the
302 the consent POST answers with, and refused to follow it; Firefox did not.

The failure mode is why this was worth closing rather than carrying. All three
engines delivered the POST *before* the block, so the grant was created every
time and only the authorization code was lost — the operator had consented, the
platform held a live grant, and the agent received nothing. A clean refusal
would have been better than success on one side and silence on the other.

The origin is derived from the pending authorization record, which is the
redirect the server has already matched exactly, so nothing a submission
proposes can widen the policy. An origin rather than the full URI, because CSP
path matching has its own rules and the path adds no protection over a value
the server already checked. A private-use scheme becomes a bare `scheme:`
source; anything unparseable yields nothing and leaves the policy at `'self'`,
which fails visibly rather than widening silently.

**A request after the handshake may omit `MCP-Protocol-Version`.** The
2025-06-18 specification requires clients to send it on every request after
`initialize`. Gemini CLI 0.60.0 sends it on none of them, so the guard refused
its `notifications/initialized` and the session never opened — transport
correct, ecosystem unreachable.

Admitting a header-less request is not a guess about which era it belongs to.
The modern revision carries its protocol version and client capabilities in
`params._meta` and requires the header to agree with them, so a request with no
header cannot satisfy the modern ladder at all: absence of the header *is* the
evidence that this is the handshake era. That property is asserted against the
real SDK (`ModernEraReachabilityTests`) rather than argued, because the whole
relaxation rests on it.

What it costs: the guard can no longer tell a client "you forgot the header",
and a malformed header is now the only header-shaped complaint it makes. What it
does not cost: a declared revision is still checked against the served set, a
malformed one is still refused, `initialize` is still held to the admitted
handshake list, and the modern era still requires both the header and the
envelope — measured together, not assumed.

**P2a — the server speaks the revisions the target ecosystems speak.** The
specification fixed `2026-07-28` and told Phase 0 to document any ecosystem that
could not reach it as unsupported rather than add a legacy transport path. Every
shipped client measured sits behind it:

| Revision | Era | Measured in |
| --- | --- | --- |
| `2026-07-28` | modern, per-request envelope | the specification's target |
| `2025-11-25` | handshake | Claude Code 2.1.272 |
| `2025-06-18` | handshake | Codex CLI 0.154.0, Gemini CLI 0.60.0 |

Under the original rule the three-ecosystem release gate could be met by no
client at all, which is not a line held but a gate made unreachable. P2 names
those three ecosystems and gates release on one pinned client from each, so the
governing decision is that all three are supported.

The amendment costs less than the rule assumed, which is why it is an amendment
rather than a deviation: the SDK already serves the handshake era, so admitting
a revision removes a refusal rather than adding a transport, and `stateless_http`
completes a full handshake session without minting a session identifier.

**The control is not the number of revisions but that the set is explicit and
enforced here.** `2024-11-05` and `2025-03-26` are served by the SDK and refused
by this guard, because no target ecosystem needs them. The SDK cannot be left to
make that distinction: it negotiates an `initialize` to whatever the client
offers, and an unrecognisable offer is counter-offered the newest handshake
revision rather than refused. The guard reads the offered revision out of the
body and admits only the listed set — so admitting these revisions made the
guard stricter than leaving the era refused-by-default would have been, because
the alternative was never "no handshake" but "whatever the SDK decides".

The list should shrink rather than grow: each entry exists for a client that has
not caught up, and should be removed when its ecosystem does. Authorization is
untouched by any of it — the guard runs before authentication and no credential,
audience or binding rule differs between the eras.

**O18 is resolved as state-based reconciliation.** Of the three options — a
two-phase response, background-submittable apply and reload, or reconciliation
— only reconciliation needs no new platform capability, and Phase 0 verified
the premise rather than assuming it. mapp-mcp treats a lost response as unknown
and re-reads state; it must never re-send a consequential effect. Phase 1 owes
the unambiguous proposal state that makes that decidable.

**O19 is resolved as "the flag is required."** Without a per-action eligibility
flag, adding an action to the manifest could silently fall inside an existing
standing-approval window, so a new high-risk action would inherit an approval
nobody gave it. The flag makes a window fail closed on anything it does not
name. Publication lands in Contract 1.7.

**Contract 1.7 stays closed until after Phase 0**, by decision. Only
`mcp:connect` is a genuinely new scope — `apply`, `derive`,
`federation:provision`, `semantic:apply` and `semantic:inspect` already exist
in the platform's token vocabulary — so P5's "hard cutover" is far smaller than
the specification implies.

**Multi-operator use is expected, so connection pooling becomes a Phase 1
requirement** rather than a revisit condition. The measured ceiling of 8 with
no pooling refuses the ninth caller cleanly and recovers on release, which is
adequate for one operator and not for several. Raising the limit without
pooling only moves the number.

**`recovery_epoch` is wired, in Phase 1, and the cost estimate that nearly
prevented it was wrong.** The
columns exist on eight tables. The estimate that deferred this said wiring them
meant an epoch predicate on 46 statements across two components — and that was
for the wrong design. Every credential read already filters on a revocation, so
the epoch only has to be applied *once, at restore*, by revoking what predates
it. Reads never change.

The remaining problem — rows inserted after an advance must carry the new epoch
— is solved by a function-backed column default rather than by passing it at
every INSERT, which is where the 46 statements came from. **Not one INSERT in
either component changed.**

Phase 1's tested list requires it outright: "a restore advances the recovery
epoch and cannot make a pre-restore grant/A mapping, refresh family or B
usable". That requirement was missed in the Phase 0 reading that recorded the
deferral. `./bin/mapp advance-recovery-epoch --confirm` is the entry point, and
it is step 5 of the restore procedure — before the stack starts, because a
pre-restore credential is usable until it has run.

The sweep invalidates every *live* credential rather than only those stamped
below the counter. A surviving mutation showed why: at sweep time nothing can
legitimately carry the new epoch, so an epoch predicate selects the same rows —
except when a *newer* snapshot is restored over an older database, where it
spares exactly the credentials from a state this database does not recognise.
The stamp records which restore era a credential was minted in; it is not the
filter.

**The audit table (P19) is deferred past Phase 1**, by decision. The audit log
stays append-only at `var/control/audit.jsonl` and does not share a transaction
with the authorization decision.

### Decisions taken while building the read surface

Recorded here rather than in the waves that made them, because each one binds
future work.

**`semantic:source` is a scope an operator may grant.** It was pinned as never
offered to agents, which made `semantic_source_relations` impossible rather than
merely unbuilt: one guard requires the dashboard to offer every scope an
allowlisted read needs and another forbids it offering a privileged one, so the
scope had to be reclassified or the tool could not exist. Reclassified on the
owner's decision, on the reasoning that it is disclosure and not authority — it
names the database's relation inventory, writes nothing, reads no row, and the
source aliases it exposes were already visible to `federation:observe`. It is
left out of every dashboard preset, so granting it stays deliberate.

**`operations.show` costs `derive`, not `inspect`.** The route admits any
authenticated credential and then demands the scope that the operation's *kind*
would have cost to perform — `visual` for a visual test, `apply` for a proposal
apply, `derive` for derived-layer work. An exchanged credential carries only
the single scope declared for its action, so `inspect` made the tool unusable
for every kind. `derive` was, at the time, exactly the set of operations an agent
could itself have caused.

Splitting `derive` in Phase 1 wave 1 ended that coincidence: performing
derived-layer work now costs `derive:manage`, while inspecting it still costs
`derive`. The classification stands on a different footing — inspecting an
operation is a read, and the derived-layer queue is already listed to any
`inspect` credential — but the original reasoning no longer applies and is
recorded here rather than left to look load-bearing. This is the first place where the
per-request credential's narrowness is in tension with a route whose required
scope depends on the resource being read; Phase 1 will meet it again.

**`sql.test` is classified `aggregate-data-read` and costs `derive`.** It
evaluates a caller-supplied expression against a configured layer's own
relation and returns a sample value, which is what `layers.values` already
does. A read risk rather than a write one because replaying it evaluates the
same expression and changes nothing.

**`tools/list` is filtered to what the grant can call.** Registration is not
filtered and `spend` still checks the scope on the way in, so this changes what
is described rather than what is permitted. Taken because a grant carrying
`mcp:connect` alone was shown all 37 tools and could invoke none of them, which
reads to a person as a broken server rather than a narrow grant. It fails
closed: with no caller in context, only the tool that reaches no platform route
is listed.

**Read tools strip the response envelope; mutating tools must not.** The
configuration API puts `operationId` in `meta` when a response carries an
asynchronous operation, so a blanket strip would remove the handle a mutating
tool needs to follow its own work. The filter is therefore on the read path and
not inside `spend`.

## Consequences

**The three-place routing problem does not apply.** Token B is a credential, not
a route, so the configuration API needed no changes inside its two large request
routers — the credential is recognised in the existing `Bearer` branch and spent
at the single authorization gate both routers already funnel through. Existing
scope enforcement applies unchanged and cannot be bypassed.

**`_required_scope`'s `return "full"` catch-all became a safety asset.** Any
route the scope table does not classify demands `full`, `full` is on the
broker's hard deny-list, so an unclassified route is refused by construction
rather than by an allowlist somebody has to maintain.

**The control role's connection limit is a ceiling, not headroom.** Neither
consumer pools — both open a connection per operation — so the limit of 8 bounds
concurrent control-plane operations. Measured: the ninth caller is refused by
PostgreSQL rather than queued, and the ceiling recovers as soon as a connection
is released. A connection-per-operation store also costs about 22 ms per spend,
which is where the introspection timing signal came from before it was
equalised. Pooling is the actual fix and belongs with the component.

**Two independent controls keep the internal surface internal.** The component's
route tables are disjoint and server-owned, so an `/internal/*` path is a 404 on
the edge listener regardless of the Caddy allowlist; and Caddy publishes only
four paths on the MCP origin. Both are asserted, in different suites.

## Rejected alternatives

| Alternative | Why rejected | Revisit when |
| --- | --- | --- |
| An off-the-shelf authorization server (Keycloak, Ory, Auth0) as issuer or broker | P1 forbids it: the restricted exchange, the operation binding and the grant-shaped revocation are not features these expose, and the platform must remain the canonical issuer | Never for the issuer role; an external identity provider federating *into* the platform issuer stays permitted |
| Sharing one canonicalization implementation between components | The specification requires independent evaluation at each boundary. A shared defect would produce a consistent wrong answer everywhere, which no amount of agreement between components would reveal | Packaging gains a shared, separately reviewed library *and* per-boundary tests remain |
| A global cap on parked authorization records | Measured worse than none — one unauthenticated caller denied every other user in ~24s | Never; the per-source bound supersedes it |
| Intersecting requested and permitted scopes in the exchange | Turns a widening attempt into a silently narrowed success the client cannot detect | Never |
| Registering agent clients automatically | An agent client is a third party asking for access and must be registered by a person. The configuration API's own client is provisioned automatically because it is a resource server, not a third party | Never for agents |
| Putting the exchange on `/oauth/token` | Would place the most security-critical surface on an edge-routed path | Never |
| A site-level Content-Security-Policy in Caddy for the MCP origin | Caddy's `header` directive *replaces* the upstream value, so a site-level CSP would discard the nonce-based policy the consent and login pages emit. Verified against Caddy 2.11.4 | Caddy gains append semantics for this directive |

## Open assumptions and accepted risks

| # | Item | Accepted because | Revisit condition |
| --- | --- | --- | --- |
| 1 | ~~No agent client can be registered~~ **Closed in Phase 0** | `./bin/mapp mcp-client-register` registers a public client, and the full flow is proven end to end against one | — |
| 2 | ~~The digest producer does not exist~~ **Closed** | `mapp-mcp` is built. It carries the third independent canonicalizer and the envelope builder, cross-checked against the other copies and against a pinned golden vector, and a real client has driven a digest-bound request end to end | — |
| 3 | Only one abuse budget exists | The per-grant exchange cap closes the one unbounded multiplier; P11's remaining budgets are provisional and measured in Phase 6 | Phase 6 for figures |
| 4 | Audit not transactional with the effect | Owner decision: deferred past Phase 1. The file store is durable and append-only | After Phase 1 |
| 5 | ~~`recovery_epoch` unimplemented~~ **Closed** | Wired in Phase 1, as the amendment above records: `control.current_recovery_epoch()`, a function-backed column default, `./bin/mapp advance-recovery-epoch --confirm` as step 5 of the restore procedure, and the sweep audited. Covered by 20 tests in `config-ui/tests/test_recovery_epoch.py` and by `mcp-auth/tests/test_recovery_epoch_effect.py`, which asserts the effect on the component that reads the credentials. This row contradicted the amendment for one revision | — |
| 6 | ~~O20 — `form-action 'self'` across the consent redirect~~ **Closed** | Measured in three engines and it failed in two: Chromium and WebKit re-check `form-action` across the consent redirect and blocked it, Firefox did not. The directive now names the client's own redirect origin, taken from the pending record. Re-measured: all three reach the callback | — |
| 7 | Connection ceiling of 8 with no pooling | Measured to refuse cleanly and recover. Multi-operator use is expected, so this is now a Phase 1 requirement rather than a risk to revisit | Phase 1 |
| 8 | ~~Client acceptance is 1 of 3~~ **Closed: 3 of 3** | Claude Code 2.1.272, Codex CLI 0.154.0 and Gemini CLI 0.60.0 have each connected to the running platform, listed the tools and called them, returning real aggregates through the exchange and the request binding. Each needed exactly one measured accommodation: 2025-11-25 for Claude, 2025-06-18 for Codex and Gemini, and a header-less post-handshake request for Gemini | — |
| 9 | Two threat-model rows postdate the owner's acceptance | Credential administration from a browser session and serving a second protocol era were added after the surfaces were built. Both are mitigated and recorded; neither has been through an acceptance decision | At the next acceptance review, before any public route |

| 10 | Safari is untested | WebKit, the engine Safari ships, was driven through Playwright and reproduced both the O20 failure and its fix. Safari proper needs macOS, which this project has no access to. Owner decision: a backlog nice-to-have, not a gate condition | If macOS becomes available, or a Safari-specific report arrives |

## References

- Evidence and gate status: [`mcp-phase0-evidence.md`](../mcp-phase0-evidence.md)
- Threat model: [`mcp-threat-model.md`](../mcp-threat-model.md)
- Component reference: [`mcp-authorization.md`](../mcp-authorization.md)
- Benchmark: `scripts/control_plane_benchmark.py`
