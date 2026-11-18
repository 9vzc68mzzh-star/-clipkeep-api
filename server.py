"""KlipNivo API. Run behind HTTPS. Requires CLIPKEEP_API_KEY and CORS_ORIGINS."""
import io
import zipfile
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
from fastapi import FastAPI, Depends, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import yt_dlp
from yt_dlp.extractor.facebook import FacebookIE, FacebookReelIE
from media import MediaError, normalize

API_KEY = os.environ.get('CLIPKEEP_API_KEY', '')
ORIGINS = [s.strip() for s in os.environ.get('CORS_ORIGINS', '').split(',') if s.strip()]
PUBLIC_SITE_ORIGIN = os.environ.get('PUBLIC_SITE_ORIGIN', '').rstrip('/')
if PUBLIC_SITE_ORIGIN and PUBLIC_SITE_ORIGIN not in ORIGINS:
    ORIGINS.append(PUBLIC_SITE_ORIGIN)
ROOT = Path('/tmp/clipkeep')
ROOT.mkdir(exist_ok=True)
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_VIDEO_SECONDS = 2 * 60 * 60
PUBLIC_DOWNLOADS = os.environ.get('CLIPKEEP_PUBLIC_DOWNLOADS', '').lower() in ('1', 'true')
PUBLIC_RATE_WINDOW = 600
PUBLIC_RATE_LIMIT = 5  # Per batch, not per clip.
MAX_BATCH_LINKS = 200
MAX_ACTIVE_BATCHES = 2
MAX_STORED_BYTES = 4 * 1024 * 1024 * 1024
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
    'audio_unavailable': 'ไม่พบแทร็กเสียงที่แปลงเป็น MP3 ได้จากคลิปนี้ กรุณาเลือกวิดีโอ MP4 หรือลองคลิปอื่น',
    'extractor_error': 'ตัวดาวน์โหลดยังอ่านข้อมูลคลิปนี้ไม่ได้ กรุณาลองลิงก์วิดีโอโดยตรงหรืออัปเดตเซิร์ฟเวอร์',
    'network_error': 'เซิร์ฟเวอร์ติดต่อแพลตฟอร์มวิดีโอไม่สำเร็จ กรุณาลองใหม่ภายหลัง',
    'cancelled': 'ยกเลิกรายการแล้ว',
    'invalid_link': 'ลิงก์นี้ไม่ใช่วิดีโอ Facebook หรือ YouTube ที่รองรับ',
    'storage_limit': 'พื้นที่เตรียมไฟล์เต็มชั่วคราว กรุณาบันทึกไฟล์แล้วลองใหม่ภายหลัง',
    'video_unavailable': 'ไม่พบแทร็กภาพในคลิปนี้ ไม่ส่งไฟล์เสียงที่ตั้งชื่อเป็น MP4',
    'conversion_failed': 'แปลงไฟล์ให้รองรับอุปกรณ์ไม่สำเร็จ กรุณาลองใหม่หรือลดคุณภาพ',
    'download_failed': 'ดาวน์โหลดคลิปนี้ไม่สำเร็จ กรุณาลองคลิปสาธารณะอื่น หากยังไม่ได้ให้ตรวจ Logs ของเซิร์ฟเวอร์',
}
logger = logging.getLogger('clipkeep')
jobs = {}
batches = {}
futures = {}
public_requests = {}
lock = threading.RLock()
pool = ThreadPoolExecutor(max_workers=3)
encoder_lock = threading.Semaphore(1)
app = FastAPI(title='KlipNivo', docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=['GET', 'POST'], allow_headers=['Content-Type', 'Authorization'], expose_headers=['Content-Disposition', 'Content-Length', 'Accept-Ranges'])

class Download(BaseModel):
    url: str = Field(max_length=2000)
    format: str = 'mp4'
    quality: str = '1080'

class Batch(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=MAX_BATCH_LINKS)
    format: str = 'mp4'
    quality: str = '1080'
    request_id: str = Field(default='', max_length=80)

class DownloadFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def error_code(exc):
    if isinstance(exc, (DownloadFailure, MediaError)):
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
    if 'unable to obtain file audio codec' in detail:
        return 'audio_unavailable'
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

def settled(job):
    return job['status'] in ('ready', 'error', 'cancelled')


def finish_batch(batch_id):
    batch = batches.get(batch_id)
    if batch and not batch.get('finished') and all(settled(jobs[j]) for j in batch['ids']):
        batch['finished'] = time.time()


def cleanup():
    now = time.time()
    with lock:
        for batch_id, batch in list(batches.items()):
            if batch.get('finished') and now - batch['finished'] > TTL and not batch.get('readers'):
                for job_id in batch['ids']:
                    jobs.pop(job_id, None)
                    futures.pop(job_id, None)
                    shutil.rmtree(ROOT / job_id, ignore_errors=True)
                batches.pop(batch_id)
        for job_id, job in list(jobs.items()):
            if not job.get('batch_id') and settled(job) and now - job.get('finished', job['created']) > TTL:
                jobs.pop(job_id)
                futures.pop(job_id, None)
                shutil.rmtree(ROOT / job_id, ignore_errors=True)


def rate_limit(client):
    now = time.time()
    for key, attempts in list(public_requests.items()):
        recent = [t for t in attempts if now - t < PUBLIC_RATE_WINDOW]
        if recent:
            public_requests[key] = recent
        else:
            public_requests.pop(key)
    recent = public_requests.get(client, [])
    if len(recent) >= PUBLIC_RATE_LIMIT or client not in public_requests and len(public_requests) >= 1000:
        raise HTTPException(429, 'มีคำขอหลายชุดเกินไป กรุณารอประมาณ 10 นาทีแล้วลองใหม่')
    public_requests[client] = recent + [now]


def valid_options(data):
    if data.format not in ('mp4', 'mp3') or data.quality not in ('1080', '720', '480', '360'):
        raise HTTPException(400, 'รูปแบบไฟล์หรือคุณภาพไม่ถูกต้อง')


def submit(job_id):
    job = jobs[job_id]
    futures[job_id] = pool.submit(process, job_id, Download(**job['selection']), job['url'])


def janitor():
    while True:
        time.sleep(60)
        cleanup()
threading.Thread(target=janitor, daemon=True).start()

@app.get('/api/health')
def health():
    return {'service': 'clipkeep', 'version': 4, 'public_downloads': PUBLIC_DOWNLOADS, 'batch_limit': MAX_BATCH_LINKS, 'parallel_downloads': 3}

@app.get('/', include_in_schema=False)
def homepage():
    if PUBLIC_SITE_ORIGIN:
        return RedirectResponse(PUBLIC_SITE_ORIGIN + '/', status_code=302, headers={'Cache-Control':'no-store'})
    if static.exists():
        return FileResponse(static / 'index.html', media_type='text/html')
    raise HTTPException(404, 'ไม่พบหน้าเว็บ')

@app.get('/api/auth-check', dependencies=[Depends(authenticate)])
def auth_check():
    return {'ok': True}

@app.post('/api/jobs', dependencies=[Depends(authenticate)])
def create_job(data: Download, request: Request):
    return enqueue_job(data)

@app.post('/api/public/jobs')
def create_public_job(data: Download, request: Request):
    if not PUBLIC_DOWNLOADS:
        raise HTTPException(403, 'เซิร์ฟเวอร์นี้ยังไม่ได้เปิดดาวน์โหลดสาธารณะ')
    client = request.client.host if request.client else 'unknown'
    return enqueue_job(data, public=True, client=client)

