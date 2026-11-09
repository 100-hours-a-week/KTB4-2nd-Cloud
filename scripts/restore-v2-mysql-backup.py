#!/usr/bin/env python3
"""Download one approved V2 backup and restore only into disposable local Docker."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

import v2_v3_mysql_logical as logical
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('v3_bootstrap',ROOT/'scripts/create-v3-mysql-bootstrap-secret.py')
bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)
ACCOUNT='483175530259'
BUCKET='yeodam-v2-production-mysql-backup-483175530259-ap-northeast-2'
INSTANCE='i-02b2db8886a13dc25'
RUN=re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}')


def aws(profile,*args):
    result=subprocess.run(['aws','--profile',profile,'--region','ap-northeast-2',*map(str,args)],
        capture_output=True,text=True,timeout=180)
    logical.require(result.returncode==0,'Private AWS operation failed; response suppressed')
    return result.stdout


def download(run_id,directory,profile):
    logical.require(not os.environ.get('GITHUB_ACTIONS'),'Real backup download forbidden in CI')
    logical.require(RUN.fullmatch(run_id),'Invalid run ID')
    identity=json.loads(aws(profile,'sts','get-caller-identity'))
    logical.require(identity['Account']==ACCOUNT,'Wrong AWS account')
    logical.require(not directory.exists() and not directory.is_symlink(),'New private destination required')
    directory.mkdir(mode=0o700)
    prefix='migration/v2-v3/'+run_id+'/'
    aws(profile,'s3api','get-object','--bucket',BUCKET,'--key',prefix+'ready.json',directory/'ready.json')
    ready=json.loads((directory/'ready.json').read_text())
    logical.require(ready['formatVersion']==1 and ready['runId']==run_id
        and ready['bucket']==BUCKET and ready['sourceInstance']==INSTANCE
        and set(ready['files'])=={'yeodam.sql.gz','backup.json'},'Ready marker identity mismatch')
    package=directory/'package';package.mkdir(mode=0o700)
    for name in ['yeodam.sql.gz','backup.json']:
        expected=ready['files'][name]
        logical.require(isinstance(expected['versionId'],str) and expected['versionId'] not in ['', 'null']
            and re.fullmatch(r'[a-f0-9]{64}',expected['sha256']),'Versioned backup required')
        value=json.loads(aws(profile,'s3api','get-object','--bucket',BUCKET,'--key',prefix+name,
            '--version-id',expected['versionId'],package/name))
        (package/name).chmod(0o600)
        logical.require(value.get('ServerSideEncryption')=='AES256'
            and value.get('VersionId')==expected['versionId']
            and logical.file_digest(package/name)==expected['sha256'],'Downloaded backup mismatch')
    (directory/'ready.json').chmod(0o600)
    manifest=logical.verify_package(package)
    logical.require(manifest['sensitivity']=='private-user-data' and manifest['sourceInstance']==INSTANCE
        and manifest['runId']==run_id,'Unexpected backup source')
    return package


def schema_image_id(image):
    image_id=json.loads(logical.docker('image','inspect',image))[0]['Id']
    logical.require(re.fullmatch(r'sha256:[a-f0-9]{64}',image_id),'Immutable local schema image required')
    # Compare the actual entry script, source lock and SQL hash list with this checkout.
    script='sha256sum /schema/migrate.sh /schema/source.lock.json; cat /schema/checksums.sha256'
    actual=logical.docker('run','--rm','--network','none','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges:true','--entrypoint','/bin/bash',image_id,'-ec',script)
    lock=json.loads((ROOT/'deploy/v3/mysql-schema-image/source.lock.json').read_text())
    expected=''.join(hashlib.sha256((ROOT/'deploy/v3/mysql-schema-image'/name).read_bytes()).hexdigest()
        +'  /schema/'+name+'\n' for name in ['migrate.sh','source.lock.json'])
    expected+=''.join(item['sha256']+'  '+item['name']+'\n' for item in lock['migrations'])
    logical.require(actual==expected,'Schema image differs from pinned V35 source')
    return image_id


def restore_package(package,image,directory,after_verify=None):
    manifest=logical.verify_package(package)
    image_id=schema_image_id(image)
    logical.require(not directory.exists() and not directory.is_symlink(),'Fresh runtime directory required')
    directory.mkdir(mode=0o755)
    name='v2-v3-live-restore-'+uuid.uuid4().hex[:10]+'-target';net=name+'-net'
    created=False;network=False
    def sql(query):return logical.sql(name,query)
    try:
        cred=directory/'credentials';cred.mkdir();payload=bootstrap.create_payload(cred);cred.chmod(0o755)
        for key,filename in [('rootPassword','root-password'),('appPassword','app-password')]:
            (cred/filename).write_text(payload[key])
        for file in cred.iterdir():file.chmod(0o444) # Ephemeral synthetic credentials, never copied from prod.
        data=directory/'data';data.mkdir();data.chmod(0o777)
        logical.docker('network','create','--internal','--label','yeodam.issue=593',net);network=True
        logical.docker('run','-d','--platform','linux/amd64','--name',name,'--label','yeodam.issue=593',
            '--network',net,'--network-alias','mysql-primary.mysql.svc.cluster.local',
            '--memory','768m','--cpus','1','--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--user','999:999',
            '--tmpfs','/tmp:rw,size=128m','--tmpfs','/var/run/mysqld:rw,size=8m',
            '-v',f'{cred}:/credentials:ro','-v',f'{cred}:/tls:ro','-v',f'{data}:/var/lib/mysql',
            '-e','MYSQL_ROOT_PASSWORD_FILE=/credentials/root-password','-e','MYSQL_DATABASE=yeodam',
            '-e','MYSQL_USER=yeodam','-e','MYSQL_PASSWORD_FILE=/credentials/app-password',logical.TARGET_IMAGE,
            '--socket=/tmp/mysql.sock','--pid-file=/tmp/mysql.pid','--innodb-buffer-pool-size=64M',
            '--innodb-redo-log-capacity=100M','--max-connections=20','--require-secure-transport=ON',
            '--ssl-ca=/tls/ca.crt','--ssl-cert=/tls/tls.crt','--ssl-key=/tls/tls.key',
            '--server-id=1','--gtid-mode=ON','--enforce-gtid-consistency=ON','--log-bin=mysql-bin')
        created=True
        for _ in range(120):
            try:
                if sql('SELECT VERSION();')=='8.4.13' and logical.docker('exec',name,'/bin/bash','-ec',
                    "tr '\\0' ' ' </proc/1/cmdline").startswith('mysqld '):break
            except RuntimeError:pass
            time.sleep(2)
        else:raise RuntimeError('Isolated MySQL startup timed out')
        logical.restore(name,package)
        def migrate():
            logical.docker('run','--rm','--platform','linux/amd64','--network',net,
                '--memory','512m','--cpus','.5','--read-only','--cap-drop','ALL',
                '--security-opt','no-new-privileges:true','--tmpfs','/tmp:rw,size=32m',
                '-v',f'{cred}:/credentials:ro','-v',f'{cred}:/tls:ro',image_id)
        migrate()
        logical.require(sql("SELECT CONCAT(COUNT(*),':',SUM(success),':',MAX(CAST(version AS UNSIGNED))) FROM yeodam.flyway_schema_history;")=='35:35:35','V35 migration incomplete')
        logical.require(logical.capture(name,manifest['tables'])==manifest['tables'],'Legacy data changed')
        logical.require(logical.foreign_key_violations(name)==0,'Foreign key orphans detected')
        history=sql('SELECT * FROM yeodam.flyway_schema_history ORDER BY installed_rank;')
        new=set(logical.catalog(name))-set(manifest['tables'])
        logical.require(len(new)==2 and all(sql('SELECT COUNT(*) FROM yeodam.'+logical.identifier(t))=='0' for t in new),'Unexpected new-table contents')
        migrate()
        logical.require(sql('SELECT * FROM yeodam.flyway_schema_history ORDER BY installed_rank;')==history
            and logical.capture(name,manifest['tables'])==manifest['tables'],'V35 rerun changed data')
        if after_verify is not None:
            sql('SET GLOBAL read_only=ON; SET GLOBAL super_read_only=ON;')
            after_verify(name,manifest)
            logical.require(logical.capture(name,manifest['tables'])==manifest['tables'],'Read-only inspection changed legacy data')
        return {'restored':True,'sensitivity':manifest['sensitivity'],'legacyTablesCompared':len(manifest['tables']),
            'legacyRowsAndHistoryMatch':True,'foreignKeyViolations':0,'targetEngine':'8.4.13','targetSchema':35,
            'rerunNoop':True,'sourceDumpSha256':manifest['sha256'],'qaDatabaseAccessed':False,
            'runtimeName':name}
    finally:
        # No DB logs retained: errors may include user values. Cleanup only our named runtime.
        if created:logical.docker('rm','-f',name)
        if network:logical.docker('network','rm',net)
        data=directory/'data'
        if data.exists():
            logical.docker('run','--rm','--platform','linux/amd64','--network','none',
                '--read-only','--cap-drop','ALL','--user','999:999','-v',f'{data}:/cleanup',
                '--entrypoint','/bin/bash',logical.TARGET_IMAGE,'-ec',
                'rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?*')
        shutil.rmtree(directory)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download-and-restore',action='store_true',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--image',required=True)
    parser.add_argument('--profile',default='yeodam-admin')
    args=parser.parse_args();os.umask(0o077)
    package=download(args.run_id,args.directory,args.profile)
    result=restore_package(package,args.image,args.directory/'runtime')
    logical.private_json(args.directory/'result.json',{**result,'runtimeCleaned':True})
    print('PASS: private V2 backup restored and V35 verified; isolated runtime removed')
