import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'monitoring/v1-staging'))
from nginx_create_dry_run import PREFIX, LOCATION, instrument_create_dry_run
from nginx_observability import instrument_site
from nginx_read_enforcement import instrument_read_enforcement


class CreateDryRunTest(unittest.TestCase):
    def source(self):
        return instrument_read_enforcement(instrument_site((ROOT/'nginx/yeodam.conf').read_text().replace(
            'yeodam-2gether.com', 'v1-staging.yeodam-2gether.com')))

    def test_reversible_and_idempotent_with_enforced_reads_untouched(self):
        source = self.source()
        changed = instrument_create_dry_run(source)
        self.assertEqual(changed.replace(PREFIX, '', 1).replace(LOCATION, '', 1), source)
        self.assertEqual(instrument_create_dry_run(changed), changed)

    def test_reject_production_partial_policy_or_conflicting_exact_route(self):
        changed = instrument_create_dry_run(self.source())
        for value in [(ROOT/'nginx/yeodam.conf').read_text(),
                      self.source().replace('limit_req_dry_run off;', 'limit_req_dry_run on;'),
                      changed.replace('burst=5 nodelay;', 'burst=6 nodelay;'),
                      self.source()+'\nlocation = /api/trips { }\n']:
            with self.subTest(value=value[-80:]), self.assertRaises(ValueError):
                instrument_create_dry_run(value)

    def test_renderer_keeps_both_policy_contexts_and_upload_timeout(self):
        spec = importlib.util.spec_from_file_location('renderer430', ROOT/'scripts/render-v1-staging-nginx.py')
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        result = mod.render((ROOT/'nginx/yeodam.conf').read_text(), 'a'*40)
        self.assertEqual(result.count('limit_req_dry_run off;'), 2)
        self.assertEqual(result.count('limit_req_dry_run on;'), 0)
        self.assertIn('"POST:/api/trips" $binary_remote_addr;', result)
        self.assertIn('location /api/trips {\n        proxy_pass http://yeodam_backend;\n        proxy_read_timeout 600s;', result)


if __name__ == '__main__':
    unittest.main()
