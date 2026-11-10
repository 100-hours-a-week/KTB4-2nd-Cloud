"""Cutover ordering, no-write failure paths and operating-only config; no live credentials."""
import copy,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import v2_v3_cutover as c

def module(name,path):
 s=importlib.util.spec_from_file_location(name,ROOT/'scripts'/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
run=module('runtime602','run-v2-v3-cutover.py');final=module('final602','finalize-v2-v3-cutover.py')

def review():
 jobs={t:{'statuses':{'COMPLETED':2},'unresolved':0} for t in ['trip_creation_executions','story_generation_jobs','trip_creation_outbox']}
 return {'photoObjectsVerified':1511,'sourceQdrantMatchesBackup':True,'capturedAt':c.now(),'sourceJobs':jobs,'qaJobs':copy.deepcopy(jobs),'qaConsumerLag':{g:0 for g in c.GROUPS},'qaAiQueueItems':0,'qaUploadsInFlight':0,'sourceWritersStopped':True,'qaAccepted':True}
def dns_plan():
 before={'zone_id':c.POLICY['zoneId'],'name':c.POLICY['domain'],'type':'A','alias':[{'name':c.POLICY['oldDns']}]}
 after=copy.deepcopy(before);after['alias'][0]['name']=c.POLICY['newDns']
 return {'resource_changes':[{'mode':'managed','address':'aws_route53_record.operating["root"]','change':{'actions':['update'],'before':before,'after':after}}]}

class Contracts(unittest.TestCase):
 def test_valid_review(self):c.validate_reconciliation(review())
 def test_pending_job_lag_upload_and_unknown_group_block(self):
  for field in ['sourceJobs','qaJobs','qaConsumerLag','qaAiQueueItems','qaUploadsInFlight','sourceWritersStopped','qaAccepted']:
   v=review()
   if field.endswith('Jobs'):v[field]['trip_creation_executions']['unresolved']=1
   elif field=='qaConsumerLag':v[field][next(iter(c.GROUPS))]=1
   elif field in ['qaAiQueueItems','qaUploadsInFlight']:v[field]=1
   else:v[field]=False
   with self.subTest(field=field),self.assertRaises(ValueError):c.validate_reconciliation(v)
  v=review();v['qaConsumerLag'].pop(next(iter(c.GROUPS)))
  with self.assertRaises(ValueError):c.validate_reconciliation(v)
 def test_stale_review_blocks(self):
  v=review();v['capturedAt']='2020-01-01T00:00:00+00:00'
  with self.assertRaises(ValueError):c.validate_reconciliation(v)
 def test_terminal_failure_is_not_rewritten(self):
  def execute(sql):
   if 'table_name FROM' in sql:return 'trip_creation_executions\nstory_generation_jobs\ntrip_creation_outbox'
   if 'column_name FROM' in sql:return 'id\nstatus'
   return 'FAILED\t3\nCOMPLETED\t7'
  value=c.audit_jobs(execute);self.assertEqual(value['trip_creation_executions']['unresolved'],0)
  self.assertEqual(value['trip_creation_executions']['statuses']['FAILED'],3)
 def test_unknown_job_state_blocks(self):
  v=review();v['sourceJobs']['trip_creation_executions']={'statuses':{'RETRY':1},'unresolved':1}
  with self.assertRaises(ValueError):c.validate_reconciliation(v)
 def test_receipt_chain_and_missing_prior(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);sha='a'*64
   with self.assertRaises(ValueError):c.prior_receipts(p,'mysql-restore',sha)
   previous=None
   for phase in ['pause','qa-backup']:
    self.assertEqual(c.prior_receipts(p,phase,sha),previous)
    previous=c.digest(c.receipt(p,phase,sha,previous,{'verified':True}))
   self.assertEqual(c.prior_receipts(p,'mysql-restore',sha),previous)
   with self.assertRaises(ValueError):c.prior_receipts(p,'pause',sha)
   value=c.load(p/'qa-backup.json');value['previous']='tampered';(p/'qa-backup.json').write_text(json.dumps(value))
   with self.assertRaises(ValueError):c.prior_receipts(p,'mysql-restore',sha)
 def test_unverified_receipt_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(ValueError):c.receipt(Path(tmp),'pause','a'*64,None,{'verified':False})
 def test_candidate_preserves_images_resources_and_replicas(self):
  source=json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text());candidate=c.operating_candidate(source)
  self.assertEqual(candidate['components'],source['components'])
  for profile in ['frontend','backend','websocket']:
   self.assertEqual(candidate['values'][profile]['defaults']['resources'],source['values'][profile]['defaults']['resources'])
   for role in source['values'][profile]['workloads']:
    self.assertEqual(candidate['values'][profile]['workloads'][role].get('replicas'),source['values'][profile]['workloads'][role].get('replicas'))
  for role,w in candidate['values']['backend']['workloads'].items():self.assertEqual(w['configMapData']['KAKAO_REDIRECT_URI'],'https://yeodam-2gether.com/api/auth/kakao/callback')
 def test_fresh_cdc_does_not_change_wire_or_snapshot_filters(self):
  source=json.loads((ROOT/'deploy/v3/outbox/files/connector.json').read_text());actual=c.operating_cdc(source)
  changes={k for k in actual if actual[k]!=source[k]}
  self.assertEqual(changes,{'topic.prefix','schema.history.internal.kafka.topic','predicates.heartbeat.pattern'})
  self.assertEqual(actual['snapshot.mode'],'initial')
  self.assertIn("status = 'PENDING'",actual['snapshot.select.statement.overrides.yeodam.trip_creation_outbox'])
 def test_no_destructive_mysql_restore_without_qa_archive(self):
  class K:
   def apps_stopped(self):return True
   def sql(self,q):raise AssertionError('SQL must not run before QA backup verification')
  with tempfile.TemporaryDirectory() as d,self.assertRaises(ValueError):run.mysql_restore(K(),Path(d),{})
 def test_backup_tamper_blocks_before_write(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'qa').mkdir();(root/'qa/x').write_text('changed')
   c.write(root/'qa/backup.json',{'files':{'x':'0'*64}})
   with self.assertRaises(ValueError):run.qa_proof(root)
 def test_rollback_after_resume_forbidden(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'resume.json').write_text('{}')
   with self.assertRaises(ValueError):run.rollback_qa(None,p)
 def test_only_existing_root_dns_update(self):self.assertTrue(final.check_dns_plan(dns_plan()))
 def test_dns_wrong_source_target_and_extra_resource_rejected(self):
  for kind in ['delete','create','source','target','type','zone','extra']:
   p=dns_plan();r=p['resource_changes'][0]
   if kind in ['delete','create']:r['change']['actions']=[kind]
   elif kind in ['source','target']:r['change']['before' if kind=='source' else 'after']['alias'][0]['name']='unexpected.example.com'
   elif kind=='type':r['change']['after']['type']='AAAA'
   elif kind=='zone':r['change']['after']['zone_id']='other'
   else:p['resource_changes'].append(copy.deepcopy(r))
   with self.subTest(kind=kind),self.assertRaises(ValueError):final.check_dns_plan(p)
 def test_photo_revalidation_is_read_only_and_detects_latest_version_change(self):
  import v2_v3_photo_migration as photos
  plan={'ready':True,'backupSha256':'a'*64,'sourceBucket':photos.SOURCE,'targetBucket':photos.TARGET,'sqlSha256':photos.SQL_SHA256,'objects':[{'key':'photos/a','source':{'fixture':True}}]}
  results={'completed':True,'planSha256':photos.digest(plan),'results':[{'key':'photos/a','status':'copied','versionId':'original'}]}
  class Store:
   def assert_buckets(self):pass
   def observe(self,bucket,key):return {'head':{'VersionId':self.version}}
   def copy(self,*args):raise AssertionError('Read-only verification must never copy')
  store=Store();store.version='original'
  with patch.object(photos,'equal',return_value=True):
   self.assertEqual(c.verify_photos(plan,results,store,'a'*64),1)
   store.version='changed'
   with self.assertRaises(ValueError):c.verify_photos(plan,results,store,'a'*64)
 def test_mongo_actions_skip_implicit_plaintext_connection(self):
  class TlsOnlyShell:
   def execute(self,ns,pod,container,argv):
    if '--nodb' not in argv:raise RuntimeError('Implicit plaintext startup connection rejected')
    self.argv=argv
    return b'[]'
  k=TlsOnlyShell()
  for action in ['backup','clear','verify-stories','restore']:
   with self.subTest(action=action):
    self.assertEqual(run.mongo_action(k,action),b'[]')
    self.assertIn('CUTOVER_ACTION='+action,k.argv)
    self.assertIn("load('/runtime/common.js')",k.argv[-1])
    self.assertNotIn('--tlsAllowInvalidCertificates',k.argv)
 def test_qa_backup_receipt_cannot_omit_an_archive(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'qa').mkdir();proof={'files':{}}
   c.write(p/'qa/backup.json',proof);c.write(p/'qa-backup.json',{'result':{'backupManifestSha256':c.digest(proof)}})
   with self.assertRaises(ValueError):run.qa_proof(p)
 def test_capacity_hold_does_not_change_specs_or_create_nodes(self):
  class K:
   def __init__(self):self.patches=[]
   def patch(self,*args):self.patches.append(args)
  k=K();baseline={'pools':{name:{'metadata':{'uid':name},'spec':{'disruption':{'budgets':[{'nodes':'1','reasons':['Empty']}]}}} for name in ['api','worker']}}
  run.hold_capacity(k,baseline)
  self.assertEqual(len(k.patches),2)
  for ns,kind,name,original,ops in k.patches:
   self.assertEqual(kind,'nodepool');self.assertEqual(ops,[{'op':'replace','path':'/spec/disruption/budgets','value':[{'nodes':'0'}]}])
 def test_changed_node_claims_block_capacity_restore(self):
  class K:
   def get(self,*args):return {'items':[{'metadata':{'uid':'new','labels':{'karpenter.sh/nodepool':'api'}}}]}
   def patch(self,*args):raise AssertionError('Do not restore a changed pool')
  with self.assertRaises(ValueError):run.restore_capacity(K(),{'claimUids':['old'],'pools':{}})
 def test_operating_cors_rejects_unrelated_changes(self):
  rule={'allowed_origins':['https://v3.yeodam-2gether.com'],'allowed_methods':['GET','HEAD','PUT','POST'],'allowed_headers':['*'],'max_age_seconds':600}
  before={'bucket':'yeodam-v3-foundation-app-data-483175530259-ap-northeast-2','cors_rule':[rule]};after=copy.deepcopy(before);after['cors_rule'][0]['allowed_origins'].append('https://yeodam-2gether.com')
  p={'resource_changes':[{'mode':'managed','address':'aws_s3_bucket_cors_configuration.qa[0]','change':{'actions':['update'],'before':before,'after':after}}]}
  self.assertTrue(final.check_cors_plan(p))
  p['resource_changes'][0]['change']['after']['cors_rule'][0]['allowed_methods'].append('DELETE')
  with self.assertRaises(ValueError):final.check_cors_plan(p)
 def test_no_qadrant_or_mysql_secret_print_in_error_path(self):
  # Kube errors may contain row values, so every subprocess failure is redacted.
  with patch.object(run.subprocess,'run') as process:
   process.return_value.returncode=1;process.return_value.stdout=b'private row';process.return_value.stderr=b'password'
   k=object.__new__(run.Kube);k.prefix=['kubectl']
   with self.assertRaisesRegex(ValueError,'private response suppressed'):k.call('get','pods')
 def test_same_api_request_in_ci_cannot_execute_cutover(self):
  with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'}):
   p={'planSha256':'wrong'}
   with self.assertRaises(ValueError):c.verify_plan(p,'wrong')

if __name__=='__main__':unittest.main()
