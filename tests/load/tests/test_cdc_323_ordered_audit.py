"""Check that the ordered-topic audit detects FK and snapshot boundary errors."""

import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    'ordered_audit', ROOT / 'scripts/audit-v2-staging-cdc-ordered-events.py')
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def event(table, op, pos=100):
    return json.dumps({'payload': {'op': op, 'source': {
        'table': table, 'file': 'binlog.000005', 'pos': pos}}})


class OrderedAuditTest(unittest.TestCase):
    def test_parent_child_snapshot_then_stream(self):
        lines = [event('users', 'r'), event('trips', 'r'),
                 event('trip_regions', 'r'), event('trips', 'c', 120),
                 event('trip_regions', 'c', 121)]
        result = AUDIT.audit(lines, 5)
        self.assertTrue(result['passed'])
        self.assertEqual(result['counts']['trip_regions'],
                         {'r': 1, 'c': 1, 'u': 0, 'd': 0})
        self.assertEqual(result['snapshotPosition'], ['binlog.000005', 100])

    def test_reject_child_first_and_snapshot_after_stream(self):
        lines = [event('trip_regions', 'r'), event('users', 'r'),
                 event('trips', 'c', 120), event('trips', 'r')]
        result = AUDIT.audit(lines, 4)
        self.assertFalse(result['passed'])
        self.assertEqual({item['kind'] for item in result['violations']},
                         {'snapshot_fk_order', 'snapshot_after_stream'})

    def test_reject_incomplete_or_malformed_stream(self):
        result = AUDIT.audit([event('users', 'r')], 2)
        self.assertFalse(result['passed'])
        self.assertIn('event_count_mismatch',
                      [item['kind'] for item in result['violations']])
        with self.assertRaisesRegex(ValueError, 'index 1'):
            AUDIT.audit(['{"payload":{"op":"r"}}'], 1)

    def test_reject_stream_before_snapshot_position(self):
        result = AUDIT.audit([event('users', 'r', 100),
                              event('trips', 'c', 90)], 2)
        self.assertFalse(result['passed'])
        self.assertIn('stream_before_snapshot_position',
                      [item['kind'] for item in result['violations']])


if __name__ == '__main__':
    unittest.main()
