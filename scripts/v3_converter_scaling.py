"""Fixed-scope photo scaling review. No credential clients or mutations in this module."""
import hashlib,json,re,time
from pathlib import Path
from v3_autoscaling import LANES,prepare_candidate
from v3_cdc_fault import require
ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'tests/ci/v3/converter/ConverterScalingFixture.java'
TOPICS={'prod.yeodam.image.'+lane for lane in LANES}
PROOF=('twoPartitions','twoConsumersAssigned','parallelProgress','noEarlyCommit','inflightDrain','redeliveryCompleted','duplicateResultPrevented')
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def verify_proof(proof,component):
    require(proof.get('schemaVersion')==1 and all(proof.get(k)is True for k in PROOF),'Exact two-partition Worker/Listener proof required')
    require(proof.get('image')==component['image'] and proof.get('sourceSha')==component['sourceSha'],'Proof must match deployed BE image/source')
    require(proof.get('fixtureSha256')==hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),'Fixture changed since proof')
    require(proof.get('syntheticPersistence')is True and proof.get('nativeConversionTested')is False,'Preserve honest fixture limitations')
def kafka_quiet(result,partitions):
    groups=result.get('groups',[])
    require(len(groups)==4 and {g['lane'] for g in groups}==set(LANES),'Exactly four lane groups required')
    ends={}
    for g in groups:
        parts=g['partitions']
        require(g['state'].lower()=='stable' and 1<=g['members']<=2,'All converter groups must be Stable')
        require(g['lagKnown']is True and g['lag']==0 and g['uniqueAssignments']is True and g['assigned']==partitions,'No unknown lag or incomplete assignment may pass')
        require(len(parts)==partitions and {p['partition']for p in parts}==set(range(partitions)),'Unexpected partitions')
        for p in parts:
            require(p['replicas']==3 and p['isr']==3,'All three replicas must be in sync before change')
            require(all(type(p[k])is int and p[k]>=0 for k in ('begin','end','committed')) and p['begin']<=p['committed']==p['end'],'Never reset or assume a nonempty unknown offset')
            ends[g['lane'],p['partition']]=p['end']
    return ends

def verify_sample(sample,component,partitions,now=None):
    now=time.time() if now is None else now
    require(type(sample.get('recordedAt'))in (int,float) and 0<=now-sample['recordedAt']<=90,'Observation is stale or from the future')
    require(sample.get('clusterUid')=='46b80c44-56f4-416d-9f9b-e8b7b4cdc6a9','Wrong cluster')
    require(sample.get('image')==component['image'],'Live converter image differs from review')
    require(sample.get('converterReady')is True and sample.get('graceSeconds')==180,'Preserve Ready converter and shutdown grace')
    require(sample.get('cdcRunning')is True,'CDC must be running before drain/partition review')
    require(type(sample.get('brokerFreeBytes'))is list and len(sample['brokerFreeBytes'])==3 and all(type(v)is int and v>=1024**3 for v in sample['brokerFreeBytes']),'Three broker disks need at least 1Gi free each')
    db=sample.get('business',{})
    require(set(db)=={'waiting','running','unknown','unpublishedActive'} and all(type(v)is int and v==0 for v in db.values()),'All active DB work and unpublished active tasks must drain')
    require(type(sample.get('topics'))is list and {t['name']for t in sample['topics']}==TOPICS and len(sample['topics'])==4,'Only four photo topics permitted')
    for t in sample['topics']:
        require(t['partitions']==partitions and t['ready']is True and bool(t['uid']),'Unexpected Topic identity/state')
    return kafka_quiet(sample['kafka'],partitions)

def prepare(samples,proof,candidate,traffic_quiet=False,now=None):
    require(traffic_quiet is True,'Operator must confirm the coordinated photo quiet window')
    require(len(samples)==2,'Two fresh observations required')
    component=candidate['components']['backend'];verify_proof(proof,component)
    require(all(re.fullmatch('[a-f0-9]{40}',s.get('approvedRef',''))for s in samples),'Exact approved Snapshot ref required')
    counts={t['partitions']for t in samples[-1]['topics']};require(len(counts)==1,'Partial partition expansion: stop and review; never decrease')
    partitions=next(iter(counts));require(partitions in (1,2),'Only reviewed 1 to 2 expansion permitted')
    a,b=[verify_sample(s,component,partitions,now)for s in samples]
    require(5<=samples[1]['recordedAt']-samples[0]['recordedAt']<=45,'Observe a bounded quiet interval')
    require(a==b,'Kafka end offsets moved during quiet window')
    for key in ['approvedRef','converterUid','connectorUid']:
        require(samples[0].get(key)==samples[1].get(key) and bool(samples[1].get(key)),'Live identity changed during review')
    require({(x['name'],x['uid'])for x in samples[0]['topics']}=={(x['name'],x['uid'])for x in samples[1]['topics']},'Topics changed during review')
    return {'schemaVersion':1,'preparedAt':time.time() if now is None else now,'approvedRef':samples[-1]['approvedRef'],'image':component['image'],'sourceSha':component['sourceSha'],'proofSha256':digest(proof),'observationsSha256':digest(samples),'connectorUid':samples[-1]['connectorUid'],'cdcRestart':{'connector':'yeodam-v3-outbox','preserveOffsets':True},'currentPartitions':partitions,'desiredPartitions':2,'topics':samples[-1]['topics'],'hpaMin':1,'hpaMax':2,'lagThreshold':5,'rollback':{'partitions':2,'converterReplicas':1,'disableScaledObject':True},'gpuChanges':False}

def validate_plan(plan,sha,samples,proof,candidate,now=None):
    require(digest(plan)==sha,'Reviewed plan hash mismatch')
    now=time.time() if now is None else now
    require(0<=now-plan['preparedAt']<=600,'Plan expired; collect fresh observations')
    expected=prepare(samples,proof,candidate,True,plan['preparedAt'])
    expected['preparedAt']=plan['preparedAt']
    require(plan==expected,'Plan source/evidence changed')
    return plan

def activation_candidate(candidate,plan):
    require(plan['image']==candidate['components']['backend']['image'] and plan['sourceSha']==candidate['components']['backend']['sourceSha'],'Candidate changed after plan')
    require(plan['desiredPartitions']==2 and plan['hpaMin']==1 and plan['hpaMax']==2,'Fixed converter budget required')
    return prepare_candidate(candidate,True)

def review_partition_change(before,after):
    def keyed(docs):return {(d['kind'],d['metadata'].get('namespace'),d['metadata']['name']):d for d in docs}
    from copy import deepcopy
    old=keyed(before);new=keyed(after);require(set(old)==set(new),'Never add/delete resources during partition expansion')
    changed=[]
    for key,current in new.items():
        previous=deepcopy(old[key])
        if previous!=current:
            require(key[0]=='KafkaTopic'and key[2]in TOPICS and previous['spec']['partitions']==1 and current['spec']['partitions']==2,'Only reviewed 1-to-2 photo partitions may change')
            previous['spec']['partitions']=2;require(previous==current,'Preserve all names/config/replication/retention/ACLs');changed.append(key[2])
    require(set(changed)==TOPICS,'Exactly four photo topics must expand')
