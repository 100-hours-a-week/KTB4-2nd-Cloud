#!/usr/bin/env python3
"""Capture private no-reboot images of the two approved migration rehearsal hosts.

Does not stop/reboot instances, modify disks, register connectors or start apps.
Snapshots are crash-consistent OS copies; use the reviewed logical backup for data.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess

ACCOUNT = '483175530259'
REGION = 'ap-northeast-2'
ORIGINALS = {
    'source': {'id': 'i-05d320e3a87200d5a', 'type': 't3.medium', 'data_gib': 20},
    'target': {'id': 'i-05535ae6ff812972c', 'type': 't4g.medium', 'data_gib': 120},
}


def aws(profile, *args):
    result = subprocess.run(['aws', '--profile', profile, '--region', REGION, *args,
                             '--output', 'json'], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('AWS ' + '/'.join(args[:2]) + ' failed; inspect privately')
    return json.loads(result.stdout)


def validate_originals(rows, volumes):
    instances = {row['InstanceId']: row for row in rows}
    disks = {row['VolumeId']: row for row in volumes}
    if set(instances) != {v['id'] for v in ORIGINALS.values()}:
        raise ValueError('Unexpected original instances')
    for role, expected in ORIGINALS.items():
        row = instances[expected['id']]
        if row['InstanceType'] != expected['type'] or row['State']['Name'] != 'running':
            raise ValueError('Original ' + role + ' type/state differs')
        mapping = {d['DeviceName']: d['Ebs']['VolumeId'] for d in row['BlockDeviceMappings']}
        if set(mapping) != {'/dev/sda1', '/dev/sdf'}:
            raise ValueError('Unexpected original disk layout')
        for device, size in [('/dev/sda1', 30), ('/dev/sdf', expected['data_gib'])]:
            disk = disks[mapping[device]]
            if not disk['Encrypted'] or (disk['Size'], disk['VolumeType'], disk['Iops'], disk['Throughput']) != (size, 'gp3', 3000, 125):
                raise ValueError('Original ' + role + ' disk contract differs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['capture', 'status'])
    parser.add_argument('--profile', default='yeodam-admin')
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--tfvars', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if aws(args.profile, 'sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise ValueError('Wrong AWS account')
    if args.action == 'capture':
        if args.receipt.exists():
            raise ValueError('Receipt already exists; use status, do not create more snapshots')
        response = aws(args.profile, 'ec2', 'describe-instances', '--instance-ids',
                       *[v['id'] for v in ORIGINALS.values()])
        rows = [i for r in response['Reservations'] for i in r['Instances']]
        ids = [d['Ebs']['VolumeId'] for i in rows for d in i['BlockDeviceMappings']]
        disks = aws(args.profile, 'ec2', 'describe-volumes', '--volume-ids', *ids)['Volumes']
        validate_originals(rows, disks)
        receipt = {'account': ACCOUNT, 'region': REGION, 'noReboot': True,
                   'createdAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   'originals': rows, 'volumes': disks, 'images': {}}
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, default=str) + '\n')
        suffix = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        for role, expected in ORIGINALS.items():
            tags = [{'Key': 'MigrationCloneIssue', 'Value': '444'},
                    {'Key': 'MigrationCloneRole', 'Value': role},
                    {'Key': 'OriginalInstanceId', 'Value': expected['id']}]
            specs = [{'ResourceType': kind, 'Tags': tags} for kind in ['image', 'snapshot']]
            response = aws(args.profile, 'ec2', 'create-image', '--instance-id', expected['id'],
                           '--name', 'yeodam-migration-perf-' + role + '-' + suffix,
                           '--description', 'Issue 444 private no-reboot OS clone; logical data restored separately',
                           '--no-reboot', '--tag-specifications', json.dumps(specs))
            receipt['images'][role] = {'ami_id': response['ImageId']}
            args.receipt.write_text(json.dumps(receipt, indent=2, default=str) + '\n')
            print(json.dumps({'role': role, 'imageId': response['ImageId'], 'state': 'requested'}), flush=True)
    receipt = json.loads(args.receipt.read_text())
    if (receipt.get('account'), receipt.get('region'), receipt.get('noReboot')) != (ACCOUNT, REGION, True):
        raise ValueError('Unexpected receipt')
    if set(receipt['images']) != set(ORIGINALS):
        raise ValueError('Partial capture: preserve receipt and inspect before further action')
    images = {}
    all_ready = True
    for role, ref in receipt['images'].items():
        image = aws(args.profile, 'ec2', 'describe-images', '--image-ids', ref['ami_id'])['Images'][0]
        if image.get('Public') or image['OwnerId'] != ACCOUNT:
            raise ValueError('Clone AMI must be private and account owned')
        mapping = {d['DeviceName']: d['Ebs'] for d in image['BlockDeviceMappings'] if 'Ebs' in d}
        if set(mapping) != {'/dev/sda1', '/dev/sdf'}:
            raise ValueError('Unexpected clone image disk mapping')
        snapshots = aws(args.profile, 'ec2', 'describe-snapshots', '--snapshot-ids',
                        *[d['SnapshotId'] for d in mapping.values()])['Snapshots']
        completed = all(s['State'] == 'completed' and s['Encrypted'] for s in snapshots)
        print(json.dumps({'role': role, 'imageId': image['ImageId'], 'state': image['State'],
                          'snapshotsCompleted': completed}), flush=True)
        if image['State'] != 'available' or not completed:
            all_ready = False
        images[role] = {'ami_id': image['ImageId'], 'data_snapshot_id': mapping['/dev/sdf']['SnapshotId']}
    if not all_ready:
        return
    args.tfvars.parent.mkdir(parents=True, exist_ok=True)
    args.tfvars.write_text(json.dumps({'images': images}, indent=2) + '\n')
    print('Both private images and four encrypted snapshots are ready; wrote ignored tfvars', flush=True)


if __name__ == '__main__':
    main()
