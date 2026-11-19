"""Offline subprocess adapter for integration tests; never loaded in production."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs,urlparse

def fixture_worker(job_id, selection, url, initial, root, connection):
    os.setsid()
    import server
    from prepare_fixtures import ensure
    fixtures=ensure();root=Path(root);server.ROOT=root;server.jobs[job_id]=initial
    index=initial['index'];attempt=initial['attempt'];browser=initial.get('fixture_mode')=='browser'
    key=parse_qs(urlparse(url).query)['v'][0]
    number=int(key[3:]) if browser else index
    def record(kind,**extra):
        with (root/'sequence.jsonl').open('a') as log:
            log.write(json.dumps({'kind':kind,'index':index,'attempt':attempt,'key':key,'time':time.monotonic(),**extra})+'\n')
    record('start',pid=os.getpid());allowed=(root/'allow-retry').exists()
    class FixtureDownloader:
        def __init__(self,opts):self.opts=opts
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract_info(self,url,download):
            info={'title':f'Clip {number:02d}','height':180,'duration':1.2}
            self.opts['match_filter'](info)
            if (not browser and number==7 and attempt==1) or (browser and number==13 and attempt==1):raise RuntimeError('Connection reset')
            if (not browser and number==10 and not allowed) or (browser and number==17 and attempt<=2):raise RuntimeError('HTTP Error 403')
            for count in range(1,4):
                self.opts['progress_hooks'][0]({'status':'downloading','downloaded_bytes':count*100,'total_bytes':300,'info_dict':info})
                time.sleep(.03)
            source=fixtures/('compatible.mp4')
            shutil.copyfile(source,self.opts['outtmpl'].replace('%(ext)s',source.suffix[1:]))
            return info
    server.yt_dlp.YoutubeDL=FixtureDownloader
    if not browser and number==4 and not allowed:
        def stuck(source,folder,kind,height,check,report,*args):
            child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)'])
            record('stuck-encoder',pid=child.pid)
            while True:
                report('converting',82)  # Repeated unchanged 82% must not reset the watchdog.
                time.sleep(.1)
        server.normalize=stuck
    def emit(event):
        if event['kind']=='result':record('finish',status=event['fields']['status'])
        connection.send(event)
    try:server.prepare(job_id,server.Download(**selection),url,emit)
    finally:connection.close()

def browser_worker(job_id, selection, url, initial, root, connection):
    initial['fixture_mode']='browser'
    fixture_worker(job_id,selection,url,initial,root,connection)
