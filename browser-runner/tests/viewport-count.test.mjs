import assert from 'node:assert/strict';
import test from 'node:test';

const DEFAULT_MODULE_URL = new URL(
  '../../instance/public/plugins/viewport-layer-count/index.mjs',
  import.meta.url,
).href;
const MODULE_SPECIFIER = process.env.VIEWPORT_COUNT_MODULE || DEFAULT_MODULE_URL;

let cachedModule;
async function loadPlugin() {
  if (!cachedModule) {
    globalThis.mapp ??= {plugins: {}};
    cachedModule = await import(MODULE_SPECIFIER);
  }
  return cachedModule;
}

async function withMockEnv(run) {
  const original = {
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    document: globalThis.document,
    mapp: globalThis.mapp,
    warn: console.warn,
  };
  const timers = new Map();
  const timerDelays = [];
  const calls = [];
  const warnings = [];
  let timerId = 0;

  globalThis.setTimeout = (fn, delay) => {
    const id = ++timerId;
    timers.set(id, fn);
    timerDelays.push(delay);
    return id;
  };
  globalThis.clearTimeout = id => {
    timers.delete(id);
  };
  globalThis.document = {
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
  globalThis.mapp = {
    plugins: {},
    ui: {
      utils: {
        locationCount: layer =>
          new Promise((resolve, reject) => {
            calls.push({
              layer,
              key: layer.key,
              viewport: layer.viewport,
              filter: structuredClone(layer.filter),
              resolve,
              reject,
            });
          }),
      },
    },
  };
  console.warn = (...args) => {
    warnings.push(args);
  };

  const tick = () => {
    const pending = [...timers.values()];
    timers.clear();
    pending.forEach(fn => fn());
  };
  const settle = async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  };
  const makeLayer = (overrides = {}) => {
    let visible = overrides.visible ?? true;
    let inScale = overrides.inScale ?? true;
    const layer = {
      key: overrides.key ?? 'layer-0',
      name: overrides.name ?? String(overrides.key ?? 'layer-0'),
      qID: 'id',
      mapview: {},
      viewport: overrides.viewport ?? 0,
      tableCurrent: () => inScale,
      L: {
        getVisible: () => visible,
        setVisible: next => {
          visible = next;
        },
      },
      setInScale: next => {
        inScale = next;
      },
      changeEndCallbacks: [],
      showCallbacks: [],
      hideCallbacks: [],
      ...overrides,
    };
    delete layer.visible;
    delete layer.inScale;
    return layer;
  };

  try {
    const {viewportLayerCount} = await loadPlugin();
    return await run({
      viewportLayerCount,
      timers,
      timerDelays,
      calls,
      warnings,
      tick,
      settle,
      makeLayer,
    });
  } finally {
    globalThis.setTimeout = original.setTimeout;
    globalThis.clearTimeout = original.clearTimeout;
    globalThis.document = original.document;
    globalThis.mapp = original.mapp;
    console.warn = original.warn;
  }
}

test('counts coalesce per layer across multi-layer rapid pans and only latest pending viewport runs next', async () => {
  await withMockEnv(async ({viewportLayerCount, calls, tick, settle, makeLayer}) => {
    const layers = [0, 1].map(key =>
      makeLayer({
        key,
        name: String(key),
        filter: {default: {status: 'ready'}},
      }),
    );

    layers.forEach(viewportLayerCount);
    tick();
    assert.equal(calls.length, 2);

    for (let pan = 1; pan <= 3; pan++) {
      layers.forEach(layer => {
        layer.viewport = pan;
        layer.changeEndCallbacks[0]();
      });
      tick();
    }

    // Max 1 active count per layer across all rapid pans.
    assert.equal(calls.length, 2);

    calls[0].resolve(100);
    await settle();
    assert.equal(calls.length, 3);
    assert.equal(calls[2].layer, layers[0]);
    assert.equal(calls[2].viewport, 3);
    assert.equal(layers[0].viewConfig.headerBtn[0].textContent, '(…)');

    calls[2].resolve(3);
    await settle();
    assert.equal(layers[0].viewConfig.headerBtn[0].textContent, '(3)');

    // Layer 1 also drains only its latest pending viewport.
    calls[1].resolve(200);
    await settle();
    assert.equal(calls.length, 4);
    assert.equal(calls[3].layer, layers[1]);
    assert.equal(calls[3].viewport, 3);
    calls[3].resolve(1234);
    await settle();
    assert.equal(
      layers[1].viewConfig.headerBtn[0].textContent,
      `(${Number(1234).toLocaleString()})`,
    );
  });
});

