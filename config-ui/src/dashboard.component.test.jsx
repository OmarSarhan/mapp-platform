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
  derivedLayerFormDefinition,
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
  test('refreshes the derived-layer form after converting its kind', async () => {
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
    })).toEqual({
      name: 'hex_summary',
      kind: 'materialized',
      sources: 'leeds.bus_stops',
      idColumn: 'id',
      geometryColumn: 'geom',
      description: '',
      query: 'SELECT id, geom FROM leeds.bus_stops',
    });
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
    render(<Dashboard openSecurity={() => {}}/>);

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
    expect(screen.getByText(/Order:/).textContent)
      .not.toMatch(/(?:^|→ )hover(?: →|$)/);
    const filterSelect = screen.getByRole('combobox', {
      name: /Interactive filter/,
    });
    fireEvent.change(filterSelect, {target: {value: 'Automatic'}});
    expect(filterSelect.value).toBe('Automatic');
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
