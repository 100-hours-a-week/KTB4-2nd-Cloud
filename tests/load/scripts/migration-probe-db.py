#!/usr/bin/env python3
"""Manage only the staging migration_write_probe table; replica actions are read-only."""
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

SCHEMA = 'yeodam_v1_source'
TABLE = 'migration_write_probe'
DDL = f'''CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLE} (
run_id CHAR(36) NOT NULL, request_id CHAR(36) NOT NULL,
payload_hash CHAR(64) NOT NULL,
created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
PRIMARY KEY (run_id, request_id)) ENGINE=InnoDB;'''

def canonical_uuid(value):
    if str(UUID(value)) != value:
        raise ValueError('Use a canonical lowercase UUID')
    return value

def mysql_command(role):
    if role == 'source':
        return ['mysql', '--protocol=socket', '-uroot', '--batch', '--skip-column-names', '--quick']
    return ['docker', 'exec', '-i', 'yeodam-v2-mysql', 'sh', '-ec',
            'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --batch --skip-column-names --quick']

def query(role, sql):
    result = subprocess.run(mysql_command(role), input=sql, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('MySQL operation failed: ' + result.stderr[:500])
    return result.stdout.strip()

def verify_table(role):
    rows = query(role, f"SELECT COLUMN_NAME, LOWER(COLUMN_TYPE), IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='{SCHEMA}' AND TABLE_NAME='{TABLE}' ORDER BY ORDINAL_POSITION;")
    expected = 'run_id\tchar(36)\tNO\nrequest_id\tchar(36)\tNO\npayload_hash\tchar(64)\tNO\ncreated_at\tdatetime(6)\tNO'
    if rows != expected:
        raise RuntimeError('Table is missing or has incompatible columns; no alteration was attempted')
    key = query(role, f"SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) FROM information_schema.STATISTICS WHERE TABLE_SCHEMA='{SCHEMA}' AND TABLE_NAME='{TABLE}' AND INDEX_NAME='PRIMARY';")
    engine = query(role, f"SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA='{SCHEMA}' AND TABLE_NAME='{TABLE}';")
    default = query(role, f"SELECT LOWER(COLUMN_DEFAULT) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='{SCHEMA}' AND TABLE_NAME='{TABLE}' AND COLUMN_NAME='created_at';")
    if key != 'run_id,request_id' or engine != 'InnoDB' or default != 'current_timestamp(6)':
        raise RuntimeError('Table key, engine, or timestamp default differs from the API contract')
    grants = query(role, f"SELECT GRANTEE, GROUP_CONCAT(PRIVILEGE_TYPE ORDER BY PRIVILEGE_TYPE) FROM information_schema.SCHEMA_PRIVILEGES WHERE TABLE_SCHEMA='{SCHEMA}' GROUP BY GRANTEE;")
    app_grants = [line for line in grants.splitlines() if line.startswith("'yeodam_app'@")]
    if not app_grants or not all({'SELECT', 'INSERT'} <= set(line.split('\t')[1].split(',')) for line in app_grants):
        raise RuntimeError('Expected yeodam_app schema SELECT/INSERT grants are missing')
    return {'schema': SCHEMA, 'table': TABLE, 'role': role, 'structure': 'verified', 'appSchemaGrants': app_grants}

def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'export', 'cleanup'))
    parser.add_argument('--role', required=True, choices=('source', 'replica'))
    parser.add_argument('--run-id', type=canonical_uuid)
    parser.add_argument('--confirm-run-cleanup', type=canonical_uuid)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run as root on the selected staging DB host')
    server_id = query(args.role, 'SELECT @@server_id;')
    if server_id != ('1' if args.role == 'source' else '2'):
        raise RuntimeError('Unexpected source/replica server_id')
    if args.action in ('prepare', 'cleanup') and args.role != 'source':
        raise ValueError('Write actions run only on source while native replication is active')
    if args.action in ('prepare', 'cleanup') and query(args.role, 'SELECT @@read_only, @@super_read_only;') != '0\t0':
        raise RuntimeError('Source is write-fenced; refuse table mutations after the writer handoff')
    if args.action == 'prepare':
        query(args.role, DDL)
    info = verify_table(args.role)
    if args.action == 'cleanup':
        if not args.run_id or args.confirm_run_cleanup != args.run_id:
            raise ValueError('Run cleanup requires matching --run-id and --confirm-run-cleanup')
        count = query(args.role, f"DELETE FROM {SCHEMA}.{TABLE} WHERE run_id='{args.run_id}'; SELECT ROW_COUNT();")
        info.update(runId=args.run_id, deletedRows=int(count))
    elif args.action == 'export':
        if not args.run_id:
            raise ValueError('Export requires --run-id')
        directory = Path('/run/yeodam-migration-probe')
        directory.mkdir(mode=0o700, exist_ok=True)
        output = directory / f'{args.run_id}.{args.role}.tsv.gz'
        # One consistent read, streamed so millions of rows do not fill RAM or SSM output.
        sql = f"START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY; SELECT run_id,request_id,payload_hash,created_at FROM {SCHEMA}.{TABLE} WHERE run_id='{args.run_id}' ORDER BY request_id; COMMIT;"
        with output.open('xb') as destination:
            process = subprocess.Popen(mysql_command(args.role), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            process.stdin.write(sql.encode()); process.stdin.close()
            rows = 0
            with gzip.GzipFile(fileobj=destination, mode='wb') as compressed:
                for line in process.stdout:
                    compressed.write(line); rows += 1
            error = process.stderr.read().decode()
            status = process.wait()
        if status:
            output.unlink(missing_ok=True)
            raise RuntimeError('Export failed: ' + error[:500])
        info.update(runId=args.run_id, rows=rows, export=str(output))
    if args.run_id:
        info['runRows'] = int(query(args.role, f"SELECT COUNT(*) FROM {SCHEMA}.{TABLE} WHERE run_id='{args.run_id}';"))
    print(json.dumps(info))

if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(1)
