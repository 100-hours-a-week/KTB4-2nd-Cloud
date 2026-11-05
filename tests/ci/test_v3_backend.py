"""Render real BE overlays; reject incomplete or mixed V2/V3 execution inputs."""
from copy import deepcopy
import importlib.util
import json
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

    def test_all_five_roles_share_image_but_have_different_switches(self):
        names = ['trip-api', 'photo-api', 'photo-converter', 'trip-result-worker']
        documents, _ = self.rendered({'workloads': {n: {'enabled': True} for n in names}})
        self.assertEqual(len(documents), 13)
        deployments = [d for d in documents if d['kind'] == 'Deployment']
        self.assertEqual(len({d['spec']['template']['spec']['containers'][0]['image'] for d in deployments}), 1)
        self.assertEqual(len({d['data']['SPRING_APPLICATION_JSON'] for d in documents if d['kind'] == 'ConfigMap'}), 2)

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
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
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

    def test_duplicate_literal_json_is_rejected(self):
        self.reject_data({'SPRING_APPLICATION_JSON': '{"app":{},"app":{}}'})

    def test_required_mongo_connection_is_a_secret_reference(self):
        documents, _ = self.rendered()
        pod = next(d for d in documents if d['kind'] == 'Deployment')['spec']['template']['spec']
        env = next(e for e in pod['containers'][0]['env'] if e['name'] == 'MONGODB_URI')
        self.assertEqual(env['valueFrom']['secretKeyRef'], {'name': 'backend-credentials', 'key': 'MONGODB_URI', 'optional': False})
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.rendered({'defaults': {'secretRefs': {'MONGODB_URI': None}}})
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.rendered({'defaults': {'secretRefs': {'SPRING_MONGODB_URI': {'name': 'backend-credentials', 'key': 'MONGODB_URI'}}}})

    def test_mongo_connection_cannot_be_plaintext(self):
        self.reject_data({'SPRING_MONGODB_URI': 'mongodb://synthetic:synthetic@db.example.invalid/yeodam'})

    def test_root_health_is_not_a_liveness_probe(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.rendered({'defaults': {'probes': {'liveness': {'httpGet': {'path': '/actuator/health'}}}}})

    def test_kafka_without_admission_is_rejected_even_on_api(self):
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.rendered({'defaults': {'springProperties': {'app': {'attachment-conversion': {'admission': None}}}}})

    def test_kafka_invalid_fetch_budget_is_rejected(self):
        self.reject_data({'ATTACHMENT_CONVERSION_PARTITION_FETCH_MAX_BYTES': '1'}, kafka=True)

    def test_kafka_recovery_zero_is_rejected(self):
        self.reject_data({'ATTACHMENT_CONVERSION_RECOVERY_PAGE_SIZE': '0'}, kafka=True)

    def test_functional_group_cannot_differ_between_namespaces(self):
        with self.assertRaises(ValueError):
            self.rendered({'workloads': {'trip-api': {'enabled': True, 'springProperties': {'app': {'trip': {
                'result-consumer-group': 'v3.foundation.yeodam.trip-other-results'}}}}}})

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

    def all_roles(self, override=None):
        enabled = {'workloads': {n: {'enabled': True} for n in ('auth-api', 'trip-api', 'photo-api', 'photo-converter', 'trip-result-worker')}}
        return self.rendered(apps.merge(enabled, override or {}))

    def test_role_matrix_and_common_admission_survive_serialization(self):
        documents, _ = self.all_roles()
        configs = {d['metadata']['name'].removesuffix('-runtime'): d['data'] for d in documents if d['kind'] == 'ConfigMap'}
        photo = ['ATTACHMENT_CONVERSION_CONSUMER_ENABLED', 'ATTACHMENT_CONVERSION_RECEIPT_LISTENER_ENABLED', 'ATTACHMENT_CONVERSION_RECOVERY_ENABLED']
        result = ['TRIP_RESULT_WORKER_ENABLED', 'TRIP_STATUS_POLLING_ENABLED', 'STORY_RESULT_LISTENER_ENABLED', 'STORY_STATUS_POLLING_ENABLED']
        for name, data in configs.items():
            spring = json.loads(data['SPRING_APPLICATION_JSON'])['app']
            self.assertEqual(data['ATTACHMENT_CONVERSION_MODE'], 'KAFKA')
            self.assertTrue(all(data[k] == ('true' if name == 'photo-converter' else 'false') for k in photo))
            self.assertTrue(all(data[k] == ('true' if name == 'trip-result-worker' else 'false') for k in result))
            self.assertIs(spring['trip']['result-listener-enabled'], name == 'trip-result-worker')
            self.assertIs(spring['outbox']['receipt-listener-enabled'], name == 'trip-result-worker')
            self.assertIs(spring['attachment-conversion']['operations']['enabled'], False)
            self.assertNotIn('result-listener-enabled', spring['story'])
            self.assertEqual(spring['attachment-conversion']['admission']['max-unfinished-photos'], {'LIGHT': 100, 'HEIC': 50})
            self.assertEqual(spring['attachment-conversion']['admission']['max-unfinished-bytes'], {'LIGHT': 1572864000, 'HEIC': 786432000})

    def test_every_environment_switch_is_explicit_and_role_bound(self):
        flags = apps.load_values([BASE])['workloads']
        for name, config in flags.items():
            if name not in ('auth-api', 'trip-api', 'photo-api', 'photo-converter', 'trip-result-worker'): continue
            for key, value in config['configMapData'].items():
                for invalid in (('false' if value == 'true' else 'true'), None):
                    with self.subTest(name=name, key=key, invalid=invalid), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        self.rendered({'workloads': {name: {'enabled': True, 'configMapData': {key: invalid}}}})

    def test_json_role_switches_cannot_be_overridden_or_stringified(self):
        for name in ('auth-api', 'photo-converter', 'trip-result-worker'):
            for section, key in [('trip', 'result-listener-enabled'), ('outbox', 'receipt-listener-enabled'), ('attachment-conversion', 'operations')]:
                invalid = {'enabled': True} if section == 'attachment-conversion' else name != 'trip-result-worker'
                with self.subTest(name=name, section=section), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                    self.rendered({'workloads': {name: {'enabled': True, 'springProperties': {'app': {section: {key: invalid}}}}}})
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.rendered({'defaults': {'springProperties': {'app': {'trip': {'result-listener-enabled': 'false'}}}}})

    def test_json_cannot_shadow_environment_or_carry_secrets(self):
        for properties in ({'app': {'story': {'result-listener-enabled': True}}},
                           {'app': {'attachment-conversion': {'consumer-enabled': True}}},
                           {'spring': {'datasource': {'password': 'synthetic'}}}):
            with self.subTest(properties=properties), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                self.rendered({'defaults': {'springProperties': properties}})

    def test_db_mode_and_local_only_aliases_are_rejected(self):
        self.reject_data({'ATTACHMENT_CONVERSION_MODE': 'DB'})
        self.reject_data({'KAFKA_TRIP_RESULT_LISTENER_ENABLED': 'true'})
        self.reject_data({'KAFKA_TRIP_RESULT_CONSUMER_GROUP': 'v3.foundation.yeodam.other'})

    def test_common_settings_are_merged_not_replaced_by_role_json(self):
        documents, _ = self.all_roles({'defaults': {'springProperties': {'app': {'attachment-conversion': {'admission': {'max-unfinished-photos': {'LIGHT': 101}}}}}}})
        for d in documents:
            if d['kind'] == 'ConfigMap':
                app = json.loads(d['data']['SPRING_APPLICATION_JSON'])['app']
                self.assertEqual(app['attachment-conversion']['admission']['max-unfinished-photos'], {'LIGHT': 101, 'HEIC': 50})
                self.assertIn('result-consumer-group', app['trip'])
                self.assertIn('receipt-consumer-group', app['outbox'])

    def test_legacy_json_is_rejected_even_when_role_flags_are_correct(self):
        self.reject_data({'SPRING_APPLICATION_JSON': '{}'})

    def test_workers_have_probes_without_services_and_resources_stay_small(self):
        documents, _ = self.all_roles()
        self.assertEqual({d['metadata']['name'] for d in documents if d['kind'] == 'Service'}, {'auth-api', 'trip-api', 'photo-api'})
        for d in documents:
            if d['kind'] != 'Deployment': continue
            pod = d['spec']['template']['spec']; c = pod['containers'][0]
            self.assertEqual(d['spec']['replicas'], 1)
            self.assertEqual(c['resources']['requests'], {'cpu': '100m', 'memory': '512Mi', 'ephemeral-storage': '64Mi'})
            self.assertEqual(c['resources']['limits']['memory'], '1Gi')
            self.assertEqual(pod['terminationGracePeriodSeconds'], 180)
            self.assertEqual(next(p for p in c['ports'] if p['name'] == 'management')['containerPort'], 9091)
            for name in ('startup', 'liveness', 'readiness'): self.assertEqual(c[name+'Probe']['httpGet']['port'], 'management')
        apps.platform.schema_check(yaml.safe_dump_all(documents))

    def test_drain_budget_shortening_is_rejected(self):
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.rendered({'defaults': {'terminationGracePeriodSeconds': 120}})

    def test_duplicate_yaml_properties_and_unstructured_json_are_rejected(self):
        file = self.write_override({})
        file.write_text('defaults:\n  springProperties:\n    app: {}\n    app: {}\n')
        with self.assertRaises(ValueError): apps.load_values([BASE, FIXTURE, file])
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.rendered({'defaults': {'springProperties': 'not-an-object'}})

    def test_tampered_rendered_json_and_environment_are_rejected(self):
        documents, values = self.all_roles()
        for key, value in [('SPRING_APPLICATION_JSON', '{}'), ('ATTACHMENT_CONVERSION_CONSUMER_ENABLED', 'true')]:
            changed = deepcopy(documents)
            next(d for d in changed if d['kind'] == 'ConfigMap' and d['metadata']['name'] == 'auth-api-runtime')['data'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): apps.validate(changed, values)

    def test_alternate_configuration_paths_cannot_bypass_roles(self):
        for key in ('APP_ATTACHMENT_CONVERSION_CONSUMER_ENABLED', 'APP_STORY_RESULT_LISTENER_ENABLED', 'SPRING_CONFIG_LOCATION', 'SPRING_PROFILES_INCLUDE'):
            with self.subTest(key=key): self.reject_data({key: 'true'})
        self.reject_data({'JAVA_TOOL_OPTIONS': '-Dapp.attachment-conversion.consumer-enabled=true'})

    def test_role_budget_and_shared_endpoint_drift_is_rejected(self):
        for data in ({'ATTACHMENT_CONVERSION_RECOVERY_MAX_REISSUES_PER_RUN': '6'}, {'KAFKA_BOOTSTRAP_SERVERS': 'other.v3.example.invalid:9092'}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.all_roles({'workloads': {'photo-converter': {'configMapData': data}}})

    def test_secret_alias_cannot_override_a_role_flag(self):
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.rendered({'defaults': {'secretRefs': {'APP_STORY_RESULT_LISTENER_ENABLED': {'name': 'backend-credentials', 'key': 'UNREVIEWED'}}}})


if __name__ == '__main__':
    unittest.main()
