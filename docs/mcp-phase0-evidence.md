# MCP authorization — Phase 0 evidence bundle

Companion to [ADR 0001](adr/0001-mcp-authorization-and-topology.md) and the
[threat model](mcp-threat-model.md). This is the artefact the Phase 0 gate asks
for: what was built, what was measured, which gate items are satisfied, and a
go/no-go recommendation.

- **Branch:** `spike/mcp-phase0`, unmerged
- **Owner:** to be assigned at approval
- **Status:** ADR and threat model **accepted by the owner** for this version.
  Phase 1 design work approved; implementation gated on the conditions below.
- **Revised 2026-09-16**, after `mapp-mcp` was built. The earlier revision was
  written while the runtime did not exist and said so in several places; those
  rows are now answered by measurement rather than deferred. What changed is
  listed under "What the runtime changed" below.

Every figure below was produced from the tree rather than recalled. Where a
gate item is unsatisfied the row says so; several are, and the recommendation
depends on which.

## What was built

Seven milestones, M1 to M7, and the Phase 1 work that followed them: rotating
refresh with a retry window, a durable audit trail, an operator client registry,
a working recovery epoch and a tested rollback ladder. A new `mcp-auth`
component and the configuration API's validation of the credential it issues.

| Surface | Detail |
| --- | --- |
| Edge listener | 6 method/path routes across the 4 public paths Caddy publishes, over an `AF_UNIX` socket |
| Control listener | `/healthz`, `/internal/oauth/exchange`, `/internal/oauth/introspect`, `/internal/oauth/revoke`, `/internal/oauth/redeem` — `mcp-control` network only |
| MCP runtime | `mapp-mcp`: the official Python SDK behind a protocol-era guard, two read-only tools, RFC 9728 metadata, token-A authentication per call |
| Control schema | 15 tables, 8 migrations, owned by `mapp_control` |
| Canonicalization | `mapp-jcs-v1`, vendored independently into the broker, the configuration API and the MCP runtime — three copies, cross-checked |
| Credential administration | Dashboard: agent-client issuance and consent inspection and revocation, both audited, administrator-session only |
| Operation allowlist | 5 of the platform's 52 actions |
| Scope vocabulary | 9 accepted, derived from the allowlist plus the MCP scopes; 2 advertised in metadata |

Public paths on the MCP origin: `/.well-known/oauth-authorization-server`,
`/oauth/authorize`, `/oauth/token`, `/oauth/login`. Everything else 404s.

Accepted scopes: `apply`, `derive`, `federation:provision`, `inspect`,
`mcp:connect`, `propose`, `semantic:apply`, `semantic:inspect`, `visual`.
Derived from the operation table rather than restated, after the two disagreed
and four of the five allowlisted operations turned out to require scopes the
issuer refused to issue.

## Test evidence

| Suite | Tests | Notes |
| --- | --- | --- |
| `mcp-auth/tests` | 473 | Zero skips with a control database attached. Includes the registered-client spike and the control-listener capability separation |
| `config-ui/tests` | 1049 | Token-B validation, the canonical envelope, canonicalization agreement, and the agent-client registry |
| `mapp-mcp/tests` | 118 | The protocol-era guard, RFC 9728 metadata, token-A authentication, three-way canonicalization agreement, and a complete legacy session driven through the composed application. No database and no network: driven over the ASGI contract, where the obligations live |
| `scripts/tests` | 182 | Compose isolation, Caddy contract, production validation, benchmark invariants |

The three container suites run in CI behind a `grep -q "skipped="` guard: a
suite that silently skips fails the job. That guard exists because a whole suite
once went unnoticed-dead. `scripts/tests` carries no such guard and skips 22 on
a host without Docker-in-Docker, which is a gap in the same shape as the one the
guard was written for.

**Mutation testing.** Every control introduced in the last three milestones was
mutation-tested, and the figure is recorded in each commit rather than totalled
here: 12 for the grant and introspection work (`9c030f0`), 30 for the audit
fixes and platform wiring (`1451d97`), 26 for the operation binding
(`371b226`), 11 for the client registry and abuse budget, 8 for the refresh
wiring, 8 for the retry window and 8 for the audit trail. All were caught. Six survived on first attempt; every one was a
weak test rather than weak code, and each is now covered. The most recent:
deleting the `FOR UPDATE` that stops two simultaneous retries forking a refresh
family survived, because the general concurrency test never overlapped two
retries. A test that has every thread present the same spent token catches it
in all four rounds.

The instructive case: a test named for refusing lower-case percent-escapes used
`%2f`, which the *separator* rule refuses, so it passed with the hex-case check
deleted. A test can be green for the wrong reason, and only mutation shows it.

