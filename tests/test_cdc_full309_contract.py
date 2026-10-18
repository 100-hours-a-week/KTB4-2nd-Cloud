import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIGURE = load_script('cdc_full309_configure', ROOT / 'scripts/configure-v2-staging-cdc-full.py')
DISPATCH = load_script('cdc_full309_dispatch', ROOT / 'scripts/send-v2-staging-cdc-full-ssm.py')


class FullSnapshotContractTest(unittest.TestCase):
    def test_full_snapshot_has_new_identity_and_no_pilot_filter(self):
        source = json.loads((ROOT / 'deploy/v2-staging/kafka/cdc-full309-source.json').read_text())
        pilot = json.loads((ROOT / 'deploy/v2-staging/kafka/cdc-pilot304-source.json').read_text())
        for key in ('name', 'database.server.id', 'topic.prefix',
                    'schema.history.internal.kafka.topic'):
            self.assertNotEqual(source[key], pilot[key])
        self.assertEqual(source['snapshot.mode'], 'initial')
        self.assertFalse(any(key.startswith('snapshot.select.statement.overrides') for key in source))
        self.assertEqual(source['table.include.list'], pilot['table.include.list'])

    def test_full_sink_targets_new_schema_with_existing_restricted_account(self):
        sink = json.loads((ROOT / 'deploy/v2-staging/kafka/cdc-full309-sink.json').read_text())
        self.assertTrue(sink['connection.url'].endswith('/yeodam_cdc_full309?sslMode=REQUIRED'))
        self.assertEqual(sink['connection.username'], 'yeodam_cdc_sink')
        ddl = (ROOT / 'deploy/v2-staging/kafka/cdc-full309-destination.sql').read_text()
        self.assertNotIn('CREATE TABLE yeodam_cdc.', ddl)
        self.assertIn('FOREIGN KEY (user_id)', ddl)
        self.assertIn('FOREIGN KEY (trip_id)', ddl)

    def test_dispatch_targets_only_migration_hosts_with_bounded_lines(self):
        for phase in DISPATCH.PHASES:
            run_id = '00000000-0000-4000-8000-000000000309' if phase == 'api-marker' else None
            instance, commands = DISPATCH.command_for(phase, run_id)
            expected = (DISPATCH.SOURCE_DB if phase in ('attest-source', 'api-marker') else
                        DISPATCH.DESTINATION if phase in ('destination', 'attest-destination') else
                        DISPATCH.KAFKA)
            self.assertEqual(instance, expected)
            self.assertLess(max(len(command.encode()) for command in commands), 4096)
            self.assertFalse(any('delete-topic' in command or 'reset-offset' in command for command in commands))

    def test_existing_full_source_cannot_be_replaced_by_initial_config(self):
        with patch.object(CONFIGURE, 'kafka', return_value='\n'.join(CONFIGURE.topic_names())), \
             patch.object(CONFIGURE, 'existing_credential'), \
             patch.object(CONFIGURE, 'config', return_value={
                 'name': CONFIGURE.SOURCE, 'topic.prefix': CONFIGURE.PREFIX}), \
             patch.object(CONFIGURE, 'api', return_value=[CONFIGURE.SOURCE]):
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                CONFIGURE.register('initial-source')

    def test_snapshot_sinks_cannot_pause_while_source_runs(self):
        with patch.object(CONFIGURE, 'api', return_value={'connector': {'state': 'RUNNING'}}):
            with self.assertRaisesRegex(RuntimeError, 'pause source'):
                CONFIGURE.pause_legacy_sinks()

    def test_region_sink_requires_drained_trip_sink(self):
        def response(path, *_args):
            if path == '/connectors':
                return [CONFIGURE.SOURCE]
            if path.endswith('/config'):
                return {}
            if path.endswith('/offsets'):
                return {'offsets': [{'offset': {'snapshot': None}}]}
            if path.endswith('/status'):
                return {'connector': {'state': 'RUNNING'}, 'tasks': [{'state': 'RUNNING'}]}
            raise AssertionError(path)

        with patch.object(CONFIGURE, 'kafka', return_value='\n'.join(CONFIGURE.topic_names())), \
             patch.object(CONFIGURE, 'existing_credential'), \
             patch.object(CONFIGURE, 'config', return_value={}), \
             patch.object(CONFIGURE, 'api', side_effect=response), \
             patch.object(CONFIGURE, 'sink_lag', return_value=(0, 1, 1)):
            with self.assertRaisesRegex(RuntimeError, 'parent snapshot sink has not drained'):
                CONFIGURE.register('sink-trip_regions')


if __name__ == '__main__':
    unittest.main()
