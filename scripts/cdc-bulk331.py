#!/usr/bin/env python3
"""One-use, three-table B1 bulk copy. Run on the Source or dedicated B1 host.

The caller starts a fresh no_data Debezium source and proves an ordered delta
event exists *before* capture. This script never changes that connector.
"""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID

BUCKET = 'yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2'
CASE = os.environ.get('CDC_LIVE_CASE', '331')
if CASE not in ('331', '331r', '331s'):
    raise RuntimeError('unsupported CDC Bulk case')
SCHEMA = 'yeodam_cdc_live' + CASE
SOURCE_SCHEMA = 'yeodam_v1_source'
TABLES = ('users', 'trips', 'trip_regions')
BASE = Path('/var/lib/yeodam/cdc-bulk331')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(command, *, stdin=None):
    result = subprocess.run(command, input=stdin, text=True, capture_output=True)
    require(result.returncode == 0, 'host command failed: ' + command[0])
    return result.stdout.strip()


def mysql_command(role, dump=False):
    tool = 'mysqldump' if dump else 'mysql'
    if role == 'source':
        return [tool, '--protocol=socket', '-uroot']
    return ['docker', 'exec', '-i', 'yeodam-mysql-cdc-test', 'sh', '-ec',
            'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; '
            'exec ' + tool + ' -uroot --default-character-set=utf8mb4 "$@"', 'sh']


def query(role, sql):
    return run(mysql_command(role) + ['--batch', '--skip-column-names'], stdin=sql)


def identity(role):
    columns = query(role, 'SELECT @@server_id, @@read_only, @@super_read_only;').split('\t')
    require(len(columns) == 3 and columns[0] == ('1' if role == 'source' else '300'),
            'unexpected MySQL host')
    require(columns[1:] == (['0', '0'] if role == 'source' else ['0', '0']),
            'unexpected MySQL read_only state')
    return columns


def key_prefix(run_id):
    require(str(UUID(run_id)) == run_id, 'canonical run UUID required')
    return f'migration/cdc-live331/{run_id}'


def paths(run_id):
    directory = BASE / run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(directory.stat().st_mode & 0o077 == 0, 'private directory is not private')
    return directory / 'snapshot.sql.gz', directory / 'snapshot.ready.json'


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def s3_upload(path, key):
    run(['aws', 's3', 'cp', str(path), f's3://{BUCKET}/{key}',
         '--region', 'ap-northeast-2', '--only-show-errors'])


def s3_download(key, path):
    run(['aws', 's3', 'cp', f's3://{BUCKET}/{key}', str(path),
         '--region', 'ap-northeast-2', '--only-show-errors'])


def assert_dump_shape(path):
    seen = []
    with gzip.open(path, 'rt', encoding='utf8') as file:
        for line in file:
            upper = line.upper()
            require('FOREIGN_KEY_CHECKS=0' not in upper.replace(' ', ''),
                    'dump disables FK checks; preserve it without importing')
            require(not re.match(r'^\s*(DROP|CREATE|ALTER|TRUNCATE|DELETE|REPLACE|USE)\b',
                                 line, re.IGNORECASE), 'unexpected dump statement')
            match = re.match(r'^INSERT INTO `([^`]+)` ', line)
            if match:
                table = match.group(1)
                require(table in TABLES, 'dump includes an unexpected table')
                if not seen or seen[-1] != table:
                    seen.append(table)
    require(seen == list(TABLES), 'dump table order/coverage is unexpected')


def capture(run_id):
    identity('source')
    require(query('source', "SELECT COUNT(*) FROM information_schema.tables "
                  "WHERE table_schema='yeodam_v1_source' AND table_name IN "
                  "('users','trips','trip_regions') AND engine='InnoDB';") == '3',
            'source tables are not the expected three InnoDB tables')
    archive, ready = paths(run_id)
    require(not archive.exists() and not ready.exists(), 'run already captured; inspect it')
    prefix = key_prefix(run_id)
    command = mysql_command('source', dump=True) + [
        '--single-transaction', '--quick', '--set-gtid-purged=OFF', '--hex-blob',
        '--order-by-primary', '--no-create-info', '--skip-triggers', '--compact',
        '--skip-add-locks', '--skip-disable-keys', '--skip-lock-tables',
        '--default-character-set=utf8mb4', SOURCE_SCHEMA, *TABLES]
    with archive.open('xb') as output:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressed:
            for chunk in iter(lambda: process.stdout.read(1024 * 1024), b''):
                compressed.write(chunk)
        error = process.stderr.read()
        require(process.wait() == 0, 'mysqldump failed (stderr bytes=' + str(len(error)) + ')')
    archive.chmod(0o600)
    assert_dump_shape(archive)
    marker = {'case': 'live' + CASE, 'runId': run_id, 'sourceServerId': 1,
              'sourceSchema': SOURCE_SCHEMA, 'destinationSchema': SCHEMA,
              'tables': list(TABLES), 'archiveKey': prefix + '/snapshot.sql.gz',
              'archiveBytes': archive.stat().st_size, 'archiveSha256': digest(archive),
              'mode': 'single-transaction; no-data CDC started before dump'}
    ready.write_text(json.dumps(marker, sort_keys=True) + '\n')
    ready.chmod(0o600)
    s3_upload(archive, marker['archiveKey'])
    s3_upload(ready, prefix + '/snapshot.ready.json')
    print(json.dumps({'runId': run_id, 'readyKey': prefix + '/snapshot.ready.json',
                      'archiveBytes': marker['archiveBytes'],
                      'archiveSha256': marker['archiveSha256']}))


