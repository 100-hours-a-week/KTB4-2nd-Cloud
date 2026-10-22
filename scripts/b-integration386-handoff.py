#!/usr/bin/env python3
"""Prearmed host controllers exchange a scoped CDC proof without an SSM round trip."""
import argparse,json,subprocess,time,hmac,hashlib
from importlib.machinery import SourceFileLoader
from pathlib import Path
from uuid import UUID
from confluent_kafka import Consumer,Producer,TopicPartition

ROOT=Path('/run/yeodam-b391')
def topic(run):return 'staging.yeodam.migration.b391.'+run+'.handoff-proof'
def signature(proof,key):
    return hmac.new(key,b'b391-handoff-proof:'+json.dumps(proof,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
def store(path,value):
    temp=path.with_suffix('.next');temp.write_text(json.dumps(value));temp.chmod(0o600);temp.replace(path)
def wait_prepared(run,latest):
    control=SourceFileLoader('ready402',str(ROOT/'b-integration376-control.py')).load_module()
    while time.time()<latest:
        if (ROOT/run/'cutover-ready').exists():
            try:
                control.ensure_prepared(run)
                observed=time.time()
                if observed<=latest:return observed
            except (OSError,RuntimeError,subprocess.CalledProcessError):pass
        time.sleep(.1)
    raise RuntimeError('Preparation not ready by original +360s limit')

def publish_start(run,at):
    value={'runId':run,'kind':'cutover_start','fenceAt':at}
    key=bytes.fromhex(Path('/run/yeodam-cdc-auth368/aes.hex').read_text().strip())
    value['signature']=signature(value,key)
    producer=Producer({'bootstrap.servers':'10.30.10.95:9092','acks':'all'})
    errors=[]
    producer.produce(topic(run),key=run,value=json.dumps(value),on_delivery=lambda e,msg:errors.append(e) if e else None)
    if producer.flush(2) or errors:raise RuntimeError('Cutover start publication failed; Source not fenced')

def receive_start(client,run,latest,key):
    while time.time()<latest+45:
        msg=client.poll(.1)
        if msg is None:continue
        if msg.error():raise RuntimeError('Cutover start stream error')
        value=json.loads(msg.value());signed=value.pop('signature','')
        if not hmac.compare_digest(signed,signature(value,key)):raise RuntimeError('Cutover start signature rejected')
        if value.get('runId')!=run or value.get('kind')!='cutover_start':raise RuntimeError('Cutover start Run/type rejected')
        at=value.get('fenceAt')
        if not isinstance(at,(int,float)) or not 0<at<=latest or not 0<=time.time()-at<=45:
            raise RuntimeError('Cutover start outside existing budget')
        return at
    raise RuntimeError('Cutover start deadline exceeded')

def kafka(run,at,ready=False):
    m=SourceFileLoader('boundary388',str(ROOT/'b-integration383-cutover.py')).load_module()
    key=bytes.fromhex(Path('/etc/yeodam/kafka/cdc/auth368-aes.hex').read_text().strip())
    delivery_attempted=False
    def publish(value):
        nonlocal delivery_attempted
        proof=dict(value);proof['signature']=signature(proof,key)
        producer=Producer({'bootstrap.servers':'10.30.10.95:9092','acks':'all'})
        errors=[];delivery_attempted=True
        producer.produce(topic(run),key=run,value=json.dumps(proof),on_delivery=lambda e,msg:errors.append(e) if e else None)
        if producer.flush(2) or errors:raise RuntimeError('Handoff proof publication failed')
        print(json.dumps({'runId':run,'proofPublished':True,'equal':proof['equal'],'failureCode':proof.get('failureCode'),'stage':proof.get('stage'),'at':time.time()}),flush=True)
    trigger=None
    if ready:
        trigger=Consumer({'bootstrap.servers':'10.30.10.95:9092','group.id':'yeodam-b402-start-'+run,'enable.auto.commit':False})
        trigger.assign([TopicPartition(topic(run),0,0)])
    try:m.cutover(run,'standby',marker_timeout=900,cutover_at=(lambda:receive_start(trigger,run,at,key)) if ready else at,on_proof=publish)
    except m.BoundaryError as e:
        if delivery_attempted:raise
        publish(e.proof(run))
    finally:
        if trigger is not None:trigger.close()
def source(run,at,ready=False):
    directory=ROOT/run;directory.mkdir(parents=True,exist_ok=True)
    c=Consumer({'bootstrap.servers':'10.30.10.95:9092','group.id':'yeodam-b391-proof-'+run,'enable.auto.commit':False})
    c.assign([TopicPartition(topic(run),0,0)])
    result={'runId':run,'switched':False,'controllerArmedAt':time.time(),'plannedFenceAt':at,'cdcDeadlineAt':at+45,'activationDeadlineAt':at+50}
    def action(name,*extra):
        p=subprocess.run(['python3',str(ROOT/'b-integration376-control.py'),name,'--run-id',run,*extra],capture_output=True,text=True)
        if p.returncode:raise RuntimeError('Local handoff action failed: '+name)
    try:
        if ready:
            result['latestFenceAt']=at
            at=wait_prepared(run,at)
            result.update(readyObservedAt=at,plannedFenceAt=at,budgetStartedAt=at,cdcDeadlineAt=at+45,activationDeadlineAt=at+50)
            # A signed start shares the SAME budget origin with the prearmed Kafka host.
            # Broker delivery and Source drain are both charged to this budget.
            publish_start(run,at)
            action('fence','--budget-started-at',str(at))
        else:action('fence','--at',str(at))
        result['fencedAt']=time.time()
        end=at+47 # 45s validation + up to 2s proof delivery; 3s activation reserve.
        while time.time()<end:
            msg=c.poll(.1)
            if msg is None:continue
            if msg.error():raise RuntimeError('Handoff proof stream error')
            proof=json.loads(msg.value())
            signed=proof.pop('signature','')
            key=bytes.fromhex(Path('/run/yeodam-cdc-auth368/aes.hex').read_text().strip())
            if not hmac.compare_digest(signed,signature(proof,key)):raise RuntimeError('Handoff proof signature rejected')
            if proof.get('runId')!=run:raise RuntimeError('Handoff Run mismatch')
            if proof.get('kind')=='cutover_start':
                if not ready or proof.get('fenceAt')!=at:raise RuntimeError('Unexpected cutover start echo')
                continue
            result['proofReceivedAt']=time.time()
            if proof.get('equal') is not True:
                result['boundaryFailure']=proof
                raise RuntimeError('Handoff proof rejected: '+proof.get('failureCode','UNSPECIFIED'))
            if proof.get('finishedAt',0)>at+45:raise RuntimeError('Handoff validation deadline exceeded')
            store(Path('/var/lib/yeodam/b391')/(run+'.cdc-drained.json'),proof)
            result['proofReceivedAt']=time.time();break
        else:raise RuntimeError('Handoff proof deadline exceeded')
        # Existing promote rechecks GTID_SUBSET, frozen Binlog and both Writer flags.
        action('promote');result.update(switched=True,promotedAt=time.time(),activationWithinBudget=False)
        subprocess.run(['python3',str(ROOT/'b-integration376-app.py'),'enable-consumer','--run-id',run,'--deadline',str(at+50)],check=True,capture_output=True)
        result.update(switched=True,enabledAt=time.time())
        result['activationWithinBudget']=result['enabledAt']<=at+50
    except Exception as e:
        result.update(errorClass=type(e).__name__,error=str(e))
    finally:
        c.close();result['finishedAt']=time.time();store(directory/'handoff-result.json',result);print(json.dumps(result),flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('role',choices=('source','kafka'));p.add_argument('--run-id',required=True);p.add_argument('--at',type=float);p.add_argument('--when-ready',action='store_true');a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id:raise ValueError('Canonical Run required')
    expected='10.30.0.174' if a.role=='source' else '10.30.10.95'
    if expected not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Wrong migration host')
    if a.at is None:raise ValueError('Fixed handoff time required')
    if a.role=='source':source(a.run_id,a.at,a.when_ready)
    else:kafka(a.run_id,a.at,a.when_ready)
