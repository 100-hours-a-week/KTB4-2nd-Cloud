#!/usr/bin/env python3
"""Bounded frozen-topic Sink diagnosis; never executes Source/API/cutover work."""
import argparse
import base64
import concurrent.futures
import copy
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from uuid import UUID,uuid4

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT.parent/'KTB4-2nd-Cloud/.local-docs'
BASE=DOCS/'evidence/461-sink-diagnostics'
PARENT=DOCS/'evidence/458-sink-shutdown'
REMOTE='/var/lib/yeodam/cdc461'
HOSTS={'target':'i-0c9aee73a0dc9b59d','kafka':'i-0b86e517fda2cfafc'}


class PendingInvocation(RuntimeError):pass


def validate(c):
    if c['account']!='483175530259' or c['region']!='ap-northeast-2' or c['targetInstance']!=HOSTS['target'] or c['kafkaInstance']!=HOSTS['kafka']:
        raise ValueError('Only the existing diagnostic clone/Kafka are allowed')
    if (c['tripTaskLimit'],c['tripBatch'],c['journalBatch'],c['workerCpuLimit'],c['workerMemoryLimit'])!=(8,1,100,1000000000,939524096) or c['tripPoll'] not in (1,50):
        raise ValueError('Frozen resources/concurrency differ')
    if c['initialTrips']!=2048196 or c['finalTrips']!=2065718 or sum(c['tripEnd'].values())!=35212 or c['journalEnd']!={'0':35212}:
        raise ValueError('Unexpected immutable replay data')
    if set(c['tripEnd'])!={str(x) for x in range(8)} or c['diagnosticDeadlineSeconds']!=600 or c['sampleIntervalSeconds']!=2:
        raise ValueError('Unexpected partitions or diagnostic budget')


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def poll_configs(configs, trip_poll):
    if trip_poll not in (1,50) or len(configs)!=2:
        raise ValueError('Only baseline or reviewed Poll50 is allowed')
    if configs[0]['consumer.override.max.poll.records']!='1' or configs[0]['batch.size']!='1':
        raise ValueError('Frozen travel Sink differs')
    result=copy.deepcopy(configs)
    result[0]['consumer.override.max.poll.records']=str(trip_poll)
    return result


def pinned_sink_configs(run, trip_poll=1):
    manifest=json.loads((PARENT/'bundle-manifest.json').read_text())
    path=PARENT/'runtime/pinned/scripts/b-integration376-kafka.py'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['files']['scripts/b-integration376-kafka.py']['renderedSha256']:
        raise RuntimeError('Actual frozen Sink source changed')
    m=module('frozen_sink461',path);parent=json.loads((ROOT/'tests/load/fixtures/cdc461-contract.json').read_text())['parentRun']
    prefix='yeodam-diag461-'+run[:8];values=[]
    for suffix,config in [('sink',m.sink(parent)),('sink-journal',m.journal_sink(parent))]:
        config['name']=prefix+'-'+suffix;values.append(config)
    return poll_configs(values,trip_poll)