def destination(run_id):
    identity('destination')
    prefix = key_prefix(run_id)
    archive, ready = paths(run_id)
    require(not archive.exists() and not ready.exists(), 'run already downloaded; inspect it')
    require(query('destination', "SELECT COUNT(*) FROM information_schema.tables "
                  f"WHERE table_schema='{SCHEMA}' AND table_name IN "
                  "('users','trips','trip_regions');") == '3',
            'B1 destination schema must already contain three tables')
    for table in TABLES:
        require(query('destination', f'SELECT COUNT(*) FROM {SCHEMA}.{table};') == '0',
                'B1 destination is not empty: ' + table)
    s3_download(prefix + '/snapshot.ready.json', ready)
    marker = json.loads(ready.read_text())
    require(marker.get('runId') == run_id and marker.get('case') == 'live' + CASE and
            marker.get('destinationSchema') == SCHEMA and
            marker.get('tables') == list(TABLES) and
            marker.get('archiveKey') == prefix + '/snapshot.sql.gz',
            'unexpected source snapshot marker')
    s3_download(marker['archiveKey'], archive)
    archive.chmod(0o600)
    ready.chmod(0o600)
    require(archive.stat().st_size == marker['archiveBytes'] and
            digest(archive) == marker['archiveSha256'], 'snapshot archive mismatch')
    assert_dump_shape(archive)
    # Preserve a partial import on failure for diagnosis; this run is one-use.
    process = subprocess.Popen(mysql_command('destination') + ['--database=' + SCHEMA],
                               stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    broken = False
    try:
        with gzip.open(archive, 'rb') as data:
            for chunk in iter(lambda: data.read(1024 * 1024), b''):
                process.stdin.write(chunk)
    except BrokenPipeError:
        broken = True
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError:
            broken = True
    error = process.stderr.read().decode(errors='replace')
    code = process.wait()
    if broken or code:
        match = re.search(r'ERROR (\d+) \(([^)]+)\) at line (\d+):', error)
        detail = (f' MySQL {match.group(1)}/{match.group(2)} line {match.group(3)}'
                  if match else f' stderrBytes={len(error)}')
        raise RuntimeError('B1 import failed; preserve partial run;' + detail)
    counts = {table: int(query('destination', f'SELECT COUNT(*) FROM {SCHEMA}.{table};'))
              for table in TABLES}
    require(all(counts.values()), 'B1 import has an empty table')
    orphans = {
        'tripsWithoutUser': int(query('destination',
            f'SELECT COUNT(*) FROM {SCHEMA}.trips t LEFT JOIN {SCHEMA}.users u '
            'ON t.user_id=u.user_id WHERE u.user_id IS NULL;')),
        'regionsWithoutTrip': int(query('destination',
            f'SELECT COUNT(*) FROM {SCHEMA}.trip_regions r LEFT JOIN {SCHEMA}.trips t '
            'ON r.trip_id=t.trip_id WHERE t.trip_id IS NULL;')),
    }
    require(not any(orphans.values()), 'B1 bulk FK orphan found; do not start Sink')
    result = {'runId': run_id, 'destinationSchema': SCHEMA,
              'archiveSha256': marker['archiveSha256'], 'rows': counts,
              'foreignKeyOrphans': orphans}
    imported = ready.parent / 'snapshot.imported.json'
    with imported.open('x') as file:
        json.dump(result, file, sort_keys=True)
        file.write('\n')
    imported.chmod(0o600)
    s3_upload(imported, prefix + '/snapshot.imported.json')
    print(json.dumps(result, sort_keys=True))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('capture', 'destination'))
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, 'run as root on the dedicated host')
    (capture if args.phase == 'capture' else destination)(args.run_id)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('B1_BULK_FAILED', type(exc).__name__, str(exc)[:180], file=sys.stderr)
        raise SystemExit(1)
