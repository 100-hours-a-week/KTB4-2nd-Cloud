#!/usr/bin/env python3
"""Dispatch guarded staging-only table preparation, inspection, export and run cleanup."""
import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import importlib.util

spec = importlib.util.spec_from_file_location('pool_ssm', Path(__file__).with_name('run-synthetic-pool-ssm.py'))
ssm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssm)
NAMES = {'source': 'yeodam-v2-staging-v1-source-app', 'replica': 'yeodam-v2-staging-mysql'}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'export', 'cleanup'))
    parser.add_argument('--role', required=True, choices=('source', 'replica'))
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--run-id', type=ssm.canonical_uuid)
    parser.add_argument('--confirm-run-cleanup', type=ssm.canonical_uuid)
    parser.add_argument('--profile', default='yeodam-admin')
    args = parser.parse_args()
    if args.action in ('prepare', 'cleanup') and args.role != 'source':
        raise ValueError('Writes are allowed only on the V1 staging source')
    if args.action == 'cleanup' and (not args.run_id or args.confirm_run_cleanup != args.run_id):
        raise ValueError('Cleanup requires the same run ID in both confirmation arguments')
    if args.action == 'export' and not args.run_id:
        raise ValueError('Export requires run-id')
    if ssm.aws(args.profile, 'sts', 'get-caller-identity')['Account'] != ssm.ACCOUNT:
        raise RuntimeError('Wrong AWS account')
    reservations = ssm.aws(args.profile, 'ec2', 'describe-instances', '--instance-ids', args.instance_id)['Reservations']
    instances = [i for r in reservations for i in r['Instances']]
    if len(instances) != 1:
        raise RuntimeError('Expected one staging instance')
    instance = instances[0]
    tags = {t['Key']: t['Value'] for t in instance.get('Tags', [])}
    if tags.get('Name') != NAMES[args.role] or tags.get('Environment') != 'staging' or instance['State']['Name'] != 'running':
        raise RuntimeError('Target is not the running staging host for this role')
    encoded = base64.b64encode(Path(__file__).with_name('migration-probe-db.py').read_bytes()).decode()
    remote = '/run/yeodam-migration-probe-db.py'
    command = f'python3 {remote} {args.action} --role {args.role}'
    if args.run_id: command += f' --run-id {args.run_id}'
    if args.confirm_run_cleanup: command += f' --confirm-run-cleanup {args.confirm_run_cleanup}'
    request = {'InstanceIds': [args.instance_id], 'DocumentName': 'AWS-RunShellScript',
               'Parameters': {'commands': ['set -eu', 'umask 077', f"printf '%s' '{encoded}' | base64 -d > {remote}", command]},
               'Comment': '#201 migration probe table ' + args.action}
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as file:
        json.dump(request, file); file.flush()
        command_id = ssm.aws(args.profile, 'ssm', 'send-command', '--cli-input-json', 'file://' + file.name)['Command']['CommandId']
    print('SSM command: ' + command_id, flush=True)
    for _ in range(150):
        time.sleep(2)
        try:
            result = ssm.aws(args.profile, 'ssm', 'get-command-invocation', '--command-id', command_id, '--instance-id', args.instance_id)
        except RuntimeError: continue
        if result['Status'] == 'Success':
            print(result['StandardOutputContent'].strip()); return
        if result['Status'] in ('Failed', 'Cancelled', 'TimedOut'):
            raise RuntimeError(result['StandardErrorContent'].strip()[:1000])
    raise RuntimeError('Command still running; inspect the displayed SSM command ID')

if __name__ == '__main__':
    try: main()
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print(f'ERROR: {error}', file=sys.stderr); raise SystemExit(1)
