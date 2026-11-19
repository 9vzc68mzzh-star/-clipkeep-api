"""Select compatible source streams; never encode video or buffer a full movie."""
import json
import os
import re
import select
import signal
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import yt_dlp
from media import MediaError

MEDIA_HOSTS = ('.fbcdn.net', '.fbsbx.com', '.googlevideo.com', '.youtube.com', '.facebook.com', '.akamaihd.net')

def media_url(value):
    u = urlparse(value or '')
    if u.scheme not in ('https','http') or u.username or u.password or u.port or not any((u.hostname or '').endswith(h) or u.hostname == h[1:] for h in MEDIA_HOSTS):
        raise MediaError('format_unavailable')
    return value


def headers_for(info, source):
    return {k:str(v).replace('\r','').replace('\n','') for k,v in source.get('http_headers',info.get('http_headers',{})).items()
            if k.lower() in ('user-agent','referer','origin','accept','accept-language')}


def input_args(source):
    args = ['-rw_timeout','20000000']
    headers = ''.join(k+': '+v+'\r\n' for k,v in source['headers'].items())
    if headers: args += ['-headers',headers]
    return args + ['-i',source['url']]


def inspect_source(source):
    try:
        result = subprocess.run(['ffprobe','-v','error',*input_args(source),'-show_streams','-show_format','-of','json'],
                                capture_output=True,timeout=25,check=True)
        info = json.loads(result.stdout)
        video = next((s for s in info['streams'] if s['codec_type']=='video' and not s.get('disposition',{}).get('attached_pic')),None)
        audio = next((s for s in info['streams'] if s['codec_type']=='audio'),None)
        return {'video_codec':video.get('codec_name') if video else None,'pix_fmt':video.get('pix_fmt') if video else None,
                'width':video.get('width') if video else None,'height':video.get('height') if video else None,
                'audio_codec':audio.get('codec_name') if audio else None,'audio_profile':audio.get('profile') if audio else None,
                'size':int(info.get('format',{}).get('size') or 0)}
    except (subprocess.SubprocessError,ValueError,KeyError):
        raise MediaError('format_unavailable') from None


def quality_edge(width,height):
    # A vertical 720x1280 Reel is 720p, just as a landscape 1280x720 clip is.
    return min(width,height) if width and height else height or width or 0


def choose_plan(info, kind, quality, inspect=inspect_source):
    formats=info.get('formats') or [info]
    def source(f):
        return {'url':media_url(f['url']),'headers':headers_for(info,f),'format_id':f.get('format_id','source')}
    def h264(f):return str(f.get('vcodec') or '').startswith(('avc1','h264'))
    def rank(f):return (quality_edge(f.get('width'),f.get('height')) or (720 if f.get('format_id')=='hd' else 224 if f.get('format_id')=='sd' else 0),f.get('tbr') or 0)
    if kind=='mp4':
        combined=[f for f in formats if f.get('ext')=='mp4' and f.get('vcodec')!='none' and f.get('acodec')!='none' and f.get('url')]
        for f in sorted(combined,key=rank,reverse=True):
            known=quality_edge(f.get('width'),f.get('height'))
            if known and known>quality:continue
            s=source(f)
            if h264(f) and str(f.get('acodec') or '').startswith('mp4a.40.2') and known:
                details={'video_codec':'h264','pix_fmt':'yuv420p','width':f.get('width'),'height':f.get('height'),
                         'audio_codec':'aac','audio_profile':'LC','size':f.get('filesize') or 0}
            else:
                try:details=inspect(s)
                except MediaError:continue
            if details['video_codec']!='h264' or details.get('pix_fmt') not in ('yuv420p',None):continue
            if quality_edge(details.get('width'),details.get('height'))>quality:continue
            copy_audio=details.get('audio_codec')=='aac' and details.get('audio_profile') in ('LC','HE-AAC','HE-AACv2')
            return {'sources':[s],'mode':'direct' if copy_audio or not details.get('audio_codec') else 'audio_aac',
                    'kind':kind,'details':details,'audio_copy':copy_audio,'source_format':s['format_id']}
        videos=[f for f in formats if h264(f) and f.get('acodec')=='none' and quality_edge(f.get('width'),f.get('height'))<=quality and f.get('url')]
        audios=[f for f in formats if f.get('vcodec')=='none' and f.get('acodec')!='none' and f.get('url')]
        if not videos or not audios:raise MediaError('compatible_video_unavailable')
        v=max(videos,key=rank);a=max(audios,key=lambda f:(str(f.get('acodec','')).startswith('mp4a.40.2'),f.get('abr') or f.get('tbr') or 0))
        return {'sources':[source(v),source(a)],'mode':'merge','kind':kind,'audio_copy':str(a.get('acodec','')).startswith(('mp4a.40.2','mp4a.40.5','mp4a.40.29')),
                'details':{'video_codec':'h264','audio_codec':'aac','width':v.get('width'),'height':v.get('height'),'size':0},
                'source_format':str(v.get('format_id'))+'+'+str(a.get('format_id'))}
    audios=[f for f in formats if f.get('acodec') not in ('none',None) and f.get('url')]
    if not audios:raise MediaError('audio_unavailable')
    a=max(audios,key=lambda f:(f.get('vcodec')=='none',f.get('abr') or f.get('tbr') or 0))
    return {'sources':[source(a)],'mode':'mp3','kind':'mp3','audio_copy':False,'details':{'video_codec':None,'audio_codec':'mp3','height':None,'size':0},'source_format':a.get('format_id','audio')}


