#!/usr/bin/env python3
"""Attach AWS EC2 lifecycle state to the GPU agent scrape; keep last good target on API failure."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess

# Standalone deployment uses a local atomic writer below; no dependency on GPU host files.
import os
import tempfile


def target(instance):
    state = instance['State']['Name']
    if state not in {'pending', 'running', 'stopping', 'stopped', 'shutting-down', 'terminated'}:
        raise ValueError('Unexpected EC2 state')
    address = instance.get('PrivateIpAddress')
    if not address or ipaddress.ip_address(address) not in ipaddress.ip_network('10.30.0.0/16'):
        raise ValueError('GPU has no production private address')
    return [{'targets': [address + ':12346'], 'labels': {
        'env': 'production', 'service': 'ai-gpu', 'instance_id': instance['InstanceId'], 'ec2_state': state}}]


def refresh(instance_id, path):
    result = subprocess.run(['aws', 'ec2', 'describe-instances', '--instance-ids', instance_id,
                             '--region', 'ap-northeast-2', '--output', 'json'],
                            check=True, capture_output=True, text=True, timeout=25)
    instances = [i for r in json.loads(result.stdout)['Reservations'] for i in r['Instances']]
    if len(instances) != 1 or instances[0]['InstanceId'] != instance_id:
        raise ValueError('Expected exactly the configured GPU instance')
    content = json.dumps(target(instances[0])) + '\n'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
            temporary = file.name
            file.write(content)
            os.fchmod(file.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary): os.unlink(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--output', type=Path, default=Path('/opt/yeodam/plg/targets/gpu.json'))
    args = parser.parse_args()
    if not re.fullmatch(r'i-[0-9a-f]{17}', args.instance_id): raise SystemExit('Invalid instance ID')
    refresh(args.instance_id, args.output)
