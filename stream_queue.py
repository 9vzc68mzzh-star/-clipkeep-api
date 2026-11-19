"""One ordered delivery, one look-ahead metadata plan, and a single ZIP backup."""
import asyncio
import json
import logging
import multiprocessing
import os
import re
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote
from fastapi import HTTPException
from fastapi.responses import StreamingResponse,FileResponse
from fast_media import plan_worker,stream_bytes
from media import MediaError

class State:
    def __init__(self,env):self.env=env
    def __getattr__(self,name):return self.env[name]

class StreamQueue:
    def __init__(self,env):
        self.s=State(env);self.prepare_pool=ThreadPoolExecutor(max_workers=1)
        # Fork from a clean, single-threaded helper with yt-dlp already imported.
        # Avoid importing it anew for every clip, without forking the API threads.
        self.process_method='forkserver' if 'forkserver' in multiprocessing.get_all_start_methods() else 'spawn'
        if self.process_method=='forkserver':multiprocessing.set_forkserver_preload(['fast_media'])
        self.plans={};self.plan_target=plan_worker;self.byte_source=stream_bytes
        self.active=None;self.lookahead=None

    def timing(self,job_id,key,value):
        job=self.s.jobs[job_id];job.setdefault('timings',{})[key]=round(value,4)
        self.s.logger.info('timing index=%s job=%s attempt=%s step=%s seconds=%.4f',job['index'],job_id[:8],job['attempt'],key,value)

    def archive_path(self,batch_id):return self.s.ROOT/batch_id/'clips.zip'

    def enqueue(self,batch_id):
        batch=self.s.batches[batch_id]
        batch['future']=self.s.pool.submit(self.run,batch_id)

    def prepare_future(self,job_id,prefetch=False):
        with self.s.lock:
            if job_id not in self.plans:
                job=self.s.jobs[job_id];job['prefetched']=prefetch
                job.update(status='processing',stage='prefetching' if prefetch else 'resolving',progress=1)
                self.plans[job_id]=self.prepare_pool.submit(self.prepare,job_id)
            return self.plans[job_id]

    def prepare(self,job_id):
        s=self.s;job=s.jobs[job_id];batch=s.batches[job['batch_id']]
        started=time.monotonic();job.setdefault('_started',started)
        resolved=job.get('resolved_url') or batch.setdefault('resolved_links',{}).get(job['url'])
        initial={'url':job['url'],'resolved_url':resolved,'timings':{},'index':job['index'],'attempt':job['attempt'],
                 'spawned_at':started,'max_video_seconds':s.MAX_VIDEO_SECONDS,'max_file_bytes':s.MAX_FILE_BYTES}
        context=multiprocessing.get_context(self.process_method);receive,send=context.Pipe(duplex=False)
        worker=context.Process(target=self.plan_target,args=(job_id,job['selection'],initial,send))
        last=time.monotonic()
        try:
            worker.start();send.close()
            while True:
                if job.get('cancel'):raise MediaError('cancelled')
                if time.monotonic()-last>s.STALL_SECONDS:raise MediaError('stalled')
                if receive.poll(.2):
                    try:event=receive.recv()
                    except EOFError:raise MediaError('download_failed') from None
                    if event['kind']=='error':raise MediaError(event['code'])
                    fields=event['fields'];old=dict(job.get('timings') or {})
                    if job.get('cancel'):raise MediaError('cancelled')
                    with s.lock:job.update(fields)
                    if fields.get('resolved_url'):batch['resolved_links'][job['url']]=fields['resolved_url']
                    for key,value in fields.get('timings',{}).items():
                        if old.get(key)!=value:self.timing(job_id,key,value)
                    if event.get('progressed'):last=time.monotonic()
                    if event['kind']=='result':break
                elif not worker.is_alive():raise MediaError('download_failed')
            title=job.get('title') or 'clip'
            readable=re.sub(r'[\\/:*?"<>|\x00-\x1f]','-',title).strip(' .-').encode('utf-8')[:160].decode('utf-8','ignore') or 'clip'
            with s.lock:
                if job.get('cancel'):raise MediaError('cancelled')
                job.update(status='ready',stage='next_ready' if job.get('prefetched') else 'sending',progress=5,
                           stream_ready=True,delivery_status='waiting',filename=f"{job['index']:03d}-{readable}-{job_id[:8]}.{job['selection']['format']}",
                           download_url='/api/files/'+job_id,preview_url='/api/preview/'+job_id)
            self.timing(job_id,'prepare_s',time.monotonic()-started)
            job['_plan_done']=time.monotonic()
        finally:
            if worker.pid:s.stop_worker(worker)
            receive.close();send.close()

    def next_prefetch(self,job_id):
        s=self.s;job=s.jobs[job_id];batch=s.batches[job['batch_id']]
        if self.active!=job_id or self.lookahead:return
        after=batch['ids'][batch['ids'].index(job_id)+1:]
        next_id=next((j for j in after if not s.settled(s.jobs[j]) and not s.jobs[j].get('cancel')),None)
        if next_id:
            self.lookahead=next_id;self.prepare_future(next_id,True)

    def run(self,batch_id):
        s=self.s;batch=s.batches[batch_id]
        try:
            for job_id in batch['ids']:
                job=s.jobs[job_id]
                if s.settled(job):continue
                self.active=job_id;batch['_active']=job_id
                if self.lookahead==job_id:self.lookahead=None
                job['prefetched']=False
                for automatic in range(2):
                    try:
                        job['_can_start']=False
                        if job.get('cancel'):raise MediaError('cancelled')
                        if automatic:
                            job['attempt']+=1;job['auto_retry']=1
                            for key in ('error','error_code','_stream_error','_response_claimed'):job.pop(key,None)
                            if not job.get('file_ready'):self.plans.pop(job_id,None)
                        if not job.get('file_ready'):
                            future=self.prepare_future(job_id)
                            while not future.done():
                                if job.get('cancel'):raise MediaError('cancelled')
                                time.sleep(.1)
                            future.result()
                        job['_delivery_touched']=time.monotonic()
                        job.update(status='ready',stage='sending',delivery_status='waiting',prefetched=False,_can_start=True)
                        while job.get('delivery_status')!='sent':
                            if job.get('cancel'):raise MediaError('cancelled')
                            if job.get('_stream_error'):raise MediaError(job['_stream_error'])
                            if time.monotonic()-job['_delivery_touched']>s.DELIVERY_SECONDS:
                                raise MediaError('delivery_timeout')
                            time.sleep(.1)
                        break
                    except Exception as exc:
                        code='cancelled' if job.get('cancel') else s.error_code(exc);retrying=automatic==0 and code!='cancelled'
                        self.stop_response(job)
                        job.update(status='processing' if retrying else 'cancelled' if code=='cancelled' else 'error',
                                   stage='retrying' if retrying else 'cancelled' if code=='cancelled' else 'failed',
                                   progress=0,error=None if retrying else s.ERROR_MESSAGES[code],error_code=code)
                        if not retrying:break
                job['finished']=time.time()
            s.finish_batch(batch_id)
        finally:
            self.active=None;self.lookahead=None;batch.pop('_active',None)
            s.finish_batch(batch_id)

    def change(self,batch_id,action):
        s=self.s;batch=s.batches[batch_id]
        if action=='cancel':
            for job_id in batch['ids']:
                job=s.jobs[job_id]
                if not s.settled(job):
                    job['cancel']=True
                    job.update(status='cancelled',stage='cancelled',finished=time.time())
                    future=self.plans.get(job_id)
                    if future:future.cancel()
            future=batch.get('future')
            if future:future.cancel()
        else:
            if not batch.get('finished'):raise HTTPException(409,'โปรดรอให้คิวจบก่อนลองใหม่')
            if sum(not b.get('finished') for b in s.batches.values())>=s.MAX_ACTIVE_BATCHES:raise HTTPException(429,'คิวเต็มชั่วคราว')
            batch.pop('finished',None);batch.pop('cancel_reason',None);batch['last_seen']=time.time()
            for job_id in batch['ids']:
                job=s.jobs[job_id]
                if job['status']=='error' and job.get('error_code')!='invalid_link':
                    job.update(status='queued',stage='waiting',progress=0,auto_retry=0,cancel=False,attempt=job['attempt']+1)
                    for key in ('error','error_code','finished','_stream_error','_response_claimed'):job.pop(key,None)
                    self.plans.pop(job_id,None)
            self.enqueue(batch_id)
        s.finish_batch(batch_id)

    def can_download(self,job_id):
        job=self.s.jobs[job_id]
        return self.active==job_id and job.get('_can_start',False) and job['status']=='ready' and job.get('delivery_status') in ('waiting','requested')

    def stop_response(self,job):
        job['_stop']=True
        process=job.get('_process')
        if process and process.poll() is None:
            try:os.killpg(process.pid,9)
            except ProcessLookupError:pass
        task=job.get('_response_task');loop=job.get('_response_loop')
        if task and loop and not task.done():loop.call_soon_threadsafe(task.cancel)
        done=job.get('_response_done')
        if done:done.wait(timeout=25)

    def response(self,job_id,request,preview=False):
        s=self.s;job=s.jobs.get(job_id)
        if not job:raise HTTPException(404,'ไม่พบไฟล์หรือไฟล์หมดอายุแล้ว')
        if job.get('file_ready'):
            if not preview and not s.batches[job['batch_id']].get('finished') and not self.can_download(job_id):
                raise HTTPException(409,'โปรดรอคิวจบก่อนบันทึกซ้ำ')
            return self.stored_response(job_id,job,request,preview)
        if preview or not self.can_download(job_id) or job.get('_response_claimed'):raise HTTPException(409,'คลิปนี้ยังไม่ถึงลำดับดาวน์โหลด')
        job['_response_claimed']=True;job['_stop']=False;job['_stream_error']=None
        job['_response_done']=threading.Event()
        return ClipResponse(self,job_id)

    def stream(self,job_id):
        s=self.s;job=s.jobs[job_id];batch_id=job['batch_id'];plan=job['plan'];attempt=job['attempt']
        started=time.monotonic();job['_send_started']=started
        job.update(delivery_status='streaming',stage='sending',progress=5)
        job['_delivery_touched']=time.monotonic();self.next_prefetch(job_id)
        path=self.archive_path(batch_id);path.parent.mkdir(exist_ok=True)
        stats={};size=0;first=True;success=False
        self.timing(job_id,'wait_turn_s',max(0,started-job.get('_plan_done',started)))
        archive=zipfile.ZipFile(path,'a',compression=zipfile.ZIP_STORED,allowZip64=True)
        original=list(archive.filelist);names=dict(archive.NameToInfo);start_dir=archive.start_dir
        target=archive.open(job['filename'],'w',force_zip64=True);offset=archive.fp.tell()
        def check():
            if job.get('cancel') or job.get('_stop') or job['attempt']!=attempt:raise MediaError('cancelled')
            if time.monotonic()-job['_delivery_touched']>s.STALL_SECONDS:raise MediaError('stalled')
            if size>s.MAX_FILE_BYTES:raise MediaError('file_too_large')
            if path.stat().st_size>s.MAX_STORED_BYTES or shutil.disk_usage(path.parent).free<64*1024*1024:raise MediaError('storage_limit')
        try:
            for chunk in self.byte_source(plan,check,stats,lambda process:job.__setitem__('_process',process)):
                check();size+=len(chunk)
                if size>s.MAX_FILE_BYTES:raise MediaError('file_too_large')
                target.write(chunk)
                if first:self.timing(job_id,'first_byte_s',time.monotonic()-started);first=False
                known=(plan['details'].get('size') or 0) if plan['mode']=='direct' else 0
                job.update(bytes_sent=size,bytes_total=known)
                job['progress']=min(98,5+size/known*93) if known else 10
                yield chunk
            if not size:raise MediaError('download_failed')
            target.close();archive.close();success=True
            job.update(file_ready=True,_archive_offset=offset,size=size)
            for key,value in stats.items():self.timing(job_id,key,value)
        except BaseException as exc:
            if job['attempt']==attempt:job['_stream_error']=s.error_code(exc)
            if isinstance(exc,GeneratorExit):raise
            raise OSError('Media transfer interrupted') from None
        finally:
            if not success:
                target.close();archive.filelist=original;archive.NameToInfo=names
                archive.start_dir=start_dir;archive.fp.seek(start_dir);archive.close()
                with path.open('r+b') as f:f.truncate(path.stat().st_size)
            job.pop('_process',None)
            self.timing(job_id,'transfer_s',time.monotonic()-started)
            # No per-clip video file is created; only the required ZIP backup remains.
            shutil.rmtree(s.ROOT/job_id,ignore_errors=True)

    def sent(self,job_id,seconds,attempt):
        job=self.s.jobs[job_id]
        if job.get('file_ready') and job['attempt']==attempt and not job.get('cancel') and not job.get('_stream_error'):
            job.update(delivery_status='sent',stage='done',progress=100)
            self.timing(job_id,'send_s',seconds)
            self.timing(job_id,'total_s',time.monotonic()-job.get('_started',time.monotonic()))
            self.s.finish_batch(job['batch_id'])

    def stored_response(self,job_id,job,request,preview=False):
        size=job['size'];start,end=0,size-1;status=200
        headers={'Accept-Ranges':'bytes','Cache-Control':'no-store','Content-Disposition':("inline" if preview else "attachment")+"; filename*=UTF-8''"+quote(job['filename'])}
        value=request.headers.get('range','')
        if value:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',value)
            if not match or not any(match.groups()):raise HTTPException(416,'ช่วงไฟล์ไม่ถูกต้อง')
            if not match[1]:start=max(0,size-int(match[2]))
            else:start=int(match[1]);end=min(size-1,int(match[2])) if match[2] else size-1
            if start>end or start>=size:raise HTTPException(416,'ช่วงไฟล์ไม่ถูกต้อง')
            headers['Content-Range']=f'bytes {start}-{end}/{size}';status=206
        headers['Content-Length']=str(end-start+1)
        def chunks():
            with self.archive_path(job['batch_id']).open('rb') as source:
                source.seek(job['_archive_offset']+start);left=end-start+1
                while left:
                    chunk=source.read(min(64*1024,left))
                    if not chunk:raise OSError('Backup file truncated')
                    left-=len(chunk);yield chunk
        response=StreamingResponse(chunks(),status_code=status,headers=headers,media_type='audio/mpeg' if job['filename'].endswith('.mp3') else 'video/mp4')
        if not preview and status==200 and self.active==job_id:
            job['_response_done']=threading.Event();job['_stop']=False;job['_stream_error']=None
            return StoredClipResponse(self,job_id,chunks(),headers)
        return response

    def archive_response(self,batch_id):
        batch=self.s.batches[batch_id]
        if not batch.get('finished'):raise HTTPException(409,'ZIP จะพร้อมเมื่อคิวจบ')
        path=self.archive_path(batch_id)
        if not path.exists():raise HTTPException(409,'ยังไม่มีไฟล์ที่ดาวน์โหลดสำเร็จ')
        return FileResponse(path,filename='KlipNivo-'+batch_id[:8]+'.zip',media_type='application/zip',headers={'Cache-Control':'no-store'})

    def discard(self,batch_id):
        batch=self.s.batches[batch_id]
        for job_id in batch['ids']:self.plans.pop(job_id,None)
        shutil.rmtree(self.s.ROOT/batch_id,ignore_errors=True)

