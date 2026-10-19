"""Keep the #324 throughput preflight isolated from the preserved #323 run."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[3]
SENDER_SPEC = importlib.util.spec_from_file_location(
    'cdc_324_sender', ROOT / 'scripts/send-v2-staging-cdc-live-ssm.py')
SENDER = importlib.util.module_from_spec(SENDER_SPEC)
SENDER_SPEC.loader.exec_module(SENDER)


def transferred(source):
    commands = []
    SENDER.append_file(commands, '/run/cdc-324-test', source, '324replay')
    parts = [re.search(r"printf '%s' '([^']+)' >>", line).group(1)
             for line in commands if line.startswith("printf '%s'")]
    return base64.b64decode(''.join(parts))


class Cdc324ReplayContractTest(unittest.TestCase):
    def test_new_destination_and_no_source_or_topic_mutation_phase(self):
        sink = json.loads(transferred(SENDER.SINK_CONFIG))
        ddl = transferred(SENDER.DDL).decode()
        self.assertIn('/yeodam_cdc_live324replay?', sink['connection.url'])
        self.assertIn('yeodam_cdc_live324replay', ddl)
        for phase in ('topics', 'initial-source', 'ordered-sink', 'pause-source',
                      'ordered-source', 'generator-deploy', 'ordered-audit'):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                SENDER.command_for(phase, case='324replay')
        for phase in ('destination', 'replay-sink', 'status', 'attest-destination'):
            SENDER.command_for(phase, case='324replay')

        commands = []
        SENDER.append_file(commands, '/run/cdc-324-test', SENDER.DDL, '324replay1000')
        parts = [re.search(r"printf '%s' '([^']+)' >>", line).group(1)
                 for line in commands if line.startswith("printf '%s'")]
        self.assertIn('yeodam_cdc_live324replay1000',
                      base64.b64decode(''.join(parts)).decode())
        with self.assertRaises(ValueError):
            SENDER.command_for('initial-source', case='324replay1000')

    def test_replay_uses_distinct_sink_and_preserved_topic(self):
        previous = os.environ.get('CDC_LIVE_CASE')
        os.environ['CDC_LIVE_CASE'] = '324replay'
        try:
            spec = importlib.util.spec_from_file_location(
                'cdc_324_config', ROOT / 'scripts/configure-v2-staging-cdc-live.py')
            config = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(config)
        finally:
            if previous is None:
                os.environ.pop('CDC_LIVE_CASE', None)
            else:
                os.environ['CDC_LIVE_CASE'] = previous

        written = []
        config.existing_credential = lambda kind: self.assertEqual(kind, 'sink')
        config.kafka = lambda *args: config.REPLAY_TOPIC + '\n'
        config.topic_end = lambda topic: 2766655
        config.config = lambda kind: {'connection.url': 'jdbc:mysql://example/yeodam_cdc_live324replay'}

        def api(path, value=None, method=None):
            if path == '/connectors':
                return [config.REPLAY_SOURCE, config.REPLAY_PREVIOUS_SINK,
                        'yeodam-staging-outbox']
            if path.endswith('/status'):
                state = 'RUNNING' if 'outbox' in path else 'STOPPED'
                return {'connector': {'state': state}, 'tasks': [{'state': state}]}
            if path.endswith('/config/validate'):
                return {'error_count': 0}
            if path.endswith('/config'):
                written.append((path, value, method))
                return {}
            raise AssertionError(path)

        config.api = api
        config.register('replay-sink')
        self.assertEqual(len(written), 1)
        path, data, method = written[0]
        self.assertIn('live324replay-sink-ordered', path)
        self.assertEqual(data['topics'], config.REPLAY_TOPIC)
        self.assertEqual(data['batch.size'], '500')
        self.assertEqual(data['consumer.override.max.poll.records'], '500')
        self.assertEqual(data['use.reduction.buffer'], 'false')
        self.assertEqual(method, 'PUT')

    def test_second_replay_requires_first_sink_stopped_and_uses_new_group(self):
        previous = os.environ.get('CDC_LIVE_CASE')
        os.environ['CDC_LIVE_CASE'] = '324replay1000'
        try:
            spec = importlib.util.spec_from_file_location(
                'cdc_324_second_config', ROOT / 'scripts/configure-v2-staging-cdc-live.py')
            config = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(config)
        finally:
            if previous is None:
                os.environ.pop('CDC_LIVE_CASE', None)
            else:
                os.environ['CDC_LIVE_CASE'] = previous

        written = []
        config.existing_credential = lambda kind: None
        config.kafka = lambda *args: config.REPLAY_TOPIC + '\n'
        config.topic_end = lambda topic: 2766655
        config.config = lambda kind: {}

        def api(path, value=None, method=None):
            if path == '/connectors':
                return [config.REPLAY_SOURCE, config.REPLAY_PREVIOUS_SINK,
                        'yeodam-migration-b1-live324replay-sink-ordered',
                        'yeodam-staging-outbox']
            if path.endswith('/status'):
                state = 'RUNNING' if 'outbox' in path else 'STOPPED'
                return {'connector': {'state': state}, 'tasks': [{'state': state}]}
            if path.endswith('/config/validate'):
                return {'error_count': 0}
            if path.endswith('/config'):
                written.append((path, value))
                return {}
            raise AssertionError(path)

        config.api = api
        config.register('replay-sink')
        self.assertEqual(len(written), 1)
        self.assertIn('live324replay1000-sink-ordered', written[0][0])
        self.assertEqual(written[0][1]['batch.size'], '1000')
        self.assertEqual(written[0][1]['consumer.override.max.poll.records'], '1000')


if __name__ == '__main__':
    unittest.main()
