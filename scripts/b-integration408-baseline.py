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


def legacy():
    spec = importlib.util.spec_from_file_location('reset391', '/run/yeodam-b391/b-integration376-reset.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


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
    module.snapshot('source', archive)
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
    module.snapshot(role, before)
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
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('capture', 'restore'))
    parser.add_argument('--baseline-id', required=True); parser.add_argument('--role', choices=('source', 'target'), required=True)
    parser.add_argument('--attempt', choices=('control', 'limited')); args = parser.parse_args()
    if str(UUID(args.baseline_id)) != args.baseline_id:raise ValueError('Canonical baseline required')
    address = {'source': '10.30.0.174', 'target': '10.30.10.28'}[args.role]
    if address not in subprocess.check_output(['hostname', '-I'], text=True).split():raise RuntimeError('Wrong rehearsal host')
    directory = BASE / args.baseline_id; directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    module = legacy()
    if args.action == 'capture':
        if args.role != 'source':raise ValueError('Baseline captured from authoritative Source only')
        capture(module, args.baseline_id, directory)
    else:
        if not args.attempt:raise ValueError('Restore attempt required')
        restore(module, args.baseline_id, args.attempt, args.role, directory)


if __name__ == '__main__':main()
