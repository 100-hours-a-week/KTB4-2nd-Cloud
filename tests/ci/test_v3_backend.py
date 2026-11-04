"""Render real BE overlays; reject incomplete or mixed V2/V3 execution inputs."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('apps', ROOT / 'scripts/check-v3-apps.py')
apps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apps)
BASE = ROOT / 'deploy/v3/environments/foundation/backend.values.yaml'
KAFKA = ROOT / 'deploy/v3/environments/foundation/backend-kafka.values.yaml'
FIXTURE = ROOT / 'tests/ci/v3/backend-values.fixture.yaml'


class BackendRuntime(unittest.TestCase):
    def rendered(self, override=None, kafka=False):
        files = [BASE, *([KAFKA] if kafka else []), FIXTURE]
        with tempfile.TemporaryDirectory() as directory:
            if override:
                file = Path(directory) / 'override.yaml'
                file.write_text(yaml.safe_dump(override))
                files.append(file)
            values = apps.load_values(files)
            documents = apps.platform.load_documents(apps.render(files))
            apps.validate(documents, values)
            return documents, values

    def reject_data(self, data, kafka=False):
        with self.assertRaises((ValueError, KeyError, subprocess.CalledProcessError)):
            self.rendered({'defaults': {'configMapData': data}}, kafka)

    def test_disabled_profile_creates_nothing(self):
        self.assertEqual(apps.platform.load_documents(apps.render([BASE])), [])

    def test_single_be_has_generated_configmap_and_reference(self):
        documents, _ = self.rendered()
        self.assertEqual({d['kind'] for d in documents}, {'Deployment', 'Service', 'ConfigMap'})
        deployment = next(d for d in documents if d['kind'] == 'Deployment')
        container = deployment['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['envFrom'], [{'configMapRef': {'name': 'auth-api-runtime', 'optional': False}}])
        self.assertNotIn('cpu', container['resources']['limits'])
        apps.platform.schema_check(yaml.safe_dump_all(documents))

    def test_kafka_handoff_candidate_is_complete(self):
        documents, _ = self.rendered(kafka=True)
        config = next(d for d in documents if d['kind'] == 'ConfigMap')
        self.assertEqual(config['data']['ATTACHMENT_CONVERSION_MODE'], 'KAFKA')
        self.assertEqual(config['data']['ATTACHMENT_CONVERSION_RECOVERY_MAX_REISSUES_PER_RUN'], '5')

    def test_all_five_copies_share_image_and_functional_configuration(self):
        names = ['trip-api', 'photo-api', 'photo-converter', 'trip-result-worker']
        documents, _ = self.rendered({'workloads': {n: {'enabled': True} for n in names}})
        self.assertEqual(len(documents), 13)
        deployments = [d for d in documents if d['kind'] == 'Deployment']
        self.assertEqual(len({d['spec']['template']['spec']['containers'][0]['image'] for d in deployments}), 1)
        self.assertEqual(len({d['data']['SPRING_APPLICATION_JSON'] for d in documents if d['kind'] == 'ConfigMap'}), 1)

    def test_enable_with_missing_dependencies_is_rejected(self):
        with self.assertRaises(subprocess.CalledProcessError):
            apps.render([BASE, FIXTURE, self.write_override({'defaults': {'configMapData': {'MYSQL_URL': ''}}})])

    def write_override(self, value):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        file = Path(temporary.name) / 'override.yaml'
        file.write_text(yaml.safe_dump(value))
        return file

    def test_localhost_database_is_rejected(self):
        self.reject_data({'MYSQL_URL': 'jdbc:mysql://localhost:3306/yeodam'})

    def test_credentials_in_url_are_rejected(self):
        self.reject_data({'MYSQL_URL': 'jdbc:mysql://user:synthetic@db.example.invalid/yeodam'})

    def test_management_loopback_is_rejected(self):
        self.reject_data({'MANAGEMENT_SERVER_ADDRESS': '127.0.0.1'})

    def test_plaintext_secret_in_configmap_is_rejected(self):
        self.reject_data({'JWT_SECRET': 'synthetic-plaintext'})

    def test_configmap_cannot_shadow_secret_reference(self):
        self.reject_data({'MYSQL_USERNAME': 'synthetic-user'})

    def test_wrong_secret_key_is_rejected(self):
        with self.assertRaises(ValueError):
            self.rendered({'defaults': {'secretRefs': {'MYSQL_PASSWORD': {'key': 'WRONG'}}}})

    def test_json_cannot_hide_credentials(self):
        self.reject_data({'SPRING_APPLICATION_JSON': '{"spring":{"datasource":{"password":"synthetic"}}}'})

    def test_production_topic_is_rejected(self):
        self.reject_data({'KAFKA_TRIP_CREATE_REQUEST_TOPIC': 'prod.yeodam.trip-create.request'})

    def test_default_redis_prefix_is_rejected(self):
        self.reject_data({'AUTH_SESSION_KEY_PREFIX': 'yeodam:prod:auth:'})

    def test_direct_trip_update_group_must_be_v3(self):
        self.reject_data({'KAFKA_TRIP_UPDATE_RESULT_CONSUMER_GROUP': 'yeodam-trip-update-results'})

    def test_logical_group_collision_is_rejected(self):
        self.reject_data({'KAFKA_TRIP_UPDATE_RESULT_CONSUMER_GROUP': 'v3.foundation.yeodam.trip-results'})

    def test_duplicate_structured_keys_are_rejected(self):
        spring = apps.load_values([BASE])['defaults']['configMapData']['SPRING_APPLICATION_JSON']
        duplicated = '{"app":{},' + spring.lstrip()[1:]
        self.reject_data({'SPRING_APPLICATION_JSON': duplicated})

    def test_optional_mongo_connection_is_a_secret_reference(self):
        documents, _ = self.rendered({'defaults': {'secretRefs': {
            'SPRING_MONGODB_URI': {'name': 'backend-credentials', 'key': 'MONGODB_URI'}}}})
        pod = next(d for d in documents if d['kind'] == 'Deployment')['spec']['template']['spec']
        env = next(e for e in pod['containers'][0]['env'] if e['name'] == 'SPRING_MONGODB_URI')
        self.assertIn('secretKeyRef', env['valueFrom'])

    def test_mongo_connection_cannot_be_plaintext(self):
        self.reject_data({'SPRING_MONGODB_URI': 'mongodb://synthetic:synthetic@db.example.invalid/yeodam'})

    def test_root_health_is_not_a_liveness_probe(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.rendered({'defaults': {'probes': {'liveness': {'httpGet': {'path': '/actuator/health'}}}}})

    def test_kafka_without_admission_is_rejected(self):
        self.reject_data({'ATTACHMENT_CONVERSION_MODE': 'KAFKA'})

    def test_kafka_invalid_fetch_budget_is_rejected(self):
        self.reject_data({'ATTACHMENT_CONVERSION_PARTITION_FETCH_MAX_BYTES': '1'}, kafka=True)

    def test_kafka_recovery_zero_is_rejected(self):
        self.reject_data({'ATTACHMENT_CONVERSION_RECOVERY_PAGE_SIZE': '0'}, kafka=True)

    def test_functional_group_cannot_differ_between_namespaces(self):
        data = apps.load_values([BASE, FIXTURE])['defaults']['configMapData']['SPRING_APPLICATION_JSON']
        with self.assertRaises(ValueError):
            self.rendered({'workloads': {'trip-api': {'enabled': True, 'configMapData': {
                'SPRING_APPLICATION_JSON': data.replace('trip-results', 'trip-other-results')}}}})

    def test_config_change_updates_pod_checksum(self):
        before, _ = self.rendered()
        after, _ = self.rendered({'defaults': {'configMapData': {'VIPS_CONCURRENCY': '2'}}})
        annotation = lambda docs: next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['metadata']['annotations']
        self.assertNotEqual(annotation(before)['yeodam.io/configuration-checksum'], annotation(after)['yeodam.io/configuration-checksum'])

    def test_rendered_configmap_tampering_is_rejected(self):
        documents, values = self.rendered()
        changed = deepcopy(documents)
        next(d for d in changed if d['kind'] == 'ConfigMap')['data']['MYSQL_URL'] = 'jdbc:mysql://wrong.example.invalid/other'
        with self.assertRaises(ValueError):
            apps.validate(changed, values)

    def test_non_be_workload_cannot_use_be_overlay(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.rendered({'workloads': {'trip-websocket': {'enabled': True}}, 'images': {'websocket': 'ghcr.io/example/ws@sha256:' + '4' * 64}})


if __name__ == '__main__':
    unittest.main()
