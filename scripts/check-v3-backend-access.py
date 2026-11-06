#!/usr/bin/env python3
"""Verify public trust, exact scoped policy and existing ESO references offline."""
import argparse,importlib.util,json,os,subprocess
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('platform',ROOT/'scripts/check-v3-platform.py');platform=importlib.util.module_from_spec(s);s.loader.exec_module(platform)
s=importlib.util.spec_from_file_location('supply',ROOT/'scripts/prepare-v3-backend-supply.py');supply=importlib.util.module_from_spec(s);s.loader.exec_module(supply)
require=platform.require

def render(files=()):
 args=[os.environ['V3_HELM'],'template','yeodam-v3-backend-access',str(ROOT/'deploy/v3/backend-access'),'--kube-version','1.36.0']
 for f in files:args+=['-f',str(f)]
 return subprocess.check_output(args,text=True)

def expected_policies():
 back={'matchLabels':{'app.kubernetes.io/name':'backend','app.kubernetes.io/part-of':'yeodam','yeodam.io/stage':'v3'}}
 peers={'mysql':({'matchLabels':{'app.kubernetes.io/name':'mysql','app.kubernetes.io/instance':'yeodam-v3-mysql'}},[3306]),'redis':({'matchLabels':{'app.kubernetes.io/name':'redis','app.kubernetes.io/instance':'yeodam-v3-redis'},'matchExpressions':[{'key':'yeodam.io/redis-process','operator':'In','values':['redis','sentinel']}]},[6379,26379]),'mongodb':({'matchLabels':{'app.kubernetes.io/name':'mongodb','app.kubernetes.io/instance':'yeodam-v3-mongodb'}},[27017]),'kafka':({'matchLabels':{'strimzi.io/cluster':'yeodam-v3-kafka','strimzi.io/kind':'Kafka'}},[9093])}
 result={}
 for ns in ['auth','trip','photo']:
  e=[{'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':dst}},'podSelector':sel}],'ports':[{'protocol':'TCP','port':p} for p in ports]} for dst,(sel,ports) in peers.items()]
  e += [{'to':[{'ipBlock':{'cidr':'169.254.170.23/32'}}],'ports':[{'protocol':'TCP','port':80}]},{'to':[{'ipBlock':{'cidr':'0.0.0.0/0'}}],'ports':[{'protocol':'TCP','port':443}]}]
  result[(ns,'allow-backend-dependencies')]={'podSelector':back,'policyTypes':['Egress'],'egress':e}
 for ns,(sel,ports) in peers.items():result[(ns,'allow-from-v3-backend')]={'podSelector':sel,'policyTypes':['Ingress'],'ingress':[{'from':[{'namespaceSelector':{'matchExpressions':[{'key':'kubernetes.io/metadata.name','operator':'In','values':['auth','trip','photo']}]},'podSelector':back}],'ports':[{'protocol':'TCP','port':p} for p in ports]}]}
 return result

def validate(docs,enabled=False):
 if not enabled:require(not docs,'Disabled must render zero resources');return
 require(len(docs)==10,'Only public trust3 and policies7 are allowed')
 policies={(d['metadata']['namespace'],d['metadata']['name']):d['spec'] for d in docs if d['kind']=='NetworkPolicy'}
 require(policies==expected_policies(),'Backend/storage peer or port boundary changed')
 trust=[d for d in docs if d['kind']=='Secret'];require({d['metadata']['namespace'] for d in trust}=={'auth','trip','photo'} and len(trust)==3,'One public trust copy per app namespace')
 for d in trust:
  require(d['metadata']['name']=='backend-trust' and d['type']=='Opaque' and not d.get('data') and set(d['stringData'])=={x+'.crt' for x in ['mysql','redis','mongodb','kafka']},'Public CA entries only')
  for pem in d['stringData'].values():supply.public_ca(pem)
 require(all(d['metadata']['labels'].get('yeodam.io/stage')=='v3' for d in docs),'V3 scope required')

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('-f',action='append',default=[]);p.add_argument('--output',type=Path);a=p.parse_args()
 source=render(a.f);docs=platform.load_documents(source);validate(docs,bool(docs));platform.schema_check(source)
 if a.output:a.output.write_text(source)
 print('Backend access contracts passed; no AWS, secret source or cluster accessed')
if __name__=='__main__':main()
