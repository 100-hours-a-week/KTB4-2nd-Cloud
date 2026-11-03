"""Exercise real Helm output plus adversarial identity/policy mutations."""
import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('v3_platform', ROOT / 'scripts/check-v3-platform.py')
platform = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(platform)


class PlatformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = platform.render()
        cls.documents = platform.load_documents(cls.source)
        cls.values = yaml.safe_load((platform.CHART / 'values.yaml').read_text())
        cls.accounts = platform.validate_platform(cls.documents, cls.values)

    def mutated(self, kind, name, mutation):
        documents = copy.deepcopy(self.documents)
        document = next(doc for doc in documents if doc['kind'] == kind and doc['metadata']['name'] == name)
        mutation(document)
        return documents

    def reject(self, documents):
        with self.assertRaises(platform.ContractError):
            platform.validate_platform(documents, self.values)

    def test_render_has_only_the_reviewed_resources(self):
        self.assertEqual(len(self.documents), 23)
        self.assertEqual(len(self.accounts), 8)
        self.assertEqual(sum(labels[platform.NAME] == 'backend' for labels in self.accounts.values()), 5)

    def test_reject_missing_namespace_and_duplicate_resources(self):
        self.reject(self.documents[1:])
        self.reject(self.documents + [self.documents[0]])

    def test_reject_system_namespace_and_unreviewed_privileges(self):
        self.reject(self.documents + [{'kind': 'Namespace', 'metadata': {'name': 'kube-system'}}])
        self.reject(self.documents + [{'kind': 'ClusterRoleBinding', 'metadata': {'name': 'app-admin'}}])

    def test_reject_api_token_or_aws_permission_injection(self):
        for mutation in [lambda d: d.update(automountServiceAccountToken=True),
                         lambda d: d.update(secrets=[{'name': 'legacy-token'}]),
                         lambda d: d['metadata'].update(annotations={'eks.amazonaws.com/role-arn': 'unreviewed'})]:
            self.reject(self.mutated('ServiceAccount', 'trip-api', mutation))

    def test_reject_namespace_deletion_on_release_removal(self):
        self.reject(self.mutated('Namespace', 'trip', lambda d: d['metadata'].pop('annotations')))

    def test_reject_nonmatching_serviceaccount_identity(self):
        self.reject(self.mutated('ServiceAccount', 'trip-api',
                                lambda d: d['metadata']['labels'].update({platform.INSTANCE: 'trip-result-worker'})))

    def test_reject_ingress_only_or_selective_default_deny(self):
        for mutation in [lambda d: d['spec'].update(policyTypes=['Ingress']),
                         lambda d: d['spec'].update(podSelector={'matchLabels': {platform.NAME: 'backend'}}),
                         lambda d: d['spec'].update(egress=[{}])]:
            self.reject(self.mutated('NetworkPolicy', 'default-deny', mutation))

    def test_reject_dns_namespace_or_pod_selector_union(self):
        def union(document):
            peer = document['spec']['egress'][0]['to'][0]
            document['spec']['egress'][0]['to'] = [
                {'namespaceSelector': peer['namespaceSelector']}, {'podSelector': peer['podSelector']},
            ]
        self.reject(self.mutated('NetworkPolicy', 'allow-cluster-dns', union))

    def test_reject_wildcard_dns_peers_and_ports(self):
        for mutation in [lambda d: d['spec']['egress'][0].update(to=[{'namespaceSelector': {}}]),
                         lambda d: d['spec']['egress'][0].pop('ports'),
                         lambda d: d['spec']['egress'][0]['ports'].append({'protocol': 'TCP', 'port': 443})]:
            self.reject(self.mutated('NetworkPolicy', 'allow-cluster-dns', mutation))

    def test_reject_duplicate_yaml_keys(self):
        with self.assertRaises(platform.ContractError):
            platform.load_documents('kind: Namespace\nkind: ServiceAccount\n')

    def test_values_schema_rejects_privilege_and_unknown_topology(self):
        overrides = [
            'namespaces:\n  kube-system:\n    workloads: {}\n',
            'namespaces:\n  auth:\n    workloads:\n      auth-api:\n        name: auth-only\n',
            'clusterAdmin: true\n',
        ]
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'override.yaml'
            for value in overrides:
                file.write_text(value)
                result = subprocess.run([os.environ['V3_HELM'], 'template', 'negative', str(platform.CHART),
                                         '--kube-version', '1.36.0', '-f', str(file)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)

    def test_strict_kubernetes_schema_rejects_unknown_fields(self):
        source = yaml.safe_dump({'apiVersion': 'v1', 'kind': 'Namespace',
                                 'metadata': {'name': 'trip'}, 'unexpectedPrivilege': True})
        with self.assertRaises(subprocess.CalledProcessError):
            platform.schema_check(source)

    def deployment(self, instance='trip-api'):
        labels = copy.deepcopy(self.accounts[('trip', instance)])
        return {'apiVersion': 'apps/v1', 'kind': 'Deployment',
                'metadata': {'name': instance, 'namespace': 'trip', 'labels': copy.deepcopy(labels)},
                'spec': {'replicas': 1, 'selector': {'matchLabels': platform.stable_selector(labels)},
                         'template': {'metadata': {'labels': labels}, 'spec': {
                             'serviceAccountName': instance,
                             'containers': [{'name': 'backend', 'image': 'example.invalid/backend:fixture',
                                             'resources': {'requests': {'cpu': '100m', 'memory': '512Mi'}}}],
                         }}}}

    def service(self):
        return {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'namespace': 'trip', 'name': 'trip-api'},
                'spec': {'selector': platform.stable_selector(self.accounts[('trip', 'trip-api')]),
                         'ports': [{'port': 8080, 'targetPort': 8080}]}}

    def test_same_image_distinct_workload_routing_and_version_rollout(self):
        api = self.deployment()
        worker = self.deployment('trip-result-worker')
        service = self.service()
        platform.validate_workloads([api, worker, service], self.accounts)
        platform.schema_check(yaml.safe_dump_all([api, worker, service]))
        api['spec']['template']['metadata']['labels']['app.kubernetes.io/version'] = 'new-commit'
        platform.validate_workloads([api, worker, service], self.accounts)
        selector = service['spec']['selector']
        self.assertTrue(all(api['spec']['template']['metadata']['labels'][k] == v for k, v in selector.items()))
        self.assertFalse(all(worker['spec']['template']['metadata']['labels'][k] == v for k, v in selector.items()))

    def test_reject_service_backend_only_or_wrong_worker_target(self):
        for selector in [{platform.NAME: 'backend'},
                         platform.stable_selector(self.accounts[('trip', 'trip-result-worker')])]:
            service = self.service()
            service['spec']['selector'] = selector
            with self.assertRaises(platform.ContractError):
                platform.validate_workloads([service], self.accounts)

    def test_reject_commit_in_service_or_deployment_selector(self):
        for document in [self.service(), self.deployment()]:
            selector = document['spec']['selector']
            if document['kind'] == 'Deployment':
                selector = selector['matchLabels']
            selector['app.kubernetes.io/version'] = 'old-commit'
            with self.assertRaises(platform.ContractError):
                platform.validate_workloads([document], self.accounts)

    def test_reject_missing_pod_labels_default_account_or_host_network(self):
        for mutation in [lambda d: d['spec']['template']['metadata'].update(labels={}),
                         lambda d: d['spec']['template']['spec'].update(serviceAccountName='default'),
                         lambda d: d['spec']['template']['spec'].update(automountServiceAccountToken=True),
                         lambda d: d['spec']['template']['spec'].update(hostNetwork=True),
                         lambda d: d['spec']['template']['spec']['containers'][0].pop('resources')]:
            document = self.deployment()
            mutation(document)
            with self.assertRaises(platform.ContractError):
                platform.validate_workloads([document], self.accounts)


if __name__ == '__main__':
    unittest.main()
