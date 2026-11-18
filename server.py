"""ClipKeep API. Run behind HTTPS. Requires CLIPKEEP_API_KEY and CORS_ORIGINS."""
import hmac
import logging
import os
import re
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request as URLRequest, build_opener
from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import yt_dlp
from yt_dlp.extractor.facebook import FacebookIE, FacebookReelIE

API_KEY = os.environ.get('CLIPKEEP_API_KEY', '')
ORIGINS = [s.strip() for s in os.environ.get('CORS_ORIGINS', '').split(',') if s.strip()]
ROOT = Path('/tmp/clipkeep')
ROOT.mkdir(exist_ok=True)
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_VIDEO_SECONDS = 2 * 60 * 60
TTL = 3600
FACEBOOK_HOSTS = ('facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com', 'fb.watch')
ERROR_MESSAGES = {
    'share_link': 'เปิดลิงก์แชร์ Facebook ไม่สำเร็จ กรุณาเปิดคลิปใน Facebook แล้วคัดลอกลิงก์วิดีโอโดยตรงมาลองอีกครั้ง',
    'file_too_large': 'ไฟล์เกินขนาดสูงสุด 512 MB กรุณาเลือกคุณภาพ 720p หรือ 480p แล้วลองใหม่',
    'duration_limit': 'คลิปยาวเกิน 2 ชั่วโมง กรุณาเลือกคลิปที่สั้นกว่า',
    'live_video': 'ยังไม่รองรับคลิปที่กำลังถ่ายทอดสด กรุณารอให้การถ่ายทอดจบก่อน',
    'time_limit': 'การดาวน์โหลดใช้เวลานานเกินกำหนด กรุณาลองใหม่หรือเลือกคุณภาพต่ำลง',
    'access_restricted': 'Facebook หรือ YouTube ปฏิเสธการเข้าถึงคลิปจากเซิร์ฟเวอร์ คลิปอาจต้องเข้าสู่ระบบหรือมีข้อจำกัดการรับชม',
    'rate_limited': 'แพลตฟอร์มจำกัดจำนวนคำขอชั่วคราว กรุณารอสักครู่แล้วลองใหม่',
    'format_unavailable': 'คลิปนี้ไม่มีไฟล์ในคุณภาพที่เลือก กรุณาลองคุณภาพอื่น',
    'extractor_error': 'ตัวดาวน์โหลดยังอ่านข้อมูลคลิปนี้ไม่ได้ กรุณาลองลิงก์วิดีโอโดยตรงหรืออัปเดตเซิร์ฟเวอร์',
    'network_error': 'เซิร์ฟเวอร์ติดต่อแพลตฟอร์มวิดีโอไม่สำเร็จ กรุณาลองใหม่ภายหลัง',
    'download_failed': 'ดาวน์โหลดคลิปนี้ไม่สำเร็จ กรุณาลองคลิปสาธารณะอื่น หากยังไม่ได้ให้ตรวจ Logs ของเซิร์ฟเวอร์',
}
logger = logging.getLogger('clipkeep')
jobs = {}
lock = threading.Lock()
pool = ThreadPoolExecutor(max_workers=2)
app = FastAPI(title='ClipKeep', docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=['GET', 'POST'], allow_headers=['Content-Type', 'Authorization'])

class Download(BaseModel):
    url: str = Field(max_length=2000)
    format: str = 'mp4'
    quality: str = '1080'

class DownloadFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def error_code(exc):
    if isinstance(exc, DownloadFailure):
        return exc.code
    detail = str(exc).lower()
    if 'file_too_large' in detail or 'larger than max-filesize' in detail:
        return 'file_too_large'
    if 'duration_limit' in detail:
        return 'duration_limit'
    if 'time_limit' in detail:
        return 'time_limit'
    if isinstance(exc, HTTPError) and exc.code == 429 or 'http error 429' in detail:
        return 'rate_limited'
    if isinstance(exc, HTTPError) and exc.code in (401, 403) or any(s in detail for s in ('http error 403', 'http error 401', 'login required', 'log in', 'sign in', 'private video', 'video unavailable')):
        return 'access_restricted'
    if 'unsupported url' in detail:
        return 'share_link'
    if 'requested format is not available' in detail:
        return 'format_unavailable'
    if any(s in detail for s in ('cannot parse data', 'unable to extract', 'no video formats found')):
        return 'extractor_error'
    if isinstance(exc, (URLError, TimeoutError)) or any(s in detail for s in ('timed out', 'unable to download', 'connection refused', 'connection reset')):
        return 'network_error'
    return 'download_failed'

