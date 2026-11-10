#!/usr/bin/env python3
"""Read bounded migration evidence and disposable local/source or QA metadata. Never drains or resets work."""
import argparse,importlib.util,json,os,sys,tempfile
from pathlib import Path
import v2_v3_cutover as c

def load(name,file):
 s=importlib.util.spec_from_file_location(name,c.ROOT/'scripts'/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
run=load('cutover_runtime','run-v2-v3-cutover.py');restore=load('cutover_mysql','restore-v2-mysql-backup.py')
contract=json.loads((c.ROOT/'deploy/v3/kafka-access/files/business-contract.json').read_text())
GROUPS=sorted({g for role,a in contract['users'].items() if role!='connect-outbox' for g in a['groups']})

def kafka_audit(k):
 name='v3-cutover-audit';created=[]
 common={'namespace':'kafka','labels':{'strimzi.io/cluster':'yeodam-v3-kafka','yeodam.io/issue':'602'}}
 user={'apiVersion':'kafka.strimzi.io/v1','kind':'KafkaUser','metadata':{'name':name,**common},'spec':{'authentication':{'type':'scram-sha-512'},'authorization':{'type':'simple','acls':[
  *[{'resource':{'type':'group','name':g,'patternType':'literal'},'operations':['Read'],'host':'*'} for g in GROUPS],
  *[{'resource':{'type':'topic','name':t['name'],'patternType':'literal'},'operations':['Describe'],'host':'*'} for t in contract['topics']]]}}}
 podlabels={'yeodam.io/cutover':'audit'}
 policy={'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':name,**common},'spec':{'podSelector':{'matchLabels':podlabels},'policyTypes':['Egress'],'egress':[{'to':[{'podSelector':{'matchLabels':{'strimzi.io/name':'yeodam-v3-kafka-kafka'}}}],'ports':[{'protocol':'TCP','port':9093}]},{'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'kube-system'}},'podSelector':{'matchLabels':{'k8s-app':'kube-dns'}}}],'ports':[{'protocol':'UDP','port':53},{'protocol':'TCP','port':53}]}]}}
 ingress={'apiVersion':'networking.k8s.io/v1','kind':'NetworkPolicy','metadata':{'name':name+'-broker',**common},'spec':{'podSelector':{'matchLabels':{'strimzi.io/name':'yeodam-v3-kafka-kafka'}},'policyTypes':['Ingress'],'ingress':[{'from':[{'podSelector':{'matchLabels':podlabels}}],'ports':[{'protocol':'TCP','port':9093}]}]}}
 image=json.loads((c.ROOT/'deploy/v3/kafka-operator/upstream.lock.json').read_text()).get('kafkaImage')
 if not image:
  # Same immutable Kafka image already running on the brokers, no registry credential copies.
  brokers=k.get('kafka','pods')['items'];brokers=[p for p in brokers if p['metadata']['name'].startswith('yeodam-v3-kafka-') and any(x['name']=='kafka' for x in p['spec']['containers'])]
  c.require(brokers,'Kafka broker image unavailable');image=next(x['image'] for x in brokers[0]['spec']['containers'] if x['name']=='kafka')
 c.require('@sha256:' in image,'Pinned existing broker image required')
 pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,**common,'labels':podlabels},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'yeodam.io/role':'system'},'containers':[{'name':'audit','image':image,'command':['/bin/bash','-ec','sleep 600'],'resources':{'requests':{'cpu':'50m','memory':'128Mi'},'limits':{'cpu':'250m','memory':'256Mi'}},'securityContext':{'readOnlyRootFilesystem':True,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},'volumeMounts':[{'name':'credentials','mountPath':'/credentials','readOnly':True},{'name':'ca','mountPath':'/ca','readOnly':True},{'name':'tmp','mountPath':'/tmp'}]}],'volumes':[{'name':'credentials','secret':{'secretName':name,'defaultMode':288}},{'name':'ca','secret':{'secretName':'yeodam-v3-kafka-cluster-ca-cert','defaultMode':288}},{'name':'tmp','emptyDir':{'sizeLimit':'8Mi'}}],'securityContext':{'runAsUser':1001,'runAsGroup':1001,'fsGroup':1001,'runAsNonRoot':True,'seccompProfile':{'type':'RuntimeDefault'}}}}
 try:
  for obj in [user,policy,ingress,pod]:
   # Create-only: never adopt another operator's audit resources.
   k.call('create','-f','-',source=c.canonical(obj));created.append((obj['kind'],obj['metadata']['name']))
  k.call('-n','kafka','wait','--for=condition=Ready','kafkauser/'+name,'--timeout=120s')
  k.wait(lambda:k.get('kafka','pod',name).get('status',{}).get('phase')=='Running','Read-only audit Pod did not start')
  totals={}
  for group in GROUPS:
   text=k.execute('kafka',name,'audit',['/bin/bash','-s','--',group,*c.POLICY['consumerTopics'][group]],(c.ROOT/'scripts/cutover/kafka-audit.sh').read_bytes()).decode()
   if text.strip()=='EMPTY_GROUP_VERIFIED':totals[group]=0;continue
   rows=[line.split() for line in text.splitlines() if line.startswith(group+' ')]
   c.require(rows,'Group has no committed-offset evidence; inspect before migration')
   c.require(all(len(row)>=6 and row[5].isdigit() for row in rows),'Unknown consumer lag cannot count as zero')
   totals[group]=sum(int(row[5]) for row in rows)
  return totals
 finally:
  for kind,name in reversed(created):
   k.call('-n','kafka','delete',kind,name,'--wait=true')
   if kind=='KafkaUser':k.wait(lambda:not k.call('-n','kafka','get','secret',name,'--ignore-not-found','-o','jsonpath={.metadata.uid}').strip(),'Temporary audit Secret cleanup incomplete')

