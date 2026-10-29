#!/usr/bin/env python3
"""Diagnostic lifecycle: parent preparation,180s input,initial load,no Writer switch."""
import argparse
import inspect
import json
from pathlib import Path
import subprocess
import time
import importlib.util

def load_driver(root):
 s=importlib.util.spec_from_file_location('driver467',root/'scripts/diagnose-javamysql467.py');a=importlib.util.module_from_spec(s);s.loader.exec_module(a);return a

def preparation_source(text):
 anchor="    kafka_standby=raw("
 if text.count(anchor)!=1:raise ValueError('Parent handoff anchor differs')
 text=text[:text.index(anchor)].replace('def execute(case,index):','def prepare_diagnostic(case,index):',1)
 text=text.replace("'duration':900","'duration':180",1)
 point="    load_id=phase('generator','b-integration376-generator.py',run,'start','--rate',str(rate),sync=False)"
 if text.count(point)!=1:raise ValueError('Parent input anchor differs')
 text=text.replace(point,"    phase('source','javamysql467-host.py',run,'mysql','--label','before-input')\n"+point,1)
 text+='    return state\n'
 if any(x in text for x in ['kafka_standby=','fence_id=','service_start(']):raise ValueError('Diagnostic must never arm handoff')
 compile(text,'diagnostic-preparation','exec');return text

def resume_preparation_source(text):
 text=preparation_source(text)
 begin="    phase('source','b-integration376-control.py',run,'preflight')"
 end="    phase('source','b-integration376-app.py',run,'start-app')"
 if text.count(begin)!=1 or text.count(end)!=1:raise ValueError('Pre-input resume anchors differ')
 return text[:text.index(begin)]+text[text.index(end):]

