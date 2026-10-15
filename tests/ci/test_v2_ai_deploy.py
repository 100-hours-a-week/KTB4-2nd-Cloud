import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[2] / 'scripts/deploy-v2-staging-ai.py'
spec = importlib.util.spec_from_file_location('ai_deploy', path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
OLD = mod.PACKAGE + '@sha256:' + '1' * 64
NEW = mod.PACKAGE + '@sha256:' + '2' * 64
PREV = f'arn:aws:ecs:{mod.REGION}:{mod.ACCOUNT}:task-definition/{mod.FAMILY}:3'
NEXT = PREV.rsplit(':', 1)[0] + ':4'


def definition():
    return {'family': mod.FAMILY, 'status': 'ACTIVE', 'taskDefinitionArn': PREV,
            'revision': 3, 'executionRoleArn': 'execution-role', 'taskRoleArn': 'task-role',
            'cpu': '512', 'memory': '1024', 'networkMode': 'awsvpc',
            'containerDefinitions': [{'name': 'ai-api', 'image': OLD,
                'environment': [{'name': 'RELEASE', 'value': 'old'}, {'name': 'VLM_MODEL', 'value': 'gemma-4'}],
                'secrets': [{'name': 'API_KEY', 'valueFrom': 'ssm-arn'}]}]}


class DeployTest(unittest.TestCase):
    def test_rejects_wrong_package_and_mutable_image(self):
        for value in ['ghcr.io/100-hours-a-week/yeodam-ai-worker@sha256:' + '1'*64,
                      mod.PACKAGE + ':latest', NEW + ';echo x']:
            with self.assertRaises(ValueError):
                mod.image_ref(value)

    def test_only_image_and_release_change(self):
        before = definition()
        after = mod.candidate_definition(before, NEW)
        self.assertEqual(before['containerDefinitions'][0]['image'], OLD)
        self.assertNotIn('revision', after)
        self.assertEqual(after['taskRoleArn'], before['taskRoleArn'])
        self.assertEqual(after['containerDefinitions'][0]['secrets'], before['containerDefinitions'][0]['secrets'])
        self.assertIn({'name': 'VLM_MODEL', 'value': 'gemma-4'}, after['containerDefinitions'][0]['environment'])
        self.assertIn({'name': 'RELEASE', 'value': NEW}, after['containerDefinitions'][0]['environment'])

    def exercise(self, failure=None, action='deploy'):
        current = {'status': 'ACTIVE', 'taskDefinition': PREV, 'desiredCount': 1,
                   'runningCount': 1, 'pendingCount': 0,
                   'deployments': [{'status': 'PRIMARY', 'rolloutState': 'COMPLETED'}]}
        def aws(*args):
            if args[:2] == ('sts', 'get-caller-identity'):
                return {'Account': mod.ACCOUNT}
            if args[:2] == ('ec2', 'describe-instances'):
                return {'Reservations': [{'Instances': [{'State': {'Name': 'running'}}]}]}
            if args[:2] == ('elbv2', 'describe-target-groups'):
                return {'TargetGroups': [{'TargetGroupArn': f'arn:aws:elasticloadbalancing:{mod.REGION}:{mod.ACCOUNT}:targetgroup/{mod.TARGET}/123'}]}
            if args[:2] == ('ecs', 'register-task-definition'):
                return {'taskDefinition': {'taskDefinitionArn': NEXT}}
            raise AssertionError(args)
        initial = {'image': OLD, 'configuredImage': OLD, 'health': 'healthy'}
        results = [initial, {'image': NEW, 'health': 'healthy'}]
        if failure:
            results = [initial, failure, {'image': OLD, 'health': 'healthy'}]
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            env = {'AI_DEPLOY_REPORT': str(report), 'AWS_REGION': mod.REGION}
            with patch.dict(os.environ, env), patch.object(sys, 'argv', ['deploy', action, NEW if action == 'deploy' else '3']), \
                 patch.object(mod, 'aws', side_effect=aws), patch.object(mod, 'service', return_value=current), \
                 patch.object(mod, 'definition', return_value=definition()), \
                 patch.object(mod, 'runner', side_effect=results) as runner, \
                 patch.object(mod, 'update_api') as update, patch.object(mod, 'wait_api'):
                if failure:
                    with self.assertRaises(type(failure)):
                        mod.main()
                else:
                    mod.main()
                return json.loads(report.read_text()), runner.call_args_list, update.call_args_list

    def test_runner_failure_restores_both_previous_targets(self):
        report, runner, update = self.exercise(RuntimeError('Runner failed'))
        self.assertEqual(report['status'], 'restored_previous')
        self.assertEqual(update[-1].args[0], PREV)
        self.assertEqual(runner[-1].args, ('deploy', OLD))

    def test_unknown_ssm_outcome_does_not_start_competing_update(self):
        report, runner, update = self.exercise(TimeoutError('Runner status unknown; inspect before recovery'))
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(len(runner), 2)
        self.assertEqual(len(update), 1)

    def test_rollback_derives_runner_image_from_api_revision(self):
        report, runner, update = self.exercise(action='rollback')
        self.assertEqual(report['status'], 'healthy')
        self.assertEqual(runner[-1].args, ('deploy', OLD))

    def test_gpu_stopped_blocks_all_mutations(self):
        def aws(*args):
            if args[0] == 'sts':
                return {'Account': mod.ACCOUNT}
            return {'Reservations': [{'Instances': [{'State': {'Name': 'stopped'}}]}]}
        with patch.object(sys, 'argv', ['deploy', 'deploy', NEW]), \
             patch.object(mod, 'aws', side_effect=aws), patch.object(mod, 'runner') as runner:
            with self.assertRaisesRegex(RuntimeError, 'GPU is stopped'):
                mod.main()
            runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
