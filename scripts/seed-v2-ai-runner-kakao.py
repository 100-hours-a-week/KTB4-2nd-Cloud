#!/usr/bin/env python3
"""Copy the current BE Kakao client ID to runner-readable SSM, without rotation.

Default is read-only. --apply creates only a missing Parameter; never overwrites.
No values, ciphertext or hashes of credentials are printed or persisted in Git.
"""
import argparse
import json
import os
import subprocess
import tempfile

ACCOUNT = '483175530259'


def aws(*args, optional=False):
    result = subprocess.run(['aws', '--region', 'ap-northeast-2', '--output', 'json', *args],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        if optional and 'ParameterNotFound' in result.stderr:
            return None
        raise RuntimeError('Kakao configuration lookup/write failed: ' + args[0] + '/' + args[1])
    return json.loads(result.stdout)


def source_value(environment):
    service = f'yeodam-v2-{environment}-backend'
    row = aws('ecs', 'describe-services', '--cluster', f'yeodam-v2-{environment}-app', '--services', service)
    if row.get('failures') or len(row['services']) != 1:
        raise RuntimeError('Backend service not found')
    definition = aws('ecs', 'describe-task-definition', '--task-definition', row['services'][0]['taskDefinition'])
    containers = [c for c in definition['taskDefinition']['containerDefinitions'] if c['name'] == 'backend']
    if len(containers) != 1:
        raise RuntimeError('Expected exactly one Backend container')
    values = [v['value'] for v in containers[0].get('environment', []) if v['name'] == 'KAKAO_CLIENT_ID']
    secrets = [v['valueFrom'] for v in containers[0].get('secrets', []) if v['name'] == 'KAKAO_CLIENT_ID']
    if not values and len(secrets) == 1:
        expected = f'arn:aws:ssm:ap-northeast-2:{ACCOUNT}:parameter/yeodam/v2/{environment}/app/KAKAO_CLIENT_ID'
        if secrets[0] != expected:
            raise ValueError('Unexpected Backend Kakao Parameter reference')
        values = [aws('ssm', 'get-parameter', '--name', secrets[0], '--with-decryption')['Parameter']['Value']]
    elif secrets:
        raise ValueError('Ambiguous Backend Kakao sources')
    if len(values) != 1 or not values[0] or any(c in values[0] for c in '\r\n\x00'):
        raise ValueError('Missing or invalid Backend Kakao client ID')
    return values[0]


def seed(environment, apply=False):
    if environment not in {'staging', 'production'}:
        raise ValueError('Unexpected environment')
    if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise ValueError('Wrong AWS account')
    name = f'/yeodam/v2/{environment}/app/KAKAO_CLIENT_ID'
    value = source_value(environment)
    current = aws('ssm', 'get-parameter', '--name', name, '--with-decryption', optional=True)
    if current:
        if current['Parameter']['Value'] != value:
            raise RuntimeError('Existing Kakao Parameter differs from Backend; refusing to overwrite')
        return {'name': name, 'status': 'preserved', 'version': current['Parameter']['Version']}
    if not apply:
        return {'name': name, 'status': 'missing', 'proposedAction': 'create SecureString from current Backend'}
    if value != source_value(environment):
        raise RuntimeError('Backend Kakao setting changed during preparation; retry')
    os.umask(0o077)
    with tempfile.NamedTemporaryFile(mode='w') as request:
        json.dump({'Name': name, 'Type': 'SecureString', 'Value': value,
                   'Description': 'Existing BE Kakao client ID for AI runner; no rotation'}, request)
        request.flush()
        result = aws('ssm', 'put-parameter', '--cli-input-json', 'file://' + request.name)
    return {'name': name, 'status': 'created', 'version': result['Version']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--environment', choices=['staging', 'production'], required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(seed(args.environment, args.apply)))
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
