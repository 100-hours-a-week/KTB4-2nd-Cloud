import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('runpod_update', ROOT / 'scripts/update-v2-runpod.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
URL = 'https://newpod-8000.proxy.runpod.net/v1'
OLD = 'https://oldpod-8000.proxy.runpod.net/v1'


def definition(environment='production'):
    return {'family': m.ENVIRONMENTS[environment][1], 'cpu': '512', 'taskRoleArn': 'task-role',
            'containerDefinitions': [{'name': 'ai-api', 'image': 'approved-image',
                'environment': [{'name': 'VLM_BASE_URL', 'value': OLD}, {'name': 'VLM_MODEL', 'value': 'gemma-4'}],
                'secrets': [{'name': 'VLM_API_KEY', 'valueFrom': environment + '-key-arn'}]}]}


class RunpodTests(unittest.TestCase):
    def test_rejects_non_runpod_hosts_and_url_tricks(self):
        for url in ['http://newpod-8000.proxy.runpod.net/v1', URL + '?x=1',
                    'https://newpod-8000.proxy.runpod.net.evil.test/v1',
                    'https://user@newpod-8000.proxy.runpod.net/v1', 'https://127.0.0.1/v1']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.validate_url(url)
        self.assertEqual(m.validate_url(URL), URL)

    def test_candidate_preserves_image_key_roles_and_original(self):
        for environment in m.ENVIRONMENTS:
            before = definition(environment)
            result = m.candidate(environment, before)
            expected = copy.deepcopy(before)
            container = expected['containerDefinitions'][0]
            container['environment'] = [{'name': 'VLM_MODEL', 'value': 'gemma-4'}]
            container['secrets'].append({'name': 'VLM_BASE_URL', 'valueFrom':
                f'arn:aws:ssm:{m.ai.REGION}:{m.ai.ACCOUNT}:parameter{m.parameter(environment)}'})
            self.assertEqual(result, expected)
            self.assertEqual(m.candidate(environment, result), result)
            self.assertEqual(before, definition(environment))

    def exercise(self, environment='production', failure=None):
        self.calls = []
        self.parameters = {m.STAGING_PARAMETER: {'Value': URL, 'Version': 7},
                           m.parameter('production'): {'Value': OLD, 'Version': 2}}
        if environment == 'staging':
            self.parameters[m.STAGING_PARAMETER] = {'Value': OLD, 'Version': 2}
        if failure == 'stale-before':
            self.parameters[m.STAGING_PARAMETER]['Version'] = 8
        def aws(*args):
            self.calls.append(args)
            if args[:2] == ('sts', 'get-caller-identity'):
                return {'Account': m.ai.ACCOUNT}
            if args[:2] == ('ssm', 'get-parameter'):
                return {'Parameter': copy.deepcopy(self.parameters[args[args.index('--name') + 1]])}
            if args[:2] == ('ssm', 'put-parameter'):
                name, value = args[args.index('--name') + 1], args[args.index('--value') + 1]
                self.parameters[name] = {'Value': value, 'Version': self.parameters[name]['Version'] + 1}
                if failure == 'ambiguous-write':
                    raise RuntimeError('write response unavailable')
                return {'Version': self.parameters[name]['Version']}
            if args[:2] == ('ecs', 'register-task-definition'):
                if failure == 'stale-during':
                    self.parameters[m.STAGING_PARAMETER]['Version'] += 1
                return {'taskDefinition': {'taskDefinitionArn': 'new-revision'}}
            return {}
        report = {'status': 'preflight'}
        def wait(*args):
            if args[1] == 'new-revision' and failure in {'rollout', 'external-write'}:
                if failure == 'external-write':
                    self.parameters[m.parameter(environment)] = {'Value': OLD, 'Version': 99}
                raise RuntimeError('rollout failed')
        with patch.object(m.ai, 'aws', side_effect=aws), \
             patch.object(m, 'service', return_value={'taskDefinition': 'old-revision'}), \
             patch.object(m.ai, 'stable', return_value=True), \
             patch.object(m, 'definition', return_value=definition(environment)), \
             patch.object(m, 'target_group', return_value='target'), \
             patch.object(m, 'healthy_tasks', return_value=True), \
             patch.object(m, 'probe', side_effect=m.ProbeError('probe failed') if failure == 'probe' else None) as probe, \
             patch.object(m, 'wait_api', side_effect=wait):
            if failure:
                with self.assertRaises(RuntimeError):
                    m.promote(environment, URL, report, lambda: None, 7 if environment == 'production' else None)
            else:
                m.promote(environment, URL, report, lambda: None, 7 if environment == 'production' else None)
            self.probes = probe.call_args_list
        return report

    def writes(self):
        return [c for c in self.calls if c[:2] == ('ssm', 'put-parameter')]

    def test_staging_success_exports_written_version(self):
        report = self.exercise('staging')
        self.assertEqual(report['status'], 'healthy')
        self.assertEqual(report['parameterVersion'], 3)
        self.assertTrue(all(c[c.index('--name') + 1] == m.STAGING_PARAMETER for c in self.writes()))

    def test_production_uses_only_production_writes_and_preserves_staging(self):
        report = self.exercise()
        self.assertEqual(report['status'], 'healthy')
        self.assertTrue(all(c[c.index('--name') + 1] == m.parameter('production') for c in self.writes()))
        self.assertEqual(self.parameters[m.STAGING_PARAMETER], {'Value': URL, 'Version': 7})
        self.assertEqual([c.args[0] for c in self.probes], ['production', 'production'])
        update = next(c for c in self.calls if c[:2] == ('ecs', 'update-service'))
        self.assertEqual(update[update.index('--cluster') + 1], m.ENVIRONMENTS['production'][0])
        self.assertEqual(update[update.index('--service') + 1], m.ENVIRONMENTS['production'][1])
        self.assertFalse(any(c[0] == 'ec2' or c[:2] == ('ssm', 'send-command') for c in self.calls))

    def test_stale_staging_receipt_stops_before_production_write(self):
        for failure in ['stale-before', 'stale-during']:
            self.exercise(failure=failure)
            self.assertFalse(self.writes())
            self.assertFalse(any(c[:2] == ('ecs', 'update-service') for c in self.calls))

    def test_failed_probe_does_not_register_or_write(self):
        self.exercise(failure='probe')
        self.assertFalse(self.writes())
        self.assertFalse(any(c[:2] in [('ecs', 'update-service'), ('ecs', 'register-task-definition')]
                             for c in self.calls))

    def test_failed_rollout_restores_only_its_url_and_forces_old_revision(self):
        report = self.exercise(failure='rollout')
        self.assertEqual([c[c.index('--value') + 1] for c in self.writes()], [URL, OLD])
        updates = [c for c in self.calls if c[:2] == ('ecs', 'update-service')]
        self.assertIn('--force-new-deployment', updates[-1])
        self.assertEqual(updates[-1][updates[-1].index('--task-definition') + 1], 'old-revision')
        self.assertEqual(report['status'], 'restored_previous')
        self.assertEqual(self.parameters[m.STAGING_PARAMETER], {'Value': URL, 'Version': 7})

    def test_recovery_does_not_clobber_external_parameter_write(self):
        report = self.exercise(failure='external-write')
        self.assertEqual(report['status'], 'recovery_required')
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.parameters[m.parameter('production')]['Version'], 99)

    def test_unknown_write_result_requires_inspection_before_another_write(self):
        report = self.exercise(failure='ambiguous-write')
        self.assertEqual(report['status'], 'recovery_required')
        self.assertEqual(len(self.writes()), 1)
        self.assertFalse(any(c[:2] == ('ecs', 'update-service') for c in self.calls))

    def test_old_healthy_target_cannot_attest_current_revision(self):
        def aws(*args):
            if args[1] == 'list-tasks':
                return {'taskArns': ['task']}
            if args[1] == 'describe-tasks':
                return {'tasks': [{'taskDefinitionArn': 'new', 'lastStatus': 'RUNNING', 'healthStatus': 'HEALTHY',
                    'containers': [{'networkInterfaces': [{'privateIpv4Address': '10.0.0.2'}]}]}]}
            return {'TargetHealthDescriptions': [{'Target': {'Id': '10.0.0.1'}, 'TargetHealth': {'State': 'healthy'}}]}
        with patch.object(m.ai, 'aws', side_effect=aws):
            self.assertFalse(m.healthy_tasks('production', {'taskDefinition': 'new', 'desiredCount': 1}, 'target'))

    def test_definition_rejects_cross_environment_key(self):
        row = definition()
        row['status'] = 'ACTIVE'
        with patch.object(m.ai, 'aws', return_value={'taskDefinition': row}), patch.object(m.ai, 'image_ref'):
            with self.assertRaisesRegex(ValueError, 'own environment'):
                m.definition('production', 'revision')

    def test_probe_uses_environment_key_via_stdin_only(self):
        with patch.object(m.ai, 'aws', return_value={'Parameter': {'Value': 'secret-value'}}) as aws, \
             patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
                          '{"data":[{"id":"gemma-4"}]}\n200', '')) as curl:
            m.probe('production', URL, 'gemma-4')
        self.assertIn(m.parameter('production', 'VLM_API_KEY'), aws.call_args.args)
        self.assertNotIn('secret-value', repr(curl.call_args.args))
        self.assertEqual(curl.call_args.kwargs['input'], 'Authorization: Bearer secret-value\n')

    def test_old_entry_point_cannot_select_production(self):
        for options in [['--environment', 'production'], ['--environment=production'], ['--env', 'production']]:
            with self.subTest(options=options):
                run = subprocess.run(['python3', str(ROOT / 'scripts/update-v2-staging-runpod.py'), URL,
                                      *options], capture_output=True, text=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertTrue('explicit environment' in run.stderr or 'unrecognized arguments' in run.stderr)

    def test_workflow_requires_staging_success_and_approved_production(self):
        text = (ROOT / '.github/workflows/v2-staging-runpod-update.yml').read_text()
        production = text.split('  production:\n', 1)[1]
        self.assertIn('needs: staging', production)
        self.assertIn('environment: production', production)
        self.assertIn('group: yeodam-v2-production-release', production)
        self.assertIn('AWS_V2_PRODUCTION_RUNPOD_UPDATE_ROLE_ARN', production)
        self.assertIn('--expected-staging-version "$STAGING_VERSION"', production)
        self.assertEqual(text.count('ref: ${{ github.sha }}'), 2)


if __name__ == '__main__':
    unittest.main()
