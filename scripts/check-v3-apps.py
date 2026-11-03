#!/usr/bin/env python3
"""Render and check V3 app definitions; never contact a cluster or resolve Secrets."""
import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/v3/apps'
SPEC = importlib.util.spec_from_file_location('v3_platform', ROOT / 'scripts/check-v3-platform.py')
platform = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(platform)
require = platform.require


def merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


def load_values(files=()):
    values = platform.load_documents((CHART / 'values.yaml').read_text())[0]
    for file in files:
        documents = platform.load_documents(Path(file).read_text())
        require(len(documents) == 1 and isinstance(documents[0], dict), 'Each values file must contain one mapping')
        values = merge(values, documents[0])
    return values


def render(files=()):
    lock = json.loads((ROOT / 'tests/ci/v3/tools.lock.json').read_text())
    command = [os.environ['V3_HELM'], 'template', 'yeodam-v3-apps', str(CHART), '--kube-version', lock['kubernetesVersion']]
    for file in files:
        command += ['-f', str(file)]
    return subprocess.check_output(command, text=True)


def quantity(value, resource):
    if resource == 'cpu':
        require(bool(re.fullmatch(r'(?:[0-9]+m|[0-9]+(?:\.[0-9]+)?)', value)), 'Invalid CPU quantity')
        result = Decimal(value[:-1]) / 1000 if value.endswith('m') else Decimal(value)
    else:
        match = re.fullmatch(r'([1-9][0-9]*)(Ki|Mi|Gi|Ti)', value)
        require(bool(match), 'Use positive Ki/Mi/Gi/Ti memory or storage quantities')
        result = Decimal(match[1]) * (1024 ** ('Ki', 'Mi', 'Gi', 'Ti').index(match[2])) * 1024
    require(result > 0, 'Resource quantities must be positive')
    return result


def resource_budget(resources):
    for resource in ['cpu', 'memory', 'ephemeral-storage']:
        requested = quantity(resources['requests'][resource], resource)
        if resources['limits'].get(resource):
            require(requested <= quantity(resources['limits'][resource], resource), 'Resource request exceeds its limit')


