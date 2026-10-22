import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('success408', ROOT / 'scripts/run-b408-success-version.py')
subject = importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)


class SuccessfulVersion(unittest.TestCase):
    def test_retry_uses_a_separate_predeclared_case(self):
        self.assertEqual(subject.CASES['success391-limited'], 7276913)
        self.assertEqual(subject.CASES['success391-limited-9mb'], 9000000)

    def test_only_snapshot_submission_is_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            old, legacy = subject.EVIDENCE, subject.LEGACY
            subject.EVIDENCE = subject.LEGACY = Path(d)
            template = Path(d) / 'terraform/v2/staging/files/capture-mysql-replication-source.sh'
            template.parent.mkdir(parents=True)
            template.write_text((ROOT / 'terraform/v2/staging/files/capture-mysql-replication-source.sh').read_text())
            calls, submitted = [], []
            def aws(*args):
                calls.append(args); return {'original': True}
            def raw(host, commands, **kwargs):
                submitted.append((host, commands, kwargs)); return 'snapshot-id'
            m = SimpleNamespace(ssm=SimpleNamespace(aws=aws), raw=raw,
                                wait=lambda *a, **k: {}, log=lambda *a, **k: None)
            try:
                subject.instrument(m, {'runId': '76a86985-db80-41a3-bc68-6631aa8ad437'})
                request = Path(d) / 'request.json'
                request.write_text(json.dumps({'DocumentName': 'AWS-RunShellScript'}))
                self.assertEqual(m.ssm.aws('ssm', 'send-command', '--cli-input-json', 'file://' + str(request)), {'original': True})
                self.assertEqual(len(submitted), 0)
                request.write_text(json.dumps({'DocumentName': 'yeodam-v2-staging-mysql-source-snapshot'}))
                result = m.ssm.aws('ssm', 'send-command', '--cli-input-json', 'file://' + str(request))
                self.assertEqual(result['Command']['CommandId'], 'snapshot-id')
                self.assertEqual(submitted[0][0], 'source')
                shell = submitted[0][1][-1]
                self.assertIn('--rate 7276913', shell)
                self.assertIn('--single-transaction', shell)
                self.assertIn('--source-data=2', shell)
                with self.assertRaises(RuntimeError):
                    m.ssm.aws('ssm', 'send-command', '--cli-input-json', 'file://' + str(request))
            finally:
                subject.EVIDENCE = old
                subject.LEGACY = legacy

    def test_modified_successful_checkout_is_refused_before_import(self):
        with patch.object(subject.subprocess, 'check_output', side_effect=['abc', 'abc', ' M scripts/runner.py']):
            with self.assertRaisesRegex(RuntimeError, 'pinned and clean'):
                subject.load()


if __name__ == '__main__':
    unittest.main()