class ClipResponse(StreamingResponse):
    def __init__(self,queue,job_id):
        self.queue=queue;self.job_id=job_id
        job=queue.s.jobs[job_id]
        self.attempt=job['attempt'];self.generator=queue.stream(job_id);self.done=job['_response_done']
        super().__init__(self.generator,media_type='audio/mpeg' if job['filename'].endswith('.mp3') else 'video/mp4',
                         headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(job['filename']),
                                  'Cache-Control':'no-store','Accept-Ranges':'none','X-Accel-Buffering':'no'})
    async def __call__(self,scope,receive,send):
        started=time.monotonic();job=self.queue.s.jobs[self.job_id]
        job['_response_task']=asyncio.current_task();job['_response_loop']=asyncio.get_running_loop()
        async def tracked(message):
            await asyncio.wait_for(send(message),timeout=self.queue.s.STALL_SECONDS)
            if message['type']=='http.response.body' and message.get('body') and job['attempt']==self.attempt:job['_delivery_touched']=time.monotonic()
        try:
            await super().__call__(scope,receive,tracked)
            self.queue.sent(self.job_id,time.monotonic()-started,self.attempt)
        except BaseException as exc:
            if job['attempt']==self.attempt and not job.get('_stream_error'):job['_stream_error']=self.queue.s.error_code(exc)
            raise
        finally:
            if job['attempt']==self.attempt:
                job['_stop']=True
                process=job.get('_process')
                if process and process.poll() is None:
                    try:os.killpg(process.pid,9)
                    except ProcessLookupError:pass
            try:
                for _ in range(220):
                    try:self.generator.close();break
                    except ValueError:await asyncio.sleep(.1)
                    except BaseException:break
            finally:self.done.set()

class StoredClipResponse(ClipResponse):
    def __init__(self,queue,job_id,chunks,headers):
        self.queue=queue;self.job_id=job_id
        job=queue.s.jobs[job_id];self.attempt=job['attempt'];self.generator=chunks;self.done=job['_response_done']
        StreamingResponse.__init__(self,chunks,headers=headers,media_type='audio/mpeg' if headers['Content-Disposition'].endswith('.mp3') else 'video/mp4')
