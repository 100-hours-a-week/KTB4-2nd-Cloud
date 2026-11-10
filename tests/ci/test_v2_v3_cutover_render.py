"""Render the operating profile and prove every unrelated delivery/security constraint stays intact."""
import copy,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import yaml
import v2_v3_cutover as c
import v3_release_bundle as bundle

def module(name,file):
 s=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
outbox=module('outbox602','check-v3-outbox.py');kafka=module('access602','check-v3-kafka-access.py');external=module('external602','check-v3-external.py')

def file_values(value):
 return json.dumps(value)

class Render(unittest.TestCase):
 def test_operating_cdc_only_moves_its_source_partition_and_history(self):
  files=[ROOT/'tests/ci/v3/outbox.values.fixture.json',ROOT/'deploy/v3/cutover/cdc.values.yaml']
  text=outbox.render(files);docs=outbox.connect.kafka.platform.load_documents(text);outbox.connect.kafka.platform.schema_check(text)
  normalized=copy.deepcopy(docs)
  for d in normalized:
   if d['kind']=='KafkaConnector':
    expected=c.operating_cdc(outbox.CONFIG);expected={k:v for k,v in expected.items() if k not in ['connector.class','tasks.max']}
    self.assertEqual(d['spec']['config'],expected);self.assertEqual(d['spec']['state'],'stopped')
    d['spec']['config']={k:v for k,v in outbox.CONFIG.items() if k not in ['connector.class','tasks.max']};d['spec']['state']='running'
   if d['kind']=='KafkaTopic' and d['metadata']['name']==c.POLICY['cdcHistory']:d['metadata']['name']='prod.yeodam.schema-history';d['spec']['topicName']='prod.yeodam.schema-history'
   if d['kind']=='KafkaTopic' and d['metadata']['name']=='v3-operating-outbox-heartbeat':d['metadata']['name']='v3-outbox-heartbeat';d['spec']['topicName']='__debezium-heartbeat.v3.yeodam.outbox'
  outbox.validate(normalized,True,True)
 def test_operating_kafka_acl_adds_no_new_business_ownership(self):
  text=kafka.render([ROOT/'deploy/v3/environments/foundation/kafka-access.values.yaml',ROOT/'deploy/v3/cutover/kafka-access.values.yaml'])
  docs=kafka.kafka.platform.load_documents(text);kafka.kafka.platform.schema_check(text)
  user=next(d for d in docs if d['kind']=='KafkaUser' and d['metadata']['name']=='v3-connect-outbox')
  extras={c.POLICY['cdcHistory'],'__debezium-heartbeat.'+c.POLICY['cdcPrefix'],c.POLICY['cdcPrefix']+'-schemahistory'}
  added=[a for a in user['spec']['authorization']['acls'] if a['resource'].get('name') in extras]
  self.assertEqual(len(added),3);self.assertTrue(all(a['resource']['patternType']=='literal' for a in added))
  self.assertEqual(next(a['operations'] for a in added if a['resource']['name']==c.POLICY['cdcHistory']),['Describe','Read','Write','DescribeConfigs'])
  user['spec']['authorization']['acls']=[a for a in user['spec']['authorization']['acls'] if a not in added]
  kafka.validate(docs,True,False)
 def test_operating_ingress_keeps_all_routes_policies_and_budgets(self):
  values=yaml.safe_load((ROOT/'deploy/v3/cutover/external.values.yaml').read_text());values['albSecurityGroupId']='sg-'+'1'*17
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'values.json';p.write_text(json.dumps(values));text=external.render_access([p])
  docs=external.platform.load_documents(text);external.validate_access(docs,values);external.platform.schema_check(text)
  ingress=[d for d in docs if d['kind']=='Ingress'];self.assertEqual(len(ingress),7)
  self.assertTrue(all(d['spec']['rules'][0]['host']==c.POLICY['domain'] for d in ingress))
  bad=copy.deepcopy(values);bad['host']='other.example.com'
  with self.assertRaises(ValueError):external.validate_access(docs,bad)
 def test_all_nine_operating_apps_render_with_identical_qa_images(self):
  data=c.operating_candidate(json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text()));bundle.prepared(data)
  files={}
  for chart in ['apps','ai-apps','service-access']:
   for path in (ROOT/'deploy/v3'/chart).rglob('*'):
    if path.is_file():files['charts/'+chart+'/'+str(path.relative_to(ROOT/'deploy/v3'/chart))]=path.read_bytes()
  docs=bundle.render_profiles(files,bundle.normalize(data))
  self.assertEqual(sum(d['kind']=='Deployment' for d in docs),9)
  for d in docs:
   if d['kind']=='Deployment':self.assertEqual(d['spec']['replicas'],1)
 def test_mixed_operating_and_qa_callback_is_rejected(self):
  data=c.operating_candidate(json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text()));data['values']['frontend']['defaults']['configMapData']['SERVER_API_BASE_URL']='https://v3.yeodam-2gether.com/api'
  with self.assertRaises(ValueError):bundle.prepared(data)

if __name__=='__main__':unittest.main()