def plan_worker(job_id, selection, initial, connection):
    os.setsid()
    import server
    timings=dict(initial.get('timings') or {})
    try:
        started=time.monotonic()
        resolved=initial.get('resolved_url') or server.resolve_facebook_share(initial['url'])
        timings['resolve_s']=round(time.monotonic()-started,4)
        connection.send({'kind':'update','fields':{'resolved_url':resolved,'timings':dict(timings)},'progressed':True})
        opts={'quiet':True,'no_warnings':True,'noplaylist':True,'skip_download':True,'socket_timeout':20,'retries':0,
              'extractor_retries':0,'cachedir':False,'js_runtimes':{'node':{}},
              'allowed_extractors':['youtube','youtube:tab','facebook','facebook:reel']}
        started=time.monotonic()
        with yt_dlp.YoutubeDL(opts) as ydl:info=ydl.extract_info(resolved,download=False)
        timings['extract_s']=round(time.monotonic()-started,4)
        if info.get('is_live'):raise MediaError('live_video')
        if (info.get('duration') or 0)>server.MAX_VIDEO_SECONDS:raise MediaError('duration_limit')
        started=time.monotonic();plan=choose_plan(info,selection['format'],int(selection['quality']))
        timings['probe_s']=round(time.monotonic()-started,4)
        if plan['details'].get('size',0)>server.MAX_FILE_BYTES:raise MediaError('file_too_large')
        details=plan['details']
        connection.send({'kind':'result','fields':{'plan':plan,'resolved_url':resolved,'timings':timings,'title':str(info.get('title') or 'คลิปของคุณ')[:200],
                         'thumbnail':info.get('thumbnail'),'duration':info.get('duration'),'height':details.get('height'),
                         'quality_label':quality_edge(details.get('width'),details.get('height')),
                         'video_codec':details.get('video_codec'),'audio_codec':details.get('audio_codec'),
                         'mode':plan['mode'],'source_format':plan['source_format'],
                         'warning':'คลิปต้นฉบับไม่มีแทร็กเสียง' if selection['format']=='mp4' and not details.get('audio_codec') else None},'progressed':True})
    except Exception as exc:
        connection.send({'kind':'error','code':server.error_code(exc),'type':type(exc).__name__})
    finally:connection.close()


def command_for(plan):
    args=['ffmpeg','-nostdin','-hide_banner','-nostats','-benchmark','-loglevel','info']
    for s in plan['sources']:args+=input_args(s)
    if plan['kind']=='mp3':
        return args+['-map','0:a:0','-vn','-c:a','libmp3lame','-b:a','192k','-f','mp3','pipe:1']
    args+=['-map','0:v:0','-map','1:a:0' if len(plan['sources'])==2 else '0:a:0?','-c:v','copy']
    args+=['-c:a','copy' if plan['audio_copy'] else 'aac']
    if not plan['audio_copy']:args+=['-b:a','160k','-ar','48000','-ac','2']
    # +faststart requires seeking. A pipe uses an initial moov and 1s fragments.
    return args+['-tag:v','avc1','-movflags','+frag_keyframe+empty_moov+default_base_moof','-frag_duration','1000000','-f','mp4','pipe:1']


def stream_bytes(plan, check, stats, on_process=lambda process:None):
    if plan['mode']=='direct':
        t=time.monotonic()
        with urlopen(Request(plan['sources'][0]['url'],headers=plan['sources'][0]['headers']),timeout=20) as response:
            stats['download_s']=time.monotonic()-t
            expected=int(response.headers.get('Content-Length') or 0);received=0
            while True:
                check();t=time.monotonic();chunk=response.read1(64*1024)
                stats['download_s']+=time.monotonic()-t
                if not chunk:break
                received+=len(chunk);yield chunk
            if expected and received!=expected:raise MediaError('network_error')
        stats['merge_s']=0.0
        stats['mux_cpu_s']=0.0
        return
    started=time.monotonic()
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command_for(plan),stdout=subprocess.PIPE,stderr=errors,start_new_session=True) as process:
            on_process(process)
            last=time.monotonic()
            try:
                while True:
                    check()
                    if time.monotonic()-last>75:raise MediaError('stalled')
                    if select.select([process.stdout],[],[],.25)[0]:
                        chunk=process.stdout.read1(64*1024)
                        if not chunk:break
                        last=time.monotonic();yield chunk
                    elif process.poll() is not None:break
                if process.wait(timeout=5):raise MediaError('network_error')
            finally:
                if process.poll() is None:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.wait()
                on_process(None)
        errors.seek(0)
        bench=re.search(rb'bench: utime=([\d.]+)s stime=([\d.]+)s',errors.read())
        if bench:stats['mux_cpu_s']=float(bench[1])+float(bench[2])
    stats['merge_s']=time.monotonic()-started
    # Remote-input merge and download overlap; do not add them to send wall time.
    stats['download_s']=stats['merge_s']
