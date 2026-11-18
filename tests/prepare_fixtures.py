"""Small deterministic video+audio fixtures for the offline integration suite."""
import os
import subprocess
import tempfile
from pathlib import Path
ROOT=Path(os.environ.get('KLIPNIVO_TEST_DIR',Path(tempfile.gettempdir())/'klipnivo-tests'))
def ensure():
    ROOT.mkdir(parents=True,exist_ok=True)
    for name,codec,audio in [('compatible.mp4','libx264','aac'),('incompatible.webm','libvpx-vp9','libopus')]:
        target=ROOT/name
        if target.exists():continue
        opts=['-preset','ultrafast','-pix_fmt','yuv420p'] if codec=='libx264' else ['-deadline','realtime','-cpu-used','8']
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=24',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','1.2',
                        '-c:v',codec,*opts,'-c:a',audio,'-y',str(target)],check=True)
    return ROOT
if __name__=='__main__':print(ensure())
