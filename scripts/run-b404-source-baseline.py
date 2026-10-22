#!/usr/bin/env python3
"""One fixed Source-only B baseline; no CDC, snapshot, promotion or Target writes."""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import time
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT.parent / 'KTB4-2nd-Cloud/.local-docs/evidence/404-source-baseline'
spec = importlib.util.spec_from_file_location('b376', ROOT/'scripts/b-integration376-run.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
b.EVIDENCE = b.ssm.EVIDENCE = EVIDENCE
IMAGE = 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:89b24d02225c8fb6143a4ee323e9b3221c18676c0ab14056b38a6edc608ad4e1'


def save(value):
    (EVIDENCE/'state.json').write_text(json.dumps(value, indent=2))


def remote(host, code, interpreter='python3', **kwargs):
    return b.raw(host, [interpreter+" - <<'BASELINE404'\n"+code+"\nBASELINE404"], **kwargs)


def deploy():
    files = {
        'source': ['b-integration376-app.py', 'b-integration376-control.py',
                   'b-integration376-auth.py', 'b-integration376-smoke.py',
                   'b-integration388-connections.py', 'b-integration393-resources.py',
                   'b-integration396-analyze.py'],
        'generator': ['b-integration376-generator.py'],
    }
    for host, names in files.items():
        for name in names:
            b.raw(host, b.ssm.deploy('scripts/'+name, '/run/yeodam-b391/'+name))
    for name in ('scenarios/migration-b-integration.js', 'scripts/run-k6.sh'):
        target = '/opt/yeodam-load/source/tests/load/'+name
        b.raw('generator', b.ssm.deploy('tests/load/'+name, target)+['chown ubuntu:ubuntu '+target])
    b.raw('generator', b.ssm.deploy('tests/load/scripts/private-pool-transfer.py', '/run/yeodam-b391/pool-transfer.py'))
    # Reuse only the exact immutable image; registry failure must not select another image.
    remote('source', "from importlib.machinery import SourceFileLoader\nm=SourceFileLoader('app404','/run/yeodam-b391/b-integration376-app.py').load_module()\nx=m.inspect(m.ORIGINAL)\ni=m.docker('GET','/images/'+m.quote(m.IMAGE,safe='')+'/json')\nassert m.IMAGE=="+repr(IMAGE)+"\nassert m.IMAGE in i['RepoDigests'] and i['Architecture']=='amd64' and i['Os']=='linux'\nassert x['State']['Running']\nprint('PINNED_IMAGE_CACHED')")


def topics(run):
    # Request transport only. No Connect REST call, CDC topic, Sink or Worker start.
    code = """from confluent_kafka.admin import AdminClient,NewTopic
import json
prefix='staging.yeodam.migration.b391.'+RUN
items=[(prefix+'.requests',8),(prefix+'.auth-requests',4),(prefix+'.auth-requests.results',4)]
a=AdminClient({'bootstrap.servers':'10.30.10.95:9092'})
existing=a.list_topics(timeout=10).topics
assert not any(n in existing for n,_ in items),'Run topics already exist'
for f in a.create_topics([NewTopic(n,num_partitions=p,replication_factor=1,config={'cleanup.policy':'delete','retention.ms':'86400000'}) for n,p in items],request_timeout=15).values():f.result()
print(json.dumps({'createdTopics':len(items),'requestPartitions':8,'cdcConnectorsStarted':0}))
""".replace('RUN', repr(run), 1)
    return remote('kafka', code, '/opt/yeodam-b383/venv/bin/python3')


def source_observation(run):
    code = """import json,time
from importlib.machinery import SourceFileLoader
m=SourceFileLoader('control404','/run/yeodam-b391/b-integration376-control.py').load_module();d=m.database()
try:
 run=RUN
 prefix='t'+m.hashlib.sha256(run.encode()).hexdigest()[:5]
 assert d.identity('source')[2:]==['0','0'] and d.identity('target')[2:]==['1','1']
 print(json.dumps({'at':time.time(),'sourceRows':int(d.query('source','SELECT COUNT(*) FROM yeodam_v1_source.trips;')),'targetRows':int(d.query('target','SELECT COUNT(*) FROM yeodam_v1_source.trips;')),'runCommittedRows':int(d.query('source',"SELECT COUNT(*) FROM yeodam_v1_source.trips WHERE trip_name LIKE '"+prefix+"%';")),'sourceFlags':d.identity('source')[2:],'targetFlags':d.identity('target')[2:]}))
finally:d.close()
""".replace('RUN', repr(run), 1)
    return b.records(remote('source', code))[-1]


def start():
    if (EVIDENCE/'state.json').exists():
        raise RuntimeError('Run already prepared; use observe/finish, never rerun its ID')
    run = str(uuid.uuid4())
    state = {'runId':run, 'phase':'preparing', 'rate':30, 'duration':900,
             'checkpointSeconds':1020, 'image':IMAGE, 'sourceConcurrency':4,
             'maxPollRecords':32, 'logicalUsers':120, 'migrationEnabled':False}
    save(state)
    state['before'] = source_observation(run); save(state)
    topics(run)
    prepared = subprocess.run(['python3',str(ROOT/'tests/load/scripts/run-synthetic-pool-ssm.py'),
        'prepare-existing','--instance-id',b.ssm.HOSTS['source'],'--run-id',run,
        '--dataset-id',b.DATASET,'--count','140'],capture_output=True,text=True)
    if prepared.returncode:
        raise RuntimeError('Pool preparation failed; no input started')
    reference=json.loads((EVIDENCE/'reference.json').read_text())
    expected=sorted({int(v['userId']) for v in reference['actors']})
    code="""import json,pathlib,statistics
from importlib.machinery import SourceFileLoader
p=json.loads((pathlib.Path('/run/yeodam-synthetic-sessions')/(RUN+'.json')).read_text())
ids=sorted(int(x['userId']) for x in p['sessions'][:120])
assert ids==EXPECTED,'Logical users differ from reference; no input allowed'
m=SourceFileLoader('control404','/run/yeodam-b391/b-integration376-control.py').load_module();d=m.database()
try:
 values=d.query('source','SELECT user_id,COUNT(*) FROM yeodam_v1_source.trips WHERE user_id IN ('+','.join(map(str,ids))+') AND deleted_at IS NULL GROUP BY user_id;')
 counts={int(x.split('\\t')[0]):int(x.split('\\t')[1]) for x in values.splitlines()}
 print(json.dumps({'logicalUsersMatchReference':True,'existingTripsByUser':counts}))
finally:d.close()
""".replace('RUN',repr(run)).replace('EXPECTED',repr(expected))
    state['userDistribution']=b.records(remote('source',code))[-1];save(state)
    b.transfer_pool(run)
    b.auth(run,'configure')
    b.phase('source','b-integration376-app.py',run,'auth-files')
    b.phase('source','b-integration376-app.py',run,'start-app')
    state['phase']='app_started'; save(state)
    b.raw('source',["python3 - <<'HEALTH'\nimport time,urllib.request\nend=time.monotonic()+100\nwhile time.monotonic()<end:\n try:\n  if urllib.request.urlopen('http://127.0.0.1:8080/api/actuator/health',timeout=2).status==200:break\n except OSError:pass\n time.sleep(1)\nelse:raise RuntimeError('Baseline BE not healthy')\nHEALTH"])
    b.phase('source','b-integration376-app.py',run,'route-status')
    b.phase('source','b-integration376-app.py',run,'source-ready')
    b.phase('source','b-integration376-smoke.py',run)
    b.phase('source','b-integration376-app.py',run,'diagnostic-ready')
    # No Target container: attest the actual Source listener, not configuration intent.
    remote('source', "from importlib.machinery import SourceFileLoader\nimport json\nm=SourceFileLoader('app404','/run/yeodam-b391/b-integration376-app.py').load_module()\nx=m.inspect('yeodam-b391-app')\nassert x['Config']['Labels']['yeodam.run']=="+repr(run)+"\nc=m.Connection('localhost',timeout=15);c.request('GET','/containers/yeodam-b391-app/logs?stdout=1&stderr=1');r=c.getresponse();data=r.read();c.close()\nassert r.status==200 and b'migration_trip_consumer_config max_poll_records=32 enable_auto_commit=false concurrency=4' in data\nprint(json.dumps({'actualMaxPollRecords':32,'autoCommit':False,'sourceConcurrency':4,'targetContainerStarted':False}))")
    state['k6Command'] = b.phase('generator','b-integration376-generator.py',run,'start','--rate','30',sync=False)
    save(state)
    until = time.monotonic()+100
    while time.monotonic()<until:
        info = b.records(b.phase('generator','b-integration376-generator.py',run,'read'))[-1]
        if info.get('actualStartedAt') and info.get('authActorMappingValid'):
            state['startedAt']=info['actualStartedAt']; break
        if 'k6ExitCode' in info:raise RuntimeError('Input stopped during initialization')
        time.sleep(3)
    else:raise RuntimeError('Input start/actor mapping unconfirmed; retain state')
    state['phase']='running'; save(state)
    for key, script in [('resources','b-integration393-resources.py'),('connections','b-integration388-connections.py')]:
        state[key+'Command']=b.raw('source',['python3 /run/yeodam-b391/'+script+' --run-id '+run+' --started-at '+str(state['startedAt'])],sync=False,bound=1100)
    save(state)
    print(json.dumps(state),flush=True)


def observe():
    state=json.loads((EVIDENCE/'state.json').read_text())
    state['lastObservation']=source_observation(state['runId']);save(state)
    print(json.dumps({'phase':state['phase'],'elapsed':time.time()-state['startedAt'],**state['lastObservation']}))


def finish():
    state=json.loads((EVIDENCE/'state.json').read_text());run=state['runId']
    if state['phase']!='running':raise RuntimeError('Not an active baseline; do not repeat cleanup')
    if time.time()<state['startedAt']+1020:raise RuntimeError('Fixed checkpoint not yet reached')
    state['checkpoint']=source_observation(run); save(state)
    state['load']=b.records(b.phase('generator','b-integration376-generator.py',run,'audit'))[-1]
    state['completionEvidence']=b.records(b.phase('source','b-integration376-app.py',run,'stop-app'))
    state['phase']='app_stopped'; save(state)
    b.phase('source','b-integration376-app.py',run,'restore-route')
    b.phase('source','b-integration376-app.py',run,'restore-original')
    for key in ('resources','connections'):
        b.wait('source',state[key+'Command'],bound=60,allow_failure=True)
    state['resources']=b.records(b.raw('source',['python3 /run/yeodam-b391/b-integration393-resources.py --run-id '+run+' --started-at '+str(state['startedAt'])+' --summarize']))
    code="""from importlib.machinery import SourceFileLoader
import json,base64,zlib,pathlib
m=SourceFileLoader('control404','/run/yeodam-b391/b-integration376-control.py').load_module();d=m.database()
try:print(json.dumps({'source':m.audit(d,RUN,'source'),'target':m.audit(d,RUN,'target')}))
finally:d.close()
""".replace('RUN',repr(run))
    state['database']=b.records(remote('source',code,bound=240))[-1]
    # Whitelisted completion metadata already strips SQL, parameters and secrets.
    code="""import pathlib,base64,zlib,json
from importlib.machinery import SourceFileLoader
m=SourceFileLoader('analyzer404','/run/yeodam-b391/b-integration396-analyze.py').load_module()
p=pathlib.Path('/run/yeodam-b391')/RUN
rows=json.loads((p/'completions.json').read_text())
value=m.analyze(rows,START,EXPECTED)
batches=json.loads((p/'diagnostic-batches.json').read_text())
starts=[v for v in batches if v['event']=='start' and v['atMs']>=START*1000]
value['actualBatchMax']=max([sum(v['counts'].values()) for v in starts] or [0])
value['over32Batches']=sum(sum(v['counts'].values())>32 for v in starts)
print(base64.b64encode(zlib.compress(json.dumps(value).encode())).decode())
""".replace('RUN',repr(run)).replace('START',repr(state['startedAt'])).replace('EXPECTED',str(state['load']['started']))
    result=remote('source',code,bound=120)
    analysis=json.loads(zlib.decompress(base64.b64decode(result['StandardOutputContent'].strip())))
    (EVIDENCE/'diagnostic-analysis.json').write_text(json.dumps(analysis,indent=2))
    cleanup=subprocess.run(['python3',str(ROOT/'tests/load/scripts/run-synthetic-pool-ssm.py'),'cleanup-existing','--instance-id',b.ssm.HOSTS['source'],'--run-id',run,'--dataset-id',b.DATASET],capture_output=True,text=True)
    if cleanup.returncode:raise RuntimeError('Scoped pool cleanup failed')
    b.raw('generator',['rm -- /opt/yeodam-load/source/tests/load/private/'+run+'.json'])
    b.raw('source',['rm -f -- /run/yeodam-b391/'+run+'/container-auth/aes.hex /run/yeodam-b391/'+run+'/container-auth/redis.json /run/yeodam-b391/'+run+'/container-auth/control.token'])
    state['after']=source_observation(run);state['phase']='completed';save(state)
    print(json.dumps({'phase':state['phase'],'runId':run,'checkpoint':state['checkpoint'],'load':state['load'],'database':state['database'],'after':state['after']}))


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('deploy','start','observe','finish'));a=p.parse_args()
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    if b.ssm.aws('sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong account')
    globals()[a.action]()

if __name__=='__main__':main()
