import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('reconcile', Path(__file__).parents[1] / 'scripts/reconcile-migration-probe.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
RUN = '11111111-1111-4111-a111-111111111111'
REQUEST = '22222222-2222-4222-a222-222222222222'
HASH = 'a' * 64

class ReconciliationTest(unittest.TestCase):
    def load_report(self, counts, plans):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); events=[]; rows=[]; summaries=[]
            for generator,(actual,planned) in enumerate(zip(counts,plans)):
                gid=str(generator)
                events.append({'source':'migration-probe','event':'run_started','runId':RUN,'generatorId':gid,'mode':'load','rate':planned,'duration':1})
                for n in range(actual):
                    request=f'{generator:08x}-2222-4222-a222-{n:012x}'
                    event={'source':'migration-probe','event':'started','runId':RUN,'generatorId':gid,'requestId':request,'payloadHash':HASH}
                    events += [event,{**event,'event':'final','outcome':'confirmed'}]
                    rows.append(f'{RUN}\t{request}\t{HASH}\t2026-10-14\n')
                path=root/f'summary{generator}'
                path.write_text(json.dumps({'metrics':{'probe_started':{'values':{'count':actual}},'probe_confirmed':{'values':{'count':actual}}}})); summaries.append(path)
            (root/'events').write_text('\n'.join(json.dumps(e) for e in events));(root/'target').write_text(''.join(rows))
            return module.reconcile([root/'events'],root/'target',RUN,summaries)

    def test_one_extra_iteration_but_no_under_delivery(self):
        self.assertTrue(self.load_report([601],[600])['passed'])
        self.assertFalse(self.load_report([599],[600])['passed'])
        self.assertFalse(self.load_report([602],[600])['passed'])

    def test_one_generator_extra_does_not_mask_another_under_delivery(self):
        self.assertFalse(self.load_report([601,599],[600,600])['passed'])

    def report(self, outcome='confirmed', stored=True, final=True, payload=HASH, duplicate=False, metric_count=1):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            start = {'source': 'migration-probe', 'event': 'started', 'runId': RUN, 'requestId': REQUEST, 'payloadHash': HASH}
            events = [{'source': 'migration-probe', 'event': 'run_started', 'runId': RUN, 'generatorId': 'test', 'mode': 'load', 'rate': 1, 'duration': 1}, start]
            if duplicate: events.append(start)
            if final: events.append({**start, 'event': 'final', 'outcome': outcome})
            (root / 'events').write_text('\n'.join(json.dumps(e) for e in events))
            (root / 'target').write_text(f'{RUN}\t{REQUEST}\t{payload}\t2026-10-14 00:00:00.000000\n' if stored else '')
            metrics = {'probe_started': {'values': {'count': metric_count}},
                       'probe_confirmed' if outcome == 'confirmed' and final else 'probe_final_failures': {'values': {'count': 1 if final else 0}}}
            (root / 'summary').write_text(json.dumps({'metrics': metrics}))
            return module.reconcile([root / 'events'], root / 'target', RUN, root / 'summary')

    def test_committed_row_matches(self):
        self.assertTrue(self.report()['passed'])

    def test_http_timeout_with_committed_row_is_failure_not_loss(self):
        report = self.report(outcome='unconfirmed')
        self.assertEqual(report['unconfirmedButStored'], 1)
        self.assertEqual(report['confirmedMissing'], 0)
        self.assertFalse(report['passed'])

    def test_acknowledged_write_loss(self):
        self.assertEqual(self.report(stored=False)['confirmedMissing'], 1)

    def test_interrupted_request_and_truncated_log(self):
        self.assertEqual(self.report(final=False)['interrupted'], 1)
        self.assertFalse(self.report(metric_count=2)['completeEventLog'])

    def test_hash_mismatch_and_duplicate_generator_ids(self):
        self.assertEqual(self.report(payload='b' * 64)['hashMismatch'], 1)
        with self.assertRaises(sqlite3.IntegrityError): self.report(duplicate=True)

class StagingIngressTest(unittest.TestCase):
    def test_renderer_preserves_only_valid_probe_ids_and_refuses_template_drift(self):
        root = Path(__file__).parents[3]
        spec = importlib.util.spec_from_file_location('renderer', root / 'scripts/render-v1-staging-nginx.py')
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        original = (root / 'nginx/yeodam.conf').read_text()
        result = renderer.render(original, 'a' * 40)
        self.assertIn('default $request_id;', result)
        self.assertIn('^POST:/api/internal/migration-write-probe:', result)
        self.assertIn('proxy_set_header X-Request-ID $yeodam_staging_request_id;', result)
        self.assertIn('server_name v1-staging.yeodam-2gether.com;', result)
        with self.assertRaises(ValueError): renderer.render('missing routing markers', 'a' * 40)
