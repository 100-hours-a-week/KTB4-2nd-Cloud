import copy,hashlib,json,sys,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import v3_converter_scaling as m
class Converter(unittest.TestCase):
 def setUp(self):
  self.now=time.time();self.c=json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text())
  self.p={k:True for k in m.PROOF};self.p.update(schemaVersion=1,image=self.c['components']['backend']['image'],sourceSha=self.c['components']['backend']['sourceSha'],fixtureSha256=hashlib.sha256(m.FIXTURE.read_bytes()).hexdigest(),syntheticPersistence=True,nativeConversionTested=False)
  self.s={'recordedAt':self.now-10,'clusterUid':'46b80c44-56f4-416d-9f9b-e8b7b4cdc6a9','image':self.p['image'],'converterReady':True,'graceSeconds':180,'cdcRunning':True,'brokerFreeBytes':[2*1024**3]*3,'business':dict(waiting=0,running=0,unknown=0,unpublishedActive=0),'topics':[dict(name='prod.yeodam.image.'+lane,uid=lane,partitions=1,ready=True)for lane in m.LANES],'approvedRef':'a'*40,'converterUid':'uid','connectorUid':'cdcuid','kafka':{'groups':[dict(lane=lane,state='Stable',members=1,lagKnown=True,lag=0,uniqueAssignments=True,assigned=1,partitions=[dict(partition=0,replicas=3,isr=3,begin=0,end=10,committed=10)])for lane in m.LANES]}}
  self.samples=[self.s,copy.deepcopy(self.s)];self.samples[1]['recordedAt']=self.now-2
 def plan(self):return m.prepare(self.samples,self.p,self.c,True,self.now)
 def test_access_preparation_does_not_activate_converter(self):
  import importlib.util,subprocess,os,yaml
  spec=importlib.util.spec_from_file_location('scaling',ROOT/'scripts/check-v3-autoscaling.py');check=importlib.util.module_from_spec(spec);spec.loader.exec_module(check)
  text=subprocess.check_output([os.environ['V3_HELM'],'template','yeodam-v3-autoscaling',str(check.CHART),'--set','enabled=true,apiEnabled=true,metricsServerReady=true,replicaOwnershipReviewed=true,converterAccessEnabled=true'],text=True)
  docs=[d for d in yaml.safe_load_all(text)if d];check.validate(docs,api=True,converter=False)
  self.assertEqual(sum(d['kind']=='HorizontalPodAutoscaler'for d in docs),4)
  self.assertFalse(any(d['kind']=='ScaledObject'for d in docs))
  user=next(d for d in docs if d['kind']=='KafkaUser');self.assertTrue(all(x['operations']==['Describe']for x in user['spec']['authorization']['acls']))
 def test_cdc_transition_only_patches_state_and_checks_uid(self):
  import importlib.util
  from unittest.mock import patch
  spec=importlib.util.spec_from_file_location('manager',ROOT/'scripts/manage-v3-converter-scaling.py');manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager)
  class Fake:
   def __init__(self):self.state='running';self.calls=[]
   def get(self,*args):return dict(metadata=dict(uid='cdcuid',resourceVersion='42',generation=2),spec=dict(state=self.state,config={'immutable':'offset-history'}),status=dict(observedGeneration=2,connectorStatus=dict(connector=dict(state=self.state.upper()),tasks=[dict(state='RUNNING')]if self.state=='running'else [])))
   def call(self,args):
    self.calls.append(args);body=json.loads(args[-1]);self.state=body[-1]['value'];return ''
  fake=Fake();manager.cdc_state(fake,'cdcuid','stopped');self.assertEqual(fake.state,'stopped');manager.cdc_state(fake,'cdcuid','running');self.assertEqual(fake.state,'running')
  self.assertTrue(all(json.loads(x[-1])[-1]['path']=='/spec/state'for x in fake.calls))
  before=len(fake.calls)
  with self.assertRaises(ValueError):manager.cdc_state(fake,'wrong','stopped')
  self.assertEqual(len(fake.calls),before)
 def test_mutation_entry_points_require_explicit_approval(self):
  import importlib.util
  from unittest.mock import patch
  spec=importlib.util.spec_from_file_location('manager_approval',ROOT/'scripts/manage-v3-converter-scaling.py');manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager)
  for action in ('provision-access','observe','expand','activate','rollback'):
   with self.subTest(action=action),patch.object(sys,'argv',['manage',action,'--kubeconfig','synthetic']),patch.object(manager,'Live',side_effect=AssertionError('No live access before approval')),self.assertRaises(ValueError):manager.main()
 def test_slow_read_does_not_add_another_quiet_wait(self):
  import importlib.util
  from unittest.mock import patch
  spec=importlib.util.spec_from_file_location('manager_spacing',ROOT/'scripts/manage-v3-converter-scaling.py');manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager)
  for latency,expected in [(1,5),(42,42)]:
   clock=[100.0]
   def read(*args):
    start=clock[0];clock[0]+=latency;return {'recordedAt':start}
   def sleep(seconds):clock[0]+=seconds
   with self.subTest(latency=latency),patch.object(manager,'sample',side_effect=read),patch.object(manager.time,'time',side_effect=lambda:clock[0]),patch.object(manager.time,'sleep',side_effect=sleep):
    pair=manager.observe_pair(None,None,None)
    self.assertEqual(pair[1]['recordedAt']-pair[0]['recordedAt'],expected)
 def test_topic_resource_and_helm_storage_namespaces_are_distinct(self):
  import importlib.util
  spec=importlib.util.spec_from_file_location('manager_owner',ROOT/'scripts/manage-v3-converter-scaling.py');manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager)
  class Fake:
   def __init__(self):self.changed=None;self.calls=[]
   def get(self,kind,namespace,name):
    self.calls.append((kind,namespace,name));return {'metadata':{'annotations':{'meta.helm.sh/release-name':'yeodam-v3-kafka-access','meta.helm.sh/release-namespace':self.changed if name==sorted(m.TOPICS)[-1] and self.changed else 'default'}}}
  fake=Fake();self.assertEqual(manager.topic_release(fake),('yeodam-v3-kafka-access','default'))
  self.assertEqual({x[2]for x in fake.calls},m.TOPICS);self.assertTrue(all(x[1]=='kafka'for x in fake.calls))
  fake.changed='kafka'
  with self.assertRaises(ValueError):manager.topic_release(fake)
 def test_fixed_sql_hash_and_observer_has_no_mutation_api(self):
  lock=json.loads((ROOT/'deploy/v3/autoscaling/converter-runtime.lock.json').read_text());sql=(ROOT/'deploy/v3/autoscaling/files/converter-drain.sql').read_bytes()
  self.assertEqual(hashlib.sha256(sql).hexdigest(),lock['businessSqlSha256'])
  code=(ROOT/'tests/ci/v3/converter/ConverterLag.java').read_text()
  for forbidden in ['KafkaConsumer<','KafkaProducer<','alterConsumerGroupOffsets','deleteRecords','createTopics','subscribe(']:self.assertNotIn(forbidden,code)
 def test_review_and_no_topic_decrease_rollback(self):
  p=self.plan();self.assertEqual(p['rollback'],dict(partitions=2,converterReplicas=1,disableScaledObject=True));self.assertEqual(m.validate_plan(p,m.digest(p),self.samples,self.p,self.c,self.now),p)
 def test_unknown_or_active_work_is_never_zero(self):
  for key,value in [('waiting',1),('running',1),('unknown',1),('unpublishedActive',1),('running',False)]:
   with self.subTest(key=key,value=value):
    self.setUp();self.samples[-1]['business'][key]=value
    with self.assertRaises(ValueError):self.plan()
 def test_stale_future_unstable_and_new_messages_block(self):
  for change in ('stale','future','unstable','moved','lag','unknown','assignment','isr'):
   with self.subTest(change=change):
    self.setUp();s=self.samples[-1];g=s['kafka']['groups'][0]
    if change=='stale':s['recordedAt']=self.now-91
    elif change=='future':s['recordedAt']=self.now+1
    elif change=='unstable':g['state']='PreparingRebalance'
    elif change=='moved':g['partitions'][0].update(end=11,committed=11)
    elif change=='lag':g['lag']=1
    elif change=='unknown':g['lagKnown']=False
    elif change=='assignment':g['assigned']=0
    elif change=='isr':g['partitions'][0]['isr']=2
    with self.assertRaises(ValueError):self.plan()
 def test_unsafe_proof_and_identity_changes_block(self):
  for change in ('source','image','fixture','drain','topic','pod','ref','partial'):
   with self.subTest(change=change):
    self.setUp()
    if change in ('source','image','fixture'):self.p[{'source':'sourceSha','image':'image','fixture':'fixtureSha256'}[change]]='changed'
    elif change=='drain':self.p['inflightDrain']=False
    elif change=='topic':self.samples[-1]['topics'][0]['uid']='changed'
    elif change=='pod':self.samples[-1]['converterUid']='changed'
    elif change=='ref':self.samples[-1]['approvedRef']='changed'
    elif change=='partial':self.samples[-1]['topics'][0]['partitions']=2
    with self.assertRaises(ValueError):self.plan()
 def test_topic_expansion_preserves_every_other_resource(self):
  old=[dict(kind='KafkaTopic',metadata=dict(namespace='kafka',name=name),spec=dict(partitions=1,replicas=3,config={'retention.bytes':67108864}))for name in sorted(m.TOPICS)]
  new=copy.deepcopy(old)
  for d in new:d['spec']['partitions']=2
  m.review_partition_change(old,new)
  for mutation in ('replicas','retention','extra','decrease','partial'):
   with self.subTest(mutation=mutation):
    bad=copy.deepcopy(new)
    if mutation=='replicas':bad[0]['spec']['replicas']=1
    elif mutation=='retention':bad[0]['spec']['config']['retention.bytes']=0
    elif mutation=='extra':bad.append(dict(kind='Secret',metadata=dict(name='forbidden')))
    elif mutation=='decrease':bad[0]['spec']['partitions']=0
    else:bad[0]['spec']['partitions']=1
    with self.assertRaises(ValueError):m.review_partition_change(old,bad)
 def test_low_or_unknown_disk_capacity_blocks(self):
  self.samples[-1]['brokerFreeBytes']=[0,2*1024**3,2*1024**3]
  with self.assertRaises(ValueError):self.plan()
 def test_quiet_window_and_plan_hash_required(self):
  with self.assertRaises(ValueError):m.prepare(self.samples,self.p,self.c,False,self.now)
  p=self.plan()
  with self.assertRaises(ValueError):m.validate_plan(p,'wrong',self.samples,self.p,self.c,self.now)
 def test_candidate_keeps_images_resources_and_other_fields(self):
  n=m.activation_candidate(self.c,self.plan());n['values']['backend'].pop('autoscaledWorkloads');n['values']['frontend'].pop('autoscaledWorkloads')
  original=copy.deepcopy(self.c)
  for profile in ('backend','frontend'):original['values'][profile].pop('autoscaledWorkloads',None)
  self.assertEqual(n,original)
if __name__=='__main__':unittest.main()
