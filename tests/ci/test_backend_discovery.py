import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('discovery', Path(__file__).resolve().parents[2] / 'monitoring/v2-staging/discover-backend-tasks.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def task(ip='10.30.10.20', group='service:' + m.SERVICE, desired='RUNNING'):
    return {'group': group, 'lastStatus': 'RUNNING', 'desiredStatus': desired, 'taskArn': 'arn/task/task1',
            'taskDefinitionArn': 'arn/yeodam-v2-staging-backend:7', 'attachments': [
                {'type': 'ElasticNetworkInterface', 'details': [{'name': 'privateIPv4Address', 'value': ip}]}]}


class DiscoveryTests(unittest.TestCase):
    def test_only_running_desired_backend_is_scraped(self):
        rows = m.targets([task(), task(group='service:other'), task(desired='STOPPED')])
        self.assertEqual([r['targets'] for r in rows], [['10.30.10.20:9091']])

    def test_unexpected_network_is_not_published(self):
        with self.assertRaises(ValueError):
            m.targets([task(ip='203.0.113.1')])

    def test_api_failure_preserves_last_good_targets(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'backend.json'
            path.write_text('last-good')
            with patch.object(m, 'aws', side_effect=RuntimeError('AWS unavailable')), self.assertRaises(RuntimeError):
                m.refresh(path)
            self.assertEqual(path.read_text(), 'last-good')

    def test_task_replacement_removes_old_address(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'backend.json'
            path.write_text('[{"targets":["10.30.10.1:9091"]}]')
            with patch.object(m, 'aws', side_effect=[{'taskArns': ['task1']}, {'tasks': [task()]}]):
                m.refresh(path)
            self.assertEqual(json.loads(path.read_text())[0]['targets'], ['10.30.10.20:9091'])
