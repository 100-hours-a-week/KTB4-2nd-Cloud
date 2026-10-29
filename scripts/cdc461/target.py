#!/usr/bin/env python3
"""Guarded clone-only restoration and numeric Performance Schema sampling."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import UUID

ROOT = Path('/var/lib/yeodam/cdc461')
SCHEMA = 'yeodam_v1_source'
BUCKET = 'yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def helpers():
    return (module('clone461', '/var/lib/yeodam/migration444/prepare.py'),
            module('reset461', '/run/yeodam-b391/b-integration376-reset.py'),
            module('baseline461', '/run/yeodam-b391/b-integration408-baseline.py'))


def guard(m, r, contract):
    mount = '/srv/yeodam/mysql'
    subprocess.run(['mountpoint', '-q', mount], check=True)
    device = m.run(['findmnt', '-n', '-o', 'SOURCE', mount])
    serial = m.run(['lsblk', '-dn', '-o', 'SERIAL', device])
    m.guard('target', contract['targetInstance'], contract['targetVolume'], m.identity(),
            json.loads((m.ROOT / 'identity.json').read_text()), serial)
    if r.query('target', 'SELECT @@server_id;') != '2': raise RuntimeError('Wrong database server')
    for table in ['replication_connection_status', 'replication_applier_status']:
        if r.query('target', "SELECT COUNT(*) FROM performance_schema."+table+" WHERE SERVICE_STATE='ON';") != '0':
            raise RuntimeError('Native replication active; no diagnostic')


def protected(r):
    if r.query('target', 'SELECT @@read_only,@@super_read_only;') != '1\t1':
        raise RuntimeError('Target must be protected before restoration')
    if r.query('target', "SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('root','event_scheduler','system user') AND ID!=CONNECTION_ID();") != '0':
        raise RuntimeError('Unexpected Target clients; do not restore')


def import_sql(r, archive):
    r.query('target', 'SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
    try:
        with gzip.open(archive, 'rb') as data, archive.with_suffix('.import.stderr.private').open('wb') as error:
            p = subprocess.Popen(r.command('target')+['--default-character-set=utf8mb4'], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=error)
            try:
                p.stdin.write(('SET SESSION sql_log_bin=0; DROP DATABASE `'+SCHEMA+'`; CREATE DATABASE `'+SCHEMA+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; USE `'+SCHEMA+'`;\n').encode())
                for block in iter(lambda: data.read(65536), b''): p.stdin.write(block)
                p.stdin.close()
                if p.wait(): raise RuntimeError('Import failed; private stderr preserved')
            except BaseException:
                if p.poll() is None: p.kill(); p.wait()
                raise
    finally:
        r.query('target', 'SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')


def scope_hashes(r):
    names = ['users', 'trips', 'trip_regions', 'migration_trip_request_result']
    return {table: r.query('target', 'CHECKSUM TABLE `'+SCHEMA+'`.`'+table+'` EXTENDED;').split('\t')[-1] for table in names}


def prepare(directory, c, m, r, b):
    protected(r); directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    before = directory / 'before.sql.gz'; r.snapshot('target', before)
    original = b.fingerprint(r, 'target')
    if original['trips']!=c['finalTrips']:raise RuntimeError('Current preserved Target differs from parent Run')
    receipt = {'beforeSha256': r.sha(before), 'beforeFingerprint': original, 'expectedFinalScope': scope_hashes(r)}
    r.transfer(str(before), 's3://'+BUCKET+'/migration/performance455/diagnostics461/'+directory.name+'/before.sql.gz')
    (directory/'before.json').write_text(json.dumps(receipt, indent=2))
    marker=directory/'source.ready'; archive=directory/'source.sql.gz'
    r.transfer('s3://'+BUCKET+'/'+c['snapshotMarker'], str(marker))
    info=dict(line.split('=',1) for line in marker.read_text().splitlines() if '=' in line)
    expected_key=c['snapshotMarker'][:-6]+'.sql.gz'
    if info.get('snapshot_key')!=expected_key or info.get('sha256')!=c['snapshotSha256'] or info.get('source_schema')!=SCHEMA:
        raise RuntimeError('Original source Snapshot differs')
    r.transfer('s3://'+BUCKET+'/'+expected_key, str(archive))
    if r.sha(archive)!=c['snapshotSha256'] or archive.stat().st_size!=c['snapshotBytes']:
        raise RuntimeError('Original Snapshot bytes differ; no import')
    protected(r); import_sql(r, archive)
    # Match the original Bulk helper's COUNT, without adding a full-table checksum
    # scan that would change cache state before the observed Sink workload.
    initial={'trips':int(r.query('target','SELECT COUNT(*) FROM '+SCHEMA+'.trips;')),
             'tables':int(r.query('target',"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='"+SCHEMA+"';"))}
    if initial['trips']!=c['initialTrips'] or initial['tables']!=33: raise RuntimeError('Initial Bulk state differs')
    indexes=r.query('target', "SELECT COLUMN_NAME,NON_UNIQUE FROM information_schema.statistics WHERE TABLE_SCHEMA='"+SCHEMA+"' AND TABLE_NAME='trips' AND INDEX_NAME='IDX_TRIPS_USER_NAME_DELETED' ORDER BY SEQ_IN_INDEX;")
    if indexes.splitlines()!=['user_id\t1','trip_name\t1','deleted_at\t1']: raise RuntimeError('Index differs')
    receipt.update(initialBulkState=initial, snapshotSha256=c['snapshotSha256'], rowsRestored=c['initialTrips'],
                   binlogImport=False, gtidReset=False)
    (directory/'prepared.json').write_text(json.dumps(receipt, indent=2))
    r.query('target', 'SET PERSIST super_read_only=OFF;') # read_only remains ON; only CDC may write.
    print(json.dumps({'prepared':True,'rows':initial['trips'],'snapshotSha256':c['snapshotSha256']}),flush=True)


def sample(directory, r, seconds, interval):
    end=time.monotonic()+seconds
    with (directory/'mysql.jsonl').open('w') as file:
        while time.monotonic()<end and not (directory/'STOP').exists():
            began=time.monotonic()
            # Collector SELECTs use root; only Sink-user statements are counted.
            statements=r.query('target', "SELECT s.EVENT_NAME,SUM(s.COUNT_STAR),SUM(s.SUM_TIMER_WAIT) FROM performance_schema.events_statements_summary_by_thread_by_event_name s JOIN performance_schema.threads t ON t.THREAD_ID=s.THREAD_ID WHERE t.PROCESSLIST_USER='yeodam_cdc_sink' GROUP BY s.EVENT_NAME;")
            waits=r.query('target', "SELECT s.EVENT_NAME,SUM(s.COUNT_STAR),SUM(s.SUM_TIMER_WAIT) FROM performance_schema.events_waits_summary_by_thread_by_event_name s JOIN performance_schema.threads t ON t.THREAD_ID=s.THREAD_ID WHERE t.PROCESSLIST_USER='yeodam_cdc_sink' AND s.COUNT_STAR>0 GROUP BY s.EVENT_NAME;")
            status=r.query('target', "SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected','Threads_running','Innodb_log_waits','Innodb_row_lock_current_waits','Innodb_data_pending_reads','Innodb_data_pending_writes','Innodb_os_log_fsyncs','Innodb_data_fsyncs','Handler_commit','Com_commit');")
            global_statements=r.query('target', "SELECT EVENT_NAME,COUNT_STAR,SUM_TIMER_WAIT FROM performance_schema.events_statements_summary_global_by_event_name WHERE EVENT_NAME IN ('statement/sql/commit','statement/sql/rollback','statement/sql/insert','statement/sql/update','statement/sql/delete');")
            row={'at':time.time(),'statementCounters':[x.split('\t') for x in statements.splitlines()],
                 'globalStatementCounters':[x.split('\t') for x in global_statements.splitlines()],
                 'waitCounters':[x.split('\t') for x in waits.splitlines()], 'status':dict(x.split('\t',1) for x in status.splitlines()),
                 'sampleMillis':(time.monotonic()-began)*1000}
            file.write(json.dumps(row)+'\n');file.flush();time.sleep(max(0,interval-(time.monotonic()-began)))


def verify(directory, r, c):
    expected=json.loads((directory/'before.json').read_text())['expectedFinalScope'];actual=scope_hashes(r)
    rows=int(r.query('target','SELECT COUNT(*) FROM '+SCHEMA+'.trips;'))
    missing=int(r.query('target','SELECT COUNT(*) FROM '+SCHEMA+'.trip_regions r LEFT JOIN '+SCHEMA+'.trips t ON t.trip_id=r.trip_id WHERE t.trip_id IS NULL;'))
    report={'scopeHashEqual':actual==expected,'scopeChecksums':actual,'expectedScopeChecksums':expected,'trips':rows,
            'expectedTrips':c['finalTrips'],'orphanRegions':missing,'complete':actual==expected and rows==c['finalTrips'] and missing==0}
    (directory/'verified.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)


def restore(directory, r, b):
    protected(r)
    before=json.loads((directory/'before.json').read_text());archive=directory/'before.sql.gz'
    if r.sha(archive)!=before['beforeSha256']: raise RuntimeError('Preserved Target archive differs')
    # Preserve the diagnostic result before replacing its schema.
    result=directory/'result.sql.gz';r.snapshot('target', result)
    r.transfer(str(result),'s3://'+BUCKET+'/migration/performance455/diagnostics461/'+directory.name+'/result.sql.gz')
    import_sql(r,archive)
    final=b.fingerprint(r,'target')
    if final!=before['beforeFingerprint']:raise RuntimeError('Target restoration fingerprint differs')
    print(json.dumps({'restored':True,'fingerprintEqual':True,'trips':final['trips'],'gtidReset':False}),flush=True)


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['check','prepare','sample','verify','protect','restore']);p.add_argument('--run',required=True);p.add_argument('--contract',required=True);a=p.parse_args()
    if str(UUID(a.run))!=a.run:raise ValueError('Canonical diagnostic UUID required')
    os.umask(0o077);c=json.loads(Path(a.contract).read_text());m,r,b=helpers();guard(m,r,c);directory=ROOT/a.run
    if a.action=='check':
        protected(r)
        instruments=r.query('target',"SELECT NAME,ENABLED,TIMED FROM performance_schema.setup_instruments WHERE NAME IN ('statement/sql/commit','transaction');")
        if 'statement/sql/commit\tYES\tYES' not in instruments:raise RuntimeError('COMMIT timing instrumentation unavailable; do not restore')
        print(json.dumps({'protected':True,'instruments':instruments}));return
    if a.action=='prepare':prepare(directory,c,m,r,b)
    elif a.action=='sample':sample(directory,r,c['diagnosticDeadlineSeconds']+90,c['sampleIntervalSeconds'])
    elif a.action=='verify':verify(directory,r,c)
    elif a.action=='protect':
        (directory/'STOP').touch()
        r.query('target','SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')
        print(json.dumps({'protected':True}))
    else:restore(directory,r,b)


if __name__=='__main__':main()
