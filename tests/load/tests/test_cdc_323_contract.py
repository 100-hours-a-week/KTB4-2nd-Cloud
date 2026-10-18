"""Guard the fixed #323 snapshot and binlog route before a runtime pilot."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[3]
SENDER_SPEC = importlib.util.spec_from_file_location(
    'cdc_323_sender', ROOT / 'scripts/send-v2-staging-cdc-live-ssm.py')
SENDER = importlib.util.module_from_spec(SENDER_SPEC)
SENDER_SPEC.loader.exec_module(SENDER)


def transferred(source):
    commands = []
    SENDER.append_file(commands, '/run/cdc-test', source, '323')
    parts = [re.search(r"printf '%s' '([^']+)' >>", line).group(1)
             for line in commands if line.startswith("printf '%s'")]
    return base64.b64decode(''.join(parts))


class Cdc323ContractTest(unittest.TestCase):
    def test_isolated_initial_source_and_bounded_single_sink(self):
        source = json.loads(transferred(SENDER.SOURCE_CONFIG))
        sink = json.loads(transferred(SENDER.SINK_CONFIG))
        ddl = transferred(SENDER.DDL).decode()
        self.assertEqual(source['database.server.id'], '5409')
        self.assertEqual(source['snapshot.mode'], 'initial')
        self.assertEqual(source['topic.prefix'], 'staging.yeodam.migration.b1.live323')
        self.assertEqual(source['schema.history.internal.kafka.topic'],
                         'staging.yeodam.migration.b1.live323.schema-history')
        self.assertIn('/yeodam_cdc_live323?', sink['connection.url'])
        self.assertEqual(sink['tasks.max'], '1')
        self.assertEqual(sink['insert.mode'], 'upsert')
        self.assertEqual(sink['connection.pool.max_size'], '2')
        self.assertIn('yeodam_cdc_live323', ddl)

    def test_ordered_route_is_set_before_source_registration(self):
        previous = os.environ.get('CDC_LIVE_CASE')
        os.environ['CDC_LIVE_CASE'] = '323'
        try:
            spec = importlib.util.spec_from_file_location(
                'cdc_323_config', ROOT / 'scripts/configure-v2-staging-cdc-live.py')
            config = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(config)
        finally:
            if previous is None:
                os.environ.pop('CDC_LIVE_CASE', None)
            else:
                os.environ['CDC_LIVE_CASE'] = previous
        source = {}
        config.configure_ordered_route(source)
        self.assertEqual(source['transforms'], 'route')
        self.assertEqual(source['transforms.route.topic.replacement'],
                         'staging.yeodam.migration.b1.live323.ordered-records')
        self.assertEqual(source['producer.override.enable.idempotence'], 'true')
        self.assertEqual(source['producer.override.max.in.flight.requests.per.connection'], '1')
        self.assertIn('configure_ordered_route(data)',
                      (ROOT / 'scripts/configure-v2-staging-cdc-live.py').read_text())

    def test_no_mid_run_switch_or_table_sink_phases(self):
        for phase in ('ordered-source', 'ordered-topic', 'pause-source',
                      'resume-source', 'sink-users', 'sink-trips',
                      'sink-trip_regions', 'pause-legacy-sinks'):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                SENDER.command_for(phase, case='323')
        for phase in ('topics', 'initial-source', 'ordered-sink', 'status'):
            SENDER.command_for(phase, case='323')
        instance, commands = SENDER.command_for('ordered-audit', case='323')
        self.assertEqual(instance, SENDER.KAFKA)
        self.assertTrue(any('ordered-audit.py' in line for line in commands))
        with self.assertRaises(ValueError):
            SENDER.command_for('ordered-audit', case='313')


if __name__ == '__main__':
    unittest.main()
