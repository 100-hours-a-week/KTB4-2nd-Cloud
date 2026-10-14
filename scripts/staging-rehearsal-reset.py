#!/usr/bin/env python3
"""Controlled staging maintenance between experiments, never production failback."""
import argparse
import base64
import fcntl
import gzip
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import UUID

SCHEMA='yeodam_v1_source'
BASE=Path('/var/lib/yeodam/rehearsal-reset')
BUCKET='yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2'

def require(ok,message):
    if not ok: raise RuntimeError(message)

def run(args,sql=None):
    p=subprocess.run(args,input=sql,text=True,capture_output=True,timeout=600)
    require(p.returncode==0,'Reset command failed; keep application paused and inspect the checkpoint')
    return p.stdout.rstrip('\n')

def mysql_args(role,tool='mysql'):
    if role=='source': return [tool,'--protocol=socket','-uroot']
    # Password stays inside the container process environment, never in SSM.
    return ['docker','exec','-i','yeodam-v2-mysql','sh','-ec',
            'export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec '+tool+' -uroot "$@"','sh']

def query(role,sql): return run(mysql_args(role)+['--batch','--skip-column-names'],sql)

def identity(role): return query(role,'SELECT @@server_id,@@server_uuid,@@read_only,@@super_read_only;').split('\t')

def sha_file(path):
    digest=hashlib.sha256()
    with path.open('rb') as file:
        while chunk:=file.read(65536): digest.update(chunk)
    return digest.hexdigest()

def stream(role,path,data_only=False):
    options=['--single-transaction','--quick','--set-gtid-purged=OFF','--hex-blob','--order-by-primary','--default-character-set=utf8mb4']
    if data_only: options += ['--compact','--no-create-info','--skip-triggers','--skip-extended-insert','--skip-add-locks','--skip-disable-keys']
    else: options += ['--routines','--events','--triggers']
    digest=hashlib.sha256()
    with tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(mysql_args(role,'mysqldump')+options+[SCHEMA],stdout=subprocess.PIPE,stderr=errors)
        with path.open('wb') as file, gzip.GzipFile(fileobj=file,mode='wb',mtime=0) as compressed:
            while chunk:=process.stdout.read(65536):
                digest.update(chunk); compressed.write(chunk)
        require(process.wait()==0,'Schema dump failed; application must remain paused')
    path.chmod(0o600)
    return digest.hexdigest()

