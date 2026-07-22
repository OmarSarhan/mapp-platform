const BADGE_CLASS = 'mapp-viewport-layer-count';

function formatCount(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString() : null;
}

function makeBadge(layer) {
  const badge = document.createElement('span');
  badge.className = BADGE_CLASS;
  badge.dataset.id = 'viewport-layer-count';
  badge.setAttribute('aria-live', 'polite');
  badge.setAttribute('title', 'Features in the current map viewport');
  badge.style.cssText = [
    'align-self:center',
    'font-size:.8em',
    'font-variant-numeric:tabular-nums',
    'font-weight:600',
    'margin-inline:.3em auto',
    'opacity:.72',
    'white-space:nowrap',
  ].join(';');
  badge.textContent = '(…)';
  badge.setAttribute('aria-label', `${layer.name}: counting features in viewport`);
  return badge;
}

async function refresh(layer, badge, state) {
  if (!layer.tableCurrent?.() || !layer.L?.getVisible?.()) {
    badge.hidden = true;
    return;
  }

  badge.hidden = false;
  const request = ++state.request;
  badge.textContent = '(…)';

  try {
    const count = formatCount(await mapp.ui.utils.locationCount(layer));
    if (request !== state.request) return;
    badge.textContent = count === null ? '(–)' : `(${count})`;
    badge.setAttribute(
      'aria-label',
      `${layer.name}: ${count ?? 'unknown'} features in viewport`,
    );
  } catch (error) {
    if (request !== state.request) return;
    badge.textContent = '(–)';
    badge.setAttribute('aria-label', `${layer.name}: viewport count unavailable`);
    console.warn(`Viewport count failed for layer [${layer.key}].`, error);
  }
}

function viewportLayerCount(layer) {
  if (!layer?.tableCurrent || !layer?.qID || !layer?.mapview) return;

  layer.filter ??= {};
  layer.filter.viewport = true;

  const badge = makeBadge(layer);
  const state = {request: 0, timer: null};
  const delay = Number.isFinite(layer.viewport_layer_count?.debounce)
    ? Math.max(0, layer.viewport_layer_count.debounce)
    : 250;
  const schedule = () => {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => refresh(layer, badge, state), delay);
  };

  layer.viewConfig ??= {};
  layer.viewConfig.headerBtn ??= [];
  layer.viewConfig.headerBtn.unshift(badge);
  layer.changeEndCallbacks.push(schedule);
  layer.showCallbacks.push(schedule);
  layer.hideCallbacks.push(() => {
    state.request++;
    clearTimeout(state.timer);
    badge.hidden = true;
  });
  schedule();
}

mapp.plugins.viewport_layer_count = viewportLayerCount;

export {formatCount, viewportLayerCount};
