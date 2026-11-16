import copy,importlib.util,json,os,sys,tempfile,unittest,zipfile,subprocess
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests/ci/v3')]
import v3_conversion_lease as l
import v3_worker_fault as w
import v3_worker_container as h
import v3_cdc_fault as f
import test_v3_worker_container as old
import v3_release_bundle as bundle
from full_release_fixture import candidate as synthetic_candidate


def support(component):
    d={'version':1,'mode':'image','beSource':component['sourceSha'],'beImage':component['image'],
       'structuralCompatibility':True,'semanticSafetyProven':False,'filesSha256':{k:'a'*64 for k in ('policy','worker','transaction','yaml')}}
    return dict(d,proofSha256=f.sha(d))


def candidate():return synthetic_candidate()
def budget():return {'maxObservedRenewGapSeconds':31,'maxObservedRenewSeconds':1,
    'unobservedPauseAllowanceSeconds':25,'rationale':'Synthetic fixture engineering allowance, not measured production budget','evidenceSha256':['a'*64]}


def fixture(duration=600):
    p,b,o,k=old.fixture();c=candidate();s=support(c['components']['backend']);c=l.candidate(c,s,duration,budget()if duration<600 else None)
    review=l.review(c,s,p['snapshot']['approvedRevision'],budget()if duration<600 else None)
    snap=copy.deepcopy(p['snapshot']);snap['leaseReview']=review
    snap.update(beSource=review['beSource'],beImage=review['beImage'])
    snap['victim']['image']=review['beImage']
    with patch.object(w,'runtime'):daemon=h.daemon_plan(k,snap)
    p=w.prepare(snap,p['tripId'],p['runId'],p['input'],True,daemon)
    task=next(x for x in b['rows']if x['section']=='worker_task'and x['taskId']=='task1');task['leaseSeconds']=duration-1
    return p,b,o,k


