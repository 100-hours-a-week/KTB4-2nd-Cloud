#!/usr/bin/env python3
"""Execute the agreed 10/30/50 RPS plan with fixed stages and retained failures."""
import argparse
import ast
import base64
import importlib.util
import json
from pathlib import Path
import shlex
import signal
import socket
import os
import subprocess
import time
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ssm376',ROOT/'scripts/run-b-integration376.py');ssm=importlib.util.module_from_spec(spec);spec.loader.exec_module(ssm)
EVIDENCE=ssm.EVIDENCE
CLUSTER='yeodam-v2-staging-app'
SERVICE='yeodam-v2-staging-a-rehearsal-backend'
ROUTE='d7dae9c5-5808-4a63-9a35-96cec0659660'
DATASET='c0014ce1-0398-4a0c-95de-f502cb2e412e'
VUS={10:40,30:120,50:200}

def log(phase,**values):print(json.dumps({'phase':phase,'at':time.time(),**values}),flush=True)
def wait(host,cid,bound=1800,allow_failure=False):
    end=time.monotonic()+bound
    while time.monotonic()<end:
        try:r=ssm.result(host,cid)
        except RuntimeError:time.sleep(2);continue
        if r['Status'] not in ('Pending','InProgress','Delayed'):
            if r['Status']!='Success' and not allow_failure:raise RuntimeError('Host stage failed: '+host+' '+cid+'; inspect saved sanitized evidence')
            return r
        time.sleep(2)
    raise RuntimeError('Host stage still running; preserve command ID '+cid)
def raw(host,commands,*,sync=True,bound=1800,allow_failure=False):
    cid=ssm.send(host,commands,timeout=bound)
    return wait(host,cid,bound,allow_failure) if sync else cid

def phase(host,script,run,action=None,*extra,sync=True,allow_failure=False):
    args=([action] if action else [])+['--run-id',run,*extra]
    cmd='python3 /run/yeodam-b386/'+script+' '+shlex.join(args)
    return raw(host,[cmd],sync=sync,allow_failure=allow_failure)
def records(result):
    values=[]
    for line in result.get('StandardOutputContent','').splitlines():
        try:values.append(json.loads(line))
        except ValueError:pass
    return values

def deploy():
    source=['b-integration376-app.py','b-integration376-control.py','b-integration376-auth.py','b-integration376-e2e.py','b-integration376-reset.py','b-integration376-smoke.py','b-integration386-handoff.py']
    for host,files in [('source',source),('target',['b-integration376-bulk.py','b-integration376-reset.py']),('kafka',['b-integration376-kafka.py','b-integration383-cutover.py','b-integration386-handoff.py']),('generator',['b-integration376-generator.py'])]:
        commands=[]
        for name in files:commands+=ssm.deploy('scripts/'+name,'/run/yeodam-b386/'+name)
        if host=='source':
            commands+=ssm.deploy('scripts/audit-cdc-auth368.py','/opt/yeodam-cdc-auth368/audit-cdc-auth368.py')
            commands+=ssm.deploy('scripts/b2-pilot370-host.py','/run/yeodam-b386/image-pull.py')
            image=json.loads((EVIDENCE/'plan.json').read_text())['backendImage']
            commands+=["python3 - <<'PULL'\nfrom importlib.machinery import SourceFileLoader\nm=SourceFileLoader('pull382','/run/yeodam-b386/image-pull.py').load_module()\nm.IMAGE="+repr(image)+"\nm.pull_image()\nPULL"]
        if host=='target':
            tree=ast.parse((ROOT/'scripts/b2-pilot370-target.py').read_text())
            ddl=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DDL' for t in node.targets))
            ddl=ddl.replace('CREATE TABLE yeodam_v1_source.migration_trip_request_result', 'CREATE TABLE IF NOT EXISTS yeodam_v1_source.migration_trip_request_result')
            ddl+='\nCREATE TABLE IF NOT EXISTS yeodam_v1_source.migration_auth_request_result(request_id CHAR(64) PRIMARY KEY, encrypted_result MEDIUMTEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);\n'
            commands+=["printf '%s' '"+base64.b64encode(ddl.encode()).decode()+"' | base64 -d > /run/yeodam-b386/journal.sql"]
        if host=='generator':
            for name in ('scenarios/migration-b-integration.js','scripts/run-k6.sh'):
                commands+=ssm.deploy('tests/load/'+name,'/opt/yeodam-load/source/tests/load/'+name)
                commands+=['chown ubuntu:ubuntu /opt/yeodam-load/source/tests/load/'+name]
            commands+=ssm.deploy('tests/load/scripts/private-pool-transfer.py','/run/yeodam-b386/pool-transfer.py')
        raw(host,commands)
    log('artifacts_installed')

