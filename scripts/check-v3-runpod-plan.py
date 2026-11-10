#!/usr/bin/env python3
"""Accept RunPod bootstrap; no workload changes."""
import argparse,json,re
from pathlib import Path
from v3_delivery_contract import OIDC_SUBJECT


def require(ok,message):
 if not ok:raise ValueError(message)


def trust(subject):
 return {'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Federated':'arn:aws:iam::483175530259:oidc-provider/token.actions.githubusercontent.com'},'Action':'sts:AssumeRoleWithWebIdentity','Condition':{'StringEquals':{'token.actions.githubusercontent.com:aud':'sts.amazonaws.com','token.actions.githubusercontent.com:sub':subject},'StringLike':{'sts:RoleSessionName':'v3-runpod-*'}}}]}


def validate(plan,review=None):
 changes=[r for r in plan.get('resource_changes',[])if r.get('mode','managed')=='managed'and r['change']['actions']!=['no-op']]
 if not changes:return {'creates':0,'updates':0,'deletes':0,'newNodes':0}
 providers=[p for p in plan.get('configuration',{}).get('provider_config',{}).values() if p.get('full_name')=='registry.terraform.io/hashicorp/aws']
 require(len(providers)==1 and providers[0]['expressions']['region'].get('constant_value')=='ap-northeast-2' and providers[0]['expressions']['allowed_account_ids'].get('constant_value')==['483175530259'],'Fixed AWS account and region required')
 require(review and set(review)=={'systemInstanceIds','eksEndpoint','runtimeSecretArn'},'Reviewed V3 system targets and endpoint required')
 ids=review['systemInstanceIds'];endpoint=review['eksEndpoint']
 require(1<=len(ids)<=2 and len(set(ids))==len(ids)and all(re.fullmatch('i-[a-f0-9]{17}',i)for i in ids),'Exact existing system instance IDs')
 require(re.fullmatch(r'https://[A-Fa-f0-9]{32}\.[a-z0-9-]+\.ap-northeast-2\.eks\.amazonaws\.com',endpoint),'Exact EKS TLS hostname')
 expected={'aws_iam_role.status[0]','aws_iam_role_policy.status[0]','aws_ssm_document.status[0]','aws_eks_access_entry.status[0]'}
 require(len(changes)==4 and {r['address']for r in changes}==expected,'Only four RunPod access resources may be created')
 data={}
 for r in changes:
  require(r['change']['actions']==['create']and r['change'].get('before')is None,'No update/deletion/replacement')
  data[r['address']]=r['change']['after']
 role='yeodam-v3-foundation-runpod-update';arn='arn:aws:iam::483175530259:role/'+role
 r=data['aws_iam_role.status[0]'];require(r['name']==role,'Exact RunPod update role')
 require(json.loads(r['assume_role_policy'])==trust(OIDC_SUBJECT),'Exact approved-environment immutable OIDC trust')
 doc=data['aws_ssm_document.status[0]'];require(doc['name']=='Yeodam-V3-RunPod-Update'and doc['document_type']=='Session'and doc['document_format']=='JSON','Port tunnel only')
 require(json.loads(doc['content'])=={'schemaVersion':'1.0','description':'V3 model URL management TLS tunnel','sessionType':'Port','inputs':{'idleSessionTimeout':'5','maxSessionDuration':'60'},'properties':{'host':endpoint.removeprefix('https://'),'portNumber':'443','localPortNumber':'18574','type':'LocalPortForwarding'}},'No shell or arbitrary host/port parameters')
 require(re.fullmatch(r'arn:aws:secretsmanager:ap-northeast-2:483175530259:secret:yeodam/v3/foundation/ai/runtime-[A-Za-z0-9]{6}',review['runtimeSecretArn']),'Only the existing V3 AI runtime Secret')
 policy={'Version':'2012-10-17','Statement':[
  {'Effect':'Allow','Action':['secretsmanager:GetSecretValue','secretsmanager:DescribeSecret','secretsmanager:PutSecretValue','secretsmanager:UpdateSecretVersionStage'],'Resource':review['runtimeSecretArn']},
  {'Effect':'Allow','Action':['eks:DescribeCluster'],'Resource':'arn:aws:eks:ap-northeast-2:483175530259:cluster/yeodam-v3-foundation'},
  {'Effect':'Allow','Action':['ssm:StartSession'],'Resource':['arn:aws:ec2:ap-northeast-2:483175530259:instance/'+i for i in ids]+['arn:aws:ssm:ap-northeast-2:483175530259:document/Yeodam-V3-RunPod-Update']},
  {'Effect':'Allow','Action':['ssmmessages:OpenDataChannel'],'Resource':'arn:aws:ssm:ap-northeast-2:483175530259:session/v3-runpod-*'},
  {'Effect':'Allow','Action':['ssm:TerminateSession'],'Resource':'arn:aws:ssm:ap-northeast-2:483175530259:session/*','Condition':{'StringEquals':{'ssm:resourceTag/aws:ssmmessages:session-id':'${aws:userid}'}}}]}
 p=data['aws_iam_role_policy.status[0]'];require(p['role']==role and p['name']=='ai-url-update-only'and json.loads(p['policy'])==policy,'Only the exact AI Secret, fixed TLS tunnel and own session cleanup')
 entry=data['aws_eks_access_entry.status[0]'];require(entry['cluster_name']=='yeodam-v3-foundation'and entry['principal_arn']==arn and entry['type']=='STANDARD'and entry['kubernetes_groups']==['yeodam-v3-runpod-updater'],'AI updater group only; no admin policy')
 return {'creates':4,'updates':0,'deletes':0,'newNodes':0}


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan',type=Path);p.add_argument('--review',type=Path);a=p.parse_args();print(json.dumps(validate(json.loads(a.plan.read_text()),json.loads(a.review.read_text())if a.review else None)))
if __name__=='__main__':main()
