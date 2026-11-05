#!/usr/bin/env python3
"""Render pinned ESO and namespace references; never read a real secret or cluster."""
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
load_documents = platform.load_documents
CHART = ROOT / 'deploy/v3/secret-access'
OPERATOR = ROOT / 'deploy/v3/secret-operator'


def render(chart, release, values=None):
    command = [os.environ['V3_HELM'], 'template', release, str(chart),
               '--namespace', 'external-secrets', '--kube-version', '1.36.0']
    if values:
        for value_file in values if isinstance(values, list) else [values]:
            command += ['-f', str(value_file)]
    return subprocess.check_output(command, text=True)


def merge(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def validate_operator(documents):
    lock = json.loads((OPERATOR / 'upstream.lock.json').read_text())
    require(Counter(d['kind'] for d in documents) == {
        'CustomResourceDefinition': 20, 'ClusterRole': 4, 'ClusterRoleBinding': 2,
        'Deployment': 3, 'Secret': 1, 'Service': 1, 'ServiceAccount': 3,
        'ValidatingWebhookConfiguration': 2}, 'Pinned operator inventory changed')
    require(len({(d['kind'], d['metadata'].get('namespace'), d['metadata']['name']) for d in documents})
            == len(documents), 'Duplicate operator resource')
    deployments = {d['metadata']['name']: d for d in documents if d['kind'] == 'Deployment'}
    require(set(deployments) == {'external-secrets', 'external-secrets-webhook',
                                 'external-secrets-cert-controller'}, 'Exactly three ESO components required')
    for name, obj in deployments.items():
        require(obj['metadata']['namespace'] == 'external-secrets', 'Operator namespace mismatch')
        require(obj['spec']['replicas'] == 1, 'One replica per component in the initial foundation')
        pod = obj['spec']['template']['spec']
        require(pod.get('hostNetwork') is False, 'No host networking')
        require(len(pod['containers']) == 1 and not pod.get('initContainers'), 'No extra operator containers')
        container = pod['containers'][0]
        require(container['image'] == lock['image'], 'Operator image must match reviewed digest')
        core = name == 'external-secrets'
        expected = {'requests': {'cpu': '50m' if core else '10m', 'memory': '64Mi' if core else '32Mi'},
                    'limits': {'memory': '256Mi' if core else '128Mi'}}
        require(container['resources'] == expected, 'Initial controller resource budget changed')
        security = container['securityContext']
        require(security.get('runAsNonRoot') is True and security.get('readOnlyRootFilesystem') is True
                and security.get('allowPrivilegeEscalation') is False
                and security.get('capabilities', {}).get('drop') == ['ALL']
                and security.get('seccompProfile', {}).get('type') == 'RuntimeDefault', 'Unsafe operator container')
        require(pod['serviceAccountName'] == name, 'Each component uses its own service account')
        if core:
            require(set(container['args']) >= {
                '--enable-cluster-store-reconciler=false',
                '--enable-cluster-external-secret-reconciler=false',
                '--enable-cluster-push-secret-reconciler=false',
                '--enable-push-secret-reconciler=false'}, 'Cluster/push reconcilers disabled')
    for obj in documents:
        if obj['kind'] == 'Secret':
            require(obj['metadata']['name'] == 'external-secrets-webhook'
                    and not obj.get('data') and not obj.get('stringData'), 'Only empty webhook TLS placeholder allowed')
        if obj['kind'] == 'ClusterRole':
            require(not any(k.startswith('rbac.authorization.k8s.io/aggregate-to-')
                            for k, v in obj['metadata'].get('labels', {}).items() if v == 'true'),
                    'Do not expand existing admin/edit/view permissions')
            for rule in obj['rules']:
                require('*' not in rule['verbs'] and '*' not in rule['resources'], 'No wildcard Kubernetes RBAC')
                require('serviceaccounts/token' not in rule['resources'], 'No token minting')
                require(not any(r.startswith('clustersecret') or r.startswith('clusterexternal')
                                or 'pushsecret' in r for r in rule['resources']), 'Cluster/push Secret features disabled')
    crds = [d for d in documents if d['kind'] == 'CustomResourceDefinition']
    require(len(crds) == 20, 'Pinned chart CRD inventory changed')
    require({d['metadata']['name'] for d in crds if d['spec']['group'] == 'external-secrets.io'} ==
            {'externalsecrets.external-secrets.io', 'secretstores.external-secrets.io'}, 'Only namespaced pull Secret APIs')
    require(all(d['spec']['scope'] == 'Namespaced' for d in crds), 'No cluster Secret/Generator APIs')


def validate_access(documents, stores):
    require(platform.NAMESPACES <= set(stores) <= platform.NAMESPACES | {'mysql', 'redis'},
            'Five application slots and optional MySQL/Redis slots only')
    actual = {}
    for doc in documents:
        identity = (doc['kind'], doc['metadata'].get('namespace', ''), doc['metadata']['name'])
        require(identity not in actual, 'Duplicate resource identity')
        actual[identity] = doc
    baseline = [d for d in documents if d['kind'] in ('Namespace', 'NetworkPolicy')]
    require(Counter(d['kind'] for d in baseline) == {'Namespace': 1, 'NetworkPolicy': 4}, 'Access baseline inventory changed')
    ns = actual.get(('Namespace', '', 'external-secrets'), {})
    require(ns.get('metadata', {}).get('annotations', {}).get('helm.sh/resource-policy') == 'keep', 'Retain infrastructure namespace')
    policies = {d['metadata']['name']: d['spec'] for d in baseline if d['kind'] == 'NetworkPolicy'}
    require(all(d['metadata']['namespace'] == 'external-secrets' for d in baseline if d['kind'] == 'NetworkPolicy'), 'Policy namespace mismatch')
    require(policies['default-deny'] == {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}, 'Default deny widened')
    egress = policies['allow-controller-egress']
    require(egress == {'podSelector': {'matchLabels': {'app.kubernetes.io/part-of': 'yeodam'}},
                      'policyTypes': ['Egress'], 'egress': [
                          {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                                   'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                           'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]},
                          {'to': [{'ipBlock': {'cidr': '0.0.0.0/0'}}], 'ports': [{'protocol': 'TCP', 'port': 443}]}]}, 'Controller egress widened')
    for policy, component, direction, peer, cidr, port in [
        ('allow-core-pod-identity', 'external-secrets', 'egress', 'to', '169.254.170.23/32', 80),
        ('allow-control-plane-webhook', 'external-secrets-webhook', 'ingress', 'from', '10.40.0.0/16', 10250),
    ]:
        require(policies[policy] == {'podSelector': {'matchLabels': {'app.kubernetes.io/name': component,
                                                                   'app.kubernetes.io/instance': 'external-secrets'}},
                                    'policyTypes': [direction.title()], direction: [
                                        {peer: [{'ipBlock': {'cidr': cidr}}], 'ports': [{'protocol': 'TCP', 'port': port}]}]}, 'Operator network boundary widened')
    expected = {key for key in actual if key[0] in ('Namespace', 'NetworkPolicy')}
    for namespace, store in stores.items():
        require(set(store) == {'enabled', 'roleArn', 'secrets'}, 'Unknown store configuration')
        if not store['enabled']:
            require(not store['roleArn'] and not store['secrets'], 'Disabled store contains configuration')
            continue
        role = f'arn:aws:iam::483175530259:role/yeodam-v3-foundation-eso-{namespace}'
        require(store['roleArn'] == role, 'Namespace reader role mismatch')
        key = ('SecretStore', namespace, 'aws-secretsmanager')
        require(key in actual, 'Missing namespace SecretStore')
        require(actual[key]['spec'] == {'provider': {'aws': {'service': 'SecretsManager',
                'region': 'ap-northeast-2', 'role': role}}}, 'Static credentials or unexpected provider settings')
        expected.add(key)
        for binding in store['secrets']:
            key = ('ExternalSecret', namespace, binding['name'])
            require(key not in expected and key in actual, 'Missing or duplicate ExternalSecret')
            expected.add(key)
            data = []
            seen = set()
            require(binding.get('type', 'Opaque') in ('Opaque', 'kubernetes.io/dockerconfigjson'), 'Unsupported Secret type')
            for mapping in binding['data']:
                require(mapping['secretKey'] not in seen, 'Duplicate target Secret key')
                seen.add(mapping['secretKey'])
                require(mapping['remoteKey'].startswith(f'yeodam/v3/foundation/{namespace}/'), 'Cross-namespace source')
                ref = {'key': mapping['remoteKey'], 'version': 'AWSCURRENT', 'decodingStrategy': 'None'}
                if mapping.get('property'):
                    ref['property'] = mapping['property']
                data.append({'secretKey': mapping['secretKey'], 'remoteRef': ref})
            if binding.get('type') == 'kubernetes.io/dockerconfigjson':
                require(seen == {'.dockerconfigjson'}, 'Registry Secret requires exactly .dockerconfigjson')
            require(data and actual[key]['spec'] == {
                'refreshPolicy': 'Periodic', 'refreshInterval': '1h',
                'secretStoreRef': {'kind': 'SecretStore', 'name': 'aws-secretsmanager'},
                'target': {'name': binding['name'], 'creationPolicy': 'Orphan', 'deletionPolicy': 'Retain',
                           'template': {'type': binding.get('type', 'Opaque')}},
                'data': data}, 'Unexpected Secret payload/template/store/lifecycle')
    require(set(actual) == expected, 'Unexpected resources in Secret access chart')


