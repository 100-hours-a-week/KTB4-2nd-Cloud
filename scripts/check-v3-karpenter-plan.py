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


def validate(plan, app_node_credits=False):
    changes = plan.get('resource_changes', [])
    # This independent root has no data sources. A missing/truncated inventory is unsafe.
    if plan.get('errored') or plan.get('deferred_changes'):
        raise ValueError('Errored or deferred Plan is not reviewable')
    addresses = [c.get('address') for c in changes]
    credit = 'aws_iam_role_policy.node_credits[0]'
    expected = EXPECTED | ({credit} if app_node_credits else set())
    if set(addresses) != expected or len(addresses) != len(expected):
        raise ValueError('Unexpected resource inventory outside reviewed Karpenter foundation/optional credit policy')
    counts = {'create': 0, 'no-op': 0}
    for c in changes:
        if c.get('mode') != 'managed' or c.get('type') != c['address'].split('.')[0]:
            raise ValueError('Unexpected resource mode or type')
        actions = c['change']['actions']
        if actions not in (['create'], ['no-op']):
            raise ValueError('Updates, deletions and replacements require separate review')
        if app_node_credits and c['address'] != credit and actions != ['no-op']:
            raise ValueError('App-credit opt-in must preserve all 22 existing objects')
        if c['address'] == credit:
            expected_policy = json.loads((Path(__file__).resolve().parents[1] / 'deploy/v3/nodepools/files/node-credit-policy.json').read_text())
            after = c['change'].get('after', {})
            if after.get('role') != 'yeodam-v3-foundation-karpenter-node' or after.get('name') != 'v3-app-node-standard-credits' or json.loads(after.get('policy', '{}')) != expected_policy:
                raise ValueError('Unexpected credit-policy role, actions or resource boundary')
        counts[actions[0]] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan_json', type=Path)
    parser.add_argument('--app-node-credits', action='store_true')
    args = parser.parse_args()
    print(json.dumps(validate(json.loads(args.plan_json.read_text()), args.app_node_credits), sort_keys=True))


if __name__ == '__main__': main()
