#!/usr/bin/env python3
"""Offline app pool, AMI and bootstrap contracts; no kubeconfig or AWS mutations."""
import argparse
from collections import Counter
import email
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'deploy/v3/nodepools'
FOUNDATION = ROOT / 'deploy/v3/environments/foundation/nodepools.values.yaml'
FIXTURE = ROOT / 'tests/ci/v3/nodepools-enabled.fixture.yaml'
spec = importlib.util.spec_from_file_location('platform', ROOT / 'scripts/check-v3-platform.py')
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)
require = platform.require


def render(files=()):
    cmd = [os.environ['V3_HELM'], 'template', 'yeodam-v3-nodepools', str(CHART), '--kube-version', '1.36.0']
    for file in files: cmd += ['-f', str(file)]
    return subprocess.check_output(cmd, text=True)


def bootstrap_script(userdata):
    msg = email.message_from_string(userdata)
    require(msg.is_multipart() and len(msg.get_payload()) == 1, 'One reviewed shell MIME part only')
    part = msg.get_payload()[0]
    require(part.get_content_type() == 'text/x-shellscript', 'Do not override Karpenter NodeConfig or nodeadm')
    source = part.get_payload(decode=True).decode()
    for filename, marker, target in [('kubelet-credit-gate.conf', 'CREDIT_GATE', '/etc/systemd/system/kubelet.service.d/20-yeodam-credit.conf'),
                                     ('cpu-credit-standard.service', 'CREDIT_UNIT', '/etc/systemd/system/yeodam-cpu-credit-standard.service'),
                                     ('cpu-credit-standard.sh', 'CREDIT_SCRIPT', '/usr/local/sbin/yeodam-cpu-credit-standard')]:
        content = (CHART / 'files' / filename).read_text()
        require(f"cat > {target} <<'{marker}'\n{content}\n{marker}" in source, 'Bootstrap file injection or gate content changed')
    require(source.startswith('#!/bin/bash\nset -Eeuo pipefail\numask 077\n'), 'Strict root bootstrap required')
    require('systemctl daemon-reload' in source and 'chmod 0700 /usr/local/sbin/yeodam-cpu-credit-standard' in source, 'Install the executable and load its systemd dependency')
    require('nodeadm init' not in source and 'modify-default-credit' not in source, 'No duplicate bootstrap or account-wide credit mutation')
    return source


def validate(docs, enabled):
    if not enabled:
        require(docs == [], 'Disabled app pools must not create any objects')
        return
    require(Counter(d['kind'] for d in docs) == {'NodePool': 2, 'EC2NodeClass': 1}, 'Two CPU pools and one class only; no workloads/compute')
    ids = [(d['kind'], d['metadata']['name']) for d in docs]
    require(len(set(ids)) == 3 and all(not d['metadata'].get('namespace') for d in docs), 'Unique cluster-scoped identities required')
    reviewed = yaml.safe_load(FOUNDATION.read_text())['nodeClass']
    c = next(d for d in docs if d['kind'] == 'EC2NodeClass')
    require(c['metadata']['name'] == 'yeodam-v3-app', 'Dedicated app class required')
    expected = {
        'amiFamily': 'AL2023', 'amiSelectorTerms': [{'id': reviewed['amiId']}],
        'subnetSelectorTerms': [{'id': reviewed['subnetIds'][az]} for az in ['ap-northeast-2a', 'ap-northeast-2c']],
        'securityGroupSelectorTerms': [{'id': reviewed['securityGroupId']}],
        'instanceProfile': 'yeodam-v3-foundation-karpenter-node', 'associatePublicIPAddress': False,
        'metadataOptions': {'httpEndpoint': 'enabled', 'httpProtocolIPv6': 'disabled', 'httpPutResponseHopLimit': 1, 'httpTokens': 'required'},
        'blockDeviceMappings': [{'deviceName': '/dev/xvda', 'rootVolume': True, 'ebs': {'volumeSize': '20Gi', 'volumeType': 'gp3', 'encrypted': True, 'deleteOnTermination': True}}],
        'detailedMonitoring': False, 'kubelet': {'maxPods': 17},
        'tags': {'Project': 'yeodam', 'Version': 'v3', 'Environment': 'foundation', 'Role': 'app', 'Issue': '531'},
    }
    require(set(c['spec']) == set(expected) | {'userData'} and all(c['spec'][k] == v for k, v in expected.items()), 'Unreviewed class, AMI, network, IAM, IMDS or disk change')
    bootstrap_script(c['spec']['userData'])
    pools = {d['metadata']['name']: d for d in docs if d['kind'] == 'NodePool'}
    require(set(pools) == {'api', 'worker'}, 'Never adopt fixed system/Kafka/data or GPU nodes')
    for role, d in pools.items():
        expected = {
            'template': {'metadata': {'labels': {'yeodam.io/role': role, 'yeodam.io/stage': 'v3'}}, 'spec': {
                'nodeClassRef': {'group': 'karpenter.k8s.aws', 'kind': 'EC2NodeClass', 'name': 'yeodam-v3-app'},
                'taints': [{'key': 'yeodam.io/role', 'value': role, 'effect': 'NoSchedule'}],
                'requirements': [
                    {'key': 'kubernetes.io/arch', 'operator': 'In', 'values': ['amd64']},
                    {'key': 'kubernetes.io/os', 'operator': 'In', 'values': ['linux']},
                    {'key': 'node.kubernetes.io/instance-type', 'operator': 'In', 'values': ['t3a.medium']},
                    {'key': 'karpenter.sh/capacity-type', 'operator': 'In', 'values': ['on-demand']},
                    {'key': 'topology.kubernetes.io/zone', 'operator': 'In', 'values': ['ap-northeast-2a', 'ap-northeast-2c']}],
                'expireAfter': 'Never', 'terminationGracePeriod': '5m'}},
            'limits': {'cpu': '4', 'memory': '8Gi'},
            'disruption': {'consolidationPolicy': 'WhenEmpty', 'consolidateAfter': '60s', 'budgets': [
                {'nodes': '1', 'reasons': ['Empty']}, {'nodes': '0', 'reasons': ['Underutilized', 'Drifted']}]}}
        require(d['spec'] == expected, 'App pool capacity/role/taint/class or disruption boundary changed')


def schemas(directory):
    lock = json.loads((ROOT / 'deploy/v3/karpenter/upstream.lock.json').read_text())
    archive = directory / 'karpenter.tgz'
    require(hashlib.sha256(archive.read_bytes()).hexdigest() == lock['sha256'], 'Unreviewed Karpenter CRDs')
    with tarfile.open(archive) as tar:
        for kind, name in [('nodepool', 'karpenter.sh_nodepools'), ('ec2nodeclass', 'karpenter.k8s.aws_ec2nodeclasses')]:
            with tar.extractfile('karpenter/crds/' + name + '.yaml') as file:
                crd = yaml.safe_load(file)
            schema = next(v['schema']['openAPIV3Schema'] for v in crd['spec']['versions'] if v['name'] == 'v1')
            (Path(os.environ['V3_SCHEMA_DIR']) / (kind + '.json')).write_text(json.dumps(schema))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for files in [[], [FOUNDATION]]: validate(platform.load_documents(render(files)), False)
    source = render([FOUNDATION, FIXTURE]); docs = platform.load_documents(source)
    validate(docs, True); schemas(args.directory); platform.schema_check(source)
    args.output.mkdir(exist_ok=True, parents=True)
    (args.output / 'nodepools-ci-fixture.yaml').write_text(source)
    print('App CPU pool/placement schema contracts passed; default0, opt-in3 resources. No AWS changes.')


if __name__ == '__main__': main()
