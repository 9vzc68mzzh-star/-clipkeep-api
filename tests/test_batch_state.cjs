// Deterministic component tests: real batch.js, simulated DOM/network/timers.
const test=require('node:test'), assert=require('node:assert/strict'), vm=require('node:vm'), fs=require('node:fs');
const source=fs.readFileSync('dist/batch.js','utf8');
function storage(){const m=new Map();return {getItem:k=>m.get(k)??null,setItem:(k,v)=>m.set(k,String(v)),removeItem:k=>m.delete(k),entries:()=>Object.fromEntries(m)};}
function launch({session=storage(),local=storage(),base='https://backend.example',apiHandler,fetchHandler}={}){
 const elements=new Map(),timers=[],requests=[],saves=[],messages=[],fetches=[],shares=[];let uuid=0;
 class Element{
  constructor(tag='div'){this.tag=tag;this.hidden=false;this.disabled=false;this.checked=true;this.value='';this.dataset={};this.children=[];this.handlers={};this.parts=new Map();}
  set id(value){this._id=value;elements.set('#'+value,this);}get id(){return this._id;}
  set innerHTML(value){this.html=value;if(this.tag==='article')for(const selector of ['input','h3','.clip-status','progress','.clip-save-status','.clip-timing','.clip-actions','.clip-thumbnail','[data-action="save"]','[data-action="preview"]'])this.parts.set(selector,new Element(selector==='input'?'input':'div'));}
  get innerHTML(){return this.html||'';}
  querySelector(selector){return this.parts.get(selector)||this.children.find(c=>c.tag===selector)||null;}
  append(el){this.children.push(el);}replaceChildren(){this.children=[];}
  setAttribute(name,value){this[name]=value;}getAttribute(name){return this[name];}after(el){}focus(){}remove(){}load(){}pause(){}showModal(){this.open=true;}removeAttribute(name){delete this[name];}close(){this.open=false;}
  addEventListener(type,fn){(this.handlers[type]??=[]).push(fn);}removeEventListener(){}
  emit(type,extra={}){const event={target:this,preventDefault(){},...extra};return Promise.all([...(this.handlers[type]||[]).map(fn=>fn(event)),this['on'+type]?.(event)]);}
 }
 const $=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};$('#quality').value='720';
 const context=vm.createContext({$,console,URL,Blob,File,Headers,AbortController,AbortSignal,sessionStorage:session,localStorage:local,
  document:{createElement:tag=>new Element(tag),querySelector:$,querySelectorAll:()=>[],addEventListener(){}},
  navigator:{share:async data=>{shares.push(data);},canShare:()=>true,clipboard:{readText:async()=>''}},location:{origin:'https://site.example'},
  setTimeout:(fn,ms)=>{const item={fn,ms};timers.push(item);return item;},clearTimeout:item=>{if(item)item.cancelled=true;},
  crypto:{randomUUID:()=>`00000000-0000-4000-8000-${String(++uuid).padStart(12,'0')}`},apiBase:base,apiKey:'owner-key',format:'mp4',busy:false,records:[],
  checkBackend:async()=>({batch_limit:200,public_downloads:true,ordered_delivery:true}),
  api:async(path,options={},base,key)=>{requests.push({path,options,base,key});return apiHandler?apiHandler(path,options,base,key):snapshot([]);},
  saveAnchor:(url,name)=>saves.push({url,name}),message:text=>messages.push(text),toast:()=>{},persist:()=>{},parseLink:url=>({url,source:'youtube'}),
  download:()=>{},updateDetected:()=>{},renderSaveGuide:()=>{},DEVICE_GUIDES:{},
  fetch:async(url,options)=>{fetches.push({url,options});return fetchHandler?fetchHandler(url,options):{ok:true,blob:async()=>new Blob(['clip'],{type:'video/mp4'})};}});
 vm.runInContext(source,context);
 return {context,$,elements,timers,requests,saves,fetches,shares,messages,session,rows:()=>$('#batch-list').children,
  start:async(urls=['https://youtu.be/TST00000001'])=>{$('#video-url').value=urls.join('\n');await context.download();},
  tick:async ms=>{const pending=timers.filter(t=>t.ms===ms&&!t.cancelled&&!t.ran);for(const t of pending){t.ran=true;t.fn();}for(let n=0;n<100;n++)await Promise.resolve();},
  settle:async()=>{for(let n=0;n<100;n++)await Promise.resolve();}};
}
function job(index,status='ready'){return {id:'job-'+index,index,status,stage:status==='ready'?'done':'waiting',progress:status==='ready'?100:0,url:'https://youtu.be/TST'+String(index).padStart(8,'0'),title:'Clip '+index,download_url:'/api/files/job-'+index,preview_url:'/api/preview/job-'+index,filename:`clip-${index}.mp4`,size:4};}
function snapshot(items,finished=true){return {id:'batch-1',items,finished,archive_url:'/api/archives/batch-1'};}
const post=options=>options.method==='POST';
test('deselect all also deselects clips that finish later',async()=>{
 let read=0;const app=launch({apiHandler:(path,options)=>post(options)?snapshot([job(1),job(2,'queued')],false):snapshot([job(1),job(2)])});
 await app.start();app.$('#select-all').checked=false;await app.$('#select-all').emit('change');await app.tick(1500);
 assert.equal(app.$('#zip-all').disabled,false);assert(app.$('#zip-all').textContent.includes('(2)'));assert.equal(app.rows()[1].querySelector('input').checked,false);
});
test('refresh restores queue history without refetching completed clips automatically',async()=>{
 const session=storage(),items=[job(1),job(2,'queued')];
 const first=launch({session,apiHandler:()=>snapshot(items,false)});await first.start();await first.settle();
 assert.equal(first.fetches.length,1);assert.equal(first.saves.length,0);
 const reopened=launch({session,apiHandler:()=>snapshot([job(1),job(2)])});await reopened.settle();await reopened.tick(1500);await reopened.settle();
 assert.equal(reopened.rows().length,2);assert.equal(reopened.requests.filter(r=>post(r.options)).length,0);
 assert.equal(reopened.saves.length,0);assert.equal(reopened.fetches.length,0);
 assert(!JSON.stringify(session.entries()).includes('owner-key'));
});
test('expired queue can restart rather than repeatedly checking a missing queue',async()=>{
 let missing=false;const app=launch({apiHandler:(path,options)=>{if(!post(options)&&missing)throw Object.assign(Error('expired'),{status:404});return snapshot([job(1,'queued')],false);}});
 await app.start();missing=true;await app.tick(1500);assert.equal(app.$('#resume-batch').hidden,false);
 missing=false;await app.$('#resume-batch').emit('click');await app.settle();
 assert.equal(app.requests.filter(r=>post(r.options)&&r.path==='/api/public/batches').length,2);
});
test('lost create responses keep the same idempotency key when resuming',async()=>{
 let failures=2;const app=launch({apiHandler:(path,options)=>{if(post(options)&&failures-->0)throw TypeError('network lost');return snapshot([job(1)]);}});
 await app.start();await app.$('#resume-batch').emit('click');await app.settle();
 const ids=app.requests.filter(r=>r.path==='/api/public/batches').map(r=>JSON.parse(r.options.body).request_id);
 assert.equal(new Set(ids).size,1);
});
test('refreshing a finished queue does not resend any already-requested files',async()=>{
 const session=storage(),first=launch({session,apiHandler:()=>snapshot([job(1),job(2)])});
 await first.start();await first.tick(1100);await first.tick(1100);assert.equal(first.fetches.length,2);assert.equal(first.saves.length,0);
 const reopened=launch({session,apiHandler:()=>snapshot([job(1),job(2)])});await reopened.settle();await reopened.tick(1100);
 assert.equal(reopened.saves.length,0);assert.equal(reopened.fetches.length,0);assert.equal(reopened.context.busy,false);
 assert.equal(reopened.$('#load-previous').hidden,false);
 await reopened.$('#load-previous').emit('click');await reopened.settle();assert.equal(reopened.fetches.length,2);assert.equal(reopened.saves.length,0);
});
test('clearing an in-flight retry cancels the old queue after the reply arrives',async()=>{
 let resolveRetry;const pending=new Promise(resolve=>resolveRetry=resolve);
 const failed={...job(1,'error'),error_code:'network_error'};
 const app=launch({apiHandler:(path)=>path.endsWith('/retry')?pending:snapshot([failed])});await app.start();
 const retrying=app.$('#retry-failed').emit('click');await app.settle();assert.equal(app.context.busy,true);
 await app.$('#clear-links').emit('click');resolveRetry(snapshot([job(1,'queued')],false));await retrying;
 assert.equal(app.$('#batch-results').hidden,true);assert.equal(app.context.busy,false);
 assert.equal(app.requests.filter(r=>r.path.endsWith('/cancel')).length,2);
});
test('restore ignores a bookmark for a different backend without contacting it',async()=>{
 const session=storage();session.setItem('klipnivo-batch-v1',JSON.stringify({base:'https://other.example',id:'batch-1'}));
 const app=launch({session});await app.settle();assert.equal(app.requests.length,0);assert.equal(session.getItem('klipnivo-batch-v1'),null);
});
test('14 links dispatch and acknowledge in order; a lost receipt does not resend; ZIP includes all successes',async()=>{
 const items=Array.from({length:14},(_,i)=>({...job(i+1,'queued'),attempt:1}));
 let next=0,ackFailed=false,app;const acknowledgements=[];
 const state=()=>{
   if(next===3){Object.assign(items[next],{status:'error',stage:'failed',error:'Permanent failure'});next++;}
   if(next<items.length)Object.assign(items[next],{status:'ready',stage:'sending',file_ready:true,delivery_status:'waiting',progress:99});
   return JSON.parse(JSON.stringify(snapshot(items,next===items.length)));
 };
 app=launch({apiHandler:(path,options)=>{
   if(path.endsWith('/sent')){
     const receipt=JSON.parse(options.body),number=Number(receipt.id.split('-')[1]);
     assert.equal(number,next+1);assert(app.fetches.at(-1).url.endsWith('job-'+number));
     if(!ackFailed){ackFailed=true;throw TypeError('receipt response lost');}
     acknowledgements.push(number);Object.assign(items[next],{stage:'done',delivery_status:'sent',progress:100});next++;
     return {ok:true};
   }
   return state();
 }});
 await app.start(items.map(j=>j.url));
 assert.equal(app.rows()[0].dataset.active,'true');assert.equal(app.fetches.length,1);assert.equal(app.saves.length,0);
 for(let n=0;n<35;n++){await app.tick(1100);await app.tick(1500);await app.settle();}
 assert.deepEqual(acknowledgements,[1,2,3,5,6,7,8,9,10,11,12,13,14]);
 assert.equal(app.fetches.length,13);assert.equal(app.saves.length,0);
 assert(app.$('#batch-summary').textContent.includes('สำเร็จ 13'));
 assert(app.$('#batch-summary').textContent.includes('ไม่สำเร็จ 1'));
 assert(app.$('#batch-summary').textContent.includes('04'));
 app.$('#select-all').checked=false;await app.$('#select-all').emit('change');
 assert(app.$('#zip-all').textContent.includes('(13)'));await app.$('#zip-all').emit('click');
 assert(!app.$('#zip-all').href.includes('items='));assert(app.$('#zip-all').download.endsWith('.zip'));assert.equal(app.saves.length,0);
});
test('streaming dispatches only the active clip, sends no receipt POST, and skips the old delay',async()=>{
 let phase=0;
 const one={...job(1),engine:'stream',stream_ready:true,file_ready:false,can_download:true,delivery_status:'waiting',stage:'sending',timings:{extract_s:.2}};
 const two={...job(2),engine:'stream',stream_ready:true,file_ready:false,can_download:false,prefetched:true,delivery_status:'waiting',stage:'next_ready'};
 const app=launch({apiHandler:()=>phase===0?snapshot([one,two],false):snapshot([{...one,file_ready:true,can_download:false,delivery_status:'sent',stage:'done'}, {...two,file_ready:true,prefetched:false,can_download:true,delivery_status:'waiting',stage:'sending'}],true)});
 app.context.checkBackend=async()=>({batch_limit:200,public_downloads:true,ordered_delivery:true,streaming:true});
 await app.start([one.url,two.url]);await app.settle();
 assert.equal(app.fetches.length,1);assert.equal(app.saves.length,0);assert.equal(app.rows()[1].dataset.active,'false');
 assert.equal(JSON.parse(app.requests[0].options.body).streaming,true);
 assert.equal(app.requests.filter(r=>r.path.endsWith('/sent')).length,0);
 assert.equal(app.timers.filter(t=>t.ms===1100).length,0);
 assert(app.rows()[0].querySelector('.clip-timing').textContent.includes('0.20s'));
 phase=1;await app.tick(1500);await app.settle();assert.equal(app.fetches.length,2);assert.equal(app.saves.length,0);
});

