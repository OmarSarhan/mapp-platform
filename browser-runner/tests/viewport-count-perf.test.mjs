import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import http from 'node:http';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {performance} from 'node:perf_hooks';
import test from 'node:test';
import {fileURLToPath, pathToFileURL} from 'node:url';

const BASELINE_COMMIT = '47d373770b92dd929b82fe3bbc26b18ce07abc22';
const REPO_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const CURRENT_PLUGIN_URL = new URL(
  '../../instance/public/plugins/viewport-layer-count/index.mjs',
  import.meta.url,
).href;

const POOL_CONCURRENCY = 2;
const COUNT_SERVICE_MS = 70;
const TILE_SERVICE_MS = 15;
const DEBOUNCE_MS = 20;
const PAN_INTERVAL_MS = 45;
const TOTAL_PANS = 6;
const TILES_PER_PAN = 4;
const HIDE_AT_PAN = 3;
const FILTER_UPDATE_AT_PAN = 4;

const LAYER_SPECS = [
  {
    key: 'roads',
    name: 'Roads',
    defaultFilter: {status: 'ready', class: 'highway'},
    initialCurrentFilter: {tier: 'all'},
    updatedCurrentFilter: {tier: 'primary', pan: FILTER_UPDATE_AT_PAN},
  },
  {
    key: 'stops',
    name: 'Bus Stops',
    defaultFilter: {status: 'ready', mode: 'bus'},
    initialCurrentFilter: {zone: {in: ['1', '2']}},
  },
  {
    key: 'buildings',
    name: 'Buildings',
    defaultFilter: {status: 'ready', footprint: true},
    initialCurrentFilter: {floors: {gte: 2}},
  },
  {
    key: 'parcels',
    name: 'Parcels',
    defaultFilter: {status: 'ready', tenure: 'public'},
    initialCurrentFilter: {active: true},
  },
];

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function stableHash(input) {
  let hash = 2166136261;
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function computeExpectedCount(layerKey, viewport, filter) {
  const payload = JSON.stringify({
    layerKey,
    viewport: Number(viewport),
    default: filter?.default ?? null,
    current: filter?.current ?? null,
    viewportFlag: filter?.viewport === true,
  });
  return 100 + (stableHash(payload) % 9000);
}

function summarizeLatencies(samples) {
  assert.ok(samples.length > 0, 'Expected at least one latency sample');
  const sorted = [...samples].sort((a, b) => a - b);
  const percentile = p => {
    const idx = Math.min(sorted.length - 1, Math.ceil(sorted.length * p) - 1);
    return Number(sorted[idx].toFixed(1));
  };
  const mean = samples.reduce((sum, v) => sum + v, 0) / samples.length;
  return {
    count: samples.length,
    mean: Number(mean.toFixed(1)),
    p50: percentile(0.5),
    p95: percentile(0.95),
    max: Number(sorted[sorted.length - 1].toFixed(1)),
  };
}

function createMockDocument() {
  return {
    createElement: () => {
      const attrs = {};
      return {
        dataset: {},
        style: {},
        hidden: false,
        textContent: '',
        attrs,
        setAttribute(name, value) {
          attrs[name] = value;
        },
        getAttribute(name) {
          return attrs[name];
        },
      };
    },
  };
}

async function startContendedBackend() {
  const expectedDefaults = new Map(
    LAYER_SPECS.map(spec => [spec.key, JSON.stringify(spec.defaultFilter)]),
  );

  let activeWorkers = 0;
  const queue = [];
  let maxQueueDepth = 0;
  let totalCountRequests = 0;
  let totalTileRequests = 0;
  let validatedFilterRequests = 0;
  let invalidFilterRequests = 0;
  let totalCountInFlight = 0;
  let maxTotalCountInFlight = 0;
  const inFlightByLayer = new Map();
  const maxInFlightByLayer = new Map();
  const countRequestsByLayer = new Map();
  const countLatenciesMs = [];

  const drainQueue = () => {
    while (activeWorkers < POOL_CONCURRENCY && queue.length > 0) {
      const task = queue.shift();
      activeWorkers++;
      task().finally(() => {
        activeWorkers--;
        drainQueue();
      });
    }
  };

  const enqueue = (serviceMs, work) =>
    new Promise((resolve, reject) => {
      queue.push(async () => {
        try {
          await sleep(serviceMs);
          resolve(await work());
        } catch (error) {
          reject(error);
        }
      });
      if (queue.length > maxQueueDepth) {
        maxQueueDepth = queue.length;
      }
      drainQueue();
    });

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1');

    if (url.pathname === '/count') {
      const startedAt = performance.now();
      const layerKey = url.searchParams.get('layer') || '';
      const viewport = Number(url.searchParams.get('viewport'));
      const rawFilter = url.searchParams.get('filter') || '';

      totalCountRequests++;
      countRequestsByLayer.set(layerKey, (countRequestsByLayer.get(layerKey) || 0) + 1);

      const nextLayerInFlight = (inFlightByLayer.get(layerKey) || 0) + 1;
      inFlightByLayer.set(layerKey, nextLayerInFlight);
      if (nextLayerInFlight > (maxInFlightByLayer.get(layerKey) || 0)) {
        maxInFlightByLayer.set(layerKey, nextLayerInFlight);
      }

      totalCountInFlight++;
      if (totalCountInFlight > maxTotalCountInFlight) {
        maxTotalCountInFlight = totalCountInFlight;
      }

      try {
        let parsedFilter;
        try {
          parsedFilter = JSON.parse(rawFilter);
        } catch {
          parsedFilter = null;
        }
        const defaultMatches =
          parsedFilter &&
          JSON.stringify(parsedFilter.default) === expectedDefaults.get(layerKey);
        const currentPresent =
          parsedFilter &&
          typeof parsedFilter.current === 'object' &&
          parsedFilter.current !== null;
        const viewportEnabled = parsedFilter?.viewport === true;

        if (!defaultMatches || !currentPresent || !viewportEnabled) {
          invalidFilterRequests++;
          res.writeHead(400, {'content-type': 'application/json'});
          res.end(JSON.stringify({error: 'invalid-effective-filter'}));
          return;
        }

        validatedFilterRequests++;
        const count = await enqueue(COUNT_SERVICE_MS, () =>
          computeExpectedCount(layerKey, viewport, parsedFilter),
        );
        res.writeHead(200, {'content-type': 'application/json'});
        res.end(JSON.stringify({count}));
      } finally {
        inFlightByLayer.set(layerKey, (inFlightByLayer.get(layerKey) || 1) - 1);
        totalCountInFlight--;
        countLatenciesMs.push(performance.now() - startedAt);
      }
      return;
    }

    if (url.pathname === '/tile') {
      totalTileRequests++;
      const payload = await enqueue(TILE_SERVICE_MS, () => `tile:${url.searchParams.get('pan')}:${url.searchParams.get('tile')}`);
      res.writeHead(200, {'content-type': 'text/plain'});
      res.end(payload);
      return;
    }

    if (url.pathname === '/health') {
      res.writeHead(200, {'content-type': 'text/plain'});
      res.end('ok');
      return;
    }

    res.writeHead(404);
    res.end();
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      resolve();
    });
  });

  const {port} = server.address();
  const baseUrl = `http://127.0.0.1:${port}`;

  return {
    baseUrl,
    getMetrics() {
      const perLayerMax = Object.fromEntries(
        LAYER_SPECS.map(spec => [spec.key, maxInFlightByLayer.get(spec.key) || 0]),
      );
      const perLayerCounts = Object.fromEntries(
        LAYER_SPECS.map(spec => [spec.key, countRequestsByLayer.get(spec.key) || 0]),
      );
      return {
        totalCountRequests,
        totalTileRequests,
        validatedFilterRequests,
        invalidFilterRequests,
        maxInFlightPerLayer: Math.max(0, ...Object.values(perLayerMax)),
        maxInFlightByLayer: perLayerMax,
        countRequestsByLayer: perLayerCounts,
        maxTotalCountInFlight,
        maxQueueDepth,
        countLatency: summarizeLatencies(countLatenciesMs),
      };
    },
    async close() {
      server.closeAllConnections?.();
      await new Promise((resolve, reject) => {
        server.close(error => (error ? reject(error) : resolve()));
      });
    },
  };
}

