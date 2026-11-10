#!/usr/bin/env python3
"""Receipt-gated DNS activation and post-write acceptance. Never deletes V2 resources."""
import argparse,hashlib,importlib.util,json,os,subprocess,time
from pathlib import Path
import v2_v3_cutover as c
s=importlib.util.spec_from_file_location('runtime602',c.ROOT/'scripts/run-v2-v3-cutover.py');runtime=importlib.util.module_from_spec(s);s.loader.exec_module(runtime)

def live_delivery(k):
 app=k.get('argocd','application','yeodam-v3-apps');status=app.get('status',{})
 c.require(app['spec']['source']['targetRevision']=='deploy/v3-approved' and status.get('sync',{}).get('status')=='Synced' and status.get('health',{}).get('status')=='Healthy','Approved app release must be Synced/Healthy')
 origin='https://'+c.POLICY['domain']
 candidate=json.loads((c.ROOT/'deploy/v3/releases/candidate.json').read_text())
 for name,unit in c.UNITS.items():
  ns=unit['namespace'];d=k.get(ns,'deployment',name)
  c.require(d['spec']['replicas']==1 and d.get('status',{}).get('readyReplicas')==1 and d['status'].get('observedGeneration')==d['metadata']['generation'],'Every minimum app replica must be freshly Ready')
  image=d['spec']['template']['spec']['containers'][0]['image'];c.require(image==candidate['components'][unit['component']]['image'],'Use exactly the QA-reviewed image digest')
  maps=[r['configMapRef']['name'] for r in d['spec']['template']['spec']['containers'][0].get('envFrom',[]) if 'configMapRef' in r]
  data={key:value for m in maps for key,value in k.get(ns,'configmap',m).get('data',{}).items()}
  if name=='frontend':c.require(data.get('SERVER_API_BASE_URL')==origin+'/api','FE must use operating origin')
  if unit['component']=='backend':c.require(data.get('KAKAO_REDIRECT_URI')==origin+'/api/auth/kakao/callback' and data.get('FRONTEND_ORIGIN')==origin,'BE OAuth must use operating origin')
  if name=='trip-websocket':c.require(data.get('WEBSOCKET_ALLOWED_ORIGINS')==origin,'WS must use operating origin')
 connector=k.get('kafka','kafkaconnector','yeodam-v3-outbox');s=connector.get('status',{})
 c.require(connector['spec']['config']['topic.prefix']==c.POLICY['cdcPrefix'] and s.get('observedGeneration')==connector['metadata']['generation'],'Fresh operating CDC generation required')
 tasks=s.get('connectorStatus',{}).get('tasks',[])
 c.require(len(tasks)==1 and tasks[0].get('state')=='RUNNING','Operating CDC Task must be running')
 ids=k.sql("SELECT s.story_id FROM yeodam.stories s JOIN yeodam.trips t ON t.current_story_id=s.story_id AND s.trip_id=t.trip_id WHERE t.deleted_at IS NULL AND s.deleted_at IS NULL AND s.processing_status='COMPLETED' ORDER BY s.story_id;").splitlines()
 script=(c.ROOT/'scripts/cutover/mongo.js').read_text()
 result=runtime.mongo_action(k,'verify-stories',c.canonical(ids))
 stories=json.loads(result);c.require(stories['missing']==0 and stories['expected']==len(ids),'Existing story content migration incomplete')
 return {'approvedRevision':status['sync']['revision'],'readyApps':9,'cdcRunning':True,'existingStoriesVerified':stories['found']}

def aws(*args):
 r=subprocess.run(['aws','--profile','yeodam-admin','--region','ap-northeast-2',*args],capture_output=True,text=True,timeout=60)
 c.require(r.returncode==0,'AWS read failed; private output suppressed');return json.loads(r.stdout)