test('hiding while active and pending cancels timer, drops pending work, and suppresses stale resolve and reject', async () => {
  await withMockEnv(async ({viewportLayerCount, timers, calls, warnings, tick, settle, makeLayer}) => {
    const layerA = makeLayer({key: 'a'});
    const layerB = makeLayer({key: 'b'});
    viewportLayerCount(layerA);
    viewportLayerCount(layerB);
    tick();
    assert.equal(calls.length, 2);

    // Queue pending work on both layers and keep a pending debounce timer on layerA.
    layerA.viewport = 1;
    layerA.changeEndCallbacks[0]();
    layerB.viewport = 1;
    layerB.changeEndCallbacks[0]();
    tick();

    layerA.viewport = 2;
    layerA.changeEndCallbacks[0]();
    assert.equal(timers.size, 1);

    // Hide both layers.
    layerA.L.setVisible(false);
    layerA.hideCallbacks[0]();
    layerB.L.setVisible(false);
    layerB.hideCallbacks[0]();

    assert.equal(timers.size, 0);
    assert.equal(layerA.viewConfig.headerBtn[0].hidden, true);
    assert.equal(layerB.viewConfig.headerBtn[0].hidden, true);

    // Ticking after hide must not start any work.
    tick();
    assert.equal(calls.length, 2);

    // Stale resolve on hidden layer A is suppressed and does not drain pending.
    calls[0].resolve(999);
    await settle();
    assert.equal(calls.length, 2);
    assert.equal(layerA.viewConfig.headerBtn[0].hidden, true);
    assert.notEqual(layerA.viewConfig.headerBtn[0].textContent, '(999)');

    // Stale reject on hidden layer B is suppressed and does not warn or drain pending.
    calls[1].reject(new Error('stale hidden error'));
    await settle();
    assert.equal(calls.length, 2);
    assert.equal(layerB.viewConfig.headerBtn[0].hidden, true);
    assert.equal(warnings.length, 0);

    // Showing layer B later works because active state reset after the rejected request.
    layerB.L.setVisible(true);
    layerB.viewport = 3;
    layerB.showCallbacks[0]();
    tick();
    assert.equal(calls.length, 3);
    assert.equal(calls[2].viewport, 3);
    calls[2].resolve(7);
    await settle();
    assert.equal(layerB.viewConfig.headerBtn[0].hidden, false);
    assert.equal(layerB.viewConfig.headerBtn[0].textContent, '(7)');
  });
});

test('hide then show while old request is still active shows loading badge, suppresses stale result, and runs latest viewport', async () => {
  await withMockEnv(async ({viewportLayerCount, calls, tick, settle, makeLayer}) => {
    const layer = makeLayer({key: 'toggle', name: 'Toggle', viewport: 0});
    viewportLayerCount(layer);
    const badge = layer.viewConfig.headerBtn[0];
    tick();
    calls[0].resolve(10);
    await settle();
    assert.equal(badge.hidden, false);
    assert.equal(badge.textContent, '(10)');

    // Pan to viewport 1 so a request is active.
    layer.viewport = 1;
    layer.changeEndCallbacks[0]();
    tick();
    assert.equal(calls.length, 2);
    assert.equal(badge.textContent, '(…)');

    // Hide while viewport 1 is in flight.
    layer.L.setVisible(false);
    layer.hideCallbacks[0]();
    assert.equal(badge.hidden, true);

    // Show again at viewport 2 while viewport 1 is STILL active.
    layer.L.setVisible(true);
    layer.viewport = 2;
    layer.showCallbacks[0]();
    assert.equal(badge.hidden, false);
    assert.equal(badge.textContent, '(…)');
    assert.equal(badge.getAttribute('aria-label'), 'Toggle: counting features in viewport');

    tick();

    // Badge remains visible with loading state and no second concurrent request starts.
    assert.equal(badge.hidden, false);
    assert.equal(badge.textContent, '(…)');
    assert.equal(calls.length, 2);

    // Old viewport 1 request settles with stale value -> suppressed, then viewport 2 starts.
    calls[1].resolve(999);
    await settle();
    assert.equal(calls.length, 3);
    assert.equal(calls[2].viewport, 2);
    assert.equal(badge.hidden, false);
    assert.equal(badge.textContent, '(…)');

    calls[2].resolve(42);
    await settle();
    assert.equal(badge.hidden, false);
    assert.equal(badge.textContent, '(42)');
  });
});

