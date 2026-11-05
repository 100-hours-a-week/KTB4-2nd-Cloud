import copy
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('kafka_plan',ROOT/'scripts/check-v3-kafka-plan.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def plan():
    rows=[]
    for address in m.CREATES:
        kind='aws_eks_node_group' if address in m.NODES else 'aws_launch_template' if address in m.TEMPLATES else 'network'
        after={}
        if kind in ['aws_eks_node_group','aws_launch_template']:az=address.split('["',1)[1].split('"',1)[0]
        if kind=='aws_eks_node_group':
            after=dict(instance_types=None,capacity_type='ON_DEMAND',node_group_name='kafka-'+az+'-small',
                cluster_name='yeodam-v3-foundation',node_role_arn='reviewed-node-role',scaling_config=[dict(min_size=1,max_size=1,desired_size=1)],
                labels={'yeodam.io/role':'kafka'},taint=[dict(key='yeodam.io/role',value='kafka',effect='NO_SCHEDULE')])
        if kind=='aws_launch_template':
            after=dict(instance_type=m.TYPES[az],credit_specification=[{'cpu_credits':'standard'}],
                block_device_mappings=[{'ebs':[dict(volume_size=20,volume_type='gp3',encrypted=True)]}])
        rows.append(dict(address=address,type=kind,mode='managed',change=dict(actions=['create'],before=None,after=after)))
    for address,before,after in [
        ('aws_eks_cluster.this',{'name':'yeodam-v3-foundation','vpc_config':[{'subnet_ids':['a','c'],'endpoint_public_access':True}]},
         {'name':'yeodam-v3-foundation','vpc_config':[{'subnet_ids':['a','c'],'endpoint_public_access':True}]}),
        ('aws_vpc_endpoint.s3',{'route_table_ids':['a','c']},{'route_table_ids':None})]:
        rows.append(dict(address=address,type='existing',mode='managed',change=dict(actions=['no-op'] if address=='aws_eks_cluster.this' else ['update'],before=before,after=after)))
    return {'complete':True,'variables':{k:{'value':v} for k,v in dict(expected_account_id='483175530259',cluster_name='yeodam-v3-foundation',enable_kafka_nodes=True,nat_gateway_az_overrides={'ap-northeast-2b':'ap-northeast-2a'}).items()},'resource_changes':rows}


class KafkaPlanProtection(unittest.TestCase):
    def test_accepts_only_reviewed_delta(self):self.assertEqual(m.validate(plan()),{'create':12,'update':1})
    def test_rejects_larger_or_unbounded_nodes(self):
        for kind,key,value in [('aws_launch_template','instance_type','t3a.medium'),('aws_eks_node_group','scaling_config',[dict(min_size=1,max_size=3,desired_size=1)]),('aws_eks_node_group','instance_types',['t3a.small'])]:
            p=plan();next(x for x in p['resource_changes'] if x['type']==kind)['change']['after'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):m.validate(p)
    def test_rejects_omitted_type_or_surplus_credit_and_large_root(self):
        for mutate in [lambda a:a.pop('instance_type'),lambda a:a.update(credit_specification=[{'cpu_credits':'unlimited'}]),lambda a:a['block_device_mappings'][0]['ebs'][0].update(volume_size=100)]:
            p=plan();mutate(next(x for x in p['resource_changes'] if x['type']=='aws_launch_template')['change']['after'])
            with self.assertRaises(ValueError):m.validate(p)
    def test_rejects_system_replacement_and_new_nat(self):
        for address,actions in [('aws_eks_node_group.system["ap-northeast-2a-t3a-medium"]',['delete','create']),('aws_nat_gateway.this["ap-northeast-2b"]',['create'])]:
            p=plan();p['resource_changes'].append(dict(address=address,type='extra',mode='managed',change=dict(actions=actions,after={})))
            with self.assertRaises(ValueError):m.validate(p,True)
    def test_rejects_any_eks_control_plane_change(self):
        p=plan();row=next(x for x in p['resource_changes'] if x['address']=='aws_eks_cluster.this');row['change']['actions']=['update']
        with self.assertRaises(ValueError):m.validate(p,True)
    def test_accepts_partial_and_no_changes(self):
        p=plan()
        for r in p['resource_changes']:
            if r['type']!='aws_eks_node_group':r['change']['actions']=['no-op']
        self.assertEqual(m.validate(p),{'create':3})
        for r in p['resource_changes']:r['change']['actions']=['no-op']
        self.assertEqual(m.validate(p),{})
    def test_initial_group_repair_requires_explicit_flag_and_exact_old_identity(self):
        p=plan();r=next(x for x in p['resource_changes'] if x['type']=='aws_eks_node_group');az=r['address'].split('["',1)[1].split('"',1)[0]
        r['change']['actions']=['delete','create'];r['change']['before']=copy.deepcopy(r['change']['after']);r['change']['before']['node_group_name']='kafka-'+az+'-'+m.TYPES[az].replace('.','-')
        with self.assertRaises(ValueError):m.validate(p)
        self.assertEqual(m.validate(p,True).get('delete,create'),1)
        r['change']['before']['node_role_arn']='unreviewed'
        with self.assertRaises(ValueError):m.validate(p,True)
    def test_rejects_wrong_account_and_partial_plan(self):
        p=plan();p['variables']['expected_account_id']['value']='111111111111'
        with self.assertRaises(ValueError):m.validate(p)
        p=plan();p['complete']=False
        with self.assertRaises(ValueError):m.validate(p)
        p=plan();p['resource_changes'].pop()
        with self.assertRaises(ValueError):m.validate(p)


if __name__=='__main__':unittest.main()
