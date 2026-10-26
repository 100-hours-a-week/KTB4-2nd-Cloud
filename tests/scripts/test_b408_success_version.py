import importlib.util
import json
import ast
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

    def test_paired_input_requires_both_matching_restores(self):
        baseline = {'baselineId': 'id', 'sha256': 'digest', 'fingerprint': {'rows': 1}}
        plan = {'commonBaseline': baseline, 'pairedRestoreAttempt': 'control'}
        receipt = [{'role': role, 'attempt': 'control', 'baselineId': 'id', 'baselineSha256': 'digest', 'fingerprint': {'rows': 1}}
                   for role in ('source', 'target')]
        subject.validate_pair_receipt(plan, receipt)
        with self.assertRaisesRegex(RuntimeError, 'Both'):
            subject.validate_pair_receipt(plan, receipt[:1])
        receipt[1]['attempt'] = 'limited'
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            subject.validate_pair_receipt(plan, receipt)
        receipt[1]['attempt'] = 'control'
        receipt[1]['fingerprint']['rows'] = 2
        with self.assertRaisesRegex(RuntimeError, 'differs'):
            subject.validate_pair_receipt(plan, receipt)
        self.assertEqual(subject.CASES['paired391-unlimited-30'], 0)
        self.assertEqual(subject.CASES['paired391-limited-30'], 9000000)

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

    def test_bulk_wait_releases_naturally_and_timeout_prevents_import(self):
        template = (ROOT / 'scripts/b-integration376-bulk.py').read_text()
        patched = subject.render_bulk(template)
        # The import/backup body is byte-identical after the original guard.
        tail = "    if mysql(\"SELECT COUNT(*) FROM performance_schema.replication_connection_status"
        self.assertEqual(patched[patched.index(tail):], template[template.index(tail):])
        module = ast.parse(patched)
        fn = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'import_snapshot')
        namespace = {'json': json, 'time': SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None)}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<bulk>', 'exec'), namespace)
        answers = iter(['2\t1\t1', '1', '0'])
        def mysql(query):
            try: return next(answers)
            except StopIteration: raise ValueError('Reached unchanged native replication guard')
        namespace['mysql'] = mysql
        with self.assertRaisesRegex(ValueError, 'unchanged native'):
            namespace['import_snapshot']('run', 'marker')
        clock = iter([0, 46])
        namespace['time'] = SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None)
        namespace['mysql'] = lambda q: '2\t1\t1' if '@@server_id' in q else '1'
        with self.assertRaisesRegex(RuntimeError, 'import not started'):
            namespace['import_snapshot']('run', 'marker')

    def test_modified_successful_checkout_is_refused_before_import(self):
        with patch.object(subject.subprocess, 'check_output', side_effect=['abc', 'abc', ' M scripts/runner.py']):
            with self.assertRaisesRegex(RuntimeError, 'pinned and clean'):
                subject.load()


if __name__ == '__main__':
    unittest.main()
