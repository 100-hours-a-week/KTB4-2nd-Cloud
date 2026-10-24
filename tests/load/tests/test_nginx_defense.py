"""Offline fixtures only: fake HTTP transport, no listener or remote request."""

import argparse
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


idle = load('nginx-idle-probe')
logs = load('summarize-nginx-defense')


class NginxDefenseTest(unittest.TestCase):
    def args(self, **changes):
        return argparse.Namespace(**{'target': idle.ORIGIN, 'run_id': 'fixture', 'connections': 1,
            'hold_seconds': 1, 'open_interval': 0.05, 'request_timeout': 1,
            'execute': False, 'confirm_target': None, 'output': None, **changes})

    def test_plan_and_bounds_open_no_connection(self):
        with patch.object(idle.http.client, 'HTTPSConnection', side_effect=AssertionError('network forbidden')):
            self.assertFalse(idle.validate(self.args())['executionEnabled'])
            self.assertEqual(idle.validate(self.args(hold_seconds=90))['holdSeconds'], 90)
            with self.assertRaises(ValueError):
                idle.run_probe(idle.validate(self.args()))
            for change in ({'target': 'https://yeodam-2gether.com'}, {'connections': 65},
                           {'hold_seconds': float('nan')}, {'hold_seconds': 91}, {'open_interval': 0}, {'execute': True}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    idle.validate(self.args(**change))

    def test_cli_default_only_prints_plan_and_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'not-created.json'
            result = subprocess.run([sys.executable, str(SCRIPTS / 'nginx-idle-probe.py'),
                '--target', idle.ORIGIN, '--run-id', 'fixture', '--connections', '1',
                '--hold-seconds', '1', '--open-interval', '1', '--request-timeout', '1',
                '--output', str(output)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)['executionEnabled'])
            self.assertFalse(output.exists())

    def test_idle_eof_and_hold_limit_with_fake_transport(self):
        class Sock:
            def settimeout(self, seconds): pass
            def shutdown(self, mode): pass
            def recv(self, size): return b''
        class Response:
            status, version, will_close = 401, 11, False
            def read(self, limit): return b'{}'
        class Connection:
            def __init__(self, *args, **kwargs): self.sock = Sock()
            def connect(self): pass
            def request(self, method, path, headers):
                assert method == 'GET' and path == idle.PATH and 'Authorization' not in headers
            def getresponse(self): return Response()
            def close(self): pass
        plan = idle.validate(self.args(execute=True, confirm_target=idle.ORIGIN, output=Path('fixture')))
        with patch.object(idle.http.client, 'HTTPSConnection', Connection):
            result = idle.run_probe(plan)
            self.assertTrue(result['complete'])
            self.assertEqual(result['rows'][0]['outcome'], 'server_closed_during_idle')
            with patch.object(Sock, 'recv', side_effect=socket.timeout()):
                result = idle.run_probe(plan)
                self.assertEqual(result['rows'][0]['outcome'], 'client_hold_limit_reached')
            with patch.object(Response, 'status', 500):
                self.assertFalse(idle.run_probe(plan)['complete'])

    def test_empty_window_is_not_evidence_of_success(self):
        with tempfile.TemporaryDirectory() as directory:
            access = Path(directory) / 'empty.jsonl'
            access.write_text('')
            result = logs.summarize(access, None, logs.timestamp('2026-10-18T00:00:00Z'),
                                    logs.timestamp('2026-10-18T00:01:00Z'), None)
            self.assertTrue(result['complete'])
            self.assertFalse(result['hasAccessData'])
            self.assertEqual(result['routes'], [])

    def test_log_window_status_scope_timezone_and_invalid_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            access, error = Path(directory) / 'access.jsonl', Path(directory) / 'error.log'
            def entry(at, status, upstream):
                return {'timestamp': at, 'method': 'GET', 'normalized_route': '/api/trips/map',
                        'status': status, 'upstream_status': upstream, 'request_time': 0.001,
                        'Cookie': 'PRIVATE_DO_NOT_COPY'}
            access.write_text('\n'.join(json.dumps(row) for row in [
                entry('2026-09-29T12:26:00Z', 401, '401'),
                {'timestamp': 0, 'message': json.dumps(entry('2026-09-29T21:27:00+09:00', 429, '-'))},
                entry('2026-09-29T12:28:00Z', 500, '-'),
                entry('2026-09-29T12:31:00Z', 500, '-')]) + '\n')
            error.write_text('2026/09/29 21:29:00 [alert] worker_connections are not enough\n' * 2)
            start, end = logs.timestamp('2026-09-29T12:26:00Z'), logs.timestamp('2026-09-29T12:31:00Z')
            result = logs.summarize(access, error, start, end, logs.timezone(logs.timedelta(hours=9)))
            self.assertTrue(result['complete'])
            self.assertEqual(result['included']['access'], 3)
            self.assertEqual(sum(row['count'] for row in result['routes']), 3)
            self.assertEqual(sum(row['connectionErrors'] for row in result['minutes']), 2)
            self.assertEqual(result['outsideWindow']['access'], 1)
            self.assertTrue(all(row['limit_req_status'] == 'not_logged' for row in result['routes']))
            self.assertNotIn('PRIVATE', json.dumps(result))
            with access.open('a') as stream:
                stream.write(json.dumps({**entry('2026-09-29T12:29:00Z', 429, '-'),
                    'limit_req_status': 'REJECTED_DRY_RUN', 'limit_conn_status': '-'}) + '\n')
            limits = logs.summarize(access, None, start, end, None)
            self.assertEqual(sum(row['count'] for row in limits['routes']
                                 if row['limit_req_status'] == 'REJECTED_DRY_RUN'), 1)
            self.assertFalse(logs.summarize(access, error, start, end, None)['complete'])
            access.write_text(access.read_text() + 'invalid\n' + json.dumps(entry(None, 200, '200')) + '\n')
            result = logs.summarize(access, None, start, end, None)
            self.assertFalse(result['complete'])
            self.assertEqual(result['invalidLines']['access'], 2)
            with self.assertRaises(ValueError): logs.timestamp('2026-09-29T12:26:00')


if __name__ == '__main__':
    unittest.main()