def main():
 p=argparse.ArgumentParser(description=__doc__)
 for n in ['kubeconfig','mysql-package','qdrant-package','photos-plan','photos-results','schema-image','output']:p.add_argument('--'+n,required=True,type=str if n=='schema-image' else Path)
 p.add_argument('--qa-accepted',action='store_true',required=True);a=p.parse_args();os.umask(0o077)
 c.require(not os.environ.get('GITHUB_ACTIONS'),'Private audit forbidden in CI')
 k=run.Kube(a.kubeconfig);manifest=c.mysql.verify_package(a.mysql_package);source={}
 def inspect(container,_):source.update(c.audit_jobs(lambda sql:c.mysql.sql(container,sql)))
 with tempfile.TemporaryDirectory(prefix='602-readonly-mysql-') as tmp:
  restore.restore_package(a.mysql_package,a.schema_image,Path(tmp)/'db',after_verify=inspect)
 qa=c.audit_jobs(k.sql)
 # SSO metadata confirms the stopped source and paused V2 ECS, not an assumption based on names.
 s=load('source599','backup-v2-qdrant.py');aws=s.Aws('yeodam-admin');c.require(aws('sts','get-caller-identity')['Account']=='483175530259','Wrong account')
 state=aws('ec2','describe-instances','--instance-ids',c.POLICY['sourceMysql'])['Reservations'][0]['Instances'][0]
 c.require(state['State']['Name']=='stopped','Source MySQL was resumed; review backup freshness')
 token=None
 while True:
  args=['cloudtrail','lookup-events','--lookup-attributes','AttributeKey=ResourceName,AttributeValue='+c.POLICY['sourceMysql'],'--start-time',manifest['createdAt'],'--max-results','50']
  if token:args+=['--next-token',token]
  events=aws(*args)
  c.require(not any(x['EventName'] in ['StartInstances','RebootInstances'] for x in events['Events']),'Source ran after backup; obtain a fresh frozen backup')
  token=events.get('NextToken')
  if not token:break
 aws.preflight()
 expected=c.qdrant.verify_package(a.qdrant_package)
 proof=aws.proof();c.qdrant.proof_valid(proof,'private-user-data')
 with s.tunnel(aws):c.require(c.qdrant.fingerprint(c.qdrant.Client(s.PORT,source=True))==expected['fingerprint'],'Source Qdrant changed after backup; refresh the approved snapshot')
 services=aws('ecs','describe-services','--cluster','yeodam-v2-production-app','--services',*['yeodam-v2-production-'+n for n in ['frontend','backend','ai-api','websocket']])
 c.require(len(services['services'])==4 and all(x[k]==0 for x in services['services'] for k in ['desiredCount','runningCount','pendingCount']),'Source ECS must stay stopped')
 import boto3,v2_v3_photo_migration as photos
 verified=c.verify_photos(c.load(a.photos_plan),c.load(a.photos_results),photos.S3Store(boto3.Session(profile_name='yeodam-admin')),manifest['sha256'])
 review={'photoObjectsVerified':verified,'sourceQdrantMatchesBackup':True,'capturedAt':c.now(),'sourceJobs':source,'qaJobs':qa,'qaConsumerLag':kafka_audit(k),'qaAiQueueItems':int(run.redis_action(k,'ai-redis','queue-count')),'qaUploadsInFlight':int(k.sql("SELECT COUNT(*) FROM yeodam.files WHERE upload_status='PENDING' AND deleted_at IS NULL;")),'sourceWritersStopped':True,'qaAccepted':True}
 c.write(a.output,review)
 c.validate_reconciliation(review)
 print('Reconciliation passed; only aggregates retained; temporary DB/audit Pod/user/network policies removed')
if __name__=='__main__':main()
