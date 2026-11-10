#!/usr/bin/env python3
"""Prepare or apply a private, exact deletion-only Plan after operating cutover."""
import argparse,importlib.util,json,os,subprocess,sys,traceback
from pathlib import Path
import v2_retirement as c

FAILURE_DIRECTORY=None
FAILURE_PHASE=None

def run(argv,**kw):
 p=subprocess.run(argv,capture_output=True,**kw)
 c.require(p.returncode==0,'Operation failed; private response suppressed')
 return p.stdout

def aws(*argv):
 return json.loads(run(['aws','--profile','yeodam-admin','--region',c.POLICY['region'],*argv]))
def phase_root(phase):return c.ROOT/'terraform/v2/retirement'/('production' if phase=='association' else phase)
def tfenv(directory,phase):return {**os.environ,'AWS_PROFILE':'yeodam-admin','AWS_REGION':c.POLICY['region'],'TF_DATA_DIR':str(directory/(('production' if phase=='association' else phase)+'-tfdata'))}
def merged():
 c.require(not os.environ.get('GITHUB_ACTIONS'),'Live retirement is forbidden in CI')
 c.require(not run(['git','status','--porcelain','--untracked-files=normal'],cwd=c.ROOT).strip(),'Commit the reviewed implementation first')
 main=json.loads(run(['gh','api','repos/100-hours-a-week/KTB4-2nd-Cloud/commits/main']))['sha']
 run(['git','fetch','--no-tags','origin',main],cwd=c.ROOT)
 head=run(['git','rev-parse','HEAD'],cwd=c.ROOT).decode().strip()
 c.require(head==main or subprocess.run(['git','merge-base','--is-ancestor',head,main],cwd=c.ROOT).returncode==0,'Implementation must be merged before live changes')
