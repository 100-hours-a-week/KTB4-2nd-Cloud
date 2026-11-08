"""Synthetic full-app candidate; no credentials or live endpoints."""
import copy,importlib.util,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT/'scripts'))
import v3_release_bundle as b
from v3_delivery_contract import COMPONENTS


def candidate(cloud='e'*40):
 values={}
 sources={'frontend':'1'*40,'backend':'2'*40,'websocket':'3'*40,'ai-app':'4'*40}
 components={k:{'sourceRepository':r,'sourceSha':sources[k],'image':'ghcr.io/100-hours-a-week/'+image+'@sha256:'+str(n)*64,
                'buildRunId':100+n,'buildSourceSha':cloud if k=='websocket'else sources[k]}
             for n,(k,(r,image,w))in enumerate(COMPONENTS.items(),1)}
 for name,profile in [('frontend','frontend.values.yaml'),('backend','backend.values.yaml'),('websocket','websocket.values.yaml')]:
  v=b.apps.load_values([ROOT/'deploy/v3/environments/foundation'/profile])
  if name=='backend':v=b.apps.load_values([ROOT/'deploy/v3/environments/foundation'/profile,ROOT/'tests/ci/v3/backend-values.fixture.yaml',ROOT/'deploy/v3/environments/foundation/backend-connections.values.yaml'])
  if name=='backend':v['defaults']['configMapData'].update(FRONTEND_ORIGIN='https://v3.yeodam-2gether.com',FRONTEND_OAUTH_CALLBACK_URI='https://v3.yeodam-2gether.com/auth/callback',KAKAO_REDIRECT_URI='https://v3.yeodam-2gether.com/api/auth/kakao/callback')
  c=components[name];v['images'][name]=c['image'];v['defaults']['configurationRevision']='a'*64
  v['defaults']['configMapData']['RELEASE']=c['sourceSha']
  if name=='websocket':v['defaults']['configMapData']['WEBSOCKET_ALLOWED_ORIGINS']='https://v3.yeodam-2gether.com'
  for role in b.PROFILE_ROLES[name]:v['workloads'][role]['enabled']=True
  values[name]=v
 values['ai']=b.ai.fixture();values['ai'].update(image=components['ai-app']['image'],source=sources['ai-app'])
 from v3_service_access import SERVICES,review,values as alias_values
 services={'kind':'List','items':[]}
 for n,(key,s)in enumerate(SERVICES.items(),10):
  services['items'].append({'apiVersion':'v1','kind':'Service','metadata':{'name':s['name'],'namespace':s['namespace'],'uid':'00000000-0000-4000-8000-'+str(n).zfill(12)},
   'spec':{'type':'ClusterIP','clusterIP':'172.20.200.'+str(n),'selector':s['selector'],'ports':[{'port':s['port'],'targetPort':'http'}]}})
 evidence=review(services);values['service-access']=alias_values(evidence)
 return b.normalize({'schemaVersion':2,'target':'yeodam-v3-foundation','state':'prepared','components':components,'values':values,'serviceReview':evidence})
