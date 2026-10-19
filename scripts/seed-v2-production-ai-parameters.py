#!/usr/bin/env python3
"""Seed the initial production RunPod pair without exposing or rotating values."""
import json
import os
import re
import subprocess
import tempfile

REGION = 'ap-northeast-2'
PREFIX = '/yeodam/v2/production/ai/'
NAMES = ('VLM_BASE_URL', 'VLM_API_KEY')


def aws(*args, optional=False):
    result = subprocess.run(['aws', '--region', REGION, '--output', 'json', *args],
                            capture_output=True, text=True)
    if result.returncode:
        if optional and 'ParameterNotFound' in result.stderr:
            return None
        raise RuntimeError('AWS operation failed: ' + '/'.join(args[:2]))
    return json.loads(result.stdout) if result.stdout.strip() else {}


def parameter(name):
    return aws('ssm', 'get-parameter', '--name', name, '--with-decryption', optional=True)


def seed():
    os.umask(0o077)
    if aws('sts', 'get-caller-identity')['Account'] != '483175530259':
        raise RuntimeError('Unexpected AWS account')
    for path in (PREFIX + 'API_KEY', PREFIX + 'REDIS_URL',
                 '/yeodam/v2/staging/ai/EMBEDDING_API_KEY'):
        if parameter(path) is None:
            raise RuntimeError('Required parameter is missing: ' + path)
    existing = [parameter(PREFIX + name) for name in NAMES]
    if all(existing):
        print(json.dumps({'status': 'preserved', 'names': list(NAMES)}))
        return
    if any(existing):
        raise RuntimeError('Partial production RunPod pair exists; review it before seeding')
    source = [parameter('/yeodam/v2/staging/ai/' + name) for name in NAMES]
    if not all(source):
        raise RuntimeError('Staging RunPod pair is incomplete')
    url, key = [item['Parameter']['Value'] for item in source]
    if not re.fullmatch(r'https://[a-z0-9-]+\.proxy\.runpod\.net/v1', url) or not key:
        raise RuntimeError('Unexpected RunPod URL or empty credential')
    # No overwrite: a concurrent registration must fail rather than replace a value.
    for name, value in zip(NAMES, (url, key)):
        with tempfile.NamedTemporaryFile(mode='w') as request:
            json.dump({'Name': PREFIX + name, 'Type': 'String' if name == 'VLM_BASE_URL'
                       else 'SecureString', 'Value': value,
                       'Description': 'Initial production RunPod configuration; managed outside Terraform'},
                      request)
            request.flush()
            result = aws('ssm', 'put-parameter', '--cli-input-json', 'file://' + request.name)
        print(json.dumps({'name': PREFIX + name, 'version': result['Version']}))


if __name__ == '__main__':
    seed()
