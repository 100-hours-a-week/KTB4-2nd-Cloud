import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mongo_contract', ROOT / 'scripts/check-v3-mongodb.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MongoDBContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = m.platform.load_documents(m.render(m.FOUNDATION))

    def reject(self, kind, path, value):
        documents = copy.deepcopy(self.base)
        obj = next(d for d in documents if d['kind'] == kind)
        for key in path[:-1]:
            obj = obj[key]
        obj[path[-1]] = value
        with self.assertRaises(m.platform.ContractError):
            m.validate(documents, True)

    def test_reviewed_default_enabled_and_smoke(self):
        m.validate(m.platform.load_documents(m.render()))
        m.validate(self.base, True)
        m.validate(m.platform.load_documents(m.render(m.FOUNDATION, ROOT / 'tests/ci/v3/mongodb-smoke.values.yaml')), True, True)

    def test_no_data_deletion_or_unreviewed_rollout(self):
        for path, value in [(['replicas'], 3), (['updateStrategy'], {'type': 'RollingUpdate'}),
            (['persistentVolumeClaimRetentionPolicy', 'whenDeleted'], 'Delete'),
            (['volumeClaimTemplates', 0, 'spec', 'resources', 'requests', 'storage'], '50Gi')]:
            self.reject('StatefulSet', ['spec'] + path, value)

    def test_no_small_node_budget_or_privilege_widening(self):
        prefix = ['spec', 'template', 'spec']
        self.reject('StatefulSet', prefix + ['nodeSelector', 'yeodam.io/memory-tier'], '2gi')
        self.reject('StatefulSet', prefix + ['containers', 0, 'resources', 'limits', 'memory'], '4Gi')
        self.reject('StatefulSet', prefix + ['automountServiceAccountToken'], True)
        self.reject('Service', ['spec', 'type'], 'LoadBalancer')
        self.reject('NetworkPolicy', ['spec', 'ingress'], [{}])

    def test_unknown_values_or_accidental_job_rejected(self):
        for value in ['smoke=true', 'image=mongo:latest', 'replicas=3']:
            q = subprocess.run([os.environ['V3_HELM'], 'template', 'test', str(m.CHART), '--set', value], capture_output=True)
            self.assertNotEqual(q.returncode, 0)

    def test_mongodb_credentials_san_and_creation_guard(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import json
        spec = importlib.util.spec_from_file_location('creator', ROOT / 'scripts/create-v3-mongodb-bootstrap-secret.py')
        creator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(creator)
        self.assertEqual(creator.NAME, 'yeodam/v3/foundation/mongodb/bootstrap')
        with tempfile.TemporaryDirectory() as private:
            payload = creator.create_payload(private)
            self.assertEqual(set(payload), {'adminPassword', 'appPassword', 'healthPassword', 'tlsCa', 'tlsCert', 'tlsKey'})
            self.assertTrue(all(len(payload[k]) == 48 for k in ('adminPassword', 'appPassword', 'healthPassword')))
            cert = subprocess.check_output(['openssl', 'x509', '-in', private + '/tls.crt', '-noout', '-text'], text=True)
            self.assertIn('mongodb.mongodb.svc.cluster.local', cert)
            subprocess.run(['openssl', 'verify', '-CAfile', private + '/ca.crt', private + '/tls.crt'], check=True, capture_output=True)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in Path(private).iterdir()))
        for account in ['000000000000', '483175530259']:
            calls = []
            def call(argv, **kwargs):
                calls.append(argv)
                if 'get-caller-identity' in argv:
                    return SimpleNamespace(returncode=0, stdout=json.dumps({'Account': account}), stderr='')
                if 'describe-secret' in argv:
                    return SimpleNamespace(returncode=0, stdout='{}', stderr='')
                self.fail('Unexpected AWS mutation')
            with patch.object(creator.subprocess, 'run', side_effect=call), patch('sys.argv', ['create', '--create-new']):
                with self.assertRaises(SystemExit):
                    creator.main()
            self.assertFalse(any('create-secret' in c for c in calls))
