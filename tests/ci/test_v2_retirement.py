"""Deletion boundary mutations use public metadata only; no AWS calls or secrets."""
import copy,json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import v2_retirement as r

def plan(changes):
 return {'configuration':{'provider_config':{'aws':{'full_name':'registry.terraform.io/hashicorp/aws','expressions':{'region':{'constant_value':r.POLICY['region']},'allowed_account_ids':{'constant_value':[r.POLICY['account']]}}}}},'resource_changes':changes}
def deletion(address,phase='production'):
 scope=r.POLICY['states'][phase];typ=address.split('.')[-2];before={'id':scope['expectedIds'].get(address,'association'),'vpc_id':r.POLICY['retiredVpc']}
 if address==r.ASSOCIATION:typ='aws_route53_zone_association';before.update(zone_id=r.POLICY['privateZone'])
 return {'mode':'managed','address':address,'type':typ,'change':{'actions':['delete'],'before':before,'after':None}}
def production():return plan([deletion('aws_instance.kafka'),deletion(r.ASSOCIATION)])
def adoption():
 return plan([{'mode':'managed','address':r.ASSOCIATION,'type':'aws_route53_zone_association','change':{'actions':['no-op'],'before':None,'after':{'zone_id':r.POLICY['privateZone'],'vpc_id':r.POLICY['retiredVpc']},'importing':{'id':r.POLICY['privateZone']+':'+r.POLICY['retiredVpc']}}}])

