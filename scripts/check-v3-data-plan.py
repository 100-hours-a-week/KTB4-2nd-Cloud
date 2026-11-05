#!/usr/bin/env python3
"""Reject any live foundation Plan delta beyond the four reviewed data resources."""
import argparse
from collections import Counter
import json
from pathlib import Path

ACCOUNT = '483175530259'
CLUSTER = 'yeodam-v3-foundation'
TYPES = {'ap-northeast-2a': 't3a.medium', 'ap-northeast-2c': 't3a.small'}
TIERS = {'ap-northeast-2a': '4gi', 'ap-northeast-2c': '2gi'}
NODES = {f'aws_eks_node_group.data["{az}"]' for az in TYPES}
TEMPLATES = {f'aws_launch_template.data["{az}"]' for az in TYPES}
CREATES = NODES | TEMPLATES
PROTECTED = {'aws_eks_cluster.this', 'aws_vpc.this', 'aws_iam_role.node',
             'aws_launch_template.system', 'aws_vpc_endpoint.s3'}
PROTECTED |= {f'aws_subnet.private["{az}"]' for az in ['ap-northeast-2a', 'ap-northeast-2c', 'ap-northeast-2b']}
PROTECTED |= {f'aws_nat_gateway.this["{az}"]' for az in TYPES}
PROTECTED |= {f'aws_eks_node_group.system["{az}-t3a-medium"]' for az in TYPES}
PROTECTED |= {f'{kind}["{az}"]' for kind in ['aws_eks_node_group.kafka', 'aws_launch_template.kafka']
              for az in ['ap-northeast-2a', 'ap-northeast-2b', 'ap-northeast-2c']}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def tf_true(value):
    # Launch Template EBS booleans are nullable strings in the AWS provider schema.
    return value is True or value == 'true'


def refs(value):
    if isinstance(value, dict):
        result = set(value.get('references', []))
        for child in value.values():
            result |= refs(child)
        return result
    if isinstance(value, list):
        return set().union(*(refs(child) for child in value)) if value else set()
    return set()


