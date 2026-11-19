import React from 'react';
import {afterEach, expect, test, vi} from 'vitest';
import {act, cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {Dashboard, DerivedLayers, Root} from './main.jsx';

const savedLayer = {
  name:'Roads', format:'mvt', table:'derived_layers.roads', geom:'geom', qID:'id',
  infoj:[{field:'old',type:'text'},{field:'old',fieldfx:'upper(label)',type:'text'}],
  style:{theme:{type:'categorized',field:'score',categories:[]}},
};
const workspace = {key:'map',dbs:'MAPP',locale:{layers:{Roads:savedLayer}},
  locales:{cy:{layers:{Roads:{name:'Ffyrdd'}}}}};
const binding = (name, generation, status='ready') => ({assetId:`asset:${name}`, generation, status});
const result = (name, generation, changes={added:[],removed:[],changed:[]}) => ({
  name, kind:'view', sources:['transit.roads'], idColumn:'id', geometryColumn:'geom',
  query:'SELECT id, geom FROM transit.roads', semanticProfile:binding(name,generation,'registering'),
  columnChanges:changes, consumerLabels:['Roads (default map)'],
});
const response = payload => ({ok:true, status:200, json:async()=>payload});
const asset = (name, generation) => ({id:`asset:${name}`,generation,status:'ready',generated:{
  binding:{adapter:'postgresql',schema:'derived_layers',relation:name},
  fields:[{name:'geom',geometryType:'LineString',type:'geometry(LineString,3857)'},{name:'id',type:'bigint'},
    {name:'score',type:'numeric'},{name:'total',type:'integer'}],
}});
function mockApi(handle, data=workspace) {
  const fetch = vi.fn(async(path,options={})=>{
    const custom = handle(path,options);
    if(custom !== undefined)return response(await custom);
    switch(path){
      case '/api/auth/me': return response({actor:'admin',scopes:['admin']});
      case '/api/workspace': if(!options.method)return response({workspace:data,revision:'rev-1'}); break;
      case '/api/catalog': return response({databases:['MAPP'],tables:[{dbs:'MAPP',schema:'derived_layers',table:'roads',
        columns:[{name:'old',type:'text'},{name:'total',type:'boolean'}]}]});
      case '/api/icons': return response({icons:[]});
      case '/api/plugins': return response({plugins:{external:[]}});
      case '/api/derived-layers/drafts': return response({drafts:[]});
      case '/api/derived-layers/capabilities': return response({});
      case '/api/layers?locale=locale': return response({layers:{}});
    }
    throw new Error('Unexpected request: '+path);
  });
  vi.stubGlobal('fetch',fetch);
  return fetch;
}
function emit(replacement){
  act(()=>window.dispatchEvent(new CustomEvent('mapp-derived-layer-changed',{detail:replacement})));
}
const review = name => screen.getByRole('region',{name:`Replacement of ${name}`});
const accept = panel => within(panel).queryByRole('button',{name:'Use suggested unsaved edits'});
afterEach(()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals()});

test.each(['drafts', 'capabilities'])('opens the definition of a relation named %s', async name=>{
  const definition=result(name,1);
  mockApi(path=>{
    if(path==='/api/derived-layers')return {derivedLayers:[{name,kind:'view',sources:definition.sources}]};
    if(path===`/api/derived-layers/${name}?definition=true`)return {derivedLayer:definition};
  });
  render(<DerivedLayers close={()=>{}}/>);
  fireEvent.change(await screen.findByLabelText(`Edit or delete derived_layers.${name}`),{target:{value:'edit'}});
  expect(await screen.findByDisplayValue(definition.query)).toBeTruthy();
});

