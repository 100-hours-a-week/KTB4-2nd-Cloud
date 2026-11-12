#!/usr/bin/env python3
"""Explicit post-merge converter observation/provision/activation. No AWS Secret client or GPU actions."""
import argparse,base64,copy,hashlib,json,os,subprocess,tempfile,time
from pathlib import Path
import yaml
from v3_cdc_fault import Kube,require,private_write,ready_pod
import v3_converter_scaling as policy
from v3_autoscaling import LANES,API
ROOT=Path(__file__).resolve().parents[1];CHART=ROOT/'deploy/v3/autoscaling';RELEASE='yeodam-v3-autoscaling'

def helm(args,source=None):
 p=subprocess.run([os.environ.get('V3_HELM','helm')]+args,input=source,text=True,capture_output=True,timeout=240,env=dict(os.environ,AWS_PROFILE='yeodam-admin'))
 require(p.returncode==0,'Controlled Helm operation failed; inspect release status without printing values')
 return p.stdout

def records(text):return [d for d in yaml.safe_load_all(text)if d]
def index(docs):return {(d['kind'],d['metadata'].get('namespace'),d['metadata']['name']):d for d in docs}
def release_values(k):
 return json.loads(helm(['get','values',RELEASE,'-n','observability','--kubeconfig',k.config,'-o','json']))
def identity(k,candidate):
 uid=k.identity();app=k.get('application','argocd','yeodam-v3-apps');status=app.get('status',{})
 require(status.get('sync',{}).get('status')=='Synced'and status.get('health',{}).get('status')=='Healthy','Wait for approved Argo reconciliation')
 require(not app.get('operation'),'Another Argo operation is pending')
 dep=k.get('deployment','photo','photo-converter');require(dep['spec']['template']['spec']['containers'][0]['image']==candidate['components']['backend']['image'],'Deployed BE differs from reviewed image')
 resources=dep['spec']['template']['spec']['containers'][0]['resources'];require(resources=={'requests':{'cpu':'100m','memory':'512Mi','ephemeral-storage':'64Mi'},'limits':{'memory':'1Gi','ephemeral-storage':'512Mi'}},'Preserve exact minimum converter budget')
 require(dep['status'].get('observedGeneration',0)>=dep['metadata']['generation'] and dep['status'].get('readyReplicas',0)==dep['spec']['replicas'],'Converter is not freshly Ready')
 return uid,app,dep

def provisioning(k,candidate):
 identity(k,candidate);v=release_values(k)
 require(v.get('apiEnabled')is True and not v.get('converterEnabled'),'Only prepare access before converter activation')
 v.update(converterAccessEnabled=True)
 helm(['upgrade',RELEASE,str(CHART),'-n','observability','--kubeconfig',k.config,'-f','-','--rollback-on-failure','--wait','--timeout','180s'],json.dumps(v))
 user=k.get('kafkauser','kafka','v3-conversion-lag')
 require(user['spec']['authorization']['type']=='simple' and all(x['operations']==['Describe']for x in user['spec']['authorization']['acls']),'Observer must have Describe only')
 require({x['resource']['name']for x in user['spec']['authorization']['acls']}==policy.TOPICS|{'prod.yeodam.image-conversion-'+l for l in LANES},'Unexpected observer scope')
 for _ in range(30):
  user=k.get('kafkauser','kafka','v3-conversion-lag')
  if user.get('status',{}).get('observedGeneration',0)>=user['metadata']['generation'] and any(c['type']=='Ready'and c['status']=='True'for c in user.get('status',{}).get('conditions',[])):break
  time.sleep(1)
 else:raise ValueError('Kafka observer not Ready')
 source=k.get('secret','kafka','v3-conversion-lag');ca=k.get('secret','kafka','yeodam-v3-kafka-cluster-ca-cert')
 require(any(x.get('uid')==user['metadata']['uid']for x in source['metadata'].get('ownerReferences',[])),'Unexpected source credential owner')
 pem=base64.b64decode(ca['data']['ca.crt'],validate=True);require(b'BEGIN CERTIFICATE'in pem and b'PRIVATE KEY'not in pem,'Public CA only')
 desired={'username':base64.b64encode(b'v3-conversion-lag').decode(),'password':source['data']['password'],'ca.crt':ca['data']['ca.crt']}
 existing=json.loads(k.call(['-n','keda','get','secret','v3-keda-kafka','--ignore-not-found','-o','json'])or'{}')
 if existing:
  require(existing['metadata'].get('labels',{}).get('yeodam.io/owner')=='converter-scaling' and existing.get('data')==desired,'Never overwrite/adopt a different observer Secret')
 else:k.call(['create','-f','-'],json.dumps({'apiVersion':'v1','kind':'Secret','metadata':{'name':'v3-keda-kafka','namespace':'keda','labels':{'yeodam.io/owner':'converter-scaling'}},'type':'Opaque','data':desired}))
 print(json.dumps({'accessReady':True,'targetSecret':'keda/v3-keda-kafka','originalCredentialsChanged':False,'scaledObjectEnabled':False}))

