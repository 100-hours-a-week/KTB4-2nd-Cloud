"""Exercise staged manifests and read-only prerequisite failures without AWS or a cluster."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


rollout = module('rollout_test', 'prepare-v3-backend-rollout.py')
preflight = module('preflight_test', 'check-v3-backend-prerequisites.py')
supply = module('supply_test', 'prepare-v3-backend-supply.py')


class Rollout(unittest.TestCase):
    def setUp(self):
        self.current = rollout.apps.load_values([
            rollout.BASE, ROOT / 'tests/ci/v3/backend-values.fixture.yaml', rollout.CONNECTIONS])
        self.review = {'backendSourceCommit': self.current['defaults']['configMapData']['RELEASE'],
                       'redisSentinelFeatureReviewed': True, 'resultWorkerAiPathReviewed': False}

    def test_cumulative_roles_same_image_and_existing_manifests_preserved(self):
        current = self.current
        for stage, expected in [('apis', 3), ('conversion', 4), ('all', 5)]:
            self.review['resultWorkerAiPathReviewed'] = stage == 'all'
            candidate = rollout.prepare(current, stage, self.review)
            _, before = rollout.render_checked(current)
            source, after = rollout.render_checked(candidate)
            rollout.verify_preservation(before, after)
            rollout.apps.platform.schema_check(source)
            deployments = [d for d in after if d['kind'] == 'Deployment']
            self.assertEqual(len(deployments), expected)
            self.assertEqual({d['spec']['template']['spec']['containers'][0]['image'] for d in deployments},
                             {current['images']['backend']})
            for deployment in deployments:
                c = deployment['spec']['template']['spec']['containers'][0]
                self.assertEqual(c['resources']['requests']['memory'], '512Mi')
                self.assertEqual(c['resources']['limits']['memory'], '1Gi')
            current = candidate

    def test_foundation_replacement_does_not_double_pods(self):
        self.assertEqual(self.current['defaults']['rollout'], {'maxSurge': 0, 'maxUnavailable': 1})

    def test_no_skipping_or_removing_roles(self):
        with self.assertRaises(ValueError): rollout.prepare(self.current, 'all', self.review)
        apis = rollout.prepare(self.current, 'apis', self.review)
        conversion = rollout.prepare(apis, 'conversion', self.review)
        with self.assertRaises(ValueError): rollout.prepare(conversion, 'apis', self.review)
        self.assertEqual(rollout.prepare(apis, 'apis', self.review), apis)

    def test_review_is_source_bound_and_explicit(self):
        for changes in [{'backendSourceCommit': 'f' * 40}, {'redisSentinelFeatureReviewed': False},
                        {'redisSentinelFeatureReviewed': 'true'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                rollout.prepare(self.current, 'apis', dict(self.review, **changes))

    def test_result_worker_needs_ai_review(self):
        apis = rollout.prepare(self.current, 'apis', self.review)
        conversion = rollout.prepare(apis, 'conversion', self.review)
        with self.assertRaises(ValueError): rollout.prepare(conversion, 'all', self.review)

    def test_budget_increase_and_extra_workload_rejected(self):
        mutated = deepcopy(self.current)
        mutated['defaults']['resources']['requests']['memory'] = '1Gi'
        with self.assertRaises(ValueError): rollout.prepare(mutated, 'apis', self.review)
        changed = deepcopy(self.current)
        changed['workloads']['frontend']['enabled'] = True
        with self.assertRaises(ValueError): rollout.prepare(changed, 'apis', self.review)

    def test_existing_auth_configuration_or_image_cannot_change_silently(self):
        candidate = rollout.prepare(self.current, 'apis', self.review)
        _, before = rollout.render_checked(self.current)
        for change in ['image', 'config']:
            changed = deepcopy(candidate)
            if change == 'image': changed['images']['backend'] = 'ghcr.io/100-hours-a-week/backend@sha256:' + 'a' * 64
            else: changed['defaults']['configMapData']['FRONTEND_ORIGIN'] = 'https://different.invalid'
            _, after = rollout.render_checked(changed)
            with self.assertRaises(ValueError): rollout.verify_preservation(before, after)

    def test_wrong_listener_is_rejected_by_actual_helm(self):
        candidate = rollout.prepare(self.current, 'apis', self.review)
        candidate['workloads']['photo-api']['configMapData']['ATTACHMENT_CONVERSION_CONSUMER_ENABLED'] = 'true'
        with self.assertRaises(subprocess.CalledProcessError): rollout.render_checked(candidate)

    def test_cli_writes_validated_reference_only_values(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / 'current.json').write_text(json.dumps(self.current))
            (d / 'review.json').write_text(json.dumps(self.review))
            command = [os.sys.executable, str(ROOT / 'scripts/prepare-v3-backend-rollout.py'),
                       '--current-values', str(d / 'current.json'), '--stage', 'apis',
                       '--review', str(d / 'review.json'), '--output', str(d / 'candidate.json')]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(rollout.stage_name(json.loads((d / 'candidate.json').read_text())), 'apis')
            self.assertIn('No deployment', result.stdout)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def resources(self, documents):
        result = {}
        for document in documents:
            kind, ns, name = rollout.identity(document)
            item = deepcopy(document)
            if kind == 'Deployment':
                item['metadata']['generation'] = 2
                item['status'] = dict(observedGeneration=2, replicas=1, readyReplicas=1, updatedReplicas=1, availableReplicas=1)
            result[(ns, kind.lower(), name)] = item
        return result

    def preflight_fixture(self):
        candidate = rollout.prepare(self.current, 'apis', self.review)
        _, before = rollout.render_checked(self.current)
        _, after = rollout.render_checked(candidate)
        objects = self.resources(before)
        for role in rollout.enabled(candidate):
            ns = rollout.NAMESPACES[role]
            objects[(ns, 'serviceaccount', role)] = {'metadata': {'name': role}}
            refs = rollout.apps.merge(candidate['defaults'], candidate['workloads'][role])['secretRefs']
            for ref in refs.values():
                objects.setdefault((ns, 'secret-metadata', ref['name']), {'type': 'Opaque', 'keys': []})['keys'].append(ref['key'])
            objects[(ns, 'secret-metadata', 'backend-trust')] = {'type': 'Opaque', 'keys': ['mysql.crt', 'redis.crt', 'mongodb.crt', 'kafka.crt']}
            objects[(ns, 'secret-metadata', 'ghcr-pull')] = {'type': 'kubernetes.io/dockerconfigjson', 'keys': ['.dockerconfigjson']}
            for name in ('backend-credentials', 'kafka-' + role):
                objects[(ns, 'externalsecret', name)] = {'spec': {'target': {'name': name}, 'secretStoreRef': {'kind': 'SecretStore', 'name': 'aws-secretsmanager'}, 'data': [
                    {'remoteRef': {'key': 'yeodam/v3/foundation/' + ns + '/backend'}}]},
                    'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        return candidate, before, after, objects

    def test_read_only_preflight_and_runtime_defaulting(self):
        candidate, before, after, objects = self.preflight_fixture()
        objects[('auth', 'deployment', 'auth-api')]['spec'].setdefault('progressDeadlineSeconds', 600)
        result = preflight.check(self.current, candidate, before, after, self.review,
                                 lambda ns, kind, name: objects[(ns, kind, name)])
        self.assertEqual(result['replicas'], 3)
        self.assertNotIn('password', json.dumps(result))

    def test_missing_or_unsynced_secret_and_changed_auth_stop_preflight(self):
        for failure in ['missing-key', 'wrong-source', 'not-ready', 'auth-image', 'stale-generation']:
            candidate, before, after, objects = self.preflight_fixture()
            if failure == 'missing-key': objects[('trip', 'secret-metadata', 'backend-credentials')]['keys'] = []
            if failure == 'wrong-source': objects[('photo', 'externalsecret', 'backend-credentials')]['spec']['data'][0]['remoteRef']['key'] = 'yeodam/v2/production/backend'
            if failure == 'not-ready': objects[('trip', 'externalsecret', 'kafka-trip-api')]['status']['conditions'][0]['status'] = 'False'
            if failure == 'auth-image': objects[('auth', 'deployment', 'auth-api')]['spec']['template']['spec']['containers'][0]['image'] = 'wrong'
            if failure == 'stale-generation': objects[('auth', 'deployment', 'auth-api')]['status']['observedGeneration'] = 1
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                preflight.check(self.current, candidate, before, after, self.review,
                                lambda ns, kind, name: objects[(ns, kind, name)])

    def test_preflight_cannot_bypass_review_or_change_other_settings(self):
        candidate, before, after, objects = self.preflight_fixture()
        with self.assertRaises(ValueError):
            preflight.check(self.current, candidate, before, after,
                            dict(self.review, redisSentinelFeatureReviewed=False), lambda *args: None)
        candidate['images']['backend'] = 'wrong'
        with self.assertRaises(ValueError):
            preflight.check(self.current, candidate, before, after, self.review, lambda *args: None)

    def test_preflight_cli_uses_only_read_operations_and_secret_key_projection(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from types import SimpleNamespace
        candidate, before, after, objects = self.preflight_fixture()
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if 'config' in args:
                value = json.dumps({'contexts': [{'context': {'cluster': preflight.CLUSTER}}]})
            else:
                ns = args[args.index('-n') + 1]
                i = args.index('get'); kind, name = args[i + 1:i + 3]
                if kind == 'secret':
                    self.assertNotIn('json', args)
                    template = args[args.index('-o') + 1]
                    self.assertIn('{{$key}}', template)
                    self.assertNotIn('{{$value}}', template)
                    item = objects[(ns, 'secret-metadata', name)]
                    value = '\n'.join([item['type'], *item['keys']])
                else: value = json.dumps(objects[(ns, kind, name)])
            return SimpleNamespace(returncode=0, stdout=value, stderr='')
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory); review = d / 'review.json'; review.write_text(json.dumps(self.review))
            argv = ['check', '--current-values', 'current', '--candidate-values', 'candidate',
                    '--review', str(review), '--context', 'reviewed-context']
            output = StringIO()
            with patch('sys.argv', argv), patch.object(preflight.rollout, 'load_current', side_effect=[self.current, candidate]), \
                 patch.object(preflight.rollout, 'render_checked', side_effect=[('', before), ('', after)]), \
                 patch.object(preflight.subprocess, 'run', side_effect=run), redirect_stdout(output):
                preflight.main()
            self.assertEqual(json.loads(output.getvalue())['targetStage'], 'apis')
        self.assertTrue(all('get' in c or 'config' in c for c in calls))
        self.assertTrue(all('--context' in c and 'reviewed-context' in c for c in calls))

    def test_selected_supply_does_not_touch_existing_auth(self):
        sources = {'trip': {'fixture': 'private'}, 'photo': {'fixture': 'private'}}
        from types import SimpleNamespace
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if 'get-caller-identity' in args: return SimpleNamespace(returncode=0, stdout=json.dumps({'Account': '483175530259'}), stderr='')
            if 'describe-secret' in args: return SimpleNamespace(returncode=1, stdout='', stderr='ResourceNotFoundException')
            return SimpleNamespace(returncode=0, stdout='private-output-withheld', stderr='')
        with patch.object(supply.subprocess, 'run', side_effect=run), patch('builtins.print') as output:
            supply.create(sources, 'fixture')
        self.assertEqual(sum('create-secret' in call for call in calls), 2)
        self.assertNotIn('yeodam/v3/foundation/auth/backend', json.dumps(calls))
        self.assertNotIn('private-output-withheld', str(output.call_args_list))


if __name__ == '__main__':
    unittest.main()
