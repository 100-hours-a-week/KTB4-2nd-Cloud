import copy
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('redis_contract', ROOT / 'scripts/check-v3-redis.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class RedisContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = m.platform.load_documents(m.render(m.FOUNDATION))

    def reject(self, kind, path, value, name=None):
        docs = copy.deepcopy(self.base)
        obj = next(d for d in docs if d['kind'] == kind and (name is None or d['metadata']['name'] == name))
        for part in path[:-1]:
            obj = obj[part]
        obj[path[-1]] = value
        with self.assertRaises(m.platform.ContractError):
            m.validate(docs, True)

    def test_default_foundation_and_initial_smoke(self):
        m.validate(m.platform.load_documents(m.render()))
        m.validate(self.base, True)
        m.validate(m.platform.load_documents(m.render(m.FOUNDATION, ROOT / 'tests/ci/v3/redis-bootstrap.values.yaml')), True, True, True)

    def test_preserve_data_member_count_and_maintenance(self):
        for kind in ('redis', 'sentinel'):
            for path, value in [(['replicas'], 1), (['updateStrategy'], {'type': 'RollingUpdate'}),
                    (['persistentVolumeClaimRetentionPolicy', 'whenDeleted'], 'Delete'),
                    (['volumeClaimTemplates', 0, 'spec', 'resources', 'requests', 'storage'], '10Gi')]:
                with self.subTest(kind=kind, path=path):
                    self.reject('StatefulSet', ['spec'] + path, value, kind)

    def test_no_widened_network_or_budget(self):
        self.reject('NetworkPolicy', ['spec', 'ingress'], [{}])
        self.reject('Service', ['spec', 'type'], 'LoadBalancer')
        self.reject('StatefulSet', ['spec', 'template', 'spec', 'containers', 0, 'resources', 'limits', 'memory'], '1Gi')
        self.reject('StatefulSet', ['spec', 'template', 'spec', 'automountServiceAccountToken'], True)
        self.reject('StatefulSet', ['spec', 'template', 'spec', 'affinity', 'podAntiAffinity', 'requiredDuringSchedulingIgnoredDuringExecution'], [], 'sentinel')

    def test_no_unknown_values_or_accidental_initial_bootstrap(self):
        for value in ['initialBootstrap=true', 'image=redis:latest', 'replicas=1']:
            q = subprocess.run([os.environ['V3_HELM'], 'template', 'test', str(m.CHART), '--set', value], capture_output=True)
            self.assertNotEqual(q.returncode, 0)

    def test_majority_survives_first_monitor_stale_response(self):
        source = shlex.quote(str(m.CHART / 'files/common.sh'))
        fake = r'''
cli() {
 if [[ $6 == master ]]; then printf 'flags\nmaster\n'; return; fi
 case "$1" in sentinel-0.*) printf 'redis-0.redis-members.redis.svc.cluster.local\n6379\n';;
 *) printf 'redis-1.redis-members.redis.svc.cluster.local\n6379\n';; esac
}
discover_primary
'''
        q = subprocess.run(['bash', '-ec', 'source ' + source + ';' + fake], capture_output=True, text=True)
        self.assertEqual(q.returncode, 0)
        self.assertEqual(q.stdout, 'redis-1.redis-members.redis.svc.cluster.local')

    def test_unhealthy_or_untrusted_monitors_cannot_select_primary(self):
        source = shlex.quote(str(m.CHART / 'files/common.sh'))
        for fake in [
            "cli() { if [[ $6 == master ]]; then printf 'flags\\nmaster,s_down\\n'; else printf 'redis-0.redis-members.redis.svc.cluster.local\\n6379\\n'; fi; }; discover_primary",
            "cli() { printf 'untrusted.example\\n6379\\n'; }; discover_primary"]:
            q = subprocess.run(['bash', '-ec', 'source ' + source + ';' + fake], capture_output=True)
            self.assertNotEqual(q.returncode, 0)

    def test_new_secret_credentials_certificates_and_modes(self):
        spec = importlib.util.spec_from_file_location('redis_secret', ROOT / 'scripts/create-v3-redis-bootstrap-secret.py')
        creator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(creator)
        self.assertEqual(creator.NAME, 'yeodam/v3/foundation/redis/bootstrap')
        with tempfile.TemporaryDirectory() as private:
            payload = creator.create_payload(private)
            self.assertEqual(len(payload), 10)
            self.assertTrue(all(len(v) == 48 for k, v in payload.items() if k.endswith('Password')))
            cert = subprocess.check_output(['openssl', 'x509', '-in', private + '/tls.crt', '-noout', '-text'], text=True)
            for i in range(3):
                self.assertIn(f'sentinel-{i}.redis-sentinel.redis.svc.cluster.local', cert)
            for i in range(2):
                self.assertIn(f'redis-{i}.redis-members.redis.svc.cluster.local', cert)
            subprocess.run(['openssl', 'verify', '-CAfile', private + '/ca.crt', private + '/tls.crt'], check=True, capture_output=True)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in Path(private).iterdir()))

    def test_secret_creator_refuses_existing_secret_or_wrong_account(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import json
        spec = importlib.util.spec_from_file_location('redis_creator_guard', ROOT / 'scripts/create-v3-redis-bootstrap-secret.py')
        creator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(creator)
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
