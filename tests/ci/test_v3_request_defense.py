import copy
import importlib.util
import json
import os
import subprocess
import yaml
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests/ci/v3')]
from full_release_fixture import candidate
import v3_backend_contract as contract
import v3_release_bundle as bundle
spec = importlib.util.spec_from_file_location('request_defense', ROOT / 'scripts/prepare-v3-request-defense-candidate.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RequestDefense(unittest.TestCase):
    def configs(self, data):
        v = data['values']['backend']
        result = {n: bundle.apps.merge(v['defaults'], w) for n, w in v['workloads'].items() if w.get('enabled')}
        for c in result.values():
            contract.prepare_config(c)
        return result

    def test_only_two_flags_change_and_input_preserved(self):
        before = candidate(); frozen = copy.deepcopy(before)
        after = module.prepare(before)
        self.assertEqual(before, frozen)
        for role in module.TARGETS:
            self.assertEqual(after['values']['backend']['workloads'][role]['configMapData'].pop(module.KEY), 'true')
        self.assertEqual(after, before)

    def test_active_candidate_reapplication(self):
        active = module.prepare(candidate())
        self.assertEqual(module.prepare(active), active)

    def test_existing_profile_compatibility(self):
        for source in [candidate(), module.prepare(candidate())]:
            contract.validate_backend(self.configs(source), connected=True)

    def test_worker_flag_rejected(self):
        data = module.prepare(candidate())
        data['values']['backend']['workloads']['photo-converter']['configMapData'][module.KEY] = 'true'
        with self.assertRaisesRegex(ValueError, 'only on trip/photo'):
            contract.validate_backend(self.configs(data), connected=True)

    def test_false_or_arbitrary_flag_rejected(self):
        for value in ['false', 'TRUE', True, '1']:
            data = module.prepare(candidate())
            data['values']['backend']['workloads']['trip-api']['configMapData'][module.KEY] = value
            with self.assertRaises(ValueError):
                contract.validate_backend(self.configs(data), connected=True)

    def test_shared_default_and_unrelated_role_rejected(self):
        data = candidate()
        data['values']['backend']['defaults']['configMapData'][module.KEY] = 'true'
        with self.assertRaisesRegex(ValueError, 'unrelated roles'):
            module.prepare(data)

    @unittest.skipUnless(os.environ.get('V3_HELM'), 'Pinned Helm required')
    def test_rendered_diff_is_only_two_api_configmaps_and_deployments(self):
        data = json.loads((ROOT / 'deploy/v3/releases/candidate.json').read_text())
        active = module.prepare(data)
        def render(value):
            raw = subprocess.check_output([os.environ['V3_HELM'], 'template', 'yeodam-v3-apps',
                str(ROOT / 'deploy/v3/apps'), '--kube-version', '1.36.0', '-f', '-'],
                input=yaml.safe_dump(value['values']['backend']), text=True, stderr=subprocess.PIPE)
            return {(x['kind'], x['metadata']['namespace'], x['metadata']['name']): x
                    for x in yaml.safe_load_all(raw) if x}
        before, after = render(data), render(active)
        self.assertEqual(set(before), set(after))
        changed = {k for k in before if before[k] != after[k]}
        expected = {(kind, ns, role + ('-runtime' if kind == 'ConfigMap' else ''))
                    for role, ns in [('trip-api', 'trip'), ('photo-api', 'photo')]
                    for kind in ['ConfigMap', 'Deployment']}
        self.assertEqual(changed, expected)
        for kind, ns, role in changed:
            if kind != 'Deployment':
                continue
            old_pod = copy.deepcopy(before[(kind, ns, role)]['spec']['template'])
            new_pod = copy.deepcopy(after[(kind, ns, role)]['spec']['template'])
            old_pod['metadata']['annotations'].pop('yeodam.io/configuration-checksum')
            new_pod['metadata']['annotations'].pop('yeodam.io/configuration-checksum')
            self.assertEqual(old_pod, new_pod)
        active['values']['backend']['workloads']['photo-converter']['configMapData'][module.KEY] = 'true'
        with self.assertRaises(subprocess.CalledProcessError):
            render(active)

    def test_existing_operating_candidate_preserved(self):
        data = json.loads((ROOT / 'deploy/v3/releases/candidate.json').read_text())
        active = module.prepare(data)
        self.assertEqual(active['components'], data['components'])
        for profile in ['ai', 'frontend', 'websocket', 'service-access']:
            self.assertEqual(active['values'][profile], data['values'][profile])
        self.assertEqual(active['values']['backend']['defaults'], data['values']['backend']['defaults'])
        for role in module.TARGETS:
            self.assertEqual(self.configs(active)[role]['resources'], self.configs(data)[role]['resources'])


if __name__ == '__main__':
    unittest.main()