test('reviews ordinary replacement before submitting, waits for its exact generation and offers only local edits', async()=>{
  const prefix='SELECT id, geom, /* '+ 'Shared calculation. '.repeat(10) +'*/ ';
  const previous=prefix+'passengers FROM transit.roads';
  const proposed=prefix+'vehicles FROM transit.roads';
  const committed={...result('roads',2,{added:['total'],removed:['old'],changed:['score']}),query:proposed};
  let profile=binding('roads',2,'registering'), posted, saved;
  let finish;
  const completion=new Promise(resolve=>{finish=resolve});
  const data={...workspace,locale:{layers:{Roads:savedLayer,Other:{...savedLayer,name:'Other'}}}};
  const fetch=mockApi((path,options)=>{
    if(path==='/api/derived-layers')return {derivedLayers:[{name:'roads',kind:'view',sources:['transit.roads']}]};
    if(path==='/api/derived-layers/roads')return {derivedLayer:{...committed,query:previous}};
    if(path==='/api/dependencies')return {dependencies:[
      {relation:'derived_layers.roads',workspaceLayers:['cy:Zoom','locale:Roads'],derivedLayers:[]},
      {relation:'derived_layers.roads',workspaceLayers:[],derivedLayers:['totals']},
    ]};
    if(path==='/api/derived-layers/roads/replace'){
      posted=JSON.parse(options.body);return {operation:{id:'replace-1',status:'running'},statusUrl:'/api/operations/replace-1'};
    }
    if(path==='/api/operations/replace-1')return completion;
    if(path==='/api/semantic/derived-profiles/roads')return {derivedProfile:profile};
    if(path==='/api/semantic/catalog/objects/asset%3Aroads')return {asset:asset('roads',2)};
    if(path==='/api/workspace' && options.method==='POST'){
      saved=JSON.parse(options.body);
      const fingerprint='a'.repeat(64);
      return {workspace:saved.workspace,saved:true,revision:'rev-2',fingerprint,
        reload:{expectedWorkspaceFingerprint:fingerprint,requestedGeneration:1,status:{
          workspaceFingerprint:fingerprint,requestedGeneration:1,appliedGeneration:1,completed:true,healthy:true}}};
    }
  },data);
  render(<Root/>);
  fireEvent.click(await screen.findByRole('button',{name:'Derived layers'}));
  fireEvent.change(await screen.findByLabelText('Edit or delete derived_layers.roads'),{target:{value:'edit'}});
  const sql=await screen.findByDisplayValue(previous);
  fireEvent.change(sql,{target:{value:proposed}});
  fireEvent.click(screen.getByRole('button',{name:'Save derived layer'}));
  const packet=await screen.findByRole('region',{name:'Review derived replacement'});
  expect(packet.textContent).toContain(previous);
  expect(packet.textContent).toContain(proposed);
  for(const name of ['cy:Zoom','locale:Roads','totals'])expect(within(packet).getByText(name)).toBeTruthy();
  expect(posted).toBeUndefined();
  fireEvent.click(within(packet).getByRole('button',{name:'Back to definition'}));
  expect(posted).toBeUndefined();
  fireEvent.click(screen.getByRole('button',{name:'Save derived layer'}));
  fireEvent.click(await screen.findByRole('button',{name:'Confirm in-place replacement'}));
  expect(posted).toMatchObject({confirmed:true,background:true,query:proposed});
  fireEvent.click(screen.getByRole('button',{name:'Workspace',exact:true}));
  expect(screen.getByRole('button',{name:'Save & reload XYZ'}).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'Other'}));
  const editor=screen.getByLabelText('Advanced layer JSON');
  const localLayer={...JSON.parse(editor.value),infoj:[{field:'old',title:'Changed while the operation ran'}]};
  fireEvent.change(editor,{target:{value:JSON.stringify(localLayer)}});
  await act(async()=>finish({operation:{id:'replace-1',status:'succeeded',result:{derivedLayer:committed}}}));
  await screen.findByText(/Waiting for ready metadata/,{},{timeout:5000});
  expect(fetch.mock.calls.some(([path])=>path==='/api/operations/replace-1')).toBe(true);
  expect(accept(review('roads'))).toBeNull();
  expect(fetch.mock.calls.some(([path])=>path.includes('/objects/'))).toBe(false);
  fireEvent.click(screen.getByRole('button',{name:'Workspace',exact:true}));
  fireEvent.change(screen.getByDisplayValue('map'),{target:{value:'local map title'}});
  profile=binding('roads',2);
  fireEvent.click(within(review('roads')).getByRole('button',{name:'Check metadata again'}));
  fireEvent.click(await screen.findByRole('button',{name:'Use suggested unsaved edits'}));
  await screen.findByText(/Suggested edits are in the workspace editor/);
  expect(saved).toBeUndefined();
  fireEvent.click(screen.getByRole('button',{name:'Save & reload XYZ'}));
  await waitFor(()=>expect(saved).toBeTruthy());
  expect(saved.revision).toBe('rev-1');
  expect(saved.workspace.key).toBe('local map title');
  expect(saved.workspace.locales).toEqual(workspace.locales);
  expect(saved.workspace.locale.layers.Other).toEqual(localLayer);
  const layer=saved.workspace.locale.layers.Roads;
  expect(layer.infoj).toEqual([savedLayer.infoj[1],expect.objectContaining({field:'total',type:'integer'})]);
  expect(layer._dashboard.symbologyInspection.fields).toEqual(['score']);
},15000);

