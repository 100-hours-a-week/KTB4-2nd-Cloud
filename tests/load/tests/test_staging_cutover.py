import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import unittest

spec = importlib.util.spec_from_file_location('cutover', Path(__file__).parents[3]/'scripts/staging-db-cutover.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
RUN = '11111111-1111-4111-a111-111111111111'

class DatabaseFailureModel:
    target = '10.30.10.28'
    deadline = None
    def __init__(self, failure=None):
        self.failure, self.calls = failure, []
        self.flags = {'source':['0','0'], 'target':['0','0']}
        self.replica_running = 'Yes'
        self.rule = 30
    def identity(self, role): return ['1' if role=='source' else '2',role]+self.flags[role]
    def status(self): return {'Relay_Source_Log_File':'binlog.000005','Exec_Source_Log_Pos':'100','Replica_IO_Running':self.replica_running,'Replica_SQL_Running':self.replica_running,'Last_IO_Errno':'0','Last_SQL_Errno':'0'}
    def query(self, role, sql, **kwargs):
        self.calls.append((role,sql))
        if 'SOURCE_POS_WAIT' in sql: return '-1' if self.failure=='lag' else '0'
        if 'SHOW BINARY' in sql: return 'binlog.000005\t100\t\t'
        if 'Active_Transactions' in sql or 'innodb_trx' in sql: return '0'
        if 'SET PERSIST super_read_only=ON' in sql: self.flags[role]=['1','1']
        if 'STOP REPLICA' in sql: self.replica_running='No'
        if 'START REPLICA' in sql: self.replica_running='Yes'
        if 'SET PERSIST read_only=OFF' in sql:
            self.flags[role]=['0','0']
            if role=='target' and self.failure=='lost-promotion-response':
                raise RuntimeError('Lost acknowledgement AFTER target became writable')
        if 'SELECT destination_hostgroup' in sql: return str(self.rule)
        return ''
    def route(self, group): self.rule=group
    def own_target_gtid(self, uuid): return 'target:1' if self.failure=='unexpected-target-write' else ''

class HandoffUnderTest(module.Handoff):
    def check(self):
        return {'sourceUuid':'source','targetUuid':'target','targetFlags':['0','0'],'targetOwnGtid':'', 'sourcePosition':['binlog.000005',100]}

class HandoffSafetyTest(unittest.TestCase):
    def test_normal_handoff_leaves_one_writer_and_durable_intent(self):
        with tempfile.TemporaryDirectory() as name:
            db=DatabaseFailureModel(); path=Path(name)/'active.json'
            handoff=HandoffUnderTest(db,path,RUN,8); handoff.cutover()
            self.assertEqual(db.flags, {'source':['1','1'],'target':['0','0']})
            state=json.loads(path.read_text())
            self.assertTrue(state['promotionPossible'])
            self.assertEqual(state['phase'],'switched')
            self.assertLess([v['phase'] for v in state['events']].index('replica_stopped'),[v['phase'] for v in state['events']].index('promotion_intent'))
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_replica_catchup_timeout_restores_only_v1(self):
        with tempfile.TemporaryDirectory() as name:
            db=DatabaseFailureModel('lag'); h=HandoffUnderTest(db,Path(name)/'active.json',RUN,8)
            with self.assertRaises(RuntimeError): h.cutover()
            self.assertEqual(h.state['phase'],'aborted')
            self.assertEqual(db.flags, {'source':['0','0'],'target':['1','1']})
            self.assertFalse(h.state['promotionPossible'])
            self.assertFalse(any(role=='target' and 'read_only=OFF' in sql for role,sql in db.calls))

    def test_lost_promotion_response_never_reopens_old_writer(self):
        with tempfile.TemporaryDirectory() as name:
            db=DatabaseFailureModel('lost-promotion-response'); h=HandoffUnderTest(db,Path(name)/'active.json',RUN,8)
            with self.assertRaises(RuntimeError): h.cutover()
            self.assertEqual(h.state['phase'],'hold_after_promotion')
            self.assertEqual(db.flags['source'],['1','1'])
            with self.assertRaises(RuntimeError): h.abort()
            self.assertFalse(any(role=='source' and 'read_only=OFF' in sql for role,sql in db.calls))
            h.finish()
            self.assertEqual(h.state['phase'],'switched')
            self.assertEqual(db.flags['source'],['1','1'])

    def test_position_validation_and_rotation(self):
        self.assertTrue(module.caught_up({'Relay_Source_Log_File':'binlog.000006','Exec_Source_Log_Pos':'4'},['binlog.000005',100]))
        self.assertFalse(module.caught_up({'Relay_Source_Log_File':'binlog.000005','Exec_Source_Log_Pos':'99'},['binlog.000005',100]))
        with self.assertRaises(RuntimeError): module.position("bad';DROP TABLE users\t100")

    def test_gtid_newline_is_escaped_in_batch_status(self):
        row=module.replica_status('Channel_Name\tExecuted_Gtid_Set\nv1_to_v2\tuuid1:1-2,\\nuuid2:1-3')
        self.assertEqual(row['Channel_Name'],'v1_to_v2')

    def test_command_keeps_empty_trailing_status_columns(self):
        value=module.command([sys.executable,'-c',"print('Channel_Name\\tGTID\\tBlank\\nv1_to_v2\\tuuid1:1-2\\t')"])
        self.assertEqual(module.replica_status(value)['Blank'],'')

    def test_unexpected_target_write_blocks_promotion_and_source_reopen(self):
        with tempfile.TemporaryDirectory() as name:
            db=DatabaseFailureModel('unexpected-target-write'); h=HandoffUnderTest(db,Path(name)/'active.json',RUN,8)
            with self.assertRaises(RuntimeError): h.cutover()
            self.assertEqual(h.state['phase'],'hold_before_promotion')
            self.assertEqual(db.flags['source'],['1','1'])

if __name__=='__main__': unittest.main()
