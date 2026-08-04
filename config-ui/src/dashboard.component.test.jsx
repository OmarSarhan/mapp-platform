import React from 'react';
import {afterEach, describe, expect, test, vi} from 'vitest';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';

import {
  Dashboard,
  DerivedLayers,
  Root,
  Security,
  TOKEN_ACCESS_PRESETS,
  TOKEN_SCOPE_OPTIONS,
  derivedLayerFormDefinition,
  geometryKind,
  reconcileDerivedWorkspace,
} from './main.jsx';

const workspace = {
  key: 'demo',
  dbs: 'MAPP',
  locale: {
    name: 'Default',
    ScaleLine: 'metric',
    extent: {north: 54, east: 0, south: 53, west: -2, mask: false},
    view: {lat: 53.5, lng: -1, z: 10},
    layers: {},
  },
  locales: {
    cy: {name: 'Cymraeg'},
  },
};

const response = (payload, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  statusText: status >= 200 && status < 300 ? 'OK' : 'Request failed',
  json: async () => payload,
});

describe('Scoped token administration', () => {
  test('offers semantic privilege tiers and provisions the selected scopes', async () => {
    const created = [];
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (options.method === 'POST' && path === '/api/admin/tokens') {
        created.push(JSON.parse(options.body));
        return response({token: 'mapp_one_time', record: {id: 'token-1'}}, 201);
      }
      if (path === '/api/admin/tokens') {
        return response({tokens: []});
      }
      if (path === '/api/admin/device-authorizations') {
        return response({authorizations: []});
      }
      if (path === '/api/admin/audit') {
        return response({events: []});
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));

    render(<Security close={() => {}}/>);
    const access = await screen.findByLabelText('Token access level');
    expect(access.value).toBe('full');
    for (const scope of TOKEN_SCOPE_OPTIONS) {
      expect(screen.getByRole('checkbox', {name: new RegExp(scope.label)}).checked)
        .toBe(true);
    }
    expect(screen.getByText(
      'Selected scopes: full (all bearer-token workspace and semantic scopes)',
    )).toBeTruthy();
    fireEvent.change(access, {target: {value: 'semantic-administrator'}});
    fireEvent.click(screen.getByRole('button', {name: 'Create scoped CLI token'}));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]).toMatchObject({
      name: 'CLI operator',
      scopes: [
        'semantic:inspect',
        'semantic:source',
        'semantic:generate',
        'semantic:data',
        'semantic:propose',
        'semantic:apply',
        'semantic:admin',
      ],
    });
    expect(new Date(created[0].expires).getTime()).toBeGreaterThan(Date.now());
    expect(await screen.findByText('mapp_one_time')).toBeTruthy();
    expect(Object.fromEntries(TOKEN_ACCESS_PRESETS.map(item => [
      item.id,
      item.scopes,
    ]))).toEqual({
      'semantic-reader': ['semantic:inspect'],
      'semantic-proposer': ['semantic:inspect', 'semantic:propose'],
      'semantic-ai-author': [
        'semantic:inspect',
        'semantic:source',
        'semantic:generate',
        'semantic:data',
        'semantic:propose',
      ],
      'semantic-curator': [
        'semantic:inspect',
        'semantic:propose',
        'semantic:apply',
      ],
      'semantic-operator': ['semantic:inspect', 'semantic:admin'],
      'semantic-administrator': [
        'semantic:inspect',
        'semantic:source',
        'semantic:generate',
        'semantic:data',
        'semantic:propose',
        'semantic:apply',
        'semantic:admin',
      ],
      full: ['full'],
    });
    expect(TOKEN_ACCESS_PRESETS.find(item => item.id === 'full')).toMatchObject({
      label: 'Full platform operator',
      help: expect.stringContaining('dashboard-session-only'),
    });
    expect(TOKEN_SCOPE_OPTIONS.map(item => item.id)).toEqual([
      'inspect',
      'propose',
      'visual',
      'apply',
      'reload',
      'derive',
      'semantic:inspect',
      'semantic:source',
      'semantic:generate',
      'semantic:data',
      'semantic:propose',
      'semantic:apply',
      'semantic:admin',
    ]);
  });

  test('provisions every named access level without expanding its scopes', async () => {
    const created = [];
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (options.method === 'POST' && path === '/api/admin/tokens') {
        created.push(JSON.parse(options.body));
        return response({
          token: `mapp_${created.length}`,
          record: {id: `token-${created.length}`},
        }, 201);
      }
      if (path === '/api/admin/tokens') {
        return response({tokens: []});
      }
      if (path === '/api/admin/device-authorizations') {
        return response({authorizations: []});
      }
      if (path === '/api/admin/audit') {
        return response({events: []});
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));

    render(<Security close={() => {}}/>);
    const access = await screen.findByLabelText('Token access level');
    const create = screen.getByRole('button', {
      name: 'Create scoped CLI token',
    });
    fireEvent.change(screen.getByLabelText('Token expiry'), {
      target: {value: 'never'},
    });
    expect(create.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('Confirm extended token lifetime'));
    expect(create.disabled).toBe(false);

    for (const preset of TOKEN_ACCESS_PRESETS) {
      fireEvent.change(access, {target: {value: preset.id}});
      await waitFor(() => expect(access.value).toBe(preset.id));
      fireEvent.click(create);
      await waitFor(() => expect(created).toHaveLength(
        TOKEN_ACCESS_PRESETS.indexOf(preset) + 1,
      ));
      expect(created.at(-1)).toEqual({
        name: 'CLI operator',
        scopes: preset.scopes,
        expires: null,
        extendedExpiryConfirmed: true,
      });
      await waitFor(() => expect(create.disabled).toBe(false));
    }
  });

  test('narrows full access before custom selection and confirms no expiry', async () => {
    const created = [];
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (options.method === 'POST' && path === '/api/admin/tokens') {
        created.push(JSON.parse(options.body));
        return response({token: 'mapp_custom', record: {id: 'token-2'}}, 201);
      }
      if (path === '/api/admin/tokens') {
        return response({tokens: []});
      }
      if (path === '/api/admin/device-authorizations') {
        return response({authorizations: []});
      }
      if (path === '/api/admin/audit') {
        return response({events: []});
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));

    render(<Security close={() => {}}/>);
    const access = await screen.findByLabelText('Token access level');
    const create = screen.getByRole('button', {name: 'Create scoped CLI token'});
    expect(access.value).toBe('full');
    expect(screen.getByText(
      'Selected scopes: full (all bearer-token workspace and semantic scopes)',
    )).toBeTruthy();

    const semanticInspect = screen.getByRole('checkbox', {
      name: /Inspect semantic catalog/,
    });
    fireEvent.click(semanticInspect);
    expect(access.value).toBe('custom');
    expect(screen.getByText(new RegExp(
      TOKEN_SCOPE_OPTIONS
        .filter(scope => scope.id !== 'semantic:inspect')
        .map(scope => scope.id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
        .join(', '),
    ))).toBeTruthy();
    expect(create.disabled).toBe(false);
    fireEvent.change(access, {target: {value: 'custom'}});
    expect(screen.getByText(/inspect, propose, visual/)).toBeTruthy();

    fireEvent.click(semanticInspect);
    for (const scope of TOKEN_SCOPE_OPTIONS) {
      expect(screen.getByRole('checkbox', {name: new RegExp(scope.label)}).checked)
        .toBe(true);
    }
    expect(screen.getAllByText(/semantic:inspect/).length).toBeGreaterThan(1);
    expect(create.disabled).toBe(false);

    for (const scope of TOKEN_SCOPE_OPTIONS) {
      const checkbox = screen.getByRole('checkbox', {name: new RegExp(scope.label)});
      if (checkbox.checked) fireEvent.click(checkbox);
    }
    expect(screen.getByText('Selected scopes: none')).toBeTruthy();
    expect(create.disabled).toBe(true);

    fireEvent.click(semanticInspect);
    fireEvent.change(screen.getByLabelText('Token expiry'), {
      target: {value: 'never'},
    });
    expect(create.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('Confirm extended token lifetime'));
    fireEvent.click(create);

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]).toEqual({
      name: 'CLI operator',
      scopes: ['semantic:inspect'],
      expires: null,
      extendedExpiryConfirmed: true,
    });
    expect(await screen.findByText('mapp_custom')).toBeTruthy();
  });
});

