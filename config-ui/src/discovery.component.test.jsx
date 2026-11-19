import React from 'react';
import {act, cleanup, fireEvent, render, renderHook, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, test, vi} from 'vitest';
import {Security, useDiscovery} from './main.jsx';
import {SemanticCatalog} from './semantic.jsx';

const response = data => ({ok: true, status: 200, json: async () => data});
afterEach(() => {cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks()});

test('semantic catalog is usable while source discovery is pending and proposals fail', async () => {
  const api = vi.fn(path => {
    if(path.includes('/source/relations')) return new Promise(() => {});
    if(path.includes('/proposals')) return Promise.reject(new Error('offline'));
    if(path.includes('/catalog')) return Promise.resolve({assets:[{id:'source:test',status:'ready',version:1,generated:{qualifiedName:'public.independent',fields:[]},curated:{displayName:'Independent layer'}}]});
    return Promise.resolve({derivedProfiles:[]});
  });
  render(<SemanticCatalog api={api} identity={{actor:'admin'}} close={() => {}}/>);
  expect((await screen.findAllByText('Independent layer')).length).toBeGreaterThan(0);
  expect(await screen.findByText(/proposals: Refresh failed/)).toBeTruthy();
  expect(screen.getByText('source/relations: Loading…')).toBeTruthy();
});

test('late initial semantic catalog cannot overwrite a source synchronization', async () => {
  vi.spyOn(window,'confirm').mockReturnValue(true);
  let initialCatalog;
  let synchronized=false;
  const source={alias:'MAPP',schema:'data',relation:'roads',kind:'table',assetId:'source:roads'};
  const asset={id:source.assetId,status:'ready',version:1,generated:{qualifiedName:'data.roads',fields:[]},curated:{displayName:'Synchronized roads'}};
  const api=vi.fn(path=>{
    if(path==='/api/semantic/source/sync'){synchronized=true;return Promise.resolve({asset,operation:'register'})}
    if(path.includes('/catalog'))return synchronized?Promise.resolve({assets:[asset]}):new Promise(resolve=>{initialCatalog=resolve});
    if(path.includes('/source/relations'))return Promise.resolve({relations:[source]});
    return Promise.resolve({derivedProfiles:[],proposals:[]});
  });
  render(<SemanticCatalog api={api} identity={{actor:'admin'}} close={()=>{}}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Advanced mode'}));
  const sync=await screen.findByRole('button',{name:'Register or sync source metadata'});
  await waitFor(()=>expect(sync.disabled).toBe(false));
  fireEvent.click(sync);
  await screen.findAllByText('Synchronized roads');
  await act(async()=>initialCatalog({assets:[]}));
  expect(screen.getAllByText('Synchronized roads').length).toBeGreaterThan(0);
});

test('discovery pauses automatic requests, coalesces in flight, retains stale data and permits explicit retry', async () => {
  vi.useFakeTimers();
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  let resolve;
  const fetch = vi.fn(() => new Promise(done => {resolve = done}));
  vi.stubGlobal('fetch', fetch);
  const {result, rerender} = renderHook(({enabled}) => useDiscovery('/api/catalog', 'tables', enabled), {initialProps: {enabled: false}});
  await act(async () => vi.advanceTimersByTime(60000));
  expect(fetch).toHaveBeenCalledTimes(0);
  rerender({enabled: true});
  await act(async () => vi.advanceTimersByTime(60000));
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => resolve(response({tables: [{name: 'retained'}]})));
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
  await act(async () => vi.advanceTimersByTime(60000));
  expect(fetch).toHaveBeenCalledTimes(1);
  fetch.mockRejectedValueOnce(new Error('offline'));
  await act(async () => result.current.refresh());
  expect(result.current.error).toBeTruthy();
  expect(result.current.data.tables[0].name).toBe('retained');
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(result.current.loading).toBe(true);
  expect(result.current.data.tables[0].name).toBe('retained');
  await act(async () => resolve(response({tables:[{name:'fresh'}]})));
  expect(result.current.loading).toBe(false);
  expect(result.current.error).toBe('');
  expect(result.current.data.tables[0].name).toBe('fresh');
  expect(result.current.lastSuccess).toBeInstanceOf(Date);
});

test('lifecycle invalidation during discovery requests one fresh snapshot after completion', async () => {
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  let resolve;
  const fetch = vi.fn(() => new Promise(done => {resolve = done}));
  vi.stubGlobal('fetch', fetch);
  renderHook(() => useDiscovery('/api/catalog', 'tables'));
  act(() => {window.dispatchEvent(new Event('mapp-discovery-changed')); window.dispatchEvent(new Event('mapp-discovery-changed'))});
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => resolve(response({tables: []})));
  expect(fetch).toHaveBeenCalledTimes(2);
});

