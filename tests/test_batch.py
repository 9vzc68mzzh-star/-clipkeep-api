import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE/'backend' if (BASE/'backend').exists() else BASE))
import server
from media import normalize, probe, MediaError

from prepare_fixtures import ensure
FIXTURES = ensure()
URL='https://youtu.be/TST00000001'
class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root_patch=patch.object(server,'ROOT',Path(self.tmp.name));self.root_patch.start()
        self.public_patch=patch.object(server,'PUBLIC_DOWNLOADS',True);self.public_patch.start()
        self.submit_patch=patch.object(server.pool,'submit');self.submit_patch.start()
        self.client=TestClient(server.app)
        server.jobs.clear();server.batches.clear();server.public_requests.clear();server.futures.clear()
    def tearDown(self):
        self.submit_patch.stop();self.public_patch.stop();self.root_patch.stop();self.tmp.cleanup()
    def batch(self,urls=None,**extra):
        response=self.client.post('/api/public/batches',json={'urls':urls or [URL]*50,**extra})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()
    def test_50_links_count_as_one_public_request_and_keep_duplicates(self):
        result=self.batch()
        self.assertEqual(len(result['items']),50)
        self.assertEqual(sum(map(len,server.public_requests.values())),1)
        self.assertEqual(len(set(i['id'] for i in result['items'])),50)
        self.assertEqual(server.pool.submit.call_count,50)
    def test_200_links_and_reject_201_without_truncating(self):
        self.assertEqual(self.batch([URL]*200)['total'],200)
        r=self.client.post('/api/public/batches',json={'urls':[URL]*201})
        self.assertEqual(r.status_code,422)
    def test_bad_link_is_one_failure_and_other_links_still_queue(self):
        data=self.batch([URL,'http://127.0.0.1/private',URL])
        self.assertEqual(data['failed'],1)
        self.assertEqual(data['items'][1]['error_code'],'invalid_link')
        self.assertEqual(server.pool.submit.call_count,2)
    def test_lost_post_retry_is_idempotent(self):
        first=self.batch(request_id='unique-test')
        second=self.batch(request_id='unique-test')
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(server.pool.submit.call_count,50)
    def test_retry_only_failures_preserves_ready(self):
        data=self.batch([URL]*3);ids=[i['id'] for i in data['items']]
        for key,status in zip(ids,['ready','error','ready']): server.jobs[key]['status']=status
        server.batches[data['id']]['finished']=time.time()
        count=server.pool.submit.call_count
        r=self.client.post(f"/api/public/batches/{data['id']}/retry").json()
        self.assertEqual(server.pool.submit.call_count,count+1)
        self.assertEqual([j['status'] for j in r['items']],['ready','queued','ready'])
        self.assertEqual(r['items'][1]['attempt'],2)
    def test_cancel_waiting_jobs(self):
        data=self.batch([URL]*12)
        for future in server.futures.values(): future.cancel.return_value=True
        response=self.client.post(f"/api/public/batches/{data['id']}/cancel").json()
        self.assertTrue(response['finished'])
        self.assertTrue(all(i['status']=='cancelled' for i in response['items']))
    def test_private_batches_cannot_be_read_or_changed_publicly(self):
        data=self.batch([URL]);server.batches[data['id']]['public']=False
        self.assertEqual(self.client.get(f"/api/public/batches/{data['id']}").status_code,404)
        self.assertEqual(self.client.post(f"/api/public/batches/{data['id']}/retry").status_code,404)
    def test_retention_from_completion_not_batch_start(self):
        data=self.batch([URL]);batch=server.batches[data['id']];job=server.jobs[data['items'][0]['id']]
        batch['created']-=server.TTL+10;job['created']-=server.TTL+10;job['status']='ready'
        server.cleanup();self.assertIn(data['id'],server.batches)
        batch['finished']=time.time()-server.TTL-1;server.cleanup()
        self.assertNotIn(data['id'],server.batches)
    def test_full_disk_marks_job_failed_and_settles_batch(self):
        import errno
        data=self.batch([URL]);item=data['items'][0]
        selection=server.Download(url=URL)
        with patch.object(server,'run_attempt',side_effect=lambda job_id,data,url:server.prepare(job_id,data,url)), patch.object(Path,'mkdir',side_effect=OSError(errno.ENOSPC,'No space left on device')):
            server.process(item['id'],selection,URL)
        result=self.client.get('/api/public/batches/'+data['id']).json()
        self.assertTrue(result['finished'])
        self.assertEqual(result['items'][0]['error_code'],'storage_limit')

    def test_real_attachments_unique_names_range_and_streaming_zip(self):
        data=self.batch([URL]*12)
        for i,item in enumerate(data['items'],1):
            path=Path(self.tmp.name)/f'{i}.mp4';path.write_bytes(FIXTURES.joinpath('compatible.mp4').read_bytes())
            server.jobs[item['id']].update(status='ready',file=str(path),filename=f'{i:03d}-คลิป-{item["id"][:8]}.mp4')
        first=self.client.get('/api/files/'+data['items'][0]['id'])
        self.assertIn('attachment;',first.headers['content-disposition'])
        self.assertIn('.mp4',first.headers['content-disposition'])
        self.assertEqual(first.headers['content-type'],'video/mp4')
        part=self.client.get('/api/preview/'+data['items'][0]['id'],headers={'Range':'bytes=0-99'})
        self.assertEqual(part.status_code,206);self.assertEqual(len(part.content),100)
        zipped=self.client.get(data['archive_url'])
        self.assertIn('attachment',zipped.headers['content-disposition'])
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as z:
            self.assertEqual(len(z.namelist()),12);self.assertEqual(len(set(z.namelist())),12)
            self.assertIsNone(z.testzip())
        subset=self.client.get(data['archive_url']+'?items=1,3,7')
        with zipfile.ZipFile(io.BytesIO(subset.content)) as z:self.assertEqual(len(z.namelist()),3)
        self.assertEqual(server.batches[data['id']]['readers'],0)