def validate(documents, values):
    platform_values = platform.load_documents((ROOT / 'deploy/v3/platform/values.yaml').read_text())[0]
    accounts = platform.expected_accounts(platform_values)
    configs = {name: merge(values['defaults'], item) for name, item in values['workloads'].items() if item['enabled']}
    expected = {('Deployment', c['namespace'], name) for name, c in configs.items()}
    expected |= {('Service', c['namespace'], name) for name, c in configs.items() if c['service']['enabled']}
    identities = {(doc['kind'], doc['metadata'].get('namespace'), doc['metadata']['name']) for doc in documents}
    require(len(identities) == len(documents) and identities == expected, 'App resource set differs from enabled values')
    platform.validate_workloads(documents, accounts)
    for doc in documents:
        name = doc['metadata']['name']
        config = configs[name]
        labels = accounts[(config['namespace'], config['serviceAccountName'])]
        require(config['imageRef'] == labels[platform.NAME], 'Image reference must match the platform application identity')
        require(all(doc['metadata']['labels'].get(key) == value for key, value in labels.items()), 'Resource identity labels mismatch')
        if doc['kind'] == 'Service':
            require(config['purpose'] not in ['conversion-worker', 'result-worker'], 'Worker-purpose copies have no HTTP Service')
            require(doc['spec'].get('type') == 'ClusterIP', 'External load balancers are outside this chart')
            require(doc['spec']['ports'] == [{'name': 'http', 'port': config['service']['port'], 'targetPort': 'http', 'protocol': 'TCP'}],
                    'Service must expose only the application HTTP port')
            continue
        spec = doc['spec']
        pod_template = spec['template']
        pod = pod_template['spec']
        require(not any(pod.get(flag, False) for flag in ['hostNetwork', 'hostPID', 'hostIPC']),
                'Host namespace access is outside the application contract')
        require(spec['replicas'] == config['replicas'], 'Replica count must be explicit')
        require(spec['strategy'] == {'type': 'RollingUpdate', 'rollingUpdate': config['rollout']}, 'Rollout budget mismatch')
        require(config['rollout']['maxSurge'] + config['rollout']['maxUnavailable'] > 0, 'Rollout cannot have two zero budgets')
        require(config['rollout']['maxUnavailable'] <= config['replicas'], 'Rollout unavailability exceeds replicas')
        require(pod['terminationGracePeriodSeconds'] == config['terminationGracePeriodSeconds'] > config['preStopDelaySeconds'],
                'Termination grace must include time after preStop')
        require(pod['securityContext'] == {'runAsNonRoot': True, **config['security'], 'seccompProfile': {'type': 'RuntimeDefault'}},
                'Pod security must remain non-root with RuntimeDefault seccomp')
        require(all(value > 0 for value in config['security'].values()), 'Use explicit non-root user/group IDs')
        require(pod['imagePullSecrets'] == [{'name': item} for item in config['imagePullSecrets']], 'Pull Secret references mismatch')
        container = pod['containers'][0]
        require(not container.get('command') and not container.get('args'), 'Use image default startup, not per-role commands')
        require(len(pod['containers']) == 1, 'Unreviewed sidecars are outside this chart')
        image = values['images'][config['imageRef']]
        require(bool(re.fullmatch(r'ghcr\.io/[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}', image)), 'Use an immutable GHCR image digest')
        require(container['image'] == image and container['name'] == config['name'], 'Shared image selection mismatch')
        require(pod_template['metadata']['annotations'] == {'yeodam.io/image': image, 'yeodam.io/configuration-revision': config['configurationRevision']},
                'Image and external configuration revision must be recorded')
        require(bool(config['configurationRevision']), 'Configuration revision is required')
        expected_resources = deepcopy(config['resources'])
        if not expected_resources['limits']['cpu']:
            del expected_resources['limits']['cpu']
        resource_budget(expected_resources)
        require(container['resources'] == expected_resources, 'Pod resources differ from explicit values')
        ports = {item['name']: item['containerPort'] for item in container['ports']}
        require(ports == config['ports'] and len(ports) == len(container['ports']), 'Named ports mismatch')
        for probe in ['startup', 'liveness', 'readiness']:
            expected_probe = config['probes'][probe]
            require(container[probe + 'Probe'] == expected_probe, 'Probe must use the configured endpoint and timing')
            require(expected_probe['httpGet']['port'] in ports, 'Probe port does not exist')
        expected_env = [{'name': key, 'value': value} for key, value in sorted(config['environment'].items())]
        require(not any(re.search(r'(password|secret|token|api_?key|private_?key)$', key, re.I) or key == 'KAKAO_CLIENT_ID'
                        for key in config['environment']), 'Known secret environment keys must use secretRefs')
        for key, secret in sorted(config['secretRefs'].items()):
            require(key not in config['environment'], 'Literal and Secret environment entries must not overlap')
            expected_env.append({'name': key, 'valueFrom': {'secretKeyRef': {**secret, 'optional': False}}})
        require(container.get('env', []) == expected_env, 'Only explicit configuration and Secret references may be injected')
        expected_env_from = [{'configMapRef': {'name': key, 'optional': False}} for key in config['configMapRefs']]
        require(container.get('envFrom', []) == expected_env_from, 'Only configured ConfigMap references may be imported')
        volumes = {'scratch': {'name': 'scratch', 'emptyDir': {'sizeLimit': config['scratchSizeLimit']}}}
        mounts = [{'name': 'scratch', 'mountPath': '/tmp'}]
        storage = quantity(config['scratchSizeLimit'], 'ephemeral-storage')
        if config['name'] == 'backend':
            volumes['catalog'] = {'name': 'catalog', 'emptyDir': {'sizeLimit': config['catalogSizeLimit']}}
            mounts.append({'name': 'catalog', 'mountPath': '/app/data'})
            storage += quantity(config['catalogSizeLimit'], 'ephemeral-storage')
            initializers = pod.get('initContainers', [])
            require(len(initializers) == 1, 'Backend catalog must be seeded from the same image')
            seed = initializers[0]
            require(seed['name'] == 'seed-catalog' and seed['image'] == image and seed['command'] == ['cp', '-R', '/app/data/.', '/catalog/'],
                    'Catalog seed must copy the bundled data using the shared backend image')
            require(seed['volumeMounts'] == [{'name': 'catalog', 'mountPath': '/catalog'}], 'Catalog seed must not mask its bundled source')
            require(seed['resources'] == expected_resources, 'Init resources must fit the explicit Pod budget')
        else:
            require(not pod.get('initContainers'), 'Only backend images have the catalog seed contract')
        require(storage <= quantity(config['resources']['limits']['ephemeral-storage'], 'ephemeral-storage'),
                'Scratch/catalog volume budgets exceed the Pod ephemeral-storage limit')
        mount_paths = {m['mountPath'] for m in mounts}
        for secret in config['secretVolumes']:
            require(secret['name'] not in volumes and secret['mountPath'] not in mount_paths, 'Secret volume name/path collides with writable storage')
            require(secret['mountPath'].startswith('/etc/yeodam/') and '..' not in secret['mountPath'].split('/'),
                    'Mount Secret files only below /etc/yeodam')
            require(not any(secret['mountPath'].startswith(path.rstrip('/') + '/') or path.startswith(secret['mountPath'].rstrip('/') + '/')
                            for path in mount_paths), 'Secret mounts must not overlap other mount paths')
            mount_paths.add(secret['mountPath'])
            volumes[secret['name']] = {'name': secret['name'], 'secret': {'secretName': secret['secretName'], 'optional': False, 'defaultMode': 0o440}}
            mounts.append({'name': secret['name'], 'mountPath': secret['mountPath'], 'readOnly': True})
        require(pod['volumes'] == list(volumes.values()) and container['volumeMounts'] == mounts, 'Unreviewed volume or mount found')
        for item in [container, *pod.get('initContainers', [])]:
            require(item['securityContext'] == {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                    'Container filesystem and privilege protection must remain enabled')
        expected_lifecycle = {'preStop': {'sleep': {'seconds': config['preStopDelaySeconds']}}} if config['preStopDelaySeconds'] else {}
        require(container.get('lifecycle', {}) == expected_lifecycle, 'preStop delay mismatch')
    return {'enabledDeployments': len(configs), 'renderedResources': dict(Counter(d['kind'] for d in documents)), 'clusterAccessed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-f', '--values', action='append', default=[], type=Path)
    parser.add_argument('--rendered', type=Path)
    parser.add_argument('--output', type=Path, help='Optionally retain the rendered public-reference-only YAML')
    args = parser.parse_args()
    source = args.rendered.read_text() if args.rendered else render(args.values)
    documents = platform.load_documents(source)
    summary = validate(documents, load_values(args.values))
    if documents:
        platform.schema_check(source)
    if args.output:
        args.output.write_text(source)
    print(json.dumps(summary, sort_keys=True))


if __name__ == '__main__':
    main()