def public_preflight(k):
 c.require(aws('sts','get-caller-identity')['Account']=='483175530259','Wrong account')
 lbs=aws('elbv2','describe-load-balancers','--names','yeodam-v3-validation')['LoadBalancers'];c.require(len(lbs)==1 and lbs[0]['VpcId']=='vpc-0ecc3132f3b2170e9','Existing V3 ALB only')
 groups=aws('elbv2','describe-target-groups','--load-balancer-arn',lbs[0]['LoadBalancerArn'])['TargetGroups']
 c.require(len(groups)>=5,'Missing app target groups')
 for group in groups:
  health=aws('elbv2','describe-target-health','--target-group-arn',group['TargetGroupArn'])['TargetHealthDescriptions']
  c.require(health and all(x['TargetHealth']['State']=='healthy' for x in health),'All existing app targets must be Healthy')
 for ns in ['frontend','auth','trip','photo']:
  for ingress in k.get(ns,'ingress')['items']:
   if ingress['metadata']['name'].startswith('v3-'):
    c.require(all(r['host']==c.POLICY['domain'] for r in ingress['spec']['rules']),'Operating ingress must be configured before DNS')
    c.require(ingress['metadata']['annotations'].get('alb.ingress.kubernetes.io/certificate-arn')==c.POLICY['certificateArn'],'Retain the reviewed issued operating certificate')
 cert=aws('acm','describe-certificate','--certificate-arn',c.POLICY['certificateArn'])['Certificate']
 c.require(cert['Status']=='ISSUED' and c.POLICY['domain'] in cert['SubjectAlternativeNames'],'Operating certificate not issued')
 cors=aws('s3api','get-bucket-cors','--bucket','yeodam-v3-foundation-app-data-483175530259-ap-northeast-2')['CORSRules']
 c.require(len(cors)==1 and set(cors[0]['AllowedOrigins'])=={'https://v3.yeodam-2gether.com','https://yeodam-2gether.com'},'Enable operating S3 browser origin before DNS')
 records=aws('route53','list-resource-record-sets','--hosted-zone-id',c.POLICY['zoneId'])['ResourceRecordSets']
 c.require(not any(r['Name']==c.POLICY['domain']+'.' and r['Type']=='AAAA' for r in records),'Existing operating AAAA needs explicit migration review')
 return {'targetGroupsHealthy':len(groups),'issuedCertificate':True,'operatingCors':True}

def dns(k,directory,terraform_plan,approved_tf_sha):
 delivery=live_delivery(k);health=public_preflight(k)
 c.require(terraform_plan.is_file() and not terraform_plan.is_symlink() and hashlib.sha256(terraform_plan.read_bytes()).hexdigest()==approved_tf_sha,'Exact reviewed binary Terraform Plan required')
 target=c.ROOT/'terraform/v3/cutover'
 raw=subprocess.run(['terraform','-chdir='+str(target),'show','-json',str(terraform_plan)],capture_output=True,text=True,timeout=60)
 c.require(raw.returncode==0,'Could not inspect the private saved Plan')
 data=json.loads(raw.stdout);check_dns_plan(data)
 r=subprocess.run(['terraform','-chdir='+str(target),'apply','-input=false','-auto-approve',str(terraform_plan)],capture_output=True,timeout=300,env={**os.environ,'AWS_PROFILE':'yeodam-admin','AWS_REGION':'ap-northeast-2'})
 c.require(r.returncode==0,'DNS apply failed; captured private output is suppressed')
 records=aws('route53','list-resource-record-sets','--hosted-zone-id',c.POLICY['zoneId'])['ResourceRecordSets']
 operating=[r for r in records if r['Name']==c.POLICY['domain']+'.' and r['Type']=='A']
 c.require(len(operating)==1 and operating[0]['AliasTarget']['DNSName'].removeprefix('dualstack.')==c.POLICY['newDns'],'DNS did not converge to V3')
 return {'verified':True,**delivery,**health,'operatingAliasV3':True,'automaticRollbackAllowed':False}

def check_cors_plan(plan):
 changes=[r for r in plan.get('resource_changes',[]) if r['mode']=='managed' and r['change']['actions']!=['no-op']]
 c.require(len(changes)==1,'Only one existing V3 CORS resource may change')
 r=changes[0];change=r['change'];c.require(r['address']=='aws_s3_bucket_cors_configuration.qa[0]' and change['actions']==['update'],'No other IAM/S3 changes during cutover')
 before,after=change['before'],change['after'];bucket='yeodam-v3-foundation-app-data-483175530259-ap-northeast-2'
 c.require(before['bucket']==after['bucket']==bucket and len(before['cors_rule'])==len(after['cors_rule'])==1,'Wrong bucket or rule count')
 old,new=before['cors_rule'][0],after['cors_rule'][0]
 c.require(set(old['allowed_origins'])=={'https://v3.yeodam-2gether.com'} and set(new['allowed_origins'])=={'https://v3.yeodam-2gether.com','https://yeodam-2gether.com'},'Only add the exact operating origin')
 c.require({k:v for k,v in old.items() if k!='allowed_origins'}=={k:v for k,v in new.items() if k!='allowed_origins'},'Do not widen methods/headers or change other CORS settings')
 return True

