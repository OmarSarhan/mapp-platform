# Map performance (OMA-64): partial implementation

Inspected from clean HEAD `47d373770b92dd929b82fe3bbc26b18ce07abc22`.
No deployment, merge, framework patch, pool-budget change or pin update.

## Viewport counts (OMA-75)

`instance/public/plugins/viewport-layer-count/index.mjs` enforces single-flight
coalescing with debounced scheduling per layer:

- **Single-flight and coalesced debounce**: Each plugin instance keeps at most
  one active `mapp.ui.utils.locationCount(layer)` request and at most one
  pending follow-up refresh. Movement invalidates in-flight display results
  (`state.request++`) and resets the `viewport_layer_count.debounce` timer
  (clamped `>= 0`, default `250 ms`). Cancellation is not claimed to abort an
  already-dispatched XYZ request; existing active work finishes before another
  request starts.
- **Effective filter preservation**: Each refresh passes the live `layer`
  through the unchanged XYZ `locationCount` helper, preserving
  `layer.filter.default`, `layer.filter.current`, and `layer.filter.viewport = true`.
- **Hide/show and out-of-scale lifecycle**: Hiding cancels the debounce timer,
  drops pending work, invalidates any active result, and sets
  `badge.hidden = true`. Showing again while an earlier request is still in
  flight immediately restores the visible loading badge `(…)` and its
  accessible `aria-label` when countable while keeping at most one request in
  flight, suppressing the stale pre-hide response, and queuing the latest
  viewport refresh. Going out of scale (`tableCurrent?.() === false`) or
  invisible (`L?.getVisible?.() === false`) hides the badge immediately without
  queuing follow-up work and suppresses stale settled results.
- **Error recovery and accessibility**: Both synchronous throws and async
  rejections release the active-request lock in `finally` (allowing pending and
  subsequent refreshes to recover), suppress superseded errors, render `(–)`
  with `${layer.name}: viewport count unavailable` for current errors, and
  consistently reset `(…)` and `${layer.name}: counting features in viewport` on
  every loading transition.

Both test suites are wired into `.github/workflows/ci.yml` (`Check browser
runner`) and `./bin/mapp test` (Node container check).

Repeatable verification commands:

```sh
# Deterministic unit suite (7 tests) + loopback HTTP rapid-pan benchmark (auto-compares against git commit 47d37377 when available)
node --test browser-runner/tests/viewport-count.test.mjs browser-runner/tests/viewport-count-perf.test.mjs

# Strict baseline-comparison HTTP benchmark (fails if baseline commit 47d37377 cannot be extracted)
REQUIRE_BASELINE=1 node --test browser-runner/tests/viewport-count-perf.test.mjs

# Baseline unit regression check (expected failure: 6 of 7 tests fail on 47d37377, including 8 vs 2 calls in rapid-pan coalescing)
tmp=$(mktemp /tmp/oma64-baseline-count-XXXXXX.mjs)
git show 47d373770b92dd929b82fe3bbc26b18ce07abc22:instance/public/plugins/viewport-layer-count/index.mjs > "$tmp"
VIEWPORT_COUNT_MODULE="file://$tmp" node --test browser-runner/tests/viewport-count.test.mjs
rm -f "$tmp"
```

When run inside gitless containers (`./bin/mapp test`) or shallow CI checkouts,
`browser-runner/tests/viewport-count-perf.test.mjs` runs all current-plugin HTTP
invariants (`maxInFlightPerLayer <= 1`, bounded count traffic, effective filter
parameters, final badges, hidden-layer suppression, and bounded tile latency)
with zero skipped tests; pass `REQUIRE_BASELINE=1` (or
`BASELINE_VIEWPORT_COUNT_MODULE`) to require the baseline delta check.

### Measured baseline vs current loopback HTTP benchmark

Workload (`REQUIRE_BASELINE=1 node --test browser-runner/tests/viewport-count-perf.test.mjs`):
real loopback `node:http` server (`127.0.0.1`) with a shared FIFO worker pool
(`concurrency: 2`, `/count` = `70 ms`, `/tile` = `15 ms`), 4 layers (`roads`,
`stops`, `buildings`, `parcels`) carrying distinct `filter.default` and
`filter.current`, `debounce: 20 ms`, 6 rapid pans spaced `45 ms` apart, 4
concurrent `/tile` requests per pan (`24` tiles total), hiding `parcels` at pan
3 and mutating `roads.filter.current` at pan 4. Request counts and
`maxInFlightPerLayer` are deterministic; wall-clock latencies reflect a clean
isolated run and vary slightly with host scheduling.

