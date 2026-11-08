#!/usr/bin/env python3
"""Validate Service alias manifests without live Service discovery or policy widening."""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import yaml
from v3_service_access import policies, require

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/v3/service-access'
spec = importlib.util.spec_from_file_location('service_platform', ROOT / 'scripts/check-v3-platform.py')
platform = importlib.util.module_from_spec(spec);spec.loader.exec_module(platform)
FIXTURE = {'enabled': True, 'serviceAddresses': {'aiApi': '172.20.200.10', 'tripApi': '172.20.200.11'}}


def render(values=None):
    args = [os.environ['V3_HELM'], 'template', 'yeodam-v3-service-access', str(CHART), '--kube-version', '1.36.0']
    return subprocess.check_output(args + (['-f', '-'] if values else []),
                                   input=yaml.safe_dump(values) if values else None, text=True, stderr=subprocess.PIPE)


def validate(docs, values):
    if not values['enabled']:
        require(not docs, 'Disabled Service access must render nothing')
        return
    expected = policies(values['serviceAddresses'])
    identities = [(d['metadata']['namespace'], d['metadata']['name']) for d in docs]
    require(len(docs) == 3 and len(set(identities)) == 3 and set(identities) == set(expected), 'Exact three Service aliases')
    for d in docs:
        require(d['kind'] == 'NetworkPolicy' and d['apiVersion'] == 'networking.k8s.io/v1', 'Policies only; no workload/Service/Secret')
        require(d['spec'] == expected[(d['metadata']['namespace'], d['metadata']['name'])], 'Only exact app roles, /32 Service destinations and TCP ports')
        require(d['metadata'].get('labels') == {'yeodam.io/stage': 'v3', 'app.kubernetes.io/part-of': 'yeodam'}, 'V3 ownership labels')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path)
    p.add_argument('-f', '--values', type=Path, help='Prepared public Service access values')
    a = p.parse_args()
    cases = [('prepared', yaml.safe_load(a.values.read_text()))] if a.values else [
        ('disabled', yaml.safe_load((CHART / 'values.yaml').read_text())), ('enabled', FIXTURE)]
    for name, v in cases:
        source = render(v);validate(platform.load_documents(source), v);platform.schema_check(source)
        if a.output:
            a.output.mkdir(parents=True, exist_ok=True)
            (a.output / ('service-access-' + name + '.yaml')).write_text(source)
    print('Service access contracts passed; no added Pods, AWS or live cluster access')


if __name__ == '__main__':
    main()
