#!/usr/bin/env python3
"""Refresh one staging ECS service's Prometheus file_sd targets; retain last good file on failure."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile

REGION = 'ap-northeast-2'
CLUSTER = 'yeodam-v2-staging-app'
SERVICE = 'yeodam-v2-staging-backend'


def aws(*args):
    p = subprocess.run(['aws', 'ecs', *args, '--region', REGION, '--output', 'json'],
                       check=True, text=True, capture_output=True, timeout=25)
    return json.loads(p.stdout)


def targets(tasks):
    result = []
    for task in tasks:
        if task.get('group') != 'service:' + SERVICE or task.get('lastStatus') != 'RUNNING' or task.get('desiredStatus') != 'RUNNING':
            continue
        addresses = [d['value'] for a in task.get('attachments', []) if a.get('type') == 'ElasticNetworkInterface'
                     for d in a.get('details', []) if d.get('name') == 'privateIPv4Address']
        if len(addresses) != 1 or ipaddress.ip_address(addresses[0]) not in ipaddress.ip_network('10.30.0.0/16'):
            raise ValueError('Expected one staging VPC address per backend task')
        result.append({'targets': [addresses[0] + ':9091'], 'labels': {
            'env': 'staging', 'service': 'backend', 'task_id': task['taskArn'].rsplit('/', 1)[1],
            'task_definition': task['taskDefinitionArn'].rsplit('/', 1)[1]}})
    return sorted(result, key=lambda row: row['targets'][0])


def refresh(path):
    arns = aws('list-tasks', '--cluster', CLUSTER, '--service-name', SERVICE, '--desired-status', 'RUNNING')['taskArns']
    rows = []
    for start in range(0, len(arns), 100):
        response = aws('describe-tasks', '--cluster', CLUSTER, '--tasks', *arns[start:start + 100])
        if response.get('failures'):
            raise RuntimeError('ECS describe-tasks returned failures')
        rows.extend(response['tasks'])
    data = targets(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
            temporary = f.name
            json.dump(data, f)
            f.write('\n')
            os.fchmod(f.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    print(json.dumps({'service': SERVICE, 'targets': len(data)}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=Path('/opt/yeodam/plg/targets/backend.json'))
    refresh(p.parse_args().output)
