// Requires the synthetic Vite fixture on port 5179. No extra dependencies.
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {once} from 'node:events';

const chrome=process.argv[2];
if(!chrome)throw new Error('Usage: node perf/allocations.mjs /path/to/chrome [output-directory]');
const output=process.argv[3]||'/tmp/oma63-allocations';
await mkdir(output,{recursive:true});
const profile=await mkdtemp(join(tmpdir(),'oma63-chrome-'));
const child=spawn(chrome,['--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--remote-debugging-port=0','--remote-debugging-address=127.0.0.1',`--user-data-dir=${profile}`,'about:blank'],{stdio:['ignore','ignore','pipe']});
let socket;
try{
  const endpoint=await new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(new Error('Chrome startup timeout')),15000);
    child.once('error',reject);
    child.stderr.on('data',data=>{const match=data.toString().match(/DevTools listening on (ws:\/\/\S+)/);if(match){clearTimeout(timeout);resolve(match[1])}});
  });
  socket=new WebSocket(endpoint);
  await once(socket,'open');
  let id=0;
  const pending=new Map();
  socket.addEventListener('message',({data})=>{
    const message=JSON.parse(data);
    if(!message.id)return;
    const call=pending.get(message.id);
    if(!call)return;
    pending.delete(message.id);clearTimeout(call.timeout);
    message.error?call.reject(new Error(JSON.stringify(message.error))):call.resolve(message.result);
  });
  const send=(method,params={},sessionId)=>new Promise((resolve,reject)=>{
    const request=++id;
    const timeout=setTimeout(()=>{pending.delete(request);reject(new Error(`CDP timeout: ${method}`))},120000);
    pending.set(request,{resolve,reject,timeout});
    socket.send(JSON.stringify({id:request,method,params,...(sessionId?{sessionId}:{})}));
  });
  const results=[];
  for(const layers of [100,1000])for(const baseline of [true,false]){
    const {targetId}=await send('Target.createTarget',{url:'about:blank'});
    const {sessionId}=await send('Target.attachToTarget',{targetId,flatten:true});
    const cdp=(method,params)=>send(method,params,sessionId);
    const evaluate=async expression=>{
      const result=await cdp('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});
      if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    try{
      await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:800,deviceScaleFactor:1,mobile:false});
      await cdp('Page.navigate',{url:`http://127.0.0.1:5179/perf/perf.html?layers=${layers}${baseline?'&baseline':''}`});
      await evaluate(`(async()=>{
        const deadline=performance.now()+15000;
        while(!window.fixtureReady||!document.querySelector('input[type=search]')){if(performance.now()>deadline)throw Error('Fixture timeout');await new Promise(r=>setTimeout(r,50))}
        window.frame=()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
        window.change=(el,value)=>{if(!el)throw Error('Missing input');Object.getOwnPropertyDescriptor(el.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input',{bubbles:true}))};
        window.perform=async(action,n,offset=0)=>{for(let j=0;j<n;j++){const i=j+offset;if(action==='search')change(document.querySelector('input[type=search]'),i%2?'Layer':'Layer 0');if(action==='locale')change(document.querySelector('.locale-control select'),i%2?'locale':'named0');if(action==='edit')change(document.querySelector('input[aria-label="Layer folder"]'),'Folder '+i);await frame()}};
        await frame();
      })()`);
      await cdp('HeapProfiler.enable');
      for(const action of ['search','locale','edit']){
        await evaluate(`(async()=>{
          change(document.querySelector('input[type=search]'),'');
          change(document.querySelector('.locale-control select'),'locale');await frame();
          if('${action}'==='edit'){[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='Layer 0').click();await frame()}
          await perform('${action}',3);
        })()`);
        await cdp('HeapProfiler.collectGarbage');
        await cdp('HeapProfiler.startSampling',{samplingInterval:32768,includeObjectsCollectedByMajorGC:true,includeObjectsCollectedByMinorGC:true});
        await evaluate(`perform('${action}',15,3)`);
        const {profile:allocation}=await cdp('HeapProfiler.stopSampling');
        const filename=`${layers}-${baseline?'baseline':'current'}-${action}.heapprofile`;
        await writeFile(join(output,filename),JSON.stringify(allocation));
        let bytes=0;const frames=[];
        const visit=node=>{bytes+=node.selfSize;if(node.selfSize)frames.push({function:node.callFrame.functionName,url:node.callFrame.url,line:node.callFrame.lineNumber+1,bytes:node.selfSize});node.children.forEach(visit)};
        visit(allocation.head);
        const result={layers,baseline,action,operations:15,samplingInterval:32768,estimatedAllocatedBytes:bytes,profile:filename,topFrames:frames.sort((a,b)=>b.bytes-a.bytes).slice(0,8)};
        results.push(result);console.log(JSON.stringify({layers,baseline,action,estimatedAllocatedBytes:bytes}));
      }
      if(!await evaluate(`document.body.innerText.includes('Unsaved changes')`))throw new Error('Edits were not retained');
    }finally{await send('Target.closeTarget',{targetId})}
  }
  await writeFile(join(output,'summary.json'),JSON.stringify({method:'CDP HeapProfiler sampling, including collected objects; allocation estimates, not retained heap or total native memory',results},null,2));
}finally{
  socket?.close();
  child.kill('SIGTERM');
  if(child.exitCode===null)await once(child,'exit');
  await rm(profile,{recursive:true,force:true});
}
