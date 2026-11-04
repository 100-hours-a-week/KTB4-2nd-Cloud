import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('ws_runtime', ROOT / 'scripts/configure-production-websocket.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
SOURCE = json.loads(m.SOURCE.read_text())
OLD = f'arn:aws:ecs:{m.REGION}:{m.ACCOUNT}:task-definition/{m.BACKEND}:4'
NEW = f'arn:aws:ecs:{m.REGION}:{m.ACCOUNT}:task-definition/{m.BACKEND}:5'
CURRENT = {'taskDefinitionArn': OLD, 'status': 'ACTIVE', 'family': m.BACKEND,
           'taskRoleArn': 'existing-task-role', 'executionRoleArn': 'existing-execution-role',
           'cpu': '1024', 'memory': '2048', 'networkMode': 'awsvpc',
           'containerDefinitions': [{'name': 'backend', 'image': SOURCE['backendImage'],
               'environment': [{'name': 'MYSQL_URL', 'value': 'existing-database'},
                               {'name': 'RELEASE', 'value': 'existing-release'},
                               {'name': 'AUTH_SESSION_KEY_PREFIX', 'value': 'existing-auth:'}],
               'secrets': [{'name': 'MYSQL_PASSWORD', 'valueFrom': 'existing-mysql-parameter'},
                           {'name': 'REDIS_PASSWORD', 'valueFrom': 'existing-redis-secret'}],
               'healthCheck': {'command': ['CMD', 'existing-check']},
               'repositoryCredentials': {'credentialsParameter': 'existing-ghcr-secret'},
               'portMappings': [{'containerPort': 8080}], 'stopTimeout': 120}]}
SERVICE = {'serviceName': m.BACKEND, 'clusterArn': f'arn:aws:ecs:{m.REGION}:{m.ACCOUNT}:cluster/{m.CLUSTER}',
           'status': 'ACTIVE', 'taskDefinition': OLD, 'desiredCount': 1, 'runningCount': 1, 'pendingCount': 0,
           'serviceRegistries': [{'registryArn': f'arn:aws:servicediscovery:{m.REGION}:{m.ACCOUNT}:service/srv-reviewed'}],
           'deployments': [{'status': 'PRIMARY', 'rolloutState': 'COMPLETED'}]}


class RuntimeTests(unittest.TestCase):
    def test_patch_preserves_image_database_auth_secrets_roles_and_resources(self):
        with patch.object(m, 'definition', return_value=CURRENT):
            result = m.patched_definition(CURRENT)
        self.assertEqual(CURRENT['containerDefinitions'][0]['image'], SOURCE['backendImage'])
        expected = copy.deepcopy(result)
        c = expected['containerDefinitions'][0]
        c['environment'] = [v for v in c['environment'] if v['name'] != 'WEBSOCKET_EVENTS_CHANNEL']
        c['secrets'] = [v for v in c['secrets'] if v['name'] != 'WEBSOCKET_INTERNAL_SERVICE_TOKEN']
        before = {k: v for k, v in CURRENT.items() if k in m.production.common.ai.FIELDS}
        # Ordering is irrelevant; every pre-existing value must be identical.
        before = copy.deepcopy(before)
        for item in [before['containerDefinitions'][0], c]:
            item['environment'] = sorted(item['environment'], key=lambda v: v['name'])
            item['secrets'] = sorted(item['secrets'], key=lambda v: v['name'])
        self.assertEqual(expected, before)

    def test_newer_or_foreign_backend_image_is_rejected(self):
        for change in [{'family': 'staging-backend'}, {'status': 'INACTIVE'},
                       {'containerDefinitions': [{'name': 'backend', 'image': 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:' + 'a'*64}]}]:
            with self.subTest(change=change), patch.object(m, 'aws', return_value={'taskDefinition': CURRENT | change}), self.assertRaises(ValueError):
                m.definition(OLD)

    def test_foreign_account_is_rejected(self):
        with patch.object(m, 'aws', return_value={'Account': '111111111111'}), self.assertRaises(ValueError):
            m.identity()

    def test_existing_plaintext_token_or_different_channel_is_not_overwritten(self):
        for item in [{'name': 'WEBSOCKET_INTERNAL_SERVICE_TOKEN', 'value': 'must-not-print'},
                     {'name': 'WEBSOCKET_EVENTS_CHANNEL', 'value': 'other-environment'}]:
            d = copy.deepcopy(CURRENT)
            d['containerDefinitions'][0]['environment'].append(item)
            with patch.object(m, 'definition', return_value=d), self.assertRaises(ValueError):
                m.patched_definition(d)

    def test_token_reference_cannot_silently_switch_to_another_parameter(self):
        d = copy.deepcopy(CURRENT)
        d['containerDefinitions'][0]['secrets'].append({'name': 'WEBSOCKET_INTERNAL_SERVICE_TOKEN', 'valueFrom': 'other-token'})
        with patch.object(m, 'definition', return_value=d), self.assertRaises(ValueError):
            m.patched_definition(d)

    def exercise(self, apply=False, fail=False, drift=False, exists=True):
        calls = []
        def aws(*args, payload=None):
            calls.append((args, payload))
            if args[:2] == ('ecs', 'register-task-definition'):
                return {'taskDefinition': {'taskDefinitionArn': NEW}}
            if args[:2] == ('ecs', 'update-service'):
                if fail:
                    raise RuntimeError('submission outcome unknown')
                return {}
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            seq = [SERVICE, SERVICE, SERVICE | {'taskDefinition': 'concurrent'}] if drift else [SERVICE]*3
            with patch.object(m, 'service', side_effect=seq), patch.object(m, 'ensure_idle'), \
                    patch.object(m, 'verify_discovery'), patch.object(m, 'definition', return_value=CURRENT), \
                    patch.object(m, 'token_metadata', return_value=exists), patch.object(m, 'aws', side_effect=aws), \
                    patch.object(m, 'wait_backend'):
                if fail or drift:
                    with self.assertRaises(RuntimeError):
                        m.configure(True, path)
                    result = json.loads(path.read_text())
                    self.assertEqual(result['status'], 'review-required')
                elif not exists and apply:
                    with self.assertRaises(ValueError):
                        m.configure(True, path)
                    self.assertFalse(path.exists())
                    result = {}
                else:
                    result = m.configure(apply, path)
                    self.assertEqual(path.exists(), apply)
                    if apply:
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return result, calls

    def test_default_is_plan_without_mutations(self):
        result, calls = self.exercise()
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(calls, [])

    def test_real_patch_registers_preserved_definition_and_only_updates_backend(self):
        result, calls = self.exercise(apply=True)
        self.assertEqual(result['status'], 'backend-configured')
        payload = calls[0][1]
        self.assertEqual(payload['containerDefinitions'][0]['image'], SOURCE['backendImage'])
        self.assertIn(m.BACKEND, calls[1][0])
        self.assertNotIn(m.WEBSOCKET, calls[1][0])

    def test_unknown_submission_remains_review_required_without_blind_rollback(self):
        _, calls = self.exercise(fail=True)
        self.assertEqual(len(calls), 2)

    def test_concurrent_backend_revision_is_not_overwritten(self):
        _, calls = self.exercise(drift=True)
        self.assertEqual(len(calls), 1)

    def test_missing_token_blocks_rollout_before_any_mutation(self):
        _, calls = self.exercise(apply=True, exists=False)
        self.assertEqual(calls, [])

    def test_rollback_refuses_unrelated_active_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            path.write_text(json.dumps({'schemaVersion': 1, 'status': 'backend-configured', 'previousTaskDefinition': OLD, 'taskDefinition': NEW}))
            with patch.object(m, 'definition', return_value=CURRENT), patch.object(m, 'service', return_value=SERVICE | {'taskDefinition': 'unrelated'}), self.assertRaises(ValueError):
                m.rollback(path, True)

    def test_rollback_requires_websocket_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.json'
            path.write_text(json.dumps({'schemaVersion': 1, 'status': 'backend-configured', 'previousTaskDefinition': OLD, 'taskDefinition': NEW}))
            with patch.object(m, 'definition', return_value=CURRENT), patch.object(m, 'service', side_effect=[SERVICE | {'taskDefinition': NEW}, {'desiredCount': 1}]), self.assertRaises(ValueError):
                m.rollback(path, True)

    def test_token_payload_is_private_temporary_file_and_removed_even_on_error(self):
        captured = []
        def run(cmd, **kwargs):
            path = Path(cmd[-1].removeprefix('file://'))
            captured.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())['Value'], 'must-not-print')
            self.assertNotIn('must-not-print', ' '.join(cmd))
            return type('Result', (), {'returncode': 1, 'stderr': 'must-not-print', 'stdout': ''})()
        with patch.object(m.subprocess, 'run', side_effect=run), self.assertRaisesRegex(RuntimeError, 'response omitted'):
            m.aws('ssm', 'put-parameter', payload={'Value': 'must-not-print'})
        self.assertFalse(captured[0].exists())

    def test_production_release_in_progress_blocks_configuration(self):
        replies = [{'Parameters': [{'Name': m.production.ACTIVE}]}, {'Parameter': {'Value': json.dumps({'status': 'deploying'})}}]
        with patch.object(m, 'aws', side_effect=replies), self.assertRaises(ValueError):
            m.ensure_idle(SERVICE)


if __name__ == '__main__':
    unittest.main()
