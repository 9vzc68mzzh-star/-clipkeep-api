"""ClipKeep API. Run behind HTTPS. Requires CLIPKEEP_API_KEY and CORS_ORIGINS."""
import hmac
import os
import re
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import yt_dlp

API_KEY = os.environ.get('CLIPKEEP_API_KEY', '')
ORIGINS = [s.strip() for s in os.environ.get('CORS_ORIGINS', '').split(',') if s.strip()]
ROOT = Path('/tmp/clipkeep')
ROOT.mkdir(exist_ok=True)
MAX_FILE_BYTES = 512 * 1024 * 1024
TTL = 3600
jobs = {}
lock = threading.Lock()
pool = ThreadPoolExecutor(max_workers=2)
app = FastAPI(title='ClipKeep', docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=['GET', 'POST'], allow_headers=['Content-Type', 'Authorization'])

class Download(BaseModel):
    url: str = Field(max_length=2000)
    format: str = 'mp4'
    quality: str = '1080'

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
        if host in ('facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com', 'fb.watch'):
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
    return {'service': 'clipkeep', 'version': 1}

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
            raise yt_dlp.utils.DownloadError('file_too_large')
        if time.monotonic() > deadline:
            raise yt_dlp.utils.DownloadError('time_limit')
    opts = {
        'outtmpl': str(folder / 'video.%(ext)s'), 'noplaylist': True,
        'quiet': True, 'no_warnings': True, 'socket_timeout': 20,
        'retries': 2, 'fragment_retries': 2, 'max_filesize': MAX_FILE_BYTES,
        'cachedir': False, 'restrictfilenames': True,
        'js_runtimes': {'node': {}},
        'progress_hooks': [progress],
        'match_filter': lambda info, **kwargs: 'duration_limit' if (info.get('duration') or 0) > 1800 or info.get('is_live') else None,
        'allowed_extractors': ['youtube', 'youtube:tab', 'facebook', 'facebook:reel'],
    }
    if data.format == 'mp3':
        opts.update(format='bestaudio/best', postprocessors=[{'key':'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'192'}])
    else:
        h = data.quality
        opts.update(format=f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={h}][ext=mp4]', merge_output_format='mp4')
    with lock:
        jobs[job_id]['status'] = 'processing'
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = list(folder.glob('*.' + data.format))
        if not info or not files:
            raise ValueError('no_file')
        file = files[0]
        if file.stat().st_size > MAX_FILE_BYTES:
            raise ValueError('file_too_large')
        title = str(info.get('title') or 'ClipKeep video')[:200]
        with lock:
            jobs[job_id].update(status='ready', title=title, duration=info.get('duration'), height=info.get('height'), file=str(file), filename='clipkeep-' + job_id[:8] + '.' + data.format, download_url='/api/files/' + job_id)
    except Exception:
        with lock:
            jobs[job_id].update(status='error', error='ดาวน์โหลดไม่ได้ คลิปอาจถูกจำกัดการเข้าถึง แพลตฟอร์มปฏิเสธการเชื่อมต่อ หรือไฟล์เกิน 512 MB / 30 นาที กรุณาลองคลิปสาธารณะอื่น')
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