def live(kubeconfig):
 c.require(aws('sts','get-caller-identity')['Account']==c.POLICY['account'],'Wrong AWS account')
 s=importlib.util.spec_from_file_location('retirement_live',c.ROOT/'scripts/finalize-v2-v3-cutover.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
 k=m.runtime.Kube(kubeconfig);delivery=m.live_delivery(k);health=m.public_preflight(k)
 records=aws('route53','list-resource-record-sets','--hosted-zone-id',m.c.POLICY['zoneId'])['ResourceRecordSets']
 root=[r for r in records if r['Name']==m.c.POLICY['domain']+'.' and r['Type']=='A']
 c.require(len(root)==1 and root[0]['AliasTarget']['DNSName'].removeprefix('dualstack.')==m.c.POLICY['newDns'],'Operating DNS must still point to V3')
 gpu=aws('ec2','describe-instances','--instance-ids',c.POLICY['gpuInstance'],'--query','Reservations[0].Instances[0].{id:InstanceId,state:State.Name,type:InstanceType,vpc:VpcId,ip:PrivateIpAddress,profile:IamInstanceProfile.Arn,volumes:BlockDeviceMappings[].Ebs.VolumeId,sgs:SecurityGroups[].GroupId}')
 c.require(gpu['state']=='running' and gpu['type']=='g6.xlarge' and gpu['vpc']==c.POLICY['gpuVpc'] and gpu['ip']=='10.30.10.162','Preserve the exact shared GPU identity')
 gpu['volumes']=sorted(gpu['volumes']);gpu['sgs']=sorted(gpu['sgs'])
 ai=k.get('ai','deployment','ai-api');selector=','.join(n+'='+v for n,v in sorted(ai['spec']['selector']['matchLabels'].items()))
 pods=json.loads(k.call('-n','ai','get','pods','-l',selector,'-o','json'))['items'];c.require(len(pods)==1,'Exactly one AI API Pod required for the private path check')
 probe="import socket; host='ai-gpu.production.yeodam.internal'; assert socket.gethostbyname(host)=='10.30.10.162'; socket.create_connection((host,8000),5).close(); print('EMBEDDING_TCP_OK')"
 c.require(k.execute('ai',pods[0]['metadata']['name'],pods[0]['spec']['containers'][0]['name'],['python','-c',probe]).strip()==b'EMBEDDING_TCP_OK','V3 embedding DNS/TCP path failed')
 zone=aws('route53','get-hosted-zone','--id',c.POLICY['privateZone'])
 vpcs={v['VPCId'] for v in zone['VPCs']};c.require({c.POLICY['gpuVpc'],c.POLICY['v3Vpc']}<=vpcs,'GPU and V3 private DNS links must remain')
 runs=json.loads(run(['gh','run','list','--workflow','v3-approved-release.yml','--limit','10','--json','status'],cwd=c.ROOT))
 c.require(all(x['status']=='completed' for x in runs),'Wait for the current approved CD')
 return {'delivery':delivery,'public':health,'gpuIdentitySha256':c.digest(gpu),'gpuDnsPreserved':True,'v3DnsPreserved':True,'embeddingTcpReachable':True}
def stopped_instances(ids):
 if not ids:return
 states=aws('ec2','describe-instances','--instance-ids',*ids,'--query','Reservations[].Instances[].{id:InstanceId,state:State.Name}')
 c.require({x['id'] for x in states}==set(ids) and all(x['state']=='stopped' for x in states),'Only the exact stopped V2 instances may terminate')
def main():
 global FAILURE_DIRECTORY,FAILURE_PHASE
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['plan','apply']);p.add_argument('--phase',required=True,choices=['association','production','base']);p.add_argument('--directory',type=Path,required=True);p.add_argument('--backend-config',type=Path);p.add_argument('--cutover-run',type=Path,required=True);p.add_argument('--kubeconfig',type=Path,required=True);p.add_argument('--draft',action='store_true');p.add_argument('--approve-plan-sha256');a=p.parse_args();os.umask(0o077)
 d=a.directory;d.mkdir(parents=True,exist_ok=True,mode=0o700);c.require(not d.is_symlink() and d.stat().st_mode&0o077==0,'Private directory required')
 FAILURE_DIRECTORY=d;FAILURE_PHASE=a.action+'-'+a.phase
 c.require(not os.environ.get('GITHUB_ACTIONS'),'Live retirement is forbidden in CI')
 evidence=c.cutover_evidence(a.cutover_run,complete=a.action=='apply' and a.phase!='association');env=tfenv(d,a.phase);prefix=['terraform','-chdir='+str(phase_root(a.phase))]
 plan=d/(a.phase+('-draft' if a.draft else '')+'.tfplan');review_path=plan.with_suffix('.review.json')
 if a.action=='plan':
  c.require(a.backend_config is not None and a.backend_config.is_file(),'Private backend config required')
  with (d/(plan.stem+'-init.log')).open('xb') as f:
   q=subprocess.run(prefix+['init','-input=false','-backend-config='+str(a.backend_config.resolve())],stdout=f,stderr=f,env=env)
  c.require(q.returncode==0,'Terraform initialization failed; private log retained')
  cmd=prefix+['plan','-input=false','-out='+str(plan.resolve())]
  if a.phase=='association':cmd+=['-target=aws_route53_zone_association.legacy_production','-var=import_legacy_zone_association=true']
  with (d/(plan.stem+'-plan.log')).open('xb') as f:q=subprocess.run(cmd,stdout=f,stderr=f,env=env)
  c.require(q.returncode==0,'Plan failed; inspect private diagnostics without printing values')
  raw=run(prefix+['show','-json',str(plan.resolve())],env=env);(plan.with_suffix('.private.json')).write_bytes(raw);summary=c.inspect_plan(json.loads(raw),a.phase,a.draft)
  record={'version':1,'phase':a.phase,'draft':a.draft,'planSha256':c.file_sha(plan),'sourceSha256':c.source_hash(),'cutoverEvidence':evidence,'summary':summary};c.write(review_path,record)
  print(json.dumps({k:record[k] for k in ['phase','draft','planSha256']}|{'deletions':summary['deletions'],'imports':summary['imports'],'creates':0,'updates':0}));return
 c.require(not a.draft,'Draft Plans cannot be applied');record=c.load(review_path)
 c.require(record['phase']==a.phase and record['draft'] is False and c.file_sha(plan)==record['planSha256']==a.approve_plan_sha256,'Exact reviewed binary Plan required')
 c.require(record['sourceSha256']==c.source_hash(),'Retirement implementation changed; prepare a fresh Plan');merged()
 raw=run(prefix+['show','-json',str(plan.resolve())],env=env);summary=c.inspect_plan(json.loads(raw),a.phase)
 c.require(summary==record['summary'],'Plan review changed')
 attempt=d/(a.phase+'-apply-attempt.json');c.require(not attempt.exists(),'Review the previous attempt before repeating live changes')
 before=live(a.kubeconfig)
 if a.phase=='production':
  c.require(c.load(d/'association-applied.json')['completed'] is True,'Adopt the obsolete DNS association first')
  ids=[c.INSTANCES[x] for x in summary['deletedAddresses'] if x in c.INSTANCES];stopped_instances(ids)
  services=aws('ecs','describe-services','--cluster','yeodam-v2-production-app','--services',*['yeodam-v2-production-'+x for x in ['frontend','backend','ai-api','websocket']])
  c.require(all(s['runningCount']==s['pendingCount']==s['desiredCount']==0 for s in services['services']) and all(x['reason']=='MISSING' for x in services.get('failures',[])),'V2 ECS must remain stopped')
 if a.phase=='base':
  c.require(c.load(d/'production-applied.json')['completed'] is True,'Retire V2 runtime before the shared base network')
  interfaces=aws('ec2','describe-network-interfaces','--filters','Name=vpc-id,Values='+c.POLICY['retiredVpc'])['NetworkInterfaces'];c.require(not interfaces,'Retired VPC still has network consumers')
 c.write(attempt,{'phase':a.phase,'planSha256':record['planSha256'],'sourceSha256':record['sourceSha256'],'before':before,'cutoverEvidence':evidence})
 if a.phase=='production':
  for instance in ids:
   run(['aws','--profile','yeodam-admin','--region',c.POLICY['region'],'ec2','modify-instance-attribute','--instance-id',instance,'--disable-api-termination','Value=false'])
 with (d/(a.phase+'-apply.log')).open('xb') as f:q=subprocess.run(prefix+['apply','-input=false','-auto-approve',str(plan.resolve())],stdout=f,stderr=f,env=env)
 c.require(q.returncode==0,'Apply stopped; preserve evidence and inspect before recovery')
 after=live(a.kubeconfig);c.require(after['gpuIdentitySha256']==before['gpuIdentitySha256'],'Shared GPU metadata changed')
 c.write(d/(a.phase+'-applied.json'),{'phase':a.phase,'completed':True,'planSha256':record['planSha256'],'sourceSha256':record['sourceSha256'],'summary':summary,'before':before,'after':after,'cutoverEvidence':evidence});print(json.dumps({'phase':a.phase,'completed':True,'deletions':summary['deletions'],'gpuAndV3Preserved':True}))
if __name__=='__main__':
 try:main()
 except Exception as error:
  if FAILURE_DIRECTORY is not None:
   path=FAILURE_DIRECTORY/(FAILURE_PHASE+'-failure.json')
   if not path.exists():
    frames=[{'file':Path(x.filename).name,'line':x.lineno,'function':x.name} for x in traceback.extract_tb(error.__traceback__)]
    check=str(error) if isinstance(error,ValueError) and frames and frames[-1]['function']=='require' else None
    c.write(path,{'errorType':type(error).__name__,'frames':frames,'failedCheck':check})
  raise SystemExit('Retirement stopped; inspect private evidence. No State, plan values or credentials printed.') from None
