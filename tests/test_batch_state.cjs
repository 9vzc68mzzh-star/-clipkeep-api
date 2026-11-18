// Deterministic component tests: real batch.js, simulated DOM/network/timers.
const test=require('node:test'), assert=require('node:assert/strict'), vm=require('node:vm'), fs=require('node:fs');
const source=fs.readFileSync('dist/batch.js','utf8');
function storage(){const m=new Map();return {getItem:k=>m.get(k)??null,setItem:(k,v)=>m.set(k,String(v)),removeItem:k=>m.delete(k),entries:()=>Object.fromEntries(m)};}
function launch({session=storage(),apiHandler,fetchHandler}={}){
 const elements=new Map(),timers=[],requests=[],saves=[],messages=[];let uuid=0;
 class Element{
  constructor(tag='div'){this.tag=tag;this.hidden=false;this.disabled=false;this.checked=true;this.value='';this.dataset={};this.children=[];this.handlers={};this.parts=new Map();}
  set id(value){this._id=value;elements.set('#'+value,this);}get id(){return this._id;}
  set innerHTML(value){this.html=value;if(this.tag==='article')for(const selector of ['input','h3','.clip-status','progress','.clip-save-status','.clip-actions','.clip-thumbnail','[data-action="save"]','[data-action="preview"]'])this.parts.set(selector,new Element(selector==='input'?'input':'div'));}
  get innerHTML(){return this.html||'';}
  querySelector(selector){return this.parts.get(selector)||this.children.find(c=>c.tag===selector)||null;}
  append(el){this.children.push(el);}replaceChildren(){this.children=[];}
  after(el){}focus(){}remove(){}load(){}pause(){}showModal(){this.open=true;}removeAttribute(){}
  addEventListener(type,fn){(this.handlers[type]??=[]).push(fn);}removeEventListener(){}
  emit(type,extra={}){const event={target:this,preventDefault(){},...extra};return Promise.all([...(this.handlers[type]||[]).map(fn=>fn(event)),this['on'+type]?.(event)]);}
 }
 const $=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};$('#quality').value='720';
 const context=vm.createContext({$,console,URL,Blob,File,AbortController,AbortSignal,sessionStorage:session,
  document:{createElement:tag=>new Element(tag),querySelector:$,querySelectorAll:()=>[],addEventListener(){}},
  navigator:{share:async()=>{},canShare:()=>true,clipboard:{readText:async()=>''}},location:{origin:'https://site.example'},
  setTimeout:(fn,ms)=>{const item={fn,ms};timers.push(item);return item;},clearTimeout:item=>{if(item)item.cancelled=true;},
  crypto:{randomUUID:()=>`request-${++uuid}`},apiBase:'https://backend.example',apiKey:'owner-key',format:'mp4',busy:false,records:[],
  checkBackend:async()=>({batch_limit:200,public_downloads:true,ordered_delivery:true}),
  api:async(path,options={},base,key)=>{requests.push({path,options,base,key});return apiHandler?apiHandler(path,options,base,key):snapshot([]);},
  saveAnchor:(url,name)=>saves.push({url,name}),message:text=>messages.push(text),toast:()=>{},persist:()=>{},parseLink:url=>({url,source:'youtube'}),
  download:()=>{},updateDetected:()=>{},renderSaveGuide:()=>{},DEVICE_GUIDES:{},
  fetch:fetchHandler||(async()=>({ok:true,blob:async()=>new Blob(['clip'],{type:'video/mp4'})}))});
 vm.runInContext(source,context);
 return {context,$,elements,timers,requests,saves,messages,session,rows:()=>$('#batch-list').children,
  start:async(urls=['https://youtu.be/TST00000001'])=>{$('#video-url').value=urls.join('\n');await context.download();},
  tick:async ms=>{const pending=timers.filter(t=>t.ms===ms&&!t.cancelled&&!t.ran);for(const t of pending){t.ran=true;t.fn();}for(let n=0;n<15;n++)await Promise.resolve();},
  settle:async()=>{for(let n=0;n<20;n++)await Promise.resolve();}};
}
function job(index,status='ready'){return {id:'job-'+index,index,status,stage:status==='ready'?'done':'waiting',progress:status==='ready'?100:0,url:'https://youtu.be/TST'+String(index).padStart(8,'0'),title:'Clip '+index,download_url:'/api/files/job-'+index,preview_url:'/api/preview/job-'+index,filename:`clip-${index}.mp4`,size:4};}
function snapshot(items,finished=true){return {id:'batch-1',items,finished,archive_url:'/api/archives/batch-1'};}
const post=options=>options.method==='POST';
test('deselect all also deselects clips that finish later',async()=>{
 let read=0;const app=launch({apiHandler:(path,options)=>post(options)?snapshot([job(1),job(2,'queued')],false):snapshot([job(1),job(2)])});
 await app.start();app.$('#select-all').checked=false;await app.$('#select-all').emit('change');await app.tick(1500);
 assert.equal(app.$('#zip-all').disabled,false);assert(app.$('#zip-all').textContent.includes('(2)'));assert.equal(app.rows()[1].querySelector('input').checked,false);
});
test('refresh restores the existing queue and does not resubmit or resend sent files',async()=>{
 const session=storage(),items=[job(1),job(2,'queued')];
 const first=launch({session,apiHandler:()=>snapshot(items,false)});await first.start();await first.settle();
 assert.equal(first.saves.length,1);
 const reopened=launch({session,apiHandler:()=>snapshot([job(1),job(2)])});await reopened.settle();await reopened.tick(1500);await reopened.settle();
 assert.equal(reopened.rows().length,2);assert.equal(reopened.requests.filter(r=>post(r.options)).length,0);
 assert.equal(reopened.saves.length,1);assert(reopened.saves[0].name.includes('2'));
 assert(!JSON.stringify(session.entries()).includes('owner-key'));
});
test('expired queue can restart rather than repeatedly checking a missing queue',async()=>{
 let missing=false;const app=launch({apiHandler:(path,options)=>{if(!post(options)&&missing)throw Object.assign(Error('expired'),{status:404});return snapshot([job(1,'queued')],false);}});
 await app.start();missing=true;await app.tick(1500);assert.equal(app.$('#resume-batch').hidden,false);
 missing=false;await app.$('#resume-batch').emit('click');await app.settle();
 assert.equal(app.requests.filter(r=>post(r.options)&&r.path==='/api/public/batches').length,2);
});
test('clearing during share preparation cannot write an old error or share result',async()=>{
 let rejectFetch;const pending=new Promise((_,reject)=>rejectFetch=reject);
 const app=launch({apiHandler:()=>snapshot([job(1)]),fetchHandler:()=>pending});await app.start();
 const sharing=app.$('#prepare-share').emit('click');await app.settle();await app.$('#clear-links').emit('click');
 rejectFetch(Error('old share request failed'));await sharing;
 assert.equal(app.messages.length,0);assert.equal(app.$('#share-files').hidden,true);
});
test('lost create responses keep the same idempotency key when resuming',async()=>{
 let failures=2;const app=launch({apiHandler:(path,options)=>{if(post(options)&&failures-->0)throw TypeError('network lost');return snapshot([job(1)]);}});
 await app.start();await app.$('#resume-batch').emit('click');await app.settle();
 const ids=app.requests.filter(r=>r.path==='/api/public/batches').map(r=>JSON.parse(r.options.body).request_id);
 assert.equal(new Set(ids).size,1);
});
test('refreshing a finished queue does not resend any already-requested files',async()=>{
 const session=storage(),first=launch({session,apiHandler:()=>snapshot([job(1),job(2)])});
 await first.start();await first.tick(1100);await first.tick(1100);assert.equal(first.saves.length,2);
 const reopened=launch({session,apiHandler:()=>snapshot([job(1),job(2)])});await reopened.settle();await reopened.tick(1100);
 assert.equal(reopened.saves.length,0);assert.equal(reopened.context.busy,false);
});
test('share preparation stays locked across polling and repeated clicks',async()=>{
 let resolveFetch,calls=0;const pending=new Promise(resolve=>resolveFetch=resolve);
 const app=launch({apiHandler:()=>snapshot([job(1),job(2,'queued')],false),fetchHandler:()=>{calls++;return pending;}});
 await app.start();const sharing=app.$('#prepare-share').emit('click');await app.settle();await app.tick(1500);
 assert.equal(app.$('#prepare-share').disabled,true);await app.$('#prepare-share').emit('click');assert.equal(calls,1);
 resolveFetch({ok:true,blob:async()=>new Blob(['clip'],{type:'video/mp4'})});await sharing;
 assert.equal(app.$('#share-files').hidden,false);
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
