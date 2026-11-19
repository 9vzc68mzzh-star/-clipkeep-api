import importlib.util
import os
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
SERVER=BASE/"backend" if (BASE/"backend").exists() else BASE
sys.path.insert(0, str(SERVER))
import time
import unittest
from unittest.mock import patch, MagicMock
from urllib.request import Request
from fastapi.testclient import TestClient
os.environ['CLIPKEEP_API_KEY'] = 'test-key-only'
os.environ['CORS_ORIGINS'] = 'https://frontend.example'
os.environ['PUBLIC_SITE_ORIGIN'] = 'https://static-frontend.example'
spec = importlib.util.spec_from_file_location('server', SERVER/'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
client = TestClient(server.app)
AUTH = {'Authorization': 'Bearer test-key-only'}

class BackendTests(unittest.TestCase):
    def setUp(self):
        # These legacy tests exercise HTTP/auth contracts with byte-only fake media.
        # Real codec inspection/conversion is covered separately in test_batch.py.
        self.normalizer = patch.object(server, 'normalize', side_effect=lambda source, *args: (source, {"height": None}))
        self.normalizer.start()
        self.attempt=patch.object(server,'run_attempt',side_effect=lambda job_id,data,url:server.prepare(job_id,data,url))
        self.attempt.start()

    def tearDown(self):
        self.attempt.stop()
        self.normalizer.stop()
        with server.lock:
            ids = list(server.jobs)
            server.jobs.clear()
            server.public_requests.clear()
        for job_id in ids:
            server.shutil.rmtree(server.ROOT / job_id, ignore_errors=True)

    def test_auth_and_health(self):
        self.assertEqual(client.get('/api/health').json()['service'], 'clipkeep')
        self.assertEqual(client.get('/api/auth-check').status_code, 401)
        self.assertEqual(client.get('/api/auth-check', headers=AUTH).status_code, 200)

    def test_legacy_homepage_redirects_to_the_static_site(self):
        response = client.get('/', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['location'], 'https://static-frontend.example/')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        with patch.object(server, 'PUBLIC_SITE_ORIGIN', ''):
            response = client.get('/', follow_redirects=False)
            self.assertEqual(response.status_code, 200)
            self.assertIn('KlipNivo', response.text)

    def test_separate_static_origin_is_authorized_for_api_requests(self):
        response = client.options('/api/public/jobs', headers={'Origin':'https://static-frontend.example','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['access-control-allow-origin'], 'https://static-frontend.example')

    def test_public_routes_require_explicit_public_mode(self):
        with patch.object(server, 'PUBLIC_DOWNLOADS', False):
            self.assertFalse(client.get('/api/health').json()['public_downloads'])
            self.assertEqual(client.post('/api/public/jobs', json={'url':'https://youtu.be/BaW_jenozKc'}).status_code, 403)
            self.assertEqual(client.get('/api/public/jobs/unknown').status_code, 403)

    def test_public_job_needs_no_owner_key_but_cannot_read_private_jobs(self):
        class FakeDownloader:
            def __init__(self, opts): self.opts = opts
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, url, download):
                server.Path(self.opts['outtmpl'].replace('%(ext)s','mp4')).write_bytes(b'public-fixture')
                return {'title':'Public fixture','duration':5,'height':360}
        with patch.object(server, 'PUBLIC_DOWNLOADS', True), patch.object(server.yt_dlp, 'YoutubeDL', FakeDownloader):
            response = client.post('/api/public/jobs', json={'url':'https://youtu.be/BaW_jenozKc'})
            self.assertEqual(response.status_code, 200)
            job_id = response.json()['id']
            for _ in range(50):
                response = client.get('/api/public/jobs/' + job_id)
                job = response.json()
                if job['status'] in ('ready','error'): break
                time.sleep(.02)
            self.assertEqual(job['status'], 'ready', job)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertNotIn('public', job)
            self.assertNotIn('file', job)
            self.assertEqual(client.get(job['download_url']).content, b'public-fixture')
            server.jobs['private-fixture'] = {'status':'queued','created':time.time(),'public':False}
            self.assertEqual(client.get('/api/public/jobs/private-fixture').status_code, 404)
            self.assertEqual(client.get('/api/jobs/' + job_id).status_code, 401)

    def test_public_requests_keep_url_validation_and_queue_limits(self):
        with patch.object(server, 'PUBLIC_DOWNLOADS', True), patch.object(server.pool, 'submit'):
            self.assertEqual(client.post('/api/public/jobs', json={'url':'http://127.0.0.1/'}).status_code, 400)
            for _ in range(4):
                self.assertEqual(client.post('/api/public/jobs', json={'url':'https://youtu.be/BaW_jenozKc'}).status_code, 200)
            self.assertEqual(client.post('/api/public/jobs', json={'url':'https://youtu.be/BaW_jenozKc'}).status_code, 429)

    def test_public_rate_limit_expires(self):
        with patch.object(server, 'PUBLIC_DOWNLOADS', True), patch.object(server.pool, 'submit'):
            for _ in range(server.PUBLIC_RATE_LIMIT):
                response = client.post('/api/public/jobs', json={'url':'https://youtu.be/BaW_jenozKc'})
                self.assertEqual(response.status_code, 200)
                server.jobs[response.json()['id']]['status'] = 'ready'
            self.assertEqual(client.post('/api/public/jobs', json={'url':'https://youtu.be/BaW_jenozKc'}).status_code, 429)
            for key in server.public_requests:
                server.public_requests[key] = [t-server.PUBLIC_RATE_WINDOW-1 for t in server.public_requests[key]]
            self.assertEqual(client.post('/api/public/jobs', json={'url':'https://youtu.be/BaW_jenozKc'}).status_code, 200)

    def test_reject_untrusted_urls(self):
        for url in ['http://127.0.0.1/', 'https://youtube.com.evil.test/watch?v=BaW_jenozKc', 'https://youtube.com@evil.test/watch?v=BaW_jenozKc', 'https://youtube.com/playlist?list=x', 'file:///etc/passwd', 'https://facebook.com/']:
            response = client.post('/api/jobs', headers=AUTH, json={'url':url})
            self.assertEqual(response.status_code, 400, url)

    def test_job_and_file_contract(self):
        class FakeDownloader:
            def __init__(self, opts): self.opts = opts
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, url, download):
                from pathlib import Path
                Path(self.opts['outtmpl'].replace('%(ext)s', 'mp4')).write_bytes(b'fixture-video')
                return {'title':'A test video', 'duration':5, 'height':720}
        with patch.object(server.yt_dlp, 'YoutubeDL', FakeDownloader):
            response = client.post('/api/jobs', headers=AUTH, json={'url':'https://youtu.be/BaW_jenozKc', 'quality':'720'})
            self.assertEqual(response.status_code, 200)
            job_id = response.json()['id']
            for _ in range(50):
                job = client.get('/api/jobs/' + job_id, headers=AUTH).json()
                if job['status'] == 'ready': break
                time.sleep(.02)
            self.assertEqual(job['status'], 'ready')
            self.assertNotIn('file', job)
            file = client.get(job['download_url'])
            self.assertEqual(file.content, b'fixture-video')
            self.assertIn('attachment', file.headers['content-disposition'])
            self.assertEqual(file.headers['cache-control'], 'no-store')
            server.jobs[job_id]['finished'] -= server.TTL + 1
            self.assertEqual(client.get(job['download_url']).status_code, 404)

    def test_cors(self):
        r = client.options('/api/jobs', headers={'Origin':'https://frontend.example', 'Access-Control-Request-Method':'POST', 'Access-Control-Request-Headers':'authorization,content-type'})
        self.assertEqual(r.headers['access-control-allow-origin'], 'https://frontend.example')
        r = client.options('/api/jobs', headers={'Origin':'https://untrusted.example', 'Access-Control-Request-Method':'POST'})
        self.assertNotIn('access-control-allow-origin', r.headers)

    def test_unknown_and_unsupported_format(self):
        self.assertEqual(client.get('/api/jobs/missing', headers=AUTH).status_code, 404)
        r = client.post('/api/jobs', headers=AUTH, json={'url':'https://youtu.be/BaW_jenozKc', 'format':'exe'})
        self.assertEqual(r.status_code, 400)

    def test_facebook_share_resolves_to_supported_extractor(self):
        targets = [
            ('https://www.facebook.com/share/v/example/', 'https://www.facebook.com/watch/?v=123456789'),
            ('https://www.facebook.com/share/r/example/', 'https://www.facebook.com/reel/123456789/'),
            ('https://fb.watch/example/', 'https://www.facebook.com/example/videos/123456789/'),
        ]
        for shared, direct in targets:
            with self.subTest(shared=shared):
                response = MagicMock()
                response.__enter__.return_value.geturl.return_value = direct
                opener = MagicMock()
                opener.open.return_value = response
                with patch('source_utils.build_opener', return_value=opener):
                    resolved = server.resolve_facebook_share(shared)
                self.assertEqual(resolved, direct)
                # Regression: the real restricted yt-dlp configuration has no
                # extractor for share links; it must receive the resolved URL.
                with server.yt_dlp.YoutubeDL({'quiet': True, 'allowed_extractors': ['youtube', 'youtube:tab', 'facebook', 'facebook:reel']}) as ydl:
                    self.assertFalse(any(ie.suitable(shared) for ie in ydl._ies.values()))
                    self.assertTrue(any(ie.suitable(resolved) for ie in ydl._ies.values()))

    def test_share_redirect_cannot_follow_external_or_local_hosts(self):
        handler = server.FacebookRedirectHandler()
        request = Request('https://www.facebook.com/share/v/example/')
        for target in ('https://evil.test/video', 'http://127.0.0.1/', 'https://facebook.com.evil.test/reel/123', 'file:///etc/passwd', 'https://www.facebook.com:8443/reel/123', 'https://user:pass@www.facebook.com/reel/123'):
            with self.subTest(target=target), self.assertRaises(server.DownloadFailure):
                handler.redirect_request(request, None, 302, 'Found', {}, target)
        redirected = handler.redirect_request(request, None, 302, 'Found', {}, 'https://www.facebook.com/reel/123')
        self.assertEqual(redirected.full_url, 'https://www.facebook.com/reel/123')

    def test_share_login_or_unresolved_pages_show_specific_error(self):
        for target in ('https://www.facebook.com/login/', 'https://www.facebook.com/share/v/example/', 'https://evil.test/reel/123', 'https://www.youtube.com/watch?v=BaW_jenozKc'):
            response = MagicMock()
            response.__enter__.return_value.geturl.return_value = target
            with patch('source_utils.build_opener') as opener:
                opener.return_value.open.return_value = response
                with self.subTest(target=target), self.assertRaises(server.DownloadFailure) as raised:
                    server.resolve_facebook_share('https://www.facebook.com/share/v/example/')
                self.assertEqual(raised.exception.code, 'share_link')

    def test_share_download_job_uses_resolved_video(self):
        expected = 'https://www.facebook.com/reel/123456789/'
        class FakeDownloader:
            def __init__(self, opts): self.opts = opts
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, url, download):
                if url != expected:
                    raise AssertionError('The share URL was not resolved')
                server.Path(self.opts['outtmpl'].replace('%(ext)s', 'mp4')).write_bytes(b'facebook-fixture')
                self.opts['match_filter']({'duration':3832.361})
                return {'title':'Facebook fixture', 'duration':3832.361, 'height':720}
        response = MagicMock()
        response.__enter__.return_value.geturl.return_value = expected
        with patch('source_utils.build_opener') as opener, patch.object(server.yt_dlp, 'YoutubeDL', FakeDownloader):
            opener.return_value.open.return_value = response
            result = client.post('/api/jobs', headers=AUTH, json={'url':'https://www.facebook.com/share/v/example/'})
            job_id = result.json()['id']
            for _ in range(50):
                job = client.get('/api/jobs/' + job_id, headers=AUTH).json()
                if job['status'] in ('ready', 'error'): break
                time.sleep(.02)
            self.assertEqual(job['status'], 'ready', job)
            self.assertEqual(job['duration'], 3832.361)
            self.assertEqual(client.get(job['download_url']).content, b'facebook-fixture')

    def test_failure_messages_identify_the_actual_limit(self):
        for code in ('file_too_large', 'duration_limit', 'live_video'):
            class FailingDownloader:
                def __init__(self, opts): self.opts = opts
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def extract_info(self, url, download):
                    if code == 'file_too_large':
                        self.opts['logger'].debug('[download] File is larger than max-filesize (536870913 bytes > 536870912 bytes). Aborting.')
                    if code == 'duration_limit':
                        self.opts['match_filter']({'duration':server.MAX_VIDEO_SECONDS + 1})
                    if code == 'live_video':
                        self.opts['match_filter']({'is_live':True})
            with self.subTest(code=code), patch.object(server.yt_dlp, 'YoutubeDL', FailingDownloader):
                result = client.post('/api/jobs', headers=AUTH, json={'url':'https://youtu.be/BaW_jenozKc'})
                job_id = result.json()['id']
                for _ in range(50):
                    job = client.get('/api/jobs/' + job_id, headers=AUTH).json()
                    if job['status'] == 'error': break
                    time.sleep(.02)
                self.assertEqual(job['error_code'], code)
                self.assertEqual(job['error'], server.ERROR_MESSAGES[code])
                if code != 'file_too_large':
                    self.assertNotIn('512 MB', job['error'])
                self.assertFalse((server.ROOT / job_id).exists())

    def test_platform_errors_are_classified_without_leaking_details(self):
        cases = {
            'ERROR: Unsupported URL: https://www.facebook.com/share/v/example/': 'share_link',
            'ERROR: HTTP Error 403: Forbidden': 'access_restricted',
            'ERROR: HTTP Error 429: Too Many Requests': 'rate_limited',
            'ERROR: Requested format is not available': 'format_unavailable',
            'ERROR: Postprocessing: WARNING: unable to obtain file audio codec with ffprobe': 'audio_unavailable',
            'ERROR: Cannot parse data': 'extractor_error',
            'ERROR: Unable to download webpage: timed out': 'network_error',
        }
        for detail, code in cases.items():
            with self.subTest(detail=detail):
                self.assertEqual(server.error_code(server.yt_dlp.utils.DownloadError(detail)), code)
        class FailingDownloader:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, url, download):
                raise server.yt_dlp.utils.DownloadError('Cannot parse data https://example.test/?signed=secret test-key-only')
        with patch.object(server.yt_dlp, 'YoutubeDL', FailingDownloader), self.assertLogs('clipkeep') as logs:
            result = client.post('/api/jobs', headers=AUTH, json={'url':'https://youtu.be/BaW_jenozKc'})
            job_id = result.json()['id']
            for _ in range(50):
                job = client.get('/api/jobs/' + job_id, headers=AUTH).json()
                if job['status'] == 'error': break
                time.sleep(.02)
            self.assertEqual(job['error_code'], 'extractor_error')
        self.assertNotIn('test-key-only', ''.join(logs.output))
        self.assertNotIn('signed=secret', ''.join(logs.output))

    def test_facebook_sd_without_height_can_download_below_known_hd(self):
        real_downloader = server.yt_dlp.YoutubeDL
        class FixtureDownloader:
            def __init__(self, opts): self.opts = opts
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def extract_info(self, url, download):
                info = {'id':'123', 'title':'Facebook SD fixture', 'duration':3832.361, 'formats':[
                    {'format_id':'sd','url':'https://media.example/sd.mp4','ext':'mp4','quality':-3},
                    {'format_id':'hd','url':'https://media.example/hd.mp4','ext':'mp4','quality':-2},
                    {'format_id':'dash-video','url':'https://media.example/video.mp4','ext':'mp4','height':720,'vcodec':'h264','acodec':'none'},
                    {'format_id':'dash-audio','url':'https://media.example/audio.m4a','ext':'m4a','vcodec':'none','acodec':'aac'},
                ]}
                output = self.opts['outtmpl']
                with real_downloader(self.opts) as ydl:
                    result = ydl.process_video_result(info, download=False)
                if result['format_id'] != 'sd':
                    raise AssertionError('Expected unlabelled SD fallback at 480p')
                server.Path(output.replace('%(ext)s','mp4')).write_bytes(b'sd-fixture')
                return result
        with patch.object(server.yt_dlp, 'YoutubeDL', FixtureDownloader):
            result = client.post('/api/jobs', headers=AUTH, json={'url':'https://www.facebook.com/reel/123/', 'quality':'480'})
            job_id = result.json()['id']
            for _ in range(50):
                job = client.get('/api/jobs/' + job_id, headers=AUTH).json()
                if job['status'] in ('ready','error'): break
                time.sleep(.02)
            self.assertEqual(job['status'], 'ready', job)
            self.assertIsNone(job['height'])
            self.assertEqual(client.get(job['download_url']).content, b'sd-fixture')

if __name__ == '__main__':
    unittest.main()
