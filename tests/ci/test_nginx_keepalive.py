from pathlib import Path
import sys,unittest,importlib.util
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'monitoring/v1-staging'))
from nginx_observability import instrument_site
from nginx_read_enforcement import instrument_read_enforcement
from nginx_keepalive import instrument_keepalive


class KeepaliveTest(unittest.TestCase):
    def baseline(self):
        return instrument_read_enforcement(instrument_site((ROOT/'nginx/yeodam.conf').read_text().replace('yeodam-2gether.com','v1-staging.yeodam-2gether.com')))

    def test_only_idle_block_changes_and_baseline_roundtrip_is_exact(self):
        base=self.baseline()
        for seconds in [15,30]:
            value=instrument_keepalive(base,seconds)
            self.assertEqual(instrument_keepalive(value,seconds),value)
            self.assertEqual(instrument_keepalive(value,75),base)
            self.assertIn('keepalive_timeout '+str(seconds)+'s;',value)
        self.assertEqual(instrument_keepalive(instrument_keepalive(base,30),15),instrument_keepalive(base,15))

    def test_staging_renderer_keeps_verified_timeout_and_source_template_unchanged(self):
        template=ROOT/'nginx/yeodam.conf';original=template.read_text()
        spec=importlib.util.spec_from_file_location('keepalive_renderer',ROOT/'scripts/render-v1-staging-nginx.py')
        renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
        value=renderer.render(original,'a'*40)
        self.assertIn('keepalive_timeout 15s;',value)
        self.assertEqual(value.count('keepalive_timeout '),1)
        self.assertIn('proxy_read_timeout 600s;',value)
        self.assertEqual(template.read_text(),original)

    def test_partial_foreign_or_unreviewed_timeout_is_rejected(self):
        base=self.baseline();active=instrument_keepalive(base,15)
        for text,seconds in [(base,5),(active.replace('15s;','14s;'),30),
                             (base+'\nkeepalive_timeout 65;\n',15),
                             (active.replace('# END yeodam staging idle keepalive',''),15),
                             ((ROOT/'nginx/yeodam.conf').read_text(),15)]:
            with self.subTest(),self.assertRaises(ValueError):instrument_keepalive(text,seconds)


if __name__=='__main__':unittest.main()
