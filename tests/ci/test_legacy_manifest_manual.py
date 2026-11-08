"""Keep legacy verification executable while preventing unsolicited candidate updates."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('legacy_manual', ROOT / 'scripts/prepare-legacy-manifest-input.py')
manual = importlib.util.module_from_spec(spec);spec.loader.exec_module(manual)
CONTEXT = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': manual.CLOUD,
           'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REF': 'refs/heads/main'}


def payload(generation='v1', component='backend'):
    repo, package = manual.COMPONENTS[generation][component]
    value = {'component': component, 'source_repository': '100-hours-a-week/KTB4-2nd-' + repo,
             'source_sha': 'a' * 40, 'image': 'ghcr.io/100-hours-a-week/' + package + ':sha-' + 'a' * 40,
             'digest': 'sha256:' + 'b' * 64}
    if generation == 'v2':
        value['build_run_id'] = '123'
    return value


def event(value):
    return {'inputs': {'payload': json.dumps(value)}}


class ManualLegacy(unittest.TestCase):
    def test_all_supported_components_keep_the_original_public_payload(self):
        for generation, components in manual.COMPONENTS.items():
            for component in components:
                value = payload(generation, component)
                result = manual.prepare(generation, event(value), CONTEXT)
                self.assertEqual(result['client_payload'], value)
                self.assertEqual(result['action'], 'image-published' if generation == 'v1' else 'v2-image-published')

    def test_automatic_pr_foreign_repo_and_non_main_requests_rejected(self):
        for field, value in [('GITHUB_ACTIONS', 'false'), ('GITHUB_EVENT_NAME', 'repository_dispatch'),
                             ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REF', 'refs/heads/feature'),
                             ('GITHUB_REPOSITORY', 'foreign/repo')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                manual.prepare('v1', event(payload()), CONTEXT | {field: value})

    def test_wrong_package_repository_digest_sha_and_injection_rejected(self):
        changes = [{'source_repository': 'foreign/repo'}, {'component': 'worker'},
                   {'source_sha': 'main'}, {'digest': 'latest'}, {'image': payload()['image'] + '\nEVIL=true'},
                   {'digest': payload()['digest'] + '; echo unsafe'}, {'unexpected': 'value'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                manual.prepare('v1', event(payload() | change), CONTEXT)

    def test_v2_run_identity_remains_required(self):
        for run in ['', '0', '1/../2', 123, '1\nEVIL=true']:
            with self.subTest(run=run), self.assertRaises(ValueError):
                manual.prepare('v2', event(payload('v2') | {'build_run_id': run}), CONTEXT)
        missing = payload('v2');missing.pop('build_run_id')
        with self.assertRaises(ValueError):
            manual.prepare('v2', event(missing), CONTEXT)
        with self.assertRaises(ValueError):
            manual.prepare('v1', event(payload('v2')), CONTEXT)

    def test_duplicate_oversize_or_extra_manual_inputs_rejected(self):
        values = [{'inputs': {'payload': '{"component":"frontend","component":"backend"}'}},
                  {'inputs': {'payload': ' ' * 4097}},
                  {'inputs': {'payload': json.dumps(payload()), 'script': 'untrusted'}},
                  {'client_payload': payload()}, {'inputs': {'payload': []}}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                manual.prepare('v1', value, CONTEXT)

    def test_cli_exports_only_validated_values_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);source = root / 'event.json';source.write_text(json.dumps(event(payload())))
            target = root / 'normalized.json';outputs = root / 'outputs'
            cmd = [sys.executable, str(ROOT / 'scripts/prepare-legacy-manifest-input.py'), '--generation', 'v1',
                   '--event-file', str(source), '--output', str(target)]
            env = dict(os.environ, **CONTEXT, GITHUB_OUTPUT=str(outputs))
            subprocess.run(cmd, env=env, check=True, capture_output=True)
            self.assertEqual(json.loads(target.read_text())['client_payload'], payload())
            self.assertEqual(dict(line.split('=', 1) for line in outputs.read_text().splitlines()), payload())
            previous = target.read_bytes()
            self.assertNotEqual(subprocess.run(cmd, env=env, capture_output=True).returncode, 0)
            self.assertEqual(target.read_bytes(), previous)
            target.unlink();outputs.unlink()
            source.write_text(json.dumps(event(payload() | {'image': 'malicious\nCOMMAND=value'})))
            self.assertNotEqual(subprocess.run(cmd, env=env, capture_output=True).returncode, 0)
            self.assertFalse(target.exists())
            self.assertFalse(outputs.exists())

    def test_v1_existing_validator_still_rejects_bad_digest_and_updates_one_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root / 'deploy').mkdir();(root / 'scripts').mkdir()
            for name in ['update-image-version.sh', 'validate-image-versions.sh']:
                (root / 'scripts' / name).write_bytes((ROOT / 'scripts' / name).read_bytes())
            target = root / 'deploy/image-versions.env';target.write_bytes((ROOT / 'deploy/image-versions.env').read_bytes())
            before = target.read_text();value = manual.prepare('v1', event(payload()), CONTEXT)['client_payload']
            cmd = ['bash', 'scripts/update-image-version.sh']
            for key, item in value.items():
                cmd += ['--' + key.replace('_', '-'), item]
            subprocess.run(cmd, cwd=root, check=True, capture_output=True)
            after = target.read_text()
            for line in before.splitlines():
                if not line.startswith(('BACKEND_IMAGE=', 'BACKEND_DIGEST=')):
                    self.assertIn(line, after.splitlines())
            cmd[-1] = 'invalid'
            self.assertNotEqual(subprocess.run(cmd, cwd=root, capture_output=True).returncode, 0)
            self.assertEqual(target.read_text(), after)

    def test_manual_workflows_preserve_existing_verifiers_and_no_auto_trigger(self):
        for name in ['release-manifest-update.yml', 'v2-release-manifest-update.yml']:
            text = (ROOT / '.github/workflows' / name).read_text()
            # Inspect the controlled top-level trigger block, independently of job text.
            block = text.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
            self.assertEqual(re.findall(r'^  ([a-z_]+):', block, re.M), ['workflow_dispatch'])
            self.assertIn("if: github.ref == 'refs/heads/main'", text)
            self.assertNotIn('github.event.client_payload', text)
            self.assertNotIn('continue-on-error', text)
        v1 = (ROOT / '.github/workflows/release-manifest-update.yml').read_text()
        v2 = (ROOT / '.github/workflows/v2-release-manifest-update.yml').read_text()
        self.assertIn('bash scripts/update-image-version.sh', v1)
        self.assertIn('gh pr create', v1)
        self.assertIn('docker/login-action@', v2)
        self.assertIn('python3 scripts/update-v2-release-manifest.py --event-file', v2)
        self.assertIn('CLOUD_RELEASE_TOKEN', v1)
        self.assertIn('CLOUD_RELEASE_TOKEN', v2)


if __name__ == '__main__':
    unittest.main()
