import importlib.util
from datetime import datetime
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecoveryProjectionTests(unittest.TestCase):
    def auth(self):
        dummy = types.ModuleType('cdc_auth368')
        for name in ('apply_change', 'decrypt_event', 'parse_change'):
            setattr(dummy, name, Mock())
        with patch.dict(sys.modules, {'cdc_auth368': dummy}):
            return load('auth383journal', 'b-integration376-auth.py')

    def test_source_recovery_reply_is_mirrored_without_redis_session_mutation(self):
        module = self.auth()
        cursor = Mock()
        connection = Mock()
        connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
        connection.cursor.return_value.__exit__ = Mock(return_value=False)
        client = Mock()
        event = {'source': {'table': 'migration_auth_request_result'}, 'before': None,
                 'after': {'request_id': 'opaque-id', 'encrypted_result': 'ciphertext',
                           'created_at': '2026-10-21T00:00:01.123456Z'}}
        module.project(event, 'source', connection, client, {})
        sql, args = cursor.execute.call_args.args
        self.assertIn('`migration_auth_request_result`', sql)
        self.assertIn('ON DUPLICATE KEY UPDATE', sql)
        self.assertEqual(args, ('opaque-id', 'ciphertext', datetime(2026, 10, 21, 0, 0, 1, 123456)))
        module.apply_change.assert_not_called()
        client.time.assert_not_called()

    def test_recovery_reply_delete_uses_its_own_primary_key(self):
        module = self.auth()
        cursor = Mock()
        connection = Mock()
        connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
        connection.cursor.return_value.__exit__ = Mock(return_value=False)
        module.mirror({'source': {'table': 'migration_auth_request_result'},
                       'before': {'request_id': 'opaque-id'}, 'after': None}, connection)
        self.assertEqual(cursor.execute.call_args.args,
                         ('DELETE FROM `migration_auth_request_result` WHERE `request_id`=%s', ('opaque-id',)))

    def test_recovery_journal_stays_encrypted_and_does_not_feed_back_from_target(self):
        module = load('kafka383journal', 'b-integration376-kafka.py')
        run = '26b45888-9b29-4afb-b090-ff2acbfc3ab6'
        source = module.source(run, 'auth-source', 0)
        target = module.source(run, 'auth-target', 0)
        table = 'yeodam_v1_source.migration_auth_request_result'
        self.assertIn(table, source['table.include.list'].split(','))
        self.assertNotIn(table, target['table.include.list'].split(','))
        self.assertIsNotNone(re.fullmatch(source['predicates.isAuth.pattern'], 'prefix.' + table))
        self.assertIn('INTERVAL 120 SECOND', source['snapshot.select.statement.overrides.' + table])


if __name__ == '__main__':
    unittest.main()
