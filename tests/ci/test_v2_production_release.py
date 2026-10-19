import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('production_release', ROOT / 'scripts/deploy-v2-production-release.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
BASE = m.common.manifest.load(ROOT / m.common.MANIFEST)
REFS = m.common.image_refs(BASE)
SELECTED = {'stagingRunId': 123, 'stagingAttempt': 1, 'cloudCommit': 'a'*40,
            'ciRunId': 456, 'pr': 349, 'manifest': BASE}
BEFORE = {'images': REFS, 'revisions': {k: f'arn:aws:ecs:{m.REGION}:{m.ACCOUNT}:task-definition/{v}:2'
                                     for k, v in m.SERVICES.items()},
          'configs': {k: 'config-' + k for k in m.SERVICES}}


class CandidateTests(unittest.TestCase):
    def exercise(self, run_change=None, artifact_change=None, report_change=None, jobs_success=True, manifests=None):
        run = {'path': '.github/workflows/v2-staging-release.yml', 'head_branch': 'main',
               'head_repository': {'full_name': m.common.CLOUD}, 'event': 'push',
               'head_sha': 'a'*40, 'status': 'completed', 'conclusion': 'success', 'run_attempt': 2}
        run.update(run_change or {})
        artifact = {'id': 77, 'name': 'v2-staging-release-123-2', 'expired': False, 'size_in_bytes': 1000}
        artifact.update(artifact_change or {})
        report = {'schemaVersion': 1, 'status': 'healthy', 'cloudCommit': 'a'*40, 'pr': 349, 'ciRunId': 456}
        report.update(report_change or {})
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as archive:
            archive.writestr('release.json', json.dumps(report))
        def api(path):
            if path.endswith('/runs/123'):
                return run
            if '/artifacts?' in path:
                return {'artifacts': [artifact]}
            if '/pulls/' in path:
                return {'merged': True, 'merge_commit_sha': 'a'*40,
                        'base': {'ref': 'main', 'repo': {'full_name': m.common.CLOUD}},
                        'head': {'sha': 'b'*40, 'repo': {'full_name': m.common.CLOUD}}}
            if '/jobs?' in path:
                return {'jobs': [{'name': n, 'conclusion': 'success' if jobs_success else 'skipped'}
                                 for n in ['V2 image combination smoke', 'cloud-ci-gate']]}
            return {'id': 456, 'path': '.github/workflows/cloud-pr-gate.yml', 'event': 'pull_request',
                    'head_sha': 'b'*40, 'status': 'completed', 'conclusion': 'success',
                    'head_repository': {'full_name': m.common.CLOUD}}
        with patch.object(m, 'api', side_effect=api), patch.object(m.common, 'manifest_at', side_effect=manifests or [BASE, BASE]), \
             patch.object(m.subprocess, 'run', return_value=type('Result', (), {'stdout': buf.getvalue()})()):
            return m.staging_candidate('123')

    def test_successful_exact_attempt_is_bound_to_manifest_and_ci(self):
        result = self.exercise()
        self.assertEqual(result['stagingAttempt'], 2)
        self.assertEqual(result['manifest'], BASE)

    def test_failed_foreign_branch_and_non_deployment_workflow_rejected(self):
        for change in [{'conclusion': 'failure'}, {'head_branch': 'test'},
                       {'path': '.github/workflows/cloud-pr-gate.yml'},
                       {'head_repository': {'full_name': 'other/fork'}}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.exercise(run_change=change)

    def test_expired_and_previous_attempt_evidence_rejected(self):
        for change in [{'expired': True}, {'name': 'v2-staging-release-123-1'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.exercise(artifact_change=change)

    def test_unhealthy_report_and_mismatched_trigger_rejected(self):
        for change in [{'status': 'restored_previous'}, {'cloudCommit': 'c'*40}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.exercise(report_change=change)

    def test_changed_merge_manifest_and_skipped_ci_rejected(self):
        changed = copy.deepcopy(BASE)
        changed['components']['frontend']['digest'] = 'sha256:' + 'a'*64
        with self.assertRaises(ValueError):
            self.exercise(manifests=[BASE, changed])
        with self.assertRaises(ValueError):
            self.exercise(jobs_success=False)


class PreservedConfigurationTests(unittest.TestCase):
    def test_new_image_preserves_db_redis_secrets_roles_ports_and_health(self):
        current = {'family': m.SERVICES['backend'], 'networkMode': 'awsvpc',
                   'taskRoleArn': 'production-task', 'executionRoleArn': 'production-execution',
                   'containerDefinitions': [{'name': 'backend', 'image': REFS['backend'],
                       'environment': [{'name': 'MYSQL_URL', 'value': 'production-db'},
                                       {'name': 'AUTH_SESSION_KEY_PREFIX', 'value': 'production-auth:'},
                                       {'name': 'RELEASE', 'value': 'previous'}],
                       'secrets': [{'name': 'MYSQL_PASSWORD', 'valueFrom': 'production-parameter'}],
                       'healthCheck': {'command': ['CMD', 'true']},
                       'portMappings': [{'containerPort': 8080}]}]}
        new_image = 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:' + 'a'*64
        with patch.object(m, 'aws', return_value={'taskDefinition': {'taskDefinitionArn': 'revision'}}) as aws:
            m.register('backend', current, new_image)
        candidate = json.loads(aws.call_args.args[-1])
        self.assertEqual(m.config_hash(candidate), m.config_hash(current))
        self.assertEqual(candidate['containerDefinitions'][0]['image'], new_image)
        self.assertEqual(current['containerDefinitions'][0]['image'], REFS['backend'])

    def test_old_healthy_target_cannot_attest_a_new_unhealthy_task(self):
        service = {'taskDefinition': BEFORE['revisions']['frontend'], 'desiredCount': 1,
                   'loadBalancers': [{'targetGroupArn': f'arn:aws:elasticloadbalancing:{m.REGION}:{m.ACCOUNT}:targetgroup/{m.TARGETS["frontend"]}/id'}]}
        task = {'taskDefinitionArn': service['taskDefinition'], 'lastStatus': 'RUNNING',
                'healthStatus': 'UNHEALTHY', 'containers': [{'networkInterfaces': [{'privateIpv4Address': '10.20.10.2'}]}]}
        results = [{'taskArns': ['task']}, {'tasks': [task]},
                   {'TargetHealthDescriptions': [{'Target': {'Id': '10.20.10.1'}, 'TargetHealth': {'State': 'healthy'}}]}]
        with patch.object(m, 'aws', side_effect=results), self.assertRaisesRegex(RuntimeError, 'health is incomplete'):
            m.verify_targets('frontend', service)

    def test_unfinished_journal_blocks_all_new_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(m, 'aws', return_value={'Account': m.ACCOUNT}), \
                 patch.object(m, 'get_state', side_effect=[{'status': 'recovery_required', 'releaseId': 'pending'}, None]), \
                 patch.object(m, 'snapshot') as snapshot, patch.object(m, 'runner') as runner, \
                 self.assertRaisesRegex(RuntimeError, 'journal needs review'):
                m.promote(SELECTED, 'deploy', True, Path(directory))
            snapshot.assert_not_called()
            runner.assert_not_called()


class PromotionTests(unittest.TestCase):
    def exercise(self, changes=(), fail=None, unknown=False, finalization_fail=False,
                 first=False, drift=False, apply=True, force=False):
        selected = copy.deepcopy(SELECTED)
        for name in changes:
            selected['manifest']['components'][name]['digest'] = 'sha256:' + 'a'*64
        desired = m.common.image_refs(selected['manifest'])
        after = copy.deepcopy(BEFORE) | {'images': desired}
        last = {'schemaVersion': 1, **SELECTED, 'releaseId': 'previous'} if not first else None
        calls, writes = [], []
        def get(path):
            return last if path == m.LAST else None
        def put(path, data):
            writes.append((path, copy.deepcopy(data)))
            if finalization_fail and path == m.LAST:
                raise RuntimeError('State write outcome unknown')
        def update(name, revision):
            calls.append(('ecs', name, revision))
            if fail == name and revision == 'new-' + name:
                raise RuntimeError('Health failed')
        def runner(action='inspect', image=''):
            calls.append(('runner', action, image))
            if unknown and action == 'deploy':
                raise TimeoutError('SSM outcome unknown: command-id')
            return {'image': image or (REFS['ai-app'] if len(calls) == 1 or fail else desired['ai-app'])}
        before = copy.deepcopy(BEFORE)
        if drift:
            before['images']['backend'] = 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:' + 'c'*64
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(m, 'aws', return_value={'Account': m.ACCOUNT}), patch.object(m, 'get_state', side_effect=get), \
                 patch.object(m, 'put_state', side_effect=put), patch.object(m, 'snapshot', side_effect=[before, BEFORE if fail else after]), \
                 patch.object(m, 'definition', return_value={}), patch.object(m, 'register', side_effect=lambda n, d, i: 'new-' + n), \
                 patch.object(m, 'update', side_effect=update), patch.object(m, 'runner', side_effect=runner):
                if fail or unknown or finalization_fail or drift or (first and changes):
                    with self.assertRaises((ValueError, RuntimeError, TimeoutError)):
                        m.promote(selected, 'deploy', apply, Path(directory), force)
                else:
                    m.promote(selected, 'deploy', apply, Path(directory), force)
                report = json.loads((Path(directory)/'release.json').read_text())
        return report, calls, writes

    def test_initial_attestation_does_not_redeploy_healthy_apps(self):
        report, calls, writes = self.exercise(first=True)
        self.assertEqual(report['status'], 'healthy')
        self.assertFalse(any(c[0] == 'ecs' for c in calls))
        self.assertTrue(any(path == m.LAST for path, data in writes))

    def test_read_only_plan_never_sends_ssm_or_writes_state(self):
        report, calls, writes = self.exercise(changes=['frontend'], apply=False)
        self.assertEqual(report['status'], 'planned')
        self.assertEqual((calls, writes), ([], []))

    def test_drift_and_unattested_first_release_stop_before_mutation(self):
        for args in [{'drift': True}, {'first': True, 'changes': ['frontend']}]:
            report, calls, writes = self.exercise(**args)
            self.assertEqual((calls, writes), ([], []))

    def test_only_changed_component_is_deployed(self):
        report, calls, writes = self.exercise(changes=['frontend'])
        self.assertEqual([c[1] for c in calls if c[0] == 'ecs'], ['frontend'])
        self.assertTrue(any(path == m.PREVIOUS for path, data in writes))

    def test_partial_failure_recovers_attempted_services_in_reverse_order(self):
        report, calls, writes = self.exercise(changes=m.ORDER, fail='frontend')
        self.assertEqual(report['status'], 'restored_previous')
        self.assertEqual([c[1] for c in calls if c[0] == 'ecs'],
                         ['ai-app', 'backend', 'frontend', 'frontend', 'backend', 'ai-app'])
        self.assertFalse(any(path == m.LAST for path, data in writes))

    def test_unknown_ssm_never_starts_competing_rollback(self):
        report, calls, writes = self.exercise(changes=['ai-app'], unknown=True)
        self.assertEqual(report['status'], 'recovery_required')
        self.assertEqual(len([c for c in calls if c[0] == 'ecs']), 1)
        self.assertEqual(len([c for c in calls if c[:2] == ('runner', 'deploy')]), 1)

    def test_state_finalization_failure_does_not_roll_back_healthy_apps(self):
        report, calls, writes = self.exercise(changes=['frontend'], finalization_fail=True)
        self.assertEqual(report['status'], 'recovery_required')
        self.assertEqual(len([c for c in calls if c[0] == 'ecs']), 1)

    def test_explicit_frontend_recreation_leaves_be_ai_unchanged(self):
        report, calls, writes = self.exercise(force=True)
        self.assertEqual([c[1] for c in calls if c[0] == 'ecs'], ['frontend'])
        self.assertFalse(any(path == m.PREVIOUS for path, data in writes))


class RunnerUncertaintyTests(unittest.TestCase):
    def test_submission_and_polling_errors_remain_unknown(self):
        for effects in [[RuntimeError('Network response unavailable')],
                        [{'Command': {'CommandId': 'command-id'}}, RuntimeError('GetCommandInvocation unavailable')]]:
            with self.subTest(effects=effects), patch.object(m, 'aws', side_effect=effects), \
                 self.assertRaisesRegex(TimeoutError, 'SSM outcome unknown'):
                m.runner('deploy', REFS['ai-app'])


if __name__ == '__main__':
    unittest.main()
