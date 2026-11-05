#!/usr/bin/env python3
"""Synthetic-only CDC integration: no AWS, registry credentials, kubeconfig or production endpoints."""
import argparse,base64,collections,http.client,importlib.util,json,secrets,subprocess,sys,time,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
from v3_outbox_contract import CONFIG,CONTRACT,ENV_CONFIG

def module(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
base=module('foundation',ROOT/'tests/ci/smoke-v3-connect.py');bootstrap=module('mysql_bootstrap',ROOT/'scripts/create-v3-mysql-bootstrap-secret.py');cdc=module('cdc',ROOT/'scripts/manage-v3-mysql-cdc.py');ca=module('public_ca',ROOT/'scripts/prepare-v3-outbox-ca.py')
MYSQL=next(line.split(': ',1)[1].strip() for line in (ROOT/'deploy/v3/mysql/values.yaml').read_text().splitlines() if line.startswith('image: '))
TABLES=CONTRACT['tables'];HISTORY=CONTRACT['historyTopic'];OUTPUTS=CONTRACT['outputs'];NAME=CONTRACT['connectorName']
class Outbox:
 def sql(self,query,check=True):
  return self.ctx['docker']('exec','-i',self.mysql,'/bin/bash','-ec',cdc.ROOT_SHELL,source=query,check=check).stdout.strip()
 def prepare(self,ctx):
  self.ctx=ctx;d=ctx['directory'];docker=ctx['docker'];cred=d/'mysql-credentials';cred.mkdir();values=bootstrap.create_payload(cred);cred.chmod(0o755)
  for key,name in [('rootPassword','root'),('adminPassword','admin'),('appPassword','app'),('replicationPassword','replication'),('healthPassword','health')]: (cred/(name+'-password')).write_text(values[key])
  for f in cred.iterdir():f.chmod(0o444) # Disposable synthetic fixtures only.
  data=d/'mysql-data';data.mkdir();data.chmod(0o777);self.mysql=ctx['prefix']+'-mysql';ctx['containers'].append(self.mysql)
  docker('run','-d','--name',self.mysql,'--label','yeodam.issue=542','--platform','linux/amd64','--network',ctx['net'],'--network-alias','mysql-primary.mysql.svc.cluster.local','--network-alias','mysql-0.mysql-members.mysql.svc.cluster.local','--memory','768m','--cpus','1','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--user','999:999','--tmpfs','/tmp:rw,size=128m','-v',f'{ROOT}/deploy/v3/mysql/files:/runtime:ro','-v',f'{cred}:/credentials:ro','-v',f'{cred}:/tls:ro','-v',f'{data}:/var/lib/mysql','-e','POD_NAME=mysql-0','-e','PRIMARY_MEMBER=mysql-0','--entrypoint','/bin/bash',MYSQL,'/runtime/entrypoint.sh')
  for _ in range(90):
   state=json.loads(docker('inspect',self.mysql).stdout)[0]['State']
   if not state['Running']:raise RuntimeError('Synthetic MySQL exited; see fixture logs')
   # TCP confirms the final TLS server, not the temporary socket-only initializer.
   r=docker('exec',self.mysql,'/bin/bash','-ec','source /runtime/common.sh; f=/tmp/check-ready.cnf; trap \'rm -f "$f"\' EXIT; client_file yeodam mysql-primary.mysql.svc.cluster.local "$f" app; echo "SELECT 1;" | query "$f"',check=False)
   if r.returncode==0:break
   time.sleep(2)
  else:raise RuntimeError('Synthetic MySQL startup timeout')
  self.sql('SET GLOBAL rpl_semi_sync_source_enabled=OFF; SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;')
  for table in TABLES:
   idtype='VARCHAR(36)' if table in ['trip_creation_outbox','attachment_conversion_outbox'] else 'BIGINT'
   self.sql(f"CREATE TABLE yeodam.{table} (outbox_id {idtype} PRIMARY KEY,trip_id BIGINT NOT NULL,topic VARCHAR(255),payload LONGTEXT,event_source VARCHAR(64),request_id VARCHAR(128),cdc_managed BOOLEAN NOT NULL,status VARCHAR(20));")
  self.dbpassword=secrets.token_hex(24);self.sql(cdc.create_sql(self.dbpassword))
  public=d/'mysql-ca';public.mkdir();ca.prepare(values['tlsCa'],d/'public-ca-overlay.json');overlay=json.loads((d/'public-ca-overlay.json').read_text());(public/'mysql-truststore.jks').write_bytes(base64.b64decode(overlay['mysqlTruststore']));(public/'mysql-truststore.jks').chmod(0o444)

  # Compile against the exact client jar from the pinned runtime image, not host libraries.
  cid=docker('create','--label','yeodam.run='+ctx['prefix'],ctx['image']).stdout.strip()
  try:docker('cp',cid+':/opt/kafka/libs/kafka-clients-4.2.2.jar',str(d/'kafka-clients.jar'))
  finally:docker('rm',cid,check=False)
  ctx['command'](['javac','--release','17','-cp',str(d/'kafka-clients.jar'),'-d',str(d/'classes'),str(ROOT/'tests/ci/v3/OutboxWire.java')])
  docker('run','--rm','--label','yeodam.run='+ctx['prefix'],'--label','yeodam.issue=542','--platform','linux/amd64','--network',ctx['net'],'--memory','256m','--cpus','.5','--read-only','--tmpfs','/tmp:rw,size=32m','-v',f'{d}:/check:ro','--entrypoint','java',ctx['image'],'-Xmx96m','-cp','/opt/kafka/libs/*:/check/classes','OutboxWire','--setup','/check/admin.properties')
  self.expected={};self.stage_rows(3)
  for table in TABLES:
   self.sql(self.insert(table,'1',False,'PENDING',OUTPUTS[0])+self.insert(table,'2',True,'PUBLISHED',OUTPUTS[0]))

  print('Synthetic source/schema-history/business7 TLS setup ready; initial PENDING rows seeded',flush=True)
 def worker_config(self,ctx):
  return '\n'+ '\n'.join(k+'='+str(v).lower() if isinstance(v,bool) else k+'='+str(v) for k,v in (ENV_CONFIG | {'producer.compression.type':'lz4','offset.flush.interval.ms':60000}).items())+'\n'
 def worker_args(self,ctx):
  return ['-e','V3_CDC_PASSWORD='+self.dbpassword,'-e','V3_KAFKA_PASSWORD='+ctx['password'],'-e','V3_KAFKA_CA_PEM='+(ctx['tls']/'ca.crt').read_text(),'-v',str(ctx['directory']/'mysql-ca')+':/mnt/mysql-ca:ro']
 def insert(self,table,id,managed,status,topic):
  payload=json.dumps({'fixture':'542','source':table,'id':id},separators=(',',':'))
  return f"INSERT INTO yeodam.{table} VALUES ('{id}',77,'{topic}','{payload}','{table}','trace-542',{1 if managed else 0},'{status}');"
 def stage_rows(self,stage):
  for i,table in enumerate(TABLES):
   targets=OUTPUTS[3:] if table=='attachment_conversion_outbox' else [OUTPUTS[i]]
   for topic in targets:
    id=str(stage)+'-'+topic.rsplit('.',1)[-1] if table=='attachment_conversion_outbox' else str(stage)
    self.sql(self.insert(table,id,True,'PENDING',topic));self.expected[(table,id)]={'topic':topic,'payload':json.dumps({'fixture':'542','source':table,'id':id},separators=(',',':'))}
 def request(self,path,method='GET',data=None):
  req=urllib.request.Request('http://127.0.0.1:'+str(self.port)+path,data=(json.dumps(data).encode() if data is not None else None),headers={'Content-Type':'application/json'},method=method)
  try:
   with urllib.request.urlopen(req,timeout=(90 if method != 'GET' else 10)) as r:return json.load(r) if r.status!=204 else None
  except urllib.error.HTTPError as e:
   (self.ctx['output']/'outbox-rest-error.json').write_text(json.dumps({'path':path,'code':e.code,'message':e.read().decode()[:12000]}));raise
 def running(self):
  for _ in range(120):
   try:
    status=self.request('/connectors/'+NAME+'/status')
    if status.get('connector',{}).get('state')=='FAILED' or any(t['state']=='FAILED' for t in status.get('tasks',[])):
     (self.ctx['output']/'outbox-failed-status.json').write_text(json.dumps(status,indent=2));raise RuntimeError('Source connector/task failed; see synthetic worker diagnostics')
    if status.get('tasks') and all(t['state']=='RUNNING' for t in status['tasks']):return
   except urllib.error.HTTPError as e:
    if e.code not in [404,409,500]:raise
   except (urllib.error.URLError,TimeoutError,ConnectionError,http.client.HTTPException):pass
   time.sleep(2)
  raise RuntimeError('Source task readiness timeout')
 def records(self):
  ctx=self.ctx;r=ctx['docker']('run','--rm','--platform','linux/amd64','--network',ctx['net'],'--memory','256m','--cpus','.5','--read-only','--tmpfs','/tmp:rw,size=32m','-v',f"{ctx['directory']}:/check:ro",'--entrypoint','java',ctx['image'],'-Xmx96m','-cp','/opt/kafka/libs/*:/check/classes','OutboxWire','/check/admin.properties',*OUTPUTS,timeout=60)
  return [json.loads(line.split('OUTBOX_RECORD ',1)[1]) for line in r.stdout.splitlines() if line.startswith('OUTBOX_RECORD ')]
 def check_wire(self,records,minimum):
  found=collections.Counter()
  for r in records:
   h={k:base64.b64decode(v).decode() for k,v in r['headers'].items()};identity=(h['outbox-source'],h['id']);assert identity in self.expected,'Unexpected filtered/replayed event'
   expected=self.expected[identity];assert r['topic']==expected['topic'] and base64.b64decode(r['key']).decode()=='77' and base64.b64decode(r['value']).decode()==expected['payload'] and h['X-Request-ID']=='trace-542';found[identity]+=1
  return found if len(found)>=minimum else None
 def receive(self,minimum):
  for _ in range(20):
   records=self.records();found=self.check_wire(records,minimum)
   if found:return found
   time.sleep(2)
  raise RuntimeError('Expected Outbox records not observed')
 def exercise(self,ctx):
  self.port=ctx['port'];config=dict(CONFIG);config['schema.history.internal.kafka.bootstrap.servers']='broker1:9093'
  self.request('/connectors','POST',{'name':NAME,'config':config});self.running();self.receive(7)
  print('PASS: initial snapshot7 pending rows and exact wire',flush=True)
  # Updates, polling-owned/PUBLISHED inserts and rolled-back inserts must not publish work.
  for table in TABLES:
   self.sql(f"UPDATE yeodam.{table} SET status='PUBLISHED' WHERE outbox_id LIKE '3%';"+self.insert(table,'5',False,'PENDING',OUTPUTS[0])+self.insert(table,'6',True,'PUBLISHED',OUTPUTS[0])+'START TRANSACTION;'+self.insert(table,'8',True,'PENDING',OUTPUTS[0])+'ROLLBACK;')
  self.stage_rows(4);self.receive(14)
  print('PASS: stream7 and filtered insert/update/rollback checks; waiting for production60s offset flush',flush=True)
  raw=self.request('/connectors/'+NAME+'/config');assert raw['database.password']=='${env:V3_CDC_PASSWORD}' and self.dbpassword not in json.dumps(raw),'Config provider leaked literal credentials'
  # Wait through the real production60s flush budget; never modify/reset offsets.
  time.sleep(65);offsets=self.request('/connectors/'+NAME+'/offsets');assert offsets['offsets'] and offsets['offsets'][0]['offset'].get('file')
  (ctx['output']/'outbox-offsets-before.json').write_text(json.dumps(offsets,indent=2))
  print('PASS: committed source offset observed; stopping worker and inserting offline7',flush=True)
  ctx['docker']('stop','--time','90',ctx['worker'],timeout=110);self.stage_rows(7);ctx['docker']('start',ctx['worker'])
  state=json.loads(ctx['docker']('inspect',ctx['worker']).stdout)[0];self.port=state['NetworkSettings']['Ports']['8083/tcp'][0]['HostPort'];self.running()
  found=self.receive(21);time.sleep(3);found=self.check_wire(self.records(),21);assert found and all(v==1 for v in found.values()),'Controlled restart unexpectedly replayed acknowledged events'
  state=json.loads(ctx['docker']('inspect',ctx['worker']).stdout)[0];assert state['State']['Running'] and not state['State']['OOMKilled']
  (ctx['output']/'outbox-result.json').write_text(json.dumps({'sourceTables':4,'businessTopics':7,'initialPendingRecords':7,'streamRecords':7,'offlineRecoveryRecords':7,'unexpectedEvents':0,'controlledRestartDuplicates':0,'committedOffsetBeforeRestart':True,'configProviderReferencesRetained':True,'dbTlsVerifyIdentity':True,'kafkaTlsScram':True,'schemaHistoryRf':3,'schemaHistoryMinIsr':2,'connectMemoryMi':768,'heapMi':256,'cpuLimit':0.5,'businessTasks':1},indent=2))
  print('PASS: Outbox4 -> topics7 exact wire; snapshot/stream/offline recovery21, filters+rollback, committed offsets and768Mi worker',flush=True)
 def cleanup(self,ctx):
  data=ctx['directory']/'mysql-data'
  if data.exists():ctx['docker']('run','--rm','--platform','linux/amd64','--network','none','--read-only','--cap-drop','ALL','--user','999:999','-v',f'{data}:/cleanup','--entrypoint','/bin/bash',MYSQL,'-ec','rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?*',check=False)
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True);p.add_argument('--output',required=True,type=Path);a=p.parse_args();base.smoke(a.image,a.output,Outbox())
