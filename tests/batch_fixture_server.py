"""Test-only source adapter; the normal API and child-process queue are real."""
import json
import functools
import subprocess
import os
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'backend' if (BASE/'backend').exists() else BASE))
os.environ.update(CLIPKEEP_PUBLIC_DOWNLOADS='true',CORS_ORIGINS='http://127.0.0.1:8765',PUBLIC_SITE_ORIGIN='')
import server
from prepare_fixtures import ensure
from fast_fixture import fixture_plan
server.ROOT=ensure()/'browser-jobs';server.ROOT.mkdir(exist_ok=True)
log=server.ROOT/'sequence.jsonl';log.unlink(missing_ok=True)
fixtures=ensure()
for name,opts in [('copy.mp4',['-c','copy']),('mp3-audio.mp4',['-c:v','copy','-c:a','libmp3lame']),('video.mp4',['-an','-c:v','copy'])]:
    if not (fixtures/name).exists():subprocess.run(['ffmpeg','-v','error','-i',str(fixtures/'compatible.mp4'),*opts,'-movflags','+faststart',str(fixtures/name)],check=True)
if not (fixtures/'audio.webm').exists():subprocess.run(['ffmpeg','-v','error','-i',str(fixtures/'compatible.mp4'),'-vn','-c:a','libopus',str(fixtures/'audio.webm')],check=True)
(fixtures/'plan-events.jsonl').unlink(missing_ok=True)
server.streamer.plan_target=functools.partial(fixture_plan,base_url='http://127.0.0.1:8765/test-media',root=str(fixtures),browser_cases=True)
app=server.app
def stats():
    state={'active':0,'maximum':0,'attempts':{},'starts':[]}
    events=fixtures/'plan-events.jsonl'
    if events.exists():
        for line in events.read_text().splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            key=event['key'];state['attempts'][key]=state['attempts'].get(key,0)+1;state['starts'].append(key)
    state['maximum']=1
    return state
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount
app.router.routes.insert(0,Mount('/test-media',app=StaticFiles(directory=fixtures)))
app.router.routes.insert(0,APIRoute('/test-stats',stats,methods=['GET']))
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8765,log_level='warning')
