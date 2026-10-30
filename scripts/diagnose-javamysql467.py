#!/usr/bin/env python3
"""Source-only 180-second diagnosis on the immutable successful463 workload."""
import argparse
import base64
import subprocess
import time
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import uuid

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT.parent/'KTB4-2nd-Cloud/.local-docs'
PARENT=DOCS/'evidence/463-cdc-poll/integration'
BASE=DOCS/'evidence/467-java-mysql'
CASE='index50'
OLD=DOCS/'evidence/455-index-comparison'
EXPECTED_BASELINE='51474607af2aae8212b32bf4093fcde164dfcbda8f01345e88e8eac595559cb3'

def module(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def verify(root,manifest):
 for name,entry in manifest['files'].items():
  if hashlib.sha256((root/name).read_bytes()).hexdigest()!=entry['renderedSha256']:raise RuntimeError('Pinned workload differs: '+name)

def initialize():
 if BASE.exists():raise RuntimeError('Keep existing diagnostic evidence')
 manifest=json.loads((PARENT/'bundle-manifest.json').read_text());verify(PARENT/'runtime/pinned',manifest)
 if json.loads((PARENT/'index50/acceptance.json').read_text()).get('passed') is not True:raise RuntimeError('Completed parent required')
 BASE.mkdir();shutil.copytree(PARENT/'runtime',BASE/'runtime');shutil.copytree(PARENT/'helpers',BASE/'helpers')
 shutil.copyfile(PARENT/'helpers-manifest.json',BASE/'helpers-manifest.json')
 manifest=copy.deepcopy(manifest);manifest.update(issue=467,parent='87645d31-f2cd-49d3-8dc2-b41f968515a3');(BASE/'bundle-manifest.json').write_text(json.dumps(manifest,indent=2))
 p=BASE/CASE;p.mkdir();plan=json.loads((PARENT/'index50/plan.json').read_text())
 plan.update(issue=467,durationSeconds=180,cutoverSeconds=None,writerSwitch=False,diagnosticOnly=True,commonBaseline=None,pairedRestoreAttempt=None)
 plan['rates'][0]['runId']=str(uuid.uuid4());(p/'plan.json').write_text(json.dumps(plan,indent=2))
 print(json.dumps({'runId':plan['rates'][0]['runId'],'duration':180,'writerSwitch':False,'runtimeChanged':False}))

def load():
 verify(BASE/'runtime/pinned',json.loads((BASE/'bundle-manifest.json').read_text()))
 h=json.loads((BASE/'helpers-manifest.json').read_text())
 for n,digest in h['sha256'].items():
  if hashlib.sha256((BASE/'helpers/scripts'/n).read_bytes()).hexdigest()!=digest:raise RuntimeError('Helper changed')
 a=module('a467',BASE/'helpers/scripts/run-migration-cutover458.py');a.BASE=BASE;a.ROOT=BASE/'helpers'
 return a,*a.load()

def validate_plan(plan):
 parent=json.loads((PARENT/'index50/plan.json').read_text())
 for key in ['httpTimeoutSeconds','authResponseBudgetSeconds','sourceApplyConcurrency','snapshotBytesPerSecond','indexApplied','backendCommit','backendImage']:
  if plan.get(key)!=parent.get(key):raise RuntimeError('Fixed successful setting changed: '+key)
 if plan.get('durationSeconds')!=180 or plan.get('writerSwitch') is not False or plan.get('cutoverSeconds') is not None:raise RuntimeError('Diagnostic must not arm cutover')
 if len(plan['rates'])!=1 or any(plan['rates'][0][k]!=v for k,v in {'rate':50,'preAllocatedVUs':1000,'maxVUs':6000}.items()):raise RuntimeError('Scenario allocation changed')

def previous_restored(case):
 if case!=CASE:raise RuntimeError('Only one diagnostic case')

def render():
 manifest=json.loads((BASE/'bundle-manifest.json').read_text())
 if manifest.get('diagnosticOverlay'):raise RuntimeError('Do not render twice')
 overlay=module('overlay467',ROOT/'scripts/javamysql467-overlay.py');changes={}
 items=[('scripts/b-integration376-generator.py',overlay.generator),('tests/load/scenarios/migration-b-integration.js',overlay.scenario),('scripts/b-integration376-app.py',overlay.app),('scripts/b-integration393-resources.py',overlay.resources)]
 for name,fn in items:
  p=BASE/'runtime/pinned'/name;old=p.read_text();new=fn(old)
  if name.endswith('.py'):compile(new,name,'exec')
  p.write_text(new);changes[name]={'before':hashlib.sha256(old.encode()).hexdigest(),'after':hashlib.sha256(new.encode()).hexdigest()};manifest['files'][name]['renderedSha256']=changes[name]['after']
 manifest['diagnosticOverlay']=changes;(BASE/'bundle-manifest.json').write_text(json.dumps(manifest,indent=2))
 plan=json.loads((BASE/CASE/'plan.json').read_text());validate_plan(plan)
 source=(DOCS/'evidence/465-final-rps/tools/prepare.py').read_text()
 source=source.replace('KTB4-2nd-Cloud-465','KTB4-2nd-Cloud-467').replace('scripts/cdc-final465.py','scripts/diagnose-javamysql467.py').replace('a.load(case)','a.load()')
 tools=BASE/'tools';tools.mkdir();(tools/'prepare.py').write_text(source)
 print(json.dumps({'renderedFiles':list(changes),'processingChanges':False}))

def deploy():
 _,_,m,success=load();m.deploy()
 for host in ['source','target']:m.raw(host,m.ssm.deploy('scripts/b-integration376-reset.py','/run/yeodam-b391/b-integration376-reset.py'))
 text=success.render_bulk((BASE/'runtime/pinned/scripts/b-integration376-bulk.py').read_text())
 m.raw('target',["printf '%s' '"+base64.b64encode(text.encode()).decode()+"' | base64 -d > /run/yeodam-b391/b-integration376-bulk.py"])
 for src,dst in [('scripts/b-integration393-resources.py','/run/yeodam-b391/b-integration393-resources.py')]:m.raw('source',m.ssm.deploy(src,dst))
 blob=base64.b64encode((ROOT/'scripts/javamysql467-host.py').read_bytes()).decode()
 m.raw('source',["printf '%s' '"+blob+"' | base64 -d > /run/yeodam-b391/javamysql467-host.py"])

def source_http_ready(m):
 code="""import time,urllib.request
end=time.monotonic()+100
while time.monotonic()<end:
 try:
  for url in ['http://127.0.0.1:8080/api/actuator/health','https://v1-staging.yeodam-2gether.com/api/auth/csrf']:
   with urllib.request.urlopen(url,timeout=3) as response:
    if response.status!=200:raise RuntimeError('HTTP not ready')
  print('BASE_HTTP_READY');break
 except Exception:time.sleep(2)
else:raise RuntimeError('HTTP not ready before pool')
"""
 m.raw('source',["python3 - <<'HEALTH467'\n"+code+'\nHEALTH467'])

def preflight():
 _,_,m,_=load()
 code="""import json,subprocess,shutil,os
from pathlib import Path
assert '10.30.0.245' in subprocess.check_output(['hostname','-I'],text=True).split()
image='ghcr.io/100-hours-a-week/yeodam-backend@sha256:d39fd16fc92e77d9c51be9056a2d0c79e355fdf0ad81e38a8d463ab554fd2010'
print(json.dumps({'hostCpus':os.cpu_count(),'hostJcmd':shutil.which('jcmd'),'hostJavac':shutil.which('javac'),'jdkDirectories':[p.name for p in Path('/usr/lib/jvm').glob('*')],'rootFreeBytes':shutil.disk_usage('/').free,'aesExists':Path('/run/yeodam-cdc-auth368/aes.hex').is_file(),'pinnedImageCached':subprocess.run(['docker','image','inspect',image],capture_output=True).returncode==0}))
# Inspect running containers without printing their environment or credentials.
for name in ['yeodam-app-backend-1','yeodam-b391-app','yeodam-b391-consumer']:
 p=subprocess.run(['docker','inspect',name],capture_output=True,text=True)
 if p.returncode:continue
 v=json.loads(p.stdout)[0]
 print(json.dumps({'container':name,'running':v['State']['Running'],'image':v['Config']['Image']}))
 if v['State']['Running']:
  p=subprocess.run(['docker','exec',name,'sh','-c','command -v jcmd || true; java --list-modules'],capture_output=True,text=True)
  print(json.dumps({'container':name,'diagnosticModules':[x for x in p.stdout.splitlines() if any(t in x for t in ['jdk.jfr','jdk.attach','jdk.management','/jcmd'])]}))
"""
 out=m.raw('source',["python3 - <<'READONLY467'\n"+code+'\nREADONLY467']);(BASE/'source-tools-preflight.json').write_text(json.dumps(m.records(out),indent=2));print(json.dumps(m.records(out)))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('action',choices=['init','preflight','render','deploy']);a=p.parse_args();{'init':initialize,'render':render,'preflight':preflight,'deploy':deploy}[a.action]()
