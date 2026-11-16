import copy,importlib.util,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import v3_worker_fault as w
import v3_worker_container as h
import v3_worker_chaos as c
import v3_cdc_fault as f
import test_v3_worker_fault as old
U=old.U; WORKER='ip-10-40-41-221.ap-northeast-2.compute.internal';SYSTEM='ip-10-40-21-11.ap-northeast-2.compute.internal'
NU='00000000-0000-4000-8000-000000007100'


def daemon_fixture():
 return {'apiVersion':'apps/v1','kind':'DaemonSet','metadata':{'name':h.DAEMON,'namespace':'chaos-mesh','uid':U,'resourceVersion':'1','generation':1},
  'spec':{'selector':{'matchLabels':{'app.kubernetes.io/component':'chaos-daemon'}},'template':{'metadata':{'labels':{'app.kubernetes.io/component':'chaos-daemon'}},'spec':{
   'nodeSelector':{'yeodam.io/role':'system','kubernetes.io/hostname':SYSTEM},'hostPID':True,'hostNetwork':False,
   'containers':[{'name':'chaos-daemon','image':json.loads((f.CHART/'upstream.lock.json').read_text())['images']['chaos-daemon'],
    'command':['/usr/local/bin/chaos-daemon','--runtime','containerd','--runtime-socket-path','/host-run/containerd.sock','--cert','/etc/chaos-daemon/cert/tls.crt','--ca','/etc/chaos-daemon/cert/ca.crt'],
    'resources':copy.deepcopy(h.BUDGET),'securityContext':{'privileged':True}}],
   'volumes':[{'name':'socket-path','hostPath':{'path':'/run/containerd'}},{'name':'cert','secret':{'secretName':'existing-daemon-tls'}}]}}},
  'status':{'observedGeneration':1,'desiredNumberScheduled':1,'numberReady':1}}


def pod(node=WORKER,uid=U,cid='containerd://abc'):
 return {'metadata':{'name':'photo-converter-a-b','uid':uid},'spec':{'nodeName':node,'hostNetwork':False},
         'status':{'phase':'Running','podIP':'10.40.1.2','conditions':[{'type':'Ready','status':'True'}],
                   'containerStatuses':[{'name':'backend','containerID':cid,'ready':True,'state':{'running':{}}}]}}


class K(old.Fake):
 def __init__(self):super().__init__();self.ds=daemon_fixture();self.nodeuid=NU;self.full=False;self.injected=False
 def get(self,kind,namespace=None,name=None,**kw):
  if kind=='daemonset':return copy.deepcopy(self.ds)
  if kind=='node':return {'metadata':{'name':name,'uid':self.nodeuid if name==WORKER else U,'labels':{'yeodam.io/role':'worker'if name==WORKER else'system','node.kubernetes.io/instance-type':'t3a.medium'}},'status':{'allocatable':{'pods':'1'if self.full else'17'},'nodeInfo':{'operatingSystem':'linux','containerRuntimeVersion':'containerd://2.3.7'},'conditions':[{'type':'Ready','status':'True'}]}}
  if kind=='pods'and namespace=='photo':return {'items':[self.victim()]}
  if kind=='pods'and namespace=='chaos-mesh':
   host=self.ds['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'];v=pod(host,NU);v['metadata']['ownerReferences']=[{'kind':'DaemonSet','uid':U,'controller':True}];return {'items':[v]}
  if kind=='endpointslices':return {'items':[{'metadata':{'name':'chaos-daemon-abc'},'endpoints':[{'nodeName':WORKER,'addresses':['10.40.1.2'],'conditions':{'ready':True}}]}]}
  if kind=='pod':return self.victim()
  if kind=='podchaos'and name:return copy.deepcopy(self.objs[(kind,name)])
  return super().get(kind,namespace,name,**kw)
 def victim(self):
  v=pod()
  if self.injected:
   s=v['status']['containerStatuses'][0];s['containerID']='containerd://new';s['restartCount']=1;s['lastState']={'terminated':{'containerID':'containerd://abc','exitCode':137,'reason':'Error','finishedAt':f.now()}}
  return v
 def call(self,args,source=None,timeout=20):
  if 'get'in args and '-A'in args:return json.dumps({'items':[pod()]})
  if 'patch'in args and 'daemonset'in args:
   self.calls.append((args,source));ops=json.loads(args[-1]);assert ops[0]['value']==self.ds['metadata']['uid'];assert ops[2]['value']==self.ds['spec'];self.ds['spec']=ops[-1]['value'];return '{}'
  if 'create'in args and json.loads(source)['kind']=='PodChaos':
   self.injected=True;out=json.loads(super().call(args,source,timeout));out['status']={'experiment':{'containerRecords':[{'id':'photo/photo-converter-a-b/backend','injectedCount':1}]}};self.objs[('podchaos',out['metadata']['name'])]=out;return json.dumps(out)
  if 'get'in args and 'pods'in args:return json.dumps({'items':[self.victim()]})
  if 'get'in args and 'pod'in args:return json.dumps(self.victim())
  return super().call(args,source,timeout)


