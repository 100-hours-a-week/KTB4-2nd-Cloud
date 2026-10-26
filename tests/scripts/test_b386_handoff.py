import importlib.util,json,sys,tempfile,types,unittest,time
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]
def load():
    module=types.ModuleType('confluent_kafka')
    module.Consumer=Mock();module.Producer=Mock();module.TopicPartition=Mock()
    spec=importlib.util.spec_from_file_location('handoff386',ROOT/'scripts/b-integration386-handoff.py')
    value=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,{'confluent_kafka':module}):spec.loader.exec_module(value)
    return value
class HandoffTests(unittest.TestCase):
    def test_proof_signature_covers_run_offsets_and_success(self):
        m=load();key=bytes(range(32));proof={'runId':'a','equal':True,'sourceOffsets':{'trips':{'gtids':'uuid:1-20'}}}
        signed=m.signature(proof,key)
        for modified in [{**proof,'runId':'b'},{**proof,'equal':False},{**proof,'sourceOffsets':{}}]:
            self.assertNotEqual(signed,m.signature(modified,key))
        self.assertNotEqual(signed,m.signature(proof,bytes(reversed(key))))
    def test_forged_proof_never_promotes_or_enables_writes(self):
        m=load();run='26b45888-9b29-4afb-b090-ff2acbfc3ab6';message=Mock()
        message.error.return_value=None;message.value.return_value=json.dumps({'runId':run,'equal':True,'signature':'forged'}).encode()
        client=Mock();client.poll.return_value=message
        def read(path,*args,**kwargs):
            if str(path)=='/run/yeodam-cdc-auth368/aes.hex':return bytes(range(32)).hex()
            return original(path,*args,**kwargs)
        original=Path.read_text
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'ROOT',Path(tmp)),patch.object(m,'Consumer',return_value=client),patch.object(m.subprocess,'run',return_value=Mock(returncode=0)) as execute,patch.object(Path,'read_text',read):
            m.source(run,time.time())
            result=json.loads((Path(tmp)/run/'handoff-result.json').read_text())
            self.assertFalse(result['switched'])
            self.assertEqual(len(execute.call_args_list),1)
            self.assertIn('fence',execute.call_args.args[0])
if __name__=='__main__':unittest.main()
