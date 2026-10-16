import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('runpod_update', Path(__file__).resolve().parents[2] / 'scripts/update-v2-staging-runpod.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
URL = 'https://newpod-8000.proxy.runpod.net/v1'
OLD = 'https://oldpod-8000.proxy.runpod.net/v1'


def definition():
    return {'family': m.ai.FAMILY, 'containerDefinitions': [{
        'name': 'ai-api', 'image': 'approved-image',
        'environment': [{'name': 'VLM_BASE_URL', 'value': OLD}, {'name': 'VLM_MODEL', 'value': 'gemma-4'}],
        'secrets': [{'name': 'VLM_API_KEY', 'valueFrom': 'key-arn'}]}]}


class RunpodTests(unittest.TestCase):
    def test_rejects_non_runpod_hosts_and_url_tricks(self):
        for url in ['http://newpod-8000.proxy.runpod.net/v1', URL + '?x=1',
                    'https://newpod-8000.proxy.runpod.net.evil.test/v1',
                    'https://user@newpod-8000.proxy.runpod.net/v1', 'https://127.0.0.1/v1']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.validate_url(url)
        self.assertEqual(m.validate_url(URL), URL)

    def test_candidate_preserves_image_key_and_source_definition(self):
        before = definition()
        candidate = m.candidate(before)['containerDefinitions'][0]
        self.assertEqual(candidate['image'], 'approved-image')
        self.assertEqual(candidate['secrets'], [{'name': 'VLM_API_KEY', 'valueFrom': 'key-arn'},
                                                {'name': 'VLM_BASE_URL', 'valueFrom': m.PARAMETER_ARN}])
        self.assertNotIn('VLM_BASE_URL', [e['name'] for e in candidate['environment']])
        self.assertEqual(before['containerDefinitions'][0]['environment'][0]['value'], OLD)
        self.assertEqual(len(m.candidate(m.candidate(before))['containerDefinitions'][0]['secrets']), 2)

    def fake_aws(self, *args):
        self.calls.append(args)
        if args[:2] == ('sts', 'get-caller-identity'): return {'Account': m.ai.ACCOUNT}
        if args[:2] == ('ssm', 'get-parameter'): return {'Parameter': {'Value': OLD, 'Version': 1}}
        if args[:2] == ('elbv2', 'describe-target-groups'): return {'TargetGroups': [{'TargetGroupArn': 'target'}]}
        if args[:2] == ('ecs', 'register-task-definition'): return {'taskDefinition': {'taskDefinitionArn': 'new-revision'}}
        return {}

    def exercise(self, probe_failure=False, deployment_failure=False):
        self.calls = []
        report = {'status': 'preflight'}
        with patch.object(m.ai, 'aws', side_effect=self.fake_aws), \
             patch.object(m.ai, 'service', return_value={'taskDefinition': 'old-revision'}), \
             patch.object(m.ai, 'stable', return_value=True), \
             patch.object(m.ai, 'definition', return_value=definition()), \
             patch.object(m, 'probe', side_effect=RuntimeError('probe failed') if probe_failure else None), \
             patch.object(m.ai, 'update_api', side_effect=RuntimeError('rollout failed') if deployment_failure else None), \
             patch.object(m.ai, 'wait_api'):
            if probe_failure or deployment_failure:
                with self.assertRaises(RuntimeError): m.promote(URL, report, lambda: None)
            else:
                m.promote(URL, report, lambda: None)
        return report

    def test_failed_probe_does_not_mutate_parameter_or_service(self):
        self.exercise(probe_failure=True)
        self.assertFalse(any(c[:2] in [('ssm', 'put-parameter'), ('ecs', 'update-service'),
                                      ('ecs', 'register-task-definition')] for c in self.calls))

    def test_failed_rollout_restores_parameter_and_restarts_old_revision(self):
        report = self.exercise(deployment_failure=True)
        writes = [c for c in self.calls if c[:2] == ('ssm', 'put-parameter')]
        self.assertEqual([c[c.index('--value') + 1] for c in writes], [URL, OLD])
        rollback = next(c for c in self.calls if c[:2] == ('ecs', 'update-service'))
        self.assertIn('--force-new-deployment', rollback)
        self.assertEqual(rollback[rollback.index('--task-definition') + 1], 'old-revision')
        self.assertEqual(report['status'], 'restored_previous')

    def test_success_does_not_touch_runner_or_gpu(self):
        report = self.exercise()
        self.assertEqual(report['status'], 'healthy')
        self.assertFalse(any(c[0] == 'ec2' or c[:2] == ('ssm', 'send-command') for c in self.calls))


if __name__ == '__main__': unittest.main()
