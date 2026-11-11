import copy,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
def load(name,path):
 s=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
apm=load('apm_check','scripts/check-v3-apm.py');ai=load('ai_check','scripts/check-v3-ai.py');be=load('be_check','scripts/check-v3-apps.py')
class APM(unittest.TestCase):
 def test_backend_default_disabled_and_resource_growth_rejected(self):
  self.assertEqual(apm.render(False),[]);docs=apm.render();self.assertTrue(apm.validate(docs))
  d=copy.deepcopy(docs);next(x for x in d if x['kind']=='Deployment')['spec']['replicas']=2
  with self.assertRaises(ValueError):apm.validate(d)
 def test_public_service_or_sanitizer_tampering_rejected(self):
  for mutate in [lambda d:next(x for x in d if x['kind']=='Service')['spec'].update(type='LoadBalancer'),lambda d:next(x for x in d if x['kind']=='ConfigMap')['data'].update({'config.alloy':'debug raw payload'})]:
   docs=apm.render();mutate(docs)
   with self.assertRaises(ValueError):apm.validate(docs)
 def test_ai_selected_role_only_and_existing_commands_preserved(self):
  v=ai.fixture();v['apmWorkloads']=['api'];d=ai.platform.load_documents(ai.render(v));ai.validate(d,v)
  deps={x['metadata']['name']:x for x in d if x['kind']=='Deployment'}
  self.assertEqual(len(deps['ai-api']['spec']['template']['spec']['initContainers']),2)
  self.assertEqual(len(deps['ai-runner']['spec']['template']['spec']['initContainers']),1)
  self.assertEqual(deps['ai-runner']['spec']['template']['spec']['containers'][0]['command'],['python','/runtime/start-runner.py'])
 def test_ai_exporter_override_and_init_tamper_rejected(self):
  v=ai.fixture();v['apmWorkloads']=['api','runner'];docs=ai.platform.load_documents(ai.render(v))
  for init in [False,True]:
   d=copy.deepcopy(docs);pod=next(x for x in d if x['kind']=='Deployment')['spec']['template']['spec']
   if init:pod['initContainers'][-1]['image']='untrusted:latest'
   else:next(x for x in pod['containers'][0]['env'] if x['name']=='OTEL_EXPORTER_OTLP_ENDPOINT')['value']='https://untrusted.invalid'
   with self.assertRaises(ValueError):ai.validate(d,v)
 def test_be_agent_preserves_heap_and_original_trust_settings(self):
  files=[ROOT/'deploy/v3/environments/foundation/backend.values.yaml',ROOT/'tests/ci/v3/backend-values.fixture.yaml',ROOT/'deploy/v3/environments/foundation/backend-connections.values.yaml']
  v=be.load_values(files);v['apmWorkloads']=['auth-api']
  with tempfile.NamedTemporaryFile('w',suffix='.yaml') as f:
   yaml.safe_dump(v,f);f.flush();docs=be.platform.load_documents(be.render([Path(f.name)]));be.validate(docs,v)
  dep=next(x for x in docs if x['kind']=='Deployment' and x['metadata']['name']=='auth-api');env={x['name']:x.get('value') for x in dep['spec']['template']['spec']['containers'][0]['env']}
  self.assertTrue(env['JAVA_TOOL_OPTIONS'].endswith(' -javaagent:/otel/javaagent.jar'))
  self.assertIn('-XX:MaxRAMPercentage=50.0',env['JAVA_TOOL_OPTIONS'])
  self.assertIn('-Djavax.net.ssl.trustStore=/etc/yeodam/trust/cacerts',env['JAVA_TOOL_OPTIONS'])
  self.assertIn('-Djavax.net.ssl.trustStorePassword=public-ca-only',env['JAVA_TOOL_OPTIONS'])
  bad=copy.deepcopy(docs);target=next(x for x in bad if x['kind']=='Deployment' and x['metadata']['name']=='auth-api')
  next(x for x in target['spec']['template']['spec']['containers'][0]['env'] if x['name']=='JAVA_TOOL_OPTIONS')['value']+=' -Xmx3G'
  with self.assertRaises(ValueError):be.validate(bad,v)
 def test_existing_grafana_tempo_datasource_and_self_metrics_opt_in(self):
  import subprocess
  text=subprocess.check_output([os.environ['V3_HELM'],'template','metrics',str(ROOT/'deploy/v3/metrics'),'--set','enabled=true','--set','apmEnabled=true'],text=True)
  docs=list(yaml.safe_load_all(text));data=next(x for x in docs if x and x['kind']=='ConfigMap' and x['metadata']['name']=='v3-metrics-config')['data']
  grafana=next(x for x in docs if x and x['kind']=='ConfigMap' and x['metadata']['name']=='v3-grafana-config')['data']
  source=yaml.safe_load(grafana['datasource.yaml']);tempo=next(x for x in source['datasources'] if x['uid']=='v3-tempo')
  self.assertEqual(tempo['type'],'tempo');self.assertEqual(tempo['url'],'http://v3-tempo:3200')
  jobs=yaml.safe_load(data['prometheus.yaml'])['scrape_configs']
  self.assertTrue({'apm-alloy','apm-tempo'}.issubset({x['job_name'] for x in jobs}))
if __name__=='__main__':unittest.main()