class Runtime:
    def __init__(self,directory):self.directory=directory
    def aws(self,*args):
        p=subprocess.run(['aws',*args,'--profile','yeodam-admin','--region','ap-northeast-2','--output','json'],capture_output=True,text=True)
        if p.returncode:
            if 'InvocationDoesNotExist' in p.stderr:raise PendingInvocation('SSM delivery pending')
            raise RuntimeError('AWS '+args[0]+'/'+args[1]+' failed; private stderr suppressed')
        return json.loads(p.stdout) if p.stdout.strip() else {}
    def send(self,role,commands,seconds=900):
        if role not in HOSTS:raise ValueError('Source/generator/production actions are forbidden')
        request={'DocumentName':'AWS-RunShellScript','InstanceIds':[HOSTS[role]],'Comment':'Cloud461 Sink diagnostic '+role,
                 'Parameters':{'commands':['set -eu','umask 077',*commands],'executionTimeout':[str(seconds)]}}
        with tempfile.NamedTemporaryFile('w',suffix='.json') as file:
            json.dump(request,file);file.flush();cid=self.aws('ssm','send-command','--cli-input-json','file://'+file.name)['Command']['CommandId']
        (self.directory/(cid+'.command.json')).write_text(json.dumps({'role':role,'id':cid,'submittedAt':time.time()}));return cid
    def wait(self,role,cid,seconds=1000):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            try:r=self.aws('ssm','get-command-invocation','--command-id',cid,'--instance-id',HOSTS[role])
            except PendingInvocation:time.sleep(2);continue
            if r['Status'] not in ('InProgress','Pending','Delayed'):
                (self.directory/(cid+'.result.json')).write_text(json.dumps(r,indent=2))
                if r['Status']!='Success':raise RuntimeError('Host stage failed: '+role+' '+cid+'; preserve private evidence')
                return r
            time.sleep(2)
        raise RuntimeError('Command still running; retain its ID '+cid)
    def raw(self,role,commands,seconds=900):return self.wait(role,self.send(role,commands,seconds),seconds+30)
    def action(self,role,action,run,async_=False):
        python='/opt/yeodam-b383/venv/bin/python3' if role=='kafka' else 'python3'
        cmd=shlex.join([python,REMOTE+'/'+role+'.py',action,'--run',run,'--contract',REMOTE+'/contract.json'])
        return self.send(role,[cmd],780) if async_ else self.raw(role,[cmd],780)
    def deploy(self,role,source,destination):
        data=base64.b64encode(Path(source).read_bytes()).decode()
        return self.raw(role,['install -d -m 0700 '+shlex.quote(str(Path(destination).parent)),
                              "printf '%s' '"+data+"' | base64 -d > "+shlex.quote(destination)])


def values(result):
    rows=[]
    for line in result.get('StandardOutputContent','').splitlines():
        try:rows.append(json.loads(line))
        except ValueError:pass
    return rows


