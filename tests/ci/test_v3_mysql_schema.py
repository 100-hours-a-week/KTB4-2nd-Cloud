import copy,importlib.util,json,os,tempfile,unittest,subprocess
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def module(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
c=module('schema',ROOT/'scripts/check-v3-mysql-schema.py');a=module('cdc',ROOT/'scripts/manage-v3-mysql-cdc.py');p=module('prepare',ROOT/'scripts/prepare-v3-mysql-schema-image.py')
class Contracts(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.docs=c.platform.load_documents(c.render([ROOT/'tests/ci/v3/mysql-schema.values.fixture.yaml']))
 def test_default_and_contract(self):
  c.validate(c.platform.load_documents(c.render()),False);c.validate(self.docs,True);c.validate_source()
 def test_sizing_scheduling_secret_and_command_mutations_rejected(self):
  for change in ['budget','placement','root','command','token','image','extra']:
   with self.subTest(change=change):
    docs=copy.deepcopy(self.docs);spec=next(x for x in docs if x['kind']=='Job')['spec']['template']['spec'];container=spec['containers'][0]
    if change=='budget':container['resources']['limits']['memory']='1Gi'
    if change=='placement':spec['nodeSelector']={'yeodam.io/role':'data'}
    if change=='root':spec['volumes'][1]['secret']['items'][0]['key']='root-password'
    if change=='command':container['command']=['bash','-c','echo bypass']
    if change=='token':spec['automountServiceAccountToken']=True
    if change=='image':container['image']='redgate/flyway:latest'
    if change=='extra':docs.append({'kind':'Secret','metadata':{'name':'bad'}})
    with self.assertRaises(ValueError):c.validate(docs,True)
 def test_network_widening_rejected(self):
  docs=copy.deepcopy(self.docs);next(x for x in docs if x['kind']=='NetworkPolicy')['spec']['egress'][0]['to']=[{'podSelector':{}}]
  with self.assertRaises(ValueError):c.validate(docs,True)
 def test_cdc_table_scope_cannot_expand(self):
  with patch.dict(c.LOCK,{'cdcTables':['users']}):
   with self.assertRaisesRegex(ValueError,'Outbox'):c.validate_source()
 def test_cdc_sql_rejects_injection_and_has_no_global_select(self):
  for value in ['bad',"a'*48",'a'*49]:
   with self.assertRaises(ValueError):a.create_sql(value)
  sql=a.create_sql('a'*48);self.assertEqual(sql.count('GRANT SELECT ON yeodam.'),4);self.assertNotIn('GRANT SELECT ON *.*',sql);self.assertNotIn('ALTER USER',sql);self.assertIn('REQUIRE SSL WITH MAX_USER_CONNECTIONS 4',sql)
 def test_unexpected_grants_rejected(self):
  grants="GRANT RELOAD, SHOW DATABASES, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO `v3_connect_cdc`@`%`\n"+'\n'.join(f'GRANT SELECT ON `yeodam`.`{t}` TO `v3_connect_cdc`@`%`' for t in a.TABLES)
  self.assertTrue(a.grants_match(grants));self.assertFalse(a.grants_match(grants.replace('GRANT SELECT','GRANT SELECT, UPDATE',1)));self.assertFalse(a.grants_match(grants+'\nGRANT SELECT ON *.* TO `v3_connect_cdc`@`%`'))
 def test_cached_archive_tamper_rejected_before_extraction(self):
  with tempfile.TemporaryDirectory() as d:
   Path(d,'source.tar.gz').write_bytes(b'tampered')
   with self.assertRaisesRegex(ValueError,'checksum'):p.prepare(Path(d))
 def test_complete_mysql_bootstrap_binding_retained(self):
  import yaml
  root=ROOT/'deploy/v3/environments/foundation';old=yaml.safe_load((root/'mysql-secrets.values.yaml').read_text());new=yaml.safe_load((root/'mysql-cdc-secrets.values.yaml').read_text());self.assertEqual(new['stores']['mysql']['secrets'][:1],old['stores']['mysql']['secrets']);self.assertEqual(len(new['stores']['mysql']['secrets']),2)
class AdminBoundaries(unittest.TestCase):
 def test_existing_secret_never_overwritten(self):
  replies=[subprocess.CompletedProcess([],0,json.dumps({'Account':'483175530259'}),''),subprocess.CompletedProcess([],0,'{}','')]
  with patch('sys.argv',['manage','create-secret']),patch.object(a.subprocess,'run',side_effect=replies) as run:
   with self.assertRaisesRegex(RuntimeError,'exists'):a.main()
  self.assertEqual(run.call_count,2)
 def test_wrong_aws_account_cannot_proceed(self):
  with patch('sys.argv',['manage','create-secret']),patch.object(a.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps({'Account':'000000000000'}),'')) as run:
   with self.assertRaisesRegex(ValueError,'Wrong AWS account'):a.main()
  self.assertEqual(run.call_count,1)
 def test_wrong_cluster_endpoint_cannot_exec(self):
  replies=[{'Account':'483175530259'},'yeodam-v3-foundation',{'cluster':{'status':'ACTIVE','resourcesVpcConfig':{'vpcId':'vpc-0ecc3132f3b2170e9'},'endpoint':'https://verified.eks.example'}},{'clusters':[{'cluster':{'server':'https://other-cluster.example'}}]}]
  def reply(cmd,**kw):
   data=replies.pop(0);return subprocess.CompletedProcess(cmd,0,data if isinstance(data,str) else json.dumps(data),'')
  with patch('sys.argv',['manage','create-account','--kubeconfig','/tmp/test-fixture']),patch.object(a.subprocess,'run',side_effect=reply) as run:
   with self.assertRaisesRegex(ValueError,'endpoint'):a.main()
  self.assertFalse(any('exec' in call.args[0] for call in run.call_args_list))

if __name__=='__main__':unittest.main()
