import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('trial456', Path(__file__).parents[1] / 'scripts/nginx-shared-ip-trial.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SharedIpProof(unittest.TestCase):
    def setUp(self):
        self.generator, self.control, self.access = [], [], []
        def add(role, cohort, t, status):
            rid = f'{len(self.access) + 1:032x}'
            row = dict(runId='fixture', role=role, cohort=cohort, scheduledSecond=t,
                       phase=m.phase(t), status=status, requestId=rid, errorType=None, guardTriggered=False)
            (self.generator if role == 'generator' else self.control).append(row)
            self.access.append(dict(request_id=rid, method='GET', normalized_route='/api/trips',
                                    sourceId=('a' if role == 'generator' else 'b') * 16,
                                    status=status, limit_req_status='REJECTED' if status == 429 else 'PASSED',
                                    upstream_status='' if status == 429 else str(status)))
        for t in range(m.SECONDS):
            add('generator', 'normal', t, 429 if t == 30 else 200)
            add('control', 'normal', t, 200)
        for tick in range(m.LOAD_START * m.RATE, m.LOAD_END * m.RATE):
            add('generator', 'load', tick / m.RATE, 429)

    def analyze(self):
        return m.analyze(self.generator, self.control, self.access)

    def test_complete_roles_source_and_server_ids_prove_scoped_impact(self):
        r = self.analyze()
        self.assertTrue(r['sharedIpNormal429Demonstrated'])
        self.assertEqual(r['affectedNormalRequests'], 1)
        self.assertFalse(r['historicalUserImpactProven'])

    def test_other_ip_control_must_be_independent(self):
        for row in self.access:
            row['sourceId'] = 'a' * 16
        self.assertFalse(self.analyze()['sharedIpNormal429Demonstrated'])

    def test_partial_or_duplicate_schedule_is_not_complete(self):
        self.generator.pop()
        self.assertFalse(self.analyze()['timelinesComplete'])
        self.assertFalse(self.analyze()['sharedIpNormal429Demonstrated'])

    def test_ids_are_exact_unique_and_route_bound(self):
        for mutation in ('missing', 'duplicate', 'wrong-status', 'wrong-route', 'missing-peer'):
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(self.access)
                if mutation == 'missing': rows.pop(0)
                if mutation == 'duplicate': rows.append(rows[0])
                if mutation == 'wrong-status': rows[0]['status'] = 500
                if mutation == 'wrong-route': rows[0]['normalized_route'] = '/login'
                if mutation == 'missing-peer': rows[0].pop('sourceId')
                self.assertFalse(m.analyze(self.generator, self.control, rows)['sharedIpNormal429Demonstrated'])

    def test_upstream_429_is_not_nginx_read_limit_proof(self):
        for row in self.access:
            if row['status'] == 429:
                row.update(limit_req_status='PASSED', upstream_status='429')
        self.assertFalse(self.analyze()['sharedIpNormal429Demonstrated'])

    def test_expected_normal_429_is_impact_but_other_failures_stop(self):
        self.assertFalse(m.guard('generator', 'normal', 429, 1, 30))
        self.assertTrue(m.guard('generator', 'normal', 429, 1, 5))
        self.assertTrue(m.guard('control', 'normal', 429, 1, 30))
        self.assertTrue(m.guard('generator', 'normal', 500, 1, 30))
        self.assertTrue(m.guard('generator', 'normal', 200, 1001, 30))
        self.assertTrue(m.guard('generator', 'load', 200, 1, 30))

    def test_steady_recovery_must_succeed(self):
        target = next(x for x in self.generator if x['cohort'] == 'normal' and x['scheduledSecond'] == 85)
        target['status'] = 429
        next(x for x in self.access if x['request_id'] == target['requestId'])['status'] = 429
        self.assertFalse(self.analyze()['sharedIpNormal429Demonstrated'])

    def test_preflight_socket_is_closed_before_common_start_wait(self):
        client = Mock()
        client.request.return_value = {'status': 200}
        self.assertEqual(m.preflight(client), {'status': 200})
        client.close.assert_called_once()
        client.request.side_effect = RuntimeError('fixture failure')
        with self.assertRaises(RuntimeError):
            m.preflight(client)
        self.assertEqual(client.close.call_count, 2)

    def test_transport_error_is_recorded_without_retry_or_header_leak(self):
        connection = Mock()
        connection.request.side_effect = m.http.client.RemoteDisconnected('private fixture text')
        with patch.object(m.http.client, 'HTTPSConnection', return_value=connection) as factory:
            client = m.Client('run', 'generator', 'normal', 'PRIVATE_COOKIE')
            result = client.request()
        self.assertEqual(result['status'], 0)
        self.assertEqual(result['errorType'], 'RemoteDisconnected')
        self.assertNotIn('PRIVATE_COOKIE', str(result))
        self.assertNotIn('private fixture text', str(result))
        factory.assert_called_once()
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_429_without_response_id_requires_unique_logged_probe_mapping(self):
        target = next(x for x in self.generator if x['cohort'] == 'normal' and x['status'] == 429)
        rid = target['requestId']
        target.update(requestId=None, probeId='unique-fixture-probe')
        self.assertFalse(self.analyze()['sharedIpNormal429Demonstrated'])
        mapping = [{'probeId': target['probeId'], 'request_id': rid}]
        r = m.analyze(self.generator, self.control, self.access, mapping)
        self.assertTrue(r['sharedIpNormal429Demonstrated'])
        self.assertFalse(m.analyze(self.generator, self.control, self.access, mapping * 2)['sharedIpNormal429Demonstrated'])

    def test_response_id_must_agree_with_logged_probe(self):
        self.generator[0]['probeId'] = 'fixture'
        mapping = [{'probeId': 'fixture', 'request_id': 'f' * 32}]
        self.assertFalse(m.analyze(self.generator, self.control, self.access, mapping)['sharedIpNormal429Demonstrated'])

    def test_independent_fractional_schedule_still_requires_one_normal_per_second(self):
        for rows in [self.generator, self.control]:
            for row in rows:
                if row['cohort'] == 'normal':
                    row['scheduledSecond'] += m.NORMAL_OFFSETS[int(row['scheduledSecond']) % 4]
        self.assertTrue(self.analyze()['sharedIpNormal429Demonstrated'])
        self.generator[0]['scheduledSecond'] = self.generator[1]['scheduledSecond']
        self.assertFalse(self.analyze()['sharedIpNormal429Demonstrated'])


if __name__ == '__main__':
    unittest.main()