def enqueue_job(data: Download, public=False, client=None):
    url = validate_url(data.url)
    valid_options(data)
    cleanup()
    with lock:
        if sum(not settled(v) for v in jobs.values() if not v.get('batch_id')) >= 4 or len(jobs) >= 800:
            raise HTTPException(429, 'เซิร์ฟเวอร์มีงานเต็ม กรุณาลองใหม่ภายหลัง')
        if public:
            rate_limit(client)
        job_id = secrets.token_urlsafe(32)
        jobs[job_id] = new_job(data, url, public)
        submit(job_id)
    return {'id': job_id}


def new_job(data, url, public, batch_id=None, index=1):
    return {'status': 'queued', 'stage': 'waiting', 'progress': 0, 'created': time.time(),
            'public': public, 'url': url, 'selection': data.model_dump(),
            'batch_id': batch_id, 'index': index, 'attempt': 1, 'cancel': False}


def require_public():
    if not PUBLIC_DOWNLOADS:
        raise HTTPException(403, 'เซิร์ฟเวอร์นี้ยังไม่ได้เปิดดาวน์โหลดสาธารณะ')


def client_id(request):
    return request.client.host if request.client else 'unknown'


@app.post('/api/batches', dependencies=[Depends(authenticate)])
def create_batch(data: Batch, request: Request):
    return enqueue_batch(data, False, client_id(request))


@app.post('/api/public/batches')
def create_public_batch(data: Batch, request: Request):
    require_public()
    return enqueue_batch(data, True, client_id(request))


def enqueue_batch(data, public, client):
    valid_options(data)
    cleanup()
    with lock:
        # A stable request ID lets a client retry a lost POST response safely.
        if data.request_id:
            for batch_id, batch in batches.items():
                if batch['client'] == client and batch['public'] == public and batch['request_id'] == data.request_id:
                    return batch_response(batch_id, public)
        if sum(not b.get('finished') for b in batches.values()) >= MAX_ACTIVE_BATCHES or len(jobs) + len(data.urls) > 800:
            raise HTTPException(429, 'คิวดาวน์โหลดเต็มชั่วคราว กรุณาลองใหม่เมื่อชุดก่อนเสร็จ')
        if public:
            rate_limit(client)
        batch_id = secrets.token_urlsafe(32)
        batch = {'ids': [], 'created': time.time(), 'public': public, 'client': client,
                 'request_id': data.request_id, 'format': data.format, 'readers': 0}
        batches[batch_id] = batch
        for index, raw in enumerate(data.urls, 1):
            job_id = secrets.token_urlsafe(32)
            raw = raw.strip()[:2001]
            selection = Download(url=raw[:2000], format=data.format, quality=data.quality)
            job = new_job(selection, raw, public, batch_id, index)
            try:
                if len(raw) > 2000:
                    raise HTTPException(400)
                job['url'] = validate_url(raw)
            except HTTPException:
                job.update(status='error', stage='failed', error_code='invalid_link',
                           error=ERROR_MESSAGES['invalid_link'], finished=time.time())
            jobs[job_id] = job
            batch['ids'].append(job_id)
        for job_id in batch['ids']:
            if not settled(jobs[job_id]):
                submit(job_id)
        finish_batch(batch_id)
        return batch_response(batch_id, public)


JOB_FIELDS = ('status', 'stage', 'progress', 'title', 'thumbnail', 'duration', 'height',
              'download_url', 'preview_url', 'filename', 'size', 'error', 'error_code',
              'warning', 'index', 'attempt', 'video_codec', 'audio_codec', 'url', 'error_type')


def batch_response(batch_id, public=False):
    with lock:
        batch = batches.get(batch_id)
        if not batch or public and not batch['public']:
            raise HTTPException(404, 'ไม่พบชุดดาวน์โหลดหรือไฟล์หมดอายุแล้ว')
        items = [dict(id=j, **{k: jobs[j][k] for k in JOB_FIELDS if k in jobs[j]}) for j in batch['ids']]
        return {'id': batch_id, 'items': items, 'finished': bool(batch.get('finished')),
                'total': len(items), 'ready': sum(j['status'] == 'ready' for j in items),
                'failed': sum(j['status'] == 'error' for j in items), 'parallel': 3,
                'archive_url': '/api/archives/' + batch_id,
                'expires_in': max(0, int(TTL - (time.time() - batch['finished']))) if batch.get('finished') else None}


