import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('log456', Path(__file__).parents[2] / 'monitoring/v1-staging/nginx_shared_ip_observation.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Observation(unittest.TestCase):
    def setUp(self):
        self.original = '''log_format yeodam_json escape=json
      '{"request_id":"$request_id",'
      '"status":$status}';
limit_req_zone $key zone=reads:10m rate=5r/s;
server { limit_req zone=reads burst=30 nodelay; }
'''

    def test_only_additive_logging_changes_with_exact_recovery(self):
        observed = m.instrument(self.original)
        self.assertEqual(observed.replace(m.PREFIX, '', 1).replace(m.FIELD, '', 1), self.original)
        self.assertEqual(m.instrument(observed), observed)
        self.assertIn('^GET:/api/trips:', observed)
        self.assertIn('default "";', observed)
        self.assertNotIn('Cookie', observed)

    def test_partial_unknown_or_duplicate_fields_refused(self):
        for text in [self.original + m.FIELD, self.original * 2,
                     self.original.replace('escape=json', ''), self.original + 'trial_probe_id']:
            with self.assertRaises(ValueError):
                m.instrument(text)


if __name__ == '__main__':
    unittest.main()
