import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import json
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('edge438', Path(__file__).parents[1]/'scripts/nginx-external-diagnosis.py')
edge = importlib.util.module_from_spec(spec);spec.loader.exec_module(edge)


class DiagnosisTest(unittest.TestCase):
    def test_requested_http2_fallback_is_not_success(self):
        data = {'status': '200', 'dns': '0.01', 'tcp': '0.02', 'tls': '0.03',
                'ttfb': '0.04', 'total': '0.05', 'httpVersion': '1.1', 'remoteIp': '127.0.0.1'}
        result = SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr='')
        with patch.object(edge.subprocess, 'run', return_value=result) as run:
            self.assertFalse(edge.probe('/login', http_version='2')['ok'])
            self.assertIn('--http2', run.call_args.args[0])
        data['httpVersion'] = '2'
        with patch.object(edge.subprocess, 'run', return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(data), stderr='')):
            self.assertTrue(edge.probe('/login', http_version='2')['ok'])

    def test_unsupported_curl_is_not_a_server_tls_failure(self):
        self.assertEqual(edge.failure_stage(4, {}), 'probe-tool')

    def test_timeouts_preserve_last_completed_transport_stage(self):
        self.assertEqual(edge.failure_stage(28, {'tcp': .1, 'tls': 0}), 'tls-timeout')
        self.assertEqual(edge.failure_stage(28, {'tcp': .1, 'tls': .2}), 'http-timeout')
        self.assertEqual(edge.failure_stage(28, {}), 'dns-or-tcp-timeout')
        self.assertEqual(edge.failure_stage(60, {}), 'tls')

    def test_lifetime_counters_are_not_run_deltas_and_reset_is_visible(self):
        first = {key: 500 for key in edge.ENA_KEYS[:5]}
        last = dict(first);last['pps_allowance_exceeded'] = 507
        self.assertIsNone(edge.counter_delta(first, None))
        self.assertEqual(edge.counter_delta(last, first)['pps_allowance_exceeded'], 7)
        self.assertIsNone(edge.counter_delta(first, last))

    def test_missing_ena_is_a_collection_failure_not_zero(self):
        with self.assertRaises(ValueError):edge.parse_ena('pps_allowance_exceeded: 0')

    def test_production_and_unbounded_duration_refused_before_execution(self):
        args = SimpleNamespace(run_id='1593f308-3b97-44cb-8c40-fbba4ee86d00',
            confirm_target='https://yeodam-2gether.com', seconds=30, mode='probe')
        with self.assertRaises(ValueError):edge.validate(args)
        args.confirm_target=edge.ORIGIN;args.seconds=1801
        with self.assertRaises(ValueError):edge.validate(args)


if __name__ == '__main__':
    unittest.main()
