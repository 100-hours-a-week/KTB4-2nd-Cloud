#!/usr/bin/env python3
"""Render business Kafka CRs and validate exact topics, groups and least-privilege ACLs offline."""
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
spec = importlib.util.spec_from_file_location('kafka_contract', ROOT / 'scripts/check-v3-kafka.py')
kafka = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kafka)
require = kafka.require
CHART = ROOT / 'deploy/v3/kafka-access'
CONTRACT = json.loads((CHART / 'files/business-contract.json').read_text())
VALUES = yaml.safe_load((CHART / 'values.yaml').read_text())
FOUNDATION = ROOT / 'deploy/v3/environments/foundation/kafka-access.values.yaml'


def render(files=()):
    args = [os.environ['V3_HELM'], 'template', 'yeodam-v3-kafka-access', str(CHART),
            '--namespace', 'default', '--kube-version', '1.36.0']
    for file in files:
        args += ['-f', str(file)]
    return subprocess.check_output(args, text=True)


def acl_rules(access):
    rules = []
    for topic in sorted(set(access['readTopics'] + access['writeTopics'] + access['describeTopics'])):
        operations = ['Describe']
        if topic in access['readTopics']:
            operations += ['Read']
        if topic in access['writeTopics']:
            operations += ['Write']
        operations += access.get('extraTopicOperations', {}).get(topic, [])
        rules += [{'resource': {'type': 'topic', 'name': topic, 'patternType': 'literal'},
                   'operations': operations, 'host': '*'}]
    rules += [{'resource': {'type': 'group', 'name': group, 'patternType': 'literal'},
               'operations': ['Read'], 'host': '*'} for group in access['groups']]
    if access.get('clusterOperations'):
        rules += [{'resource': {'type': 'cluster'}, 'operations': access['clusterOperations'], 'host': '*'}]
    return rules


def topic_spec(topic, image_partitions=1):
    policy = VALUES['topicPolicies'][topic['kind']]
    return {'topicName': topic['name'], 'partitions': image_partitions if topic['kind']=='image-request' else topic['partitions'], 'replicas': 3,
            'config': {'min.insync.replicas': 2, 'cleanup.policy': 'delete',
                       'retention.ms': policy['retentionMs'], 'retention.bytes': VALUES['retentionBytes'],
                       'segment.bytes': VALUES['segmentBytes'], 'segment.ms': VALUES['segmentMs'],
                       'max.message.bytes': policy['maxMessageBytes'], 'message.timestamp.type': policy['timestampType']}}


def expected_access():
    requests = [f'prod.yeodam.{f}.request' for f in ('trip-create', 'trip-update', 'story-create')]
    results = [t.removesuffix('.request') + '.result' for t in requests]
    lanes = ['initial-light', 'initial-heic', 'additional-light', 'additional-heic']
    images = ['prod.yeodam.image.' + lane for lane in lanes]
    def access(read=(), write=(), describe=(), groups=()):
        return {'readTopics': list(read), 'writeTopics': list(write), 'describeTopics': list(describe), 'groups': list(groups)}
    users = {r: access(describe=requests + images) for r in ('auth-api', 'trip-api', 'photo-api')}
    users['photo-converter'] = access(read=images, write=['prod.yeodam.image.conversion-dlt'],
        groups=['prod.yeodam.image-conversion-' + lane for lane in lanes + ['receipts']])
    users['trip-result-worker'] = access(read=requests + results, write=[t + '-dlt' for t in results],
        groups=['prod.yeodam-trip-results', 'yeodam-trip-update-results', 'prod.yeodam-story-results', 'prod.yeodam-outbox-receipts'])
    users['ai'] = access(read=requests, write=results + [t + '-dlt' for t in requests],
        groups=['yeodam-ai-' + f for f in ('trip-create', 'trip-update', 'story-create')])
    internal = ['prod.connect-configs', 'prod.connect-offsets', 'prod.connect-status']
    users['connect-outbox'] = access(read=internal, write=requests + images + internal, groups=['connect-v3', 'v3.yeodam.outbox-schemahistory'])
    users['connect-outbox']['extraTopicOperations'] = {t: ['Create', 'DescribeConfigs'] for t in internal}
    users['connect-outbox']['readTopics'] += ['prod.yeodam.schema-history']
    users['connect-outbox']['writeTopics'] += ['prod.yeodam.schema-history', '__debezium-heartbeat.v3.yeodam.outbox']
    users['connect-outbox']['extraTopicOperations']['prod.yeodam.schema-history'] = ['DescribeConfigs']
    users['connect-outbox']['clusterOperations'] = ['Describe']
    return users


