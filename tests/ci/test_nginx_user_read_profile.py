from pathlib import Path
import importlib.util
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'monitoring/v1-staging'))
from nginx_user_read_profile import BACKEND_DIGEST

spec = importlib.util.spec_from_file_location('renderer459', ROOT/'scripts/render-v1-staging-nginx.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class ProfileTest(unittest.TestCase):
    def test_activation_requires_reviewed_backend_and_preserves_other_settings(self):
        template = (ROOT/'nginx/yeodam.conf').read_text()
        legacy = renderer.render(template, 'a'*40)
        active = renderer.render(template, 'a'*40, user_read_enabled=True, backend_digest=BACKEND_DIGEST)
        self.assertEqual(active.replace('zone=yeodam_staging_reads:10m rate=25r/s;',
                                        'zone=yeodam_staging_reads:10m rate=5r/s;'), legacy)
        self.assertIn('burst=30 nodelay;', active)
        self.assertIn('rate=1r/s;', active)
        self.assertIn('burst=5 nodelay;', active)
        self.assertIn('keepalive_timeout 15s;', active)

    def test_no_implicit_activation_or_wrong_image(self):
        template = (ROOT/'nginx/yeodam.conf').read_text()
        self.assertIn('rate=5r/s;', renderer.render(template, 'a'*40))
        for enabled, digest in [(True, None), (True, 'sha256:'+'0'*64), (False, BACKEND_DIGEST)]:
            with self.subTest(), self.assertRaises(ValueError):
                renderer.render(template, 'a'*40, user_read_enabled=enabled, backend_digest=digest)


if __name__ == '__main__':
    unittest.main()
