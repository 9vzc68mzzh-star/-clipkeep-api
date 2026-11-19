"""KlipNivo API. Run behind HTTPS. Requires CLIPKEEP_API_KEY and CORS_ORIGINS."""
import io
import zipfile
import multiprocessing
import signal
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
from fastapi import FastAPI, Depends, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import yt_dlp
from media import MediaError, normalize
from source_utils import FACEBOOK_HOSTS, DownloadFailure, error_code, FacebookRedirectHandler, resolve_facebook_share

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
STALL_SECONDS = 75
DELIVERY_SECONDS = 75
ATTEMPT_SECONDS = 1200
MAX_STORED_BYTES = 4 * 1024 * 1024 * 1024
TTL = 3600
ERROR_MESSAGES = {
    'share_link': 'เปิดลิงก์แชร์ Facebook ไม่สำเร็จ กรุณาเปิดคลิปใน Facebook แล้วคัดลอกลิงก์วิดีโอโดยตรงมาลองอีกครั้ง',
    'file_too_large': 'ไฟล์เกินขนาดสูงสุด 512 MB กรุณาเลือกคุณภาพ 720p หรือ 480p แล้วลองใหม่',
    'duration_limit': 'คลิปยาวเกิน 2 ชั่วโมง กรุณาเลือกคลิปที่สั้นกว่า',
    'live_video': 'ยังไม่รองรับคลิปที่กำลังถ่ายทอดสด กรุณารอให้การถ่ายทอดจบก่อน',
    'stalled': 'ไม่มีความคืบหน้าเกิน 75 วินาที จึงหยุดคลิปนี้และไปคลิปถัดไป',
    'delivery_timeout': 'ไฟล์พร้อมแล้วแต่ยังส่งไปยังเบราว์เซอร์ไม่ได้ กรุณาใช้ ZIP หรือบันทึกไฟล์อีกครั้ง',
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
    'compatible_video_unavailable': 'ไม่มีสตรีม H.264 ที่เข้ากันได้ในคุณภาพนี้ จึงไม่แปลงภาพใหม่ กรุณาลองคุณภาพอื่น',
    'conversion_failed': 'แปลงไฟล์ให้รองรับอุปกรณ์ไม่สำเร็จ กรุณาลองใหม่หรือลดคุณภาพ',
    'download_failed': 'ดาวน์โหลดคลิปนี้ไม่สำเร็จ กรุณาลองคลิปสาธารณะอื่น หากยังไม่ได้ให้ตรวจ Logs ของเซิร์ฟเวอร์',
}
logger = logging.getLogger('clipkeep')
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
jobs = {}
batches = {}
futures = {}
public_requests = {}
lock = threading.RLock()
pool = ThreadPoolExecutor(max_workers=1)
encoder_lock = threading.Semaphore(1)
app = FastAPI(title='KlipNivo', docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=['GET', 'POST'], allow_headers=['Content-Type', 'Authorization'], expose_headers=['Content-Disposition', 'Content-Length', 'Accept-Ranges'])

class Download(BaseModel):
    url: str = Field(max_length=2000)
    format: str = 'mp4'
    quality: str = '720'

class Batch(BaseModel):
    streaming: bool = False
    urls: list[str] = Field(min_length=1, max_length=MAX_BATCH_LINKS)
    format: str = 'mp4'
    quality: str = '720'
    request_id: str = Field(default='', max_length=80)

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
    return job['status'] in ('error', 'cancelled') or job['status'] == 'ready' and (not job.get('batch_id') or job.get('delivery_status') == 'sent')


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
                if batch.get('engine') == 'stream': streamer.discard(batch_id)
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
if multiprocessing.current_process().name == 'MainProcess':
    threading.Thread(target=janitor, daemon=True).start()

@app.get('/api/health')
def health():
    return {'service': 'clipkeep', 'version': 7, 'public_downloads': PUBLIC_DOWNLOADS, 'batch_limit': MAX_BATCH_LINKS, 'parallel_downloads': 1, 'stall_seconds': STALL_SECONDS, 'ordered_delivery': True, 'streaming': True, 'prefetch_limit': 1, 'video_encoding': False, 'default_quality': '720'}

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
            'batch_id': batch_id, 'index': index, 'attempt': 1, 'auto_retry': 0, 'cancel': False}


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
                 'request_id': data.request_id, 'format': data.format, 'readers': 0, 'engine': 'stream' if data.streaming else 'buffered'}
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
        if data.streaming:
            for job_id in batch['ids']:jobs[job_id]['engine']='stream'
            streamer.enqueue(batch_id)
        else:
            for job_id in batch['ids']:
                if not settled(jobs[job_id]):submit(job_id)
        finish_batch(batch_id)
        return batch_response(batch_id, public)


