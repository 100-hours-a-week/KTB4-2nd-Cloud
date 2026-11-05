#!/usr/bin/env python3
"""Offline MongoDB resource, placement, privilege and lifecycle contracts."""
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
CHART = ROOT / 'deploy/v3/mongodb'
FOUNDATION = ROOT / 'deploy/v3/environments/foundation/mongodb.values.yaml'
IMAGE = yaml.safe_load((CHART / 'values.yaml').read_text())['image']
LABELS = {'app.kubernetes.io/name': 'mongodb', 'app.kubernetes.io/instance': 'yeodam-v3-mongodb'}


def render(*values):
    command = [os.environ['V3_HELM'], 'template', 'yeodam-v3-mongodb', str(CHART), '--namespace', 'default', '--kube-version', '1.36.0']
    for value in values:
        command += ['-f', str(value)]
    return subprocess.check_output(command, text=True)


def validate(documents, enabled=False, smoke=False):
    expected = {'Namespace': 1, 'NetworkPolicy': 3}
    if enabled:
        expected.update({'ServiceAccount': 1, 'ConfigMap': 1, 'Service': 2, 'StatefulSet': 1})
    if smoke:
        expected['Job'] = 1
    require(Counter(d['kind'] for d in documents) == expected, 'Unexpected inventory or embedded Secret')
    identities = [(d['kind'], d['metadata'].get('namespace', ''), d['metadata']['name']) for d in documents]
    require(len(identities) == len(set(identities)), 'Duplicate resource')
    require(all(ns == ('' if k == 'Namespace' else 'mongodb') for k, ns, _ in identities), 'MongoDB namespace boundary')
    ns = next(d for d in documents if d['kind'] == 'Namespace')['metadata']
    require(ns['name'] == 'mongodb' and ns['annotations']['helm.sh/resource-policy'] == 'keep'
            and ns['labels']['pod-security.kubernetes.io/enforce'] == 'restricted', 'Retain restricted namespace')
    policies = {d['metadata']['name']: d['spec'] for d in documents if d['kind'] == 'NetworkPolicy'}
    require(policies['default-deny'] == {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}, 'Default deny widened')
    require(policies['allow-dns'] == {'podSelector': {'matchLabels': LABELS}, 'policyTypes': ['Egress'], 'egress': [
        {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}, 'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
         'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}, 'DNS boundary widened')
    peers = {'podSelector': {'matchLabels': LABELS}, 'policyTypes': ['Ingress', 'Egress']}
    for direction, key in [('ingress', 'from'), ('egress', 'to')]:
        peers[direction] = [{key: [{'podSelector': {'matchLabels': LABELS}}], 'ports': [{'protocol': 'TCP', 'port': 27017}]}]
    require(policies['allow-mongodb-peers'] == peers, 'Only same-namespace database clients initially allowed')
    if not enabled:
        return
    state = next(d for d in documents if d['kind'] == 'StatefulSet')['spec']
    require(state['replicas'] == 1 and state['updateStrategy'] == {'type': 'OnDelete'}, 'One standalone, explicit replacement only')
    require(state['persistentVolumeClaimRetentionPolicy'] == {'whenDeleted': 'Retain', 'whenScaled': 'Retain'}, 'Never delete data with workload')
    claims = state['volumeClaimTemplates']
    require(len(claims) == 1 and claims[0]['spec'] == {'accessModes': ['ReadWriteOncePod'], 'storageClassName': 'yeodam-v3-gp3-retain',
            'resources': {'requests': {'storage': '5Gi'}}}, 'Exactly one retained5Gi claim')
    pod = state['template']['spec']
    container = pod['containers'][0]
    require(container['resources'] == {'requests': {'cpu': '100m', 'memory': '512Mi'}, 'limits': {'cpu': '500m', 'memory': '1Gi'}}, 'Initial resource budget changed')
    require(container['command'] == ['/bin/bash', '/runtime/entrypoint.sh'], 'Guarded initialization required')
    require(pod['terminationGracePeriodSeconds'] == 120, 'Graceful storage shutdown required')
    for probe in ['startupProbe', 'readinessProbe', 'livenessProbe']:
        require(container[probe]['exec']['command'] == ['/bin/bash', '/runtime/shell.sh', '/runtime/health.js']
                and container[probe]['timeoutSeconds'] == 10, 'Authenticated local probe contract')
    require(container['startupProbe']['periodSeconds'] * container['startupProbe']['failureThreshold'] == 600, 'Bounded 10min initial startup')
    require(any(m['name'] == 'data' and m['mountPath'] == '/data/db' for m in container['volumeMounts']), 'Use image data volume, not an anonymous nested path')
    config = next(d for d in documents if d['kind'] == 'ConfigMap')['data']
    require(config == {p.name: p.read_text() for p in (CHART / 'files').iterdir()}, 'Runtime source mismatch')
    options = yaml.safe_load(config['mongod.yaml'])
    require(options['storage'] == {'dbPath': '/data/db/files', 'wiredTiger': {'engineConfig': {'cacheSizeGB': 0.25}}}, 'Bounded WiredTiger cache and isolated data directory')
    require(options['security'] == {'authorization': 'enabled'} and options['net']['tls']['mode'] == 'requireTLS'
            and not options.get('replication'), 'Authenticated TLS standalone only')
    require('no reset' in config['entrypoint.sh'] and '--bind_ip 127.0.0.1 --noauth' in config['entrypoint.sh'], 'First accounts cannot be exposed or data reset automatically')
    require(yaml.safe_load(config['mongosh.conf']) == {'mongosh': {'forceDisableTelemetry': True, 'logLocation': '/tmp/mongosh-logs', 'logMaxFileCount': 8, 'logRetentionGB': 0.001, 'updateURL': ''}}, 'Bound probe logs outside database files')
    require('authenticatedUsers' in config['health.js'], 'Ping alone is not proof of authenticated access')
    for service in [d['spec'] for d in documents if d['kind'] == 'Service']:
        require(service.get('type', 'ClusterIP') == 'ClusterIP' and not service.get('externalIPs') and
                service['ports'] == [{'name': 'mongodb', 'port': 27017, 'targetPort': 'mongodb'}] and
                service['selector'] == dict(LABELS, **{'yeodam.io/mongodb-process': 'server'}), 'Private single member only')
    pods = [pod] + [d['spec']['template']['spec'] for d in documents if d['kind'] == 'Job']
    for p in pods:
        require(p['nodeSelector'] == {'yeodam.io/role': 'data', 'yeodam.io/memory-tier': '4gi'}
                and p['tolerations'] == [{'key': 'yeodam.io/role', 'operator': 'Equal', 'value': 'data', 'effect': 'NoSchedule'}], 'Existing4Gi data node only')
        require(p['serviceAccountName'] == 'mongodb' and p['automountServiceAccountToken'] is False, 'No API token')
        security = p['securityContext']
        require(security['runAsNonRoot'] is True and security['runAsUser'] == security['runAsGroup'] == security['fsGroup'] == 999
                and security['seccompProfile'] == {'type': 'RuntimeDefault'}, 'Restricted UID999')
        require(len(p['containers']) == 1, 'No sidecars')
        c = p['containers'][0]
        require(c['image'] == IMAGE and c['securityContext'] == {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                'capabilities': {'drop': ['ALL']}}, 'Pinned restricted image')
        require(any(m['mountPath'] == '/etc/mongosh.conf' and m.get('subPath') == 'mongosh.conf' for m in c['volumeMounts']), 'Read-only global shell log config')
        env = {e['name']: e.get('value') for e in c['env']}
        require(env['NODE_OPTIONS'] == '--max-old-space-size=128', 'Bound probe/client JS heap')
        volumes = {v['name']: v for v in p['volumes']}
        require(volumes['credentials']['secret'] == {'secretName': 'mongodb-bootstrap', 'defaultMode': 0o440}, 'Reference-only Secret')
        require(volumes['tmp']['emptyDir'] == {'medium': 'Memory', 'sizeLimit': '16Mi'}
                and not any('hostPath' in v for v in p['volumes']), 'Bounded private tmp, no host access')
    for job in [d for d in documents if d['kind'] == 'Job']:
        jp = job['spec']['template']['spec']
        require(any(v.get('name') == 'client-home' and v.get('emptyDir') == {'medium': 'Memory', 'sizeLimit': '16Mi'} for v in jp['volumes']) and any(m['mountPath'] == '/data/db' and m['name'] == 'client-home' for m in jp['containers'][0]['volumeMounts']), 'Ephemeral client home, no anonymous/writable root reliance')
        require(job['spec']['backoffLimit'] == 0 and job['spec']['activeDeadlineSeconds'] == 600, 'Bound explicit smoke retries')
        require(job['spec']['template']['spec']['containers'][0]['resources']['limits'] == {'cpu': '500m', 'memory': '256Mi'}, 'Bound smoke process')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, files, enabled, smoke in [('disabled', [], False, False), ('foundation', [FOUNDATION], True, False),
        ('smoke', [FOUNDATION, ROOT / 'tests/ci/v3/mongodb-smoke.values.yaml'], True, True)]:
        source = render(*files)
        validate(platform.load_documents(source), enabled, smoke)
        platform.schema_check(source)
        (args.output / f'mongodb-{name}.yaml').write_text(source)
    subprocess.run(['bash', '-n', str(CHART / 'files/entrypoint.sh')], check=True)
    print(json.dumps({'mongodbPods': 1, 'memoryLimit': '1Gi', 'cacheMi': 256, 'retainedDataGiB': 5, 'additionalNodes': 0, 'clusterAccessed': False}))


if __name__ == '__main__':
    main()