def validate(documents, enabled=False, verification=False, image_partitions=1):
    require(image_partitions in (1,2), "Only one-to-two image partition expansion")
    require({k: CONTRACT[k] for k in ('backendEnvironment', 'backendSpringGroups')} == json.loads((ROOT / 'deploy/v3/apps/files/business-contract.json').read_text()),
            'App and Kafka copies of the shared contract differ')
    if not enabled:
        require(not documents and not verification, 'Default disabled profile must create zero objects')
        return
    topics = {t['name']: t for t in CONTRACT['topics']}
    # Independent inventory guard: editing the catalog must not silently add/remove business flows.
    ai = {f'prod.yeodam.{flow}.{direction}' for flow in ('trip-create', 'trip-update', 'story-create')
          for direction in ('request', 'result')}
    image = {f'prod.yeodam.image.{lane}' for lane in ('initial-light', 'initial-heic', 'additional-light', 'additional-heic')}
    expected = ai | {name + '-dlt' for name in ai} | image | {'prod.yeodam.image.conversion-dlt'}
    require(set(topics) == expected and len(CONTRACT['topics']) == 17, 'Require exactly the BE handoff 17 topics')
    require(set(CONTRACT['users']) == {'auth-api', 'trip-api', 'photo-api', 'photo-converter', 'trip-result-worker', 'ai', 'connect-outbox'}, 'Unexpected business identity inventory')
    require(CONTRACT['users'] == expected_access(), 'Catalog changed role ownership or literal consumer groups')
    require({k: VALUES[k] for k in ('replicas', 'minInSyncReplicas', 'retentionBytes', 'segmentBytes')} ==
            {'replicas': 3, 'minInSyncReplicas': 2, 'retentionBytes': 67108864, 'segmentBytes': 16777216}, 'Preserve the initial storage/replication budget')
    for t in CONTRACT['topics']:
        kind = 'image-dlt' if t['name'] == 'prod.yeodam.image.conversion-dlt' else 'image-request' if t['name'] in image else 'ai-dlt' if t['name'].endswith('-dlt') else 'ai-request' if t['name'].endswith('.request') else 'ai-result'
        require(t['kind'] == kind and t['partitions'] == (2 if kind == 'image-dlt' else 1), 'Preserve one-consumer initial partition layout and DLT routing')
    counts = {'KafkaTopic': 17 + (2 if verification else 0), 'KafkaUser': 7 + (3 if verification else 0)}
    if verification:
        counts['Job'] = 1
    require(Counter(d['kind'] for d in documents) == counts, 'Unexpected resource inventory; no Broker/Node/Storage/app mutation allowed')
    identities = [(d['kind'], d['metadata']['name']) for d in documents]
    require(len(identities) == len(set(identities)), 'Duplicate resource identity')
    for d in documents:
        m = d['metadata']
        require(m.get('namespace') == 'kafka', 'Only the existing kafka namespace is allowed')
        require(m.get('labels', {}).get('yeodam.io/stage') == 'v3', 'Missing V3 ownership label')
        if d['kind'] in ('KafkaTopic', 'KafkaUser'):
            require(m['labels'].get('strimzi.io/cluster') == 'yeodam-v3-kafka', 'Never target the existing V2 cluster')
        is_verification = m['name'].startswith('v3-access-')
        if is_verification:
            require(verification, 'Verification objects must be explicitly enabled')
            continue
        require(m.get('annotations') == {'helm.sh/resource-policy': 'keep'}, 'Business data and identities require explicit deletion review')
        if d['kind'] == 'KafkaTopic':
            require(m['name'] in topics and d['spec'] == topic_spec(topics[m['name']], image_partitions), 'Topic settings differ from bounded RF3/ISR2 policy')
        elif d['kind'] == 'KafkaUser':
            role = m['name'].removeprefix('v3-')
            require(role in CONTRACT['users'], 'Unknown Kafka identity')
            require(d['spec'] == {'authentication': {'type': 'scram-sha-512'},
                    'authorization': {'type': 'simple', 'acls': acl_rules(CONTRACT['users'][role])}},
                    'Role ACL differs: no wildcard topic/group, unreviewed Create/Delete/Alter or superuser rights')
    for t in CONTRACT['topics']:
        if t['name'] in ai:
            require(topics[t['name'] + '-dlt']['partitions'] >= t['partitions'], 'DLT must accept the source partition used by BE recoverers')
    backend = yaml.safe_load((ROOT / 'deploy/v3/environments/foundation/backend.values.yaml').read_text())
    require(all(backend['defaults']['configMapData'].get(k) == v for k, v in CONTRACT['backendEnvironment'].items()),
            'BE values differ from Kafka Topic/Group names')
    require(CONTRACT['aiEnvironment'] == {k: v for k, v in CONTRACT['backendEnvironment'].items()
            if k.startswith('KAFKA_') and k.endswith('_TOPIC')}, 'AI and BE topic names differ')
    if verification:
        validate_verification([d for d in documents if d['metadata']['name'].startswith('v3-access-')])