JOB_FIELDS = ('status', 'stage', 'progress', 'title', 'thumbnail', 'duration', 'height',
              'download_url', 'preview_url', 'filename', 'size', 'error', 'error_code',
              'warning', 'index', 'attempt', 'video_codec', 'audio_codec', 'url', 'error_type', 'auto_retry', 'delivery_status', 'file_ready', 'timings', 'mode', 'source_format', 'quality_label', 'prefetched', 'stream_ready', 'engine', 'bytes_sent', 'bytes_total')


def batch_response(batch_id, public=False):
    with lock:
        batch = batches.get(batch_id)
        if not batch or public and not batch['public']:
            raise HTTPException(404, 'ไม่พบชุดดาวน์โหลดหรือไฟล์หมดอายุแล้ว')
        items = [dict(id=j, **{k: jobs[j][k] for k in JOB_FIELDS if k in jobs[j]}) for j in batch['ids']]
        if batch.get('engine')=='stream':
            for item in items:item['can_download']=streamer.can_download(item['id'])
        return {'id': batch_id, 'items': items, 'finished': bool(batch.get('finished')),
                'total': len(items), 'ready': sum(j['status'] == 'ready' and j.get('delivery_status') == 'sent' for j in items),
                'failed': sum(j['status'] == 'error' for j in items), 'parallel': 1,
                'prepared': sum(bool(j.get('file_ready')) for j in items),
                'active_index': next((j['index'] for j in items if j['status'] == 'processing' or j['status'] == 'ready' and j.get('delivery_status') != 'sent'), None),
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
        if batch.get('engine') == 'stream':
            streamer.change(batch_id, action)
            return batch_response(batch_id, public)
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


def prepare(job_id: str, data: Download, url: str, emit=lambda event: None):
    folder = ROOT / job_id
    deadline = time.monotonic() + ATTEMPT_SECONDS
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

    report_state = [None, 0, None, 0.0]
    def notify(kind='update', progressed=False):
        emit({'kind':kind, 'fields':{k:v for k,v in jobs[job_id].items() if k in (*JOB_FIELDS, 'file', 'finished')},
              'progressed':progressed})

    def report(stage, percent, activity=None):
        now = time.monotonic()
        changed = stage != report_state[0] or percent > report_state[1] or activity is not None and activity != report_state[2]
        if not changed or stage == report_state[0] and now - report_state[3] < .25:
            return
        report_state[:] = [stage, percent, activity, now]
        with lock:
            job = jobs[job_id]
            job.update(stage=stage, progress=round(max(job.get('progress', 0), percent), 1))
        notify(progressed=True)

    def metadata(info):
        values = {'title':str(info.get('title') or 'คลิปของคุณ')[:200],
                  'thumbnail':info.get('thumbnail'), 'duration':info.get('duration')}
        changed = any(jobs[job_id].get(k) != v for k,v in values.items())
        with lock:
            jobs[job_id].update(values)
        if changed:
            notify(progressed=True)

    def progress(info):
        check()
        if info.get('downloaded_bytes', 0) > MAX_FILE_BYTES:
            raise DownloadFailure('file_too_large')
        if info.get('info_dict'):
            metadata(info['info_dict'])
        total = info.get('total_bytes') or info.get('total_bytes_estimate')
        percent = min(76, info.get('downloaded_bytes', 0) / total * 72 + 4) if total else 5
        report('downloading' if info['status'] == 'downloading' else 'merging', percent, (info.get('filename'), info.get('downloaded_bytes', 0)))

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
        'retries': 0, 'fragment_retries': 0, 'concurrent_fragment_downloads': 4, 'max_filesize': MAX_FILE_BYTES,
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
            f'best[height<=?{h}][ext=mp4][acodec!=none][vcodec^=avc1]/'
            f'best[height<=?{h}][format_id={"sd" if int(h)<=480 else "hd"}][ext=mp4]/'
            f'best[height<=?{h}][format_id=sd][ext=mp4]/'
            f'bestvideo[height<={h}][vcodec^=avc1]+bestaudio[acodec^=mp4a]/'
            f'best[height<={h}][vcodec^=avc1][acodec!=none]/'
            f'bestvideo[height<={h}]+bestaudio/'
            f'best[height<={h}][vcodec!=none]/'
            f'best[height<=?{h}][format_id=sd][vcodec!=?none]/'
            f'best[height<=?{h}][vcodec!=?none]'), merge_output_format='mp4')
    with lock:
        jobs[job_id].update(status='processing', stage='resolving', progress=1)
    try:
        folder.mkdir(exist_ok=True)
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
            jobs[job_id].update(status='ready', stage='sending' if jobs[job_id].get('batch_id') else 'done',
                               progress=99 if jobs[job_id].get('batch_id') else 100, file_ready=True,
                               delivery_status='waiting' if jobs[job_id].get('batch_id') else 'sent', title=title,
                               file=str(output), filename=filename, size=output.stat().st_size,
                               download_url='/api/files/' + job_id, preview_url='/api/preview/' + job_id,
                               finished=time.time(), **media)
    except Exception as exc:
        code = error_code(exc)
        detail = str(exc).replace(API_KEY, '[redacted]') if API_KEY else str(exc)
        detail = re.sub(r'https?://\S+', '[url]', detail)
        detail = re.sub(r'\x1b\[[0-9;]*m', '', detail)
        logger.warning('download_failed code=%s exception=%s detail=%s', code, type(exc).__name__, ' '.join(detail.split())[:400])
        shutil.rmtree(folder, ignore_errors=True)
        with lock:
            jobs[job_id].update(status='cancelled' if code == 'cancelled' else 'error',
                               stage='cancelled' if code == 'cancelled' else 'failed',
                               error=ERROR_MESSAGES[code], error_code=code, error_type=type(exc).__name__, finished=time.time())
    finally:
        notify('result', progressed=True)