## Measured capacity

Produced by `scripts/control_plane_benchmark.py` against PostgreSQL 17. Figures
are machine-dependent; the invariants are not, and the script asserts them.

| Scenario | Result | Invariant |
| --- | --- | --- |
| Replay — 16 simultaneous presenters, 20 trials | exactly 1 winner every trial; median 55 ms, max 75 ms | exactly one winner |
| Exchange throughput — 8 workers, 200 distinct tokens | 345 spends/s; median 22 ms, p95 30 ms; 0 failures | each token spends once |
| Approval admission — per-source flood | 64 admitted, 10 refused, a second source unaffected | a flood denies only the flooder |
| Cleanup under load — 16 writers, 4 sweeps | 100 expired removed, 116 live survived, max 148 ms, no deadlock | no live record removed |
| Connection ceiling — role capped at 8 | 8 opened; the 9th refused `FATAL: too many connections for role`; recovers on release | refuses and recovers |

Two readings worth carrying forward. **22 ms per spend** is the
connection-per-operation cost: the store opens a connection per call, which is
also where the introspection timing signal came from before it was equalised.
And the connection limit is a **ceiling, not headroom** — the ninth caller is
refused rather than queued.

## What the runtime changed

The previous revision was written with the authorization half built and the MCP
runtime absent. Four things are answered now that were deferred then, and each
was found by putting a real client in front of the real server rather than by
reasoning about either.

**The transport era had to be amended (P2a).** The specification fixed
`2026-07-28` and told Phase 0 to document any ecosystem that could not reach it
as unsupported rather than add a legacy path. Claude Code 2.1.272 opens with
`initialize` offering **2025-11-25** and no protocol-version header, so under
the original rule the three-ecosystem release gate could be met by no shipped
client at all. The amendment costs less than the rule assumed: the SDK already
serves the handshake era, so admitting it removed a refusal rather than adding a
transport, and `stateless_http` completes a full legacy session without minting
a session identifier — so the no-session obligation survived intact.

Admitting it made the guard *stricter*. Measured against the installed SDK, an
`initialize` is negotiated to whatever the client offers — 2024-11-05 verbatim,
and an unrecognisable offer counter-offered 2025-11-25 — so letting the method
through would have admitted four revisions and a fallback. The guard now reads
the offered revision out of the body and refuses everything that is not the one
admitted handshake revision.

**A real client found a defect no unit test could.** `layer_values` raised
`ValueError` for every anticipated refusal. The SDK treats only `ToolError` as
anticipated and puts its text in the result the model reads; everything else is
a crash, replaced with `Error executing tool layer_values` and logged at ERROR.
So every message written to be acted on — which scope to ask for, which platform
code refused — was discarded before reaching the caller, and routine scope
refusals were logged as crashes. The unit tests passed throughout, because they
call the registered function and assert on the exception it raises: true of the
function, and silent about what crosses the wire.

**Credential administration moved to the dashboard.** Registering an agent
client was an operator command, so the person who runs the platform and the
person who runs the agent had to be the same person. An administrator now issues
a client from Access and audit and is given the configuration to hand over, and
the consents that client holds are listed and revocable on the same panel.
Before this, `mcp-auth` could revoke a grant through its control listener and
nothing an operator could reach ever called it — the design's strongest claim
was reachable only from a test.

**The claims about withdrawal are now measurements.** Revoking a consent from
the dashboard left the agent's next call answering `401` and its refresh
answering `invalid_grant`. Disabling a client refused a refresh carrying an
unspent token while a live token A kept working for the remainder of the
runtime's 30-second introspection cache — measured at 4 seconds, not the
15-minute token lifetime. That second figure was initially recorded the other
way round, from a reading taken inside the cache window; the correction is in
the spike plan, because understating a control the design depends on is the more
dangerous error.

## Gate status

The Phase 0 checklist, item by item. "Decided and designed" (P1–P12, P19–P21)
was already complete before this work and is unchanged except where the ADR
records an amendment.

### Implemented or prototyped

