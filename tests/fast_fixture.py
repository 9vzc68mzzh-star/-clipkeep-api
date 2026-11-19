"""Offline media plans for streaming/prefetch tests. Never loaded in production."""
import os
import json
import time
from pathlib import Path

def fixture_plan(job_id,selection,initial,connection,base_url,root,browser_cases=False):
    os.setsid();index=int(initial['url'].split('=')[-1][-8:]);root=Path(root)
    time.sleep(.06)
    if browser_cases:
        with (root/'plan-events.jsonl').open('a') as log:log.write(json.dumps({'key':'TST'+str(index).zfill(8),'attempt':initial['attempt']})+'\n')
        if index==13 and initial['attempt']==1 or index==17 and initial['attempt']<=2:
            connection.send({'kind':'error','code':'network_error','type':'fixture'});connection.close();return
    mode=['direct','audio_aac','merge'][(index-1)%3]
    files={'direct':['copy.mp4'],'audio_aac':['mp3-audio.mp4'],'merge':['video.mp4','audio.webm']}[mode]
    details={'video_codec':'h264','pix_fmt':'yuv420p','audio_codec':'aac','width':320,'height':180,
             'size':(root/files[0]).stat().st_size if mode=='direct' else 0}
    plan={'mode':mode,'kind':'mp4','audio_copy':mode=='direct','details':details,'source_format':mode,
          'fixture_index':index,'fixture_attempt':initial.get('attempt',1),'sources':[{'url':base_url+'/'+name+'?clip='+str(index),'headers':{},'format_id':name} for name in files]}
    fields={'plan':plan,'timings':{'resolve_s':0.,'extract_s':.06,'probe_s':0.},'resolved_url':initial['url'],
            'title':f'Fixture {index:02d}','height':180,'quality_label':180,'duration':1.2,'mode':mode,'source_format':mode,
            'video_codec':'h264','audio_codec':'aac'}
    connection.send({'kind':'result','fields':fields,'progressed':True});connection.close()
