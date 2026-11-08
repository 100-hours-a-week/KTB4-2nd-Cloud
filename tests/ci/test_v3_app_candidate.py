"""Candidate safety: image/source binding, SQL integrity and disabled workloads."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('candidate', ROOT / 'scripts/check-v3-app-candidate.py')
candidate = importlib.util.module_from_spec(spec); spec.loader.exec_module(candidate)


class Candidate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'candidate'
        shutil.copytree(candidate.DEFAULT, self.folder)

    def change(self, filename, update):
        path = self.folder / filename
        is_yaml = filename.endswith('.yaml')
        data = yaml.safe_load(path.read_text()) if is_yaml else json.loads(path.read_text())
        update(data)
        path.write_text(yaml.safe_dump(data) if is_yaml else json.dumps(data))

    def reject(self, filename, update):
        self.change(filename, update)
        with self.assertRaises(ValueError):
            candidate.validate(self.folder)

    def test_offline_plan_has_no_activation_or_runtime_claim(self):
        result = candidate.validate(self.folder)
        self.assertFalse(result['deploymentEnabled'])
        self.assertFalse(result['runtimeVerified'])
        self.assertEqual(len(result['backendRoles']), 5)
        self.assertIn('v35-backup-migration-and-validation', result['pending'])
        self.assertEqual(result['aiResources']['api']['requests']['memory'], '256Mi')
        self.assertIn('KAKAO_REST_API_KEY', result['aiSecretKeys'])

    def test_tag_instead_of_digest_rejected(self):
        self.reject('manifest.json', lambda d: d['components']['ai'].update(image='ghcr.io/100-hours-a-week/yeodam-ai-app:latest'))

    def test_cross_component_image_rejected(self):
        self.reject('manifest.json', lambda d: d['components']['ai'].update(image=d['components']['backend']['image']))

    def test_candidate_activation_rejected(self):
        self.reject('manifest.json', lambda d: d.update(deploymentEnabled=True))

    def test_runtime_claim_rejected(self):
        self.reject('manifest.json', lambda d: d['components']['ai'].update(runtimeVerified=True))

    def test_different_ai_source_screen_rejected(self):
        self.reject('ai-source-screen.json', lambda d: d.update(source='f' * 40))

    def test_failed_ai_capability_rejected(self):
        self.reject('ai-source-screen.json', lambda d: d['checks'].update(kafkaClientOptions=False))

    def test_migration_applied_claim_rejected(self):
        self.reject('manifest.json', lambda d: d['migration'].update(applied=True))

    def test_baseline_migration_rewrite_rejected(self):
        self.reject('manifest.json', lambda d: d['migration']['baselineMigrations'][0].update(sha256='f' * 64))

    def test_sql_tampering_rejected(self):
        (self.folder / 'V35__add_story_prepared_content_metadata.sql').write_text('DROP TABLE story_generation_jobs;')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            candidate.validate(self.folder)

    def test_be_role_enabled_rejected(self):
        self.reject('backend.values.yaml', lambda d: d['workloads']['photo-api'].update(enabled=True))

    def test_ai_role_enabled_rejected(self):
        self.reject('ai.values.yaml', lambda d: d['workloads'].update(api=True))

    def test_fake_completed_ai_review_rejected(self):
        self.reject('ai.values.yaml', lambda d: d['review'].update(imageReviewed=True))

    def test_candidate_resource_override_rejected(self):
        self.reject('backend.values.yaml', lambda d: d['defaults'].update(resources={'requests': {'memory': '4Gi'}}))

    def test_secret_value_or_unreviewed_setting_rejected(self):
        self.reject('backend.values.yaml', lambda d: d['defaults']['configMapData'].update(MYSQL_PASSWORD='fixture-only'))

    def test_model_route_prerequisite_cannot_be_removed(self):
        self.reject('manifest.json', lambda d: d['pending'].remove('shared-model-route-and-capacity-review'))

    def test_registry_report_source_mismatch_rejected(self):
        self.reject('registry-inspection.json', lambda d: d['ai'].update(source='f' * 40))

    def test_registry_report_image_mismatch_rejected(self):
        self.reject('registry-inspection.json', lambda d: d['backend'].update(image='wrong'))

    def test_registry_root_user_rejected(self):
        self.reject('registry-inspection.json', lambda d: d['ai'].update(defaultUser='root'))

    def test_ai_file_unreadable_for_chart_uid_rejected(self):
        self.reject('registry-inspection.json', lambda d: d['ai']['filesystem']['selectedFiles']['app/app/v2_main.py'].update(mode='0o600'))

    def test_renamed_ai_capability_rejected(self):
        def change(data):
            data['checks']['unknown'] = data['checks'].pop('kafkaClientOptions')
        self.reject('ai-source-screen.json', change)

    def test_source_path_traversal_rejected(self):
        self.reject('manifest.json', lambda d: d['components']['ai']['sourceFiles'].update({'../outside': 'f' * 64}))

    def test_selected_source_change_rejected(self):
        source = Path(self.temp.name) / 'ai'
        for name in json.loads((self.folder / 'manifest.json').read_text())['components']['ai']['sourceFiles']:
            path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Source file differs'):
            candidate.validate(self.folder, ai_source=source)


if __name__ == '__main__':
    unittest.main()
