#!/usr/bin/env python3
"""Read only: export one trip-create run from the isolated staging Source or Target."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID


SCHEMA = 'yeodam_v1_source'


def run_prefix(run_id):
    if str(UUID(run_id)) != run_id:
        raise ValueError('Use a canonical lowercase run UUID')
    return 't' + hashlib.sha256(run_id.encode('ascii')).hexdigest()[:5]


def mysql_command(role):
    if role == 'source':
        return ['mysql', '--protocol=socket', '-uroot', '--database=' + SCHEMA,
                '--batch', '--raw', '--skip-column-names', '--quick']
    return ['docker', 'exec', '-i', 'yeodam-v2-mysql', 'sh', '-ec',
            'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; '
            'exec mysql -uroot --database=yeodam_v1_source --batch --raw --skip-column-names --quick']


def query(role, statement):
    result = subprocess.run(mysql_command(role), input=statement, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError('Staging MySQL read failed; preserve the run and check the host locally')
    return result.stdout.strip()


def verify(role):
    actual = query(role, 'SELECT @@server_id, @@read_only, @@super_read_only;')
    server = '1' if role == 'source' else '2'
    if not actual.startswith(server + '\t'):
        raise RuntimeError('Unexpected Source/Target server_id')
    return actual


def export(role, run_id):
    prefix = run_prefix(run_id)
    output_dir = Path('/run/yeodam-migration-trip')
    output_dir.mkdir(mode=0o700, exist_ok=True)
    output = output_dir / f'{run_id}.{role}.tsv.gz'
    if output.exists():
        raise RuntimeError('Export exists; retain it and use the same run evidence')
    # A LEFT JOIN retains a trip whose region is missing so reconciliation fails visibly.
    statement = (
        'START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY; '
        'SELECT t.trip_id,t.user_id,t.trip_name,t.start_date,t.end_date,t.processing_status,'
        'r.region_id,r.region_code,HEX(r.region_name),r.latitude,r.longitude '
        'FROM trips t LEFT JOIN trip_regions r ON r.trip_id=t.trip_id '
        f"WHERE t.trip_name LIKE '{prefix}%' ORDER BY t.trip_id,r.region_id; COMMIT;"
    )
    rows = 0
    try:
        with output.open('xb') as destination:
            process = subprocess.Popen(mysql_command(role), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            process.stdin.write(statement.encode('ascii'))
            process.stdin.close()
            with gzip.GzipFile(fileobj=destination, mode='wb') as compressed:
                for line in process.stdout:
                    compressed.write(line)
                    rows += 1
            error = process.stderr.read()
            code = process.wait()
        if code:
            raise RuntimeError('Trip export failed; inspect staging MySQL locally (stderr bytes=' + str(len(error)) + ')')
    except Exception:
        output.unlink(missing_ok=True)
        raise
    output.chmod(0o600)
    return {'runId': run_id, 'role': role, 'prefix': prefix, 'rows': rows, 'export': str(output)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'export'))
    parser.add_argument('--role', required=True, choices=('source', 'target'))
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run as root on the isolated staging DB host')
    prefix = run_prefix(args.run_id)
    state = verify(args.role)
    if args.action == 'check':
        count = int(query(args.role, f"SELECT COUNT(*) FROM trips WHERE trip_name LIKE '{prefix}%';"))
        if count:
            raise RuntimeError(f'Run prefix already has {count} trips; choose a new run UUID')
        result = {'runId': args.run_id, 'role': args.role, 'prefix': prefix,
                  'serverState': state, 'existingTrips': count}
    else:
        result = export(args.role, args.run_id)
        result['serverState'] = state
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
