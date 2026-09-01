import {afterEach, describe, expect, test, vi} from 'vitest';

import {
  ERROR,
  LOADING,
  TileRequestError,
  createMvtTileLoadFunction,
  fetchWithRetry,
  installLayerRetry,
  retryAfterMilliseconds,
  retryDelay,
  tileRetry,
} from '../../instance/public/plugins/tile-retry/index.mjs';

function response(status, options = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name) => name === 'retry-after' ? options.retryAfter ?? null : null,
    },
    body: options.body ?? null,
    arrayBuffer: async () => options.data ?? new ArrayBuffer(0),
    blob: async () => options.blob ?? new Blob(['tile']),
  };
}

function tileSource() {
  return {
    on: vi.fn(),
    setTileLoadFunction: vi.fn(),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('tile retry plugin', () => {
  test('retries only the bounded transient status set and honors Retry-After', async () => {
    const cancel500 = vi.fn();
    const cancel429 = vi.fn();
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(response(500, {body: {cancel: cancel500}}))
      .mockResolvedValueOnce(response(429, {
        body: {cancel: cancel429},
        retryAfter: '2',
      }))
      .mockResolvedValueOnce(response(200));
    const delays = [];

    const result = await fetchWithRetry('/tile', {
      fetchImpl,
      waitImpl: async (delay) => delays.push(delay),
      random: () => 0,
      now: () => 1_000,
    });

    expect(result.status).toBe(200);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(delays).toEqual([500, 2_000]);
    expect(cancel500).toHaveBeenCalledOnce();
    expect(cancel429).toHaveBeenCalledOnce();

    for (const status of [400, 401, 403, 404]) {
      const terminalFetch = vi.fn().mockResolvedValue(response(status));
      await expect(fetchWithRetry('/terminal', {
        fetchImpl: terminalFetch,
        waitImpl: vi.fn(),
      })).rejects.toMatchObject({status});
      expect(terminalFetch).toHaveBeenCalledOnce();
    }
  });

  test('stops after two retries and parses Retry-After dates within the cap', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response(503));
    const delays = [];

    await expect(fetchWithRetry('/busy', {
      fetchImpl,
      waitImpl: async (delay) => delays.push(delay),
      random: () => 0,
    })).rejects.toBeInstanceOf(TileRequestError);

    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(delays).toEqual([500, 1_000]);
    expect(retryAfterMilliseconds(
      'Thu, 01 Jan 2026 00:00:05 GMT',
      Date.parse('Thu, 01 Jan 2026 00:00:00 GMT'),
    )).toBe(5_000);
    expect(retryDelay(1, '120', {random: () => 0})).toBe(30_000);
  });

  test('marks an MVT tile as an error after both retries are exhausted', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response(504));
    let loader;
    let state = LOADING;
    const tile = {
      getState: () => state,
      setLoader: (value) => { loader = value; },
      setState: vi.fn((value) => { state = value; }),
    };
    createMvtTileLoadFunction({
      fetchImpl,
      waitImpl: async () => {},
      random: () => 0,
    })(tile, '/api/query?template=mvt');

    await loader([], 1, {});

    expect(fetchImpl).toHaveBeenCalledTimes(3);
    expect(tile.setState).toHaveBeenLastCalledWith(ERROR);
  });

  test('retries a failed MVT response-body transfer', async () => {
    const failedBody = response(200);
    failedBody.arrayBuffer = vi.fn().mockRejectedValue(new TypeError('stream reset'));
    const data = new ArrayBuffer(8);
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(failedBody)
      .mockResolvedValueOnce(response(200, {data}));
    let loader;
    const tile = {
      getFormat: () => ({readFeatures: () => []}),
      getState: () => LOADING,
      setFeatures: vi.fn(),
      setLoader: (value) => { loader = value; },
      setState: vi.fn(),
    };
    createMvtTileLoadFunction({
      fetchImpl,
      waitImpl: async () => {},
      random: () => 0,
    })(tile, '/api/query?template=mvt');

    await loader([], 1, {});

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(tile.setFeatures).toHaveBeenCalledWith([]);
  });

  test('loads MVT data with the tile format and installs on both MVT sources', async () => {
    const source = tileSource();
    const featureSource = tileSource();
    installLayerRetry({format: 'mvt', source, featureSource});
    expect(source.setTileLoadFunction).toHaveBeenCalledOnce();
    expect(featureSource.setTileLoadFunction).toHaveBeenCalledOnce();

    const data = new ArrayBuffer(4);
    const features = [{id: 1}];
    const format = {readFeatures: vi.fn(() => features)};
    let loader;
    let state = LOADING;
    const tile = {
      getFormat: () => format,
      getState: () => state,
      setFeatures: vi.fn(() => { state = 2; }),
      setLoader: vi.fn((value) => { loader = value; }),
      setState: vi.fn((value) => { state = value; }),
    };
    const tileLoad = createMvtTileLoadFunction({
      fetchImpl: vi.fn().mockResolvedValue(response(200, {data})),
    });
    const extent = [0, 1, 2, 3];
    const projection = {code: 'EPSG:3857'};

    tileLoad(tile, '/api/query?template=mvt');
    await loader(extent, 1, projection);

    expect(format.readFeatures).toHaveBeenCalledWith(data, {
      extent,
      featureProjection: projection,
    });
    expect(tile.setFeatures).toHaveBeenCalledWith(features);
    expect(tile.setState).not.toHaveBeenCalledWith(ERROR);
  });

  test('uses a status-aware loader for same-origin raster tiles', async () => {
    const source = tileSource();
    const fetchImpl = vi.fn().mockResolvedValue(response(200));
    const urlApi = {
      createObjectURL: vi.fn(() => 'blob:tile'),
      revokeObjectURL: vi.fn(),
    };
    installLayerRetry({
      format: 'tiles',
      URI: '/tiles/{z}/{x}/{y}.png',
      L: {getSource: () => source},
    }, {
      fetchImpl,
      locationLike: {href: 'https://maps.example/', origin: 'https://maps.example'},
      urlApi,
    });

    const load = source.setTileLoadFunction.mock.calls[0][0];
    const image = {addEventListener: vi.fn(), src: ''};
    const tile = {
      getImage: () => image,
      getState: () => LOADING,
      setState: vi.fn(),
    };
    await load(tile, 'https://maps.example/tiles/1/2/3.png');

    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(urlApi.createObjectURL).toHaveBeenCalledOnce();
    expect(image.src).toBe('blob:tile');
  });

  test('retries a failed raster response-body transfer', async () => {
    const source = tileSource();
    const failedBody = response(200);
    failedBody.blob = vi.fn().mockRejectedValue(new TypeError('stream reset'));
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(failedBody)
      .mockResolvedValueOnce(response(200));
    installLayerRetry({
      format: 'tiles',
      URI: '/tiles/{z}/{x}/{y}.png',
      L: {getSource: () => source},
    }, {
      fetchImpl,
      locationLike: {href: 'https://maps.example/', origin: 'https://maps.example'},
      random: () => 0,
      waitImpl: async () => {},
      urlApi: {createObjectURL: () => 'blob:tile', revokeObjectURL: vi.fn()},
    });
    const image = {addEventListener: vi.fn(), src: ''};
    const tile = {
      getImage: () => image,
      getState: () => LOADING,
      setState: vi.fn(),
    };

    await source.setTileLoadFunction.mock.calls[0][0](tile, '/tiles/1/2/3.png');

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(image.src).toBe('blob:tile');
  });

  test('skips tile sources without the relevant OpenLayers methods', () => {
    expect(() => installLayerRetry({
      format: 'mvt',
      source: 'XYZ',
      featureSource: {},
    })).not.toThrow();
    expect(() => installLayerRetry({
      format: 'tiles',
      URI: '/tiles/{z}/{x}/{y}.png',
      L: {getSource: () => ({})},
    }, {
      locationLike: {href: 'https://maps.example/', origin: 'https://maps.example'},
    })).not.toThrow();
  });

  test('retains cross-origin image loading and retries tile errors twice', () => {
    const handlers = {};
    const source = tileSource();
    const originalLoader = vi.fn();
    source.getTileLoadFunction = vi.fn(() => originalLoader);
    source.on.mockImplementation((event, handler) => { handlers[event] = handler; });
    const delays = [];
    installLayerRetry({
      format: 'tiles',
      URI: 'https://tiles.example/{z}/{x}/{y}.png',
      L: {getSource: () => source},
    }, {
      locationLike: {href: 'https://maps.example/', origin: 'https://maps.example'},
      random: () => 0,
      schedule: (callback, delay) => {
        delays.push(delay);
        callback();
      },
    });
    const tile = {getState: () => ERROR, load: vi.fn()};

    handlers.tileloaderror({tile});
    handlers.tileloaderror({tile});
    handlers.tileloaderror({tile});

    expect(source.setTileLoadFunction).not.toHaveBeenCalled();
    expect(source.getTileLoadFunction()).toBe(originalLoader);
    expect(tile.load).toHaveBeenCalledTimes(2);
    expect(delays).toEqual([500, 1_000]);
  });

  test('the synchronous locale hook wraps decoration before a layer is shown', async () => {
    const source = tileSource();
    const mapview = {};
    const originalDecorate = vi.fn(async (layer) => layer);
    const mapp = {
      layer: {decorate: originalDecorate},
      plugins: {},
      utils: {
        compose: (...functions) => (argument) => functions.reduce(
          (value, fn) => fn(value),
          argument,
        ),
      },
    };
    vi.stubGlobal('mapp', mapp);
    tileRetry({}, mapview);

    const layer = {format: 'mvt', mapview, source, featureSource: tileSource()};
    expect(source.setTileLoadFunction).not.toHaveBeenCalled();
    await mapp.layer.decorate(layer);
    expect(source.setTileLoadFunction).toHaveBeenCalledOnce();
  });
});
