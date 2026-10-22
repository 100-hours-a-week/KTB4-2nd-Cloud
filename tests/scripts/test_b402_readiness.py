import importlib.util,json,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]
def load():
    kafka=types.ModuleType('confluent_kafka');kafka.Consumer=Mock();kafka.Producer=Mock();kafka.TopicPartition=Mock()
    spec=importlib.util.spec_from_file_location('handoff402',ROOT/'scripts/b-integration386-handoff.py');m=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,{'confluent_kafka':kafka}):spec.loader.exec_module(m)
    return m
RUN='bd454484-bf72-4ef2-b98c-fbef08d99b41';KEY=bytes(range(32))
def message(m,value):
    value=dict(value);value['signature']=m.signature(value,KEY)
    msg=Mock();msg.error.return_value=None;msg.value.return_value=json.dumps(value).encode();return msg

class ReadinessTests(unittest.TestCase):
    def test_prepared_signal_still_checks_auth_projection_readiness(self):
        m=load();control=Mock();control.ensure_prepared.side_effect=[RuntimeError('projection pending'),None]
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/RUN;p.mkdir();(p/'cutover-ready').touch()
            with patch.object(m,'ROOT',Path(tmp)),patch.object(m,'SourceFileLoader') as loader,patch.object(m.time,'time',return_value=100),patch.object(m.time,'sleep'):
                loader.return_value.load_module.return_value=control
                self.assertEqual(m.wait_prepared(RUN,360),100)
                self.assertEqual(control.ensure_prepared.call_count,2)

    def test_preparation_timeout_never_fences_or_publishes(self):
        m=load()
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'ROOT',Path(tmp)),patch.object(m,'wait_prepared',side_effect=RuntimeError('not prepared')),patch.object(m,'publish_start') as publish,patch.object(m.subprocess,'run') as execute:
            m.source(RUN,360,True)
            self.assertFalse(json.loads((Path(tmp)/RUN/'handoff-result.json').read_text())['switched'])
            execute.assert_not_called();publish.assert_not_called()

    def test_signed_start_requires_same_run_kind_and_original_deadline(self):
        m=load();client=Mock()
        good={'runId':RUN,'kind':'cutover_start','fenceAt':100}
        with patch.object(m.time,'time',return_value=110):
            client.poll.return_value=message(m,good)
            self.assertEqual(m.receive_start(client,RUN,360,KEY),100)
            for changed in ({**good,'runId':'other'},{**good,'kind':'other'},{**good,'fenceAt':400},{**good,'fenceAt':50}):
                client.poll.return_value=message(m,changed)
                with self.assertRaises(RuntimeError):m.receive_start(client,RUN,360,KEY)
            client.poll.return_value=message(m,good)
            with self.assertRaises(RuntimeError):m.receive_start(client,RUN,360,KEY[::-1])

    def test_ready_handoff_uses_one_budget_and_skips_own_signed_start(self):
        m=load();client=Mock()
        client.poll.side_effect=[message(m,{'runId':RUN,'kind':'cutover_start','fenceAt':100}),message(m,{'runId':RUN,'equal':True,'finishedAt':120})]
        original=Path.read_text
        def read(path,*a,**kw):
            if str(path)=='/run/yeodam-cdc-auth368/aes.hex':return KEY.hex()
            return original(path,*a,**kw)
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'ROOT',Path(tmp)),patch.object(m,'Consumer',return_value=client),patch.object(m,'wait_prepared',return_value=100),patch.object(m,'publish_start') as publish,patch.object(m.time,'time',return_value=125),patch.object(Path,'read_text',read),patch.object(m,'store') as store,patch.object(m.subprocess,'run',return_value=Mock(returncode=0)) as execute:
            m.source(RUN,360,True)
            result=store.call_args.args[1]
            self.assertTrue(result['switched']);self.assertTrue(result['activationWithinBudget'])
            self.assertEqual(result['cdcDeadlineAt'],145);self.assertEqual(result['activationDeadlineAt'],150)
            self.assertEqual(result['latestFenceAt'],360)
            publish.assert_called_once_with(RUN,100)
            self.assertIn('--budget-started-at',execute.call_args_list[0].args[0])
            self.assertEqual(execute.call_count,3)

    def test_budget_expired_proof_never_promotes(self):
        m=load();client=Mock();client.poll.return_value=message(m,{'runId':RUN,'equal':True,'finishedAt':146})
        original=Path.read_text
        def read(path,*a,**kw):
            if str(path)=='/run/yeodam-cdc-auth368/aes.hex':return KEY.hex()
            return original(path,*a,**kw)
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'ROOT',Path(tmp)),patch.object(m,'Consumer',return_value=client),patch.object(m,'wait_prepared',return_value=100),patch.object(m,'publish_start'),patch.object(m.time,'time',return_value=125),patch.object(Path,'read_text',read),patch.object(m,'store') as store,patch.object(m.subprocess,'run',return_value=Mock(returncode=0)) as execute:
            m.source(RUN,360,True)
            self.assertFalse(store.call_args.args[1]['switched']);self.assertEqual(execute.call_count,1)

if __name__=='__main__':unittest.main()
