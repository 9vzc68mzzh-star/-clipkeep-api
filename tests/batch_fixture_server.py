"""Test-only source adapter; the normal API and child-process queue are real."""
import json
import os
import sys
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'backend' if (BASE/'backend').exists() else BASE))
os.environ.update(CLIPKEEP_PUBLIC_DOWNLOADS='true',CORS_ORIGINS='http://127.0.0.1:8765',PUBLIC_SITE_ORIGIN='')
import server
from prepare_fixtures import ensure
from sequence_fixture import browser_worker
server.ROOT=ensure()/'browser-jobs';server.ROOT.mkdir(exist_ok=True)
log=server.ROOT/'sequence.jsonl';log.unlink(missing_ok=True)
server.prepare_worker=browser_worker
app=server.app
def stats():
    state={'active':0,'maximum':0,'attempts':{},'starts':[]}
    if log.exists():
        for line in log.read_text().splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if event['kind']=='start':
                state['active']+=1;state['maximum']=max(state['maximum'],state['active'])
                key=event['key'];state['attempts'][key]=state['attempts'].get(key,0)+1;state['starts'].append(key)
            elif event['kind']=='finish':state['active']-=1
    return state
from fastapi.routing import APIRoute
app.router.routes.insert(0,APIRoute('/test-stats',stats,methods=['GET']))
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8765,log_level='warning')
