"""Render and validate complete approved snapshots. Contains public config/Secret references only."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import ssl
from functools import lru_cache
import yaml
from v3_delivery_contract import ROOT, UNITS, APP_NAMES, WAVES, CHARTS, PROFILE_ROLES, COMPONENTS, RESOURCES, KINDS
from v3_service_access import policies, values as service_values


def require(ok,message):
 if not ok: raise ValueError(message)


def load(name):
 s=importlib.util.spec_from_file_location(name,ROOT/'scripts'/('check-v3-'+name+'.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m


apps=load('apps');ai=load('ai');access=load('service-access')


def parse_json(source):
 def unique(pairs):
  result={}
  for key,value in pairs:
   require(key not in result,'Duplicate JSON key rejected')
   result[key]=value
  return result
 return json.loads(source,object_pairs_hook=unique)


def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def digest(v):return hashlib.sha256(v).hexdigest()


@lru_cache(maxsize=4)
def prepare_custom_schema(directory):
 # Release-only workflows also need ESO's pinned CRD schema, without installing ESO.
 secrets=load('secrets');archive=Path(directory).parent/'external-secrets.tgz'
 lock=parse_json((ROOT/'deploy/v3/secret-operator/upstream.lock.json').read_text())
 require(digest(archive.read_bytes())==lock['sha256'],'Pinned ESO schema archive required')
 docs=secrets.load_documents(secrets.render(archive,'external-secrets',secrets.OPERATOR/'values.yaml'))
 secrets.validate_operator(docs);secrets.write_custom_schemas(docs)


def prepared(data):
 require(set(data)=={'schemaVersion','target','state','components','values','serviceReview'},'Exact V3 candidate fields required')
 require(data['schemaVersion']==2 and data['target']=='yeodam-v3-foundation' and data['state']=='prepared','Complete V3 candidate required')
 require(set(data['components'])==set(COMPONENTS) and set(data['values'])=={'frontend','backend','websocket','ai','service-access'},'All four images and five config inputs required')
 for name,c in data['components'].items():
  repo,image,_=COMPONENTS[name]
  require(set(c)=={'sourceRepository','sourceSha','image','buildRunId','buildSourceSha'},'Exact publishing evidence fields')
  require(c['sourceRepository']==repo and re.fullmatch('[a-f0-9]{40}',c['sourceSha']) and re.fullmatch('[a-f0-9]{40}',c['buildSourceSha']),'Exact source/publisher commit required')
  require(type(c['buildRunId']) is int and c['buildRunId']>0 and re.fullmatch('ghcr.io/100-hours-a-week/'+image+'@sha256:[a-f0-9]{64}',c['image']),'Successful publisher Run and immutable image required')
  require(name=='websocket' or c['buildSourceSha']==c['sourceSha'],'App publisher must build that main source')
 for ca in data['values']['ai']['trust'].values():
  require('BEGIN CERTIFICATE' in ca and 'PRIVATE KEY' not in ca,'Public CA only; no private keys in delivery inputs')
  ssl.create_default_context(cadata=ca)
 require(data['values']['service-access']==service_values(data['serviceReview']),'Service aliases must match reviewed public Service identities')
 # Revalidate every identity field rather than trusting a serialized review flag.
 from v3_service_access import review
 items=[]
 for key,s in data['serviceReview']['services'].items():
  items.append({'apiVersion':'v1','kind':'Service','metadata':{'namespace':s['namespace'],'name':s['name'],'uid':s['uid']},
                'spec':{'type':'ClusterIP','clusterIP':s['address'],'selector':s['selector'],'ports':[{'port':s['port'],'targetPort':'http','protocol':'TCP'}]}})
 require(review({'kind':'List','items':items})==data['serviceReview'],'Reviewed Service metadata mismatch')
 fe=data['values']['frontend'];be=data['values']['backend'];ws=data['values']['websocket']
 f=apps.merge(fe['defaults'],fe['workloads']['frontend'])['configMapData']
 domain=f['SERVER_API_BASE_URL'].removesuffix('/api')
 require(domain in ['https://v3.yeodam-2gether.com','https://yeodam-2gether.com'],'Explicit QA or operating domain only')
 for role in PROFILE_ROLES['backend']:
  b=apps.merge(be['defaults'],be['workloads'][role])['configMapData']
  require(b['FRONTEND_ORIGIN']==domain and b['FRONTEND_OAUTH_CALLBACK_URI']==domain+'/auth/callback'and b['KAKAO_REDIRECT_URI']==domain+'/api/auth/kakao/callback','Coherent FE/BE OAuth domain required')
 require(apps.merge(ws['defaults'],ws['workloads']['trip-websocket'])['configMapData']['WEBSOCKET_ALLOWED_ORIGINS']==domain,'WebSocket origin must match release domain')
 return data


def normalize(data):
 data=deepcopy(data);data['state']='prepared';prepared(data)
 for profile,roles in PROFILE_ROLES.items():
  v=data['values'][profile];c=data['components'][profile]
  require(set(k for k,x in v['workloads'].items()if x.get('enabled'))==set(roles),'Exact live roles required')
  v['workloads']={k:x for k,x in v['workloads'].items()if k in roles}
  v['images']={profile:c['image']};v['defaults']['configMapData']['RELEASE']=c['sourceSha']
  for w in v['workloads'].values():
   if 'configMapData'in w:w['configMapData']['RELEASE']=c['sourceSha']
  require(re.fullmatch('[a-f0-9]{64}',v['defaults']['configurationRevision']),'Preserve reviewed config/Secret revision')
 a=data['values']['ai'];c=data['components']['ai-app'];a.update(image=c['image'],source=c['sourceSha'])
 require(a['workloads']=={'api':True,'runner':True},'Both AI roles required')
 require(re.fullmatch('[a-f0-9]{64}',a['configurationRevision']),'Preserve reviewed AI trust/Secret revision')
 return data


def render_profiles(files, data):
 documents=[]
 with tempfile.TemporaryDirectory(prefix='v3-full-release-')as directory:
  root=Path(directory)
  for name,content in files.items():
   if not name.startswith('charts/'):continue
   path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
  for profile in ['frontend','backend','websocket','ai','service-access']:
   v=data['values'][profile];chart='apps'if profile in PROFILE_ROLES else 'ai-apps'if profile=='ai'else profile
   release={'frontend':'yeodam-v3-frontend','backend':'yeodam-v3-apps','websocket':'yeodam-v3-websocket','ai':'yeodam-v3-ai-apps','service-access':'yeodam-v3-service-access'}[profile]
   text=subprocess.check_output([os.environ['V3_HELM'],'template',release,str(root/'charts'/chart),'--namespace','default','--kube-version','1.36.0','-f','-'],input=yaml.safe_dump(v),text=True,stderr=subprocess.PIPE)
   docs=apps.platform.load_documents(text)
   if profile in PROFILE_ROLES:
    merged=apps.merge(yaml.safe_load(files['charts/apps/values.yaml']),v);apps.validate(docs,merged)
    require({d['metadata']['name']for d in docs if d['kind']=='Deployment'}==set(PROFILE_ROLES[profile]),'No omitted or extra app role')
    c=data['components'][profile]
    for d in docs:
     if d['kind']=='Deployment':require(d['spec']['template']['spec']['containers'][0]['image']==c['image'],'Role image differs from approved component')
     if d['kind']=='ConfigMap':require(d['data']['RELEASE']==c['sourceSha'],'Source annotation differs from image source')
   elif profile=='ai':
    ai.validate(docs,v);require(v['image']==data['components']['ai-app']['image']and v['source']==data['components']['ai-app']['sourceSha'],'AI image/source mismatch')
   else:access.validate(docs,v)
   documents+=docs
 identities=[(d['kind'],d['metadata'].get('namespace'),d['metadata']['name'])for d in documents]
 require(len(identities)==len(set(identities)),'Shared ownership conflict')
 require({d['metadata']['name']for d in documents if d['kind']=='Deployment'}==set(UNITS),'Exactly nine app Deployments')
 for d in documents:
  require(d['kind']in ['Deployment','Service','ConfigMap','ServiceAccount','ExternalSecret','NetworkPolicy'],'No Secret, data, bootstrap or cluster resource ownership')
  resource=next(k for k,v in KINDS.items()if v==d['kind'])
  require(d['metadata']['name']in RESOURCES.get(d['metadata'].get('namespace'),{}).get(resource,[]),'Resource outside reviewed Argo role inventory')
  wave=WAVES[d['metadata']['name']]if d['kind']=='Deployment'else -20
  d['metadata'].setdefault('annotations',{})['argocd.argoproj.io/sync-wave']=str(wave)
 prepare_custom_schema(os.environ['V3_SCHEMA_DIR'])
 apps.platform.schema_check(yaml.safe_dump_all(documents))
 return documents


def build(data, charts, evidence):
 prepared(data)
 files=dict(charts)
 require(all(n.startswith(tuple('charts/'+c+'/'for c in CHARTS))for n in files),'Only approved Chart snapshots')
 for name,v in data['values'].items():files['values/'+name+'.json']=canonical(v)+b'\n'
 files['candidate.json']=canonical(data)+b'\n'
 docs=render_profiles(files,data)
 files['manifests/release.yaml']=yaml.safe_dump_all(docs,sort_keys=True).encode()
 meta={'schemaVersion':2,'target':'yeodam-v3-foundation','units':UNITS,'evidence':evidence,'files':{n:digest(v)for n,v in sorted(files.items())}}
 meta['releaseId']=digest(canonical(meta));files['release.json']=canonical(meta)+b'\n'
 return files


def validate(files):
 require('release.json'in files and 'candidate.json'in files and 'manifests/release.yaml'in files,'Full release snapshot required')
 require(len(files)<150 and sum(map(len,files.values()))<4*1024*1024,'Bundle limits')
 for name in files:
  p=PurePosixPath(name);require(not p.is_absolute()and '..'not in p.parts and str(p)==name,'Unsafe bundle path')
  require(name in ['release.json','candidate.json','manifests/release.yaml']or name in ['values/'+n+'.json'for n in ['frontend','backend','websocket','ai','service-access']]or name.startswith(tuple('charts/'+c+'/'for c in CHARTS)),'Unapproved path')
 meta=parse_json(files['release.json']);require(set(meta)=={'schemaVersion','target','units','evidence','files','releaseId'},'Exact release metadata')
 require(meta['schemaVersion']==2 and meta['target']=='yeodam-v3-foundation'and meta['units']==UNITS,'Wrong destination or partial bundle')
 require(type(meta['evidence'].get('candidatePr'))is int and meta['evidence']['candidatePr']>=0 and type(meta['evidence'].get('cloudGateRunId'))is int and meta['evidence']['cloudGateRunId']>=0 and re.fullmatch('[a-f0-9]{40}',meta['evidence'].get('cloudCommit','')),'Public candidate evidence only')
 expected=dict(meta);expected.pop('releaseId');require(meta['releaseId']==digest(canonical(expected)),'Release identity mismatch')
 require(meta['files']=={n:digest(v)for n,v in sorted(files.items())if n!='release.json'},'Bundle checksum mismatch')
 data=prepared(parse_json(files['candidate.json']))
 require(set(meta['evidence'])=={'candidatePr','cloudCommit','cloudGateRunId'},'Exact candidate evidence metadata')
 for profile,v in data['values'].items():require(parse_json(files['values/'+profile+'.json'])==v,'Value input mismatch')
 expected_docs=render_profiles(files,data);actual=apps.platform.load_documents(files['manifests/release.yaml'].decode())
 require(actual==expected_docs,'Published manifests differ from re-rendered approved Charts/config')
 return meta
