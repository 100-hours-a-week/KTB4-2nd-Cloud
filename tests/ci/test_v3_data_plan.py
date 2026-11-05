import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('data_plan', ROOT / 'scripts/check-v3-data-plan.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def plan(created=True):
    rows = {address: {'address': address, 'mode': 'managed', 'change': {
        'actions': ['no-op'], 'before': {}, 'after': {}}} for address in m.PROTECTED}
    for az in ['ap-northeast-2a', 'ap-northeast-2b', 'ap-northeast-2c']:
        rows[f'aws_subnet.private["{az}"]']['change']['after'] = {'id': 'subnet-' + az}
    rows['aws_eks_cluster.this']['change']['after'] = {'name': m.CLUSTER, 'vpc_config': [
        {'subnet_ids': ['subnet-ap-northeast-2a', 'subnet-ap-northeast-2c']}]}
    role = f'arn:aws:iam::{m.ACCOUNT}:role/foundation-node'
    rows['aws_iam_role.node']['change']['after'] = {'arn': role}
    for az in m.TYPES:
        template = dict(instance_type=m.TYPES[az], update_default_version=True,
            credit_specification=[{'cpu_credits': 'standard'}],
            metadata_options=[{'http_endpoint': 'enabled', 'http_tokens': 'required', 'http_put_response_hop_limit': 1}],
            block_device_mappings=[{'device_name': '/dev/xvda', 'ebs': [
                {'volume_type': 'gp3', 'volume_size': 20, 'encrypted': True, 'delete_on_termination': True}]}])
        node = dict(cluster_name=m.CLUSTER, node_group_name=f'data-{az}-minimal', node_role_arn=role,
            subnet_ids=['subnet-' + az], instance_types=None, ami_type='AL2023_x86_64_STANDARD',
            capacity_type='ON_DEMAND', version='1.36', release_version='1.36.5-20261031',
            labels={'yeodam.io/role': 'data', 'yeodam.io/stage': 'v3', 'yeodam.io/memory-tier': m.TIERS[az]},
            taint=[{'key': 'yeodam.io/role', 'value': 'data', 'effect': 'NO_SCHEDULE'}],
            scaling_config=[{'desired_size': 1, 'min_size': 1, 'max_size': 1}],
            launch_template=[{'id': None if created else 'lt-' + az, 'version': None if created else '1'}])
        if not created:
            template.update(id='lt-' + az, latest_version=1)
        for kind, after in [('aws_launch_template.data', template), ('aws_eks_node_group.data', node)]:
            address = f'{kind}["{az}"]'
            rows[address] = {'address': address, 'mode': 'managed', 'change': {
                'actions': ['create'] if created else ['no-op'], 'before': None if created else copy.deepcopy(after),
                'after': after, 'after_unknown': {'launch_template': [{'id': True, 'version': True}]} if created and kind.startswith('aws_eks') else {}}}
    variables = dict(expected_account_id=m.ACCOUNT, aws_region='ap-northeast-2', cluster_name=m.CLUSTER,
        kubernetes_version='1.36', node_release_version='1.36.5-20261031', enable_data_nodes=True,
        enable_kafka_nodes=True, availability_zones=['ap-northeast-2a','ap-northeast-2c','ap-northeast-2b'],
        nat_gateway_az_overrides={'ap-northeast-2b': 'ap-northeast-2a'})
    return {'complete': True, 'variables': {k: {'value': v} for k,v in variables.items()},
            'configuration': {'root_module': {'resources': [{'address': 'aws_eks_node_group.data',
                'expressions': {'launch_template': [{'id': {'references': ['aws_launch_template.data', 'each.key']},
                    'version': {'references': ['aws_launch_template.data', 'each.key']}}]}}]}},
            'resource_changes': list(rows.values())}


def row(p, prefix):
    return next(r for r in p['resource_changes'] if r['address'].startswith(prefix))


class DataPlanProtection(unittest.TestCase):
    def test_only_four_creates_and_idempotent_no_changes(self):
        self.assertEqual(m.validate(plan()), {'create': 4})
        self.assertEqual(m.validate(plan(False)), {})

    def test_accepts_provider_nullable_string_flags_and_computed_name(self):
        p=plan(False)
        for r in p['resource_changes']:
            if r['address'].startswith('aws_launch_template.data'):
                r['change']['after']['name']='computed-name'
                e=r['change']['after']['block_device_mappings'][0]['ebs'][0]
                e['encrypted']='true';e['delete_on_termination']='true'
            elif r['address'].startswith('aws_eks_node_group.data'):
                r['change']['after']['launch_template'][0]['name']='computed-name'
        self.assertEqual(m.validate(p),{})
        row(p,'aws_launch_template.data')['change']['after']['block_device_mappings'][0]['ebs'][0]['encrypted']='false'
        with self.assertRaises(ValueError):m.validate(p)

    def test_rejects_larger_nodes_unlimited_credit_and_unbounded_count(self):
        for kind, key, value in [('aws_launch_template.data', 'instance_type', 'm6i.large'),
            ('aws_launch_template.data', 'credit_specification', [{'cpu_credits': 'unlimited'}]),
            ('aws_eks_node_group.data', 'scaling_config', [{'desired_size': 1, 'min_size': 1, 'max_size': 3}]),
            ('aws_eks_node_group.data', 'instance_types', ['t3a.medium'])]:
            p=plan();row(p, kind)['change']['after'][key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):m.validate(p)

    def test_rejects_extra_disk_unencrypted_or_larger_root(self):
        for key,value in [('volume_size',100),('encrypted',False),('volume_type','io2'),('delete_on_termination',False),('iops',16000),('throughput',1000)]:
            p=plan();row(p,'aws_launch_template.data')['change']['after']['block_device_mappings'][0]['ebs'][0][key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):m.validate(p)
        p=plan();a=row(p,'aws_launch_template.data')['change']['after'];a['block_device_mappings'].append(copy.deepcopy(a['block_device_mappings'][0]))
        with self.assertRaises(ValueError):m.validate(p)

    def test_rejects_unreviewed_template_payloads_and_network(self):
        for key, value in [('image_id', 'ami-unreviewed'), ('user_data', 'bootstrap-payload'),
                           ('network_interfaces', [{'associate_public_ip_address': True}]),
                           ('iam_instance_profile', [{'arn': 'unreviewed'}])]:
            p=plan();row(p,'aws_launch_template.data')['change']['after'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):m.validate(p)

    def test_rejects_wrong_namespace_tier_role_ami_or_subnet(self):
        for key,value in [('labels',{'yeodam.io/role':'data','yeodam.io/stage':'v3','yeodam.io/memory-tier':'2gi'}),
                          ('taint',[]),('subnet_ids',['public-subnet']),('node_role_arn','unreviewed'),
                          ('ami_type','AL2023_ARM_64_STANDARD'),('release_version',None),('version','1.37')]:
            p=plan();row(p,'aws_eks_node_group.data')['change']['after'][key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):m.validate(p)

    def test_rejects_imds_exposure_and_unrelated_template(self):
        p=plan();row(p,'aws_launch_template.data')['change']['after']['metadata_options'][0]['http_put_response_hop_limit']=2
        with self.assertRaises(ValueError):m.validate(p)
        for expression in [{}, {'launch_template':[{'id':{'references':['aws_launch_template.system']}}]}]:
            p=plan();p['configuration']['root_module']['resources'][0]['expressions']=expression
            with self.assertRaises(ValueError):m.validate(p)
        p=plan();row(p,'aws_eks_node_group.data')['change']['after']['launch_template'][0]['id']='lt-unreviewed'
        with self.assertRaises(ValueError):m.validate(p)

    def test_rejects_any_existing_resource_change_or_additional_creation(self):
        for address, actions in [('aws_eks_cluster.this',['update']),
            ('aws_eks_node_group.kafka["ap-northeast-2a"]',['delete','create']),
            ('aws_eks_node_group.system["ap-northeast-2a-t3a-medium"]',['delete']),
            ('aws_nat_gateway.this["ap-northeast-2b"]',['create']),('aws_iam_role.extra',['create'])]:
            p=plan();r=next((r for r in p['resource_changes'] if r['address']==address),None)
            if r is None:r={'address':address,'mode':'managed','change':{'before':None,'after':{}}};p['resource_changes'].append(r)
            r['change']['actions']=actions
            with self.subTest(address=address),self.assertRaises(ValueError):m.validate(p)
        p=plan();r=row(p,'aws_eks_node_group.data');r['change']['actions']=['delete','create']
        with self.assertRaises(ValueError):m.validate(p)

    def test_rejects_wrong_target_partial_plan_and_missing_retained_resources(self):
        for key,value in [('expected_account_id','111111111111'),('aws_region','us-east-1'),
                          ('enable_kafka_nodes',False),('nat_gateway_az_overrides',{})]:
            p=plan();p['variables'][key]['value']=value
            with self.subTest(key=key),self.assertRaises(ValueError):m.validate(p)
        for key,value in [('complete',False),('errored',True),('deferred_changes',[{}])]:
            p=plan();p[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):m.validate(p)
        p=plan();p['resource_changes'].pop(0)
        with self.assertRaises(ValueError):m.validate(p)
        p=plan();p['resource_changes'].append(copy.deepcopy(p['resource_changes'][0]))
        with self.assertRaises(ValueError):m.validate(p)

    def test_rejects_eks_eni_expansion_or_wrong_template_version(self):
        p=plan();row(p,'aws_eks_cluster.this')['change']['after']['vpc_config'][0]['subnet_ids'].append('subnet-ap-northeast-2b')
        with self.assertRaises(ValueError):m.validate(p)
        p=plan(False);row(p,'aws_eks_node_group.data')['change']['after']['launch_template'][0]['version']='9'
        with self.assertRaises(ValueError):m.validate(p)


if __name__=='__main__':unittest.main()
