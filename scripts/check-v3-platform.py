#!/usr/bin/env python3
"""Check rendered V3 platform contracts without applying Kubernetes objects."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/v3/platform'
NAME = 'app.kubernetes.io/name'
INSTANCE = 'app.kubernetes.io/instance'
COMPONENT = 'app.kubernetes.io/component'
PART_OF = 'app.kubernetes.io/part-of'
STAGE = 'yeodam.io/stage'
PURPOSE = 'yeodam.io/deployment-purpose'
NAMESPACES = frozenset({'frontend', 'auth', 'trip', 'photo', 'ai'})


class ContractError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ContractError(f'Duplicate YAML key: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_documents(source):
    return [doc for doc in yaml.load_all(source, Loader=UniqueLoader) if doc is not None]


def require(condition, message):
    if not condition:
        raise ContractError(message)


def expected_accounts(values):
    require(set(values['namespaces']) == NAMESPACES, 'Only the five reviewed application namespaces are allowed')
    result = {}
    for namespace, config in values['namespaces'].items():
        for instance, workload in config['workloads'].items():
            result[(namespace, instance)] = {
                NAME: workload['name'], INSTANCE: instance, COMPONENT: workload['component'],
                PART_OF: 'yeodam', STAGE: 'v3', PURPOSE: workload['purpose'],
            }
    return result


def dns_spec():
    return {
        'podSelector': {}, 'policyTypes': ['Egress'],
        'egress': [{
            'to': [{
                'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}},
            }],
            'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}],
        }],
    }


def validate_platform(documents, values):
    accounts = expected_accounts(values)
    identities = set()
    expected = {('Namespace', '', ns) for ns in NAMESPACES}
    expected |= {('ServiceAccount', ns, instance) for ns, instance in accounts}
    expected |= {('NetworkPolicy', ns, policy) for ns in NAMESPACES
                 for policy in ('default-deny', 'allow-cluster-dns')}
    for document in documents:
        require(isinstance(document, dict), 'Every manifest must be a resource object')
        metadata = document.get('metadata', {})
        identity = document.get('kind'), metadata.get('namespace', ''), metadata.get('name')
        require(identity in expected, f'Unexpected platform resource: {identity}')
        require(identity not in identities, f'Duplicate resource: {identity}')
        identities.add(identity)
        kind, namespace, name = identity
        labels = metadata.get('labels', {})
        require(labels.get(PART_OF) == 'yeodam' and labels.get(STAGE) == 'v3', 'Missing platform identity labels')
        if kind == 'Namespace':
            require(labels.get('yeodam.io/domain') == name, 'Namespace domain label does not match its name')
            require(metadata.get('annotations', {}).get('helm.sh/resource-policy') == 'keep',
                    'Namespaces must survive release removal')
        elif kind == 'ServiceAccount':
            require(labels == accounts[(namespace, name)], 'ServiceAccount workload labels are inconsistent')
            require(document.get('automountServiceAccountToken') is False, 'App API token mount must be disabled')
            require(not document.get('secrets'), 'Legacy API token secrets must not be attached')
            require(not metadata.get('annotations'), 'AWS permissions require a separate reviewed identity change')
        elif name == 'default-deny':
            require(document.get('spec') == {
                'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [], 'egress': [],
            }, 'The baseline must isolate ingress and egress for every application Pod')
        else:
            require(document.get('spec') == dns_spec(), 'DNS must target kube-system AND kube-dns on TCP/UDP53 only')
    require(identities == expected, f'Missing platform resources: {sorted(expected - identities)}')
    return accounts


def stable_selector(labels):
    return {NAME: labels[NAME], INSTANCE: labels[INSTANCE]}


def validate_workloads(documents, accounts):
    """Reusable check for future app manifests; this chart deploys no apps."""
    seen = set()
    for document in documents:
        metadata = document['metadata']
        namespace = metadata.get('namespace')
        require(namespace in NAMESPACES, 'Workloads must use an explicit application namespace')
        identity = document['kind'], namespace, metadata['name']
        require(identity not in seen, 'Duplicate workload resource')
        seen.add(identity)
        spec = document['spec']
        if document['kind'] == 'Deployment':
            template = spec['template']
            pod = template['spec']
            account = namespace, pod.get('serviceAccountName')
            require(account in accounts, 'Use the reviewed per-deployment ServiceAccount, not default')
            require(metadata['name'] == account[1], 'Deployment name must match its workload identity')
            labels = template['metadata'].get('labels', {})
            require(all(labels.get(key) == value for key, value in accounts[account].items()),
                    'Pod template must carry its workload labels; namespace labels are not inherited')
            require(spec.get('selector') == {'matchLabels': stable_selector(accounts[account])},
                    'Deployment selector must use stable name+instance only')
            require(pod.get('automountServiceAccountToken', False) is False, 'Pod must not override API token protection')
            require(not pod.get('hostNetwork', False), 'Host networking bypasses application policy boundaries')
            for container in pod['containers']:
                require(container.get('resources', {}).get('requests'), 'App resources must be explicitly budgeted')
        elif document['kind'] == 'Service':
            selector = spec.get('selector', {})
            matches = [labels for (ns, _), labels in accounts.items()
                       if ns == namespace and selector == stable_selector(labels)]
            require(len(matches) == 1, 'Service must select exactly one stable workload identity in its namespace')
            require(metadata['name'] == matches[0][INSTANCE], 'Service must not select another deployment purpose')
        else:
            raise ContractError('Only Deployment and Service app contracts are supported in this stage')


def render():
    lock = json.loads((ROOT / 'tests/ci/v3/tools.lock.json').read_text())
    return subprocess.check_output([
        os.environ['V3_HELM'], 'template', 'yeodam-v3-platform', str(CHART),
        '--kube-version', lock['kubernetesVersion'],
    ], text=True)


def schema_check(source):
    lock = json.loads((ROOT / 'tests/ci/v3/tools.lock.json').read_text())
    subprocess.run([
        os.environ['V3_KUBECONFORM'], '-strict', '-summary',
        '-kubernetes-version', lock['kubernetesVersion'],
        '-schema-location', str(Path(os.environ['V3_SCHEMA_DIR']) / '{{.ResourceKind}}.json'),
    ], input=source, text=True, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rendered', type=Path, help='Check supplied platform rendering instead of Helm output')
    parser.add_argument('--workloads', type=Path, help='Additionally check future Deployment/Service definitions')
    args = parser.parse_args()
    values = yaml.load((CHART / 'values.yaml').read_text(), Loader=UniqueLoader)
    source = args.rendered.read_text() if args.rendered else render()
    documents = load_documents(source)
    accounts = validate_platform(documents, values)
    schema_check(source)
    if args.workloads:
        app_source = args.workloads.read_text()
        validate_workloads(load_documents(app_source), accounts)
        schema_check(app_source)
    print(json.dumps({'platformResources': dict(Counter(doc['kind'] for doc in documents)),
                      'applicationPodsDeployed': 0, 'clusterAccessed': False}, sort_keys=True))


if __name__ == '__main__':
    main()