test('15 clips become cached Files in order, no automatic downloads; Save all shares once on the tap',async()=>{
 const items=Array.from({length:15},(_,i)=>job(i+1));let active=0,max=0;
 const app=launch({apiHandler:()=>snapshot(items),fetchHandler:async()=>{
  active++;max=Math.max(max,active);await Promise.resolve();active--;
  return {ok:true,blob:async()=>new Blob(['clip'],{type:'video/mp4'})};
 }});
 await app.start(items.map(j=>j.url));await app.settle();await app.settle();
 assert.equal(app.fetches.length,15);assert.equal(max,1);assert.equal(app.saves.length,0);assert.equal(app.shares.length,0);
 assert.deepEqual(app.fetches.map(f=>Number(f.url.split('-').at(-1))),Array.from({length:15},(_,i)=>i+1));
 assert(app.rows().every(row=>row.querySelector('.clip-status').textContent==='พร้อม · Ready'));
 assert(app.$('#save-location').textContent.includes('ยังไม่ได้บันทึก'));
 const fetchCount=app.fetches.length;
 // Invoke the click synchronously and inspect before a single promise tick.
 app.$('#save-selected').onclick();assert.equal(app.shares.length,1);
 assert.equal(app.fetches.length,fetchCount);assert.equal(app.shares[0].files.length,15);
 assert(app.shares[0].files.every(file=>file instanceof File&&file.type==='video/mp4'&&file.size===4));
 assert.deepEqual(Array.from(app.shares[0].files,f=>f.name),items.map(j=>j.filename));
 await app.settle();
 app.rows()[4].querySelector('[data-action="save"]').onclick();assert.equal(app.shares.length,2);
 assert.equal(app.shares[1].files.length,1);assert.equal(app.shares[1].files[0].name,'clip-5.mp4');
 assert.equal(app.saves.length,0);
});
test('Save selected shares cached selection; cancelling or unsupported sharing never downloads files',async()=>{
 const app=launch({apiHandler:()=>snapshot([job(1),job(2),job(3)])});await app.start();await app.settle();
 const box=app.rows()[1].querySelector('input');box.checked=false;await box.emit('change');
 app.context.navigator.share=data=>{app.shares.push(data);return Promise.reject(Object.assign(Error('cancel'),{name:'AbortError'}));};
 app.$('#save-selected').onclick();assert.equal(app.shares.length,1);assert.equal(app.shares[0].files.length,2);await app.settle();
 assert.equal(app.messages.length,0);assert.equal(app.$('#save-selected').disabled,false);
 app.context.navigator.canShare=()=>false;app.$('#save-selected').onclick();
 assert.equal(app.shares.length,1);assert.equal(app.saves.length,0);assert(app.messages.at(-1).includes('ZIP'));
});
test('clearing aborts the active fetch and discards late files; no downloads or shares occur',async()=>{
 let resolveFetch;const pending=new Promise(resolve=>resolveFetch=resolve);
 const app=launch({apiHandler:()=>snapshot([job(1)]),fetchHandler:()=>pending});await app.start();
 const signal=app.fetches[0].options.signal;await app.$('#clear-links').emit('click');assert.equal(signal.aborted,true);
 resolveFetch({ok:true,blob:async()=>new Blob(['clip'])});await app.settle();
 assert.equal(app.$('#batch-results').hidden,true);assert.equal(app.$('#save-selected').disabled,true);
 assert.equal(app.saves.length,0);assert.equal(app.shares.length,0);assert.equal(app.messages.length,0);
});
test('failed page fetch can be retried without a download; preview uses its local Blob',async()=>{
 let failed=true;const app=launch({apiHandler:()=>snapshot([job(1)]),fetchHandler:async()=>({ok:!failed,blob:async()=>new Blob(['clip'])})});
 await app.start();await app.settle();assert.equal(app.rows()[0].dataset.status,'error');
 failed=false;await app.rows()[0].querySelector('[data-action="save"]').emit('click');await app.settle();
 assert.equal(app.rows()[0].querySelector('.clip-status').textContent,'พร้อม · Ready');
 await app.rows()[0].querySelector('[data-action="preview"]').emit('click');
 assert(app.$('#preview-video').src.startsWith('blob:'));assert.equal(app.saves.length,0);
});