async function resolveBaselineModule() {
  if (process.env.BASELINE_VIEWPORT_COUNT_MODULE) {
    return {
      source: 'env',
      moduleUrl: process.env.BASELINE_VIEWPORT_COUNT_MODULE,
      cleanup: async () => {},
    };
  }

  let sourceCode;
  try {
    sourceCode = execFileSync(
      'git',
      [
        'show',
        `${BASELINE_COMMIT}:instance/public/plugins/viewport-layer-count/index.mjs`,
      ],
      {
        cwd: REPO_ROOT,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      },
    );
  } catch (error) {
    if (process.env.REQUIRE_BASELINE === '1') {
      throw new Error(
        `REQUIRE_BASELINE=1 was set, but git show ${BASELINE_COMMIT} failed: ${error.message}`,
      );
    }
    return {
      source: 'unavailable',
      moduleUrl: null,
      cleanup: async () => {},
    };
  }

  const tempDir = await mkdtemp(join(tmpdir(), 'mapp-oma75-baseline-'));
  const baselineFile = join(tempDir, 'viewport-layer-count-baseline.mjs');
  await writeFile(baselineFile, sourceCode, 'utf8');
  return {
    source: `git:${BASELINE_COMMIT.slice(0, 8)}`,
    moduleUrl: pathToFileURL(baselineFile).href,
    cleanup: async () => {
      await rm(tempDir, {recursive: true, force: true});
    },
  };
}

