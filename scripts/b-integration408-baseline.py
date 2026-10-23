#!/usr/bin/env python3
"""Preserve and restore one immutable rehearsal schema for a paired comparison."""
import argparse
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from uuid import UUID

BUCKET = 'yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2'
SCHEMA = 'yeodam_v1_source'
BASE = Path('/var/lib/yeodam/b408-baseline')
HISTORICAL_KEY = 'migration/rehearsal-reset/3502031b-0040-4333-9a8c-9ce63d9a2ce8/b391-target.sql.gz'
HISTORICAL_SHA = 'ab1a0935f6335f0385f5df2cfb2eb4c75c9fce4f05a742d9e81bed3bfe2cfae6'


def legacy():
    spec = importlib.util.spec_from_file_location('reset391', '/run/yeodam-b391/b-integration376-reset.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def preserve(module, role, path):
    """Keep the legacy dump settings and retain private stderr on failure."""
    error_path = path.with_suffix(path.suffix + '.stderr')
    with path.open('xb') as file, gzip.GzipFile(fileobj=file, mode='wb', mtime=0) as compressed, error_path.open('xb') as errors:
        path.chmod(0o600); error_path.chmod(0o600)
        process = subprocess.Popen(module.command(role, 'mysqldump') + [
            '--single-transaction', '--quick', '--set-gtid-purged=OFF', '--hex-blob',
            '--routines', '--events', '--triggers', '--default-character-set=utf8mb4', SCHEMA],
            stdout=subprocess.PIPE, stderr=errors)
        for block in iter(lambda: process.stdout.read(65536), b''):compressed.write(block)
        if process.wait():raise RuntimeError('Preservation dump failed; inspect private stderr before any database replacement')


def protected(module, role):
    expected = {'source': '1\t1\t1', 'target': '2\t1\t1'}[role]
    if module.query(role, 'SELECT @@server_id,@@read_only,@@super_read_only;') != expected:
        raise RuntimeError('Expected fenced rehearsal database before baseline operation')
    for table in ('replication_connection_status', 'replication_applier_status'):
        if module.query(role, "SELECT COUNT(*) FROM performance_schema." + table + " WHERE SERVICE_STATE='ON';") != '0':
            raise RuntimeError('Native replication must remain OFF')
    if role == 'target':
        deadline = time.monotonic() + 45
        while module.query(role, "SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('root','event_scheduler','system user') AND ID!=CONNECTION_ID();") != '0':
            if time.monotonic() >= deadline:raise RuntimeError('Target clients remain; baseline not imported')
            time.sleep(.5)


def fingerprint(module, role):
    names = module.query(role, "SELECT table_name FROM information_schema.tables WHERE table_schema='" + SCHEMA + "' AND table_type='BASE TABLE' ORDER BY table_name;").splitlines()
    if not names or any(not re.fullmatch('[a-zA-Z0-9_]+', name) for name in names):
        raise RuntimeError('Unexpected baseline tables')
    checks = module.query(role, 'CHECKSUM TABLE ' + ','.join('`' + SCHEMA + '`.`' + name + '`' for name in names) + ' EXTENDED;')
    values = {}
    for line in checks.splitlines():
        table, checksum = line.split('\t')
        if checksum == 'NULL':raise RuntimeError('Table checksum unavailable; do not claim matching data')
        values[table.split('.')[-1]] = checksum
    if set(values) != set(names):raise RuntimeError('Incomplete baseline checksum')
    auto = module.query(role, "SELECT table_name,COALESCE(auto_increment,0) FROM information_schema.tables WHERE table_schema='" + SCHEMA + "' AND table_type='BASE TABLE' ORDER BY table_name;")
    return {'tables': values, 'autoIncrement': auto,
            'trips': int(module.query(role, 'SELECT COUNT(*) FROM ' + SCHEMA + '.trips;')),
            'journal': int(module.query(role, 'SELECT COUNT(*) FROM ' + SCHEMA + '.migration_trip_request_result;'))}


def capture(module, baseline, directory):
    protected(module, 'source')
    archive = directory / 'baseline.sql.gz'; marker = directory / 'baseline.ready'
    if archive.exists() or marker.exists():raise RuntimeError('Immutable baseline already exists')
    if shutil.disk_usage('/').free < 2 * 1024**3:
        raise RuntimeError('Source root requires2GiB free before baseline preservation')
    preserve(module, 'source', archive)
    value = {'baselineId': baseline, 'bucket': BUCKET,
             'key': 'migration/rehearsal-reset/' + baseline + '/b408-baseline.sql.gz',
             'sha256': module.sha(archive), 'bytes': archive.stat().st_size,
             'fingerprint': fingerprint(module, 'source'), 'gtidReset': False}
    marker.write_text(json.dumps(value, indent=2)); marker.chmod(0o600)
    module.transfer(str(archive), 's3://' + BUCKET + '/' + value['key'])
    module.transfer(str(marker), 's3://' + BUCKET + '/' + value['key'] + '.ready')
    print(json.dumps({'phase': 'baseline_preserved', **value}), flush=True)


def validate_marker(value, baseline):
    expected = 'migration/rehearsal-reset/' + baseline + '/b408-baseline.sql.gz'
    if value.get('baselineId') != baseline or value.get('bucket') != BUCKET or value.get('key') != expected:
        raise RuntimeError('Wrong immutable baseline identity')
    if not re.fullmatch('[0-9a-f]{64}', value.get('sha256', '')) or not value.get('fingerprint'):
        raise RuntimeError('Missing baseline digest or fingerprint')


def adopt_historical(module, baseline, directory):
    """Restore the verified pre-30RPS archive; preserve the current Source first."""
    protected(module, 'source')
    archive = directory / 'baseline.sql.gz'
    done = directory / 'source-control.restored.json'
    before = directory / 'source-control-before.sql.gz'
    if archive.exists() or before.exists() or done.exists():
        raise RuntimeError('Historical adoption already attempted; inspect checkpoint')
    module.transfer('s3://' + BUCKET + '/' + HISTORICAL_KEY, str(archive))
    archive.chmod(0o600)
    if module.sha(archive) != HISTORICAL_SHA:
        raise RuntimeError('Historical archive digest differs; no database change')
    if shutil.disk_usage('/').free < 2 * 1024**3:
        raise RuntimeError('Requires2GiB free before historical restoration')
    preserve(module, 'source', before)
    backup = 'migration/rehearsal-reset/' + baseline + '/b408-source-control-before.sql.gz'
    module.transfer(str(before), 's3://' + BUCKET + '/' + backup)
    meta = {'role': 'source', 'attempt': 'control', 'key': backup,
            'sha256': module.sha(before), 'bytes': before.stat().st_size}
    preservation = directory / 'source-control-before.ready'
    preservation.write_text(json.dumps(meta)); preservation.chmod(0o600)
    module.transfer(str(preservation), 's3://' + BUCKET + '/' + backup + '.ready')
    protected(module, 'source')
    module.query('source', 'SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
    try:
        with gzip.open(archive, 'rb') as content, (directory / 'source-control.stderr').open('wb') as errors:
            process = subprocess.Popen(module.command('source') + ['--default-character-set=utf8mb4'],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
            process.stdin.write(('SET SESSION sql_log_bin=0; DROP DATABASE `' + SCHEMA + '`; CREATE DATABASE `' + SCHEMA + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; USE `' + SCHEMA + '`;\n').encode())
            for block in iter(lambda: content.read(65536), b''):process.stdin.write(block)
            process.stdin.close()
            if process.wait():raise RuntimeError('Historical import failed; preserve backups and private stderr')
        observed = fingerprint(module, 'source')
        if observed['trips'] != 2021037 or observed['journal'] != 219511 or len(observed['tables']) != 33:
            raise RuntimeError('Historical data differs; no measured input')
        if module.query('source', 'SELECT COUNT(*) FROM ' + SCHEMA + '.trip_regions;') != '2021037':
            raise RuntimeError('Historical region rows differ; no measured input')
    finally:
        module.query('source', 'SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')
    key = 'migration/rehearsal-reset/' + baseline + '/b408-baseline.sql.gz'
    value = {'baselineId': baseline, 'bucket': BUCKET, 'key': key, 'sha256': HISTORICAL_SHA,
             'bytes': archive.stat().st_size, 'fingerprint': observed, 'gtidReset': False,
             'historicalOriginKey': HISTORICAL_KEY, 'historical30RpsRun': '56f1a134-7867-42d6-84df-1c19f030916e'}
    marker = directory / 'baseline.ready'; marker.write_text(json.dumps(value)); marker.chmod(0o600)
    module.transfer(str(archive), 's3://' + BUCKET + '/' + key)
    module.transfer(str(marker), 's3://' + BUCKET + '/' + key + '.ready')
    result = {'phase': 'baseline_restored', 'role': 'source', 'baselineId': baseline, 'attempt': 'control',
              'baselineSha256': HISTORICAL_SHA, 'beforePreserved': meta, 'fingerprint': observed,
              'gtidReset': False, 'binlogImportEnabled': False}
    done.write_text(json.dumps(result)); done.chmod(0o600)
    print(json.dumps({'phase': 'historical_baseline_adopted', 'baseline': value, 'receipt': result}), flush=True)


def restore(module, baseline, attempt, role, directory):
    protected(module, role)
    marker = directory / ('download-' + attempt + '.ready')
    key = 'migration/rehearsal-reset/' + baseline + '/b408-baseline.sql.gz'
    module.transfer('s3://' + BUCKET + '/' + key + '.ready', str(marker))
    value = json.loads(marker.read_text()); validate_marker(value, baseline)
    archive = directory / 'baseline.sql.gz'
    if not archive.exists():module.transfer('s3://' + BUCKET + '/' + value['key'], str(archive))
    if archive.stat().st_size != value['bytes'] or module.sha(archive) != value['sha256']:
        raise RuntimeError('Baseline archive digest mismatch; no database change')
    if shutil.disk_usage('/').free < value['bytes'] * 3 + 512 * 1024**2:
        raise RuntimeError('Insufficient space for baseline and preserved current data')
    done = directory / (role + '-' + attempt + '.restored.json')
    before = directory / (role + '-' + attempt + '-before.sql.gz')
    if done.exists() or before.exists():raise RuntimeError('Restore already attempted; preserve checkpoint')
    preserve(module, role, before)
    backup = 'migration/rehearsal-reset/' + baseline + '/b408-' + role + '-' + attempt + '-before.sql.gz'
    module.transfer(str(before), 's3://' + BUCKET + '/' + backup)
    meta = {'role': role, 'attempt': attempt, 'key': backup, 'sha256': module.sha(before), 'bytes': before.stat().st_size}
    preservation = directory / (role + '-' + attempt + '-before.ready')
    preservation.write_text(json.dumps(meta)); preservation.chmod(0o600)
    module.transfer(str(preservation), 's3://' + BUCKET + '/' + backup + '.ready')
    # Verify the privately preserved copy before replacing only the rehearsal schema.
    protected(module, role)
    module.query(role, 'SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
    try:
        with gzip.open(archive, 'rb') as content, (directory / (role + '-' + attempt + '.stderr')).open('wb') as errors:
            process = subprocess.Popen(module.command(role) + ['--default-character-set=utf8mb4'], stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=errors)
            process.stdin.write(('SET SESSION sql_log_bin=0; DROP DATABASE `' + SCHEMA + '`; CREATE DATABASE `' + SCHEMA + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; USE `' + SCHEMA + '`;\n').encode())
            for block in iter(lambda: content.read(65536), b''):process.stdin.write(block)
            process.stdin.close()
            if process.wait():raise RuntimeError('Baseline import failed; preserve private stderr and backups')
        observed = fingerprint(module, role)
        if observed != value['fingerprint']:raise RuntimeError('Restored data differs; do not start measured input')
    finally:
        module.query(role, 'SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')
    result = {'phase': 'baseline_restored', 'role': role, 'baselineId': baseline, 'attempt': attempt,
              'baselineSha256': value['sha256'], 'beforePreserved': meta, 'fingerprint': observed,
              'gtidReset': False, 'binlogImportEnabled': False}
    done.write_text(json.dumps(result, indent=2)); done.chmod(0o600)
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('capture', 'restore', 'adopt-historical'))
    parser.add_argument('--baseline-id', required=True); parser.add_argument('--role', choices=('source', 'target'), required=True)
    parser.add_argument('--attempt', choices=('control', 'limited')); args = parser.parse_args()
    if str(UUID(args.baseline_id)) != args.baseline_id:raise ValueError('Canonical baseline required')
    address = {'source': '10.30.0.174', 'target': '10.30.10.28'}[args.role]
    if address not in subprocess.check_output(['hostname', '-I'], text=True).split():raise RuntimeError('Wrong rehearsal host')
    directory = BASE / args.baseline_id; directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    module = legacy()
    if args.action == 'adopt-historical':
        if args.role != 'source' or args.attempt != 'control':raise ValueError('Historical baseline starts with Source control restoration')
        adopt_historical(module, args.baseline_id, directory)
    elif args.action == 'capture':
        if args.role != 'source':raise ValueError('Baseline captured from authoritative Source only')
        capture(module, args.baseline_id, directory)
    else:
        if not args.attempt:raise ValueError('Restore attempt required')
        restore(module, args.baseline_id, args.attempt, args.role, directory)


if __name__ == '__main__':main()
