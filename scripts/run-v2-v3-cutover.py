#!/usr/bin/env python3
"""Explicit, receipt-gated V3 maintenance. No operation is invoked on import or without --execute."""
import argparse,base64,gzip,hashlib,importlib.util,json,os,shutil,socket,subprocess,tempfile,time,uuid
from contextlib import contextmanager
from pathlib import Path
import v2_v3_cutover as c
import qdrant_migration as q

ROOT=c.ROOT
MYSQL_SHELL='export MYSQL_PWD="$(cat /credentials/root-password)"; exec mysql --no-defaults --protocol=SOCKET --socket=/tmp/mysql.sock -uroot --batch --raw --skip-column-names'

class Kube:
 def __init__(self,config):
  self.prefix=['kubectl','--kubeconfig',str(config),'--context',c.POLICY['cluster'],'--request-timeout=30s']
  context=self.call('config','get-contexts',c.POLICY['cluster'],'-o','name').decode().strip()
  c.require(context==c.POLICY['cluster'],'Only the fixed V3 context is allowed')
  r=subprocess.run(['aws','--profile','yeodam-admin','--region','ap-northeast-2','eks','describe-cluster','--name','yeodam-v3-foundation'],capture_output=True,text=True,timeout=60)
  c.require(r.returncode==0,'Could not verify the fixed AWS cluster')
  cluster=json.loads(r.stdout)['cluster']
  server=self.call('config','view','--minify','-o','jsonpath={.clusters[0].cluster.server}').decode()
  c.require(cluster['arn']==c.POLICY['cluster'] and server==cluster['endpoint'],'Kubeconfig endpoint differs from the approved AWS cluster')
 def call(self,*args,source=None,limit=256*1024*1024):
  r=subprocess.run(self.prefix+list(args),input=source,capture_output=True,timeout=600)
  c.require(r.returncode==0 and len(r.stdout)<=limit,'Kubernetes operation failed; private response suppressed')
  return r.stdout
 def get(self,ns,kind,name=None):return json.loads(self.call('-n',ns,'get',kind,*([name] if name else []),'-o','json'))
 def apply(self,value):return self.call('apply','-f','-',source=c.canonical(value))
 def patch(self,ns,kind,name,original,operations):
  tests=[{'op':'test','path':'/metadata/uid','value':original['metadata']['uid']},{'op':'test','path':'/metadata/resourceVersion','value':original['metadata']['resourceVersion']}]
  self.call('-n',ns,'patch',kind,name,'--type=json','-p',json.dumps(tests+operations))
 def execute(self,ns,pod,container,command,source=None):return self.call('-n',ns,'exec','-i',pod,'-c',container,'--',*command,source=source)
 def primary(self):
  name=self.get('mysql','service','mysql-primary')['spec']['selector']['statefulset.kubernetes.io/pod-name']
  c.require(name in ['mysql-0','mysql-1'],'Unreviewed MySQL primary');return name
 def sql(self,query):return self.execute('mysql',self.primary(),'mysql',['/bin/bash','-ec',MYSQL_SHELL],query.encode()).decode().strip()
 def scale(self,ns,name,count):
  d=self.get(ns,'deployment',name);c.require(count in [0,1],'Preserve minimum one-replica app budget')
  self.patch(ns,'deployment',name,d,[{'op':'replace','path':'/spec/replicas','value':count}])
 def wait(self,predicate,message,attempts=90):
  for _ in range(attempts):
   if predicate():return
   time.sleep(2)
  raise RuntimeError(message)
 def apps_stopped(self):
  for name,u in c.UNITS.items():
   d=self.get(u['namespace'],'deployment',name)
   if d['spec'].get('replicas',1)!=0 or d.get('status',{}).get('replicas',0)!=0:return False
   selectors=','.join(k+'='+v for k,v in sorted(d['spec']['selector']['matchLabels'].items()))
   pods=json.loads(self.call('-n',u['namespace'],'get','pods','-l',selectors,'-o','json'))
   if pods['items']:return False
  return True

@contextmanager
def forward(k,ns,target,port):
 with socket.socket() as s:s.bind(('127.0.0.1',0));local=s.getsockname()[1]
 with tempfile.TemporaryFile() as log:
  p=subprocess.Popen(k.prefix+['-n',ns,'port-forward',target,str(local)+':'+str(port),'--address','127.0.0.1'],stdout=log,stderr=log)
  try:
   for _ in range(60):
    c.require(p.poll() is None,'Owned port-forward exited')
    try:
     with socket.create_connection(('127.0.0.1',local),timeout=.3):break
    except OSError:time.sleep(.5)
   else:raise RuntimeError('Owned port-forward startup timed out')
   yield local
  finally:
   p.terminate()
   try:p.wait(timeout=10)
   except subprocess.TimeoutExpired:p.kill();p.wait()

