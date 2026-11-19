/* Batch flow shares the existing connection, device guides, and collection. */
(() => {
  'use strict';
  const input = $('#video-url'), form = $('#download-form'), panel = $('#batch-results');
  const MAX_LINKS = 200;
  const STATE_KEY = 'klipnivo-batch-v1';
  const browserTokens=new Map();
  function browserToken() {
    const storageKey='klipnivo-client-v1:'+(apiBase||location.origin);
    if(browserTokens.has(storageKey))return browserTokens.get(storageKey);
    let value;
    try{value=localStorage.getItem(storageKey);}catch{}
    if(!value)try{value=sessionStorage.getItem(storageKey);}catch{}
    if(!/^[A-Za-z0-9_-]{32,100}$/.test(value||''))value=crypto.randomUUID()+crypto.randomUUID();
    browserTokens.set(storageKey,value);
    try{localStorage.setItem(storageKey,value);}catch{}
    try{sessionStorage.setItem(storageKey,value);}catch{}
    return value;
  }

  let run = null, generation = 0, timer, lastInput = '', rows = new Map();
  let sendQueue = [], sending = false, receiveAbort = null, sharePending = false;
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const lines = () => input.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  const canSave = job => !!job.file_ready || job.status === 'ready' && !job.stream_ready;
  const canDispatch = job => job.stream_ready ? job.can_download : job.status === 'ready';
  const isDone = job => job.status === 'ready' && (!job.delivery_status || job.delivery_status === 'sent');
  const receipt = job => `${job.id}:${job.attempt || 1}`;
  const loaded = job => run?.files.get(receipt(job));
  const selected = () => run ? run.items.filter(j => loaded(j) && !run.unselected.has(j.id)) : [];
  const freshRun = () => ({base:apiBase,key:apiKey,format,items:[],sent:new Set(),dispatched:new Set(),queued:new Set(),unselected:new Set(),seenReady:new Set(),selectNew:true,finished:false,files:new Map(),objectURLs:new Map(),fetchErrors:new Map(),history:new Set(),loading:null});
  function forgetState() { try { sessionStorage.removeItem(STATE_KEY); } catch {} }
  function saveState() {
    if (!run?.selection || !run.path || !run.id && !run.pendingCreate) return;
    // Keep the queue bookmark in this tab. Never duplicate the connection key here.
    const {id,base,path,selection,pendingCreate,selectNew,remembered} = run;
    try { sessionStorage.setItem(STATE_KEY,JSON.stringify({id,base,path,selection,pendingCreate,selectNew,remembered,
      sent:[...run.sent],dispatched:[...run.dispatched],unselected:[...run.unselected],seenReady:[...run.seenReady]})); } catch {}
  }
  const fileURL = path => {
    const url = new URL(path, run.base || location.origin);
    if (url.origin !== new URL(run.base || location.origin).origin || !/^\/api\/(files|preview|archives)\//.test(url.pathname)) throw Error('ลิงก์ไฟล์ไม่ถูกต้อง');
    return url.href;
  };
  function controls(active) {
    busy = active;
    input.disabled = active;
    $('#paste-btn').disabled = active;
    $('#quality').disabled = active || format === 'mp3';
    $('#demo-btn').disabled = active;
    document.querySelectorAll('.format,.tab').forEach(b => b.disabled = active);
  }
  function releaseFiles() {
    receiveAbort?.abort(); receiveAbort=null;
    if (!run) return;
    for (const url of run.objectURLs.values()) URL.revokeObjectURL(url);
    run.objectURLs.clear(); run.files.clear();
    if ($('#clip-preview').open) $('#clip-preview').close();
  }
  function saveNotice() {
    if (!run || sharePending) return;
    const count=run.items.filter(loaded).length;
    if(run.history.size){$('#save-location').textContent='รายการรอบก่อนจะไม่โหลดซ้ำอัตโนมัติ · แตะโหลดเมื่อคุณต้องการ หรือวางลิงก์เพื่อเริ่มชุดใหม่';return;}
    $('#save-location').textContent = count
      ? `พร้อมบันทึก ${count} คลิป · แตะ “บันทึกทั้งหมด” หรือ “บันทึก” แล้วเลือก “บันทึกวิดีโอ” (รูปภาพ) หรือ “บันทึกไปยังไฟล์” ในเมนูแชร์ ตามที่อุปกรณ์รองรับ · ยังไม่ได้บันทึกลงเครื่อง`
      : 'กำลังเตรียมไฟล์ในหน้านี้ จะไม่เปิดดาวน์โหลดอัตโนมัติ · เมื่อพร้อม แตะบันทึกเพื่อเลือกแอปรูปภาพหรือ Files';
    if (typeof navigator.share !== 'function' || typeof navigator.canShare !== 'function')
      $('#save-location').textContent += ' · เบราว์เซอร์นี้ไม่รองรับแชร์ไฟล์ ใช้ปุ่ม ZIP หลังคิวจบ';
  }
  async function receiveFile(job) {
    if (!run || !(canSave(job) || canDispatch(job))) return;
    const current=run,token=generation,key=receipt(job);
    if (!loaded(job)) {
      const controller=new AbortController();receiveAbort=controller;
      let timeout,lastPaint=0;
      const touch=()=>{clearTimeout(timeout);timeout=setTimeout(()=>controller.abort(),75000);};
      current.loading={id:job.id,bytes:0,total:job.size||job.bytes_total||0};current.fetchErrors.delete(key);row(job);touch();
      try {
        // Fetch never navigates to an attachment and cannot open Safari's download prompt.
        const response=await fetch(fileURL(job.download_url),{signal:controller.signal,cache:'no-store'});
        if (!response.ok) throw Error('รับไฟล์ไม่สำเร็จ กรุณาลองโหลดอีกครั้ง');
        let blob;
        if (response.body?.getReader) {
          const reader=response.body.getReader(),chunks=[];
          const expected=Number(response.headers.get('Content-Length'))||0;
          current.loading.total=expected||current.loading.total;
          while (true) {
            const {done,value}=await reader.read();
            if (token!==generation) { await reader.cancel();return; }
            touch();if(done)break;
            chunks.push(value);current.loading.bytes+=value.byteLength;
            if(Date.now()-lastPaint>=100){row(job);lastPaint=Date.now();}
          }
          if (expected && current.loading.bytes!==expected) throw Error('รับไฟล์ไม่ครบ กรุณาลองโหลดอีกครั้ง');
          blob=new Blob(chunks);
        } else blob=await response.blob();
        if(token!==generation)return;
        if(!blob.size)throw Error('ไฟล์ว่าง กรุณาลองโหลดอีกครั้ง');
        const type=job.filename.endsWith('.mp3')?'audio/mpeg':'video/mp4';
        current.files.set(key,new File([blob],job.filename,{type}));
        current.sent.add(job.id);current.dispatched.add(key);saveState();
      } catch(error) {
        if(token===generation)current.fetchErrors.set(key,error.name==='AbortError'?'รับไฟล์ไม่มีความคืบหน้า กรุณาลองโหลดอีกครั้ง':error.message);
        return;
      } finally {
        clearTimeout(timeout);
        if(receiveAbort===controller)receiveAbort=null;
        if(token===generation){current.loading=null;row(job);updateActions();saveNotice();}
      }
    }
    if (token!==generation)return;
    // Legacy servers need a receipt only after the complete Blob arrived.
    if (!job.stream_ready && job.delivery_status === 'waiting') {
      try {
        await api(`${current.path}/${current.id}/sent`,{method:'POST',body:JSON.stringify({id:job.id,attempt:job.attempt})},current.base,current.key);
      } catch { if(token===generation)current.queued.delete(key); }
    }
  }
  async function drainDownloads() {
    if (sending) return;
    sending = true;
    try {
      while (sendQueue.length) {
        const entry = sendQueue.shift();
        if (run && entry.generation === generation) await receiveFile(entry.job);
      }
    } finally {
      sending = false;
      if(run){controls(!run.finished);updateActions();saveNotice();}
    }
  }
  function shareJobs(jobs) {
    if(sharePending||!jobs.length)return;
    const files=jobs.map(loaded);
    if(files.some(file=>!file))return;
    const token=generation;
    try {
      if(typeof navigator.share!=='function'||typeof navigator.canShare!=='function'||!navigator.canShare({files})) {
        message('อุปกรณ์นี้แชร์ไฟล์ชุดนี้ไม่ได้ กรุณาเลือกคลิปน้อยลง หรือใช้ ZIP หลังคิวจบ');return;
      }
      // No await/fetch before this call: retain the user's iOS tap activation.
      const sharing=navigator.share({files});
      sharePending=true;updateActions();
      Promise.resolve(sharing).then(()=>{
        if(token===generation)$('#save-location').textContent=`ส่ง ${files.length} คลิปให้เมนูแชร์แล้ว · หากเลือก “บันทึกวิดีโอ” ให้ดูในรูปภาพ หรือ “บันทึกไปยังไฟล์” ให้ดูโฟลเดอร์ที่คุณเลือก`;
      },error=>{
        if(token===generation&&error.name!=='AbortError')message('เปิดเมนูแชร์ไม่ได้ กรุณาเลือกน้อยลงหรือใช้ ZIP');
      }).finally(()=>{sharePending=false;if(run)updateActions();});
    } catch(error) {if(token===generation&&error.name!=='AbortError')message('เปิดเมนูแชร์ไม่ได้ กรุณาใช้ ZIP');}
  }
  function row(job) {
    let el = rows.get(job.id);
    if (!el) {
      el = document.createElement('article'); el.className = 'clip-row'; el.dataset.id = job.id;
      el.innerHTML = '<label class="clip-select"><input type="checkbox" checked aria-label="เลือกคลิป"></label><div class="clip-thumbnail"><span>▶</span></div><div class="clip-details"><h3></h3><p class="clip-status"></p><progress max="100" value="0" aria-label="ความคืบหน้าคลิป"></progress><p class="clip-save-status"></p><p class="clip-timing"></p><div class="clip-actions"><button type="button" data-action="preview">ดู / ฟัง</button><button type="button" data-action="save">บันทึกไฟล์</button></div></div>';
      rows.set(job.id, el); $('#batch-list').append(el);
      el.querySelector('input').addEventListener('change', e => {
        e.target.checked ? run.unselected.delete(job.id) : run.unselected.add(job.id);
        saveState(); updateActions();
      });
      el.querySelector('[data-action="save"]').onclick = () => {
        const item=run.items.find(j=>j.id===job.id);
        if(loaded(item))shareJobs([item]);
        else if((run.fetchErrors.has(receipt(item))||run.history.has(item.id))&&(!run.history.has(item.id)||run.finished)){run.history.delete(item.id);run.fetchErrors.delete(receipt(item));sendQueue.push({generation,job:item});drainDownloads();}
      };
      el.querySelector('[data-action="preview"]').onclick = () => preview(run.items.find(j => j.id === job.id));
    }
    const signature = JSON.stringify([job.status,job.stage,job.progress,job.title,job.thumbnail,job.warning,job.error,job.attempt,job.delivery_status,job.can_download,job.prefetched,job.timings,job.bytes_sent,!!loaded(job),run.loading?.id===job.id?run.loading:null,run.history.has(job.id),run.fetchErrors.get(receipt(job)),sharePending]);
    if (el.dataset.signature === signature) return;
    el.dataset.signature = signature;
    el.dataset.status = run.fetchErrors.has(receipt(job))?'error':loaded(job)?'ready':job.status;
    el.dataset.active = run.loading?.id===job.id || !loaded(job) && !run.fetchErrors.has(receipt(job)) && !job.prefetched && (job.status === 'processing' || job.stream_ready && job.delivery_status === 'streaming' || canDispatch(job) && !isDone(job)) ? 'true' : 'false';
    el.querySelector('input').checked = canSave(job) ? !run.unselected.has(job.id) : run.selectNew;
    el.querySelector('h3').textContent = `${String(job.index).padStart(2,'0')} · ${job.title || job.url || 'กำลังอ่านข้อมูลคลิป'}`;
    const stages = {waiting:'รอคิว',resolving:'กำลังดาวน์โหลด · อ่านข้อมูล',downloading:'กำลังดาวน์โหลด',merging:'กำลังดาวน์โหลด · รวมภาพและเสียง',prefetching:'เตรียมข้อมูลคลิปถัดไป',next_ready:'รอส่งตามลำดับ · ข้อมูลพร้อมแล้ว',converting:'รวมสตรีม · ไม่แปลงภาพใหม่',sending:'กำลังรับไฟล์เข้าหน้าเว็บ',retrying:'ลองใหม่อัตโนมัติ',done:'เสร็จแล้ว',failed:'ไม่สำเร็จ',cancelled:'ยกเลิกแล้ว'};
    el.querySelector('.clip-status').textContent = loaded(job)?'พร้อม · Ready':run.history.has(job.id)?'รายการจากรอบก่อน · แตะโหลดเมื่อคุณต้องการ':run.fetchErrors.get(receipt(job)) || job.error || `${stages[job.stage] || 'รอคิว'}${el.dataset.active === 'true' ? job.stream_ready && !job.bytes_total && job.bytes_sent ? ` · ${Math.ceil(job.bytes_sent/1024)} KB` : ` · ${Math.round(job.progress || 0)}%` : ''}${job.auto_retry ? ' · ลองใหม่ 1/1' : ''}${job.warning ? ' · ' + job.warning : ''}`;
    if(run.loading?.id===job.id)el.querySelector('.clip-status').textContent=run.loading.total?`กำลังรับไฟล์เข้าหน้าเว็บ · ${Math.min(99,Math.round(run.loading.bytes/run.loading.total*100))}%`:`กำลังรับไฟล์เข้าหน้าเว็บ · ${Math.ceil(run.loading.bytes/1024)} KB`;
    const progress = el.querySelector('progress'); progress.value = run.loading?.id===job.id && run.loading.total ? Math.min(99,run.loading.bytes/run.loading.total*100) : job.progress || 0;
    progress.hidden = el.dataset.active !== 'true' && job.status !== 'queued';
    el.querySelector('input').disabled = !loaded(job);
    el.querySelector('.clip-actions').hidden = !loaded(job) && !run.fetchErrors.has(receipt(job)) && !run.history.has(job.id);
    el.querySelector('[data-action="preview"]').disabled = !loaded(job);
    el.querySelector('[data-action="save"]').disabled = sharePending;
    el.querySelector('[data-action="save"]').textContent = loaded(job)?'บันทึก · Save':run.history.has(job.id)?'โหลดเข้าหน้านี้':'โหลดอีกครั้ง';
    const labels={startup_s:'เริ่มงาน',resolve_s:'ลิงก์',extract_s:'ข้อมูล',probe_s:'ตรวจรูปแบบ',wait_turn_s:'รอส่ง',download_s:'รับข้อมูล',merge_s:'รวม/เสียง',send_s:'ส่ง',total_s:'รวมเวลา'};
    el.querySelector('.clip-timing').textContent=Object.entries(labels).filter(([key])=>job.timings?.[key]!=null).map(([key,label])=>`${label} ${Number(job.timings[key]).toFixed(2)}s`).join(' · ');
    el.querySelector('.clip-save-status').textContent=loaded(job)?'ไฟล์พร้อมในหน้านี้ · แตะบันทึกเพื่อเปิดเมนูแชร์':'';
    if (job.thumbnail && !el.querySelector('img')) {
      try {
        const url = new URL(job.thumbnail);
        if (url.protocol === 'https:') {
          const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.src = url.href;
          img.onerror = () => img.remove(); el.querySelector('.clip-thumbnail').append(img);
        }
      } catch {}
    }
  }
  function updateActions() {
    if (!run) return;
    const ready = run.items.filter(loaded), chosen = selected();
    const all = $('#select-all'); all.checked = ready.length > 0 && chosen.length === ready.length;
    all.indeterminate = chosen.length > 0 && chosen.length !== ready.length;
    all.disabled = !ready.length;
    const successful=run.items.filter(canSave),zip=$('#zip-all');
    $('#load-previous').hidden=!run.history.size;
    $('#load-previous').disabled=sending||!run.finished;
    $('#load-previous').textContent=`โหลดไฟล์รอบก่อนเข้าหน้านี้ (${run.history.size})`;
    zip.disabled = run.expired || !successful.length || !run.finished;
    zip.setAttribute('aria-disabled',String(zip.disabled));
    if(zip.disabled)zip.removeAttribute('href');else zip.href=fileURL(run.archive_url);
    zip.download=`KlipNivo-${(run.id||'batch').slice(0,8)}.zip`;
    zip.textContent = `ดาวน์โหลดทั้งหมดเป็น ZIP (${successful.length})`;
    $('#save-selected').disabled = !chosen.length || sharePending;
    $('#save-selected').textContent = `${chosen.length===ready.length?'บันทึกทั้งหมด · Save all':'บันทึกที่เลือก · Save selected'} (${chosen.length})`;
    $('#retry-failed').hidden = run.expired || !run.finished || !run.items.some(j => j.status === 'error' && j.error_code !== 'invalid_link');
    for(const job of run.items){const button=rows.get(job.id)?.querySelector('[data-action="save"]');if(button)button.disabled=sharePending||run.history.has(job.id)&&!run.finished;}
    if(run.history.size)$('#save-location').textContent='พบรายการรอบก่อน จะไม่โหลดซ้ำอัตโนมัติ · กดโหลดไฟล์รอบก่อนเมื่อคุณต้องการ หรือวางลิงก์เพื่อเริ่มชุดใหม่';
    if(run.items.length){
      const failed=run.items.filter(j=>j.status==='error'||run.fetchErrors.has(receipt(j))),cancelled=run.items.filter(j=>j.status==='cancelled');
      const done=ready.length+failed.length+cancelled.length;
      $('#batch-count').textContent=`${done} / ${run.items.length} คลิป`;
      $('#batch-progress').value=run.items.reduce((sum,j)=>sum+(loaded(j)||['error','cancelled'].includes(j.status)||run.fetchErrors.has(receipt(j))?100:Math.min(99,j.progress||0)),0)/run.items.length;
      if(run.finished&&!run.expired){
        const pending=run.items.length-done;
        $('#batch-summary').textContent=run.history.size?`รายการจากรอบก่อน ${run.history.size} คลิป · ยังไม่ได้โหลดซ้ำ · พร้อมบันทึกในหน้านี้ ${ready.length} คลิป`:pending?`กำลังรับไฟล์เข้าหน้านี้ · พร้อม ${ready.length} คลิป · รอ ${pending} คลิป`:`พร้อมบันทึก · สำเร็จ ${ready.length} คลิป · ไม่สำเร็จ ${failed.length} คลิป${failed.length?` (คลิป ${failed.map(j=>String(j.index).padStart(2,'0')).join(', ')})`:''}${cancelled.length?` · ยกเลิก ${cancelled.length} คลิป`:''}`;
        if(run.cancel_reason==='replaced'||run.cancel_reason==='inactive')$('#batch-summary').textContent=(run.cancel_reason==='replaced'?'ชุดนี้หยุดแล้ว เพราะเริ่มชุดใหม่ในเบราว์เซอร์เดียวกัน':'ชุดนี้พักแล้ว เพราะไม่ได้เปิดหน้าดาวน์โหลดเกิน 90 วินาที')+` · ไฟล์ที่พร้อมในหน้านี้ ${ready.length} คลิป ยังบันทึกได้ · วางลิงก์เพื่อเริ่มชุดใหม่`;
      }
    }

  }
  function rememberBatch() {
    if (!run || run.remembered) return;
    run.remembered = true;
    for (const job of run.items.filter(j => j.status === 'ready').reverse()) {
      const parsed = parseLink(job.url);
      if (!parsed) continue;
      records = records.filter(r => r.url !== parsed.url);
      records.unshift({id:crypto.randomUUID(),date:Date.now(),title:job.title||'คลิปของคุณ',source:parsed.source,url:parsed.url,format:run.format,quality:(job.quality_label||job.height) ? (job.quality_label||job.height)+'p' : 'ต้นฉบับ'});
    }
    records = records.slice(0,30); persist();
    saveState();
  }
  function render(snapshot) {
    if (!run) return;
    run.items = snapshot.items; run.finished = snapshot.finished; run.archive_url = snapshot.archive_url;run.streaming=run.items.some(job=>job.engine==='stream');run.revision=snapshot.revision;run.cancel_reason=snapshot.cancel_reason;
    for (const job of run.items) {
      if (canSave(job) && !run.seenReady.has(job.id)) {
        if (!run.selectNew) run.unselected.add(job.id);
        run.seenReady.add(job.id);
      }
      row(job);
      if (!run.history.has(job.id) && (canDispatch(job) || run.finished && canSave(job)) && (!loaded(job) || !job.stream_ready && job.delivery_status==='waiting') && !run.fetchErrors.has(receipt(job)) && !run.queued.has(receipt(job))) {
        run.queued.add(receipt(job)); sendQueue.push({generation,job});
      }
    }
    const done = run.items.filter(j => isDone(j) || ['error','cancelled'].includes(j.status)).length;
    const percent = run.items.reduce((sum,j) => sum + (['error','cancelled'].includes(j.status) ? 100 : j.progress||0),0) / run.items.length;
    $('#batch-progress').value = percent;
    $('#batch-count').textContent = `${done} / ${run.items.length} คลิป`;
    $('#batch-title').textContent = run.finished ? 'ผลลัพธ์และบันทึกไฟล์' : 'คิวดาวน์โหลด';
    const ready = run.items.filter(isDone).length, failed = run.items.filter(j => j.status === 'error');
    $('#batch-summary').textContent = run.finished
      ? `เตรียมครบแล้ว · สำเร็จ ${ready} คลิป · ไม่สำเร็จ ${failed.length} คลิป${failed.length ? ` (คลิป ${failed.map(j=>String(j.index).padStart(2,'0')).join(', ')})` : ''}`
      : snapshot.queue_position>0?`มีชุดอื่นกำลังทำงาน · ชุดนี้รออีก ${snapshot.queue_position} ชุด`:'รับไฟล์ทีละคลิป ตามลำดับ · เตรียมล่วงหน้าเพียง 1 คลิป · พร้อมแล้วแตะบันทึกเพื่อเปิดเมนูแชร์';
    controls(!run.finished || sendQueue.length > 0); updateActions(); saveNotice(); drainDownloads();
    if (run.finished) rememberBatch();
    saveState();
  }
  async function request(path,options={}) { return api(path,options,run.base,run.key); }
  function queueError(error,token) {
    if (!run || token !== generation) return;
    controls(false);
    if (error.status === 404 || error.status === 410) {
      run.id=null; run.finished=true; run.expired=true; run.pendingCreate=false; sendQueue=[]; forgetState();
      for (const job of run.items) {
        if (job.status !== 'error') Object.assign(job,{status:'error',stage:'failed',error:'ไฟล์หมดอายุหรือเซิร์ฟเวอร์เริ่มใหม่แล้ว'});
        row(job);
      }
      updateActions(); $('#retry-failed').hidden=true;
      $('#batch-summary').textContent='คิวเดิมหมดอายุหรือเซิร์ฟเวอร์เริ่มใหม่แล้ว ไฟล์ที่บันทึกลงเครื่องยังอยู่';
      message('ใช้ “เริ่มชุดใหม่” เพื่อเตรียมลิงก์เดิมอีกครั้ง');
      $('#resume-batch').textContent='เริ่มชุดใหม่';
    } else {
      message('ติดตามคิวไม่สำเร็จ: '+error.message+' ใช้ “เช็กคิวอีกครั้ง” เพื่อติดตามงานเดิม');
      $('#resume-batch').textContent='เช็กคิวอีกครั้ง';
      if (error.status === 401) requestConnection('API Key ไม่ถูกต้อง กรุณาตรวจสอบการเชื่อมต่อ');
    }
    $('#resume-batch').hidden=false;
  }
  async function poll(token) {
    if(!run||run.polling)return;
    const current=run;current.polling=true;
    try{
    let errors = 0;
    while (run && token === generation && !run.finished) {
      if(!run.revision)await pause(1500);
      if (!run || token !== generation) return;
      try {
        const watch=run.revision?`?after=${encodeURIComponent(run.revision)}&wait=15`:'';
        const snapshot = await request(`${run.path}/${encodeURIComponent(run.id)}${watch}`);
        if (token !== generation) return;
        errors = 0; render(snapshot);
      } catch (e) {
        if (token !== generation) return;
        if (e.status === 404 || e.status === 401 || ++errors >= 5) {
          queueError(e,token);
          return;
        }
        $('#batch-summary').textContent = 'การเชื่อมต่อสะดุด กำลังเช็กคิวเดิมอีกครั้ง…';
        await pause(2500);
      }
    }
    }finally{current.polling=false;}
  }
  async function submitBatch(current,token) {
    const create = () => api(current.path,{method:'POST',body:JSON.stringify(current.selection)},current.base,current.key);
    current.pendingCreate=true; saveState();
    try {
      let snapshot;
      try { snapshot=await create(); }
      catch (error) { if (error.status) throw error; snapshot=await create(); }
      if (token !== generation) {
        api(`${current.path}/${snapshot.id}/cancel`,{method:'POST'},current.base,current.key).catch(()=>{});
        return;
      }
      current.id=snapshot.id; current.pendingCreate=false;
      $('#form-message').hidden=true; render(snapshot); poll(token);
    } catch (error) {
      if (token !== generation) return;
      current.pendingCreate=!error.status; current.finished=!current.pendingCreate; controls(false);
      $('#batch-summary').textContent=current.pendingCreate?'ยังยืนยันสถานะคำขอไม่ได้':'เริ่มชุดดาวน์โหลดไม่สำเร็จ';
      message(error.message || 'ติดต่อเซิร์ฟเวอร์ไม่สำเร็จ');
      if (error.status === 401) requestConnection('API Key ไม่ถูกต้อง กรุณาตรวจสอบการเชื่อมต่อ');
      current.pendingCreate ? saveState() : forgetState();
      $('#resume-batch').textContent=current.pendingCreate?'เช็กคำขอเดิมอีกครั้ง':'ลองเริ่มใหม่';
      $('#resume-batch').hidden=false;
    }
  }
  async function start(event) {
    event?.preventDefault();
    clearTimeout(timer);
    if (busy || !lines().length) return;
    if (run?.pendingCreate) { message('ยังไม่ทราบสถานะคำขอเดิม กรุณากดเช็กคำขอเดิมอีกครั้งก่อนเริ่มชุดใหม่'); return; }
    const urls = lines();
    if (urls.length > MAX_LINKS) { message(`มี ${urls.length} ลิงก์ รองรับสูงสุด ${MAX_LINKS} ต่อชุด กรุณาแบ่งชุด ไม่มีลิงก์ถูกตัดทิ้ง`); return; }
    lastInput = input.value;
    const replaces=run?.base===apiBase?run.id:'';
    const token = ++generation;
    releaseFiles();run = freshRun(); forgetState();
    const current = run;
    rows.clear(); $('#batch-list').replaceChildren(); sendQueue=[];
    $('#result').hidden = true; panel.hidden = false; controls(true);
    $('#batch-title').textContent='กำลังเริ่มชุดดาวน์โหลด'; $('#batch-count').textContent=`${urls.length} คลิป`;
    $('#batch-progress').value=0; $('#batch-summary').textContent='กำลังปลุกเซิร์ฟเวอร์ อาจใช้เวลาประมาณหนึ่งนาที…';
    $('#form-message').hidden=true; $('#resume-batch').hidden=true; updateActions(); saveNotice();
    try {
      const backend = await checkBackend();
      if (token !== generation) return;
      if (!backend.ordered_delivery) throw Error('เซิร์ฟเวอร์กำลังอัปเดตคิวทีละคลิป โปรดรอสักครู่แล้วลองใหม่');
      if (!backend.public_downloads && !run.key) { requestConnection('เซิร์ฟเวอร์นี้ต้องใช้ API Key'); controls(false); return; }
      run.path = backend.public_downloads ? '/api/public/batches' : '/api/batches';
      current.selection = {streaming:!!backend.streaming,urls,format:current.format,quality:$('#quality').value,request_id:crypto.randomUUID(),client_token:browserToken(),replace_id:replaces||''};
      await submitBatch(current,token);
    } catch (e) {
      if (token !== generation) return;
      controls(false); run.finished=true; $('#batch-summary').textContent='เริ่มชุดดาวน์โหลดไม่สำเร็จ';
      message(e.message || 'ติดต่อเซิร์ฟเวอร์ไม่สำเร็จ'); lastInput='';
      $('#resume-batch').textContent='ลองเริ่มใหม่';
      $('#resume-batch').hidden=false;
    }
  }
  function schedule(event) {
    clearTimeout(timer);
    const count = lines().length;
    $('#detected').textContent=count ? `${count} ลิงก์` : '';
    $('#form-message').hidden=true;
    if (count && !busy && input.value !== lastInput) timer=setTimeout(start,event?.inputType==='insertFromPaste'?0:600);
  }
  function preview(job) {
    const audio = job.filename.endsWith('.mp3');
    const player = $(audio?'#preview-audio':'#preview-video'), other = $(audio?'#preview-video':'#preview-audio');
    other.pause(); other.removeAttribute('src'); other.load(); other.hidden=true;
    if(!loaded(job))return;
    const key=receipt(job);if(!run.objectURLs.has(key))run.objectURLs.set(key,URL.createObjectURL(loaded(job)));
    player.hidden=false; player.src=run.objectURLs.get(key); player.load();
    $('#preview-title').textContent=job.title; $('#clip-preview').showModal();
  }
  $('#clip-preview').addEventListener('close',()=>{
    for (const id of ['#preview-video','#preview-audio']) { const el=$(id); el.pause(); el.removeAttribute('src'); el.load(); }
  });
  $('#select-all').onchange=e=>{
    run.selectNew=e.target.checked;
    for (const j of run.items.filter(loaded)) {
      e.target.checked ? run.unselected.delete(j.id) : run.unselected.add(j.id);
      rows.get(j.id).querySelector('input').checked=e.target.checked;
    }
    saveState(); updateActions();
  };
  $('#zip-all').onclick=event=>{
    if(!run?.finished||$('#zip-all').disabled){event.preventDefault();return;}
    $('#save-location').textContent='เลือกดาวน์โหลด ZIP แล้ว · ตรวจสอบรายการดาวน์โหลดของเบราว์เซอร์ แล้วแตะ ZIP ในแอปไฟล์เพื่อแตกไฟล์';
  };
  $('#save-selected').onclick=()=>shareJobs(selected());
  $('#load-previous').onclick=()=>{
    if(!run||sending||!run.finished)return;
    for(const job of run.items){if(run.history.has(job.id)){run.history.delete(job.id);run.queued.add(receipt(job));sendQueue.push({generation,job});}}
    updateActions();drainDownloads();
  };
  $('#retry-failed').onclick=async()=>{
    const token=generation,current=run; $('#retry-failed').disabled=true;controls(true);
    try{
      run.remembered=false;
      const snapshot=await request(`${run.path}/${run.id}/retry`,{method:'POST'});
      if(token!==generation){api(`${current.path}/${current.id}/cancel`,{method:'POST'},current.base,current.key).catch(()=>{});return;}
      $('#form-message').hidden=true;render(snapshot);poll(token);
    }catch(e){if(token===generation){controls(false);message(e.message);}}
    finally{if(token===generation)$('#retry-failed').disabled=false;}
  };
  $('#clear-links').onclick=()=>{
    clearTimeout(timer);
    if(run?.id)request(`${run.path}/${run.id}/cancel`,{method:'POST'}).catch(()=>{});
    ++generation;releaseFiles();run=null;sendQueue=[];rows.clear();forgetState();controls(false);
    input.value='';lastInput='';$('#detected').textContent='';$('#form-message').hidden=true;
    panel.hidden=true;$('#batch-list').replaceChildren();$('#result').hidden=true;input.focus();
  };
  const resume=document.createElement('button');resume.type='button';resume.id='resume-batch';resume.className='secondary-btn';resume.textContent='เช็กคิวอีกครั้ง / ลองเริ่มใหม่';resume.hidden=true;$('#batch-summary').after(resume);
  resume.onclick=()=>{
    resume.hidden=true;
    if(run?.id){controls(true);if(run.base===apiBase)run.key=apiKey;poll(generation);}
    else if(run?.pendingCreate&&run.selection){controls(true);if(run.base===apiBase)run.key=apiKey;return submitBatch(run,generation);}
    else return start();
  };
  form.removeEventListener('submit',download);
  input.removeEventListener('input',updateDetected);
  updateDetected=schedule;
  download=start;form.addEventListener('submit',start);input.addEventListener('input',schedule);
  $('#paste-btn').onclick=async()=>{try{input.value=await navigator.clipboard.readText();schedule({inputType:'insertFromPaste'});input.focus();}catch{toast('แตะช่องลิงก์แล้วใช้คำสั่งวาง');input.focus();}};
  const originalSaveGuide=renderSaveGuide;
  renderSaveGuide=(value=format)=>{
    for(const device of Object.values(DEVICE_GUIDES)) {
      device.steps[0]='รอให้คลิปขึ้น “พร้อม · Ready” แล้วแตะ “บันทึก” หรือเลือกหลายคลิปแล้วแตะ “บันทึกทั้งหมด” เพื่อเปิดเมนูแชร์ครั้งเดียว';
      device.steps[1]=value==='mp3'?'เลือก “บันทึกไปยังไฟล์” แล้วเลือกโฟลเดอร์ ไฟล์ MP3 เปิดฟังได้ในแอปไฟล์หรือแอปเล่นเพลง':device.mobile?'เลือก “บันทึกวิดีโอ” เพื่อเก็บในรูปภาพ หรือ “บันทึกไปยังไฟล์” แล้วเลือกโฟลเดอร์ ตามเมนูที่เครื่องรองรับ':'เลือกตำแหน่งบันทึกในเมนูแชร์ หากไม่มีเมนูแชร์ ใช้ปุ่ม ZIP หลังคิวจบ';
      device.steps[2]='ไฟล์ยังไม่ถูกบันทึกลงเครื่องจนกว่าคุณจะเลือกในเมนูแชร์ · เปิดหน้านี้ไว้จนบันทึกเสร็จ หรือใช้ ZIP สำรอง';
    }
    originalSaveGuide(value);
  };
  renderSaveGuide();
  async function restoreState() {
    let saved;
    try { saved=JSON.parse(sessionStorage.getItem(STATE_KEY)||'null'); } catch { forgetState(); return; }
    if (!saved) return;
    const selection=saved.selection;
    if (saved.base!==apiBase || !['/api/public/batches','/api/batches'].includes(saved.path)
        || !selection || !Array.isArray(selection.urls) || !selection.urls.length || selection.urls.length>MAX_LINKS
        || !selection.urls.every(url=>typeof url==='string'&&url.length<=10000)
        || !['mp4','mp3'].includes(selection.format) || !['1080','720','480','360'].includes(selection.quality)
        || typeof selection.request_id!=='string' || !selection.request_id || selection.request_id.length>80
        || !saved.id && !saved.pendingCreate
        || saved.id && (typeof saved.id!=='string'||! /^[\w-]{1,100}$/.test(saved.id))) { forgetState(); return; }
    const token=++generation;
    run=Object.assign(freshRun(),{id:saved.id,path:saved.path,selection,format:selection.format,
      pendingCreate:!!saved.pendingCreate,selectNew:saved.selectNew!==false,remembered:!!saved.remembered});
    // Resume unfinished work, but load completed history only on an explicit tap.
    for (const field of ['unselected','seenReady']) {
      if (Array.isArray(saved[field])) run[field]=new Set(saved[field].filter(value=>typeof value==='string').slice(-(field==='dispatched'?MAX_LINKS*4:MAX_LINKS)));
    }
    input.value=selection.urls.join('\n');lastInput=input.value;
    if (typeof resetFormat==='function') resetFormat(selection.format); else format=selection.format;
    $('#quality').value=selection.quality;$('#detected').textContent=`${selection.urls.length} ลิงก์`;
    panel.hidden=false;$('#batch-title').textContent='กำลังเช็กคิวเดิม';
    $('#batch-summary').textContent='กำลังเรียกคืนรายการจากแท็บนี้…';controls(true);updateActions();saveNotice();
    if (!run.id) {
      controls(false);resume.textContent='เช็กคำขอเดิมอีกครั้ง';resume.hidden=false;
      $('#batch-summary').textContent='มีคำขอเดิมที่ยังไม่ได้รับการยืนยัน กดเช็กเพื่อใช้คำขอเดิมต่อ';
      return;
    }
    try {
      const snapshot=await request(`${run.path}/${encodeURIComponent(run.id)}`);
      if (token!==generation) return;
      for(const job of snapshot.items)if(isDone(job)&&canSave(job))run.history.add(job.id);
      render(snapshot);poll(token);
    } catch(error) { queueError(error,token); }
  }
  restoreState();
})();
