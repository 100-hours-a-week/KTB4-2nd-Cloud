"""Opt-in TLS/identity/connection policy boundaries, not runtime deployment proof."""
from copy import deepcopy
import importlib.util,json,os,subprocess,tempfile,unittest,sys
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
def module(name,path):
 s=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
apps=module('apps','scripts/check-v3-apps.py');access=module('access','scripts/check-v3-backend-access.py');supply=module('supply','scripts/prepare-v3-backend-supply.py');values=module('values','scripts/prepare_v3_backend_values_contract.py')
BASE=ROOT/'deploy/v3/environments/foundation/backend.values.yaml';PROFILE=ROOT/'deploy/v3/environments/foundation/backend-connections.values.yaml';FIXTURE=ROOT/'tests/ci/v3/backend-values.fixture.yaml'
class Connections(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.tmp.cleanup);d=Path(cls.tmp.name)
  subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(d/'key'),'-out',str(d/'ca'),'-days','2','-subj','/CN=synthetic-ci-ca','-addext','basicConstraints=critical,CA:TRUE'],capture_output=True,check=True)
  cls.pem=(d/'ca').read_text();cls.access_file=d/'access.json';cls.access_file.write_text(json.dumps({'enabled':True,'certificates':{k:cls.pem for k in ['mysql','redis','mongodb','kafka']}}))
 def rendered(self,override=None):
  with tempfile.TemporaryDirectory() as d:
   files=[BASE,FIXTURE,PROFILE]
   if override:
    p=Path(d)/'v.yaml';p.write_text(yaml.safe_dump(override));files.append(p)
   v=apps.load_values(files);docs=apps.platform.load_documents(apps.render(files));apps.validate(docs,v);return docs,v
 def reject(self,override):
  with self.assertRaises((ValueError,KeyError,subprocess.CalledProcessError)):self.rendered(override)
 def test_disabled_creates_nothing(self):
  self.assertEqual(apps.platform.load_documents(apps.render([BASE,PROFILE])),[]);self.assertEqual(access.platform.load_documents(access.render()),[])
 def test_exact_cluster_ai_service_allowed_without_external_http(self):
  settings=self.settings();settings['AI_SERVER_BASE_URL']='http://ai-api.ai.svc.cluster.local:8000'
  image='ghcr.io/100-hours-a-week/backend@sha256:'+'a'*64
  self.assertEqual(values.prepare('auth',image,'b'*40,settings,'c'*64)['defaults']['configMapData']['AI_SERVER_BASE_URL'],settings['AI_SERVER_BASE_URL'])
  for bad in ['http://example.invalid:8000','http://ai-api.ai.svc.cluster.local:8001','http://ai-api.ai.svc.cluster.local:8000/path','http://ai-api.ai.svc.cluster.local:8000?token=x','http://ai-api.ai.svc.cluster.local.evil:8000']:
   settings['AI_SERVER_BASE_URL']=bad
   with self.subTest(url=bad),self.assertRaises(ValueError):values.prepare('auth',image,'b'*40,settings,'c'*64)
 def test_public_endpoints_still_require_https(self):
  settings=self.settings();settings['FRONTEND_ORIGIN']='http://v3.yeodam-2gether.com'
  with self.assertRaises(ValueError):values.prepare('auth','ghcr.io/100-hours-a-week/backend@sha256:'+'a'*64,'b'*40,settings,'c'*64)
 def test_first_auth_and_all_role_budget(self):
  docs,v=self.rendered({'workloads':{r:{'enabled':True} for r in values.ROLES}})
  self.assertEqual(len(docs),13);apps.platform.schema_check(yaml.safe_dump_all(docs))
  for d in [x for x in docs if x['kind']=='Deployment']:
   pod=d['spec']['template']['spec'];self.assertEqual(len(pod['initContainers']),2);self.assertEqual(pod['containers'][0]['resources']['requests']['memory'],'512Mi');self.assertEqual(pod['containers'][0]['resources']['limits']['memory'],'1Gi')
 def test_wrong_role_password_rejected(self):self.reject({'workloads':{'auth-api':{'secretRefs':{'KAFKA_SASL_PASSWORD':{'name':'kafka-photo-converter','key':'password'}}}}})
 def test_connections_cannot_bypass_backend_contract(self):self.reject({'backendContract':False})
 def test_configuration_revision_must_be_explicit(self):
  with self.assertRaises(ValueError):values.prepare('auth','ghcr.io/100-hours-a-week/backend@sha256:'+'a'*64,'b'*40,self.settings(),'')
 def test_tls_disable_rejected(self):self.reject({'defaults':{'configMapData':{'REDIS_SSL_ENABLED':'false'}}})
 def test_mysql_identity_bypass_rejected(self):self.reject({'defaults':{'configMapData':{'MYSQL_URL':'jdbc:mysql://mysql-primary.mysql.svc.cluster.local/yeodam?sslMode=REQUIRED'}}})
 def test_spring_credential_literal_or_wrong_mongo_binding_rejected(self):self.reject({'defaults':{'springProperties':{'spring':{'mongodb':{'uri':'mongodb://wrong:password@localhost'}}}}})
 def test_sentinel_name_and_admin_identity_rejected(self):self.reject({'defaults':{'springProperties':{'spring':{'data':{'redis':{'sentinel':{'username':'admin'}}}}}}})
 def test_kafka_dedicated_security_protocol_cannot_fall_back_to_plaintext(self):
  docs,values=self.rendered()
  self.assertEqual(values['defaults']['springProperties']['spring']['kafka']['security']['protocol'],'SASL_SSL')
  self.reject({'defaults':{'springProperties':{'spring':{'kafka':{'security':{'protocol':'PLAINTEXT'}}}}}})
 def test_jvm_override_rejected(self):self.reject({'defaults':{'configMapData':{'JAVA_TOOL_OPTIONS':'-XX:MaxRAMPercentage=90'}}})
 def test_actual_eks_version_suffix_is_accepted(self):
  source=subprocess.check_output([os.environ['V3_HELM'],'template','yeodam-v3-backend-access',str(ROOT/'deploy/v3/backend-access'),'--kube-version','1.36.5-eks-577a51e','-f',str(self.access_file)],text=True)
  access.validate(access.platform.load_documents(source),True)
  with self.assertRaises(subprocess.CalledProcessError):subprocess.check_output([os.environ['V3_HELM'],'template','yeodam-v3-backend-access',str(ROOT/'deploy/v3/backend-access'),'--kube-version','1.37.0-eks-example','-f',str(self.access_file)],text=True,stderr=subprocess.DEVNULL)
 def test_access_and_public_ca_schema(self):
  source=access.render([self.access_file]);docs=access.platform.load_documents(source);access.validate(docs,True);access.platform.schema_check(source)
 def test_policy_peer_widening_rejected(self):
  docs=access.platform.load_documents(access.render([self.access_file]));next(d for d in docs if d['kind']=='NetworkPolicy')['spec']['podSelector']={}
  with self.assertRaises(ValueError):access.validate(docs,True)
 def test_private_key_and_expired_or_invalid_ca_rejected(self):
  for p in [self.pem+'\nPRIVATE KEY','-----BEGIN CERTIFICATE-----bad-----END CERTIFICATE-----']:
   with self.assertRaises(ValueError):supply.public_ca(p)
 def raw(self):return {'mysql':{'appPassword':'a'*48,'tlsCa':self.pem,'rootPassword':'must-not-export'},'redis':{'appPassword':'b'*48,'sentinelClientPassword':'c'*48,'tlsCa':self.pem,'adminPassword':'must-not-export'},'mongodb':{'appPassword':'d'*48,'tlsCa':self.pem},'kafkaCa':self.pem,'kafkaPasswords':{r:'synthetic-fixture-password' for r in values.ROLES},'external':{k:'fixture-'+('e'*32) for k in supply.EXTERNAL}}
 def test_supply_projects_app_credentials_only(self):
  sources,public=supply.prepare(self.raw());self.assertEqual(set(sources),{'auth','trip','photo'});self.assertNotIn('must-not-export',json.dumps(sources));self.assertNotIn('synthetic-fixture-password',json.dumps(public));self.assertIn('tls=true',sources['auth']['MONGODB_URI']);self.assertNotIn('KAFKA_PHOTO_CONVERTER_PASSWORD',sources['auth'])
 def test_supply_selected_namespaces_reuses_common_session_identity(self):
  raw=self.raw();raw['kafkaPasswords'].pop('auth-api');sources,_=supply.prepare(raw,['trip','photo'])
  self.assertEqual(set(sources),{'trip','photo'})
  for key in ['JWT_SECRET','WEBSOCKET_INTERNAL_SERVICE_TOKEN']:
   self.assertEqual(sources['trip'][key],sources['photo'][key])
  self.assertNotIn('KAFKA_AUTH_API_PASSWORD',json.dumps(sources))
  for selected in [[],['auth','auth'],['production']]:
   with self.assertRaises(ValueError):supply.prepare(raw,selected)
 def test_unsafe_jaas_or_missing_external_rejected(self):
  raw=self.raw();raw['kafkaPasswords']['auth-api']='bad"quoted-password'
  with self.assertRaises(ValueError):supply.prepare(raw)
  raw=self.raw();raw['external'].pop('JWT_SECRET')
  with self.assertRaises(ValueError):supply.prepare(raw)
 def test_source_creation_refuses_existing_without_mutation(self):
  from unittest.mock import patch
  from types import SimpleNamespace
  calls=[]
  def result(args,**kwargs):
   calls.append(args)
   return SimpleNamespace(returncode=0,stdout=json.dumps({'Account':'483175530259'}) if 'get-caller-identity' in args else '{}',stderr='')
  with patch.object(supply.subprocess,'run',side_effect=result):
   with self.assertRaises(ValueError):supply.create({'auth':{}},'fixture-profile')
  self.assertFalse(any('create-secret' in a for a in calls))
 def test_plan_rejects_shared_resource_and_partial_creates(self):
  guard=module('plan','scripts/check-v3-backend-plan.py')
  self.assertEqual(guard.validate({'complete':True,'resource_changes':[]})['creates'],0)
  for address,actions in [('aws_instance.gpu',['create']),('aws_iam_role.app["auth-api"]',['delete']),('aws_iam_role.app["auth-api"]',['create'])]:
   with self.assertRaises(ValueError):guard.validate({'complete':True,'resource_changes':[{'mode':'managed','address':address,'change':{'actions':actions,'after':{}}}]})
 def test_exact_cors_only_addition_and_wildcard_rejection(self):
  guard=module('cors_plan','scripts/check-v3-backend-plan.py')
  after={'bucket':values.BUCKET,'cors_rule':[{'id':'v3-browser-qa','allowed_origins':['https://v3.yeodam-2gether.com'],'allowed_methods':['GET','HEAD','PUT','POST'],'allowed_headers':['*'],'expose_headers':['ETag','x-amz-request-id'],'max_age_seconds':600}]}
  plan={'complete':True,'resource_changes':[{'mode':'managed','address':'aws_s3_bucket_cors_configuration.qa[0]','change':{'actions':['create'],'before':None,'after':after}}]}
  self.assertEqual(guard.validate(plan,True)['creates'],1)
  with self.assertRaises(ValueError):guard.validate(plan)
  after['cors_rule'][0]['allowed_origins']=['*']
  with self.assertRaises(ValueError):guard.validate(plan,True)
 def tagging_plan(self):
  guard=module('tagging_plan','scripts/check-v3-backend-plan.py')
  rows=[]
  for role in sorted(guard.WRITERS):
   before={'name':'v3-app-data','role':'yeodam-v3-foundation-be-'+role,'policy':json.dumps(guard.s3_policy(role,False))}
   after={**before,'policy':json.dumps(guard.s3_policy(role))}
   rows.append({'mode':'managed','address':f'aws_iam_role_policy.s3["{role}"]','change':{'actions':['update'],'before':before,'after':after,'after_unknown':{}}})
  return guard,{'complete':True,'resource_changes':rows}
 def test_tagging_only_accepts_exact_three_writer_updates_and_no_change(self):
  guard,plan=self.tagging_plan()
  self.assertEqual(guard.validate(plan,tagging_only=True)['updates'],3)
  self.assertEqual(guard.validate({'complete':True,'resource_changes':[]},tagging_only=True)['updates'],0)
  with self.assertRaises(ValueError):guard.validate(plan)
 def test_tagging_only_rejects_role_resource_action_and_metadata_widening(self):
  guard,plan=self.tagging_plan()
  for mutation in ['reader','wildcard','extra-action','metadata','delete','partial','unknown','unrelated']:
   candidate=deepcopy(plan);row=candidate['resource_changes'][0];change=row['change']
   if mutation=='reader':row['address']='aws_iam_role_policy.s3["trip-result-worker"]'
   if mutation in ['wildcard','extra-action']:
    policy=json.loads(change['after']['policy'])
    if mutation=='wildcard':policy['Statement'][1]['Resource']=['*']
    else:policy['Statement'][1]['Action'].append('s3:GetObjectTagging')
    change['after']['policy']=json.dumps(policy)
   if mutation=='metadata':change['after']['name']='other-policy'
   if mutation=='delete':change['actions']=['delete']
   if mutation=='partial':candidate['resource_changes'].pop()
   if mutation=='unknown':change['after_unknown']={'role':True}
   if mutation=='unrelated':row['address']='aws_instance.gpu'
   with self.subTest(mutation=mutation),self.assertRaises(ValueError):guard.validate(candidate,tagging_only=True)
 def settings(self):return {**{k:'https://example.invalid' for k in values.KEYS},'AI_EC2_INSTANCE_ID':'i-'+'1'*17,'redisSentinelFeatureReviewed':False}
 def test_stage_requires_compatibility_review_and_immutable_image(self):
  s=self.settings();i='ghcr.io/100-hours-a-week/backend@sha256:'+'a'*64
  a=values.prepare('auth',i,'b'*40,s,'c'*64);self.assertEqual(sum(w['enabled'] for w in a['workloads'].values()),1)
  with self.assertRaises(ValueError):values.prepare('all',i,'b'*40,s,'c'*64)
  s['redisSentinelFeatureReviewed']=True;self.assertEqual(sum(w['enabled'] for w in values.prepare('all',i,'b'*40,s,'c'*64)['workloads'].values()),5)
  with self.assertRaises(ValueError):values.prepare('auth','backend:latest','b'*40,s,'c'*64)
 def test_original_roots_kept_by_actual_keytool_script(self):
  with tempfile.TemporaryDirectory() as d:
   d=Path(d);ca=d/'ca';trust=d/'trust';ca.mkdir();trust.mkdir()
   for n in ['mysql','redis','mongodb','kafka']:(ca/(n+'.crt')).write_text(self.pem)
   home=subprocess.check_output(['/usr/libexec/java_home'],text=True).strip() if sys.platform=='darwin' else os.environ['JAVA_HOME']
   script=(ROOT/'deploy/v3/apps/files/prepare-trust.sh').read_text().replace('/etc/yeodam/ca',str(ca)).replace('/etc/yeodam/trust',str(trust))
   subprocess.run(['/bin/sh','-ec',script],env=dict(os.environ,JAVA_HOME=home),capture_output=True,check=True)
   before=subprocess.check_output(['keytool','-J-Duser.language=en','-J-Duser.country=US','-list','-keystore',home+'/lib/security/cacerts','-storepass','changeit'],text=True,stderr=subprocess.DEVNULL)
   after=subprocess.check_output(['keytool','-J-Duser.language=en','-J-Duser.country=US','-list','-keystore',str(trust/'cacerts'),'-storepass','public-ca-only'],text=True,stderr=subprocess.DEVNULL)
   import re
   self.assertEqual(int(re.search(r'(\d+) entries',after)[1]),int(re.search(r'(\d+) entries',before)[1])+4)
   self.assertIn('yeodam-v3-mongodb',after)
if __name__=='__main__':unittest.main()
