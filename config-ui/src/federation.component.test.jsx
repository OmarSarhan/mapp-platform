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

  test('an empty registry explains that the credential comes from the environment', async () => {
    const api = listOnly([]);
    render(<FederatedSources api={api} close={() => {}}/>);

    await waitFor(() => expect(
      screen.getByText(/No sources are registered/),
    ).toBeTruthy());
    expect(screen.getByText(/FEDERATION_DBS_<REF>/)).toBeTruthy();
  });
});
