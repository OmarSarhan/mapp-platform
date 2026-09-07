# ADR 0001 — MCP authorization and deployment topology

- **Status:** accepted for Phase 0. The owner accepted this record and the
  threat model as functional and safe for this version, with the two
  unmitigated threat-model rows (approval fatigue in full, client attestation
  in part) knowingly carried as accepted risks. Phase 1 implementation remains
  gated on the conditions in the evidence bundle.
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

**The canonicalizer is vendored twice, not shared.** The components ship as
separate images with no shared package, and the specification asks for each
trust boundary to evaluate the scheme independently so a defective shared helper
cannot make a bad digest look correct everywhere. Each copy runs the RFC's own
vectors, and a further test compares the copies directly.

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

**Agent clients are registered by an operator command, not an endpoint.**
`./bin/mapp mcp-client-register` writes a *public* client — PKCE and no secret,
because an agent is a native or desktop application that cannot keep one, and
issuing a secret would create a credential that has to live on the operator's
machine unprotected. Redirect URIs are validated at registration (absolute, no
fragment, no userinfo, https unless loopback) because the authorization server
matches them exactly and a permissive entry is a working way to have
authorization codes delivered elsewhere. `full` and `admin` are refused
outright. RFC 7591 dynamic registration is rejected rather than deferred: P2
requires one *pinned* client per ecosystem, and a person decides which agent
may ask for consent.

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
| 2 | The digest producer does not exist | `mapp-mcp` is Phase 1. The verifying boundary is built and proven against the broker over real HTTP | Phase 1; the third independent canonicalizer is required at that point |
| 3 | Only one abuse budget exists | The per-grant exchange cap closes the one unbounded multiplier; P11's remaining budgets are provisional and measured in Phase 6 | Phase 6 for figures |
| 4 | Audit not transactional with the effect | Owner decision: deferred past Phase 1. The file store is durable and append-only | After Phase 1 |
| 5 | `recovery_epoch` unimplemented | Owner decision: a nice-to-have, not Phase 1 scope. No restore-invalidation control is claimed anywhere in operation | End of project, or the first time a restore has to preserve a revocation |
| 6 | O20 — `form-action 'self'` across the consent redirect | The Phase 0 harness drives `http.client`, which enforces no CSP, so this is unverifiable by construction in that harness. Deferred by the owner more than once, deliberately: the flow is otherwise proven end to end | Before any public route. One manual check in Chromium, Firefox and Safari; if it fails, widen the directive to name the registered redirect origins |
| 7 | Connection ceiling of 8 with no pooling | Measured to refuse cleanly and recover. Multi-operator use is expected, so this is now a Phase 1 requirement rather than a risk to revisit | Phase 1 |
| 8 | Client acceptance is 1 of 3 | The authorization column is proven for one client against the real component. Codex/OpenAI and Gemini are untested, and SDK support is not client acceptance | Blocking for release |

## References

- Evidence and gate status: [`mcp-phase0-evidence.md`](../mcp-phase0-evidence.md)
- Threat model: [`mcp-threat-model.md`](../mcp-threat-model.md)
- Component reference: [`mcp-authorization.md`](../mcp-authorization.md)
- Benchmark: `scripts/control_plane_benchmark.py`
