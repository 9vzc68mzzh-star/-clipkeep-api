import io
import os
import json
import sys
import tempfile
import time
import unittest
import zipfile
import subprocess
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'backend' if (BASE/'backend').exists() else BASE))
import server
from sequence_fixture import fixture_worker
from prepare_fixtures import ensure
ensure()
class SequenceTests(unittest.TestCase):
    def test_14_files_order_stall_retry_delivery_and_zip(self):
        stall=float(os.environ.get('KLIPNIVO_STALL_TEST_SECONDS','2.5'))
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'ROOT',Path(tmp)), patch.object(server,'PUBLIC_DOWNLOADS',True), patch.object(server,'prepare_worker',fixture_worker), patch.object(server,'STALL_SECONDS',stall), patch.object(server,'DELIVERY_SECONDS',8):
            server.jobs.clear();server.batches.clear();server.futures.clear();server.public_requests.clear()
            client=TestClient(server.app)
            urls=[f'https://youtu.be/TST{i:08d}' for i in range(1,15)]
            batch=client.post('/api/public/batches',json={'urls':urls,'quality':'720'}).json()
            self.assertEqual(batch['parallel'],1);batch_id=batch['id'];delivered=[];held=False
            deadline=time.monotonic()+110+2*stall
            while time.monotonic()<deadline:
                result=client.get('/api/public/batches/'+batch_id).json()
                active=[j for j in result['items'] if j['status']=='processing' or j['status']=='ready' and j['delivery_status']=='waiting']
                self.assertLessEqual(len(active),1,result)
                for job in result['items']:
                    if job['status']=='ready' and job['delivery_status']=='waiting':
                        if not held:
                            time.sleep(.4);snapshot=client.get('/api/public/batches/'+batch_id).json()
                            self.assertEqual(snapshot['items'][1]['status'],'queued');held=True
                        self.assertTrue(all(server.settled(server.jobs[j['id']]) for j in result['items'] if j['index']<job['index']))
                        wrong=client.post(f'/api/public/batches/{batch_id}/sent',json={'id':job['id'],'attempt':job['attempt']+1})
                        self.assertEqual(wrong.status_code,409)
                        ack=client.post(f'/api/public/batches/{batch_id}/sent',json={'id':job['id'],'attempt':job['attempt']})
                        self.assertEqual(ack.status_code,200)
                        self.assertFalse(server.settled(server.jobs[job['id']]))
                        probe=client.get(job['download_url'],headers={'Range':'bytes=0-1'})
                        self.assertEqual(probe.status_code,206)
                        self.assertFalse(server.settled(server.jobs[job['id']]))
                        response=client.get(job['download_url']);self.assertIn('attachment',response.headers['content-disposition'])
                        path=Path(tmp)/f'download-{job["index"]}.mp4';path.write_bytes(response.content)
                        media=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(path)]))
                        self.assertTrue(any(s['codec_name']=='h264' for s in media['streams']))
                        self.assertTrue(any(s['codec_name']=='aac' for s in media['streams']))
                        subprocess.run(['ffmpeg','-v','error','-i',str(path),'-f','null','-'],check=True,capture_output=True)
                        delivered.append(job['index'])
                if result['finished']:break
                time.sleep(.05)
            self.assertTrue(result['finished'],result)
            self.assertEqual(delivered,[i for i in range(1,15) if i not in (4,10)])
            self.assertEqual((result['ready'],result['failed']),(12,2))
            self.assertEqual(result['items'][3]['error_code'],'stalled')
            for i in (4,7,10):self.assertEqual(result['items'][i-1]['attempt'],2)
            log=[json.loads(line) for line in (Path(tmp)/'sequence.jsonl').read_text().splitlines()]
            expected=[]
            for i in range(1,15):expected.extend([i,i] if i in (4,7,10) else [i])
            self.assertEqual([e['index'] for e in log if e['kind']=='start'],expected)
            hangs=[e for e in log if e['kind']=='stuck-encoder']
            self.assertEqual(len(hangs),2)
            self.assertGreaterEqual(hangs[1]['time']-hangs[0]['time'],stall)
            for event in hangs:
                stat=Path(f'/proc/{event["pid"]}/stat')
                if stat.exists():self.assertEqual(stat.read_text().split()[2],'Z','stuck encoder still running')
            with zipfile.ZipFile(io.BytesIO(client.get(result['archive_url']).content)) as zipped:
                self.assertEqual(len(zipped.namelist()),12);self.assertIsNone(zipped.testzip())
                self.assertEqual(sorted(int(n.split('-')[0]) for n in zipped.namelist()),delivered)
            (Path(tmp)/'allow-retry').touch();client.post(f'/api/public/batches/{batch_id}/retry')
            for _ in range(250):
                retry=client.get('/api/public/batches/'+batch_id).json()
                for job in retry['items']:
                    if job['status']=='ready' and job['delivery_status']=='waiting':client.get(job['download_url'])
                if retry['finished']:break
                time.sleep(.05)
            self.assertTrue(retry['finished']);self.assertEqual((retry['ready'],retry['failed']),(14,0))
            with zipfile.ZipFile(io.BytesIO(client.get(retry['archive_url']).content)) as zipped:self.assertEqual(len(zipped.namelist()),14)
            final_log=[json.loads(line) for line in (Path(tmp)/'sequence.jsonl').read_text().splitlines()]
            self.assertEqual([e['index'] for e in final_log if e['kind']=='start'],expected+[4,10])
            print(f'SEQUENTIAL PASS: 14 links; 12 successes/2 failures, one active, ordered receipt gate, {stall}s stall timeout + one retry, no live orphan encoder, H.264/AAC decode, ZIP=12; manual retry ZIP=14.',flush=True)
if __name__=='__main__':unittest.main()
