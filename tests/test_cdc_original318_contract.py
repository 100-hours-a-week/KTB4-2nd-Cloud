import base64
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DISPATCH = load_script('cdc_original318_dispatch', ROOT / 'scripts/send-v2-staging-cdc-live-ssm.py')
with patch.dict(os.environ, CDC_LIVE_CASE='318'):
    CONFIGURE = load_script('cdc_original318_configure', ROOT / 'scripts/configure-v2-staging-cdc-live.py')


def transferred_bytes(commands, path):
    suffix = ' >> ' + path + '.b64'
    chunks = [line.split("'")[3] for line in commands
              if line.startswith("printf '%s' '") and line.endswith(suffix)]
    if not chunks:
        raise AssertionError('missing transferred file: ' + path)
    return base64.b64decode(''.join(chunks), validate=True)


class Original318ContractTest(unittest.TestCase):
    def test_new_namespace_and_source_id_without_changing_the_313_source(self):
        _, commands = DISPATCH.command_for('initial-source', case='318')
        source = json.loads(transferred_bytes(commands, '/run/yeodam-cdc-live318-source.json'))
        sink = json.loads(transferred_bytes(commands, '/run/yeodam-cdc-live318-sink.json'))
        self.assertEqual(source['database.server.id'], '5405')
        self.assertEqual(source['snapshot.mode'], 'initial')
        self.assertNotIn('transforms', source)
        self.assertEqual(source['topic.prefix'], CONFIGURE.PREFIX)
        self.assertEqual(source['name'], CONFIGURE.SOURCE)
        self.assertIn('/yeodam_cdc_live318?', sink['connection.url'])
        self.assertNotIn('live313', json.dumps(source) + json.dumps(sink))
        original = json.loads((ROOT / 'deploy/v2-staging/kafka/cdc-live313-source.json').read_text())
        self.assertEqual(original['database.server.id'], '5404')

    def test_new_destination_keeps_both_foreign_keys(self):
        _, commands = DISPATCH.command_for('destination', case='318')
        ddl = transferred_bytes(commands, '/run/yeodam-cdc-live318-destination.sql.b64data').decode()
        script = transferred_bytes(commands, '/run/yeodam-cdc-live318-destination.sh').decode()
        self.assertIn('FOREIGN KEY (user_id)', ddl)
        self.assertIn('FOREIGN KEY (trip_id)', ddl)
        self.assertIn('yeodam_cdc_live318', ddl + script)
        self.assertNotIn('yeodam_cdc_live313', ddl + script)

    def test_original_run_disallows_pause_route_and_recovery(self):
        self.assertEqual(CONFIGURE.PREFIX, 'staging.yeodam.migration.b1.live318')
        for phase in ('pause-source', 'ordered-source', 'ordered-sink',
                      'pause-legacy-sinks', 'resume-legacy-trips'):
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, 'outside'):
                DISPATCH.command_for(phase, case='318')
        for phase in DISPATCH.ORIGINAL_PHASES:
            run_id = ('00000000-0000-4000-8000-000000000318'
                      if phase.startswith('trip-') or phase == 'auth-refresh-smoke' else None)
            _, commands = DISPATCH.command_for(phase, run_id, case='318')
            self.assertIn('export CDC_LIVE_CASE=318', commands)
            self.assertLess(max(len(line.encode()) for line in commands), 4096)


if __name__ == '__main__':
    unittest.main()
