#!/usr/bin/env python3
"""Create a NEW V3-only bootstrap Secret. No rotation, updates or Terraform payloads."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

NAME = 'yeodam/v3/foundation/redis/bootstrap'
ACCOUNT = '483175530259'
REGION = 'ap-northeast-2'


def create_payload(directory):
    """Private temporary directory only; caller must remove it. Never log payload."""
    directory = Path(directory)
    os.chmod(directory, 0o700)
    def openssl(*args):
        subprocess.run(['openssl', *args], cwd=directory, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    openssl('req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '3650',
            '-subj', '/CN=yeodam-v3-redis-ca', '-keyout', 'ca.key', '-out', 'ca.crt',
            '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes',
            '-subj', '/CN=yeodam-v3-app-redis', '-keyout', 'tls.key', '-out', 'server.csr')
    hosts = ['redis-members.redis.svc.cluster.local', 'redis-sentinel.redis.svc.cluster.local']
    hosts += [f'redis-{i}.redis-members.redis.svc.cluster.local' for i in range(2)]
    hosts += [f'sentinel-{i}.redis-sentinel.redis.svc.cluster.local' for i in range(3)]
    (directory / 'server.ext').write_text('basicConstraints=critical,CA:FALSE\n'
        'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\n'
        'subjectAltName=IP:127.0.0.1,' + ','.join('DNS:' + host for host in hosts) + '\n')
    openssl('x509', '-req', '-in', 'server.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key',
            '-CAcreateserial', '-out', 'tls.crt', '-days', '365', '-sha256', '-extfile', 'server.ext')
    payload = {key: secrets.token_hex(24) for key in
               ('adminPassword', 'appPassword', 'replicationPassword', 'monitorPassword',
                'sentinelPeerPassword', 'sentinelClientPassword', 'healthPassword')}
    payload.update({key: (directory / filename).read_text() for key, filename in
                    [('tlsCa', 'ca.crt'), ('tlsCert', 'tls.crt'), ('tlsKey', 'tls.key')]})
    for path in directory.iterdir():
        path.chmod(0o600)
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--create-new', action='store_true', required=True)
    parser.add_argument('--profile', default='yeodam-admin')
    args = parser.parse_args()
    os.umask(0o077)
    aws = ['aws', '--profile', args.profile, '--region', REGION]
    def call(*command):
        return subprocess.run(aws + list(command), capture_output=True, text=True)
    identity = call('sts', 'get-caller-identity', '--output', 'json')
    if identity.returncode or json.loads(identity.stdout)['Account'] != ACCOUNT:
        raise SystemExit('AWS identity unavailable or account mismatch')
    probe = call('secretsmanager', 'describe-secret', '--secret-id', NAME)
    if probe.returncode == 0:
        raise SystemExit('Secret already exists: refusing overwrite or rotation')
    if 'ResourceNotFoundException' not in probe.stderr:
        raise SystemExit('Secret existence check failed; no write attempted')
    with tempfile.TemporaryDirectory(prefix='v3-redis-secret-') as private:
        payload = create_payload(private)
        path = Path(private) / 'payload.json'
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
        result = call('secretsmanager', 'create-secret', '--name', NAME,
                      '--description', 'V3 Redis initial credentials and TLS; controlled rotation only',
                      '--secret-string', 'file://' + str(path), '--tags',
                      'Key=Project,Value=yeodam', 'Key=Version,Value=v3', 'Key=Issue,Value=523')
        if result.returncode:
            raise SystemExit('Create failed; payload and AWS error output suppressed')
    print('Created new V3 Redis bootstrap Secret; no values printed')


if __name__ == '__main__':
    main()