@contextmanager
def qdrant(k,ns_target='service/qdrant'):
 # This exact Secret read/copy is part of the concrete execution approval, never done in CI.
 raw=k.call('-n','qdrant','get','secret','qdrant-bootstrap','-o','go-template={{index .data \"api-key\"}} {{index .data \"ca.crt\"}}').decode().split()
 c.require(len(raw)==2,'Qdrant API key/public CA fields missing')
 secret={'api-key':raw[0],'ca.crt':raw[1]}
 with tempfile.TemporaryDirectory(prefix='602-public-ca-') as d:
  ca=Path(d)/'ca.crt';ca.write_bytes(base64.b64decode(secret['ca.crt']));ca.chmod(0o600)
  key=base64.b64decode(secret['api-key']).decode()
  with forward(k,'qdrant',ns_target,6333) as port:yield q.Client(port,key=key,ca=ca)

def capture(k,shape=None):return c.mysql.capture('',shape,query_fn=k.sql)
def history_hash(k):return hashlib.sha256(k.sql('SELECT * FROM yeodam.flyway_schema_history ORDER BY installed_rank;').encode()).hexdigest()
def redis_primary(k,namespace):
 if namespace=='ai-redis':return 'ai-redis-0'
 command="source /runtime/common.sh; wait_primary"
 host=k.execute('redis','redis-0','redis',['/bin/bash','-ec',command]).decode().strip()
 c.require(host in ['redis-0.redis-members.redis.svc.cluster.local','redis-1.redis-members.redis.svc.cluster.local'],'Unknown Sentinel primary')
 return host.split('.')[0]
def redis_action(k,namespace,action):
 return k.execute(namespace,redis_primary(k,namespace),'redis' if namespace=='redis' else 'ai-redis',['/bin/bash','-s','--',action],(ROOT/'scripts/cutover/redis.sh').read_bytes())
def mongo_action(k,action):
 script=(ROOT/'scripts/cutover/mongo.js').read_text()
 return k.execute('mongodb','mongodb-0','mongodb',['env','CUTOVER_ACTION='+action,'mongosh','--quiet','--norc','--eval',script])

def capacity_snapshot(k):
 pools={name:k.get('default','nodepool',name) for name in c.POLICY['heldNodePools']}
 for name,pool in pools.items():
  c.require(pool['spec']['template']['spec']['expireAfter']=='Never','Review expiring NodeClaims before maintenance')
 claims={x['metadata']['uid']:x for x in k.get('default','nodeclaims')['items'] if x['metadata'].get('labels',{}).get('karpenter.sh/nodepool') in pools}
 c.require({x['metadata']['labels']['karpenter.sh/nodepool'] for x in claims.values()}==set(pools),'Both existing app pools must have capacity before pause')
 for claim in claims.values():
  c.require(not claim['metadata'].get('deletionTimestamp'),'Wait for an active Node deletion before migration')
  name=claim.get('status',{}).get('nodeName');c.require(name,'NodeClaim is not initialized')
  node=k.get('default','node',name)
  c.require(not node['metadata'].get('deletionTimestamp') and not any(t['key']=='karpenter.sh/disrupted' for t in node['spec'].get('taints',[])),'Node is already being disrupted')
 return {'pools':pools,'claimUids':sorted(claims)}

def hold_capacity(k,baseline):
 for name,pool in baseline['pools'].items():
  k.patch('default','nodepool',name,pool,[{'op':'replace','path':'/spec/disruption/budgets','value':[{'nodes':'0'}]}])

def restore_capacity(k,baseline):
 claims=sorted(x['metadata']['uid'] for x in k.get('default','nodeclaims')['items'] if x['metadata'].get('labels',{}).get('karpenter.sh/nodepool') in c.POLICY['heldNodePools'])
 c.require(claims==baseline['claimUids'],'App capacity changed during migration; review before restoring disruption')
 for name,original in baseline['pools'].items():
  current=k.get('default','nodepool',name)
  c.require(current['metadata']['uid']==original['metadata']['uid'] and current['spec']['disruption']['budgets']==[{'nodes':'0'}],'NodePool maintenance hold changed')
  k.patch('default','nodepool',name,current,[{'op':'replace','path':'/spec/disruption/budgets','value':original['spec']['disruption']['budgets']}])

