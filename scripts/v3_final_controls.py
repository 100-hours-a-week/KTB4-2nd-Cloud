#!/usr/bin/env python3
"""Finite #726 operational plans. Original UID/config and expiry bind every mutation."""
import argparse,copy,hashlib,json,os,subprocess,time
from pathlib import Path
ACCOUNT='483175530259';REGION='ap-northeast-2';CLUSTER_UID='46b80c44-56f4-416d-9f9b-e8b7b4cdc6a9'
WAF_ID='4a976dfe-3301-413f-bff4-9fecb911a975'
def sha(v):return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def run(args):
 p=subprocess.run(args,capture_output=True,timeout=35)
 if p.returncode:raise ValueError('Operational command failed; raw output withheld')
 return json.loads(p.stdout) if p.stdout.strip() else {}
def aws(*args):return run(['/opt/homebrew/bin/aws','--profile','yeodam-admin','--region',REGION,*args,'--output','json'])
def kube(k,*args):return run(['kubectl','--kubeconfig',k,'--request-timeout=20s',*args,'-o','json'])
def save(p,v):
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w')as f:json.dump(v,f,indent=2)
def verify(k):
 if aws('sts','get-caller-identity')['Account']!=ACCOUNT:raise ValueError('Wrong AWS account')
 if kube(k,'get','namespace','kube-system')['metadata']['uid']!=CLUSTER_UID:raise ValueError('Wrong cluster')
def profile_plan(k,kind):
 if kind not in ('api','converter'):raise ValueError('One measured workload only')
 verify(k);ns,name,resource=('trip','trip-api','hpa')if kind=='api'else('photo','photo-converter','scaledobject')
 live=kube(k,'-n',ns,'get',resource,name);before={'spec':live['spec'],'annotations':live['metadata'].get('annotations',{})};after=copy.deepcopy(before)
 if kind=='api':
  if (before['spec']['minReplicas'],before['spec']['maxReplicas'])!=(1,2):raise ValueError('Reviewed API range must be 1–2')
  after['spec']['maxReplicas']=1
 else:
  if (before['spec']['minReplicaCount'],before['spec']['maxReplicaCount'])!=(1,2)or' autoscaling.keda.sh/paused-replicas'.strip()in before['annotations']:raise ValueError('Converter must be unpaused at 1–2')
  after['annotations']['autoscaling.keda.sh/paused-replicas']='1'
 return {'kind':kind,'resource':resource,'namespace':ns,'name':name,'uid':live['metadata']['uid'],'before':before,'after':after,'createdAt':time.time(),'expiresAt':time.time()+600,'experimentMaxSeconds':1500}
def profile_apply(k,p,restore=False):
 verify(k);live=kube(k,'-n',p['namespace'],'get',p['resource'],p['name'])
 if live['metadata']['uid']!=p['uid']:raise ValueError('Target UID changed')
 current={'spec':live['spec'],'annotations':live['metadata'].get('annotations',{})};expected=p['after']if restore else p['before'];wanted=p['before']if restore else p['after']
 if sha(current)!=sha(expected):raise ValueError('Concurrent policy change; inspect before applying/restoring')
 if not restore and time.time()>p['expiresAt']:raise ValueError('Plan expired')
 patch=[{'op':'test','path':'/metadata/uid','value':p['uid']},{'op':'test','path':'/metadata/resourceVersion','value':live['metadata']['resourceVersion']},{'op':'replace','path':'/spec','value':wanted['spec']},{'op':'replace','path':'/metadata/annotations','value':wanted['annotations']}]
 kube(k,'-n',p['namespace'],'patch',p['resource'],p['name'],'--type=json','-p',json.dumps(patch))
 return {'restored':restore,'kind':p['kind'],'at':time.time()}
def waf_target(original,block):
 w=copy.deepcopy(original);rules=w['Rules']
 if w['Name']!='yeodam-v3-validation'or w['Id']!=WAF_ID or {x['Name']for x in rules}!={'block-internal','observe-auth','observe-api'}:raise ValueError('Unexpected regional ACL/rule inventory')
 for rule in rules:
  name=rule['Name']
  if name=='block-internal':rule['Action']={'Block':{'CustomResponse':{'ResponseCode':403,'ResponseHeaders':[{'Name':'X-Yeodam-Edge','Value':'waf-internal'}]}}}
  else:
   s=rule['Statement']['RateBasedStatement'];key=name.split('-')[1]
   if s['AggregateKeyType']!='IP'or s['EvaluationWindowSec']!=300 or s['Limit']!={'auth':500,'api':5000}[key]:raise ValueError('Do not alter reviewed IP thresholds or window')
   rule['Action']={'Block':{'CustomResponse':{'ResponseCode':429,'ResponseHeaders':[{'Name':'X-Yeodam-Edge','Value':'waf-rate-'+key}]}}}if block else{'Count':{}}
 return w
def waf_apply(p):
 if aws('sts','get-caller-identity')['Account']!=ACCOUNT:raise ValueError('Wrong account')
 if time.time()>p['expiresAt']:raise ValueError('WAF plan expired')
 live=aws('wafv2','get-web-acl','--scope','REGIONAL','--name','yeodam-v3-validation','--id',WAF_ID)
 if sha(live['WebACL'])!=p['beforeSha256']:raise ValueError('WAF changed since plan')
 target=waf_target(live['WebACL'],p['block'])
 keys=['Name','Id','DefaultAction','Rules','VisibilityConfig','Description','CustomResponseBodies','CaptchaConfig','ChallengeConfig','TokenDomains','AssociationConfig','OnSourceDDoSProtectionConfig']
 request={k:v for k,v in target.items()if k in keys and not(k=='Description'and not v)};request.update(Scope='REGIONAL',LockToken=live['LockToken'])
 # Payload has policy only; no request samples, headers, cookies or credentials.
 path=Path('/tmp/yeodam-726-waf-policy-'+sha(request)[:12]+'.json');save(path,request)
 try:aws('wafv2','update-web-acl','--cli-input-json','file://'+str(path))
 finally:path.unlink()
 return {'block':p['block'],'at':time.time(),'rules':len(target['Rules']),'thresholdsPreserved':True}
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['plan-api','plan-converter','apply','restore','plan-waf-count','plan-waf-block','apply-waf']);p.add_argument('--kubeconfig');p.add_argument('--plan',type=Path,required=True);a=p.parse_args()
 if a.action.startswith('plan-waf'):
  original=aws('wafv2','get-web-acl','--scope','REGIONAL','--name','yeodam-v3-validation','--id',WAF_ID)['WebACL'];block=a.action.endswith('block');target=waf_target(original,block);result={'beforeSha256':sha(original),'before':original,'after':target,'block':block,'expiresAt':time.time()+600};save(a.plan,result)
 elif a.action.startswith('plan-'):result=profile_plan(a.kubeconfig,a.action.split('-')[1]);save(a.plan,result)
 elif a.action=='apply-waf':result=waf_apply(json.loads(a.plan.read_text()))
 else:result=profile_apply(a.kubeconfig,json.loads(a.plan.read_text()),a.action=='restore')
 print(json.dumps({'action':a.action,'planSha256':sha(result),'changed':not a.action.startswith('plan-')}))
if __name__=='__main__':main()
