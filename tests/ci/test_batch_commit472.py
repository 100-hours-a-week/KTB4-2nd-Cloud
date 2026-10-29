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
  control,plan=self.plans();plan['maxTransactionRecords']=11
  with self.assertRaises(RuntimeError):m.validate_plan(plan,control)

if __name__=='__main__':unittest.main()
