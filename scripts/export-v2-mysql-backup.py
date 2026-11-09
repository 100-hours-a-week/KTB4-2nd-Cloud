#!/usr/bin/env python3
"""Run only on the fixed V2 MySQL EC2 after explicit freeze/export approval."""
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

import v2_v3_mysql_logical as logical
from v2_v3_migration_contract import sql_checksums

ACCOUNT='483175530259'
REGION='ap-northeast-2'
INSTANCE='i-02b2db8886a13dc25'
BUCKET='yeodam-v2-production-mysql-backup-483175530259-ap-northeast-2'
CONTAINER='yeodam-v2-mysql'
BASE=Path('/var/lib/yeodam/migration-backups')
RUN=re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}')
ROOT_SHELL='export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysql --no-defaults --protocol=SOCKET -uroot --batch --raw --skip-column-names'
UNSUPPORTED="SELECT (SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema='yeodam')+(SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema='yeodam')+(SELECT COUNT(*) FROM information_schema.events WHERE event_schema='yeodam')+(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='yeodam' AND (table_type<>'BASE TABLE' OR engine<>'InnoDB'));"


def identity_document():
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    token=opener.open(urllib.request.Request('http://169.254.169.254/latest/api/token',
        method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=3).read().decode()
    request=urllib.request.Request('http://169.254.169.254/latest/dynamic/instance-identity/document',
        headers={'X-aws-ec2-metadata-token':token})
    return json.loads(opener.open(request,timeout=3).read())


def assert_host():
    logical.require(os.geteuid()==0 and not os.environ.get('GITHUB_ACTIONS'),'Root on approved EC2 only; CI forbidden')
    identity=identity_document()
    logical.require(identity.get('accountId')==ACCOUNT and identity.get('region')==REGION
                    and identity.get('instanceId')==INSTANCE,'Wrong source host')
    detail=json.loads(logical.docker('inspect',CONTAINER))[0]
    logical.require(detail['State']['Running'] and detail['Config']['Image']==logical.SOURCE_IMAGE,'Source image mismatch')
    mounts={(m['Source'],m['Destination'],m['RW']) for m in detail['Mounts']}
    logical.require(('/srv/yeodam/mysql','/var/lib/mysql',True) in mounts
        and ('/etc/yeodam/mysql-root-password','/run/secrets/mysql-root-password',False) in mounts,'Source mounts mismatch')
    return identity


def sql(query):
    return logical.docker('exec','-i',CONTAINER,'/bin/bash','-ec',ROOT_SHELL,source=query).strip()


def validate_history(expected):
    rows=sql("SELECT JSON_OBJECT('version',version,'script',script,'checksum',checksum,'success',success) FROM yeodam.flyway_schema_history ORDER BY installed_rank;").splitlines()
    logical.require(len(rows)==32,'Expected V32 history')
    for version,line in enumerate(rows,1):
        row=json.loads(line)
        logical.require(row['version']==str(version) and row['script']==expected[version]['script']
            and row['checksum']==expected[version]['checksum'] and row['success']==1,'Source Flyway checksum mismatch')


def upload(path,key):
    # Fixed bucket/prefix, create-only conditional write, explicit server-side encryption.
    md5=base64.b64encode(hashlib.md5(path.read_bytes()).digest()).decode()
    result=subprocess.run(['aws','s3api','put-object','--region',REGION,'--bucket',BUCKET,
        '--key',key,'--body',str(path),'--server-side-encryption','AES256',
        '--if-none-match','*','--content-md5',md5,
        '--metadata','sha256='+logical.file_digest(path)],capture_output=True,text=True,timeout=180)
    logical.require(result.returncode==0,'Private upload failed; details suppressed')
    check=subprocess.run(['aws','s3api','head-object','--region',REGION,'--bucket',BUCKET,'--key',key],
        capture_output=True,text=True,timeout=30)
    logical.require(check.returncode==0,'Uploaded object unavailable')
    value=json.loads(check.stdout)
    logical.require(value.get('ServerSideEncryption')=='AES256'
        and value['ContentLength']==path.stat().st_size
        and value.get('Metadata',{}).get('sha256')==logical.file_digest(path),'Uploaded object mismatch')
    return value['VersionId']


def export(run_id,sql_directory):
    logical.require(RUN.fullmatch(run_id),'Invalid run ID')
    assert_host()
    expected=sql_checksums(sql_directory)
    logical.require(sql('SELECT VERSION();')=='9.7.2','Source engine mismatch')
    validate_history(expected)
    logical.require(sql(UNSUPPORTED)=='0','Unsupported database objects require review')
    logical.require(not BASE.is_symlink(),'Symlink backup root refused')
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
    logical.require(BASE.is_dir() and BASE.stat().st_uid==0 and BASE.stat().st_mode & 0o077==0,'Backup root must be root-owned 0700')
    destination=BASE/run_id
    destination.mkdir(mode=0o700) # Exclusive run ID; refuse repeats before freezing.
    before=sql('SELECT CONCAT(@@global.read_only,\':\',@@global.super_read_only);')
    logical.private_json(destination/'freeze.json',{'previousReadOnlyFlags':before,
        'sourceInstance':INSTANCE,'createdAt':datetime.now(timezone.utc).isoformat()})
    # No application/GPU/SG edits. Preserve the freeze on failure until Source is stopped.
    sql('SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;')
    logical.require(sql('SELECT @@global.read_only AND @@global.super_read_only;')=='1','Source freeze failed')
    logical.require(sql('SELECT COUNT(*) FROM information_schema.innodb_trx WHERE trx_mysql_thread_id<>CONNECTION_ID();')=='0','Active transaction; leave frozen and inspect')
    tables=logical.capture(None,query_fn=sql)
    package=destination/'package';package.mkdir(mode=0o700)
    archive=package/'yeodam.sql.gz'
    command='export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec mysqldump --no-defaults --protocol=SOCKET -uroot '+' '.join(logical.DUMP_OPTIONS)+' yeodam'
    proc=None
    try:
        with tempfile.TemporaryFile() as err:
            proc=subprocess.Popen(['docker','exec',CONTAINER,'/bin/bash','-ec',command],stdout=subprocess.PIPE,stderr=err)
            with os.fdopen(os.open(archive,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'wb') as raw:
                with gzip.GzipFile(fileobj=raw,mode='wb',mtime=0) as output:
                    size=0
                    for chunk in iter(lambda:proc.stdout.read(65536),b''):
                        size+=len(chunk);logical.require(size<=logical.MAX_SQL,'Dump exceeds reviewed size');output.write(chunk)
            logical.require(proc.wait(timeout=180)==0,'Logical export failed; private stderr suppressed')
        logical.require(sql('SELECT @@global.read_only AND @@global.super_read_only;')=='1'
            and logical.capture(None,query_fn=sql)==tables,'Source changed during backup')
        manifest={'formatVersion':1,'schema':'yeodam','sourceEngine':'9.7.2','sourceSchemaVersion':32,
            'sourceFrozen':True,'sensitivity':'private-user-data','sourceInstance':INSTANCE,
            'sourceUuid':sql('SELECT @@server_uuid;'),'tables':tables,
            'compressedBytes':archive.stat().st_size,'sha256':logical.file_digest(archive),
            'createdAt':datetime.now(timezone.utc).isoformat(),'runId':run_id}
        logical.private_json(package/'backup.json',manifest)
        logical.verify_package(package)
        prefix='migration/v2-v3/'+run_id+'/'
        versions={name:upload(package/name,prefix+name) for name in ['yeodam.sql.gz','backup.json']}
        # Ready is written last; a partial upload must never be restored.
        ready=destination/'ready.json'
        logical.private_json(ready,{'formatVersion':1,'runId':run_id,'bucket':BUCKET,
            'sourceInstance':INSTANCE,'files':{name:{'versionId':versions[name],
                'sha256':logical.file_digest(package/name)} for name in versions}})
        upload(ready,prefix+'ready.json')
        return {'backedUp':True,'runId':run_id,'bucket':BUCKET,'prefix':prefix,
            'compressedBytes':manifest['compressedBytes'],'tables':len(tables),
            'sourceFrozen':True,'sourceMustBeStopped':True}
    except Exception:
        if proc and proc.poll() is None:proc.kill();proc.wait()
        # Private evidence is retained; no cleanup of source data or prior backups.
        raise

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze-export-upload',action='store_true',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--sql-directory',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    with open('/run/lock/yeodam-mysql-full-backup.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        print(json.dumps(export(args.run_id,args.sql_directory)))
