#!/usr/bin/env python3
"""Run a bounded no-network image compatibility fixture. Registry auth is external."""
import argparse,json,subprocess,tempfile,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path);args=p.parse_args()
lock=json.loads((ROOT/'tests/ci/v3/ai-observability-image.json').read_text())
if args.candidate:
    candidate=json.loads(args.candidate.read_text())
    if not candidate['values']['ai'].get('traceContextEnabled'):
        print('Async Trace disabled; previous image contract preserved');raise SystemExit(0)
    image=candidate['components']['ai-app']['image']
else:image=lock['image']
sdk=json.loads((ROOT/'deploy/v3/apm/images.lock.json').read_text())['python']
name='v3-ai-trace-extract-'+uuid.uuid4().hex[:8]
with tempfile.TemporaryDirectory(prefix='647-ai-trace-')as d:
    try:
        subprocess.run(['docker','create','--platform','linux/amd64','--name',name,sdk],check=True,capture_output=True)
        subprocess.run(['docker','cp',name+':/autoinstrumentation',str(Path(d)/'otel')],check=True,capture_output=True)
        command=['docker','run','--rm','--platform','linux/amd64','--network','none','--read-only','--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','512m','--cpus','.5','--tmpfs','/tmp:size=64m,mode=1777','-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPATH=/otel:/app','-v',d+'/otel:/otel:ro','-v',str(ROOT/'deploy/v3/ai-apps/files/apm')+':/hook:ro','-v',str(ROOT/'tests/ci/v3/apm/ai-observability-fixture.py')+':/fixture.py:ro','--entrypoint','python',image,'/fixture.py']
        subprocess.run(command,check=True,timeout=120)
    finally:subprocess.run(['docker','rm','-f',name],capture_output=True)
