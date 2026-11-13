"""Isolation/wire/security/lifecycle boundaries for the one-task V3 CDC path."""
import base64,copy,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import yaml
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
def module(name,path):
 s=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
c=module('outbox','scripts/check-v3-outbox.py');secret=module('secret','scripts/check-v3-secrets.py');ca=module('ca','scripts/prepare-v3-outbox-ca.py')
class Outbox(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.docs=c.connect.kafka.platform.load_documents(c.render([ROOT/'tests/ci/v3/outbox.values.fixture.json']))
  cls.worker=c.connect.kafka.platform.load_documents(c.connect.render([ROOT/'tests/ci/v3/connect-outbox.values.fixture.yaml']))
 def test_explicit_staged_render_and_schema(self):
  c.validate(c.connect.kafka.platform.load_documents(c.render()))
  c.validate(self.docs,True,True);c.connect.kafka.platform.schema_check(yaml.safe_dump_all(self.docs));c.connect.validate(self.worker,True,False,True)
  prepared=[d for d in self.docs if d['kind']!='KafkaConnector'];c.validate(prepared,True,False)
 def test_config_mutations_cannot_weaken_tls_filter_or_wire(self):
  for key,value in [('database.hostname','mysql.production.internal'),('database.ssl.mode','required'),('database.password','literal-password'),('snapshot.mode','no_data'),('tasks.max','2'),('transforms.owned.condition',"value.op == 'c'"),('transforms.outbox.table.fields.additional.placement','request_id:header:request_id'),('schema.history.internal.producer.security.protocol','PLAINTEXT'),('database.server.id','1'),('transforms.outbox.table.fields.additional.error.on.missing','true'),('max.queue.size','99999')]:
   with self.subTest(key=key):
    config=copy.deepcopy(c.CONFIG);config[key]=value
    with self.assertRaises(ValueError):c.config_contract(config)
 def test_missing_optional_trace_does_not_relax_mandatory_header_checks(self):
  config=copy.deepcopy(c.CONFIG)
  self.assertEqual(config['errors.tolerance'],'none')
  for field in ('event_source','request_id'):
   strict=" && value.after.getString('"+field+"').length() >= 0"
   self.assertIn(strict,config['transforms.owned.condition'])
   bad=copy.deepcopy(config);bad['transforms.owned.condition']=bad['transforms.owned.condition'].replace(strict,'')
   with self.assertRaises(ValueError):c.config_contract(bad)
 def test_retry_budget_and_fail_closed_snapshot(self):
  for retry in [{'enabled':False},{'enabled':True},{'enabled':True,'maxRestarts':4}]:
   docs=copy.deepcopy(self.docs);next(d for d in docs if d['kind']=='KafkaConnector')['spec']['autoRestart']=retry
   with self.assertRaises(ValueError):c.validate(docs,True,True)
  config=copy.deepcopy(c.CONFIG);config['snapshot.mode']='when_needed'
  with self.assertRaises(ValueError):c.config_contract(config)
 def test_reconnect_interval_does_not_relax_other_recovery_controls(self):
  self.assertEqual(c.CONFIG['connect.keep.alive.interval.ms'],'10000')
  for key,value in [('connect.keep.alive.interval.ms','60000'),('connect.keep.alive','false'),('connect.timeout.ms','1000')]:
   config=copy.deepcopy(c.CONFIG);config[key]=value
   with self.assertRaises(ValueError):c.config_contract(config)
 def test_heartbeat_cannot_enter_business_transforms_or_expand_storage(self):
  for key,value in [('heartbeat.interval.ms','0'),('transforms.owned.negate','false'),('predicates.heartbeat.pattern','.*')]:
   config=copy.deepcopy(c.CONFIG);config[key]=value
   with self.assertRaises(ValueError):c.config_contract(config)
  docs=copy.deepcopy(self.docs);next(d for d in docs if d['metadata']['name']=='v3-outbox-heartbeat')['spec']['config']['retention.bytes']=67108864
  with self.assertRaises(ValueError):c.validate(docs,True,True)
 def test_initial_pending_filter_required_for_each_table(self):
  for t in c.TABLES:
   config=copy.deepcopy(c.CONFIG);config['snapshot.select.statement.overrides.yeodam.'+t]='SELECT * FROM yeodam.'+t
   with self.assertRaises(ValueError):c.config_contract(config)
 def test_no_raw_secret_rbac_node_or_pvc_objects(self):
  for kind in ['Secret','RoleBinding','PersistentVolumeClaim','NodePool']:
   with self.assertRaises(ValueError):c.validate(self.docs+[{'kind':kind}],True,True)
 def test_history_must_be_single_partition_noncompacted_and_retained(self):
  for change in ['partitions','cleanup','retention','ownership']:
   docs=copy.deepcopy(self.docs);t=next(d for d in docs if d['kind']=='KafkaTopic')
   if change=='partitions':t['spec']['partitions']=2
   if change=='cleanup':t['spec']['config']['cleanup.policy']='compact'
   if change=='retention':t['spec']['config']['retention.ms']=1000
   if change=='ownership':t['metadata'].pop('annotations')
   with self.assertRaises(ValueError):c.validate(docs,True,True)
 def test_namespace_and_pod_selectors_cannot_be_broadened(self):
  docs=copy.deepcopy(self.docs);p=next(d for d in docs if d['kind']=='NetworkPolicy');p['spec']['egress'][0]['to'][0]['namespaceSelector']={}
  with self.assertRaises(ValueError):c.validate(docs,True,True)
 def test_worker_minimum_budget_secret_refs_and_provider_allowlist(self):
  for change in ['memory','root-secret','literal','provider','all-env','mount']:
   docs=copy.deepcopy(self.worker);s=next(d for d in docs if d['kind']=='KafkaConnect')['spec']
   if change=='memory':s['resources']['limits']['memory']='1Gi'
   if change=='root-secret':s['template']['connectContainer']['env'][0]['valueFrom']['secretKeyRef']['name']='mysql-bootstrap'
   if change=='literal':s['template']['connectContainer']['env'][0]={'name':'V3_CDC_PASSWORD','value':'bad'}
   if change=='provider':s['config']['config.providers.env.class']='org.apache.kafka.common.config.provider.FileConfigProvider'
   if change=='all-env':s['config']['config.providers.env.param.allowlist.pattern']='.*'
   if change=='mount':s['template']['pod']['volumes'][0]={'name':'mysql-ca','secret':{'secretName':'mysql-bootstrap'}}
   with self.assertRaises(ValueError):c.connect.validate(docs,True,False,True)
 def test_jks_rejects_private_key_entries_corruption_and_bad_encoding(self):
  value=json.loads((ROOT/'tests/ci/v3/outbox.values.fixture.json').read_text())['mysqlTruststore'];c.public_jks(value)
  for data in [b'bad',base64.b64decode(value)[:-1],base64.b64decode(value)[:12]+b'\x00\x00\x00\x01'+base64.b64decode(value)[16:]]:
   with self.assertRaises(ValueError):c.public_jks(base64.b64encode(data).decode())
  with self.assertRaises(ValueError):c.public_jks('not a base64 payload')
 def test_ca_preparer_refuses_private_key_before_subprocess(self):
  with tempfile.TemporaryDirectory() as d,patch.object(ca.subprocess,'run') as run:
   with self.assertRaises(ValueError):ca.prepare('-----BEGIN PRIVATE KEY-----',Path(d)/'output.json')
   run.assert_not_called()
 def test_kafka_secret_reader_cannot_get_bootstrap_or_unrelated_sources(self):
  defaults=yaml.safe_load((ROOT/'deploy/v3/secret-access/values.yaml').read_text());overlay=yaml.safe_load((ROOT/'deploy/v3/environments/foundation/outbox-secrets.values.yaml').read_text());stores=defaults['stores']|overlay['stores']
  text=__import__('subprocess').check_output([os.environ['V3_HELM'],'template','yeodam-v3-secret-access',str(ROOT/'deploy/v3/secret-access'),'-n','default','--kube-version','1.36.0','-f',str(ROOT/'deploy/v3/environments/foundation/outbox-secrets.values.yaml')],text=True)
  docs=secret.platform.load_documents(text);secret.validate_access(docs,stores)
  for field,value in [('remoteKey','yeodam/v3/foundation/mysql/bootstrap'),('property','rootPassword')]:
   bad=copy.deepcopy(stores);bad['kafka']['secrets'][0]['data'][0][field]=value
   with self.assertRaises(ValueError):secret.validate_access(docs,bad)
if __name__=='__main__':unittest.main()
