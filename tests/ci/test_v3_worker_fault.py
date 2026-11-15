import copy,contextlib,importlib.util,io,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import v3_worker_fault as w
import v3_worker_chaos as c
import v3_cdc_fault as f
U='00000000-0000-4000-8000-000000007050'

def fixture():
 s={'version':1,'cluster':f.CLUSTER,'kubeSystemUid':U,'capturedAt':f.now(),'approvedRevision':'a'*40,
    'beSource':w.BE_SOURCE,'beImage':w.BE_IMAGE,'mysql':{'uid':U,'name':'mysql-0'},
    'victim':{'name':'photo-converter-a-b','uid':U,'rsUid':U,'deploymentUid':U,'ownerKind':'ReplicaSet',
              'ownerName':'photo-converter-a','image':w.BE_IMAGE,'containerId':'containerd://abc','ip':'10.40.1.2'},
    'apps':{ns+'/'+name:{'uid':U,'templateHash':'a'*64} for ns,name in w.APPS}}
 p=w.prepare(s,705,'w1-unit',{'photos':20,'totalBytes':40302057,'region':'경기도 성남시','sha256':'a'*64},True)
 rows=[{'section':'db_clock','tripId':705,'utc':f.now()},
       {'section':'execution_timing','tripId':705,'expectedPhotos':20,'status':'PROCESSING','executionId':'e','endedAt':None}]
 for batch in (1,2):
  rows.append({'section':'worker_task','tripId':705,'taskId':'task'+str(batch),'batchId':batch,'kind':'INITIAL','lane':'LIGHT',
   'status':'RUNNING' if batch==1 else 'WAITING','photos':10,'claimHash':'a'*64 if batch==1 else None,
   'attemptOwnerHash':'a'*64 if batch==1 else None,'leaseSeconds':599 if batch==1 else None,
   'leaseUntil':f.now(),'currentItem':3 if batch==1 else None,'currentAttempt':1 if batch==1 else None})
  rows.append({'section':'worker_delivery','tripId':705,'taskId':'task'+str(batch),'deliveryNo':1,'status':'PUBLISHED','partition':0,'offset':5,'traceparent':'00-'+'b'*32+'-'+'c'*16+'-01','topic':'prod.yeodam.image.initial-light'})
 for i in range(1,21):
  rows.append({'section':'worker_item','tripId':705,'batchId':1 if i<=10 else 2,'itemId':i,'attachmentId':i if i<=2 else None,
   'attempt':1 if i<=3 else 0,'saved':1 if i<=2 else 0,'derivedHash':'a'*64 if i<=2 else None,
   'cleanupEntries':0,'accepted':1,'route':'KAFKA'})
 proof={'rows':rows,'approvedRevision':'a'*40,'observedAt':f.now(),'queryElapsedSeconds':.2,'attempt':1}
 owner={'observedAt':f.now(),'group':'prod.yeodam.image-conversion-initial-light','state':'Stable',
        'members':[{'host':'/10.40.1.2','partitions':[0,1]}],'committed':{'0':5,'1':0}}
 return p,proof,owner


def trace_fixture():
 return {'traceEvidenceReady':True,'telemetryIntegrity':'no_observed_loss','trace':{'traceId':'b'*32,'tracePathComplete':True}}


def controller():
 return {'metadata':{'uid':U,'resourceVersion':'1','generation':1},'spec':{'replicas':1,'template':{'spec':{
  'serviceAccountName':c.CONTROLLER,'containers':[{'image':json.loads((f.CHART/'upstream.lock.json').read_text())['images']['chaos-mesh'],
  'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'300m','memory':'256Mi'}},
  'env':[{'name':k,'value':v} for k,v in {'TARGET_NAMESPACE':'kafka','ENABLED_CONTROLLERS':'networkchaos,podnetworkchaos','ENABLED_WEBHOOKS':'networkchaos','CLUSTER_SCOPED':'false'}.items()]}]}}},
  'status':{'observedGeneration':1,'readyReplicas':1}}


