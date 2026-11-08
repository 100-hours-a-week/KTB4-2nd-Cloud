"""Exercise rendered least-privilege policies and stale Service reference rejection offline."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import v3_service_access as references
spec = importlib.util.spec_from_file_location('service_access_check', ROOT / 'scripts/check-v3-service-access.py')
check = importlib.util.module_from_spec(spec);spec.loader.exec_module(check)


def services():
    items = []
    for n, (key, expected) in enumerate(references.SERVICES.items(), 10):
        items.append({'apiVersion': 'v1', 'kind': 'Service',
                      'metadata': {'name': expected['name'], 'namespace': expected['namespace'],
                                   'uid': '00000000-0000-4000-8000-' + str(n).zfill(12)},
                      'spec': {'type': 'ClusterIP', 'clusterIP': f'172.20.200.{n}',
                               'selector': expected['selector'],
                               'ports': [{'name': 'http', 'port': expected['port'], 'targetPort': 'http', 'protocol': 'TCP'}]}})
    return {'kind': 'List', 'items': items}


class ServiceAccess(unittest.TestCase):
    def test_default_disabled_and_exact_three_policies(self):
        self.assertFalse(check.render().strip())
        source = check.render(check.FIXTURE)
        docs = check.platform.load_documents(source)
        check.validate(docs, check.FIXTURE)
        check.platform.schema_check(source)
        self.assertEqual(len(docs), 3)
        self.assertEqual({d['kind'] for d in docs}, {'NetworkPolicy'})

    def test_chart_rejects_wider_invalid_missing_or_duplicate_addresses(self):
        for ip in ['', '0.0.0.0/0', '172.20.0.0/16', '10.40.1.2', '172.20.999.1', '172.20.0.0', '172.20.255.255', '172.20.200.11']:
            v = deepcopy(check.FIXTURE);v['serviceAddresses']['aiApi'] = ip
            with self.subTest(ip=ip), self.assertRaises(subprocess.CalledProcessError):
                check.render(v)

    def test_rendered_policy_peer_port_direction_and_kind_widening_rejected(self):
        docs = check.platform.load_documents(check.render(check.FIXTURE))
        for mutation in ['cidr', 'port', 'role', 'ingress', 'kind', 'duplicate']:
            changed = deepcopy(docs);spec = changed[0]['spec']
            if mutation == 'cidr': spec['egress'][0]['to'][0]['ipBlock']['cidr'] = '172.20.0.0/16'
            if mutation == 'port': spec['egress'][0]['ports'][0]['port'] = 443
            if mutation == 'role': spec['podSelector'] = {}
            if mutation == 'ingress': spec['policyTypes'].append('Ingress')
            if mutation == 'kind': changed[0]['kind'] = 'Deployment'
            if mutation == 'duplicate': changed.append(deepcopy(changed[0]))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                check.validate(changed, check.FIXTURE)

    def test_metadata_selection_rejects_wrong_namespace_selector_port_or_external_service(self):
        for mutation in ['namespace', 'selector', 'port', 'external', 'duplicate', 'secret', 'ip', 'uid']:
            v = deepcopy(services());item = v['items'][0]
            if mutation == 'namespace': item['metadata']['namespace'] = 'production'
            if mutation == 'selector': item['spec']['selector'] = {}
            if mutation == 'port': item['spec']['ports'][0]['port'] = 443
            if mutation == 'external': item['spec']['type'] = 'LoadBalancer'
            if mutation == 'duplicate': v['items'][1] = deepcopy(item)
            if mutation == 'secret': item['kind'] = 'Secret'
            if mutation == 'ip': item['spec']['clusterIP'] = '8.8.8.8'
            if mutation == 'uid': item['metadata']['uid'] = ''
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, KeyError)):
                references.review(v)

    def test_prepare_and_recheck_refuse_changed_address_or_recreated_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);source = root / 'services.json';source.write_text(json.dumps(services()))
            target = root / 'prepared'
            command = [sys.executable, str(ROOT / 'scripts/prepare-v3-service-access.py'), '--services', str(source)]
            subprocess.run(command + ['--directory', str(target)], check=True, capture_output=True)
            v = json.loads((target / 'values.json').read_text())
            check.validate(check.platform.load_documents(check.render(v)), v)
            subprocess.run([sys.executable, str(ROOT / 'scripts/check-v3-service-access.py'), '-f', str(target / 'values.json')], check=True, capture_output=True)
            verify = command + ['--expected-review', str(target / 'review.json')]
            subprocess.run(verify, check=True, capture_output=True)
            self.assertNotEqual(subprocess.run(command + ['--directory', str(target)], capture_output=True).returncode, 0)
            for field in ['uid', 'clusterIP']:
                changed = services()
                if field == 'uid': changed['items'][0]['metadata']['uid'] = 'ffffffff-ffff-4fff-8fff-ffffffffffff'
                else: changed['items'][0]['spec']['clusterIP'] = '172.20.200.99'
                source.write_text(json.dumps(changed))
                self.assertNotEqual(subprocess.run(verify, capture_output=True).returncode, 0)
            self.assertNotIn('Secret', (target / 'values.json').read_text())


if __name__ == '__main__':
    unittest.main()
