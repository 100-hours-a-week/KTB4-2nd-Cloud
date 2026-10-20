import importlib.util,json,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch,Mock
ROOT=Path(__file__).resolve().parents[2]
def load(name,file):
 spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
class CutoverTests(unittest.TestCase):
 def test_read_transactions_do_not_prevent_fence_or_trigger_full_hash_audit(self):
  m=load('control383','b-integration376-control.py');run='26b45888-9b29-4afb-b090-ff2acbfc3ab6'
  db=Mock();db.identity.side_effect=lambda role:['1' if role=='source' else '2','uuid',*(['0','0'] if role=='source' else ['1','0'])]
  def query(role,sql,**kw):
   if 'information_schema.innodb_trx' in sql:return '0\t9\t0'
   if 'SHOW BINARY LOG STATUS' in sql:return 'binlog.000010\t100'
   return ''
  db.query.side_effect=query
  with tempfile.TemporaryDirectory() as d,patch.object(m,'BASE',Path(d)),patch.object(m,'database',return_value=db),patch.object(m,'ensure_prepared'),patch.object(m.subprocess,'check_output',return_value='10.30.0.174'),patch.object(m.subprocess,'run'),patch.object(m,'audit',side_effect=AssertionError('Full hash on critical path')),patch.object(sys,'argv',['control','fence','--run-id',run]):
   m.main();state=json.loads((Path(d)/(run+'.json')).read_text())
   self.assertEqual(state['phase'],'source_fenced');self.assertEqual([e['phase'] for e in state['events']],['fence_intent','requests_queued','source_read_only','source_fenced'])
   self.assertTrue(any('INSERT INTO' in c.args[1] for c in db.query.call_args_list))
 def test_auth_handoff_rejects_old_rows_and_accepts_new_and_rotated_binlog(self):
  dummy=types.ModuleType('cdc_auth368')
  for k in ['apply_change','decrypt_event','parse_change']:setattr(dummy,k,Mock())
  with patch.dict(sys.modules,{'cdc_auth368':dummy}):m=load('auth383','b-integration376-auth.py')
  boundary={'file':'binlog.000010','pos':100}
  for file,pos,expected in [('binlog.000009',9999,False),('binlog.000010',100,False),('binlog.000010',101,True),('binlog.000011',4,True)]:
   self.assertEqual(m.after_boundary({'source':{'file':file,'pos':pos}},boundary),expected)
  with self.assertRaises(RuntimeError):m.after_boundary({'source':{'file':'other.000010','pos':101}},boundary)
if __name__=='__main__':unittest.main()
