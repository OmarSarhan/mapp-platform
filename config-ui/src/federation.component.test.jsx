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
  FederatedSources,
  actionOutcome,
  evidenceState,
  expectedStatus,
  semanticCoverage,
} from './federation.jsx';

const active = {
  alias: 'leeds_ext',
  displayName: 'Leeds external',
  connectionRef: 'LEEDS_EXT',
  tlsPolicy: 'require',
  allowedRelations: ['leeds.smoke_control_orders'],
  status: 'active',
  provisionedAt: '2026-08-11T14:30:00+00:00',
  approvedBy: 'admin',
  approvedAt: '2026-08-11T14:30:00+00:00',
  acceptedEvidenceComplete: true,
  lastObservationId: 88,
  lastObservation: {connectivity: 'reachable', schema: 'current'},
  dataHandlingClassification: 'Public open data, OGL v3.',
};

const registered = {
  alias: 'pending_src',
  connectionRef: 'PENDING',
  tlsPolicy: 'verify-full',
  allowedRelations: [],
  status: 'pending',
  provisionedAt: null,
  lastObservationId: null,
  lastObservation: {},
};

afterEach(cleanup);

describe('federation evidence helpers', () => {
  test('evidence state distinguishes provisioned, incomplete, and archived', () => {
    expect(evidenceState(active)).toBe('verified');
    expect(evidenceState(registered)).toBe('registered');
    expect(evidenceState({...active, acceptedEvidenceComplete: false}))
      .toBe('incomplete');
    expect(evidenceState({...active, status: 'retired'})).toBe('archived');
  });

  test('each action declares the state it must leave behind', () => {
    expect(expectedStatus('provision')).toBe('active');
    expect(expectedStatus('retire')).toBe('retired');
  });

  test('a response that does not say what committed is indeterminate', () => {
    expect(actionOutcome('provision', undefined, 'leeds_ext'))
      .toBe('indeterminate');
    expect(actionOutcome('provision', {alias: 'other', status: 'active'}, 'leeds_ext'))
      .toBe('indeterminate');
    expect(actionOutcome('retire', {alias: 'leeds_ext', status: 'active'}, 'leeds_ext'))
      .toBe('indeterminate');
    expect(actionOutcome('retire', {alias: 'leeds_ext', status: 'retired'}, 'leeds_ext'))
      .toBe('committed');
    expect(actionOutcome('observe', {alias: 'leeds_ext', status: 'active'}, 'leeds_ext'))
      .toBe('observed');
  });
});

describe('semanticCoverage', () => {
  const alias = {alias: 'census', allowedRelations: ['leeds.a', 'leeds.b']};

  test('counts exposed relations that have a ready profile', () => {
    const assets = [
      {status: 'ready', generated: {qualifiedName: 'source_census.a'}},
      {status: 'ready', generated: {qualifiedName: 'source_census.b'}},
    ];
    expect(semanticCoverage(alias, assets)).toMatchObject({
      total: 2, profiled: 2, missing: [],
    });
  });

  test('a relation with no ready profile is reported with the selector to add', () => {
    const assets = [
      {status: 'ready', generated: {qualifiedName: 'source_census.a'}},
      // Present but not ready: not yet usable as a derived-layer source.
      {status: 'registering', generated: {qualifiedName: 'source_census.b'}},
    ];
    expect(semanticCoverage(alias, assets)).toMatchObject({
      total: 2, profiled: 1, missing: ['source_census.b'],
      selector: 'MAPP:source_census.*',
    });
  });

  test('is absent when the catalog could not be read', () => {
    expect(semanticCoverage(alias, null)).toBeNull();
  });
});