class RetirementContracts(unittest.TestCase):
 def test_valid_fixed_deletion(self):self.assertEqual(r.inspect_plan(production(),'production')['deletions'],2)
 def test_all_four_fixed_instances_match_inventory(self):
  for address,identity in r.INSTANCES.items():
   self.assertEqual(r.POLICY['states']['production']['expectedIds'][address],identity)
   self.assertEqual(r.inspect_plan(plan([deletion(address),deletion(r.ASSOCIATION)]),'production')['deletions'],2)
 def test_adoption_is_state_only(self):self.assertEqual(r.inspect_plan(adoption(),'association')['imports'],1)
 def test_import_rejects_gpu_v3_and_foreign_zone(self):
  for key,value in [('vpc_id',r.POLICY['gpuVpc']),('vpc_id',r.POLICY['v3Vpc']),('zone_id','foreign')]:
   p=adoption();p['resource_changes'][0]['change']['after'][key]=value
   with self.subTest(key=key,value=value),self.assertRaises(ValueError):r.inspect_plan(p,'association')
 def test_adoption_never_deletes_or_creates(self):
  for action in [['create'],['update'],['delete'],['delete','create']]:
   p=adoption();p['resource_changes'][0]['change']['actions']=action
   with self.subTest(action=action),self.assertRaises(ValueError):r.inspect_plan(p,'association')
 def test_final_production_requires_association_removal(self):
  p=plan([deletion('aws_instance.kafka')])
  with self.assertRaises(ValueError):r.inspect_plan(p,'production')
  self.assertTrue(r.inspect_plan(p,'production',draft=True)['draft'])
 def test_unknown_resource_and_wrong_identity_block(self):
  for address in ['aws_instance.unreviewed','aws_instance.kafka']:
   p=production();p['resource_changes'][0]['address']=address;p['resource_changes'][0]['change']['before']['id']='i-foreign'
   with self.subTest(address=address),self.assertRaises(ValueError):r.inspect_plan(p,'production')
 def test_all_non_delete_mutations_block(self):
  for action in [['create'],['update'],['delete','create'],['create','delete']]:
   p=production();p['resource_changes'][0]['change']['actions']=action
   with self.subTest(action=action),self.assertRaises(ValueError):r.inspect_plan(p,'production')
 def test_retained_certificate_oidc_bucket_and_gpu_never_delete(self):
  for phase,address in [('production','module.app_alb.aws_acm_certificate.app'),('production','module.ecs_foundation.aws_secretsmanager_secret.ghcr_credentials'),('production','aws_route53_zone.production_private'),('base','aws_iam_openid_connect_provider.github_actions'),('base','aws_s3_bucket.app_data')]:
   c=deletion(address,phase)
   with self.subTest(address=address),self.assertRaises(ValueError):r.inspect_plan(plan([c]),phase)
  p=production();p['resource_changes'][0]['change']['before']['id']=r.POLICY['gpuInstance']
  with self.assertRaises(ValueError):r.inspect_plan(p,'production')
 def test_staging_tagged_resource_never_deletes(self):
  p=production();p['resource_changes'][0]['change']['before']['tags']={'Environment':'staging'}
  with self.assertRaises(ValueError):r.inspect_plan(p,'production')
 def test_retained_resource_update_blocks(self):
  p=production();p['resource_changes'][0]['address']='aws_route53_zone.production_private';p['resource_changes'][0]['change']['actions']=['update']
  with self.assertRaises(ValueError):r.inspect_plan(p,'production')
 def test_shared_gpu_rule_requires_retired_peer(self):
  a='aws_vpc_security_group_ingress_rule.shared_gpu_production["embedding"]';p=production();c=deletion(a);c['type']='aws_vpc_security_group_ingress_rule';c['change']['before'].pop('vpc_id');c['change']['before'].update(security_group_id=r.POLICY['gpuSecurityGroup'],referenced_security_group_id='sg-v3');p['resource_changes'].append(c)
  with self.assertRaises(ValueError):r.inspect_plan(p,'production')
  c['change']['before']['referenced_security_group_id']=r.POLICY['states']['production']['expectedIds']['aws_security_group.ai_kafka_clients'];self.assertEqual(r.inspect_plan(p,'production')['deletions'],3)
 def test_v3_peering_and_return_route_block(self):
  c=deletion('aws_vpc_peering_connection.shared_gpu');c['change']['before'].update(vpc_id=r.POLICY['v3Vpc'],peer_vpc_id=r.POLICY['gpuVpc'])
  with self.assertRaises(ValueError):r.inspect_plan(plan([c,deletion(r.ASSOCIATION)]),'production')
  c=deletion('aws_route.shared_gpu_to_production');c['change']['before'].pop('vpc_id');c['change']['before'].update(route_table_id='rtb-0fc40e73348be69f0',destination_cidr_block='10.40.0.0/16')
  with self.assertRaises(ValueError):r.inspect_plan(plan([c,deletion(r.ASSOCIATION)]),'production')
 def test_account_and_region_are_fixed(self):
  for key,value in [('region','us-east-1'),('allowed_account_ids',['000000000000'])]:
   p=production();p['configuration']['provider_config']['aws']['expressions'][key]['constant_value']=value
   with self.subTest(key=key),self.assertRaises(ValueError):r.inspect_plan(p,'production')
 def test_secret_recovery_window_cannot_be_forced(self):
  a=next(x for x in r.POLICY['states']['production']['expectedIds'] if '.aws_secretsmanager_secret.' in '.'+x)
  c=deletion(a);c['type']='aws_secretsmanager_secret';c['change']['before']['recovery_window_in_days']=0
  with self.assertRaises(ValueError):r.inspect_plan(plan([c,deletion(r.ASSOCIATION)]),'production')
 def test_config_has_only_reviewed_retained_resources(self):
  for label in ['production','base']:
   for p in (ROOT/'terraform/v2/retirement'/label).rglob('retained.tf.json'):
    d=json.loads(p.read_text())
    for typ,resources in d.get('resource',{}).items():
     self.assertNotIn(typ,['aws_instance','aws_nat_gateway','aws_ecs_service','aws_lb','aws_ebs_volume','aws_secretsmanager_secret_version'])
     for conf in resources.values():self.assertIs(conf['lifecycle']['prevent_destroy'],True)
    self.assertNotIn('secret_string',p.read_text());self.assertNotIn('secret_binary',p.read_text())
 def test_declared_retained_addresses_match_existing_state_indexes(self):
  for label,scope in r.POLICY['states'].items():
   base=ROOT/'terraform/v2/retirement'/label;addresses=set()
   for path in base.rglob('retained.tf.json'):
    prefix='module.'+path.parent.name+'.' if path.parent!=base else ''
    for typ,items in json.loads(path.read_text())['resource'].items():
     for name,conf in items.items():
      a=prefix+typ+'.'+name
      if 'count' in conf:addresses.update(a+'['+str(i)+']' for i in range(conf['count']))
      elif 'for_each' in conf:addresses.update(a+'['+json.dumps(k)+']' for k in conf['for_each'])
      else:addresses.add(a)
   self.assertEqual(addresses,set(scope['retained']))
 def test_old_roots_fail_closed_by_default(self):
  for p in [ROOT/'terraform/retirement-guard.tf',ROOT/'terraform/v2/production/retirement-guard.tf']:
   t=p.read_text();self.assertIn('default = false',t);self.assertIn('condition     = var.allow_retired_environment_apply',t)
 def test_backend_ownership_keys_are_unchanged(self):
  for label,p in r.POLICY['states'].items():self.assertEqual(json.loads((ROOT/'terraform/v2/retirement'/label/'main.tf.json').read_text())['terraform']['backend']['s3']['key'],p['backendKey'])
 def test_v3_dns_association_remains_independent(self):
  t=(ROOT/'terraform/v2/retirement/production/retained.tf.json').read_text();d=json.loads(t)['resource']['aws_route53_zone']['production_private'];self.assertEqual(d['lifecycle']['ignore_changes'],['vpc']);self.assertEqual(d['vpc'][0]['vpc_id'],r.POLICY['gpuVpc'])
if __name__=='__main__':unittest.main()
