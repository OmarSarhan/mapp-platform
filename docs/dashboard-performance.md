# Dashboard responsiveness (OMA-63)

Current verification status: the allocation-profile gap described in the historical
entries below is resolved by the isolated CDP profiling run at the end of this report.

Implemented against upstream `5ac990aa8a8059c99314d2f56caf64bc3387fbbe`,
confirmed with `git ls-remote origin HEAD` on 19 November 2026.

Discovery runs only while the workspace area and document are visible. Returning
to the area or tab refreshes it; explicit retries remain available. Each resource
has an in-flight guard. Federation and derived mutations invalidate discovery;
an invalidation during a request queues one fresh snapshot. Inactive consumers
refresh on return. Failed discovery retains its previous successful result.
The catalogue route shares one derived-profile snapshot with its filtering step.

Effective locales are memoized by workspace identity. Search, selection, status,
polling and locale switches reuse that result. The default-locale helper no longer
constructs named locales. Field edits copy the workspace/locale/layer-map path and
clone only the edited layer. Broad workspace mutations retain their existing clone
path. XYZ merge rules and named-locale read-only boundaries are unchanged.

Semantic and access resources publish independently, with resource-specific
loading and failure messages. Mutation refreshes name their affected collections;
audit refresh does not hold the mutation controls busy. Proposal creation retains
the semantic draft while refreshing proposals; affected catalog refreshes clear
version-bound draft/review state. Server permissions and revision checks are unchanged.

## Repeatable checks

From the repository root:

```sh
node --test config-ui/src/api.test.js
node config-ui/perf-locale.mjs
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=config-ui:config-ui/tests \
  /root/.t3/projects/mapp-platform-discussion/.mapp-tests/final-fixes-venv/bin/python \
  -m unittest test_catalog_snapshot test_app_routes.CatalogDiscoveryTests
cd config-ui
npx vitest run --maxWorkers=1
npm run build
```

Results: 7 API helper tests, 154 component tests, and 5 focused backend tests
passed. The initial parallel component run passed 149 tests and timed out in
one existing 26-field selection test; the serial run passed. After final semantic
state cleanup, its suite plus the new discovery suite passed all 26 tests.
Final dashboard/discovery checks passed 88 tests; the four discovery regressions
also passed after adding the exact token-revocation refresh-count assertion.
The build passed with dependency `use client` directive warnings.
`git diff --check` passed.

The system `python3` lacks psycopg; the existing discussion-workspace test virtual
environment was reused. `npm ci --offline --ignore-scripts --no-audit --no-fund`
could not find cached xmlchars. The same command without `--offline` installed
the existing lockfile's 102 packages. npm warned that Node 22.22.1 is below jsdom's
declared 22.22.2 minimum. No dependency manifests changed.

## Measurements and limits

The pre-change timers schedule 18 requests/minute across three resources,
excluding initial loads and browser throttling. The fake-timer regression advances
60 seconds: inactive and hidden states issue zero automatic requests; a slow
visible resource issues only one in-flight request across all six timer ticks.
The route regression verifies one profile enumeration, including an empty snapshot.

The helper probe uses 100 and 1,000 layers, 20 information fields per layer,
five named locales, one warm-up and ten iterations. Example milliseconds per
iteration from this machine:

| Layers | Previous render derivation | Changed-workspace derivation | Default locale | Previous edit clone | Focused edit clone |
| --- | --- | --- | --- | --- | --- |
| 100 | 40.86 | 27.35 | 2.71 | 3.11 | 0.03 |
| 1,000 | 785.95 | 307.66 | 36.40 | 37.84 | 0.04 |

Unchanged-workspace renders now perform zero locale derivations. Changed-workspace
renders still materialize all effective locales once. These are isolated function
timings, with machine-load variance, not browser responsiveness or allocation profiles.
The earlier initial probe had simpler fields and measured 74/439 ms for the old
double derivation; compare variants within the repeatable script, not those runs.

There is no live deployment for end-to-end verification; Docker, database
integration, production deployment, and OMA-69 benchmarks were not run.

## Browser follow-up

The initial T3 browser failure was resolved with explicit operator approval:
`/root/.config/systemd/user/t3code.service.d/browser-sandbox.conf` sets
`T3CODE_SERVER_BROWSER_SANDBOX=0`. After restarting T3, Chrome opened successfully.
This disables Chrome sandboxing for T3's root-run browser. To reverse it, remove
that override, reload user systemd, and restart T3; sandboxed Chrome requires
running T3 as a non-root user instead.

The retained fixture mounts the real Dashboard with mocked API responses and the
same 100/1,000-layer, 20-field, five-named-locale shapes. The baseline Vite loader
reads the pinned pre-change source directly from git, without switching checkouts.
Run from `config-ui`:

```sh
./node_modules/.bin/vite --config perf/vite.config.mjs
```

