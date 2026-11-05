import copy
import importlib.util
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mysql_contract', ROOT / 'scripts/check-v3-mysql.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MySQLContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = m.platform.load_documents(m.render(m.FOUNDATION))
        cls.jobs = m.platform.load_documents(m.render(m.FOUNDATION, ROOT / 'tests/ci/v3/mysql-bootstrap.values.yaml'))

    def reject(self, kind, path, value):
        docs = copy.deepcopy(self.base)
        obj = next(d for d in docs if d['kind'] == kind)
        for part in path[:-1]:
            obj = obj[part]
        obj[path[-1]] = value
        with self.assertRaises(m.platform.ContractError):
            m.validate(docs, True)

    def test_default_foundation_and_jobs(self):
        m.validate(m.platform.load_documents(m.render()))
        m.validate(self.base, True)
        m.validate(self.jobs, True, True)

    def test_data_loss_and_rollout_guards(self):
        for path, value in [(['replicas'], 1), (['podManagementPolicy'], 'OrderedReady'),
                            (['updateStrategy'], {'type': 'RollingUpdate'}),
                            (['persistentVolumeClaimRetentionPolicy', 'whenDeleted'], 'Delete'),
                            (['volumeClaimTemplates', 0, 'spec', 'accessModes'], ['ReadWriteOnce']),
                            (['volumeClaimTemplates', 0, 'spec', 'resources', 'requests', 'storage'], '100Gi')]:
            with self.subTest(path=path):
                self.reject('StatefulSet', ['spec'] + path, value)

    def test_public_network_and_budget_rejected(self):
        self.reject('Service', ['spec', 'type'], 'LoadBalancer')
        self.reject('StatefulSet', ['spec', 'template', 'spec', 'containers', 0, 'resources', 'limits', 'memory'], '4Gi')
        self.reject('StatefulSet', ['spec', 'template', 'spec', 'automountServiceAccountToken'], True)
        self.reject('NetworkPolicy', ['spec', 'ingress'], [{}])

    def test_writer_selector_must_match_probe(self):
        docs = copy.deepcopy(self.base)
        primary = next(d for d in docs if d['kind'] == 'Service' and d['metadata']['name'] == 'mysql-primary')
        primary['spec']['selector']['statefulset.kubernetes.io/pod-name'] = 'mysql-1'
        with self.assertRaises(m.platform.ContractError):
            m.validate(docs, True)

    def test_unknown_values_and_jobs_without_workloads_fail(self):
        for overrides in [['--set', 'bootstrap=true'], ['--set', 'image=mysql:latest'], ['--set', 'replicas=5']]:
            with self.subTest(overrides=overrides):
                command = [m.os.environ['V3_HELM'], 'template', 'test', str(m.CHART)] + overrides
                result = subprocess.run(command, capture_output=True)
                self.assertNotEqual(result.returncode, 0)

    def test_no_credential_payload_resources(self):
        docs = copy.deepcopy(self.base) + [{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'bad', 'namespace': 'mysql'}}]
        with self.assertRaises(m.platform.ContractError):
            m.validate(docs, True)


class BootstrapSecret(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('mysql_secret', ROOT / 'scripts/create-v3-mysql-bootstrap-secret.py')
        cls.secret = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.secret)

    def test_fresh_credentials_tls_identity_and_file_modes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as private:
            payload = self.secret.create_payload(private)
            self.assertEqual(set(payload), {'rootPassword', 'adminPassword', 'appPassword',
                'replicationPassword', 'healthPassword', 'tlsCa', 'tlsCert', 'tlsKey'})
            self.assertEqual(len(payload['replicationPassword']), 32)
            for key in ('rootPassword', 'adminPassword', 'appPassword', 'healthPassword'):
                self.assertEqual(len(payload[key]), 48)
            self.assertEqual(len({payload[k] for k in payload if k.endswith('Password')}), 5)
            cert = subprocess.check_output(['openssl', 'x509', '-in', private + '/tls.crt', '-noout', '-text'], text=True)
            for member in range(2):
                self.assertIn(f'DNS:mysql-{member}.mysql-members.mysql.svc.cluster.local', cert)
            self.assertIn('DNS:mysql-primary.mysql.svc.cluster.local', cert)
            subprocess.run(['openssl', 'verify', '-CAfile', private + '/ca.crt', private + '/tls.crt'],
                           check=True, capture_output=True)
            self.assertEqual(Path(private).stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in Path(private).iterdir()))

    def test_existing_secret_and_wrong_account_never_write(self):
        from unittest.mock import patch
        import json
        from types import SimpleNamespace
        for account, exists in [('000000000000', False), ('483175530259', True)]:
            calls = []
            def call(argv, **kwargs):
                calls.append(argv)
                if 'get-caller-identity' in argv:
                    return SimpleNamespace(returncode=0, stdout=json.dumps({'Account': account}), stderr='')
                if 'describe-secret' in argv:
                    return SimpleNamespace(returncode=0, stdout='{}', stderr='')
                self.fail('Unexpected AWS write')
            with self.subTest(account=account, exists=exists), \
                    patch.object(self.secret.subprocess, 'run', side_effect=call), \
                    patch('sys.argv', ['create', '--create-new']):
                with self.assertRaises(SystemExit):
                    self.secret.main()
            self.assertFalse(any('create-secret' in c for c in calls))