class Fake:
 def __init__(self):
  self.d=controller();self.objs={};self.calls=[]
  for kind,prefix in [('mutatingwebhookconfiguration','m'),('validatingwebhookconfiguration','v')]:
   self.objs[(kind,'v3-chaos-'+prefix)]={'metadata':{'name':'v3-chaos-'+prefix,'uid':U,'resourceVersion':'1'},'webhooks':[{
    'name':prefix+'networkchaos.kb.io','clientConfig':{'service':{'namespace':'chaos-mesh','name':'v3-chaos-controller-manager','path':'/old'},'caBundle':'cHVibGljLWNh'},
    'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kafka'}},'rules':[{'apiGroups':['chaos-mesh.org'],'apiVersions':['v1alpha1'],'operations':['CREATE','UPDATE'],'resources':['networkchaos']}],
    'failurePolicy':'Fail','admissionReviewVersions':['v1'],'sideEffects':'None'}]}
 def identity(self):return U
 def get(self,kind,namespace=None,name=None,**kw):
  kind=kind.lower()
  if kind=='deployment':return copy.deepcopy(self.d)
  if kind in ('networkchaos','podchaos'):return {'items':[]}
  if name:return copy.deepcopy(self.objs[(kind,name)])
  return {'items':[copy.deepcopy(v)for (k,n),v in self.objs.items()if k==kind]}
 def call(self,args,source=None,timeout=20):
  self.calls.append((args,source))
  if 'wait'in args:return ''
  if 'create'in args:
   o=json.loads(source);o['metadata']['uid']=U;self.objs[(o['kind'].lower(),o['metadata']['name'])]=copy.deepcopy(o);return json.dumps(o)
  if 'get'in args:
   pos=args.index('get');kind=args[pos+1].lower();name=args[pos+2];return json.dumps(self.objs.get((kind,name),{}))
  if 'patch'in args:
   pos=args.index('patch');kind=args[pos+1];name=args[pos+2];body=json.loads(args[-1]);obj=self.d if kind=='deployment'else self.objs[(kind,name)]
   for op in body:
    if op['op']=='replace':obj[op['path'].strip('/')]=op['value']
   return json.dumps(obj)
  if 'delete'in args:
   path=args[args.index('--raw')+1];plural,name=path.split('/')[-2:];kind={'roles':'role','rolebindings':'rolebinding','podchaos':'podchaos'}[plural];self.objs.pop((kind,name),None);return ''
  raise AssertionError(args)


