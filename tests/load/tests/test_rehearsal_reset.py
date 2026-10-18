import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('reset',Path(__file__).parents[3]/'scripts/staging-rehearsal-reset.py')
reset=importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset)
RUN='11111111-1111-4111-a111-111111111111'
OLD='22222222-2222-4222-a222-222222222222'

class ResetSafetyTest(unittest.TestCase):
    def args(self,action):return SimpleNamespace(action=action,reset_id=RUN,previous_run=OLD,target_proof='')
    def test_unfenced_target_never_dumps(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(reset,'BASE',Path(directory)), patch.object(reset.os,'geteuid',return_value=0), patch.object(reset,'identity',return_value=['2','target','0','0']), patch.object(reset,'run',side_effect=['10.30.10.28','483175530259']), patch.object(reset,'stream') as dump:
            with self.assertRaisesRegex(RuntimeError,'fenced'):reset.execute(self.args('snapshot-target'))
            dump.assert_not_called()
    def test_other_incomplete_reset_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(reset,'BASE',Path(directory)), patch.object(reset.os,'geteuid',return_value=0), patch.object(reset,'identity',return_value=['1','source','1','1']), patch.object(reset,'run',side_effect=['10.30.0.174','483175530259']), patch.object(reset,'stream') as dump:
            (Path(directory)/'active.json').write_text('{"resetId":"other","phase":"paused"}')
            with self.assertRaisesRegex(RuntimeError,'Another reset'):reset.execute(self.args('pause'))
            dump.assert_not_called()
    def test_checksum_streams_full_file(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'dump';data=b'rows'*100000;path.write_bytes(data)
            self.assertEqual(reset.sha_file(path),hashlib.sha256(data).hexdigest())
    def test_initialize_refences_destination_before_validating_snapshot(self):
        class DB:
            def __init__(self,gtid):self.gtid=gtid;self.calls=[]
            def identity(self,role):
                self.calls.append(('identity',role));return ['2','target','1','1']
            def query(self,role,sql,**kwargs):
                self.calls.append(('query',sql))
                if 'gtid_executed' in sql:return self.gtid
                if 'innodb_trx' in sql:return '0'
                if 'persisted_variables' in sql:return 'ON'
                return ''
        expected={'targetUuid':'target','gtid':'snapshot-gtid'}
        db=DB('snapshot-gtid')
        reset.protect_target_for_initialize(db,expected)
        self.assertIn('SET PERSIST super_read_only=ON',db.calls[0][1])
        db=DB('unexpected-write')
        with self.assertRaisesRegex(RuntimeError,'changed after preserved snapshot'):
            reset.protect_target_for_initialize(db,expected)
        self.assertIn('SET PERSIST super_read_only=ON',db.calls[0][1])

if __name__=='__main__':unittest.main()