Open `/perf/perf.html?layers=100&baseline` on port 5179; omit `baseline` for current
source, and use `layers=1000` for the larger fixture. Once loaded, run in the browser:

```js
await (await import('/perf/probe.js')).default()
```

Reload the fixture between runs. The edit marks the workspace unsaved; accept the
discard prompt only for this synthetic fixture. Stop Vite after profiling.

The probe uses native DOM value setters and bubbling events, then waits for two
animation frames. The recorded run used five samples per action at a 1280×800 viewport.
The strengthened probe now defaults to three warm-up interactions and 15 measured
samples, accepts bounded sample counts, checks that edits reached the controlled
input and unsaved state, times out if the fixture never loads, and restores its
clone instrumentation even on failure. It records browser/viewport metadata and
omits the unreliable heap counter. These changes do not retroactively improve the
five-sample results below; rerun before drawing stronger performance conclusions.
These measure synthetic event-to-frame latency in the Vite development build,
including React/DOM work and frame scheduling, not trusted-input field INP or
an exact compositor paint timestamp. Polling remained enabled and occasionally
added baseline renders. Raw samples are in `dashboard-browser-results.json`.

Median milliseconds (five samples; no statistical confidence claim):

| Layers | Action | Baseline | Current |
| --- | --- | --- | --- |
| 100 | Search | 134.5 | 33.4 |
| 100 | Locale switch | 111.6 | 43.8 |
| 100 | Field edit | 98.0 | 141.0 |
| 1,000 | Search | 450.2 | 51.9 |
| 1,000 | Locale switch | 763.7 | 85.8 |
| 1,000 | Field edit | 498.3 | 344.2 |

The small-fixture edit result did not improve in this noisy short run; do not
generalize the helper clone speedup into a universal input latency claim.
Every run retained the edited unsaved workspace. Clone-call instrumentation
recorded zero calls for current search and locale switches (baseline normally
22 and 44 respectively), and 12 per current edit versus normally 23 before.
This is an allocation-work proxy, not a heap allocation profile. Chrome's exposed
heap counter was quantized to the same value across runs and was excluded.
Full allocation-stack sampling remains outstanding because the shared preview
tools expose no heap-profiler interface. The test tab and temporary Vite server
were closed after measurement.

## Additional regression coverage

The expanded discovery suite exercises catalogue, icon and plugin single-flight
behaviour across active/inactive transitions, invalidation during a pending request,
and refresh on returning to the area. An unmount test initially failed: a queued
invalidation launched a second request after unmount. A mounted-lifetime check
now prevents that orphan request; the regression passes.

The Dashboard regression spies on the real locale helper while exercising search,
selection, field editing, discovery invalidation, area navigation, validation,
and a named-locale switch. It verifies zero derivations for unrelated renders,
one derivation for an edit, retention of the unsaved input, no mutation of the
original workspace, and preservation of the untouched layer and raw named override
in the submitted validation payload. The named locale remains read-only.

Focused command: `npx vitest run src/discovery.component.test.jsx src/locale-performance.component.test.jsx --maxWorkers=1`
(from `config-ui`): all nine tests passed.
The full serial component run then passed 159 tests across seven files. The eight
discovery tests passed again after adding assertions that a successful retry
clears the failure and loading state and replaces stale data. The seven Node
helper tests, five backend catalogue tests, production build, probe syntax check,
and `git diff --check` also passed in this follow-up. No new browser timing run
was performed in this test-hardening pass.

## Warmed browser rerun and final review

The strengthened probe was subsequently run in alternating baseline/current order
for 100 layers, then baseline/current for 1,000 layers. Each action used three
warm-ups and 15 recorded samples. Chrome 156, 1280×800, development build, mocked
API, and synthetic event-to-two-frame methodology were unchanged. Raw results:
`dashboard-browser-warmed-results.json`.

| Layers | Action | Baseline median ms | Current median ms |
| --- | --- | --- | --- |
| 100 | Search | 66.7 | 33.3 |
| 100 | Locale switch | 83.3 | 33.5 |
| 100 | Field edit | 83.1 | 70.5 |
| 1,000 | Search | 316.8 | 66.5 |
| 1,000 | Locale switch | 617.5 | 94.2 |
| 1,000 | Field edit | 519.2 | 296.2 |

All four completed runs confirmed unsaved state. Current search and locale
switching had zero structured-clone calls; edits had 12, versus baseline 22/44/23.
Only one completed run per variant was retained, so these results are descriptive,
not confidence intervals or production INP. A 1,000-layer baseline attempt exceeded
the preview tool's 15-second evaluation timeout; an overlapping retry was discarded
after a missing-element error. The retained baseline came from a fresh navigation
with asynchronous result collection and no concurrent probe.

