"""Check the one-use Bulk import boundary without replaying production data."""

import base64
import gzip
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


SENDER = module('cdc331sender', 'scripts/send-v2-staging-cdc-live-ssm.py')
BULK = module('cdc331bulk', 'scripts/cdc-bulk331.py')


def transferred(source):
    return transferred_case(source, '331')


def transferred_case(source, case):
    commands = []
    SENDER.append_file(commands, '/run/cdc-331-test', source, case)
    parts = [re.search(r"printf '%s' '([^']+)' >>", line).group(1)
             for line in commands if line.startswith("printf '%s'")]
    return base64.b64decode(''.join(parts))


class Cdc331BulkContractTest(unittest.TestCase):
    def test_isolated_schema_source_and_one_use_phases(self):
        source = json.loads(transferred(SENDER.SOURCE_CONFIG))
        sink = json.loads(transferred(SENDER.SINK_CONFIG))
        ddl = transferred(SENDER.DDL).decode()
        self.assertEqual(source['database.server.id'], '5412')
        self.assertEqual(source['topic.prefix'], 'staging.yeodam.migration.b1.live331')
        self.assertIn('/yeodam_cdc_live331?', sink['connection.url'])
        self.assertIn('yeodam_cdc_live331', ddl)
        self.assertIn('--default-character-set=utf8mb4',
                      BULK.mysql_command('destination')[-2])
        run_id = '318582ec-33a2-4901-9b8d-ac3fb43f2828'
        for phase in ('bulk-capture', 'bulk-destination', 'ordered-sink'):
            SENDER.command_for(phase, run_id, '331')
            with self.assertRaises(ValueError):
                SENDER.command_for(phase, case='331')
        for phase in ('sink-users', 'ordered-source', 'pause-source', 'replay-sink'):
            with self.assertRaises(ValueError):
                SENDER.command_for(phase, case='331')
        retry_source = json.loads(transferred_case(SENDER.SOURCE_CONFIG, '331r'))
        self.assertEqual(retry_source['database.server.id'], '5413')
        self.assertEqual(retry_source['topic.prefix'],
                         'staging.yeodam.migration.b1.live331r')
        self.assertIn('yeodam_cdc_live331r', transferred_case(SENDER.DDL, '331r').decode())
        _, retry_sink_commands = SENDER.command_for('ordered-sink', run_id, '331r')
        self.assertTrue(any('export CDC_BULK_RUN_ID=' + run_id in command
                            for command in retry_sink_commands))
        ordered_source = json.loads(transferred_case(SENDER.SOURCE_CONFIG, '331s'))
        self.assertEqual(ordered_source['database.server.id'], '5414')
        self.assertIn('yeodam_cdc_live331s', transferred_case(SENDER.DDL, '331s').decode())
        _, ordered_sink_commands = SENDER.command_for('ordered-sink', run_id, '331s')
        self.assertTrue(any('export CDC_BULK_RUN_ID=' + run_id in command
                            for command in ordered_sink_commands))

    def test_source_is_no_data_and_delta_sink_requires_import_receipt(self):
        previous = os.environ.get('CDC_LIVE_CASE')
        os.environ['CDC_LIVE_CASE'] = '331'
        try:
            config = module('cdc331config', 'scripts/configure-v2-staging-cdc-live.py')
        finally:
            if previous is None:
                os.environ.pop('CDC_LIVE_CASE', None)
            else:
                os.environ['CDC_LIVE_CASE'] = previous
        config.existing_credential = lambda kind: None
        config.kafka = lambda *args: '\n'.join(config.topic_names() + (config.ORDERED_TOPIC,))
        config.topic_end = lambda topic: 0
        config.config = lambda kind: {
            'name': config.SOURCE, 'topic.prefix': config.PREFIX,
            'database.server.id': '5412', 'snapshot.mode': 'initial'}
        written = []

        def api(path, value=None, method=None):
            if path == '/connectors':
                return []
            if path == '/connectors/' + config.SOURCE + '/config' and method is None:
                return written[0]
            if path == '/connectors/' + config.SOURCE + '/status':
                return {'connector': {'state': 'RUNNING'},
                        'tasks': [{'state': 'RUNNING'}]}
            if path == '/connectors/' + config.SOURCE + '/offsets':
                return {'offsets': [{'offset': {'snapshot': None, 'file': 'binlog.1',
                                                'pos': 123}}]}
            if path.endswith('/config/validate'):
                return {'error_count': 0}
            if path.endswith('/config'):
                written.append(value)
                return {}
            raise AssertionError(path)

        config.api = api
        config.register('initial-source')
        self.assertEqual(written[0]['snapshot.mode'], 'no_data')
        self.assertEqual(written[0]['transforms.route.topic.replacement'], config.ORDERED_TOPIC)
        config.topic_end = lambda topic: 1
        with self.assertRaises(RuntimeError):
            config.register('ordered-sink')

    def test_dump_shape_rejects_fk_disable_and_wrong_table_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'dump.gz'
            def write(text):
                with gzip.open(path, 'wt') as file:
                    file.write(text)
            valid = ''.join(f'INSERT INTO `{table}` VALUES (1);\n' for table in BULK.TABLES)
            write(valid)
            BULK.assert_dump_shape(path)
            write('SET FOREIGN_KEY_CHECKS=0;\n' + valid)
            with self.assertRaisesRegex(RuntimeError, 'FK checks'):
                BULK.assert_dump_shape(path)
            write(valid.replace('`trips`', '`unexpected`'))
            with self.assertRaisesRegex(RuntimeError, 'unexpected table'):
                BULK.assert_dump_shape(path)

    def test_s3_copy_direction_is_explicit(self):
        commands = []
        with patch.object(BULK, 'run', side_effect=lambda cmd: commands.append(cmd)):
            BULK.s3_upload(Path('/run/snapshot.ready.json'), 'migration/cdc-live331/x/ready')
            BULK.s3_download('migration/cdc-live331/x/ready',
                             Path('/run/snapshot.ready.json'))
        self.assertEqual(commands[0][0:4],
                         ['aws', 's3', 'cp', '/run/snapshot.ready.json'])
        self.assertTrue(commands[1][3].startswith('s3://'))
        self.assertEqual(commands[1][4], '/run/snapshot.ready.json')


if __name__ == '__main__':
    unittest.main()