| Item | Status | Evidence |
| --- | --- | --- |
| Authorization-code issuance, A-to-B exchange, narrowing, introspection, revocation | **done**, with one amendment | The exchange *refuses* rather than narrows — deliberate, see ADR |
| Atomic one-time consumption, expiry cleanup, quota failure | **done** | Consumption and cleanup measured under contention. The per-grant exchange budget refuses with `slow_down` past 60 in 60 seconds; P11's remaining budgets are Phase 6 |
| Independent canonicalization prototypes for mapp-mcp, broker and API | **3 of 3** | All three vendored and cross-checked against each other over a corpus chosen to drift, on what they emit *and* on what they refuse. `mapp-mcp` also carries the envelope builder, checked against the configuration API's copy and against the pinned golden vector |
| Target-client spikes (Claude, Codex, Gemini) | **3 of 3 complete** | Claude Code 2.1.272 has connected to the running platform and used it: discovery, consent, token, `tools/list`, and `layer_values` returning real aggregates through the exchange and the request binding. Driven from an ephemeral container against the deployed stack, not a harness. The earlier revision recorded this row as blocked "because `mapp-mcp` does not exist"; it exists. Two things the run established that reading documentation had not: the shipped client speaks **2025-11-25 only**, which forced decision P2a, and it accepts a static `Authorization` header, which is how a headless test supplies a token without its OAuth store. `mcp-auth/tests/test_registered_client.py` still drives the authorization column against the real component. **Codex CLI 0.154.0** has since done the same: connected, listed the tools and called `layer_values`, returning real aggregates through the exchange and the request binding. **Gemini CLI 0.60.0** likewise: it offers `2025-06-18` and, uniquely, sends no `MCP-Protocol-Version` on anything after `initialize` — which its own specification requires — so the guard's header requirement was relaxed for the handshake era before it could connect. It then listed both tools and called them |

### Tested

| Item | Status | Evidence |
| --- | --- | --- |
| Token A/B audience separation, non-widening exchange, revocation propagation | **done**, and now also measured on the deployed stack | All three pinned by test. Against the running platform: a token A presented to the configuration API is refused `401`; revoking a consent from the dashboard left the agent's next call answering `401` and its refresh answering `invalid_grant`; disabling a client refused a refresh carrying an *unspent* token. The residual window on a live token A is the runtime's positive introspection cache, bounded at 30 seconds — measured at 4 |
| Canonicalization golden vectors independently in three implementations | **3 of 3** | RFC vectors run against all three copies, plus cross-copy comparison. One *envelope* digest is pinned literally and recomputed independently by both builders, which is what differential agreement alone could not give: two copies wrong in the same way agree perfectly |
| Benchmark the control schema under contention; record capacity, failure, recovery | **done** | Table above; `scripts/control_plane_benchmark.py` |
| Threat-model and abuse-case review | **done; 8 cases accepted, 2 recorded and not yet accepted** | [`mcp-threat-model.md`](mcp-threat-model.md) — 10 cases; 1 unmitigated, 1 unverified. The two added after the runtime and dashboard surfaces were built (credential administration from a browser session, serving a second protocol era) postdate the owner's acceptance |

### Documented and reviewed

| Item | Status |
| --- | --- |
| ADR, threat model, decision log, client matrix, prototype findings approved | **partial** — ADR and threat model **accepted for this version**, with two unmitigated rows carried knowingly. The client matrix covers 1 of 3 ecosystems |
| Open assumptions, accepted risks, rejected alternatives with revisit conditions | **done** — ADR, final two sections |

### Phase evidence and gate

| Item | Status |
| --- | --- |
| Phase 0 evidence bundle | **this document** |
| Effort and sequencing re-estimated from prototype evidence | **done** — below |
| Phase 0 go/no-go approved | **conditional go** for Phase 1 design; implementation gated below |

## Open items Phase 0 can now speak to

**O18 — early acknowledgement. RESOLVED: state-based reconciliation is the only
path.** Of the three options, only reconciliation needs no new platform
capability. mapp-mcp treats a lost response as unknown and re-reads state; it
must never re-send a consequential effect. Phase 1 owes the unambiguous
proposal state that makes that decidable. The premise was verified rather than
assumed: `_json` answers 239 of the configuration API's call sites as one
buffered write; the three paths that answer outside it serve artifacts, icons
and `OPTIONS`. There is no chunked, streaming or early-acknowledgement path
anywhere, and `recover_interrupted_operations` recovers operations but not
proposals. So for `proposals apply`, `xyz reload` and `derived drop` the
acknowledgement *is* the response that was lost.

**O19 — standing-window eligibility. RESOLVED: the flag is required.** Without
it, adding an action to the manifest could silently fall inside an operator's
existing standing window, so a new high-risk action would inherit an approval
nobody gave it. The flag makes a window fail closed on anything it does not
name. Publication lands in Contract 1.7, which is deferred past Phase 0 by
decision; Phase 0 built no standing approval, so nothing depends on it yet.

**O20 — `form-action 'self'` across the consent redirect. RESOLVED: it failed,
and the directive is widened.** Unclosable in the unit harness, which drives
`http.client` and enforces no CSP, so it was driven headlessly in Chromium,
Firefox and WebKit against the running platform.

