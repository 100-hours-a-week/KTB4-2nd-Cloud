#!/usr/bin/env python3
"""Pinned collectors at production CPU/memory; no private application images or external services."""
import json,os,platform,subprocess,tempfile,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
lock=json.loads((ROOT/'deploy/v3/apm/images.lock.json').read_text())
prefix='apm-capacity-'+uuid.uuid4().hex[:8];names=[]
platform_name='linux/arm64'if platform.system()=='Darwin'and platform.machine()=='arm64'else'linux/amd64'
out=Path(os.environ.get('V3_APM_RESULTS','/tmp/'+prefix));out.mkdir(parents=True,exist_ok=True)
def run(*args,timeout=200):
 p=subprocess.run(args,text=True,capture_output=True,timeout=timeout)
 if p.returncode:raise RuntimeError((p.stdout+p.stderr)[-2500:])
 return p.stdout
try:
 with tempfile.TemporaryDirectory(prefix=prefix)as directory:
  sdk=Path(directory)/'sdk';extract=prefix+'-sdk'
  try:
   run('docker','create','--platform','linux/amd64','--name',extract,lock['python']);run('docker','cp',extract+':/autoinstrumentation',str(sdk))
  finally:subprocess.run(['docker','rm','-f',extract],capture_output=True)
  run('docker','network','create','--internal',prefix)
  for name,cpu,memory,go in [('alloy','.2','128m','80MiB'),('tempo','.5','384m','256MiB')]:
   n=prefix+'-'+name;names.append(n)
   args=['docker','run','-d','--name',n,'--platform',platform_name,'--network',prefix,'--network-alias','v3-'+name+'.observability.svc','--user','10001:10001','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory',memory,'--cpus',cpu,'-e','GOMEMLIMIT='+go,'-e','GOGC=50','-v',str(ROOT/'deploy/v3/apm/files')+':/etc/apm:ro','--tmpfs',('/var/alloy'if name=='alloy'else'/var/tempo')+':uid=10001,gid=10001,mode=0700']
   command=['run','--server.http.listen-addr=0.0.0.0:12345','--storage.path=/var/alloy','--disable-reporting','/etc/apm/config.alloy']if name=='alloy'else['-target=all','-config.file=/etc/apm/tempo.yaml']
   run(*args,lock[name],*command)
  probe=prefix+'-probe';names.append(probe)
  # The public SDK carries protobuf. No app code, credentials or external model is loaded.
  result=run('docker','run','--name',probe,'--platform','linux/amd64','--network',prefix,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','128m','--cpus','.5','-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPATH=/otel','-e','PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python','-v',str(sdk)+':/otel:ro','-v',str(ROOT/'tests/ci/v3/apm')+':/fixture:ro','-v',str(ROOT/'scripts')+':/evidence:ro','-v',str(ROOT/'deploy/v3/apm/budget.json')+':/budget.json:ro','python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b','python','/fixture/capacity-fixture.py')
  (out/'capacity.json').write_text(result);print(result)
  for n in names[:2]:
   state=json.loads(run('docker','inspect',n))[0]['State'];assert not state['OOMKilled'] and state['Running'],state
   memory=run('docker','stats','--no-stream','--format','{{.MemUsage}}',n).strip()
   print(n+' observedMemory='+memory)
except Exception:
 raise
finally:
 for n in names:
  logs=subprocess.run(['docker','logs',n],capture_output=True,text=True);(out/(n+'.log')).write_text(logs.stdout+logs.stderr)
 subprocess.run(['docker','rm','-f',*names],capture_output=True)
 subprocess.run(['docker','network','rm',prefix],capture_output=True)
 print('Synthetic diagnostics: '+str(out))
