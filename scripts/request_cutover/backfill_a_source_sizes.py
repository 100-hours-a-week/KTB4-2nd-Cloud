#!/usr/bin/env python3
"""Backfill only existing staging V1 attachment sizes from S3 HEAD metadata."""
import argparse
import json
import os
import re
import subprocess
from uuid import UUID

RUN = 'd7dae9c5-5808-4a63-9a35-96cec0659660'
REGION = 'ap-northeast-2'


def command(args, timeout=30):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError('staging preflight or S3 HEAD failed; inspect private host diagnostics')
    return result.stdout.strip()


def mysql(sql):
    return command(['mysql', '-uroot', '--protocol=socket', '--batch', '--skip-column-names',
                    'yeodam_v1_source', '-e', sql], timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-run')
    args = parser.parse_args()
    if (os.geteuid() != 0 or args.confirm_run != RUN or str(UUID(RUN)) != RUN
            or '10.30.0.174' not in command(['hostname', '-I']).split()
            or command(['aws', 'sts', 'get-caller-identity', '--query', 'Account', '--output', 'text']) != '483175530259'
            or mysql('SELECT @@server_id,@@read_only,@@super_read_only;') != '1\t0\t0'):
        raise ValueError('dedicated staging V1 Writer and matching Run required')
    backend = command(['docker', 'ps', '--filter', 'label=com.docker.compose.project=yeodam-app',
                       '--filter', 'label=com.docker.compose.service=backend', '--quiet'])
    if not backend or '\n' in backend:
        raise ValueError('expected one staging backend')
    bucket = command(['docker', 'exec', backend, 'sh', '-c', 'printf %s "$ATTACHMENT_S3_BUCKET"'])
    if bucket != 'yeodam-v2-stg-v1-app-483175530259-ap-northeast-2':
        raise ValueError('unexpected staging attachment bucket')
    sql = """SELECT DISTINCT f.file_id,f.object_key FROM files f
        JOIN trip_attachments a ON a.file_id=f.file_id
        JOIN trips t ON t.trip_id=a.trip_id
        WHERE t.processing_status='COMPLETED' AND t.deleted_at IS NULL
          AND a.deleted_at IS NULL AND f.deleted_at IS NULL
          AND f.original_size_bytes IS NULL ORDER BY f.file_id;"""
    raw = mysql(sql)
    rows = [] if not raw else [line.split('\t') for line in raw.splitlines()]
    if len(rows) > 100 or any(len(row) != 2 or not row[0].isdigit()
            or not re.fullmatch(r'[A-Za-z0-9_./-]+', row[1]) for row in rows):
        raise ValueError('unexpected legacy attachment candidate set')
    updates = []
    for file_id, key in rows:
        length = command(['aws', 's3api', 'head-object', '--region', REGION,
                          '--bucket', bucket, '--key', key, '--query', 'ContentLength',
                          '--output', 'text'], timeout=20)
        if not length.isdigit():
            raise ValueError('invalid S3 ContentLength')
        updates.append((int(file_id), key.encode().hex(), int(length)))
    if not args.apply:
        print(json.dumps({'candidates': len(updates), 'head_verified': len(updates), 'updated': 0}))
        return
    if not updates:
        print(json.dumps({'candidates': 0, 'updated': 0}))
        return
    cases = ' '.join(f'WHEN {file_id} THEN {length}' for file_id, _, length in updates)
    matches = ' OR '.join(f'(file_id={file_id} AND object_key=0x{key_hex})'
                          for file_id, key_hex, _ in updates)
    result = mysql('UPDATE files SET original_size_bytes=CASE file_id ' + cases
                   + ' END WHERE original_size_bytes IS NULL AND (' + matches
                   + '); SELECT ROW_COUNT();')
    count = int(result.splitlines()[-1])
    if count != len(updates):
        raise ValueError('conditional update count differs from S3 HEAD candidate count')
    print(json.dumps({'candidates': len(updates), 'head_verified': len(updates),
                      'updated': count, 'source': 'actual_s3_head'}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'result': 'failed', 'error_type': type(error).__name__,
                          'message': str(error) if isinstance(error, ValueError) else 'inspect private host diagnostics'}))
        raise SystemExit(1)
