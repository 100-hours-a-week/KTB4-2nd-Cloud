#!/usr/bin/env python3
"""Render and verify V3 MySQL without AWS credentials or a cluster."""
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
CHART = ROOT / 'deploy/v3/mysql'
FOUNDATION = ROOT / 'deploy/v3/environments/foundation/mysql.values.yaml'
IMAGE = yaml.safe_load((CHART / 'values.yaml').read_text())['image']
LABELS = {'app.kubernetes.io/name': 'mysql', 'app.kubernetes.io/instance': 'yeodam-v3-mysql'}


def render(*values):
    command = [os.environ['V3_HELM'], 'template', 'yeodam-v3-mysql', str(CHART),
               '--namespace', 'default', '--kube-version', '1.36.0']
    for value in values:
        command += ['-f', str(value)]
    return subprocess.check_output(command, text=True)


def validate(documents, enabled=False, jobs=False):
    inventory = Counter(d['kind'] for d in documents)
    expected = {'Namespace': 1, 'NetworkPolicy': 3}
    if enabled:
        expected.update({'ServiceAccount': 1, 'ConfigMap': 1, 'Service': 2,
                         'StatefulSet': 1, 'PodDisruptionBudget': 1})
    if jobs:
        expected['Job'] = 2
    require(inventory == expected, 'Unexpected MySQL inventory or Secret payload')
    identities = [(d['kind'], d['metadata'].get('namespace', ''), d['metadata']['name']) for d in documents]
    require(len(set(identities)) == len(identities), 'Duplicate MySQL resource')
    require(all(ns == ('' if kind == 'Namespace' else 'mysql') for kind, ns, _ in identities), 'MySQL namespace boundary')
    ns = next(d for d in documents if d['kind'] == 'Namespace')
    require(ns['metadata']['name'] == 'mysql' and ns['metadata']['annotations']['helm.sh/resource-policy'] == 'keep', 'Retain MySQL namespace')
    require(ns['metadata']['labels']['pod-security.kubernetes.io/enforce'] == 'restricted', 'Restricted Pod admission required')
    policies = {d['metadata']['name']: d['spec'] for d in documents if d['kind'] == 'NetworkPolicy'}
    require(policies['default-deny'] == {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}, 'Default deny widened')
    require(policies['allow-dns'] == {'podSelector': {'matchLabels': LABELS}, 'policyTypes': ['Egress'], 'egress': [
        {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                 'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
         'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}, 'DNS policy widened')
    peer = {'podSelector': {'matchLabels': LABELS}, 'policyTypes': ['Ingress', 'Egress'],
            'ingress': [{'from': [{'podSelector': {'matchLabels': LABELS}}], 'ports': [{'protocol': 'TCP', 'port': 3306}]}],
            'egress': [{'to': [{'podSelector': {'matchLabels': LABELS}}], 'ports': [{'protocol': 'TCP', 'port': 3306}]}]}
    require(policies['allow-mysql-peers'] == peer, 'Only same-namespace MySQL/verification TCP allowed')
    if not enabled:
        return
    state = next(d for d in documents if d['kind'] == 'StatefulSet')['spec']
    require(state['replicas'] == 2 and state['podManagementPolicy'] == 'Parallel', 'Two members must start without readiness deadlock')
    require(state['updateStrategy'] == {'type': 'OnDelete'}, 'No automatic DB rolling replacement')
    require(state['persistentVolumeClaimRetentionPolicy'] == {'whenDeleted': 'Retain', 'whenScaled': 'Retain'}, 'Never delete data with workload')
    claim = state['volumeClaimTemplates'][0]['spec']
    require(len(state['volumeClaimTemplates']) == 1 and claim == {
        'accessModes': ['ReadWriteOncePod'], 'storageClassName': 'yeodam-v3-gp3-retain',
        'resources': {'requests': {'storage': '10Gi'}}}, 'Reviewed 2x10Gi retained encrypted storage only')
    service = {d['metadata']['name']: d['spec'] for d in documents if d['kind'] == 'Service'}
    primary = service['mysql-primary']
    require(primary.get('type') == 'ClusterIP' and primary['selector'].get('statefulset.kubernetes.io/pod-name') in ('mysql-0', 'mysql-1'), 'Single internal writer only')
    for svc in service.values():
        require(svc.get('type', 'ClusterIP') == 'ClusterIP' and not svc.get('externalIPs') and svc['ports'] == [{'name': 'mysql', 'port': 3306, 'targetPort': 'mysql'}], 'No public DB endpoint')
        require(svc['selector'].get('yeodam.io/mysql-process') == 'server', 'Do not select Job clients')
    require(service['mysql-members'].get('clusterIP') == 'None' and service['mysql-members'].get('publishNotReadyAddresses') is True, 'Initial read-only members need headless discovery')
    require(next(d for d in documents if d['kind'] == 'PodDisruptionBudget')['spec']['minAvailable'] == 1, 'Disruption budget missing')
    config = next(d for d in documents if d['kind'] == 'ConfigMap')['data']
    require(set(config) == {p.name for p in (CHART / 'files').iterdir()}, 'Runtime script inventory mismatch')
    require(all(config[p.name] == p.read_text() for p in (CHART / 'files').iterdir()), 'Runtime scripts changed during render')
    cnf = config['my.cnf']
    for setting in ['read-only=ON', 'super-read-only=ON', 'gtid-mode=ON', 'enforce-gtid-consistency=ON',
                    'binlog-expire-logs-seconds=604800', 'binlog-format=ROW', 'binlog-row-image=FULL', 'sync-binlog=1', 'innodb-flush-log-at-trx-commit=1',
                    'rpl-semi-sync-source-wait-point=AFTER_SYNC', 'rpl-semi-sync-source-wait-for-replica-count=1',
                    'rpl-semi-sync-source-timeout=5000', 'require-secure-transport=ON', 'innodb-buffer-pool-size=256M',
                    'max-connections=60', 'rpl-semi-sync-source-enabled=OFF']:
        require(setting in cnf.splitlines(), 'MySQL durability/TLS/budget setting missing: ' + setting)
    require('source_enabled=OFF' in config['entrypoint.sh'] and
            '[[ $POD_NAME == $PRIMARY_MEMBER ]] && source_enabled=ON' in config['entrypoint.sh'],
            'Replica must not wait for a nonexistent downstream replica')
    pod = state['template']['spec']
    require(pod['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0]['topologyKey'] == 'topology.kubernetes.io/zone', 'Separate database AZs')
    require(pod['containers'][0]['resources'] == {'requests': {'cpu': '250m', 'memory': '512Mi'}, 'limits': {'cpu': '1', 'memory': '768Mi'}}, 'Initial memory/CPU budget changed')
    c = pod['containers'][0]
    require(c['command'] == ['/bin/bash', '/runtime/entrypoint.sh'], 'Custom read-only startup required')
    env = {e['name']: e for e in c['env']}
    require(env['PRIMARY_MEMBER']['value'] == primary['selector']['statefulset.kubernetes.io/pod-name'], 'Primary probe and Service disagree')
    for probe, mode in [('startupProbe', 'startup'), ('readinessProbe', 'readiness'), ('livenessProbe', 'liveness')]:
        require(c[probe]['exec']['command'] == ['/bin/bash', '/runtime/health.sh', mode] and c[probe]['timeoutSeconds'] == 4, 'Probe boundary changed')
    require(c['startupProbe']['periodSeconds'] * c['startupProbe']['failureThreshold'] == 600, 'Allow initialization startup time')
    require(pod['terminationGracePeriodSeconds'] == 120, 'Allow graceful MySQL shutdown')
    pods = [pod] + [d['spec']['template']['spec'] for d in documents if d['kind'] == 'Job']
    for p in pods:
        require(p['nodeSelector'].get('yeodam.io/role') == 'data' and p['tolerations'] == [
            {'key': 'yeodam.io/role', 'operator': 'Equal', 'value': 'data', 'effect': 'NoSchedule'}], 'Existing data nodes only')
        require(p['automountServiceAccountToken'] is False and p['serviceAccountName'] == 'mysql', 'No Kubernetes API token')
        security = p['securityContext']
        require(security['runAsNonRoot'] is True and security['runAsUser'] == 999 and security['runAsGroup'] == 999
                and security['fsGroup'] == 999 and security['seccompProfile'] == {'type': 'RuntimeDefault'}, 'Non-root restricted UID required')
        require(len(p['containers']) == 1, 'No unreviewed sidecars')
        container = p['containers'][0]
        require(container['image'] == IMAGE, 'Pinned MySQL digest required')
        require(container['securityContext'] == {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                'capabilities': {'drop': ['ALL']}}, 'Container privileges widened')
        volumes = {v['name']: v for v in p['volumes']}
        require(volumes['credentials']['secret'] == {'secretName': 'mysql-bootstrap', 'defaultMode': 0o440}, 'Reference-only bootstrap Secret')
        require(volumes['tmp']['emptyDir'] == {'medium': 'Memory', 'sizeLimit': '16Mi'}, 'Bounded private scratch required')
        require(not any('hostPath' in v for v in p['volumes']), 'No host data paths')
    for job in [d for d in documents if d['kind'] == 'Job']:
        s = job['spec']
        require(s['backoffLimit'] == 0 and s['activeDeadlineSeconds'] == 600, 'No automatic bootstrap retries')
        jp = s['template']['spec']
        require(jp['restartPolicy'] == 'Never' and jp['nodeSelector']['yeodam.io/memory-tier'] == '4gi', 'Bounded one-time jobs on larger existing node')
        require(jp['containers'][0]['resources']['limits'] == {'cpu': '250m', 'memory': '128Mi'}, 'Client Job budget changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, files, enabled, jobs in [
        ('disabled', [], False, False), ('foundation', [FOUNDATION], True, False),
        ('bootstrap-smoke', [FOUNDATION, ROOT / 'tests/ci/v3/mysql-bootstrap.values.yaml'], True, True)]:
        source = render(*files)
        validate(platform.load_documents(source), enabled, jobs)
        platform.schema_check(source)
        (args.output / f'mysql-{name}.yaml').write_text(source)
    for script in (CHART / 'files').glob('*.sh'):
        subprocess.run(['bash', '-n', str(script)], check=True)
    print(json.dumps({'mysqlPods': 2, 'memoryLimitPerPod': '768Mi', 'dataGiB': 20,
                      'clusterAccessed': False, 'secretValuesRead': False}))


if __name__ == '__main__':
    main()
