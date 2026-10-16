import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('release_event', ROOT / 'scripts/update-v2-release-manifest.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
BASE = mod.manifest.load(ROOT / mod.MANIFEST)


def payload(component='frontend', sha='a'):
    repository, package = mod.manifest.COMPONENTS[component]
    return dict(component=component, source_repository=repository, source_sha=sha * 40,
                image=f'ghcr.io/100-hours-a-week/{package}:sha-' + sha * 40,
                digest='sha256:' + sha * 64, build_run_id='123')


def build_run(data):
    return {'id': 123, 'repository': {'full_name': data['source_repository']}, 'head_sha': data['source_sha'],
            'head_branch': 'main', 'path': '.github/workflows/' + mod.WORKFLOWS[data['component']],
            'event': 'push', 'status': 'completed', 'conclusion': 'success'}


class EventTests(unittest.TestCase):
    def test_rejects_v1_extra_fields_and_untrusted_identity(self):
        p = payload()
        for event in [{'action': 'image-published', 'client_payload': p},
                      {'action': 'v2-image-published', 'client_payload': p | {'surprise': 'x'}},
                      {'action': 'v2-image-published', 'client_payload': p | {'build_run_id': '1/../2'}}]:
            with self.assertRaises(ValueError):
                mod.event_payload(event)
        with self.assertRaises(ValueError):
            mod.replacement(BASE, p | {'source_repository': 'other/repo'})
        with self.assertRaises(ValueError):
            mod.replacement(BASE, p | {'image': p['image'] + '; echo unsafe'})

    def test_build_provenance_and_failed_run_rejected(self):
        p = payload()
        for fields in [{'head_branch': 'feature'}, {'head_sha': 'b'*40},
                       {'path': '.github/workflows/frontend-v1-staging-image.yml'},
                       {'conclusion': 'failure'}, {'event': 'pull_request'}]:
            with self.subTest(fields=fields), patch.object(mod, 'api', return_value=build_run(p) | fields):
                with self.assertRaises(ValueError):
                    mod.verify_build(p)

    def test_notification_can_arrive_before_run_finishes(self):
        p = payload()
        with patch.object(mod, 'api', side_effect=[build_run(p) | {'status': 'in_progress', 'conclusion': None}, build_run(p)]), \
             patch.object(mod.time, 'sleep') as wait:
            mod.verify_build(p)
            wait.assert_called_once_with(10)

    def test_stale_event_never_changes_file_or_reads_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps(BASE))
            original = path.read_bytes()
            with patch.object(mod, 'relation', return_value='behind'), patch.object(mod, 'verify_registry') as registry:
                self.assertEqual(mod.apply_event(path, payload()), 'stale')
                registry.assert_not_called()
            self.assertEqual(path.read_bytes(), original)

    def test_registry_digest_mismatch_preserves_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps(BASE))
            original = path.read_bytes()
            with patch.object(mod, 'relation', return_value='ahead'), \
                 patch.object(mod, 'command', return_value=subprocess.CompletedProcess([], 0, stdout='sha256:'+'0'*64)):
                with self.assertRaisesRegex(ValueError, 'Digest differ'):
                    mod.apply_event(path, payload())
            self.assertEqual(path.read_bytes(), original)

    def test_duplicate_event_preserves_bytes(self):
        p = payload()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps(mod.replacement(BASE, p)))
            original = path.read_bytes()
            with patch.object(mod, 'relation', return_value='identical'), patch.object(mod, 'verify_registry'):
                self.assertEqual(mod.apply_event(path, p), 'unchanged')
            self.assertEqual(path.read_bytes(), original)

    def test_diverged_history_fails_closed(self):
        with patch.object(mod, 'api', return_value={'status': 'diverged'}):
            with self.assertRaisesRegex(ValueError, 'manual review'):
                mod.relation(payload()['source_repository'], 'b'*40, 'a'*40)


class GitDeliveryTests(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.origin = self.root / 'origin.git'
        self.work = self.root / 'work'
        self.git(self.root, 'init', '--bare', str(self.origin))
        self.git(self.root, 'clone', str(self.origin), str(self.work))
        self.git(self.work, 'switch', '-c', 'main')
        self.git(self.work, 'config', 'user.name', 'test')
        self.git(self.work, 'config', 'user.email', 'test@example.invalid')
        path = self.work / mod.MANIFEST
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(BASE, indent=2)+'\n')
        self.git(self.work, 'add', '.')
        self.git(self.work, 'commit', '-m', 'baseline')
        self.git(self.work, 'push', '-u', 'origin', 'main')
        self.previous_cwd = Path.cwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self.previous_cwd)
        self.addCleanup(self.temp.cleanup)

    def rival(self, component='backend', extra_file=False):
        rival = self.root / 'rival'
        self.git(self.root, 'clone', '--branch', 'main', str(self.origin), str(rival))
        self.git(rival, 'switch', '-c', mod.BRANCH)
        self.git(rival, 'config', 'user.name', 'rival')
        self.git(rival, 'config', 'user.email', 'rival@example.invalid')
        path = rival / mod.MANIFEST
        path.write_text(json.dumps(mod.replacement(BASE, payload(component, 'b')), indent=2)+'\n')
        if extra_file:
            (rival / 'unexpected.sh').write_text('exit 1\n')
        self.git(rival, 'add', '.')
        self.git(rival, 'commit', '-m', 'other delivery')
        self.git(rival, 'push', '-u', 'origin', mod.BRANCH)

    def test_non_fast_forward_retry_preserves_other_service_and_main(self):
        real_git = mod.git
        collided = False
        def git(*args, **kwargs):
            nonlocal collided
            if args[:1] == ('push',) and not collided:
                collided = True
                self.rival()
            return real_git(*args, **kwargs)
        with patch.object(mod, 'git', side_effect=git), patch.object(mod, 'relation', return_value='ahead'), \
             patch.object(mod, 'verify_registry'), patch.object(mod.time, 'sleep'), \
             patch.object(mod, 'ensure_pr', return_value='https://example.invalid/pr') as pr:
            result = mod.publish(payload())
        self.assertTrue(collided)
        self.assertEqual(result['status'], 'updated')
        pr.assert_called_once()
        actual = json.loads(self.git(self.work, 'show', f'origin/{mod.BRANCH}:{mod.MANIFEST}'))
        self.assertEqual(actual['components']['frontend']['sourceSha'], 'a'*40)
        self.assertEqual(actual['components']['backend']['sourceSha'], 'b'*40)
        self.assertEqual(actual['components']['ai-app'], BASE['components']['ai-app'])
        self.assertEqual(json.loads(self.git(self.work, 'show', 'origin/main:' + mod.MANIFEST)), BASE)

    def test_replay_repairs_pr_creation_without_another_commit(self):
        with patch.object(mod, 'relation', return_value='ahead'), patch.object(mod, 'verify_registry'), \
             patch.object(mod, 'ensure_pr', side_effect=RuntimeError('PR creation failed')):
            with self.assertRaises(RuntimeError):
                mod.publish(payload())
        old_tip = self.git(self.work, 'rev-parse', 'HEAD')
        with patch.object(mod, 'relation', return_value='identical'), patch.object(mod, 'verify_registry'), \
             patch.object(mod, 'ensure_pr', return_value='existing-pr'):
            result = mod.publish(payload())
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(self.git(self.work, 'rev-parse', 'HEAD'), old_tip)

    def test_unrelated_code_on_automation_branch_blocks_update(self):
        self.rival(extra_file=True)
        with self.assertRaisesRegex(ValueError, 'non-manifest changes'):
            mod.publish(payload())


if __name__ == '__main__':
    unittest.main()