def prepare_worker(job_id, selection, url, initial, root, connection):
    # Isolate blocking extractors and FFmpeg so the supervisor can stop them.
    os.setsid()
    global ROOT
    ROOT = Path(root)
    jobs[job_id] = initial
    try:
        prepare(job_id, Download(**selection), url, connection.send)
    finally:
        connection.close()


def stop_worker(worker):
    if worker.is_alive():
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            worker.kill()
    worker.join(timeout=2)


def run_attempt(job_id, data, url):
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    worker = context.Process(target=prepare_worker,
                             args=(job_id, data.model_dump(), url, jobs[job_id].copy(), str(ROOT), send))
    started = last_progress = time.monotonic()
    try:
        worker.start()
        send.close()
        while True:
            if jobs[job_id].get('cancel'):
                raise DownloadFailure('cancelled')
            now = time.monotonic()
            if now - last_progress > STALL_SECONDS:
                raise DownloadFailure('stalled')
            if now - started > ATTEMPT_SECONDS:
                raise DownloadFailure('time_limit')
            if receive.poll(.2):
                try:
                    event = receive.recv()
                except EOFError:
                    raise DownloadFailure('download_failed') from None
                if event['kind'] == 'result' and event['fields']['status'] in ('error', 'cancelled'):
                    raise DownloadFailure(event['fields']['error_code'])
                with lock:
                    jobs[job_id].update(event['fields'])
                if event.get('progressed'):
                    last_progress = time.monotonic()
                if event['kind'] == 'result':
                    return
            elif not worker.is_alive():
                raise DownloadFailure('download_failed')
    finally:
        if worker.pid:
            stop_worker(worker)
        receive.close()
        send.close()


def mark_sent(job_id, attempt=None):
    with lock:
        job = jobs.get(job_id)
        if not job or not job.get('file_ready') or job.get('cancel'):
            return False
        if attempt is not None and job['attempt'] != attempt:
            return False
        if job['status'] == 'ready':
            job.update(delivery_status='sent', stage='done', progress=100, finished=time.time())
            finish_batch(job.get('batch_id'))
            return True
    return False


class Sent(BaseModel):
    id: str = Field(max_length=100)
    attempt: int = Field(ge=1)


def acknowledge(batch_id, data, public=False):
    with lock:
        batch_response(batch_id, public)
        job = jobs.get(data.id, {})
        if data.id not in batches[batch_id]['ids'] or job.get('status') != 'ready' or job.get('attempt') != data.attempt:
            raise HTTPException(409, 'ไฟล์ยังไม่พร้อมหรือเป็นคำขอจากครั้งก่อน กรุณาเช็กคิวอีกครั้ง')
        # A click is only a request. FileResponse completion confirms the transfer.
        if job.get('delivery_status') == 'waiting':
            job.update(delivery_status='requested', delivery_touched=time.monotonic())
        return {'ok': True}


@app.post('/api/public/batches/{batch_id}/sent')
def public_sent(batch_id: str, data: Sent):
    require_public()
    return acknowledge(batch_id, data, True)


@app.post('/api/batches/{batch_id}/sent', dependencies=[Depends(authenticate)])
def private_sent(batch_id: str, data: Sent):
    return acknowledge(batch_id, data)