describe('FederatedSources', () => {
  const listOnly = aliases => vi.fn(async path => {
    if (path === '/api/federation/aliases') return {aliases};
    throw new Error(`unexpected path ${path}`);
  });

  test('lists sources with their status and shows the selected evidence', async () => {
    const api = listOnly([active, registered]);
    render(<FederatedSources api={api} close={() => {}}/>);

    await waitFor(() => expect(screen.getByText('Leeds external')).toBeTruthy());
    expect(screen.getByText('active')).toBeTruthy();
    expect(screen.getByText('pending')).toBeTruthy();
    expect(screen.getByText('LEEDS_EXT · TLS require')).toBeTruthy();
    expect(screen.getByText(/reachable · schema current/)).toBeTruthy();
  });

  test('group labels are shown, and a server without them does not crash', async () => {
    // The groups column arrives only from a server that has run the migration.
    // Rendering alias.groups without a guard turns a missing key into
    // "Cannot read properties of undefined", which takes down the whole panel
    // -- including the aliases an operator needs to see to fix it.
    const labelled = {...active, groups: ['leeds', 'showcase']};
    render(<FederatedSources api={listOnly([labelled, registered])} close={() => {}}/>);

    await waitFor(() => expect(screen.getByText('Leeds external')).toBeTruthy());
    expect(screen.getByText('leeds, showcase')).toBeTruthy();

    cleanup();
    // `registered` carries no groups key at all, standing in for a server
    // predating the migration.
    render(<FederatedSources api={listOnly([registered])} close={() => {}}/>);
    await waitFor(() => expect(
      screen.getByRole('heading', {name: 'pending_src'}),
    ).toBeTruthy());
    // Relations and Groups are both empty here, so both read "none". The
    // assertion that matters is that the panel rendered at all.
    expect(screen.getAllByText('none')).toHaveLength(2);
  });

  test('provision is unavailable until the source has been observed', async () => {
    const api = listOnly([registered]);
    render(<FederatedSources api={api} close={() => {}}/>);

    // The alias appears in both the list button and the detail heading, so
    // query the heading specifically.
    await waitFor(() => expect(
      screen.getByRole('heading', {name: 'pending_src'}),
    ).toBeTruthy());
    const provision = screen.getByRole('button', {name: 'Provision'});
    expect(provision.disabled).toBe(true);
  });

  test('provision confirms first and sends only the acknowledgements ticked', async () => {
    const calls = [];
    const api = vi.fn(async (path, options) => {
      calls.push({path, body: options ? JSON.parse(options.body) : null});
      if (path === '/api/federation/aliases') return {aliases: [active]};
      return {alias: {...active}};
    });
    render(<FederatedSources api={api} close={() => {}}/>);
    await waitFor(() => expect(screen.getByText('Leeds external')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', {name: 'Provision'}));
    fireEvent.click(screen.getByRole('checkbox', {name: /Different physical database/}));
    fireEvent.click(screen.getByRole('button', {name: 'Provision and expose'}));

    await waitFor(() => expect(
      calls.some(call => call.path.endsWith('/provision')),
    ).toBe(true));
    const provision = calls.find(call => call.path.endsWith('/provision'));
    expect(provision.body).toEqual({
      expectedObservationId: 88,
      physicalRebindAcknowledged: true,
    });
  });

  test('a success that does not establish the new state is reported, not claimed', async () => {
    const api = vi.fn(async path => {
      if (path === '/api/federation/aliases') return {aliases: [active]};
      return {alias: {}};
    });
    render(<FederatedSources api={api} close={() => {}}/>);
    await waitFor(() => expect(screen.getByText('Leeds external')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', {name: 'Retire'}));
    fireEvent.click(screen.getByRole('button', {name: 'Retire and archive'}));

    await waitFor(() => expect(
      screen.getByText(/did not establish what committed/),
    ).toBeTruthy());
  });

  test('a refusal is surfaced verbatim rather than treated as success', async () => {
    const api = vi.fn(async path => {
      if (path === '/api/federation/aliases') return {aliases: [active]};
      throw new Error('Derived layers are mutating.');
    });
    render(<FederatedSources api={api} close={() => {}}/>);
    await waitFor(() => expect(screen.getByText('Leeds external')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', {name: 'Retire'}));
    fireEvent.click(screen.getByRole('button', {name: 'Retire and archive'}));

    await waitFor(() => expect(
      screen.getByText('Derived layers are mutating.'),
    ).toBeTruthy());
  });

  test('an empty list says no ACTIVE sources and names the credential source', async () => {
    const api = listOnly([]);
    render(<FederatedSources api={api} close={() => {}}/>);

    await waitFor(() => expect(
      screen.getByText(/No active sources/),
    ).toBeTruthy());
    expect(screen.getByText(/FEDERATION_DBS_<REF>/)).toBeTruthy();
  });

  test('a failed list is not reported as an empty registry', async () => {
    // A registry that could not be read is unknown, not empty. Saying "no
    // sources" here would send an operator to register one while aliases
    // they cannot see already hold the names.
    const api = vi.fn(async () => {
      throw new Error('The federation alias registry is unavailable.');
    });
    render(<FederatedSources api={api} close={() => {}}/>);

    await waitFor(() => expect(
      screen.getByText('The federation alias registry is unavailable.'),
    ).toBeTruthy());
    expect(screen.queryByText(/No active sources/)).toBeNull();
  });

  test('a rebound source shows the physical mismatch it asks to approve', async () => {
    const rebound = {
      ...active,
      acceptedPhysicalIdentityCurrent: false,
      lastObservation: {
        connectivity: 'reachable',
        schema: 'current',
        schemaFingerprint: '03fbe6948db8ff79241db6c4f4f6747389fa31070ceb8861',
        acceptedSchemaCurrent: true,
      },
    };
    render(<FederatedSources api={listOnly([rebound])} close={() => {}}/>);

    await waitFor(() => expect(screen.getByText('Leeds external')).toBeTruthy());
    // The schema is unchanged, so this mismatch is the ONLY signal that the
    // source is a different database.
    expect(screen.getByText(/Differs from the approved database/)).toBeTruthy();
  });

  test('an unaccepted baseline is not reported as a match', async () => {
    const fresh = {
      ...registered,
      lastObservationId: 7,
      lastObservation: {
        connectivity: 'reachable',
        schema: 'current',
        schemaFingerprint: 'aa11bb22cc33dd44ee55ff6677889900aabbccddeeff0011',
        // The store sets this true when no fingerprint has been accepted.
        acceptedSchemaCurrent: true,
      },
    };
    render(<FederatedSources api={listOnly([fresh])} close={() => {}}/>);

    await waitFor(() => expect(
      screen.getByRole('heading', {name: 'pending_src'}),
    ).toBeTruthy());
    expect(screen.getByText(/No fingerprint has been accepted yet/)).toBeTruthy();
    expect(screen.queryByText(/Matches the accepted fingerprint/)).toBeNull();
  });

  test('observe is disclosed as changing consumer access, not just probing', async () => {
    // _persist_observation() re-applies consumer access from the evidence it
    // gathers, so a drifted observation revokes the reader. Presenting Observe
    // as read-only would understate an unconfirmed action.
    const api = listOnly([active]);
    render(<FederatedSources api={api} close={() => {}}/>);

    await waitFor(() => expect(screen.getByText('Leeds external')).toBeTruthy());
    expect(screen.getByText(/Observe is not read-only/)).toBeTruthy();
    expect(screen.getByText(/revokes the map reader/)).toBeTruthy();
  });

  test('retirement discloses the dropped mappings and that it is terminal', async () => {
    const api = listOnly([active]);
    render(<FederatedSources api={api} close={() => {}}/>);
    await waitFor(() => expect(screen.getByText('Leeds external')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', {name: 'Retire'}));

    expect(screen.getByText(/user mappings are dropped/i)).toBeTruthy();
    expect(screen.getByText(/terminal/i)).toBeTruthy();
    expect(screen.queryByText(/Nothing is dropped/)).toBeNull();
  });

  test('the evidence an acknowledgement approves is shown before it is offered', async () => {
    const drifted = {
      ...active,
      lastObservation: {
        connectivity: 'reachable',
        schema: 'changed',
        schemaFingerprint: '03fbe6948db8ff79241db6c4f4f6747389fa31070ceb8861',
        acceptedSchemaCurrent: false,
        rowLevelSecurityDetected: true,
      },
    };
    const api = listOnly([drifted]);
    render(<FederatedSources api={api} close={() => {}}/>);

    await waitFor(() => expect(screen.getByText('Leeds external')).toBeTruthy());
    expect(screen.getByText(/Differs from the accepted fingerprint/)).toBeTruthy();
    expect(screen.getByText(/Detected on the source/)).toBeTruthy();
  });
});
