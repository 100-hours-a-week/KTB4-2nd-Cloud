#!/usr/bin/env python3
"""Render the retained storage class; never create PVCs or access AWS/a cluster."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('platform_contract', ROOT / 'scripts/check-v3-platform.py')
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)
CHART = ROOT / 'deploy/v3/storage'


def validate(documents):
    platform.require(len(documents) == 1, 'Only one StorageClass may be created; no PVC, PV or workload')
    d = documents[0]
    platform.require(d.get('apiVersion') == 'storage.k8s.io/v1' and d.get('kind') == 'StorageClass', 'Expected StorageClass')
    meta = d.get('metadata', {})
    platform.require(meta.get('name') == 'yeodam-v3-gp3-retain' and not meta.get('namespace'), 'Use the explicit cluster-scoped identity')
    platform.require(meta.get('annotations') == {'helm.sh/resource-policy': 'keep'}, 'Do not change the cluster default or uninstall the shared class')
    expected = dict(provisioner='ebs.csi.aws.com', reclaimPolicy='Retain', volumeBindingMode='WaitForFirstConsumer',
                    allowVolumeExpansion=True, parameters={'type':'gp3', 'encrypted':'true', 'csi.storage.k8s.io/fstype':'ext4'})
    platform.require(set(d) == {'apiVersion', 'kind', 'metadata', *expected}, 'Unexpected storage configuration')
    platform.require(all(d.get(k) == v for k, v in expected.items()), 'Keep encryption, CSI, retention and delayed AZ binding')


def render():
    lock = json.loads((ROOT / 'tests/ci/v3/tools.lock.json').read_text())
    return subprocess.check_output([os.environ['V3_HELM'], 'template', 'yeodam-v3-storage', str(CHART),
                                   '--kube-version', lock['kubernetesVersion']], text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    source = render()
    validate(platform.load_documents(source))
    platform.schema_check(source)
    if args.output: args.output.write_text(source)
    print(json.dumps({'storageClasses':1, 'volumesCreated':0, 'clusterAccessed':False}))


if __name__ == '__main__': main()
