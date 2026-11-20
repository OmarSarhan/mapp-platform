# OMA-76 integrated validation checkpoint

**NOT READY for final acceptance.** The OMA-124 runtime and measurement gate
passed on `632d04a228406c87aa0456f7a8654b6e846e432c` on 2026-11-20.
This supersedes the initial runtime evidence, not the outstanding review and
deployment gates. The original `auth-admission.md` remains historical.

The independent validator used Python 3.14.4, psycopg 3.3.4 and PostgreSQL
18.6 in a new Unix-only cluster on port 55486. No production environment,
deployment, source data or live workspace was changed.

## Executed checks

Config passed 1,343 tests; OAuth passed 516; MCP passed 481 of 482, with one
skip requiring the adjacent standalone `mapp-config-cli` checkout. Both opt-in
real Codex client cancellation tests ran. Semantic service passed 64 tests,
database-access contracts 29, and benchmark/preflight 17. The default benchmark
also passed with a non-superuser capped at eight connections. These counts
are the validator's results, not an additional parent rerun.

Complete HTTP/MCP fixtures established downstream waiter removal before held
capacity was released, cumulative cold-hop deadlines, structured overload
responses, fresh-call recovery, and recorded receipt/publication outcomes.
Six service sessions left two diagnostic sessions; PostgreSQL refused the
ninth and admitted a replacement after release. Session reset, transaction
failure and cancellation ownership checks passed.

An observer-confirmed 300 ms PostgreSQL lock hold recorded 310 ms reader,
312 ms configured-generation and 315 ms derived-generation database lease
time. Queue time remained below 0.023 ms across the six 100/300 ms checks.
Late connectors retained admission through delayed close and executed zero
tracked borrower SQL. Fresh requests recovered.

## Complete MCP wire measurements

Separate MCP, config and OAuth processes ran two fresh-fixture series.
Session and statement counts came from PostgreSQL, including setup/reset SQL.
Queue and database lease time below are aggregate seconds across overlapping
workers, not wall-clock latency or SQL CPU time.

| Path | Successes, runs 1 / 2 | New sessions | Statements, runs 1 / 2 | Queue seconds | Database lease seconds |
| --- | --- | --- | --- | --- | --- |
| Cold, one call | 1 / 1 | 2 | 63 / 63 | 0.00025–0.00052 | 0.02961–0.03824 |
| Warm, one call | 1 / 1 | 0 | 50 / 50 | 0.00017–0.00019 | 0.01582–0.04670 |
| Warm, eight calls | 8 / 8 | 4 | 400 / 400 | 0.29793–0.34029 | 0.49447–0.61117 |
| Warm, 32 calls | 24 / 14 | 0 | 1248 / 800 | 2.53632–2.89606 | 1.53284–2.38116 |

The 26 refusals across the two largest bursts comprised 25
`auth.exchange_unavailable` and one `database.busy`. All were JSON-RPC tool
errors inside HTTP 200, with `retryable=true` and `indeterminate=false`.
Both bursts recovered on the next request. These two runs do not establish
production throughput.

Preserved historical whole-service wire evidence at `47d3737` recorded cold
19 sessions/41 statements and warm 14 sessions/30 statements. It was not
rerun; historical wire queue and database timings are unavailable. Safe
connection reset adds statements while reuse reduces connection creation.

## Evidence and limits

The sanitized local evidence bundle is
`/tmp/oma124-evidence-Wd9YejXW.tar.gz`. Its directory contains `report.txt`,
`exact-commands.txt`, `suites.json`, both `wire-load.json` runs,
`held-lock.json`, `wire-budget.json`, `outcomes.txt`, and `cleanup.txt`.
Exact commands include disposable fixture setup; do not copy their DSNs to
an existing database. Temporary wire-fixture copies changed only private
endpoint validation and source-root lookup. The repository stayed clean.

Outstanding acceptance limits:

- The literal launcher zero-skip gate is unmet without the adjacent CLI
  checkout. The Docker launcher itself was not run.
- OMA-114's independent review was blocked by the review service before
  findings. This runtime gate did not retry or replace that review.
- The catalog used fixture PostGIS signatures, reload used a fixture
  supervisor acknowledgement, and generation used a provider stub.
- Full Leeds/XYZ deployment, browser behavior and the pinned Alpine image
  remain unverified. Required images and representative source extensions
  were absent; no builds or downloads were performed.
- Config emitted psycopg resource warnings at interpreter exit. Teardown
  nevertheless verified zero other client backends, removed fixture
  databases and roles, stopped the cluster and removed raw logs.

No merge, push, deployment or production-readiness claim follows from this
checkpoint. OMA-76 and its parent must remain open.