def collect(runtime,role,run,name):
    # Numeric-only observer output. Compress before bounded SSM chunks; no DB values.
    remote=REMOTE+'/'+run+'/'+name
    code="import gzip,pathlib,json; p=pathlib.Path("+repr(remote)+"); out=p.with_suffix(p.suffix+'.gz'); out.write_bytes(gzip.compress(p.read_bytes())); print(json.dumps({'bytes':out.stat().st_size}))"
    count=values(runtime.raw(role,["python3 -c "+shlex.quote(code)]))[-1]['bytes']
    if count>16*1024*1024:raise RuntimeError('Unexpected observer output size; do not print it')
    def chunk(offset):
        code="import pathlib,base64; p=pathlib.Path("+repr(remote+'.gz')+"); f=p.open('rb'); f.seek("+str(offset)+"); print(base64.b64encode(f.read(12000)).decode())"
        r=runtime.raw(role,['python3 -c '+shlex.quote(code)])
        return offset,base64.b64decode(r['StandardOutputContent'].strip(),validate=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        pieces=list(pool.map(chunk,range(0,count,12000)))
    data=b''.join(blob for _,blob in sorted(pieces))
    if len(data)!=count:raise RuntimeError('Observer transfer incomplete')
    (runtime.directory/(role+'-'+name+'.gz')).write_bytes(data)
    (runtime.directory/(role+'-'+name)).write_bytes(gzip.decompress(data))


def execute(directory,run,c):
    rt=Runtime(directory);state={'run':run,'phase':'preparing','sourceApiUsed':False,'parentRun':c['parentRun']}
    def mark(phase,**kwargs):
        state.update(phase=phase,**kwargs);(directory/'state.json').write_text(json.dumps(state,indent=2));print(json.dumps({'phase':phase,**kwargs}),flush=True)
    if (directory/'state.json').exists():raise RuntimeError('Do not replay the same diagnostic ID')
    for path in (path for group in ('461-sink-diagnostics','463-cdc-poll') for path in (DOCS/'evidence'/group).glob('*/state.json')):
        old=json.loads(path.read_text())
        if old.get('run')!=run and old.get('phase') not in ('completed','preparation_failed_restored','failed_restored'):
            raise RuntimeError('Another diagnostic is unfinished; inspect it before any artifact deployment')
    if rt.aws('sts','get-caller-identity')['Account']!=c['account']:raise RuntimeError('Wrong AWS account')
    for role in HOSTS:
        rt.deploy(role,ROOT/'tests/load/fixtures/cdc461-contract.json',REMOTE+'/contract.json')
        rt.deploy(role,ROOT/'scripts/cdc461'/str(role+'.py'),REMOTE+'/'+role+'.py')
    with tempfile.TemporaryDirectory() as temporary:
        subprocess.run(['javac','--release','17','-d',temporary,str(ROOT/'scripts/cdc461/Metrics461.java')],check=True,capture_output=True)
        rt.deploy('kafka',Path(temporary)/'Metrics461.class',REMOTE+'/Metrics461.class')
        rt.raw('kafka',['chmod 0644 '+REMOTE+'/Metrics461.class'])
    mark('checking')
    rt.action('target','check',run)
    rt.action('kafka','check',run)
    try:rt.action('kafka','warmup',run)
    except Exception:
        rt.action('kafka','close',run)
        mark('preparation_failed_restored',reason='JMX observer unavailable; no DB replacement or replay')
        raise
    warm=rt.raw('kafka',["python3 - <<'WARM461'\nfrom pathlib import Path\nimport json\np=Path('"+REMOTE+'/'+run+"/jmx.jsonl'); rows=[json.loads(x) for x in p.read_text().splitlines()]; assert len(rows)>=2 and all('workerCpuNanos' in r for r in rows); print(json.dumps({'jmxReadable':True,'samples':len(rows)}))\nWARM461"])
    (directory/'warmup.json').write_text(json.dumps(values(warm),indent=2))
    configs=directory/'sink-configs.private.json';configs.write_text(json.dumps(pinned_sink_configs(run,c['tripPoll'])));configs.chmod(0o600)
    rt.deploy('kafka',configs,REMOTE+'/'+run+'/sink-configs.private.json')
    mark('restoring_bulk_snapshot')
    rt.action('target','prepare',run)
    mark('sampling_started')
    observers={'mysql':rt.action('target','sample',run,True),'jmx':rt.action('kafka','jmx',run,True),'offsets':rt.action('kafka','sample',run,True)}
    mark('observers_armed',observerCommands=observers)
    # A baseline sample is required before either connector can write.
    rt.raw('target',["python3 - <<'READY461'\nimport time,pathlib\np=pathlib.Path('"+REMOTE+'/'+run+"/mysql.jsonl'); end=time.monotonic()+10\nwhile time.monotonic()<end:\n if p.exists() and p.stat().st_size: break\n time.sleep(.2)\nelse: raise RuntimeError('MySQL observer not armed')\nREADY461"])
    result=rt.action('kafka','register',run);registered=values(result)[-1]['registeredAt']
    mark('replaying',registeredAt=registered,deadlineAt=registered+c['diagnosticDeadlineSeconds'])
    last=None;complete=False
    while time.time()<registered+c['diagnosticDeadlineSeconds']:
        result=rt.action('kafka','progress',run);last=values(result)[-1]
        (directory/'progress.json').write_text(json.dumps(last,indent=2))
        statuses=last['statuses']
        if any(t['state']=='FAILED' for v in statuses.values() for t in v['tasks']):break
        if last['last'] and all(v['equal'] for v in last['last']['groups']):complete=True;break
        time.sleep(5)
    mark('replay_finished',allOffsetsAtFrozenEnd=complete,finishedAt=time.time())
    # Cleanup time is outside the diagnostic consumption window.
    rt.action('kafka','stop',run)
    rt.raw('target',['touch '+REMOTE+'/'+run+'/STOP'])
    for name,cid in observers.items():rt.wait('target' if name=='mysql' else 'kafka',cid,90)
    verification=rt.action('target','verify',run)
    (directory/'data-verification.json').write_text(json.dumps(values(verification),indent=2))
    for role,name in [('target','mysql.jsonl'),('kafka','jmx.jsonl'),('kafka','offsets.jsonl')]:collect(rt,role,run,name)
    mark('observations_collected')
    rt.action('kafka','close',run) # releases all Sink connections and restores original Worker state.
    rt.action('target','protect',run)
    restored=rt.action('target','restore',run)
    (directory/'target-restored.json').write_text(json.dumps(values(restored),indent=2))
    mark('completed',dataEqual=values(verification)[-1]['complete'],originalTargetRestored=True,
         serviceCutoverTest=False,redisE2ETested=False)


def recover(directory,run):
    """Protect/restore only this diagnostic's clone after an interrupted stage."""
    path=directory/'state.json'
    if not path.exists():return
    state=json.loads(path.read_text())
    if state.get('phase') in ('completed','preparation_failed_restored','failed_restored'):return
    rt=Runtime(directory)
    local=REMOTE+'/'+run
    if state['phase'] in ('sampling_started','observers_armed','replaying','replay_finished','observations_collected'):
        rt.action('kafka','stop',run)
    # No schema replacement can occur before the prepared clone's before.json.
    metadata=values(rt.raw('target',["python3 -c "+shlex.quote("from pathlib import Path; import json; print(json.dumps({'before':Path("+repr(local+'/before.json')+").exists()}))")]))[-1]
    if metadata['before']:
        rt.raw('target',['touch '+local+'/STOP'])
        rt.action('target','protect',run)
    exists=values(rt.raw('kafka',["python3 -c "+shlex.quote("from pathlib import Path; import json; print(json.dumps({'initial':Path("+repr(local+'/initial-worker.json')+").exists()}))")]))[-1]
    if exists['initial']:rt.action('kafka','close',run)
    if metadata['before']:
        result=rt.action('target','restore',run)
        (directory/'recovery-target-restored.json').write_text(json.dumps(values(result),indent=2))
    state.update(phase='failed_restored',failurePreserved=True,originalTargetRestored=metadata['before'],targetNotReplaced=not metadata['before'])
    path.write_text(json.dumps(state,indent=2));print(json.dumps({'phase':'failed_restored','targetWasReplaced':metadata['before']}),flush=True)


def main():
    global BASE
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['init','execute']);p.add_argument('--run');p.add_argument('--variant',choices=['baseline461','poll463'],default='baseline461');a=p.parse_args()
    os.umask(0o077);c=json.loads((ROOT/'tests/load/fixtures/cdc461-contract.json').read_text())
    if a.variant=='poll463':
        c['tripPoll']=50;BASE=DOCS/'evidence/463-cdc-poll'
    validate(c)
    if a.action=='init':
        run=str(uuid4());directory=BASE/run;directory.mkdir(parents=True,exist_ok=False)
        (directory/'contract.json').write_text(json.dumps(c,indent=2));print(json.dumps({'run':run,'directory':str(directory)}));return
    if not a.run or str(UUID(a.run))!=a.run:raise ValueError('Exact diagnostic UUID required')
    directory=BASE/a.run
    if json.loads((directory/'contract.json').read_text())!=c:raise RuntimeError('Initialized contract changed')
    try:execute(directory,a.run,c)
    except Exception:
        try:recover(directory,a.run)
        except Exception as recovery:
            (directory/'recovery-pending.json').write_text(json.dumps({'run':a.run,'errorClass':type(recovery).__name__,'note':'Keep clone protected; inspect saved command IDs, never reuse the Group'}))
        raise


if __name__=='__main__':main()