def validate(plan):
    require(plan.get('complete') is True and not plan.get('errored') and not plan.get('deferred_changes'),
            'Only a complete successful Plan may be reviewed')
    variables = {k: v['value'] for k, v in plan.get('variables', {}).items()}
    require(variables.get('expected_account_id') == ACCOUNT and variables.get('aws_region') == 'ap-northeast-2'
            and variables.get('cluster_name') == CLUSTER and variables.get('kubernetes_version') == '1.36',
            'Wrong account, region, cluster or Kubernetes version')
    require(variables.get('enable_data_nodes') is True and variables.get('enable_kafka_nodes') is True
            and variables.get('availability_zones') == ['ap-northeast-2a', 'ap-northeast-2c', 'ap-northeast-2b']
            and variables.get('nat_gateway_az_overrides') == {'ap-northeast-2b': 'ap-northeast-2a'},
            'Retain the applied three-AZ network, Kafka and shared b-AZ NAT inputs')
    rows = [r for r in plan.get('resource_changes', []) if r.get('mode') == 'managed']
    by_address = {r['address']: r for r in rows}
    require(len(rows) == len(by_address), 'Duplicate resource identity')
    require((CREATES | PROTECTED).issubset(by_address), 'Missing existing or planned identities')
    actions = Counter()
    for row in rows:
        c = row['change']
        require(not c.get('importing'), 'No resource import in a capacity-only Plan')
        if c['actions'] == ['no-op']:
            continue
        require(row['address'] in CREATES and c['actions'] == ['create'] and c.get('before') is None,
                'No existing resource update, deletion, replacement or unrelated creation is allowed')
        actions['create'] += 1
    # The exact source graph prevents silently binding the new groups to a different LT.
    blocks = plan.get('configuration', {}).get('root_module', {}).get('resources', [])
    node_config = next((r for r in blocks if r.get('address') == 'aws_eks_node_group.data'), None)
    require(node_config is not None, 'Missing source configuration for template binding')
    references = refs(node_config.get('expressions', {}))
    require(any(r.startswith('aws_launch_template.data') for r in references)
            and not any(r.startswith('aws_launch_template.') and not r.startswith('aws_launch_template.data')
                        for r in references), 'Only the reviewed typed data template may be used')
    cluster = by_address['aws_eks_cluster.this']['change']['after']
    require(cluster.get('name') == CLUSTER, 'Wrong retained EKS cluster')
    original_subnets = {by_address[f'aws_subnet.private["{az}"]']['change']['after']['id'] for az in TYPES}
    require(set(cluster['vpc_config'][0]['subnet_ids']) == original_subnets, 'Preserve original EKS ENI AZs')
    role = by_address['aws_iam_role.node']['change']['after']['arn']
    require(role.startswith(f'arn:aws:iam::{ACCOUNT}:role/'), 'Unexpected Node IAM role')
    for az, instance_type in TYPES.items():
        template = by_address[f'aws_launch_template.data["{az}"]']['change']['after']
        require(template.get('instance_type') == instance_type and template.get('update_default_version') is True,
                'Unexpected type or template version policy')
        require(template.get('credit_specification') == [{'cpu_credits': 'standard'}], 'No surplus CPU charges')
        disk = template.get('block_device_mappings', [])
        require(len(disk) == 1 and disk[0].get('device_name') == '/dev/xvda' and len(disk[0].get('ebs', [])) == 1,
                'Exactly one reviewed root volume per Node')
        ebs = disk[0]['ebs'][0]
        require(ebs.get('volume_size') == 20 and ebs.get('volume_type') == 'gp3'
                and tf_true(ebs.get('encrypted')) and tf_true(ebs.get('delete_on_termination'))
                and not ebs.get('snapshot_id') and ebs.get('iops') in (None, 0, 3000)
                and ebs.get('throughput') in (None, 0, 125), 'Unexpected root volume budget or data source')
        require(not any(template.get(key) for key in ('user_data', 'image_id', 'key_name',
                'network_interfaces', 'vpc_security_group_ids', 'iam_instance_profile', 'instance_market_options')),
                'No extra bootstrap payload, image, SSH, network, identity or market override')
        metadata = template.get('metadata_options', [])
        require(len(metadata) == 1 and metadata[0].get('http_tokens') == 'required'
                and metadata[0].get('http_endpoint') == 'enabled'
                and metadata[0].get('http_put_response_hop_limit') == 1, 'Require IMDSv2 and deny Pod IMDS access')
        node_row = by_address[f'aws_eks_node_group.data["{az}"]']
        node = node_row['change']['after']
        require(node.get('cluster_name') == CLUSTER and node.get('node_role_arn') == role
                and node.get('node_group_name') == f'data-{az}-minimal'
                and node.get('subnet_ids') == [by_address[f'aws_subnet.private["{az}"]']['change']['after']['id']],
                'Wrong Node identity, role or AZ')
        require(node.get('instance_types') in (None, []) and node.get('capacity_type') == 'ON_DEMAND'
                and node.get('ami_type') == 'AL2023_x86_64_STANDARD'
                and node.get('version') == variables['kubernetes_version']
                and node.get('release_version') == variables.get('node_release_version')
                and bool(node.get('release_version')), 'Require pinned x86 AMI and typed launch template')
        require(node.get('scaling_config') == [{'desired_size': 1, 'min_size': 1, 'max_size': 1}],
                'Exactly one steady-state Node per AZ')
        require(node.get('labels') == {'yeodam.io/role': 'data', 'yeodam.io/stage': 'v3',
                                      'yeodam.io/memory-tier': TIERS[az]}
                and node.get('taint') == [{'key': 'yeodam.io/role', 'value': 'data', 'effect': 'NO_SCHEDULE'}],
                'Missing isolation or Mongo placement tier')
        binding = node.get('launch_template', [])
        require(len(binding) == 1, 'Use one dedicated template binding')
        if binding[0].get('name'):
            require(binding[0]['name'] == template.get('name'), 'Wrong computed template name')
        unknown = node_row['change'].get('after_unknown', {}).get('launch_template', [{}])[0]
        if template.get('id'):
            require(binding[0].get('id') == template['id'], 'Wrong existing template ID')
        else:
            require(binding[0].get('id') is None and unknown.get('id') is True, 'Unexpected unreviewed template ID')
        if template.get('latest_version'):
            require(binding[0].get('version') == str(template['latest_version']), 'Wrong template version')
        else:
            require(binding[0].get('version') is None and unknown.get('version') is True, 'Unreviewed template version')
    return dict(actions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path, help='Private Terraform show -json file; never upload raw State/Plan')
    args = parser.parse_args()
    print(json.dumps({'validatedActions': validate(json.loads(args.plan.read_text()))}))


if __name__ == '__main__':
    main()
