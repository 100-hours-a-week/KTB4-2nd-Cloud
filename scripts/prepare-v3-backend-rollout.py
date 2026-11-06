#!/usr/bin/env python3
"""Prepare a cumulative BE rollout from reviewed public Helm values. Never deploy or read secrets."""
import argparse
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('backend_apps', ROOT / 'scripts/check-v3-apps.py')
apps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apps)
require = apps.require
BASE = ROOT / 'deploy/v3/environments/foundation/backend.values.yaml'
CONNECTIONS = ROOT / 'deploy/v3/environments/foundation/backend-connections.values.yaml'
STAGES = {
    'auth': {'auth-api'},
    'apis': {'auth-api', 'trip-api', 'photo-api'},
    'conversion': {'auth-api', 'trip-api', 'photo-api', 'photo-converter'},
    'all': {'auth-api', 'trip-api', 'photo-api', 'photo-converter', 'trip-result-worker'},
}
NAMESPACES = {'auth-api': 'auth', 'trip-api': 'trip', 'photo-api': 'photo',
              'photo-converter': 'photo', 'trip-result-worker': 'trip'}


def enabled(values):
    return {name for name, config in values['workloads'].items() if config['enabled']}


def stage_name(values):
    matches = [name for name, roles in STAGES.items() if roles == enabled(values)]
    require(len(matches) == 1, 'Current values must describe one cumulative BE stage')
    return matches[0]


def prepare(current, stage, review):
    require(stage in STAGES and stage != 'auth', 'Expansion target must be apis, conversion or all')
    require(current.get('backendContract') and current.get('backendConnectionContract'),
            'Use complete TLS-connected current values, never an unconnected fixture')
    previous = stage_name(current)
    order = list(STAGES)
    require(order.index(stage) in (order.index(previous), order.index(previous) + 1),
            'Expand one stage at a time; do not skip or remove running roles')
    require(set(review) == {'backendSourceCommit', 'redisSentinelFeatureReviewed', 'resultWorkerAiPathReviewed'},
            'Exact source-bound compatibility review required')
    require(review['backendSourceCommit'] == current['defaults']['configMapData']['RELEASE'],
            'Review must match the deployed BE source, not a different main commit')
    require(all(type(review[k]) is bool for k in ('redisSentinelFeatureReviewed', 'resultWorkerAiPathReviewed')),
            'Review flags must be explicit booleans')
    require(review['redisSentinelFeatureReviewed'],
            'Confirm the selected image photo cache/lock Sentinel/TLS/ACL path before expansion')
    require(stage != 'all' or review['resultWorkerAiPathReviewed'],
            'Result worker needs reviewed AI transport, status polling and shared-resource access')
    candidate = deepcopy(current)
    for role in STAGES['all']:
        candidate['workloads'][role]['enabled'] = role in STAGES[stage]
    for role in STAGES[stage]:
        config = apps.merge(candidate['defaults'], candidate['workloads'][role])
        require(config['replicas'] == 1, 'One replica per role is the initial budget')
        require(config['resources']['requests']['cpu'] == '100m' and
                config['resources']['requests']['memory'] == '512Mi' and
                config['resources']['limits']['memory'] == '1Gi' and
                not config['resources']['limits'].get('cpu'), 'Keep the reviewed minimum BE resources')
        require(config['configMapData']['VIPS_CONCURRENCY'] == '1', 'Keep conversion concurrency one')
    return candidate


def identity(document):
    return (document['kind'], document['metadata'].get('namespace'), document['metadata']['name'])


def verify_preservation(before, after):
    previous = {identity(d): d for d in before}
    candidate = {identity(d): d for d in after}
    require(all(candidate.get(key) == value for key, value in previous.items()),
            'Expansion must preserve every existing rendered resource, including auth Pod template')


def render_checked(values):
    with tempfile.TemporaryDirectory(prefix='v3-be-rollout-') as directory:
        path = Path(directory) / 'values.json'
        path.write_text(json.dumps(values))
        source = apps.render([path])
    documents = apps.platform.load_documents(source)
    apps.validate(documents, values)
    return source, documents


def load_current(path):
    # No Secret payload input. Same merge order as the existing app release.
    return apps.load_values([BASE, CONNECTIONS, path])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current-values', required=True, type=Path,
                        help='Public current release overlay; never helm get all or Secret YAML')
    parser.add_argument('--stage', choices=['apis', 'conversion', 'all'], required=True)
    parser.add_argument('--review', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    require(not args.output.exists(), 'Refusing to replace an existing release input')
    current = load_current(args.current_values)
    _, before = render_checked(current)
    candidate = prepare(current, args.stage, json.loads(args.review.read_text()))
    source, after = render_checked(candidate)
    verify_preservation(before, after)
    apps.platform.schema_check(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as output:
        json.dump(candidate, output, indent=2)
    print('Prepared ' + args.stage + ': ' + str(len(enabled(candidate))) +
          ' BE replicas; existing resources preserved. No deployment or secret transfer performed.')


if __name__ == '__main__':
    main()