def validate_verification(documents):
    # Defined below alongside the opt-in Job. No business account/Topic is used by it.
    require(Counter(d['kind'] for d in documents) == {'KafkaTopic': 2, 'KafkaUser': 3, 'Job': 1}, 'Unexpected verification inventory')
    def rule(kind, name, ops):
        return {'resource': {'type': kind, 'name': name, 'patternType': 'literal'}, 'operations': ops, 'host': '*'}
    rules = {
        'v3-access-writer': [rule('topic', 'v3-access-check', ['Write', 'Describe', 'DescribeConfigs'])],
        'v3-access-reader': [rule('topic', 'v3-access-check', ['Read', 'Describe']), rule('group', 'v3-access-check-reader', ['Read', 'Delete'])],
        'v3-access-denied': [rule('topic', 'v3-access-check', ['Describe']), rule('group', 'v3-access-check-denied', ['Read', 'Delete'])]}
    for d in documents:
        require(not d['metadata'].get('annotations'), 'Verification objects must be removable')
        if d['kind'] == 'KafkaTopic':
            require(d['spec'] == {'partitions': 1, 'replicas': 3, 'config': {'min.insync.replicas': 2, 'retention.ms': 3600000, 'retention.bytes': 10485760, 'segment.bytes': 1048576}}, 'Bound the temporary test data')
        elif d['kind'] == 'KafkaUser':
            require(d['spec'] == {'authentication': {'type': 'scram-sha-512'}, 'authorization': {'type': 'simple', 'acls': rules[d['metadata']['name']]}}, 'Test users cannot access business Topics or Groups')
    job = next(d for d in documents if d['kind'] == 'Job')
    require(set(job['spec']) == {'backoffLimit', 'activeDeadlineSeconds', 'template'}, 'No parallel test expansion or additional Job settings')
    pod = job['spec']['template']['spec']
    require(set(pod) == {'restartPolicy', 'automountServiceAccountToken', 'enableServiceLinks', 'nodeSelector', 'securityContext', 'containers', 'volumes'} and len(pod['containers']) == 1, 'No sidecars, host mounts or extra scheduling overrides')
    c = pod['containers'][0]
    require(set(c) == {'name', 'image', 'securityContext', 'resources', 'env', 'command', 'args', 'volumeMounts'}, 'No additional Secret or execution injection')
    require(job['spec']['template']['metadata']['labels'].get('app.kubernetes.io/instance') == 'kafka-smoke', 'Use the existing restricted smoke network peer')
    require(job['spec']['backoffLimit'] == 0 and job['spec']['activeDeadlineSeconds'] == 480, 'Bound test retries/duration')
    require(pod['nodeSelector'] == {'yeodam.io/role': 'system'} and not pod.get('tolerations'), 'Use existing system nodes only')
    require(pod['automountServiceAccountToken'] is False and not pod.get('serviceAccountName'), 'No test API/Secret-read permissions')
    require(pod['restartPolicy'] == 'Never' and pod['enableServiceLinks'] is False and not pod.get('hostNetwork'), 'Bound verification networking and restarts')
    require(c['command'] == ['/bin/bash', '-ec'] and c['env'] == [{'name': 'KAFKA_HEAP_OPTS', 'value': '-Xms32m -Xmx128m'}], 'Do not override test execution or heap')
    require(c['image'] == kafka.LOCK['kafkaImage'], 'Pin the existing Kafka tool image')
    require(c['resources'] == {'requests': {'cpu': '50m', 'memory': '256Mi'}, 'limits': {'memory': '384Mi'}}, 'Keep verification budget small')
    require(c['securityContext'] == {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}, 'Test container must be restricted')
    require(pod['securityContext'] == {'runAsNonRoot': True, 'seccompProfile': {'type': 'RuntimeDefault'}}, 'Nonroot test execution required')
    require(c['args'] == [(CHART / 'files/verify.sh').read_text()], 'Run the reviewed test script only')
    require(c['volumeMounts'] == [{'name': 'temp', 'mountPath': '/tmp'}] + [
        {'name': r, 'mountPath': '/credentials/' + r, 'readOnly': True} for r in ('writer', 'reader', 'denied')] +
        [{'name': 'ca', 'mountPath': '/ca', 'readOnly': True}], 'Only exact temporary credential mounts allowed')
    require(pod['volumes'] == [{'name': 'temp', 'emptyDir': {'sizeLimit': '16Mi'}}] + [
        {'name': r, 'secret': {'secretName': 'v3-access-' + r, 'items': [{'key': 'password', 'path': 'password'}]}} for r in ('writer', 'reader', 'denied')] +
        [{'name': 'ca', 'secret': {'secretName': 'yeodam-v3-kafka-cluster-ca-cert', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}], 'No host storage or additional credential keys')
    allowed = {'v3-access-writer', 'v3-access-reader', 'v3-access-denied'}
    require({v['secret']['secretName'] for v in pod['volumes'] if 'secret' in v} == allowed | {'yeodam-v3-kafka-cluster-ca-cert'}, 'Mount only disposable credentials and public CA')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    # Reuse schemas from the checksum-pinned installed Operator version.
    require(hashlib.sha256((args.directory / 'strimzi.tgz').read_bytes()).hexdigest() == kafka.LOCK['sha256'], 'Pinned Strimzi archive checksum mismatch')
    operator = kafka.platform.load_documents(kafka.render(args.directory / 'strimzi.tgz', kafka.OPERATOR / 'values.yaml', True))
    kafka.write_custom_schemas(operator)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, files, enabled, verify in [('disabled', [], False, False), ('business', [FOUNDATION], True, False),
            ('verification', [FOUNDATION, ROOT / 'tests/ci/v3/kafka-access-check.values.yaml'], True, True)]:
        source = render(files);validate(kafka.platform.load_documents(source), enabled, verify)
        kafka.platform.schema_check(source);(args.output / f'kafka-access-{name}.yaml').write_text(source)
    print('Kafka access contracts passed: default0, business17Topics/7Users; opt-in test only. No cluster or Secret access.')


if __name__ == '__main__':
    main()