class FacebookRedirectHandler(HTTPRedirectHandler):
    # Resolve share links without enabling yt-dlp's unrestricted generic extractor.
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            u = urlparse(newurl)
            if u.scheme not in ('https', 'http') or u.hostname not in FACEBOOK_HOSTS or u.username or u.password or u.port:
                raise ValueError()
        except (ValueError, TypeError):
            raise DownloadFailure('share_link') from None
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def resolve_facebook_share(url):
    u = urlparse(url)
    if u.hostname != 'fb.watch' and not re.match(r'^/share/(v|r)/', u.path):
        return url
    request = URLRequest(url, headers={'User-Agent': 'ClipKeep/2.0'})
    with build_opener(FacebookRedirectHandler()).open(request, timeout=20) as response:
        resolved = response.geturl()
    # A 200 login/consent page is not a resolved video. Do not download that page.
    try:
        resolved = validate_url(resolved)
    except HTTPException:
        raise DownloadFailure('share_link') from None
    if urlparse(resolved).hostname not in FACEBOOK_HOSTS or not any(ie.suitable(resolved) for ie in (FacebookIE, FacebookReelIE)):
        raise DownloadFailure('share_link')
    return resolved

def authenticate(authorization: str = Header(default='')):
    if not API_KEY:
        raise HTTPException(503, 'เซิร์ฟเวอร์ยังไม่ได้ตั้งค่า API Key')
    if not hmac.compare_digest(authorization.encode(), ('Bearer ' + API_KEY).encode()):
        raise HTTPException(401, 'API Key ไม่ถูกต้อง กรุณาตรวจสอบการตั้งค่า')

def validate_url(value: str) -> str:
    try:
        u = urlparse(value)
        if u.scheme not in ('http', 'https') or u.username or u.password or u.port:
            raise ValueError()
        host = u.hostname
        if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'):
            parts = u.path.strip('/').split('/')
            video_id = parts[0] if host == 'youtu.be' else parse_qs(u.query).get('v', [''])[0] if u.path == '/watch' else parts[1] if len(parts) == 2 and parts[0] in ('shorts', 'live', 'embed') else ''
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
                raise ValueError()
            return 'https://www.youtube.com/watch?v=' + video_id
        if host in FACEBOOK_HOSTS:
            if host == 'fb.watch' and len(u.path) > 2 or re.match(r'^/(reel|share/(v|r))/.+', u.path) or re.search(r'/videos/.+', u.path) or u.path.rstrip('/') == '/watch' and parse_qs(u.query).get('v'):
                return value
    except (ValueError, TypeError):
        pass
    raise HTTPException(400, 'รองรับเฉพาะลิงก์วิดีโอ Facebook และ YouTube ที่ถูกต้อง')

def cleanup():
    now = time.time()
    with lock:
        expired = [k for k, v in jobs.items() if now - v['created'] > TTL and v['status'] in ('ready', 'error')]
        for k in expired:
            jobs.pop(k)
            shutil.rmtree(ROOT / k, ignore_errors=True)

def janitor():
    while True:
        time.sleep(60)
        cleanup()
threading.Thread(target=janitor, daemon=True).start()

@app.get('/api/health')
def health():
    return {'service': 'clipkeep', 'version': 2}

@app.get('/api/auth-check', dependencies=[Depends(authenticate)])
def auth_check():
    return {'ok': True}

@app.post('/api/jobs', dependencies=[Depends(authenticate)])
def create_job(data: Download, request: Request):
    url = validate_url(data.url)
    if data.format not in ('mp4', 'mp3') or data.quality not in ('1080', '720', '480', '360'):
        raise HTTPException(400, 'รูปแบบไฟล์หรือคุณภาพไม่ถูกต้อง')
    cleanup()
    with lock:
        # Global cap bounds queue, CPU and storage even behind a reverse proxy.
        if sum(v['status'] in ('queued', 'processing') for v in jobs.values()) >= 4 or len(jobs) >= 20:
            raise HTTPException(429, 'เซิร์ฟเวอร์มีงานเต็ม กรุณาลองใหม่ภายหลัง')
        job_id = secrets.token_urlsafe(32)
        jobs[job_id] = {'status': 'queued', 'created': time.time()}
    pool.submit(process, job_id, data, url)
    return {'id': job_id}