test('a stalled fetch is aborted and the next clip loads; no automatic save fallback',async()=>{
 const app=launch({apiHandler:()=>snapshot([job(1),job(2)]),fetchHandler:(url,{signal})=>url.endsWith('job-1')?new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(Object.assign(Error('timeout'),{name:'AbortError'})))):Promise.resolve({ok:true,blob:async()=>new Blob(['clip'])})});
 await app.start();await app.tick(75000);await app.settle();
 assert.equal(app.rows()[0].dataset.status,'error');assert.equal(app.rows()[1].querySelector('.clip-status').textContent,'พร้อม · Ready');
 assert.equal(app.fetches.length,2);assert.equal(app.saves.length,0);assert.equal(app.shares.length,0);
});
test('streamed response is fully read into a File before it becomes Ready or shareable',async()=>{
 let release;const gate=new Promise(resolve=>release=resolve);let read=0;
 const app=launch({apiHandler:()=>snapshot([job(1)]),fetchHandler:async()=>({ok:true,headers:new Headers({'Content-Length':'4'}),body:{getReader:()=>({read:async()=>{
  if(read++===0)return {done:false,value:new Uint8Array([1,2])};
  if(read===2){await gate;return {done:false,value:new Uint8Array([3,4])};}
  return {done:true};
 }})}})});
 await app.start();await app.settle();assert.equal(app.$('#save-selected').disabled,true);assert.equal(app.rows()[0].querySelector('progress').value,50);
 release();await app.settle();app.$('#save-selected').onclick();
 assert.equal(app.shares.length,1);assert.deepEqual(Array.from(new Uint8Array(await app.shares[0].files[0].arrayBuffer())),[1,2,3,4]);
 assert.equal(app.saves.length,0);
});

