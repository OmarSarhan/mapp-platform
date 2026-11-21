// Isolated candidate preview against assets extracted from the built XYZ image.
// XYZ_PREVIEW_ASSETS contains public/ and ol.js; no live services are contacted.
import {chromium} from 'playwright';
import {readFile, mkdir} from 'node:fs/promises';
import assert from 'node:assert/strict';
import path from 'node:path';

const assets = process.env.XYZ_PREVIEW_ASSETS;
assert.ok(assets, 'Set XYZ_PREVIEW_ASSETS to the extracted candidate assets');
const root = path.resolve(import.meta.dirname, '../..');
const output = path.join(root, 'var/evidence/xyz-v5');
await mkdir(output, {recursive: true});
const browser = await chromium.launch({headless: true});
try {
  const page = await browser.newPage({viewport: {width: 1000, height: 700}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const attempts = new Map();
  let count = 3;
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    const uri = url.pathname;
    if (uri === '/') return route.fulfill({contentType: 'text/html', body:
      '<link rel="stylesheet" href="/public/css/ui.css"><div id="map" style="height:450px"></div><div id="drawer"></div>'});
    if (uri === '/api/query') return route.fulfill({json: count});
    if (uri.startsWith('/tiles/')) {
      const n = (attempts.get(uri) || 0) + 1;
      attempts.set(uri, n);
      if (n === 1) return route.fulfill({status: 503, body: 'retry'});
      return route.fulfill({contentType: 'image/png', body: Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64')});
    }
    const file = uri.startsWith('/instance/')
      ? path.join(root, 'instance/public', uri.slice('/instance/'.length))
      : path.join(assets, uri);
    try {
      return route.fulfill({body: await readFile(file), contentType:
        uri.endsWith('.css') ? 'text/css' : 'text/javascript'});
    } catch { return route.abort(); }
  });
  await page.goto('http://candidate.test/');
  await page.addScriptTag({url: '/ol.js'});
  await page.evaluate(async () => {
    await import('/public/js/lib/mapp.js');
    await import('/public/js/lib/ui.js');
    await import('/instance/plugins/tile-retry/index.mjs');
    await import('/instance/plugins/viewport-layer-count/index.mjs');
    if (mapp.version !== '5.0.9') throw Error('Unexpected candidate version');
    globalThis.preview = await mapp.Mapview({target: 'map', locale: {}, view: {center: [0, 0], zoom: 2}});
    mapp.plugins.tile_retry({}, preview);
    [globalThis.layer] = await preview.addLayer({key: 'fixture', name: 'Candidate features',
      format: 'tiles', source: 'XYZ', URI: '/tiles/{z}/{x}/{y}.png', display: true,
      table: 'fixture', geom: 'geom', qID: 'id', viewport_layer_count: {debounce: 0}});
    mapp.ui.layers.view(layer);
    document.querySelector('#drawer').append(layer.view);
  });
  await page.getByLabel('Candidate features: 3 features in viewport').waitFor();
  await page.waitForTimeout(2000);
  assert.ok([...attempts.values()].some(n => n === 2), 'Transient tile failure retried');
  assert.ok([...attempts.values()].every(n => n <= 3), 'Retries bounded');
  await page.screenshot({path: path.join(output, 'plugins.png')});
  await page.evaluate(() => layer.hide());
  await page.getByLabel('Candidate features: 3 features in viewport').waitFor({state: 'hidden'});
  count = 7;
  await page.evaluate(() => layer.show());
  await page.getByLabel('Candidate features: 7 features in viewport').waitFor();
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({version: '5.0.9', countBefore: 3, countAfter: 7,
    tileAttempts: Object.fromEntries(attempts), pageErrors: errors, screenshot: output}));
} finally { await browser.close(); }
