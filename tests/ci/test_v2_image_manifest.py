import argparse
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('v2_manifest', ROOT / 'scripts/v2-image-manifest.py')
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


class V2ManifestTests(unittest.TestCase):
    def setUp(self):
        self.baseline = manifest.load(ROOT / 'deploy/v2/image-manifest.json')

    def event(self, **changes):
        entry = self.baseline['components']['frontend']
        values = dict(component='frontend', source_repository=entry['sourceRepository'],
                      source_sha='a' * 40,
                      image='ghcr.io/100-hours-a-week/yeodam-frontend:sha-' + 'a' * 40,
                      digest='sha256:' + 'b' * 64)
        return argparse.Namespace(**(values | changes))

    def test_update_preserves_other_components_and_repeat_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps(self.baseline))
            manifest.update(path, self.event())
            updated = manifest.load(path)
            for name in ['backend', 'ai-app']:
                self.assertEqual(updated['components'][name], self.baseline['components'][name])
            original = path.read_bytes()
            with patch.object(manifest.os, 'replace', side_effect=AssertionError('should not write')):
                manifest.update(path, self.event())
            self.assertEqual(path.read_bytes(), original)

    def test_bad_events_do_not_overwrite_file(self):
        for changes in [
            {'source_repository': 'untrusted/repo'},
            {'source_sha': '$(touch /tmp/unsafe)'},
            {'image': 'ghcr.io/100-hours-a-week/yeodam-frontend:latest'},
            {'digest': 'sha256:abc'},
        ]:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'manifest.json'
                path.write_text(json.dumps(self.baseline))
                original = path.read_bytes()
                with self.assertRaises(ValueError):
                    manifest.update(path, self.event(**changes))
                self.assertEqual(path.read_bytes(), original)

    def test_v1_ai_worker_package_is_rejected(self):
        data = copy.deepcopy(self.baseline)
        entry = data['components']['ai-app']
        entry['image'] = entry['image'].replace('yeodam-ai-app:', 'yeodam-ai-worker:')
        with self.assertRaises(ValueError):
            manifest.validate(data)

    def test_unknown_components_and_non_amd64_are_rejected(self):
        for kind in ['extra', 'missing', 'platform']:
            with self.subTest(kind=kind):
                data = copy.deepcopy(self.baseline)
                if kind == 'extra':
                    data['components']['ai-worker'] = {}
                elif kind == 'missing':
                    del data['components']['backend']
                else:
                    data['components']['frontend']['platform'] = 'linux/arm64'
                with self.assertRaises(ValueError):
                    manifest.validate(data)

    def test_duplicate_json_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text('{"schemaVersion":1,"schemaVersion":1,"components":{}}')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                manifest.load(path)

    def test_failed_atomic_replace_keeps_original_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            path.write_text(json.dumps(self.baseline))
            original = path.read_bytes()
            with patch.object(manifest.os, 'replace', side_effect=OSError('replace failed')):
                with self.assertRaises(OSError):
                    manifest.update(path, self.event())
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_remote_mismatch_blocks_pull(self):
        with patch.object(manifest.subprocess, 'run', return_value=subprocess.CompletedProcess(
                [], 0, stdout='sha256:' + '0' * 64)) as run:
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                manifest.verify(self.baseline)
            self.assertEqual(run.call_count, 1)

    def test_registry_failure_is_not_ignored(self):
        with patch.object(manifest.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'docker')):
            with self.assertRaises(subprocess.CalledProcessError):
                manifest.verify(self.baseline)

    def test_verify_pulls_each_immutable_digest_for_amd64(self):
        responses = []
        for entry in self.baseline['components'].values():
            responses.extend([subprocess.CompletedProcess([], 0, stdout=entry['digest'] + '\n'),
                              subprocess.CompletedProcess([], 0)])
        with patch.object(manifest.subprocess, 'run', side_effect=responses) as run:
            manifest.verify(self.baseline)
        for call, entry in zip(run.call_args_list[1::2], self.baseline['components'].values()):
            self.assertEqual(call.args[0], [
                'docker', 'pull', '--platform', 'linux/amd64',
                entry['image'].split(':sha-', 1)[0] + '@' + entry['digest'],
            ])


if __name__ == '__main__':
    unittest.main()
