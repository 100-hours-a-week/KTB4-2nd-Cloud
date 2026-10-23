import importlib.util
import json
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/nginx-read-demand.py'
spec = importlib.util.spec_from_file_location('read_demand', SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class ReadDemandTest(unittest.TestCase):
    def test_sliding_window_spans_second_boundary(self):
        # A fixed UTC-second bucket would hide this four-request burst.
        times = [990, 999, 1001, 1010]
        self.assertEqual(d.peak_count(times, 1000), 4)
        self.assertEqual(d.peak_count([0, 1000], 1000), 1)

    def test_burst_tracks_cumulative_excess_not_peak_rps(self):
        self.assertEqual(d.required_burst([0, 0, 0], 1), 2)
        self.assertEqual(d.required_burst([0, 1000, 2000], 1), 0)
        self.assertEqual(d.required_burst([0, 100, 200, 300], 1), 3)
        self.assertEqual(d.replay_rejections([0, 0, 1000], 1, 0), 1)
        times = [0, 0, 30, 31, 100, 500, 500, 1000, 1001]
        for rate in [1, 2, 5, 10]:
            burst = d.required_burst(times, rate)
            self.assertEqual(d.replay_rejections(times, rate, burst), 0)

    def test_export_anonymizes_peer_and_never_exports_extra_fields(self):
        meta = {'start_ms': 100000, 'phases': [{'name': 'single-1', 'start_ms': 100000},
                                             {'name': 'single-2', 'start_ms': 101000}]}
        row = {'msec': '101.150', 'request_time': '0.200', 'method': 'GET',
               'normalized_route': '/api/trips/:tripId', 'peer_addr': '192.0.2.5',
               'request_id': 'a'*32, 'status': '200', 'cookie': 'secret-token',
               'request_uri': '/api/trips/7?private=true'}
        events, _ = d.sanitize([row], meta, 102000, b'test-key')
        self.assertEqual(events[0]['start_ms'], 100950)
        self.assertEqual(events[0]['phase'], 'single-1')
        text = json.dumps(events)
        for secret in ['192.0.2.5', 'secret-token', 'private=true', 'peer_addr', 'cookie']:
            self.assertNotIn(secret, text)
        other, _ = d.sanitize([row], meta, 102000, b'other-key')
        self.assertNotEqual(events[0]['source_id'], other[0]['source_id'])

    def test_mixed_source_limits_are_not_globally_collapsed(self):
        events = [{'source_id': source, 'start_ms': t, 'phase': 'single-1',
                   'route': '/api/trips', 'status': 200}
                  for source in ['a', 'b'] for t in [0, 1000, 2000]]
        report = d.report(events, ['single-1'], [1])
        self.assertFalse(report['policy_selected'])
        self.assertEqual(len(report['groups']['all']), 2)
        self.assertEqual(report['groups']['all'][0]['rate_burst_tradeoffs'][0]['required_burst'], 0)

    def test_missing_field_inside_window_and_duplicate_request_fail(self):
        meta = {'start_ms': 100000, 'phases': [{'name': 'single-1', 'start_ms': 100000}]}
        with self.assertRaises(ValueError):
            d.sanitize([{'timestamp': '1970-01-01T00:01:40+00:00'}], meta, 102000, b'key')
        row = {'msec': '101.000', 'request_time': '.1', 'method': 'GET',
               'normalized_route': '/api/trips', 'peer_addr': '192.0.2.1',
               'request_id': 'b'*32, 'status': 200}
        with self.assertRaises(ValueError):
            d.sanitize([row, row], meta, 102000, b'key')

    def test_upload_auth_health_and_static_are_excluded(self):
        for method, route in [('POST', '/api/trips'), ('POST', '/api/trips/:tripId/initial-attachments'),
                              ('GET', '/api/auth/csrf'), ('GET', '/api/actuator/health'), ('GET', 'other')]:
            self.assertFalse(d.read_api({'method': method, 'normalized_route': route}))
        self.assertTrue(d.read_api({'method': 'GET', 'normalized_route': '/api/trips/:tripId/processing-status'}))
        self.assertTrue(d.read_api({'method': 'HEAD', 'normalized_route': '/api/trips'}))


if __name__ == '__main__':
    unittest.main()
