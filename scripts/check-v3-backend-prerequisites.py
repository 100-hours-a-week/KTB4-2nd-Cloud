#!/usr/bin/env python3
"""Read-only BE rollout preflight. Prints no credentials and performs no deployment or AI calls."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('rollout', ROOT / 'scripts/prepare-v3-backend-rollout.py')
rollout = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rollout)
require = rollout.require
CLUSTER = 'arn:aws:eks:ap-northeast-2:483175530259:cluster/yeodam-v3-foundation'
# Ask kubectl to project only Secret type and key NAMES, never payload values.
SECRET_METADATA = '{{.type}}{{"\n"}}{{range $key, $value := .data}}{{$key}}{{"\n"}}{{end}}'


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(subset(e, a) for e, a in zip(expected, actual))
    return expected == actual


def ready(resource):
    return any(c.get('type') == 'Ready' and c.get('status') == 'True'
               for c in resource.get('status', {}).get('conditions', []))


def check(current, candidate, before, after, review, query):
    expected = rollout.prepare(current, rollout.stage_name(candidate), review)
    require(candidate == expected, 'Candidate must be the reviewed cumulative expansion, without other changes')
    rollout.verify_preservation(before, after)
    require(rollout.enabled(current) <= rollout.enabled(candidate), 'Preflight cannot remove existing roles')
    # Always verify the current release, not only a label or a Ready count.
    for document in before:
        kind, ns, name = rollout.identity(document)
        if kind == 'Deployment':
            actual = query(ns, 'deployment', name)
            status = actual.get('status', {})
            require(subset(document['spec'], actual.get('spec', {})), 'Existing BE Deployment differs from the reviewed release: ' + name)
            require(status.get('observedGeneration', 0) >= actual['metadata']['generation'] and
                    all(status.get(k) == 1 for k in ('replicas', 'readyReplicas', 'updatedReplicas', 'availableReplicas')),
                    'Existing BE rollout is not stable: ' + name)
        elif kind == 'Service':
            require(subset(document['spec'], query(ns, 'service', name).get('spec', {})),
                    'Existing BE Service differs from the reviewed release: ' + name)
        elif kind == 'ConfigMap':
            require(query(ns, 'configmap', name).get('data') == document['data'],
                    'Existing BE runtime ConfigMap differs from the reviewed release: ' + name)
    for role in sorted(rollout.enabled(candidate)):
        ns = rollout.NAMESPACES[role]
        config = rollout.apps.merge(candidate['defaults'], candidate['workloads'][role])
        account = query(ns, 'serviceaccount', config['serviceAccountName'])
        require(account['metadata']['name'] == role, 'Role ServiceAccount mismatch')
        keys = {}
        for ref in config['secretRefs'].values():
            keys.setdefault(ref['name'], set()).add(ref['key'])
        keys['backend-trust'] = {'mysql.crt', 'redis.crt', 'mongodb.crt', 'kafka.crt'}
        keys['ghcr-pull'] = {'.dockerconfigjson'}
        for name, required in keys.items():
            metadata = query(ns, 'secret-metadata', name)
            expected_type = 'kubernetes.io/dockerconfigjson' if name == 'ghcr-pull' else 'Opaque'
            require(metadata['type'] == expected_type and required <= set(metadata['keys']),
                    'Missing Secret type or keys: ' + ns + '/' + name)
        for name in ('backend-credentials', 'kafka-' + role):
            external = query(ns, 'externalsecret', name)
            require(ready(external) and external['spec']['target']['name'] == name and
                    bool(external['spec']['data']) and
                    external['spec'].get('secretStoreRef') == {'kind': 'SecretStore', 'name': 'aws-secretsmanager'} and
                    all(d['remoteRef']['key'] == 'yeodam/v3/foundation/' + ns + '/backend'
                        for d in external['spec']['data']), 'ExternalSecret not Ready or source mismatch: ' + ns + '/' + name)
    return {'currentStage': rollout.stage_name(current), 'targetStage': rollout.stage_name(candidate),
            'replicas': len(rollout.enabled(candidate)), 'existingResourcesPreserved': True,
            'scope': 'Prerequisites only; not application E2E, IAM permission or AI transport proof'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current-values', required=True, type=Path)
    parser.add_argument('--candidate-values', required=True, type=Path)
    parser.add_argument('--review', required=True, type=Path)
    parser.add_argument('--context', required=True, help='Explicit kubeconfig context for V3 foundation')
    args = parser.parse_args()
    command = ['kubectl', '--context', args.context, '--request-timeout=20s']

    def run(parts):
        result = subprocess.run(command + parts, text=True, capture_output=True, timeout=30)
        require(result.returncode == 0, 'Read-only prerequisite query failed; output withheld')
        return result.stdout

    context = json.loads(run(['config', 'view', '--minify', '-o', 'json']))
    require(len(context['contexts']) == 1 and context['contexts'][0]['context']['cluster'] == CLUSTER,
            'Select the reviewed V3 foundation cluster context')

    def query(ns, kind, name):
        if kind == 'secret-metadata':
            lines = run(['-n', ns, 'get', 'secret', name, '-o', 'go-template=' + SECRET_METADATA]).splitlines()
            require(bool(lines), 'Secret metadata query returned no type')
            return {'type': lines[0], 'keys': lines[1:]}
        return json.loads(run(['-n', ns, 'get', kind, name, '-o', 'json']))

    current = rollout.load_current(args.current_values)
    candidate = rollout.load_current(args.candidate_values)
    _, before = rollout.render_checked(current)
    _, after = rollout.render_checked(candidate)
    print(json.dumps(check(current, candidate, before, after, json.loads(args.review.read_text()), query), indent=2))


if __name__ == '__main__':
    main()
