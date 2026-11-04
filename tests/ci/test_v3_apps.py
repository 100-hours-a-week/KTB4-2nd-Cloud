"""Test actual app renderings and reject unsafe/incomplete deployment inputs."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v3_apps', ROOT / 'scripts/check-v3-apps.py')
apps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apps)
FIXTURE = ROOT / 'tests/ci/v3/apps-values.fixture.yaml'


class AppsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = apps.platform.load_documents(apps.render([FIXTURE]))
        cls.values = apps.load_values([FIXTURE])

    def configured(self, override):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'override.yaml'
            file.write_text(yaml.safe_dump(override))
            result = subprocess.run([os.environ['V3_HELM'], 'template', 'test', str(apps.CHART),
                                     '--kube-version', '1.36.0', '-f', str(FIXTURE), '-f', str(file)],
                                    text=True, capture_output=True)
            return result, apps.load_values([FIXTURE, file])

    def accepts(self, override):
        result, values = self.configured(override)
        self.assertEqual(result.returncode, 0, result.stderr)
        documents = apps.platform.load_documents(result.stdout)
        apps.validate(documents, values)
        return documents, values

    def rejects(self, override):
        result, values = self.configured(override)
        if result.returncode:
            return
        with self.assertRaises((apps.platform.ContractError, KeyError, ValueError)):
            apps.validate(apps.platform.load_documents(result.stdout), values)

    @staticmethod
    def deployment(documents, name='trip-api'):
        return next(doc for doc in documents if doc['kind'] == 'Deployment' and doc['metadata']['name'] == name)

    def test_default_values_render_no_resources(self):
        documents = apps.platform.load_documents(apps.render())
        self.assertEqual(documents, [])
        self.assertEqual(apps.validate(documents, apps.load_values())['enabledDeployments'], 0)

    def test_enabling_an_unconfigured_workload_fails_at_render(self):
        result = subprocess.run([os.environ['V3_HELM'], 'template', 'test', str(apps.CHART),
                                 '--kube-version', '1.36.0', '--set', 'workloads.trip-api.enabled=true'],
                                capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_six_configured_deployments_and_four_services_pass_strict_schema(self):
        result = apps.validate(self.documents, self.values)
        self.assertEqual(result['renderedResources'], {'Deployment': 6, 'Service': 4})
        apps.platform.schema_check(yaml.safe_dump_all(self.documents))
        self.assertEqual({doc['metadata']['name'] for doc in self.documents if doc['kind'] == 'Service'},
                         {'auth-api', 'trip-api', 'photo-api', 'trip-websocket'})

    def test_backend_digest_is_shared_and_websocket_independent(self):
        documents, values = self.accepts({'images': {'backend': 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:' + '3' * 64}})
        for document in documents:
            if document['kind'] != 'Deployment':
                continue
            image = document['spec']['template']['spec']['containers'][0]['image']
            if document['metadata']['name'] == 'trip-websocket':
                self.assertEqual(image, values['images']['websocket'])
            else:
                self.assertEqual(image, values['images']['backend'])
                self.assertEqual(document['spec']['template']['spec']['initContainers'][0]['image'], image)

    def test_per_workload_resource_override_does_not_change_other_copies(self):
        documents, _ = self.accepts({'workloads': {'trip-api': {'resources': {'requests': {'cpu': '250m'}}}}})
        self.assertEqual(self.deployment(documents)['spec']['template']['spec']['containers'][0]['resources']['requests']['cpu'], '250m')
        self.assertEqual(self.deployment(documents, 'photo-api')['spec']['template']['spec']['containers'][0]['resources']['requests']['cpu'], '100m')

    def test_configuration_revision_changes_template_not_selectors(self):
        documents, _ = self.accepts({'defaults': {'configurationRevision': 'next-fixture'}})
        old = self.deployment(self.documents)
        new = self.deployment(documents)
        self.assertEqual(old['spec']['selector'], new['spec']['selector'])
        self.assertNotEqual(old['spec']['template']['metadata']['annotations'], new['spec']['template']['metadata']['annotations'])

    def test_backend_seed_keeps_bundled_catalog_and_writable_tmp(self):
        pod = self.deployment(self.documents)['spec']['template']['spec']
        seed = pod['initContainers'][0]
        self.assertEqual(seed['command'], ['cp', '-R', '/app/data/.', '/catalog/'])
        self.assertNotIn('/app/data', [mount['mountPath'] for mount in seed['volumeMounts']])
        self.assertIn('/app/data', [mount['mountPath'] for mount in pod['containers'][0]['volumeMounts']])
        self.assertIn('/tmp', [mount['mountPath'] for mount in pod['containers'][0]['volumeMounts']])

    def test_config_and_secret_references_have_no_inline_secret_values(self):
        pod = self.deployment(self.documents)['spec']['template']['spec']
        container = pod['containers'][0]
        secret = next(item for item in container['env'] if item['name'] == 'EXAMPLE_API_KEY')
        self.assertNotIn('value', secret)
        self.assertEqual(secret['valueFrom']['secretKeyRef'], {'name': 'ci-only-secrets', 'key': 'API_KEY', 'optional': False})
        self.assertEqual(container['envFrom'], [{'configMapRef': {'name': 'ci-only-runtime', 'optional': False}}])
        self.assertFalse(any(doc['kind'] in ['Secret', 'ConfigMap', 'ServiceAccount', 'Namespace'] for doc in self.documents))

    def test_readonly_secret_file_mount(self):
        documents, _ = self.accepts({'workloads': {'trip-api': {'secretVolumes': [
            {'name': 'tls', 'secretName': 'ci-only-tls', 'mountPath': '/etc/yeodam/tls'}]}}})
        pod = self.deployment(documents)['spec']['template']['spec']
        mount = next(m for m in pod['containers'][0]['volumeMounts'] if m['name'] == 'tls')
        self.assertTrue(mount['readOnly'])
        self.assertEqual(next(v for v in pod['volumes'] if v['name'] == 'tls')['secret']['defaultMode'], 0o440)

    def test_reject_mutable_or_missing_images_and_per_workload_image_override(self):
        for override in [{'images': {'backend': ''}}, {'images': {'backend': 'ghcr.io/example/backend:latest'}},
                         {'workloads': {'trip-api': {'image': 'unreviewed'}}}]:
            with self.subTest(override=override):
                self.rejects(override)

    def test_reject_wrong_namespace_account_and_startup_role(self):
        for field, value in [('namespace', 'default'), ('serviceAccountName', 'trip-result-worker'),
                             ('component', 'trip-only'), ('imageRef', 'websocket')]:
            self.rejects({'workloads': {'trip-api': {field: value}}})

    def test_reject_request_above_limits_and_nonpositive_resources(self):
        for resource, value in [('memory', '2Gi'), ('cpu', '0m'), ('ephemeral-storage', '1Gi')]:
            self.rejects({'workloads': {'trip-api': {'resources': {'requests': {resource: value}}}}})
        self.rejects({'workloads': {'trip-api': {'resources': {'limits': {'cpu': '50m'}}}}})

    def test_reject_missing_required_resources_and_pull_secret(self):
        for override in [{'defaults': {'resources': {'requests': {'memory': ''}}}},
                         {'defaults': {'resources': {'limits': {'ephemeral-storage': ''}}}},
                         {'defaults': {'imagePullSecrets': []}}, {'defaults': {'configurationRevision': ''}}]:
            self.rejects(override)

    def test_reject_missing_probe_or_unknown_port(self):
        self.rejects({'defaults': {'probes': {'startup': None}}})
        self.rejects({'workloads': {'trip-api': {'probes': {'readiness': {'httpGet': {'port': 'missing'}}}}}})
        self.rejects({'workloads': {'trip-api': {'probes': {'liveness': {'successThreshold': 2}}}}})

    def test_reject_plaintext_secret_keys_and_duplicate_environment(self):
        for key in ['MYSQL_PASSWORD', 'JWT_SECRET', 'WEBSOCKET_INTERNAL_SERVICE_TOKEN', 'KAKAO_CLIENT_ID']:
            self.rejects({'defaults': {'environment': {key: 'ci-only-not-a-secret'}}})
        self.rejects({'defaults': {'environment': {'RELEASE': 'ci-only'},
                                  'secretRefs': {'RELEASE': {'name': 'ci-only', 'key': 'RELEASE'}}}})

    def test_reject_secret_mount_collision_and_system_paths(self):
        for name, path in [('scratch', '/etc/yeodam/tls'), ('tls', '/tmp'), ('tls', '/'),
                           ('tls', '/var/run/secrets/kubernetes.io/serviceaccount'), ('tls', '/etc/yeodam/../escape')]:
            self.rejects({'workloads': {'trip-api': {'secretVolumes': [{'name': name, 'secretName': 'ci-only', 'mountPath': path}]}}})

    def test_reject_disk_budgets_smaller_than_writable_volumes(self):
        self.rejects({'workloads': {'trip-api': {'resources': {'limits': {'ephemeral-storage': '64Mi'}}}}})

    def test_reject_zero_rollout_and_insufficient_shutdown_time(self):
        self.rejects({'defaults': {'rollout': {'maxSurge': 0, 'maxUnavailable': 0}}})
        self.rejects({'workloads': {'trip-api': {'rollout': {'maxUnavailable': 2}}}})
        self.rejects({'defaults': {'terminationGracePeriodSeconds': 5, 'preStopDelaySeconds': 5}})

    def test_replicas_and_worker_service_contract(self):
        self.assertEqual(self.deployment(self.documents)['spec']['replicas'], 1)
        self.assertEqual(self.deployment(self.documents, 'trip-websocket')['spec']['replicas'], 2)
        self.rejects({'workloads': {'trip-websocket': {'replicas': 1}}})
        self.rejects({'workloads': {'photo-converter': {'service': {'enabled': True}}}})

    def test_reject_api_service_pointing_to_worker_or_exposing_management(self):
        for mutation in [lambda d: d['spec']['selector'].update({'app.kubernetes.io/instance': 'trip-result-worker'}),
                         lambda d: d['spec']['ports'].append({'name': 'management', 'port': 9091, 'targetPort': 'management'}),
                         lambda d: d['spec'].update(type='LoadBalancer')]:
            documents = deepcopy(self.documents)
            service = next(d for d in documents if d['kind'] == 'Service' and d['metadata']['name'] == 'trip-api')
            mutation(service)
            with self.assertRaises(apps.platform.ContractError):
                apps.validate(documents, self.values)

    def test_reject_rendered_privilege_escalation_and_extra_resources(self):
        for mutation in [lambda pod: pod.update(automountServiceAccountToken=True),
                         lambda pod: pod.update(hostNetwork=True),
                         lambda pod: pod.update(hostPID=True),
                         lambda pod: pod.update(hostIPC=True),
                         lambda pod: pod['securityContext'].update(runAsUser=0),
                         lambda pod: pod['containers'][0].update(command=['run-one-role']),
                         lambda pod: pod['containers'][0]['securityContext'].update(allowPrivilegeEscalation=True),
                         lambda pod: pod['initContainers'][0].update(image='unreviewed')]:
            documents = deepcopy(self.documents)
            mutation(self.deployment(documents)['spec']['template']['spec'])
            with self.assertRaises(apps.platform.ContractError):
                apps.validate(documents, self.values)
        with self.assertRaises(apps.platform.ContractError):
            apps.validate(self.documents + [{'kind': 'Secret', 'metadata': {'namespace': 'trip', 'name': 'inline'}}], self.values)

    def test_enabling_frontend_requires_its_own_ports_uid_and_probes(self):
        self.rejects({'workloads': {'frontend': {'enabled': True}}})
        probes = {key: {**deepcopy(value), 'httpGet': {'path': '/healthz', 'port': 'http', 'scheme': 'HTTP'}}
                  for key, value in self.values['defaults']['probes'].items()}
        documents, _ = self.accepts({'images': {'frontend': 'ghcr.io/example/frontend@sha256:' + '4' * 64},
                                    'workloads': {'frontend': {'enabled': True, 'ports': {'http': 3000},
                                    'service': {'port': 3000}, 'security': {'runAsUser': 1001, 'runAsGroup': 1001, 'fsGroup': 1001},
                                    'probes': probes}}})
        pod = self.deployment(documents, 'frontend')['spec']['template']['spec']
        self.assertNotIn('initContainers', pod)


if __name__ == '__main__':
    unittest.main()
