#!/usr/bin/env python3
"""Back up the rehearsal Target, then import a full consistent Source snapshot.

No native replication is started; the old channel and its GTID history stay
stopped. CDC alone applies changes after this initial import.
"""
import argparse
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
from uuid import UUID

BUCKET='yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2'
SCHEMA='yeodam_v1_source'

def run(args,**kwargs):
    p=subprocess.run(args,capture_output=True,**kwargs)
    if p.returncode:raise RuntimeError('Target host operation failed: '+args[0])
    return p.stdout

def mysql(sql):
    cmd=['docker','exec','-i','yeodam-v2-mysql','sh','-ec','export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --binary-mode --batch --skip-column-names']
    return run(cmd,input=sql.encode()).decode().strip()

def parameter(name):
    return run(['aws','ssm','get-parameter','--name',name,'--with-decryption','--region','ap-northeast-2','--query','Parameter.Value','--output','text']).decode().strip()

def import_snapshot(run_id,key):
    if mysql('SELECT @@server_id,@@read_only,@@super_read_only;')!='2\t1\t1':raise RuntimeError('Expected protected migration Target')
    if mysql("SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('root','event_scheduler','system user') AND ID!=CONNECTION_ID();")!='0':raise RuntimeError('Target has unexpected clients')
    if mysql("SELECT COUNT(*) FROM performance_schema.replication_connection_status WHERE SERVICE_STATE='ON';")!='0' or mysql("SELECT COUNT(*) FROM performance_schema.replication_applier_status WHERE SERVICE_STATE='ON';")!='0':raise RuntimeError('Native replication must stay OFF')
    if not re.fullmatch(r'migration/v1-source/\d{8}T\d{6}Z-[0-9a-f]{8}\.ready',key):raise ValueError('Unexpected source marker')
    directory=Path('/var/lib/yeodam/b388')/run_id;directory.mkdir(parents=True,mode=0o700)
    marker=directory/'source.ready';archive=directory/'source.sql.gz'
    for path,s3key in ((marker,key),(archive,key[:-6]+'.sql.gz')):
        run(['aws','s3','cp','s3://'+BUCKET+'/'+s3key,str(path),'--region','ap-northeast-2','--only-show-errors']);path.chmod(0o600)
    info=dict(line.split('=',1) for line in marker.read_text().splitlines() if '=' in line)
    if info['source_schema']!=SCHEMA or info['snapshot_key']!=key[:-6]+'.sql.gz':raise RuntimeError('Snapshot identity mismatch')
    h=hashlib.sha256()
    with archive.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    if h.hexdigest()!=info['sha256']:raise RuntimeError('Archive checksum mismatch')
    backup=run(['/usr/local/sbin/yeodam-mysql-full-backup']).decode().strip()
    if '.ready' not in backup:raise RuntimeError('Target backup incomplete')
    (directory/'target-backup.txt').write_text(backup+'\n')
    mysql('SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;')
    completed=False
    try:
        cmd=['docker','exec','-i','yeodam-v2-mysql','sh','-ec','export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql -uroot --binary-mode --default-character-set=utf8mb4']
        with gzip.open(archive,'rb') as content, (directory/'import.stderr').open('wb') as errors:
            p=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=errors)
            p.stdin.write(('SET SESSION sql_log_bin=0; DROP DATABASE `'+SCHEMA+'`; CREATE DATABASE `'+SCHEMA+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; USE `'+SCHEMA+'`;\n').encode())
            for b in iter(lambda:content.read(65536),b''):p.stdin.write(b)
            p.stdin.close()
            if p.wait():raise RuntimeError('Import failed; stderr preserved privately on Target')
        password=parameter('/yeodam/v2/staging/cdc-test/SINK_PASSWORD')
        if not re.fullmatch('[0-9a-fA-F]{32,128}',password):raise RuntimeError('Unexpected CDC credential format')
        mysql("SET SESSION sql_log_bin=0; CREATE USER IF NOT EXISTS 'yeodam_cdc_sink'@'10.30.10.95' IDENTIFIED BY '"+password+"' REQUIRE SSL; GRANT REPLICATION SLAVE,REPLICATION CLIENT,CONNECTION_ADMIN ON *.* TO 'yeodam_cdc_sink'@'10.30.10.95'; "+' '.join("GRANT SELECT,INSERT,UPDATE,DELETE ON "+SCHEMA+'.'+t+" TO 'yeodam_cdc_sink'@'10.30.10.95';" for t in ('trips','trip_regions','users','login_sessions','csrf_tokens')))
        mysql("SET SESSION sql_log_bin=0; CREATE USER IF NOT EXISTS 'yeodam_cdc_sink'@'10.30.0.174' IDENTIFIED BY '"+password+"' REQUIRE SSL; GRANT CONNECTION_ADMIN ON *.* TO 'yeodam_cdc_sink'@'10.30.0.174'; "+' '.join("GRANT SELECT,INSERT,UPDATE,DELETE ON "+SCHEMA+'.'+t+" TO 'yeodam_cdc_sink'@'10.30.0.174';" for t in ('login_sessions','csrf_tokens')))
        # This file contains only public DDL, installed with the helper bundle.
        ddl=Path('/run/yeodam-b388/journal.sql').read_text()
        # Each CREATE is idempotent: an existing trip journal must not suppress a new auth journal.
        mysql('SET SESSION sql_log_bin=0; USE '+SCHEMA+'; '+ddl)
        if mysql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='"+SCHEMA+"' AND table_name IN ('migration_trip_request_result','migration_auth_request_result');")!='2':
            raise RuntimeError('Required migration journals missing; do not proceed to Writer cutover')
        completed=True
        print(json.dumps({'phase':'bulk_imported','runId':run_id,'tables':int(mysql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='"+SCHEMA+"';")),
                          'trips':int(mysql('SELECT COUNT(*) FROM '+SCHEMA+'.trips;')),'sourceFile':info['source_file'],'sourcePosition':int(info['source_position']),
                          'nativeReplicationStarted':False,'targetBackup':backup}),flush=True)
    finally:
        # CDC account alone may bypass read_only. Application writes remain fenced.
        mysql('SET GLOBAL read_only=ON; SET GLOBAL super_read_only='+('OFF' if completed else 'ON')+';')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run-id',required=True);p.add_argument('--source-marker',required=True);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id or '10.30.10.28' not in run(['hostname','-I']).decode().split():raise ValueError('Wrong host or Run')
    import_snapshot(a.run_id,a.source_marker)