def process(job_id: str, data: Download, url: str):
    folder = ROOT / job_id
    folder.mkdir()
    deadline = time.monotonic() + 840
    def progress(info):
        if info.get('downloaded_bytes', 0) > MAX_FILE_BYTES:
            raise DownloadFailure('file_too_large')
        if time.monotonic() > deadline:
            raise DownloadFailure('time_limit')
    def check_media(info, **kwargs):
        if info.get('is_live'):
            raise DownloadFailure('live_video')
        if (info.get('duration') or 0) > MAX_VIDEO_SECONDS:
            raise DownloadFailure('duration_limit')
        if (info.get('filesize') or 0) > MAX_FILE_BYTES:
            raise DownloadFailure('file_too_large')
    class DownloaderLogger:
        def debug(self, message):
            if 'larger than max-filesize' in message.lower():
                raise DownloadFailure('file_too_large')
        def info(self, message):
            self.debug(message)
        def warning(self, message):
            pass
        def error(self, message):
            pass
    opts = {
        'outtmpl': str(folder / 'video.%(ext)s'), 'noplaylist': True,
        'quiet': True, 'no_warnings': True, 'socket_timeout': 20,
        'retries': 2, 'fragment_retries': 2, 'max_filesize': MAX_FILE_BYTES,
        'cachedir': False, 'restrictfilenames': True,
        'js_runtimes': {'node': {}},
        'progress_hooks': [progress],
        'match_filter': check_media, 'logger': DownloaderLogger(),
        'allowed_extractors': ['youtube', 'youtube:tab', 'facebook', 'facebook:reel'],
    }
    if data.format == 'mp3':
        opts.update(format='bestaudio/best', postprocessors=[{'key':'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'192'}])
    else:
        h = data.quality
        # Facebook's SD/HD files sometimes omit height. Keep them as a fallback
        # after formats with a known resolution instead of rejecting every file.
        opts.update(format=f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={h}][ext=mp4]/best[height<=?{h}][format_id=sd][ext=mp4]/best[height<=?{h}][ext=mp4]', merge_output_format='mp4')
    with lock:
        jobs[job_id]['status'] = 'processing'
    try:
        url = resolve_facebook_share(url)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = list(folder.glob('*.' + data.format))
        if not info or not files:
            raise ValueError('no_file')
        file = files[0]
        if file.stat().st_size > MAX_FILE_BYTES:
            raise DownloadFailure('file_too_large')
        title = str(info.get('title') or 'ClipKeep video')[:200]
        with lock:
            jobs[job_id].update(status='ready', title=title, duration=info.get('duration'), height=info.get('height'), file=str(file), filename='clipkeep-' + job_id[:8] + '.' + data.format, download_url='/api/files/' + job_id)
    except Exception as exc:
        code = error_code(exc)
        # Keep diagnostics useful without exposing API keys or signed media URLs.
        detail = str(exc).replace(API_KEY, '[redacted]') if API_KEY else str(exc)
        detail = re.sub(r'https?://\S+', '[url]', detail)
        detail = re.sub(r'\x1b\[[0-9;]*m', '', detail)
        detail = ' '.join(detail.split())[:400]
        logger.warning('download_failed code=%s exception=%s detail=%s', code, type(exc).__name__, detail)
        with lock:
            jobs[job_id].update(status='error', error=ERROR_MESSAGES[code], error_code=code)
        shutil.rmtree(folder, ignore_errors=True)

@app.get('/api/jobs/{job_id}', dependencies=[Depends(authenticate)])
def get_job(job_id: str):
    cleanup()
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, 'ไม่พบงานหรือไฟล์หมดอายุแล้ว')
        return {k:v for k,v in job.items() if k not in ('file', 'filename', 'created')}

@app.get('/api/files/{job_id}')
def file(job_id: str):
    # A 256-bit capability URL expires after one hour. API key never enters URLs.
    cleanup()
    with lock:
        job = jobs.get(job_id, {}).copy()
    if job.get('status') != 'ready' or not Path(job['file']).is_file():
        raise HTTPException(404, 'ไม่พบไฟล์หรือไฟล์หมดอายุแล้ว')
    return FileResponse(job['file'], filename=job['filename'], media_type='audio/mpeg' if job['file'].endswith('.mp3') else 'video/mp4', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

static = Path(__file__).parent / 'dist'
if not static.exists():
    static = Path(__file__).parent.parent / 'dist'
if static.exists():
    app.mount('/', StaticFiles(directory=static, html=True), name='website')
