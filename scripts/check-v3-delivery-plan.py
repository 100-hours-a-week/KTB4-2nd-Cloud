#!/usr/bin/env python3
"""Accept observer bootstrap or its exact immutable-subject repair; no workload changes."""
import argparse,json,re
from pathlib import Path
from v3_delivery_contract import OIDC_SUBJECT


def require(ok,message):
 if not ok:raise ValueError(message)


def trust(subject):
 return {'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Federated':'arn:aws:iam::483175530259:oidc-provider/token.actions.githubusercontent.com'},'Action':'sts:AssumeRoleWithWebIdentity','Condition':{'StringEquals':{'token.actions.githubusercontent.com:aud':'sts.amazonaws.com','token.actions.githubusercontent.com:sub':subject},'StringLike':{'sts:RoleSessionName':'v3-cd-*'}}}]}


def validate(plan,review=None):
 changes=[r for r in plan.get('resource_changes',[])if r.get('mode','managed')=='managed'and r['change']['actions']!=['no-op']]
 if not changes:return {'creates':0,'updates':0,'deletes':0,'newNodes':0}
 require(review and set(review)=={'systemInstanceIds','eksEndpoint'},'Reviewed V3 system targets and endpoint required')
 ids=review['systemInstanceIds'];endpoint=review['eksEndpoint']
 require(1<=len(ids)<=2 and len(set(ids))==len(ids)and all(re.fullmatch('i-[a-f0-9]{17}',i)for i in ids),'Exact existing system instance IDs')
 require(re.fullmatch(r'https://[A-Fa-f0-9]{32}\.[a-z0-9-]+\.ap-northeast-2\.eks\.amazonaws\.com',endpoint),'Exact EKS TLS hostname')
 if len(changes)==1 and changes[0]['address']=='aws_iam_role.status[0]':
  change=changes[0]['change'];require(change['actions']==['update'],'Only in-place observer trust repair')
  before=dict(change['before']);after=dict(change['after'])
  require(before.get('name')==after.get('name')=='yeodam-v3-foundation-delivery-status','Exact observer role')
  require(json.loads(before.pop('assume_role_policy'))==trust('repo:100-hours-a-week/KTB4-2nd-Cloud:environment:v3-production'),'Only the known legacy subject may be repaired')
  require(json.loads(after.pop('assume_role_policy'))==trust(OIDC_SUBJECT),'Exact repository IDs and approved Environment required')
  require(before==after,'No role property or permission changes beyond subject')
  return {'creates':0,'updates':1,'deletes':0,'newNodes':0,'scope':'immutable OIDC subject only'}
 expected={'aws_iam_role.status[0]','aws_iam_role_policy.status[0]','aws_ssm_document.status[0]','aws_eks_access_entry.status[0]'}
 require(len(changes)==4 and {r['address']for r in changes}==expected,'Only four observer resources may be created')
 data={}
 for r in changes:
  require(r['change']['actions']==['create']and r['change'].get('before')is None,'No update/deletion/replacement')
  data[r['address']]=r['change']['after']
 role='yeodam-v3-foundation-delivery-status';arn='arn:aws:iam::483175530259:role/'+role
 r=data['aws_iam_role.status[0]'];require(r['name']==role,'Exact observer role')
 require(json.loads(r['assume_role_policy'])==trust(OIDC_SUBJECT),'Exact approved-environment immutable OIDC trust')
 doc=data['aws_ssm_document.status[0]'];require(doc['name']=='Yeodam-V3-Delivery-Status'and doc['document_type']=='Session'and doc['document_format']=='JSON','Port tunnel only')
 require(json.loads(doc['content'])=={'schemaVersion':'1.0','description':'Read-only Argo status TLS tunnel','sessionType':'Port','inputs':{'idleSessionTimeout':'5','maxSessionDuration':'20'},'properties':{'host':endpoint.removeprefix('https://'),'portNumber':'443','localPortNumber':'18573','type':'LocalPortForwarding'}},'No shell or arbitrary host/port parameters')
 policy={'Version':'2012-10-17','Statement':[
  {'Effect':'Allow','Action':['eks:DescribeCluster'],'Resource':'arn:aws:eks:ap-northeast-2:483175530259:cluster/yeodam-v3-foundation'},
  {'Effect':'Allow','Action':['ssm:StartSession'],'Resource':['arn:aws:ec2:ap-northeast-2:483175530259:instance/'+i for i in ids]+['arn:aws:ssm:ap-northeast-2:483175530259:document/Yeodam-V3-Delivery-Status']},
  {'Effect':'Allow','Action':['ssmmessages:OpenDataChannel'],'Resource':'arn:aws:ssm:ap-northeast-2:483175530259:session/v3-cd-*'},
  {'Effect':'Allow','Action':['ssm:TerminateSession'],'Resource':'arn:aws:ssm:ap-northeast-2:483175530259:session/*','Condition':{'StringEquals':{'ssm:resourceTag/aws:ssmmessages:session-id':'${aws:userid}'}}}]}
 p=data['aws_iam_role_policy.status[0]'];require(p['role']==role and p['name']=='read-only-argo-status'and json.loads(p['policy'])==policy,'No commands, Secret reads, infrastructure writes or other session termination')
 entry=data['aws_eks_access_entry.status[0]'];require(entry['cluster_name']=='yeodam-v3-foundation'and entry['principal_arn']==arn and entry['type']=='STANDARD'and entry['kubernetes_groups']==['yeodam-v3-delivery-observer'],'Read-only group only; no admin policy')
 return {'creates':4,'updates':0,'deletes':0,'newNodes':0}


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('plan',type=Path);p.add_argument('--review',type=Path);a=p.parse_args();print(json.dumps(validate(json.loads(a.plan.read_text()),json.loads(a.review.read_text())if a.review else None)))
if __name__=='__main__':main()