test('isolates pending replacements, ignores old responses, and protects edited consumers from delayed suggestions', async()=>{
  let release, objectGeneration=2, currentGeneration=2;
  const held=new Promise(resolve=>{release=resolve});
  let first=true;
  const data={...workspace,locale:{layers:{...workspace.locale.layers,Other:{...savedLayer,name:'Other'}}}};
  const fetch=mockApi(path=>{
    if(path==='/api/semantic/derived-profiles/roads'){
      if(first){first=false;return held}
      return {derivedProfile:binding('roads',currentGeneration)};
    }
    if(path==='/api/semantic/derived-profiles/parks')return {derivedProfile:binding('parks',3,'repair_required')};
    if(path==='/api/semantic/catalog/objects/asset%3Aroads')return {asset:asset('roads',objectGeneration)};
  },data);
  render(<Dashboard/>);
  fireEvent.click(await screen.findByRole('button',{name:'Roads'}));
  emit(result('roads',1,{added:[],removed:['old'],changed:[]}));
  emit(result('parks',3));
  await screen.findByText(/repair_required/, {selector:'p[role="status"]'});
  const json=screen.getByLabelText('Advanced layer JSON');
  const local={...JSON.parse(json.value),infoj:[{field:'old',title:'My subsequent edit'}]};
  fireEvent.change(json,{target:{value:JSON.stringify(local)}});
  objectGeneration=1;
  emit(result('roads',2,{added:['total'],removed:[],changed:[]}));
  await within(review('roads')).findByText(/catalog object does not match/);
  expect(accept(review('roads'))).toBeNull();
  await act(async()=>release({derivedProfile:binding('roads',1)}));
  expect(accept(review('roads'))).toBeNull();
  objectGeneration=2;
  fireEvent.click(within(review('roads')).getByRole('button',{name:'Check metadata again'}));
  await within(review('roads')).findByText(/excluded from suggestions: Roads/);
  expect(within(review('roads')).getByText(/1 information field\(s\) to add, 1 to remove/)).toBeTruthy();
  currentGeneration=3;
  fireEvent.click(accept(review('roads')));
  await within(review('roads')).findByText(/replacement was superseded/);
  expect(accept(review('roads'))).toBeNull();
  expect(JSON.parse(json.value)).toEqual(local);
  expect(review('parks')).toBeTruthy();
  expect(fetch.mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(0);
});

test.each([
  ['type-only',{added:[],removed:[],changed:['score']},true],
  ['meaning-only',{added:[],removed:[],changed:[]},false],
])('surfaces %s impact without automatically changing a clean editor',async(_label,changes,hasEdits)=>{
  mockApi(path=>{
    if(path==='/api/semantic/derived-profiles/roads')return {derivedProfile:binding('roads',2)};
    if(path==='/api/semantic/catalog/objects/asset%3Aroads')return {asset:asset('roads',2)};
  });
  render(<Dashboard/>);
  await screen.findByRole('button',{name:'Roads'});
  emit(result('roads',2,changes));
  const panel=review('roads');
  await within(panel).findByText('Matching generation is ready.');
  expect(within(panel).getByText(/Values can have a new meaning/)).toBeTruthy();
  expect(!!accept(panel)).toBe(hasEdits);
  fireEvent.click(within(panel).getByRole('button',{name:'Dismiss workspace review'}));
  expect(screen.getByRole('button',{name:'Save & reload XYZ'}).disabled).toBe(true);
});

test('automatically retries a delayed delivery and keeps a recreated asset separate from the prior offer', async()=>{
  let current=binding('roads',5,'registering');
  const fetch=mockApi(path=>{
    if(path==='/api/semantic/derived-profiles/roads')return {derivedProfile:current};
    if(path==='/api/semantic/catalog/objects/asset%3Aroads')return {asset:asset('roads',5)};
  });
  render(<Dashboard/>);
  await screen.findByRole('button',{name:'Roads'});
  vi.useFakeTimers();
  await act(async()=>emit(result('roads',5,{added:['total'],removed:[],changed:[]})));
  expect(within(review('roads')).getByText(/Waiting for ready metadata/)).toBeTruthy();
  current=binding('roads',5);
  await act(async()=>{await vi.advanceTimersByTimeAsync(10000)});
  expect(accept(review('roads'))).toBeTruthy();
  const recreated={...result('roads',1),semanticProfile:{...binding('roads',1),assetId:'new-asset'}};
  current={...recreated.semanticProfile,status:'registering'};
  await act(async()=>emit(recreated));
  const [oldPanel,newPanel]=screen.getAllByRole('region',{name:'Replacement of roads'});
  expect(within(newPanel).getByText(/Generation 1/)).toBeTruthy();
  expect(within(oldPanel).getByText(/replacement was superseded/)).toBeTruthy();
  expect(accept(oldPanel)).toBeNull();
  fireEvent.click(within(oldPanel).getByRole('button',{name:'Dismiss workspace review'}));
  expect(review('roads')).toBe(newPanel);
  expect(fetch.mock.calls.filter(([,options])=>options?.method==='POST')).toHaveLength(0);
});

test('acceptance sees a replacement committed while the catalogue still serves the old generation',async()=>{
  let objectReads=0, current=2;
  mockApi(path=>{
    if(path==='/api/semantic/derived-profiles/roads')return {derivedProfile:binding('roads',current)};
    if(path==='/api/semantic/catalog/objects/asset%3Aroads'){
      if(++objectReads===2)current=3;
      return {asset:asset('roads',2)};
    }
  });
  render(<Dashboard/>);
  await screen.findByRole('button',{name:'Roads'});
  emit(result('roads',2,{added:['total'],removed:['old'],changed:[]}));
  fireEvent.click(await screen.findByRole('button',{name:'Use suggested unsaved edits'}));
  await within(review('roads')).findByText(/replacement was superseded/);
  expect(screen.getByRole('button',{name:'Save & reload XYZ'}).disabled).toBe(true);
});

test.each([{table:'source_west.roads'},{tables:{8:'source_west.roads'}}])(
  'excludes a default layer whose field edits would reach a different locale binding %j',async override=>{
  const data={...workspace,locales:{west:{layers:{Roads:override}}}};
  mockApi(path=>{
    if(path==='/api/semantic/derived-profiles/roads')return {derivedProfile:binding('roads',2)};
    if(path==='/api/semantic/catalog/objects/asset%3Aroads')return {asset:asset('roads',2)};
  },data);
  render(<Dashboard/>);
  await screen.findByRole('button',{name:'Roads'});
  emit(result('roads',2,{added:['total'],removed:['old'],changed:[]}));
  await within(review('roads')).findByText(/different relation or zoom binding: west:Roads/);
  expect(accept(review('roads'))).toBeNull();
  expect(screen.getByRole('button',{name:'Save & reload XYZ'}).disabled).toBe(true);
});

test('an earlier completion arriving after the latest generation requires manual review',async()=>{
  mockApi(path=>{
    if(path==='/api/semantic/derived-profiles/roads')return {derivedProfile:binding('roads',3)};
    if(path==='/api/semantic/catalog/objects/asset%3Aroads')return {asset:asset('roads',3)};
  });
  render(<Dashboard/>);
  await screen.findByRole('button',{name:'Roads'});
  emit(result('roads',3,{added:['total'],removed:[],changed:[]}));
  await screen.findByRole('button',{name:'Use suggested unsaved edits'});
  emit(result('roads',2,{added:[],removed:['old'],changed:[]}));
  await within(review('roads')).findByText(/Review earlier changes manually/);
  expect(accept(review('roads'))).toBeNull();
});
