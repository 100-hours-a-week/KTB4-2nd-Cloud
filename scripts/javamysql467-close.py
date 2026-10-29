#!/usr/bin/env python3
"""Preserve diagnostic evidence and return only owned runtime to its original state."""
import argparse
import base64
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]

def main():
 s=importlib.util.spec_from_file_location('a467close',ROOT/'scripts/diagnose-javamysql467.py');a=importlib.util.module_from_spec(s);s.loader.exec_module(a);_,_,m,_=a.load();p=a.BASE/a.CASE;plan=json.loads((p/'plan.json').read_text());run=plan['rates'][0]['runId'];sp=p/(run+'.state.json');state=json.loads(sp.read_text())
 if state.get('phase')!='apps_stopped':raise RuntimeError('Preserve before cleanup; apps must have stopped')
 # Both stopped applications flush JFR on exit; parse with the same installed JRE.
 code='''import json,subprocess
from pathlib import Path
run=RUN;d=Path('/run/yeodam-javamysql467')/run;prefix='migration/performance455/diagnostics467/'+run+'/'
image='ghcr.io/100-hours-a-week/yeodam-backend@sha256:d39fd16fc92e77d9c51be9056a2d0c79e355fdf0ad81e38a8d463ab554fd2010'
reports={}
for role in ['app','consumer']:
 f=d/role/'trace.jfr'
 if not f.is_file():raise RuntimeError('Missing '+role+' JFR; retain runtime evidence')
 f.chmod(0o600)
 # Root reads the private files after the measured interval; no network/ports.
 cmd=['docker','run','--rm','--network','none','--cpus','1','--memory','256m','--pids-limit','64','--mount','type=bind,source='+str(d/'app')+',target=/run/reader:ro','--mount','type=bind,source='+str(d/role)+',target=/run/trace:ro','--entrypoint','java',image,'-Xms8m','-Xmx96m','-cp','/run/reader','Jfr467Summary','/run/trace/trace.jfr',str(FROM),str(TO)]
 result=subprocess.run(cmd,capture_output=True,text=True)
 if result.returncode:raise RuntimeError('JFR analysis/privacy validation failed for '+role)
 report=json.loads(result.stdout)
 if report['deniedEventCounts']:raise RuntimeError('Unexpected sensitive events; quarantine file')
 target=d/role/'analysis.json';target.write_text(json.dumps(report));target.chmod(0o600)
 for source,name in [(f,role+'.jfr'),(target,role+'-jfr-analysis.json')]:
  subprocess.run(['aws','s3','cp',str(source),'s3://yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/'+prefix+name,'--region','ap-northeast-2','--sse','AES256','--only-show-errors'],check=True,capture_output=True)
 reports[role]={'bytes':f.stat().st_size,'executionSamples':report['executionSamples'],'secretEventCount':0,'fromEpochMs':FROM,'toEpochMs':TO}
for label in ['before-input','snapshot-start','snapshot-end','input-end']:
 f=d/('mysql-'+label+'.json')
 if not f.is_file():raise RuntimeError('Missing MySQL checkpoint '+label)
 subprocess.run(['aws','s3','cp',str(f),'s3://yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/'+prefix+f.name,'--region','ap-northeast-2','--sse','AES256','--only-show-errors'],check=True,capture_output=True)
for name in ['resource-samples.jsonl','completions.json','snapshot-stream.json']:
 f=Path('/run/yeodam-b391')/run/name
 if not f.is_file():raise RuntimeError('Missing diagnostic '+name)
 subprocess.run(['aws','s3','cp',str(f),'s3://yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/'+prefix+name,'--region','ap-northeast-2','--sse','AES256','--only-show-errors'],check=True,capture_output=True)
print(json.dumps({'runId':run,'jfr':reports,'mysqlCheckpoints':4,'privatePoolOrEnvironmentArchived':False,'prefix':prefix}))
'''.replace('RUN',repr(run)).replace('FROM',str(round(state['startedAt']*1000))).replace('TO',str(round((state['startedAt']+180)*1000)))
 r=m.raw('source',["python3 - <<'ARCHIVE467'\n"+code+'\nARCHIVE467']);(p/'archive-receipt.json').write_text(json.dumps(m.records(r),indent=2));print(json.dumps(m.records(r)),flush=True)
 # Snapshot both DBs after stopping CDC/auth writers. Source remained authoritative.
 def preserve(role):
  code='''import importlib.util,json,time
from pathlib import Path
s=importlib.util.spec_from_file_location('r','/run/yeodam-b391/b-integration376-reset.py');r=importlib.util.module_from_spec(s);s.loader.exec_module(r)
role=ROLE;run=RUN
if role=='target':r.query(role,'SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')
path=Path('/var/lib/yeodam/diagnostics467')/run/(role+'.sql.gz');path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
if path.exists():raise RuntimeError('Diagnostic SQL already preserved; inspect receipt')
r.snapshot(role,path);key='migration/performance455/diagnostics467/'+run+'/'+role+'.sql.gz'
import subprocess
subprocess.run(['aws','s3','cp',str(path),'s3://'+r.BUCKET+'/'+key,'--region','ap-northeast-2','--sse','AES256','--only-show-errors'],check=True,capture_output=True)
print(json.dumps({'role':role,'key':key,'sha256':r.sha(path),'bytes':path.stat().st_size,'flags':r.query(role,'SELECT @@read_only,@@super_read_only;'),'preservedAt':time.time()}))
'''.replace('ROLE',repr(role)).replace('RUN',repr(run))
  out=m.raw(role,["python3 - <<'PRESERVE467'\n"+code+'\nPRESERVE467']);(p/('final-preserved-'+role+'.json')).write_text(json.dumps(m.records(out),indent=2));return m.records(out)
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
  for result in pool.map(preserve,['source','target']):print(json.dumps(result),flush=True)
 # Scoped synthetic cleanup uses the existing tool and the exact approved pool.
 cleaned=subprocess.run(['python3',str(a.BASE/'runtime/pinned/tests/load/scripts/run-synthetic-pool-ssm.py'),'cleanup-existing','--instance-id',m.ssm.HOSTS['source'],'--run-id',run,'--dataset-id',m.DATASET],capture_output=True,text=True)
 if cleaned.returncode:raise RuntimeError('Scoped pool cleanup failed; preserve files')
 m.raw('generator',['rm -- /opt/yeodam-load/source/tests/load/private/'+run+'.json'])
 m.raw('source',['rm -f -- /run/yeodam-b391/'+run+'/container-auth/aes.hex /run/yeodam-b391/'+run+'/container-auth/redis.json /run/yeodam-b391/'+run+'/container-auth/control.token'])
 m.phase('source','b-integration376-app.py',run,'restore-original');a.source_http_ready(m)
 code="""import json,subprocess,urllib.request
get=lambda u:json.load(urllib.request.urlopen(u,timeout=10))
s=get('http://127.0.0.1:18083/connectors?expand=status')
assert all(v['status']['connector']['state']=='STOPPED' and not v['status']['tasks'] for v in s.values())
shared=get('http://127.0.0.1:8083/connectors/yeodam-staging-outbox/status')
assert shared['connector']['state']=='RUNNING' and all(t['state']=='RUNNING' for t in shared['tasks'])
subprocess.run(['docker','stop','yeodam-b383-connect'],check=True,capture_output=True)
print(json.dumps({'ownedWorkerStopped':True,'stoppedConnectors':len(s),'outboxRunning':True,'topicsOrOffsetsDeleted':False}))
"""
 out=m.raw('kafka',["python3 - <<'WORKER467'\n"+code+'\nWORKER467']);(p/'runtime-final-kafka.json').write_text(json.dumps(m.records(out),indent=2))
 status=m.records(m.phase('source','b-integration376-control.py',run,'status'))[-1];(p/'runtime-final-source.json').write_text(json.dumps(status,indent=2))
 if status.get('source')!=['0','0'] or status.get('target')!=['1','1'] or status.get('proxy')!='10':raise RuntimeError('Final Writer route differs')
 state.update(phase='completed',writerSwitched=False,diagnosticOnly=True,runtimeRestored=True,finishedAt=time.time());sp.write_text(json.dumps(state,indent=2));print(json.dumps({'phase':'completed','runId':run,'writerSwitched':False,'runtimeRestored':True}),flush=True)

if __name__=='__main__':main()