def process(job_id: str, data: Download, url: str):
    # Keep the single executor slot through preparation and delivery dispatch.
    for automatic in range(2):
        with lock:
            job = jobs[job_id]
            if automatic:
                job['attempt'] += 1
                job.update(auto_retry=1, status='processing', stage='retrying', progress=0)
                for key in ('error', 'error_code', 'error_type', 'finished'):
                    job.pop(key, None)
                job.pop('delivered_ranges', None)
            else:
                job.update(status='processing', stage='resolving', progress=1)
        try:
            if job.get('cancel'):
                raise DownloadFailure('cancelled')
            if job.get('file_ready'):
                job.update(status='ready', stage='sending', progress=99, delivery_status='waiting')
            else:
                run_attempt(job_id, data, url)
            if job['status'] == 'error':
                raise DownloadFailure(job['error_code'])
            if job.get('batch_id') and job['status'] == 'ready':
                job['delivery_touched'] = time.monotonic()
                while job.get('delivery_status') != 'sent':
                    if job.get('cancel'):
                        raise DownloadFailure('cancelled')
                    if time.monotonic() - job['delivery_touched'] > DELIVERY_SECONDS:
                        raise DownloadFailure('delivery_timeout')
                    time.sleep(.1)
            break
        except Exception as exc:
            code = error_code(exc)
            retrying = automatic == 0 and code != 'cancelled'
            if not job.get('file_ready'):
                shutil.rmtree(ROOT / job_id, ignore_errors=True)
            with lock:
                job.update(status='processing' if retrying else 'cancelled' if code == 'cancelled' else 'error',
                           stage='retrying' if retrying else 'cancelled' if code == 'cancelled' else 'failed',
                           error=None if retrying else ERROR_MESSAGES[code], error_code=code, finished=time.time())
            if code == 'cancelled' or automatic == 1:
                break
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
    if not job.get('file_ready', job.get('status') == 'ready') or not Path(job['file']).is_file():
        raise HTTPException(404, 'ไม่พบไฟล์หรือไฟล์หมดอายุแล้ว')
    return job


class AttachmentResponse(FileResponse):
    def __init__(self, job_id, job):
        super().__init__(job['file'], filename=job['filename'],
                         media_type='audio/mpeg' if job['file'].endswith('.mp3') else 'video/mp4',
                         headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        self.job_id, self.attempt = job_id, job.get('attempt')

    async def __call__(self, scope, receive, send):
        status = 0
        content_range = ''
        async def track(message):
            nonlocal status, content_range
            await send(message)
            if message['type'] == 'http.response.start':
                status = message['status']
                content_range = dict(message.get('headers', [])).get(b'content-range', b'').decode()
            elif message['type'] == 'http.response.body' and message.get('body'):
                with lock:
                    job = jobs.get(self.job_id)
                    if job and job.get('attempt') == self.attempt:
                        job['delivery_touched'] = time.monotonic()
        await super().__call__(scope, receive, track)
        # Range probes/previews must not release the sequential queue.
        if status == 200 and scope['method'] == 'GET':
            mark_sent(self.job_id, self.attempt)
        elif status == 206 and scope['method'] == 'GET':
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', content_range)
            if match:
                start, end, total = map(int, match.groups())
                with lock:
                    job = jobs.get(self.job_id)
                    if job and job.get('attempt') == self.attempt:
                        merged = []
                        for left, right in sorted([*job.get('delivered_ranges', []), (start, end)]):
                            if merged and left <= merged[-1][1] + 1:
                                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
                            else:
                                merged.append((left, right))
                        job['delivered_ranges'] = merged[:100]
                        if merged and merged[0][0] == 0 and merged[0][1] >= total - 1:
                            mark_sent(self.job_id, self.attempt)


@app.get('/api/files/{job_id}')
def file(job_id: str, request: Request):
    if jobs.get(job_id,{}).get('engine')=='stream':
        return streamer.response(job_id, request)
    # Capability URL; proper attachment + filename works across origins, including Safari.
    job = ready_file(job_id)
    return AttachmentResponse(job_id, job)


@app.get('/api/preview/{job_id}')
def preview(job_id: str, request: Request):
    if jobs.get(job_id,{}).get('engine')=='stream':
        return streamer.response(job_id, request, True)
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
        if batch.get('engine')=='stream':return streamer.archive_response(batch_id)
        try:
            indexes = {int(i) for i in items.split(',')} if items else None
        except ValueError:
            raise HTTPException(400, 'รายการไฟล์ไม่ถูกต้อง') from None
        selected = [jobs[j].copy() for j in batch['ids'] if jobs[j].get('file_ready', jobs[j]['status'] == 'ready')
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


from stream_queue import StreamQueue
streamer = StreamQueue(globals())

static = Path(__file__).parent / 'dist'
if not static.exists():
    static = Path(__file__).parent.parent / 'dist'
if static.exists():
    app.mount('/', StaticFiles(directory=static, html=True), name='website')
