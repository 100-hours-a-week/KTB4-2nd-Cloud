import importlib.util,sys,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]

def load():
    dummy=types.ModuleType('confluent_kafka');dummy.Consumer=Mock()
    dummy.TopicPartition=lambda topic,partition,offset=0:types.SimpleNamespace(topic=topic,partition=partition,offset=offset)
    spec=importlib.util.spec_from_file_location('boundary388',ROOT/'scripts/b-integration383-cutover.py')
    m=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,{'confluent_kafka':dummy}):spec.loader.exec_module(m)
    return m

class BoundaryTests(unittest.TestCase):
    def case(self,committed):
        m=load();clients=[Mock() for _ in range(5)]
        for c in clients[:3]:
            c.list_topics.return_value=types.SimpleNamespace(topics={'trip':types.SimpleNamespace(error=None,partitions={0:None})})
            c.get_watermark_offsets.return_value=(0,10)
            c.committed.return_value=[types.SimpleNamespace(partition=0,offset=committed)]
        clients[4].committed.return_value=[types.SimpleNamespace(partition=i,offset=i+5) for i in range(8)]
        def prepare(c,topic):return [m.TopicPartition(topic,0)]
        def api(path,method=None):
            if path.endswith('/status'):return {'connector':{'state':'STOPPED'},'tasks':[]}
            if path.endswith('/offsets'):return {'offsets':[{'offset':{'gtids':'uuid:1-10'}}]}
        return m,clients,prepare,api
    def test_nonzero_frozen_tail_retains_failure_stage_offsets_and_fixed_deadline(self):
        m,clients,prepare,api=self.case(7);clock=[100.0]
        with patch.object(m,'consumer',side_effect=clients),patch.object(m,'prepare',side_effect=prepare),patch.object(m,'marker_seen'),patch.object(m,'api',side_effect=api) as calls,patch.object(m.time,'time',side_effect=lambda:clock[0]),patch.object(m.time,'sleep',side_effect=lambda _:clock.__setitem__(0,clock[0]+10)):
            with self.assertRaises(m.BoundaryError) as caught:m.cutover('run','standby',cutover_at=100)
        proof=caught.exception.proof('run')
        self.assertEqual(proof['failureCode'],'FROZEN_TAIL_DEADLINE')
        self.assertEqual(proof['stage'],'frozen_tail')
        self.assertEqual(proof['deadlineAt'],145)
        self.assertEqual(proof['groups'][0]['current'],{0:7})
        self.assertEqual(proof['groups'][0]['end'],{0:10})
        self.assertFalse(any('-sink/stop' in c.args[0] for c in calls.call_args_list))
    def test_validated_proof_is_published_before_readonly_client_teardown(self):
        m,clients,prepare,api=self.case(10);published=[]
        for c in clients:c.close.side_effect=lambda:self.assertEqual(len(published),1)
        with patch.object(m,'consumer',side_effect=clients),patch.object(m,'prepare',side_effect=prepare),patch.object(m,'marker_seen'),patch.object(m,'api',side_effect=api),patch.object(m.time,'time',return_value=101):
            proof=m.cutover('run','standby',cutover_at=100,on_proof=published.append)
        self.assertEqual(proof['requestHandoff']['committed'],{i:i+5 for i in range(8)})
        self.assertTrue(proof['equal']);self.assertEqual(published,[proof])
        for c in clients:c.close.assert_called_once()
    def test_missing_eighth_request_offset_rejects_handoff_before_stopping_sources(self):
        m,clients,prepare,api=self.case(10)
        clients[4].committed.return_value=[types.SimpleNamespace(partition=i,offset=5) for i in range(7)]
        with patch.object(m,'consumer',side_effect=clients),patch.object(m,'prepare',side_effect=prepare),patch.object(m,'marker_seen'),patch.object(m,'api',side_effect=api) as calls:
            with self.assertRaises(m.BoundaryError) as caught:m.cutover('run','standby',cutover_at=100)
        self.assertEqual(caught.exception.proof('run')['failureCode'],'SOURCE_REQUEST_OFFSET_MISSING')
        self.assertFalse(any('/stop' in c.args[0] for c in calls.call_args_list))
    def test_frozen_tail_poll_reuses_prepared_metadata_and_end_offsets(self):
        m=load();c=Mock();parts=[m.TopicPartition('trip',0)];c.committed.return_value=[types.SimpleNamespace(partition=0,offset=10)]
        equal,_=m.caught(c,'trip',parts,{0:10})
        self.assertTrue(equal);c.list_topics.assert_not_called();c.get_watermark_offsets.assert_not_called()
if __name__=='__main__':unittest.main()
