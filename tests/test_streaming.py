import functools
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
BASE=Path(__file__).resolve().parents[1];sys.path.insert(0,str(BASE/'backend' if (BASE/'backend').exists() else BASE))
import server
from fast_media import choose_plan,command_for,stream_bytes
from media import MediaError
from prepare_fixtures import ensure
from fast_fixture import fixture_plan

def video_packets(path):
    data=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_packets','-show_data_hash','sha256','-of','json',str(path)]))
    return [packet['data_hash'] for packet in data['packets']]

class StreamingTests(unittest.TestCase):
    def test_10_streams_no_video_encode_one_prefetch_timings_and_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);inputs=root/'inputs';inputs.mkdir();outputs=root/'jobs';outputs.mkdir()
            source=ensure()/'compatible.mp4'
            for name,opts in [('copy.mp4',['-c','copy']),('mp3-audio.mp4',['-c:v','copy','-c:a','libmp3lame']),('video.mp4',['-an','-c:v','copy'])]:
                subprocess.run(['ffmpeg','-v','error','-i',str(source),*opts,'-movflags','+faststart',str(inputs/name)],check=True)
            subprocess.run(['ffmpeg','-v','error','-i',str(source),'-vn','-c:a','libopus',str(inputs/'audio.webm')],check=True)
            upstream_done={}
            class Handler(SimpleHTTPRequestHandler):
                def log_message(self,*args):pass
                def copyfile(self,src,out):
                    while chunk:=src.read(4096):
                        out.write(chunk);out.flush();time.sleep(.015)
                    index=int(self.path.split('clip=')[-1]);upstream_done.setdefault(index,[]).append(time.monotonic())
            upstream=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(inputs)))
            thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
            target=functools.partial(fixture_plan,base_url=f'http://127.0.0.1:{upstream.server_port}',root=str(inputs))
            first_body={}
            class Capture:
                async def __call__(self,scope,receive,send):
                    async def observe(message):
                        if scope.get('path','').startswith('/api/files/') and message['type']=='http.response.body' and message.get('body'):
                            first_body.setdefault(scope['path'].split('/')[-1],time.monotonic())
                        await send(message)
                    await server.app(scope,receive,observe)
            try:
                with patch.object(server,'ROOT',outputs),patch.object(server,'PUBLIC_DOWNLOADS',True),patch.object(server.streamer,'plan_target',target):
                    server.jobs.clear();server.batches.clear();server.futures.clear();server.public_requests.clear();server.streamer.plans.clear()
                    client=TestClient(Capture());urls=[f'https://www.youtube.com/watch?v=TST{i:08d}' for i in range(1,11)]
                    response=client.post('/api/public/batches',json={'urls':urls,'streaming':True})
                    self.assertEqual(response.status_code,200,response.text);batch=response.json();batch_id=batch['id'];order=[]
                    deadline=time.monotonic()+70
                    while time.monotonic()<deadline:
                        batch=client.get('/api/public/batches/'+batch_id).json()
                        active=[job for job in batch['items'] if job.get('can_download') or job.get('delivery_status')=='streaming']
                        self.assertLessEqual(len(active),1)
                        self.assertLessEqual(sum(job.get('prefetched',False) and not server.settled(server.jobs[job['id']]) for job in batch['items']),1)
                        for job in batch['items']:
                            if job.get('can_download'):
                                # Response starts while the source/muxer is still producing bytes.
                                result=client.get(job['download_url']);self.assertEqual(result.status_code,200)
                                self.assertIn('attachment',result.headers['content-disposition'])
                                path=root/f'output-{job["index"]}.mp4';path.write_bytes(result.content)
                                self.assertEqual(video_packets(path),video_packets(inputs/'copy.mp4'))
                                media=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(path)]))
                                self.assertTrue(any(s['codec_name']=='h264' for s in media['streams']))
                                self.assertTrue(any(s['codec_name']=='aac' and s.get('profile')=='LC' for s in media['streams']))
                                subprocess.run(['ffmpeg','-v','error','-i',str(path),'-f','null','-'],capture_output=True,check=True)
                                self.assertLess(first_body[job['id']],max(upstream_done[job['index']]))
                                self.assertFalse((outputs/job['id']).exists())
                                order.append(job['index'])
                        if batch['finished']:break
                        time.sleep(.03)
                    self.assertTrue(batch['finished'],batch);self.assertEqual(order,list(range(1,11)))
                    self.assertEqual((batch['ready'],batch['failed']),(10,0))
                    items=batch['items']
                    for previous,current in zip(items,items[1:]):
                        prev=server.jobs[previous['id']];cur=server.jobs[current['id']]
                        self.assertLess(cur['_started'],prev['_send_started']+prev['timings']['send_s'])
                        if current['index']>2:
                            before=server.jobs[items[current['index']-3]['id']]
                            self.assertGreaterEqual(cur['_started'],server.jobs[previous['id']]['_send_started'])
                    archive=client.get(batch['archive_url'])
                    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
                        self.assertEqual(len(zipped.namelist()),10);self.assertIsNone(zipped.testzip())
                        for item in items:self.assertEqual(zipped.read(item['filename']), (root/f'output-{item["index"]}.mp4').read_bytes())
                    # Completed clips are served directly from ZIP offsets, with ranges.
                    last=items[-1];part=client.get(last['preview_url'],headers={'Range':'bytes=0-99'})
                    self.assertEqual(part.status_code,206);self.assertEqual(len(part.content),100)
                    rows=[{'clip':j['index'],'mode':j['mode'],**j['timings']} for j in items]
                    out=Path(os.environ.get('KLIPNIVO_TEST_DIR','/tmp/klipnivo-tests'));out.mkdir(exist_ok=True,parents=True)
                    (out/'stream-timings.json').write_text(json.dumps(rows,indent=2))
                    audio_batch=client.post('/api/public/batches',json={'urls':[urls[0]],'streaming':True,'format':'mp3'}).json()
                    for _ in range(200):
                        audio_batch=client.get('/api/public/batches/'+audio_batch['id']).json()
                        current=audio_batch['items'][0]
                        if current.get('can_download'):
                            audio_result=client.get(current['download_url']);audio_path=root/'audio.mp3';audio_path.write_bytes(audio_result.content)
                            audio_info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(audio_path)]))
                            self.assertTrue(any(stream['codec_name']=='mp3' for stream in audio_info['streams']))
                            break
                        time.sleep(.03)
                    else:self.fail('streaming MP3 did not start')
                    print('STREAM PASS: 10 ordered files; bytes arrive before production completes; exactly one look-ahead; video packet hashes unchanged; all H.264/AAC LC decode; ZIP=10; no individual temp movies.',flush=True)
            finally:upstream.shutdown();upstream.server_close()

    def test_stream_failures_timeout_retry_zip_rollback_and_manual_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);inputs=root/'inputs';inputs.mkdir();jobs=root/'jobs';jobs.mkdir()
            payload=(ensure()/'compatible.mp4').read_bytes();(inputs/'copy.mp4').write_bytes(payload)
            target=functools.partial(fixture_plan,base_url='http://unused.test',root=str(inputs))
            def bytes_with_fault(plan,check,stats,on_process):
                index=plan['fixture_index'];attempt=plan['fixture_attempt']
                yield payload[:4096]
                if index==2 and attempt==1:raise MediaError('network_error')
                if index==3 and attempt<=2:
                    while True:time.sleep(.05);check()
                check();yield payload[4096:];stats.update(download_s=.01,merge_s=0,mux_cpu_s=0)
            with patch.object(server,'ROOT',jobs),patch.object(server,'PUBLIC_DOWNLOADS',True),patch.object(server,'STALL_SECONDS',1.2),patch.object(server,'DELIVERY_SECONDS',4),patch.object(server.streamer,'plan_target',target),patch.object(server.streamer,'byte_source',bytes_with_fault):
                server.jobs.clear();server.batches.clear();server.futures.clear();server.public_requests.clear();server.streamer.plans.clear()
                client=TestClient(server.app);result=client.post('/api/public/batches',json={'urls':[f'https://youtu.be/TST{i:08d}' for i in range(1,5)],'streaming':True}).json()
                batch_id=result['id'];seen=set();errors=0;deadline=time.monotonic()+25
                while time.monotonic()<deadline:
                    result=client.get('/api/public/batches/'+batch_id).json()
                    for item in result['items']:
                        key=(item['id'],item['attempt'])
                        if item.get('can_download') and key not in seen:
                            seen.add(key)
                            try:client.get(item['download_url'])
                            except Exception:errors+=1
                    if result['finished']:break
                    time.sleep(.03)
                self.assertTrue(result['finished'],result);self.assertEqual((result['ready'],result['failed']),(3,1));self.assertEqual(errors,3)
                self.assertEqual(result['items'][1]['attempt'],2);self.assertEqual(result['items'][2]['attempt'],2)
                self.assertEqual(result['items'][2]['error_code'],'stalled')
                with zipfile.ZipFile(io.BytesIO(client.get(result['archive_url']).content)) as archive:
                    self.assertEqual(len(archive.namelist()),3);self.assertIsNone(archive.testzip())
                    self.assertTrue(all(archive.read(name)==payload for name in archive.namelist()))
                client.post('/api/public/batches/'+batch_id+'/retry')
                for _ in range(200):
                    result=client.get('/api/public/batches/'+batch_id).json()
                    for item in result['items']:
                        if item.get('can_download'):client.get(item['download_url'])
                    if result['finished']:break
                    time.sleep(.03)
                self.assertTrue(result['finished']);self.assertEqual((result['ready'],result['failed']),(4,0))
                with zipfile.ZipFile(io.BytesIO(client.get(result['archive_url']).content)) as archive:
                    self.assertEqual(len(archive.namelist()),4);self.assertIsNone(archive.testzip())

    def test_share_resolution_is_reused_after_an_extraction_failure(self):
        from unittest.mock import MagicMock
        from fast_media import plan_worker
        events=[]
        class Connection:
            def send(self,event):events.append(event)
            def close(self):pass
        calls=[]
        class Downloader:
            def __init__(self,opts):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def extract_info(self,url,download=False):
                calls.append(url)
                if len(calls)==1:raise RuntimeError('connection reset')
                return {'title':'Cached share','duration':2,'formats':[{'url':'https://video.xx.fbcdn.net/clip.mp4','format_id':'hd','ext':'mp4','vcodec':'avc1.64001f','acodec':'mp4a.40.2','width':1280,'height':720}]}
        with patch('fast_media.os.setsid'),patch('fast_media.resolve_facebook_share',return_value='https://www.facebook.com/reel/123/') as resolve,patch('fast_media.yt_dlp.YoutubeDL',Downloader):
            initial={'url':'https://www.facebook.com/share/v/example/'}
            plan_worker('fixture',{'format':'mp4','quality':'720'},initial,Connection())
            resolved=next(event['fields']['resolved_url'] for event in events if event['kind']=='update')
            plan_worker('fixture',{'format':'mp4','quality':'720'},{**initial,'resolved_url':resolved},Connection())
            self.assertEqual(resolve.call_count,1);self.assertEqual(events[-1]['kind'],'result')
            self.assertEqual(events[-1]['fields']['plan']['mode'],'direct')

    def test_eleven_abandoned_or_replaced_clips_do_not_hold_the_next_batch(self):
        for reason in ('replaced','inactive'):
            with self.subTest(reason=reason),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'copy.mp4').write_bytes((ensure()/'compatible.mp4').read_bytes())
                target=functools.partial(fixture_plan,base_url='http://127.0.0.1:1',root=str(root))
                with patch.object(server,'ROOT',root),patch.object(server,'PUBLIC_DOWNLOADS',True),patch.object(server.streamer,'plan_target',target),TestClient(server.app) as client:
                    server.jobs.clear();server.batches.clear();server.public_requests.clear()
                    def create(token):
                        response=client.post('/api/public/batches',json={'streaming':True,'urls':['https://youtu.be/TST00000001']*11,'client_token':token})
                        self.assertEqual(response.status_code,200,response.text);return response.json()
                    def until_active(batch):
                        deadline=time.monotonic()+3
                        while time.monotonic()<deadline:
                            data=client.get('/api/public/batches/'+batch['id']).json()
                            if data['items'][0].get('can_download'):return data
                            time.sleep(.02)
                        self.fail('Replacement batch never became active')
                    old=create('a'*40);until_active(old)
                    if reason=='inactive':server.batches[old['id']]['last_seen']=time.time()-server.CLIENT_IDLE_SECONDS-1
                    started=time.monotonic();new=create('a'*40 if reason=='replaced' else 'b'*40);until_active(new)
                    self.assertLess(time.monotonic()-started,2)
                    previous=client.get('/api/public/batches/'+old['id']).json()
                    self.assertTrue(previous['finished']);self.assertEqual(previous['cancel_reason'],reason)
                    self.assertTrue(all(job['status']=='cancelled' for job in previous['items']))
                    client.post('/api/public/batches/'+new['id']+'/cancel')
                    server.batches[new['id']]['future'].result(timeout=2)

    def test_selection_prefers_complete_mp4_and_commands_never_encode_video(self):
        info={'formats':[{'format_id':'adaptive','url':'https://video.xx.fbcdn.net/v.mp4','ext':'mp4','vcodec':'vp9','acodec':'none','height':640},
                         {'format_id':'hd','url':'https://video.xx.fbcdn.net/hd.mp4','ext':'mp4'}]}
        details={'video_codec':'h264','pix_fmt':'yuv420p','width':720,'height':1280,'audio_codec':'aac','audio_profile':'LC','size':100}
        plan=choose_plan(info,'mp4',720,lambda source:details)
        self.assertEqual(plan['mode'],'direct');self.assertEqual(plan['source_format'],'hd')
        details['audio_profile']='HE-AAC';self.assertEqual(choose_plan(info,'mp4',720,lambda source:details)['mode'],'direct')
        details['audio_codec']='opus';plan=choose_plan(info,'mp4',720,lambda source:details)
        self.assertEqual(plan['mode'],'audio_aac');command=command_for(plan)
        self.assertEqual(command[command.index('-c:v')+1],'copy');self.assertNotIn('libx264',command)
        self.assertEqual(command[command.index('-c:a')+1],'aac');self.assertIn('pipe:1',command)
        details['video_codec']='vp9'
        with self.assertRaises(MediaError):choose_plan(info,'mp4',720,lambda source:details)

if __name__=='__main__':unittest.main()