The shared browser process uses `--remote-debugging-pipe`, with no attachable CDP
WebSocket endpoint. Available preview tools expose JavaScript evaluation but no
HeapProfiler interface. Allocation-stack sampling therefore remains an explicit
OMA-71 acceptance gap; clone counts are not a substitute. No browser transport or
server configuration was changed during this rerun. The tab and Vite server were
closed afterward.

Final diff review covered source filtering, merge semantics, focused edit ownership,
discovery lifecycle, resource publication and mutation refresh dependencies. It
found an audit ordering race: the original slow audit request could overwrite the
newer response after a mutation. A deferred-response regression failed before the
fix; per-resource request generations now reject superseded responses and errors.
No unrelated production changes, new dependencies, deployment, merge or commit
were introduced.

After the audit fix, `npx vitest run src/discovery.component.test.jsx
src/dashboard.component.test.jsx --maxWorkers=1` passed 93 tests. The seven Node
helper tests and five backend catalogue tests also passed again, as did the
production build, probe syntax check and `git diff --check`. The earlier full
159-test component run predates the additional audit regression; only the affected
93-test subset was rerun after that fix.

## Allocation acceptance evidence — completed

An isolated Chrome Headless Shell 156.0.8078.70 session supplied a loopback-only
DevTools endpoint, independent of T3's shared browser. It used the already installed
browser and Node's built-in WebSocket client; no packages were added. The temporary
browser profile was removed and both Chrome and Vite were stopped after the run.
The isolated root-run Chrome used `--no-sandbox`, matching the approved environment
workaround; no further server settings were changed.

`config-ui/perf/allocations.mjs` runs baseline then current for each fixture size,
at 1280×800 with device scale factor 1. For each of search, locale switch and field
edit it performs three warm-up interactions, collects garbage, starts CDP
`HeapProfiler.startSampling` at a 32 KiB sampling interval with both
`includeObjectsCollectedByMajorGC` and `includeObjectsCollectedByMinorGC`, performs
15 interactions, and stops sampling. It saves a Chrome `.heapprofile` call tree
and sample list for each action. All 12 files were parsed and confirmed to contain
nonempty samples. Both fixture sizes and both source variants retained unsaved edits.

The summary and largest allocation frames are retained in
`dashboard-allocation-results.json`. The downloadable raw profile bundle is
`/tmp/oma63-allocation-profiles.tar.gz` (720 KiB on this machine); extract it and
load individual `.heapprofile` files into Chrome DevTools' Memory panel. That
temporary archive is not committed; the runner can regenerate it.

Estimated sampled JavaScript allocation volume per interaction, MiB:

| Layers | Action | Baseline | Current |
| --- | --- | --- | --- |
| 100 | Search | 5.04 | 0.75 |
| 100 | Locale switch | 10.53 | 2.25 |
| 100 | Field edit | 8.96 | 6.74 |
| 1,000 | Search | 48.28 | 2.10 |
| 1,000 | Locale switch | 95.68 | 12.39 |
| 1,000 | Field edit | 56.06 | 31.79 |

These are allocation-volume estimates from sampled stack profiles, including
objects subsequently collected, not retained heap, peak process memory, exact byte
counts, or a leak test. They include React development-build overhead and any
ordinary discovery polling during the measured interval. They exclude allocations
not attributed by V8's JavaScript heap sampler. Each cell is one 15-interaction
batch; no confidence interval or production-memory claim is made. Latency evidence
comes from the separate warmed browser run, without heap-sampling overhead.

Baseline profiles attribute the largest allocation frames to `mergeLocale`.
Current search/locale profiles instead show React element/rendering overhead;
current editing still includes `mergeLocale`, consistent with recomputing effective
locales once after a workspace edit. The evidence supports the scoped optimization
without implying that large-workspace editing has no remaining cost.

Reproduction from `config-ui`, in two terminals:

```sh
./node_modules/.bin/vite --config perf/vite.config.mjs
```

```sh
node perf/allocations.mjs \
  /root/.t3/tools/chrome-headless-shell/linux64/156.0.8078.70/chrome-headless-shell \
  /tmp/oma63-allocations
```

Stop Vite afterward. The runner owns and closes its Chrome session automatically.
This pass ran the full 12-profile collection, JSON/sample validation,
`node --check config-ui/perf/allocations.mjs`, and `git diff --check`.
Production code was unchanged, so the previously recorded regression results
were not represented as newly rerun tests.

## Karpathy review before PR

Reviewed the patch for scope, unnecessary abstraction, mutation ownership,
permissions, stale results and verification claims. One additional defect was
reproduced: independently available source synchronization could publish a new
catalogue before the initial catalogue request returned; the late initial response
then erased the synchronized result. Per-resource request generations now reject
superseded semantic results and errors. The focused semantic/discovery run passed
all 32 tests after that fix. No remaining blocking finding was identified in this
self-review. This is not an independent reviewer approval. Live deployment and
production-browser latency remain outside the evidence collected here.