def run(root,resume_after_transfer=False):
 a=load_driver(root);_,adapter,m,_=a.load();p=a.BASE/a.CASE;plan=json.loads((p/'plan.json').read_text());a.validate_plan(plan);run=plan['rates'][0]['runId']
 if (p/(run+'.state.json')).exists():raise RuntimeError('Started diagnosis is never replayed')
 if resume_after_transfer:
  receipt=json.loads((p/'pre-input-resume-receipt.json').read_text())
  if receipt.get('runId')!=run or receipt.get('inputStarted') is not False or receipt.get('privatePoolAlreadyTransferred') is not True or receipt.get('resumeFrom')!='start-app':raise RuntimeError('Exact pre-input resume receipt required')
  if not (p/'execute-invocation.json').exists() or (p/'resume-invocation.json').exists():raise RuntimeError('No repeat of resume/input')
 elif (p/'execute-invocation.json').exists():raise RuntimeError('Attempted diagnosis is never replayed')
 ready=json.loads((p/'ready.json').read_text());approval=json.loads((a.BASE/'transfer-approval.json').read_text())
 if approval.get('runId')!=run or approval.get('sessions')!=220 or approval.get('transfersAllowed')!=1 or approval.get('approved') is not True:raise RuntimeError('Exact private transfer approval missing')
 if not all(ready.get(k) is True for k in ['verified','targetBackupReady','all33TablesAndAutoIncrementEqual','indexApplied']):raise RuntimeError('Common baseline not restored')
 if plan['commonBaseline']['sha256']!=a.EXPECTED_BASELINE:raise RuntimeError('Wrong baseline')
 probe=json.loads((a.BASE/'jfr-probe.json').read_text())[-1]
 if probe.get('exitCode')!=0 or probe.get('deniedEvents')!={}:raise RuntimeError('JFR safety probe must pass before input')
 if m.ssm.aws('sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong account')
 ready.update(privatePoolTransferApproved=True,privatePoolTransferApproval=approval);(p/'ready.json').write_text(json.dumps(ready,indent=2))
 (p/('resume-invocation.json' if resume_after_transfer else 'execute-invocation.json')).write_text(json.dumps({'runId':run,'at':time.time(),'diagnosticOnly':True,'writerSwitch':False,'reuseTransferredPool':resume_after_transfer}))
 a.source_http_ready(m)
 render=resume_preparation_source if resume_after_transfer else preparation_source
 exec(render(inspect.getsource(m.execute)),m.__dict__)
 state=m.prepare_diagnostic(plan['rates'][0],2);started=state['startedAt'];path=p/(run+'.state.json')
 def checkpoint(phase,**values):
  state.update(phase=phase,**values);path.write_text(json.dumps(state,indent=2));m.log(phase,runId=run,**values)
 # Fixed fourth checkpoint is independent of Snapshot/Bulk completion.
 end_code="import time,subprocess\nat="+str(started+180)+"\nwhile time.time()<at:time.sleep(min(.2,max(0,at-time.time())))\nsubprocess.run(['python3','/run/yeodam-b391/javamysql467-host.py','mysql','--run-id','"+run+"','--label','input-end'],check=True)"
 end_id=m.raw('source',["python3 - <<'END467'\n"+end_code+'\nEND467'],sync=False,bound=300);state['mysqlEndCommand']=end_id;path.write_text(json.dumps(state,indent=2))
 m.kafka(run,'trips',2);m.kafka(run,'auth-source',2);m.kafka(run,'ready-trips',2);m.kafka(run,'ready-auth-source',2)
 m.phase('source','javamysql467-host.py',run,'mysql','--label','snapshot-start')
 import tempfile
 request={'InstanceIds':[m.ssm.HOSTS['source']],'DocumentName':'yeodam-v2-staging-mysql-source-snapshot','Parameters':{}}
 with tempfile.NamedTemporaryFile('w',suffix='.json') as f:
  json.dump(request,f);f.flush();snap=m.ssm.aws('ssm','send-command','--cli-input-json','file://'+f.name)['Command']['CommandId']
 checkpoint('snapshot_running',snapshotCommand=snap)
 result=m.wait('source',snap);m.phase('source','javamysql467-host.py',run,'mysql','--label','snapshot-end')
 marker=next(x.split('Source snapshot ready: ',1)[1].strip() for x in result['StandardOutputContent'].splitlines() if x.startswith('Source snapshot ready: '));prefix='s3://yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/'
 if not marker.startswith(prefix):raise RuntimeError('Snapshot outside approved bucket')
 marker=marker[len(prefix):];bulk=m.phase('target','b-integration376-bulk.py',run,None,'--source-marker',marker,sync=False);checkpoint('bulk_running',snapshotMarker=marker,bulkCommand=bulk)
 # Complete the same initial-load/CDC preparation, without arming any Writer controller.
 m.wait('target',bulk);m.kafka(run,'sink',2);m.kafka(run,'sink-journal',2)
 m.raw('source',['systemd-run --unit=yeodam-b391-auth-'+run[:8]+' --property=Type=exec /opt/yeodam-cdc-auth368/venv/bin/python3 /run/yeodam-b391/b-integration376-auth.py consume --run-id '+run])
 m.kafka(run,'auth-target',2);m.kafka(run,'ready-auth-target',2)
 m.raw('source',['systemd-run --unit=yeodam-b391-auth-target-'+run[:8]+' --property=Type=exec /opt/yeodam-cdc-auth368/venv/bin/python3 /run/yeodam-b391/b-integration376-auth.py consume --run-id '+run+' --origin target'])
 load=m.wait('generator',state['k6Command'],bound=400,allow_failure=True);result=m.records(m.phase('generator','b-integration376-generator.py',run,'audit'))[-1]
 checkpoint('input_finished',loadResult=result,mysqlEndStatus=m.wait('source',end_id,allow_failure=True)['Status'])
 m.sleep_until(started+300) # Fixed +120s drain observation, never extend input to force success.
 audit=m.records(m.phase('source','b-integration376-control.py',run,'audit-source'))[-1];checkpoint('fixed_source_audit',sourceAudit=audit,auditAt=time.time())
 m.phase('source','b-integration376-app.py',run,'stop-consumer')
 for kind in ['trips','auth-source','sink','sink-journal','auth-target']:m.kafka(run,'stop-'+kind,2,allow_failure=True)
 m.pause_auth(run)
 completion=m.records(m.phase('source','b-integration376-app.py',run,'stop-app'))[-1];checkpoint('apps_stopped',completionEvidence=completion)
 # Keep Source authoritative. No Target->Source copy or promotion ever occurs.
 m.phase('source','b-integration376-app.py',run,'restore-route')
 return state

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);args=p.parse_args();run(args.root)
