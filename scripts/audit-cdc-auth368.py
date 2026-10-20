#!/usr/bin/env python3
"""Compare the two V1 authentication tables with a run-scoped Redis projection.

Run on the isolated Source App after the dedicated Kafka consumer reaches lag 0.
Authentication values are held in memory and never printed.
"""
import argparse
import json
import os
import subprocess
from pathlib import Path

from cdc_auth368 import _expiry_ms


QUERY = """START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT 'S',sid,user_id,refresh_token_hash,
  DATE_FORMAT(expires_at,'%Y-%m-%dT%H:%i:%s.%f') FROM yeodam_v1_source.login_sessions;
SELECT 'C',browser_context_hash,token_value,
  DATE_FORMAT(expires_at,'%Y-%m-%dT%H:%i:%s.%f') FROM yeodam_v1_source.csrf_tokens;
COMMIT;"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    prefix = config['redis']['prefix']
    if prefix != 'yeodam:cutover:' + config['run_id'] + ':':
        raise ValueError('run prefix mismatch')
    import redis
    section = config['redis']
    client = redis.Redis(host=section['host'], port=section.get('port', 6379),
                         password=os.environ[section['password_env']], ssl=True,
                         decode_responses=True, socket_timeout=5)
    seconds, micros = client.time()
    now_ms = seconds * 1000 + micros // 1000
    result = subprocess.run(['mysql', '--protocol=socket', '--user=root', '--batch',
                             '--raw', '--skip-column-names', '-e', QUERY],
                            capture_output=True, text=True, check=True)
    expected = set()
    expected_index = {}
    rows = {'sessions': 0, 'csrf': 0}
    mismatches = {'sessions': 0, 'refresh': 0, 'index': 0, 'csrf': 0,
                  'extra': 0}
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if parts[0] == 'S' and len(parts) == 5:
            rows['sessions'] += 1
            _, sid, user_id, refresh_hash, expiry = parts
            end_ms = _expiry_ms(expiry, config['source_timezone'])
            if end_ms <= now_ms:
                continue
            session_key = prefix + 'session:' + sid
            refresh_key = prefix + 'refresh:' + refresh_hash
            index_key = prefix + 'user-sessions:' + user_id
            expected.update((session_key, refresh_key, index_key))
            expected_index.setdefault(index_key, {})[sid] = end_ms
            if client.hgetall(session_key) != {
                'userId': user_id, 'refreshTokenHash': refresh_hash,
                'expiresAt': str(end_ms),
            } or client.pexpiretime(session_key) != end_ms:
                mismatches['sessions'] += 1
            if client.get(refresh_key) != sid or client.pexpiretime(refresh_key) != end_ms:
                mismatches['refresh'] += 1
        elif parts[0] == 'C' and len(parts) == 4:
            rows['csrf'] += 1
            _, context_hash, token_value, expiry = parts
            end_ms = _expiry_ms(expiry, config['source_timezone'])
            if end_ms <= now_ms:
                continue
            csrf_key = prefix + 'csrf:' + context_hash
            expected.add(csrf_key)
            if client.get(csrf_key) != token_value or client.pexpiretime(csrf_key) != end_ms:
                mismatches['csrf'] += 1
        else:
            raise ValueError('unexpected Source row shape')
    for key, members in expected_index.items():
        actual = {sid: int(score) for sid, score in
                  client.zrange(key, 0, -1, withscores=True)}
        if actual != members or client.pexpiretime(key) != max(members.values()):
            mismatches['index'] += 1
    actual_keys = set(client.scan_iter(match=prefix + '*', count=500))
    mismatches['extra'] = len(actual_keys - expected)
    report = {'source_rows': rows, 'expected_keys': len(expected),
              'actual_keys': len(actual_keys), 'mismatches': mismatches,
              'equal': not any(mismatches.values()) and actual_keys == expected}
    print(json.dumps(report, sort_keys=True))
    if not report['equal']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
