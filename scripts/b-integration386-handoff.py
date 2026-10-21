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
def kafka(run,at):
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
    try:m.cutover(run,'standby',marker_timeout=900,cutover_at=at,on_proof=publish)
    except m.BoundaryError as e:
        if delivery_attempted:raise
        publish(e.proof(run))
def source(run,at):
    directory=ROOT/run;directory.mkdir(parents=True,exist_ok=True)
    c=Consumer({'bootstrap.servers':'10.30.10.95:9092','group.id':'yeodam-b391-proof-'+run,'enable.auto.commit':False})
    c.assign([TopicPartition(topic(run),0,0)])
    result={'runId':run,'switched':False,'controllerArmedAt':time.time(),'plannedFenceAt':at,'cdcDeadlineAt':at+45,'activationDeadlineAt':at+50}
    def action(name,*extra):
        p=subprocess.run(['python3',str(ROOT/'b-integration376-control.py'),name,'--run-id',run,*extra],capture_output=True,text=True)
        if p.returncode:raise RuntimeError('Local handoff action failed: '+name)
    try:
        action('fence','--at',str(at));result['fencedAt']=time.time()
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
    p=argparse.ArgumentParser();p.add_argument('role',choices=('source','kafka'));p.add_argument('--run-id',required=True);p.add_argument('--at',type=float);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id:raise ValueError('Canonical Run required')
    expected='10.30.0.174' if a.role=='source' else '10.30.10.95'
    if expected not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Wrong migration host')
    if a.at is None:raise ValueError('Fixed handoff time required')
    if a.role=='source':source(a.run_id,a.at)
    else:kafka(a.run_id,a.at)
