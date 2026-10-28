#!/usr/bin/env python3
"""Inspect or apply the approved index on guarded #444 clones only.

No HTTP input, Consumer changes, schema reset, GTID reset or shared services.
Applying requires a successful control Run receipt and the restored baseline.
"""
import argparse
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import time
from uuid import UUID

INDEX = 'IDX_TRIPS_USER_NAME_DELETED'
COLUMNS = ['user_id', 'trip_name', 'deleted_at']
SCHEMA = 'yeodam_v1_source'
BASELINE_SHA = 'ab1a0935f6335f0385f5df2cfb2eb4c75c9fce4f05a742d9e81bed3bfe2cfae6'
DDL = 'CREATE INDEX ' + INDEX + ' ON ' + SCHEMA + '.trips (user_id, trip_name, deleted_at);'


def validate_control(receipt, role, instance_id):
    if receipt.get('issue') != 455 or receipt.get('case') != 'control30':
        raise ValueError('A #455 control30 receipt is required')
    if receipt.get('hosts', {}).get(role) != instance_id:
        raise ValueError('Control evidence belongs to a different instance')
    report = receipt['report']
    UUID(report['runId'])
    if (report.get('rate') != 30 or report.get('phase') != 'completed'
            or report.get('started', 0) <= 0 or report.get('started') != report.get('targetRows')
            or report.get('pendingAtFinalAudit') != 0 or report.get('dropped') != 0
            or report.get('k6ExitCode') != 0 or report.get('missingRegions') != 0):
        raise ValueError('Control input or final data checks did not pass')
    for key in ['sourceBoundaryEqual', 'requestHashEqual', 'redisEqual', 'authAuditBeforeE2E']:
        if report.get(key) is not True:
            raise ValueError('Missing control check: ' + key)
    if any(report.get(k) != [[30, 30]] for k in ['v1E2E', 'v2E2E']):
        raise ValueError('Both real E2E checks must pass before comparison')


def helper():
    path = Path('/var/lib/yeodam/migration444/prepare.py')
    spec = importlib.util.spec_from_file_location('prepare444', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def index_rows(m, role):
    return m.query(role, f"SELECT index_name,non_unique,seq_in_index,column_name FROM information_schema.statistics WHERE table_schema='{SCHEMA}' AND table_name='trips' ORDER BY index_name,seq_in_index;")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['inspect', 'apply'])
    p.add_argument('--role', required=True, choices=['source', 'target'])
    p.add_argument('--instance-id', required=True)
    p.add_argument('--data-volume-id', required=True)
    p.add_argument('--control-receipt', type=Path)
    p.add_argument('--case', choices=['index30', 'index50'], default='index30')
    p.add_argument('--contract', type=Path)
    args = p.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        raise ValueError('Root required on the clone')
    m = helper()
    mount = '/var/lib/mysql' if args.role == 'source' else '/srv/yeodam/mysql'
    if subprocess.run(['mountpoint', '-q', mount]).returncode:
        raise ValueError('Dedicated data disk is not mounted')
    device = m.run(['findmnt', '-n', '-o', 'SOURCE', mount])
    serial = m.run(['lsblk', '-dn', '-o', 'SERIAL', device])
    m.guard(args.role, args.instance_id, args.data_volume_id, m.identity(),
            json.loads((m.ROOT / 'identity.json').read_text()), serial)
    before = index_rows(m, args.role)
    if args.action == 'apply':
        if not args.control_receipt:
            raise ValueError('Successful measured control receipt required')
        validate_control(json.loads(args.control_receipt.read_text()), args.role, args.instance_id)
        if not args.contract:
            raise ValueError('Frozen comparison contract required')
        contract = json.loads(args.contract.read_text())
        baseline = contract['baseline30' if args.case == 'index30' else 'baseline50']
        if args.case == 'index30' and baseline.get('sha256') != BASELINE_SHA:
            raise ValueError('30 RPS baseline must match the historical success archive')
        if args.case == 'index50' and baseline.get('status') != 'verified':
            raise ValueError('Historical 50 RPS backup fingerprint is not verified yet')
        if not re.fullmatch('[a-f0-9]{64}', baseline.get('sha256', '')):
            raise ValueError('A verified baseline archive SHA is required')
        restored = json.loads((m.ROOT / 'baseline-restored.json').read_text())
        if restored.get('baselineSha256') != baseline['sha256']:
            raise ValueError('Unexpected baseline archive')
        counts = m.query(args.role, f'SELECT (SELECT COUNT(*) FROM {SCHEMA}.trips),(SELECT COUNT(*) FROM {SCHEMA}.trip_regions),(SELECT COUNT(*) FROM {SCHEMA}.migration_trip_request_result);')
        if counts.split('\t') != [str(baseline[k]) for k in ['trips', 'regions', 'journal']]:
            raise ValueError('Restore the identical baseline before adding the index')
        if any(row.split('\t')[0] == INDEX for row in before.splitlines()):
            raise ValueError('Index already exists; preserve and inspect instead of reapplying')
        read_only = m.query(args.role, 'SELECT @@super_read_only;')
        try:
            m.query(args.role, 'SET GLOBAL super_read_only=OFF;')
            started = time.monotonic()
            m.query(args.role, DDL)
            elapsed = time.monotonic() - started
        finally:
            m.query(args.role, 'SET GLOBAL super_read_only=' + read_only + ';')
        after = index_rows(m, args.role)
        rows = [x.split('\t') for x in after.splitlines() if x.split('\t')[0] == INDEX]
        if [r[3] for r in rows] != COLUMNS or any(r[1] != '1' for r in rows):
            raise ValueError('Unexpected created index structure')
        print(json.dumps({'role': args.role, 'index': INDEX, 'columns': COLUMNS,
                          'unique': False, 'ddlClientCommandElapsedSeconds': elapsed}))
    else:
        stats = m.query(args.role, f"SELECT table_rows,data_length,index_length FROM information_schema.tables WHERE table_schema='{SCHEMA}' AND table_name='trips';")
        digests = m.query(args.role, "SELECT DIGEST,DIGEST_TEXT,COUNT_STAR,SUM_TIMER_WAIT,SUM_ROWS_EXAMINED,SUM_ROWS_SENT FROM performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME='" + SCHEMA + "' AND (DIGEST_TEXT LIKE '%trip_name%' OR DIGEST_TEXT LIKE 'INSERT%trips%' OR DIGEST_TEXT LIKE 'INSERT%trip_regions%') ORDER BY DIGEST LIMIT 30;")
        print(json.dumps({'role': args.role, 'indexes': [x.split('\t') for x in before.splitlines()],
                          'tableStatisticsEstimate': stats.split('\t'),
                          'statementDigestCounters': [x.split('\t') for x in digests.splitlines()],
                          'digestCounterUnits': {'sumTimerWait': 'picoseconds', 'rows': 'cumulative'},
                          'measurement': 'Take deltas before/after input; do not reset global counters'}))


if __name__ == '__main__':
    main()