async function runRapidPanWorkload(moduleUrl, label) {
  const backend = await startContendedBackend();
  const original = {
    document: globalThis.document,
    mapp: globalThis.mapp,
  };

  let activeClientCounts = 0;
  let lastCountSettledAt = performance.now();
  const tileLatenciesMs = [];
  const tilePromises = [];
  let hiddenLayerPostHideTextChanges = 0;

  globalThis.document = createMockDocument();
  globalThis.mapp = {
    plugins: {},
    ui: {
      utils: {
        locationCount: async layer => {
          activeClientCounts++;
          try {
            const params = new URLSearchParams({
              layer: layer.key,
              viewport: String(layer.viewport),
              filter: JSON.stringify(layer.filter),
            });
            const response = await fetch(`${backend.baseUrl}/count?${params}`);
            if (!response.ok) {
              throw new Error(`HTTP ${response.status} for layer ${layer.key}`);
            }
            const body = await response.json();
            return body.count;
          } finally {
            activeClientCounts--;
            lastCountSettledAt = performance.now();
          }
        },
      },
    },
  };

  try {
    const cacheBust = `${moduleUrl}${moduleUrl.includes('?') ? '&' : '?'}workload=${label}`;
    const {viewportLayerCount} = await import(cacheBust);

    const layers = LAYER_SPECS.map(spec => {
      let visible = true;
      return {
        spec,
        key: spec.key,
        name: spec.name,
        qID: 'id',
        mapview: {},
        viewport: 0,
        tableCurrent: () => true,
        L: {
          getVisible: () => visible,
          setVisible: next => {
            visible = next;
          },
        },
        filter: {
          default: structuredClone(spec.defaultFilter),
          current: structuredClone(spec.initialCurrentFilter),
        },
        viewport_layer_count: {debounce: DEBOUNCE_MS},
        changeEndCallbacks: [],
        showCallbacks: [],
        hideCallbacks: [],
      };
    });

    // Warm up the HTTP client before timing so cold-start setup does not skew baseline vs current.
    await fetch(`${backend.baseUrl}/health`).then(res => res.text());

    const startedAt = performance.now();
    layers.forEach(viewportLayerCount);

    const hiddenLayer = layers.find(l => l.key === 'parcels');
    const hiddenBadge = hiddenLayer.viewConfig.headerBtn[0];
    let hiddenAfterPan = false;
    let trackedHiddenText = hiddenBadge.textContent;
    Object.defineProperty(hiddenBadge, 'textContent', {
      configurable: true,
      enumerable: true,
      get() {
        return trackedHiddenText;
      },
      set(value) {
        if (hiddenAfterPan) {
          hiddenLayerPostHideTextChanges++;
        }
        trackedHiddenText = value;
      },
    });

    for (let pan = 1; pan <= TOTAL_PANS; pan++) {
      await sleep(PAN_INTERVAL_MS);

      if (pan === HIDE_AT_PAN) {
        hiddenLayer.L.setVisible(false);
        hiddenLayer.hideCallbacks[0]();
        hiddenAfterPan = true;
      }

      if (pan === FILTER_UPDATE_AT_PAN) {
        const roads = layers.find(l => l.key === 'roads');
        roads.filter.current = structuredClone(roads.spec.updatedCurrentFilter);
      }

      for (const layer of layers) {
        if (!layer.L.getVisible()) continue;
        layer.viewport = pan;
        layer.changeEndCallbacks[0]();
      }

      for (let tile = 0; tile < TILES_PER_PAN; tile++) {
        const tileStart = performance.now();
        tilePromises.push(
          fetch(`${backend.baseUrl}/tile?pan=${pan}&tile=${tile}`).then(async res => {
            assert.equal(res.status, 200);
            await res.text();
            tileLatenciesMs.push(performance.now() - tileStart);
          }),
        );
      }
    }

    await Promise.all(tilePromises);

    // Wait for all debounced and in-flight count requests to settle cleanly.
    const quietWindowMs = DEBOUNCE_MS + 40;
    const deadline = performance.now() + 5000;
    while (performance.now() < deadline) {
      if (
        activeClientCounts === 0 &&
        performance.now() - lastCountSettledAt >= quietWindowMs
      ) {
        break;
      }
      await sleep(15);
    }
    assert.equal(activeClientCounts, 0, `${label}: count requests did not settle before timeout`);

    const totalDurationMs = Number((performance.now() - startedAt).toFixed(1));
    const serverMetrics = backend.getMetrics();
    const badges = Object.fromEntries(
      layers.map(layer => {
        const badge = layer.viewConfig.headerBtn[0];
        return [
          layer.key,
          {
            hidden: badge.hidden,
            textContent: badge.textContent,
            expectedText: `(${computeExpectedCount(layer.key, TOTAL_PANS, layer.filter).toLocaleString()})`,
            filter: structuredClone(layer.filter),
          },
        ];
      }),
    );

    return {
      label,
      totalDurationMs,
      ...serverMetrics,
      tileLatency: summarizeLatencies(tileLatenciesMs),
      hiddenLayerPostHideTextChanges,
      badges,
    };
  } finally {
    globalThis.document = original.document;
    globalThis.mapp = original.mapp;
    await backend.close();
  }
}

