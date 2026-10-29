import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).parents[2]
SPEC=importlib.util.spec_from_file_location('diag461',ROOT/'scripts/diagnose-cdc461.py')
m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)


class ContractTests(unittest.TestCase):
    def setUp(self): self.c=json.loads((ROOT/'tests/load/fixtures/cdc461-contract.json').read_text())
    def test_exact_contract(self):m.validate(self.c)
    def test_poll50_changes_only_travel_poll(self):
        c=copy.deepcopy(self.c);c['tripPoll']=50;m.validate(c)
        configs=[{'consumer.override.max.poll.records':'1','batch.size':'1','tasks.max':'8'},
                 {'consumer.override.max.poll.records':'100','batch.size':'100'}]
        before=copy.deepcopy(configs);actual=m.poll_configs(configs,50)
        self.assertEqual(configs,before)
        self.assertEqual(actual[1],before[1])
        actual[0]['consumer.override.max.poll.records']='1'
        self.assertEqual(actual,before)
    def test_poll_overlay_refuses_changed_batch(self):
        with self.assertRaises(ValueError):m.poll_configs([{'consumer.override.max.poll.records':'1','batch.size':'50'},{}],50)
    def test_initialized_poll50_contract_is_deployed_not_baseline_fixture(self):
        class DeploymentObserved(Exception):pass
        contract=copy.deepcopy(self.c);contract['tripPoll']=50
        seen=[]
        class FakeRuntime:
            def __init__(self,directory):pass
            def aws(self,*args):return {'Account':contract['account']}
            def deploy(self,role,source,destination):
                seen.append(json.loads(source.read_text()))
                raise DeploymentObserved()
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);(directory/'contract.json').write_text(json.dumps(contract))
            with patch.object(m,'Runtime',FakeRuntime),patch.object(m,'DOCS',directory),self.assertRaises(DeploymentObserved):
                m.execute(directory,'00000000-0000-4000-8000-000000000000',contract)
        self.assertEqual(seen,[contract])
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


class IntegrationOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('poll463',ROOT/'scripts/cdc-poll463.py')
        cls.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.m)
    def test_only_travel_poll_changes_in_real_functions(self):
        text="def sink():\n return {'batch.size':'1','consumer.override.max.poll.records':'1','tasks.max':'8'}\ndef journal_sink():\n d=sink();d.update({'batch.size':'100','consumer.override.max.poll.records':'100'});return d\n"
        before={};after={};exec(text,before);exec(self.m.render_trip_poll(text),after)
        self.assertEqual(before['journal_sink'](),after['journal_sink']())
        changed=after['sink']();changed['consumer.override.max.poll.records']='1'
        self.assertEqual(before['sink'](),changed)
    def test_ambiguous_or_changed_template_refused(self):
        for text in ['pass',"a={'batch.size':'1','consumer.override.max.poll.records':'1'}\nb={'batch.size':'1','consumer.override.max.poll.records':'1'}"]:
            with self.assertRaises(ValueError):self.m.render_trip_poll(text)


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
