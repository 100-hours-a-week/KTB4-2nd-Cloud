"""One-time recovery must fail closed on nonempty data or stale/foreign source."""
import copy
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
s = importlib.util.spec_from_file_location('recovery', ROOT / 'scripts/prepare-v3-cdc-recovery.py')
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


def connector():
    return {'metadata': {'name': m.NAME, 'namespace': 'kafka', 'resourceVersion': '10', 'generation': 1},
            'spec': {'class': 'io.debezium.connector.mysql.MySqlConnector', 'tasksMax': 1, 'state': 'running',
                     'config': dict(m.NORMAL_CONFIG), 'autoRestart': {'enabled': False}},
            'status': {'observedGeneration': 1, 'conditions': [{'type': 'NotReady', 'status': 'True'}],
                       'connectorStatus': {'connector': {'state': 'RUNNING'}, 'tasks': [
                           {'id': 0, 'state': 'FAILED', 'trace': 'binlog mysql-bin.000001 is no longer available'}]}}}


class Recovery(unittest.TestCase):
    def setUp(self):
        self.source = connector()
        self.counts = {t: 0 for t in m.CONTRACT['tables']}

    def test_empty_source_plan_only_changes_mode_and_retry(self):
        ops = m.plan(self.source, self.counts)
        self.assertEqual([x['path'] for x in ops if x['op'] != 'test'], ['/spec/config/snapshot.mode', '/spec/autoRestart'])
        self.assertEqual(ops[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '10'})
        self.assertEqual(ops[-2]['value'], 'when_needed')
        self.assertEqual(ops[-1]['value'], {'enabled': False})
        self.assertEqual(self.source, connector())

    def test_any_rows_missing_tables_boolean_or_negative_count_rejected(self):
        for value in [1, -1, False, '0']:
            counts = dict(self.counts);counts[next(iter(counts))] = value
            with self.subTest(value=value), self.assertRaises(ValueError):m.plan(self.source, counts)
        counts = dict(self.counts);counts.pop(next(iter(counts)))
        with self.assertRaises(ValueError):m.plan(self.source, counts)

    def test_foreign_namespace_identity_source_and_unrelated_failure_rejected(self):
        for change in ['name', 'namespace', 'server', 'filter', 'trace', 'running', 'task']:
            c = copy.deepcopy(self.source)
            if change == 'name':c['metadata']['name'] = 'production'
            if change == 'namespace':c['metadata']['namespace'] = 'production'
            if change == 'server':c['spec']['config']['database.hostname'] = 'production.internal'
            if change == 'filter':c['spec']['config']['transforms.owned.condition'] = 'true'
            if change == 'trace':c['status']['connectorStatus']['tasks'][0]['trace'] = 'Connection refused'
            if change == 'running':c['status']['connectorStatus']['tasks'][0]['state'] = 'RUNNING'
            if change == 'task':c['status']['connectorStatus']['tasks'][0]['id'] = 1
            with self.subTest(change=change), self.assertRaises(ValueError):m.plan(c, self.counts)

    def test_restore_requires_ready_task_and_committed_non_snapshot_offset(self):
        c = copy.deepcopy(self.source);c['spec']['config']['snapshot.mode'] = 'when_needed'
        c['status']['conditions'] = [{'type': 'Ready', 'status': 'True'}]
        c['status']['connectorStatus']['tasks'][0] = {'id': 0, 'state': 'RUNNING'}
        offsets = {'offsets': [{'partition': {'server': 'v3.yeodam.outbox'}, 'offset': {'file': 'mysql-bin.000002', 'pos': 200}}]}
        result = m.plan(c, offsets=offsets, restore=True)
        self.assertEqual(result[-2]['value'], 'initial')
        self.assertEqual(result[-1]['value'], m.RETRIES)
        for change in ['snapshot', 'snapshot_completed', 'foreign', 'stale', 'task']:
            bad, off = copy.deepcopy(c), copy.deepcopy(offsets)
            if change == 'snapshot':off['offsets'][0]['offset']['snapshot'] = True
            if change == 'snapshot_completed':off['offsets'][0]['offset']['snapshot_completed'] = False
            if change == 'foreign':off['offsets'][0]['partition']['server'] = 'prod'
            if change == 'stale':bad['status']['observedGeneration'] = 0
            if change == 'task':bad['status']['connectorStatus']['tasks'][0]['state'] = 'FAILED'
            with self.subTest(change=change), self.assertRaises(ValueError):m.plan(bad, offsets=off, restore=True)


if __name__ == '__main__':unittest.main()
