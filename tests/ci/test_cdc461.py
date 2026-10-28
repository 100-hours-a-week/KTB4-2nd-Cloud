import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).parents[2]
SPEC=importlib.util.spec_from_file_location('diag461',ROOT/'scripts/diagnose-cdc461.py')
m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)


class ContractTests(unittest.TestCase):
    def setUp(self): self.c=json.loads((ROOT/'tests/load/fixtures/cdc461-contract.json').read_text())
    def test_exact_contract(self):m.validate(self.c)
    def test_other_destination_rejected(self):
        for key in ['targetInstance','kafkaInstance','account','region']:
            c=copy.deepcopy(self.c);c[key]='other'
            with self.subTest(key=key),self.assertRaises(ValueError):m.validate(c)
    def test_changed_resources_batch_or_budget_rejected(self):
        for key in ['tripTaskLimit','tripBatch','tripPoll','journalBatch','workerCpuLimit','workerMemoryLimit','diagnosticDeadlineSeconds']:
            c=copy.deepcopy(self.c);c[key]+=1
            with self.subTest(key=key),self.assertRaises(ValueError):m.validate(c)
    def test_missing_or_different_replay_data_rejected(self):
        c=copy.deepcopy(self.c);c['tripEnd']['0']-=1
        with self.assertRaises(ValueError):m.validate(c)
        c=copy.deepcopy(self.c);c['tripEnd']={'0':35212}
        with self.assertRaises(ValueError):m.validate(c)
    def test_no_source_or_generator_actions(self):
        with tempfile.TemporaryDirectory() as d:
            rt=m.Runtime(Path(d))
            for role in ['source','generator','production']:
                with self.subTest(role=role),self.assertRaises(ValueError):rt.send(role,[])


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('analysis461',ROOT/'scripts/cdc461/analyze.py')
        cls.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.m)
    def test_commit_picoseconds_are_not_sql_or_milliseconds(self):
        event='statement/sql/commit'
        result=self.m.statement_delta([[event,'10','1000000000000']],[[event,'110','3000000000000']])[event]
        self.assertEqual(result['calls'],100);self.assertEqual(result['totalSeconds'],2);self.assertEqual(result['meanMs'],20)
    def test_counter_reset_cannot_be_reported_as_latency(self):
        event='statement/sql/commit'
        result=self.m.statement_delta([[event,'100','3000']],[[event,'1','10']])[event]
        self.assertTrue(result['counterReset']);self.assertIsNone(result['meanMs'])
    def test_commit_stack_distinguished_from_batch_execution(self):
        self.assertEqual(self.m.category(['com.mysql.cj.jdbc.ConnectionImpl.commit(ConnectionImpl.java:1)']),'commit path')
        self.assertEqual(self.m.category(['com.mysql.cj.jdbc.ClientPreparedStatement.executeBatchInternal(File.java:1)']),'SQL/write path')
        self.assertEqual(self.m.category(['com.mysql.cj.jdbc.ConnectionImpl.setAutoCommit(File.java:1)']),'transaction state path')


@unittest.skipUnless(shutil.which('javac') and shutil.which('java'),'JDK unavailable; compilation remains required before remote deployment')
class ObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.path=Path(cls.temp.name)
        subprocess.run(['javac','--release','17','-d',str(cls.path),str(ROOT/'scripts/cdc461/Metrics461.java')],check=True,capture_output=True)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_non_diagnostic_scope_refused_before_attachment(self):
        r=subprocess.run(['java','--add-modules','jdk.attach','-cp',str(self.path),'Metrics461','production', '2','1'],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(r.returncode,0);self.assertIn('Scoped connector prefix required',r.stderr)
    def test_json_roundtrip_and_non_finite_values(self):
        source=self.path/'EncodeCheck.java';source.write_text('public class EncodeCheck {public static void main(String[] a) {System.out.println(Metrics461.json(java.util.Map.of("large",Long.MAX_VALUE,"nan",Double.NaN,"text","quote\\\"\\n")));}}')
        subprocess.run(['javac','--release','17','-cp',str(self.path),'-d',str(self.path),str(source)],check=True,capture_output=True)
        r=subprocess.run(['java','-cp',str(self.path),'EncodeCheck'],capture_output=True,text=True,check=True,timeout=10)
        value=json.loads(r.stdout);self.assertEqual(value['large'],9223372036854775807);self.assertIsNone(value['nan']);self.assertEqual(value['text'],'quote"\n')


if __name__=='__main__':unittest.main()
