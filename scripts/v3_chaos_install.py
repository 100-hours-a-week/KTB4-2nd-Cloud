"""Render Helm's pinned upstream chart, then narrow it to the CDC experiment.

The returned YAML is a private reviewed apply bundle, NOT a Helm release. Using
upstream `helm install` directly bypasses this reduction and is unsupported.
"""
import copy
import hashlib
import json
import os
import subprocess

import yaml
import v3_cdc_fault as f


def controller_tolerations(s):
    return ([{'key': 'yeodam.io/role', 'operator': 'Equal', 'value': 'worker', 'effect': 'NoSchedule'}]
            if s['controllerRole'] == 'worker' else [])


def values(snapshot):
    f.validate_snapshot(snapshot)
    f.validate_install_capacity(snapshot)
    v = yaml.safe_load((f.CHART / 'values.yaml').read_text())
    v['controllerManager']['nodeSelector'] = {'yeodam.io/role': snapshot['controllerRole'], 'kubernetes.io/hostname': snapshot['controllerNode']}
    v['controllerManager']['tolerations'] = controller_tolerations(snapshot)
    v['chaosDaemon']['nodeSelector'] = {'yeodam.io/role': 'system', 'kubernetes.io/hostname': snapshot['connect']['node']}
    return v


def reduce_upstream(documents):
    result = []
    lock = json.loads((f.CHART / 'upstream.lock.json').read_text())
    for original in documents:
        d = copy.deepcopy(original)
        if not d:
            continue
        kind, name = d['kind'], d['metadata']['name']
        if 'dashboard' in name:
            continue
        if kind == 'CustomResourceDefinition' and name not in ('networkchaos.chaos-mesh.org', 'podnetworkchaos.chaos-mesh.org'):
            continue
        if kind == 'ClusterRole' and name.endswith('chaos-controller-manager-target-namespace'):
            d['rules'] = [
                {'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list', 'watch']},
                {'apiGroups': [''], 'resources': ['events'], 'verbs': ['get', 'list', 'watch', 'create', 'patch']},
                {'apiGroups': ['chaos-mesh.org'], 'resources': ['networkchaos', 'networkchaos/status', 'networkchaos/finalizers',
                                                              'podnetworkchaos', 'podnetworkchaos/status', 'podnetworkchaos/finalizers'],
                 'verbs': ['get', 'list', 'watch', 'create', 'update', 'patch', 'delete']},
            ]
        if kind == 'ClusterRole' and name.endswith('chaos-controller-manager-cluster-level'):
            d['rules'] = [
                {'apiGroups': [''], 'resources': ['nodes'], 'verbs': ['get', 'list', 'watch']},
                {'apiGroups': ['authorization.k8s.io'], 'resources': ['subjectaccessreviews'], 'verbs': ['create']},
            ]
        if kind == 'Role':
            # Preserve only discovery and read-only TLS material in chaos-mesh.
            d['rules'] = [r for r in d['rules'] if r['resources'] in (['services', 'secrets'], ['endpointslices'], ['subjectaccessreviews'])]
        if kind.endswith('WebhookConfiguration'):
            d['webhooks'] = [w for w in d['webhooks'] if w['name'] in ('mnetworkchaos.kb.io', 'vnetworkchaos.kb.io', 'vauth.kb.io')]
            for w in d['webhooks']:
                w['namespaceSelector'] = {'matchLabels': {'kubernetes.io/metadata.name': 'kafka'}}
                for rule in w['rules']:
                    rule['resources'] = ['networkchaos']
            if not d['webhooks']:
                continue
        if kind in ('Deployment', 'DaemonSet'):
            key = 'chaos-mesh' if kind == 'Deployment' else 'chaos-daemon'
            c = d['spec']['template']['spec']['containers'][0]
            f.require(c['image'] == 'ghcr.io/chaos-mesh/' + key + ':v' + lock['version'], 'Unexpected upstream image')
            c['image'] = lock['images'][key]
        result.append(d)
    return result