def write_custom_schemas(operator_documents):
    # The schema is from the same checksum-pinned CRD as the operator, not latest.
    directory = Path(os.environ['V3_SCHEMA_DIR'])
    for doc in operator_documents:
        if doc['kind'] == 'CustomResourceDefinition' and doc['spec']['group'] == 'external-secrets.io':
            version = next(v for v in doc['spec']['versions'] if v['name'] == 'v1')
            schema = version['schema']['openAPIV3Schema']
            (directory / f"{doc['spec']['names']['kind'].lower()}.json").write_text(json.dumps(schema))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('-f', '--values', type=Path, action='append', default=[],
                        help='Additionally validate candidate reference-only values, never Secret payloads')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    operator_source = render(args.directory / 'external-secrets.tgz', 'external-secrets', OPERATOR / 'values.yaml')
    operator = load_documents(operator_source)
    validate_operator(operator)
    write_custom_schemas(operator)
    # Kubernetes-json-schema has no CRD envelope schema. CRD source is pinned,
    # inventory checked above, and its own schema checks our v1 custom resources.
    platform.schema_check(yaml.safe_dump_all(d for d in operator if d['kind'] != 'CustomResourceDefinition'))
    (args.output / 'secret-operator.yaml').write_text(operator_source)
    defaults = yaml.safe_load((CHART / 'values.yaml').read_text())
    source = render(CHART, 'yeodam-v3-secret-access')
    validate_access(load_documents(source), defaults['stores'])
    platform.schema_check(source)
    (args.output / 'secret-access-disabled.yaml').write_text(source)
    fixture = ROOT / 'tests/ci/v3/secrets-values.fixture.yaml'
    source = render(CHART, 'yeodam-v3-secret-access', fixture)
    validate_access(load_documents(source), yaml.safe_load(fixture.read_text())['stores'])
    platform.schema_check(source)
    (args.output / 'secret-access-ci-fixture.yaml').write_text(source)
    if args.values:
        values = defaults
        for path in args.values:
            values = merge(values, yaml.load(path.read_text(), Loader=platform.UniqueLoader))
        source = render(CHART, 'yeodam-v3-secret-access', args.values)
        validate_access(load_documents(source), values['stores'])
        platform.schema_check(source)
        (args.output / 'secret-access-candidate.yaml').write_text(source)
    print(json.dumps({'operatorResources': dict(Counter(d['kind'] for d in operator)),
                      'controllerRequests': {'cpu': '70m', 'memory': '128Mi'},
                      'clusterAccessed': False, 'realSecretsRead': False}, sort_keys=True))


if __name__ == '__main__':
    main()
