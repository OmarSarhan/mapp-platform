import assert from 'node:assert/strict';
import test from 'node:test';

test('counts coalesce per layer and hiding invalidates active and pending work', async () => {
  const timers = new Map();
  const original = {setTimeout, clearTimeout, document: globalThis.document, mapp: globalThis.mapp};
  let id = 0;
  globalThis.setTimeout = fn => { timers.set(++id, fn); return id; };
  globalThis.clearTimeout = key => timers.delete(key);
  globalThis.document = {createElement: () => ({dataset: {}, style: {}, setAttribute() {}})};
  const calls = [];
  globalThis.mapp = {plugins: {}, ui: {utils: {locationCount: layer => new Promise((resolve, reject) => calls.push({layer, viewport: layer.viewport, resolve, reject}))}}};
  const tick = () => { const pending = [...timers.values()]; timers.clear(); pending.forEach(fn => fn()); };
  const settle = async () => { await Promise.resolve(); await Promise.resolve(); };
  try {
    const {viewportLayerCount} = await import(process.env.VIEWPORT_COUNT_MODULE || '../../instance/public/plugins/viewport-layer-count/index.mjs');
    const layers = [0, 1].map(key => ({key, name: String(key), qID: 'id', mapview: {}, tableCurrent: () => true,
      L: {getVisible: () => true}, filter: {default: {status: 'ready'}}, changeEndCallbacks: [], showCallbacks: [], hideCallbacks: []}));
    layers.forEach(viewportLayerCount);
    tick();
    for (let pan = 1; pan <= 3; pan++) {
      layers.forEach(layer => { layer.viewport = pan; layer.changeEndCallbacks[0](); });
      tick();
    }
    assert.equal(calls.length, 2);
    calls[0].resolve(100);
    await settle();
    assert.equal(calls.length, 3);
    assert.equal(calls[2].viewport, 3);
    assert.equal(layers[0].viewConfig.headerBtn[0].textContent, '(…)');
    calls[2].resolve(3);
    await settle();
    assert.equal(layers[0].viewConfig.headerBtn[0].textContent, '(3)');
    layers[1].hideCallbacks[0]();
    calls[1].resolve(999);
    await settle();
    assert.equal(calls.length, 3);
    assert.equal(layers[1].viewConfig.headerBtn[0].hidden, true);
    layers[1].showCallbacks[0]();
    tick();
    assert.equal(calls.length, 4);
    calls[3].resolve(4);
    await settle();
    assert.deepEqual(layers[1].filter, {default: {status: 'ready'}, viewport: true});
  } finally {
    Object.assign(globalThis, original);
  }
});