class Worker(unittest.TestCase):
 def test_plan_is_inactive_exact_one_pod_and_hash_bound(self):
  p,_,_=fixture();w.verify(p,p['planSha256']);self.assertEqual(p['fault']['spec']['selector'],{'namespaces':['photo'],'pods':{'photo':['photo-converter-a-b']}})
  self.assertEqual(p['fault']['metadata']['annotations'][f.PAUSE],'true')
  self.assertEqual(p['criteria']['leaseSeconds'],600)
  for key,value in [('mode','all'),('gracePeriod',180),('action','container-kill')]:
   bad=copy.deepcopy(p);bad['fault']['spec'][key]=value;bad['planSha256']=f.sha({k:v for k,v in bad.items()if k!='planSha256'})
   with self.assertRaises(ValueError):w.verify(bad,bad['planSha256'])
 def test_changed_runtime_source_and_stale_snapshot_block(self):
  p,_,_=fixture()
  for key,value in [('beSource','b'*40),('capturedAt','2000-01-01T00:00:00+00:00')]:
   s=copy.deepcopy(p['snapshot']);s[key]=value
   with self.assertRaises(ValueError):w.validate_snapshot(s)
  with self.assertRaises(ValueError):w.prepare(p['snapshot'],705,'w1-unit',p['input'],False)
 def test_partial_requires_persisted_same_task_and_current_attempt(self):
  p,b,o=fixture();s=w.partial(b,p,o);self.assertEqual(s['currentItem'],3);self.assertEqual(len(s['savedItems']),2)
  for change in ('saved','complete','token','attempt','accepted','late','retry','slow','owner','ack','unstable','duplicate-owner'):
   pp,bb,oo=fixture();task=next(r for r in bb['rows']if r['section']=='worker_task')
   if change=='saved':next(r for r in bb['rows']if r.get('itemId')==2).update(saved=0,derivedHash=None)
   elif change=='complete':next(r for r in bb['rows']if r['section']=='execution_timing')['status']='COMPLETED'
   elif change=='token':task['attemptOwnerHash']='b'*64
   elif change=='attempt':task['currentAttempt']=2
   elif change=='accepted':next(r for r in bb['rows']if r.get('itemId')==20)['accepted']=0
   elif change=='late':bb['observedAt']='2000-01-01T00:00:00Z'
   elif change=='retry':bb['attempt']=2
   elif change=='slow':bb['queryElapsedSeconds']=3
   elif change=='owner':oo['members'][0]['host']='/10.40.1.3'
   elif change=='ack':oo['committed']['0']=6
   elif change=='unstable':oo['state']='PreparingRebalance'
   else:oo['members']*=2
   with self.subTest(change=change),self.assertRaises(ValueError):w.partial(bb,pp,oo)
 def test_extra_rows_unknown_route_and_raw_claim_never_exported(self):
  _,b,_=fixture();b['rows'].append(copy.deepcopy(next(r for r in b['rows']if r['section']=='worker_item')))
  with self.assertRaises(ValueError):w.validate_rows(b['rows'],705,True)
  text=w.sql(705)
  self.assertIn('READ ONLY',text);self.assertIn('SHA2(claim_token,256)',text)
  for forbidden in ['original_file_name','private_payload','object_key','SELECT *','UPDATE ','DELETE FROM','INSERT INTO']:self.assertNotIn(forbidden,text)
  for bad in [True,-1,'705;DELETE',0]:
   with self.assertRaises(ValueError):w.sql(bad)
 def test_zero_grace_omitted_by_go_serialization_is_equivalent(self):
  p,_,_=fixture();server=copy.deepcopy(p['fault']['spec']);server.pop('gracePeriod')
  self.assertEqual(w.normalized_fault(server),p['fault']['spec'])
  server['gracePeriod']=180;self.assertNotEqual(w.normalized_fault(server),p['fault']['spec'])
 def test_named_delete_role_never_deletes_other_pods_or_secrets(self):
  p,_,_=fixture();r=w.delete_role(p)[0]['rules'];self.assertEqual(r,[{'apiGroups':[''],'resources':['pods'],'resourceNames':['photo-converter-a-b'],'verbs':['delete']}])
  self.assertFalse(any('delete'in x['verbs']for x in c.access()[0]['rules']))
 def test_unknown_cleanup_uid_cannot_delete_replacement(self):
  class K:
   def call(self,args,source=None):
    if 'get'in args:return json.dumps({'metadata':{'uid':'different','labels':{w.LABEL:'w1-unit'}}})
    raise AssertionError('Mutation before ownership verified')
  with self.assertRaises(ValueError):w.owned_delete(K(),'role','name','photo',U,'w1-unit')
 def test_profile_is_existing_controller_only_and_roundtrip_restores_tls(self):
  k=Fake();old=copy.deepcopy(k.objs);d=copy.deepcopy(k.d);p=c.profile_plan(k);c.verify(p,p['planSha256'])
  self.assertEqual(p['afterSpec']['template']['spec']['containers'][0]['resources'],d['spec']['template']['spec']['containers'][0]['resources'])
  with tempfile.TemporaryDirectory()as tmp:
   os.chmod(tmp,0o700);receipt=c.apply(k,p,p['planSha256'],Path(tmp)/'receipt.json');c.require_active(k,receipt);c.restore(k,receipt)
  self.assertEqual(k.d,d)
  for key,val in old.items():self.assertEqual(k.objs[key],val)
  self.assertFalse(any(key[0]in ('role','rolebinding')for key in k.objs))
  self.assertTrue(any(key[0]=='customresourcedefinition'for key in k.objs)) # global CRD retained
  self.assertFalse(any('secret'in str(args).lower()for args,_ in k.calls))
 def test_profile_tamper_and_foreign_change_never_overwritten(self):
  k=Fake();p=c.profile_plan(k);p['webhooks'][0]['after'][-1]['clientConfig']['service']['namespace']='auth';p['planSha256']=f.sha({k:v for k,v in p.items()if k!='planSha256'})
  with self.assertRaises(ValueError):c.verify(p,p['planSha256'])
  k=Fake();p=c.profile_plan(k);k.d['spec']['replicas']=2
  with self.assertRaises(ValueError):c.apply(k,p,p['planSha256'],Path('/tmp/not-written-705.json'))
 def test_restoration_refuses_unowned_equal_name_role(self):
  k=Fake();p=c.profile_plan(k);k.objs[('role',c.ROLE)]=dict(copy.deepcopy(c.access()[0]),metadata={'uid':U,'labels':c.access()[0]['metadata']['labels']})
  with self.assertRaises(ValueError):c.restore(k,{'plan':p,'resources':[]})
 def test_cli_no_live_access_without_explicit_action_arguments(self):
  spec=importlib.util.spec_from_file_location('worker_cli',ROOT/'scripts/prepare-v3-worker-fault.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  for action in ['run','profile-apply','collect']:
   with contextlib.redirect_stderr(io.StringIO()),self.subTest(action=action),patch.object(sys,'argv',['worker',action]),patch.object(f,'Kube',side_effect=AssertionError('No access before arguments validated')),self.assertRaises(SystemExit):m.main()
 def test_assessment_requires_reprocessed_attempt_and_preserved_saved_items(self):
  p,b,_=fixture();a=copy.deepcopy(b)
  next(r for r in a['rows']if r['section']=='execution_timing')['status']='COMPLETED'
  for r in a['rows']:
   if r['section']=='worker_item':r.update(saved=1,attachmentId=r['itemId'],derivedHash='a'*64,attempt=2 if r['itemId']==3 else 1)
  a['rows'] +=[dict(section='final_attachment',tripId=705,attachmentId=i,fileId=i)for i in range(1,21)]
  summary={'approvedRevision':'a'*40,'tripIds':[705],'observationGaps':[],'continuousObservationComplete':True}
  timing={'derivedFromRecordedSamples':True,'podKillObserved':True,'prematureClaimObserved':False,'eligibilityToClaimBounds':[3,5],'claimToCompletionBounds':[175,178],'eligibilityAt':'2026-11-15T06:00:00Z','newClaimAt':'2026-11-15T06:00:04Z','completedAt':'2026-11-15T06:03:00Z','terminationLowerAt':'2026-11-15T05:50:00Z','terminationUpperAt':'2026-11-15T05:50:03Z','manualActions':[]}
  report=w.assess(b,a,timing,trace_fixture(),summary);self.assertTrue(report['automaticRecovery']);self.assertFalse(report['exactlyOnceProven'])
  for change in ['gap','reuse','missing','attempt','trace','manual','time','lease']:
   aa=copy.deepcopy(a);ss=copy.deepcopy(summary);tt=copy.deepcopy(timing);trace=trace_fixture()
   if change=='gap':ss['observationGaps']=['preserved-gap']
   elif change=='reuse':next(r for r in aa['rows']if r.get('itemId')==1)['derivedHash']='b'*64
   elif change=='missing':aa['rows']=[r for r in aa['rows']if not(r['section']=='final_attachment'and r['attachmentId']==20)]
   elif change=='attempt':next(r for r in aa['rows']if r.get('itemId')==3)['attempt']=1
   elif change=='trace':trace['traceEvidenceReady']=False
   elif change=='manual':tt['manualActions']=['restart']
   elif change=='lease':tt['prematureClaimObserved']=True
   else:tt['newClaimAt']=None
   with self.subTest(change=change):self.assertIsNone(w.assess(b,aa,tt,trace,ss)['automaticRecovery'])
 def test_phase_deadline_clamps_api_calls_and_exhaustion_never_calls_api(self):
  class K:
   def call(self,args,source=None,timeout=20):self.timeout=timeout;return '{}'
  k=K();b=w.DeadlineKube(k);b.deadline=12
  with patch.object(w.time,'monotonic',return_value=10):b.call(['get','pod']);self.assertEqual(k.timeout,2)
  with patch.object(w.time,'monotonic',return_value=13),self.assertRaises(subprocess.TimeoutExpired):b.call(['get','pod'])
 def test_run_cleanup_on_success_interrupt_and_unacknowledged_create(self):
  class K(Fake):
   def __init__(self,fail):super().__init__();self.fail=fail;self.injected=False
   def get(self,kind,namespace=None,name=None,**kw):
    if kind=='pod':return {'metadata':{'uid':U}}
    if kind=='pods':return {'items':[]}
    if kind=='podchaos'and name:return copy.deepcopy(self.objs[(kind,name)])
    return super().get(kind,namespace,name,**kw)
   def call(self,args,source=None,timeout=20):
    if 'create'in args and json.loads(source)['kind']=='PodChaos'and self.fail=='create':raise subprocess.TimeoutExpired(['create'],1)
    if 'patch'in args and 'podchaos'in args:
     self.injected=True
     if self.fail=='interrupt':raise KeyboardInterrupt()
     return '{}'
    if 'get'in args and 'podchaos'in args:return json.dumps(self.objs.get(('podchaos','w1-unit'),{}))
    if 'get'in args and 'pods'in args:return '{"items":[]}'
    if 'get'in args and 'pod'in args:return '{}'
    return super().call(args,source,timeout)
  class Observer:
   gaps=[];exhausted=False
   def __init__(self,*args):self.count=0
   def sample(self):
    self.count+=1;_,b,_=fixture()
    if self.count>=3:next(r for r in b['rows']if r['section']=='execution_timing')['status']='COMPLETED'
    return b
   def summary(self,state):return {'state':state}
  for failure in ('none','interrupt','create'):
   p,_,o=fixture();k=K(failure);calls=[]
   with tempfile.TemporaryDirectory()as tmp:
    os.chmod(tmp,0o700)
    with patch.object(w,'runtime'),patch.object(c,'require_active'),patch.object(c,'restore',side_effect=lambda *a:calls.append('restore')or {'restored':True}),patch.object(w,'WorkerDbObserver',Observer),patch.object(f,'ready_pod',return_value=True):
     def run():return w.execute(k,p,p['planSha256'],tmp,lambda **kw:o,{},lambda:calls.append('gpu-stop-signal'),lambda:calls.append('reader-cleanup')or True)
     if failure=='none':self.assertTrue(run()['injectionRequested'])
     else:
      with self.assertRaises((KeyboardInterrupt,subprocess.TimeoutExpired)):run()
     receipt=json.loads((Path(tmp)/'receipt.json').read_text())
     self.assertEqual(calls,['gpu-stop-signal','reader-cleanup','restore'])
     self.assertEqual(receipt['injectionRequested'],failure!='create')
     self.assertEqual(any(key[0]in ('role','rolebinding')for key in k.objs),failure=='create')
     if failure=='create':self.assertEqual(receipt['cleanupErrors'][0]['reason'],'create_response_unknown_manual_uid_review_required')
 def test_recorded_timing_bounds_and_native_grafana_output(self):
  p,b,o=fixture();selected=w.partial(b,p,o)
  with tempfile.TemporaryDirectory()as tmp:
   q=Path(tmp);os.chmod(q,0o700);(q/'db').mkdir(mode=0o700)
   def save(name,v):f.private_write(q/name,v)
   save('pre-injection.json',selected);save('injection-intent.json',{'victimUid':U})
   save('receipt.json',{'injectionRequested':True,'requestAt':'2026-11-15T12:00:00Z','terminationConfirmed':True,'terminationObservedAt':'2026-11-15T12:00:03Z'})
   rows=[]
   for i,(at,claim)in enumerate([('12:00:03','a'),('12:00:11','a'),('12:00:13','d'),('12:01:30','d')]):
    sample=copy.deepcopy(b);sample.update(observedAt='2026-11-15T'+at+'Z',dbMinusLocalIntervalSeconds=[0,0])
    next(x for x in sample['rows']if x['section']=='db_clock')['utc']=sample['observedAt']
    task=next(x for x in sample['rows']if x['section']=='worker_task');task.update(claimHash=claim*64,leaseUntil='2026-11-15T12:00:10Z')
    if i==3:next(x for x in sample['rows']if x['section']=='execution_timing').update(status='COMPLETED',endedAt=sample['observedAt'])
    save('db/sample-'+str(i)+'.json',sample);rows.append('sample-'+str(i)+'.json')
   save('db-summary.json',{'sampleFiles':rows,'tripIds':[705],'observationGaps':[]})
   oo=copy.deepcopy(o);oo.update(observedAt='2026-11-15T12:00:05Z');oo['members'][0]['host']='/10.40.1.3'
   save('runtime-0001.json',{'at':oo['observedAt'],'pods':[{'uid':'new','ip':'10.40.1.3','ready':True}],'ownership':oo})
   record=w.summarize(q);self.assertEqual(record['eligibilityToClaimBounds'],[0,3]);self.assertEqual(record['claimToCompletionBounds'],[77,79])
   self.assertTrue(w.bounded_target(record['eligibilityToClaimBounds'],15));self.assertIsNone(w.bounded_target([14,16],15))
   board=w.dashboard(record);self.assertEqual(board['panels'][0]['type'],'timeseries');self.assertTrue(board['panels'][0]['snapshotData'])
   self.assertIn('db/sample-0.json',record['sourceHashes'])
 def test_reader_bundle_rejects_mutation_before_any_live_read(self):
  spec=importlib.util.spec_from_file_location('reader_cli',ROOT/'scripts/prepare-v3-worker-fault.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  bundle=m.reader_manifest('ip-10-40-1-2','w1-reader','AA==')
  class NoAccess:
   def get(self,*a,**kw):raise AssertionError('No live query for noncanonical bundle')
  bundle['items'][-1]['spec']['containers'][0]['command']=['sh','-c','unsafe']
  with self.assertRaises(ValueError):m.reader_start(NoAccess(),bundle,f.sha(bundle),Path('/tmp/never-written'))
  with tempfile.TemporaryDirectory()as tmp:
   q=Path(tmp);os.chmod(q,0o700);g=q/'guard.json';plan,_,_=fixture()
   f.private_write(g,{'planSha256':plan['planSha256'],'instanceId':'i-05e1ad9dd96dba4ae','independent':True,'pid':os.getpid(),'at':f.now(),'remainingSeconds':20,'stopFile':str(q/'stop')})
   with self.assertRaises(ValueError):m.guard_ready(g,plan)
 @unittest.skipUnless(os.environ.get('V3_KUBECONFORM'),'Pinned schema binary required')
 def test_actual_official_podchaos_and_rbac_schemas(self):
  p,_,_=fixture();d=c.crd();schema=next(v['schema']['openAPIV3Schema'] for v in d['spec']['versions']if v['name']=='v1alpha1')
  with tempfile.TemporaryDirectory()as tmp:
   q=Path(tmp);(q/'schema.json').write_text(json.dumps(schema));(q/'fault.json').write_text(json.dumps(p['fault']))
   subprocess.run([os.environ['V3_KUBECONFORM'],'-strict','-schema-location',str(q/'schema.json'),str(q/'fault.json')],check=True)
   import yaml
   (q/'rbac.yaml').write_text(yaml.safe_dump_all(c.access()+w.delete_role(p)))
   subprocess.run([os.environ['V3_KUBECONFORM'],'-strict','-kubernetes-version','1.36.0','-schema-location',str(Path(os.environ['V3_SCHEMA_DIR'])/'{{.ResourceKind}}.json'),str(q/'rbac.yaml')],check=True)

if __name__=='__main__':unittest.main()
