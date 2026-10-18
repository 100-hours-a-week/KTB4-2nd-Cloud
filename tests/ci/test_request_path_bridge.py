import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/render-request-path-bridge.py'
spec = importlib.util.spec_from_file_location('request_bridge', SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class RequestBridgeTest(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'deploy/request-path-bridge/config.example.json').read_text())

    def test_pending_compatibility_blocks_normal_render(self):
        self.assertEqual(len(bridge.validate(self.config)), 6)
        with self.assertRaisesRegex(ValueError, 'Unverified readiness'):
            bridge.render(self.config, '1' * 40)

    def test_injection_or_wrong_destination_is_rejected(self):
        for field, value in (
            ('public_host', 'yeodam-2gether.com'),
            ('public_host', 'cutover-staging.yeodam-2gether.com; include /tmp/x;'),
            ('alb_dns_name', self.config['public_host']),
            ('alb_dns_name', 'https://example.ap-northeast-2.elb.amazonaws.com/api'),
            ('alb_dns_name', '10.30.0.10'),
            ('upstream_tls_name', 'stage.example.com\nproxy_ssl_verify off;'),
            ('certificate', '/tmp/cert; include /tmp/x;'),
            ('certificate_key', '/tmp/../key'),
            ('environment', 'production'),
            ('dns_resolvers', ['8.8.8.8; include /tmp/x;']),
            ('read_timeout_seconds', True),
            ('trip_timeout_seconds', 601),
        ):
            with self.subTest(field=field, value=value):
                config = copy.deepcopy(self.config)
                config[field] = value
                with self.assertRaises(ValueError):
                    bridge.validate(config)

    def test_typo_or_incomplete_readiness_is_rejected(self):
        for change in ('unknown', 'missing', 'boolean', 'blank'):
            with self.subTest(change=change):
                config = copy.deepcopy(self.config)
                if change == 'unknown':
                    config['proxy_retry'] = True
                elif change == 'missing':
                    del config['readiness']['session_csrf_refresh']
                else:
                    config['readiness']['session_csrf_refresh'] = True if change == 'boolean' else ''
                with self.assertRaises(ValueError):
                    bridge.validate(config)

    def test_unverified_output_is_marked_and_does_not_retry_or_rewrite(self):
        output = bridge.render(self.config, 'a' * 40, allow_unverified=True)
        self.assertIn('UNVERIFIED / LOCAL CHECK ONLY', output)
        self.assertIn('proxy_next_upstream off;', output)
        self.assertIn('proxy_pass https://$yeodam_bridge_alb$request_uri;', output)
        self.assertIn('proxy_ssl_verify on;', output)
        self.assertIn('resolver 169.254.169.253 valid=30s ipv6=off;', output)
        self.assertNotIn('@@', output)

    def test_check_exit_distinguishes_pending_from_manual_attestation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            for verified in (False, True):
                config = copy.deepcopy(self.config)
                if verified:
                    config['readiness'] = {key: 'local-evidence.md#verified' for key in bridge.GATES}
                path.write_text(json.dumps(config))
                result = subprocess.run([sys.executable, str(SCRIPT), 'check', '--config', str(path)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if verified else 2, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report['runtime_verified_by_this_tool'])
                self.assertEqual(report['manual_attestation_complete'], verified)

    def test_render_never_overwrites_and_pending_failure_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(self.config))
            output = Path(folder) / 'bridge.conf'
            command = [sys.executable, str(SCRIPT), 'render', '--config', str(path),
                       '--release', '1' * 40, '--output', str(output)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            output.write_text('existing config\n')
            result = subprocess.run(command + ['--allow-unverified'], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), 'existing config\n')

    def test_duplicate_json_cannot_override_a_reviewed_field(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON field'):
            json.loads('{"environment":"staging","environment":"production"}',
                       object_pairs_hook=bridge.unique_object)


if __name__ == '__main__':
    unittest.main()