def transfer_pool(run):
    # Only encrypted SSM tunnels carry the pool. No credential file is created on Mac.
    sender=raw('source',['python3 /run/yeodam-private-pool-transfer.py send --file /run/yeodam-synthetic-sessions/'+run+'.json --port 18765'],sync=False,bound=150)
    receiver=raw('generator',['python3 /run/yeodam-b386/pool-transfer.py receive --file /opt/yeodam-load/source/tests/load/private/'+run+'.json --port 18765'],sync=False,bound=150)
    processes=[]
    sockets=[socket.socket(),socket.socket()]
    for sock in sockets:sock.bind(('127.0.0.1',0))
    local_ports=[sock.getsockname()[1] for sock in sockets]
    for sock in sockets:sock.close()
    try:
        for host,port in zip(('source','generator'),local_ports):
            params=json.dumps({'portNumber':['18765'],'localPortNumber':[str(port)]})
            file=(EVIDENCE/(run+'-tunnel-'+host+'.log')).open('w');file.chmod(0o600) if hasattr(file,'chmod') else None
            process=subprocess.Popen(['aws','ssm','start-session','--target',ssm.HOSTS[host],'--document-name','AWS-StartPortForwardingSession','--parameters',params,'--profile','yeodam-admin','--region','ap-northeast-2'],stdout=file,stderr=subprocess.STDOUT,start_new_session=True)
            processes.append((process,file))
        end=time.monotonic()+35
        for port in local_ports:
            while subprocess.run(['lsof','-nP','-iTCP:'+str(port),'-sTCP:LISTEN'],capture_output=True).returncode!=0:
                if time.monotonic()>end:raise RuntimeError('SSM pool tunnel did not open')
                time.sleep(1)
        with urlopen('http://127.0.0.1:'+str(local_ports[0])+'/pool',timeout=15) as response:data=response.read()
        pool=json.loads(data)
        if pool.get('runId')!=run:raise RuntimeError('Pool Run mismatch')
        with urlopen(Request('http://127.0.0.1:'+str(local_ports[1])+'/pool',data=data,method='POST',headers={'Content-Type':'application/json'}),timeout=15) as response:
            if response.status!=204:raise RuntimeError('Pool receiver rejected file')
        count=len(pool['sessions']);data=None;pool=None
        wait('source',sender,160);wait('generator',receiver,160)
        raw('generator',['chown ubuntu:ubuntu /opt/yeodam-load/source/tests/load/private/'+run+'.json','chmod 0600 /opt/yeodam-load/source/tests/load/private/'+run+'.json'])
        log('private_pool_transferred',runId=run,sessions=count,macFileCreated=False)
    finally:
        for process,file in processes:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
            file.close()

def auth(host_run,action,*,sync=True,role='source',allow_failure=False):
    command='/opt/yeodam-cdc-auth368/venv/bin/python3 /run/yeodam-b386/b-integration376-auth.py '+action+' --run-id '+host_run+' --mysql-role '+role
    return raw('source',[command],sync=sync,allow_failure=allow_failure)

def service_start(run):
    service=ssm.aws('ecs','describe-services','--cluster',CLUSTER,'--services',SERVICE)['services'][0]
    if service['desiredCount']!=0 or service['runningCount']!=0:raise RuntimeError('Isolated E2E service is already in use')
    old=service['taskDefinition']
    original=ssm.aws('ecs','describe-task-definition','--task-definition',old)['taskDefinition']
    allowed=('family','taskRoleArn','executionRoleArn','networkMode','containerDefinitions','volumes','placementConstraints','requiresCompatibilities','cpu','memory','runtimePlatform','ephemeralStorage')
    candidate={k:original[k] for k in allowed if k in original}
    found=0
    for container in candidate['containerDefinitions']:
        for item in container.get('environment',[]):
            if item['name']=='AUTH_SESSION_KEY_PREFIX':item['value']='yeodam:cutover:'+run+':';found+=1
    if found!=1:raise RuntimeError('Isolated Task lacks the expected Redis prefix')
    import tempfile
    with tempfile.NamedTemporaryFile('w',suffix='.json') as file:
        json.dump(candidate,file);file.flush()
        revision=ssm.aws('ecs','register-task-definition','--cli-input-json','file://'+file.name)['taskDefinition']['taskDefinitionArn']
    ssm.aws('ecs','update-service','--cluster',CLUSTER,'--service',SERVICE,'--task-definition',revision,'--desired-count','1')
    log('isolated_e2e_service_start',runId=run,taskDefinition=revision)
    return old