def pause(k,directory,plan):
 application=k.get('argocd','application','yeodam-v3-apps')
 c.require(application.get('status',{}).get('operationState',{}).get('phase')!='Running','Wait for the current Argo operation')
 schema=k.get('mysql','job','v3-mysql-schema')
 spec=schema['spec']['template']['spec'];c.require(len(spec['containers'])==1 and spec['automountServiceAccountToken'] is False and spec['nodeSelector']=={'yeodam.io/role':'system'} and spec['containers'][0]['image']==plan['schemaImage'] and schema['spec'].get('suspend') is True,'Review fixed schema Job before maintenance')
 k.get('qdrant','persistentvolumeclaim','data-qdrant-0')
 k.get('mongodb','pod','mongodb-0')
 baseline={'capacity':capacity_snapshot(k),'schemaJob':schema,'application':application,'deployments':{},'connector':k.get('kafka','kafkaconnector','yeodam-v3-outbox')}
 for name,u in c.UNITS.items():
  d=k.get(u['namespace'],'deployment',name);c.require(d['spec'].get('replicas')==1,'Unexpected app replica count');baseline['deployments'][name]=d
 c.write(directory/'before-pause.json',baseline)
 hold_capacity(k,baseline['capacity'])
 if 'automated' in application.get('spec',{}).get('syncPolicy',{}):
  k.patch('argocd','application','yeodam-v3-apps',application,[{'op':'remove','path':'/spec/syncPolicy/automated'}])
 connector=baseline['connector'];k.patch('kafka','kafkaconnector','yeodam-v3-outbox',connector,[{'op':'add','path':'/spec/state','value':'stopped'}])
 for name,u in c.UNITS.items():k.scale(u['namespace'],name,0)
 k.wait(k.apps_stopped,'App Pods did not stop')
 jobs=c.audit_jobs(k.sql);c.require(all(x['unresolved']==0 for x in jobs.values()),'QA jobs changed before writer stop; recover QA instead of replacing data')
 c.require(int(redis_action(k,'ai-redis','queue-count'))==0,'QA AI work remains after stopping writers')
 spec=importlib.util.spec_from_file_location('paused_audit602',ROOT/'scripts/collect-v2-v3-cutover.py');audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
 c.require(all(lag==0 for lag in audit.kafka_audit(k).values()),'QA Kafka work arrived before writer stop; do not import yet')
 def stopped():
  d=k.get('kafka','kafkaconnector','yeodam-v3-outbox');s=d.get('status',{})
  return s.get('observedGeneration')==d['metadata']['generation'] and s.get('connectorStatus',{}).get('connector',{}).get('state')=='STOPPED'
 k.wait(stopped,'CDC did not stop')
 return {'verified':True,'appPodsStopped':True,'cdcStopped':True,'argoAutomaticSyncSuspended':True}

def qa_backup(k,directory):
 c.require(k.apps_stopped(),'QA writers must be stopped')
 target=directory/'qa';target.mkdir(mode=0o700)
 tables=capture(k);history=history_hash(k)
 dump_command='export MYSQL_PWD="$(cat /credentials/root-password)"; exec mysqldump --no-defaults --protocol=SOCKET --socket=/tmp/mysql.sock -uroot '+' '.join(c.mysql.DUMP_OPTIONS)+' yeodam'
 raw=k.execute('mysql',k.primary(),'mysql',['/bin/bash','-ec',dump_command])
 with (target/'yeodam.sql.gz').open('xb') as f:f.write(gzip.compress(raw,mtime=0))
 (target/'yeodam.sql.gz').chmod(0o600);del raw
 files={'yeodam.sql.gz':c.mysql.file_digest(target/'yeodam.sql.gz')}
 for ns in ['redis','ai-redis']:
  data=redis_action(k,ns,'backup');c.require(data.startswith(b'REDIS') and len(data)>16,'Invalid Redis backup')
  path=target/(ns+'.rdb');path.write_bytes(data);path.chmod(0o600);files[path.name]=c.mysql.file_digest(path)
 data=mongo_action(k,'backup');c.require(len(data)<=32*1024*1024 and isinstance(json.loads(data),list),'Mongo backup must be bounded EJSON')
 path=target/'mongodb.ejson';path.write_bytes(data);path.chmod(0o600);files[path.name]=c.mysql.file_digest(path)
 with qdrant(k) as client:
  collections=client.request('GET','/collections')['collections']
  c.require(all(x['name']==q.COLLECTION for x in collections),'Review additional QA collections before migration')
  before=None
  if collections:
   before=q.fingerprint(client,allow_empty=True);snapshot=client.request('POST',q.BASE+'/snapshots?wait=true')
   client.download(snapshot['name'],target/'qdrant.snapshot',snapshot['size'],snapshot['checksum'])
   c.require(q.fingerprint(client,allow_empty=True)==before,'QA Qdrant changed during backup')
  else:(target/'qdrant.snapshot').write_bytes(b'');(target/'qdrant.snapshot').chmod(0o600)
 files['qdrant.snapshot']=c.mysql.file_digest(target/'qdrant.snapshot')
 c.require(capture(k)==tables and history_hash(k)==history,'QA DB changed during backup')
 proof={'version':1,'files':files,'tables':tables,'historySha256':history,'qdrant':before,'createdAt':c.now()}
 c.write(target/'backup.json',proof)
 return {'verified':True,'backupManifestSha256':c.digest(proof),'files':len(files),'qaDataPreserved':True}

