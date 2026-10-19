"""Check #336's dependency partition and bounded audit contracts."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


SENDER = load('cdc336sender', 'scripts/send-v2-staging-cdc-live-ssm.py')
AUDIT = load('cdc336audit', 'scripts/audit-v2-staging-cdc-shards.py')


def transferred(path):
    commands = []
    SENDER.append_file(commands, '/run/cdc336-test', path, '336')
    parts = [re.search(r"printf '%s' '([^']+)' >>", command).group(1)
             for command in commands if command.startswith("printf '%s'")]
    return base64.b64decode(''.join(parts))


def event(table, trip_id):
    return json.dumps({'payload': {'source': {'table': table}, 'op': 'c',
                                   'after': {'trip_id': trip_id}}}) + '\n'


class ShardedCdcContractTest(unittest.TestCase):
    def test_isolated_case_and_same_trip_partition_route(self):
        source = json.loads(transferred(SENDER.SOURCE_CONFIG))
        sink = json.loads(transferred(SENDER.SINK_CONFIG))
        self.assertEqual(source['database.server.id'], '5415')
        self.assertEqual(source['topic.prefix'], 'staging.yeodam.migration.b1.live336')
        self.assertIn('yeodam_cdc_live336', sink['connection.url'])
        self.assertIn('yeodam_cdc_live336', transferred(SENDER.DDL).decode())
        run_id = '318582ec-33a2-4901-9b8d-ac3fb43f2828'
        for phase in ('destination', 'topics', 'initial-source', 'bulk-ready',
                      'bulk-capture', 'bulk-destination', 'ordered-sink', 'shard-audit'):
            SENDER.command_for(phase, run_id, '336')
        with patch.dict(os.environ, {'CDC_LIVE_CASE': '336'}):
            config = load('cdc336config', 'scripts/configure-v2-staging-cdc-live.py')
        routed = {}
        config.configure_ordered_route(routed)
        self.assertEqual(routed['transforms'], 'route,partition')
        self.assertEqual(routed['transforms.partition.partition.payload.fields'],
                         'change.trip_id')
        self.assertEqual(routed['transforms.partition.partition.topic.num'], '8')
        self.assertEqual(config.ORDERED_TOPIC,
                         routed['transforms.route.topic.replacement'])
        self.assertEqual(config.BULK_CASES[-1], '336')
        topic_lines = '\n'.join(f'{config.ORDERED_TOPIC}:{i}:{i + 1}' for i in range(8))
        with patch.object(config.subprocess, 'run', return_value=SimpleNamespace(stdout=topic_lines)):
            self.assertEqual(config.topic_end(config.ORDERED_TOPIC), 36)
        group = 'connect-yeodam-migration-b1-live336-sink-ordered'
        group_lines = '\n'.join(f'{group} {config.ORDERED_TOPIC} {i} 10 20 10'
                                for i in range(8))
        with patch.object(config.subprocess, 'run', return_value=SimpleNamespace(stdout=group_lines)):
            self.assertEqual(config.sink_lag(group.removeprefix('connect-'),
                                             config.ORDERED_TOPIC), (80, 160, 80))

    def test_parent_child_order_and_cross_partition_detection(self):
        first = AUDIT.audit_partition([event('trips', 10),
                                       event('trip_regions', 10)], 0, 2)
        second = AUDIT.audit_partition([event('trips', 11),
                                        event('trip_regions', 11)], 1, 2)
        self.assertEqual(first['reversedPairs'], 0)
        self.assertEqual(second['reversedPairs'], 0)
        self.assertTrue(AUDIT.summarize([first, second])['passed'])
        misplaced = AUDIT.audit_partition([event('trip_regions', 10)], 1, 1)
        self.assertEqual(AUDIT.summarize([first, misplaced])['crossPartitionTripIds'], 1)
        new_user = AUDIT.audit_partition([event('users', 0)], 2, 1)
        self.assertFalse(AUDIT.summarize([first, new_user])['passed'])
        reversed_pair = AUDIT.audit_partition([event('trip_regions', 12),
                                               event('trips', 12)], 2, 2)
        self.assertEqual(reversed_pair['reversedPairs'], 1)
        self.assertFalse(AUDIT.summarize([reversed_pair])['passed'])
        with self.assertRaises(ValueError):
            AUDIT.audit_partition([event('trips', 10)], 0, 2)


if __name__ == '__main__':
    unittest.main()