@app.get('/api/batches/{batch_id}', dependencies=[Depends(authenticate)])
def get_batch(batch_id: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    cleanup()
    return batch_response(batch_id)


@app.get('/api/public/batches/{batch_id}')
def get_public_batch(batch_id: str, response: Response):
    require_public()
    return get_batch_public_response(batch_id, response)


def get_batch_public_response(batch_id, response):
    response.headers['Cache-Control'] = 'no-store'
    cleanup()
    return batch_response(batch_id, True)


def change_batch(batch_id, action, public=False):
    cleanup()
    with lock:
        batch_response(batch_id, public)
        batch = batches[batch_id]
        if action == 'retry':
            if not batch.get('finished'):
                raise HTTPException(409, 'โปรดรอให้ชุดนี้เสร็จก่อนลองรายการที่ไม่สำเร็จ')
            if sum(not b.get('finished') for b in batches.values()) >= MAX_ACTIVE_BATCHES:
                raise HTTPException(429, 'คิวเต็มชั่วคราว กรุณาลองใหม่ภายหลัง')
            batch.pop('finished', None)
            for job_id in batch['ids']:
                old = jobs[job_id]
                if old['status'] != 'error':
                    continue
                selection = Download(**old['selection'])
                try:
                    url = validate_url(old['url'])
                except HTTPException:
                    continue
                jobs[job_id] = new_job(selection, url, old['public'], batch_id, old['index'])
                jobs[job_id]['attempt'] = old['attempt'] + 1
                submit(job_id)
            finish_batch(batch_id)
        else:
            for job_id in batch['ids']:
                job = jobs[job_id]
                if not settled(job):
                    job['cancel'] = True
                    future = futures.get(job_id)
                    if future and future.cancel():
                        job.update(status='cancelled', stage='cancelled', finished=time.time())
            finish_batch(batch_id)
        return batch_response(batch_id, public)


@app.post('/api/batches/{batch_id}/retry', dependencies=[Depends(authenticate)])
def retry_batch(batch_id: str):
    return change_batch(batch_id, 'retry')


@app.post('/api/public/batches/{batch_id}/retry')
def retry_public_batch(batch_id: str):
    require_public()
    return change_batch(batch_id, 'retry', True)


@app.post('/api/batches/{batch_id}/cancel', dependencies=[Depends(authenticate)])
def cancel_batch(batch_id: str):
    return change_batch(batch_id, 'cancel')


@app.post('/api/public/batches/{batch_id}/cancel')
def cancel_public_batch(batch_id: str):
    require_public()
    return change_batch(batch_id, 'cancel', True)


def process(job_id: str, data: Download, url: str):
    folder = ROOT / job_id
    folder.mkdir(exist_ok=True)
    deadline = time.monotonic() + 1200
    last_report = [0.0]

    def check():
        if jobs[job_id].get('cancel'):
            raise DownloadFailure('cancelled')
        if time.monotonic() > deadline:
            raise DownloadFailure('time_limit')
        # Check current disk use, including downloads still in progress.
        if time.monotonic() - last_report[0] > 2:
            last_report[0] = time.monotonic()
            used = 0
            for path in ROOT.glob('*/*'):
                try:
                    used += path.stat().st_size if path.is_file() else 0
                except FileNotFoundError:
                    pass
            if used > MAX_STORED_BYTES or shutil.disk_usage(ROOT).free < 96 * 1024 * 1024:
                raise DownloadFailure('storage_limit')

    def report(stage, percent):
        with lock:
            job = jobs[job_id]
            job.update(stage=stage, progress=round(max(job.get('progress', 0), percent), 1))

    def metadata(info):
        with lock:
            jobs[job_id].update(title=str(info.get('title') or 'คลิปของคุณ')[:200],
                               thumbnail=info.get('thumbnail'), duration=info.get('duration'))

    def progress(info):
        check()
        if info.get('downloaded_bytes', 0) > MAX_FILE_BYTES:
            raise DownloadFailure('file_too_large')
        if info.get('info_dict'):
            metadata(info['info_dict'])
        total = info.get('total_bytes') or info.get('total_bytes_estimate')
        percent = min(76, info.get('downloaded_bytes', 0) / total * 72 + 4) if total else 5
        report('downloading' if info['status'] == 'downloading' else 'merging', percent)

    def check_media(info, **kwargs):
        check()
        metadata(info)
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
        'outtmpl': str(folder / 'source.%(ext)s'), 'noplaylist': True,
        'quiet': True, 'no_warnings': True, 'socket_timeout': 20,
        'retries': 2, 'fragment_retries': 2, 'max_filesize': MAX_FILE_BYTES,
        'cachedir': False, 'restrictfilenames': True,
        'js_runtimes': {'node': {}}, 'progress_hooks': [progress],
        'match_filter': check_media, 'logger': DownloaderLogger(),
        'allowed_extractors': ['youtube', 'youtube:tab', 'facebook', 'facebook:reel'],
    }
    if data.format == 'mp3':
        opts.update(format='bestaudio/best')
    else:
        h = data.quality
        # Prefer H.264+AAC, fall back to other codecs, then inspect/convert them.
        opts.update(format=(
            f'bestvideo[height<={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/'
            f'best[height<={h}][vcodec^=avc1][acodec!=none]/'
            f'bestvideo[height<={h}]+bestaudio/'
            f'best[height<={h}][vcodec!=none]/'
            f'best[height<=?{h}][format_id=sd][vcodec!=?none]/'
            f'best[height<=?{h}][vcodec!=?none]'), merge_output_format='mp4')
    with lock:
        jobs[job_id].update(status='processing', stage='resolving', progress=1)
    try:
        check()
        url = resolve_facebook_share(url)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        check()
        files = [p for p in folder.glob('source.*') if p.suffix not in ('.part', '.ytdl', '.json') and '.f' not in p.stem]
        if not info or not files:
            raise DownloadFailure('download_failed')
        metadata(info)
        report('merging', 80)
        source = max(files, key=lambda p: p.stat().st_size)
        if source.stat().st_size > MAX_FILE_BYTES:
            raise DownloadFailure('file_too_large')
        output, media = normalize(source, folder, data.format, int(data.quality), check, report, encoder_lock, MAX_FILE_BYTES)
        title = str(info.get('title') or 'คลิปของคุณ')[:200]
        readable = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '-', title).strip(' .-')[:70] or 'clip'
        readable = readable.encode('utf-8')[:160].decode('utf-8', 'ignore')
        filename = f"{jobs[job_id]['index']:03d}-{readable}-{job_id[:8]}.{data.format}"
        with lock:
            jobs[job_id].update(status='ready', stage='done', progress=100, title=title,
                               file=str(output), filename=filename, size=output.stat().st_size,
                               download_url='/api/files/' + job_id, preview_url='/api/preview/' + job_id,
                               finished=time.time(), **media)
    except Exception as exc:
        code = error_code(exc)
        detail = str(exc).replace(API_KEY, '[redacted]') if API_KEY else str(exc)
        detail = re.sub(r'https?://\S+', '[url]', detail)
        detail = re.sub(r'\x1b\[[0-9;]*m', '', detail)
        logger.warning('download_failed code=%s exception=%s detail=%s', code, type(exc).__name__, ' '.join(detail.split())[:400])
        with lock:
            jobs[job_id].update(status='cancelled' if code == 'cancelled' else 'error',
                               stage='cancelled' if code == 'cancelled' else 'failed',
                               error=ERROR_MESSAGES[code], error_code=code, error_type=type(exc).__name__, finished=time.time())
        shutil.rmtree(folder, ignore_errors=True)
    finally:
        with lock:
            finish_batch(jobs[job_id].get('batch_id'))