def qa_proof(directory):
 path=directory/'qa';proof=c.load(path/'backup.json')
 completed=c.load(directory/'qa-backup.json')
 c.require(completed.get('result',{}).get('backupManifestSha256')==c.digest(proof),'QA manifest differs from the completed backup receipt')
 c.require(set(proof['files'])=={'yeodam.sql.gz','redis.rdb','ai-redis.rdb','mongodb.ejson','qdrant.snapshot'},'Complete QA archive set required')
 for name,sha in proof['files'].items():
  c.require((path/name).is_file() and not (path/name).is_symlink(),'Regular QA archive required')
  c.require(name in ['yeodam.sql.gz','redis.rdb','ai-redis.rdb','mongodb.ejson','qdrant.snapshot'] and c.mysql.file_digest(path/name)==sha,'QA backup bytes changed')
 return proof

def mysql_restore(k,directory,plan):
 qa_proof(directory);c.require(k.apps_stopped(),'Do not replace a live database')
 source=Path(plan['mysqlPackage']);manifest=c.mysql.verify_package(source)
 # Never import mysql.* accounts, SOURCE GTIDs or old replication configuration.
 k.sql('DROP DATABASE yeodam; CREATE DATABASE yeodam CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;')
 with gzip.open(source/'yeodam.sql.gz','rb') as f:raw=f.read(c.mysql.MAX_SQL+1)
 c.require(len(raw)<=c.mysql.MAX_SQL,'Oversized source dump')
 k.execute('mysql',k.primary(),'mysql',['/bin/bash','-ec',MYSQL_SHELL+' yeodam'],raw)
 c.require(capture(k,manifest['tables'])==manifest['tables'],'V2 legacy row hashes differ after import')
 baseline=c.mysql.upgrade_baseline(k.sql,manifest['tables']);c.write(directory/'mysql-upgrade-baseline.json',baseline)
 # Run the approved pinned V40 Job using its already-reviewed image/spec.
 job=c.load(directory/'before-pause.json')['schemaJob'];meta={'name':'v3-cutover-schema-602','namespace':'mysql','labels':{'yeodam.io/stage':'v3','yeodam.io/issue':'602'}}
 spec=job['spec'];template=spec['template'];template['metadata']={'labels':{'app.kubernetes.io/instance':'mysql-schema','app.kubernetes.io/part-of':'yeodam','yeodam.io/stage':'v3','yeodam.io/issue':'602'}}
 new={'apiVersion':'batch/v1','kind':'Job','metadata':meta,'spec':{'backoffLimit':0,'activeDeadlineSeconds':600,'template':template}}
 k.apply(new)
 try:k.call('-n','mysql','wait','--for=condition=complete','job/v3-cutover-schema-602','--timeout=600s')
 finally:k.call('-n','mysql','delete','job','v3-cutover-schema-602','--wait=true')
 c.require(k.sql('SELECT CONCAT(COUNT(*),\':\',SUM(success),\':\',MAX(CAST(version AS UNSIGNED))) FROM yeodam.flyway_schema_history;')=='40:40:40','V40 migration failed')
 c.mysql.verify_upgrade(k.sql,baseline)
 c.write(directory/'mysql-restored-tables.json',capture(k))
 # Same fixed FK verifier, executed through the existing primary connection.
 failures=0
 relations=k.sql("SELECT table_name,column_name,referenced_table_name,referenced_column_name FROM information_schema.key_column_usage WHERE table_schema='yeodam' AND referenced_table_name IS NOT NULL;").splitlines()
 for row in relations:
  table,col,parent,key=row.split('\t');ids=[c.mysql.identifier(x) for x in [table,col,parent,key]]
  failures+=int(k.sql(f'SELECT COUNT(*) FROM yeodam.{ids[0]} c LEFT JOIN yeodam.{ids[2]} p ON c.{ids[1]}=p.{ids[3]} WHERE c.{ids[1]} IS NOT NULL AND p.{ids[3]} IS NULL;'))
 c.require(failures==0,'Foreign key orphans after import')
 # The import/migration must replicate before apps are allowed to start.
 primary=k.primary();replica='mysql-1' if primary=='mysql-0' else 'mysql-0'
 gtid=k.sql('SELECT @@GLOBAL.gtid_executed;').replace('\n','');c.require(__import__('re').fullmatch('[a-f0-9:,-]+',gtid),'Invalid target GTID format')
 result=k.execute('mysql',replica,'mysql',['/bin/bash','-ec',MYSQL_SHELL],("SELECT WAIT_FOR_EXECUTED_GTID_SET('"+gtid+"',30);").encode()).decode().strip()
 c.require(result=='0','Replica did not apply the restored operating data')
 return {'verified':True,'replicaCaughtUp':True,'legacyTablesCompared':len(manifest['tables']),'foreignKeyViolations':0,'schemaVersion':40,'reviewedDataChangesVerified':True}

