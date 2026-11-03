from pathlib import Path
import importlib.util
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'monitoring/v1-staging'))
from nginx_user_read_profile import BACKEND_DIGEST
from nginx_create_enforcement import ENFORCED_LOCATION
spec = importlib.util.spec_from_file_location('renderer468', ROOT/'scripts/render-v1-staging-nginx.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)

class JwtProfileTest(unittest.TestCase):
    def render(self, **kwargs):
        return renderer.render((ROOT/'nginx/yeodam.conf').read_text(), 'a'*40, **kwargs)

    def test_explicit_activation_requires_reviewed_backend_and_leaves_defaults_intact(self):
        self.assertNotIn('js_import', self.render())
        self.assertIn('rate=25r/s;', self.render(user_read_enabled=True, backend_digest=BACKEND_DIGEST))
        for options in [dict(jwt_read_gate=True),dict(jwt_read_gate=True,user_read_enabled=True,backend_digest='sha256:'+'0'*64)]:
            with self.assertRaises(ValueError): self.render(**options)

    def test_two_budgets_and_preserved_write_connection_and_timeout_settings(self):
        text=self.render(jwt_read_gate=True,user_read_enabled=True,backend_digest=BACKEND_DIGEST)
        self.assertNotIn('zone=yeodam_staging_reads', text)
        self.assertIn('zone=yeodam_jwt_users:10m rate=5r/s;',text)
        self.assertIn('zone=yeodam_jwt_invalid:10m rate=25r/s;',text)
        self.assertEqual(text.count('if ($yeodam_jwt_read_candidate) { return 419; }'),3)
        # The create block has only the explicit GET/HEAD dispatch added.
        from nginx_jwt_gate import ROUTING
        self.assertIn(ENFORCED_LOCATION, text.replace(ROUTING,''))
        self.assertIn('keepalive_timeout 15s;', text)
        self.assertIn('proxy_read_timeout 600s;', text)
        self.assertIn('"jwt_gate_result":"$yeodam_jwt_gate_result"',text)
        self.assertNotIn('$http_x_user',text)
        self.assertNotIn('$cookie_accessToken',text)

if __name__ == '__main__': unittest.main()