def service_stop(old):
    # Include tasks already requested to stop: desiredCount/runningCount can be zero while a task drains.
    tasks=list(set(ssm.aws('ecs','list-tasks','--cluster',CLUSTER,'--service-name',SERVICE,'--desired-status','RUNNING')['taskArns']+ssm.aws('ecs','list-tasks','--cluster',CLUSTER,'--service-name',SERVICE,'--desired-status','STOPPED')['taskArns']))
    ssm.aws('ecs','update-service','--cluster',CLUSTER,'--service',SERVICE,'--task-definition',old,'--desired-count','0')
    end=time.monotonic()+420
    while time.monotonic()<end:
        service=ssm.aws('ecs','describe-services','--cluster',CLUSTER,'--services',SERVICE)['services'][0]
        stopped=not tasks or all(t['lastStatus']=='STOPPED' for t in ssm.aws('ecs','describe-tasks','--cluster',CLUSTER,'--tasks',*tasks)['tasks'])
        if service['runningCount']==0 and stopped:return
        time.sleep(3)
    raise RuntimeError('Isolated E2E task is still stopping; do not reset Target')

def pause_auth(run):
    # Consumer is an owned systemd transient unit; signal closes and commits cleanly.
    raw('source',['systemctl stop yeodam-b386-auth-'+run[:8]+'.service yeodam-b386-auth-target-'+run[:8]+'.service'])

def kafka(run,action,index,**kwargs):return phase('kafka','b-integration376-kafka.py',run,action,'--index',str(index),**kwargs)
def sleep_until(at):
    while time.time()<at:time.sleep(min(2,at-time.time()))

