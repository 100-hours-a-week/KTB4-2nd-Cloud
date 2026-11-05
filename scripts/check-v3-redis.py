#!/usr/bin/env python3
"""Offline contracts for minimal App Redis and three-AZ Sentinel."""
import argparse
from collections import Counter
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
CHART = ROOT / 'deploy/v3/redis'
FOUNDATION = ROOT / 'deploy/v3/environments/foundation/redis.values.yaml'
IMAGE = yaml.safe_load((CHART / 'values.yaml').read_text())['image']
LABELS = {'app.kubernetes.io/name': 'redis', 'app.kubernetes.io/instance': 'yeodam-v3-redis'}


def render(*values):
    cmd = [os.environ['V3_HELM'], 'template', 'yeodam-v3-redis', str(CHART), '--namespace', 'default', '--kube-version', '1.36.0']
    for value in values:
        cmd += ['-f', str(value)]
    return subprocess.check_output(cmd, text=True)


def validate(documents, enabled=False, smoke=False, bootstrap=False):
    expected = {'Namespace': 1, 'NetworkPolicy': 3}
    if enabled:
        expected.update({'ServiceAccount': 1, 'ConfigMap': 1, 'Service': 2, 'PodDisruptionBudget': 2, 'StatefulSet': 2})
    if smoke:
        expected['Job'] = 1
    require(Counter(d['kind'] for d in documents) == expected, 'Unexpected Redis inventory, public resource or Secret payload')
    identities = [(d['kind'], d['metadata'].get('namespace', ''), d['metadata']['name']) for d in documents]
    require(len(set(identities)) == len(identities), 'Duplicate Redis resources')
    require(all(ns == ('' if kind == 'Namespace' else 'redis') for kind, ns, _ in identities), 'Only Redis namespace may be used')
    namespace = next(d for d in documents if d['kind'] == 'Namespace')['metadata']
    require(namespace['name'] == 'redis' and namespace['annotations']['helm.sh/resource-policy'] == 'keep'
            and namespace['labels']['pod-security.kubernetes.io/enforce'] == 'restricted', 'Retain restricted namespace')
    policies = {d['metadata']['name']: d['spec'] for d in documents if d['kind'] == 'NetworkPolicy'}
    require(policies['default-deny'] == {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}, 'Default deny widened')
    require(policies['allow-dns'] == {'podSelector': {'matchLabels': LABELS}, 'policyTypes': ['Egress'], 'egress': [
        {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                 'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
         'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}, 'DNS widened')
    peer = {'podSelector': {'matchLabels': LABELS}, 'policyTypes': ['Ingress', 'Egress']}
    for direction, key in [('ingress', 'from'), ('egress', 'to')]:
        peer[direction] = [{key: [{'podSelector': {'matchLabels': LABELS}}],
                            'ports': [{'protocol': 'TCP', 'port': 6379}, {'protocol': 'TCP', 'port': 26379}]}]
    require(policies['allow-redis-peers'] == peer, 'Redis traffic must stay inside its namespace')
    if not enabled:
        return
    configs = next(d for d in documents if d['kind'] == 'ConfigMap')['data']
    require(configs == {p.name: p.read_text() for p in (CHART / 'files').iterdir()}, 'Runtime files changed in render')
    for line in ['port 0', 'tls-port 6379', 'tls-replication yes', 'appendonly yes', 'appendfsync everysec',
                 'maxmemory 64mb', 'maxmemory-policy noeviction', 'maxmemory-clients 32mb', 'replica-read-only yes']:
        require(line in configs['redis-base.conf'].splitlines(), 'Persistence/auth/resource setting changed: ' + line)
    require('selected=$(wait_primary)' in configs['start-redis.sh'] and
            'empty selected primary requires data recovery' in configs['start-redis.sh'], 'Redis restart must not blindly restore ordinal primary')
    services = {d['metadata']['name']: d['spec'] for d in documents if d['kind'] == 'Service'}
    require(set(services) == {'redis-members', 'redis-sentinel'}, 'No random two-member writer Service')
    for name, svc in services.items():
        kind = 'redis' if name == 'redis-members' else 'sentinel'
        require(svc.get('type', 'ClusterIP') == 'ClusterIP' and svc['clusterIP'] == 'None'
                and svc['publishNotReadyAddresses'] is True and not svc.get('externalIPs')
                and svc['selector'] == dict(LABELS, **{'yeodam.io/redis-process': kind})
                and svc['ports'] == [{'name': 'tls', 'port': 6379 if kind == 'redis' else 26379, 'targetPort': 'tls'}], 'Only stable internal TLS discovery endpoints')
    pdb = {d['metadata']['name']: d['spec'] for d in documents if d['kind'] == 'PodDisruptionBudget'}
    require(pdb['redis']['minAvailable'] == 1 and pdb['sentinel']['minAvailable'] == 2, 'Preserve member/quorum disruption budgets')
    states = {d['metadata']['name']: d['spec'] for d in documents if d['kind'] == 'StatefulSet'}
    for kind, state in states.items():
        data = kind == 'redis'
        require(state['replicas'] == (2 if data else 3) and state['podManagementPolicy'] == 'Parallel', 'Reviewed member counts/startup order changed')
        require(state['updateStrategy'] == {'type': 'OnDelete'} and state['persistentVolumeClaimRetentionPolicy'] ==
                {'whenDeleted': 'Retain', 'whenScaled': 'Retain'}, 'No automatic rolling replacement/data deletion')
        claim = state['volumeClaimTemplates']
        require(len(claim) == 1 and claim[0]['spec'] == {'accessModes': ['ReadWriteOncePod'],
                'storageClassName': 'yeodam-v3-gp3-retain', 'resources': {'requests': {'storage': '1Gi'}}}, 'Exactly five retained 1Gi claims')
        pod = state['template']['spec']
        anti = pod['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
        require(anti == [{'labelSelector': {'matchLabels': dict(LABELS, **{'yeodam.io/redis-process': kind})},
                         'topologyKey': 'topology.kubernetes.io/zone'}], 'Required per-role AZ separation')
        require(pod['dnsConfig']['options'] == [{'name': 'ndots', 'value': '1'}, {'name': 'timeout', 'value': '1'}, {'name': 'attempts', 'value': '1'}], 'Bounded DNS lookup for Sentinel/Redis discovery')
        role = 'data' if data else 'kafka'
        require(pod['tolerations'] == [{'key': 'yeodam.io/role', 'operator': 'Equal', 'value': role, 'effect': 'NoSchedule'}], 'No arbitrary node access')
        if data:
            require(pod['nodeSelector'] == {'yeodam.io/role': 'data'}, 'Redis must use existing data nodes')
        else:
            require(pod['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms'] == [
                {'matchExpressions': [{'key': 'yeodam.io/role', 'operator': 'In', 'values': ['system']},
                                      {'key': 'topology.kubernetes.io/zone', 'operator': 'In', 'values': ['ap-northeast-2a', 'ap-northeast-2c']}]},
                {'matchExpressions': [{'key': 'yeodam.io/role', 'operator': 'In', 'values': ['kafka']},
                                      {'key': 'topology.kubernetes.io/zone', 'operator': 'In', 'values': ['ap-northeast-2b']}]}], 'Sentinel uses existing system a/c and Kafka b only')
        container = pod['containers'][0]
        require(container['resources'] == {'requests': {'cpu': '25m' if data else '5m', 'memory': '128Mi' if data else '32Mi'},
                'limits': {'cpu': '250m' if data else '100m', 'memory': '256Mi' if data else '64Mi'}}, 'Minimal budget changed')
        env = {e['name']: e for e in container['env']}
        require(env['INITIAL_BOOTSTRAP']['value'] == str(bootstrap).lower() and env['PROCESS_KIND']['value'] == kind, 'Explicit bootstrap lifecycle required')
        for probe, mode in [('startupProbe', 'startup'), ('readinessProbe', 'readiness'), ('livenessProbe', 'liveness')]:
            require(container[probe]['exec']['command'] == ['/bin/bash', '/runtime/health.sh', mode], 'Probe boundary changed')
    pods = [s['template']['spec'] for s in states.values()] + [d['spec']['template']['spec'] for d in documents if d['kind'] == 'Job']
    for pod in pods:
        require(pod['serviceAccountName'] == 'redis' and pod['automountServiceAccountToken'] is False, 'No API token')
        security = pod['securityContext']
        require(security['runAsNonRoot'] is True and security['runAsUser'] == security['runAsGroup'] == security['fsGroup'] == 999
                and security['seccompProfile'] == {'type': 'RuntimeDefault'}, 'Restricted UID999 required')
        require(len(pod['containers']) == 1, 'No unreviewed sidecars')
        container = pod['containers'][0]
        require(container['image'] == IMAGE and container['securityContext'] == {'allowPrivilegeEscalation': False,
                'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}, 'Pinned image and restricted container required')
        volumes = {v['name']: v for v in pod['volumes']}
        require(volumes['credentials']['secret'] == {'secretName': 'redis-bootstrap', 'defaultMode': 0o440}, 'Reference-only V3 Secret required')
        require(volumes['tmp']['emptyDir'] == {'medium': 'Memory', 'sizeLimit': '8Mi'} and not any('hostPath' in v for v in pod['volumes']), 'Bounded private scratch, no host access')
    for job in [d for d in documents if d['kind'] == 'Job']:
        require(job['spec']['backoffLimit'] == 0 and job['spec']['activeDeadlineSeconds'] == 300, 'Bounded explicit smoke only')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, files, enabled, smoke, bootstrap in [
        ('disabled', [], False, False, False), ('foundation', [FOUNDATION], True, False, False),
        ('initial-smoke', [FOUNDATION, ROOT / 'tests/ci/v3/redis-bootstrap.values.yaml'], True, True, True)]:
        source = render(*files)
        validate(platform.load_documents(source), enabled, smoke, bootstrap)
        platform.schema_check(source)
        (args.output / f'redis-{name}.yaml').write_text(source)
    for script in (CHART / 'files').glob('*.sh'):
        subprocess.run(['bash', '-n', str(script)], check=True)
    print(json.dumps({'redisPods': 2, 'sentinelPods': 3, 'memoryLimitsMi': 704, 'retainedDataGiB': 5,
                      'additionalNodes': 0, 'clusterAccessed': False, 'realSecretsRead': False}))


if __name__ == '__main__':
    main()
