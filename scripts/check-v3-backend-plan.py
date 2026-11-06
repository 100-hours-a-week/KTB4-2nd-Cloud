#!/usr/bin/env python3
"""Inspect private Plan JSON without printing values. Initial V3 BE IAM/S3 creates only."""
import argparse,json
from pathlib import Path
from prepare_v3_backend_values_contract import BUCKET,ROLES,require

def validate(plan):
 require(plan.get('complete') and not plan.get('errored') and not plan.get('deferred_changes'),'Plan must be complete')
 expected={f'{kind}.{name}[0]' for kind,name in [('aws_s3_bucket','app'),('aws_s3_bucket_public_access_block','app'),('aws_s3_bucket_server_side_encryption_configuration','app'),('aws_s3_bucket_versioning','app'),('aws_s3_bucket_policy','tls')]}
 expected|={f'{kind}.app["{r}"]' for kind in ['aws_iam_role','aws_eks_pod_identity_association'] for r in ROLES}
 expected|={f'aws_iam_role_policy.s3["{r}"]' for r in ROLES if r!='auth-api'}
 changed=[]
 for r in plan.get('resource_changes',[]):
  if r.get('mode')!='managed':continue
  require(r['address'] in expected,'Unexpected resource; no shared/EC2/GPU/Secret changes permitted')
  actions=r['change']['actions'];require(actions in [['no-op'],['create']],'No updates/replacement/deletion in initial BE Plan')
  if actions==['create']:
   changed.append(r['address'])
   after=r['change']['after']
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
    writes=['s3:PutObject','s3:DeleteObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'] if role!='trip-result-worker' else []
    require(policy=={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:ListBucket','s3:GetBucketLocation','s3:ListBucketMultipartUploads'],'Resource':['arn:aws:s3:::'+BUCKET]},{'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion']+writes,'Resource':['arn:aws:s3:::'+BUCKET+'/*']}]},'Only exact V3 S3 actions/resources allowed')

  if r['address']=='aws_s3_bucket.app[0]':require(r['change']['after'].get('bucket')==BUCKET and r['change']['after'].get('force_destroy') is False,'Only the retained V3 bucket')
 require(not changed or set(changed)==expected,'Only a complete19-resource initial provision or no-change Plan is permitted')
 return {'creates':len(changed),'updates':0,'deletes':0,'newEc2':0,'secretPayloads':0}

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan',type=Path);a=p.parse_args();print(json.dumps(validate(json.loads(a.plan.read_text()))))
if __name__=='__main__':main()
