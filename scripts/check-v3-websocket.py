#!/usr/bin/env python3
"""Render WebSocket access and verify exact peers and public trust; never read Secret sources."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/v3/websocket-access'


def module(name, script):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / script)
    result = importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


platform = module('ws_platform', 'check-v3-platform.py')
supply = module('ws_supply', 'prepare-v3-backend-supply.py')
require = platform.require


def render(files=()):
    command = [os.environ['V3_HELM'], 'template', 'yeodam-v3-websocket-access', str(CHART), '--kube-version', '1.36.5-eks-577a51e']
    for file in files: command += ['-f', str(file)]
    return subprocess.check_output(command, text=True)


def validate(documents, enabled=False):
    if not enabled:
        require(not documents, 'Disabled WebSocket access must render nothing');return
    expected = {('Secret', 'trip', 'websocket-trust'), ('ExternalSecret', 'trip', 'websocket-credentials')}
    policies = json.loads((CHART / 'files/policies.json').read_text())
    expected |= {('NetworkPolicy', p['namespace'], p['name']) for p in policies}
    require(len(documents) == 5 and {(d['kind'], d['metadata']['namespace'], d['metadata']['name']) for d in documents} == expected, 'Exact WebSocket access inventory required')
    require(all(d['metadata']['labels'] == {'yeodam.io/stage':'v3','app.kubernetes.io/part-of':'yeodam'} for d in documents), 'V3 labels required')
    for d in documents:
        if d['kind'] == 'Secret':
            require(d['type'] == 'Opaque' and not d.get('data') and set(d['stringData']) == {'redis.crt'}, 'Only public Redis CA may be rendered')
            supply.public_ca(d['stringData']['redis.crt'])
        elif d['kind'] == 'NetworkPolicy':
            p = next(p for p in policies if (p['namespace'], p['name']) == (d['metadata']['namespace'], d['metadata']['name']))
            require(d['spec'] == p['spec'], 'WebSocket policy peer/port widening rejected')
        else:
            data = [{'secretKey':k, 'remoteRef':{'key':'yeodam/v3/foundation/trip/backend','property':k,'version':'AWSCURRENT','decodingStrategy':'None'}} for k in ['REDIS_PASSWORD','REDIS_SENTINEL_PASSWORD','WEBSOCKET_INTERNAL_SERVICE_TOKEN']]
            require(d['spec'] == {'refreshPolicy':'Periodic','refreshInterval':'1h','secretStoreRef':{'kind':'SecretStore','name':'aws-secretsmanager'},'target':{'name':'websocket-credentials','creationPolicy':'Orphan','deletionPolicy':'Retain','template':{'type':'Opaque'}},'data':data}, 'WebSocket uses only three selected keys from the existing V3 trip source')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-f', action='append', default=[])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    source = render(args.f);documents = platform.load_documents(source)
    validate(documents, bool(documents));platform.schema_check(source)
    if args.output: args.output.write_text(source)
    print('WebSocket access validated; no deployment, source reads or credentials copied')


if __name__ == '__main__': main()
