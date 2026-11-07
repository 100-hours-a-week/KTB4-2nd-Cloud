#!/usr/bin/env python3
"""Prepare public FE/Ingress/Origin references only. No AWS, credentials, DNS writes or deployment."""
import argparse,json,os,re
from pathlib import Path
HOST='v3.yeodam-2gether.com'
def require(ok,message):
    if not ok:raise ValueError(message)
def prepare(outputs,image,source,revision):
    require(set(outputs)=={'host','alb_security_group_id','certificate_arn'},'Only reviewed public Terraform outputs accepted')
    values={k:v['value'] for k,v in outputs.items()}
    require(values['host']==HOST and re.fullmatch('sg-[a-f0-9]{17}',values['alb_security_group_id']),'Dedicated V3 host/SG required')
    require(re.fullmatch(r'arn:aws:acm:ap-northeast-2:483175530259:certificate/[a-f0-9-]{36}',values['certificate_arn']),'Regional V3 certificate required; verify issued SAN before apply')
    require(re.fullmatch(r'ghcr.io/100-hours-a-week/yeodam-frontend@sha256:[a-f0-9]{64}',image),'Immutable separate FE image required')
    require(re.fullmatch('[a-f0-9]{40}',source) and re.fullmatch('[a-f0-9]{64}',revision),'Explicit source and public metadata revision required')
    common={'images':{'frontend':image},'defaults':{'configurationRevision':revision,'configMapData':{'RELEASE':source}},'workloads':{'frontend':{'enabled':True}}}
    return {
      'frontend-service.values.json':dict(common,frontendServiceOnly=True),
      'frontend-app.values.json':dict(common,frontendServiceOnly=False),
      'controller-policy.values.json':{'controllerPolicyEnabled':True},
      'ingress.values.json':{'controllerPolicyEnabled':True,'ingressEnabled':True,'backendRoutesEnabled':False,'certificateArn':values['certificate_arn'],'albSecurityGroupId':values['alb_security_group_id']},
      'ingress-full.values.json':{'controllerPolicyEnabled':True,'ingressEnabled':True,'backendRoutesEnabled':True,'certificateArn':values['certificate_arn'],'albSecurityGroupId':values['alb_security_group_id']},
      'backend-origin.values.json':{'defaults':{'configurationRevision':revision,'configMapData':{'FRONTEND_ORIGIN':'https://'+HOST,'FRONTEND_OAUTH_CALLBACK_URI':'https://'+HOST+'/auth/callback','KAKAO_REDIRECT_URI':'https://'+HOST+'/api/auth/kakao/callback'}}},
      'websocket-origin.values.json':{'defaults':{'configurationRevision':revision,'configMapData':{'WEBSOCKET_ALLOWED_ORIGINS':'https://'+HOST}}}
    }
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--infrastructure-outputs',required=True,type=Path);p.add_argument('--image',required=True);p.add_argument('--source',required=True);p.add_argument('--configuration-revision',required=True);p.add_argument('--directory',required=True,type=Path)
    a=p.parse_args();os.umask(0o077);require(not a.directory.exists(),'Refusing to replace existing release inputs')
    values=prepare(json.loads(a.infrastructure_outputs.read_text()),a.image,a.source,a.configuration_revision)
    a.directory.mkdir(parents=True)
    for name,value in values.items():
        with (a.directory/name).open('x') as f:json.dump(value,f,indent=2)
    print('Prepared public inputs. Verify certificate SAN, image, OAuth registration and existing release inputs before applying. No DNS or deployment performed.')
if __name__=='__main__':main()
