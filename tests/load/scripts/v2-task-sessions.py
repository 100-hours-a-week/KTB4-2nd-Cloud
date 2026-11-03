#!/usr/bin/env python3
"""Run only on the V2 staging generator; secrets stay in memory/on that host."""
import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import time
import uuid

BASE = 'https://staging.yeodam-2gether.com'
PREFIX = 'yeodam:v2:staging:auth:'
HOST = 'master.yeodam-v2-staging-redis.mzwqmx.apn2.cache.amazonaws.com'


def credential(service, action, *args):
    result = subprocess.run(['aws', service, action, '--region', 'ap-northeast-2', *args],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Credential retrieval failed; sensitive output suppressed')
    return json.loads(result.stdout)


class Redis:
    def __init__(self, password):
        self.socket = ssl.create_default_context().wrap_socket(
            socket.create_connection((HOST, 6379), 5), server_hostname=HOST)
        self.socket.settimeout(5)
        self.file = self.socket.makefile('rb')
        self.command('AUTH', password)

    def read(self):
        line = self.file.readline()
        if not line: raise RuntimeError('Redis connection closed')
        kind, value = line[:1], line[1:-2]
        if kind == b'-': raise RuntimeError('Redis command rejected; response withheld')
        if kind == b'+': return value.decode()
        if kind == b':': return int(value)
        if kind == b'$':
            n = int(value)
            if n == -1: return None
            result = self.file.read(n); self.file.read(2)
            return result.decode()
        if kind == b'*': return [self.read() for _ in range(int(value))]
        raise RuntimeError('Unknown Redis protocol response')

    def command(self, *args):
        parts = [str(arg).encode() for arg in args]
        self.socket.sendall(b'*%d\r\n' % len(parts) + b''.join(
            b'$%d\r\n' % len(p) + p + b'\r\n' for p in parts))
        return self.read()


def encoded(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def jwt(user, sid, secret, now):
    header = encoded(b'{"alg":"HS256"}')
    body = encoded(json.dumps({'iss': BASE, 'aud': ['yeodam-api'], 'sub': str(user),
        'sid': sid, 'iat': now, 'exp': now + 1800}, separators=(',', ':')).encode())
    content = header + '.' + body
    return content + '.' + encoded(hmac.new(secret.encode(), content.encode(), hashlib.sha256).digest())


def owned(pool, run):
    if pool.get('issue') != 474 or pool.get('runId') != run or pool.get('baseUrl') != BASE:
        raise ValueError('Not a #474 pool')
    if len(pool.get('sessions', [])) != 100: raise ValueError('Expected exactly 100 sessions')
    for entry in pool['sessions']:
        if str(uuid.UUID(entry['sid'])) != entry['sid'] or int(entry['userId']) <= 0:
            raise ValueError('Invalid session identity')


def keys(entry):
    return [PREFIX + 'session:' + entry['sid'], PREFIX + 'refresh:' + entry['refreshHash'],
            PREFIX + 'user-sessions:' + str(entry['userId']),
            PREFIX + 'csrf:' + hashlib.sha256(entry['csrfContext'].encode()).hexdigest()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'audit', 'revoke'])
    parser.add_argument('--run-id', required=True, type=uuid.UUID)
    parser.add_argument('--users', type=Path)
    args = parser.parse_args()
    run = str(args.run_id)
    path = Path('/opt/yeodam-load/source/tests/load/private') / (run + '.json')
    if args.action == 'prepare' and path.exists(): raise ValueError('Pool already exists; refuse overwrite')
    password = credential('secretsmanager', 'get-secret-value', '--secret-id',
                          'yeodam-v2-staging/redis-auth-token')['SecretString']
    redis = Redis(password)
    if args.action == 'prepare':
        users = json.loads(args.users.read_text())
        if len(users) != 100 or len(set(users)) != 100 or any(type(i) is not int or i <= 0 for i in users):
            raise ValueError('Expected 100 distinct synthetic user IDs')
        secret = credential('ssm', 'get-parameter', '--name',
            '/yeodam/v2/staging/app/JWT_SECRET', '--with-decryption')['Parameter']['Value']
        now = int(time.time()); end = (now + 1800) * 1000
        sessions = []
        for user in users:
            sid = str(uuid.uuid4()); context = str(uuid.uuid4())
            sessions.append({'userId': user, 'sid': sid, 'csrfContext': context,
                'csrfToken': secrets.token_urlsafe(32), 'refreshHash': secrets.token_hex(32),
                'accessToken': jwt(user, sid, secret, now)})
        pool = {'version': 1, 'issue': 474, 'runId': run, 'baseUrl': BASE,
            'expiresAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 1800)),
            'expiresAtMs': end, 'sessions': sessions}
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output: json.dump(pool, output)
        # Save every identity before Redis writes, permitting scoped recovery after partial failure.
        for entry in sessions:
            session, refresh, index, csrf = keys(entry)
            if any(redis.command('EXISTS', k) for k in (session, refresh, csrf, index)):
                raise ValueError('Synthetic user/session key already exists; refuse merge')
            redis.command('HSET', session, 'userId', entry['userId'],
                          'refreshTokenHash', entry['refreshHash'], 'expiresAt', end)
            redis.command('PEXPIREAT', session, end)
            redis.command('SET', refresh, entry['sid'], 'PXAT', end)
            redis.command('ZADD', index, end, entry['sid'])
            redis.command('PEXPIREAT', index, end)
            redis.command('SET', csrf, entry['csrfToken'], 'PXAT', end)
        print(json.dumps({'prepared': len(sessions), 'expiresAt': pool['expiresAt']}))
    else:
        pool = json.loads(path.read_text()); owned(pool, run)
        if args.action == 'audit':
            mismatches = 0
            for entry in pool['sessions']:
                session, refresh, index, csrf = keys(entry)
                raw = redis.command('HGETALL', session)
                actual = dict(zip(raw[::2], raw[1::2]))
                expected = {'userId': str(entry['userId']), 'refreshTokenHash': entry['refreshHash'],
                            'expiresAt': str(pool['expiresAtMs'])}
                mismatches += int(actual != expected)
                mismatches += int(redis.command('GET', refresh) != entry['sid'])
                mismatches += int(redis.command('GET', csrf) != entry['csrfToken'])
                mismatches += int(redis.command('ZSCORE', index, entry['sid']) != str(pool['expiresAtMs']))
            print(json.dumps({'sessions': 100, 'keys': 400, 'mismatches': mismatches}))
            if mismatches: raise RuntimeError('Redis audit failed')
        else:
            for entry in pool['sessions']:
                session, refresh, index, csrf = keys(entry)
                redis.command('DEL', session, refresh, csrf)
                redis.command('ZREM', index, entry['sid'])
            path.unlink()
            print(json.dumps({'revoked': 100, 'poolFileRemoved': True}))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        raise SystemExit(type(error).__name__ + ': preparation/audit failed; credential output withheld')
