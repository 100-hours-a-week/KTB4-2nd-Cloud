import importlib.util
from pathlib import Path
import sys,types,unittest
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]
def load():
    lib=types.ModuleType('confluent_kafka');lib.Consumer=Mock();lib.Producer=Mock();lib.TopicPartition=lambda topic,partition:types.SimpleNamespace(topic=topic,partition=partition)
    spec=importlib.util.spec_from_file_location('handoff406',ROOT/'scripts/b-integration386-handoff.py');m=importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,{'confluent_kafka':lib}):spec.loader.exec_module(m)
    return m
class FixedWatermark(unittest.TestCase):
    def test_single_capture_and_accepts_offsets_beyond_frontier(self):
        m=load();clients=[]
        for _ in range(3):
            c=Mock();c.list_topics.return_value.topics={'staging.yeodam.migration.b391.r.trips':types.SimpleNamespace(error=None,partitions={0:None}),'staging.yeodam.migration.b391.r.yeodam_v1_source.migration_trip_request_result':types.SimpleNamespace(error=None,partitions={0:None}),'staging.yeodam.migration.auth368.b391-r-encrypted':types.SimpleNamespace(error=None,partitions={0:None})}
            c.get_watermark_offsets.return_value=(0,10)
            c.committed.side_effect=[[types.SimpleNamespace(partition=0,offset=9)],[types.SimpleNamespace(partition=0,offset=11)]]
            clients.append(c)
        with patch.object(m,'Consumer',side_effect=clients),patch.object(m,'wait_prepared'),patch.object(m.time,'time',return_value=100),patch.object(m.time,'sleep'),patch.object(m,'store') as store:
            self.assertEqual(m.wait_cdc_watermark('r',360),100)
            self.assertTrue(store.call_args.args[1]['ready'])
        for c in clients:
            c.get_watermark_offsets.assert_called_once();c.subscribe.assert_not_called();c.assign.assert_not_called();c.poll.assert_not_called();c.commit.assert_not_called();c.close.assert_called_once()
    def test_timeout_leaves_source_unfenced(self):
        m=load()
        with patch.object(m,'wait_prepared'),patch.object(m.time,'time',return_value=360),patch.object(m,'store'),patch.object(m,'Consumer',return_value=Mock()) as clients:
            with self.assertRaises(RuntimeError):m.wait_cdc_watermark('r',360)
    def test_new_candidate_is_explicit(self):
        spec=importlib.util.spec_from_file_location('runner406',ROOT/'scripts/b-integration376-run.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        self.assertEqual(m.handoff_flags({'candidate':'finite-watermark'}),' --when-ready --finite-watermark')
        self.assertEqual(m.handoff_flags({}),'')
        self.assertEqual(m.handoff_flags({'candidate':'readiness-cutover'}),' --when-ready')
if __name__=='__main__':unittest.main()
