import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from v3_workflow_trace import BOUNDARIES, inspect_trace, read_tempo, trace_id, trial


class Evidence(unittest.TestCase):
    def fixture(self):
        tid = '1' * 32
        services = ['photo-converter', 'trip-result-worker', 'ai-api', 'ai-runner', 'ai-api', 'trip-result-worker']
        batches = []
        for i, (name, service) in enumerate(zip(BOUNDARIES, services), 1):
            span = {'traceId':tid,'spanId':f'{i:016x}','name':name,'attributes':[{'secret':'must-not-export'}]}
            if i > 1:span['parentSpanId'] = f'{i-1:016x}'
            batches.append({'resource':{'attributes':[{'key':'service.name','value':{'stringValue':service}}]},
                            'scopeSpans':[{'spans':[span]}]})
        return tid, {'batches':batches}

    def test_complete_parent_path_and_no_arbitrary_payload(self):
        tid, body = self.fixture();result = inspect_trace(body, tid)
        self.assertTrue(result['tracePathComplete']);self.assertNotIn('must-not-export', str(result))

    def test_present_names_without_parent_path_is_not_complete(self):
        tid, body = self.fixture();body['batches'][3]['scopeSpans'][0]['spans'][0].pop('parentSpanId')
        self.assertFalse(inspect_trace(body, tid)['tracePathComplete'])

    def test_missing_trace_and_boundary(self):
        tid, body = self.fixture();self.assertEqual(inspect_trace(None, tid)['status'], 'missing_trace')
        body['batches'].pop();self.assertFalse(inspect_trace(body, tid)['tracePathComplete'])

    def test_invalid_graph_and_mixed_trace_rejected(self):
        tid, body = self.fixture()
        for field, value in [('traceId','2'*32),('parentSpanId','0000000000000001')]:
            bad = copy.deepcopy(body);bad['batches'][0]['scopeSpans'][0]['spans'][0][field] = value
            with self.assertRaises(ValueError):inspect_trace(bad, tid)

    def test_sampled_trial_only_and_no_existing_context_rewrite(self):
        manifest = trial();self.assertEqual(len(trace_id(manifest)), 32)
        manifest['traceparent'] = manifest['traceparent'][:-2] + '00'
        with self.assertRaises(ValueError):trace_id(manifest)

    def test_external_or_credential_url_rejected_before_network(self):
        for url in ['http://169.254.169.254','https://example.com','http://user@localhost:3200','http://localhost/path']:
            with self.assertRaises(ValueError):read_tempo(url, '1'*32)

    def test_embedded_runtime_probe_programs_compile(self):
        import ast
        path = Path(__file__).resolve().parent / 'smoke-v3-apm.py'
        assignments = [node for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Assign)]
        checked = set()
        for node in assignments:
            names = {target.id for target in node.targets if isinstance(target, ast.Name)} & {'code', 'cross', 'lookup'}
            if not names:continue
            value = node.value
            if isinstance(value, ast.Call):value = value.func.value
            compile(ast.literal_eval(value), str(path) + ':' + next(iter(names)), 'exec')
            checked.update(names)
        self.assertEqual(checked, {'code', 'cross', 'lookup'})

    def test_private_manifest_refuses_overwrite_and_public_parent(self):
        import importlib.util, tempfile, os
        spec = importlib.util.spec_from_file_location('inspector', Path(__file__).resolve().parents[2] / 'scripts/inspect-v3-workflow-trace.py')
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder);os.chmod(parent, 0o700);path = parent / 'trial.json'
            module.private_write(path, trial())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):module.private_write(path, trial())
            self.assertEqual(path.read_bytes(), before)
            os.chmod(parent, 0o755)
            with self.assertRaises(ValueError):module.private_write(parent / 'public.json', trial())


if __name__ == '__main__':unittest.main()
