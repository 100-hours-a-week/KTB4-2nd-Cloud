#!/usr/bin/env python3
"""Copy checked V1 auth/catalog values into the new V2 namespace without rotation."""
import json
import os
import subprocess
import tempfile

REGION = 'ap-northeast-2'
PREFIX = '/yeodam/v2/production/app/'
NAMES = ('JWT_SECRET', 'KAKAO_CLIENT_ID', 'KAKAO_CLIENT_SECRET',
         'DATA_GO_KR_SERVICE_KEY', 'DATA_GO_KR_LEGAL_DISTRICT_API_URL', 'DATA_GO_KR_TIMEOUT')


def aws(*args, optional=False):
    result = subprocess.run(['aws', '--region', REGION, '--output', 'json', *args],
                            capture_output=True, text=True)
    if result.returncode:
        if optional and 'ParameterNotFound' in result.stderr:
            return None
        raise RuntimeError('AWS operation failed: ' + '/'.join(args[:2]))
    return json.loads(result.stdout) if result.stdout.strip() else {}


def parameter(path):
    response = aws('ssm', 'get-parameter', '--name', path, '--with-decryption', optional=True)
    return response['Parameter'] if response else None


def seed():
    os.umask(0o077)
    if aws('sts', 'get-caller-identity')['Account'] != '483175530259':
        raise RuntimeError('Unexpected AWS account')
    for path in (PREFIX + 'MYSQL_PASSWORD', '/yeodam/v2/production/ai/API_KEY'):
        if parameter(path) is None:
            raise RuntimeError('Required production credential missing: ' + path)
    pending = []
    # Review every source/destination before any write. Never rotate the V1 namespace.
    for name in NAMES:
        source = parameter('/yeodam/v1/app/' + name)
        if source is None or not source['Value']:
            raise RuntimeError('Required V1 value missing: ' + name)
        if name == 'JWT_SECRET' and len(source['Value'].encode()) < 32:
            raise RuntimeError('V1 JWT key is too short')
        existing = parameter(PREFIX + name)
        if existing is not None:
            if existing['Value'] != source['Value'] or existing['Type'] != source['Type']:
                raise RuntimeError('Existing production parameter differs; no overwrite: ' + name)
        else:
            pending.append((name, source))
    for name, source in pending:
        with tempfile.NamedTemporaryFile(mode='w') as request:
            json.dump({'Name': PREFIX + name, 'Type': source['Type'], 'Value': source['Value'],
                       'Description': 'Production V2 backend initial value copied from verified V1 configuration'}, request)
            request.flush()
            result = aws('ssm', 'put-parameter', '--cli-input-json', 'file://' + request.name)
        print(json.dumps({'name': PREFIX + name, 'version': result['Version']}))
    print(json.dumps({'created': len(pending), 'preserved': len(NAMES) - len(pending)}))


if __name__ == '__main__':
    seed()