def qdrant_restore(k,directory,plan):
 qa_proof(directory);c.require(k.apps_stopped(),'Do not restore Qdrant while writers run')
 package=Path(plan['qdrantPackage']);manifest=q.verify_package(package)
 stats=k.execute('qdrant','qdrant-0','qdrant',['/bin/sh','-ec','df -Pk /data']).decode().strip().splitlines()[-1].split()
 c.require(len(stats)>=6 and stats[3].isdigit() and int(stats[3])*1024>=manifest['snapshotBytes']*3,'Insufficient Qdrant PVC headroom')
 sts=k.get('qdrant','statefulset','qdrant');c.require(sts['spec']['replicas']==1,'Exactly one Qdrant member required')
 k.patch('qdrant','statefulset','qdrant',sts,[{'op':'replace','path':'/spec/replicas','value':0}])
 k.wait(lambda:not k.get('qdrant','pods')['items'],'Wait for Qdrant Pod to release RWOP volume')
 name='v3-qdrant-restore-602'
 pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':'qdrant','labels':{'yeodam.io/issue':'602'}},'spec':{'restartPolicy':'Never','automountServiceAccountToken':False,'nodeSelector':{'yeodam.io/role':'data'},'tolerations':[{'key':'yeodam.io/role','operator':'Equal','value':'data','effect':'NoSchedule'}],'securityContext':{'runAsUser':1000,'runAsGroup':1000,'fsGroup':1000,'runAsNonRoot':True,'seccompProfile':{'type':'RuntimeDefault'}},'containers':[{'name':'restore','image':q.IMAGE,'command':['/bin/sh','-ec','while [ ! -f /data/cutover-602.ready ]; do sleep 1; done; mkdir -p /data/cutover-602; mv /data/storage /data/cutover-602/qa-storage; mkdir -p /data/storage/collections /data/tmp; exec /qdrant/qdrant --config-path /runtime/qdrant.yaml --snapshot /data/cutover-602.snapshot:'+q.COLLECTION],'env':[{'name':'QDRANT__SERVICE__API_KEY','valueFrom':{'secretKeyRef':{'name':'qdrant-bootstrap','key':'api-key'}}}],'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'500m','memory':'512Mi'}},'securityContext':{'readOnlyRootFilesystem':True,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},'volumeMounts':[{'name':'data','mountPath':'/data'},{'name':'runtime','mountPath':'/runtime','readOnly':True},{'name':'credentials','mountPath':'/credentials','readOnly':True},{'name':'tmp','mountPath':'/tmp'}]}],'volumes':[{'name':'data','persistentVolumeClaim':{'claimName':'data-qdrant-0'}},{'name':'runtime','configMap':{'name':'qdrant-runtime'}},{'name':'credentials','secret':{'secretName':'qdrant-bootstrap','defaultMode':288}},{'name':'tmp','emptyDir':{'sizeLimit':'16Mi'}}]}}
 k.call('create','-f','-',source=c.canonical(pod))
 try:
  k.wait(lambda:k.get('qdrant','pod',name).get('status',{}).get('phase')=='Running','Restore Pod startup timeout')
  command="test ! -e /data/cutover-602 && test ! -e /data/cutover-602.ready && cat > /data/cutover-602.snapshot && printf '%s  /data/cutover-602.snapshot\\n' "+manifest["snapshotSha256"]+" | sha256sum -c - >/dev/null && touch /data/cutover-602.ready"
  k.execute('qdrant',name,'restore',['/bin/sh','-ec',command],(package/'collection.snapshot').read_bytes())
  with qdrant(k,'pod/'+name) as client:
   k.wait(lambda:_qdrant_ready(client),'Qdrant recovery timeout')
   aliases=manifest['fingerprint']['aliases']
   if aliases:client.request('POST','/collections/aliases',{'actions':[{'create_alias':a} for a in aliases]})
   c.require(q.fingerprint(client)==manifest['fingerprint'],'Restored Qdrant differs')
 finally:
  k.call('-n','qdrant','delete','pod',name,'--wait=true')
 # Keep old QA storage on the PVC as a rollback copy, in addition to the private snapshot.
 sts=k.get('qdrant','statefulset','qdrant');k.patch('qdrant','statefulset','qdrant',sts,[{'op':'replace','path':'/spec/replicas','value':1}])
 k.call('-n','qdrant','rollout','status','statefulset/qdrant','--timeout=300s')
 with qdrant(k) as client:c.require(q.fingerprint(client)==manifest['fingerprint'],'Normal Qdrant did not reopen restored data')
 k.execute('qdrant','qdrant-0','qdrant',['/bin/sh','-ec','rm -f /data/cutover-602.snapshot /data/cutover-602.ready'])
 return {'verified':True,'copiedSnapshotRemoved':True,'pointCount':manifest['fingerprint']['count'],'oldQaStorageRetained':True,'temporaryPodRemoved':True}

