#!/usr/bin/env python3
"""Resume a guarded, data-preserving staging reset using SSM checkpoints."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import UUID

SOURCE='i-05d320e3a87200d5a'
TARGET='i-05535ae6ff812972c'
NAMES={SOURCE:'yeodam-v2-staging-v1-source-app',TARGET:'yeodam-v2-staging-mysql'}

def aws(profile,*args):
    r=subprocess.run(['aws',*args,'--profile',profile,'--region','ap-northeast-2','--output','json'],capture_output=True,text=True)
    if r.returncode:raise RuntimeError('AWS request failed; check SSO and preserve reset ID/checkpoint')
    return json.loads(r.stdout)

def dispatch(profile,instance,commands,document='AWS-RunShellScript',parameters=None):
    request={'InstanceIds':[instance],'DocumentName':document,'Parameters':parameters if parameters is not None else {'commands':['set -eu','umask 077',*commands],'executionTimeout':['1800']}}
    with tempfile.NamedTemporaryFile('w',suffix='.json') as file:
        json.dump(request,file);file.flush();id=aws(profile,'ssm','send-command','--cli-input-json','file://'+file.name)['Command']['CommandId']
    print('SSM '+id,flush=True)
    for _ in range(900):
        time.sleep(2)
        try:r=aws(profile,'ssm','get-command-invocation','--command-id',id,'--instance-id',instance)
        except RuntimeError:continue
        if r['Status']=='Success':return r['StandardOutputContent'].strip()
        if r['Status'] in ('Failed','TimedOut','Cancelled'):raise RuntimeError('SSM '+id+' failed: '+r['StandardErrorContent'][-700:])
    raise RuntimeError('SSM still running: '+id)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--reset-id',required=True);p.add_argument('--previous-run',required=True);p.add_argument('--confirm-reset',required=True);p.add_argument('--profile',default='yeodam-admin')
    args=p.parse_args()
    if str(UUID(args.reset_id))!=args.reset_id or str(UUID(args.previous_run))!=args.previous_run or args.confirm_reset!=args.reset_id:raise ValueError('Canonical UUIDs and matching reset confirmation required')
    if aws(args.profile,'sts','get-caller-identity')['Account']!='483175530259':raise RuntimeError('Wrong AWS account')
    for instance,name in NAMES.items():
        i=aws(args.profile,'ec2','describe-instances','--instance-ids',instance)['Reservations'][0]['Instances'][0]
        tags={v['Key']:v['Value'] for v in i['Tags']}
        if tags.get('Name')!=name or tags.get('Environment')!='staging' or i['State']['Name']!='running':raise RuntimeError('Unexpected staging host')
    directory=Path('/private/tmp')/('yeodam-reset-'+args.reset_id);directory.mkdir(mode=0o700,exist_ok=True)
    path=directory/'checkpoint.json'
    state=json.loads(path.read_text()) if path.exists() else {'resetId':args.reset_id,'previousRun':args.previous_run,'done':[]}
    if state['previousRun']!=args.previous_run:raise RuntimeError('Checkpoint mismatch')
    if 'resume-source' in state['done']:
        print('This reset is already complete; no host changes performed.');return
    def checkpoint(name):
        state['done'].append(name);path.write_text(json.dumps(state,indent=2));path.chmod(0o600)
    encoded=base64.b64encode(Path(__file__).with_name('staging-rehearsal-reset.py').read_bytes()).decode()
    # Install before every resumed run; operational data is kept in host checkpoints.
    for instance in NAMES:
        dispatch(args.profile,instance,["printf '%s' '"+encoded+"' | base64 -d > /usr/local/sbin/yeodam-staging-rehearsal-reset",'chmod 0750 /usr/local/sbin/yeodam-staging-rehearsal-reset'])
    def stage(name,instance,extra='',always=False):
        if name in state['done'] and not always:return
        output=dispatch(args.profile,instance,[f'python3 /usr/local/sbin/yeodam-staging-rehearsal-reset {name} --reset-id {args.reset_id} --previous-run {args.previous_run}'+extra])
        print(output,flush=True)
        if name not in state['done']:checkpoint(name)
    stage('pause',SOURCE)
    stage('snapshot-target',TARGET)
    stage('restore-source',SOURCE)
    if 'source-snapshot' not in state['done']:
        output=dispatch(args.profile,SOURCE,[],document='yeodam-v2-staging-mysql-source-snapshot',parameters={})
        line=next(v for v in output.splitlines() if v.startswith('Source snapshot ready: '))
        state['readyKey']=line.split('s3://yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2/')[1]
        print(output,flush=True);checkpoint('source-snapshot')
    if 'initialize-replica' not in state['done']:
        stage('allow-target-initialize',SOURCE,always=True)
        output=dispatch(args.profile,TARGET,[],document='yeodam-v2-staging-mysql-initialize-replica',parameters={'snapshotReadyKey':[state['readyKey']],'confirmDestinationReset':['RESET_STAGING_V1_COMPAT_SCHEMA_AFTER_BACKUP']})
        print(output,flush=True);checkpoint('initialize-replica')
    # Always reverify before opening the writer, including resumed execution.
    proof=dispatch(args.profile,TARGET,[f'python3 /usr/local/sbin/yeodam-staging-rehearsal-reset verify-target --reset-id {args.reset_id} --previous-run {args.previous_run}'])
    obj=json.loads(proof); (directory/'target-proof.json').write_text(json.dumps(obj,indent=2))
    stage('resume-source',SOURCE,' --target-proof '+base64.b64encode(proof.encode()).decode())
    print('Reset completed. Check BE health and prepare fresh sessions before user-run k6.',flush=True)

if __name__=='__main__':
    try:main()
    except (RuntimeError,ValueError,OSError,KeyError) as e:print('ERROR: '+str(e));raise SystemExit(1)