test('unmount discards queued invalidation instead of starting an orphan request', async () => {
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  let resolve;
  const fetch = vi.fn(() => new Promise(done => {resolve = done}));
  vi.stubGlobal('fetch', fetch);
  const {unmount} = renderHook(() => useDiscovery('/api/catalog', 'tables'));
  act(() => window.dispatchEvent(new Event('mapp-discovery-changed')));
  unmount();
  await act(async () => resolve(response({tables: []})));
  expect(fetch).toHaveBeenCalledTimes(1);
});

test.each([
  ['/api/catalog', 'tables', {tables: []}],
  ['/api/icons', 'icons', {icons: []}],
  ['/api/plugins', 'plugins', {plugins: {plugins: []}}],
])('%s preserves single flight across area changes and refreshes on return', async (path, collection, data) => {
  vi.useFakeTimers();
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  let resolve;
  const fetch = vi.fn(() => new Promise(done => {resolve = done}));
  vi.stubGlobal('fetch', fetch);
  const {result, rerender} = renderHook(({enabled}) => useDiscovery(path, collection, enabled), {initialProps: {enabled: true}});
  rerender({enabled: false});
  act(() => window.dispatchEvent(new Event('mapp-discovery-changed')));
  rerender({enabled: true});
  expect(fetch).toHaveBeenCalledTimes(1);
  rerender({enabled: false});
  await act(async () => resolve(response(data)));
  await act(async () => vi.advanceTimersByTime(60000));
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(result.current.data).toEqual(data);
  rerender({enabled: true});
  expect(fetch).toHaveBeenCalledTimes(2);
  await act(async () => resolve(response(data)));
  expect(fetch).toHaveBeenCalledTimes(2);
});

test('pending audit does not hold back tokens and failures identify the resource', async () => {
  let revoked = false;
  const fetch = vi.fn(path => {
    if(path.endsWith('/revoke')) {revoked = true; return Promise.resolve(response({}));}
    if(path.endsWith('/audit')) return new Promise(() => {});
    if(path.endsWith('/mcp-grants')) return Promise.reject(new Error('offline'));
    return Promise.resolve(response({tokens: [{id:'t',name:'Independent token',scopes:[],revoked}], authorizations:[], clients:[], approvals:[], windows:[]}));
  });
  vi.stubGlobal('fetch', fetch);
  render(<Security close={() => {}}/>);
  fireEvent.click(screen.getByRole('button', {name:'CLI access'}));
  expect(await screen.findByText('Independent token')).toBeTruthy();
  expect(await screen.findByText(/mcp-grants: Refresh failed/)).toBeTruthy();
  expect(screen.getByText('audit: Loading…')).toBeTruthy();
  fetch.mockClear();
  fireEvent.click(screen.getByRole('button', {name:'Revoke', exact:true}));
  await waitFor(() => expect(screen.queryByRole('button', {name:'Revoke', exact:true})).toBeNull());
  expect(fetch.mock.calls.map(([path]) => path).sort()).toEqual(['/api/admin/audit','/api/admin/tokens','/api/admin/tokens/t/revoke']);
});

test('an older audit response cannot overwrite the refresh after a mutation', async () => {
  const audits=[];
  vi.stubGlobal('fetch', vi.fn(path => {
    if(path.endsWith('/audit')) return new Promise(resolve=>audits.push(resolve));
    return Promise.resolve(response({tokens:[{id:'t',name:'Token',scopes:[]}],authorizations:[],clients:[],grants:[],approvals:[],windows:[]}));
  }));
  render(<Security close={()=>{}}/>);
  fireEvent.click(screen.getByRole('button',{name:'CLI access'}));
  fireEvent.click(await screen.findByRole('button',{name:'Revoke',exact:true}));
  await waitFor(()=>expect(audits).toHaveLength(2));
  await act(async()=>audits[1](response({events:[{event:'new audit',time:'now',actor:'admin'}]})));
  await act(async()=>audits[0](response({events:[{event:'old audit',time:'before',actor:'admin'}]})));
  fireEvent.click(screen.getByRole('button',{name:'Audit trail'}));
  expect(screen.getByText('now new audit admin')).toBeTruthy();
  expect(screen.queryByText('before old audit admin')).toBeNull();
});