class Live(Kube):
 def __init__(self,config):super().__init__(config);self.config=str(config)

def probe(k,jar):
 lock=json.loads((CHART/'converter-runtime.lock.json').read_text());require(hashlib.sha256(jar.read_bytes()).hexdigest()==lock['kafkaClientSha256'],'Use the pinned public Kafka client jar')
 nodes=k.get('nodes')['items'];pods=json.loads(k.call(['get','pods','-A','-o','json']))['items']
 from importlib.util import spec_from_file_location,module_from_spec
 s=spec_from_file_location('capacity',ROOT/'scripts/inspect-v3-autoscaling.py');m=module_from_spec(s);s.loader.exec_module(m)
 rooms=[r for r in m.capacity(nodes,pods)if r['managementPodSlots']>=1];require(rooms,'No existing CPU worker slot; do not create an EC2 for observation')
 node=rooms[0]['node'];name='v3-converter-lag-check';created=[]
 # No Secret data is read here: the credential stays mounted inside this short-lived Pod.
 require(not json.loads(k.call(['-n','keda','get','pod',name,'--ignore-not-found','-o','json'])or'{}'),'Owned observer Pod name already in use')
 require(not json.loads(k.call(['-n','keda','get','configmap',name,'--ignore-not-found','-o','json'])or'{}'),'Owned observer ConfigMap name already in use')
 with tempfile.TemporaryDirectory(prefix='converter-probe-')as tmp:
  p=subprocess.run([os.environ.get('JAVAC','javac'),'--release','17','-cp',str(jar),'-d',tmp,str(ROOT/'tests/ci/v3/converter/ConverterLag.java')],capture_output=True,text=True)
  require(p.returncode==0,'Read-only metadata probe compilation failed')
  cm={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':name,'namespace':'keda','labels':{'yeodam.io/issue':'674'}},'binaryData':{'ConverterLag.class':base64.b64encode((Path(tmp)/'ConverterLag.class').read_bytes()).decode()}}
  image=json.loads((ROOT/'deploy/v3/connect-image/source.lock.json').read_text())['baseImage']
  pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':'keda','labels':{'yeodam.io/issue':'674','yeodam.io/component':'converter-lag-probe'}},'spec':{'nodeName':node,'restartPolicy':'Never','activeDeadlineSeconds':90,'automountServiceAccountToken':False,'securityContext':{'runAsNonRoot':True,'runAsUser':1001,'runAsGroup':1001,'seccompProfile':{'type':'RuntimeDefault'}},'containers':[{'name':'probe','image':image,'command':['java','-Xmx64m','-cp','/probe:/opt/kafka/libs/*','ConverterLag'],'resources':{'requests':{'cpu':'25m','memory':'64Mi'},'limits':{'cpu':'250m','memory':'192Mi'}},'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},'volumeMounts':[{'name':'code','mountPath':'/probe','readOnly':True},{'name':'auth','mountPath':'/auth','readOnly':True},{'name':'tmp','mountPath':'/tmp'}]}],'volumes':[{'name':'code','configMap':{'name':name}},{'name':'auth','secret':{'secretName':'v3-keda-kafka'}},{'name':'tmp','emptyDir':{'medium':'Memory','sizeLimit':'16Mi'}}]}}
  try:
   for kind,obj in [('configmaps',cm),('pods',pod)]:
    live=json.loads(k.call(['create','-f','-','-o','json'],json.dumps(obj)));created.append((kind,live['metadata']['uid']))
   for _ in range(90):
    live=k.get('pod','keda',name)
    if live.get('status',{}).get('phase')in ('Succeeded','Failed'):break
    time.sleep(1)
   require(live.get('status',{}).get('phase')=='Succeeded','Read-only Kafka metadata probe failed; log details withheld')
   lines=k.call(['-n','keda','logs',name]).splitlines();result=[json.loads(x.removeprefix('CONVERTER_LAG '))for x in lines if x.startswith('CONVERTER_LAG ')]
   require(len(result)==1,'Missing bounded Kafka metadata result');return result[0]
  finally:
   for kind,uid in reversed(created):
    k.call(['delete','--raw','/api/v1/namespaces/keda/'+kind+'/'+name,'-f','-'],json.dumps({'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':uid}}))

def cdc_state(k,uid,state):
 require(state in ('stopped','running'),'Reviewed CDC state only')
 current=k.get('kafkaconnector','kafka','yeodam-v3-outbox');require(current['metadata']['uid']==uid,'CDC identity changed')
 if current['spec'].get('state','running')!=state:
  patch=[{'op':'test','path':'/metadata/uid','value':uid},{'op':'test','path':'/metadata/resourceVersion','value':current['metadata']['resourceVersion']},{'op':'add','path':'/spec/state','value':state}]
  k.call(['-n','kafka','patch','kafkaconnector','yeodam-v3-outbox','--type=json','-p',json.dumps(patch)])
 for _ in range(60):
  live=k.get('kafkaconnector','kafka','yeodam-v3-outbox');status=live.get('status',{});cs=status.get('connectorStatus',{});tasks=cs.get('tasks',[])
  observed=status.get('observedGeneration',0)>=live['metadata']['generation']
  ok=cs.get('connector',{}).get('state')==state.upper() and (not tasks if state=='stopped' else bool(tasks) and all(t['state']=='RUNNING'for t in tasks))
  if observed and ok:return
  time.sleep(1)
 raise ValueError('CDC state transition incomplete; no offset reset; inspect before explicit rollback')


def sample(k,candidate,jar):
 measured=time.time();uid,app,dep=identity(k,candidate);parts=dep['spec']['template']['spec'];pods=k.get('pods','photo',selector='app.kubernetes.io/instance=photo-converter')['items'];require(pods and all(ready_pod(p)for p in pods),'Converter Pods must be Ready')
 topics=[]
 for topic in sorted(policy.TOPICS):
  t=k.get('kafkatopic','kafka',topic);topics.append({'name':topic,'uid':t['metadata']['uid'],'partitions':t['spec']['partitions'],'ready':any(c['type']=='Ready'and c['status']=='True'for c in t.get('status',{}).get('conditions',[]))})
 connector=k.get('kafkaconnector','kafka','yeodam-v3-outbox');cs=connector.get('status',{}).get('connectorStatus',{});cdc=cs.get('connector',{}).get('state')=='RUNNING'and bool(cs.get('tasks'))and all(t['state']=='RUNNING'for t in cs['tasks'])
 sql=(CHART/'files/converter-drain.sql').read_bytes();lock=json.loads((CHART/'converter-runtime.lock.json').read_text());require(hashlib.sha256(sql).hexdigest()==lock['businessSqlSha256'],'Use fixed read-only aggregate SQL')
 command='source /runtime/common.sh; umask 077; c=$(mktemp /tmp/converter-drain.XXXXXX); trap \'rm -f "$c"\' EXIT; client_file root local "$c" root; mysql --defaults-extra-file="$c" --batch --skip-column-names --silent 2>/dev/null'
 db=json.loads(k.call(['-n','mysql','exec','-i','mysql-0','-c','mysql','--','/bin/bash','-ec',command],sql.decode()))
 free=[]
 brokers=[p for p in k.get('pods','kafka')['items']if p['metadata']['name'].startswith('yeodam-v3-kafka-dual-role-')]
 require(len(brokers)==3 and all(ready_pod(p)for p in brokers),'Exactly three Ready brokers required')
 for broker in brokers:
  line=k.call(['-n','kafka','exec',broker['metadata']['name'],'-c','kafka','--','df','-Pk','/var/lib/kafka/data-0']).splitlines()[-1].split();free.append(int(line[3])*1024)
 kafka=probe(k,jar)
 return {'brokerFreeBytes':free,'recordedAt':measured,'clusterUid':uid,'approvedRef':app['status']['sync']['revision'],'converterUid':dep['metadata']['uid'],'converterReady':True,'image':parts['containers'][0]['image'],'graceSeconds':parts['terminationGracePeriodSeconds'],'business':db,'topics':topics,'cdcRunning':cdc,'connectorUid':connector['metadata']['uid'],'kafka':kafka}

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['provision-access','observe','prepare','candidate','expand','activate','rollback']);p.add_argument('--kubeconfig');p.add_argument('--candidate',type=Path,default=ROOT/'deploy/v3/releases/candidate.json');p.add_argument('--proof',type=Path);p.add_argument('--observations',type=Path);p.add_argument('--plan',type=Path);p.add_argument('--plan-sha256');p.add_argument('--output',type=Path);p.add_argument('--client-jar',type=Path);p.add_argument('--traffic-quiet-confirmed',action='store_true');p.add_argument('--execute-reviewed-change',action='store_true');p.add_argument('--approved-secret-copy',action='store_true');p.add_argument('--execute-reviewed-observation',action='store_true');a=p.parse_args();candidate=json.loads(a.candidate.read_text())
 if a.action=='prepare':
  require(a.proof and a.observations and a.output,'Proof, observations and new private output required');plan=policy.prepare(json.loads(a.observations.read_text()),json.loads(a.proof.read_text()),candidate,a.traffic_quiet_confirmed);private_write(a.output,plan);print(json.dumps({'planSha256':policy.digest(plan),'currentPartitions':plan['currentPartitions'],'desiredPartitions':2}));return
 require(a.kubeconfig or a.action=='candidate','Explicit verified EKS kubeconfig required')
 if a.action=='provision-access':
  require(a.execute_reviewed_change and a.approved_secret_copy,'Explicit Kafka-to-keda secret-copy approval required');provisioning(Live(a.kubeconfig),candidate);return
 if a.action=='observe':
  require(a.execute_reviewed_observation and a.client_jar and a.output,'Explicit temporary probe and aggregate SQL approval required');k=Live(a.kubeconfig);first=sample(k,candidate,a.client_jar);time.sleep(5);second=sample(k,candidate,a.client_jar);private_write(a.output,[first,second]);print('Saved two fresh aggregates; temporary observer resources removed');return
 require(a.proof and a.observations and a.plan and a.plan_sha256,'Reviewed evidence and exact plan hash required');proof=json.loads(a.proof.read_text());observations=json.loads(a.observations.read_text());plan=json.loads(a.plan.read_text());
 if a.action=='rollback':
  require(policy.digest(plan)==a.plan_sha256 and a.execute_reviewed_change,'Exact reviewed rollback required');policy.verify_proof(proof,candidate['components']['backend']);k=Live(a.kubeconfig);k.identity();dep=k.get('deployment','photo','photo-converter');require(dep['metadata']['uid']==observations[-1]['converterUid'] and dep['spec']['template']['spec']['containers'][0]['image']==plan['image'],'Rollback target changed');v=release_values(k);require(v.get('apiEnabled')is True,'Preserve API HPAs');v['converterEnabled']=False;helm(['upgrade',RELEASE,str(CHART),'-n','observability','--kubeconfig',k.config,'-f','-','--wait','--timeout','180s'],json.dumps(v));k.call(['-n','photo','scale','deployment','photo-converter','--replicas=1'])
  # A partial expansion is never reversed. Resume the producer only after each desired Topic state settles.
  for reviewed in plan['topics']:
   topic=k.get('kafkatopic','kafka',reviewed['name']);status=topic.get('status',{})
   require(topic['metadata']['uid']==reviewed['uid'] and topic['spec']['partitions']in (1,2) and status.get('observedGeneration',0)>=topic['metadata']['generation'] and any(x['type']=='Ready'and x['status']=='True'for x in status.get('conditions',[])),'Converter rolled back; CDC awaits settled Topic state review')
  cdc_state(k,plan['connectorUid'],'running');print('KEDA disabled and one converter requested; no partition decrease or force deletion');return
 policy.validate_plan(plan,a.plan_sha256,observations,proof,candidate)
 if a.action=='candidate':
  require(a.output,'New private candidate output required');private_write(a.output,policy.activation_candidate(candidate,plan));print('Prepared candidate only; use candidate PR and approved CD');return
 require(a.execute_reviewed_change,'Explicit post-merge live execution approval required');k=Live(a.kubeconfig);identity(k,candidate)
 # Live execution rechecks two fresh, quiescent observations. No offset/data mutation APIs exist here.
 require(a.client_jar and a.execute_reviewed_observation and a.traffic_quiet_confirmed,'Fresh quiet-window observation approval required')
 fresh=[sample(k,candidate,a.client_jar)];time.sleep(5);fresh.append(sample(k,candidate,a.client_jar));fresh_plan=policy.prepare(fresh,proof,candidate,True)
 require(fresh_plan['approvedRef']==plan['approvedRef'] and fresh_plan['topics']==plan['topics'],'Refresh plan after approved Snapshot/Topic changes')
 if a.action=='expand':
  require(plan['currentPartitions']==1,'Only 1 to 2 expansion permitted; never decrease')
  topic=k.get('kafkatopic','kafka',next(iter(policy.TOPICS)));name=topic['metadata']['annotations']['meta.helm.sh/release-name']
  require(name=='yeodam-v3-kafka-access','Only the existing business Topic release may expand')
  v=json.loads(helm(['get','values',name,'-n','kafka','--kubeconfig',k.config,'-o','json']));before=records(helm(['template',name,str(ROOT/'deploy/v3/kafka-access'),'-n','kafka','-f','-'],json.dumps(v)));v.update(imagePartitions=2,imagePartitionExpansionReviewed=True);after=records(helm(['template',name,str(ROOT/'deploy/v3/kafka-access'),'-n','kafka','-f','-'],json.dumps(v)))
  live=records(helm(['get','manifest',name,'-n','kafka','--kubeconfig',k.config]));require(index(before)==index(live),'Deployed Topic release differs from reviewed Chart; stop rather than overwrite drift')
  policy.review_partition_change(before,after)
  cdc_state(k,plan['connectorUid'],'stopped')
  helm(['upgrade',name,str(ROOT/'deploy/v3/kafka-access'),'-n','kafka','--kubeconfig',k.config,'-f','-'],json.dumps(v));
  for _ in range(60):
   topics=[k.get('kafkatopic','kafka',n)for n in sorted(policy.TOPICS)]
   if all(t['spec']['partitions']==2 and t.get('status',{}).get('observedGeneration',0)>=t['metadata']['generation'] and any(c['type']=='Ready'and c['status']=='True'for c in t.get('status',{}).get('conditions',[]))for t in topics):break
   time.sleep(1)
  else:raise ValueError('Partial expansion; CDC stays stopped for review. Never decrease partitions.')
  policy.kafka_quiet(probe(k,a.client_jar),2)
  cdc_state(k,plan['connectorUid'],'running')
  print('Four Topics expanded; CDC restarted with retained offsets/history and fresh producer metadata. Collect new two-partition proof before activation.');return
 v=release_values(k);require(v.get('apiEnabled')is True and v.get('converterAccessEnabled')is True,'Preserve API HPAs and preprovisioned access')
 if a.action=='activate':
  require(plan['currentPartitions']==2,'Ready two-partition evidence required')
  dep=k.get('deployment','photo','photo-converter');pod=dep['spec']['template']['spec'];affinity={'podAntiAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':[{'labelSelector':{'matchLabels':{'app.kubernetes.io/instance':'photo-converter'}},'topologyKey':'kubernetes.io/hostname'}]}}
  require(pod.get('affinity')==affinity,'Approved hostname separation required before scaling')
  v.update(converterEnabled=True,converterReview={'partitions':2,'keyOrderingReviewed':True,'rebalanceAndShutdownVerified':True})

  prior=release_values(k);before=index(records(helm(['template',RELEASE,str(CHART),'-n','observability','-f','-'],json.dumps(prior))));after=index(records(helm(['template',RELEASE,str(CHART),'-n','observability','-f','-'],json.dumps(v))))
  require(index(records(helm(['get','manifest',RELEASE,'-n','observability','--kubeconfig',k.config])))==before,'Scaling release drift; do not overwrite')
  added=('ScaledObject','photo','photo-converter');require(set(after)-set(before)=={added} and all(after[key]==obj for key,obj in before.items()),'Activation may only add the bounded converter ScaledObject')
 else:raise ValueError('Unsupported activation state')
 helm(['upgrade',RELEASE,str(CHART),'-n','observability','--kubeconfig',k.config,'-f','-','--wait','--timeout','180s'],json.dumps(v))
 print(json.dumps({'converterEnabled':a.action=='activate','partitionsPreserved':2,'maxReplicas':2,'gpuChanged':False}))
if __name__=='__main__':main()
