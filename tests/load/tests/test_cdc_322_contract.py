"""Guard the three isolated #322 namespaces and original-route restrictions."""

import base64
import importlib.util
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    'cdc_sender', ROOT / 'scripts/send-v2-staging-cdc-live-ssm.py')
SENDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SENDER)


def transferred(source, case):
    commands = []
    SENDER.append_file(commands, '/run/cdc-test', source, case)
    parts = [re.search(r"printf '%s' '([^']+)' >>", line).group(1)
             for line in commands if line.startswith("printf '%s'")]
    return base64.b64decode(''.join(parts))


class Cdc322ContractTest(unittest.TestCase):
    def test_each_run_uses_its_own_schema_source_and_offsets(self):
        namespaces = set()
        for case, server_id in (('32210', '5406'), ('32230', '5407'),
                                ('322100', '5408')):
            with self.subTest(case=case):
                source = json.loads(transferred(SENDER.SOURCE_CONFIG, case))
                sink = json.loads(transferred(SENDER.SINK_CONFIG, case))
                ddl = transferred(SENDER.DDL, case).decode()
                namespace = source['topic.prefix']
                self.assertNotIn(namespace, namespaces)
                namespaces.add(namespace)
                self.assertEqual(source['name'], f'yeodam-migration-b1-live{case}-source')
                self.assertEqual(source['database.server.id'], server_id)
                self.assertEqual(source['snapshot.mode'], 'initial')
                self.assertEqual(source['schema.history.internal.kafka.topic'],
                                 namespace + '.schema-history')
                self.assertIn('/yeodam_cdc_live' + case + '?', sink['connection.url'])
                self.assertEqual(sink['connection.pool.max_size'], '2')
                self.assertIn('yeodam_cdc_live' + case, ddl)
                self.assertNotIn('yeodam_cdc_live313', ddl)

    def test_original_cases_cannot_switch_topic_or_pause_source(self):
        for case in ('318', '32210', '32230', '322100'):
            with self.subTest(case=case):
                for phase in ('ordered-source', 'ordered-sink', 'pause-source',
                              'resume-source', 'pause-legacy-sinks'):
                    with self.assertRaises(ValueError):
                        SENDER.command_for(phase, case=case)

    def test_generator_deploy_includes_native_summary_runner(self):
        instance, commands = SENDER.command_for('generator-deploy', case='32210')
        self.assertEqual(instance, SENDER.GENERATOR)
        self.assertTrue(any('install -o ubuntu -g ubuntu -m 0755' in line and
                            '/scripts/run-k6.sh' in line for line in commands))


if __name__ == '__main__':
    unittest.main()
