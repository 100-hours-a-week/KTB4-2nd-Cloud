#!/usr/bin/env python3
"""Helm post-renderer: omit upstream aggregation into existing admin roles.

Run Helm with the validation venv on PATH so this exact PyYAML is available.
No Controller RBAC or CRDs are removed. Helm stores the filtered manifest.
"""
import sys
import yaml


def filter_manifest(source):
    docs = [d for d in yaml.safe_load_all(source) if d]
    excluded = [d for d in docs if d['kind'] == 'ClusterRole' and d['metadata']['name'] == 'karpenter-admin']
    if len(excluded) != 1 or excluded[0]['metadata']['labels'].get('rbac.authorization.k8s.io/aggregate-to-admin') != 'true':
        raise ValueError('Pinned upstream aggregate role changed; review before installation')
    return yaml.safe_dump_all(d for d in docs if d is not excluded[0])


if __name__ == '__main__':
    sys.stdout.write(filter_manifest(sys.stdin.read()))