def fixture():
 p,b,o=old.fixture();p['snapshot']['victim']['node']=WORKER;k=K()
 with patch.object(w,'runtime'):dp=h.daemon_plan(k,p['snapshot'])
 p=w.prepare(p['snapshot'],705,'w1-hard-unit',p['input'],True,dp);o['members'][0]['memberHash']='a'*64
 return p,b,o,k


class ContainerFault(unittest.TestCase):
 def test_explicit_hard_plan_and_tamper_block(self):
  p,_,_,_=fixture();w.verify(p,p['planSha256']);self.assertEqual(p['case'],'W1K');self.assertEqual(p['fault']['spec']['action'],'container-kill');self.assertEqual(p['fault']['spec']['containerNames'],['backend']);self.assertNotIn('gracePeriod',p['fault']['spec'])
  for key,v in [('action','pod-kill'),('containerNames',['sidecar']),('mode','all')]:
   bad=copy.deepcopy(p);bad['fault']['spec'][key]=v;bad['planSha256']=f.sha({k:v for k,v in bad.items()if k!='planSha256'})
   with self.subTest(key=key),self.assertRaises(f.GuardError):w.verify(bad,bad['planSha256'])
 def test_admitted_container_fault_only_allows_irrelevant_zero_grace_default(self):
  p,_,_,_=fixture();spec=p['fault']['spec'];self.assertEqual(w.normalized_fault(dict(spec,gracePeriod=0)),spec)
  self.assertNotEqual(w.normalized_fault(dict(spec,containerNames=['sidecar'])),spec)
  self.assertNotEqual(w.normalized_fault(dict(spec,gracePeriod=180)),spec)
 def test_two_to_four_saved_only_and_member_identity(self):
  p,b,o,_=fixture();self.assertEqual(w.partial(b,p,o)['memberHash'],'a'*64)
  for i in range(3,6):next(x for x in b['rows']if x.get('itemId')==i).update(saved=1,derivedHash='a'*64)
  with self.assertRaisesRegex(f.GuardError,'No unique'):w.partial(b,p,o)
  p,b,o,_=fixture();o['members'][0].pop('memberHash')
  with self.assertRaisesRegex(f.GuardError,'member hash'):w.partial(b,p,o)
 def test_exact_old_container137_not_api_disappearance_oom_or_other_pod(self):
  p,_,_,k=fixture();k.injected=True;v=k.victim();self.assertEqual(h.termination(p,v)['exitCode'],137)
  for bad in [{},dict(v,metadata={'uid':NU}),dict(v,status={'containerStatuses':[{'name':'backend','lastState':{'terminated':{'containerID':'containerd://abc','exitCode':137,'reason':'OOMKilled','finishedAt':f.now()}}}]}),dict(v,status={'containerStatuses':[{'name':'backend','lastState':{'terminated':{'containerID':'containerd://different','exitCode':137,'finishedAt':f.now()}}}]})]:
   with self.subTest(bad=bad):self.assertIsNone(h.termination(p,bad))
 def test_same_pod_and_ip_need_new_container_and_new_kafka_member(self):
  p,b,o,k=fixture();sel=w.partial(b,p,o);v={'uid':U,'ip':'10.40.1.2','containerStatuses':k.victim()['status']['containerStatuses']};self.assertFalse(h.replacement(p,v));k.injected=True;v['containerStatuses']=k.victim()['status']['containerStatuses'];self.assertTrue(h.replacement(p,v));self.assertFalse(h.new_owner(sel,[v],o));o['members'][0]['memberHash']='b'*64;self.assertTrue(h.new_owner(sel,[v],o))
 def test_post_death_unfinished_photo_and_live_old_lease_are_independent(self):
  p,b,o,_=fixture();s=w.partial(b,p,o);self.assertTrue(h.post_kill_window(s,b))
  for mode in ['saved','claim','released','expired']:
   bb=copy.deepcopy(b);task=next(x for x in bb['rows']if x.get('taskId')=='task1'and x['section']=='worker_task')
   if mode=='saved':next(x for x in bb['rows']if x.get('itemId')==3)['saved']=1
   if mode=='claim':task['claimHash']='b'*64
   if mode=='released':task.update(status='WAITING',claimHash=None)
   if mode=='expired':task['leaseSeconds']=0
   with self.subTest(mode=mode):self.assertFalse(h.post_kill_window(s,bb))
 def test_daemon_retarget_roundtrip_preserves_identity_tls_budget_and_authority(self):
  p,_,_,k=fixture();before=copy.deepcopy(k.ds);dp=p['daemonPlan']
  with tempfile.TemporaryDirectory()as d:
   os.chmod(d,0o700);r=h.apply_daemon(k,dp,dp['planSha256'],Path(d)/'receipt.json');h.require_daemon(k,r,p);h.restore_daemon(k,r)
  self.assertEqual(k.ds,before);self.assertFalse(any('get'in args and any(x.lower()in('secret','secrets')for x in args)for args,_ in k.calls));self.assertFalse(any('delete'in args or 'create'in args for args,_ in k.calls));self.assertEqual(dp['desiredDaemonPods'],1)
 def test_full_or_changed_node_or_foreign_daemon_edit_blocks(self):
  p,_,_,k=fixture();k.full=True
  with self.assertRaises(f.GuardError):h.target_node(k,p['snapshot']['victim'])
  k.full=False;k.nodeuid=U
  with tempfile.TemporaryDirectory()as d,self.assertRaises(f.GuardError):h.apply_daemon(k,p['daemonPlan'],p['daemonPlan']['planSha256'],Path(d)/'r.json')
  k=K();k.ds['spec']['template']['spec']['containers'][0]['resources']['limits']['memory']='256Mi'
  with self.assertRaises(f.GuardError):h.daemon(k)
 def test_daemon_restore_does_not_overwrite_foreign_spec(self):
  p,_,_,k=fixture();k.ds['spec']['template']['spec']['containers'][0]['env']=[{'name':'FOREIGN','value':'edit'}]
  with self.assertRaises(f.GuardError):h.restore_daemon(k,{'plan':p['daemonPlan']})
 def test_missing_endpoint_blocks_even_if_daemon_ready(self):
  p,_,_,k=fixture()
  with tempfile.TemporaryDirectory()as d:
   os.chmod(d,0o700);r=h.apply_daemon(k,p['daemonPlan'],p['daemonPlan']['planSha256'],Path(d)/'r.json');get=k.get
   k.get=lambda kind,*a,**kw:{'items':[]}if kind=='endpointslices'else get(kind,*a,**kw)
   with self.assertRaisesRegex(f.GuardError,'EndpointSlice'):h.require_daemon(k,r,p)
   h.restore_daemon(k,r)
 def test_daemon_timeout_after_accepted_patch_uses_journal_to_restore(self):
  p,_,_,k=fixture();dp=p['daemonPlan'];before=copy.deepcopy(k.ds);call=k.call;first=[True]
  def ambiguous(args,source=None,timeout=20):
   v=call(args,source,timeout)
   if 'patch'in args and first[0]:first[0]=False;raise subprocess.TimeoutExpired(['patch'],1)
   return v
  k.call=ambiguous
  with tempfile.TemporaryDirectory()as d:
   os.chmod(d,0o700);path=Path(d)/'r.json'
   with self.assertRaises(subprocess.TimeoutExpired):h.apply_daemon(k,dp,dp['planSha256'],path)
   self.assertTrue(json.loads(path.read_text())['restore']['restored']);self.assertEqual(k.ds,before)
 def test_execute_captures_same_pod_exit_and_reads_post_death_before_success(self):
  p,b,o,k=fixture();calls=[];saves=[]
  class Observer:
   gaps=[];exhausted=False
   def __init__(self,*a):self.n=0
   def sample(self):
    self.n+=1;x=copy.deepcopy(b);x['observedAt']=x['queryStartedAt']=f.now()
    if self.n>=4:next(v for v in x['rows']if v['section']=='execution_timing')['status']='COMPLETED'
    return x
   def summary(self,state):return {'state':state}
  with tempfile.TemporaryDirectory()as d:
   os.chmod(d,0o700)
   with patch.object(w,'runtime'),patch.object(c,'require_active'),patch.object(h,'require_daemon'),patch.object(h,'restore_daemon',side_effect=lambda *a:calls.append('daemon')),patch.object(c,'restore',side_effect=lambda *a:calls.append('profile')),patch.object(w,'WorkerDbObserver',Observer):
    def owner(**kw):
     v=copy.deepcopy(o);v['observedAt']=f.now();return old.clock_owner(v)
    receipt=w.execute(k,p,p['planSha256'],d,owner,{},lambda:calls.append('gpu'),lambda:calls.append('reader'),{})
   self.assertTrue(receipt['processKillObserved']);self.assertTrue(receipt['postKillWindowObserved']);self.assertTrue(receipt['chaosInjectedObserved']);self.assertEqual(receipt['observationState'],'completed');self.assertEqual(calls,['gpu','reader','daemon','profile']);self.assertTrue((Path(d)/'input-ready.json').exists());self.assertTrue((Path(d)/'post-termination.json').exists());self.assertEqual(next(x for x in json.loads((Path(d)/'final.json').read_text())['rows']if x['section']=='execution_timing')['status'],'COMPLETED');self.assertFalse(any(json.loads(body)['kind']=='Role'for args,body in k.calls if 'create'in args))
 def test_lease_timing_keeps_expiry_interval_and_marks_release_path(self):
  p,b,o,k=fixture();sel=w.partial(b,p,o);start=w.epoch('2026-11-16T00:00:00Z')
  def sample(t,claim='a'*64):
   x=copy.deepcopy(b);x['queryStartedAt']=w.iso(start+t);x['observedAt']=w.iso(start+t+.1);x['dbMinusLocalIntervalSeconds']=[0,.1];next(v for v in x['rows']if v['section']=='db_clock')['utc']=w.iso(start+t);task=next(v for v in x['rows']if v['section']=='worker_task');task.update(claimHash=claim,leaseUntil=w.iso(start+600),leaseSeconds=600-t);return x
  rec={'requestAt':w.iso(start),'case':'W1K','victimContainerId':'containerd://abc','chaosInjectedObserved':True,'postKillWindowObserved':True};rt=[{'at':w.iso(start+10),'processTermination':{'exitCode':137,'killedContainerId':'containerd://abc'}},{'at':w.iso(start+80),'processTermination':None}];result={'events':{'consumerAssignedAt':w.iso(start+80)},'completedAtBounds':[w.iso(start+779),w.iso(start+780)]}
  for release in (False,True):
   ss=[sample(20),sample(590,None)if release else sample(598),sample(602,'b'*64)];r=h.lease_timing(copy.deepcopy(result),rec,sel,ss,rt)
   self.assertTrue(r['processKillObserved']);self.assertTrue(r['postKillWindowObserved']);self.assertEqual(r['leaseReleaseObserved'],release);self.assertLessEqual(r['eligibilityToClaimBounds'][1],15);self.assertFalse(r['prematureClaimObserved']);self.assertTrue(r['claimIntervalOverlapsExpiry'])
 def test_strict_assessment_requires_the_selected_photo_not_just_another_retry(self):
  p,b,o,k=fixture();a=copy.deepcopy(b)
  next(x for x in a['rows']if x['section']=='execution_timing')['status']='COMPLETED'
  for x in a['rows']:
   if x['section']=='worker_item':x.update(saved=1,attachmentId=x['itemId'],derivedHash='a'*64,attempt=max(1,x['attempt']))
  next(x for x in a['rows']if x.get('itemId')==3)['attempt']=2
  a['rows'] += [{'section':'final_attachment','tripId':705,'attachmentId':i,'fileId':i}for i in range(1,21)]
  t={'case':'W1K','eligibilityAt':'2026-11-16T00:10:00Z','newClaimAt':'2026-11-16T00:10:03Z','completedAt':'2026-11-16T00:12:00Z','terminationLowerAt':'2026-11-16T00:00:00Z','terminationUpperAt':'2026-11-16T00:00:01Z','derivedFromRecordedSamples':True,'podKillObserved':True,'prematureClaimObserved':False,'processKillObserved':True,'postKillWindowObserved':True,'leaseReleaseObserved':False,'selectedItemId':3,'selectedAttempt':1,'selectedProofSha256':f.sha(b)}
  db={'approvedRevision':b['approvedRevision'],'tripIds':[705],'observationGaps':[],'continuousObservationComplete':True}
  self.assertTrue(w.assess(b,a,t,old.trace_fixture(),db)['evidenceComplete'])
  for key,value in [('selectedItemId',4),('selectedProofSha256','0'*64),('postKillWindowObserved',False),('leaseReleaseObserved',True)]:
   bad=dict(t);bad[key]=value
   with self.subTest(key=key):self.assertFalse(w.assess(b,a,bad,old.trace_fixture(),db)['evidenceComplete'])

 def test_hard_dashboard_keeps_case_and_native_recorded_bounds(self):
  record={'case':'W1K','events':{'terminationUpperAt':'2026-11-16T00:00:01Z','completedAt':'2026-11-16T00:12:00Z'},'samples':[{'at':'2026-11-16T00:00:02Z','savedPhotos':2,'attempts':3,'leaseRemainingSeconds':599}],'eligibilityToClaimBounds':[1,4],'claimToCompletionBounds':[100,103]}
  board=w.dashboard(record);self.assertTrue(board['title'].startswith('W1K'));self.assertEqual(board['panels'][2]['snapshotData'][0]['fields'][2]['values'],[4,103])
 def test_no_cli_live_access_without_separate_daemon_ack(self):
  spec=importlib.util.spec_from_file_location('cli710',ROOT/'scripts/prepare-v3-worker-fault.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  import contextlib,io
  for action in ['daemon-apply','daemon-restore','daemon-plan']:
   with contextlib.redirect_stderr(io.StringIO()),patch.object(sys,'argv',['worker',action]),patch.object(f,'Kube',side_effect=AssertionError('No early live access')),self.assertRaises(SystemExit):m.main()
 def test_pinned_chart_retarget_and_container_crd_schema(self):
  helm=os.environ.get('V3_HELM');validator=os.environ.get('V3_KUBECONFORM');schemas=os.environ.get('V3_SCHEMA_DIR')
  if not(helm and validator and schemas):self.skipTest('Validation tool paths not set')
  import yaml
  vals=yaml.safe_load((f.CHART/'values.yaml').read_text());vals['chaosDaemon']['nodeSelector']={'yeodam.io/role':'system','kubernetes.io/hostname':SYSTEM}
  with tempfile.TemporaryDirectory()as d:
   d=Path(d);vp=d/'values.yaml';vp.write_text(yaml.safe_dump(vals));from v3_chaos_install import reduce_upstream
   docs=reduce_upstream(list(yaml.safe_load_all(subprocess.check_output([helm,'template','v3-chaos',str(f.CHART/'chaos-mesh-2.8.8.tgz'),'-n','chaos-mesh','-f',str(vp)],text=True))));ds=next(x for x in docs if x['kind']=='DaemonSet')
   class Rendered:
    def get(self,*a,**kw):return ds
   h.daemon(Rendered());changed=copy.deepcopy(ds);changed['spec']=h.after_spec(ds['spec'],WORKER);path=d/'daemon.yaml';path.write_text(yaml.safe_dump(changed));subprocess.run([validator,'-strict','-kubernetes-version','1.36.0','-schema-location',str(Path(schemas)/'{{.ResourceKind}}.json'),str(path)],check=True)
   p,_,_,_=fixture();crd=c.crd();schema=next(x['schema']['openAPIV3Schema']for x in crd['spec']['versions']if x['name']=='v1alpha1');sp=d/'schema.json';sp.write_text(json.dumps(schema));fp=d/'fault.json';fp.write_text(json.dumps(p['fault']));subprocess.run([validator,'-strict','-schema-location',str(sp),str(fp)],check=True)


class SamplingCorrection(unittest.TestCase):
 def cli(self):
  spec=importlib.util.spec_from_file_location('fast_reader_cli',ROOT/'scripts/prepare-v3-worker-fault.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
 def test_fast_frame_one_exec_preserves_uid_and_causal_clock(self):
  m=self.cli();calls=[]
  class Reader:
   def call(self,args,source=None,timeout=20):
    calls.append(args);owner=fixture()[2];owner.pop('clock');owner['observedAt']=f.now()
    return 'POD_UID '+U+'\n'+json.dumps(owner)+'\nNODE_UTC '+f'{time.time():.9f}'+'\n'
  value=m.fast_ownership(Reader(),'w1-reader',U,time.monotonic()+2)
  self.assertEqual(len(calls),1);self.assertIn('exec',calls[0]);self.assertNotIn('logs',calls[0]);self.assertNotIn('get',calls[0]);self.assertEqual(value['clock']['podUid'],U)
  command=m.FAST_FRAME_COMMAND[-1];self.assertLess(command.index('cat '),command.index('date '))
  env=m.reader_manifest(WORKER,'w1-reader','AA==')['items'][-1]['spec']['containers'][0]['env']
  self.assertEqual(env,[{'name':'W1_OBSERVER_POD_UID','valueFrom':{'fieldRef':{'fieldPath':'metadata.uid'}}}])
 @unittest.skipUnless(os.environ.get('V3_SCHEMA_DIR')and os.environ.get('V3_KUBECONFORM'),'Pinned schemas required')
 def test_reader_pod_spec_against_pinned_deployment_pod_spec(self):
  bundle=self.cli().reader_manifest(WORKER,'w1-reader','AA==');pod=bundle['items'][-1]
  # The pinned Deployment schema embeds the Kubernetes PodSpec schema. Validate
  # the reader spec in that structural wrapper, without adding a Python package.
  wrapped={'apiVersion':'apps/v1','kind':'Deployment','metadata':pod['metadata'],
           'spec':{'selector':{'matchLabels':pod['metadata']['labels']},'template':{'metadata':{'labels':pod['metadata']['labels']},'spec':pod['spec']}}}
  with tempfile.TemporaryDirectory()as d:
   path=Path(d)/'reader-podspec.json';path.write_text(json.dumps(wrapped))
   subprocess.run([os.environ['V3_KUBECONFORM'],'-strict','-schema-location',str(Path(os.environ['V3_SCHEMA_DIR'])/'{{.ResourceKind}}.json'),str(path)],check=True)
  self.assertEqual(pod['spec']['containers'][0]['resources'],{'requests':{'cpu':'25m','memory':'64Mi'},'limits':{'cpu':'250m','memory':'192Mi'}})
 def test_fast_frame_rejects_wrong_uid_partial_stale_and_unavailable(self):
  m=self.cli()
  for mode in ('uid','partial','stale','unavailable','future'):
   class Reader:
    def call(self,*args,**kw):
     row=fixture()[2];row['observedAt']=w.iso(time.time()+(-10 if mode=='stale'else 10 if mode=='future'else 0))
     if mode=='unavailable':row={'unavailable':True}
     return 'POD_UID '+(NU if mode=='uid'else U)+'\n'+('{'if mode=='partial'else json.dumps(row))+'\nNODE_UTC '+f'{time.time():.9f}'+'\n'
   with self.subTest(mode=mode),self.assertRaises(ValueError):m.fast_ownership(Reader(),'w1-reader',U)
 def test_real_shell_frame_protocol_reads_complete_json_before_clock(self):
  m=self.cli()
  with tempfile.TemporaryDirectory()as tmp:
   target=Path(tmp)/'frame.json';target.write_text(json.dumps({'observedAt':f.now()})+'\n')
   command=m.FAST_FRAME_COMMAND[-1].replace('/tmp/worker-ownership.json',str(target))
   out=subprocess.check_output(['sh','-ec',command],env=dict(os.environ,W1_OBSERVER_POD_UID=U),text=True)
   lines=out.splitlines();self.assertEqual(len(lines),3);self.assertEqual(lines[0],'POD_UID '+U)
   self.assertLessEqual(w.epoch(json.loads(lines[1])['observedAt']),float(lines[2].split()[1]))
 def sampling_inputs(self):
  p,b,o,k=fixture()
  class Observer:
   gaps=[];exhausted=False
   def __init__(self,*args):pass
   def sample(self):
    value=copy.deepcopy(b);value['observedAt']=f.now();return value
   def summary(self,state):return {'state':state}
  def owner(**kw):
   value=copy.deepcopy(o);value['observedAt']=f.now();return old.clock_owner(value)
  return p,Observer,owner,k
 def test_independent_reads_overlap_with_real_subprocesses(self):
  p,Obs,owner,k=self.sampling_inputs();timing=[]
  def delay():subprocess.run([sys.executable,'-c','import time; time.sleep(.25)'],check=True,timeout=2)
  class Reads:
   deadline=time.monotonic()+3
   def get(self,*a):delay();return pod()
  class Db(Obs):
   def sample(self):delay();return super().sample()
  def read_owner(**kw):delay();return owner(**kw)
  proof,ownership,v=w.search_sample(Reads(),Db(),p['snapshot'],read_owner,timing.append)
  self.assertEqual(v['metadata']['uid'],U);self.assertEqual(len(timing),1)
  durations=timing[0]['readSeconds'];self.assertEqual(set(durations),{'db','victim','ownership'})
  self.assertLess(timing[0]['cycleSeconds'],sum(durations.values())*.8)
  self.assertEqual(w.partial(proof,p,ownership)['taskId'],'task1')
 def test_parallel_reads_share_one_real_subprocess_deadline(self):
  p,Obs,owner,k=self.sampling_inputs();timing=[]
  class Slow:
   def call(self,args,source=None,timeout=20):
    subprocess.run([sys.executable,'-c','import time; time.sleep(2)'],timeout=timeout,check=True)
  reads=w.DeadlineKube(Slow());reads.deadline=time.monotonic()+.15;start=time.monotonic()
  with self.assertRaises(subprocess.TimeoutExpired):w.search_sample(reads,Obs(),p['snapshot'],owner,timing.append)
  self.assertLess(time.monotonic()-start,.8);self.assertEqual(timing[0]['errors']['victim'],'TimeoutExpired')
 def test_probe_proof_rejects_slow_failed_old_and_changed_target(self):
  p,Obs,owner,k=self.sampling_inputs()
  with tempfile.TemporaryDirectory()as d,patch.object(w,'WorkerDbObserver',Obs):
   os.chmod(d,0o700);v=w.sampling_probe(k,p['snapshot'],705,d,owner);w.verify_sampling(v,p['snapshot'])
   for mode in ('slow','error','old','target'):
    bad=copy.deepcopy(v)
    if mode=='slow':bad['cycles'][0]['cycleSeconds']=4.281
    elif mode=='error':bad['cycles'][0]['errors']={'ownership':'TimeoutExpired'}
    elif mode=='old':bad['at']=w.iso(time.time()-301)
    else:bad['snapshot']['victim']['uid']=NU
    bad['proofSha256']=f.sha({k:v for k,v in bad.items()if k!='proofSha256'})
    with self.subTest(mode=mode),self.assertRaises(f.GuardError):w.verify_sampling(bad,p['snapshot'])
 def test_transport_margin_is_reported_without_widening_evidence_freshness(self):
  p,Obs,owner,k=self.sampling_inputs()
  with tempfile.TemporaryDirectory()as d,patch.object(w,'WorkerDbObserver',Obs):
   os.chmod(d,0o700);v=w.sampling_probe(k,p['snapshot'],705,d,owner)
   # Actual aborted r3 total: 1.505s. Under the revised, source-bound policy it
   # misses the acquisition target; it never changes the old failed receipt.
   v['cycles'][0].update(cycleSeconds=1.505,targetMet=False);v['targetMissCount']=1
   v['proofSha256']=f.sha({k:v for k,v in v.items()if k!='proofSha256'})
   w.verify_sampling(v,p['snapshot'])
   for mode in ('over-deadline','false-target','hidden-miss','old-policy','long-interval'):
    bad=copy.deepcopy(v)
    if mode=='over-deadline':bad['cycles'][0]['cycleSeconds']=2.005
    elif mode=='false-target':bad['cycles'][0]['targetMet']=True
    elif mode=='hidden-miss':bad['targetMissCount']=0
    elif mode=='old-policy':bad['samplingPolicy']={'samples':5,'cycleSeconds':1.5,'periodSeconds':1,'maxStartIntervalSeconds':2}
    else:bad['startIntervalsSeconds'][0]=2.005
    bad['proofSha256']=f.sha({k:v for k,v in bad.items()if k!='proofSha256'})
    with self.subTest(mode=mode),self.assertRaises(f.GuardError):w.verify_sampling(bad,p['snapshot'])
 def test_transport_margin_keeps_stale_db_and_changed_victim_blocked(self):
  p,Obs,owner,k=self.sampling_inputs()
  for mode in ('stale-db','slow-query','changed-victim','elapsed-cycle'):
   class Reads:
    deadline=time.monotonic()+(-.01 if mode=='elapsed-cycle' else 2)
    def get(self,*a):return pod(uid=NU if mode=='changed-victim' else U)
   class Db(Obs):
    def sample(self):
     value=super().sample()
     if mode=='stale-db':value['observedAt']=w.iso(time.time()-2.01)
     if mode=='slow-query':value['queryElapsedSeconds']=2.01
     return value
   with self.subTest(mode=mode),self.assertRaises(f.GuardError):w.search_sample(Reads(),Db(),p['snapshot'],owner,lambda x:None)
 def test_failed_parallel_read_records_error_and_never_creates_fault(self):
  p,Obs,owner,k=self.sampling_inputs();cleanup=[]
  def failed(**kw):raise subprocess.TimeoutExpired(['readonly-owner'],.01)
  with tempfile.TemporaryDirectory()as d:
   os.chmod(d,0o700)
   with patch.object(w,'runtime'),patch.object(c,'require_active'),patch.object(h,'require_daemon'),patch.object(h,'restore_daemon',return_value={}),patch.object(c,'restore',return_value={}),patch.object(w,'WorkerDbObserver',Obs):
    with self.assertRaises(subprocess.TimeoutExpired):w.execute(k,p,p['planSha256'],d,failed,{},lambda:cleanup.append('gpu-stop'),lambda:cleanup.append('reader'),{})
   receipt=json.loads((Path(d)/'receipt.json').read_text());self.assertFalse(receipt['injectionRequested'])
   timing=json.loads((Path(d)/'search-cycle-0001.json').read_text());self.assertEqual(timing['errors']['ownership'],'TimeoutExpired')
   self.assertFalse((Path(d)/'injection-intent.json').exists());self.assertEqual(cleanup,['gpu-stop','reader'])
   self.assertFalse(any('create'in args for args,body in k.calls))
 def test_actual_missed_one_to_five_and_null_current_item_stay_ineligible(self):
  p,b,o,k=fixture()
  for saved,current in ((1,2),(5,6),(4,None)):
   proof=copy.deepcopy(b)
   for row in proof['rows']:
    if row['section']=='worker_item'and row['batchId']==1:row.update(saved=int(row['itemId']<=saved),derivedHash='a'*64 if row['itemId']<=saved else None)
    if row['section']=='worker_task'and row['taskId']=='task1':row['currentItem']=current
   with self.subTest(saved=saved,current=current),self.assertRaisesRegex(f.GuardError,'No unique'):w.partial(proof,p,o)

if __name__=='__main__':unittest.main()