test('recognizes mixed-case PostGIS geometry metadata', () => {
  const columns = type => ({columns: [
    {name: 'geom_3857', geometryType: 'Geometry'},
    {name: 'geom', geometryType: type},
  ]});
  expect(geometryKind({geom: 'geom_3857'}, columns('Point'))).toBe('point');
  expect(geometryKind({geom: 'geom_3857'}, columns('MultiLineString'))).toBe('line');
  expect(geometryKind({geom: 'geom_3857'}, columns('MultiPolygon'))).toBe('polygon');
});

function standardFetch(workspaceRequest) {
  return vi.fn(async (path, options = {}) => {
    if (path === '/api/workspace' && options.method === 'POST') {
      return workspaceRequest();
    }
    if (path === '/api/workspace') {
      return response({workspace, revision: 'rev-1'});
    }
    if (path === '/api/catalog') {
      return response({tables: [], databases: ['MAPP']});
    }
    if (path === '/api/icons') {
      return response({icons: []});
    }
    if (path === '/api/plugins') {
      return response({plugins: {external: [], fingerprint: 'catalogue'}});
    }
    throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
  });
}

function successfulSave(savedWorkspace) {
  const fingerprint = 'a'.repeat(64);
  return response({
    saved: true,
    workspace: savedWorkspace,
    revision: 'rev-2',
    fingerprint,
    reload: {
      expectedWorkspaceFingerprint: fingerprint,
      requestedGeneration: 4,
      status: {
        requestedGeneration: 4,
        appliedGeneration: 4,
        workspaceFingerprint: fingerprint,
        healthy: true,
        completed: true,
      },
    },
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Dashboard managed save lifecycle', () => {
  test('requires an explicit action to add a selected catalog table as a layer', async () => {
    const catalogTable = {
      dbs: 'MAPP',
      schema: 'leeds',
      table: 'bus_stops',
      columns: [
        {name: 'id', type: 'integer', primaryKey: true, nullable: false},
        {name: 'geom', geometryType: 'Point', srid: 3857},
      ],
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') return response({workspace, revision: 'rev-1'});
      if (path === '/api/catalog') return response({tables: [catalogTable], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      if (path === '/api/plugins') return response({plugins: {external: [], fingerprint: 'catalogue'}});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    render(<Dashboard openSecurity={() => {}}/>);

    const catalogRow = await screen.findByRole('button', {name: /leeds\.bus_stops/});
    fireEvent.click(catalogRow);

    expect(catalogRow.getAttribute('aria-pressed')).toBe('true');
    expect(screen.queryByRole('button', {name: 'Bus Stops'})).toBeNull();

    fireEvent.click(screen.getByRole('button', {name: 'Add selected table as layer'}));
    expect(await screen.findByRole('button', {name: 'Bus Stops'})).toBeTruthy();
  });

  test('explains that advanced configuration is optional and links to its guide', async () => {
    vi.stubGlobal('fetch', standardFetch(() => response({})));
    render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByText('Templates and advanced locale JSON'));

    const guide = screen.getByRole('link', {
      name: 'Open the advanced-configuration guide',
    });
    expect(guide.getAttribute('href')).toBe('/advanced-configuration.html');
    expect(guide.closest('p').textContent).toContain(
      'Gazetteer setup belongs to an individual layer.',
    );
  });

  test('reconciles added and removed derived columns into workspace info fields', () => {
    const source = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Summary: {
            table: 'derived_layers.summary',
            qID: 'id',
            infoj: [
              {type: 'geometry', field: 'geom', fieldfx: 'ST_AsGeoJSON(geom)'},
              {title: 'Old name', field: 'old_name', type: 'text'},
              {title: 'Calculated', field: 'old_name', fieldfx: 'upper(name)'},
              {title: 'Kept', field: 'kept', type: 'text'},
            ],
            filter: {include: ['old_name', 'kept']},
            style: {
              hover: {field: 'old_name', display: true},
              theme: {
                type: 'categorized',
                field: 'old_name',
                categories: [{value: 'A', label: 'A', style: {
                  icon: {type: 'dot', fillColor: '#176b4d'},
                }}],
              },
            },
          },
        },
      },
    };
    const tables = [{
      schema: 'derived_layers',
      table: 'summary',
      columns: [
        {name: 'id', type: 'bigint'},
        {name: 'geom', type: 'geometry', geometryType: 'POINT', srid: 3857},
        {name: 'kept', type: 'text'},
        {name: 'new_count', type: 'integer'},
      ],
    }];
    const result = reconcileDerivedWorkspace(source, {
      name: 'summary',
      columnChanges: {added: ['new_count'], removed: ['old_name'], changed: []},
    }, tables);
    const layer = result.workspace.locale.layers.Summary;
    expect(layer.infoj.map(entry => entry.title)).toEqual([
      undefined, 'Calculated', 'Kept', 'New Count',
    ]);
    expect(layer.infoj.at(-1)).toMatchObject({
      field: 'new_count',
      type: 'integer',
      _dashboard: {catalogField: true},
    });
    expect(layer.filter.include).toEqual(['kept']);
    expect(layer.style.hover).toBeUndefined();
    expect(layer._dashboard.symbologyInspection).toEqual({
      fields: ['old_name'],
      reason: 'derived_schema_changed',
      relation: 'derived_layers.summary',
    });
    expect(result.summary).toEqual({layers: 1, added: 1, removed: 1});
    expect(source.locale.layers.Summary.infoj).toHaveLength(4);
  });

  test('refreshes the derived-layer form after converting its kind', async () => {
    let replacementPayload;
    const materialized = {
      name: 'hex_summary',
      kind: 'materialized',
      sources: ['leeds.bus_stops'],
      idColumn: 'id',
      geometryColumn: 'geom',
      description: 'Summary',
      query: 'SELECT id, geom FROM leeds.bus_stops',
      createdAt: '2026-07-19T12:00:00Z',
      createdBy: 'admin',
      refreshedAt: '2026-07-19T12:01:00Z',
      spatialScope: {type: 'workspace-map-extent', locale: 'Leeds'},
    };
    const converted = {
      ...materialized,
      kind: 'view',
      refreshedAt: null,
      userMessage: 'Saved successfully.',
    };
    let listKind = 'materialized';
    vi.stubGlobal('confirm', vi.fn(() => true));
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (path === '/api/derived-layers' && !options.method) {
        return response({derivedLayers: [{...materialized, kind: listKind}]});
      }
      if (path === '/api/derived-layers/capabilities') {
        return response({extensions: {postgis: '3.5'}, h3Available: true});
      }
      if (path === '/api/derived-layers/hex_summary' && !options.method) {
        return response({derivedLayer: materialized});
      }
      if (
        path === '/api/derived-layers/hex_summary/replace'
        && options.method === 'POST'
      ) {
        replacementPayload = JSON.parse(options.body);
        listKind = 'view';
        return response({derivedLayer: converted});
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));
    render(<DerivedLayers close={() => {}}/>);

    const actions = await screen.findByRole('combobox', {
      name: 'Edit or delete derived_layers.hex_summary',
    });
    fireEvent.change(actions, {target: {value: 'convert'}});

    await screen.findByRole('heading', {
      name: 'Edit derived_layers.hex_summary',
    });
    expect(screen.getByDisplayValue('view')).toBeTruthy();
    expect(screen.getByText(/view · leeds\.bus_stops/)).toBeTruthy();
    expect(replacementPayload.spatialScope).toEqual({
      type: 'workspace-map-extent',
      locale: 'Leeds',
    });
  });

  test('removes server metadata from editable derived-layer definitions', () => {
    expect(derivedLayerFormDefinition({
      name: 'hex_summary',
      kind: 'materialized',
      sources: ['leeds.bus_stops'],
      idColumn: 'id',
      geometryColumn: 'geom',
      description: null,
      query: 'SELECT id, geom FROM leeds.bus_stops',
      createdAt: '2026-07-19T12:00:00Z',
      createdBy: 'admin',
      refreshedAt: '2026-07-19T12:01:00Z',
      spatialScope: {type: 'workspace-map-extent', locale: 'Leeds'},
    })).toEqual({
      name: 'hex_summary',
      kind: 'materialized',
      sources: 'leeds.bus_stops',
      idColumn: 'id',
      geometryColumn: 'geom',
      description: '',
      query: 'SELECT id, geom FROM leeds.bus_stops',
      spatialScope: {type: 'workspace-map-extent', locale: 'Leeds'},
    });
  });

  test('scopes create requests and reports the successful materialization estimate', async () => {
    let createPayload;
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (path === '/api/derived-layers' && !options.method) {
        return response({derivedLayers: []});
      }
      if (path === '/api/derived-layers/capabilities') {
        return response({extensions: {postgis: '3.5'}, h3Available: true});
      }
      if (path === '/api/derived-layers' && options.method === 'POST') {
        createPayload = JSON.parse(options.body);
        return response({derivedLayer: {
          ...createPayload,
          queryPlanProbe: {
            estimatedFinalRows: 100000,
            maxIntermediateRows: 250000,
            maxIntermediateBytes: 128 * 1024 * 1024,
          },
          materializationProbe: {
            estimatedBytes: 512 * 1024 * 1024,
            actualBytes: 480 * 1024 * 1024,
            maxEstimatedBytes: 1024 ** 3,
          },
        }}, 201);
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));
    render(<DerivedLayers close={() => {}}/>);

    fireEvent.change(await screen.findByLabelText('Name'), {target: {value: 'large_places'}});
    fireEvent.change(screen.getByLabelText('Kind'), {target: {value: 'materialized'}});
    fireEvent.change(screen.getByLabelText('ID column'), {target: {value: 'id'}});
    fireEvent.change(screen.getByLabelText('Geometry column'), {target: {value: 'geom'}});
    fireEvent.change(screen.getByLabelText('Source relations'), {target: {value: 'leeds.places'}});
    fireEvent.change(screen.getByLabelText('One read-only SELECT'), {target: {value: 'SELECT id, geom FROM leeds.places'}});
    fireEvent.click(screen.getByRole('button', {name: 'Create derived relation'}));

    expect(await screen.findByText(/Planner-estimated materialized size: 512 MiB \(limit 1.0 GiB\)/)).toBeTruthy();
    expect(screen.getByText(/Actual stored materialized size: 480 MiB \(limit 1.0 GiB\)/)).toBeTruthy();
    expect(screen.getByText(/Planner-estimated query: 100,000 output rows · largest intermediate 250,000 rows · 128 MiB intermediate data/)).toBeTruthy();
    expect(createPayload.spatialScope).toEqual({type: 'workspace-map-extent'});
    expect(screen.getByText(/workspace map area at one zoom level out \(z−1\)/)).toBeTruthy();
  });

  test('offers to switch an oversized background materialization to a view without resubmitting', async () => {
    let createRequests = 0;
    const confirm = vi.fn(() => true);
    vi.stubGlobal('confirm', confirm);
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (path === '/api/derived-layers' && !options.method) {
        return response({derivedLayers: []});
      }
      if (path === '/api/derived-layers/capabilities') {
        return response({extensions: {postgis: '3.5'}, h3Available: true});
      }
      if (path === '/api/derived-layers' && options.method === 'POST') {
        createRequests += 1;
        return response({operation: {
          id: 'operation-1',
          status: 'failed',
          error: {
            status: 409,
            code: 'derived_layer.materialization_too_large',
            userMessage: 'The materialized result is too large.',
            suggestedAction: 'Create an ordinary view instead.',
            blocked: true,
            recommendedKind: 'view',
            probe: {estimatedBytes: 2 * 1024 ** 3, maxEstimatedBytes: 1024 ** 3},
          },
        }}, 202);
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));
    render(<DerivedLayers close={() => {}}/>);

    const kind = await screen.findByLabelText('Kind');
    fireEvent.change(kind, {target: {value: 'materialized'}});
    fireEvent.click(screen.getByRole('button', {name: 'Create derived relation'}));

    await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/Switch this form to an ordinary view/)));
    expect(kind.value).toBe('view');
    expect(createRequests).toBe(1);
  });

  test('does not switch or resubmit a query that exceeds the compute guard', async () => {
    let createRequests = 0;
    const confirm = vi.fn(() => true);
    vi.stubGlobal('confirm', confirm);
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (path === '/api/derived-layers' && !options.method) {
        return response({derivedLayers: []});
      }
      if (path === '/api/derived-layers/capabilities') {
        return response({extensions: {postgis: '3.5'}, h3Available: true});
      }
      if (path === '/api/derived-layers' && options.method === 'POST') {
        createRequests += 1;
        return response({operation: {
          id: 'operation-2',
          status: 'failed',
          error: {
            status: 409,
            code: 'derived_layer.query_too_expensive',
            userMessage: 'The query plan exceeds the compute guard.',
            suggestedAction: 'Rewrite the query or reduce its H3 expansion.',
            blocked: true,
            reasons: [{
              code: 'h3_scope_cells',
              message: 'Too many H3 cells.',
              suggestedAction: 'Use a coarser H3 resolution.',
            }],
            probe: {
              estimatedTotalCost: 20000000,
              h3Expansion: {resolutions: [12], estimatedScopeCells: 20000000},
            },
            technicalDetail: 'raw planner diagnostic',
          },
        }, meta: {requestId: 'req-compute'}}, 202);
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));
    render(<DerivedLayers close={() => {}}/>);

    const kind = await screen.findByLabelText('Kind');
    fireEvent.change(kind, {target: {value: 'materialized'}});
    fireEvent.click(screen.getByRole('button', {name: 'Create derived relation'}));

    expect(await screen.findByText(/Rewrite the query or reduce its H3 expansion/)).toBeTruthy();
    expect(screen.getByText('Too many H3 cells.')).toBeTruthy();
    expect(screen.getByText('Use a coarser H3 resolution.')).toBeTruthy();
    expect(screen.getByText(/No derived layer was created/)).toBeTruthy();
    const details = screen.getByText('Technical details').closest('details');
    expect(details.open).toBe(false);
    expect(details.textContent).toContain('derived_layer.query_too_expensive');
    expect(details.textContent).toContain('req-compute');
    expect(screen.getByText('raw planner diagnostic').closest('details')).toBe(details);
    expect(kind.value).toBe('materialized');
    expect(confirm).not.toHaveBeenCalled();
    expect(createRequests).toBe(1);
  });

  test('explains a query policy rejection without misclassifying it as H3 cost', async () => {
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (path === '/api/derived-layers' && !options.method) {
        return response({derivedLayers: []});
      }
      if (path === '/api/derived-layers/capabilities') {
        return response({extensions: {postgis: '3.5'}, h3Available: true});
      }
      if (path === '/api/derived-layers' && options.method === 'POST') {
        const message = 'The derived-layer query for “places” uses SQL that is not allowed. Relation places must be schema-qualified.';
        return response({
          error: message,
          userMessage: message,
          code: 'derived_layer.query_not_allowed',
          category: 'policy',
          blocked: true,
          stateUnchanged: true,
          safeState: 'No derived layer was created.',
          reasons: [{
            code: 'unqualified_relation',
            message: 'Relation places must be schema-qualified.',
            suggestedAction: 'Change it to a permitted relation such as leeds.places.',
          }],
          meta: {requestId: 'req-policy'},
        }, 422);
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));
    render(<DerivedLayers close={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Create derived relation'}));

    const alert = await screen.findByRole('alert');
    expect(alert.querySelector('strong').textContent)
      .toBe('The derived-layer query for “places” uses SQL that is not allowed.');
    expect(screen.getByText('Relation places must be schema-qualified.')).toBeTruthy();
    expect(screen.getByText(/Change it to a permitted relation/)).toBeTruthy();
    expect(screen.getByText(/No derived layer was created/)).toBeTruthy();
    expect(screen.queryByText(/lower the H3 resolution/i)).toBeNull();
    const details = screen.getByText('Technical details').closest('details');
    expect(details.textContent).toContain('derived_layer.query_not_allowed');
    expect(details.textContent).toContain('req-policy');
  });

  test('does not claim an unchanged database state for an indeterminate operation', async () => {
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (path === '/api/derived-layers' && !options.method) {
        return response({derivedLayers: []});
      }
      if (path === '/api/derived-layers/capabilities') {
        return response({extensions: {postgis: '3.5'}, h3Available: true});
      }
      if (path === '/api/derived-layers' && options.method === 'POST') {
        return response({operation: {
          id: 'operation-indeterminate',
          status: 'indeterminate',
          error: {
            status: 500,
            code: 'derived_layer.operation_failed',
            userMessage: 'The derived-layer operation ended without a confirmed result.',
            suggestedAction: 'Inspect the authoritative derived-layer catalog before retrying.',
            blocked: true,
            indeterminate: true,
          },
        }}, 202);
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));
    render(<DerivedLayers close={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Create derived relation'}));

    expect(await screen.findByText(/ended without a confirmed result/)).toBeTruthy();
    expect(screen.getByText(/Inspect the authoritative derived-layer catalog/)).toBeTruthy();
    expect(screen.queryByText(/Database state:/)).toBeNull();
    expect(screen.queryByText(/No derived layer was created/)).toBeNull();
    expect(screen.getByText('Technical details').closest('details').textContent)
      .toContain('Indeterminate');
  });

  test('loads the exact oversized refresh as a view draft without converting it', async () => {
    const materialized = {
      name: 'hex_summary',
      kind: 'materialized',
      sources: ['leeds.bus_stops'],
      idColumn: 'id',
      geometryColumn: 'geom',
      description: 'Summary',
      query: 'SELECT id, geom FROM leeds.bus_stops',
      spatialScope: {type: 'workspace-map-extent', locale: 'Leeds'},
    };
    let refreshRequests = 0;
    let showRequests = 0;
    let replaceRequests = 0;
    const confirm = vi.fn(() => true);
    vi.stubGlobal('confirm', confirm);
    vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
      if (path === '/api/derived-layers' && !options.method) {
        return response({derivedLayers: [materialized]});
      }
      if (path === '/api/derived-layers/capabilities') {
        return response({extensions: {postgis: '3.5'}, h3Available: true});
      }
      if (
        path === '/api/derived-layers/hex_summary/refresh'
        && options.method === 'POST'
      ) {
        refreshRequests += 1;
        return response({operation: {
          id: 'operation-refresh',
          status: 'failed',
          error: {
            status: 409,
            code: 'derived_layer.materialization_too_large',
            userMessage: 'The refreshed materialized result would be too large.',
            suggestedAction: 'Convert this layer to an ordinary view or reduce its output.',
            blocked: true,
            stateUnchanged: true,
            safeState: 'The existing materialized data remains active and unchanged.',
            recommendedKind: 'view',
            probeStage: 'estimate',
            probe: {
              estimatedBytes: 2 * 1024 ** 3,
              maxEstimatedBytes: 1024 ** 3,
            },
          },
        }}, 202);
      }
      if (path === '/api/derived-layers/hex_summary' && !options.method) {
        showRequests += 1;
        return response({derivedLayer: materialized});
      }
      if (
        path === '/api/derived-layers/hex_summary/replace'
        && options.method === 'POST'
      ) {
        replaceRequests += 1;
        return response({derivedLayer: {...materialized, kind: 'view'}});
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    }));
    render(<DerivedLayers close={() => {}}/>);

    const actions = await screen.findByRole('combobox', {
      name: 'Edit or delete derived_layers.hex_summary',
    });
    fireEvent.change(actions, {target: {value: 'refresh'}});

    await screen.findByRole('heading', {name: 'Edit derived_layers.hex_summary'});
    expect(screen.getByLabelText('Kind').value).toBe('view');
    expect(screen.getByLabelText('One read-only SELECT').value).toBe(materialized.query);
    expect(screen.getByText(/Review it and select Save derived layer to convert it/)).toBeTruthy();
    expect(screen.getByText(/existing materialized data remains active and unchanged/i)).toBeTruthy();
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(confirm.mock.calls[1][0]).toMatch(/will not change until you explicitly save/);
    expect(refreshRequests).toBe(1);
    expect(showRequests).toBe(1);
    expect(replaceRequests).toBe(0);
  });

  test('keeps the derived-layer menu interactive after loading its options', async () => {
    let resolveDerivedLayers;
    const derivedLayers = new Promise(resolve => {
      resolveDerivedLayers = resolve;
    });
    let derivedLayerRequests = 0;
    const openDerivedLayers = vi.fn();
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      if (path === '/api/derived-layers') {
        derivedLayerRequests += 1;
        return derivedLayers;
      }
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    render(
      <Dashboard
        openSecurity={() => {}}
        openDerivedLayers={openDerivedLayers}
      />,
    );

    const menu = await screen.findByRole('combobox', {
      name: 'Create or edit a derived layer',
    });
    await waitFor(() => expect(derivedLayerRequests).toBe(1));
    expect(menu.disabled).toBe(true);

    resolveDerivedLayers(response({
      derivedLayers: [{name: 'hex_summary'}],
    }));
    await waitFor(() => expect(menu.disabled).toBe(false));

    fireEvent.focus(menu);
    expect(menu.disabled).toBe(false);
    expect(derivedLayerRequests).toBe(1);

    fireEvent.change(menu, {target: {value: 'edit:hex_summary'}});
    expect(openDerivedLayers).toHaveBeenCalledWith({
      action: 'edit',
      name: 'hex_summary',
    });
  });

  test('groups layer navigation and edits folder membership as layer.group', async () => {
    const groupedWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          'Bus Stops': {
            name: 'Bus Stops',
            format: 'mvt',
            group: 'Transport',
            style: {
              default: {icon: {type: 'dot'}},
              hover: {display: true, field: 'stop_id', title: 'Feature'},
            },
            infoj: [
              {title: 'Stop ID', field: 'stop_id', type: 'text'},
            ],
          },
          'Rail Stations': {name: 'Rail Stations', format: 'mvt', group: 'Transport'},
          Boundaries: {name: 'Boundaries', format: 'mvt'},
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: groupedWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    expect(await screen.findByRole('heading', {name: 'Transport'})).toBeTruthy();
    fireEvent.click(screen.getByRole('button', {name: 'Bus Stops'}));
    const folder = screen.getByDisplayValue('Transport');
    expect(folder.value).toBe('Transport');
    expect(screen.getByLabelText(/Layer folder:/).getAttribute('aria-label'))
      .toMatch(/does not control map drawing order/);
    const drawingOrder = screen.getByLabelText(/Drawing order:/)
      .closest('label').querySelector('input');
    fireEvent.change(drawingOrder, {target: {value: '20'}});
    expect(drawingOrder.value).toBe('20');
    const promote = screen.getByLabelText(/Promote when shown:/)
      .closest('label').querySelector('input');
    fireEvent.click(promote);
    expect(promote.checked).toBe(true);
    fireEvent.change(folder, {target: {value: 'Public transport'}});
    expect(screen.getByRole('heading', {name: 'Public transport'})).toBeTruthy();
    const checks = screen.getAllByRole('checkbox');
    const hoverToggle = checks.find(
      checkbox => checkbox.getAttribute('aria-label')?.startsWith('Hover toggle')
        || checkbox.closest('label')?.textContent.includes('Hover toggle'),
    );
    const labelToggle = checks.find(
      checkbox => checkbox.closest('label')?.textContent.includes('Label toggle'),
    );
    expect(hoverToggle.checked).toBe(true);
    expect(labelToggle.disabled).toBe(true);
    fireEvent.click(hoverToggle);
    expect(screen.getByText(/Control order:/).textContent)
      .not.toMatch(/(?:^|→ )hover(?: →|$)/);
    const filterSelect = screen.getByRole('combobox', {
      name: /Interactive filter/,
    });
    fireEvent.change(filterSelect, {target: {value: 'Automatic'}});
    expect(filterSelect.value).toBe('Automatic');
  });

  test('limits interactive filters to choices compatible with each information type', async () => {
    const filteredWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Measurements: {
            name: 'Measurements',
            format: 'mvt',
            style: {default: {icon: {type: 'dot'}}},
            infoj: [
              {title: 'Name', field: 'name', type: 'text'},
              {title: 'Count', field: 'count', type: 'integer'},
              {title: 'Measured', field: 'measured_at', type: 'datetime'},
              {title: 'Photo', field: 'photo', type: 'image'},
              {
                title: 'Calculated score',
                field: 'score_rounded',
                fieldfx: 'round(score)::bigint',
                type: 'integer',
              },
            ],
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: filteredWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Measurements'}));
    const filters = screen.getAllByRole('combobox', {name: /Interactive filter/});
    const choices = select => [...select.options].map(option => option.textContent);

    expect(choices(filters[0])).toContain('Text prefix');
    expect(choices(filters[0])).not.toContain('Numeric range');
    expect(choices(filters[1])).toContain('Integer range');
    expect(choices(filters[1])).not.toContain('Text prefix');
    expect(choices(filters[2])).toContain('Date/time range');
    expect(choices(filters[2])).not.toContain('Date range');
    expect(filters[3].disabled).toBe(true);
    expect(choices(filters[3])).toEqual(['None']);
    expect(filters[4].disabled).toBe(true);
    expect(choices(filters[4])).toEqual(['None']);
    expect(screen.getByText(/XYZ filters need a real table column/)).toBeTruthy();
  });

  test('optionally configures a viewport count beside the layer name', async () => {
    const countedWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Places: {
            name: 'Places',
            format: 'mvt',
            table: 'public.places',
            geom: 'geom_3857',
            qID: 'id',
            style: {default: {icon: {type: 'dot'}}},
            infoj: [{title: 'Name', field: 'name', type: 'text'}],
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: countedWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Places'}));
    const toggle = screen.getByRole('checkbox', {
      name: /Show viewport count beside layer name/,
    });
    fireEvent.click(toggle);

    let managed = JSON.parse(screen.getByLabelText('Advanced layer JSON').value);
    expect(managed.plugins).toContain(
      '/instance/plugins/viewport-layer-count.mjs',
    );
    expect(managed.viewport_layer_count).toEqual({});
    expect(managed.filter.viewport).toBe(true);

    fireEvent.click(toggle);
    managed = JSON.parse(screen.getByLabelText('Advanced layer JSON').value);
    expect(managed.viewport_layer_count).toBeUndefined();
    expect(managed.plugins).toBeUndefined();
    expect(managed.filter.viewport).toBe(true);
  });

  test('structures layer tasks and gates controls behind their requirements', async () => {
    const configuredWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Places: {
            name: 'Places',
            format: 'mvt',
            table: 'public.places',
            geom: 'geom_3857',
            qID: 'id',
            style: {default: {icon: {type: 'dot'}}, hidden: true},
            filter: {hidden: true, viewport: true},
            infoj: [{title: 'Name', field: 'name', type: 'text'}],
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: configuredWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Places'}));
    const sections = [...container.querySelectorAll('.layer-section>summary span')]
      .map(element => element.textContent).slice(-6);
    expect(sections).toEqual([
      'Identity and display',
      'Data source',
      'Appearance and legend',
      'Interaction',
      'Feature information',
      'Advanced layer JSON',
    ]);

    fireEvent.click(screen.getByText('Interaction').closest('summary'));
    expect(screen.getByRole('checkbox', {name: /Opacity slider/}).disabled).toBe(true);
    expect(screen.getByRole('checkbox', {
      name: /Offer all compatible fields/,
    }).disabled).toBe(true);
    expect(screen.queryByRole('textbox', {name: /Count label/})).toBeNull();

    fireEvent.click(screen.getByRole('checkbox', {name: /Show Filtering panel/}));
    expect(screen.getByRole('checkbox', {
      name: /Offer all compatible fields/,
    }).disabled).toBe(false);
    expect(screen.getByRole('textbox', {name: /Count label/})).toBeTruthy();
  });

  test('optionally manages a geometry info symbol from the layer default style', async () => {
    const symbolWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Paths: {
            name: 'Paths',
            format: 'mvt',
            style: {
              default: {strokeColor: '#ff007b', strokeWidth: 3},
            },
            infoj: [{
              type: 'geometry',
              label: 'Path geometry',
              field: 'geom_3857',
              fieldfx: 'ST_AsGeoJSON(geom_3857)',
              display: true,
            }],
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: symbolWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Paths'}));
    const toggle = screen.getByRole('checkbox', {
      name: /Keep information symbol synchronized: Path geometry/,
    });
    expect(toggle.checked).toBe(false);
    fireEvent.click(toggle);

    const advanced = screen.getByLabelText('Advanced layer JSON');
    const managed = JSON.parse(advanced.value);
    expect(managed.infoj[0].style).toEqual({
      fillColor: null,
      strokeColor: '#ff007b',
      strokeWidth: 3,
    });
    expect(managed.infoj[0]._dashboard.styleFromLayerDefault).toBe(true);

    fireEvent.click(toggle);
    const removed = JSON.parse(advanced.value);
    expect(removed.infoj[0].style).toBeUndefined();
    expect(removed.infoj[0]._dashboard).toBeUndefined();
  });

  test('edits label-backed information fields without adding a title', async () => {
    const infoWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Places: {
            name: 'Places',
            format: 'mvt',
            style: {default: {icon: {type: 'dot'}}},
            infoj: [
              {type: 'geometry', field: 'geom'},
              {type: 'pin', label: 'Location', field: 'pin', fieldfx: 'ARRAY[0,0]'},
              {title: 'Name', field: 'name', type: 'text'},
            ],
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: infoWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Places'}));
    fireEvent.change(screen.getByDisplayValue('Location'), {
      target: {value: 'Map location'},
    });

    const managed = JSON.parse(screen.getByLabelText('Advanced layer JSON').value);
    expect(managed.infoj[1]).toMatchObject({
      type: 'pin',
      label: 'Map location',
    });
    expect(managed.infoj[1]).not.toHaveProperty('title');
  });

  test('identifies and previews data-driven symbology without treating it as static', async () => {
    const themedWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Status: {
            name: 'Status',
            format: 'mvt',
            style: {
              default: {icon: {type: 'dot', fillColor: '#777777'}},
              theme: {
                type: 'categorized',
                field: 'status',
                categories: [
                  {value: 'Open', label: 'Open', style: {
                    icon: {type: 'dot', fillColor: '#00aa44'},
                  }},
                  {value: 'Closed', label: 'Closed', style: {
                    icon: {type: 'square', fillColor: '#cc2233'},
                  }},
                ],
              },
            },
            infoj: [
              {type: 'geometry', field: 'geom'},
              {title: 'Status', field: 'status', type: 'text'},
            ],
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: themedWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Status'}));
    expect(screen.getByText(/Data-driven categorized symbology/).closest('p').textContent)
      .toContain('field status · 2 legend classes');
    expect(screen.getByText('Default / fallback symbology')).toBeTruthy();
    expect(screen.getAllByText('Open')
      .some(element => element.closest('.symbol-state'))).toBe(true);
    expect(screen.getAllByText('Closed')
      .some(element => element.closest('.symbol-state'))).toBe(true);
    expect(screen.getByText('Selected geometry')).toBeTruthy();
    const informationLegend = screen.getByText('Legend')
      .closest('.info-legend-preview');
    expect(informationLegend.textContent).toContain('Open');
    expect(informationLegend.textContent).toContain('Closed');
  });

  test('creates categorized symbology through guided dashboard controls', async () => {
    const staticWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Places: {
            name: 'Places',
            format: 'mvt',
            style: {default: {icon: {type: 'dot', fillColor: '#777777'}}},
            infoj: [
              {type: 'geometry', field: 'geom'},
              {title: 'Status', field: 'status', type: 'text'},
              {title: 'Priority', field: 'priority', type: 'integer'},
            ],
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: staticWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Places'}));
    fireEvent.change(screen.getByRole('combobox', {name: /Symbology mode/}), {
      target: {value: 'Data-driven categorized'},
    });
    expect(screen.getByRole('combobox', {name: /Category field/}).value)
      .toBe('status');
    fireEvent.change(screen.getByRole('combobox', {name: /Category field/}), {
      target: {value: 'priority'},
    });
    fireEvent.click(screen.getByRole('button', {name: 'Add legend category'}));
    fireEvent.change(screen.getByRole('spinbutton', {name: /Exact value/}), {
      target: {value: '10'},
    });
    fireEvent.change(screen.getByRole('textbox', {name: /Legend label/}), {
      target: {value: 'Open places'},
    });

    const managed = JSON.parse(screen.getByLabelText('Advanced layer JSON').value);
    expect(managed.style.theme).toMatchObject({
      type: 'categorized',
      field: 'priority',
      categories: [{value: 10, label: 'Open places'}],
    });
  });

  test('configures multi-field categorized point icons without a top-level field', async () => {
    const staticWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Places: {
            name: 'Places',
            format: 'mvt',
            style: {default: {icon: {type: 'dot', fillColor: '#777777'}}},
            infoj: [
              {type: 'geometry', field: 'geom'},
              {title: 'Status', field: 'status', type: 'text'},
              {title: 'Priority', field: 'priority', type: 'integer'},
            ],
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: staticWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Places'}));
    fireEvent.change(screen.getByRole('combobox', {name: /Symbology mode/}), {
      target: {value: 'Data-driven categorized'},
    });
    fireEvent.click(screen.getByRole('checkbox', {
      name: /Compose icons from multiple fields/,
    }));
    expect(screen.queryByRole('combobox', {name: /Category field/}))
      .toBeNull();
    fireEvent.click(screen.getByRole('checkbox', {name: 'priority'}));
    fireEvent.click(screen.getByRole('button', {name: 'Add legend category'}));
    fireEvent.change(screen.getByRole('combobox', {name: /Category field/}), {
      target: {value: 'priority'},
    });
    fireEvent.change(screen.getByRole('spinbutton', {name: /Exact value/}), {
      target: {value: '10'},
    });

    const managed = JSON.parse(screen.getByLabelText('Advanced layer JSON').value);
    expect(managed.style.theme).toMatchObject({
      type: 'categorized',
      fields: ['status', 'priority'],
      categories: [{field: 'priority', value: 10}],
    });
    expect(managed.style.theme).not.toHaveProperty('field');
  });

  test('uses display names in navigation and assigns line category stroke colours without catalog metadata', async () => {
    const lineWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          internal_routes_key: {
            name: 'Walking routes',
            format: 'mvt',
            geom: 'geom',
            style: {
              default: {strokeColor: '#008000', strokeWidth: 3},
            },
            infoj: [
              {type: 'geometry', field: 'geom'},
              {title: 'Length metres', field: 'length_metres', type: 'numeric'},
            ],
          },
          Walking_routes: {
            name: 'Reserved route key',
            format: 'mvt',
            style: {default: {strokeColor: '#333333'}},
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: lineWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    expect(await screen.findByRole('button', {name: 'Walking routes'})).toBeTruthy();
    expect(screen.queryByRole('button', {name: 'internal_routes_key'})).toBeNull();
    fireEvent.click(screen.getByRole('button', {name: 'Walking routes'}));
    const displayName = screen.getByRole('textbox', {name: /Display name/});
    fireEvent.change(displayName, {target: {value: 'Walking routes!'}});
    fireEvent.blur(displayName);
    expect(screen.getByRole('button', {name: 'Walking routes!'}).title)
      .toBe('Layer key: Walking_routes_1');
    expect(screen.getByText(/Line features use stroke colour/)).toBeTruthy();
    fireEvent.change(screen.getByRole('combobox', {name: /Symbology mode/}), {
      target: {value: 'Data-driven graduated'},
    });
    fireEvent.click(screen.getByRole('button', {name: 'Add legend category'}));
    fireEvent.click(screen.getByRole('button', {name: 'Add legend category'}));

    const managed = JSON.parse(
      container.querySelector('[aria-label="Advanced layer JSON"]').value,
    );
    expect(managed.style.theme.categories.map(category => category.style.strokeColor))
      .toEqual(['#176b4d', '#277da1']);
    expect(managed.style.theme.categories.every(category => (
      category.style.fillColor === undefined
    ))).toBe(true);
  });

  test('warns before replacing a configured theme and guides graduated and distributed modes', async () => {
    const modeWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Measures: {
            name: 'Measures',
            format: 'mvt',
            qID: 'id',
            style: {
              default: {icon: {type: 'dot', fillColor: '#777777'}},
              theme: {
                type: 'categorized',
                field: 'status',
                categories: [{value: 'Open', label: 'Open', style: {
                  icon: {type: 'dot', fillColor: '#00aa44'},
                }}],
              },
            },
            infoj: [
              {type: 'geometry', field: 'geom'},
              {title: 'Status', field: 'status', type: 'text'},
              {title: 'Score', field: 'score', type: 'numeric'},
            ],
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: modeWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false)
      .mockReturnValue(true);
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Measures'}));
    const mode = screen.getByRole('combobox', {name: /Symbology mode/});
    fireEvent.change(mode, {target: {value: 'Data-driven graduated'}});
    expect(JSON.parse(container.querySelector('[aria-label="Advanced layer JSON"]').value)
      .style.theme.type).toBe('categorized');

    fireEvent.change(mode, {target: {value: 'Data-driven graduated'}});
    let managed = JSON.parse(
      container.querySelector('[aria-label="Advanced layer JSON"]').value,
    );
    expect(managed.style.theme).toMatchObject({
      type: 'graduated',
      field: 'score',
      graduated_breaks: 'less_than',
      categories: [],
    });
    fireEvent.click(screen.getByRole('button', {name: 'Add legend category'}));
    expect(screen.getByRole('spinbutton', {name: /Numeric break/})).toBeTruthy();
    fireEvent.click(screen.getByRole('button', {name: 'Add legend category'}));
    expect(screen.getByText('XYZ effective range: ≤ 10')).toBeTruthy();
    expect(screen.getByText('XYZ effective range: > 10')).toBeTruthy();

    fireEvent.change(mode, {target: {value: 'Data-driven distributed'}});
    managed = JSON.parse(
      container.querySelector('[aria-label="Advanced layer JSON"]').value,
    );
    expect(managed.style.theme).toMatchObject({
      type: 'distributed',
      field: 'id',
      categories: [],
    });
    expect(confirm).toHaveBeenCalledTimes(3);
  });

  test('previews the effective highlighted style with inherited opacity', async () => {
    const styledWorkspace = {
      ...workspace,
      locale: {
        ...workspace.locale,
        layers: {
          Orders: {
            name: 'Smoke Control Orders',
            format: 'mvt',
            style: {
              default: {
                fillColor: '#13fbaa',
                fillOpacity: 0.3,
                strokeColor: '#0b1913',
                strokeWidth: 2,
              },
              highlight: {
                fillColor: '#4513fb',
                strokeColor: '#0e0d0b',
                strokeWidth: 3,
              },
            },
          },
        },
      },
    };
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/workspace') {
        return response({workspace: styledWorkspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') return response({tables: [], databases: ['MAPP']});
      if (path === '/api/icons') return response({icons: []});
      throw new Error(`Unexpected request: GET ${path}`);
    }));
    render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.click(await screen.findByRole('button', {name: 'Smoke Control Orders'}));
    const defaultPreview = screen.getAllByText('Default')
      .find(element => element.closest('.symbol-state'))
      .closest('.symbol-state');
    const highlightPreview = screen.getByText('Highlighted').closest('.symbol-state');
    expect(defaultPreview.querySelector('path').getAttribute('fill')).toBe('#13fbaa');
    expect(highlightPreview.querySelector('path').getAttribute('fill')).toBe('#4513fb');
    expect(highlightPreview.querySelector('path').getAttribute('fill-opacity')).toBe('0.3');
  });

  test('provides a visible logout action', async () => {
    const logout = vi.fn();
    vi.stubGlobal('fetch', standardFetch(() => successfulSave(workspace)));
    render(
      <Dashboard
        onLogout={logout}
        openSecurity={() => {}}
        openDerivedLayers={() => {}}
      />,
    );

    await screen.findByDisplayValue('demo');
    fireEvent.click(screen.getByRole('button', {name: 'Logout'}));

    expect(logout).toHaveBeenCalledOnce();
  });

  test('serializes activity, freezes edits, and persistently announces readiness', async () => {
    let resolveSave;
    const deferredSave = new Promise(resolve => {
      resolveSave = resolve;
    });
    vi.stubGlobal('fetch', standardFetch(() => deferredSave));
    const {container} = render(<Dashboard openSecurity={() => {}}/>);

    const keyInput = await screen.findByDisplayValue('demo');
    fireEvent.change(keyInput, {target: {value: 'changed'}});
    fireEvent.click(screen.getByRole('button', {name: 'Save & reload XYZ'}));

    await screen.findByText('Saving workspace and restarting XYZ…');
    const main = container.querySelector('main');
    const liveRegion = container.querySelector('.status-region');
    expect(main.hasAttribute('inert')).toBe(true);
    expect(main.getAttribute('aria-busy')).toBe('true');
    expect(liveRegion.getAttribute('aria-live')).toBe('polite');
    expect(liveRegion.getAttribute('aria-atomic')).toBe('true');
    expect(screen.getByRole('button', {name: 'Validate'}).disabled).toBe(true);
    expect(screen.getByRole('button', {name: 'Reload editor'}).disabled).toBe(true);
    expect(screen.getByRole('button', {name: 'Access & audit'}).disabled).toBe(true);
    expect(container.querySelector('header select').disabled).toBe(true);

    fireEvent.change(keyInput, {target: {value: 'late edit'}});
    resolveSave(successfulSave({...workspace, key: 'changed'}));

    await screen.findByText(
      'Workspace saved. XYZ restarted and is ready for connections with this workspace.',
    );
    expect(screen.getByDisplayValue('changed')).toBeTruthy();
    expect(main.hasAttribute('inert')).toBe(false);
    expect(main.getAttribute('aria-busy')).toBe('false');
    expect(
      screen.getByText(
        'Workspace saved. XYZ restarted and is ready for connections with this workspace.',
      ),
    ).toBeTruthy();
  });

  test('keeps dirty state and requires reconciliation after an ambiguous failure', async () => {
    vi.stubGlobal(
      'fetch',
      standardFetch(async () => {
        throw new TypeError('Network connection lost.');
      }),
    );
    render(<Dashboard openSecurity={() => {}}/>);

    fireEvent.change(await screen.findByDisplayValue('demo'), {
      target: {value: 'changed'},
    });
    fireEvent.click(screen.getByRole('button', {name: 'Save & reload XYZ'}));

    await screen.findByText(
      'Save outcome could not be confirmed. Reload the workspace before retrying.',
    );
    await waitFor(() => {
      expect(screen.getByRole('button', {name: 'Save & reload XYZ'}).disabled)
        .toBe(false);
    });
    expect(screen.getByText('Unsaved changes')).toBeTruthy();
    expect(screen.getByDisplayValue('changed')).toBeTruthy();
  });

  test('shows an initial-load failure and can retry it', async () => {
    let workspaceAttempts = 0;
    const fetchMock = vi.fn(async path => {
      if (path === '/api/workspace') {
        workspaceAttempts += 1;
        if (workspaceAttempts === 1) {
          throw new TypeError('Initial connection failed.');
        }
        return response({workspace, revision: 'rev-1'});
      }
      if (path === '/api/catalog') {
        return response({tables: [], databases: ['MAPP']});
      }
      if (path === '/api/icons') {
        return response({icons: []});
      }
      throw new Error(`Unexpected request: GET ${path}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<Dashboard openSecurity={() => {}}/>);

    await screen.findByText('Unable to load configuration.');
    fireEvent.click(screen.getByRole('button', {name: 'Retry'}));

    expect(await screen.findByDisplayValue('demo')).toBeTruthy();
    expect(workspaceAttempts).toBe(2);
  });
});

describe('Dashboard authentication lifecycle', () => {
  test('returns to sign-in when an authenticated API request receives 401', async () => {
    sessionStorage.setItem('mapp-csrf', 'expired-csrf');
    vi.stubGlobal('fetch', vi.fn(async path => {
      if (path === '/api/auth/me') {
        return response({actor: 'admin', scopes: ['admin']});
      }
      if (path === '/api/workspace') {
        return response({error: 'Authentication required.'}, 401);
      }
      if (path === '/api/catalog') {
        return response({tables: [], databases: ['MAPP']});
      }
      if (path === '/api/icons') {
        return response({icons: []});
      }
      throw new Error(`Unexpected request: GET ${path}`);
    }));

    render(<Root/>);

    expect(await screen.findByRole('button', {name: 'Sign in'})).toBeTruthy();
    expect(sessionStorage.getItem('mapp-csrf')).toBeNull();
  });
});
