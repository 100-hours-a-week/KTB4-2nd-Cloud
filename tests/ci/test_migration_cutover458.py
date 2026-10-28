import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('overlay458', Path(__file__).parents[2] / 'scripts/migration-cutover458.py')
m = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(m)
RUN = '2091f08c-4dc3-4b2d-96ad-87d0f00b012b'


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.calls = []

    def pause(self, seconds): self.now += seconds

    def cleanup(self, api):
        return m.cleanup_sinks(RUN, api, seconds=1, clock=lambda: self.now, pause=self.pause)

    def test_only_run_sinks_stopped_no_source_or_offset_mutation(self):
        def api(path, method=None, **kwargs):
            self.calls.append((path, method))
            return {'connector': {'state': 'STOPPED'}, 'tasks': []}
        value = self.cleanup(api)
        self.assertEqual(value['status'], 'completed')
        self.assertEqual(len(self.calls), 4)
        for path, method in self.calls:
            self.assertIn('yeodam-b391-2091f08c-sink', path)
            self.assertIn(method, (None, 'PUT'))
            self.assertTrue(path.endswith(('/stop', '/status')))

    def test_stopped_connector_with_live_task_is_still_pending(self):
        def api(path, method=None, **kwargs):
            return {'connector': {'state': 'STOPPED'}, 'tasks': [{'state': 'RUNNING'}]}
        value = self.cleanup(api)
        self.assertEqual(value['status'], 'pending')
        self.assertGreaterEqual(self.now, 1)
        self.assertEqual(value['writerAction'], 'none')

    def test_unavailable_worker_preserves_cleanup_debt_and_retry_is_idempotent(self):
        def failed(*args, **kwargs): raise TimeoutError('private connection details')
        value = self.cleanup(failed)
        self.assertEqual(value['status'], 'pending')
        self.assertNotIn('private connection', str(value))
        self.assertTrue(all(v['errorClass'] == 'TimeoutError' for v in value['connectors'].values()))
        self.now = 0
        again = self.cleanup(lambda *a, **k: {'connector': {'state': 'STOPPED'}, 'tasks': []})
        self.assertEqual(again['status'], 'completed')

    def test_invalid_run_has_no_api_effect(self):
        with self.assertRaises(ValueError):
            m.cleanup_sinks('../other', lambda *a: self.calls.append(a))
        self.assertEqual(self.calls, [])


class OverlayTests(unittest.TestCase):
    def test_unknown_template_fails_closed(self):
        for renderer in (m.render_boundary, m.render_control, m.render_handoff):
            with self.subTest(renderer=renderer), self.assertRaises(ValueError): renderer('different version')

    def test_reused_credentials_do_not_skip_fresh_checks_or_close_early(self):
        template = '''def main():
    a=p.parse_args()
    db=database()
    try:
        source,target=db.identity('source'),db.identity('target')
        verify_gtid_and_binlog(db)
    finally:db.close()
'''
        rendered = m.render_control(template)
        self.assertIn("source,target=db.identity('source'),db.identity('target')", rendered)
        self.assertIn('verify_gtid_and_binlog(db)', rendered)
        self.assertIn('if shared_db is None:db.close()', rendered)
        compile(rendered, '<rendered>', 'exec')


if __name__ == '__main__': unittest.main()
