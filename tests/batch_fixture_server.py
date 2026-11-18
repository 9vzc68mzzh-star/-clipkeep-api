"""Test-only source adapter: real media/ffmpeg/API; no platform network dependency.
Never imported by production server. Run with uvicorn tests... or this file.
"""
import os
import sys
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend' if (BASE / 'backend').exists() else BASE))
os.environ.update(CLIPKEEP_PUBLIC_DOWNLOADS='true', CORS_ORIGINS='http://127.0.0.1:8765', PUBLIC_SITE_ORIGIN='')
import server
from prepare_fixtures import ensure
FIXTURES=ensure()
server.ROOT=FIXTURES/'jobs'
server.ROOT.mkdir(exist_ok=True)
state = {'active':0,'maximum':0,'attempts':{},'starts':[]}
state_lock = threading.Lock()

class FixtureDownloader:
    def __init__(self, opts): self.opts=opts
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def extract_info(self, url, download):
        key = parse_qs(urlparse(url).query)['v'][0]
        number = int(key[3:])
        with state_lock:
            state['active']+=1
            state['maximum']=max(state['maximum'],state['active'])
            state['attempts'][key]=state['attempts'].get(key,0)+1
            attempt=state['attempts'][key]
            state['starts'].append(key)
        try:
            info={'title':f'คลิป {number:03d} / Test <safe>', 'duration':1.2, 'height':180,
                  'thumbnail':'http://127.0.0.1:8765/assets/flower.jpg'}
            self.opts['match_filter'](info)
            for n in range(1,5):
                time.sleep(.06)
                self.opts['progress_hooks'][0]({'status':'downloading','downloaded_bytes':n*100,'total_bytes':400,'info_dict':info})
            if number==13 and attempt==1:
                raise Exception('HTTP Error 403: fixture temporarily inaccessible')
            source=FIXTURES/('incompatible.webm' if number%7==0 else 'compatible.mp4')
            target=Path(self.opts['outtmpl'].replace('%(ext)s',source.suffix[1:]))
            shutil.copyfile(source,target)
            return info
        finally:
            with state_lock: state['active']-=1

server.yt_dlp.YoutubeDL=FixtureDownloader
app=server.app
# API routes must precede the catch-all StaticFiles mount.
def stats(): return state
from fastapi.routing import APIRoute
app.router.routes.insert(0,APIRoute('/test-stats',stats,methods=['GET']))
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8765,log_level='warning')
