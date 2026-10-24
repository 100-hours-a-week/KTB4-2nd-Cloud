import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('baseline408', ROOT / 'scripts/b-integration408-baseline.py')
subject = importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)
BASELINE = '72d49f82-ff47-46bd-9b12-0dd307a68fb2'


class BaselineSafety(unittest.TestCase):
    def test_historical_digest_mismatch_preserves_current_database(self):
        with tempfile.TemporaryDirectory() as directory:
            module = SimpleNamespace(query=Mock(side_effect=['1\t1\t1', '0', '0']),
                                     transfer=lambda source, target: Path(target).write_bytes(b'wrong'),
                                     sha=Mock(return_value='0' * 64), snapshot=Mock())
            with self.assertRaisesRegex(RuntimeError, 'Historical archive digest differs'):
                subject.adopt_historical(module, BASELINE, Path(directory))
            module.snapshot.assert_not_called()
            self.assertTrue(all('SET ' not in call.args[1] and 'DROP ' not in call.args[1]
                                for call in module.query.call_args_list))

    def test_wrong_roles_refuse_before_database_mutation(self):
        module = SimpleNamespace(query=Mock(return_value='1\t0\t0'))
        with self.assertRaisesRegex(RuntimeError, 'fenced'):
            subject.protected(module, 'source')
        self.assertEqual(module.query.call_count, 1)

    def test_marker_cannot_select_another_archive(self):
        value = {'baselineId': BASELINE, 'bucket': subject.BUCKET, 'key': 'other/archive.sql.gz',
                 'sha256': '0' * 64, 'fingerprint': {'trips': 1}}
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            subject.validate_marker(value, BASELINE)

    def test_null_checksum_cannot_claim_matching_data(self):
        module = SimpleNamespace(query=Mock(side_effect=['trips', subject.SCHEMA + '.trips\tNULL']))
        with self.assertRaisesRegex(RuntimeError, 'checksum unavailable'):
            subject.fingerprint(module, 'source')

    def test_archive_mismatch_stops_before_preservation_or_drop(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory); (directory / 'baseline.sql.gz').write_bytes(b'wrong')
            marker = {'baselineId': BASELINE, 'bucket': subject.BUCKET,
                      'key': 'migration/rehearsal-reset/' + BASELINE + '/b408-baseline.sql.gz',
                      'sha256': '0' * 64, 'bytes': 5, 'fingerprint': {'trips': 1}}
            def transfer(source, target):Path(target).write_text(json.dumps(marker))
            module = SimpleNamespace(query=Mock(side_effect=['1\t1\t1', '0', '0']),
                                     transfer=transfer, sha=lambda _: '1' * 64, snapshot=Mock())
            with self.assertRaisesRegex(RuntimeError, 'digest mismatch'):
                subject.restore(module, BASELINE, 'control', 'source', directory)
            module.snapshot.assert_not_called()
            self.assertTrue(all('SET ' not in call.args[1] and 'DROP ' not in call.args[1]
                                for call in module.query.call_args_list))


if __name__ == '__main__':unittest.main()
