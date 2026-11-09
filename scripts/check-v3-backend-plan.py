#!/usr/bin/env python3
"""Inspect private Plan JSON without printing values. Initial V3 BE IAM/S3 or exact object-tagging correction only."""
import argparse,json
from pathlib import Path
from prepare_v3_backend_values_contract import BUCKET,ROLES,require

WRITERS={'trip-api','photo-api','photo-converter'}

def s3_policy(role,tagging=True):
 writes=['s3:PutObject']+(['s3:PutObjectTagging'] if tagging else [])+['s3:DeleteObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'] if role in WRITERS else []
 return {'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:ListBucket','s3:GetBucketLocation','s3:ListBucketMultipartUploads'],'Resource':['arn:aws:s3:::'+BUCKET]},{'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion']+writes,'Resource':['arn:aws:s3:::'+BUCKET+'/*']}]}

def validate_tagging(plan,expected):
 changed=[]
 allowed={f'aws_iam_role_policy.s3["{r}"]':r for r in WRITERS}
 for resource in plan.get('resource_changes',[]):
  if resource.get('mode')!='managed':continue
  address=resource['address'];change=resource['change']
  require(address in expected,'Unexpected resource in tagging correction')
  if change['actions']==['no-op']:continue
  require(address in allowed and change['actions']==['update'],'Only three existing writer policies may update')
  role=allowed[address];before=change['before'];after=change['after']
  require(json.loads(before['policy'])==s3_policy(role,False) and json.loads(after['policy'])==s3_policy(role),'Only PutObjectTagging may be added to the V3 bucket')
  require(after.get('role')=='yeodam-v3-foundation-be-'+role and after.get('name')=='v3-app-data','Exact existing writer policy identity required')
  require({k:v for k,v in before.items() if k!='policy'}=={k:v for k,v in after.items() if k!='policy'},'Policy metadata must remain unchanged')
  require(not any(change.get('after_unknown',{}).values()),'Correction must be fully known')
  changed.append(address)
 require(not changed or set(changed)==set(allowed),'All three writer policies or an unchanged Plan required')
 return {'creates':0,'updates':len(changed),'deletes':0,'newEc2':0,'secretPayloads':0}

def validate(plan,cors_only=False,tagging_only=False):
 require(plan.get('complete') and not plan.get('errored') and not plan.get('deferred_changes'),'Plan must be complete')
 expected={f'{kind}.{name}[0]' for kind,name in [('aws_s3_bucket','app'),('aws_s3_bucket_public_access_block','app'),('aws_s3_bucket_server_side_encryption_configuration','app'),('aws_s3_bucket_versioning','app'),('aws_s3_bucket_policy','tls')]}
 expected|={'aws_s3_bucket_cors_configuration.qa[0]'}
 expected|={f'{kind}.app["{r}"]' for kind in ['aws_iam_role','aws_eks_pod_identity_association'] for r in ROLES}
 expected|={f'aws_iam_role_policy.s3["{r}"]' for r in ROLES if r!='auth-api'}
 require(not (cors_only and tagging_only),'Select one correction mode')
 if tagging_only:return validate_tagging(plan,expected)
 changed=[]
 for r in plan.get('resource_changes',[]):
  if r.get('mode')!='managed':continue
  require(r['address'] in expected,'Unexpected resource; no shared/EC2/GPU/Secret changes permitted')
  actions=r['change']['actions'];require(actions in [['no-op'],['create']],'No updates/replacement/deletion in initial BE Plan')
  if actions==['create']:
   changed.append(r['address'])
   after=r['change']['after']
   if r['address']=='aws_s3_bucket_cors_configuration.qa[0]':
    require(after.get('bucket')==BUCKET and len(after.get('cors_rule',[]))==1,'Exact V3 bucket CORS only')
    rule=after['cors_rule'][0]
    require(rule.get('id')=='v3-browser-qa' and set(rule.get('allowed_origins',[]))=={'https://v3.yeodam-2gether.com'} and set(rule.get('allowed_methods',[]))=={'GET','HEAD','PUT','POST'} and rule.get('allowed_headers')==['*'] and set(rule.get('expose_headers',[]))=={'ETag','x-amz-request-id'} and rule.get('max_age_seconds')==600,'Only the exact QA origin and presigned transfer methods')
   if r['address'].startswith('aws_iam_role.app'):
    role=r['address'].split('["')[1].split('"')[0]
    ns={'auth-api':'auth','trip-api':'trip','photo-api':'photo','photo-converter':'photo','trip-result-worker':'trip'}[role]
    require('assume_role_policy' in after and 'name' in after,'Role policy must be fully known')
    trust=json.loads(after['assume_role_policy'])['Statement']
    require(after['name']=='yeodam-v3-foundation-be-'+role and len(trust)==1,'Exact BE role identity required')
    require(trust[0]=={'Effect':'Allow','Principal':{'Service':'pods.eks.amazonaws.com'},'Action':['sts:AssumeRole','sts:TagSession'],'Condition':{'StringEquals':{'aws:SourceAccount':'483175530259','aws:RequestTag/kubernetes-namespace':ns,'aws:RequestTag/kubernetes-service-account':role},'ArnEquals':{'aws:SourceArn':'arn:aws:eks:ap-northeast-2:483175530259:cluster/yeodam-v3-foundation'}}},'Pod Identity trust boundary widened')
   if r['address'].startswith('aws_iam_role_policy.s3'):
    role=r['address'].split('["')[1].split('"')[0]
    require('policy' in after,'S3 policy must be fully known')
    policy=json.loads(after['policy'])
    require(policy==s3_policy(role),'Only exact V3 S3 actions/resources allowed')

  if r['address']=='aws_s3_bucket.app[0]':require(r['change']['after'].get('bucket')==BUCKET and r['change']['after'].get('force_destroy') is False,'Only the retained V3 bucket')
 require(set(changed)=={'aws_s3_bucket_cors_configuration.qa[0]'} if cors_only else (not changed or set(changed)==expected),'Only exact CORS addition, complete20-resource initial provision or no-change Plan permitted')
 return {'creates':len(changed),'updates':0,'deletes':0,'newEc2':0,'secretPayloads':0}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan',type=Path);m=p.add_mutually_exclusive_group();m.add_argument('--cors-only',action='store_true');m.add_argument('--tagging-only',action='store_true');a=p.parse_args();print(json.dumps(validate(json.loads(a.plan.read_text()),a.cors_only,a.tagging_only)))
if __name__=='__main__':main()
