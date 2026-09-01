const MAX_RETRIES = 2;
const BASE_DELAY_MS = 500;
const MAX_DELAY_MS = 30_000;
// The pinned OpenLayers 10.8 global bundle does not export TileState.
const LOADING = 1;
const ERROR = 3;

const RETRYABLE_STATUSES = new Set([408, 429, 500, 502, 503, 504]);
const enabledMapviews = new WeakSet();
const configuredSources = new WeakSet();
let decoratedLayerApi = null;

class TileRequestError extends Error {
  constructor(status) {
    super(status === null ? 'Tile request failed.' : `Tile request returned HTTP ${status}.`);
    this.name = 'TileRequestError';
    this.status = status;
  }
}

function retryAfterMilliseconds(value, now = Date.now()) {
  if (typeof value !== 'string' || !value.trim()) return 0;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, date - now) : 0;
}

function retryDelay(retryNumber, retryAfter, options = {}) {
  const random = options.random ?? Math.random;
  const now = options.now ?? Date.now;
  const backoff = Math.min(
    BASE_DELAY_MS * (2 ** Math.max(0, retryNumber - 1)),
    MAX_DELAY_MS,
  );
  const requested = retryAfterMilliseconds(retryAfter, now());
  const base = Math.min(MAX_DELAY_MS, Math.max(backoff, requested));
  return Math.min(MAX_DELAY_MS, Math.ceil(base + (base * 0.2 * random())));
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function discardResponse(response) {
  try {
    await response.body?.cancel?.();
  } catch {
    // Preserve the HTTP status as the terminal or retry decision.
  }
}

async function fetchWithRetry(url, options = {}) {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const waitImpl = options.waitImpl ?? wait;
  const random = options.random ?? Math.random;
  const now = options.now ?? Date.now;
  const shouldContinue = options.shouldContinue ?? (() => true);
  let retries = 0;

  while (shouldContinue()) {
    let response;
    try {
      response = await fetchImpl(url, options.requestInit);
      if (response.ok) {
        return options.consume ? await options.consume(response) : response;
      }
    } catch (error) {
      if (retries >= MAX_RETRIES || !shouldContinue()) throw error;
      retries += 1;
      await waitImpl(retryDelay(retries, null, {random, now}));
      continue;
    }

    if (!RETRYABLE_STATUSES.has(response.status) || retries >= MAX_RETRIES) {
      await discardResponse(response);
      throw new TileRequestError(response.status);
    }

    await discardResponse(response);
    retries += 1;
    await waitImpl(retryDelay(
      retries,
      response.headers?.get?.('retry-after'),
      {random, now},
    ));
  }

  throw new TileRequestError(null);
}

function isLoading(tile) {
  return tile?.getState?.() === LOADING;
}

function markError(tile) {
  if (isLoading(tile)) tile.setState(ERROR);
}

function createMvtTileLoadFunction(options = {}) {
  return (tile, url) => {
    tile.setLoader(async (extent, _resolution, projection) => {
      try {
        const data = await fetchWithRetry(url, {
          ...options,
          requestInit: {credentials: 'same-origin'},
          shouldContinue: () => isLoading(tile),
          consume: (response) => response.arrayBuffer(),
        });
        if (!isLoading(tile)) return;
        const features = tile.getFormat().readFeatures(data, {
          extent,
          featureProjection: projection,
        });
        if (isLoading(tile)) tile.setFeatures(features);
      } catch {
        markError(tile);
      }
    });
  };
}

function createRasterTileLoadFunction(options = {}) {
  const urlApi = options.urlApi ?? globalThis.URL;
  return async (tile, url) => {
    try {
      const blob = await fetchWithRetry(url, {
        ...options,
        requestInit: {credentials: 'same-origin'},
        shouldContinue: () => isLoading(tile),
        consume: (response) => response.blob(),
      });
      if (!isLoading(tile)) return;
      const objectUrl = urlApi.createObjectURL(blob);
      const image = tile.getImage();
      const release = () => {
        image.removeEventListener?.('load', release);
        image.removeEventListener?.('error', release);
        urlApi.revokeObjectURL(objectUrl);
      };
      image.addEventListener?.('load', release);
      image.addEventListener?.('error', release);
      image.src = objectUrl;
    } catch {
      markError(tile);
    }
  };
}

function isSameOrigin(uri, locationLike = globalThis.location) {
  if (typeof uri !== 'string' || !locationLike?.href) return false;
  try {
    return new URL(uri, locationLike.href).origin === locationLike.origin;
  } catch {
    return false;
  }
}

function attachCrossOriginRasterRetry(source, options = {}) {
  const attempts = new WeakMap();
  const schedule = options.schedule ?? setTimeout;
  const random = options.random ?? Math.random;
  const now = options.now ?? Date.now;

  source.on('tileloadend', ({tile}) => attempts.delete(tile));
  source.on('tileloaderror', ({tile}) => {
    const retries = attempts.get(tile) ?? 0;
    if (retries >= MAX_RETRIES) return;
    const retryNumber = retries + 1;
    attempts.set(tile, retryNumber);
    schedule(() => {
      if (tile?.getState?.() === ERROR) tile.load();
    }, retryDelay(retryNumber, null, {random, now}));
  });
}

function installLayerRetry(layer, options = {}) {
  if (layer?.format === 'mvt') {
    const loader = createMvtTileLoadFunction(options);
    for (const source of new Set([layer.source, layer.featureSource])) {
      if (
        !source
        || typeof source.setTileLoadFunction !== 'function'
        || configuredSources.has(source)
      ) continue;
      source.setTileLoadFunction(loader);
      configuredSources.add(source);
    }
    return;
  }

  if (layer?.format !== 'tiles') return;
  const source = layer.L?.getSource?.();
  if (
    !source
    || !['object', 'function'].includes(typeof source)
    || configuredSources.has(source)
  ) return;
  if (isSameOrigin(layer.URI, options.locationLike)) {
    if (typeof source.setTileLoadFunction !== 'function') return;
    source.setTileLoadFunction(createRasterTileLoadFunction(options));
  } else {
    if (typeof source.on !== 'function') return;
    attachCrossOriginRasterRetry(source, options);
  }
  configuredSources.add(source);
}

async function installAfterDecoration(layerPromise) {
  const layer = await layerPromise;
  if (layer && enabledMapviews.has(layer.mapview)) installLayerRetry(layer);
  return layer;
}

function tileRetry(_configuration, mapview) {
  if (!mapview || typeof mapview !== 'object') return;
  enabledMapviews.add(mapview);
  const layerApi = globalThis.mapp?.layer;
  if (!layerApi || decoratedLayerApi === layerApi) return;
  layerApi.decorate = globalThis.mapp.utils.compose(
    layerApi.decorate.bind(),
    installAfterDecoration,
  );
  decoratedLayerApi = layerApi;
}

function registerTileRetry(target = globalThis.mapp) {
  if (target?.plugins) target.plugins.tile_retry = tileRetry;
}

registerTileRetry();

export {
  ERROR,
  LOADING,
  MAX_RETRIES,
  RETRYABLE_STATUSES,
  TileRequestError,
  attachCrossOriginRasterRetry,
  createMvtTileLoadFunction,
  createRasterTileLoadFunction,
  fetchWithRetry,
  installLayerRetry,
  isSameOrigin,
  registerTileRetry,
  retryAfterMilliseconds,
  retryDelay,
  tileRetry,
};
