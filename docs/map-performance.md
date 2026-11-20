# Map performance (OMA-64): partial implementation

Inspected from clean HEAD `47d373770b92dd929b82fe3bbc26b18ce07abc22`.
No deployment, merge, framework patch, pool-budget change or pin update.

## Viewport counts (OMA-75)

Each plugin instance now retains one active request and one pending refresh.
Movement immediately invalidates old display results, then debounces the pending
refresh. The next request reads the current layer through the unchanged XYZ
`locationCount` helper, preserving its filter construction. Hiding clears both
the timer and pending work and invalidates the active result. Cancellation is not
claimed to reach XYZ. Existing active work finishes before another request starts.

Reproduce the deterministic regression:

```sh
node --test browser-runner/tests/viewport-count.test.mjs
git show 47d373770b92dd929b82fe3bbc26b18ce07abc22:instance/public/plugins/viewport-layer-count/index.mjs > /tmp/oma64-baseline-count.mjs
VIEWPORT_COUNT_MODULE=file:///tmp/oma64-baseline-count.mjs node --test browser-runner/tests/viewport-count.test.mjs
```

The baseline fails with eight unresolved calls where two are expected after
initial scheduling and three pans across two layers. Current source passes;
settling a request starts only its latest pending viewport. The test also checks
stale-display suppression, hide/show and preservation of the default filter.
These are stubbed count calls, not network or database timings. Tile latency,
real effective-filter integration and isolated candidate-preview evidence remain
outstanding; OMA-75 is not acceptance-complete.

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

Ran the focused Node regression (pass), baseline variant (expected failure),
upstream source inspection, `docker info` (daemon available), and
`git diff --check`. No database fixture, Caddy client-isolation experiment,
representative tile timing, full image build or end-to-end verifier was run.
No fixture servers or browsers were started. Self-review covered request lifecycle,
debounce invalidation, hide/show, filter preservation and framework boundaries.
Parent and children remain incomplete pending the evidence and dependencies above.