| Engine | Before | After |
| --- | --- | --- |
| Chromium | blocked the redirect | reaches the callback |
| Firefox | followed the redirect | reaches the callback |
| WebKit | blocked the redirect | reaches the callback |

Two of three blocked it, which is the answer the item existed to get. The
failure mode matters more than the count: every engine delivered the consent
POST *before* blocking, so the grant was created each time and only the
authorization code was lost. The operator had consented, the platform held a
live grant, and the agent received nothing — worse than a clean refusal, because
one side looks like success and the other like silence. Three grants per run,
one per engine, confirmed in `control.oauth_grants`.

`form-action` now names the client's own redirect origin, derived from the
pending authorization record — the redirect the server has already matched
exactly, so no submission can widen it. Safari itself was not driven; WebKit,
the engine it ships, was, through Playwright on Linux.

## Re-estimate

The plan estimated M1–M8 at roughly 12 days. Actual shape, with the drivers:

| Milestone | Planned | What actually dominated |
| --- | --- | --- |
| M1–M3 | ~4 days | Close. The `AF_UNIX` overrides and the disjoint route tables were as scoped |
| M4 | ~3 days | Larger. The file-to-SQL move touched 11 demo and end-to-end call sites, and the bootstrap had to move into a container |
| M5–M6 | ~3 days | Larger. Grants were not in the plan as a table; the design needed them before revocation could mean anything |
| M7 | ~1.5 days | **Much larger.** The plan scoped a client and two edits. The canonical envelope, a redeem endpoint and client provisioning were all load-bearing and unscoped |
| M8 | — | The benchmark was the only code; the documents were the work |

**Revised Phase 1 estimate:** the plan's 20–32 days should be read as its upper
half. Two things it did not account for: agent client registration is a
deliverable in its own right, and the audit table that P19 requires has to
commit with the effect, which the current file-backed audit does not.

## Recommendation

**Conditional go. Phase 1 design work approved. No public route.**

**Owner decision, 2026-09-16: go.** Phase 1 design work approved, and the
conditions below accepted as recorded — refresh semantics as built, P2a's two
revisions, dashboard credential administration, and the two threat-model rows
added this revision. Merge approved with one condition: **the MCP feature stays
off `main`.** That condition has two possible readings and is recorded here
verbatim rather than interpreted — see the merge note below.

Updated twice. First after the owner's decisions, when agent client
registration closed the merge blocker. Again on 2026-09-16, after `mapp-mcp` was
built: the authorization flow is no longer proven only for an operator-registered
client against the component, but for a real MCP client against the running
platform, end to end.

The authorization design works against the real platform. The properties that
mattered are built and pinned: audience separation, a non-widening exchange,
grant-shaped revocation that reaches an already-issued credential, atomic
single-use consumption under real concurrency, and an operation binding that
confines a credential to one request. Phase 0's purpose was to find out whether
the design survives contact, and it did — with amendments the ADR records.

**One** gate item remains unsatisfied in a way documentation cannot close, and
it does not block design work:

1. ~~**Client acceptance is 1 of 3.**~~ **Closed: 3 of 3.** Claude Code 2.1.272,
   Codex CLI 0.154.0 and Gemini CLI 0.60.0 have each connected to the running
   platform, listed the tools and called them, returning real aggregates through
   the exchange and the request binding. Not SDK compatibility — the clients
   themselves, driven from ephemeral containers against the deployed stack.

   Each cost exactly one measured accommodation, and none was predictable from
   documentation:

   | Ecosystem | Client | Needed |
   | --- | --- | --- |
   | Claude | Claude Code 2.1.272 | `2025-11-25` admitted |
   | Codex/OpenAI | Codex CLI 0.154.0 | `2025-06-18` admitted |
   | Gemini | Gemini CLI 0.60.0 | `2025-06-18`, plus a header-less post-handshake request |

   The pattern held three times out of three: **no shipped client speaks the
   specification's target revision**, and one of them does not follow the
   handshake specification it does speak. A transport rule written from the
   specification alone would have supported none of them.

The previous revision listed a second item — the absent third canonicalization
implementation — on the grounds that `mapp-mcp` did not exist. It does, it
carries its own `canonical.py` and envelope builder, and
`mapp-mcp/tests/test_envelope_agreement.py` checks all three copies against each
other and against a pinned golden vector. The gate table above already recorded
this as 3 of 3 while the recommendation still called it absent; the two said
different things about the same row for one revision, which is the sort of drift
this document exists to prevent.