test('rapid-pan HTTP benchmark: coalesced viewport counts reduce count traffic and tile latency under shared backend contention', async t => {
  const baselineInfo = await resolveBaselineModule();

  try {
    const baseline = baselineInfo.moduleUrl
      ? await runRapidPanWorkload(baselineInfo.moduleUrl, 'baseline')
      : null;
    const current = await runRapidPanWorkload(CURRENT_PLUGIN_URL, 'current');

    const summary = {
      config: {
        poolConcurrency: POOL_CONCURRENCY,
        countServiceMs: COUNT_SERVICE_MS,
        tileServiceMs: TILE_SERVICE_MS,
        debounceMs: DEBOUNCE_MS,
        panIntervalMs: PAN_INTERVAL_MS,
        layers: LAYER_SPECS.length,
        pans: TOTAL_PANS,
        tilesPerPan: TILES_PER_PAN,
        hideLayerAtPan: {layer: 'parcels', pan: HIDE_AT_PAN},
        baselineSource: baselineInfo.source,
      },
      baseline: baseline
        ? {
            totalDurationMs: baseline.totalDurationMs,
            totalCountRequests: baseline.totalCountRequests,
            maxInFlightPerLayer: baseline.maxInFlightPerLayer,
            maxTotalCountInFlight: baseline.maxTotalCountInFlight,
            maxQueueDepth: baseline.maxQueueDepth,
            tileLatencyMs: baseline.tileLatency,
            countLatencyMs: baseline.countLatency,
          }
        : null,
      current: {
        totalDurationMs: current.totalDurationMs,
        totalCountRequests: current.totalCountRequests,
        maxInFlightPerLayer: current.maxInFlightPerLayer,
        maxTotalCountInFlight: current.maxTotalCountInFlight,
        maxQueueDepth: current.maxQueueDepth,
        tileLatencyMs: current.tileLatency,
        countLatencyMs: current.countLatency,
      },
    };
    t.diagnostic(`viewport-count-perf summary: ${JSON.stringify(summary)}`);

    // Current-plugin invariants (always verified, including gitless containers and shallow CI).
    assert.equal(current.invalidFilterRequests, 0);
    assert.equal(current.validatedFilterRequests, current.totalCountRequests);
    assert.equal(current.totalTileRequests, TOTAL_PANS * TILES_PER_PAN);
    assert.equal(
      current.maxInFlightPerLayer,
      1,
      `Expected current maxInFlightPerLayer === 1, got ${JSON.stringify(current.maxInFlightByLayer)}`,
    );
    assert.ok(
      current.totalCountRequests <= 15,
      `Expected bounded current count requests <= 15, got ${current.totalCountRequests}`,
    );

    for (const spec of LAYER_SPECS) {
      const badge = current.badges[spec.key];
      if (spec.key === 'parcels') {
        assert.equal(badge.hidden, true);
        assert.equal(
          current.hiddenLayerPostHideTextChanges,
          0,
          'Hidden layer badge must not receive stale count updates after hide',
        );
      } else {
        assert.equal(badge.hidden, false);
        assert.equal(
          badge.textContent,
          badge.expectedText,
          `Layer ${spec.key} final badge mismatch`,
        );
        assert.deepEqual(badge.filter.default, spec.defaultFilter);
        assert.equal(badge.filter.viewport, true);
        if (spec.updatedCurrentFilter) {
          assert.deepEqual(badge.filter.current, spec.updatedCurrentFilter);
        } else {
          assert.deepEqual(badge.filter.current, spec.initialCurrentFilter);
        }
      }
    }

    // Comparative assertions whenever the git/env baseline is available.
    if (baseline) {
      assert.equal(baseline.invalidFilterRequests, 0);
      assert.ok(
        baseline.maxInFlightPerLayer > 1,
        `Expected baseline maxInFlightPerLayer > 1, got ${JSON.stringify(baseline.maxInFlightByLayer)}`,
      );
      assert.ok(
        current.totalCountRequests < baseline.totalCountRequests,
        `Expected current count requests (${current.totalCountRequests}) < baseline (${baseline.totalCountRequests})`,
      );
      assert.ok(
        current.totalCountRequests <= Math.floor(baseline.totalCountRequests * 0.7),
        `Expected >=30% count reduction (current=${current.totalCountRequests}, baseline=${baseline.totalCountRequests})`,
      );
      assert.ok(
        current.tileLatency.mean < baseline.tileLatency.mean,
        `Expected lower mean tile latency (current=${current.tileLatency.mean}ms, baseline=${baseline.tileLatency.mean}ms)`,
      );
      assert.ok(
        current.tileLatency.p95 < baseline.tileLatency.p95,
        `Expected lower p95 tile latency (current=${current.tileLatency.p95}ms, baseline=${baseline.tileLatency.p95}ms)`,
      );
    }
  } finally {
    await baselineInfo.cleanup();
  }
});