class MediaTests(unittest.TestCase):
    def convert(self,source,kind='mp4',height=180):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        folder=Path(tmp.name);path=folder/Path(source).name;shutil.copyfile(source,path)
        reports=[]
        output,details=normalize(path,folder,kind,height,lambda:None,lambda *v:reports.append(v),threading.Semaphore(1),20*1024*1024)
        subprocess.run(['ffmpeg','-v','error','-i',str(output),'-f','null','-'],check=True,capture_output=True,timeout=15)
        return output,details,reports
    def test_h264_aac_remux_decodes_picture_and_sound(self):
        output,d,r=self.convert(FIXTURES/'compatible.mp4')
        self.assertEqual((d['video_codec'],d['audio_codec']),('h264','aac'))
        self.assertEqual(probe(output)['streams'][0]['pix_fmt'],'yuv420p')
        content=output.read_bytes();self.assertLess(content.find(b'moov'),content.find(b'mdat'))
    def test_vp9_opus_transcodes_to_h264_aac_and_decodes(self):
        output,d,r=self.convert(FIXTURES/'incompatible.webm')
        self.assertEqual((d['video_codec'],d['audio_codec']),('h264','aac'))
        self.assertTrue(r)
    def test_ffmpeg_na_progress_does_not_abort_a_valid_conversion(self):
        from unittest.mock import patch
        original=subprocess.Popen
        reads=[]
        def started(command, **kwargs):
            if '-progress' not in command:
                return original(command, **kwargs)
            progress=Path(command[command.index('-progress')+1])
            progress.write_text('out_time_us=N/A\nprogress=continue\n')
            return original(command, **kwargs)
        def pending_progress(path, *args, **kwargs):
            reads.append(str(path))
            return 'out_time_us=N/A\nprogress=continue\n'
        with patch('media.subprocess.Popen', side_effect=started), patch.object(Path,'read_text',pending_progress):
            _,details,_=self.convert(FIXTURES/'incompatible.webm')
        self.assertEqual((details['video_codec'],details['audio_codec']),('h264','aac'))
        self.assertTrue(reads)

    def test_mp3_has_playable_audio(self):
        _,d,_=self.convert(FIXTURES/'compatible.mp4','mp3')
        self.assertEqual(d['audio_codec'],'mp3')
    def test_audio_only_cannot_be_mislabeled_mp4(self):
        audio,_,_=self.convert(FIXTURES/'compatible.mp4','mp3')
        with self.assertRaises(MediaError) as e:self.convert(audio)
        self.assertEqual(e.exception.code,'video_unavailable')
    def test_silent_source_stays_video_and_explains_missing_source_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            silent=Path(tmp)/'silent.mp4'
            subprocess.run(['ffmpeg','-v','error','-i',str(FIXTURES/'compatible.mp4'),'-an','-c:v','copy',str(silent)],check=True)
            _,d,_=self.convert(silent)
            self.assertEqual(d['video_codec'],'h264');self.assertIsNone(d['audio_codec']);self.assertTrue(d['warning'])
            with self.assertRaises(MediaError) as e:self.convert(silent,'mp3')
            self.assertEqual(e.exception.code,'audio_unavailable')
    def test_resolution_limit_and_cancellation(self):
        _,d,_=self.convert(FIXTURES/'compatible.mp4',height=90)
        self.assertLessEqual(d['height'],90)
        with tempfile.TemporaryDirectory() as tmp:
            def abort():raise RuntimeError('cancelled')
            with self.assertRaisesRegex(RuntimeError,'cancelled'):
                normalize(FIXTURES/'compatible.mp4',Path(tmp),'mp4',180,abort,lambda *v:None,threading.Semaphore(1),20*1024*1024)

if __name__=='__main__':unittest.main()