Conditions carried into Phase 1:

- ~~**O20 must be checked manually in Chromium, Firefox and Safari**~~
  **Closed 2026-09-16, and it failed before it passed.** Driven headlessly in
  all three engines against the running platform: `form-action 'self'` did not
  survive the consent redirect in Chromium or WebKit, and every engine delivered
  the POST first — so the grant was created and only the code was lost. The
  directive now names the client's own redirect origin and all three reach the
  callback.

  **Safari itself is a backlog item, by owner decision.** WebKit, the engine it
  ships, was driven through Playwright on Linux and behaved identically to the
  failure and the fix. Driving Safari proper needs macOS, which this project has
  no access to; it is recorded as a nice-to-have rather than a gate condition.
- **Connection pooling is a Phase 1 requirement**, not a revisit condition,
  because multi-operator use is expected and the ceiling of 8 is a ceiling.
- **Phase 1 owes the unambiguous proposal state** that O18's reconciliation
  answer depends on, and the per-action eligibility flag O19 requires, both in
  Contract 1.7.
- **Two threat-model rows stay open by decision**: approval fatigue entirely,
  client attestation in part. Each has a revisit condition in the ADR.
- **Deferred by decision, and not to be forgotten:** Contract 1.7 until after
  Phase 0, and the transactional audit table until after Phase 1.
- **Closed since this was written:** restore-time credential invalidation.
  `recovery_epoch` is no longer reserved storage. It is wired, audited, covered
  by 20 tests in `config-ui` and by an effect test in `mcp-auth`, and
  `./bin/mapp advance-recovery-epoch --confirm` is step 5 of the restore
  procedure. This condition, the ADR's accepted-risk row and the threat model
  all still described it as deferred; the ADR's own amendment section did not,
  so the documents disagreed with each other and with the code.

**Merge.** The single argument for keeping this on the branch was that
`mapp-mcp` did not exist, so nothing in `main` would use it. That argument has
lapsed: the runtime exists, and a real client uses it. The branch now carries 58
commits that `main` does not, including the platform's only MCP surface and the
only dashboard route to revoke a consent, and every suite is green.

What still argues for care is not the code but the blast radius of the schema:
merging brings eight control-plane migrations and the `mapp_control` role onto
`main`. One operator-facing message is already out of step and was corrected
here — `./bin/mapp reset-system --confirm` destroys the packaged volume, which
now also holds every registered agent client and every MCP consent, while its
warning enumerated only dashboard authentication, CLI API tokens and device
authorizations. The recovery-epoch command names grants; the reset did not, so
the two disagreed about what the database contains.

The owner has approved merging with the condition that **the MCP feature stays
off `main`**. That admits two readings, and they are not close:

1. **Merge the code, keep the feature dormant.** Everything lands on `main`, and
   `mapp-mcp` and `mcp-auth` do not start, are not routed, and are not reachable
   unless explicitly enabled. `main` carries and tests the code; the running
   product is unchanged.
2. **Merge only what is not MCP.** The MCP components stay on the branch
   entirely. Since nearly every commit here is MCP work, this merges very
   little — mainly the reset-system warning fix and the recovery-epoch work.

Reading 1 keeps the benefit the merge was argued for, which is that the code
stops being maintained against a branch nobody else runs. Reading 2 keeps `main`
free of the control schema and the `mapp_control` role entirely. **Not yet
resolved; the decision is the owner's and this document does not assume one.**

Either way the merge preparation is the same: confirm the migration ladder
applies to an existing volume, and keep the "no public route" condition.

## Reproducing this

```bash
# Suites (a control database is required, or the guards fail the run)
export CONTROL_TEST_DATABASE_URL=postgresql://…/mapp_control_test
PYTHONPATH=mcp-auth  python -m unittest discover -s mcp-auth/tests
PYTHONPATH=config-ui python -m unittest discover -s config-ui/tests
PYTHONPATH=.         python -m unittest discover -s scripts/tests

# The MCP runtime needs the SDK, so it runs inside its own image rather than
# on the host. No database and no network.
docker run --rm -v "$PWD:/workspace:ro" \
  -e PYTHONPATH=/app:/workspace/mapp-mcp/tests \
  mapp-mcp:local python -m unittest discover -s /workspace/mapp-mcp/tests

# The dashboard's own suite
cd config-ui && npm ci && npm test

# Capacity figures
export CONTROL_BENCHMARK_DATABASE_URL=postgresql://…/scratch
python scripts/control_plane_benchmark.py            # or --json
```

The scratch database needs an empty `control` schema, which a superuser creates
— the owning role deliberately cannot.
