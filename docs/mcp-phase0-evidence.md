# MCP authorization — Phase 0 evidence bundle

Companion to [ADR 0001](adr/0001-mcp-authorization-and-topology.md) and the
[threat model](mcp-threat-model.md). This is the artefact the Phase 0 gate asks
for: what was built, what was measured, which gate items are satisfied, and a
go/no-go recommendation.

- **Branch:** `spike/mcp-phase0`, unmerged
- **Owner:** to be assigned at approval
- **Status:** submitted for go/no-go; **not approved**

Every figure below was produced from the tree rather than recalled. Where a
gate item is unsatisfied the row says so; several are, and the recommendation
depends on which.

## What was built

Seven milestones, M1 to M7. A new `mcp-auth` component and the configuration
API's validation of the credential it issues.

| Surface | Detail |
| --- | --- |
| Edge listener | 6 method/path routes across the 4 public paths Caddy publishes, over an `AF_UNIX` socket |
| Control listener | `/healthz`, `/internal/oauth/exchange`, `/internal/oauth/introspect`, `/internal/oauth/revoke`, `/internal/oauth/redeem` — `mcp-control` network only |
| Control schema | 12 tables, 4 migrations, owned by `mapp_control` |
| Canonicalization | `mapp-jcs-v1`, vendored independently into the broker and the configuration API |
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
| `mcp-auth/tests` | 361 | Zero skips with a control database attached |
| `config-ui/tests` | 935 | Includes the token-B validation, envelope and canonicalization-agreement suites |
| `scripts/tests` | 168 | Compose isolation, Caddy contract, production validation, benchmark invariants |

All three run in CI, each behind a `grep -q "skipped="` guard: a suite that
silently skips fails the job. That guard exists because a whole suite once went
unnoticed-dead.

**Mutation testing.** Every control introduced in the last three milestones was
mutation-tested, and the figure is recorded in each commit rather than totalled
here: 12 for the grant and introspection work (`9c030f0`), 30 for the audit
fixes and platform wiring (`1451d97`), and 26 for the operation binding
(`371b226`). All were caught. Six survived on first attempt; every one was a
weak test rather than weak code, and each is now covered.

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

## Gate status

The Phase 0 checklist, item by item. "Decided and designed" (P1–P12, P19–P21)
was already complete before this work and is unchanged except where the ADR
records an amendment.

### Implemented or prototyped

| Item | Status | Evidence |
| --- | --- | --- |
| Authorization-code issuance, A-to-B exchange, narrowing, introspection, revocation | **done**, with one amendment | The exchange *refuses* rather than narrows — deliberate, see ADR |
| Atomic one-time consumption, expiry cleanup, quota failure | **partial** | Consumption and cleanup done and measured. **Quotas do not exist** — no quota column anywhere in the schema |
| Independent canonicalization prototypes for mapp-mcp, broker and API | **2 of 3** | Broker and API each vendored and cross-checked. `mapp-mcp` does not exist |
| Target-client spikes (Claude, Codex, Gemini) | **not started** | No client work of any kind |

### Tested

| Item | Status | Evidence |
| --- | --- | --- |
| Token A/B audience separation, non-widening exchange, revocation propagation | **done** | All three pinned; revocation reaches an already-issued token B |
| Canonicalization golden vectors independently in three implementations | **2 of 3** | RFC vectors run against both copies, plus a copy-to-copy comparison |
| Benchmark the control schema under contention; record capacity, failure, recovery | **done** | Table above; `scripts/control_plane_benchmark.py` |
| Threat-model and abuse-case review | **done, unapproved** | [`mcp-threat-model.md`](mcp-threat-model.md) — 8 cases; 1 unmitigated, 1 unverified |

### Documented and reviewed

| Item | Status |
| --- | --- |
| ADR, threat model, decision log, client matrix, prototype findings approved | **partial** — ADR, threat model and findings exist and are unapproved; **no client matrix** |
| Open assumptions, accepted risks, rejected alternatives with revisit conditions | **done** — ADR, final two sections |

### Phase evidence and gate

| Item | Status |
| --- | --- |
| Phase 0 evidence bundle | **this document**; owner and date pending approval |
| Effort and sequencing re-estimated from prototype evidence | **done** — below |
| Phase 0 go/no-go approved | **not approved** |

## Open items Phase 0 can now speak to

**O18 — early acknowledgement.** The premise is now verified rather than
assumed. `_json` answers 239 of the configuration API's call sites as one
buffered write; the three paths that answer outside it serve artifacts, icons
and `OPTIONS`. There is no chunked, streaming or early-acknowledgement path
anywhere, and `recover_interrupted_operations` recovers operations but not
proposals. So for `proposals apply`, `xyz reload` and `derived drop` the
acknowledgement *is* the response that was lost. **Still open**, but the options
are now a design choice rather than an investigation.

**O19 — standing-window eligibility.** Unchanged. `ACTION_SCHEMAS` publishes one
`risk` and one `scope` per action (plus a `requiredScopes` array added for the
allowlist), and none of those expresses which action classes a standing approval
window may cover. **Still open.**

**O20 — `form-action 'self'` across the consent redirect.** **Still open, and
unclosable in this harness.** The suite drives `http.client`, which enforces no
CSP. Needs one manual check in Chromium, Firefox and Safari. If it fails, widen
the directive to name the registered redirect origins.

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

**Conditional go, for Phase 1 design work only. No merge, and no public route.**

The authorization design works against the real platform. The properties that
mattered are built and pinned: audience separation, a non-widening exchange,
grant-shaped revocation that reaches an already-issued credential, atomic
single-use consumption under real concurrency, and an operation binding that
confines a credential to one request. Phase 0's purpose was to find out whether
the design survives contact, and it did — with amendments the ADR records.

What it did not do is make anything usable. Three gate items are unsatisfied in
ways documentation cannot close:

1. **No agent client can be registered.** A correctly deployed component refuses
   every authorization request from an agent. This is the single blocker.
2. **No client acceptance work.** P2's matrix is untouched, and SDK support is
   not client acceptance. Nothing can be said about how Claude, Codex or Gemini
   behave against this component.
3. **The third canonicalization implementation is absent**, because `mapp-mcp`
   is absent. Two independent implementations agree; the specification's own
   reason for wanting three is that two might both be wrong in the same way.

Conditions on the go:

- Merge is gated on agent client registration existing, because until then the
  component cannot serve its purpose and merging it would put an unreachable
  surface in `main`.
- O20 must be checked manually in three browser engines before any public route.
- The ADR and threat model require approval by the platform, security and MCP
  owners. Two threat-model rows carry no mitigation (approval fatigue, client
  attestation); accepting those is an owner's decision, not an author's.
- Quotas and the transactional audit table are Phase 1 scope, not Phase 0 debt
  to be forgotten.

**No-go on Phase 1 implementation** until the ADR and threat model are approved,
which is what the gate says and what the first two conditions above make
material rather than procedural.

## Reproducing this

```bash
# Suites (a control database is required, or the guards fail the run)
export CONTROL_TEST_DATABASE_URL=postgresql://…/mapp_control_test
PYTHONPATH=mcp-auth  python -m unittest discover -s mcp-auth/tests
PYTHONPATH=config-ui python -m unittest discover -s config-ui/tests
PYTHONPATH=.         python -m unittest discover -s scripts/tests

# Capacity figures
export CONTROL_BENCHMARK_DATABASE_URL=postgresql://…/scratch
python scripts/control_plane_benchmark.py            # or --json
```

The scratch database needs an empty `control` schema, which a superuser creates
— the owning role deliberately cannot.
