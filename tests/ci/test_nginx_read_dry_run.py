import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'monitoring/v1-staging'))
from nginx_observability import instrument_site
from nginx_read_dry_run import PREFIX, SERVER, instrument_read_dry_run


class ReadDryRunTest(unittest.TestCase):
    def source(self):
        return instrument_site((ROOT/'nginx/yeodam.conf').read_text().replace(
            'yeodam-2gether.com', 'v1-staging.yeodam-2gether.com'))

    def test_exact_patch_preserves_routes_headers_and_timeouts(self):
        source = self.source()
        changed = instrument_read_dry_run(source)
        restored = changed.replace(PREFIX, '', 1).replace(SERVER, '', 1).replace(
            '\n      \'"limit_req_status":"$limit_req_status",\'', '', 1)
        self.assertEqual(restored, source)
        self.assertEqual(instrument_read_dry_run(changed), changed)

    def test_other_servers_and_existing_or_partial_limits_are_rejected(self):
        for source in [(ROOT/'nginx/yeodam.conf').read_text(), self.source()+'\nlimit_req zone=other;\n',
                       self.source()+'\nlimit_conn address 10;\n',
                       instrument_read_dry_run(self.source()).replace('limit_req_dry_run on;', 'limit_req_dry_run off;'),
                       instrument_read_dry_run(self.source()).replace('"limit_req_status":"$limit_req_status",', '')]:
            with self.subTest(source=source[-80:]), self.assertRaises(ValueError):
                instrument_read_dry_run(source)

    def test_staging_renderer_includes_reviewed_policy_and_preserves_request_id(self):
        spec = importlib.util.spec_from_file_location('staging_renderer', ROOT/'scripts/render-v1-staging-nginx.py')
        mod = importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        result = mod.render((ROOT/'nginx/yeodam.conf').read_text(), 'a'*40)
        self.assertIn('zone=yeodam_staging_reads:10m rate=5r/s;', result)
        self.assertIn('proxy_set_header X-Request-ID $yeodam_staging_request_id;', result)
        self.assertIn('limit_req_dry_run off;', result)


if __name__ == '__main__':
    unittest.main()
