const BADGE_CLASS = 'mapp-viewport-layer-count';

function formatCount(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString() : null;
}

function isCountable(layer) {
  return Boolean(layer.tableCurrent?.() && layer.L?.getVisible?.());
}

function setLoading(layer, badge) {
  badge.hidden = false;
  badge.textContent = '(…)';
  badge.setAttribute('aria-label', `${layer.name}: counting features in viewport`);
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
  setLoading(layer, badge);
  return badge;
}

async function refresh(layer, badge, state) {
  if (!isCountable(layer)) {
    state.pending = false;
    badge.hidden = true;
    return;
  }
  if (state.active) {
    state.pending = true;
    setLoading(layer, badge);
    return;
  }

  const request = state.request;
  state.active = true;
  setLoading(layer, badge);

  try {
    const count = formatCount(await mapp.ui.utils.locationCount(layer));
    if (request !== state.request) return;
    if (!isCountable(layer)) {
      badge.hidden = true;
      return;
    }
    badge.textContent = count === null ? '(–)' : `(${count})`;
    badge.setAttribute(
      'aria-label',
      `${layer.name}: ${count ?? 'unknown'} features in viewport`,
    );
  } catch (error) {
    if (request !== state.request) return;
    if (!isCountable(layer)) {
      badge.hidden = true;
      return;
    }
    badge.textContent = '(–)';
    badge.setAttribute('aria-label', `${layer.name}: viewport count unavailable`);
    console.warn(`Viewport count failed for layer [${layer.key}].`, error);
  } finally {
    state.active = false;
    if (state.pending) {
      state.pending = false;
      refresh(layer, badge, state);
    }
  }
}

function viewportLayerCount(layer) {
  if (!layer?.tableCurrent || !layer?.qID || !layer?.mapview) return;

  layer.filter ??= {};
  layer.filter.viewport = true;

  const badge = makeBadge(layer);
  const state = {request: 0, timer: null, active: false, pending: false};
  const delay = Number.isFinite(layer.viewport_layer_count?.debounce)
    ? Math.max(0, layer.viewport_layer_count.debounce)
    : 250;
  const schedule = () => {
    state.request++;
    state.pending = false;
    clearTimeout(state.timer);
    state.timer = setTimeout(() => refresh(layer, badge, state), delay);
  };

  layer.viewConfig ??= {};
  layer.viewConfig.headerBtn ??= [];
  layer.viewConfig.headerBtn.unshift(badge);
  layer.changeEndCallbacks.push(schedule);
  layer.showCallbacks.push(() => {
    if (isCountable(layer)) setLoading(layer, badge);
    else badge.hidden = true;
    schedule();
  });
  layer.hideCallbacks.push(() => {
    state.request++;
    state.pending = false;
    clearTimeout(state.timer);
    badge.hidden = true;
  });
  schedule();
}

mapp.plugins.viewport_layer_count = viewportLayerCount;

export {formatCount, viewportLayerCount};
