import copy
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('batch472',ROOT/'scripts/migration-batch472.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

class FrozenBatchComparison(unittest.TestCase):
 def plans(self):
  control={'issue':467,'rates':[{'rate':50,'runId':'control','preAllocatedVUs':1000,'maxVUs':6000}],
   'backendCommit':'a'*40,'backendImage':'ghcr.io/100-hours-a-week/yeodam-backend@sha256:'+'a'*64,
   'durationSeconds':180,'writerSwitch':False,'snapshotBytesPerSecond':9000000,'sourceApplyConcurrency':4,
   'httpTimeoutSeconds':100,'authResponseBudgetSeconds':55,'indexApplied':True}
  plan=copy.deepcopy(control);plan.update(issue=472,backendCommit='b'*40,
   backendImage='ghcr.io/100-hours-a-week/yeodam-backend@sha256:'+'b'*64,
   runtimeImageParent=control['backendImage'],maxTransactionRecords=10)
  plan['rates'][0]['runId']='candidate';return control,plan
 def test_only_candidate_artifact_and_bounded_batch_change(self):
  control,plan=self.plans();m.validate_plan(plan,control)
 def test_spec_concurrency_dump_waits_and_input_never_change(self):
  for key in ['durationSeconds','snapshotBytesPerSecond','sourceApplyConcurrency','httpTimeoutSeconds','authResponseBudgetSeconds']:
   control,plan=self.plans();plan[key]+=1
   with self.subTest(key=key),self.assertRaises(RuntimeError):m.validate_plan(plan,control)
 def test_no_extra_fields_or_writer_switch(self):
  control,plan=self.plans();plan['writerSwitch']=True
  with self.assertRaises(RuntimeError):m.validate_plan(plan,control)
  control,plan=self.plans();plan['newSetting']='unreviewed'
  with self.assertRaises(RuntimeError):m.validate_plan(plan,control)
 def test_runtime_upgrade_and_same_run_reuse_are_rejected(self):
  control,plan=self.plans();plan['runtimeImageParent']=plan['backendImage']
  with self.assertRaises(RuntimeError):m.validate_plan(plan,control)
  control,plan=self.plans();plan['rates'][0]['runId']='control'
  with self.assertRaises(RuntimeError):m.validate_plan(plan,control)
 def test_no_mutable_image_tag_or_larger_transaction(self):
  control,plan=self.plans();plan['backendImage']='ghcr.io/100-hours-a-week/yeodam-backend:latest'
  with self.assertRaises(RuntimeError):m.validate_plan(plan,control)
 def test_candidate_digest_reaches_existing_registry_preparation(self):
  from unittest.mock import Mock,patch
  old="def pull_image():\n    from importlib.machinery import SourceFileLoader\n    SourceFileLoader('prep444', '/var/lib/yeodam/migration444/prepare.py').load_module().ensure_image()\n"
  ns={'IMAGE':'candidate-digest'};preparation=Mock();preparation.ensure_image.side_effect=lambda:self.assertEqual(preparation.IMAGE,'candidate-digest')
  loader=Mock();loader.load_module.return_value=preparation
  exec(m.candidate_pull_source(old),ns)
  with patch('importlib.machinery.SourceFileLoader',return_value=loader):ns['pull_image']()
  preparation.ensure_image.assert_called_once()
 def test_pre_input_resume_cannot_generate_or_transfer_another_pool(self):
  r=m.module('resume472test',ROOT/'scripts/javamysql467-run.py')
  source="def execute(case,index):\n    state={'duration':900}\n    phase('source','b-integration376-control.py',run,'preflight')\n    prepared=prepare_existing()\n    transfer_pool(run)\n    phase('source','b-integration376-app.py',run,'start-app')\n    load_id=phase('generator','b-integration376-generator.py',run,'start','--rate',str(rate),sync=False)\n    kafka_standby=raw('kafka',[])\n"
  resumed=r.resume_preparation_source(source)
  self.assertNotIn('transfer_pool(',resumed);self.assertNotIn('prepare_existing()',resumed)
  self.assertIn("run,'start-app'",resumed);self.assertIn("run,'start'",resumed)
  self.assertNotIn('kafka_standby=',resumed)
 def test_full_integration_keeps_original_cutover_and_response_budgets(self):
  import json,tempfile
  f=m.module('integration472test',ROOT/'scripts/migration-batch472-integration.py')
  control,plan=self.plans();control.update(durationSeconds=900,cutoverSeconds=360)
  plan.update(durationSeconds=900,cutoverSeconds=360,imageWorkflow=123)
  with tempfile.TemporaryDirectory() as directory:
   f.ISSUE=Path(directory)
   (f.ISSUE/'image-receipt.json').write_text(json.dumps({'backendCommit':plan['backendCommit'],'backendImage':plan['backendImage'],'workflow':123}))
   f.validate_plan(plan,control)
   for key in ['durationSeconds','cutoverSeconds','httpTimeoutSeconds','sourceApplyConcurrency','snapshotBytesPerSecond']:
    changed=copy.deepcopy(plan);changed[key]+=1
    with self.subTest(key=key),self.assertRaises(RuntimeError):f.validate_plan(changed,control)
  control,plan=self.plans();plan['maxTransactionRecords']=11
  with self.assertRaises(RuntimeError):m.validate_plan(plan,control)

if __name__=='__main__':unittest.main()