def _qdrant_ready(client):
 try:return client.request('GET',q.BASE).get('status')=='green'
 except Exception:return False

def ephemeral_reset(k,directory):
 qa_proof(directory);c.require(k.apps_stopped(),'Do not clear active QA state')
 for ns in ['redis','ai-redis']:c.require(redis_action(k,ns,'clear').strip()==b'CLEARED','Redis state not cleared')
 c.require(mongo_action(k,'clear').strip()==b'CLEARED','MongoDB state not cleared')
 return {'verified':True,'freshLogin':True,'qaCachesAndMongoArchived':True}

def verify(k,directory,plan):
 c.require(k.apps_stopped(),'Apps must remain stopped until storage verification')
 c.mysql.verify_package(Path(plan['mysqlPackage']));c.mysql.verify_upgrade(k.sql,c.load(directory/'mysql-upgrade-baseline.json'));c.require(capture(k)==c.load(directory/'mysql-restored-tables.json'),'Restored DB changed before resume')
 c.validate_reconciliation(plan['reconciliation'])
 with qdrant(k) as client:c.require(q.fingerprint(client)==q.verify_package(Path(plan['qdrantPackage']))['fingerprint'],'Qdrant changed before resume')
 return {'verified':True,'storageReady':True,'appsRemainStopped':True,'dnsUnchanged':True}

def resume(k,directory):
 # Storage restore is already verified. Only restore the original minimum replica counts/sync policy.
 before=c.load(directory/'before-pause.json')
 connector=k.get('kafka','kafkaconnector','yeodam-v3-outbox')
 c.require(connector['spec'].get('config',{}).get('topic.prefix')==c.POLICY['cdcPrefix'],'Apply the reviewed fresh operating CDC profile first')
 c.require(connector['spec']['config']['schema.history.internal.kafka.topic']==c.POLICY['cdcHistory'],'Use separate operating schema history')
 k.patch('kafka','kafkaconnector','yeodam-v3-outbox',connector,[{'op':'add','path':'/spec/state','value':'running'}])
 for name,u in c.UNITS.items():k.scale(u['namespace'],name,1)
 app=k.get('argocd','application','yeodam-v3-apps')
 old=before['application']['spec'].get('syncPolicy',{}).get('automated')
 if old is not None:k.patch('argocd','application','yeodam-v3-apps',app,[{'op':'add','path':'/spec/syncPolicy/automated','value':old}])
 def ready():
  for name,u in c.UNITS.items():
   d=k.get(u['namespace'],'deployment',name)
   if d.get('status',{}).get('readyReplicas')!=1 or d['status'].get('observedGeneration')!=d['metadata']['generation']:return False
  return True
 k.wait(ready,'Minimum app replicas did not become Ready; retain the NodePool maintenance hold',attempts=300)
 restore_capacity(k,before['capacity'])
 return {'verified':True,'minimumReplicasRestored':True,'existingNodeClaimsPreserved':True,'nodePoolBudgetsRestored':True,'approvedCdStillRequired':True}

