"""W1/W1K: one converter Pod/container kill, bounded evidence, no AWS lifecycle API.

Offline by default. A kill is irreversible: cleanup removes owned CR/RBAC and
restores the Chaos profile; kubelet/ReplicaSet, not this tool, restarts the victim.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import time

import v3_cdc_fault as f
from v3_cdc_followup import DbObserver

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / 'deploy/v3/worker-fault'
BE_SOURCE = '20ec33bf4cf2f2ff18bf1e2849b7be95541dcfaf'
BE_IMAGE = 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:635f1055e04c14c758eb5cc8b91d3641efd684cf9954de5e08a450b6184762e9'
LABEL = 'yeodam.io/worker-experiment'
CONVERTER = 'photo-converter'
SHA = re.compile('[a-f0-9]{64}')
NAME = re.compile('[a-z0-9][a-z0-9-]{0,50}')
APPS = [('frontend','frontend'),('auth','auth-api'),('trip','trip-api'),('photo','photo-api'),
        ('photo',CONVERTER),('trip','trip-result-worker'),('trip','trip-websocket'),('ai','ai-api'),('ai','ai-runner')]


def epoch(value):
    f.require(isinstance(value,str), 'Timestamp required')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def contract():
    lock = json.loads((FILES/'contract.json').read_text())
    f.require(lock['beSource'] == BE_SOURCE and lock['beImage'] == BE_IMAGE, 'Review changed BE image')
    for name, digest in lock['files'].items():
        f.require(hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, 'Worker evidence contract changed')
    return lock


def tool_hash():
    return f.sha({name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                  ['scripts/v3_worker_fault.py','scripts/v3_conversion_lease.py','scripts/prepare-v3-conversion-lease.py','scripts/v3_worker_chaos.py','scripts/v3_worker_container.py','scripts/prepare-v3-worker-fault.py',
                   'scripts/v3_cdc_fault.py','scripts/v3_cdc_followup.py','deploy/v3/worker-fault/contract.json']})


def criteria(container=False):
    value = {'claimAfterEligibilitySeconds':15,'completionAfterClaimSeconds':240,'observationSeconds':900,
            'leaseSeconds':600,'heartbeatSeconds':30,'technicalBackoffSeconds':3,
            'searchSeconds':20,'searchPollSeconds':1,'recoveryPollSeconds':3,
            'basis':{'claim':'3s retry + 3s observation interval + 9s DB/scheduler policy margin',
                     'completion':'normal 3-sample max 200.206262s rounded to 210 + 30s variation',
                     'diagnostic':'max(lease 600, earlier one-sample cold readiness 165 rounded to 180) + recovery 60 + normal 210 + margin 30',
                     'criticalPath':'max(remaining lease, replacement consumer readiness); waits need not be serial'},
            'guaranteed':False,'statisticalSlo':False}
    if container:value.update(searchSavedMaximum=4,requirePostKillUnfinishedPhoto=True,requireTerminatedContainer137=True,requireNewMemberHash=True)
    return value


def sql(trip_id):
    f.trip_ids([trip_id]); contract()
    return (FILES/'observe.sql').read_text().replace('__TRIP_ID__',str(trip_id))


def validate_rows(rows, trip_id, complete=False):
    f.require(rows and all(r.get('tripId') == trip_id for r in rows), 'Unexpected trial ID')
    allowed = {'db_clock':1,'execution_timing':1,'worker_task':2,'worker_item':20,'worker_delivery':20,'final_attachment':20}
    f.require(all(r.get('section') in allowed for r in rows), 'Unexpected observation section')
    for section, limit in allowed.items():
        f.require(sum(r['section']==section for r in rows)<=limit, 'Truncated or duplicate worker evidence')
    clocks = [r for r in rows if r['section']=='db_clock']; executions=[r for r in rows if r['section']=='execution_timing']
    f.require(len(clocks)==len(executions)==1 and executions[0]['expectedPhotos']==20, 'One JPEG20 execution required')
    items=[r for r in rows if r['section']=='worker_item']
    f.require(len({r['itemId'] for r in items})==len(items), 'Duplicate input item identity')
    for r in items:
        pending=(r.get('accepted')==0 and r['route']=='DB' and r['attempt']==0 and r['saved']==0
                 and r.get('startedAt') is None and r.get('attachmentId') is None)
        f.require(type(r.get('accepted')) is int and r['accepted'] in (0,1) and
                  type(r['attempt']) is int and 0<=r['attempt']<=3 and type(r['saved']) is int and r['saved'] in (0,1)
                  and r['cleanupEntries']>=0 and (r['route']=='KAFKA' or pending), 'Unreviewed item state')
        f.require((r['saved']==0 and r['derivedHash'] is None) or (r['saved']==1 and SHA.fullmatch(r['derivedHash'] or '')), 'Invalid persisted photo hash')
    tasks=[r for r in rows if r['section']=='worker_task']
    f.require(all(r['kind']=='INITIAL' and r['lane']=='LIGHT' and type(r['photos']) is int and 1<=r['photos']<=10
                  for r in tasks), 'JPEG INITIAL LIGHT tasks only')
    if complete:
        f.require(len(items)==20 and all(r['accepted']==1 for r in items) and sum(r['photos'] for r in tasks)==20,
                  'All twenty accepted photos required')
    return rows


def input_accepted(rows):
    items=[r for r in rows if r['section']=='worker_item']
    return len(items)==20 and all(r['accepted']==1 and r['route']=='KAFKA'for r in items) and sum(r['photos']for r in rows if r['section']=='worker_task')==20


class DeadlineKube:
    """All reads in one phase share a monotonic deadline; cleanup uses original Kube."""
    def __init__(self,k):self.k,self.deadline=k,None
    def call(self,args,source=None,timeout=20):
        remaining=self.deadline-time.monotonic() if self.deadline is not None else timeout
        if remaining<=0:raise subprocess.TimeoutExpired(['phase-budget'],0)
        return self.k.call(args,source,timeout=min(timeout,remaining))
    def get(self,kind,namespace=None,name=None,selector=None):
        args=(['-n',namespace] if namespace else [])+['get',kind]
        if name:args.append(name)
        if selector:args+=['-l',selector]
        return f.bounded_json(self.call(args+['-o','json']))


class WorkerDbObserver(DbObserver):
    def __init__(self,k,snapshot,trip_id,directory):
        super().__init__(k,snapshot,trip_id,directory)
        self.sql=sql(trip_id)
        f.private_write(self.directory/'worker-policy.json',{'sqlSha256':hashlib.sha256(self.sql.encode()).hexdigest(),
                        'readOnly':True,'tripId':trip_id,'rawClaimTokens':False,'maxPhotos':20})

    def sample(self):
        result=super().sample()
        try:
            validate_rows(result['rows'],self.trip_id)
        except ValueError:
            self.exhausted=True
            if not (self.directory/'blocked.json').exists():
                f.private_write(self.directory/'blocked.json',{'at':f.now(),'reason':'worker evidence validation failed'})
            self.status('unavailable')
            raise
        return result


# Preserve the existing 2s evidence-freshness ceiling as the total concurrent
# cycle deadline. 1.5s is a reported acquisition target, not a transport timeout:
# pinned MySQL Pod identity GET + exec are sequential inside the DB path. Neither
# deadline nor a passing probe guarantees the native saved=2..4 kill window.
SAMPLING = {'samples':5,'cycleSeconds':2,'targetCycleSeconds':1.5,'periodSeconds':1,'maxStartIntervalSeconds':2}


def search_sample(reads,observer,snapshot,owner_reader,record):
    started=time.monotonic();timing={'startedAt':f.now(),'readSeconds':{},'errors':{}}
    def measure(name,call):
        before=time.monotonic()
        try:return call()
        except BaseException as error:
            timing['errors'][name]=type(error).__name__;raise
        finally:timing['readSeconds'][name]=time.monotonic()-before
    try:
        # Exactly one DB sampler; the independent Pod and Kafka reads share the
        # same phase deadline. All workers finish before cleanup can run.
        with ThreadPoolExecutor(max_workers=3)as pool:
            tasks={name:pool.submit(measure,name,call) for name,call in {
                'victim':lambda:reads.get('pod','photo',snapshot['victim']['name']),
                'ownership':lambda:owner_reader(deadline=reads.deadline),'db':observer.sample}.items()}
            values={name:task.result()for name,task in tasks.items()}
        pod=values['victim'];v=snapshot['victim']
        f.require(pod['metadata']['uid']==v['uid'] and f.ready_pod(pod) and
                  any(x.get('name')=='backend' and x.get('containerID')==v['containerId']
                      for x in pod.get('status',{}).get('containerStatuses',[])),'Victim changed before create')
        f.require(not observer.gaps and not observer.exhausted,'Observation gap before injection')
        proof=values['db'];owner=values['ownership']
        f.require(0<=time.time()-epoch(proof['observedAt'])<=2 and proof['queryElapsedSeconds']<=2,'DB observation expired')
        ownership_age_bounds(owner,time.time())
        f.require(time.monotonic()<=reads.deadline,'Concurrent observation deadline exceeded')
        return proof,owner,pod
    finally:
        timing['cycleSeconds']=time.monotonic()-started;timing['completedAt']=f.now()
        timing['targetMet']=not timing['errors'] and timing['cycleSeconds']<=SAMPLING['targetCycleSeconds']
        record(timing)


def sampling_probe(k,snapshot,trip_id,directory,owner_reader):
    validate_snapshot(snapshot);directory=Path(directory)
    f.require(directory.is_dir() and not directory.is_symlink() and directory.stat().st_mode&0o077==0 and not any(directory.iterdir()),'Empty private sampling directory required')
    db=directory/'db';db.mkdir(mode=0o700);reads=DeadlineKube(k);observer=WorkerDbObserver(reads,snapshot,trip_id,db)
    cycles=[];starts=[];error=None
    try:
        for i in range(SAMPLING['samples']):
            start=time.monotonic();starts.append(start);reads.deadline=start+SAMPLING['cycleSeconds']
            observer.SAMPLE_SECONDS=SAMPLING['cycleSeconds']
            search_sample(reads,observer,snapshot,owner_reader,cycles.append)
            if i+1<SAMPLING['samples']:time.sleep(max(0,start+SAMPLING['periodSeconds']-time.monotonic()))
    except (ValueError,OSError,subprocess.SubprocessError)as failure:error=type(failure).__name__
    intervals=[b-a for a,b in zip(starts,starts[1:])]
    ready=error is None and len(cycles)==SAMPLING['samples'] and not observer.gaps and not observer.exhausted and all(
        not x['errors'] and x['cycleSeconds']<=SAMPLING['cycleSeconds']for x in cycles) and all(x<=SAMPLING['maxStartIntervalSeconds']for x in intervals)
    result={'at':f.now(),'toolSha256':tool_hash(),'snapshot':snapshot,'readTripId':trip_id,'samplingPolicy':SAMPLING,
            'cycles':cycles,'targetMissCount':sum(not x['targetMet'] for x in cycles),
            'startIntervalsSeconds':intervals,'samplingReady':ready,'errorKind':error,
            'injected':False,'gpuChanged':False,'notRecoveryOrWindowProof':True}
    result['proofSha256']=f.sha(result);f.private_write(directory/'sampling-proof.json',result)
    f.private_write(directory/'db-summary.json',observer.summary('sampling_ready'if ready else'sampling_unavailable'))
    f.require(ready,'Sampling too slow or unavailable; no input or fault authorized')
    return result


def verify_sampling(proof,snapshot):
    f.require(proof.get('proofSha256')==f.sha({k:v for k,v in proof.items()if k!='proofSha256'}) and
              proof.get('toolSha256')==tool_hash() and proof.get('samplingPolicy')==SAMPLING and
              proof.get('samplingReady')is True and proof.get('errorKind')is None and proof.get('injected')is False and proof.get('gpuChanged')is False and
              0<=time.time()-epoch(proof['at'])<=300,'Fresh passing sampling proof required')
    validate_snapshot(proof['snapshot'])
    f.require(all(proof['snapshot'][key]==snapshot[key]for key in ('kubeSystemUid','approvedRevision','victim','mysql','apps')),'Sampling proof target changed')
    cycles=proof.get('cycles',[]);intervals=proof.get('startIntervalsSeconds',[])
    f.require(len(cycles)==SAMPLING['samples'] and len(intervals)==SAMPLING['samples']-1 and
              all(not x['errors'] and finite(x['cycleSeconds']) and 0<=x['cycleSeconds']<=SAMPLING['cycleSeconds'] and
                  x.get('targetMet')is (x['cycleSeconds']<=SAMPLING['targetCycleSeconds'])for x in cycles)and
              proof.get('targetMissCount')==sum(not x.get('targetMet')for x in cycles)and
              all(finite(x)and 0<=x<=SAMPLING['maxStartIntervalSeconds']for x in intervals),'Sampling timing proof incomplete')


def app_templates(k):
    result={}
    for ns,name in APPS:
        d=k.get('deployment',ns,name)
        f.require(d['status'].get('observedGeneration')==d['metadata']['generation'] and
                  d['status'].get('readyReplicas',0)>=1, 'All nine apps must be Ready')
        result[ns+'/'+name]={'uid':d['metadata']['uid'],'templateHash':f.sha(d['spec']['template'])}
    return result


def snapshot(k, lease_review=None):
    if lease_review is not None:
        import v3_conversion_lease as lease
        lease.validate_review(lease_review)
    image=lease_review['beImage'] if lease_review is not None else BE_IMAGE
    source=lease_review['beSource'] if lease_review is not None else BE_SOURCE
    uid=k.identity()
    argo=k.get('application','argocd','yeodam-v3-apps')
    f.require(argo['spec']['source']['targetRevision']=='deploy/v3-approved' and
              argo['status']['sync']['status']=='Synced' and argo['status']['health']['status']=='Healthy', 'Approved apps must be Synced/Healthy')
    ps=k.get('pods','photo',selector='app.kubernetes.io/instance=photo-converter')['items']
    f.require(len(ps)==1 and f.ready_pod(ps[0]), 'Start with exactly one Ready converter')
    p=ps[0]; victim=f.pod_record(p)
    f.require(victim['ownerKind']=='ReplicaSet' and victim['name'].startswith('photo-converter-'), 'Expected converter ReplicaSet')
    rs=k.get('replicaset','photo',victim['ownerName'])
    owners=rs['metadata'].get('ownerReferences',[])
    f.require(len(owners)==1 and owners[0]['kind']=='Deployment' and owners[0]['name']==CONVERTER and owners[0].get('controller'), 'Wrong ReplicaSet owner')
    c=[x for x in p['spec']['containers'] if x['name']=='backend']
    f.require(len(c)==1 and c[0]['image']==image and c[0]['resources']['limits']['memory']=='1Gi', 'Review changed converter image or memory')
    statuses=[x for x in p['status']['containerStatuses'] if x['name']=='backend']
    f.require(len(statuses)==1 and statuses[0].get('state',{}).get('running'), 'Running backend container required')
    victim.update(rsUid=rs['metadata']['uid'],deploymentUid=owners[0]['uid'],image=c[0]['image'],
                  containerId=statuses[0]['containerID'],startedAt=statuses[0]['state']['running']['startedAt'])
    mp=k.get('pod','mysql','mysql-0'); mysql=f.pod_record(mp)
    f.require(mysql['ownerKind']=='StatefulSet' and mysql['ownerName']=='mysql','Unexpected MySQL owner')
    connector=k.get('kafkaconnector','kafka',f.CONNECTOR)
    cs=connector['status']['connectorStatus']
    f.require(cs['connector']['state']=='RUNNING' and len(cs['tasks'])==1 and cs['tasks'][0]['state']=='RUNNING', 'CDC must remain normal')
    f.require(not k.get('networkchaos','kafka')['items'],'No concurrent CDC fault')
    result={'version':1,'cluster':f.CLUSTER,'kubeSystemUid':uid,'capturedAt':f.now(),
            'approvedRevision':argo['status']['sync']['revision'],'victim':victim,'mysql':mysql,
            'apps':app_templates(k),'beSource':source,'beImage':image}
    if lease_review is not None:
        result['leaseReview']=lease_review
        runtime_lease(k,result)
    return result


def validate_snapshot(s, current=None):
    contract(); now=time.time() if current is None else current
    f.require(s['version']==1 and s['cluster']==f.CLUSTER and f.UUID.fullmatch(s['kubeSystemUid']) and
              re.fullmatch('[a-f0-9]{40}',s['approvedRevision']), 'Invalid cluster/revision')
    f.require(0<=now-epoch(s['capturedAt'])<=300, 'Refresh snapshot within five minutes')
    v=s['victim'];review=s.get('leaseReview')
    if review is not None:
        import v3_conversion_lease as lease
        lease.validate_review(review)
        f.require(s['approvedRevision']==review['approvedRevision'],'Use the reviewed approved Snapshot')
    image=review['beImage'] if review is not None else BE_IMAGE
    source=review['beSource'] if review is not None else BE_SOURCE
    f.require(v['image']==image and s['beImage']==image and s['beSource']==source and
              f.UUID.fullmatch(v['uid']) and f.UUID.fullmatch(v['rsUid']) and f.UUID.fullmatch(v['deploymentUid']) and
              v['ownerKind']=='ReplicaSet' and v['name'].startswith(CONVERTER+'-') and
              re.fullmatch('[a-z0-9.-]+',v['name']) and v['containerId'].startswith('containerd://'), 'Wrong converter identity')
    f.require(set(s['apps'])=={ns+'/'+name for ns,name in APPS}, 'All app templates required')


def prepare(s,trip_id,run_id,input_manifest,traffic_quiet=False,daemon_plan=None):
    validate_snapshot(s);f.trip_ids([trip_id]);f.require(NAME.fullmatch(run_id or '') and run_id.startswith('w1-'), 'Use a unique w1- run ID')
    f.require(traffic_quiet is True, 'Current maintenance cooperation required')
    f.require(input_manifest.get('photos')==20 and input_manifest.get('totalBytes')==40302057 and
              input_manifest.get('region')=='경기도 성남시' and SHA.fullmatch(input_manifest.get('sha256','')), 'Fixed Seongnam JPEG20 manifest required')
    value={'version':1,'case':'W1','runId':run_id,'tripId':trip_id,'snapshot':s,'input':input_manifest,
           'criteria':criteria(),'toolSha256':tool_hash(),'createdAt':f.now(),'injected':False,
           'fault':{'apiVersion':'chaos-mesh.org/v1alpha1','kind':'PodChaos','metadata':{
               'name':run_id,'namespace':'photo','labels':{LABEL:run_id}},
               'spec':{'action':'pod-kill','mode':'one','gracePeriod':0,'selector':{'namespaces':['photo'],'pods':{'photo':[s['victim']['name']]}}}}}
    if daemon_plan is not None:
        import v3_worker_container as hard
        hard.verify_daemon(daemon_plan,daemon_plan['planSha256'])
        f.require(daemon_plan['victim']==s['victim'] and daemon_plan['clusterUid']==s['kubeSystemUid'],'Daemon Plan must pin this exact victim')
        value.update(version=2,case='W1K',daemonPlan=daemon_plan,criteria=criteria(True))
        value['fault']['spec'].pop('gracePeriod');value['fault']['spec'].update(action='container-kill',containerNames=['backend'])
    if s.get('leaseReview') is not None:
        import v3_conversion_lease as lease
        f.require(daemon_plan is not None,'W2K requires verified container-kill; PodKill is not equivalent')
        value.update(case='W2K',criteria=lease.criteria(s['leaseReview']))
    return dict(value,planSha256=f.sha(value))


def verify(p,expected,fresh=True):
    f.require(p['planSha256']==expected==f.sha({k:v for k,v in p.items() if k!='planSha256'}), 'Plan hash mismatch')
    container=p.get('case') in ('W1K','W2K')
    expected_criteria=criteria(container)
    if p.get('case')=='W2K':
        import v3_conversion_lease as lease
        expected_criteria=lease.criteria(p['snapshot']['leaseReview'])
    else:f.require('leaseReview' not in p['snapshot'],'A new lease policy cannot masquerade as W1/W1K')
    f.require(p['toolSha256']==tool_hash() and p['criteria']==expected_criteria and p['case']in('W1','W1K','W2K'), 'Reprepare changed tool/criteria')
    if fresh: validate_snapshot(p['snapshot'])
    f.trip_ids([p['tripId']]);f.require(p['version']==(2 if container else 1) and p['injected'] is False and NAME.fullmatch(p['runId']) and p['runId'].startswith('w1-'),'Invalid one-shot identity')
    f.require(p['fault']['apiVersion']=='chaos-mesh.org/v1alpha1' and p['fault']['kind']=='PodChaos','PodChaos only')
    expected_spec={'action':'pod-kill','mode':'one','gracePeriod':0,'selector':{'namespaces':['photo'],'pods':{'photo':[p['snapshot']['victim']['name']]}}}
    if container:
        import v3_worker_container as hard
        hard.verify_daemon(p['daemonPlan'],p['daemonPlan']['planSha256'])
        f.require(p['daemonPlan']['victim']==p['snapshot']['victim'] and p['daemonPlan']['clusterUid']==p['snapshot']['kubeSystemUid'],'Daemon Plan target mismatch')
        expected_spec.pop('gracePeriod');expected_spec.update(action='container-kill',containerNames=['backend'])
    f.require(p['fault']['spec']==expected_spec and p['fault']['metadata']=={
        'name':p['runId'],'namespace':'photo','labels':{LABEL:p['runId']}}, 'Exact create-triggered one-shot target required')


def ownership_age_bounds(ownership,current):
    # observedAt is the observer container's UTC clock, not this Mac's clock.
    # Bind it to a read-only date sample from the same pinned observer Pod.
    clock=ownership.get('clock',{});keys=['nodeUnixSeconds','localStartedUnixSeconds','localCompletedUnixSeconds','monotonicElapsedSeconds']
    f.require(clock.get('kind')=='observer-pod-utc' and f.UUID.fullmatch(clock.get('podUid','')) and
              all(finite(clock.get(key))for key in keys),'Observer clock proof required')
    node,start,end,elapsed=(clock[key]for key in keys)
    observed=epoch(ownership['observedAt'])
    f.require(0<=end-start<=2 and 0<=elapsed<=2 and abs((end-start)-elapsed)<=.1 and current>=end and
              0<=node-observed<=5,'Observer clock reference unavailable or stale')
    # Offset is an interval: the node sampled its clock between local start/end.
    # Causal node>=observed proves the metadata predates the reference; clamp only
    # that lower uncertainty bound, never an unproven future timestamp.
    low_offset,high_offset=node-end,node-start
    return [max(0,current-(observed-low_offset)),max(0,current-(observed-high_offset))]


def partial(proof,p,ownership,now=None):
    current=time.time() if now is None else now
    f.require(0<=current-epoch(proof['observedAt'])<=2 and proof['queryElapsedSeconds']<=2 and
              proof['attempt']==1, 'Partial state must be a fresh first-attempt observation')
    rows=validate_rows(proof['rows'],p['tripId'],True)
    f.require(next(r for r in rows if r['section']=='execution_timing')['status'] not in ['COMPLETED','FAILED','CANCELED'], 'Execution already terminal')
    age=ownership_age_bounds(ownership,current)
    f.require(ownership['group']=='prod.yeodam.image-conversion-initial-light' and ownership['state']=='Stable'
              and ownership.get('querySeconds',0)<=2 and age[1]<=5,'Fresh stable Kafka ownership required')
    candidates=[]
    for task in [r for r in rows if r['section']=='worker_task']:
        items=[r for r in rows if r['section']=='worker_item' and r['batchId']==task['batchId']]
        saved=[r for r in items if r['saved']==1]
        current_items=[r for r in items if r['itemId']==task['currentItem'] and r['saved']==0]
        if not (task['status']=='RUNNING' and 2<=len(saved)<len(items) and len(current_items)==1
                and finite(task['leaseSeconds']) and 0<task['leaseSeconds']<=p['criteria']['leaseSeconds']+1 and
                SHA.fullmatch(task['claimHash'] or '') and task['claimHash']==task['attemptOwnerHash']
                and task['currentAttempt']==current_items[0]['attempt'] and task['currentAttempt'] in (1,2)):
            continue
        if p.get('case') in ('W1K','W2K') and len(saved)>p['criteria']['searchSavedMaximum']:continue
        deliveries=[r for r in rows if r['section']=='worker_delivery' and r['taskId']==task['taskId'] and r['status']=='PUBLISHED']
        if not deliveries:continue
        delivery=max(deliveries,key=lambda r:r['deliveryNo'])
        owners=[m for m in ownership['members'] if delivery['partition'] in m['partitions']]
        if len(owners)!=1:continue
        host=owners[0]['host'].lstrip('/')
        f.require(host==p['snapshot']['victim']['ip'], 'Task assigned to a different Pod')
        committed=ownership['committed'].get(str(delivery['partition']))
        f.require(type(committed) is int and committed<=delivery['offset'], 'Task already committed or offset unknown')
        if p.get('case') in ('W1K','W2K'):f.require(SHA.fullmatch(owners[0].get('memberHash','')),'Old consumer member hash required')
        candidates.append({'taskId':task['taskId'],'batchId':task['batchId'],'claimHash':task['claimHash'],
                           'leaseUntil':task['leaseUntil'],'leaseSeconds':task['leaseSeconds'],
                           'currentItem':task['currentItem'],'currentAttempt':task['currentAttempt'],
                           'savedItems':saved,'delivery':delivery,'ownership':ownership,
                           'dbSampleAt':proof['observedAt'],'proofSha256':f.sha(proof),
                           **({'memberHash':owners[0]['memberHash']}if p.get('case') in ('W1K','W2K')else{})})
    f.require(len(candidates)==1,'No unique partially persisted in-flight task; do not kill')
    return candidates[0]


def delete_role(p):
    name='worker-delete-'+p['runId']
    meta={'name':name,'namespace':'photo','labels':{LABEL:p['runId']}}
    return [{'apiVersion':'rbac.authorization.k8s.io/v1','kind':'Role','metadata':meta,'rules':[
        {'apiGroups':[''],'resources':['pods'],'resourceNames':[p['snapshot']['victim']['name']],'verbs':['delete']}]},
        {'apiVersion':'rbac.authorization.k8s.io/v1','kind':'RoleBinding','metadata':meta,
         'roleRef':{'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':name},
         'subjects':[{'kind':'ServiceAccount','name':'chaos-controller-manager','namespace':'chaos-mesh'}]}]


def owned_delete(k,kind,name,namespace,uid,run_id):
    obj=json.loads(k.call(['-n',namespace,'get',kind,name,'--ignore-not-found','-o','json']) or '{}')
    if not obj:return
    f.require(obj['metadata']['uid']==uid and obj['metadata'].get('labels',{}).get(LABEL)==run_id,'Cleanup identity mismatch; never delete a replacement')
    # API delete preconditions protect against a name reused after the read.
    k.call(['delete','--raw',f'/apis/'+('chaos-mesh.org/v1alpha1' if kind=='podchaos' else 'rbac.authorization.k8s.io/v1')+
            f'/namespaces/{namespace}/'+{'podchaos':'podchaos','role':'roles','rolebinding':'rolebindings'}[kind]+f'/{name}',
            '-f','-'],json.dumps({'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':uid}}))
    if kind=='podchaos':
        end=time.monotonic()+30
        while time.monotonic()<end:
            live=json.loads(k.call(['-n',namespace,'get',kind,name,'--ignore-not-found','-o','json'],timeout=min(10,max(.001,end-time.monotonic()))) or '{}')
            if not live:return
            f.require(live['metadata']['uid']==uid,'PodChaos name reused during finalization')
            time.sleep(min(1,max(0,end-time.monotonic())))
        raise f.GuardError('PodChaos finalizer pending; retain permissions and photo profile')


def runtime_lease(k,s):
    review=s.get('leaseReview')
    if review is None:return
    import v3_conversion_lease as lease
    lease.validate_review(review)
    f.require(s['approvedRevision']==review['approvedRevision'],'Approved revision differs from lease review')
    data=k.get('configmap','photo','photo-converter-runtime')['data']
    f.require(lease.policy(data)==review['policy'],'Live converter policy differs from preregistration')


def runtime(k,p):
    f.require(k.identity()==p['snapshot']['kubeSystemUid'],'Wrong cluster')
    runtime_lease(k,p['snapshot'])
    argo=k.get('application','argocd','yeodam-v3-apps')
    f.require(argo['status']['sync']['revision']==p['snapshot']['approvedRevision'] and
              argo['status']['sync']['status']=='Synced' and app_templates(k)==p['snapshot']['apps'], 'Apps changed since Plan')
    pod=k.get('pod','photo',p['snapshot']['victim']['name'])
    f.require(f.ready_pod(pod) and pod['metadata']['uid']==p['snapshot']['victim']['uid'] and
              any(c['name']=='backend' and c.get('containerID')==p['snapshot']['victim']['containerId'] for c in pod['status']['containerStatuses']), 'Victim replaced/restarted')
    f.require(not k.get('networkchaos','kafka')['items'] and not k.get('podchaos','photo')['items'], 'Concurrent fault forbidden')


def normalized_fault(spec):
    result=copy.deepcopy(spec)
    if result.get('action')=='container-kill':
        if result.get('gracePeriod')==0:result.pop('gracePeriod')
    else:result.setdefault('gracePeriod',0)
    return result


def execute(k,p,expected,directory,owner_reader,profile_receipt,stop_guard,reader_cleanup,daemon_receipt=None):
    from v3_worker_chaos import require_active, restore
    verify(p,expected);directory=Path(directory)
    f.require(directory.is_dir() and not directory.is_symlink() and directory.stat().st_mode&0o077==0 and not any(directory.iterdir()), 'Use empty private execution directory')
    # Exclusive local latch is never removed: repeating a one-shot after a timeout is unsafe.
    f.private_write(directory/'started.json',{'planSha256':expected,'at':f.now()})
    container=p.get('case') in ('W1K','W2K')
    import v3_worker_container as hard
    owned=[]; observer=None; receipt={'planSha256':expected,'injectionRequested':False,'terminationConfirmed':False,'cleanup':[]}
    try:
        runtime(k,p);require_active(k,profile_receipt)
        if container:
            f.require(daemon_receipt is not None,'Container kill requires applied daemon receipt')
            hard.require_daemon(k,daemon_receipt,p)
        obsdir=directory/'db';obsdir.mkdir(mode=0o700)
        reads=DeadlineKube(k)
        observer=WorkerDbObserver(reads,p['snapshot'],p['tripId'],obsdir)
        # pod-kill is one-shot in pinned Chaos Mesh 2.8.8: pause is ignored.
        # Only RBAC may be registered early; creating PodChaos IS the injection.
        for obj in ([] if container else delete_role(p)):
            f.private_write(directory/('intent-'+obj['kind']+'.json'),{'kind':obj['kind'],'name':obj['metadata']['name'],'at':f.now()})
            receipt['uncertainCreate']={'kind':obj['kind'],'name':obj['metadata']['name']}
            made=f.bounded_json(k.call(['create','-f','-','-o','json'],json.dumps(obj)))
            uid=made['metadata']['uid'];owned.append((obj['kind'].lower(),obj['metadata']['name'],uid))
            receipt.pop('uncertainCreate',None)
            f.private_write(directory/('owned-'+obj['kind']+'.json'),{'kind':obj['kind'],'name':obj['metadata']['name'],'uid':uid,'runId':p['runId']})
        f.require(not k.get('podchaos','photo')['items'],'Concurrent PodChaos forbidden')
        f.private_write(directory/'input-ready.json',{'at':f.now(),'planSha256':expected,'faultCreated':False,
                        'releaseInputNow':True,'case':p['case']})
        selected=None; search=time.monotonic()+p['criteria']['searchSeconds'];pairs=0;previous_cycle=None
        def observe_pair():
            nonlocal pairs
            owner=owner_reader(deadline=reads.deadline)
            proof=observer.sample();pairs+=1
            f.private_write(directory/f'pair-{pairs:04d}.json',{'at':f.now(),'ownership':owner,'dbProofSha256':f.sha(proof),
                            'dbAgeSeconds':time.time()-epoch(proof['observedAt'])})
            return proof,owner
        previous_start=None
        while time.monotonic()<search:
            reads.deadline=search
            observer.SAMPLE_SECONDS=min(45,max(.001,search-time.monotonic()))
            cycle_start=time.monotonic()
            if container:
                if previous_start is not None:
                    f.require(cycle_start-previous_start<=SAMPLING['maxStartIntervalSeconds'],'Search observation interval exceeded')
                previous_start=cycle_start
                reads.deadline=min(search,cycle_start+SAMPLING['cycleSeconds'])
                observer.SAMPLE_SECONDS=max(.001,reads.deadline-time.monotonic())
                timing=[]
                try:proof,owner,pod=search_sample(reads,observer,p['snapshot'],owner_reader,timing.append)
                finally:
                    if timing:
                        timing[0]['startIntervalSeconds']=None if previous_cycle is None else cycle_start-previous_cycle
                        f.private_write(directory/f'search-cycle-{pairs+1:04d}.json',timing[0])
                previous_cycle=cycle_start;pairs+=1
                f.private_write(directory/f'pair-{pairs:04d}.json',{'at':f.now(),'ownership':owner,'dbProofSha256':f.sha(proof),
                                'dbAgeSeconds':time.time()-epoch(proof['observedAt'])})
            else:
                pod=reads.get('pod','photo',p['snapshot']['victim']['name'])
                f.require(pod['metadata']['uid']==p['snapshot']['victim']['uid'] and f.ready_pod(pod) and
                          any(x.get('name')=='backend' and x.get('containerID')==p['snapshot']['victim']['containerId']
                              for x in pod.get('status',{}).get('containerStatuses',[])), 'Victim changed before create')
                # Fetch target identity before the final metadata -> DB pair, so no
                # registration/API read consumes the fresh partial-work window.
                proof,owner=observe_pair()
            f.require(not observer.gaps and not observer.exhausted,'Observation gap before injection')
            f.require(next(r for r in proof['rows']if r['section']=='execution_timing')['status']not in ('COMPLETED','FAILED','CANCELED'),'Execution already terminal')
            if input_accepted(proof['rows']):
                try:selected=partial(proof,p,owner)
                except f.GuardError as e:
                    if str(e)!='No unique partially persisted in-flight task; do not kill':raise
            if selected:break
            delay=max(0,cycle_start+p['criteria']['searchPollSeconds']-time.monotonic())if container else 1
            time.sleep(min(delay,max(0,search-time.monotonic())))
        f.require(selected is not None,'Partial processing window missed; no injection')
        f.private_write(directory/'partial.json',selected)
        f.private_write(directory/'pre-injection.json',selected)
        f.require(time.time()-epoch(selected['dbSampleAt'])<=2,'Last partial observation expired')
        # Journal before the destructive CREATE, including an ambiguous timeout.
        # Never label a created one-shot as inactive or wait for an unpause PATCH.
        receipt['injectionRequested']=True;receipt['requestAt']=f.now();mono=time.monotonic()
        f.private_write(directory/'injection-intent.json',{'at':receipt['requestAt'],'trigger':'PodChaos.create',
                        'victimUid':pod['metadata']['uid'],'victimContainerId':p['snapshot']['victim']['containerId'],'runId':p['runId'],'case':p['case']})
        fault_obj=p['fault']
        receipt['uncertainCreate']={'kind':'PodChaos','name':p['runId']}
        raw=k.call(['create','-f','-','-o','json'],json.dumps(fault_obj))
        receipt['createResponseAt']=f.now()
        fault=f.bounded_json(raw);uid=fault['metadata']['uid']
        owned.append(('podchaos',p['runId'],uid));receipt.pop('uncertainCreate',None)
        receipt['faultCreateAccepted']=True
        f.private_write(directory/'owned-PodChaos.json',{'kind':'PodChaos','name':p['runId'],'uid':uid,'runId':p['runId']})
        receipt['case']=p['case'];receipt['victimContainerId']=p['snapshot']['victim']['containerId']
        if container:f.require(normalized_fault(fault['spec'])==p['fault']['spec'],'Admitted container fault changed; inspect possible injection')
        deadline=mono+p['criteria']['observationSeconds'];state='incomplete'; samples=[]
        while time.monotonic()<deadline:
            reads.deadline=deadline;observer.SAMPLE_SECONDS=min(45,max(.001,deadline-time.monotonic()))
            proof=observer.sample();samples.append(proof)
            fault_state=reads.get('podchaos','photo',p['runId'])
            f.require(fault_state['metadata']['uid']==fault['metadata']['uid'],'Fault UID changed during observation')
            if container:f.require(normalized_fault(fault_state['spec'])==p['fault']['spec'],'Container fault changed during observation')
            records=[{key:value for key,value in record.items() if key in ('id','phase','injectedCount','recoveredCount')} for record in fault_state.get('status',{}).get('experiment',{}).get('containerRecords',[])]
            receipt['chaosInjectedObserved']=receipt.get('chaosInjectedObserved',False) or any(record.get('id')==('photo/'+p['snapshot']['victim']['name']+('/backend'if container else '')) and record.get('injectedCount',0)>0 for record in records)
            raw=reads.call(['-n','photo','get','pod',p['snapshot']['victim']['name'],'--ignore-not-found','-o','json'])
            old=json.loads(raw or '{}')
            term=not old or old['metadata']['uid']!=p['snapshot']['victim']['uid'] or any(
                c['name']=='backend' and c.get('state',{}).get('terminated') for c in old.get('status',{}).get('containerStatuses',[]))
            process_end=hard.termination(p,old) if container else None
            if container:term=bool(process_end) # disappearance or a different Pod UID is not process death proof
            try:recovery_owner=owner_reader(deadline=deadline)
            except (f.GuardError,subprocess.SubprocessError,OSError) as error:
                # A post-injection metadata gap is evidence, not proof the fault
                # did not happen and not a reason to abandon business recovery.
                recovery_owner={'unavailable':True,'at':f.now(),'errorKind':type(error).__name__}
            evidence={'at':f.now(),'chaosRecords':records,'chaosConditions':[{key:value for key,value in condition.items() if key in ('type','status')}for condition in fault_state.get('status',{}).get('conditions',[])],'victimApiObjectAbsentOrReplaced':not old or old['metadata']['uid']!=p['snapshot']['victim']['uid'],
                      'terminationEvidenceObserved':bool(term),'apiAbsenceIsNotKernelDeathProof':True,
                      'pods':[{'name':x['metadata']['name'],'uid':x['metadata']['uid'],'ip':x['status'].get('podIP'),'node':x['spec'].get('nodeName'),'ready':f.ready_pod(x),
                               'containerStatuses':[{a:v for a,v in c.items() if a in ['name','restartCount','containerID','state','lastState']} for c in x['status'].get('containerStatuses',[])]}
                              for x in reads.get('pods','photo',selector='app.kubernetes.io/instance=photo-converter')['items']],
                      'ownership':recovery_owner,'processTermination':process_end}
            evidence['at']=f.now() # completion upper bound, after ownership/Pod reads
            f.private_write(directory/f'runtime-{len(samples):04d}.json',evidence)
            # Preserve precise container termination if obtainable, otherwise only an API-object bound.
            if term and not receipt['terminationConfirmed']:
                receipt['terminationConfirmed']=True;receipt['terminationObservedAt']=evidence['at'];receipt['terminationPrecision']='api-object-or-container-state; inspect runtime records'
            if container and process_end and 'postKillWindowObserved'not in receipt:
                # Re-read AFTER seeing old-container exit137; the earlier DB sample might predate death.
                after=observer.sample();samples.append(after);f.private_write(directory/'post-termination.json',after)
                receipt['postKillWindowObserved']=hard.post_kill_window(selected,after)
                if p['case']=='W2K':
                    import v3_conversion_lease as lease
                    frozen=lease.cohort(selected,after)
                    f.private_write(directory/'post-death-cohort.json',frozen)
                    receipt['postDeathCohortQualified']=frozen['qualified']
                receipt['processKillObserved']=True;receipt['terminationPrecision']='old containerID + exit137 (not OOM) + Chaos Injected required'
                proof=after # business monitoring continues even when the selected photo window was missed
            status=next(r for r in proof['rows'] if r['section']=='execution_timing')['status']
            if status in ('COMPLETED','FAILED','CANCELED'):
                state='completed' if status=='COMPLETED' else 'business_failure';break
            time.sleep(min(3,max(0,deadline-time.monotonic())))
        receipt['observationState']=state
        if samples:f.private_write(directory/'final.json',samples[-1])
    except BaseException as error:
        f.private_write(directory/'failure.json',{'at':f.now(),'kind':type(error).__name__,
                        'guardReason':str(error)if isinstance(error,f.GuardError)else None,
                        'injectionRequested':receipt['injectionRequested']})
        raise
    finally:
        # Do not remove an active kill's CR and claim that the deleted Pod recovered.
        # Its one-shot effect is irreversible; preserve intent and separately follow ReplicaSet/business.
        try:stop_guard()
        except Exception:receipt['gpuStopSignalFailed']=True
        if observer:
            summary=observer.summary(receipt.get('observationState','aborted'))
            f.private_write(directory/'db-summary.json',summary)
        errors=[{'kind':'gpu-stop-signal','reason':'guard_signal_failed'}] if receipt.get('gpuStopSignalFailed') else []
        if receipt.get('uncertainCreate'):errors.append(dict(receipt['uncertainCreate'],reason='create_response_unknown_manual_uid_review_required'))
        for kind,name,uid in reversed(owned):
            if receipt.get('uncertainCreate',{}).get('kind')=='PodChaos':break # retain finalizer permissions for manual UID review
            try:owned_delete(k,kind,name,'photo',uid,p['runId']);receipt['cleanup'].append({'kind':kind,'name':name,'removed':True})
            except Exception:
                errors.append({'kind':kind,'name':name,'reason':'cleanup_failed_raw_withheld'})
                if kind=='podchaos':break # do not revoke permissions required by its finalizer
        try:receipt['readerRemoved']=reader_cleanup()
        except Exception:errors.append({'kind':'reader','reason':'reader_cleanup_failed_raw_withheld'})
        if container and daemon_receipt is not None:
            try:receipt['daemonRestored']=hard.restore_daemon(k,daemon_receipt)
            except Exception:errors.append({'kind':'chaos-daemon','reason':'restore_failed_raw_withheld'})
        try:receipt['profileRestored']=restore(k,profile_receipt)
        except Exception:errors.append({'kind':'chaos-profile','reason':'restore_failed_raw_withheld'})
        receipt['cleanupErrors']=errors
        f.private_write(directory/'receipt.json',receipt)
    f.require(not receipt['cleanupErrors'],'Owned cleanup incomplete; inspect receipt')
    return receipt


def assess(before,after,timing,trace,db_summary):
    f.require(timing.get('case','W1') in ('W1','W1K'),'Use the preregistered W2K assessor; never reclassify a lease trial as W1')
    trip=before['rows'][0]['tripId'];validate_rows(before['rows'],trip,True);validate_rows(after['rows'],trip,True)
    b={r['itemId']:r for r in before['rows'] if r['section']=='worker_item'}
    a={r['itemId']:r for r in after['rows'] if r['section']=='worker_item'}
    attachments=[r for r in after['rows'] if r['section']=='final_attachment']
    status=next(r for r in after['rows'] if r['section']=='execution_timing')['status']
    saved_preserved=all(a[i]['derivedHash']==v['derivedHash'] and a[i]['attachmentId']==v['attachmentId'] and a[i]['attempt']==v['attempt'] for i,v in b.items() if v['saved'])
    complete=set(a)==set(b) and all(r['saved']==1 and r['attachmentId'] is not None for r in a.values())
    ids=[r['attachmentId'] for r in attachments];files=[r['fileId'] for r in attachments]
    identity=complete and len(ids)==len(set(ids))==len(files)==len(set(files))==20 and set(ids)=={r['attachmentId'] for r in a.values()}
    gaps=db_summary.get('observationGaps',[])
    tid=trace.get('trace',{}).get('traceId');parents=[r.get('traceparent') for r in after['rows'] if r['section']=='worker_delivery']
    trace_ready=bool(trace.get('traceEvidenceReady') is True and trace.get('telemetryIntegrity')=='no_observed_loss' and
        trace.get('trace',{}).get('tracePathComplete') is True and re.fullmatch('[a-f0-9]{32}',tid or '') and parents and
        all(re.fullmatch('00-'+tid+'-[a-f0-9]{16}-[a-f0-9]{2}',x or '') for x in parents))
    # No automatic semantic mapping of an API delete/AllInjected to actual process death.
    keys=['eligibilityAt','newClaimAt','completedAt','terminationLowerAt','terminationUpperAt']
    times_known=all(timing.get(x) is not None for x in keys)
    eligible=epoch(timing['newClaimAt'])-epoch(timing['eligibilityAt']) if times_known else None
    completion=epoch(timing['completedAt'])-epoch(timing['newClaimAt']) if times_known else None
    valid_times=times_known and 0<=eligible and 0<=completion and epoch(timing['terminationLowerAt'])<=epoch(timing['terminationUpperAt'])
    current_ids={r['currentItem'] for r in before['rows'] if r['section']=='worker_task' and r['currentItem'] is not None}
    interrupted=any(i in b and b[i]['attempt']>0 and a[i]['attempt']>b[i]['attempt'] for i in current_ids if i in a)
    f.require(before['approvedRevision']==after['approvedRevision']==db_summary['approvedRevision'] and db_summary['tripIds']==[trip], 'Mismatched deployment or DB summary')
    coverage=identity and saved_preserved and interrupted and valid_times and timing.get('derivedFromRecordedSamples') is True and timing.get('podKillObserved') is True and timing.get('prematureClaimObserved') is False and not gaps and db_summary.get('continuousObservationComplete') is True and not timing.get('manualActions') and trace_ready
    result={'case':'W1','tripId':trip,'evidenceComplete':bool(coverage),'automaticRecovery':bool(status=='COMPLETED' and coverage) if coverage else None,
            'savedItemsPreserved':saved_preserved,'identityMatched':identity,'interruptedAttemptReprocessed':interrupted,'attemptIncreases':{str(i):a[i]['attempt']-b[i]['attempt'] for i in a if i in b and a[i]['attempt']!=b[i]['attempt']},
            'eligibilityToClaimSeconds':eligible,'claimToCompletionSeconds':completion,
            'claimTargetMet':bounded_target(timing.get('eligibilityToClaimBounds'),15),
            'completionTargetMet':bounded_target(timing.get('claimToCompletionBounds'),240),
            'missingItemIds':sorted(set(b)-set(a)),'exactlyOnceProven':False,'s3SideEffectsVerified':False,
            'killedOpenSpanMayBeAbsent':True,'traceAvailable':trace_ready,
            'limitations':['timestamps are observer bounds; do not equate API disappearance with kernel death',
                           'raw broker delivery count and all S3 effects unmeasured','attempt increase is expected, not itself duplicate business output'],
            'sourceHashes':{name:f.sha(v) for name,v in [('before',before),('after',after),('timing',timing),('trace',trace),('db',db_summary)]}}
    if timing.get('case')=='W1K':
        result['case']='W1K'
        chosen=timing.get('selectedItemId');chosen_attempt=timing.get('selectedAttempt')
        result['selectedPhotoRetried']=bool(chosen in b and chosen in a and b[chosen]['attempt']==chosen_attempt and a[chosen]['attempt']>chosen_attempt)
        result['selectedObservationMatched']=timing.get('selectedProofSha256')==f.sha(before)
        result['processKillObserved']=timing.get('processKillObserved')is True
        result['postKillWindowObserved']=timing.get('postKillWindowObserved')is True
        result['leaseReleaseObserved']=timing.get('leaseReleaseObserved')is True
        hard_coverage=(result['selectedPhotoRetried'] and result['selectedObservationMatched'] and result['processKillObserved'] and result['postKillWindowObserved'] and not result['leaseReleaseObserved'])
        if not hard_coverage:result.update(evidenceComplete=False,automaticRecovery=None)
    return result


def bounded_target(bounds,target):
    if not bounds or any(not finite(x) for x in bounds) or not 0<=bounds[0]<=bounds[1]:return None
    if bounds[1]<=target:return True
    if bounds[0]>target:return False
    return None # interval straddles target: do not invent millisecond precision


def iso(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()


def summarize(directory):
    """Derive clock-aware observation bounds, preserving all source hashes and gaps."""
    directory=Path(directory)
    def read(path):
        f.require(path.is_file() and not path.is_symlink() and path.stat().st_size<=4*1024*1024,'Bounded evidence file required')
        return f.bounded_json(path.read_text())
    sources={}
    def take(path):
        v=read(path);sources[str(path.relative_to(directory))]=f.sha(v);return v
    receipt=take(directory/'receipt.json');selected=take(directory/'pre-injection.json')
    db=take(directory/'db-summary.json')
    samples=[take(directory/'db'/name) for name in db['sampleFiles']]
    f.require(samples and receipt['injectionRequested'],'No injected recorded trial')
    runtime=[take(path) for path in sorted(directory.glob('runtime-*.json'))]
    task_id=selected['taskId'];old_hash=selected['claimHash'];last_old=None;new=None;new_prior=None
    events={};series=[];completed=None;pre_acceptance=[]
    # Store local timeline with DB clock interval mapping per actual sample. No interpolation.
    for sample in samples:
        rows=validate_rows(sample['rows'],db['tripIds'][0],False)
        if not input_accepted(rows):
            pre_acceptance.append(sample['observedAt']);continue
        clock=epoch(next(r['utc'] for r in rows if r['section']=='db_clock'))
        task=next(r for r in rows if r['section']=='worker_task' and r['taskId']==task_id)
        offset=sample['dbMinusLocalIntervalSeconds']
        f.require(len(offset)==2 and all(finite(x) for x in offset) and offset[0]<=offset[1],'Clock interval missing')
        local_bounds=[clock-offset[1],clock-offset[0]]
        series.append({'at':sample['observedAt'],'savedPhotos':sum(x['saved'] for x in rows if x['section']=='worker_item'),
                       'attempts':sum(x['attempt'] for x in rows if x['section']=='worker_item'),
                       'leaseRemainingSeconds':max(0,task['leaseSeconds'] or 0)})
        if task['claimHash']==old_hash:last_old=(sample,task,local_bounds)
        elif new is None and task['claimHash']:
            new=(sample,task,local_bounds);new_prior=last_old
        execution=next(x for x in rows if x['section']=='execution_timing')
        if execution['status']=='COMPLETED' and execution['endedAt'] and completed is None:
            ended=epoch(execution['endedAt']);completed=[ended-offset[1],ended-offset[0]]
        if new and 'firstNewSaveAt' not in events:
            before_ids={x['itemId'] for x in selected['savedItems']}
            if any(x['section']=='worker_item' and x['saved'] and x['itemId'] not in before_ids for x in rows):events['firstNewSaveAt']=iso(local_bounds[1])
    for name in db['observationGaps']:
        gap=take(directory/'db'/name)
        if gap.get('firstFailedAttemptStartedAt'):series.append({'at':gap['firstFailedAttemptStartedAt'],'savedPhotos':None,'attempts':None,'leaseRemainingSeconds':None})
    series.sort(key=lambda x:epoch(x['at']))
    assigned=None;ready=None;previous=epoch(receipt['requestAt'])
    for rt in runtime:
        at=epoch(rt['at']);pods=[p for p in rt['pods'] if p['uid']!=receipt.get('victimUid',selected.get('victimUid'))]
        # Exclude original Pod by its pinned name, available in the injection intent.
        intent=take(directory/'injection-intent.json') if 'injection-intent.json' not in sources else read(directory/'injection-intent.json')
        if receipt.get('case') in ('W1K','W2K'):
            import v3_worker_container as hard
            pods=[p for p in rt['pods'] if hard.replacement({'snapshot':{'victim':{'containerId':intent['victimContainerId']}}},p)]
        else:pods=[p for p in pods if p['uid']!=intent['victimUid']]
        if ready is None and any(p['ready'] for p in pods):ready=[previous,at]
        owner=rt.get('ownership',{})
        try:fresh=owner.get('querySeconds',0)<=2 and ownership_age_bounds(owner,at)[1]<=5
        except (f.GuardError,KeyError,TypeError,ValueError):fresh=False
        member_changed=(hard.new_owner(selected,pods,owner)if receipt.get('case') in ('W1K','W2K')else True)
        if assigned is None and fresh and member_changed and owner.get('state')=='Stable' and any(
            m['host'].lstrip('/')==p.get('ip') and selected['delivery']['partition'] in m['partitions']
            for p in pods for m in owner.get('members',[])):assigned=[previous,at]
        previous=at
    termination=[epoch(receipt['requestAt']),epoch(receipt['terminationObservedAt'])] if receipt.get('terminationConfirmed') else None
    claim=None;eligibility=None;claim_delay=None;processing=None;premature=None
    if new and new_prior:
        # Claim committed after last old observation, by first new observation.
        claim=[new_prior[2][0],new[2][1]]
        prior_sample,old_task,_=new_prior
        offset=prior_sample['dbMinusLocalIntervalSeconds'];lease=epoch(old_task['leaseUntil'])
        lease_bounds=[lease-offset[1],lease-offset[0]]
        events['leaseUntil']=iso(lease_bounds[1]);premature=claim[1]<lease_bounds[0]
        if assigned:
            # A later unseen lease renewal can only move eligibility later. Use the
            # observed lease as a LOWER bound and first successful claim as upper.
            eligibility=[max(lease_bounds[0],assigned[0]),max(claim[1],assigned[1])]
            claim_delay=[0,max(0,claim[1]-eligibility[0])]
        events['newClaimAt']=iso(claim[1])
        if completed:processing=[max(0,completed[0]-claim[1]),max(0,completed[1]-claim[0])]
    if ready:events['replacementReadyAt']=iso(ready[1])
    if assigned:events['consumerAssignedAt']=iso(assigned[1])
    if completed:events['completedAt']=iso(completed[1])
    if termination:events.update(terminationLowerAt=iso(termination[0]),terminationUpperAt=iso(termination[1]))
    result={'derivedFromRecordedSamples':True,'podKillObserved':receipt.get('chaosInjectedObserved') is True and receipt.get('terminationConfirmed') is True,'events':events,'samples':series,
            'eligibilityAt':iso(eligibility[0]) if eligibility else None,'newClaimAt':iso(claim[1]) if claim else None,
            'completedAt':iso(completed[1]) if completed else None,
            'terminationLowerAt':iso(termination[0]) if termination else None,'terminationUpperAt':iso(termination[1]) if termination else None,
            'eligibilityToClaimBounds':claim_delay,'claimToCompletionBounds':processing,'prematureClaimObserved':premature,
            'claimAtBounds':[iso(x) for x in claim] if claim else None,
            'completedAtBounds':[iso(x) for x in completed] if completed else None,
            'manualActions':receipt.get('manualActions',[]),'observationGaps':db['observationGaps'],'sourceHashes':sources,
            'precision':'observer intervals; observed lease is a lower eligibility bound; API disappearance is not kernel death',
            'traceStillRequired':True,'preAcceptanceSamples':pre_acceptance}
    if receipt.get('case') in ('W1K','W2K'):
        import v3_worker_container as hard
        result=hard.lease_timing(result,receipt,selected,samples,runtime)
        if receipt.get('case')=='W2K':result['case']='W2K'
    return result


def dashboard(record):
    events=record['events'];samples=record['samples'];case=record.get('case','W1')
    f.require(case in('W1','W1K','W2K'),'Known worker fault case required')
    allowed={'terminationLowerAt','terminationUpperAt','replacementReadyAt','consumerAssignedAt','leaseUntil','newClaimAt','firstNewSaveAt','completedAt'}
    f.require(set(events)<=allowed and events and samples,'Recorded events and samples required')
    names=list(events);times=[epoch(events[k])*1000 for k in names];start=min(times)
    def frame(name,fields,values):return {'name':name,'fields':[dict(field,values=values[i])for i,field in enumerate(fields)],'length':len(values[0])}
    table=frame(case+' recorded events',[{'name':'Event','type':'string'},{'name':'Time','type':'time'},{'name':'Elapsed seconds','type':'number'}],
                [names,times,[(x-start)/1000 for x in times]])
    bounds=[record.get('eligibilityToClaimBounds'),record.get('claimToCompletionBounds')]
    duration=frame('Conservative duration bounds',[{'name':'Interval','type':'string'},{'name':'Lower seconds','type':'number'},{'name':'Upper seconds','type':'number'}],
                   [['Eligible → new claim','New claim → completed'],[x[0]if x else None for x in bounds],[x[1]if x else None for x in bounds]])
    series=frame('Recorded DB samples',[{'name':'Time','type':'time'},{'name':'Saved photos','type':'number'},{'name':'Attempt total','type':'number'},{'name':'Lease remaining seconds','type':'number'}],
                  [[epoch(x['at'])*1000 for x in samples]]+[[x[k]for x in samples]for k in ['savedPhotos','attempts','leaseRemainingSeconds']])
    annotations=[{'time':epoch(v)*1000,'text':k,'tags':[case+' recorded evidence']}for k,v in events.items()]
    return {'title':case+' — persisted photos and recovery boundaries','timezone':'Asia/Seoul','schemaVersion':41,
            'time':{'from':iso((start-10000)/1000),'to':iso((max(times)+10000)/1000)},
            'description':'Native Grafana Snapshot of recorded evidence. Sample bounds and gaps remain visible; API disappearance is not kernel death.',
            'annotations':{'list':[{'name':'Recorded boundaries','type':'dashboard','enable':True,'hide':False,'iconColor':'#FF9830','datasource':{'type':'grafana','uid':'-- Grafana --'},'snapshotData':annotations}]},
            'panels':[{'id':1,'type':'timeseries','title':'Saved outputs / retry attempts / remaining lease — recorded samples',
                       'gridPos':{'x':0,'y':0,'w':24,'h':10},'options':{'legend':{'displayMode':'table','placement':'bottom'}},
                       'fieldConfig':{'defaults':{'custom':{'drawStyle':'line','lineInterpolation':'stepAfter','spanNulls':False}},
                                      'overrides':[{'matcher':{'id':'byName','options':'Lease remaining seconds'},'properties':[{'id':'custom.axisPlacement','value':'right'},{'id':'unit','value':'s'}]}]},'snapshotData':[series]},
                      {'id':2,'type':'table','title':'Observed boundaries — exact source timestamps',
                       'gridPos':{'x':0,'y':10,'w':14,'h':10},'snapshotData':[table]},
                      {'id':3,'type':'table','title':'Recovery durations — lower / upper bounds',
                       'gridPos':{'x':14,'y':10,'w':10,'h':10},'snapshotData':[duration]}]}
