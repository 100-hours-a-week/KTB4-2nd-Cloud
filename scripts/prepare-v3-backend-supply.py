#!/usr/bin/env python3
"""Explicit stdin-only V3 app supply. No production reads, rotation, value output or Terraform payloads."""
import argparse,json,os,re,subprocess,tempfile
from pathlib import Path
from urllib.parse import quote
ROLES={'auth-api':'auth','trip-api':'trip','photo-api':'photo','photo-converter':'photo','trip-result-worker':'trip'}
NAMESPACES=('auth','trip','photo')
EXTERNAL={'JWT_SECRET','KAKAO_CLIENT_ID','KAKAO_CLIENT_SECRET','AI_SERVER_API_KEY','WEBSOCKET_INTERNAL_SERVICE_TOKEN'}
def require(ok,message='Invalid V3 supply input; contents withheld'):
 if not ok:raise ValueError(message)
def public_ca(pem):
 require(isinstance(pem,str) and 'PRIVATE KEY' not in pem and pem.count('-----BEGIN CERTIFICATE-----')==pem.count('-----END CERTIFICATE-----')==1)
 r=subprocess.run(['openssl','x509','-noout','-checkend','86400','-ext','basicConstraints'],input=pem,text=True,capture_output=True,timeout=10)
 require(r.returncode==0 and 'CA:TRUE' in r.stdout,'Valid public CA certificate required')
 return pem

def prepare(raw,namespaces=NAMESPACES):
 require(bool(namespaces) and len(set(namespaces))==len(namespaces) and set(namespaces)<=set(NAMESPACES),
         'Select unique V3 namespaces explicitly')
 require(set(raw)=={'mysql','redis','mongodb','kafkaCa','kafkaPasswords','external'})
 require(set(raw['external'])==EXTERNAL and all(isinstance(v,str) and v.strip() for v in raw['external'].values()))
 selected_roles={role for role,ns in ROLES.items() if ns in namespaces}
 require(len(raw['external']['JWT_SECRET'])>=32 and selected_roles<=set(raw['kafkaPasswords'])<=set(ROLES))
 for source in ['mysql','redis','mongodb']:
  require(re.fullmatch('[a-f0-9]{48}',raw[source]['appPassword']))
 require(re.fullmatch('[a-f0-9]{48}',raw['redis']['sentinelClientPassword']))
 require(all(re.fullmatch(r'[A-Za-z0-9_@%+=:.,/-]{12,128}',v) for v in raw['kafkaPasswords'].values()),'Kafka password must be safe for quoted JAAS substitution')
 certificates={k:public_ca(raw[k]['tlsCa']) for k in ['mysql','redis','mongodb']};certificates['kafka']=public_ca(raw['kafkaCa'])
 common={**raw['external'],'MYSQL_USERNAME':'yeodam','MYSQL_PASSWORD':raw['mysql']['appPassword'],'REDIS_PASSWORD':raw['redis']['appPassword'],'REDIS_SENTINEL_PASSWORD':raw['redis']['sentinelClientPassword'],
  'MONGODB_URI':'mongodb://app:'+quote(raw['mongodb']['appPassword'],safe='')+'@mongodb-0.mongodb-members.mongodb.svc.cluster.local:27017/yeodam?authSource=yeodam&tls=true&directConnection=true&retryWrites=false&maxPoolSize=3&minPoolSize=0&maxConnecting=1'}
 sources={ns:{**common,**{'KAFKA_'+role.replace('-','_').upper()+'_PASSWORD':raw['kafkaPasswords'][role] for role,rns in ROLES.items() if rns==ns}} for ns in namespaces}
 return sources,{'enabled':True,'certificates':certificates}

def create(sources,profile):
 require(bool(sources) and set(sources)<=set(NAMESPACES),'Only selected V3 namespace sources may be created')
 aws=['aws','--profile',profile,'--region','ap-northeast-2']
 def call(args):return subprocess.run(aws+args,text=True,capture_output=True,timeout=60)
 r=call(['sts','get-caller-identity','--output','json']);require(r.returncode==0 and json.loads(r.stdout)['Account']=='483175530259','Wrong AWS identity or expired session')
 for ns in sources:
  r=call(['secretsmanager','describe-secret','--secret-id','yeodam/v3/foundation/'+ns+'/backend'])
  require(r.returncode!=0 and 'ResourceNotFoundException' in r.stderr,'Source exists or lookup failed; refusing replacement/rotation')
 with tempfile.TemporaryDirectory(prefix='v3-be-supply-') as d:
  for ns,value in sources.items():
   p=Path(d)/'payload.json';p.write_text(json.dumps(value));p.chmod(0o600)
   r=call(['secretsmanager','create-secret','--name','yeodam/v3/foundation/'+ns+'/backend','--secret-string','file://'+str(p),'--tags','Key=Project,Value=yeodam','Key=Version,Value=v3','Key=Issue,Value=548'])
   require(r.returncode==0,'Creation failed; inspect source metadata before retrying. Output withheld')
 print('Created '+str(len(sources))+' new V3 namespace sources; no values printed. Existing sources untouched.')

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--public-output',required=True,type=Path);p.add_argument('--create-sources',action='store_true');p.add_argument('--profile',default='yeodam-admin');p.add_argument('--namespaces',nargs='+',choices=NAMESPACES,default=list(NAMESPACES));a=p.parse_args();os.umask(0o077)
 require(not a.public_output.exists(),'Public overlay already exists; refusing overwrite')
 import sys
 sources,public=prepare(json.load(sys.stdin),a.namespaces)
 if a.create_sources:create(sources,a.profile)
 a.public_output.parent.mkdir(parents=True,exist_ok=True)
 with a.public_output.open('x') as f:json.dump(public,f,indent=2)
 print('Supply validated; public CA overlay written. Secret values are not exported.')
if __name__=='__main__':main()
