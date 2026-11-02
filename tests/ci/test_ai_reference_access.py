import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('ai_reference_sso', ROOT / 'scripts/configure-ai-reference-sso.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ReferenceAccessTests(unittest.TestCase):
    def setUp(self):
        self.before = {'Version': '2012-10-17', 'Statement': [
            {'Sid': 'ExistingSSM', 'Effect': 'Allow', 'Action': 'ssm:StartSession', 'Resource': 'fixture'}]}

    def test_additive_idempotent_and_removable_without_losing_existing_access(self):
        after = m.merge(self.before)
        self.assertEqual(after['Statement'][0], self.before['Statement'][0])
        self.assertEqual(m.merge(after), after)
        self.assertEqual(m.merge(after, remove=True), self.before)
        self.assertEqual(len(self.before['Statement']), 1)

    def test_conflicting_sid_not_overwritten_or_removed(self):
        for remove in [False, True]:
            conflict = m.merge(self.before)
            conflict['Statement'][-1]['Resource'] = '*'
            with self.assertRaisesRegex(ValueError, 'differs'):
                m.merge(conflict, remove=remove)

    def test_hash_mismatch_does_not_call_aws(self):
        aws = Mock()
        with self.assertRaisesRegex(RuntimeError, 'reviewed hash'):
            m.apply_policy(aws, self.before, m.merge(self.before), 'stale', '/unused')
        aws.assert_not_called()

    def test_concurrent_change_stops_before_mutation(self):
        aws = Mock(return_value={'InlinePolicy': json.dumps({'Statement': []})})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'Concurrent'):
                m.apply_policy(aws, self.before, m.merge(self.before), m.digest(self.before), directory)
            backups = list(Path(directory).glob('*.json'))
            self.assertEqual(json.loads(backups[0].read_text()), self.before)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(aws.call_count, 1)

    def test_prefix_scope_and_no_runner_write_or_uploader_delete(self):
        policies = json.loads((ROOT / 'deploy/ai-reference/policies.json').read_text())
        for name in ['uploader', 'runner']:
            for statement in policies[name]['Statement']:
                actions = statement['Action']
                actions = [actions] if isinstance(actions, str) else actions
                self.assertFalse(any('Delete' in action or action == 's3:*' for action in actions))
                if name == 'runner':
                    self.assertTrue(all(action in ['s3:GetObject', 's3:GetObjectVersion', 's3:ListBucket', 's3:GetBucketLocation'] for action in actions))
                if actions == ['s3:ListBucket']:
                    self.assertEqual(statement['Condition']['StringLike']['s3:prefix'], 'ai-reference/places/*')
                elif any(action in ['s3:GetObject', 's3:PutObject'] for action in actions):
                    self.assertTrue(statement['Resource'].endswith('/ai-reference/places/*'))


if __name__ == '__main__':
    unittest.main()
