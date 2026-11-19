export default async function runProbe({samples=15,warmup=3}={}){
if(!Number.isInteger(samples)||samples<1||samples>100||!Number.isInteger(warmup)||warmup<0||warmup>20)throw new Error('Invalid sample counts');
const deadline=performance.now()+15000;
while(!window.fixtureReady||!document.querySelector('input[type=search]')){
  if(performance.now()>deadline)throw new Error('Dashboard fixture did not become ready');
  await new Promise(r=>setTimeout(r,50));
}
const paint=()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
const change=(el,value)=>{Object.getOwnPropertyDescriptor(el.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype,'value').set.call(el,value);el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input',{bubbles:true}))};
const original=window.structuredClone;let calls=0;
window.structuredClone=(...args)=>{calls++;return original(...args)};
try{
const result={samples,warmup,userAgent:navigator.userAgent,viewport:{width:innerWidth,height:innerHeight}};
const measure=async(name,fn)=>{const times=[],clones=[];for(let i=0;i<samples+warmup;i++){calls=0;const t=performance.now();fn(i);await paint();if(i>=warmup){times.push(+(performance.now()-t).toFixed(1));clones.push(calls)}}result[name]={ms:times,cloneCalls:clones};};
await measure('search',i=>change(document.querySelector('input[type=search]'),i%2?'Layer':'Layer 0'));
change(document.querySelector('input[type=search]'),'');await paint();
await measure('locale',i=>change(document.querySelector('.locale-control select'),i%2?'locale':'named0'));
change(document.querySelector('.locale-control select'),'locale');await paint();
[...document.querySelectorAll('button')].find(e=>e.textContent.trim()==='Layer 0').click();await paint();
await measure('edit',i=>change(document.querySelector('input[aria-label="Layer folder"]'),'Folder '+i));
if(document.querySelector('input[aria-label="Layer folder"]').value!==`Folder ${samples+warmup-1}`)throw new Error('Field edit did not reach the controlled input');
result.unsaved=document.body.innerText.includes('Unsaved changes');
if(!result.unsaved)throw new Error('Field edit did not mark the workspace unsaved');
result.requests=window.requests;
return result;
}finally{window.structuredClone=original}
}
