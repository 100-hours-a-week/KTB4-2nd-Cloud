#!/usr/bin/env python3
"""Guard the private minimum-cost Kafka foundation plan; never print values."""
import argparse
from collections import Counter
import json
from pathlib import Path

TYPES = {'ap-northeast-2a':'t3a.small', 'ap-northeast-2b':'t3.small', 'ap-northeast-2c':'t3a.small'}
ZONES = set(TYPES)
CREATES = {'aws_launch_template.kafka[0]'} | {f'aws_eks_node_group.kafka["{az}"]' for az in ZONES} | {
    f'{kind}["ap-northeast-2b"]' for kind in ['aws_subnet.private', 'aws_subnet.public',
        'aws_route_table.private', 'aws_route.private', 'aws_route_table_association.private', 'aws_route_table_association.public']}


def require(condition, message):
    if not condition: raise ValueError(message)


def validate(plan):
    require(not plan.get('errored') and plan.get('complete') is not False, 'Incomplete plan')
    variables = {k:v['value'] for k,v in plan.get('variables',{}).items()}
    require(variables.get('expected_account_id') == '483175530259' and variables.get('cluster_name') == 'yeodam-v3-foundation', 'Wrong target')
    require(variables.get('enable_kafka_nodes') is True and variables.get('nat_gateway_az_overrides') == {'ap-northeast-2b':'ap-northeast-2a'}, 'Use the explicit reviewed minimal-cost inputs')
    seen, observed, actions = set(), set(), Counter()
    for row in plan.get('resource_changes',[]):
        if row.get('mode') != 'managed': continue
        c, address = row['change'], row['address']
        observed.add(address)
        if c['actions'] == ['no-op']: continue
        seen.add(address); actions[','.join(c['actions'])] += 1
        before, after = c.get('before') or {}, c.get('after') or {}
        if c['actions'] == ['create']:
            require(address in CREATES, 'Unexpected creation: ' + address)
            if row['type'] == 'aws_eks_node_group':
                az = address.split('["', 1)[1].split('"', 1)[0]
                require(after['instance_types'] == [TYPES[az]] and after['capacity_type'] == 'ON_DEMAND', 'Only reviewed AZ-specific small on-demand Kafka nodes')
                require(after['scaling_config'] == [{'desired_size':1, 'min_size':1, 'max_size':1}], 'Unbounded node count')
                require(after['labels'].get('yeodam.io/role') == 'kafka' and after['taint'] == [{'key':'yeodam.io/role','value':'kafka','effect':'NO_SCHEDULE'}], 'Missing Kafka isolation')
            if row['type'] == 'aws_launch_template':
                require(after['credit_specification'] == [{'cpu_credits':'standard'}], 'Surplus CPU charges not allowed')
                disk=after['block_device_mappings'][0]['ebs'][0]
                require(disk['volume_size']==20 and disk['volume_type']=='gp3' and disk['encrypted'], 'Unexpected root disk budget')
        elif c['actions'] == ['update'] and address == 'aws_vpc_endpoint.s3':
            require({k for k in set(before)|set(after) if before.get(k)!=after.get(k)} == {'route_table_ids'}, 'Unrelated S3 endpoint mutation')
        else: raise ValueError('Unexpected deletion, replacement or update: ' + address)
    require(seen.issubset(CREATES | {'aws_vpc_endpoint.s3'}), 'Unexpected planned resource set')
    require((CREATES | {'aws_eks_cluster.this','aws_vpc_endpoint.s3'}).issubset(observed), 'Missing retained/planned identities; refresh the review')
    return dict(actions)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan',type=Path)
    args=parser.parse_args()
    print(json.dumps({'validatedActions':validate(json.loads(args.plan.read_text()))}))


if __name__ == '__main__': main()