def execute(case,index):
    run=case['runId'];rate=case['rate'];state_path=EVIDENCE/(run+'.state.json')
    if state_path.exists():raise RuntimeError('This Run already has execution evidence; do not rerun it')
    state={'runId':run,'rate':rate,'duration':900,'phase':'preparing'}
    def checkpoint(phase_name,**values):
        state.update(phase=phase_name,**values);state_path.write_text(json.dumps(state,indent=2));log(phase_name,runId=run,rate=rate,**values)
    phase('source','b-integration376-control.py',run,'preflight')
    kafka(run,'topics',index)
    prepared=subprocess.run(['python3',str(ROOT/'tests/load/scripts/run-synthetic-pool-ssm.py'),'prepare-existing','--instance-id',ssm.HOSTS['source'],'--run-id',run,'--dataset-id',DATASET,'--count',str(VUS[rate]+20)],capture_output=True,text=True)
    if prepared.returncode:raise RuntimeError('Synthetic pool preparation failed; no load started')
    transfer_pool(run)
    auth(run,'configure')
    phase('source','b-integration376-app.py',run,'auth-files')
    phase('source','b-integration376-app.py',run,'start-app')
    # One bounded health wait, before the measured load.
    raw('source',["python3 - <<'CHECK'\nimport time,urllib.request\nend=time.monotonic()+100\nwhile time.monotonic()<end:\n try:\n  r=urllib.request.urlopen('http://127.0.0.1:8080/api/actuator/health',timeout=2)\n  if r.status==200:print('B380_APP_HEALTH_200');break\n except Exception:pass\n time.sleep(2)\nelse:raise RuntimeError('Rehearsal BE did not become healthy')\nCHECK"])
    phase('source','b-integration376-app.py',run,'route-status')
    phase('source','b-integration376-app.py',run,'queue')
    try:phase('source','b-integration376-smoke.py',run)
    finally:phase('source','b-integration376-app.py',run,'direct')
    # Start and assign paused Consumers while Source is still idle, not during live Bulk.
    phase('source','b-integration376-app.py',run,'start-consumer')
    raw('source',["python3 - <<'PREWARM'\nimport time,urllib.request\nfrom importlib.machinery import SourceFileLoader\nm=SourceFileLoader('db386','/run/yeodam-b386/b-integration376-control.py').load_module()\nend=time.monotonic()+100\nwhile time.monotonic()<end:\n try:\n  if urllib.request.urlopen('http://127.0.0.1:18081/api/actuator/health',timeout=2).status==200:break\n except Exception:pass\n time.sleep(1)\nelse:raise RuntimeError('Consumer prewarm failed before input')\ndb=m.database()\ntry:\n end=time.monotonic()+45\n while time.monotonic()<end:\n  count=db.query('target',\"SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('root','event_scheduler','system user') AND ID!=CONNECTION_ID();\")\n  if count=='0':print('CONSUMER_PREPARED_TARGET_POOL_IDLE');break\n  time.sleep(1)\n else:raise RuntimeError('Paused Consumer did not release Target pool before input')\nfinally:db.close()\nPREWARM"])
    kafka_standby=raw('kafka',['/opt/yeodam-b383/venv/bin/python3 /run/yeodam-b386/b-integration386-handoff.py kafka --run-id '+run],sync=False,bound=1200)
    load_id=phase('generator','b-integration376-generator.py',run,'start','--rate',str(rate),sync=False)
    # Real timeout requires up to 6200 preallocated VUs; initialization precedes input.
    start_deadline=time.monotonic()+100
    while True:
        time.sleep(3)
        info=records(phase('generator','b-integration376-generator.py',run,'read'))[-1]
        if 'actualStartedAt' in info:break
        if 'k6ExitCode' in info:raise RuntimeError('k6 stopped before scenario setup; no cutover scheduled')
        if time.monotonic()>start_deadline:raise RuntimeError('No measured k6 start event; retain preparation failure')
    if not info.get('authActorMappingValid'):
        time.sleep(5);info=records(phase('generator','b-integration376-generator.py',run,'read'))[-1]
        if not info.get('authActorMappingValid'):
            phase('generator','b-integration376-generator.py',run,'stop')
            checkpoint('auth_actor_mapping_failed',k6Command=load_id,authActorAssignments=info.get('authActorAssignments'))
            raise RuntimeError('Authentication actor mapping invalid; no Snapshot or cutover scheduled')
    started=info['actualStartedAt'];checkpoint('load_started',k6Command=load_id,startedAt=started,authActorMappingValid=True)
    fence_id=raw('source',['/opt/yeodam-cdc-auth368/venv/bin/python3 /run/yeodam-b386/b-integration386-handoff.py source --run-id '+run+' --at '+str(started+360)],sync=False,bound=1000)
    state.update(fenceCommand=fence_id,kafkaHandoffCommand=kafka_standby);state_path.write_text(json.dumps(state,indent=2))
    kafka(run,'trips',index);kafka(run,'auth-source',index)
    kafka(run,'ready-trips',index);kafka(run,'ready-auth-source',index)
    # Capture full consistent data only after live requests and no_data CDC started.
    request={'InstanceIds':[ssm.HOSTS['source']],'DocumentName':'yeodam-v2-staging-mysql-source-snapshot','Parameters':{}}
    import tempfile
    with tempfile.NamedTemporaryFile('w',suffix='.json') as f:
        json.dump(request,f);f.flush();snapshot_id=ssm.aws('ssm','send-command','--cli-input-json','file://'+f.name)['Command']['CommandId']
    state['snapshotCommand']=snapshot_id;state_path.write_text(json.dumps(state,indent=2))
    snapshot=wait('source',snapshot_id)
    marker=next(line.split('Source snapshot ready: ',1)[1].strip() for line in snapshot['StandardOutputContent'].splitlines() if line.startswith('Source snapshot ready: '))
    prefix='s3://yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/'
    if not marker.startswith(prefix):raise RuntimeError('Snapshot URI is outside the approved bucket')
    marker=marker[len(prefix):]
    state['snapshotMarker']=marker
    state['bulkCommand']=phase('target','b-integration376-bulk.py',run,None,'--source-marker',marker,sync=False)
    state_path.write_text(json.dumps(state,indent=2))
    wait('target',state['bulkCommand'])
    return finish(case,index,state,state_path,load_id,fence_id,started)

