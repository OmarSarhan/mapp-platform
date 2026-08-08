import React, {useEffect, useMemo, useRef, useState} from 'react';
import {createPortal} from 'react-dom';
import {createRoot} from 'react-dom/client';
import '../static/app.css';
import {
  ApiError,
  activeLocale,
  confirmedWorkspaceReload,
  renderedLocales,
  requestJson,
  savedWorkspaceFromError,
  workspaceSaveFailurePhase,
  workspaceSaveStatus,
} from './api.js';
import {SemanticCatalog} from './semantic.jsx';

let csrfToken=sessionStorage.getItem('mapp-csrf')||'';
const AUTH_REQUIRED_EVENT='mapp-auth-required';
const clearAuthentication=()=>{
 csrfToken='';
 sessionStorage.removeItem('mapp-csrf');
 window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
};
const api=async(path,options={})=>{
 try{return await requestJson(path,options,{csrfToken})}
 catch(error){if(error?.status===401)clearAuthentication();throw error}
};
const waitForOperation=async response=>{
 if(!response?.operation)return response;
 let envelope=response,operation=response.operation;
 while(operation.status==='running'){
  await new Promise(resolve=>setTimeout(resolve,1500));
  envelope=await api(response.statusUrl||`/api/operations/${operation.id}`);
  operation=envelope.operation;
 }
 if(operation.status==='succeeded')return operation.result||{};
 const operationError=operation.error&&typeof operation.error==='object'?operation.error:{};
 const status=Number.isInteger(operationError.status)?operationError.status:422;
 const meta=operationError.meta||envelope.meta;
 throw new ApiError(operationError.userMessage||operationError.error||operationError.message||'Background operation failed.',{status,payload:{...operationError,operation,...(meta?{meta}:{})}});
};
const clone=x=>structuredClone(x);
const title=x=>x.replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
const HELP={
  'Key':'Stable identifier for this XYZ workspace. Keep it unique for the deployment.',
  'Database':'Named XYZ database connection. It must match a configured DBS_* connection.',
  'Locale name':'Human-readable name shown for this map locale.',
  'Scale units':'Units used by the map scale line.',
  'North':'Northern latitude of the permitted map extent, from -90 to 90.',
  'East':'Eastern longitude of the permitted map extent, from -180 to 180.',
  'South':'Southern latitude of the permitted map extent. It must be below North.',
  'West':'Western longitude of the permitted map extent, from -180 to 180.',
  'Latitude':'Initial map-centre latitude. It must fall inside the configured extent.',
  'Longitude':'Initial map-centre longitude. It must fall inside the configured extent.',
  'Zoom':'Initial map zoom level, from 0 to 30.',
  'Mask outside extent':'When enabled, XYZ visually masks areas outside the configured extent.',
  'Display name':'Human-readable layer name shown in the XYZ interface.',
  'Layer folder':'Navigation only. Layers with the same exact value share an XYZ layer-list drawer; folder position does not control map drawing order.',
  'Drawing order':'Layer z-index used for map rendering. Higher values draw above lower values, independently of the layer folder.',
  'Promote when shown':'When switched on, XYZ moves this layer above all other currently displayed layers each time it is shown.',
  'Format':'How XYZ loads and renders the layer. Database map layers normally use MVT.',
  'Visible initially':'Controls whether the layer is switched on when the workspace first loads.',
  'Tile URI':'URL template used to request raster tiles. It normally contains {z}, {x}, and {y}.',
  'Table':'Selectable PostgreSQL relation supplying this layer.',
  'Source relations':'Comma-separated, schema-qualified database tables or views read by this query, for example leeds.definitive_paths. Include every relation named in the SELECT.',
  'Geometry column':'PostGIS geometry column used to render the layer.',
  'SRID':'Spatial reference identifier reported by PostGIS. MVT layers require EPSG:3857.',
  'ID column':'Column used as the stable feature identifier. Values should be unique and non-null.',
  'Symbol':'Point-symbol shape used by the default layer style.',
  'Fill color':'Colour used inside point or polygon symbols.',
  'Fill opacity':'Transparency of the fill, from 0 (invisible) to 1 (opaque).',
  'Stroke color':'Colour used for feature outlines or line features.',
  'Stroke width':'Outline or line width in screen pixels, from 0 to 20.',
  'Stroke opacity':'Transparency of an outline or line, from 0 (invisible) to 1 (opaque).',
  'Line pattern':'OpenLayers line-dash pattern applied by XYZ.',
  'Icon scale':'Multiplier applied by XYZ to the built-in point symbol.',
  'Marker letter':'Single character drawn inside XYZ’s marker-letter symbol.',
  'Marker color':'Outer colour used by XYZ’s marker-color symbol.',
  'Dot color':'Inner dot colour used by XYZ’s marker-color symbol.',
  'SVG icon':'SVG file loaded from instance/public/svg and served by XYZ under /instance/svg.',
  'Show hover tooltip':'Shows an XYZ map tooltip when the pointer is over a feature.',
  'Hover field':'Database field displayed in the map tooltip. XYZ includes it in the rendered feature properties.',
  'Hover title':'Label used for the hover control in XYZ’s layer style panel.',
  'Dynamic hover query':'Queries the current value on every hover instead of using XYZ’s cached hover response.',
  'Title':'Label shown beside this value in the XYZ feature-information panel.',
  'Value source':'Choose whether XYZ reads a database column or evaluates one trusted SQL expression.',
  'Information type':'XYZ renderer used for this value. Validation checks that the database result has a compatible type and shape.',
  'Database column':'Database column returned for this feature-information entry.',
  'Result key':'Required result alias used by XYZ and infoj. It is not read as a database column in SQL-expression mode.',
  'SQL expression':'Trusted PostgreSQL expression evaluated instead of reading a database column.',
  'Inline':'Displays the title and value compactly on the same line when supported.',
  'Display':'Controls whether this entry is visible in feature information.',
  'Show symbol legend':'Shows the effective default map symbol and a label in XYZ’s layer Styling panel. This is optional.',
  'Legend label':'Text displayed beside the symbol in the layer legend.',
  'Keep information symbol synchronized':'Copies the static default/fallback map symbol into this geometry information entry whenever that symbology changes.',
  'Symbology mode':'Static uses one symbol. Data-driven categorized chooses a symbol from a configured list using a feature field.',
  'Category field':'Feature field whose value selects the categorized symbol.',
  'Count only features in viewport':'Scopes XYZ’s Filtering-panel count and generated ranges to the current map view. This is optional.',
  'Show viewport count beside layer name':'Shows the visible feature count in brackets beside this layer in XYZ. It refreshes after the map moves and only queries while the layer is visible.',
  'Count label':'Text displayed after the numeric feature count.',
  'Viewport count note':'XYZ workspace extension text preserved through Advanced JSON.'
};
function InfoLabel({label,help,generated=false}) { const text=help||HELP[label]||`Configure the ${label.toLowerCase()} value used by XYZ.`,[position,setPosition]=useState(null);const show=event=>{const rect=event.currentTarget.getBoundingClientRect(),width=260,left=Math.max(10,Math.min(window.innerWidth-width-10,rect.left+rect.width/2-width/2));setPosition({left,top:rect.bottom+8,width})};return <span className="field-label">{label}{generated&&<span className="generated-badge">Auto</span>}<span className="info-tip" tabIndex="0" aria-label={`${label}: ${text}`} onMouseEnter={show} onMouseLeave={()=>setPosition(null)} onFocus={show} onBlur={()=>setPosition(null)}></span>{position&&createPortal(<span className="tooltip tooltip-portal" style={position} role="tooltip">{text}</span>,document.body)}</span> }
function Field({label,help,value,onChange,onBlur,type='text',options,min,max,step,readOnly,disabled=false,generated=false}) { return <label className={disabled?'field-disabled':''}><InfoLabel label={label} help={help} generated={generated}/>{options?<select aria-label={label} disabled={disabled} value={value??''} onChange={e=>onChange(e.target.value)} onBlur={onBlur}>{!options.includes(value)&&<option>{value}</option>}{options.map(x=><option key={x}>{x}</option>)}</select>:<input aria-label={label} disabled={disabled} type={type} value={value??''} min={min} max={max} step={step} readOnly={readOnly} onBlur={onBlur} onChange={e=>onChange(type==='number'?(e.target.value===''?'':Number(e.target.value)):e.target.value)}/>}</label> }
function JsonObjectEditor({label,value,onChange,disabled=false,help}){const serialized=JSON.stringify(value||{},null,2),[draft,setDraft]=useState(serialized),[error,setError]=useState('');useEffect(()=>{setDraft(serialized);setError('')},[serialized]);const edit=text=>{setDraft(text);try{const parsed=JSON.parse(text);if(!parsed||Array.isArray(parsed)||typeof parsed!=='object')throw new Error('Enter a JSON object.');setError('');onChange(parsed)}catch(err){setError(err.message)}};return <label className={`full expression-editor ${disabled?'field-disabled':''}`}><InfoLabel label={label} help={help}/><textarea disabled={disabled} rows="9" spellCheck="false" value={draft} onChange={event=>edit(event.target.value)}/>{error&&<small className="validation-hint">{error}</small>}</label>}
function Check({label,help,value,onChange,disabled=false}) { return <label className={`check ${disabled?'field-disabled':''}`}><input disabled={disabled} type="checkbox" checked={!!value} onChange={e=>onChange(e.target.checked)}/><span className="check-content"><InfoLabel label={label} help={help}/></span></label> }
function SvgPicker({icons,value,onChange}) {
 const [open,setOpen]=useState(false),[query,setQuery]=useState(''),selected=icons.find(icon=>icon.url===value);
 const matches=icons.filter(icon=>`${icon.name} ${icon.filename}`.toLowerCase().includes(query.trim().toLowerCase()));
 const choose=icon=>{onChange(icon.url);setQuery('');setOpen(false)};
 return <label className="svg-picker"><InfoLabel label="SVG icon"/><button type="button" className="svg-picker-trigger" aria-haspopup="listbox" aria-expanded={open} onClick={()=>setOpen(!open)}>{selected?<><img src={selected.url} alt=""/><span><strong>{selected.name}</strong><small>{selected.filename}</small></span></>:<span className="svg-placeholder">Choose an SVG icon</span>}<span className="svg-chevron">⌄</span></button>{open&&<div className="svg-picker-menu"><input autoFocus type="search" value={query} placeholder="Search SVG icons…" onChange={event=>setQuery(event.target.value)} onKeyDown={event=>{if(event.key==='Escape')setOpen(false);if(event.key==='Enter'&&matches[0]){event.preventDefault();choose(matches[0])}}}/><div className="svg-picker-options" role="listbox">{matches.map(icon=><button type="button" role="option" aria-selected={icon.url===value} className={icon.url===value?'selected':''} key={icon.url} onClick={()=>choose(icon)}><img src={icon.url} alt=""/><span><strong>{icon.name}</strong><small>{icon.filename}</small></span>{icon.url===value&&<span className="svg-check">✓</span>}</button>)}{matches.length===0&&<p>No matching SVG icons.</p>}</div></div>}</label>
}
const ICON_TYPES=['dot','target','triangle','square','diamond','semiCircle','circle','markerLetter','markerColor','Custom SVG'];
const INFO_TYPES=['text','textarea','html','link','numeric','integer','boolean','date','datetime','time','json','dataview','pills','image','images','documents','geometry','pin'];
const DASHES={'Solid':undefined,'Short dash':[5,4],'Long dash':[10,6],'Dotted':[1,4],'Dash dot':[8,4,1,4]};
const STYLE_ELEMENTS=[['labels','Label selector'],['label','Label toggle'],['hovers','Hover selector'],['hover','Hover toggle'],['themes','Theme selector'],['theme','Theme legend'],['icon_scaling','Icon scaling'],['opacitySlider','Opacity slider']];
const FILTER_OPTIONS={'None':null,'Automatic':true,'Text prefix':'like','Exact match':'match','Numeric range':'numeric','Integer range':'integer','Include values':'in','Exclude values':'ni','Date range':'date','Date/time range':'datetime','Boolean':'boolean','Null status':'null'};
const FILTER_OPTIONS_BY_INFO_TYPE={
 text:['None','Automatic','Text prefix','Exact match','Include values','Exclude values','Null status'],
 numeric:['None','Automatic','Numeric range','Exact match','Include values','Exclude values','Null status'],
 integer:['None','Automatic','Integer range','Exact match','Include values','Exclude values','Null status'],
 date:['None','Automatic','Date range','Exact match','Include values','Exclude values','Null status'],
 datetime:['None','Automatic','Date/time range','Exact match','Include values','Exclude values','Null status'],
 boolean:['None','Automatic','Boolean','Exact match','Null status'],
};
const filterOptionsFor=entry=>entry?.fieldfx?['None']:FILTER_OPTIONS_BY_INFO_TYPE[entry?.type||'text']||['None'];
const infoTypeForColumn=column=>/int/.test(column?.type||'')?'integer':/numeric|double|real|decimal/.test(column?.type||'')?'numeric':/bool/.test(column?.type||'')?'boolean':/timestamp/.test(column?.type||'')?'datetime':column?.type==='date'?'date':'text';
const inferGeometry=(table,format='mvt')=>table?.columns.find(column=>column.geometryType&&(format!=='mvt'||column.srid===3857))||table?.columns.find(column=>column.geometryType);
const inferId=table=>table?.columns.find(column=>column.primaryKey)||table?.columns.find(column=>column.unique&&!column.nullable)||table?.columns.find(column=>!column.geometryType&&!column.nullable&&/^(id|object_id|objectid|gid|fid|key|.*_id)$/i.test(column.name))||table?.columns.find(column=>!column.geometryType&&!column.nullable)||table?.columns.find(column=>!column.geometryType);
const standardInfoEntries=geom=>[{type:'geometry',display:true,field:geom,fieldfx:`ST_asGeoJSON(${geom})`},{type:'pin',label:'Location',display:true,field:'pin',fieldfx:`ARRAY[ST_X(ST_PointOnSurface(${geom})),ST_Y(ST_PointOnSurface(${geom}))]`}];
const generatedLayer=(table,format='mvt')=>{const geom=inferGeometry(table,format),id=inferId(table);return{geom,id}};
const layerKeyBase=name=>String(name||'Layer').normalize('NFKD').replace(/[^A-Za-z0-9\s_]/g,'').trim().replace(/\s+/g,'_')||'Layer';
const uniqueLayerKey=(name,occupied,current)=>{const base=layerKeyBase(name);if(base===current||!occupied.has(base))return base;let suffix=1;while(occupied.has(`${base}_${suffix}`)&&`${base}_${suffix}`!==current)suffix++;return`${base}_${suffix}`};
export function reconcileDerivedWorkspace(workspace,derived,tables){
 const changes=derived?.columnChanges||{},added=new Set(changes.added||[]),removed=new Set(changes.removed||[]),affected=new Set([...(changes.removed||[]),...(changes.changed||[])]),relation=`derived_layers.${derived?.name}`,table=tables.find(item=>`${item.schema}.${item.table}`===relation),columns=new Map((table?.columns||[]).map(column=>[column.name,column])),summary={layers:0,added:0,removed:0};
 if(!derived?.name||(!added.size&&!removed.size))return{workspace,summary};
 const next=clone(workspace),locales=[next.locale,...Object.values(next.locales||{})].filter(Boolean);
 for(const locale of locales)for(const layer of Object.values(locale.layers||{})){
  if(layer?.table!==relation)continue;
  summary.layers++;
  const existing=layer.infoj||[],kept=[];
  for(const entry of existing){
   if(entry&&removed.has(entry.field)&&!entry.fieldfx){summary.removed++;continue}
   kept.push(entry);
  }
  const fields=new Set(kept.map(entry=>entry?.field).filter(Boolean));
  for(const name of added){
   const column=columns.get(name);
   if(!column||column.geometryType||name===layer.qID||fields.has(name))continue;
   kept.push({type:infoTypeForColumn(column),title:title(name),field:name,inline:true,display:true,_dashboard:{catalogField:true}});
   fields.add(name);summary.added++;
  }
  layer.infoj=kept;
  if(layer.filter&&typeof layer.filter==='object')for(const key of ['include','exclude'])if(Array.isArray(layer.filter[key]))layer.filter[key]=layer.filter[key].filter(field=>!removed.has(field));
  if(layer.style?.hover&&typeof layer.style.hover==='object'&&removed.has(layer.style.hover.field))delete layer.style.hover;
  const style=layer.style||{},theme=typeof style.theme==='string'?style.themes?.[style.theme]:style.theme,themeFields=theme&&typeof theme==='object'?[theme.field,...(theme.fields||[]),...(theme.categories||[]).map(category=>category?.field)].filter(Boolean):[],inspect=[...new Set(themeFields.filter(field=>affected.has(field)))];
  if(inspect.length)layer._dashboard={...(layer._dashboard||{}),symbologyInspection:{fields:inspect,reason:'derived_schema_changed',relation}};
 }
 return{workspace:next,summary};
}
export function geometryKind(layer,table){const selected=table?.columns.find(c=>c.name===layer.geom),specific=value=>value&&String(value).toUpperCase()!=='GEOMETRY',known=specific(selected?.geometryType)?selected:table?.columns.find(c=>specific(c.geometryType)),type=String(known?.geometryType||'').toUpperCase(),normal=layer.style?.default||{};if(type.includes('POINT'))return'point';if(type.includes('LINE'))return'line';if(type.includes('POLYGON'))return'polygon';if(normal.icon)return'point';if(normal.fillColor!==undefined||normal.fillOpacity!==undefined)return'polygon';if(normal.strokeColor!==undefined||normal.strokeWidth!==undefined||normal.lineDash!==undefined)return'line';return'polygon'}
function infoGeometryStyle(defaultStyle={}){const next=clone(defaultStyle);if(next.icon){next.fillColor=null;next.strokeColor=null}else if(!next.fillColor)next.fillColor=null;return next}
function iconDefaults(type,old={},icons=[]){if(type==='Custom SVG')return{url:icons[0]?.url||'',scale:old.scale||1};const shared={type,scale:old.scale||1};if(type==='circle')return{...shared,strokeColor:old.strokeColor||'#176b4d',strokeWidth:old.strokeWidth||2};if(type==='markerLetter')return{...shared,color:old.color||'#176b4d',letter:old.letter||'A'};if(type==='markerColor')return{...shared,colorMarker:old.colorMarker||'#176b4d',colorDot:old.colorDot||'#ffffff'};return{...shared,fillColor:old.fillColor||'#176b4d'}}
function IconSymbol({icon}){const type=icon.type||'dot',fill=icon.fillColor||'#ffffff',stroke=icon.strokeColor||'#333',width=icon.strokeWidth||1,scale=icon.scale||1;if(icon.url)return <img className="xyz-symbol custom-svg" style={{transform:`scale(${scale})`}} src={icon.url} alt="Custom SVG icon preview"/>;return <svg className="xyz-symbol" style={{transform:`scale(${scale})`}} viewBox="0 0 30 30" aria-label={`${type} symbol preview`}>{type==='dot'&&<><circle cx="16" cy="16" r="10" fill="#333" opacity=".35"/><circle cx="15" cy="15" r="10" fill={fill}/></>}{type==='target'&&<><circle cx="16" cy="15" r="10" fill="#333" opacity=".4"/><circle cx="15" cy="15" r="10" fill={fill}/><circle cx="15" cy="15" r="4" fill="#333" opacity=".35"/></>}{type==='triangle'&&<><polygon points="15,4 3,26 27,26" fill="#333" opacity=".35"/><polygon points="15,3 4,24 26,24" fill={fill}/></>}{type==='square'&&<><rect x="5" y="5" width="22" height="22" rx="1" fill="#333" opacity=".3"/><rect x="3" y="3" width="22" height="22" rx="1" fill={fill}/></>}{type==='diamond'&&<><polygon points="16,2 29,15 16,28 3,15" fill="#333" opacity=".3"/><polygon points="15,1 28,14 15,27 2,14" fill={fill}/></>}{type==='semiCircle'&&<><path d="M4 16a11 11 0 0 1 22 0z" fill="#333" opacity=".4"/><path d="M3 15a11 11 0 0 1 22 0z" fill={fill}/></>}{type==='circle'&&<circle cx="15" cy="15" r="10" stroke={stroke} strokeWidth={width} fill="#ffffff33"/>}{type==='markerLetter'&&<><path d="M15 2C8 2 5 7 5 12c0 7 10 16 10 16s10-9 10-16C25 7 22 2 15 2z" fill={icon.color||'#176b4d'}/><circle cx="15" cy="11" r="6" fill="#fff"/><text x="15" y="14" textAnchor="middle" fontSize="9" fontWeight="700" fill="#555">{String(icon.letter||'A').slice(0,1)}</text></>}{type==='markerColor'&&<><path d="M15 2C8 2 5 7 5 12c0 7 10 16 10 16s10-9 10-16C25 7 22 2 15 2z" fill={icon.colorMarker||'#176b4d'}/><circle cx="15" cy="11" r="6" fill="#fff" opacity=".85"/><circle cx="15" cy="11" r="3" fill={icon.colorDot||'#fff'}/></>}</svg>}
function StyleControls({title:heading,kind,value,onChange,icons,highlight=false,inherited={}}){if(Array.isArray(value?.icon))return <div className="subpanel"><h3>{heading}</h3><p className="muted style-note">This XYZ style contains multiple icons. It is preserved unchanged; edit it only in Advanced layer JSON.</p><IconSymbol icon={value.icon[0]||{type:'dot'}}/></div>;const set=(key,val)=>onChange({...value,[key]:val}),effective=highlight?{...inherited,...value}:value,dashName=Object.entries(DASHES).find(([,v])=>JSON.stringify(v)===JSON.stringify(effective.lineDash))?.[0]||'Solid';if(kind==='point'){const base=Array.isArray(inherited.icon)?inherited.icon[0]:inherited.icon||{},icon=value.icon||base||{type:'dot'},symbol=icon.url?'Custom SVG':icon.type||'dot',setIcon=(key,val)=>{const next={...icon,[key]:val};if(key!=='url')delete next.url;onChange({...value,icon:next})};return <div className="subpanel"><h3>{heading}</h3>{highlight&&<p className="muted style-note">Unset highlight values inherit the default symbology in XYZ.</p>}<div className="grid"><Field label="Symbol" value={symbol} options={ICON_TYPES} onChange={v=>onChange({...value,icon:iconDefaults(v,icon,icons)})}/>{symbol==='Custom SVG'&&<SvgPicker icons={icons} value={icon.url||''} onChange={v=>onChange({...value,icon:{url:v,scale:icon.scale||1}})}/>} {!['Custom SVG','circle','markerLetter','markerColor'].includes(symbol)&&<Field label="Fill color" type="color" value={icon.fillColor||'#176b4d'} onChange={v=>setIcon('fillColor',v)}/>} {symbol==='circle'&&<><Field label="Stroke color" type="color" value={icon.strokeColor||'#176b4d'} onChange={v=>setIcon('strokeColor',v)}/><Field label="Stroke width" type="number" min={0} max={20} step={.5} value={icon.strokeWidth??1} onChange={v=>setIcon('strokeWidth',v)}/></>}{symbol==='markerLetter'&&<><Field label="Marker color" type="color" value={icon.color||'#176b4d'} onChange={v=>setIcon('color',v)}/><Field label="Marker letter" value={icon.letter||'A'} onChange={v=>setIcon('letter',v.slice(0,1))}/></>}{symbol==='markerColor'&&<><Field label="Marker color" type="color" value={icon.colorMarker||'#176b4d'} onChange={v=>setIcon('colorMarker',v)}/><Field label="Dot color" type="color" value={icon.colorDot||'#ffffff'} onChange={v=>setIcon('colorDot',v)}/></>}<Field label={highlight?"Highlight scale":"Icon scale"} type="number" min={.1} max={10} step={.1} value={highlight?(value.highlightScale??value.scale??1):(icon.scale??1)} onChange={v=>highlight?onChange({...value,highlightScale:v,icon:{...icon}}):onChange({...value,icon:{...icon,scale:v}})}/></div></div>}return <div className="subpanel"><h3>{heading}</h3>{highlight&&<p className="muted style-note">These are the effective highlight values. Unset properties inherit from the default style in XYZ.</p>}<div className="grid">{kind==='polygon'&&<><Field label="Fill color" type="color" value={effective.fillColor||'#176b4d'} onChange={v=>set('fillColor',v)}/><Field label="Fill opacity" type="number" min={0} max={1} step={.05} value={effective.fillOpacity??.3} onChange={v=>set('fillOpacity',v)}/></>}<Field label="Stroke color" type="color" value={effective.strokeColor||'#0f5138'} onChange={v=>set('strokeColor',v)}/><Field label="Stroke opacity" type="number" min={0} max={1} step={.05} value={effective.strokeOpacity??1} onChange={v=>set('strokeOpacity',v)}/><Field label="Stroke width" type="number" min={0} max={20} step={.5} value={effective.strokeWidth??2} onChange={v=>set('strokeWidth',v)}/><Field label="Line pattern" value={dashName} options={Object.keys(DASHES)} onChange={v=>{const next={...value};DASHES[v]?next.lineDash=DASHES[v]:delete next.lineDash;onChange(next)}}/></div></div>}
function effectiveTheme(style={}){if(typeof style.theme==='string')return{key:style.theme,theme:style.themes?.[style.theme]};if(style.theme&&typeof style.theme==='object')return{key:null,theme:style.theme};return{key:null,theme:null}}
const CATEGORY_PALETTE=['#176b4d','#277da1','#f8961e','#d1495b'];
function categoryStyle(defaultStyle,kind,color){const next=clone(defaultStyle||{});if(kind==='point'){const icon=Array.isArray(next.icon)?clone(next.icon[0]||{}):clone(next.icon||{type:'dot'});icon.fillColor=color;next.icon=icon}else if(kind==='line')next.strokeColor=color;else next.fillColor=color;return next}
function graduatedRange(categories,index,comparison){if(categories.length===1)return'All numeric values';const value=categories[index]?.value,previous=categories[index-1]?.value;if(comparison==='greater_than')return index===0?`≥ ${value}`:index===categories.length-1?`< ${previous}`:`< ${previous} and ≥ ${value}`;return index===0?`≤ ${value}`:index===categories.length-1?`> ${previous}`:`> ${previous} and ≤ ${value}`}
const THEME_MODES={'Static':null,'Basic legend':'basic','Data-driven categorized':'categorized','Data-driven graduated':'graduated','Data-driven distributed':'distributed'};
function ThemeControls({layer,table,icons,setLayer}) {
 const style=layer.style||{},active=effectiveTheme(style),theme=active.theme,kind=geometryKind(layer,table),type=theme?.type||null,mode=Object.entries(THEME_MODES).find(([,value])=>value===type)?.[0]||'Static',columns=(table?.columns||[]).filter(column=>!column.geometryType),fields=[...new Set([...columns.map(column=>column.name),...(layer.infoj||[]).filter(entry=>entry.field&&!['geometry','pin'].includes(entry.type)).map(entry=>entry.field)])],numericFields=fields.filter(field=>{const column=columns.find(item=>item.name===field),entry=(layer.infoj||[]).find(item=>item.field===field);return /int|numeric|double|real|decimal/.test(column?.type||'')||['integer','numeric'].includes(entry?.type)}),categories=Array.isArray(theme?.categories)?theme.categories:[],namedKeys=Object.keys(style.themes||{});
 const commit=nextStyle=>{const next={...layer,style:nextStyle},dashboard={...(layer._dashboard||{})};delete dashboard.symbologyInspection;Object.keys(dashboard).length?next._dashboard=dashboard:delete next._dashboard;setLayer(next)},inspection=layer._dashboard?.symbologyInspection;
 const writeTheme=nextTheme=>{const nextStyle={...style};if(active.key)nextStyle.themes={...(style.themes||{}),[active.key]:nextTheme};else nextStyle.theme=nextTheme;if(Array.isArray(nextStyle.elements)&&!nextStyle.elements.includes('theme'))nextStyle.elements=[...nextStyle.elements,'theme'];commit(nextStyle)};
 const confirmReplace=nextMode=>!theme||type===THEME_MODES[nextMode]||window.confirm(`Switch from ${type||'static'} to ${nextMode}? Existing theme fields, categories, values, labels and symbol styles will be replaced and may be lost.`);
 const changeMode=nextMode=>{if(!confirmReplace(nextMode))return;const next={...style},nextType=THEME_MODES[nextMode];if(!nextType){if(active.key)delete next.themes[active.key];else delete next.theme;if(Array.isArray(next.elements))next.elements=next.elements.filter(key=>key!=='theme');commit(next);return}const field=(nextType==='graduated'?numericFields:fields)[0]||'';const created=nextType==='basic'?{type:'basic',label:layer.name||'Layer',style:clone(style.default||{})}:{type:nextType,title:`${layer.name||'Layer'} legend`,field:nextType==='distributed'?(layer.qID||field||'id'):field,categories:[],...(nextType==='graduated'?{graduated_breaks:'less_than'}:{})};if(active.key){next.themes={...(next.themes||{}),[active.key]:created};next.theme=active.key}else next.theme=created;if(Array.isArray(next.elements)&&!next.elements.includes('theme'))next.elements=[...next.elements,'theme'];commit(next)};
 const chooseNamed=key=>commit({...style,theme:key});
 const updateCategory=(index,key,value)=>{const next=clone(categories),category={...next[index],[key]:value};next[index]=category;writeTheme({...theme,categories:next})};
 const updateCategoryStyle=(index,value)=>{const next=clone(categories);next[index]={...next[index],style:value};writeTheme({...theme,categories:next})};
 const addCategory=()=>{const index=categories.length,category={label:`Class ${index+1}`,style:categoryStyle(style.default,kind,CATEGORY_PALETTE[index%CATEGORY_PALETTE.length])};if(type==='graduated')category.value=(index+1)*10;else if(type==='categorized')category.value=`Value ${index+1}`;if(type==='categorized'&&Array.isArray(theme.fields))category.field=theme.fields[0]||'';writeTheme({...theme,categories:[...categories,category]})};
 const categoryColour=category=>kind==='point'?(Array.isArray(category.style?.icon)?category.style.icon[0]?.fillColor:category.style?.icon?.fillColor):kind==='line'?category.style?.strokeColor:category.style?.fillColor,indistinctColours=categories.length>1&&new Set(categories.map(categoryColour).filter(Boolean)).size<2;
 const applyDistinctColours=()=>writeTheme({...theme,categories:categories.map((category,index)=>({...category,style:categoryStyle({...style.default,...category.style},kind,CATEGORY_PALETTE[index%CATEGORY_PALETTE.length])}))});
 const removeCategory=index=>writeTheme({...theme,categories:categories.filter((_,item)=>item!==index)});
 const moveCategory=(index,offset)=>{const target=index+offset;if(target<0||target>=categories.length)return;const next=clone(categories),[category]=next.splice(index,1);next.splice(target,0,category);writeTheme({...theme,categories:next})};
 const multiField=type==='categorized'&&Array.isArray(theme.fields),setMultiFields=selected=>{const nextFields=[...new Set(selected.filter(field=>fields.includes(field)))],fallback=nextFields[0]||'';writeTheme({...theme,fields:nextFields,field:undefined,categories:categories.map(category=>nextFields.includes(category.field)?category:{...category,field:fallback})})},toggleMulti=enabled=>{if(enabled){const first=theme.field||fields[0]||'';writeTheme({...theme,fields:first?[first]:[],field:undefined,categories:categories.map(category=>({...category,field:category.field||first}))})}else{const first=theme.fields?.[0]||fields[0]||'';writeTheme({...theme,field:first,fields:undefined,categories:categories.map(category=>{const next={...category};delete next.field;return next})})}};
 const categorizedValueKind=field=>{const column=columns.find(item=>item.name===field),entry=(layer.infoj||[]).find(item=>item.field===field),fieldType=`${column?.type||''} ${entry?.type||''}`.toLowerCase();return /bool/.test(fieldType)?'boolean':/int|numeric|double|real|decimal/.test(fieldType)?'number':'text'};
 const updateCategoryValue=(index,value,field=theme.field)=>updateCategory(index,'value',categorizedValueKind(field)==='number'?Number(value):categorizedValueKind(field)==='boolean'?value==='true':value);
 const fieldOptions=type==='graduated'?numericFields:fields;
 const compatibility=kind==='point'?'Point features require an icon. Fill and stroke vector styles do not draw a point by themselves.':kind==='line'?'Line features use stroke colour, width and pattern. Fill colours and point icons do not render the line.':'Polygon features use fill colour and may also use a stroke. Point icons do not render the polygon.';
 return <div className="subpanel categorized-theme-controls"><h3>Symbology mode</h3><p className="muted style-note">These controls follow XYZ’s real theme implementations. Switching mode replaces mode-specific fields, values, labels and styles, so an existing theme requires confirmation.</p><p className="symbology-compatibility"><strong>{kind[0].toUpperCase()+kind.slice(1)} layer:</strong> {compatibility} This applies to the fallback and every theme category.</p>{inspection&&<div className="symbology-inspection-warning"><strong>Derived-layer symbology needs inspection</strong><p>The derived schema changed field{inspection.fields.length===1?'':'s'} {inspection.fields.join(', ')}. Choose a valid field or deliberately update the theme before publishing.</p></div>}<div className="grid"><Field label="Symbology mode" value={mode} options={Object.keys(THEME_MODES)} onChange={changeMode}/>{active.key&&<Field label="Active named theme" value={active.key} options={namedKeys} onChange={chooseNamed}/>} {type&&type!=='basic'&&!(type==='categorized'&&multiField)&&<Field label={type==='distributed'?'Stable identity field':type==='categorized'?'Category field':'Theme field'} value={theme.field||''} options={fieldOptions} onChange={field=>writeTheme({...theme,field})}/>} {type==='graduated'&&<Field label="Break comparison" value={theme.graduated_breaks||'less_than'} options={['less_than','greater_than']} onChange={graduated_breaks=>writeTheme({...theme,graduated_breaks})}/>} {type==='categorized'&&<Check label="Compose icons from multiple fields" help="XYZ composes an array of icons, which is suitable for point geometry only." value={multiField} disabled={kind!=='point'&&!multiField} onChange={toggleMulti}/>} {multiField&&<div className="multi-field-picker"><InfoLabel label="Category fields" help="Choose every feature field that can contribute one icon layer to the final point symbol."/><div>{fields.map(field=><label className="check compact" key={field}><input type="checkbox" checked={(theme.fields||[]).includes(field)} onChange={event=>setMultiFields(event.target.checked?[...(theme.fields||[]),field]:(theme.fields||[]).filter(item=>item!==field))}/><span className="check-content">{field}</span></label>)}</div></div>}</div>{multiField&&kind!=='point'&&<div className="symbology-inspection-warning"><strong>Multi-field icons do not suit this geometry</strong><p>XYZ’s multi-field categorized mode produces icon styles for points. Turn it off or change the layer to point geometry.</p></div>}{type&&type!=='basic'&&<><div className="theme-category-editor">{categories.map((category,index)=>{const valueField=category.field||theme.field,valueKind=categorizedValueKind(valueField);return <div className="theme-category-card" key={index}><div className="theme-category-row">{type!=='distributed'&&<Field label={type==='graduated'?'Numeric break':'Exact value'} type={type==='graduated'||type==='categorized'&&valueKind==='number'?'number':'text'} options={type==='categorized'&&valueKind==='boolean'?['true','false']:undefined} step="any" value={valueKind==='boolean'?String(category.value??false):category.value??''} onChange={value=>type==='categorized'?updateCategoryValue(index,value,valueField):updateCategory(index,'value',value)}/>} {multiField&&<Field label="Category field" value={category.field||''} options={theme.fields||[]} onChange={value=>updateCategory(index,'field',value)}/>}<Field label="Legend label" value={category.label??''} onChange={value=>updateCategory(index,'label',value)}/><div className="theme-category-actions"><button type="button" disabled={index===0} onClick={()=>moveCategory(index,-1)} aria-label={`Move category ${index+1} up`}>↑</button><button type="button" disabled={index===categories.length-1} onClick={()=>moveCategory(index,1)} aria-label={`Move category ${index+1} down`}>↓</button><button type="button" className="danger" onClick={()=>removeCategory(index)} aria-label={`Remove category ${index+1}`}>Remove</button></div></div>{type==='graduated'&&<small className="graduated-range">XYZ effective range: {graduatedRange(categories,index,theme.graduated_breaks||'less_than')}</small>}<details className="theme-symbol-editor"><summary>Edit symbol or icon</summary><StyleControls title={`Class ${index+1} symbol`} kind={kind} icons={icons} value={category.style||{}} inherited={style.default||{}} onChange={value=>updateCategoryStyle(index,value)}/></details></div>})}</div><button type="button" onClick={addCategory}>Add legend category</button>{indistinctColours&&<div className="symbology-inspection-warning"><strong>Classes use the same effective colour</strong><p>XYZ will place features into classes, but they will look alike on the map.</p><button type="button" onClick={applyDistinctColours}>Apply distinct colours to classes</button></div>}{!theme.field&&!multiField&&<p className="validation-hint">Choose the feature field required by this XYZ theme.</p>}{multiField&&!(theme.fields||[]).length&&<p className="validation-hint">Enter at least one valid category field.</p>}{categories.length===0&&<p className="validation-hint">Add at least one styled legend category.</p>}{type==='categorized'&&layer.cluster&&<p className="muted">XYZ does not apply categorized styling to clusters containing more than one feature; the cluster style is used instead.</p>}{type==='distributed'&&<p className="muted">XYZ reuses this palette across unique feature identities and attempts not to repeat a style on intersecting features.</p>}{type==='graduated'&&<p className="muted">XYZ evaluates categories in their stored order. Keep breaks ascending for less_than or descending for greater_than. The final stored category is also XYZ’s catch-all when no break matches, so label it as the remaining range.</p>}</>}</div>;
}
function ThemeUsage({layer,table}) {
 const style=layer.style||{},active=effectiveTheme(style),theme=active.theme,kind=geometryKind(layer,table),dataDriven=theme&&theme.type!=='basic',categories=Array.isArray(theme?.categories)?theme.categories:[];
 if(!theme&&active.key)return <div className="subpanel theme-usage"><h3>Symbology use</h3><p className="validation-hint">Named theme “{active.key}” is selected but is not defined in style.themes. XYZ will not have a complete data-driven legend.</p></div>;
 if(!dataDriven)return <div className="subpanel theme-usage"><h3>Symbology use</h3><p><strong>Static symbology</strong> · one default symbol is used for the layer{theme?.type==='basic'?' and its basic legend':''}.</p></div>;
 const fields=[theme.field,...(theme.fields||[])].filter(Boolean),missing=fields.filter(field=>!(layer.infoj||[]).some(entry=>entry.field===field)&&!table?.columns?.some(column=>column.name===field));
 return <div className="subpanel theme-usage"><h3>Symbology use</h3><p><strong>Data-driven {theme.type} symbology</strong>{active.key?` · named theme “${active.key}”`:''}{fields.length?` · field${fields.length===1?'':'s'} ${fields.join(', ')}`:''} · {categories.length} legend {categories.length===1?'class':'classes'}.</p><p className="muted style-note">The default symbol is the fallback. Information-panel geometry swatches are static and can be synchronized to that fallback; the XYZ theme legend represents the data-driven classes.</p>{missing.length>0&&<p className="validation-hint">Theme field{missing.length===1?'':'s'} {missing.join(', ')} cannot be matched to the catalog or feature-information fields.</p>}{categories.length>0&&<div className="symbol-states theme-category-preview">{categories.slice(0,8).map((category,index)=><PreviewSymbol key={category.key??category.value??index} style={{...(category.style||style.default||{}),...(category.icon?{icon:category.icon}:{})}} kind={kind} label={String(category.label??category.value??category.key??`Class ${index+1}`)}/>)}</div>}{categories.length>8&&<small className="muted">Showing 8 of {categories.length} legend classes.</small>}</div>;
}
function Symbology({layer,table,icons,setLayer}){const kind=geometryKind(layer,table),style=layer.style||{},setState=(state,value)=>{const next={...style,[state]:value},updated={...layer,style:next},active=effectiveTheme(style);if(state==='default'){if(active.theme?.type==='basic'){const basic={...active.theme,style:clone(value)};if(active.key)next.themes={...(style.themes||{}),[active.key]:basic};else next.theme=basic}updated.infoj=(layer.infoj||[]).map(entry=>entry.type==='geometry'&&entry._dashboard?.styleFromLayerDefault?{...entry,style:infoGeometryStyle(value)}:entry)}setLayer(updated)};return <><ThemeControls layer={layer} table={table} icons={icons} setLayer={setLayer}/><ThemeUsage layer={layer} table={table}/><StyleControls title={effectiveTheme(style).theme?.type&&!['basic'].includes(effectiveTheme(style).theme.type)?'Default / fallback symbology':'Default symbology'} kind={kind} icons={icons} value={style.default||{}} onChange={v=>setState('default',v)}/><StyleControls title="Highlight symbology" kind={kind} icons={icons} value={style.highlight||{}} inherited={style.default||{}} onChange={v=>setState('highlight',v)} highlight/></>}
function BasicLegendControls({layer,setLayer}) {
 const style=layer.style||{},theme=style.theme,managed=!theme||typeof theme==='object'&&theme.type==='basic',enabled=managed&&theme?.type==='basic';
 const updateStyle=next=>setLayer({...layer,style:next});
 const toggle=checked=>{
  const next={...style};
  if(checked){
   next.theme={type:'basic',label:layer.name||'Layer',style:clone(style.default||{})};
   if(Array.isArray(next.elements)&&!next.elements.includes('theme'))next.elements=[...next.elements,'theme'];
  }else{
   delete next.theme;
   if(Array.isArray(next.elements))next.elements=next.elements.filter(key=>key!=='theme');
  }
  updateStyle(next);
 };
 if(!managed)return <><div className="subpanel"><h3>Layer symbol legend</h3><p className="muted style-note">This layer uses an advanced or named theme. Its legend configuration is preserved unchanged and remains editable in Advanced layer JSON.</p></div><GeometrySwatchControls layer={layer} setLayer={setLayer}/></>;
 return <><div className="subpanel legend-panel-controls"><h3>Layer symbol legend</h3><p className="muted style-note">Optionally show the effective default map symbol in XYZ’s layer Styling panel. Default-symbol edits remain synchronized with this basic legend.</p><div className="grid"><Check label="Show symbol legend" value={enabled} onChange={toggle}/>{enabled&&<Field label="Legend label" value={theme.label||''} onChange={label=>updateStyle({...style,theme:{...theme,label}})}/>}</div></div><GeometrySwatchControls layer={layer} setLayer={setLayer}/></>;
}
function GeometrySwatchControls({layer,setLayer}) {
 const geometries=(layer.infoj||[]).map((entry,index)=>({entry,index})).filter(({entry})=>entry.type==='geometry');
 if(!geometries.length)return null;
 const toggle=(index,checked)=>{
  const infoj=clone(layer.infoj),entry=infoj[index],dashboard={...(entry._dashboard||{})};
  if(checked){entry.style=infoGeometryStyle(layer.style?.default||{});dashboard.styleFromLayerDefault=true;entry._dashboard=dashboard}
  else if(dashboard.styleFromLayerDefault){delete entry.style;delete dashboard.styleFromLayerDefault;Object.keys(dashboard).length?entry._dashboard=dashboard:delete entry._dashboard}
  setLayer({...layer,infoj});
 };
 return <div className="subpanel geometry-swatch-controls"><h3>Feature-information symbol</h3><p className="muted style-note">Choose whether each geometry swatch follows the layer’s static default/fallback symbol. This explicit synchronization prevents drift when symbology changes. Custom geometry styles remain independent until synchronization is enabled.</p><div className="grid">{geometries.map(({entry,index})=>{const managed=entry._dashboard?.styleFromLayerDefault===true,custom=entry.style!==undefined&&!managed,drift=managed&&JSON.stringify(entry.style)!==JSON.stringify(infoGeometryStyle(layer.style?.default||{}));return <div key={entry.key||entry.field||index}><Check label={`Keep information symbol synchronized: ${entry.label||entry.title||entry.field||`geometry ${index+1}`}`} value={managed} help={custom?'Enabling replaces the existing custom information style with the layer default/fallback symbol.':'Updates this information symbol whenever the default/fallback symbology changes.'} onChange={checked=>toggle(index,checked)}/>{drift&&<p className="validation-hint">This managed information symbol has drifted. Toggle synchronization off and on to repair it.</p>}</div>})}</div></div>;
}
function HoverControls({layer,columns,setLayer}) {
 const style=layer.style||{},hover=style.hover&&typeof style.hover==='object'?style.hover:{},enabled=!!style.hover;
 const update=next=>setLayer({...layer,style:{...style,hover:next}});
 const hoverControls=typeof style.hover==='string'
  ? <div className="subpanel"><h3>Feature hover tooltip</h3><p className="muted style-note">This layer references the named hover style “{style.hover}”. It is preserved unchanged; edit it only in Advanced layer JSON.</p></div>
  : <div className="subpanel"><h3>Feature hover tooltip</h3><div className="grid"><Check label="Show hover tooltip" value={enabled&&hover.display!==false} onChange={checked=>checked?update({display:true,field:hover.field||columns[0]?.name||'',title:hover.title||'Feature'}):setLayer({...layer,style:{...style,hover:undefined}})}/>{enabled&&<><Field label="Hover field" value={hover.field||''} options={columns.map(column=>column.name)} onChange={value=>update({...hover,field:value})}/><Field label="Hover title" value={hover.title||''} onChange={value=>update({...hover,title:value})}/><Check label="Dynamic hover query" value={hover.dynamic} onChange={value=>update({...hover,dynamic:value})}/></>}</div></div>;
 return <>{hoverControls}<FilterPanelControls layer={layer} setLayer={setLayer}/></>;
}
function StylePanelControls({layer,setLayer}){const style=layer.style||{},shown=style.hidden!==true,configured=Array.isArray(style.elements),elements=configured?style.elements:STYLE_ELEMENTS.map(([key])=>key),setStyle=next=>setLayer({...layer,style:next}),togglePanel=value=>{const next={...style};value?delete next.hidden:next.hidden=true;setStyle(next)},toggleElement=(key,enabled)=>{const next={...style},current=configured?[...elements]:STYLE_ELEMENTS.map(([name])=>name);if(enabled&&!current.includes(key)){const order=STYLE_ELEMENTS.map(([name])=>name),position=order.indexOf(key),before=current.findIndex(name=>order.indexOf(name)>position);before<0?current.push(key):current.splice(before,0,key)}else if(!enabled&&current.includes(key))current.splice(current.indexOf(key),1);next.elements=current;if(key==='opacitySlider'){if(enabled)next.opacitySlider=true;else delete next.opacitySlider}setStyle(next)};return <div className="subpanel style-panel-controls"><h3>XYZ Styling panel</h3><p className="muted style-note">Choose which interactive controls XYZ shows when this layer is expanded. A control also needs its matching style configuration; unknown custom element keys are preserved.</p><div className="grid"><Check label="Show Styling panel" value={shown} onChange={togglePanel}/>{STYLE_ELEMENTS.map(([key,label])=>{const configuredProperty=Object.hasOwn(style,key),available=shown&&(key==='opacitySlider'||configuredProperty),enabled=elements.includes(key)&&(key!=='opacitySlider'||configuredProperty);return <Check key={key} label={label} value={enabled} disabled={!available} help={!shown?'Enable the Styling panel before choosing its controls.':available?`Include ${key} in the ordered style.elements array.`:`Add style.${key} configuration before enabling this control.`} onChange={value=>toggleElement(key,value)}/>})}</div>{shown&&configured&&<p className="muted style-elements-order">Control order: {elements.join(' → ')||'no controls'}</p>}</div>}
const VIEWPORT_COUNT_PLUGIN='/instance/plugins/viewport-layer-count.mjs';
function FilterPanelControls({layer,setLayer}) {
 const filter=layer.filter&&typeof layer.filter==='object'?layer.filter:{};
 const setFilter=next=>setLayer({...layer,filter:next});
 const set=(key,value)=>{const next={...filter};value===undefined?delete next[key]:next[key]=value;setFilter(next)};
 const setHeaderCount=enabled=>{const plugins=Array.isArray(layer.plugins)?layer.plugins.filter(item=>item!==VIEWPORT_COUNT_PLUGIN):[];if(enabled)plugins.push(VIEWPORT_COUNT_PLUGIN);const next={...layer,plugins};if(enabled){next.viewport_layer_count=layer.viewport_layer_count||{};next.filter={...filter,viewport:true}}else{delete next.viewport_layer_count;if(!plugins.length)delete next.plugins}setLayer(next)};
 const optionFor=value=>{
  if(value===true)return 'Automatic';
  const type=typeof value==='string'?value:value?.type;
  return Object.entries(FILTER_OPTIONS).find(([,candidate])=>candidate===type)?.[0]||'None';
 };
 const setEntryFilter=(index,option)=>{
  const infoj=clone(layer.infoj||[]),entry={...infoj[index]},value=FILTER_OPTIONS[option];
  if(value===null)delete entry.filter;
  else if(value===true)entry.filter=true;
  else entry.filter=typeof entry.filter==='object'&&!Array.isArray(entry.filter)?{...entry.filter,type:value}:value;
  infoj[index]=entry;
  setLayer({...layer,infoj});
 };
 const compatible=(layer.infoj||[]).filter(entry=>entry.field&&!entry.skipEntry&&!entry.fieldfx&&filterOptionsFor(entry).length>1),createsPanel=filter.includeAll===true||compatible.some(entry=>entry.filter);
 const shown=filter.hidden!==true;
 return <div className="subpanel filter-panel-controls"><h3>XYZ Filtering panel and count</h3><p className="muted style-note">Filters are built from feature-information fields. Counts can appear inside this panel or directly beside the layer name.</p><div className="grid"><Check label="Show viewport count beside layer name" value={!!layer.viewport_layer_count} onChange={setHeaderCount}/><Check label="Show Filtering panel" value={shown} onChange={value=>set('hidden',value?undefined:true)}/><Check label="Offer all compatible fields" disabled={!shown} value={filter.includeAll===true} onChange={value=>set('includeAll',value||undefined)}/><Check label="Count only features in viewport" disabled={!shown} value={filter.viewport===true} onChange={value=>set('viewport',value||undefined)}/>{shown&&filter.viewport===true&&<Field label="Count label" value={filter.count_meta||''} onChange={value=>set('count_meta',value.trim()||undefined)}/>}</div>{filter.viewport_description&&<p className="muted">A viewport description is preserved in Advanced layer JSON, but pinned XYZ v4.23.4 does not visibly render it.</p>}{shown&&filter.viewport===true&&!createsPanel&&<p className="validation-hint">Choose an Interactive filter below, or enable “Offer all compatible fields”, so XYZ creates the Filtering-panel count. The layer-name count does not require this panel.</p>}{shown&&<div className="filter-field-list">{(layer.infoj||[]).map((entry,index)=>{const options=filterOptionsFor(entry),usable=!!entry.field&&!entry.skipEntry&&options.length>1;return <div className="filter-field-row" key={`${entry.field||entry.key||index}-${index}`}><span><strong>{entry.title||entry.label||entry.field||`Entry ${index+1}`}</strong><small>{entry.field||'No result field'} · {entry.type||'text'}{entry.fieldfx?' · calculated value':''}</small></span><Field label="Interactive filter" disabled={!usable} value={optionFor(entry.filter)} options={options} onChange={value=>setEntryFilter(index,value)}/>{entry.fieldfx&&<small className="validation-hint">Calculated values can appear in feature information, but XYZ filters need a real table column for SQL and min/max statistics.</small>}</div>})}</div>}{filter.default!==undefined&&<p className="muted">A fixed default filter is preserved in Advanced layer JSON and is applied in addition to interactive filters.</p>}</div>;
}
function PreviewSymbol({style,kind,label}) {
 let symbol;
 if(kind==='point'){
  const source=Array.isArray(style.icon)?style.icon[0]:style.icon||{type:'dot'},scale=(source.scale||1)*(style.scale||1)*(style.highlightScale||1);
  symbol=<IconSymbol icon={{...source,scale}}/>;
 }else if(kind==='line'){
  symbol=<svg className="xyz-vector-symbol" viewBox="0 0 100 50"><path d="M5 38 C25 5 65 45 95 12" fill="none" stroke={style.strokeColor||'#333'} strokeOpacity={style.strokeOpacity??1} strokeWidth={style.strokeWidth||1} strokeDasharray={style.lineDash?.join(' ')}/></svg>;
 }else{
  symbol=<svg className="xyz-vector-symbol" viewBox="0 0 100 55"><path d="M8 45 L22 8 62 15 92 42 48 50z" fill={style.fillColor||'none'} fillOpacity={style.fillOpacity??1} stroke={style.strokeColor||'none'} strokeOpacity={style.strokeOpacity??1} strokeWidth={style.strokeWidth||1} strokeDasharray={style.lineDash?.join(' ')}/></svg>;
 }
 return <div className="symbol-state">{symbol}<strong>{label}</strong></div>;
}
function Preview({layer,table}) { const normal=layer.style?.default||{},highlight=layer.style?.highlight&&typeof layer.style.highlight==='object'?{...normal,...layer.style.highlight}:null,kind=geometryKind(layer,table),theme=effectiveTheme(layer.style||{}).theme,categories=theme&&theme.type!=='basic'&&Array.isArray(theme.categories)?theme.categories:[],geometry=(layer.infoj||[]).find(entry=>entry.type==='geometry'&&entry.display!==false),geometryStyle=geometry?.style||normal;return <div className="layer-preview"><div className="symbol-preview"><strong className="preview-layer-name">{layer.name}</strong><div className={`symbol-states ${highlight?'has-highlight':''}`}><PreviewSymbol style={normal} kind={kind} label={categories.length?'Fallback':'Default'}/>{highlight&&<PreviewSymbol style={highlight} kind={kind} label="Highlighted"/>}</div><small className="muted">Effective XYZ symbology on a map-like background</small></div><div className="info-preview"><h3>Feature information preview</h3>{geometry&&<div className="info-geometry-preview"><PreviewSymbol style={geometryStyle} kind={kind} label={geometry.label||geometry.title||'Geometry'}/><span><strong>Selected geometry</strong><small>{geometry._dashboard?.styleFromLayerDefault?'Synchronized with fallback symbology':'Static information style'}</small></span></div>}{categories.length>0&&<div className="info-legend-preview"><h4>{theme.title||'Legend'}</h4>{categories.map((category,index)=><div className="info-legend-row" key={category.key??category.value??index}><PreviewSymbol style={{...(category.style||normal),...(category.icon?{icon:category.icon}:{})}} kind={kind} label=""/><span>{category.label??category.value??category.key??`Class ${index+1}`}</span></div>)}</div>}<dl>{(layer.infoj||[]).filter(e=>!['geometry','pin'].includes(e.type)&&e.display!==false).map((e,n)=>{const type=table?.columns.find(c=>c.name===e.field)?.type||'expression', example=/date|timestamp/.test(type)?'2026-07-16 12:00':/int|numeric|double|real/.test(type)?'123.45':/bool/.test(type)?'true':`Example ${e.field}`;return <div className="info-example" key={n}><dt>{e.title||e.label||e.field}</dt><dd>{example} · {type}</dd></div>})}</dl></div></div> }
export function Dashboard({openSecurity,openDerivedLayers,openSemantic,onLogout=()=>{},derivedChange=null}){
 const [ws,setWs]=useState(null),[rev,setRev]=useState(),[catalog,setCatalog]=useState([]),[databases,setDatabases]=useState([]),[icons,setIcons]=useState([]),[pluginCatalogue,setPluginCatalogue]=useState(null),[selected,setSelected]=useState(),[selectedCatalog,setSelectedCatalog]=useState(),[selectedLocale,setSelectedLocale]=useState(),[dirty,setDirty]=useState(false),[activity,setActivity]=useState(null),[errors,setErrors]=useState([]),[status,setStatus]=useState(null),[search,setSearch]=useState(''),[catSearch,setCatSearch]=useState(''),[derivedUpdate,setDerivedUpdate]=useState(null);
 const activityRef=useRef(null),busy=activity!==null,saving=activity==='saving';
 const beginActivity=next=>{if(activityRef.current)return false;activityRef.current=next;setActivity(next);return true};
 const endActivity=completed=>{if(activityRef.current!==completed)return;activityRef.current=null;setActivity(null)};
 const localeOptions=renderedLocales(ws),fallbackLocale=activeLocale(ws),localeKey=localeOptions.some(([key])=>key===selectedLocale)?selectedLocale:fallbackLocale.key,loc=localeOptions.find(([key])=>key===localeKey)?.[1],layers=loc?.layers||{},namedReadOnly=localeKey!=='locale';
 const load=async(force=false)=>{if(activityRef.current||dirty&&!force&&!confirm('Discard unsaved changes?')||!beginActivity('loading'))return;if(!ws)setStatus(null);try{const x=await api('/api/workspace');setWs(x.workspace);setRev(x.revision);setSelectedLocale(activeLocale(x.workspace).key);setDirty(false);setSelected();setSelectedCatalog();setErrors([]);setStatus(null)}catch(e){const next=e.details||[{path:'server',message:e.message}];setErrors(next);setStatus({kind:'error',message:'Unable to load configuration.',errors:next})}finally{endActivity('loading')}};
 const poll=async()=>{try{const [data,iconData]=await Promise.all([api('/api/catalog'),api('/api/icons')]);setCatalog(data.tables);setDatabases(data.databases);setIcons(iconData.icons)}catch{}try{const pluginData=await api('/api/plugins');setPluginCatalogue(pluginData.plugins)}catch{}};
 useEffect(()=>{load(true);poll();const id=setInterval(poll,10000);return()=>clearInterval(id)},[]);
 useEffect(()=>{const receive=event=>setDerivedUpdate({derivedLayer:event.detail,nonce:Date.now()});window.addEventListener('mapp-derived-layer-changed',receive);return()=>window.removeEventListener('mapp-derived-layer-changed',receive)},[]);
 const currentDerivedChange=derivedChange||derivedUpdate;
 useEffect(()=>{if(!currentDerivedChange?.derivedLayer)return;let active=true;(async()=>{try{const data=await api('/api/catalog');if(!active)return;setCatalog(data.tables);setDatabases(data.databases);setWs(old=>{const reconciled=reconcileDerivedWorkspace(old,currentDerivedChange.derivedLayer,data.tables);if(reconciled.summary.layers){setDirty(true);setErrors([]);setStatus({kind:'pending',message:`Updated ${reconciled.summary.layers} workspace layer(s): ${reconciled.summary.added} field(s) added and ${reconciled.summary.removed} removed. Review, then save and reload XYZ.`})}return reconciled.workspace})}catch(e){if(active)setStatus({kind:'error',message:'The derived layer changed, but the workspace catalog could not be refreshed.',errors:[{path:'catalog',message:e.message}]})}})();return()=>{active=false}},[currentDerivedChange]);
 useEffect(()=>{const fn=e=>{if(dirty){e.preventDefault();e.returnValue=''}};addEventListener('beforeunload',fn);return()=>removeEventListener('beforeunload',fn)},[dirty]);
 const update=fn=>{if(activityRef.current)return;setWs(old=>{const x=clone(old);fn(x);return x});setDirty(true);setErrors([]);setStatus(null)};
 const mutateWorkspace=fn=>update(fn);
 const mutate=fn=>update(x=>{let locale;if(localeKey==='locale'){x.locale??={layers:{}};locale=x.locale}else{locale=x.locales&&Object.hasOwn(x.locales,localeKey)?x.locales[localeKey]:undefined}if(!locale)throw new Error(`Unknown locale: ${localeKey}`);fn(x,locale)});
 const mutateLayers=fn=>mutate((x,locale)=>{locale.layers??={};fn(x,locale)});
 const setLayer=layer=>mutateLayers((x,l)=>l.layers[selected]=layer), table=catalog.find(t=>t.dbs===(layers[selected]?.dbs||ws?.dbs)&&`${t.schema}.${t.table}`===layers[selected]?.table), selectedCatalogTable=catalog.find(t=>`${t.dbs}.${t.schema}.${t.table}`===selectedCatalog);
 const renameLayer=displayName=>{const occupied=new Set([...(Object.keys(ws.locale?.layers||{})),...Object.values(ws.locales||{}).flatMap(locale=>Object.keys(locale.layers||{}))]);occupied.delete(selected);const nextKey=uniqueLayerKey(displayName,occupied,selected);if(nextKey===selected)return;mutateWorkspace(next=>{for(const locale of [next.locale,...Object.values(next.locales||{})]){if(!locale?.layers||!Object.hasOwn(locale.layers,selected))continue;locale.layers[nextKey]=locale.layers[selected];delete locale.layers[selected]}});setSelected(nextKey)};
 const validate=async()=>{if(!beginActivity('validating'))return;setErrors([]);setStatus({kind:'pending',message:'Validating workspace…'});try{const x=await api('/api/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:ws})});setErrors([]);setStatus({kind:'success',message:x.message||'Configuration is valid.'})}catch(e){const next=e.details||[{path:'server',message:e.message}];setErrors(next);setStatus({kind:'error',message:'Configuration is not valid.',errors:next})}finally{endActivity('validating')}};
 const save=async()=>{if(!beginActivity('saving'))return;setErrors([]);setStatus(workspaceSaveStatus('restarting'));try{const x=await api('/api/workspace',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:ws,revision:rev})});if(!confirmedWorkspaceReload(x))throw new ApiError('Workspace save returned without fingerprint-matched XYZ readiness.',{status:502,payload:x});setWs(x.workspace);setRev(x.revision);setDirty(false);setErrors([]);setStatus(workspaceSaveStatus('ready'))}catch(e){const saved=savedWorkspaceFromError(e);if(saved){setWs(saved.workspace);setRev(saved.revision);setDirty(saved.dirty)}const next=e.details||[{path:'server',message:e.message}];setErrors(next);setStatus(workspaceSaveStatus(workspaceSaveFailurePhase(e),next))}finally{endActivity('saving')}};
 const addTable=t=>{if(namedReadOnly)return;const guessed=generatedLayer(t),geom=guessed.geom,id=guessed.id;if(!geom||!id)return;const kind=geometryKind({geom:geom.name},t),normal=kind==='point'?{icon:{type:'dot',fillColor:'#176b4d',scale:1}}:kind==='line'?{strokeColor:'#176b4d',strokeOpacity:1,strokeWidth:2}:{fillColor:'#176b4d',fillOpacity:.3,strokeColor:'#0f5138',strokeOpacity:1,strokeWidth:2},name=title(t.table),key=uniqueLayerKey(name,new Set(Object.keys(layers)));mutateLayers((x,l)=>l.layers[key]={name,display:true,format:'mvt',dbs:t.dbs,table:`${t.schema}.${t.table}`,geom:geom.name,srid:String(geom.srid),qID:id.name,_dashboard:{generated:{geom:true,qID:true,geometryInfo:true,pinInfo:true}},infoj:[...standardInfoEntries(geom.name),...t.columns.filter(c=>!c.geometryType).slice(0,8).map(c=>({type:/int/.test(c.type)?'integer':/numeric|double|real/.test(c.type)?'numeric':/bool/.test(c.type)?'boolean':'text',title:title(c.name),field:c.name,inline:true,display:true}))],style:{default:normal,highlight:kind==='point'?{highlightScale:1.4}:{strokeColor:'#e9b949',strokeWidth:3}}});setSelected(key)};
 if(!ws)return <div className="loading-shell" aria-live="polite" aria-atomic="true">{status?.kind==='error'?<div className="initial-load-error"><strong>{status.message}</strong>{status.errors?.length>0&&<ul>{status.errors.map((error,index)=><li key={index}>{error.path}: {error.message}</li>)}</ul>}<button disabled={busy} onClick={()=>load(true)}>Retry</button></div>:<p>Loading workspace…</p>}</div>;
 const layer=layers[selected],activityText=activity==='loading'?'Refreshing workspace…':activity==='validating'?'Validating…':saving?'Restarting XYZ…':dirty?'Unsaved changes':'Saved';
 return <>
  <header>
   <div><span className="eyebrow">GEOLYTIX XYZ</span><h1>Workspace configuration</h1></div>
   <div className="actions">
    {localeOptions.length>1&&<label><span className="field-label">Locale</span><select disabled={busy} value={localeKey} onChange={event=>{setSelectedLocale(event.target.value);setSelected()}}>{localeOptions.map(([key,value])=><option value={key} key={key}>{value?.name||key}</option>)}</select></label>}
    <span>{activityText}</span>
    <DerivedLayerHeaderMenu disabled={busy} openDerivedLayers={openDerivedLayers}/>
    <button disabled={busy} className="secondary" onClick={openSemantic}>Semantic catalog</button>
    <button disabled={busy} className="secondary" onClick={openSecurity}>Access & audit</button>
    <button disabled={busy} className="secondary" onClick={onLogout}>Logout</button>
    <button disabled={busy} className="secondary" onClick={()=>load()}>{activity==='loading'?'Refreshing…':'Reload editor'}</button>
    <button disabled={busy} className="validate-button" onClick={validate}>{activity==='validating'?'Validating…':'Validate'}</button>
    <button disabled={!dirty||busy} onClick={save}>{saving?'Restarting XYZ…':'Save & reload XYZ'}</button>
   </div>
  </header>
  <div className="status-region" aria-live="polite" aria-atomic="true">
   {status&&<div className={`status-bar ${status.kind}`}><div><strong>{status.message}</strong>{status.errors?.length>0&&<ul>{status.errors.map((error,index)=><li key={index}>{error.path}: {error.message}</li>)}</ul>}</div><button aria-label="Dismiss status" onClick={()=>setStatus(null)}>×</button></div>}
  </div>
  <main aria-busy={busy} inert={busy}>
   <aside><h2>Layers</h2><input type="search" placeholder="Filter configured layers" value={search} onChange={e=>setSearch(e.target.value)}/><LayerNavigation layers={layers} search={search} selected={selected} setSelected={setSelected}/><div className="catalog-head"><h2>Server catalog</h2><span>{catalog.length} tables · live</span></div><input type="search" placeholder="Filter database tables" value={catSearch} onChange={e=>setCatSearch(e.target.value)}/><div>{catalog.filter(t=>`${t.dbs}.${t.schema}.${t.table}`.toLowerCase().includes(catSearch.toLowerCase())).map(t=>{const key=`${t.dbs}.${t.schema}.${t.table}`;return <button aria-pressed={selectedCatalog===key} className={`catalog-item ${selectedCatalog===key?'active':''}`} key={key} onClick={()=>setSelectedCatalog(key)}><strong>{t.schema}.{t.table}</strong><small>{t.dbs} · {t.columns.filter(c=>c.geometryType).map(c=>`${c.geometryType} · EPSG:${c.srid}`).join(', ')||'No geometry'}</small></button>})}</div>{selectedCatalogTable&&<div className="catalog-selection"><small>Selected: {selectedCatalogTable.schema}.{selectedCatalogTable.table}</small><button disabled={namedReadOnly} title={namedReadOnly?'Switch to the default locale to add a layer.':undefined} onClick={()=>addTable(selectedCatalogTable)}>Add selected table as layer</button></div>}</aside>
   <section id="editor">{namedReadOnly&&<div className="status-bar"><div><strong>Effective named locale — read-only in the dashboard.</strong><p>XYZ composes this view from the default locale and its raw named override. Use config-cli or the API for focused JSON Pointer edits without flattening inheritance.</p></div></div>}<Workspace ws={ws} loc={loc} databases={databases} plugins={pluginCatalogue} mutate={mutate} mutateWorkspace={mutateWorkspace} localeReadOnly={namedReadOnly}/>{layer?(namedReadOnly?<EffectiveLayer layerKey={selected} layer={layer} table={table}/>:<Layer workspace={ws} localeKey={localeKey} layerKey={selected} layer={layer} table={table} catalog={catalog} databases={databases} icons={icons} plugins={pluginCatalogue} workspaceDb={ws.dbs} setLayer={setLayer} renameLayer={renameLayer} remove={()=>{mutateLayers((x,l)=>delete l.layers[selected]);setSelected()}}/>):<div className="panel empty">Select a layer or add one from the server catalog.</div>}</section>
  </main>
 </>
}
function DerivedLayerHeaderMenu({disabled,openDerivedLayers}){
 const [items,setItems]=useState([]),[loading,setLoading]=useState(false),[error,setError]=useState('');
 const load=async()=>{if(loading)return;setLoading(true);setError('');try{const result=await api('/api/derived-layers');setItems(result.derivedLayers||[])}catch(err){setError(err.message)}finally{setLoading(false)}};
 useEffect(()=>{load()},[]);
 const choose=value=>{if(!value)return;const [action,...parts]=value.split(':');openDerivedLayers({action,name:parts.join(':')||null})};
 return <label className="header-derived-menu" title={error||undefined}><span className="field-label">Derived layers</span><select disabled={disabled||loading} value="" onChange={event=>choose(event.target.value)} aria-label="Create or edit a derived layer"><option value="" disabled>{loading?'Loading…':error?'Unable to load — open manager':'Choose action…'}</option><option value="create:">Create derived layer</option>{items.map(item=><option key={item.name} value={`edit:${item.name}`}>{item.name}</option>)}</select></label>
}
function LayerNavigation({layers,search,selected,setSelected}){const matches=Object.entries(layers).filter(([key,layer])=>`${key} ${layer.name||''} ${layer.group||''}`.toLowerCase().includes(search.toLowerCase())),ungrouped=matches.filter(([,layer])=>!layer.group),groups=new Map();matches.filter(([,layer])=>layer.group).forEach(entry=>{const group=entry[1].group;if(!groups.has(group))groups.set(group,[]);groups.get(group).push(entry)});const buttons=entries=>entries.map(([key,layer])=><button className={key===selected?'active':''} key={key} title={layer.name&&layer.name!==key?`Layer key: ${key}`:undefined} onClick={()=>setSelected(key)}>{layer.name||key}</button>);return <nav>{buttons(ungrouped)}{[...groups].map(([group,entries])=><LayerFolder group={group} entries={entries} selected={selected} search={search} setSelected={setSelected} key={group}/>)}</nav>}
function LayerFolder({group,entries,selected,search,setSelected}){const containsSelected=entries.some(([key])=>key===selected),[open,setOpen]=useState(true);useEffect(()=>{if(search||containsSelected)setOpen(true)},[search,containsSelected]);return <details className="layer-folder" open={open} onToggle={event=>setOpen(event.currentTarget.open)}><summary><span>{group}</span><small>{entries.length}</small></summary><div>{entries.map(([key,layer])=><button className={key===selected?'active':''} key={key} title={layer.name&&layer.name!==key?`Layer key: ${key}`:undefined} onClick={()=>setSelected(key)}>{layer.name||key}</button>)}</div></details>}
function Workspace({ws,loc={},databases,plugins,mutate,mutateWorkspace,localeReadOnly=false}){const w=(key,v)=>mutateWorkspace(x=>x[key]=v),l=(key,v)=>mutate((x,l)=>l[key]=v),nested=(group,key,v)=>mutate((x,l)=>{l[group]??={};l[group][key]=v}),core=new Set(['name','extent','view','ScaleLine','layers']),extras=Object.fromEntries(Object.entries(loc).filter(([key])=>!core.has(key))),setExtras=value=>mutate((x,locale)=>{for(const key of Object.keys(locale))if(!core.has(key))delete locale[key];Object.assign(locale,value)});return <div className="panel"><div className="form-head"><div><h2>Workspace</h2><p>Map defaults shared by configured layers</p></div></div><div className="grid"><Field label="Key" value={ws.key} onChange={v=>w('key',v)}/><Field label="Database" value={ws.dbs} options={databases} onChange={v=>w('dbs',v)}/><Field disabled={localeReadOnly} label="Locale name" value={loc.name||''} onChange={v=>l('name',v)}/><Field disabled={localeReadOnly} label="Scale units" value={loc.ScaleLine||''} options={['metric','imperial']} onChange={v=>l('ScaleLine',v)}/>{[['North','extent','north',-90,90],['East','extent','east',-180,180],['South','extent','south',-90,90],['West','extent','west',-180,180],['Latitude','view','lat',-90,90],['Longitude','view','lng',-180,180],['Zoom','view','z',0,30]].map(([a,b,c,min,max])=><Field disabled={localeReadOnly} key={`${b}.${c}`} label={a} type="number" min={min} max={max} step="any" value={loc?.[b]?.[c]??''} onChange={v=>nested(b,c,v)}/>)}<Check disabled={localeReadOnly} label="Mask outside extent" value={loc.extent?.mask||false} onChange={v=>nested('extent','mask',v)}/></div>{!localeReadOnly&&<PluginControls scope="locale" target={loc} catalogue={plugins} onChange={value=>mutate((x,locale)=>Object.assign(locale,value))}/>}<details className="layer-section advanced"><summary><span>Templates and advanced locale JSON</span><small>Optional XYZ configuration — empty until you add it</small></summary><div className="layer-section-body grid"><p className="muted full">Templates live at workspace level; dictionaries and locale plugins belong in Advanced locale JSON. Gazetteer setup belongs to an individual layer. <a href="/advanced-configuration.html" target="_blank" rel="noreferrer">Open the advanced-configuration guide</a>.</p><JsonObjectEditor label="Workspace templates JSON" value={ws.templates||{}} onChange={value=>w('templates',value)} help="Templates keyed by lookup name. Supports inline template text and provider-qualified src references plus XYZ query flags."/><JsonObjectEditor disabled={localeReadOnly} label="Advanced locale JSON" value={extras} onChange={setExtras} help="Locale properties outside name, extent, view, scale and layers. Consult the pinned server schema before adding a capability."/><p className="muted full">The schema accepts audited native v4.23.4 fields, bundled plugins, and compatible source-controlled external plugin manifests. Unsupported properties are rejected with their JSON path.</p></div></details></div>}
function EffectiveLayer({layerKey,layer,table}){return <div className="panel"><div className="form-head"><div><h2>{layer.name||layerKey}</h2><p>Effective XYZ layer composed from default and named locale values</p></div></div><Preview layer={layer} table={table}/><div className="subpanel advanced"><h3>Effective layer JSON</h3><p className="muted">Inspection only. Focused named-locale overrides are edited through config-cli or the configuration API.</p><pre>{JSON.stringify(layer,null,2)}</pre></div></div>}
function LayerSection({title:heading,summary,children,open=false,className=''}){return <details className={`layer-section ${className}`} open={open}><summary><span>{heading}</span><small>{summary}</small></summary><div className="layer-section-body">{children}</div></details>}
function PluginControls({scope,target,catalogue,onChange}) {
 const available=(catalogue?.external||[]).filter(plugin=>plugin.available&&plugin.scope?.includes(scope));
 if(!available.length)return null;
 const toggle=(plugin,enabled)=>{const next=clone(target),urls=new Set([plugin.entryUrl,...(plugin.aliases||[])]),sources=(next.plugins||[]).filter(url=>!urls.has(url));if(enabled){sources.push(plugin.entryUrl);next[plugin.configurationKey]??={}}else{delete next[plugin.configurationKey];delete target[plugin.configurationKey]}if(sources.length){next.plugins=sources;target.plugins=sources}else{delete next.plugins;delete target.plugins}onChange(next)};
 const configure=(plugin,key,value)=>{const configuration={...(target[plugin.configurationKey]||{})};value===''?delete configuration[key]:configuration[key]=value;onChange({...target,[plugin.configurationKey]:configuration})};
 return <LayerSection title="External plugins" summary={`${available.length} trusted deployment plugin(s)`}><div className="layer-sections">{available.map(plugin=>{const enabled=[plugin.entryUrl,...(plugin.aliases||[])].some(url=>(target.plugins||[]).includes(url)),schema=plugin.configurationSchema?.properties||{};return <div className="subpanel" key={plugin.id}><h3>{plugin.name}</h3><p>{plugin.summary}</p><Check label="Enable plugin" value={enabled} onChange={value=>toggle(plugin,value)}/><small>v{plugin.version} · XYZ {plugin.xyzVersion} · {plugin.previewAssertions?.length||0} preview checks</small>{enabled&&<div className="grid">{Object.entries(schema).map(([key,field])=>field.type==='boolean'?<Check key={key} label={key} value={target[plugin.configurationKey]?.[key]??false} onChange={value=>configure(plugin,key,value)}/>:<Field key={key} label={key} help={field.description} type={['number','integer'].includes(field.type)?'number':'text'} min={field.minimum} max={field.maximum} value={target[plugin.configurationKey]?.[key]??''} onChange={value=>configure(plugin,key,value)}/>)}</div>}</div>})}<small>Catalogue {catalogue.fingerprint?.slice(0,12)} · trusted source-controlled JavaScript</small></div></LayerSection>;
}
function Layer({workspace,localeKey,layerKey,layer,table,catalog,databases,icons,plugins,workspaceDb,setLayer,renameLayer,remove}){
 const activeDb=layer.dbs||workspaceDb,set=(k,v)=>{const next={...layer};v===''?delete next[k]:next[k]=v;setLayer(next)},available=catalog.filter(t=>t.dbs===activeDb),cols=table?.columns||[],geoms=cols.filter(c=>c.geometryType&&(layer.format!=='mvt'||c.srid===3857)),ids=cols.filter(c=>!c.geometryType),databaseLayer=['cluster','mvt','geojson','vector','wkt'].includes(layer.format)&&!layer.template&&!layer.features&&!layer.tables&&!layer.geoms,tileLayer=layer.format==='tiles',generated=layer._dashboard?.generated||{},clearAuto=(next,key)=>({...next,_dashboard:{...(next._dashboard||{}),generated:{...generated,[key]:false}}});
 const selectTable=v=>{const t=available.find(x=>`${x.schema}.${x.table}`===v),guessed=generatedLayer(t,layer.format),g=guessed.geom,id=guessed.id;setLayer({...layer,table:v,geom:g?.name||'',srid:String(g?.srid||''),qID:id?.name||'',infoj:g?[...standardInfoEntries(g.name),...(layer.infoj||[]).filter(entry=>!['geometry','pin'].includes(entry.type))]:layer.infoj,_dashboard:{...(layer._dashboard||{}),generated:{...generated,geom:true,qID:true,geometryInfo:true,pinInfo:true}}})};
 const selectDb=v=>{const t=catalog.find(x=>x.dbs===v),guessed=generatedLayer(t,layer.format),g=guessed.geom,id=guessed.id;setLayer({...layer,dbs:v,table:t?`${t.schema}.${t.table}`:'',geom:g?.name||'',srid:String(g?.srid||''),qID:id?.name||'',infoj:g?[...standardInfoEntries(g.name),...(layer.infoj||[]).filter(entry=>!['geometry','pin'].includes(entry.type))]:layer.infoj,_dashboard:{...(layer._dashboard||{}),generated:{geom:true,qID:true,geometryInfo:true,pinInfo:true}}})};
 return <div className="panel layer-editor"><div className="form-head"><div><h2>{layer.name||layerKey}</h2><p>{databaseLayer?`${activeDb} · ${layer.table}`:tileLayer?'Tile layer':'Advanced/external XYZ layer'}</p></div><button className="danger" onClick={remove}>Remove layer</button></div><div className="layer-section-list">
  <LayerSection title="Identity and display" summary="Name, folder, visibility and drawing order" open><div className="grid"><Field label="Display name" help="Human-readable name. On leaving this field, the internal key is regenerated with underscores, no special characters, and a numeric suffix when needed." value={layer.name} onChange={v=>set('name',v)} onBlur={()=>renameLayer(layer.name)}/><Field label="Layer folder" value={layer.group||''} onChange={v=>set('group',v.trim())}/><Field label="Drawing order" type="number" step="any" value={layer.zIndex??''} onChange={v=>set('zIndex',v)}/><Check label="Promote when shown" value={layer.promoteDisplay} onChange={v=>set('promoteDisplay',v)}/><Check label="Visible initially" value={layer.display} onChange={v=>set('display',v)}/></div></LayerSection>
  <LayerSection title="Data source" summary={databaseLayer?'Database relation, geometry and feature ID':tileLayer?'Tile request URL':'Preserved advanced XYZ source'} open><div className="grid"><Field label="Format" value={layer.format} options={['mvt','geojson','wkt','tiles']} onChange={v=>set('format',v)}/>{tileLayer&&<Field label="Tile URI" value={layer.URI} onChange={v=>set('URI',v)}/>} {databaseLayer&&<><Field label="Database" value={activeDb} options={databases} onChange={selectDb}/><Field label="Table" value={layer.table} options={available.map(t=>`${t.schema}.${t.table}`)} onChange={selectTable}/><Field label="Geometry column" generated={!!generated.geom} value={layer.geom} options={geoms.map(c=>c.name)} onChange={v=>{const g=geoms.find(c=>c.name===v);setLayer(clearAuto({...layer,geom:v,srid:String(g.srid),infoj:[...standardInfoEntries(v),...(layer.infoj||[]).filter(entry=>!['geometry','pin'].includes(entry.type))]},'geom'))}}/><Field label="SRID" value={layer.srid} readOnly onChange={()=>{}}/><Field label="ID column" generated={!!generated.qID} value={layer.qID} options={ids.map(c=>c.name)} onChange={v=>setLayer(clearAuto({...layer,qID:v},'qID'))}/></>}{!databaseLayer&&!tileLayer&&<p className="muted full">This source uses advanced XYZ configuration. Its open-ended properties remain available below in Advanced layer JSON.</p>}</div></LayerSection>
  {databaseLayer&&<><LayerSection title="Appearance and legend" summary="Map symbols, data-driven themes and information preview" open><div className="layer-sections"><Preview layer={layer} table={table}/><Symbology layer={layer} table={table} icons={icons} setLayer={setLayer}/><BasicLegendControls layer={layer} setLayer={setLayer}/></div></LayerSection>
  <LayerSection title="Interaction" summary="Hover, filtering, counts and Styling panel"><div className="layer-sections"><HoverControls layer={layer} columns={ids} setLayer={setLayer}/><StylePanelControls layer={layer} setLayer={setLayer}/></div></LayerSection>
  <LayerSection title="Feature information" summary={`${(layer.infoj||[]).length} configured entries`}><div className="layer-sections"><InfoFields workspace={workspace} localeKey={localeKey} layerKey={layerKey} layer={layer} columns={ids} setLayer={setLayer}/></div></LayerSection></>}
  <PluginControls scope="layer" target={layer} catalogue={plugins} onChange={setLayer}/>
  <LayerSection title="Advanced layer JSON" summary="Audited pinned-XYZ properties" className="advanced"><p className="muted">For schema-advertised properties without dedicated controls. Unsupported keys are rejected on save.</p><textarea aria-label="Advanced layer JSON" value={JSON.stringify(layer,null,2)} onChange={e=>{try{setLayer(JSON.parse(e.target.value))}catch{}}}/></LayerSection>
 </div></div>
}
function InfoFields({workspace,localeKey,layerKey,layer,columns,setLayer}) {
 const [expanded,setExpanded]=useState(null),[tests,setTests]=useState({}),[testing,setTesting]=useState(null);
 const replace=(n,next)=>{const info=clone(layer.infoj||[]);info[n]=next;setLayer({...layer,infoj:info});setTests(old=>({...old,[n]:null}))};
 const update=(n,k,v)=>replace(n,{...(layer.infoj||[])[n],[k]:v});
 const updateTitle=(n,value)=>{const entry=(layer.infoj||[])[n],key=Object.hasOwn(entry,'title')||!Object.hasOwn(entry,'label')?'title':'label';update(n,key,value)};
 const remove=n=>setLayer({...layer,infoj:layer.infoj.filter((_,i)=>i!==n)});
 const alias=()=>{const used=new Set((layer.infoj||[]).map(entry=>entry.field));let key='calculated_value',i=2;while(used.has(key))key=`calculated_value_${i++}`;return key};
 const testExpression=async n=>{setTesting(n);setTests(old=>({...old,[n]:null}));try{const result=await api('/api/expression-test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace,locale:localeKey,layer:layerKey,index:n})});setTests(old=>({...old,[n]:{kind:'success',...result}}))}catch(error){setTests(old=>({...old,[n]:{kind:'error',message:error.details?.[0]?.message||error.message}}))}finally{setTesting(null)}};
 return <div className="subpanel"><h3>Feature information fields</h3><p className="muted infoj-note">Calculated values expand into a SQL editor where the expression can be tested against a live read-only sample before validation or save.</p>{(layer.infoj||[]).map((e,n)=>{const fixed=['geometry','pin'].includes(e.type),expression=fixed||Object.hasOwn(e,'fieldfx'),source=expression?'Calculated value':'Database column',open=expression&&expanded===n;return <div className={`info-row ${open?'expression-open':''}`} key={n}><Field label="Title" value={e.title??e.label??''} onChange={v=>updateTitle(n,v)}/><Field label="Information type" disabled={fixed} value={e.type||'text'} options={INFO_TYPES} onChange={v=>update(n,'type',v)}/><Field label="Value source" generated={fixed} disabled={fixed} help={fixed?'XYZ requires this system calculated value.':undefined} value={source} options={['Database column','Calculated value']} onChange={value=>{if(value==='Calculated value'){replace(n,{...e,field:alias(),fieldfx:''});setExpanded(n)}else{const next={...e,field:columns[0]?.name||''};delete next.fieldfx;replace(n,next);setTests(old=>({...old,[n]:null}));setExpanded(null)}}}/>{expression?<button type="button" className="expression-toggle" aria-expanded={open} onClick={()=>setExpanded(open?null:n)}>{open?'Hide calculation':'Edit calculation'} <span>{open?'▴':'▾'}</span></button>:<Field label="Database column" value={e.field||''} options={columns.map(c=>c.name)} onChange={v=>update(n,'field',v)}/>}<Check label="Inline" value={!!e.inline} onChange={v=>update(n,'inline',v)}/><Check label="Display" value={e.display!==false} onChange={v=>update(n,'display',v)}/><button className="remove-info" aria-label={`Remove ${e.title||e.label||e.field}`} onClick={()=>remove(n)}>×</button>{open&&<div className="expression-panel"><div className="expression-meta"><Field label="Result key" generated={fixed} value={e.field||''} onChange={v=>update(n,'field',v)}/><Field label="Information type" disabled={fixed} value={e.type||'text'} options={INFO_TYPES} onChange={v=>update(n,'type',v)}/></div><label className="expression-editor"><InfoLabel label="SQL expression" generated={fixed}/><textarea rows="5" spellCheck="false" value={e.fieldfx||''} onChange={event=>update(n,'fieldfx',event.target.value)} placeholder="e.g. concat(stop_name, ' — ', stop_id)::text"/></label><div className="expression-actions"><button type="button" className="test-expression" disabled={testing===n||!e.fieldfx?.trim()} onClick={()=>testExpression(n)}>{testing===n?'Testing…':'Test expression'}</button><span className="muted">Runs one read-only sample query with a 5 second timeout.</span></div>{tests[n]&&<div className={`expression-result ${tests[n].kind}`}><strong>{tests[n].kind==='success'?'Valid expression':'Expression failed'}</strong><span>{tests[n].message}</span>{tests[n].kind==='success'&&<><code>PostgreSQL: {tests[n].postgresType}</code><pre>{tests[n].sample===null?'No non-null sample':JSON.stringify(tests[n].sample,null,2)}</pre></>}</div>}</div>}</div>})}<button className="icon" onClick={()=>setLayer({...layer,infoj:[...(layer.infoj||[]),{type:'text',title:'Field',field:columns[0]?.name||'',inline:true,display:true}]})}>+ field</button></div>
}
function Login({onLogin}){const [password,setPassword]=useState(''),[error,setError]=useState('');const submit=async e=>{e.preventDefault();setError('');try{const result=await api('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});csrfToken=result.csrfToken;sessionStorage.setItem('mapp-csrf',csrfToken);onLogin()}catch(err){setError(err.message)}};return <main className="login-shell"><form className="panel login-panel" onSubmit={submit}><span className="eyebrow">GEOLYTIX XYZ</span><h1>Workspace configuration</h1><p>Sign in to edit workspace settings or create a remote CLI token.</p><label><span>Administrator password</span><input autoFocus type="password" value={password} onChange={e=>setPassword(e.target.value)}/></label>{error&&<div className="expression-result error">{error}</div>}<button>Sign in</button></form></main>}
const DERIVED_ERROR_DEFAULTS={
 'derived_layer.query_invalid':{
  message:'The derived-layer query is not valid SQL.',
  action:'Correct the reported SQL problem, then submit the query again.',
 },
 'derived_layer.invalid_query':{
  message:'The derived-layer query is not valid SQL.',
  action:'Correct the reported SQL problem, then submit the query again.',
 },
 'derived_layer.query_not_allowed':{
  message:'The derived-layer query uses SQL that is outside the allowed read-only boundary.',
  action:'Use only schema-qualified source relations and approved PostgreSQL, PostGIS, and H3 operations.',
 },
 'derived_layer.query_too_expensive':{
  message:'The derived-layer query exceeds the compute safety limit.',
  action:'Reduce the reported intermediate work before submitting the query again.',
 },
 'derived_layer.background_failed':{
  message:'The background derived-layer operation failed before it completed.',
  action:'Review the technical details and operation status before deciding whether to retry.',
 },
 'derived_layer.operation_failed':{
  message:'The derived-layer operation ended without a confirmed result.',
  action:'Inspect the authoritative derived-layer catalog before retrying; the requested change may or may not have completed.',
 },
};
const derivedErrorPrimary=(err,payload={})=>{
 let message=payload.userMessage||DERIVED_ERROR_DEFAULTS[payload.code]?.message||payload.error||err.message||'The derived-layer operation failed.';
 const reasonSummary=(Array.isArray(payload.reasons)?payload.reasons:[]).map(reason=>typeof reason==='string'?reason:reason?.userMessage||reason?.message||reason?.detail).filter(Boolean).join('; ');
 for(const suffix of [payload.safeState,reasonSummary])if(typeof suffix==='string'&&suffix&&message.endsWith(suffix))message=message.slice(0,-suffix.length).trim();
 return message;
};
const derivedReason=reason=>{
 if(typeof reason==='string')return{message:reason};
 if(!reason||typeof reason!=='object')return null;
 const message=reason.userMessage||reason.message||reason.detail||reason.code;
 return message?{message:String(message),action:reason.suggestedAction}:null;
};
const derivedSafeState=(payload,operation)=>{
 if(payload.indeterminate===true||payload.stateUnchanged===false)return'';
 if(typeof payload.safeState==='string'&&payload.safeState.trim())return payload.safeState;
 if(payload.stateUnchanged!==true&&payload.blocked!==true)return'';
 return {
  create:'No derived layer was created by this request.',
  replace:'The existing derived layer remains active and unchanged.',
  convert:'The existing derived layer remains active in its original form.',
  refresh:'The existing materialized data remains active and unchanged.',
  drop:'Nothing was deleted.',
  delete:'Nothing was deleted.',
 }[operation]||'No database change was made.';
};
const derivedSuggestedAction=(payload,operation)=>{
 if(payload.code==='derived_layer.materialization_too_large'){
  if(operation==='refresh')return'Convert this materialized layer to an ordinary view, or reduce its output before refreshing again.';
  if(operation==='replace')return'Switch this definition to an ordinary view and save it again, or reduce its output.';
  if(operation==='convert')return'Keep this layer as an ordinary view, or reduce its output before converting it to a materialized view.';
 }
 return payload.suggestedAction||DERIVED_ERROR_DEFAULTS[payload.code]?.action||'';
};
function DerivedLayerErrorMessage({err,operation}){
 const payload=err.payload||{},effectiveOperation=operation||payload.operation;
 const reasons=(Array.isArray(payload.reasons)?payload.reasons:[]).map(derivedReason).filter(Boolean);
 const action=derivedSuggestedAction(payload,effectiveOperation),safeState=derivedSafeState(payload,effectiveOperation);
 const requestId=payload.requestId||payload.meta?.requestId,operationId=payload.operation?.id||payload.meta?.operationId;
 const technicalDetail=payload.technicalDetail||(payload.code==='derived_layer.background_failed'?(payload.message||payload.error):'');
 const probe=payload.probe||payload.queryPlanProbe||payload.materializationProbe;
 const consumers=Array.isArray(payload.consumerLabels)?payload.consumerLabels:[];
 const missingSources=Array.isArray(payload.missingSources)?payload.missingSources:[],extraSources=Array.isArray(payload.extraSources)?payload.extraSources:[];
 const hasDetails=payload.code||payload.category||requestId||operationId||probe||technicalDetail||payload.probeStage||payload.rolledBack!==undefined||payload.indeterminate===true||payload.declaredSources||payload.resolvedSources;
 return <div className="derived-error" role="alert"><strong>{derivedErrorPrimary(err,payload)}</strong>{reasons.length>0&&<div className="derived-error-section"><span>What went wrong</span><ul>{reasons.map((reason,index)=><li key={`${reason.message}-${index}`}>{reason.message}{reason.action&&<small>{reason.action}</small>}</li>)}</ul></div>}{missingSources.length>0&&<div className="derived-error-section"><span>Add to Source relations</span><p>{missingSources.join(', ')}</p></div>}{extraSources.length>0&&<div className="derived-error-section"><span>Remove from Source relations</span><p>{extraSources.join(', ')}</p></div>}{consumers.length>0&&<div className="derived-error-section"><span>Affected map layers</span><p>{consumers.join(', ')}</p></div>}{action&&<div className="derived-error-section"><span>How to fix it</span><p>{action}</p></div>}{safeState&&<p className="derived-error-state"><strong>Database state:</strong> {safeState}</p>}{hasDetails&&<details className="derived-error-details"><summary>Technical details</summary><dl>{payload.code&&<><dt>Error code</dt><dd><code>{payload.code}</code></dd></>}{payload.category&&<><dt>Category</dt><dd>{payload.category}</dd></>}{payload.indeterminate===true&&<><dt>Result status</dt><dd>Indeterminate</dd></>}{payload.probeStage&&<><dt>Probe stage</dt><dd>{payload.probeStage}</dd></>}{payload.rolledBack!==undefined&&<><dt>Rolled back</dt><dd>{payload.rolledBack?'Yes':'No'}</dd></>}{Array.isArray(payload.declaredSources)&&<><dt>Declared sources</dt><dd>{payload.declaredSources.join(', ')||'None'}</dd></>}{Array.isArray(payload.resolvedSources)&&<><dt>Resolved sources</dt><dd>{payload.resolvedSources.join(', ')||'None'}</dd></>}{requestId&&<><dt>Request ID</dt><dd><code>{requestId}</code></dd></>}{operationId&&<><dt>Operation ID</dt><dd><code>{operationId}</code></dd></>}</dl>{probe&&<><span>Probe</span><pre>{JSON.stringify(probe,null,2)}</pre></>}{technicalDetail&&<><span>Diagnostic detail</span><pre>{typeof technicalDetail==='string'?technicalDetail:JSON.stringify(technicalDetail,null,2)}</pre></>}</details>}</div>;
}
function derivedLayerError(err,operation){return <DerivedLayerErrorMessage err={err} operation={operation}/>}
function derivedSpatialScopeRequest(current={}){
 const locale=current.spatialScope?.locale;
 return {type:'workspace-map-extent',...(typeof locale==='string'&&locale?{locale}:{})};
}
function formattedDataSize(bytes){
 if(!Number.isFinite(bytes)||bytes<0)return '';
 const units=['B','KiB','MiB','GiB','TiB'];
 let value=bytes,index=0;
 while(value>=1024&&index<units.length-1){value/=1024;index+=1}
 return `${value.toFixed(value>=10||index===0?0:1)} ${units[index]}`;
}
function materializationEstimate(result){
 const probe=result?.materializationProbe;
 const estimated=formattedDataSize(probe?.estimatedBytes),actual=formattedDataSize(probe?.actualBytes);
 if(!estimated&&!actual)return '';
 const limit=formattedDataSize(probe?.maxEstimatedBytes);
 return [estimated&&`Planner-estimated materialized size: ${estimated}${limit?` (limit ${limit})`:''}.`,actual&&`Actual stored materialized size: ${actual}${limit?` (limit ${limit})`:''}.`].filter(Boolean).join(' ');
}
function queryPlanEstimate(result){
 const probe=result?.queryPlanProbe;
 if(!probe||typeof probe!=='object')return '';
 const parts=[];
 if(Number.isFinite(probe.estimatedFinalRows))parts.push(`${Math.round(probe.estimatedFinalRows).toLocaleString('en-US')} output rows`);
 if(Number.isFinite(probe.maxIntermediateRows))parts.push(`largest intermediate ${Math.round(probe.maxIntermediateRows).toLocaleString('en-US')} rows`);
 const bytes=formattedDataSize(probe.maxIntermediateBytes);
 if(bytes)parts.push(`${bytes} intermediate data`);
 if(!parts.length&&Number.isFinite(probe.estimatedTotalCost))parts.push(`cost ${Math.round(probe.estimatedTotalCost).toLocaleString('en-US')}`);
 return parts.length?`Planner-estimated query: ${parts.join(' · ')}.`:'';
}
function derivedLayerSuccess(result){
 const message=result?.userMessage
  ? `${result.userMessage}${result.suggestedAction?` ${result.suggestedAction}`:''}`
  : `derived_layers.${result.name} completed and passed its output checks.`;
 const estimates=[queryPlanEstimate(result),materializationEstimate(result)].filter(Boolean);
 return `${message}${estimates.length?` ${estimates.join(' ')}`:''}`;
}
export function derivedLayerFormDefinition(current){
 return {
  name:current.name,
  kind:current.kind,
  sources:current.sources.join(', '),
  idColumn:current.idColumn,
  geometryColumn:current.geometryColumn,
  description:current.description||'',
  query:current.query,
  spatialScope:derivedSpatialScopeRequest(current),
 };
}
export function DerivedLayers({close,initialName=null,initialAction='create',onChanged=result=>window.dispatchEvent(new CustomEvent('mapp-derived-layer-changed',{detail:result}))}){
 const blank={name:'',kind:'view',sources:'',idColumn:'',geometryColumn:'',description:'',query:'',spatialScope:{type:'workspace-map-extent'}};
 const [items,setItems]=useState([]),[capabilities,setCapabilities]=useState(null),[definition,setDefinition]=useState(blank),[editing,setEditing]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const load=async()=>{setError('');try{const [list,caps]=await Promise.all([api('/api/derived-layers'),api('/api/derived-layers/capabilities')]);setItems(list.derivedLayers||[]);setCapabilities(caps)}catch(err){setError(err.message)}};
 useEffect(()=>{load()},[]);
 const offerView=async(err,{operation='create',item}={})=>{
  const payload=err.payload||{};
  if(payload.code!=='derived_layer.materialization_too_large'||payload.recommendedKind!=='view')return;
  const question=operation==='refresh'
   ? `Load derived_layers.${item.name} as an ordinary-view draft? The existing materialized layer will not change until you explicitly save the draft.`
   : 'Switch this form to an ordinary view?';
  if(!window.confirm(`${derivedErrorPrimary(err,payload)}\n\n${question}`))return;
  if(operation!=='refresh'){
   setDefinition(current=>({...current,kind:'view'}));
   return;
  }
  try{
   const current=(await api(`/api/derived-layers/${item.name}`)).derivedLayer;
   setEditing(current.name);
   setDefinition({...derivedLayerFormDefinition(current),kind:'view'});
   setNotice(`Loaded derived_layers.${current.name} as an ordinary-view draft. Review it and select Save derived layer to convert it; the existing materialized layer is unchanged.`);
  }catch{
   setNotice(`The refresh was blocked and derived_layers.${item.name} is unchanged, but its conversion draft could not be loaded. Open Edit definition and choose view to review the conversion manually.`);
  }
 };
 const save=async event=>{event.preventDefault();setBusy(true);setError('');setNotice('Creating the database relation in the background. You can leave this request running while PostgreSQL completes it.');try{const replacing=!!editing,payload={...definition,sources:definition.sources.split(',').map(value=>value.trim()).filter(Boolean)},submitted=replacing?await api(`/api/derived-layers/${editing}/replace`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,confirmed:true,background:true})}):await api('/api/derived-layers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,background:true})}),response=await waitForOperation(submitted),result=response.derivedLayer;setNotice(derivedLayerSuccess(result));if(replacing){setDefinition(derivedLayerFormDefinition(result));setEditing(result.name);onChanged(result)}else{setDefinition(blank);setEditing(null)}await load()}catch(err){const operation=editing?'replace':'create';setNotice('');setError(derivedLayerError(err,operation));await offerView(err,{operation})}finally{setBusy(false)}};
 const edit=async item=>{setBusy(true);setError('');setNotice('');try{const current=(await api(`/api/derived-layers/${item.name}`)).derivedLayer;setEditing(item.name);setDefinition(derivedLayerFormDefinition(current))}catch(err){setError(err.message)}finally{setBusy(false)}};
 const action=async(item,name)=>{if(!window.confirm(`${title(name)} derived_layers.${item.name}?`))return;setBusy(true);setError('');setNotice('');try{const submitted=await api(`/api/derived-layers/${item.name}/${name}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:true,...(name==='refresh'?{background:true}:{})})}),response=await waitForOperation(submitted);if(name==='refresh')setNotice(derivedLayerSuccess(response.derivedLayer));if(name==='drop'){setEditing(null);setDefinition(blank)}await load()}catch(err){setError(derivedLayerError(err,name));await offerView(err,{operation:name,item})}finally{setBusy(false)}};
 const convert=async item=>{const kind=item.kind==='materialized'?'view':'materialized';if(!window.confirm(`Atomically convert derived_layers.${item.name} to ${kind}? The conversion will be blocked if another PostgreSQL object depends on it.`))return;setBusy(true);setError('');setNotice('');let current;try{current=(await api(`/api/derived-layers/${item.name}`)).derivedLayer;const payload={name:current.name,kind,query:current.query,sources:current.sources,idColumn:current.idColumn,geometryColumn:current.geometryColumn,description:current.description||'',spatialScope:derivedSpatialScopeRequest(current),confirmed:true,background:true},submitted=await api(`/api/derived-layers/${item.name}/replace`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),result=(await waitForOperation(submitted)).derivedLayer;setEditing(result.name);setDefinition(derivedLayerFormDefinition(result));setNotice(derivedLayerSuccess(result));onChanged(result);await load()}catch(err){if(current&&err.payload?.code==='derived_layer.materialization_too_large'){setDefinition({...derivedLayerFormDefinition(current),kind});setEditing(current.name)}setError(derivedLayerError(err,'convert'));await offerView(err,{operation:'convert',item})}finally{setBusy(false)}};
 const chooseAction=(item,value)=>{if(value==='edit')edit(item);else if(value==='refresh')action(item,'refresh');else if(value==='convert')convert(item);else if(value==='delete')action(item,'drop')};
 useEffect(()=>{if(!initialName)return;if(initialAction==='delete')action({name:initialName},'drop');else edit({name:initialName})},[]);
 useEffect(()=>{if(!editing)return;const actions=document.querySelector('.derived-layer-form .form-head');if(!actions)return;const button=document.createElement('button');button.type='button';button.className='danger derived-edit-delete';button.textContent='Delete derived layer';button.disabled=busy;button.addEventListener('click',()=>action({name:editing},'drop'));actions.append(button);return()=>button.remove()},[editing,busy]);
 return <div className="modal-backdrop"><section className="panel security-panel derived-layer-panel"><div className="form-head"><div><h2>Derived layers</h2><p>Managed read-only views in <code>derived_layers</code>. Add the resulting relation to the workspace separately.</p></div><button onClick={close}>Close</button></div>{capabilities&&<p className="muted">PostGIS {capabilities.extensions?.postgis||'unavailable'} · H3 {capabilities.h3Available?(capabilities.extensions?.h3||'available'):'unavailable'} · ordinary views update dynamically; materialized views refresh only on request.</p>}{error&&<div className="expression-result error">{error}</div>}{notice&&<div className="expression-result">{notice}</div>}<div className="derived-layer-list">{items.map(item=><div className="token-row" key={item.name}><span><strong>derived_layers.{item.name}</strong><small>{item.kind} · {item.sources.join(', ')}{item.refreshedAt?` · refreshed ${item.refreshedAt}`:''}</small></span><label className="derived-action-select"><span>Edit or delete</span><select disabled={busy} value="" aria-label={`Edit or delete derived_layers.${item.name}`} onChange={event=>chooseAction(item,event.target.value)}><option value="" disabled>Choose action…</option><option value="edit">Edit definition</option>{item.kind==='materialized'&&<option value="refresh">Refresh data</option>}<option value="convert">Convert to {item.kind==='materialized'?'view':'materialized'}</option><option value="delete">Delete derived layer</option></select></label></div>)}</div><form className="derived-layer-form" onSubmit={save}><div className="form-head"><h3>{editing?`Edit derived_layers.${editing}`:'Create derived relation'}</h3>{editing&&<button type="button" onClick={()=>{setEditing(null);setDefinition(blank);setError('')}}>Cancel edit</button>}</div><div className="grid"><Field label="Name" disabled={!!editing} value={definition.name} onChange={value=>setDefinition({...definition,name:value})}/><Field label="Kind" value={definition.kind} options={['view','materialized']} onChange={value=>setDefinition({...definition,kind:value})}/><Field label="ID column" value={definition.idColumn} onChange={value=>setDefinition({...definition,idColumn:value})}/><Field label="Geometry column" value={definition.geometryColumn} onChange={value=>setDefinition({...definition,geometryColumn:value})}/><label className="full"><InfoLabel label="Source relations"/><input value={definition.sources} onChange={event=>setDefinition({...definition,sources:event.target.value})} placeholder="leeds.definitive_paths, reference.h3_cells"/></label><label className="full"><span className="field-label">Description</span><input value={definition.description} onChange={event=>setDefinition({...definition,description:event.target.value})}/></label><label className="full expression-editor"><span className="field-label">One read-only SELECT</span><textarea rows="12" spellCheck="false" value={definition.query} onChange={event=>setDefinition({...definition,query:event.target.value})} placeholder="SELECT … FROM schema.table_a JOIN schema.table_b ON …"/></label></div><p className="muted">The server retains final output intersecting the workspace map area at one zoom level out (z−1). Aggregates and window calculations still use the complete query input unless your SQL intentionally limits its source rows.</p><p className="muted">Every source must be schema-qualified and declared above. Edits that would break a PostgreSQL dependency are blocked; affected dashboard/CLI consumers and second-order changes are reported.</p><button disabled={busy}>{busy?'Saving…':editing?'Save derived layer':'Create derived relation'}</button></form></section></div>
}
export const TOKEN_SCOPE_OPTIONS=[
 {id:'inspect',group:'Workspace',label:'Inspect workspace',help:'Read workspace, catalog, and derived-layer configuration.'},
 {id:'propose',group:'Workspace',label:'Propose workspace changes',help:'Check and create revision-bound workspace proposals.'},
 {id:'visual',group:'Workspace',label:'Run visual evidence',help:'Create proposal previews, screenshots, and visual tests.'},
 {id:'apply',group:'Workspace',label:'Apply workspace proposals',help:'Apply an explicitly reviewed workspace proposal.'},
 {id:'reload',group:'Workspace',label:'Reload XYZ',help:'Request and inspect an XYZ reload.'},
 {id:'derive',group:'Workspace',label:'Manage derived layers',help:'Create, replace, refresh, and drop managed derived relations.'},
 {id:'semantic:inspect',group:'Semantic',label:'Inspect semantic catalog',help:'Read profiles, generated facts, curated meaning, history, and proposal evidence.'},
 {id:'semantic:source',group:'Semantic',label:'Sync semantic sources',help:'Discover and synchronize allowlisted PostgreSQL relation metadata without reading row values.'},
 {id:'semantic:generate',group:'Semantic',label:'Generate semantic drafts',help:'Send semantic metadata to Gemini and receive review-only proposal operations.'},
 {id:'semantic:data',group:'Semantic',label:'Send generation data context',help:'Opt in to bounded sample rows or data-derived statistics when generating with Gemini.'},
 {id:'semantic:propose',group:'Semantic',label:'Propose semantic changes',help:'Check, create, and decline curated semantic proposals.'},
 {id:'semantic:apply',group:'Semantic',label:'Apply semantic proposals',help:'Apply an explicitly reviewed curated semantic proposal.'},
 {id:'semantic:admin',group:'Semantic',label:'Administer semantic delivery',help:'Inspect delivery failures and explicitly retry retained events.'},
];
export const TOKEN_ACCESS_PRESETS=[
 {id:'semantic-reader',label:'Semantic reader',scopes:['semantic:inspect'],help:'Read-only semantic catalog access.'},
 {id:'semantic-proposer',label:'Semantic proposal author',scopes:['semantic:inspect','semantic:propose'],help:'Read and propose curated meaning; cannot apply it.'},
 {id:'semantic-ai-author',label:'AI semantic author',scopes:['semantic:inspect','semantic:source','semantic:generate','semantic:data','semantic:propose'],help:'Sync allowlisted sources, optionally send bounded generation context, review Gemini drafts, and create proposals; cannot apply them.'},
 {id:'semantic-curator',label:'Semantic curator',scopes:['semantic:inspect','semantic:propose','semantic:apply'],help:'Read, propose, review, and apply curated meaning.'},
 {id:'semantic-operator',label:'Semantic delivery operator',scopes:['semantic:inspect','semantic:admin'],help:'Read profiles, diagnose delivery blockers, and retry retained events.'},
 {id:'semantic-administrator',label:'Semantic administrator',scopes:['semantic:inspect','semantic:source','semantic:generate','semantic:data','semantic:propose','semantic:apply','semantic:admin'],help:'All semantic source, catalog, generation, bounded data-context, curation, and delivery administration capabilities.'},
 {id:'full',label:'Full platform operator',scopes:['full'],help:'Every bearer-token workspace and semantic capability. Credential, device-approval, and audit administration remains dashboard-session-only.'},
];
const FULL_TOKEN_PRESET_ID='full';
const ALL_NARROW_TOKEN_SCOPES=TOKEN_SCOPE_OPTIONS.map(scope=>scope.id);
export function Security({close}){
 const initialPreset=TOKEN_ACCESS_PRESETS.find(item=>item.id===FULL_TOKEN_PRESET_ID)||TOKEN_ACCESS_PRESETS[0];
 const [tokens,setTokens]=useState([]),[devices,setDevices]=useState([]),[audit,setAudit]=useState([]),[name,setName]=useState('CLI operator'),[preset,setPreset]=useState(initialPreset.id),[scopes,setScopes]=useState(initialPreset.scopes),[expiryDays,setExpiryDays]=useState('30'),[extendedExpiryConfirmed,setExtendedExpiryConfirmed]=useState(false),[revealed,setRevealed]=useState(null),[copied,setCopied]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const load=async()=>{const [t,d,a]=await Promise.all([api('/api/admin/tokens'),api('/api/admin/device-authorizations'),api('/api/admin/audit')]);setTokens(t.tokens);setDevices(d.authorizations);setAudit(a.events)};
 useEffect(()=>{load().catch(reason=>setError(reason.message))},[]);
 const choosePreset=id=>{const selected=TOKEN_ACCESS_PRESETS.find(item=>item.id===id);setPreset(selected?id:'custom');if(selected)setScopes(selected.scopes)};
 const toggleScope=(id,enabled)=>{setPreset('custom');setScopes(current=>{const narrow=current.includes('full')?ALL_NARROW_TOKEN_SCOPES:current;return enabled?[...new Set([...narrow,id])]:narrow.filter(scope=>scope!==id)})};
 const extendedExpiry=expiryDays==='never'||Number(expiryDays)>30;
 const create=async()=>{setBusy(true);setError('');try{const tokenRequest={name,scopes};tokenRequest.expires=expiryDays==='never'?null:new Date(Date.now()+Number(expiryDays)*86400000).toISOString();if(extendedExpiry)tokenRequest.extendedExpiryConfirmed=extendedExpiryConfirmed;const result=await api('/api/admin/tokens',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(tokenRequest)});setRevealed(result.token);setCopied(false);await load()}catch(reason){setError(reason.message)}finally{setBusy(false)}};
 const copyRevealed=async()=>{await navigator.clipboard.writeText(revealed);setCopied(true);setTimeout(()=>setCopied(false),250)};
 const approve=async userCode=>{setBusy(true);setError('');try{await api('/api/admin/device-authorizations/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({userCode})});await load()}catch(reason){setError(reason.message)}finally{setBusy(false)}};
 const revoke=async id=>{setBusy(true);setError('');try{await api(`/api/admin/tokens/${id}/revoke`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await load()}catch(reason){setError(reason.message)}finally{setBusy(false)}};
 const presetHelp=TOKEN_ACCESS_PRESETS.find(item=>item.id===preset)?.help||'Custom least-privilege scope selection.';
 const visibleScopes=scopes.includes('full')?ALL_NARROW_TOKEN_SCOPES:scopes;
 const selectedScopesLabel=scopes.includes('full')?'full (all bearer-token workspace and semantic scopes)':(scopes.join(', ')||'none');
 return <div className="modal-backdrop"><section className="panel security-panel">
  <div className="form-head"><div><h2>Access and audit</h2><p>Create revocable least-privilege tokens and approve scoped CLI devices.</p></div><button onClick={close}>Close</button></div>
  {error&&<div className="expression-result error">{error}</div>}
  <h3>Pending device authorizations</h3>
  {devices.filter(device=>device.status==='pending').map(device=><div className="token-row" key={device.userCode}><span><strong>{device.userCode} · {device.deviceName}</strong><small>{device.scopes.join(', ')} · expires {device.expires}</small></span><button disabled={busy} onClick={()=>approve(device.userCode)}>Approve</button></div>)}
  <h3>Provision CLI token</h3>
  <div className="token-provision">
   <label><span>Token name</span><input aria-label="Token name" value={name} onChange={event=>setName(event.target.value)}/></label>
   <label><span>Access level</span><select aria-label="Token access level" value={preset} onChange={event=>choosePreset(event.target.value)}>{TOKEN_ACCESS_PRESETS.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}{preset==='custom'&&<option value="custom">Custom scopes</option>}</select></label>
   <label><span>Expiry</span><select aria-label="Token expiry" value={expiryDays} onChange={event=>{setExpiryDays(event.target.value);setExtendedExpiryConfirmed(false)}}><option value="1">1 day</option><option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option><option value="never">No expiry</option></select></label>
   {extendedExpiry&&<label className="token-scope"><input aria-label="Confirm extended token lifetime" type="checkbox" checked={extendedExpiryConfirmed} onChange={event=>setExtendedExpiryConfirmed(event.target.checked)}/><span><strong>Confirm extended lifetime</strong><small>I understand this token will remain valid longer than the 30-day default.</small></span></label>}
   <p className="muted">{presetHelp}</p>
   <details>
    <summary>Customize narrow scopes</summary>
    <div className="token-scope-grid">{TOKEN_SCOPE_OPTIONS.map(scope=><label className="token-scope" key={scope.id}><input type="checkbox" checked={visibleScopes.includes(scope.id)} onChange={event=>toggleScope(scope.id,event.target.checked)}/><span><strong>{scope.label}</strong><small>{scope.id} · {scope.help}</small></span></label>)}</div>
   </details>
   <p className="muted">Selected scopes: {selectedScopesLabel}</p>
   <button disabled={busy||!name.trim()||scopes.length===0||(extendedExpiry&&!extendedExpiryConfirmed)} onClick={create}>{busy?'Creating…':'Create scoped CLI token'}</button>
  </div>
  {revealed&&<div className="token-reveal"><strong>Copy now — this token is shown once.</strong><code>{revealed}</code><button className={`copy-token ${copied?'copied':''}`} onClick={copyRevealed}>Copy</button></div>}
  <h3>CLI tokens</h3>
  {tokens.map(token=><div className="token-row" key={token.id}><span><strong>{token.name}</strong><small>{token.id} · {token.scopes.join(', ')} · expires {token.expires||'never'} · last used {token.lastUsed||'never'}{token.revoked?' · revoked':''}</small></span>{!token.revoked&&<button disabled={busy} className="danger" onClick={()=>revoke(token.id)}>Revoke</button>}</div>)}
  <h3>Recent audit events</h3><pre className="audit-log">{audit.slice(-40).reverse().map(event=>`${event.time} ${event.event} ${event.actor}`).join('\n')}</pre>
 </section></div>;
}
export function Root(){const [authenticated,setAuthenticated]=useState(null),[identity,setIdentity]=useState(null),[security,setSecurity]=useState(false),[derived,setDerived]=useState(null),[semantic,setSemantic]=useState(false);const requireLogin=()=>{setAuthenticated(false);setIdentity(null);setSecurity(false);setDerived(null);setSemantic(false)};const check=()=>api('/api/auth/me').then(result=>{setIdentity(result);setAuthenticated(true)}).catch(()=>requireLogin());useEffect(()=>{window.addEventListener(AUTH_REQUIRED_EVENT,requireLogin);check();return()=>window.removeEventListener(AUTH_REQUIRED_EVENT,requireLogin)},[]);const logout=async()=>{try{await api('/api/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})}finally{clearAuthentication();requireLogin()}};if(authenticated===null)return <p className="loading">Checking access…</p>;if(!authenticated)return <Login onLogin={check}/>;return <><Dashboard onLogout={logout} openSecurity={()=>setSecurity(true)} openDerivedLayers={choice=>setDerived(choice||{})} openSemantic={()=>setSemantic(true)}/>{security&&<Security close={()=>setSecurity(false)}/>} {derived&&<DerivedLayers initialName={derived.name} initialAction={derived.action} close={()=>setDerived(null)}/>} {semantic&&<SemanticCatalog api={api} identity={identity} close={()=>setSemantic(false)}/>}</>}
const rootElement=document.getElementById('root');
if(rootElement)createRoot(rootElement).render(<Root/>);
