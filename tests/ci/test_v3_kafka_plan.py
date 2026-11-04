import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('kafka_plan',ROOT/'scripts/check-v3-kafka-plan.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def plan():
    rows=[]
    for address in m.CREATES:
        kind='aws_eks_node_group' if address.startswith('aws_eks_node_group.') else 'aws_launch_template' if address.startswith('aws_launch_template.') else 'network'
        after={}
        if kind=='aws_eks_node_group':
            after=dict(instance_types=['t3a.small'],capacity_type='ON_DEMAND',
                scaling_config=[dict(min_size=1,max_size=1,desired_size=1)],
                labels={'yeodam.io/role':'kafka'},taint=[dict(key='yeodam.io/role',value='kafka',effect='NO_SCHEDULE')])
        if kind=='aws_launch_template':
            after=dict(credit_specification=[{'cpu_credits':'standard'}],
                block_device_mappings=[{'ebs':[dict(volume_size=20,volume_type='gp3',encrypted=True)]}])
        rows.append(dict(address=address,type=kind,mode='managed',change=dict(actions=['create'],before=None,after=after)))
    for address,before,after in [
        ('aws_eks_cluster.this',{'name':'yeodam-v3-foundation','vpc_config':[{'subnet_ids':['a','c'],'endpoint_public_access':True}]},
         {'name':'yeodam-v3-foundation','vpc_config':[{'subnet_ids':None,'endpoint_public_access':True}]}),
        ('aws_vpc_endpoint.s3',{'route_table_ids':['a','c']},{'route_table_ids':None})]:
        rows.append(dict(address=address,type='existing',mode='managed',change=dict(actions=['update'],before=before,after=after)))
    return {'complete':True,'variables':{k:{'value':v} for k,v in dict(expected_account_id='483175530259',
        cluster_name='yeodam-v3-foundation',enable_kafka_nodes=True,nat_gateway_az_overrides={'ap-northeast-2b':'ap-northeast-2a'}).items()},'resource_changes':rows}


class KafkaPlanProtection(unittest.TestCase):
    def test_accepts_only_reviewed_delta(self):
        self.assertEqual(m.validate(plan()),{'create':10,'update':2})

    def test_rejects_larger_or_unbounded_nodes(self):
        for key,value in [('instance_types',['t3a.medium']),('scaling_config',[dict(min_size=1,max_size=3,desired_size=1)])]:
            p=plan();row=next(x for x in p['resource_changes'] if x['type']=='aws_eks_node_group');row['change']['after'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):m.validate(p)

    def test_rejects_surplus_cpu_or_large_root_disk(self):
        for mutate in [lambda a:a.update(credit_specification=[{'cpu_credits':'unlimited'}]),
                       lambda a:a['block_device_mappings'][0]['ebs'][0].update(volume_size=100)]:
            p=plan();mutate(next(x for x in p['resource_changes'] if x['type']=='aws_launch_template')['change']['after'])
            with self.assertRaises(ValueError):m.validate(p)

    def test_rejects_node_replacement_and_unplanned_nat(self):
        for address,actions in [('aws_eks_node_group.system["ap-northeast-2a-t3a-medium"]',['delete','create']),
                                ('aws_nat_gateway.this["ap-northeast-2b"]',['create'])]:
            p=plan();p['resource_changes'].append(dict(address=address,type='extra',mode='managed',change=dict(actions=actions,after={})))
            with self.subTest(address=address),self.assertRaises(ValueError):m.validate(p)

    def test_rejects_cluster_access_changes(self):
        p=plan();row=next(x for x in p['resource_changes'] if x['address']=='aws_eks_cluster.this')
        row['change']['after']['vpc_config'][0]['endpoint_public_access']=False
        with self.assertRaises(ValueError):m.validate(p)

    def test_rejects_wrong_account_or_partial_plan(self):
        p=plan();p['variables']['expected_account_id']['value']='111111111111'
        with self.assertRaises(ValueError):m.validate(p)
        p=plan();p['complete']=False
        with self.assertRaises(ValueError):m.validate(p)
        p=plan();p['resource_changes'].pop()
        with self.assertRaises(ValueError):m.validate(p)


if __name__=='__main__':unittest.main()
