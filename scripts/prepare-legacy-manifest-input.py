#!/usr/bin/env python3
"""Adapt an explicit main-only manual request to existing legacy candidate validators.

Public image metadata only. No Git, registry, GitHub API or deployment calls.
"""
import argparse
import json
import os
from pathlib import Path
import re

CLOUD = '100-hours-a-week/KTB4-2nd-Cloud'
COMPONENTS = {
    'v1': {'frontend': ('FE', 'yeodam-frontend'), 'backend': ('BE', 'yeodam-backend'),
           'ai': ('AI', 'yeodam-ai-worker')},
    'v2': {'frontend': ('FE', 'yeodam-frontend'), 'backend': ('BE', 'yeodam-backend'),
           'ai-app': ('AI', 'yeodam-ai-app')},
}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'Duplicate input key')
        value[key] = item
    return value


def prepare(generation, event, context):
    require(generation in COMPONENTS, 'Unknown legacy generation')
    require(context.get('GITHUB_ACTIONS') == 'true' and
            context.get('GITHUB_REPOSITORY') == CLOUD and
            context.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            context.get('GITHUB_REF') == 'refs/heads/main',
            'Legacy candidates require an explicit manual request on Cloud main')
    inputs = event.get('inputs', {})
    require(isinstance(inputs, dict) and set(inputs) == {'payload'} and isinstance(inputs['payload'], str),
            'One public JSON payload input required')
    require(len(inputs['payload']) <= 4096, 'Oversized candidate metadata')
    payload = json.loads(inputs['payload'], object_pairs_hook=unique)
    fields = {'component', 'source_repository', 'source_sha', 'image', 'digest'}
    if generation == 'v2':
        fields.add('build_run_id')
    require(isinstance(payload, dict) and set(payload) == fields and
            all(isinstance(v, str) for v in payload.values()), 'Exact public image metadata fields required')
    require(payload['component'] in COMPONENTS[generation], 'Wrong component for legacy generation')
    repo, package = COMPONENTS[generation][payload['component']]
    require(payload['source_repository'] == '100-hours-a-week/KTB4-2nd-' + repo, 'Wrong source repository')
    require(re.fullmatch(r'[0-9a-f]{40}', payload['source_sha']), 'Exact source SHA required')
    require(payload['image'] == f"ghcr.io/100-hours-a-week/{package}:sha-{payload['source_sha']}",
            'Image package and SHA must match')
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', payload['digest']), 'Exact image digest required')
    if generation == 'v2':
        require(re.fullmatch(r'[1-9][0-9]{0,19}', payload['build_run_id']), 'Exact publishing Run ID required')
    # This is an input adapter only. Existing validators and publishing proof remain in place.
    action = 'image-published' if generation == 'v1' else 'v2-image-published'
    return {'action': action, 'client_payload': payload}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--generation', choices=COMPONENTS, required=True)
    p.add_argument('--event-file', type=Path, default=os.environ.get('GITHUB_EVENT_PATH'))
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    require(a.event_file is not None, 'Manual event file required')
    result = prepare(a.generation, json.loads(a.event_file.read_text(), object_pairs_hook=unique), os.environ)
    require(not a.output.exists(), 'Refusing overwrite')
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('x') as f:
        json.dump(result, f)
        f.write('\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as f:
            for key, value in sorted(result['client_payload'].items()):
                f.write(key + '=' + value + '\n')
    print('Manual legacy candidate input validated; existing build/registry/PR checks still required')


if __name__ == '__main__':
    main()