test('one browser identity survives tabs and a new batch can replace its old work',async()=>{
 const local=storage(),first=launch({local,apiHandler:()=>snapshot([job(1)])});await first.start();
 const second=launch({local,apiHandler:()=>snapshot([job(2)])});await second.start();
 const a=JSON.parse(first.requests[0].options.body),b=JSON.parse(second.requests[0].options.body);
 assert.equal(a.client_token,b.client_token);assert(a.client_token.length>=32);
 assert(!JSON.stringify(second.session.entries()).includes('owner-key'));
});
test('revision updates dispatch the next ready clip without a 1.5-second timer',async()=>{
 let resolveWatch;const waiting=new Promise(resolve=>resolveWatch=resolve);
 const active={...job(1),engine:'stream',stream_ready:true,can_download:true,file_ready:false,delivery_status:'waiting',stage:'sending'};
 const app=launch({apiHandler:(path,options)=>options.method==='POST'?{...snapshot([active],false),revision:'v1'}:waiting});
 await app.start();await app.settle();assert(app.requests.some(r=>r.path.includes('?after=v1&wait=15')));
 assert.equal(app.timers.filter(t=>t.ms===1500).length,0);
 resolveWatch({...snapshot([{...active,can_download:false,file_ready:true,delivery_status:'sent'}]),revision:'v2'});await app.settle();
 assert.equal(app.fetches.length,1);assert.equal(app.saves.length,0);
});

test('pasting the same links again after a queue-full rejection starts a fresh request',async()=>{
 let attempts=0;const app=launch({apiHandler:(path,options)=>{
  if(options.method==='POST'&&++attempts===1)throw Object.assign(Error('queue full'),{status:429});
  return snapshot([job(1)]);
 }});
 await app.start();assert.equal(attempts,1);assert.equal(app.context.busy,false);
 await app.$('#video-url').emit('input',{inputType:'insertFromPaste'});await app.tick(0);await app.settle();
 assert.equal(attempts,2);assert.equal(app.fetches.length,1);assert.equal(app.saves.length,0);
 const creates=app.requests.filter(r=>r.path==='/api/public/batches').map(r=>JSON.parse(r.options.body));
 assert.notEqual(creates[0].request_id,creates[1].request_id);assert.equal(creates[0].client_token,creates[1].client_token);
});