def upgrade(k,release,chart,extra):
 # Existing release public values only: these charts contain public truststores/Secret names, never passwords.
 prefix=['helm','--kubeconfig',k.prefix[2],'--kube-context',c.POLICY['cluster'],'-n','default']
 r=subprocess.run(prefix+['get','values',release,'-o','json'],capture_output=True,text=True,timeout=60)
 c.require(r.returncode==0,'Could not read the existing public Helm values')
 values=json.loads(r.stdout) or {};values.update(extra)
 with tempfile.TemporaryDirectory(prefix='602-public-values-') as tmp:
  path=Path(tmp)/'values.json';c.write(path,values)
  command=prefix+['upgrade',release,str(ROOT/'deploy/v3'/chart),'-f',str(path),'--wait','--timeout','300s','--history-max','5']
  r=subprocess.run(command,capture_output=True,timeout=360)
  c.require(r.returncode==0,'Scoped Helm upgrade failed; private output suppressed')
 return values

def operating_config(k,directory):
 c.require(k.apps_stopped(),'Keep apps stopped while preparing operating configuration')
 existing=k.call('-n','kafka','get','kafkatopic',c.POLICY['cdcHistory'],'--ignore-not-found','-o','json')
 c.require(not existing.strip(),'Operating history already exists; review the failed prior attempt instead of reusing offsets')
 upgrade(k,'yeodam-v3-kafka-access','kafka-access',{'operating':True})
 upgrade(k,'yeodam-v3-outbox','outbox',{'operating':True,'connectorEnabled':True,'connectorState':'stopped'})
 k.call('-n','kafka','wait','--for=condition=Ready','kafkatopic/'+c.POLICY['cdcHistory'],'--timeout=180s')
 k.call('-n','kafka','wait','--for=condition=Ready','kafkatopic/v3-operating-outbox-heartbeat','--timeout=180s')
 k.wait(lambda:k.get('kafka','kafkauser','v3-connect-outbox').get('status',{}).get('observedGeneration')==k.get('kafka','kafkauser','v3-connect-outbox')['metadata']['generation'],'Operating CDC ACL did not converge')
 # Preserve current V3 SG and ALB; replace only the domain/certificate on existing ingress resources.
 upgrade(k,'yeodam-v3-external-access','external-access',{'host':c.POLICY['domain'],'certificateArn':c.POLICY['certificateArn'],'backendRoutesEnabled':True})
 connector=k.get('kafka','kafkaconnector','yeodam-v3-outbox')
 c.require(connector['spec']['state']=='stopped' and connector['spec']['config']['topic.prefix']==c.POLICY['cdcPrefix'],'Fresh operating CDC must remain stopped')
 return {'verified':True,'qaOffsetsPreserved':True,'separateOperatingHistory':True,'newAlbs':0,'dnsUnchanged':True}

