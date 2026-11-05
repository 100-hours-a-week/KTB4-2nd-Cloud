#!/usr/bin/env python3
"""Validate controller-only Karpenter; never contact EKS or apply AWS resources."""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('platform_contract', ROOT / 'scripts/check-v3-platform.py')
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)
require = platform.require
CONFIG = ROOT / 'deploy/v3/karpenter'
filter_spec = importlib.util.spec_from_file_location('karpenter_filter', ROOT / 'scripts/filter-v3-karpenter.py')
post = importlib.util.module_from_spec(filter_spec)
filter_spec.loader.exec_module(post)


def render(directory):
    archive = directory / 'karpenter.tgz'
    lock = json.loads((CONFIG / 'upstream.lock.json').read_text())
    require(hashlib.sha256(archive.read_bytes()).hexdigest() == lock['sha256'], 'Unreviewed upstream chart')
    def helm(chart, release, extra):
        return subprocess.check_output([os.environ['V3_HELM'], 'template', release, str(chart),
            '--namespace', 'karpenter', '--kube-version', '1.36.0', *extra], text=True,
            env={**os.environ, 'HELM_PLUGINS': str(ROOT / 'scripts/helm-plugins'),
                 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']})
    return (helm(archive, 'karpenter', ['--include-crds', '-f', str(CONFIG / 'values.yaml'), '--post-renderer', 'yeodam-karpenter-filter']),
            helm(ROOT / 'deploy/v3/karpenter-access', 'yeodam-v3-karpenter-access', []))


def validate_operator(docs):
    require(Counter(d['kind'] for d in docs) == {
        'CustomResourceDefinition': 5, 'ServiceAccount': 1, 'PodDisruptionBudget': 1,
        'ClusterRole': 2, 'ClusterRoleBinding': 2, 'Role': 2, 'RoleBinding': 2,
        'Deployment': 1, 'Service': 1}, 'Controller-only resource inventory changed')
    ids = [(d['kind'], d['metadata'].get('namespace'), d['metadata']['name']) for d in docs]
    require(len(set(ids)) == len(ids), 'Duplicate Karpenter resource')
    deployment = next(d for d in docs if d['kind'] == 'Deployment')
    require(deployment['metadata']['namespace'] == 'karpenter' and deployment['spec']['replicas'] == 2,
            'Two controllers in the dedicated namespace')
    pod = deployment['spec']['template']['spec']
    require(pod['nodeSelector'] == {'kubernetes.io/os': 'linux', 'yeodam.io/role': 'system'}, 'Use fixed system nodes only')
    require(not pod.get('hostNetwork') and not pod.get('tolerations') and not pod.get('initContainers'), 'No host networking or taint bypass')
    require(pod['serviceAccountName'] == 'karpenter' and len(pod['containers']) == 1, 'Dedicated controller identity only')
    require(pod['terminationGracePeriodSeconds'] == 60, 'Explicit controller termination budget')
    affinity = pod['affinity']
    require(affinity['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms'] ==
            [{'matchExpressions': [{'key': 'karpenter.sh/nodepool', 'operator': 'DoesNotExist'}]}], 'Never depend on provisioned nodes')
    anti = affinity['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
    require(len(anti) == 1 and anti[0]['topologyKey'] == 'kubernetes.io/hostname' and
            anti[0]['labelSelector']['matchLabels'] == deployment['spec']['selector']['matchLabels'], 'Separate controller hosts')
    spread = pod['topologySpreadConstraints']
    require(len(spread) == 1 and spread[0]['topologyKey'] == 'topology.kubernetes.io/zone' and
            spread[0]['whenUnsatisfiable'] == 'DoNotSchedule' and spread[0]['maxSkew'] == 1, 'Spread across existing system AZs')
    c = pod['containers'][0]
    lock = json.loads((CONFIG / 'upstream.lock.json').read_text())
    require(c['image'] == lock['image'], 'Controller image digest mismatch')
    require(c['resources'] == {'requests': {'cpu': '100m', 'memory': '256Mi'},
                              'limits': {'cpu': '500m', 'memory': '512Mi'}}, 'Small initial resource budget changed')
    security = c['securityContext']
    require(security['runAsNonRoot'] and security['readOnlyRootFilesystem'] and
            not security['privileged'] and not security['allowPrivilegeEscalation'] and
            security['capabilities']['drop'] == ['ALL'] and
            pod['securityContext']['seccompProfile']['type'] == 'RuntimeDefault', 'Restricted controller required')
    for key, path in [('livenessProbe', '/healthz'), ('readinessProbe', '/readyz')]:
        require(c[key]['httpGet'] == {'path': path, 'port': 'http'}, 'Upstream health contract changed')
    env = {e['name']: e.get('value', e.get('valueFrom')) for e in c['env']}
    require(len(env) == len(c['env']) and not c.get('envFrom'), 'No duplicate or unreviewed environment sources')
    for key, value in {'AWS_REGION': 'ap-northeast-2', 'AWS_DEFAULT_REGION': 'ap-northeast-2',
                       'AWS_EC2_METADATA_DISABLED': 'true', 'CLUSTER_NAME': 'yeodam-v3-foundation',
                       'EKS_CONTROL_PLANE': 'true', 'INTERRUPTION_QUEUE': 'yeodam-v3-foundation-karpenter'}.items():
        require(env.get(key) == value, 'Cluster/queue/identity environment mismatch')
    require(all(gate.endswith('=false') for gate in env['FEATURE_GATES'].split(',')), 'Optional capacity and repair gates stay disabled')
    require('CLUSTER_ENDPOINT' not in env and 'AWS_ROLE_ARN' not in env, 'Discover endpoint; use Pod Identity instead of IRSA')
    for d in docs:
        if d['kind'] in ('ClusterRole', 'Role'):
            require(not any(k.startswith('rbac.authorization.k8s.io/aggregate-to-') for k in d['metadata'].get('labels', {})), 'Do not expand existing admin/edit/view roles')
            for rule in d['rules']:
                require('*' not in rule['verbs'] and '*' not in rule['resources'] and '*' not in rule['apiGroups'], 'No wildcard RBAC')
                require('secrets' not in rule['resources'] and 'serviceaccounts/token' not in rule['resources'], 'No Secret reads or token minting')
        if d['kind'] == 'ServiceAccount':
            require(not d['metadata'].get('annotations'), 'No static identity credentials')
        if d['kind'] == 'Service':
            require(d['spec'].get('type', 'ClusterIP') == 'ClusterIP', 'No public controller endpoint')
        if d['kind'] == 'PodDisruptionBudget':
            require(d['spec']['maxUnavailable'] == 1, 'Preserve one controller during voluntary disruption')
    require({d['metadata']['name'] for d in docs if d['kind'] == 'CustomResourceDefinition'} == {
        'nodepools.karpenter.sh', 'nodeclaims.karpenter.sh', 'nodeoverlays.karpenter.sh',
        'ec2nodeclasses.karpenter.k8s.aws', 'capacitybuffers.autoscaling.x-k8s.io'}, 'Pinned CRDs changed')


def validate_access(docs):
    require(Counter(d['kind'] for d in docs) == {'Namespace': 1, 'NetworkPolicy': 3}, 'Namespace and policy only')
    ns = next(d for d in docs if d['kind'] == 'Namespace')
    require(ns['metadata']['name'] == 'karpenter' and ns['metadata']['annotations'].get('helm.sh/resource-policy') == 'keep', 'Retain dedicated namespace')
    labels = ns['metadata']['labels']
    require(labels.get('pod-security.kubernetes.io/enforce') == 'restricted' and
            labels.get('pod-security.kubernetes.io/enforce-version') == 'v1.36', 'Restricted PSA required')
    policies = {d['metadata']['name']: d['spec'] for d in docs if d['kind'] == 'NetworkPolicy'}
    require(set(policies) == {'default-deny', 'controller-egress', 'controller-health'} and
            all(d['metadata']['namespace'] == 'karpenter' for d in docs if d['kind'] == 'NetworkPolicy'), 'Policy scope mismatch')
    require(policies['default-deny'] == {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}, 'Default deny widened')
    selector = {'matchLabels': {'app.kubernetes.io/name': 'karpenter', 'app.kubernetes.io/instance': 'karpenter'}}
    require(policies['controller-egress'] == {'podSelector': selector, 'policyTypes': ['Egress'], 'egress': [
        {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                 'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
         'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]},
        {'to': [{'ipBlock': {'cidr': '0.0.0.0/0'}}], 'ports': [{'protocol': 'TCP', 'port': 443}]},
        {'to': [{'ipBlock': {'cidr': '169.254.170.23/32'}}], 'ports': [{'protocol': 'TCP', 'port': 80}]}]}, 'Egress must be DNS/HTTPS/Pod Identity only')
    require(policies['controller-health'] == {'podSelector': selector, 'policyTypes': ['Ingress'],
        'ingress': [{'from': [{'ipBlock': {'cidr': '10.40.0.0/16'}}], 'ports': [{'protocol': 'TCP', 'port': 8081}]}]}, 'No public ingress or metrics exposure')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    operator, access = render(args.directory)
    validate_operator(platform.load_documents(operator))
    validate_access(platform.load_documents(access))
    # As with ESO, the pinned Kubernetes schema collection has no CRD envelope.
    # CRD bytes come from the verified archive; inventory is checked above.
    platform.schema_check(yaml.safe_dump_all(d for d in platform.load_documents(operator)
                                          if d['kind'] != 'CustomResourceDefinition') + '\n---\n' + access)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'karpenter-controller.yaml').write_text(operator)
    (args.output / 'karpenter-access.yaml').write_text(access)
    print('Karpenter controller-only contracts and schemas passed; no AWS/EKS changes')


if __name__ == '__main__':
    main()