def operating_cors(directory,terraform_plan,approved_tf_sha):
 c.require(terraform_plan.is_file() and not terraform_plan.is_symlink() and hashlib.sha256(terraform_plan.read_bytes()).hexdigest()==approved_tf_sha,'Exact reviewed private CORS Plan required')
 target=c.ROOT/'terraform/v3/backend'
 result=subprocess.run(['terraform','-chdir='+str(target),'show','-json',str(terraform_plan)],capture_output=True,text=True,timeout=60)
 c.require(result.returncode==0,'Could not inspect the CORS Plan');check_cors_plan(json.loads(result.stdout))
 result=subprocess.run(['terraform','-chdir='+str(target),'apply','-input=false','-auto-approve',str(terraform_plan)],capture_output=True,timeout=300,env={**os.environ,'AWS_PROFILE':'yeodam-admin','AWS_REGION':'ap-northeast-2'})
 c.require(result.returncode==0,'CORS update failed; no private Plan/output printed')
 rules=aws('s3api','get-bucket-cors','--bucket','yeodam-v3-foundation-app-data-483175530259-ap-northeast-2')['CORSRules']
 c.require(len(rules)==1 and set(rules[0]['AllowedOrigins'])=={'https://v3.yeodam-2gether.com','https://yeodam-2gether.com'},'Operating CORS not applied')
 return {'verified':True,'onlyOperatingOriginAdded':True}

def check_dns_plan(plan):
 changes=[r for r in plan.get('resource_changes',[]) if r['mode']=='managed' and r['change']['actions']!=['no-op']]
 c.require(len(changes)==1,'Only one imported operating A record may change')
 r=changes[0];change=r['change'];c.require(r['address']=='aws_route53_record.operating["root"]' and change['actions']==['update'],'No create/delete/replace during DNS cutover')
 before,after=change['before'],change['after']
 c.require(before['name'].rstrip('.')==after['name'].rstrip('.')==c.POLICY['domain'] and before['type']==after['type']=='A' and before['zone_id']==after['zone_id']==c.POLICY['zoneId'],'Wrong DNS owner/name/type')
 c.require(before['alias'][0]['name'].removeprefix('dualstack.').rstrip('.')==c.POLICY['oldDns'].removeprefix('dualstack.').rstrip('.'),'Unexpected previous DNS target')
 c.require(after['alias'][0]['name'].removeprefix('dualstack.').rstrip('.')==c.POLICY['newDns'].rstrip('.'),'Unexpected next DNS target')
 return True

def main():
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['plan','directory','kubeconfig']:p.add_argument('--'+n,required=True,type=Path)
 p.add_argument('--execute',required=True,action='store_true');p.add_argument('--approve-sha256',required=True)
 p.add_argument('--phase',required=True,choices=['operating-cors','dns','accept']);p.add_argument('--terraform-plan',type=Path);p.add_argument('--approve-terraform-sha256')
 p.add_argument('--functional-confirmation',type=Path,help='Private boolean-only operator acceptance after real operating login/upload/WS')
 a=p.parse_args();os.umask(0o077);sha=c.verify_plan(c.load(a.plan),a.approve_sha256);prior=c.prior_receipts(a.directory,a.phase,sha);k=runtime.Kube(a.kubeconfig)
 if a.phase=='operating-cors':
  c.require(a.terraform_plan is not None and a.approve_terraform_sha256,'Review exact CORS Plan first');result=operating_cors(a.directory,a.terraform_plan,a.approve_terraform_sha256)
 elif a.phase=='dns':
  c.require(a.terraform_plan is not None and a.approve_terraform_sha256,'Review exact DNS Plan first');result=dns(k,a.directory,a.terraform_plan,a.approve_terraform_sha256)
 else:
  confirmation=c.load(a.functional_confirmation)
  checks={'login','existingTrips','existingPhotos','newTripCompleted','websocket','v2StillStopped'}
  c.require(set(confirmation)==checks and all(v is True for v in confirmation.values()),'Real operating checks must finish before V2 retirement')
  result={'verified':True,'functionalChecks':confirmation,'v2DeletionStillRequiresExactTerraformPlan':True}
 c.receipt(a.directory,a.phase,sha,prior,result)
 print(json.dumps({'phase':a.phase,'completed':True,'v2Deleted':False}))
if __name__=='__main__':main()
