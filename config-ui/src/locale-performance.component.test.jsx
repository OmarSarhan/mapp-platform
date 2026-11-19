import React from 'react';
import {act, cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, expect, test, vi} from 'vitest';
import * as helpers from './api.js';
import {Dashboard} from './main.jsx';

afterEach(() => {cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals()});

test('unrelated renders reuse locales and field edits preserve the original workspace and raw overrides', async () => {
  const workspace = {key:'fixture',dbs:'MAPP',locale:{name:'Default',layers:{
    roads:{name:'Roads',format:'geojson',infoj:[{field:'name',type:'text'}],style:{default:{}}},
    parks:{name:'Parks',format:'geojson',infoj:[]},
  }},locales:{named:{name:'Named',layers:{roads:{name:'Named roads',infoj:[{field:'translated',type:'text'}]}}}}};
  const original = structuredClone(workspace);
  let submitted;
  const fetch = vi.fn(async (path, options={}) => {
    let data;
    if(path==='/api/workspace') data={workspace,revision:'r1'};
    else if(path==='/api/catalog') data={tables:[],databases:['MAPP']};
    else if(path==='/api/icons') data={icons:[]};
    else if(path==='/api/plugins') data={plugins:{external:[]}};
    else if(path==='/api/validate') {submitted=JSON.parse(options.body).workspace;data={message:'Valid fixture'};}
    else data={layers:{}};
    return {ok:true,status:200,json:async()=>data};
  });
  vi.stubGlobal('fetch', fetch);
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  const derive = vi.spyOn(helpers, 'renderedLocales');
  const view = render(<Dashboard/>);
  await screen.findByRole('button', {name:'Roads'});
  await act(async()=>{});
  derive.mockClear();
  fireEvent.change(screen.getByRole('searchbox', {name:'Filter configured layers'}), {target:{value:'Road'}});
  fireEvent.click(screen.getByRole('button', {name:'Roads'}));
  expect(derive).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Layer folder'), {target:{value:'Edited folder'}});
  expect(derive).toHaveBeenCalledTimes(1);
  derive.mockClear();
  await act(async()=>window.dispatchEvent(new Event('mapp-discovery-changed')));
  expect(derive).not.toHaveBeenCalled();
  view.rerender(<Dashboard activeArea="semantic"/>);
  view.rerender(<Dashboard activeArea="workspace"/>);
  await act(async()=>{});
  expect(screen.getByLabelText('Layer folder').value).toBe('Edited folder');
  expect(derive).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name:'Validate'}));
  await waitFor(()=>expect(submitted).toBeDefined());
  expect(workspace).toEqual(original);
  expect(submitted.locales).toEqual(original.locales);
  expect(submitted.locale.layers.parks).toEqual(original.locale.layers.parks);
  expect(submitted.locale.layers.roads).not.toEqual(original.locale.layers.roads);
  derive.mockClear();
  fireEvent.change(screen.getByLabelText('Locale'), {target:{value:'named'}});
  expect(derive).not.toHaveBeenCalled();
  expect(screen.getByText('Effective named locale — read-only in the dashboard.')).toBeTruthy();
});
