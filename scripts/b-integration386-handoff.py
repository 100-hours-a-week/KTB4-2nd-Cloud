#!/usr/bin/env python3
"""Prearmed host controllers exchange a scoped CDC proof without an SSM round trip."""
import argparse,json,subprocess,time,hmac,hashlib
from importlib.machinery import SourceFileLoader
from pathlib import Path
from uuid import UUID
from confluent_kafka import Consumer,Producer,TopicPartition

ROOT=Path('/run/yeodam-b386')
def topic(run):return 'staging.yeodam.migration.b386.'+run+'.handoff-proof'
def signature(proof,key):
    return hmac.new(key,b'b386-handoff-proof:'+json.dumps(proof,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
def store(path,value):
    temp=path.with_suffix('.next');temp.write_text(json.dumps(value));temp.chmod(0o600);temp.replace(path)
def kafka(run):
    m=SourceFileLoader('boundary386',str(ROOT/'b-integration383-cutover.py')).load_module()
    try:
        proof=m.cutover(run,'standby',marker_timeout=900)
    except Exception as e:
        proof={'runId':run,'equal':False,'errorClass':type(e).__name__}
    key=bytes.fromhex(Path('/etc/yeodam/kafka/cdc/auth368-aes.hex').read_text().strip())
    proof['signature']=signature(proof,key)
    producer=Producer({'bootstrap.servers':'10.30.10.95:9092','acks':'all'})
    errors=[]
    producer.produce(topic(run),key=run,value=json.dumps(proof),on_delivery=lambda e,msg:errors.append(e) if e else None)
    if producer.flush(10) or errors:raise RuntimeError('Handoff proof not durably published')
    print(json.dumps({'runId':run,'proofPublished':True,'equal':proof['equal']}),flush=True)
def source(run,at):
    directory=ROOT/run;directory.mkdir(parents=True,exist_ok=True)
    c=Consumer({'bootstrap.servers':'10.30.10.95:9092','group.id':'yeodam-b386-proof-'+run,'enable.auto.commit':False})
    c.assign([TopicPartition(topic(run),0,0)])
    result={'runId':run,'switched':False,'controllerArmedAt':time.time()}
    def action(name,*extra):
        p=subprocess.run(['python3',str(ROOT/'b-integration376-control.py'),name,'--run-id',run,*extra],capture_output=True,text=True)
        if p.returncode:raise RuntimeError('Local handoff action failed: '+name)
    try:
        action('fence','--at',str(at));result['fencedAt']=time.time()
        end=time.monotonic()+35
        while time.monotonic()<end:
            msg=c.poll(.1)
            if msg is None:continue
            if msg.error():raise RuntimeError('Handoff proof stream error')
            proof=json.loads(msg.value())
            signed=proof.pop('signature','')
            key=bytes.fromhex(Path('/run/yeodam-cdc-auth368/aes.hex').read_text().strip())
            if not hmac.compare_digest(signed,signature(proof,key)):raise RuntimeError('Handoff proof signature rejected')
            if proof.get('runId')!=run or proof.get('equal') is not True:raise RuntimeError('Handoff proof rejected')
            store(Path('/var/lib/yeodam/b386')/(run+'.cdc-drained.json'),proof)
            result['proofReceivedAt']=time.time();break
        else:raise RuntimeError('Handoff proof deadline exceeded')
        # Existing promote rechecks GTID_SUBSET, frozen Binlog and both Writer flags.
        action('promote')
        subprocess.run(['python3',str(ROOT/'b-integration376-app.py'),'enable-consumer','--run-id',run],check=True,capture_output=True)
        result.update(switched=True,enabledAt=time.time())
    except Exception as e:
        result.update(errorClass=type(e).__name__,error=str(e))
    finally:
        c.close();result['finishedAt']=time.time();store(directory/'handoff-result.json',result);print(json.dumps(result),flush=True)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('role',choices=('source','kafka'));p.add_argument('--run-id',required=True);p.add_argument('--at',type=float);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id:raise ValueError('Canonical Run required')
    expected='10.30.0.174' if a.role=='source' else '10.30.10.95'
    if expected not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Wrong migration host')
    if a.role=='source':
        if a.at is None:raise ValueError('Fixed handoff time required')
        source(a.run_id,a.at)
    else:kafka(a.run_id)
