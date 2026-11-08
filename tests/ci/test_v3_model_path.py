import copy,importlib.util,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('path_plan',ROOT/'scripts/check-v3-model-path-plan.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
def fixture():
 return {'resource_changes':[{'address':a,'mode':'managed','change':{'actions':['create'],'before':None,'after':v,'after_unknown':{'vpc_peering_connection_id':True}if a.startswith('aws_route.')else{}}}for a,v in m.expected().items()]}
class Plan(unittest.TestCase):
 def test_exact_additive_plan(self):self.assertEqual(m.validate(fixture())['creates'],7)
 def test_default_empty(self):self.assertEqual(m.validate({},False)['creates'],0)
 def test_gpu_or_other_resources_rejected(self):
  p=fixture();p['resource_changes'][0]['address']='aws_instance.gpu'
  with self.assertRaises(ValueError):m.validate(p)
 def test_changes_or_delete_rejected(self):
  for actions in [['update'],['delete'],['delete','create']]:
   p=fixture();p['resource_changes'][0]['change']['actions']=actions
   with self.assertRaises(ValueError):m.validate(p)
 def test_route_widening_rejected(self):
  p=fixture();next(c for c in p['resource_changes']if c['address'].startswith('aws_route.to_embedding'))['change']['after']['destination_cidr_block']='10.30.0.0/16'
  with self.assertRaises(ValueError):m.validate(p)
 def test_public_or_other_source_rejected(self):
  for key,value in [('cidr_ipv4','0.0.0.0/0'),('referenced_security_group_id','sg-other'),('from_port',0),('to_port',65535)]:
   p=fixture();next(c for c in p['resource_changes']if c['address'].startswith('aws_vpc_security'))['change']['after'][key]=value
   with self.assertRaises(ValueError):m.validate(p)
 def test_wrong_dns_vpc_rejected(self):
  p=fixture();next(c for c in p['resource_changes']if c['address'].startswith('aws_route53'))['change']['after']['vpc_id']='vpc-other'
  with self.assertRaises(ValueError):m.validate(p)
 def test_other_gateway_rejected(self):
  p=fixture();next(c for c in p['resource_changes']if c['address'].startswith('aws_route.'))['change']['after']['nat_gateway_id']='nat-other'
  with self.assertRaises(ValueError):m.validate(p)
if __name__=='__main__':unittest.main()
