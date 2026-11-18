/* Batch flow shares the existing connection, device guides, and collection. */
(() => {
  'use strict';
  const input = $('#video-url'), form = $('#download-form'), panel = $('#batch-results');
  const MAX_LINKS = 200, SHARE_LIMIT = 100 * 1024 * 1024;
  const STATE_KEY = 'klipnivo-batch-v1';
  let run = null, generation = 0, timer, lastInput = '', rows = new Map();
  let sendQueue = [], sending = false, shareFiles = [], shareSignature = '';
  let shareAbort = null, shareVersion = 0, sharePending = false, savingSelection = false;
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const lines = () => input.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  const canSave = job => job.status === 'ready' || job.file_ready;
  const isDone = job => job.status === 'ready' && (!job.delivery_status || job.delivery_status === 'sent');
  const receipt = job => `${job.id}:${job.attempt || 1}`;
  const selected = () => run ? run.items.filter(j => canSave(j) && !run.unselected.has(j.id)) : [];
  const freshRun = () => ({base:apiBase,key:apiKey,format,items:[],sent:new Set(),dispatched:new Set(),queued:new Set(),unselected:new Set(),seenReady:new Set(),selectNew:true,finished:false});
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
  function invalidateShare() {
    ++shareVersion; shareAbort?.abort(); shareAbort=null; sharePending=false;
    shareFiles = []; shareSignature = '';
    $('#share-files').hidden = true;
    $('#prepare-share').textContent='เตรียมบันทึกผ่านเมนูแชร์';
  }
  function saveNotice() {
    if (!run) return;
    const count = run.sent.size;
    $('#save-location').textContent = count
      ? `ส่งดาวน์โหลดแล้ว ${count} คลิป → ไฟล์ (Files) > ดาวน์โหลด (Downloads) ตามการตั้งค่าเบราว์เซอร์ ตรวจสอบรายการดาวน์โหลดเพื่อยืนยันว่าบันทึกครบ`
      : 'ไฟล์จะส่งไปยัง Files > Downloads ตามการตั้งค่าเบราว์เซอร์ อาจมีหน้าต่างขออนุญาตดาวน์โหลด';
  }
  async function downloadFile(job, automatic = false) {
    if (!run || !canSave(job)) return;
    const current=run,token=generation,key=receipt(job);
    if (!automatic || !run.dispatched.has(key) && !(isDone(job) && run.sent.has(job.id))) {
      saveAnchor(fileURL(job.download_url), job.filename);
      run.sent.add(job.id);run.dispatched.add(key);saveState();
    }
    const label = rows.get(job.id)?.querySelector('.clip-save-status');
    if (label) label.textContent = 'ส่งดาวน์โหลดแล้ว · ตรวจสอบใน Files / Downloads';
    saveNotice();
    if (!automatic) toast('ส่งคำขอดาวน์โหลดแล้ว');
    if (job.delivery_status === 'waiting') {
      try {
        await api(`${current.path}/${current.id}/sent`,{method:'POST',body:JSON.stringify({id:job.id,attempt:job.attempt})},current.base,current.key);
      } catch {
        // Repeat the receipt on the next poll, without resending the same file.
        if (token===generation) run.queued.delete(key);
      }
    }
  }
  async function drainDownloads() {
    if (sending) return;
    sending = true;
    try {
      while (sendQueue.length) {
        const entry = sendQueue.shift();
        if (run && entry.generation === generation && (!run.dispatched.has(receipt(entry.job)) || entry.job.delivery_status === 'waiting')) {
          await downloadFile(entry.job, true);
          await pause(1100);
        }
      }
    } finally { sending = false; if (run?.finished) controls(false); }
  }
  function row(job) {
    let el = rows.get(job.id);
    if (!el) {
      el = document.createElement('article'); el.className = 'clip-row'; el.dataset.id = job.id;
      el.innerHTML = '<label class="clip-select"><input type="checkbox" checked aria-label="เลือกคลิป"></label><div class="clip-thumbnail"><span>▶</span></div><div class="clip-details"><h3></h3><p class="clip-status"></p><progress max="100" value="0" aria-label="ความคืบหน้าคลิป"></progress><p class="clip-save-status"></p><div class="clip-actions"><button type="button" data-action="preview">ดู / ฟัง</button><button type="button" data-action="save">บันทึกไฟล์</button></div></div>';
      rows.set(job.id, el); $('#batch-list').append(el);
      el.querySelector('input').addEventListener('change', e => {
        e.target.checked ? run.unselected.delete(job.id) : run.unselected.add(job.id);
        invalidateShare(); saveState(); updateActions();
      });
      el.querySelector('[data-action="save"]').onclick = () => downloadFile(run.items.find(j => j.id === job.id));
      el.querySelector('[data-action="preview"]').onclick = () => preview(run.items.find(j => j.id === job.id));
    }
    const signature = JSON.stringify([job.status,job.stage,job.progress,job.title,job.thumbnail,job.warning,job.error,job.attempt,job.delivery_status]);
    if (el.dataset.signature === signature) return;
    el.dataset.signature = signature;
    el.dataset.status = job.status;
    el.dataset.active = job.status === 'processing' || job.status === 'ready' && !isDone(job) ? 'true' : 'false';
    el.querySelector('input').checked = canSave(job) ? !run.unselected.has(job.id) : run.selectNew;
    el.querySelector('h3').textContent = `${String(job.index).padStart(2,'0')} · ${job.title || job.url || 'กำลังอ่านข้อมูลคลิป'}`;
    const stages = {waiting:'รอคิว',resolving:'กำลังดาวน์โหลด · อ่านข้อมูล',downloading:'กำลังดาวน์โหลด',merging:'กำลังดาวน์โหลด · รวมภาพและเสียง',converting:'กำลังดาวน์โหลด · เตรียม MP4 / แปลงไฟล์',sending:'ไฟล์พร้อม · กำลังส่งคำขอดาวน์โหลด',retrying:'ลองใหม่อัตโนมัติ',done:'เสร็จแล้ว',failed:'ไม่สำเร็จ',cancelled:'ยกเลิกแล้ว'};
    el.querySelector('.clip-status').textContent = job.error || `${stages[job.stage] || 'รอคิว'}${el.dataset.active === 'true' ? ` · ${Math.round(job.progress || 0)}%` : ''}${job.auto_retry ? ' · ลองใหม่ 1/1' : ''}${job.warning ? ' · ' + job.warning : ''}`;
    const progress = el.querySelector('progress'); progress.value = job.progress || 0;
    progress.hidden = el.dataset.active !== 'true' && job.status !== 'queued';
    el.querySelector('input').disabled = !canSave(job);
    el.querySelector('.clip-actions').hidden = !canSave(job);
    if (run.sent.has(job.id)) el.querySelector('.clip-save-status').textContent='ส่งดาวน์โหลดแล้ว · ตรวจสอบใน Files / Downloads';
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
    const ready = run.items.filter(canSave), chosen = selected();
    const all = $('#select-all'); all.checked = ready.length > 0 && chosen.length === ready.length;
    all.indeterminate = chosen.length > 0 && chosen.length !== ready.length;
    all.disabled = !ready.length;
    $('#zip-all').disabled = !ready.length;
    $('#zip-all').textContent = `ดาวน์โหลดทั้งหมดเป็น ZIP (${ready.length})`;
    $('#save-selected').disabled = !chosen.length || savingSelection;
    $('#retry-failed').hidden = run.expired || !run.finished || !run.items.some(j => j.status === 'error' && j.error_code !== 'invalid_link');
    const canShare = typeof navigator.share === 'function' && typeof navigator.canShare === 'function';
    $('#prepare-share').hidden = !canShare || !chosen.length || shareFiles.length > 0;
    $('#prepare-share').disabled = sharePending || chosen.reduce((n,j) => n+(j.size||0),0) > SHARE_LIMIT;
    $('#prepare-share').title = 'เมนูแชร์รองรับรวมไม่เกิน 100 MB ต่อครั้ง เลือกน้อยลงหรือใช้ ZIP สำหรับชุดใหญ่';
  }
  function rememberBatch() {
    if (!run || run.remembered) return;
    run.remembered = true;
    for (const job of run.items.filter(j => j.status === 'ready').reverse()) {
      const parsed = parseLink(job.url);
      if (!parsed) continue;
      records = records.filter(r => r.url !== parsed.url);
      records.unshift({id:crypto.randomUUID(),date:Date.now(),title:job.title||'คลิปของคุณ',source:parsed.source,url:parsed.url,format:run.format,quality:job.height ? job.height+'p' : 'ต้นฉบับ'});
    }
    records = records.slice(0,30); persist();
    saveState();
  }
  function render(snapshot) {
    if (!run) return;
    run.items = snapshot.items; run.finished = snapshot.finished; run.archive_url = snapshot.archive_url;
    for (const job of run.items) {
      if (job.status === 'ready' && !run.seenReady.has(job.id)) {
        if (!run.selectNew) run.unselected.add(job.id);
        run.seenReady.add(job.id);
      }
      row(job);
      if (job.status === 'ready' && !run.queued.has(receipt(job))) {
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
      ? `เสร็จแล้ว · สำเร็จ ${ready} คลิป · ไม่สำเร็จ ${failed.length} คลิป${failed.length ? ` (คลิป ${failed.map(j=>String(j.index).padStart(2,'0')).join(', ')})` : ''}`
      : 'ดาวน์โหลดทีละคลิป ตามลำดับ · ลองใหม่อัตโนมัติ 1 ครั้ง · หยุดคลิปที่ไม่คืบหน้า 75 วินาที';
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
    let errors = 0;
    while (run && token === generation && !run.finished) {
      await pause(1500);
      if (!run || token !== generation) return;
      try {
        const snapshot = await request(`${run.path}/${encodeURIComponent(run.id)}`);
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
    const token = ++generation;
    run = freshRun(); forgetState();
    const current = run;
    rows.clear(); $('#batch-list').replaceChildren(); sendQueue=[]; invalidateShare();
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
      current.selection = {urls,format:current.format,quality:$('#quality').value,request_id:crypto.randomUUID()};
      await submitBatch(current,token);
    } catch (e) {
      if (token !== generation) return;
      controls(false); run.finished=true; $('#batch-summary').textContent='เริ่มชุดดาวน์โหลดไม่สำเร็จ';
      message(e.message || 'ติดต่อเซิร์ฟเวอร์ไม่สำเร็จ'); lastInput='';
      $('#resume-batch').textContent='ลองเริ่มใหม่';
      $('#resume-batch').hidden=false;
    }
  }
  function schedule() {
    clearTimeout(timer);
    const count = lines().length;
    $('#detected').textContent=count ? `${count} ลิงก์` : '';
    $('#form-message').hidden=true;
    if (count && !busy && input.value !== lastInput) timer=setTimeout(start,900);
  }
  function preview(job) {
    const audio = job.filename.endsWith('.mp3');
    const player = $(audio?'#preview-audio':'#preview-video'), other = $(audio?'#preview-video':'#preview-audio');
    other.pause(); other.removeAttribute('src'); other.load(); other.hidden=true;
    player.hidden=false; player.src=fileURL(job.preview_url); player.load();
    $('#preview-title').textContent=job.title; $('#clip-preview').showModal();
  }
  $('#clip-preview').addEventListener('close',()=>{
    for (const id of ['#preview-video','#preview-audio']) { const el=$(id); el.pause(); el.removeAttribute('src'); el.load(); }
  });
  $('#select-all').onchange=e=>{
    run.selectNew=e.target.checked;
    for (const j of run.items.filter(canSave)) {
      e.target.checked ? run.unselected.delete(j.id) : run.unselected.add(j.id);
      rows.get(j.id).querySelector('input').checked=e.target.checked;
    }
    invalidateShare(); saveState(); updateActions();
  };
  $('#zip-all').onclick=()=>{
    const jobs=run.items.filter(canSave); if(!jobs.length)return;
    const url=new URL(fileURL(run.archive_url));
    saveAnchor(url.href,`KlipNivo-${run.id.slice(0,8)}.zip`);
    $('#save-location').textContent=`ส่ง ZIP รวม ${jobs.length} คลิปไปยังรายการดาวน์โหลดแล้ว → Files > Downloads · แตะไฟล์ ZIP ในแอปไฟล์เพื่อแตกไฟล์`;
  };
  $('#save-selected').onclick=async()=>{
    if(savingSelection)return;
    const token=generation, jobs=selected(); savingSelection=true; updateActions();
    try{for(const job of jobs){if(token!==generation)break;await downloadFile(job);await pause(1100);}}
    finally{if(token===generation){savingSelection=false;updateActions();}}
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
  $('#prepare-share').onclick=async()=>{
    if(sharePending)return;
    const chosen=selected(),token=generation;
    if(!chosen.length||chosen.reduce((sum,j)=>sum+(j.size||0),0)>SHARE_LIMIT)return;
    const button=$('#prepare-share'),version=shareVersion;
    const controller=new AbortController();shareAbort=controller;
    const timeout=setTimeout(()=>controller.abort(),120000),signal=controller.signal;
    sharePending=true;button.disabled=true;button.textContent='กำลังเตรียมเมนูแชร์…';
    try{
      const files=[];let total=0;
      for(const job of chosen){
        const response=await fetch(fileURL(job.download_url),{signal});
        if(token!==generation||version!==shareVersion)return;
        if(!response.ok)throw Error('โหลดไฟล์สำหรับเมนูแชร์ไม่สำเร็จ');
        const blob=await response.blob();total+=blob.size;
        if(total>SHARE_LIMIT)throw Error('ไฟล์เกิน 100 MB กรุณาเลือกน้อยลงหรือใช้ ZIP');
        files.push(new File([blob],job.filename,{type:blob.type}));
        if(token!==generation||version!==shareVersion)return;
      }
      if(chosen.map(j=>j.id).join(',')!==selected().map(j=>j.id).join(','))throw Error('รายการเลือกเปลี่ยนแล้ว กรุณาเตรียมอีกครั้ง');
      if(!navigator.canShare({files}))throw Error('เบราว์เซอร์นี้แชร์ไฟล์ชุดนี้ไม่ได้ กรุณาใช้ ZIP');
      shareFiles=files;shareSignature=chosen.map(j=>j.id).join(',');
      $('#share-files').hidden=false;button.hidden=true;
      $('#save-location').textContent=`พร้อมแชร์ ${files.length} คลิป · แตะ “บันทึกทั้งหมด · Save to Files” แล้วเลือก “บันทึกไปยังไฟล์”`;
    }catch(e){if(token===generation&&version===shareVersion)message(e.name==='AbortError'?'เตรียมเมนูแชร์นานเกินไป กรุณาลองใหม่หรือใช้ ZIP':e.message);}
    finally{clearTimeout(timeout);if(token===generation&&version===shareVersion){sharePending=false;shareAbort=null;button.textContent='เตรียมบันทึกผ่านเมนูแชร์';updateActions();}}
  };
  $('#share-files').onclick=async()=>{
    if(!shareFiles.length||shareSignature!==selected().map(j=>j.id).join(','))return;
    const token=generation,version=shareVersion,files=shareFiles.slice();
    try{
      // A fresh click retains the transient activation required by iOS Web Share.
      await navigator.share({files,title:'KlipNivo'});
      if(token===generation&&version===shareVersion)$('#save-location').textContent=`ส่ง ${files.length} คลิปให้เมนูแชร์แล้ว หากเลือก “บันทึกไปยังไฟล์” ให้ตรวจสอบโฟลเดอร์ที่คุณเลือก`;
    }catch(e){if(token===generation&&version===shareVersion&&e.name!=='AbortError')message('เปิดเมนูแชร์ไม่ได้ กรุณาใช้ ZIP หรือบันทึกไฟล์');}
  };
  $('#clear-links').onclick=()=>{
    clearTimeout(timer);
    if(run?.id)request(`${run.path}/${run.id}/cancel`,{method:'POST'}).catch(()=>{});
    ++generation;run=null;sendQueue=[];savingSelection=false;rows.clear();invalidateShare();forgetState();controls(false);
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
  $('#paste-btn').onclick=async()=>{try{input.value=await navigator.clipboard.readText();schedule();input.focus();}catch{toast('แตะช่องลิงก์แล้วใช้คำสั่งวาง');input.focus();}};
  for(const device of Object.values(DEVICE_GUIDES)) {
    device.steps[0]='ไฟล์พร้อมแล้วจะเริ่มดาวน์โหลดอัตโนมัติ อนุญาตเมื่อเบราว์เซอร์ถาม หรือใช้ปุ่ม ZIP เพื่อเก็บหลายไฟล์ในครั้งเดียว';
    device.steps[2]=device.mobile?'แตะ ZIP เพื่อแตกไฟล์ หรือใช้เมนูแชร์ → “บันทึกไปยังไฟล์” เมื่อรองรับ เลือกโฟลเดอร์ได้ด้วยตัวเอง':'เปิดไฟล์ MP4 จาก Downloads เพื่อดู หรือแตกไฟล์ ZIP เพื่อดูคลิปทั้งหมด';
  }
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
    for (const field of ['sent','dispatched','unselected','seenReady']) {
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
      render(snapshot);poll(token);
    } catch(error) { queueError(error,token); }
  }
  restoreState();
})();
