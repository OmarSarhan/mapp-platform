// Isolated helper/clone probe, not browser input-to-paint or allocation profiling.
import {performance} from 'node:perf_hooks';
import {renderedLocales, activeLocale} from './src/api.js';

for (const count of [100, 1000]) {
  const workspace = {
    locale: {layers: Object.fromEntries(Array.from({length: count}, (_, i) => [
      `layer${i}`, {name: `Layer ${i}`, infoj: Array.from({length: 20}, (_, j) => ({field: `field${j}`, title: `Field ${j}`}))},
    ]))},
    locales: Object.fromEntries(Array.from({length: 5}, (_, i) => [`named${i}`, {name: `Named ${i}`} ])),
  };
  const measure = fn => {
    fn();
    const start = performance.now();
    for (let i = 0; i < 10; i++) fn();
    return Number(((performance.now() - start) / 10).toFixed(2));
  };
  const layer = workspace.locale.layers.layer0;
  console.log(JSON.stringify({layers: count,
    previousRenderMs: measure(() => {renderedLocales(workspace); renderedLocales(workspace)[0]}),
    changedWorkspaceRenderMs: measure(() => renderedLocales(workspace)),
    defaultLocaleMs: measure(() => activeLocale(workspace)),
    previousEditCloneMs: measure(() => structuredClone(workspace)),
    focusedEditCloneMs: measure(() => ({...workspace, locale: {...workspace.locale, layers: {...workspace.locale.layers, layer0: structuredClone(layer)}}})),
  }));
}