def finish(case,index,state,state_path,load_id,fence_id,started,resume_boundary=False):
    run=case['runId'];rate=case['rate'];marker=state['snapshotMarker']
    def checkpoint(phase_name,**values):
        state.update(phase=phase_name,**values);state_path.write_text(json.dumps(state,indent=2));log(phase_name,runId=run,rate=rate,**values)
    if not resume_boundary:
        kafka(run,'sink',index)
        unit='yeodam-b386-auth-'+run[:8]
        raw('source',['systemd-run --unit='+unit+' --property=Type=exec /opt/yeodam-cdc-auth368/venv/bin/python3 /run/yeodam-b386/b-integration376-auth.py consume --run-id '+run])
        kafka(run,'auth-target',index);kafka(run,'ready-auth-target',index)
        raw('source',['systemd-run --unit=yeodam-b386-auth-target-'+run[:8]+' --property=Type=exec /opt/yeodam-cdc-auth368/venv/bin/python3 /run/yeodam-b386/b-integration376-auth.py consume --run-id '+run+' --origin target'])
        raw('source',['touch /run/yeodam-b386/'+run+'/cutover-ready'])
        checkpoint('bulk_and_cdc_started',snapshotMarker=marker)
    fence=wait('source',fence_id,bound=500,allow_failure=True)
    handoff=records(fence)
    switched=fence['Status']=='Success' and bool(handoff and handoff[-1].get('switched'))
    old_service=None
    if switched:
        old_service=service_start(run)
        checkpoint('switched',originalE2ETaskDefinition=old_service,cutover=handoff)
        phase('source','b-integration376-control.py',run,'audit-source')
    else:checkpoint('handoff_failed',handoff=handoff)
    # Reserve E2E slots before k6 grace time, independently from its last response.
    e2e_ids=None
    if switched:
        at=started+1020
        e2e_refresh=phase('source','b-integration376-e2e.py',run,'refresh',allow_failure=True)
        v1_id=phase('source','b-integration376-e2e.py',run,'v1','--at',str(at),sync=False)
        v2_id=phase('source','b-integration376-e2e.py',run,'e2e','--route-header',ROUTE,'--at',str(at),sync=False)
        e2e_ids=(v1_id,v2_id)
        audit_at=started+1010
        audit_code="import time,subprocess,json\nat="+str(audit_at)+"\nwhile time.time()<at:time.sleep(max(0,min(.2,at-time.time())))\nstart=time.time()\np=subprocess.run(['/opt/yeodam-cdc-auth368/venv/bin/python3','/run/yeodam-b386/b-integration376-auth.py','audit','--run-id','"+run+"','--mysql-role','target'],capture_output=True,text=True)\nprint(p.stdout,end='')\nprint(json.dumps({'auditStartedAt':start,'auditFinishedAt':time.time(),'auditExitCode':p.returncode}))"
        auth_id=raw('source',["python3 - <<'AUDIT'\n"+audit_code+"\nAUDIT"],sync=False)
        checkpoint_id=raw('source',["python3 - <<'POINT'\nimport time,subprocess\nat="+str(at)+"\nwhile time.time()<at:time.sleep(max(0,min(.2,at-time.time())))\nsubprocess.run(['python3','/run/yeodam-b386/b-integration376-control.py','checkpoint','--run-id','"+run+"'],check=True)\nPOINT"],sync=False)

    # Every launched run retains its fixed duration, including strategy failures.
    load=wait('generator',load_id,bound=1000,allow_failure=True)
    result=records(phase('generator','b-integration376-generator.py',run,'audit'))[-1]
    result['fixedInputEndAt']=started+900
    result['k6FinishedAfterInputSeconds']=result['finishedAt']-(started+900)
    checkpoint('load_finished',loadResult=result)
    # Fixed checkpoint, no per-request completion polling. E2E is scheduled independently from SQL audits.
    if switched:
        checkpoint_at=result['fixedInputEndAt']+120
        v1_id,v2_id=e2e_ids
        auth_result=wait('source',auth_id,allow_failure=True)
        auth_values=[r for r in records(auth_result) if 'expected_keys' in r]
        timing=records(auth_result)[-1]
        audit_started=timing.get('auditStartedAt',0);audit_finished=timing.get('auditFinishedAt',time.time())
        checkpoint('auth_before_e2e',auth=auth_values,authAuditStartedAt=audit_started,authAuditFinishedAt=audit_finished)
        v1_e2e=wait('source',v1_id,allow_failure=True)
        e2e=wait('source',v2_id,allow_failure=True)
        checkpoint('e2e_recorded',e2e=records(e2e),v1E2E=records(v1_e2e),freshCredentialPreparation=records(e2e_refresh),fixedCheckpoint=records(wait('source',checkpoint_id,allow_failure=True)),authAuditBeforeE2E=bool(auth_values) and audit_finished<min([v.get('startedAt',0) for v in records(e2e)+records(v1_e2e)] or [0]))
        phase('source','b-integration376-app.py',run,'stop-consumer')
        target_position=records(phase('source','b-integration376-control.py',run,'target-position'))[-1]['targetPosition'].split('\t')
        drained=phase('kafka','b-integration376-kafka.py',run,'drain-target','--index',str(index),'--source-file',target_position[0],'--source-position',target_position[1],'--deadline',str(time.time()+120),allow_failure=True)
        pause_auth(run)
        db_result=phase('source','b-integration376-control.py',run,'audit-target',allow_failure=True)
        checkpoint('final_data_audited',auth=auth_values,database=records(db_result),targetAuthBoundaryStatus=drained['Status'])
        service_stop(old_service)
    else:
        phase('source','b-integration376-app.py',run,'stop-consumer')
        # Keep failed queues and offsets. Reopen Source only if promotion never began.
        for kind in ('trips','auth-source','sink','auth-target'):
            kafka(run,'stop-'+kind,index,allow_failure=True)
        pause_auth(run)
        source_audit=phase('source','b-integration376-control.py',run,'audit-source',allow_failure=True)
        target_audit=phase('source','b-integration376-control.py',run,'audit-target',allow_failure=True)
        checkpoint('unpromoted_evidence',sourceAudit=records(source_audit),targetAudit=records(target_audit),e2eExecuted=False,e2eBlockedReason='Target not promoted')
        phase('source','b-integration376-control.py',run,'recover-unpromoted')
        checkpoint('strategy_failed_unpromoted')
    phase('source','b-integration376-app.py',run,'restore-route')
    phase('source','b-integration376-app.py',run,'stop-app')
    raw('source',['rm -f -- /run/yeodam-b386/'+run+'/container-auth/aes.hex /run/yeodam-b386/'+run+'/container-auth/redis.json'])
    if switched:
        # Both sides are preserved before the between-run maintenance copy.
        phase('source','b-integration376-control.py',run,'park-target-pool')
        phase('target','b-integration376-reset.py',run,'target-snapshot')
        phase('source','b-integration376-reset.py',run,'source-restore')
        phase('source','b-integration376-control.py',run,'resume-source')
    phase('source','b-integration376-app.py',run,'restore-original')
    cleanup=subprocess.run(['python3',str(ROOT/'tests/load/scripts/run-synthetic-pool-ssm.py'),'cleanup-existing','--instance-id',ssm.HOSTS['source'],'--run-id',run,'--dataset-id',DATASET],capture_output=True,text=True)
    if cleanup.returncode:raise RuntimeError('Scoped session cleanup failed; preserve files and investigate')
    raw('generator',['rm -- /opt/yeodam-load/source/tests/load/private/'+run+'.json'])
    raw('source',['rm -f -- /run/yeodam-b386/'+run+'/e2e-pool.json /run/yeodam-b386/'+run+'/e2e-v1-pool.json'])
    checkpoint('completed',switched=switched)
    return state

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('deploy','execute'));p.add_argument('--rates',type=int,nargs='+',choices=(10,30,50),default=[10,30,50]);p.add_argument('--resume-bulk',action='store_true');a=p.parse_args()
    if ssm.aws('sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong AWS account')
    plan=json.loads((EVIDENCE/'plan.json').read_text())
    if a.action=='deploy':deploy();return
    for index,case in enumerate(plan['rates']):
        if case['rate'] not in a.rates:continue
        if a.resume_bulk:
            path=EVIDENCE/(case['runId']+'.state.json');state=json.loads(path.read_text())
            if state.get('phase')!='load_started' or not state.get('bulkCommand') or not state.get('fenceCommand'):raise RuntimeError('No known pre-mutation marker-correction checkpoint')
            wait('target',state['bulkCommand'])
            finish(case,index,state,path,state['k6Command'],state['fenceCommand'],state['startedAt'])
        else:execute(case,index)
if __name__=='__main__':main()