def rollback_qa(k,directory):
 # After public writes begin, recovery must preserve those new writes; never blindly restore QA.
 c.require(not any((directory/(p+'.json')).exists() for p in ['resume','dns','accept']),'QA rollback forbidden after app resume or public writes')
 c.require(k.apps_stopped(),'Stop all apps before QA rollback')
 proof=qa_proof(directory);target=directory/'qa'
 k.sql('DROP DATABASE yeodam; CREATE DATABASE yeodam CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;')
 with gzip.open(target/'yeodam.sql.gz','rb') as f:raw=f.read(c.mysql.MAX_SQL+1)
 c.require(len(raw)<=c.mysql.MAX_SQL,'Oversized QA dump')
 k.execute('mysql',k.primary(),'mysql',['/bin/bash','-ec',MYSQL_SHELL+' yeodam'],raw)
 c.require(capture(k)==proof['tables'] and history_hash(k)==proof['historySha256'],'QA DB rollback comparison failed')
 script=(ROOT/'scripts/cutover/mongo.js').read_text()
 result=k.execute('mongodb','mongodb-0','mongodb',['env','CUTOVER_ACTION=restore','mongosh','--quiet','--norc','--eval',script],(target/'mongodb.ejson').read_bytes())
 c.require(result.strip()==b'RESTORED','QA Mongo rollback failed')
 # Restore the retained full QA storage without ever opening an app writer.
 sts=k.get('qdrant','statefulset','qdrant')
 if sts['spec']['replicas']!=0:k.patch('qdrant','statefulset','qdrant',sts,[{'op':'replace','path':'/spec/replicas','value':0}])
 k.wait(lambda:not k.get('qdrant','pods')['items'],'Qdrant Pod must release its PVC before rollback')
 name='v3-qdrant-rollback-602';spec=sts['spec']['template']['spec']
 spec['restartPolicy']='Never';spec['containers'][0]['command']=['/bin/sh','-ec',"if [ -d /data/cutover-602/qa-storage ]; then test ! -e /data/cutover-602/operating-storage; mv /data/storage /data/cutover-602/operating-storage; mv /data/cutover-602/qa-storage /data/storage; fi; exec /qdrant/qdrant --config-path /runtime/qdrant.yaml"]
 for container in spec['containers']:
  for key in ['startupProbe','livenessProbe','readinessProbe']:container.pop(key,None)
 spec['volumes'].append({'name':'data','persistentVolumeClaim':{'claimName':'data-qdrant-0'}})
 pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':'qdrant','labels':{'yeodam.io/issue':'602'}},'spec':spec}
 k.call('create','-f','-',source=c.canonical(pod))
 try:
  k.wait(lambda:k.get('qdrant','pod',name).get('status',{}).get('phase')=='Running','Rollback Pod startup timeout')
  with qdrant(k,'pod/'+name) as client:
   k.wait(lambda:client.request('GET','/').get('version')==q.VERSION,'Rollback Qdrant startup timeout')
   if proof['qdrant'] is None:c.require(client.request('GET','/collections')['collections']==[],'QA empty collection state differs')
   else:c.require(q.fingerprint(client,allow_empty=True)==proof['qdrant'],'QA Qdrant rollback comparison failed')
 finally:k.call('-n','qdrant','delete','pod',name,'--wait=true')
 sts=k.get('qdrant','statefulset','qdrant');k.patch('qdrant','statefulset','qdrant',sts,[{'op':'replace','path':'/spec/replicas','value':1}])
 # Sessions/leases stay fresh; no pending QA jobs were allowed into this migration.
 for ns in ['redis','ai-redis']:c.require(redis_action(k,ns,'clear').strip()==b'CLEARED','Fresh QA cache reset failed')
 return {'verified':True,'mysqlRestored':True,'mongoRestored':True,'qdrantRestored':True,'redisRequiresFreshLogin':True,'appsRemainStopped':True,'automaticResume':False}

ACTIONS={'pause':pause,'qa-backup':lambda k,d,p:qa_backup(k,d),'mysql-restore':mysql_restore,'qdrant-restore':qdrant_restore,'ephemeral-reset':lambda k,d,p:ephemeral_reset(k,d),'verify':verify,'resume':lambda k,d,p:resume(k,d),'operating-config':lambda k,d,p:operating_config(k,d)}
def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute',action='store_true',required=True)
 for n in ['plan','directory','kubeconfig']:parser.add_argument('--'+n,type=Path,required=True)
 parser.add_argument('--approve-sha256',required=True);parser.add_argument('--phase',choices=list(ACTIONS)+['rollback-qa'],required=True)
 a=parser.parse_args();os.umask(0o077);plan=c.load(a.plan);sha=c.verify_plan(plan,a.approve_sha256)
 a.directory.mkdir(mode=0o700,exist_ok=True);c.require(not a.directory.is_symlink() and a.directory.stat().st_mode&0o077==0,'Private run directory required')
 if a.phase=='rollback-qa':
  c.require(not (a.directory/'rollback-qa.json').exists(),'Rollback already completed')
  result=rollback_qa(Kube(a.kubeconfig),a.directory);c.write(a.directory/'rollback-qa.json',{'planSha256':sha,'result':result});print('QA storage restored and compared; apps/CDC remain stopped for recovery review');return
 prior=c.prior_receipts(a.directory,a.phase,sha)
 attempt=a.directory/(a.phase+'-attempt.json')
 c.require(not attempt.exists(),'A prior attempt needs recovery review before repeating writes')
 c.write(attempt,{'phase':a.phase,'planSha256':sha,'startedAt':c.now()})
 try:
  result=ACTIONS[a.phase](Kube(a.kubeconfig),a.directory,plan);c.receipt(a.directory,a.phase,sha,prior,result)
  print(json.dumps({'phase':a.phase,'completed':True,'planSha256':sha}))
 except Exception:raise SystemExit('Cutover stopped; do not resume automatically. Inspect private phase evidence; no user data or credentials printed.') from None
if __name__=='__main__':main()
