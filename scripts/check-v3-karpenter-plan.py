#!/usr/bin/env python3
"""Review private terraform show -json output; print counts only, never payloads."""
import argparse
import json
from pathlib import Path

EXPECTED = {
    'aws_iam_role.controller', 'aws_iam_role.node', 'aws_eks_pod_identity_association.controller',
    'aws_iam_instance_profile.node', 'aws_eks_access_entry.node',
    'aws_iam_policy.lifecycle', 'aws_iam_policy.discovery',
    'aws_iam_role_policy_attachment.lifecycle', 'aws_iam_role_policy_attachment.discovery',
    'aws_sqs_queue.interruption', 'aws_sqs_queue_policy.interruption',
} | {f'aws_iam_role_policy_attachment.node["{name}"]' for name in (
    'AmazonEKSWorkerNodePolicy', 'AmazonEC2ContainerRegistryPullOnly', 'AmazonSSMManagedInstanceCore')}
EXPECTED |= {f'{kind}.interruption["{key}"]' for kind in (
    'aws_cloudwatch_event_rule', 'aws_cloudwatch_event_target') for key in ('health', 'spot', 'rebalance', 'state')}


def validate(plan):
    changes = plan.get('resource_changes', [])
    # This independent root has no data sources. A missing/truncated inventory is unsafe.
    if plan.get('errored') or plan.get('deferred_changes'):
        raise ValueError('Errored or deferred Plan is not reviewable')
    addresses = [c.get('address') for c in changes]
    if set(addresses) != EXPECTED or len(addresses) != len(EXPECTED):
        raise ValueError('Unexpected resource inventory: only the 22 Karpenter foundation resources are allowed')
    counts = {'create': 0, 'no-op': 0}
    for c in changes:
        if c.get('mode') != 'managed' or c.get('type') != c['address'].split('.')[0]:
            raise ValueError('Unexpected resource mode or type')
        actions = c['change']['actions']
        if actions not in (['create'], ['no-op']):
            raise ValueError('Updates, deletions and replacements require separate review')
        counts[actions[0]] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan_json', type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(json.loads(args.plan_json.read_text())), sort_keys=True))


if __name__ == '__main__': main()