test('out-of-scale layers hide badge, skip requests, and suppress stale responses', async () => {
  await withMockEnv(async ({viewportLayerCount, calls, tick, settle, makeLayer}) => {
    const outOfScaleLayer = makeLayer({key: 'out', inScale: false});
    viewportLayerCount(outOfScaleLayer);
    tick();
    assert.equal(calls.length, 0);
    assert.equal(outOfScaleLayer.viewConfig.headerBtn[0].hidden, true);

    // Layer starts in scale with an active request, then pans out of scale.
    const layer = makeLayer({key: 'zoom', viewport: 1, inScale: true});
    viewportLayerCount(layer);
    const badge = layer.viewConfig.headerBtn[0];
    tick();
    assert.equal(calls.length, 1);

    layer.setInScale(false);
    layer.viewport = 2;
    layer.changeEndCallbacks[0]();
    tick();
    assert.equal(badge.hidden, true);

    // Stale response from viewport 1 is suppressed and no new request starts.
    calls[0].resolve(555);
    await settle();
    assert.equal(calls.length, 1);
    assert.equal(badge.hidden, true);
    assert.notEqual(badge.textContent, '(555)');

    // Zooming back into scale restores the badge and runs a fresh count.
    layer.setInScale(true);
    layer.viewport = 3;
    layer.changeEndCallbacks[0]();
    tick();
    assert.equal(calls.length, 2);
    assert.equal(calls[1].viewport, 3);
    assert.equal(badge.hidden, false);
    calls[1].resolve(12);
    await settle();
    assert.equal(badge.textContent, '(12)');
  });
});

test('effective filter (default, current, and viewport) is preserved across pans and hide/show', async () => {
  await withMockEnv(async ({viewportLayerCount, calls, tick, settle, makeLayer}) => {
    const expectedFilter = {
      default: {status: 'ready', region: 'north'},
      current: {category: {in: ['a', 'b']}},
      viewport: true,
    };
    const layer = makeLayer({
      key: 'filtered',
      filter: {
        default: {status: 'ready', region: 'north'},
        current: {category: {in: ['a', 'b']}},
      },
    });
    const unfiltered = makeLayer({key: 'unfiltered'});

    viewportLayerCount(layer);
    viewportLayerCount(unfiltered);
    assert.deepEqual(layer.filter, expectedFilter);
    assert.deepEqual(unfiltered.filter, {viewport: true});

    tick();
    assert.deepEqual(calls[0].filter, expectedFilter);
    assert.deepEqual(calls[1].filter, {viewport: true});

    calls[0].resolve(10);
    calls[1].resolve(20);
    await settle();

    // Pan preserves effective filters.
    layer.viewport = 2;
    layer.changeEndCallbacks[0]();
    tick();
    assert.deepEqual(calls[2].filter, expectedFilter);
    calls[2].resolve(15);
    await settle();

    // Hide -> show preserves effective filters.
    layer.L.setVisible(false);
    layer.hideCallbacks[0]();
    layer.L.setVisible(true);
    layer.viewport = 3;
    layer.showCallbacks[0]();
    tick();
    assert.deepEqual(calls[3].filter, expectedFilter);
    calls[3].resolve(18);
    await settle();
    assert.deepEqual(layer.filter, expectedFilter);
  });
});

