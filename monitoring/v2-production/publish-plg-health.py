#!/usr/bin/env python3
"""Publish local PLG health to CloudWatch; missing publisher output is an external alarm condition."""
import argparse
import json
import re
import subprocess
import urllib.parse
import urllib.request

NAMESPACE = 'Yeodam/V2/Production'
METRIC = 'PLGHealthy'
REGION = 'ap-northeast-2'
SELF_SCRAPE = 'min(up{job="plg-prometheus"}) * (time() - max(timestamp(up{job="plg-prometheus"})) < bool 90)'


def read(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        if response.status != 200: raise ValueError('Unexpected HTTP status')
        return response.read()


def check():
    states = {}
    try:
        states['grafana'] = json.loads(read('http://127.0.0.1:3000/api/health')).get('database') == 'ok'
    except (OSError, ValueError): states['grafana'] = False
    try:
        read('http://127.0.0.1:3100/ready')
        states['loki'] = True
    except (OSError, ValueError): states['loki'] = False
    try:
        read('http://127.0.0.1:9090/-/ready')
        query = 'http://127.0.0.1:9090/api/v1/query?' + urllib.parse.urlencode({'query': SELF_SCRAPE})
        data = json.loads(read(query))
        rows = data['data']['result']
        states['prometheus'] = data['status'] == 'success' and len(rows) == 1 and float(rows[0]['value'][1]) == 1
    except (OSError, ValueError, KeyError, TypeError): states['prometheus'] = False
    return states


def publish(instance_id, states):
    healthy = int(all(states.values()))
    payload = [{'MetricName': METRIC, 'Dimensions': [{'Name': 'InstanceId', 'Value': instance_id}],
                'Unit': 'Count', 'Value': healthy, 'StorageResolution': 60}]
    subprocess.run(['aws', 'cloudwatch', 'put-metric-data', '--region', REGION,
                    '--namespace', NAMESPACE, '--metric-data', json.dumps(payload)],
                   check=True, capture_output=True, text=True, timeout=20)
    print(json.dumps({'metric': METRIC, 'healthy': healthy, 'components': states, 'published': True}))
    return healthy


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-id', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'i-[0-9a-f]{17}', args.instance_id): raise SystemExit('Invalid instance ID')
    raise SystemExit(0 if publish(args.instance_id, check()) else 1)
