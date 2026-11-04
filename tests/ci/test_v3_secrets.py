"""Secret supply contract regressions; fixtures contain references, never values."""
import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('secret_contract', ROOT / 'scripts/check-v3-secrets.py')
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


class SecretContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ROOT / 'tests/ci/v3/secrets-values.fixture.yaml'
        cls.values = yaml.safe_load(cls.fixture.read_text())
        cls.access = contract.load_documents(contract.render(contract.CHART, 'yeodam-v3-secret-access', cls.fixture))
        cls.operator = contract.load_documents(contract.render(
            Path(os.environ['V3_HELM']).parent / 'external-secrets.tgz', 'external-secrets',
            contract.OPERATOR / 'values.yaml'))

    def rejected_access(self, mutate):
        docs = copy.deepcopy(self.access)
        mutate(docs)
        with self.assertRaises(contract.platform.ContractError):
            contract.validate_access(docs, self.values['stores'])

    def rejected_operator(self, mutate):
        docs = copy.deepcopy(self.operator)
        mutate(docs)
        with self.assertRaises(contract.platform.ContractError):
            contract.validate_operator(docs)

    @staticmethod
    def find(docs, kind, name=None):
        return next(d for d in docs if d['kind'] == kind and (not name or d['metadata']['name'] == name))

    def test_valid_fixture(self):
        contract.validate_access(self.access, self.values['stores'])
        contract.validate_operator(self.operator)

    def test_disabled_default_has_no_store_or_external_secret(self):
        docs = contract.load_documents(contract.render(contract.CHART, 'yeodam-v3-secret-access'))
        values = yaml.safe_load((contract.CHART / 'values.yaml').read_text())
        contract.validate_access(docs, values['stores'])
        self.assertEqual(len(docs), 5)

    def test_registry_secret_references_only(self):
        values = copy.deepcopy(self.values)
        binding = values['stores']['auth']['secrets'][0]
        binding['type'] = 'kubernetes.io/dockerconfigjson'
        binding['data'] = [{'secretKey': '.dockerconfigjson',
                            'remoteKey': 'yeodam/v3/foundation/auth/registry'}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'values.yaml'
            path.write_text(yaml.safe_dump(values))
            docs = contract.load_documents(contract.render(contract.CHART, 'yeodam-v3-secret-access', path))
            contract.validate_access(docs, values['stores'])
            contract.platform.schema_check(yaml.safe_dump_all(docs))

    def test_partial_candidate_values(self):
        override = {'stores': {'auth': self.values['stores']['auth']}}
        defaults = yaml.safe_load((contract.CHART / 'values.yaml').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'values.yaml'
            path.write_text(yaml.safe_dump(override))
            docs = contract.load_documents(contract.render(contract.CHART, 'yeodam-v3-secret-access', [path]))
            contract.validate_access(docs, contract.merge(defaults, override)['stores'])
            self.assertEqual(sum(d['kind'] == 'ExternalSecret' for d in docs), 1)

    def test_wrong_namespace_role(self):
        self.rejected_access(lambda ds: self.find(ds, 'SecretStore')['spec']['provider']['aws'].update(
            role='arn:aws:iam::483175530259:role/yeodam-v3-foundation-eso-photo'))

    def test_static_aws_credentials(self):
        self.rejected_access(lambda ds: self.find(ds, 'SecretStore')['spec']['provider']['aws'].update(
            auth={'secretRef': {'accessKeyIDSecretRef': {'name': 'static', 'key': 'id'}}}))

    def test_cross_namespace_remote_source(self):
        self.rejected_access(lambda ds: self.find(ds, 'ExternalSecret')['spec']['data'][0]['remoteRef'].update(
            key='yeodam/v3/foundation/photo/contract-probe'))

    def test_no_inline_payload_template(self):
        self.rejected_access(lambda ds: self.find(ds, 'ExternalSecret')['spec']['target'].update(
            template={'data': {'password': 'synthetic-plaintext'}}))

    def test_no_direct_secret_creation(self):
        self.rejected_access(lambda ds: ds.append({'apiVersion': 'v1', 'kind': 'Secret',
            'metadata': {'name': 'accidental', 'namespace': 'auth'}, 'stringData': {'marker': 'synthetic'}}))

    def test_no_duplicate_objects(self):
        self.rejected_access(lambda ds: ds.append(copy.deepcopy(self.find(ds, 'ExternalSecret'))))

    def test_no_delete_on_source_removal(self):
        self.rejected_access(lambda ds: self.find(ds, 'ExternalSecret')['spec']['target'].update(deletionPolicy='Delete'))

    def test_default_deny_cannot_be_widened(self):
        self.rejected_access(lambda ds: self.find(ds, 'NetworkPolicy', 'default-deny')['spec'].update(ingress=[{}]))

    def test_dns_selectors_are_combined(self):
        def widen(ds):
            peers = self.find(ds, 'NetworkPolicy', 'allow-controller-egress')['spec']['egress'][0]['to']
            selector = peers[0].pop('podSelector')
            peers.append({'podSelector': selector})
        self.rejected_access(widen)

    def test_webhook_not_publicly_open(self):
        def widen(ds):
            self.find(ds, 'NetworkPolicy', 'allow-control-plane-webhook')['spec']['ingress'][0]['from'][0]['ipBlock']['cidr'] = '0.0.0.0/0'
        self.rejected_access(widen)

    def test_no_extra_operator_replica(self):
        self.rejected_operator(lambda ds: self.find(ds, 'Deployment')['spec'].update(replicas=2))

    def test_operator_image_pinned(self):
        self.rejected_operator(lambda ds: self.find(ds, 'Deployment')['spec']['template']['spec']['containers'][0].update(
            image='ghcr.io/external-secrets/external-secrets:latest'))

    def test_operator_budget_explicit(self):
        self.rejected_operator(lambda ds: self.find(ds, 'Deployment')['spec']['template']['spec']['containers'][0].update(resources={}))

    def test_no_aws_token_minting(self):
        self.rejected_operator(lambda ds: self.find(ds, 'ClusterRole')['rules'].append(
            {'apiGroups': [''], 'resources': ['serviceaccounts/token'], 'verbs': ['create']}))

    def test_no_secret_payload_in_operator(self):
        self.rejected_operator(lambda ds: self.find(ds, 'Secret').update(stringData={'marker': 'synthetic'}))

    def test_helm_rejects_invalid_contract(self):
        cases = []
        wrong_role = copy.deepcopy(self.values)
        wrong_role['stores']['auth']['roleArn'] = wrong_role['stores']['trip']['roleArn']
        cases.append(wrong_role)
        wrong_source = copy.deepcopy(self.values)
        wrong_source['stores']['auth']['secrets'][0]['data'][0]['remoteKey'] = 'yeodam/v2/production/db'
        cases.append(wrong_source)
        unknown = copy.deepcopy(self.values)
        unknown['stores']['auth']['password'] = 'synthetic-plaintext'
        cases.append(unknown)
        for values in cases:
            with self.subTest(values=list(values['stores']['auth'])), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'values.yaml'
                path.write_text(yaml.safe_dump(values))
                with self.assertRaises(subprocess.CalledProcessError):
                    contract.render(contract.CHART, 'yeodam-v3-secret-access', path)


if __name__ == '__main__':
    unittest.main()