test('error recovery shows fallback for current error, suppresses superseded error, and drains pending refresh', async () => {
  await withMockEnv(async ({viewportLayerCount, calls, warnings, tick, settle, makeLayer}) => {
    const layerA = makeLayer({key: 'err-current', name: 'ErrA'});
    const layerB = makeLayer({key: 'err-superseded', name: 'ErrB'});
    viewportLayerCount(layerA);
    viewportLayerCount(layerB);
    const badgeA = layerA.viewConfig.headerBtn[0];
    const badgeB = layerB.viewConfig.headerBtn[0];
    tick();
    assert.equal(calls.length, 2);

    // Current error on layerA displays fallback (–) and resets active state.
    calls[0].reject(new Error('db timeout'));
    await settle();
    assert.equal(badgeA.textContent, '(–)');
    assert.match(badgeA.getAttribute('aria-label'), /unavailable/);
    assert.equal(warnings.length, 1);

    // LayerA recovers cleanly on next pan.
    layerA.viewport = 1;
    layerA.changeEndCallbacks[0]();
    tick();
    assert.equal(calls.length, 3);
    calls[2].resolve(5);
    await settle();
    assert.equal(badgeA.textContent, '(5)');

    // LayerB has an active request (calls[1]); queue a pending refresh before rejecting.
    layerB.viewport = 2;
    layerB.changeEndCallbacks[0]();
    tick();
    assert.equal(calls.length, 3);

    calls[1].reject(new Error('stale error'));
    await settle();

    // Superseded error is suppressed (no new warning, badge stays loading) and pending drains.
    assert.equal(warnings.length, 1);
    assert.equal(badgeB.textContent, '(…)');
    assert.equal(calls.length, 4);
    assert.equal(calls[3].layer, layerB);
    assert.equal(calls[3].viewport, 2);

    calls[3].resolve(77);
    await settle();
    assert.equal(badgeB.textContent, '(77)');

    // Synchronous throw from locationCount also renders fallback and resets active state.
    const originalLocationCount = globalThis.mapp.ui.utils.locationCount;
    globalThis.mapp.ui.utils.locationCount = () => {
      throw new Error('sync failure');
    };
    layerA.viewport = 3;
    layerA.changeEndCallbacks[0]();
    tick();
    await settle();
    assert.equal(badgeA.textContent, '(–)');
    assert.equal(warnings.length, 2);

    globalThis.mapp.ui.utils.locationCount = originalLocationCount;
    layerA.viewport = 4;
    layerA.changeEndCallbacks[0]();
    tick();
    assert.equal(calls.length, 5);
    calls[4].resolve(19);
    await settle();
    assert.equal(badgeA.textContent, '(19)');
  });
});

test('debounce: 0 schedules with zero delay while clamping negative values and defaulting to 250ms', async () => {
  await withMockEnv(async ({viewportLayerCount, timerDelays, calls, tick, settle, makeLayer}) => {
    const zeroLayer = makeLayer({
      key: 'zero',
      viewport_layer_count: {debounce: 0},
    });
    const negLayer = makeLayer({
      key: 'neg',
      viewport_layer_count: {debounce: -50},
    });
    const defaultLayer = makeLayer({key: 'default'});

    viewportLayerCount(zeroLayer);
    viewportLayerCount(negLayer);
    viewportLayerCount(defaultLayer);

    assert.deepEqual(timerDelays, [0, 0, 250]);

    // Verify debounce: 0 still coalesces active + pending work cleanly.
    tick();
    assert.equal(calls.length, 3);
    zeroLayer.viewport = 1;
    zeroLayer.changeEndCallbacks[0]();
    tick();
    zeroLayer.viewport = 2;
    zeroLayer.changeEndCallbacks[0]();
    tick();
    assert.equal(calls.length, 3);

    calls[0].resolve(1);
    await settle();
    assert.equal(calls.length, 4);
    assert.equal(calls[3].viewport, 2);
    calls[3].resolve(9);
    await settle();
    assert.equal(zeroLayer.viewConfig.headerBtn[0].textContent, '(9)');
  });
});
