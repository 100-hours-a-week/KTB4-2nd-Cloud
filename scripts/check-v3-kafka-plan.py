#!/usr/bin/env python3
"""Guard the private minimum-cost Kafka foundation plan; never print values."""
import argparse
from collections import Counter
import json
from pathlib import Path

TYPES = {'ap-northeast-2a':'t3a.small', 'ap-northeast-2b':'t3.small', 'ap-northeast-2c':'t3a.small'}
ZONES = set(TYPES)
NODES = {f'aws_eks_node_group.kafka["{az}"]' for az in ZONES}
TEMPLATES = {f'aws_launch_template.kafka["{az}"]' for az in ZONES}
CREATES = NODES | TEMPLATES | {
    f'{kind}["ap-northeast-2b"]' for kind in ['aws_subnet.private', 'aws_subnet.public',
        'aws_route_table.private', 'aws_route.private', 'aws_route_table_association.private', 'aws_route_table_association.public']}


def require(condition, message):
    if not condition: raise ValueError(message)


def validate_node(after):
    # Type belongs in the Launch Template: provider drops CPU-credit settings
    # otherwise. EKS forbids setting a type in both the template and node group.
    require(after.get('instance_types') in [None, []] and after['capacity_type']=='ON_DEMAND', 'Use the typed template, not conflicting node-group types')
    require(after['scaling_config']==[{'desired_size':1,'min_size':1,'max_size':1}], 'Unbounded node count')
    require(after['labels'].get('yeodam.io/role')=='kafka' and after['taint']==[{'key':'yeodam.io/role','value':'kafka','effect':'NO_SCHEDULE'}], 'Missing Kafka isolation')


def validate_template(after, az):
    require(after.get('instance_type')==TYPES[az], 'Only reviewed small AZ-specific instance types')
    require(after['credit_specification']==[{'cpu_credits':'standard'}], 'Surplus CPU charges not allowed')
    disk=after['block_device_mappings'][0]['ebs'][0]
    require(disk['volume_size']==20 and disk['volume_type']=='gp3' and disk['encrypted'], 'Unexpected root disk budget')


def validate(plan, initial_template_repair=False):
    require(not plan.get('errored') and plan.get('complete') is not False, 'Incomplete plan')
    variables={k:v['value'] for k,v in plan.get('variables',{}).items()}
    require(variables.get('expected_account_id')=='483175530259' and variables.get('cluster_name')=='yeodam-v3-foundation', 'Wrong target')
    require(variables.get('enable_kafka_nodes') is True and variables.get('nat_gateway_az_overrides')=={'ap-northeast-2b':'ap-northeast-2a'}, 'Use explicit reviewed minimal-cost inputs')
    observed, actions=set(), Counter()
    for row in plan.get('resource_changes',[]):
        if row.get('mode')!='managed':continue
        c,address=row['change'],row['address'];observed.add(address)
        if c['actions']==['no-op']:continue
        before,after=c.get('before') or {},c.get('after') or {}
        az=address.split('["',1)[1].split('"',1)[0] if address in NODES|TEMPLATES else None
        if address in NODES:validate_node(after)
        if address in TEMPLATES:validate_template(after,az)
        if c['actions']==['create']:
            require(address in CREATES,'Unexpected creation: '+address)
        elif c['actions']==['update'] and address=='aws_vpc_endpoint.s3':
            require({k for k in set(before)|set(after) if before.get(k)!=after.get(k)}=={'route_table_ids'},'Unrelated S3 endpoint mutation')
        elif initial_template_repair and c['actions']==['update'] and address in TEMPLATES:
            require(az=='ap-northeast-2a' and before.get('instance_type') in [None,''],'Only the original untyped template may be repaired')
            diff={k for k in set(before)|set(after) if before.get(k)!=after.get(k)}
            require(diff.issubset({'instance_type','latest_version','default_version'}),'Unrelated launch-template change during repair')
        elif initial_template_repair and c['actions']==['delete','create'] and address in NODES:
            require(before.get('node_group_name')=='kafka-'+az+'-'+TYPES[az].replace('.','-') and after.get('node_group_name')=='kafka-'+az+'-small','Only the one-time empty Kafka group migration is allowed')
            require(before.get('node_role_arn')==after.get('node_role_arn') and before.get('cluster_name')==after.get('cluster_name')=='yeodam-v3-foundation','Do not migrate roles or clusters')
        else:raise ValueError('Unexpected deletion, replacement or update: '+address)
        actions[','.join(c['actions'])]+=1
    require((CREATES|{'aws_eks_cluster.this','aws_vpc_endpoint.s3'}).issubset(observed),'Missing retained/planned identities')
    return dict(actions)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan',type=Path)
    parser.add_argument('--initial-template-repair',action='store_true',help='One-time recreation of empty initial Kafka groups only; separately prove no workload/PVC exists')
    args=parser.parse_args()
    print(json.dumps({'validatedActions':validate(json.loads(args.plan.read_text()),args.initial_template_repair)}))


if __name__=='__main__':main()