@app.get('/api/jobs/{job_id}', dependencies=[Depends(authenticate)])
def get_job(job_id: str):
    return job_response(job_id)

@app.get('/api/public/jobs/{job_id}')
def get_public_job(job_id: str, response: Response):
    if not PUBLIC_DOWNLOADS:
        raise HTTPException(403, 'เซิร์ฟเวอร์นี้ยังไม่ได้เปิดดาวน์โหลดสาธารณะ')
    response.headers['Cache-Control'] = 'no-store'
    return job_response(job_id, public=True)

def job_response(job_id: str, public=False):
    cleanup()
    with lock:
        job = jobs.get(job_id)
        if not job or public and not job.get('public'):
            raise HTTPException(404, 'ไม่พบงานหรือไฟล์หมดอายุแล้ว')
        return {k: job[k] for k in JOB_FIELDS if k in job}

def ready_file(job_id):
    cleanup()
    with lock:
        job = jobs.get(job_id, {}).copy()
    if job.get('status') != 'ready' or not Path(job['file']).is_file():
        raise HTTPException(404, 'ไม่พบไฟล์หรือไฟล์หมดอายุแล้ว')
    return job


@app.get('/api/files/{job_id}')
def file(job_id: str):
    # Capability URL; proper attachment + filename works across origins, including Safari.
    job = ready_file(job_id)
    return FileResponse(job['file'], filename=job['filename'], media_type='audio/mpeg' if job['file'].endswith('.mp3') else 'video/mp4',
                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@app.get('/api/preview/{job_id}')
def preview(job_id: str):
    job = ready_file(job_id)
    return FileResponse(job['file'], media_type='audio/mpeg' if job['file'].endswith('.mp3') else 'video/mp4',
                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


class ZipBuffer(io.RawIOBase):
    # A non-seekable ZIP writer emits data as it is read. No second copy on disk.
    def __init__(self):
        self.data = bytearray()
        self.position = 0
    def writable(self):
        return True
    def seekable(self):
        return False
    def write(self, data):
        self.data.extend(data)
        self.position += len(data)
        return len(data)
    def tell(self):
        return self.position
    def drain(self):
        chunk = bytes(self.data)
        self.data.clear()
        return chunk


@app.get('/api/archives/{batch_id}')
def archive(batch_id: str, items: str = ''):
    cleanup()
    with lock:
        batch_response(batch_id)
        batch = batches[batch_id]
        try:
            indexes = {int(i) for i in items.split(',')} if items else None
        except ValueError:
            raise HTTPException(400, 'รายการไฟล์ไม่ถูกต้อง') from None
        selected = [jobs[j].copy() for j in batch['ids'] if jobs[j]['status'] == 'ready'
                    and (indexes is None or jobs[j]['index'] in indexes)]
        if not selected:
            raise HTTPException(409, 'ยังไม่มีไฟล์พร้อมบันทึก')
        batch['readers'] += 1

    def stream():
        buffer = ZipBuffer()
        try:
            with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zipped:
                for job in selected:
                    with zipped.open(job['filename'], 'w', force_zip64=True) as target, open(job['file'], 'rb') as source:
                        while chunk := source.read(512 * 1024):
                            target.write(chunk)
                            yield buffer.drain()
                    yield buffer.drain()
            yield buffer.drain()
        finally:
            with lock:
                batch['readers'] -= 1
    return StreamingResponse(stream(), media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="KlipNivo-{batch_id[:8]}.zip"',
        'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


static = Path(__file__).parent / 'dist'
if not static.exists():
    static = Path(__file__).parent.parent / 'dist'
if static.exists():
    app.mount('/', StaticFiles(directory=static, html=True), name='website')
