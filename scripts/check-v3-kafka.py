#!/usr/bin/env python3
"""Render pinned Strimzi and isolated Kafka; no AWS/Kubernetes/Secret access."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('platform_contract', ROOT / 'scripts/check-v3-platform.py')
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)
require = platform.require
CHART = ROOT / 'deploy/v3/kafka'
OPERATOR = ROOT / 'deploy/v3/kafka-operator'
LOCK = json.loads((OPERATOR / 'upstream.lock.json').read_text())
CLUSTER = 'yeodam-v3-kafka'
NS = 'kafka'
FOUNDATION = ROOT / 'deploy/v3/environments/foundation/kafka.values.yaml'
MANAGEMENT_PORTS = [9090, 9091, 8443]


def render(chart=CHART, values=None, include_crds=False):
    command = [os.environ['V3_HELM'], 'template',
               'strimzi' if include_crds else CLUSTER, str(chart),
               '--namespace', NS if include_crds else 'default', '--kube-version', '1.36.0']
    if values:
        command += ['-f', str(values)]
    if include_crds:
        command += ['--include-crds']
    return subprocess.check_output(command, text=True)


def unique(documents):
    identities = [(d['kind'], d['metadata'].get('namespace', ''), d['metadata']['name']) for d in documents]
    require(len(set(identities)) == len(identities), 'Duplicate Kafka resource identity')
    cluster_kinds = {'Namespace', 'CustomResourceDefinition', 'ClusterRole', 'ClusterRoleBinding'}
    require(all(ns == ('' if kind in cluster_kinds else NS) for kind, ns, _ in identities),
            'Kafka resources must stay in kafka namespace; only reviewed infrastructure is cluster scoped')


def validate_operator(documents):
    unique(documents)
    require(Counter(d['kind'] for d in documents) == {
        'CustomResourceDefinition': 10, 'ClusterRole': 7, 'ClusterRoleBinding': 3,
        'RoleBinding': 4, 'ServiceAccount': 1, 'ConfigMap': 1, 'Deployment': 1},
        'Pinned Strimzi inventory changed')
    deployment = next(d for d in documents if d['kind'] == 'Deployment')
    require(deployment['metadata']['namespace'] == NS and deployment['spec']['replicas'] == 1,
            'Exactly one Strimzi instance in kafka required')
    pod = deployment['spec']['template']['spec']
    require(pod.get('nodeSelector') == {'yeodam.io/role': 'system'} and not pod.get('tolerations'),
            'Operator belongs on existing system nodes')
    require(not pod.get('hostNetwork', False) and not pod.get('initContainers')
            and len(pod['containers']) == 1 and pod.get('enableServiceLinks') is False,
            'Unexpected operator networking or containers')
    c = pod['containers'][0]
    require(c['image'] == LOCK['operatorImage'], 'Operator image digest changed')
    require(c['resources'] == {'requests': {'cpu': '100m', 'memory': '384Mi'},
                              'limits': {'memory': '512Mi'}}, 'Operator budget changed')
    require(c['securityContext'] == {
        'allowPrivilegeEscalation': False, 'runAsNonRoot': True, 'readOnlyRootFilesystem': True,
        'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}},
        'Restricted operator security required')
    env = {e['name']: e for e in c['env']}
    require(len(env) == len(c['env']), 'Duplicate environment variable overrides')
    require(env['STRIMZI_NAMESPACE'] == {'name': 'STRIMZI_NAMESPACE',
            'valueFrom': {'fieldRef': {'fieldPath': 'metadata.namespace'}}}, 'Namespace-wide watch only')
    require(env['JAVA_OPTS']['value'] == '-Xms128m -Xmx256m', 'Operator heap budget changed')
    require(env['STRIMZI_POD_SECURITY_PROVIDER_CLASS']['value'] ==
            'io.strimzi.plugin.security.profiles.impl.RestrictedPodSecurityProvider', 'Restricted operands required')
    for name in ('TOPIC_OPERATOR', 'USER_OPERATOR', 'KAFKA_INIT'):
        require(env[f'STRIMZI_DEFAULT_{name}_IMAGE']['value'] == LOCK['operatorImage'],
                'Enabled operand must use pinned operator digest')
    expected_roles = {'strimzi-cluster-operator-' + suffix for suffix in
                      ('namespaced', 'global', 'leader-election', 'watched')}
    expected_roles |= {'strimzi-kafka-broker', 'strimzi-entity-operator', 'strimzi-kafka-client'}
    require({d['metadata']['name'] for d in documents if d['kind'] == 'ClusterRole'} == expected_roles,
            'Unexpected cluster permissions')
    for d in documents:
        kind = d['kind']
        if kind == 'ClusterRole':
            require(not d.get('aggregationRule') and not any(k.startswith('rbac.authorization.k8s.io/aggregate-')
                    for k in d['metadata'].get('labels', {})), 'Do not extend built-in admin roles')
            for rule in d['rules']:
                require(not any('*' in rule.get(k, []) for k in ('apiGroups', 'resources', 'verbs')),
                        'No wildcard RBAC')
        elif kind.endswith('RoleBinding'):
            require(d['roleRef']['name'] in expected_roles, 'No cluster-admin binding')
            require(d['subjects'] == [{'kind': 'ServiceAccount', 'name': 'strimzi-cluster-operator',
                                      'namespace': NS}], 'Only the dedicated operator account may be bound')
            if kind == 'RoleBinding':
                require(d['metadata']['namespace'] == NS, 'Namespace RBAC must stay in kafka')
            else:
                require(d['roleRef']['name'] in {'strimzi-cluster-operator-global',
                        'strimzi-kafka-broker', 'strimzi-kafka-client'}, 'No namespace permissions cluster-wide')
        elif kind == 'CustomResourceDefinition':
            require(d['spec']['scope'] == 'Namespaced', 'Namespaced custom resources only')
            require([v['name'] for v in d['spec']['versions'] if v.get('served')] == ['v1'],
                    'Strimzi 1.3 uses v1 only')


def validate_cluster(documents, smoke=False, enabled=True):
    unique(documents)
    expected = {'Namespace': 1, 'NetworkPolicy': 6}
    if enabled:
        expected.update({'Kafka': 1, 'KafkaNodePool': 1})
    require(not smoke or enabled, 'Smoke requires a running Kafka cluster')
    if smoke:
        expected.update({'KafkaUser': 1, 'KafkaTopic': 1, 'Job': 1})
    require(Counter(d['kind'] for d in documents) == expected, 'Unexpected workload, disk, endpoint or credentials')
    require(all(d['metadata'].get('labels', {}).get('app.kubernetes.io/part-of') == 'yeodam'
            and d['metadata'].get('labels', {}).get('yeodam.io/stage') == 'v3' for d in documents),
            'All owned Kafka resources must identify Yeodam V3')
    by_kind = {d['kind']: d for d in documents if d['kind'] != 'NetworkPolicy'}
    namespace = by_kind['Namespace']
    require(namespace['metadata']['name'] == NS and namespace['metadata']['annotations'] ==
            {'helm.sh/resource-policy': 'keep'}, 'Preserve isolated namespace')
    labels = namespace['metadata']['labels']
    for mode in ('enforce', 'audit', 'warn'):
        require(labels.get(f'pod-security.kubernetes.io/{mode}') == 'restricted' and
                labels.get(f'pod-security.kubernetes.io/{mode}-version') == 'v1.36', 'Restricted namespace required')
    validate_policies(documents)
    if not enabled:
        return
    obj = by_kind['Kafka']
    require(obj['metadata']['name'] == CLUSTER, 'Wrong Kafka cluster')
    require(obj['metadata'].get('annotations') == {'helm.sh/resource-policy': 'keep'}, 'Keep Kafka on uninstall')
    require(set(obj['spec']) == {'kafka', 'entityOperator'}, 'No exporter, balancing, Connect or extra operands')
    k = obj['spec']['kafka']
    require(set(k) == {'version', 'image', 'rack', 'listeners', 'authorization', 'config', 'template'},
            'No implicit extra Kafka operands or settings')
    require(k['version'] == LOCK['kafkaVersion'] and k['image'] == LOCK['kafkaImage'], 'Kafka version/image mismatch')
    require(k['listeners'] == [{'name': 'tls', 'port': 9093, 'type': 'internal', 'tls': True,
        'authentication': {'type': 'scram-sha-512'}, 'networkPolicyPeers': [{'podSelector': {
            'matchLabels': {'app.kubernetes.io/instance': 'kafka-smoke'}}}]}],
        'Only authenticated internal TLS and explicit smoke peer allowed')
    require(k['rack'] == {'topologyKey': 'topology.kubernetes.io/zone'}, 'Kafka rack identity must match AZ')
    require(k['authorization'] == {'type': 'simple'}, 'Kafka ACL authorization required')
    require(k['config'] == {'auto.create.topics.enable': False, 'default.replication.factor': 3,
        'min.insync.replicas': 2, 'offsets.topic.replication.factor': 3,
        'transaction.state.log.replication.factor': 3, 'transaction.state.log.min.isr': 2,
        'num.partitions': 3, 'log.retention.hours': 24, 'log.retention.bytes': 268435456,
        'log.segment.bytes': 67108864}, 'Replication, acknowledgement safety or startup disk retention changed')
    require(k['template'] == {'podDisruptionBudget': {'maxUnavailable': 1}}, 'Single broker voluntary disruption')
    entity = obj['spec']['entityOperator']
    for component in ('topicOperator', 'userOperator'):
        require(entity[component] == {'watchedNamespace': NS, 'resources': {
            'requests': {'cpu': '50m', 'memory': '256Mi'}, 'limits': {'memory': '384Mi'}},
            'jvmOptions': {'-Xms': '128m', '-Xmx': '128m'}}, 'Entity operator budget/scope changed')
    entity_pod = entity['template']['pod']
    require(entity_pod == {'affinity': system_affinity(), 'enableServiceLinks': False,
            'metadata': {'labels': {'app.kubernetes.io/name': 'kafka-entity-operator',
            'app.kubernetes.io/instance': CLUSTER, 'app.kubernetes.io/component': 'entity-operator',
            'app.kubernetes.io/part-of': 'yeodam', 'yeodam.io/stage': 'v3'}}},
            'Entity operators must use existing system nodes')
    pool_obj = by_kind['KafkaNodePool']
    require(pool_obj['metadata']['labels']['strimzi.io/cluster'] == CLUSTER
            and pool_obj['metadata']['name'] == 'dual-role'
            and pool_obj['metadata'].get('annotations') == {'helm.sh/resource-policy': 'keep'}, 'Wrong pool ownership')
    p = pool_obj['spec']
    require(p['replicas'] == 3 and p['roles'] == ['broker', 'controller'], 'Three combined voters required')
    require(p['resources'] == {'requests': {'cpu': '250m', 'memory': '1Gi'},
                              'limits': {'memory': '1Gi'}}
            and p['jvmOptions'] == {'-Xms': '512m', '-Xmx': '512m'}, 'Minimum-size broker memory budget changed')
    require(p['storage'] == {'type': 'jbod', 'volumes': [{'id': 0, 'type': 'persistent-claim',
        'class': 'yeodam-v3-gp3-retain', 'size': '10Gi', 'kraftMetadata': 'shared', 'deleteClaim': False}]},
        'Retain encrypted gp3 data volumes via the reviewed StorageClass')
    pod = p['template']['pod']
    selector = {'strimzi.io/cluster': CLUSTER, 'strimzi.io/pool-name': 'dual-role'}
    require(pod['affinity'] == {
        'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [
            {'matchExpressions': [{'key': 'yeodam.io/role', 'operator': 'In', 'values': ['kafka']}]}]}},
        'podAntiAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': [
            {'labelSelector': {'matchLabels': selector}, 'topologyKey': 'kubernetes.io/hostname'}]}},
        'Exactly one broker per dedicated node')
    require(pod['tolerations'] == [{'key': 'yeodam.io/role', 'operator': 'Equal',
            'value': 'kafka', 'effect': 'NoSchedule'}], 'Dedicated Kafka taint only')
    require(pod['topologySpreadConstraints'] == [{'maxSkew': 1, 'minDomains': 3,
        'topologyKey': 'topology.kubernetes.io/zone', 'whenUnsatisfiable': 'DoNotSchedule',
        'nodeAffinityPolicy': 'Honor', 'nodeTaintsPolicy': 'Honor',
        'labelSelector': {'matchLabels': selector}}], 'Preserve one broker per AZ')
    if smoke:
        user, topic, job = (by_kind[kind] for kind in ('KafkaUser', 'KafkaTopic', 'Job'))
        require(user['metadata']['name'] == topic['metadata']['name'] == 'v3-smoke', 'Owned smoke resources only')
        require(user['spec'] == {'authentication': {'type': 'scram-sha-512'}, 'authorization': {
            'type': 'simple', 'acls': [
                {'resource': {'type': 'topic', 'name': 'v3-smoke', 'patternType': 'literal'},
                 'operations': ['Read', 'Write', 'Describe'], 'host': '*'},
                {'resource': {'type': 'group', 'name': 'v3-smoke-', 'patternType': 'prefix'},
                 'operations': ['Read'], 'host': '*'}]}}, 'No business-topic or cluster-admin smoke ACL')
        require(topic['spec'] == {'partitions': 3, 'replicas': 3, 'config': {'min.insync.replicas': 2,
                'retention.ms': 3600000, 'retention.bytes': 10485760, 'segment.bytes': 1048576}}, 'Bounded replicated smoke topic')
        jp = job['spec']['template']['spec']; c = jp['containers'][0]
        require(job['spec']['activeDeadlineSeconds'] == 360 and job['spec']['backoffLimit'] == 0,
                'Bounded one-shot smoke only')
        require(jp['automountServiceAccountToken'] is False and jp['nodeSelector'] == {'yeodam.io/role': 'system'}
                and c['image'] == LOCK['kafkaImage'], 'Smoke needs no API access or new nodes')
        require('acks=all' in c['args'][0] and 'grep -Fxq' in c['args'][0]
                and 'set -x' not in c['args'][0], 'Authenticated matching token test without credential tracing')


def validate_policies(documents):
    policies = {d['metadata']['name']: d['spec'] for d in documents if d['kind'] == 'NetworkPolicy'}
    require(policies['default-deny'] == {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
                                       'ingress': [], 'egress': []}, 'Both directions deny by default')
    require(policies['allow-cluster-dns'] == platform.dns_spec(), 'DNS to CoreDNS only')
    require(policies['allow-private-api'] == {'podSelector': {}, 'policyTypes': ['Egress'],
        'egress': [{'to': [{'ipBlock': {'cidr': '10.40.0.0/16'}}, {'ipBlock': {'cidr': '172.20.0.1/32'}}],
                    'ports': [{'protocol': 'TCP', 'port': 443}]}]}, 'Private Kubernetes API only')
    # Pinned explicit peer/port shapes also catch accidental allow-all rules.
    cluster_peer = {'podSelector': {'matchLabels': {'strimzi.io/cluster': CLUSTER}}}
    internal_ports = [{'protocol': 'TCP', 'port': n} for n in (9090, 9091, 9093)]
    require(policies['allow-kafka-internal'] == {'podSelector': cluster_peer['podSelector'],
        'policyTypes': ['Ingress', 'Egress'], 'ingress': [{'from': [cluster_peer], 'ports': internal_ports}],
        'egress': [{'to': [cluster_peer], 'ports': internal_ports}]}, 'No broad internal network bypass')
    require(policies['allow-operator-admin'] == {'podSelector': {'matchLabels': {'name': 'strimzi-cluster-operator'}},
        'policyTypes': ['Egress'], 'egress': [{'to': [cluster_peer], 'ports': [
            {'protocol': 'TCP', 'port': n} for n in MANAGEMENT_PORTS]}]}, 'Operator management connectivity changed')
    require(policies['allow-smoke-client'] == {'podSelector': {'matchLabels': {
        'app.kubernetes.io/instance': 'kafka-smoke'}}, 'policyTypes': ['Egress'], 'egress': [{'to': [
            {'podSelector': {'matchLabels': {'strimzi.io/cluster': CLUSTER, 'strimzi.io/kind': 'Kafka'}}}],
            'ports': [{'protocol': 'TCP', 'port': 9093}]}]}, 'Smoke client only reaches TLS Kafka')


def system_affinity():
    return {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [
        {'matchExpressions': [{'key': 'yeodam.io/role', 'operator': 'In', 'values': ['system']}]}]}}}


def write_custom_schemas(operator):
    def strict(schema):
        if isinstance(schema, dict):
            if schema.get('type') == 'object' and 'properties' in schema and 'additionalProperties' not in schema:
                schema['additionalProperties'] = False
            for value in schema.values():
                strict(value)
        elif isinstance(schema, list):
            for value in schema:
                strict(value)
    for d in operator:
        if d['kind'] == 'CustomResourceDefinition':
            v = next(v for v in d['spec']['versions'] if v['name'] == 'v1')
            schema = v['schema']['openAPIV3Schema']
            strict(schema)
            (Path(os.environ['V3_SCHEMA_DIR']) / (d['spec']['names']['kind'].lower() + '.json')).write_text(json.dumps(schema))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    archive = args.directory / 'strimzi.tgz'
    require(hashlib.sha256(archive.read_bytes()).hexdigest() == LOCK['sha256'], 'Strimzi chart checksum mismatch')
    operator_source = render(archive, OPERATOR / 'values.yaml', include_crds=True)
    operator = platform.load_documents(operator_source)
    validate_operator(operator)
    write_custom_schemas(operator)
    platform.schema_check(yaml.safe_dump_all(d for d in operator if d['kind'] != 'CustomResourceDefinition'))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'kafka-operator.yaml').write_text(operator_source)
    baseline = render()
    validate_cluster(platform.load_documents(baseline), enabled=False)
    platform.schema_check(baseline)
    (args.output / 'kafka-baseline.yaml').write_text(baseline)
    for smoke in (False, True):
        source = render(values=ROOT / 'tests/ci/v3/kafka-smoke.values.yaml' if smoke else FOUNDATION)
        validate_cluster(platform.load_documents(source), smoke)
        platform.schema_check(source)
        (args.output / ('kafka-smoke.yaml' if smoke else 'kafka.yaml')).write_text(source)
    print(json.dumps({'clusterAccessed': False, 'realSecretsRead': False, 'brokerPods': 3,
          'brokerMemoryLimit': '1Gi', 'dataVolumes': '3 x 10Gi retained gp3', 'operatorPods': 2,
          'additionalEc2': 0, 'additionalNat': 0}, sort_keys=True))


if __name__ == '__main__':
    main()