| Metric | Baseline (`47d37377`) | Current (OMA-75) | Delta |
| --- | ---: | ---: | ---: |
| Total `/count` requests | 24 | 10 | -58.3% |
| Max in-flight `/count` per layer | 6 | 1 | `<= 1` enforced |
| Max total in-flight `/count` | 18 | 4 | -77.8% |
| Max backend worker queue depth | 36 | 17 | -52.8% |
| `/tile` latency mean (`n=24`) | 523.4 ms | 225.8 ms | -56.9% |
| `/tile` latency p50 (`n=24`) | 566.0 ms | 219.1 ms | -61.3% |
| `/tile` latency p95 (`n=24`) | 800.2 ms | 275.1 ms | -65.6% |
| `/tile` latency max (`n=24`) | 815.4 ms | 294.1 ms | -63.9% |
| `/count` latency mean | 515.4 ms | 190.1 ms | -63.1% |
| `/count` latency p95 | 846.4 ms | 288.8 ms | -65.9% |
| Total workload duration | 1437.4 ms | 751.2 ms | -47.7% |

All effective filter query parameters (`default`, `current`, `viewport`) were
validated on every `/count` request (`0` mismatches) and `parcels` recorded `0`
post-hide stale badge updates (`hidden === true`).

This benchmark exercises a constrained loopback `node:http` server rather than a
live XYZ + PostGIS stack or a headless Chromium browser. Live PostgreSQL/XYZ
tile timings and isolated candidate-preview browser evidence remain separate
deployment verification steps; OMA-75 is not live-stack acceptance-complete
without candidate-preview verification.

## Upstream dependencies (OMA-73 and OMA-74)

`git ls-remote https://github.com/GEOLYTIX/xyz.git HEAD` returned
`209f43eb2e7587f6d1e7b93296bae19922c56034` during this investigation.
The pinned v4.23.4 helper and that HEAD's relocated
`apps/xyz/mod/utils/dbs.js` both unconditionally destroy clients with
`release(true)` and separately issue `SET statement_timeout`. No supported fix
was identified in those inspected versions. HEAD also reorganizes the application;
it is not a drop-in pin upgrade. No other release or open PR was exhaustively audited.

Concrete upstream proposal: scope a client to each attempt; release exactly once;
return successful clients to the pool only after restoring a known session state;
destroy query, timeout-setting, cleanup and connection-error clients. Explicitly
test custom timeout followed by default timeout, retries after connection failure,
and session-mutating queries before enabling reuse. Keep the 20-connection pool
and runtime-reader role ceilings. Measure actual connection creations, statement
counts and tile/feature request latency with a real database before adopting a
verified full commit. Removing `release(true)` alone is not sufficient evidence
of safe session reuse. This proposal has not been submitted upstream.

Pinned `express.js` and current HEAD's `apps/xyz/server.js`/`router.js` install
the limiter without configuring Express proxy trust. Caddy forwards map traffic
to XYZ; the source therefore still predicts a shared proxy identity. This is
not an executed independent-client Caddy reproduction. Existing bounded tile
retry behavior and Caddy settings are unchanged.

Concrete upstream proposal: expose validated trusted-proxy addresses/subnets with
trust disabled by default; configure only the actual Caddy ingress, ensure XYZ
cannot be reached through an untrusted bypass, and verify Caddy replaces untrusted
forwarding input. Test two independent source addresses, forged X-Forwarded-For,
IPv6 identity, and direct ingress. Record 429/Retry-After separately from SQL and
connection errors and verify retries terminate. Do not use unrestricted proxy
trust or raise the rate limit as a substitute. Adopt only a supported upstream
commit after those checks; no local framework patch is proposed.

## Checks and limitations

Ran the focused Node unit suite (7/7 pass), the baseline unit regression check
(expected failure: 6/7 fail on `47d37377`), the loopback HTTP rapid-pan
benchmark (`REQUIRE_BASELINE=1`, pass against `47d37377`), gitless invocation
matching `./bin/mapp test`'s Node container (8/8 pass, 0 skipped), `bash -n
bin/mapp`, upstream source inspection, and `git diff --check`.

Limitations remain explicit: the HTTP benchmark uses an ephemeral loopback
`node:http` server with a synthetic bounded worker pool rather than a live XYZ
+ PostGIS stack. No live PostgreSQL fixture, Caddy independent-client isolation
experiment, live candidate-preview browser capture, full image build, or
end-to-end verifier was run; OMA-73 and OMA-74 upstream XYZ blockers remain
unchanged. Parent and children remain incomplete pending live candidate-preview
verification and those upstream dependencies.