def checks(role,directory):
    require(query(role,f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{SCHEMA}' AND table_type='BASE TABLE' AND ENGINE!='InnoDB';")=='0','Only InnoDB rehearsal tables are supported')
    require(query(role,f"SELECT COUNT(*) FROM information_schema.events WHERE event_schema='{SCHEMA}';")=='0','Schema event writers need a separate isolation plan')
    tables=query(role,f"SELECT table_name FROM information_schema.tables WHERE table_schema='{SCHEMA}' AND table_type='BASE TABLE' ORDER BY table_name;").splitlines()
    require(tables and all(t.replace('_','').isalnum() for t in tables),'Invalid schema table list')
    counts={table:int(query(role,f'SELECT COUNT(*) FROM `{SCHEMA}`.`{table}`;')) for table in tables}
    data=directory/f'{role}.data.sql.gz'
    sha=stream(role,data,True);data.unlink()
    return {'rows':counts,'dataSha256':sha}

def s3(args):return run(['aws','s3',*args,'--region','ap-northeast-2','--only-show-errors'])

def load_source_module():
    spec=importlib.util.spec_from_loader('cutover',SourceFileLoader('cutover','/usr/local/sbin/yeodam-staging-db-cutover'))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m.Database('10.30.10.28')

def save(path,state):
    fd,tmp=tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd,'w') as f: json.dump(state,f,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    mirror=BASE/'active.json'
    fd,tmp=tempfile.mkstemp(dir=BASE)
    with os.fdopen(fd,'w') as f:json.dump(state,f,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,mirror)

def execute(args):
    require(os.geteuid()==0 and str(UUID(args.reset_id))==args.reset_id,'Root and a canonical reset UUID are required')
    require(str(UUID(args.previous_run))==args.previous_run,'Previous Cutover Run UUID is required')
    role='target' if args.action=='snapshot-target' else 'source'
    ident=identity(role)
    require(ident[0]==('2' if role=='target' else '1'),'Wrong staging DB role')
    require(('10.30.10.28' if role=='target' else '10.30.0.174') in run(['hostname','-I']).split(),'Wrong staging private address')
    require(run(['aws','sts','get-caller-identity','--region','ap-northeast-2','--query','Account','--output','text'])=='483175530259','Wrong AWS account')
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True);BASE.chmod(0o700)
    directory=BASE/args.reset_id;directory.mkdir(mode=0o700,exist_ok=True)
    prefix='migration/rehearsal-reset/'+args.reset_id+'/'
    checkpoint=directory/'state.json'
    active=BASE/'active.json'
    if role=='source' and active.exists():
        current=json.loads(active.read_text())
        require(current.get('resetId')==args.reset_id or current.get('phase')=='completed','Another reset is incomplete; resume its reset UUID')
    state=json.loads(checkpoint.read_text()) if checkpoint.exists() else {'resetId':args.reset_id,'previousRun':args.previous_run}
    require(state['previousRun']==args.previous_run,'Checkpoint belongs to another cutover')
    with (BASE/'lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.action=='snapshot-target':
            require(ident[2:]==['1','1'],'Target must be fenced before capture')
            require(query('target','SELECT COUNT(*) FROM information_schema.innodb_trx;')=='0','Target has active transactions')
            if 'manifest' not in state:
                manifest={'resetId':args.reset_id,'previousRun':args.previous_run,'schema':SCHEMA,'targetUuid':ident[1],**checks('target',directory)}
                dump=directory/'target.sql.gz';stream('target',dump)
                manifest['dumpSha256']=sha_file(dump)
                manifest['dumpKey']=prefix+'target.sql.gz'
                manifest['gtid']=query('target','SELECT @@GLOBAL.gtid_executed;')
                s3(['cp',str(dump),'s3://'+BUCKET+'/'+manifest['dumpKey'],'--sse','AES256'])
                marker=directory/'target.ready';marker.write_text(json.dumps(manifest));marker.chmod(0o600)
                s3(['cp',str(marker),'s3://'+BUCKET+'/'+prefix+'target.ready','--sse','AES256'])
                state['manifest']=manifest;save(checkpoint,state)
            print(json.dumps({'action':args.action,'readyKey':prefix+'target.ready','manifest':state['manifest']}));return

        db=load_source_module()
        try:
            if args.action!='pause':
                require('backendId' in state and run(['docker','inspect','--format','{{.State.Running}}',state['backendId']])=='false','BE must remain stopped during reset')
            journal_path=Path('/var/lib/yeodam/migration-cutover/active.json')
            if args.action=='pause':
                if state.get('phase')=='completed':raise RuntimeError('Reset already completed; use a new reset UUID')
                if 'backendId' not in state:
                    journal=json.loads(journal_path.read_text())
                    require(journal['runId']==args.previous_run and journal['phase']=='switched','Expected completed previous handoff')
                    require(db.identity('source')[1:]==[journal['sourceUuid'],'1','1'] and db.identity('target')[1:]==[journal['targetUuid'],'0','0'],'Unexpected current writer')
                    backend=run(['docker','ps','--filter','label=com.docker.compose.project=yeodam-app','--filter','label=com.docker.compose.service=backend','--quiet'])
                    require(backend and '\n' not in backend,'Expected one BE')
                    require(run(['docker','exec',backend,'sh','-c','printf %s "$MYSQL_URL"'])=='jdbc:mysql://proxysql:6033/yeodam_v1_source','BE must use ProxySQL')
                    state.update(backendId=backend,journal=journal,phase='pause_intent');save(checkpoint,state)
                run(['docker','stop','--time','30',state['backendId']])
                db.query('target','SET SESSION lock_wait_timeout=5; SET PERSIST super_read_only=ON;',timeout=10)
                require(db.identity('target')[2:]==['1','1'],'Target fence failed')
                db.query('proxy',"UPDATE mysql_servers SET status='OFFLINE_SOFT' WHERE hostgroup_id=20; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
                state['phase']='paused';save(checkpoint,state)
            elif args.action=='restore-source':
                require(state.get('phase') in ('paused','restore_intent','source_restored'),'Pause first')
                require(db.identity('target')[2:]==['1','1'],'Target must stay fenced')
                marker=directory/'target.ready';s3(['cp','s3://'+BUCKET+'/'+prefix+'target.ready',str(marker)])
                manifest=json.loads(marker.read_text())
                require(manifest['resetId']==args.reset_id and manifest['previousRun']==args.previous_run and manifest['schema']==SCHEMA and manifest['targetUuid']==db.identity('target')[1],'Wrong preserved dataset')
                require(db.query('target','SELECT @@GLOBAL.gtid_executed;')==manifest['gtid'],'Target changed after snapshot')
                require(manifest['dumpKey']==prefix+'target.sql.gz','Snapshot escapes this reset prefix')
                dump=directory/'target.sql.gz';s3(['cp','s3://'+BUCKET+'/'+manifest['dumpKey'],str(dump)])
                require(sha_file(dump)==manifest['dumpSha256'],'Snapshot checksum mismatch')
                if 'originalSourceKey' not in state:
                    original=directory/'original-source.sql.gz';stream('source',original)
                    key=prefix+'original-source.sql.gz';s3(['cp',str(original),'s3://'+BUCKET+'/'+key,'--sse','AES256'])
                    state.update(originalSourceKey=key,originalSourceSha256=sha_file(original));save(checkpoint,state)
                    old=directory/'original-source.ready';old.write_text(json.dumps({'schema':SCHEMA,'resetId':args.reset_id,'previousRun':args.previous_run,'sourceUuid':ident[1],'dumpKey':key,'dumpSha256':state['originalSourceSha256']}));old.chmod(0o600)
                    s3(['cp',str(old),'s3://'+BUCKET+'/'+prefix+'original-source.ready','--sse','AES256'])
                state.update(phase='restore_intent',manifest=manifest);save(checkpoint,state)
                require(db.identity('source')[2:]==['1','1'],'Source must be fenced before import')
                query('source','SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
                try:
                    with gzip.open(dump,'rb') as content, tempfile.TemporaryFile() as errors:
                        proc=subprocess.Popen(mysql_args('source'),stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=errors)
                        proc.stdin.write(f'SET SESSION sql_log_bin=0; SET FOREIGN_KEY_CHECKS=0; DROP DATABASE `{SCHEMA}`; CREATE DATABASE `{SCHEMA}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; USE `{SCHEMA}`;\n'.encode())
                        while chunk:=content.read(65536):proc.stdin.write(chunk)
                        proc.stdin.close();require(proc.wait()==0,'Import failed; keep both DBs fenced')
                finally:query('source','SET PERSIST super_read_only=ON;')
                require(checks('source',directory)=={'rows':manifest['rows'],'dataSha256':manifest['dataSha256']},'Source restore data mismatch')
                state['phase']='source_restored';save(checkpoint,state)
            elif args.action=='allow-target-initialize':
                require(state.get('phase')=='source_restored' and db.identity('source')[2:]==['1','1'],'Restore Source before rebuilding replica')
                # The existing guarded initialize document makes a full backup
                # before replacing only the compatible schema. BE stays stopped.
                db.query('target','SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
            elif args.action=='resume-source':
                require(state.get('phase') in ('source_restored','resume_intent','completed'),'Source restore not verified')
                if state.get('phase')=='completed':print(json.dumps({'action':args.action,'phase':'completed'}));return
                db.query('target','SET SESSION lock_wait_timeout=5; SET PERSIST super_read_only=ON;',timeout=10)
                status=db.status()
                require(status['Replica_IO_Running']=='Yes' and status['Replica_SQL_Running']=='Yes' and status['Last_IO_Errno']=='0' and status['Last_SQL_Errno']=='0','Replica not healthy')
                require(status['Source_UUID']==db.identity('source')[1] and status['Source_Host']=='10.30.0.174','Wrong replication source')
                expected={'rows':state['manifest']['rows'],'dataSha256':state['manifest']['dataSha256']}
                require(checks('source',directory)==expected,'Source data changed before resume')
                # Remote target canonical verification is supplied by the
                # wrapper from a root-only target snapshot before resume.
                proof=json.loads(base64.b64decode(args.target_proof))
                require(proof['resetId']==args.reset_id and proof['checks']==expected and proof['uuid']==db.identity('target')[1],'Target data mismatch')
                state['phase']='resume_intent';save(checkpoint,state)
                db.query('proxy',"DELETE FROM mysql_query_rules WHERE rule_id=2050; LOAD MYSQL QUERY RULES TO RUNTIME; SAVE MYSQL QUERY RULES TO DISK; UPDATE mysql_users SET default_hostgroup=10 WHERE username='yeodam_app'; LOAD MYSQL USERS TO RUNTIME; SAVE MYSQL USERS TO DISK; UPDATE mysql_servers SET status='ONLINE' WHERE hostgroup_id=10; UPDATE mysql_servers SET status='OFFLINE_SOFT' WHERE hostgroup_id=20; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
                query('source','SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
                archive=directory/'previous-cutover.json';require(not archive.exists() or json.loads(archive.read_text())['runId']==args.previous_run,'Journal archive conflict')
                if journal_path.exists():
                    require(json.loads(journal_path.read_text())['runId']==args.previous_run,'Journal changed during reset')
                    archive.write_bytes(journal_path.read_bytes());archive.chmod(0o600);journal_path.unlink()
                Path('/var/lib/yeodam/mysql-metrics/cutover.prom').unlink(missing_ok=True)
                run(['docker','start',state['backendId']])
                deadline=time.monotonic()+150
                while True:
                    health=run(['docker','inspect','--format','{{.State.Health.Status}}',state['backendId']])
                    if health=='healthy':break
                    require(time.monotonic()<deadline,'BE did not become healthy; keep reset checkpoint')
                    time.sleep(3)
                state['phase']='completed';save(checkpoint,state)
            print(json.dumps({'action':args.action,'phase':state.get('phase'),'resetId':args.reset_id}))
        finally:db.close()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('pause','snapshot-target','restore-source','allow-target-initialize','resume-source','verify-target'))
    p.add_argument('--reset-id',required=True);p.add_argument('--previous-run',required=True);p.add_argument('--target-proof',default='')
    args=p.parse_args()
    if args.action=='verify-target':
        require(os.geteuid()==0 and identity('target')[0]=='2' and '10.30.10.28' in run(['hostname','-I']).split(),'Verify only the staging replica')
        raw=run(mysql_args('target')+['--batch'],"SHOW REPLICA STATUS FOR CHANNEL 'v1_to_v2';").splitlines()
        require(len(raw)==2 and len(raw[0].split('\t'))==len(raw[1].split('\t')),'Expected one complete replica status')
        status=dict(zip(raw[0].split('\t'),raw[1].split('\t')))
        require(status['Replica_IO_Running']=='Yes' and status['Replica_SQL_Running']=='Yes' and status['Last_IO_Errno']=='0' and status['Last_SQL_Errno']=='0','Verify only a healthy running replica, never a later Writer')
        query('target','SET SESSION lock_wait_timeout=5; SET PERSIST super_read_only=ON;')
        require(identity('target')[2:]==['1','1'],'Target fence failed')
        with tempfile.TemporaryDirectory() as d:proof={'resetId':args.reset_id,'uuid':identity('target')[1],'checks':checks('target',Path(d))}
        print(json.dumps(proof));return
    execute(args)

if __name__=='__main__':
    try:main()
    except (RuntimeError,ValueError,OSError,subprocess.SubprocessError) as error:
        print('ERROR: '+str(error),file=sys.stderr);raise SystemExit(1)