def validate(docs, s):
    images = json.loads((f.CHART / 'upstream.lock.json').read_text())['images']
    identities = [(d['kind'], d['metadata'].get('namespace', ''), d['metadata']['name']) for d in docs]
    f.require(len(set(identities)) == len(identities), 'Duplicate resource identity')
    f.require(sum(d['kind'] == 'Deployment' for d in docs) == 1 and
              sum(d['kind'] == 'DaemonSet' for d in docs) == 1, 'Exactly two minimal workloads required')
    f.require({d['metadata']['name'] for d in docs if d['kind'] == 'CustomResourceDefinition'} ==
              {'networkchaos.chaos-mesh.org', 'podnetworkchaos.chaos-mesh.org'}, 'Network CRDs only')
    for d in docs:
        kind = d['kind']
        f.require(kind not in ('NetworkChaos', 'PersistentVolumeClaim', 'Ingress', 'Job'), 'Install must not inject faults or add storage/jobs')
        if kind == 'Service':
            f.require(d['spec'].get('type', 'ClusterIP') == 'ClusterIP', 'No public services')
        if kind in ('Role', 'ClusterRole'):
            for r in d['rules']:
                f.require('*' not in r['resources'] and '*' not in r['verbs'], 'Wildcard RBAC forbidden')
                if 'secrets' in r['resources']:
                    f.require(kind == 'Role' and d['metadata']['namespace'] == 'chaos-mesh' and
                              set(r['verbs']) <= {'get', 'list', 'watch'}, 'No Kafka/runtime Secret authority')
        if kind in ('Deployment', 'DaemonSet'):
            p = d['spec']['template']['spec']
            node = s['controllerNode'] if kind == 'Deployment' else s['connect']['node']
            role = s['controllerRole'] if kind == 'Deployment' else 'system'
            tolerations = controller_tolerations(s) if kind == 'Deployment' else []
            f.require(p['nodeSelector'] == {'yeodam.io/role': role, 'kubernetes.io/hostname': node}
                      and p.get('tolerations', []) == tolerations, 'Use reviewed exact hosts and role toleration only')
            f.require(not p.get('hostNetwork') and len(p['containers']) == 1, 'Unexpected workload networking')
            c = p['containers'][0]
            key = 'chaos-mesh' if kind == 'Deployment' else 'chaos-daemon'
            f.require(c['image'] == images[key], 'Unreviewed image digest')
            memory = ('128Mi', '256Mi') if kind == 'Deployment' else ('64Mi', '128Mi')
            cpu = '100m' if kind == 'Deployment' else '50m'
            f.require(c['resources'] == {'requests': {'cpu': cpu, 'memory': memory[0]},
                                         'limits': {'cpu': '300m', 'memory': memory[1]}}, 'Minimum resource budget changed')
            if kind == 'Deployment':
                f.require(d['spec']['replicas'] == 1, 'Controller replicas must remain one')
            else:
                f.require(p.get('hostPID') is True and p['containers'][0]['securityContext'].get('privileged') is True,
                          'Reviewed privileged daemon contract changed')
        if kind.endswith('WebhookConfiguration'):
            f.require(all(w['namespaceSelector'] == {'matchLabels': {'kubernetes.io/metadata.name': 'kafka'}} and
                          all(r['resources'] == ['networkchaos'] for r in w['rules']) for w in d['webhooks']),
                      'Webhooks limited to Kafka NetworkChaos')


def render(snapshot, helm, value_file):
    lock = json.loads((f.CHART / 'upstream.lock.json').read_text())
    archive = f.CHART / ('chaos-mesh-' + lock['version'] + '.tgz')
    f.require(hashlib.sha256(archive.read_bytes()).hexdigest() == lock['sha256'], 'Upstream archive checksum mismatch')
    rendered = subprocess.check_output([helm, 'template', 'v3-chaos', str(archive), '-n', 'chaos-mesh',
                                        '-f', str(value_file), '--include-crds', '--kube-version', '1.36.0'], text=True, timeout=45)
    docs = reduce_upstream(list(yaml.safe_load_all(rendered)))
    validate(docs, snapshot)
    return docs
