#!/usr/bin/env python3
"""Inspect or explicitly configure WebSocket without changing the production BE image."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('production_release', ROOT / 'scripts/deploy-v2-production-release.py')
production = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(production)
ACCOUNT, REGION, CLUSTER = production.ACCOUNT, production.REGION, production.CLUSTER
BACKEND, WEBSOCKET = 'yeodam-v2-production-backend', 'yeodam-v2-production-websocket'
TOKEN = '/yeodam/v2/production/app/WEBSOCKET_INTERNAL_SERVICE_TOKEN'
TOKEN_ARN = f'arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{TOKEN}'
CHANNEL = 'yeodam:production:websocket:events:v1'
SOURCE = ROOT / 'deploy/v2-production/websocket/source.json'


def aws(*args, payload=None):
    cmd = ['aws', '--region', REGION, '--output', 'json', *args]
    if payload is not None:
        cmd += ['--cli-input-json', 'file:///dev/stdin']
    result = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True)
    if result.returncode:
        # Task definitions and generated tokens never enter argv, stdout or errors.
        raise RuntimeError('AWS ' + ' '.join(args[:2]) + ' failed; response omitted')
    return json.loads(result.stdout) if result.stdout.strip() else {}


def identity():
    if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise ValueError('Only the reviewed production account is allowed')


def service(name):
    result = aws('ecs', 'describe-services', '--cluster', CLUSTER, '--services', name)
    if result.get('failures') or len(result.get('services', [])) != 1:
        raise ValueError('Expected production ECS service: ' + name)
    row = result['services'][0]
    if row.get('serviceName') != name or row.get('clusterArn') != f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{CLUSTER}':
        raise ValueError('Unexpected ECS target')
    return row


def definition(arn):
    row = aws('ecs', 'describe-task-definition', '--task-definition', arn)['taskDefinition']
    if row.get('family') != BACKEND or row.get('status') != 'ACTIVE':
        raise ValueError('Only the active production backend definition is allowed')
    if [c['name'] for c in row['containerDefinitions']] != ['backend']:
        raise ValueError('Unexpected backend containers')
    expected = json.loads(SOURCE.read_text())['backendImage']
    actual = production.common.normalize_image('backend', row['containerDefinitions'][0]['image'])
    if actual != expected:
        raise ValueError('Production backend image changed; review compatibility before continuing')
    return row


def patched_definition(current):
    definition(current['taskDefinitionArn'])  # Revalidate the actual AWS revision.
    data = {key: copy.deepcopy(value) for key, value in current.items()
            if key in production.common.ai.FIELDS and value is not None}
    container = data['containerDefinitions'][0]
    env = {v['name']: v['value'] for v in container.get('environment', [])}
    if 'WEBSOCKET_INTERNAL_SERVICE_TOKEN' in env:
        raise ValueError('Plaintext WebSocket token is forbidden')
    if env.get('WEBSOCKET_EVENTS_CHANNEL', CHANNEL) != CHANNEL:
        raise ValueError('Existing WebSocket channel differs; review before replacing it')
    env['WEBSOCKET_EVENTS_CHANNEL'] = CHANNEL
    container['environment'] = [{'name': name, 'value': value} for name, value in sorted(env.items())]
    secret_refs = {v['name']: v['valueFrom'] for v in container.get('secrets', [])}
    if secret_refs.get('WEBSOCKET_INTERNAL_SERVICE_TOKEN', TOKEN_ARN) != TOKEN_ARN:
        raise ValueError('Existing WebSocket token reference differs')
    secret_refs['WEBSOCKET_INTERNAL_SERVICE_TOKEN'] = TOKEN_ARN
    container['secrets'] = [{'name': name, 'valueFrom': value} for name, value in sorted(secret_refs.items())]
    return data


def token_metadata():
    rows = aws('ssm', 'describe-parameters', '--parameter-filters',
               f'Key=Name,Option=Equals,Values={TOKEN}')['Parameters']
    if rows and (len(rows) != 1 or rows[0]['Type'] != 'SecureString'):
        raise ValueError('Expected exactly one SecureString WebSocket token')
    return bool(rows)


def ensure_idle(row):
    if not production.common.ai.stable(row, row['taskDefinition']):
        raise ValueError('Backend must be stable before configuration')
    params = aws('ssm', 'describe-parameters', '--parameter-filters',
                 f'Key=Name,Option=Equals,Values={production.ACTIVE}')['Parameters']
    if params:
        active = json.loads(aws('ssm', 'get-parameter', '--name', production.ACTIVE)['Parameter']['Value'])
        if active.get('status') not in {'healthy', 'restored_previous'}:
            raise ValueError('Another production release needs review before this operation')


def verify_discovery(row):
    registries = row.get('serviceRegistries', [])
    if len(registries) != 1:
        raise ValueError('Apply the reviewed backend Cloud Map registration first')
    arn = registries[0]['registryArn']
    if not arn.startswith(f'arn:aws:servicediscovery:{REGION}:{ACCOUNT}:service/srv-'):
        raise ValueError('Unexpected service registry account/region')
    data = aws('servicediscovery', 'get-service', '--id', arn.rsplit('/', 1)[1])['Service']
    namespace = aws('servicediscovery', 'get-namespace', '--id', data['DnsConfig']['NamespaceId'])['Namespace']
    if data['Name'] != 'backend' or namespace['Name'] != 'services.production.yeodam.internal':
        raise ValueError('Unexpected private backend DNS')


def save(path, journal):
    path.write_text(json.dumps(journal, indent=2) + '\n')


def wait_backend(revision):
    for _ in range(90):
        row = service(BACKEND)
        if row['taskDefinition'] != revision:
            raise RuntimeError('Backend revision changed concurrently; inspect before rollback')
        if production.common.ai.stable(row, revision):
            production.verify_targets('backend', row)
            return
        if any(d.get('rolloutState') == 'FAILED' for d in row.get('deployments', [])):
            raise RuntimeError('Backend rollout failed; inspect circuit breaker and journal')
        time.sleep(10)
    raise TimeoutError('Backend rollout unresolved; inspect journal before another mutation')


def configure(apply=False, journal_path=None):
    row = service(BACKEND)
    ensure_idle(row)
    verify_discovery(row)
    current = definition(row['taskDefinition'])
    candidate = patched_definition(current)
    result = {'image': current['containerDefinitions'][0]['image'], 'previousTaskDefinition': row['taskDefinition'],
              'status': 'planned', 'tokenExists': token_metadata(), 'channel': CHANNEL}
    if not apply:
        return result
    if not result['tokenExists']:
        raise ValueError('Prepare the SecureString token before backend rollout')
    if journal_path is None:
        raise ValueError('--journal is required for a real rollout')
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    journal = result | {'status': 'registering', 'schemaVersion': 1}
    save(journal_path, journal)
    try:
        if service(BACKEND)['taskDefinition'] != row['taskDefinition']:
            raise RuntimeError('Backend changed during preflight')
        new = aws('ecs', 'register-task-definition', payload=candidate)['taskDefinition']['taskDefinitionArn']
        journal.update(status='backend-configuring', taskDefinition=new)
        save(journal_path, journal)
        if service(BACKEND)['taskDefinition'] != row['taskDefinition']:
            raise RuntimeError('Backend changed before UpdateService')
        aws('ecs', 'update-service', '--cluster', CLUSTER, '--service', BACKEND, '--task-definition', new)
        wait_backend(new)
        journal['status'] = 'backend-configured'
        save(journal_path, journal)
    except Exception:
        journal['status'] = 'review-required'
        save(journal_path, journal)
        raise
    return journal


def rollback(path, apply):
    journal = json.loads(path.read_text())
    if journal.get('schemaVersion') != 1 or journal.get('status') not in {'backend-configured', 'backend-configuring', 'review-required'}:
        raise ValueError('Journal is not an eligible backend operation')
    old, new = journal['previousTaskDefinition'], journal.get('taskDefinition')
    definition(old)  # Exact old family/image; never accept another service's ARN.
    row = service(BACKEND)
    if row['taskDefinition'] not in {old, new}:
        raise ValueError('Another backend revision is active; do not overwrite it')
    if service(WEBSOCKET)['desiredCount'] != 0 or service(WEBSOCKET)['runningCount'] != 0:
        raise ValueError('Stop WebSocket and wait for zero running tasks before backend rollback')
    if not apply:
        return {'status': 'rollback-planned', 'taskDefinition': old}
    if row['taskDefinition'] != old:
        aws('ecs', 'update-service', '--cluster', CLUSTER, '--service', BACKEND, '--task-definition', old)
        wait_backend(old)
    journal['status'] = 'backend-restored'
    save(path, journal)
    return journal


def smoke():
    results = {}
    for path, expected in [('/api/ws', 401), ('/api/internal/websocket/authentication', 404)]:
        request = urllib.request.Request('https://yeodam-2gether.com' + path,
                                         headers={'Origin': 'https://yeodam-2gether.com'})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        results[path] = status
        if status != expected:
            raise RuntimeError('Unexpected unauthenticated response for ' + path + ': ' + str(status))
    return {'status': 'unauthenticated-smoke-passed', 'responses': results,
            'authenticatedStomp': 'not-tested'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect', 'prepare-token', 'configure-backend', 'rollback-backend', 'smoke'])
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--journal', type=Path)
    args = parser.parse_args()
    identity()
    if args.action == 'prepare-token':
        exists = token_metadata()
        if args.apply and not exists:
            aws('ssm', 'put-parameter', payload={'Name': TOKEN, 'Type': 'SecureString',
                'Tier': 'Standard', 'Overwrite': False, 'Value': secrets.token_urlsafe(48)})
        result = {'status': 'token-present' if exists or args.apply else 'token-create-planned'}
    elif args.action == 'configure-backend':
        result = configure(args.apply, args.journal)
    elif args.action == 'rollback-backend':
        if not args.journal:
            raise ValueError('--journal is required')
        result = rollback(args.journal, args.apply)
    elif args.action == 'smoke':
        result = smoke()
    else:
        row = service(BACKEND)
        current = definition(row['taskDefinition'])
        result = {'backendStable': production.common.ai.stable(row, row['taskDefinition']),
                  'backendImage': current['containerDefinitions'][0]['image'],
                  'tokenExists': token_metadata(), 'registryCount': len(row.get('serviceRegistries', []))}
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, TimeoutError, OSError, KeyError) as error:
        raise SystemExit(str(error))
