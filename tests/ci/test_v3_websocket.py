"""WebSocket rendered contracts and failure cases. No AWS, production reads or model calls."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def module(name, script):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / script)
    result = importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


apps = module('ws_apps', 'check-v3-apps.py')
access = module('ws_access_test', 'check-v3-websocket.py')
PROFILE = ROOT / 'deploy/v3/environments/foundation/websocket.values.yaml'
FIXTURE = {'images': {'websocket': 'ghcr.io/100-hours-a-week/yeodam-websocket@sha256:' + '6' * 64},
           'defaults': {'configurationRevision': 'c' * 64, 'configMapData': {
               'RELEASE': '3f4fd318466f52a317e770e33e2d33e9800ed525',
               'WEBSOCKET_ALLOWED_ORIGINS': 'https://v3.example.invalid'}},
           'workloads': {'trip-websocket': {'enabled': True}}}


class WebSocket(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory();cls.addClassCleanup(cls.directory.cleanup)
        cls.d = Path(cls.directory.name)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(cls.d / 'key'),
                        '-out', str(cls.d / 'ca'), '-days', '2', '-subj', '/CN=synthetic-ws-ca',
                        '-addext', 'basicConstraints=critical,CA:TRUE'], capture_output=True, check=True)
        cls.ca = (cls.d / 'ca').read_text()

    def rendered(self, override=None):
        with tempfile.TemporaryDirectory() as directory:
            files = [PROFILE]
            for name, value in [('fixture', FIXTURE), ('override', override or {})]:
                path = Path(directory) / (name + '.yaml');path.write_text(yaml.safe_dump(value));files.append(path)
            values = apps.load_values(files)
            source = apps.render(files);docs = apps.platform.load_documents(source)
            apps.validate(docs, values)
            return source, docs, values

    def rejects(self, override):
        with self.assertRaises((ValueError, subprocess.CalledProcessError)): self.rendered(override)

    def test_disabled_renders_no_resources(self):
        self.assertEqual(apps.render([PROFILE]).strip(), '')
        self.assertEqual(access.render().strip(), '')

    def test_minimum_runtime_and_actual_health_paths(self):
        source, docs, values = self.rendered()
        apps.platform.schema_check(source)
        self.assertEqual({d['kind'] for d in docs}, {'Deployment', 'Service', 'ConfigMap'})
        deployment = next(d for d in docs if d['kind'] == 'Deployment')
        self.assertEqual(deployment['spec']['replicas'], 1)
        self.assertEqual(deployment['spec']['strategy']['rollingUpdate'], {'maxSurge': 0, 'maxUnavailable': 1})
        pod = deployment['spec']['template']['spec'];container = pod['containers'][0]
        self.assertEqual(pod['nodeSelector'], {'yeodam.io/role': 'api', 'yeodam.io/stage': 'v3'})
        self.assertEqual(container['resources']['requests']['memory'], '256Mi')
        self.assertEqual(container['resources']['limits']['memory'], '512Mi')
        self.assertEqual(container['readinessProbe']['httpGet']['path'], '/api/actuator/health/readiness')
        self.assertEqual([c['name'] for c in pod['initContainers']], ['prepare-trust'])
        config = next(d['data'] for d in docs if d['kind'] == 'ConfigMap')
        self.assertEqual(config['BACKEND_INTERNAL_BASE_URL'], 'http://trip-api.trip.svc.cluster.local:8080')
        self.assertNotIn('REDIS_PASSWORD', config)
        self.assertEqual(json.loads(config['SPRING_APPLICATION_JSON'])['spring']['data']['redis']['sentinel']['username'], 'sentinel-client')

    def test_wrong_backend_redis_channel_and_tls_rejected(self):
        for key, value in [('BACKEND_INTERNAL_BASE_URL', 'http://backend.production.yeodam.internal'),
                           ('REDIS_HOST', 'redis.production.yeodam.internal'),
                           ('WEBSOCKET_EVENTS_CHANNEL', 'yeodam:production:websocket:events:v1'),
                           ('REDIS_SSL_ENABLED', 'false')]:
            with self.subTest(key=key): self.rejects({'defaults': {'configMapData': {key: value}}})

    def test_sentinel_auth_and_secret_overrides_rejected(self):
        self.rejects({'defaults': {'springProperties': {'spring': {'data': {'redis': {'sentinel': {'username': 'admin'}}}}}}})
        self.rejects({'defaults': {'secretRefs': {'REDIS_PASSWORD': {'name': 'redis-bootstrap', 'key': 'adminPassword'}}}})
        self.rejects({'defaults': {'configMapData': {'WEBSOCKET_INTERNAL_SERVICE_TOKEN': 'literal'}}})
        self.rejects({'defaults': {'configMapData': {'SPRING_APPLICATION_JSON': '{}'}}})

    def test_origin_source_revision_and_pod_budget_rejected(self):
        for origin in ['*', 'http://localhost:3000', 'https://a.invalid,https://b.invalid']:
            self.rejects({'defaults': {'configMapData': {'WEBSOCKET_ALLOWED_ORIGINS': origin}}})
        self.rejects({'defaults': {'configurationRevision': 'unset'}})
        self.rejects({'defaults': {'configMapData': {'RELEASE': 'main'}}})
        self.rejects({'defaults': {'resources': {'limits': {'memory': '1Gi'}}}})
        self.rejects({'workloads': {'trip-websocket': {'replicas': 2}}})
        self.rejects({'workloads': {'trip-websocket': {'ports': {'http': 8082}}}})

    def test_readiness_and_liveness_cannot_be_interchanged(self):
        self.rejects({'defaults': {'probes': {'liveness': {'httpGet': {'path': '/api/actuator/health/readiness', 'port': 'http', 'scheme': 'HTTP'}}}}})
        self.rejects({'defaults': {'probes': {'readiness': {'httpGet': {'path': '/health', 'port': 'http', 'scheme': 'HTTP'}}}}})

    def test_be_profile_and_other_apps_cannot_join_websocket_release(self):
        self.rejects({'images': {'websocket': 'ghcr.io/100-hours-a-week/yeodam-backend@sha256:' + '6' * 64}})
        self.rejects({'backendContract': True})
        self.rejects({'workloads': {'auth-api': {'enabled': True}}})

    def access_documents(self):
        path = self.d / 'access.values.json';path.write_text(json.dumps({'enabled': True, 'redisCa': self.ca}))
        source = access.render([path]);docs = access.platform.load_documents(source)
        access.validate(docs, True)
        return source, docs

    def test_access_public_ca_existing_eso_and_exact_peers(self):
        source, docs = self.access_documents();access.platform.schema_check(source)
        self.assertEqual(len(docs), 5)
        self.assertFalse(any(d['kind'] in ['SecretStore', 'ServiceAccount', 'Deployment'] for d in docs))
        es = next(d for d in docs if d['kind'] == 'ExternalSecret')
        self.assertEqual({d['remoteRef']['key'] for d in es['spec']['data']}, {'yeodam/v3/foundation/trip/backend'})
        policy = next(d for d in docs if d['metadata']['name'] == 'allow-websocket-dependencies')
        self.assertEqual([p['port'] for e in policy['spec']['egress'] for p in e['ports']], [6379, 26379, 8080])
        self.assertNotIn('ipBlock', json.dumps(policy))

    def test_policy_and_source_widening_rejected(self):
        _, docs = self.access_documents()
        for kind in ['policy', 'source', 'private-key']:
            changed = deepcopy(docs)
            if kind == 'policy': next(d for d in changed if d['kind'] == 'NetworkPolicy')['spec']['podSelector'] = {}
            if kind == 'source': next(d for d in changed if d['kind'] == 'ExternalSecret')['spec']['data'][0]['remoteRef']['key'] = 'yeodam/v2/production/app'
            if kind == 'private-key': next(d for d in changed if d['kind'] == 'Secret')['stringData']['redis.crt'] += 'PRIVATE KEY'
            with self.subTest(kind=kind), self.assertRaises(ValueError): access.validate(changed, True)

    def test_prepare_cli_contains_no_credentials_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'prepared'
            command = [sys.executable, str(ROOT / 'scripts/prepare-v3-websocket-values.py'),
                       '--image', FIXTURE['images']['websocket'], '--source', FIXTURE['defaults']['configMapData']['RELEASE'],
                       '--origin', 'https://v3.example.invalid', '--configuration-revision', 'c' * 64,
                       '--redis-ca', str(self.d / 'ca'), '--directory', str(target)]
            subprocess.run(command, capture_output=True, check=True)
            files = [PROFILE, target / 'apps.values.json']
            source = apps.render(files);apps.validate(apps.platform.load_documents(source), apps.load_values(files))
            access.validate(access.platform.load_documents(access.render([target / 'access.values.json'])), True)
            self.assertNotIn('PRIVATE KEY', (target / 'access.values.json').read_text())
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_trust_initializer_keeps_public_roots_and_adds_redis_only(self):
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory);ca = d / 'ca';trust = d / 'trust';ca.mkdir();trust.mkdir()
            (ca / 'redis.crt').write_text(self.ca)
            home = subprocess.check_output(['/usr/libexec/java_home'], text=True).strip() if sys.platform == 'darwin' else os.environ['JAVA_HOME']
            script = (ROOT / 'deploy/v3/apps/files/prepare-websocket-trust.sh').read_text().replace('/etc/yeodam/ca', str(ca)).replace('/etc/yeodam/trust', str(trust))
            subprocess.run(['/bin/sh', '-ec', script], env=dict(os.environ, JAVA_HOME=home), capture_output=True, check=True)
            text = subprocess.check_output(['keytool', '-J-Duser.language=en', '-list', '-keystore', str(trust / 'cacerts'), '-storepass', 'public-ca-only'], text=True, stderr=subprocess.DEVNULL)
            self.assertIn('yeodam-v3-redis', text)
            self.assertNotIn('yeodam-v3-mysql', text)
            before = subprocess.check_output(['keytool', '-J-Duser.language=en', '-list', '-keystore', home + '/lib/security/cacerts', '-storepass', 'changeit'], text=True, stderr=subprocess.DEVNULL)
            self.assertEqual(text.count('trustedCertEntry'), before.count('trustedCertEntry') + 1)


if __name__ == '__main__': unittest.main()
