#!/usr/bin/env python3
"""Bounded V2-only start/export/stop. Never changes ECS, GPU or V3 resources."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time

from v2_v3_migration_contract import sql_checksums
from v2_v3_mysql_logical import require, private_json

ROOT=Path(__file__).resolve().parents[1]
ACCOUNT='483175530259'
REGION='ap-northeast-2'
INSTANCE='i-02b2db8886a13dc25'
BUCKET='yeodam-v2-production-mysql-backup-483175530259-ap-northeast-2'
RUN=re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}')
FILES=['scripts/export-v2-mysql-backup.py','scripts/v2_v3_mysql_logical.py',
       'scripts/v2_v3_migration_contract.py','deploy/v3/mysql-schema-image/source.lock.json']


def build_bundle(sql_directory):
    sql_checksums(sql_directory)
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as tar:
        entries={name:ROOT/name for name in FILES}
        entries.update({'sql/'+p.name:p for p in sorted(sql_directory.iterdir())})
        for name,path in entries.items():
            require(path.is_file() and not path.is_symlink(),'Regular bundle files required')
            data=path.read_bytes();info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600
            tar.addfile(info,io.BytesIO(data))
    return stream.getvalue()


def source_command(run_id,bundle_sha):
    require(RUN.fullmatch(run_id) and re.fullmatch(r'[a-f0-9]{64}',bundle_sha),'Invalid execution binding')
    # The bundle is made above from an exact allowlist; it contains no credentials or DB rows.
    base='/var/lib/yeodam/migration-tools/'+run_id
    key='migration/v2-v3/'+run_id+'/tools.tar.gz'
    return '\n'.join(['set -Eeuo pipefail','umask 077',
        'test "$(id -u)" = 0',f'install -d -m 700 /var/lib/yeodam/migration-tools',
        f'mkdir -m 700 {base}',f'cd {base}',
        f'aws s3api get-object --region {REGION} --bucket {BUCKET} --key {key} tools.tar.gz >/dev/null',
        f"printf '%s  tools.tar.gz\\n' '{bundle_sha}' | sha256sum -c - >/dev/null",
        'tar --no-same-owner --no-same-permissions -xzf tools.tar.gz',
        f'PYTHONDONTWRITEBYTECODE=1 python3 scripts/export-v2-mysql-backup.py --freeze-export-upload --run-id {run_id} --sql-directory sql'])


def execute(run_id,sql_directory,profile,evidence):
    require(not os.environ.get('GITHUB_ACTIONS') and RUN.fullmatch(run_id),'Manual approved source backup only')
    require(not evidence.exists() and not evidence.is_symlink(),'New evidence path required')
    evidence.mkdir(mode=0o700)
    def aws(*args):
        result=subprocess.run(['aws','--profile',profile,'--region',REGION,*map(str,args)],
            capture_output=True,text=True,timeout=60)
        require(result.returncode==0,'AWS operation failed; private output suppressed')
        return json.loads(result.stdout) if result.stdout.strip() else {}
    require(aws('sts','get-caller-identity')['Account']==ACCOUNT,'Wrong account')
    instances=aws('ec2','describe-instances','--instance-ids',INSTANCE)['Reservations'][0]['Instances']
    require(len(instances)==1 and instances[0]['InstanceId']==INSTANCE
        and instances[0]['State']['Name']=='stopped' and instances[0]['InstanceType']=='t4g.medium',
        'Source must be the stopped original MySQL')
    cluster='yeodam-v2-production-app'
    services=aws('ecs','list-services','--cluster',cluster)['serviceArns']
    require(len(services)==4,'Expected four paused V2 services')
    details=aws('ecs','describe-services','--cluster',cluster,'--services',*services)
    require(not details.get('failures') and len(details['services'])==4 and all(
        s['desiredCount']==0 and s['runningCount']==0 and s['pendingCount']==0 for s in details['services']),
        'V2 ECS must remain paused')
    encryption=aws('s3api','get-bucket-encryption','--bucket',BUCKET)
    require(any(r['ApplyServerSideEncryptionByDefault']['SSEAlgorithm']=='AES256' for r in
        encryption['ServerSideEncryptionConfiguration']['Rules']),'Backup encryption mismatch')
    require(aws('s3api','get-bucket-versioning','--bucket',BUCKET).get('Status')=='Enabled','Versioned backup bucket required')
    bundle=build_bundle(sql_directory);bundle_path=evidence/'tools.tar.gz';bundle_path.write_bytes(bundle);bundle_path.chmod(0o600)
    sha=hashlib.sha256(bundle).hexdigest()
    aws('s3api','put-object','--bucket',BUCKET,'--key','migration/v2-v3/'+run_id+'/tools.tar.gz',
        '--body',bundle_path,'--server-side-encryption','AES256','--if-none-match','*','--metadata','sha256='+sha)
    started=False;command_id=None
    try:
        # Set before the request: even an uncertain StartInstances response triggers a stop attempt.
        started=True
        aws('ec2','start-instances','--instance-ids',INSTANCE)
        for _ in range(120):
            state=aws('ssm','describe-instance-information','--filters','Key=InstanceIds,Values='+INSTANCE)['InstanceInformationList']
            if state and state[0]['PingStatus']=='Online':break
            time.sleep(5)
        else:raise RuntimeError('Source SSM startup timeout')
        command=aws('ssm','send-command','--instance-ids',INSTANCE,'--document-name','AWS-RunShellScript',
            '--timeout-seconds','120','--parameters',json.dumps({'commands':[source_command(run_id,sha)],'executionTimeout':['600']}),
            '--cloud-watch-output-config','CloudWatchOutputEnabled=false')
        command_id=command['Command']['CommandId']
        private_json(evidence/'source-command.json',{'runId':run_id,'instanceId':INSTANCE,
            'bundleSha256':sha,'commandId':command_id})
        # list-command-invocations does not retrieve private stdout/stderr.
        for _ in range(130):
            invocations=aws('ssm','list-command-invocations','--command-id',command_id)['CommandInvocations']
            status=invocations[0]['Status'] if invocations else 'Pending'
            if status=='Success':break
            require(status in ['Pending','InProgress','Delayed'],'Source export failed; stop and inspect privately')
            time.sleep(5)
        else:raise RuntimeError('Source export timeout; stop before further work')
        private_json(evidence/'source-export.json',{'runId':run_id,'commandId':command_id,'exportSucceeded':True})
    finally:
        if started:
            # Exact source only. Never unfreeze a running source or resume ECS automatically.
            aws('ec2','stop-instances','--instance-ids',INSTANCE)
            for _ in range(120):
                reservations=aws('ec2','describe-instances','--instance-ids',INSTANCE)['Reservations']
                state=reservations[0]['Instances'][0]['State']['Name']
                if state=='stopped':
                    private_json(evidence/'source-stopped.json',{'instanceId':INSTANCE,'state':'stopped'})
                    break
                time.sleep(5)
            else:raise RuntimeError('Source stop not confirmed; manual exact-ID check required')
    return {'runId':run_id,'sourceStopped':True,'backupPrefix':'migration/v2-v3/'+run_id+'/'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-approved-source-backup',action='store_true',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--sql-directory',type=Path,required=True)
    parser.add_argument('--evidence-directory',type=Path,required=True)
    parser.add_argument('--profile',default='yeodam-admin')
    args=parser.parse_args();os.umask(0o077)
    print(json.dumps(execute(args.run_id,args.sql_directory,args.profile,args.evidence_directory)))
