import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).parents[2]
spec=importlib.util.spec_from_file_location('final465',ROOT/'scripts/cdc-final465.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class FinalRunContractTests(unittest.TestCase):
    def plan(self,case):
        p=m.PROFILES[case]
        return {'durationSeconds':900,'cutoverSeconds':360,'authActors':p['authActors'],'poolCount':p['poolCount'],
                'rates':[{k:p[k] for k in ('rate','preAllocatedVUs','maxVUs')}],
                'commonBaseline':{'sha256':m.BASELINE,'trips':2048038,'journal':246512,'all33TablesAndAutoIncrementEqual':True}}
    def test_inherited_allocations_are_not_assumed_linear(self):
        self.assertEqual(m.PROFILES['rate10']['preAllocatedVUs'],64)
        self.assertEqual(m.PROFILES['rate30']['preAllocatedVUs'],900)
        for case in m.PROFILES:m.validate_plan(self.plan(case),case,{'durationSeconds':900,'cutoverSeconds':360},True)
    def test_changed_deadline_or_vus_rejected(self):
        for key in ('durationSeconds','cutoverSeconds'):
            p=self.plan('rate10');p[key]+=1
            with self.assertRaises(ValueError):m.validate_plan(p,'rate10',{'durationSeconds':900,'cutoverSeconds':360})
        p=self.plan('rate30');p['rates'][0]['preAllocatedVUs']=600
        with self.assertRaises(ValueError):m.validate_plan(p,'rate30',{})
    def test_old_rate30_snapshot_is_not_the_common_final_baseline(self):
        p=self.plan('rate30');p['commonBaseline']['sha256']=m.OLD30
        with self.assertRaises(ValueError):m.validate_plan(p,'rate30',{},True)
    def test_other_case_and_implicit_selection_refused_before_io(self):
        for case in (None,'index50','rate50'):
            with self.subTest(case=case),self.assertRaises(ValueError):m.load(case)
    def test_only_baseline_assertion_is_adapted(self):
        before="assert all(other_checks)\nx=("+repr(m.BASELINE)+" if r['rate']==50 else "+repr(m.OLD30)+")\n"
        after=m.render_assess(before)
        self.assertEqual(after,"assert all(other_checks)\nx=("+repr(m.BASELINE)+")\n")
        with self.assertRaises(ValueError):m.render_assess('different template')
    def test_strategy_failure_can_continue_after_complete_restoration(self):
        with tempfile.TemporaryDirectory() as t,patch.object(m,'BASE',Path(t)):
            p=Path(t)/'rate10';p.mkdir();(p/'plan.json').write_text(json.dumps({'rates':[{'runId':'one'}]}))
            (p/'one.state.json').write_text(json.dumps({'phase':'completed','switched':False}))
            flags={'sourceFlags':['0','0'],'targetFlags':['1','1']}
            (p/'runtime-final-source.json').write_text(json.dumps([flags]))
            (p/'runtime-final-kafka.json').write_text(json.dumps([{'dedicatedWorkerStopped':True}]))
            m.previous_restored('rate30')
            flags['targetFlags']=['0','0'];(p/'runtime-final-source.json').write_text(json.dumps([flags]))
            with self.assertRaises(RuntimeError):m.previous_restored('rate30')


if __name__=='__main__':unittest.main()
