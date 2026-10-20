#!/usr/bin/env python3
"""Measure a live SQL auth snapshot against an isolated, one-time Redis copy."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

RUNTIME = Path('/var/lib/yeodam/a-rehearsal-358')
CONFIG = RUNTIME / 'gate.json'
PREFIX = 'yeodam:cutover:'
SOURCE_PASSWORD = '/yeodam/v2/staging/v1-source/app/MYSQL_PASSWORD'
REDIS_SECRET = 'yeodam-v2-staging/redis-auth-token'
TARGET = '10.30.10.28'


def credential(*args):
    command = ['aws', *args, '--region', 'ap-northeast-2', '--output', 'text',
               '--no-cli-pager']
    result = subprocess.run(command, capture_output=True, text=True, check=False,
                            timeout=15)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError('Scoped credential read failed; no authentication data changed')
    return result.stdout.strip()


def load_runtime(run_id, database):
    if str(UUID(run_id)) != run_id:
        raise ValueError('Canonical Run UUID required')
    config = json.loads(CONFIG.read_text())
    if (config['environment'] != 'staging-a-rehearsal' or
            config['mysql']['database'] != 'yeodam_v1_source' or
            config['mysql']['host'] != '127.0.0.1' or
            config['mysql'].get('port') != 16033 or
            config['redis']['prefix'] != PREFIX + config['run_id'] + ':'):
        raise ValueError('Unexpected rehearsal runtime scope')
    config['run_id'] = run_id
    config['redis']['prefix'] = PREFIX + run_id + ':'
    if database == 'target':
        config['mysql']['host'] = TARGET
        config['mysql']['port'] = 3306
    os.environ[config['mysql']['password_env']] = credential(
        'ssm', 'get-parameter', '--name', SOURCE_PASSWORD,
        '--with-decryption', '--query', 'Parameter.Value')
    os.environ[config['redis']['password_env']] = credential(
        'secretsmanager', 'get-secret-value', '--secret-id', REDIS_SECRET,
        '--query', 'SecretString')
    sys.path.insert(0, str(RUNTIME))
    from request_cutover import auth, control
    return config, auth, control


def current_writer(control, config, database):
    with control.database(config) as db, db.cursor() as cursor:
        cursor.execute('SELECT @@server_id AS id, @@read_only AS ro, '
                       '@@super_read_only AS sro')
        state = cursor.fetchone()
    expected_id = 1 if database == 'source' else 2
    if (state['id'], state['ro'], state['sro']) != (expected_id, 0, 0):
        raise ValueError('Requested database is not the current Writer')


def redis_entries(client, auth, prefix, now_ms):
    keys = sorted(client.scan_iter(match=prefix + '*', count=500))
    if not keys:
        return {}
    with client.pipeline(transaction=False) as pipe:
        for key in keys:
            pipe.type(key)
            pipe.pexpiretime(key)
        metadata = pipe.execute()
    active = [(key, metadata[i], int(metadata[i + 1]))
              for n, key in enumerate(keys) for i in [2 * n]
              if metadata[i] != 'none' and int(metadata[i + 1]) > now_ms]
    with client.pipeline(transaction=False) as pipe:
        for key, kind, _ in active:
            if kind == 'hash':
                pipe.hgetall(key)
            elif kind == 'zset':
                pipe.zrange(key, 0, -1, withscores=True)
            elif kind == 'string':
                pipe.get(key)
            else:
                raise ValueError('Unexpected Redis authentication key type')
        values = pipe.execute()
    result = {}
    for (key, kind, end), value in zip(active, values):
        if kind == 'zset':
            value = {member: int(score) for member, score in value}
        result[key] = auth.Entry(kind, value, end)
    return result


def kind_counts(keys, prefix):
    result = Counter()
    for key in keys:
        suffix = key.removeprefix(prefix)
        result[suffix.split(':', 1)[0]] += 1
    return dict(sorted(result.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'snapshot', 'compare'))
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--database', choices=('source', 'target'), default='source')
    parser.add_argument('--checkpoint', required=True)
    args = parser.parse_args()
    if not args.checkpoint.replace('-', '').isalnum():
        raise ValueError('Checkpoint label must be alphanumeric')
    if args.action == 'snapshot' and args.database != 'source':
        raise ValueError('T0 snapshot must read the Source Writer')
    config, auth, control = load_runtime(args.run_id, args.database)
    current_writer(control, config, args.database)
    client = control.redis_client(config)
    prefix = config['redis']['prefix']
    if args.action == 'preflight':
        if not client.ping():
            raise ValueError('Rehearsal Redis is unavailable')
        print(json.dumps({'checkpoint': args.checkpoint, 'action': 'preflight',
                          'database': args.database, 'prefixKeys': sum(1 for _ in
                              client.scan_iter(match=prefix + '*', count=500)),
                          'observedAt': datetime.now(timezone.utc).isoformat()}))
        return
    if args.action == 'snapshot':
        if any(client.scan_iter(match=prefix + '*', count=1)):
            raise ValueError('Run Redis namespace is not empty; preserve it')
        # This is deliberately a T0 observation while V1 writes continue.
        proof = control.transfer(config)
        print(json.dumps({'checkpoint': args.checkpoint, 'action': 'snapshot',
                          'database': args.database, 'keys': proof['keys'],
                          'fingerprint': proof['target_fingerprint'],
                          'observedAt': datetime.now(timezone.utc).isoformat()}))
        return
    with control.database(config) as db:
        rows, source_now = auth.snapshot(db)
    seconds, micros = client.time()
    now_ms = seconds * 1000 + micros // 1000
    if abs(auth.expiry(source_now, config['source_timezone']) - now_ms) > 2000:
        raise ValueError('SQL and Redis clocks differ by over two seconds')
    expected = auth.project(rows, prefix, config['source_timezone'], now_ms,
                            'migration-' + args.run_id)
    actual = redis_entries(client, auth, prefix, now_ms)
    missing = expected.keys() - actual.keys()
    extra = actual.keys() - expected.keys()
    changed = {key for key in expected.keys() & actual.keys()
               if expected[key] != actual[key]}
    print(json.dumps({'checkpoint': args.checkpoint, 'action': 'compare',
                      'database': args.database, 'observedAt': datetime.now(timezone.utc).isoformat(),
                      'sqlRows': {name: len(value) for name, value in rows.items()},
                      'expectedKeys': len(expected), 'redisKeys': len(actual),
                      'missing': len(missing), 'extra': len(extra), 'changed': len(changed),
                      'missingKinds': kind_counts(missing, prefix),
                      'extraKinds': kind_counts(extra, prefix),
                      'changedKinds': kind_counts(changed, prefix),
                      'sqlFingerprint': auth.fingerprint(expected),
                      'redisFingerprint': auth.fingerprint(actual)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'result': 'failed', 'errorType': type(error).__name__,
                          'detail': str(error) if isinstance(error, ValueError) else 'inspect private host diagnostics'}),
              file=sys.stderr)
        raise SystemExit(1)
