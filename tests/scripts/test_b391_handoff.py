import importlib.util,json,sys,tempfile,unittest,time
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]
def load(file):
 spec=importlib.util.spec_from_file_location('b391_'+file.replace('-','_'),ROOT/'scripts'/file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class SourceDrainTests(unittest.TestCase):
 def test_active_direct_or_unstopped_listener_cannot_freeze_database(self):
  for active,stopped in [(1,True),(0,False)]:
   with self.subTest(active=active,stopped=stopped):
    m=load('b-integration376-control.py');run='26b45888-9b29-4afb-b090-ff2acbfc3ab6';clock=[100.0]
    db=Mock();db.identity.side_effect=lambda role:['1' if role=='source' else '2','uuid',*(['0','0'] if role=='source' else ['1','0'])]
    app=Mock();app.source_control.return_value={'instance':'same','runId':run,'fencing':True,'activeDirect':active,'sourceConsumerStopped':stopped,'sourceConsumerRunning':not stopped}
    with tempfile.TemporaryDirectory() as d,patch.object(m,'BASE',Path(d)),patch.object(m,'database',return_value=db),patch.object(m,'source_app',return_value=app),patch.object(m,'ensure_prepared'),patch.object(m.subprocess,'check_output',return_value='10.30.0.174'),patch.object(m.time,'time',side_effect=lambda:clock[0]),patch.object(m.time,'sleep',side_effect=lambda _:clock.__setitem__(0,clock[0]+20)),patch.object(sys,'argv',['control','fence','--run-id',run,'--at','100']):
     with self.assertRaisesRegex(RuntimeError,'not frozen'):m.main()
     self.assertFalse(any('SET PERSIST' in c.args[1] for c in db.query.call_args_list))
     self.assertEqual(json.loads((Path(d)/(run+'.json')).read_text())['phase'],'write_drain_failed')
 def test_target_does_not_join_trip_group_before_handoff(self):
  m=load('b-integration376-app.py');run='26b45888-9b29-4afb-b090-ff2acbfc3ab6';payloads=[]
  base={'Config':{'Image':'migration-probe-v1-sha-816d69ba','Env':[]},'State':{'Running':True}}
  def docker(method,path,data=None):
   if path.startswith('/containers/create'):payloads.append(data)
   return {}
  with patch.object(m,'inspect',return_value=base),patch.object(m,'docker',side_effect=docker):m.create(run,True)
  env=dict(v.split('=',1) for v in payloads[0]['Env'])
  self.assertEqual(env['MIGRATION_TRIP_CONSUMER_ENABLED'],'false');self.assertEqual(env['MIGRATION_CONSUMER_DEFER_TRIP'],'true')
  self.assertEqual(env['MIGRATION_TRIP_APPLY_ROLE'],'target')
  self.assertEqual(env['MIGRATION_SERVICE_SCHEDULERS_ENABLED'],'false')
  self.assertEqual(env['MIGRATION_TRIP_CONCURRENCY'],'8')
 def test_journal_uses_its_own_key_and_bounded_pool(self):
  m=load('b-integration376-kafka.py');run='26b45888-9b29-4afb-b090-ff2acbfc3ab6'
  source=m.source(run,'trips',0);sink=m.journal_sink(run)
  self.assertIn('yeodam_v1_source.migration_trip_request_result',source['table.include.list'])
  self.assertNotIn('migration_trip_request_result',source['predicates.isTrip.pattern'])
  self.assertTrue(sink['topics'].endswith('.yeodam_v1_source.migration_trip_request_result'))
  self.assertEqual(sink['primary.key.mode'],'record_key');self.assertEqual(sink['connection.pool.max_size'],'1')
  self.assertEqual(sink['batch.size'],'100');self.assertEqual(sink['tasks.max'],'1')
if __name__=='__main__':unittest.main()