class Lease(unittest.TestCase):
 def test_pair_scope_budget_and_invalid_values(self):
  self.assertIsNone(l.policy({}))
  for data in [{l.ENV[0]:'120s'},{l.ENV[0]:'120s',l.ENV[1]:'15s'},dict(zip(l.ENV,['89s','30s'])),dict(zip(l.ENV,['601s','30s'])),dict(zip(l.ENV,['2m','30s']))]:
   with self.subTest(data=data),self.assertRaises(ValueError):l.policy(data)
  self.assertEqual(l.policy(dict(zip(l.ENV,['120s','30s'])))['durationSeconds'],120)
  for bad in [None,dict(budget(),maxObservedRenewSeconds=float('nan')),dict(budget(),maxObservedRenewGapSeconds=100),dict(budget(),rationale='')]:
   with self.assertRaises(ValueError):l.budget_ok(bad,120)
 def test_candidate_is_deepcopy_converter_pair_only(self):
  c=candidate();original=copy.deepcopy(c);s=support(c['components']['backend'])
  result=l.candidate(c,s,600);self.assertEqual(c,original)
  delta=result['values']['backend']['workloads']['photo-converter']['configMapData']
  self.assertEqual({k:delta.pop(k)for k in l.ENV},dict(zip(l.ENV,['600s','30s'])))
  self.assertEqual(result,c)
  with self.assertRaises(ValueError):l.candidate(c,s,120)
 def test_already_enabled_policy_reapplication_is_idempotent(self):
  for duration in (600,120):
   c=candidate();proof=support(c['components']['backend']);margin=budget()if duration<600 else None
   enabled=l.candidate(c,proof,duration,margin);original=copy.deepcopy(enabled)
   self.assertEqual(l.candidate(enabled,proof,duration,margin),original)
   self.assertEqual(enabled,original)
 def test_lease_fixture_does_not_read_current_release_candidate(self):
  live=ROOT/'deploy/v3/releases/candidate.json';read=Path.read_text
  def guarded(path,*args,**kwargs):
   if path==live:raise AssertionError('Use synthetic inputs, not the deployment candidate')
   return read(path,*args,**kwargs)
  with patch.object(Path,'read_text',guarded):
   for duration in (600,120):
    p,_,_,_=fixture(duration);w.verify(p,p['planSha256'])
    self.assertEqual(p['snapshot']['beImage'],candidate()['components']['backend']['image'])
    self.assertNotEqual(p['snapshot']['beImage'],w.BE_IMAGE)
 def test_inspected_supported_image_required_not_local_jar_or_self_claim(self):
  c=candidate();s=support(c['components']['backend'])
  for key,value in [('mode','local-jar'),('beImage','ghcr.io/100-hours-a-week/yeodam-backend@sha256:'+'b'*64),('semanticSafetyProven',True),('structuralCompatibility',False)]:
   bad=dict(s,**{key:value});bad['proofSha256']=f.sha({k:v for k,v in bad.items()if k!='proofSha256'})
   with self.assertRaises(ValueError):l.candidate(c,bad,600)
 def test_local_jar_inspection_old_image_and_partial_classes_fail_closed(self):
  with tempfile.TemporaryDirectory()as d:
   jar=Path(d)/'a.jar'
   with zipfile.ZipFile(jar,'w')as z:z.writestr('BOOT-INF/classes/application.yaml','old settings')
   with self.assertRaises(ValueError):l.inspect_jar(jar,'a'*40)
 def test_policy_change_does_not_change_other_candidate_fingerprint(self):
  c=candidate();s=support(c['components']['backend']);a=l.review(l.candidate(c,s,600),s,'a'*40)
  b=l.review(l.candidate(c,s,120,budget()),s,'b'*40,budget())
  self.assertEqual(a['nonPolicyCandidateSha256'],b['nonPolicyCandidateSha256'])
  self.assertNotEqual(a['candidateSha256'],b['candidateSha256']);l.validate_review(b)
  b['policy']['durationSeconds']=90
  with self.assertRaises(ValueError):l.validate_review(b)
 def test_new_case_is_explicit_old_plan_criteria_unchanged(self):
  p,b,o,_=fixture(120);w.verify(p,p['planSha256']);self.assertEqual(p['case'],'W2K')
  self.assertEqual(p['criteria']['leaseSeconds'],120);self.assertEqual(p['criteria']['observationSeconds'],900)
  self.assertEqual(w.criteria(True)['leaseSeconds'],600)
  self.assertEqual(p['criteria']['terminationToCompletionTargetSeconds'],465)
  self.assertEqual(w.partial(b,p,o)['taskId'],'task1')
  bad=copy.deepcopy(p);bad['case']='W1K';bad['planSha256']=f.sha({k:v for k,v in bad.items()if k!='planSha256'})
  with self.assertRaises(ValueError):w.verify(bad,bad['planSha256'])
 def test_task_frozen_before_kill_whole_post_death_cohort_does_not_cherry_pick(self):
  p,b,o,_=fixture();selected=w.partial(b,p,o);post=copy.deepcopy(b)
  current=next(x for x in post['rows']if x.get('itemId')==3);current.update(saved=1,derivedHash='d'*64,attachmentId=803)
  following=next(x for x in post['rows']if x.get('itemId')==4);following.update(startedAt=f.now(),attempt=1)
  next(x for x in post['rows']if x['section']=='worker_task'and x['taskId']=='task1')['currentItem']=4
  frozen=l.cohort(selected,post)
  self.assertEqual(frozen['members'],[{'itemId':4,'attempt':1}]);self.assertTrue(frozen['qualified'])
  self.assertTrue(frozen['notLegacyW1KPass']);self.assertEqual(len(frozen['saved']),3)
  following['startedAt']=None
  self.assertFalse(l.cohort(selected,post)['qualified'])
  task=next(x for x in post['rows']if x['section']=='worker_task'and x['taskId']=='task1');task['claimHash']='b'*64
  self.assertFalse(l.cohort(selected,post)['qualified'])
 def test_live_policy_mismatch_cannot_inject(self):
  p,_,_,_=fixture(120)
  class K:
   def get(self,*a):return {'data':dict(zip(l.ENV,['600s','30s']))}
  with self.assertRaises(ValueError):w.runtime_lease(K(),p['snapshot'])
 def test_comparison_rejects_legacy_incomplete_and_confounded_results(self):
  a={'case':'W2K','evidenceComplete':True,'beImage':'same','input':{'sha256':'a'*64},'nonPolicyCandidateSha256':'c'*64,'conditionsFingerprint':'d'*64,
     'policy':{'durationSeconds':600,'renewIntervalSeconds':30},'terminationToCompletionBounds':[790,800]}
  b=dict(a,policy={'durationSeconds':120,'renewIntervalSeconds':30},terminationToCompletionBounds=[350,360])
  self.assertEqual(l.compare(a,b,True)['reductionSecondsBounds'],[430,450])
  for bad in [dict(b,case='W1K'),dict(b,evidenceComplete=False),dict(b,beImage='other'),dict(b,nonPolicyCandidateSha256='d'*64)]:
   with self.assertRaises(ValueError):l.compare(a,bad,True)
  with self.assertRaises(ValueError):l.compare(a,b,False)
 def test_range_metrics_are_fixed_and_do_not_replace_missing_with_zero(self):
  self.assertEqual(len(l.QUERIES),9)
  self.assertTrue(all('namespace="photo"'in q and ' or 'not in q for q in l.QUERIES.values()))
 def test_w2_execution_freezes_first_post_death_cohort_and_restores(self):
  import v3_worker_container as h
  import v3_worker_chaos as c
  p,b,o,k=fixture();calls=[]
  class Observer:
   gaps=[];exhausted=False
   def __init__(self,*args):self.n=0
   def sample(self):
    self.n+=1;x=copy.deepcopy(b);x['queryStartedAt']=x['observedAt']=f.now()
    if k.injected:
     next(v for v in x['rows']if v.get('itemId')==3).update(saved=1,derivedHash='d'*64,attachmentId=3)
     next(v for v in x['rows']if v.get('itemId')==4).update(startedAt=f.now(),attempt=1)
     next(v for v in x['rows']if v['section']=='worker_task'and v['taskId']=='task1')['currentItem']=4
    if self.n>=4:next(v for v in x['rows']if v['section']=='execution_timing')['status']='COMPLETED'
    return x
   def summary(self,state):return {'state':state}
  with tempfile.TemporaryDirectory()as d:
   os.chmod(d,0o700)
   with patch.object(w,'runtime'),patch.object(c,'require_active'),patch.object(h,'require_daemon'),patch.object(h,'restore_daemon',side_effect=lambda *a:calls.append('daemon')),patch.object(c,'restore',side_effect=lambda *a:calls.append('profile')),patch.object(w,'WorkerDbObserver',Observer):
    def owner(**kw):
     v=copy.deepcopy(o);v['observedAt']=f.now();return old.old.clock_owner(v)
    rec=w.execute(k,p,p['planSha256'],d,owner,{},lambda:calls.append('gpu'),lambda:calls.append('reader'),{})
   frozen=json.loads((Path(d)/'post-death-cohort.json').read_text())
   self.assertEqual(rec['case'],'W2K');self.assertTrue(rec['processKillObserved']);self.assertFalse(rec['postKillWindowObserved'])
   self.assertEqual(frozen['members'],[{'itemId':4,'attempt':1}]);self.assertEqual(calls,['gpu','reader','daemon','profile'])
 def test_w2_assessment_does_not_regrade_legacy_selected_photo(self):
  p,b,o,k=fixture();sel=w.partial(b,p,o);post=copy.deepcopy(b)
  next(x for x in post['rows']if x.get('itemId')==3).update(saved=1,derivedHash='d'*64,attachmentId=3)
  next(x for x in post['rows']if x.get('itemId')==4).update(startedAt='2026-11-16T00:00:01Z',attempt=1)
  next(x for x in post['rows']if x['section']=='worker_task'and x['taskId']=='task1')['currentItem']=4
  post.update(queryStartedAt='2026-11-16T00:00:12Z',observedAt='2026-11-16T00:00:13Z')
  after=copy.deepcopy(post);next(x for x in after['rows']if x['section']=='execution_timing')['status']='COMPLETED'
  for x in after['rows']:
   if x['section']=='worker_item':
    x.update(saved=1,attachmentId=x['itemId'],derivedHash=x['derivedHash']or 'f'*64,attempt=2 if x['itemId']==4 else max(1,x['attempt']))
    after['rows'].append({'section':'final_attachment','tripId':p['tripId'],'attachmentId':x['itemId'],'fileId':x['itemId']})
  timing={'case':'W2K','derivedFromRecordedSamples':True,'processKillObserved':True,'podKillObserved':True,
   'postKillWindowObserved':False,'prematureClaimObserved':False,'manualActions':[],
   'terminationLowerAt':'2026-11-16T00:00:00Z','terminationUpperAt':'2026-11-16T00:00:10Z',
   'completedAtBounds':['2026-11-16T00:12:59Z','2026-11-16T00:13:00Z'],
   'eligibilityAt':'2026-11-16T00:10:00Z','newClaimAt':'2026-11-16T00:10:03Z',
   'completedAt':'2026-11-16T00:13:00Z','eligibilityToClaimBounds':[0,3],'claimToCompletionBounds':[176,180]}
  db={'approvedRevision':b['approvedRevision'],'tripIds':[p['tripId']],'continuousObservationComplete':True,'observationGaps':[]}
  rec={'chaosInjectedObserved':True}
  rt={'at':'2026-11-16T00:00:11Z','processTermination':{'killedContainerId':p['snapshot']['victim']['containerId'],'exitCode':137}}
  with tempfile.TemporaryDirectory()as directory:
   d=Path(directory)
   for name,value in [('pre-injection.json',b),('partial.json',sel),('post-termination.json',post),('post-death-cohort.json',l.cohort(sel,post)),('final.json',after),('db-summary.json',db),('receipt.json',rec),('runtime-0001.json',rt)]:
    (d/name).write_text(json.dumps(value))
   with patch.object(w,'summarize',return_value=timing),patch.object(l,'metrics_ready',return_value=(True,'a'*64)):
    result=l.assess(p,d,old.old.trace_fixture(),{})
   self.assertTrue(result['evidenceComplete']);self.assertTrue(result['cohortRetried'])
   self.assertFalse(result['legacyAssessment']['evidenceComplete']);self.assertIsNone(result['legacyAssessment']['automaticRecovery'])
   self.assertFalse(result['exactlyOnceProven']);self.assertFalse(result['allS3SideEffectsVerified'])
   next(x for x in after['rows']if x['section']=='execution_timing')['status']='PROCESSING';(d/'final.json').write_text(json.dumps(after))
   with patch.object(w,'summarize',return_value=timing),patch.object(l,'metrics_ready',return_value=(True,'a'*64)):
    self.assertFalse(l.assess(p,d,old.old.trace_fixture(),{})['evidenceComplete'])
 def test_actual_metrics_bind_policy_pod_and_window_and_missing_is_incomplete(self):
  p,_,_,_=fixture(120);rev=p['snapshot']['approvedRevision']
  app={'namespace':'photo','name':'photo-converter','replicas':1,'podTemplateSha256':'a'*64,'containers':[]}
  condition={'apps':[app],'approvedRevision':rev,'nodeTypes':{'t3a.medium':1},'scalingPolicies':{'policy':'same'}}
  m={'leaseReviewSha256':p['snapshot']['leaseReview']['reviewSha256'],'leaseVictimPod':p['snapshot']['victim']['name'],
     'conditionsBefore':condition,'conditionsAfter':copy.deepcopy(condition),'start':'2026-11-16T00:00:00Z','end':'2026-11-16T00:14:00Z',
     'ranges':{'conversion_'+k:{'status':'ok','series':[{'values':[[100,120 if k=='lease_seconds'else 30 if k=='renew_interval_seconds'else 1]]}]}for k in l.QUERIES}}
  timing={'terminationLowerAt':'2026-11-16T00:00:01Z','completedAtBounds':['2026-11-16T00:13:00Z','2026-11-16T00:13:00.9Z']}
  self.assertTrue(l.metrics_ready(m,p,timing)[0]);m['ranges']['conversion_renew_gap_max']['status']='missing'
  self.assertFalse(l.metrics_ready(m,p,timing)[0]);m['leaseVictimPod']='other'
  with self.assertRaises(ValueError):l.metrics_ready(m,p,timing)
 def test_budget_uses_actual_recorded_maxima_not_missing_or_zero(self):
  m={'leaseReviewSha256':'a'*64,'leaseVictimPod':'photo-converter-a-b','start':'2026-11-16T00:00:00Z','end':'2026-11-16T00:15:00Z',
     'ranges':{key:{'status':'ok','series':[{'values':[[1,value],[2,value+.1]]}]}for key,value in [('conversion_renew_gap_max',31),('conversion_renew_duration_max',.2)]}}
  b=l.measured_budget(m,25,'Synthetic pause allowance; decide with real baseline before applying')
  self.assertEqual(b['maxObservedRenewGapSeconds'],31.1);self.assertEqual(b['evidenceSha256'],[f.sha(m)])
  for row in [{'status':'missing','series':[]},{'status':'ok','series':[{'values':[[1,0]]}]},{'status':'ok','series':[{'values':[[1,None]]}]}]:
   bad=copy.deepcopy(m);bad['ranges']['conversion_renew_gap_max']=row
   with self.assertRaises(ValueError):l.measured_budget(bad,25,'A synthetic rationale sufficiently long for validation')
 def test_legacy_assessor_rejects_w2_records(self):
  with self.assertRaises(ValueError):w.assess({}, {}, {'case':'W2K'}, {}, {})
 def test_backend_policy_loads_in_an_isolated_interpreter_without_scripts_path(self):
  source=ROOT/'scripts/v3_backend_contract.py'
  code="import importlib.util; s=importlib.util.spec_from_file_location('backend',"+repr(str(source))+"); m=importlib.util.module_from_spec(s);s.loader.exec_module(m); assert m.conversion_lease_policy(dict(zip(m.LEASE_ENV,['120s','30s'])))['durationSeconds']==120"
  result=subprocess.run([sys.executable,'-I','-c',code],text=True,capture_output=True,timeout=5)
  self.assertEqual(result.returncode,0,result.stderr)
 def test_historical_contract_stays_frozen(self):
  lock=w.contract();self.assertEqual(lock['leaseSeconds'],600)
  self.assertEqual(lock['beSource'],w.BE_SOURCE)

if __name__=='__main__':unittest.main()
