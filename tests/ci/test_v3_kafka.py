import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('kafka_contract', ROOT / 'scripts/check-v3-kafka.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class KafkaContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = m.platform.load_documents(m.render(values=m.FOUNDATION))
        cls.smoke = m.platform.load_documents(m.render(values=ROOT / 'tests/ci/v3/kafka-smoke.values.yaml'))
        directory = Path(os.environ['V3_SCHEMA_DIR']).parent
        cls.operator = m.platform.load_documents(m.render(directory / 'strimzi.tgz',
                                    m.OPERATOR / 'values.yaml', include_crds=True))
        m.write_custom_schemas(cls.operator)

    def mutate(self, kind, path, value, *, smoke=False):
        docs = copy.deepcopy(self.smoke if smoke else self.base)
        obj = next(d for d in docs if d['kind'] == kind)
        for key in path[:-1]:
            obj = obj[key]
        obj[path[-1]] = value
        with self.assertRaises(m.platform.ContractError):
            m.validate_cluster(docs, smoke)

    def test_default_and_smoke_contracts(self):
        m.validate_operator(self.operator)
        m.validate_cluster(self.base)
        m.validate_cluster(self.smoke, True)
        self.assertFalse(any(d['kind'] in ('Secret', 'Job', 'KafkaUser', 'KafkaTopic') for d in self.base))

    def test_no_public_plaintext_or_unauthenticated_listener(self):
        for path, value in [(['type'], 'loadbalancer'), (['tls'], False),
                            (['authentication', 'type'], 'tls'), (['networkPolicyPeers'], [{}])]:
            with self.subTest(path=path):
                self.mutate('Kafka', ['spec', 'kafka', 'listeners', 0] + path, value)

    def test_baseline_install_needs_no_crds_or_adoption(self):
        baseline = m.platform.load_documents(m.render())
        m.validate_cluster(baseline, enabled=False)
        self.assertEqual({d['kind'] for d in baseline}, {'Namespace', 'NetworkPolicy'})
        m.platform.schema_check(m.yaml.safe_dump_all(baseline))
        result = subprocess.run([os.environ['V3_HELM'], 'template', 'test', str(m.CHART),
                '--set', 'smoke.enabled=true'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, 'Smoke must not bypass the explicit cluster activation')

    def test_namespace_and_rack_boundary(self):
        self.mutate('Kafka', ['metadata', 'namespace'], 'default')
        self.mutate('Kafka', ['metadata', 'namespace'], '')
        self.mutate('Kafka', ['spec', 'kafka', 'rack', 'topologyKey'], 'kubernetes.io/hostname')

    def test_smoke_shell_is_valid(self):
        job = next(d for d in self.smoke if d['kind'] == 'Job')
        command = job['spec']['template']['spec']['containers'][0]['args'][0]
        subprocess.run(['bash', '-n'], input=command, text=True, check=True)
        self.assertIn('SaslAuthenticationException', command)
        self.assertIn('TopicAuthorizationException', command)
        self.assertIn('--delete --group', command)
        self.assertIn('temporary consumer group removed', command)
        self.assertIn('--partition 0 --offset earliest', command)
        self.assertIn('^v3-smoke-v3-smoke-[0-9]+-[0-9]+$', command)

    def test_replication_output_requires_three_insync_replicas(self):
        script = ROOT / 'deploy/v3/kafka/files/check-replicas.awk'
        complete = ''.join(f'Topic: v3-smoke Partition: {i} Leader: 0 Replicas: 0,1,2 Isr: 0,1,2 Elr: LastKnownElr:\n'
                           for i in range(3))
        for source, expected in [(complete, 0), (complete.replace('Isr: 0,1,2', 'Isr: 0,1'), 1),
                                 (complete.splitlines()[0], 1), ('', 1),
                                 (complete.replace('Replicas: 0,1,2', 'Replicas: 0'), 1)]:
            with self.subTest(source=source):
                result = subprocess.run(['awk', '-f', str(script)], input=source, text=True)
                self.assertEqual(result.returncode, expected)

    def test_acl_rejection_uses_explicit_error_even_when_cli_exits_zero(self):
        job = next(d for d in self.smoke if d['kind'] == 'Job')
        command = job['spec']['template']['spec']['containers'][0]['args'][0]
        start = command.index('timeout 45 /opt/kafka/bin/kafka-console-consumer.sh')
        end = command.index("echo 'PASS: invalid password and non-owned topic rejected'", start)
        block = command[start:end]
        for message, code, expected in [('TopicAuthorizationException: denied', '0', 0),
                                        ('TOPIC_AUTHORIZATION_FAILED', '1', 0),
                                        ('', '0', 1), ('TimeoutException', '124', 1)]:
            with self.subTest(message=message, code=code), tempfile.TemporaryDirectory() as directory:
                source = 'timeout() { printf "%s\\n" "$FAKE_OUTPUT"; return "$FAKE_RC"; };\n'
                source += block.replace('/tmp/acl.log', str(Path(directory) / 'acl.log'))
                result = subprocess.run(['bash', '-ec', source], capture_output=True, text=True,
                                        env=dict(os.environ, FAKE_OUTPUT=message, FAKE_RC=code))
                self.assertEqual(result.returncode, expected)

    def test_replication_and_insync_safety(self):
        for key, value in [('min.insync.replicas', 1), ('default.replication.factor', 1),
                           ('offsets.topic.replication.factor', 1), ('transaction.state.log.min.isr', 1),
                           ('auto.create.topics.enable', True), ('log.retention.hours', 168)]:
            with self.subTest(key=key):
                self.mutate('Kafka', ['spec', 'kafka', 'config', key], value)

    def test_no_optional_costly_operands(self):
        self.mutate('Kafka', ['spec', 'cruiseControl'], {})
        self.mutate('Kafka', ['spec', 'kafka', 'tieredStorage'], {})

    def test_version_digest_coupling(self):
        for key, value in [('version', '4.3.1'), ('image', 'quay.io/strimzi/kafka:latest')]:
            self.mutate('Kafka', ['spec', 'kafka', key], value)

    def test_broker_count_heap_and_resources(self):
        for path, value in [(['replicas'], 1), (['roles'], ['broker']),
                            (['jvmOptions', '-Xmx'], '1g'),
                            (['resources', 'limits', 'memory'], '2Gi')]:
            with self.subTest(path=path):
                self.mutate('KafkaNodePool', ['spec'] + path, value)

    def test_storage_retention_and_budget(self):
        for key, value in [('deleteClaim', True), ('class', 'gp2'), ('size', '100Gi'), ('type', 'ephemeral')]:
            with self.subTest(key=key):
                self.mutate('KafkaNodePool', ['spec', 'storage', 'volumes', 0, key], value)
        self.mutate('Kafka', ['metadata', 'annotations'], {})
        self.mutate('KafkaNodePool', ['metadata', 'annotations'], {})

    def test_three_az_and_taint_placement(self):
        base = ['spec', 'template', 'pod']
        self.mutate('KafkaNodePool', base + ['affinity'], {})
        self.mutate('KafkaNodePool', base + ['tolerations', 0, 'operator'], 'Exists')
        self.mutate('KafkaNodePool', base + ['topologySpreadConstraints', 0, 'minDomains'], 1)
        self.mutate('KafkaNodePool', base + ['topologySpreadConstraints', 0, 'whenUnsatisfiable'], 'ScheduleAnyway')

    def test_no_network_allow_all(self):
        for policy in ('default-deny', 'allow-private-api', 'allow-kafka-internal',
                       'allow-operator-admin', 'allow-smoke-client'):
            docs = copy.deepcopy(self.base)
            next(d for d in docs if d['kind'] == 'NetworkPolicy' and d['metadata']['name'] == policy)['spec']['egress'] = [{}]
            with self.subTest(policy=policy), self.assertRaises(m.platform.ContractError):
                m.validate_cluster(docs)

    def test_smoke_cannot_touch_business_topic(self):
        self.mutate('KafkaUser', ['spec', 'authorization', 'acls', 0, 'resource', 'name'], '*', smoke=True)
        self.mutate('KafkaUser', ['spec', 'authorization', 'acls', 0, 'operations'],
                    ['Read', 'Write', 'Describe'], smoke=True)
        self.mutate('KafkaUser', ['spec', 'authorization', 'acls', 0, 'operations'], ['All'], smoke=True)
        self.mutate('KafkaTopic', ['spec', 'replicas'], 1, smoke=True)
        self.mutate('Job', ['spec', 'template', 'spec', 'automountServiceAccountToken'], True, smoke=True)

    def test_no_extra_secret_storage_or_deployment(self):
        for kind in ('Secret', 'PersistentVolumeClaim', 'KafkaConnect', 'Deployment'):
            with self.subTest(kind=kind), self.assertRaises(m.platform.ContractError):
                m.validate_cluster(self.base + [{'kind': kind, 'metadata': {'name': 'unexpected', 'namespace': 'kafka'}}])
        with self.assertRaises(m.platform.ContractError):
            m.validate_cluster(self.base + [self.base[0]])

    def test_operator_cannot_watch_other_namespaces_or_gain_admin(self):
        for change in ('watch', 'image', 'env', 'admin', 'global-watched'):
            docs = copy.deepcopy(self.operator)
            c = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']['containers'][0]
            if change == 'watch': c['env'][0] = {'name': 'STRIMZI_NAMESPACE', 'value': '*'}
            elif change == 'image': c['image'] = 'quay.io/strimzi/operator:latest'
            elif change == 'env': c['env'].append(c['env'][0])
            elif change == 'admin': next(d for d in docs if d['kind'] == 'ClusterRoleBinding')['roleRef']['name'] = 'cluster-admin'
            else:
                next(d for d in docs if d['kind'] == 'ClusterRoleBinding')['roleRef']['name'] = 'strimzi-cluster-operator-watched'
            with self.subTest(change=change), self.assertRaises(m.platform.ContractError):
                m.validate_operator(docs)

    def test_schema_checks_custom_resource_and_smoke_job_without_skips(self):
        m.platform.schema_check(m.yaml.safe_dump_all(self.base))
        m.platform.schema_check(m.yaml.safe_dump_all(self.smoke))

    def test_custom_schema_rejects_unknown_field_and_wrong_type(self):
        for key, value in [('unknownField', True), ('replicas', 'three')]:
            docs = copy.deepcopy(self.base)
            obj = next(d for d in docs if d['kind'] == 'KafkaNodePool'); obj['spec'][key] = value
            with self.subTest(key=key), self.assertRaises(subprocess.CalledProcessError):
                subprocess.run([os.environ['V3_KUBECONFORM'], '-strict', '-schema-location',
                    str(Path(os.environ['V3_SCHEMA_DIR']) / '{{.ResourceKind}}.json')],
                    input=m.yaml.safe_dump_all(docs), text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=True)

    def test_values_reject_expensive_or_unknown_overrides(self):
        for override in ('broker.replicas=4', 'broker.volumeSize=100Gi', 'unexpected.enabled=true'):
            result = subprocess.run([os.environ['V3_HELM'], 'template', 'test', str(m.CHART),
                    '--set', override], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, override)


if __name__ == '__main__':
    unittest.main()
