#!/usr/bin/env python3
"""Configure only run-scoped migration connectors; no topic/offset deletion."""
import argparse
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from uuid import UUID

BROKER='yeodam-v2-staging-kafka-kafka-1'
BASE='http://127.0.0.1:18083'

def api(path,data=None,method=None):
    request=Request(BASE+path,data=None if data is None else json.dumps(data).encode(),method=method,headers={'Content-Type':'application/json'})
    with urlopen(request,timeout=25) as r:
        b=r.read();return json.loads(b) if b else {}

def kafka(tool,*args):
    p=subprocess.run(['docker','exec',BROKER,'/opt/kafka/bin/'+tool+'.sh','--bootstrap-server','127.0.0.1:9092',*args],capture_output=True,text=True)
    if p.returncode:raise RuntimeError('Kafka '+tool+' failed')
    return p.stdout.strip()

def names(run):
    return 'yeodam-b386-'+run[:8], 'staging.yeodam.migration.b386.'+run, 'staging.yeodam.migration.auth368.b386-'+run

def topics(run):
    n,p,a=names(run)
    definitions=[(p+'.trips',8,86400000),(p+'.requests',4,86400000),(p+'.auth-requests',4,86400000),(p+'.auth-requests.results',4,86400000),(p+'.handoff-proof',1,86400000),(p+'.schema-history',1,-1),(a+'-encrypted',1,86400000),(a+'-target-encrypted',1,86400000),(p+'.yeodam_v1_source.migration_cutover_barrier',1,86400000),(a+'.source.yeodam_v1_source.migration_cutover_barrier',1,86400000),(a+'.source-history',1,-1),(a+'.target-history',1,-1),('__debezium-heartbeat.'+p,1,86400000),('__debezium-heartbeat.'+a+'.source',1,86400000),('__debezium-heartbeat.'+a+'.target',1,86400000)]
    # One owned AdminClient call replaces repeated JVM starts before measured input.
    code="""import json,sys
from confluent_kafka.admin import AdminClient,NewTopic
items=json.load(sys.stdin);a=AdminClient({'bootstrap.servers':'10.30.10.95:9092'})
existing=set(a.list_topics(timeout=10).topics)
if any(n in existing for n,_,_ in items):raise RuntimeError('Run topic already exists; preserve it')
futures=a.create_topics([NewTopic(n,num_partitions=p,replication_factor=1,config={'cleanup.policy':'delete','retention.ms':str(r)}) for n,p,r in items],request_timeout=15)
for f in futures.values():f.result()
"""
    result=subprocess.run(['/opt/yeodam-b383/venv/bin/python3','-c',code],input=json.dumps(definitions),capture_output=True,text=True)
    if result.returncode:raise RuntimeError('Scoped topic creation failed; existing topics preserved')
    print(json.dumps({'action':'topics','runId':run,'tripPartitions':8,'b2Partitions':4,'retentionHours':24}))

def source(run,kind,index):
    n,p,a=names(run);auth=kind!='trips';target=kind=='auth-target'
    data={'name':n+'-'+kind,'connector.class':'io.debezium.connector.mysql.MySqlConnector','tasks.max':'1',
          'database.hostname':'10.30.10.28' if target else '10.30.0.174','database.port':'3306',
          'database.user':'yeodam_cdc_sink' if target else 'yeodam_cdc_test',
          'database.password':'${file:/run/yeodam-cdc/b1-'+('sink' if target else 'source')+'.properties:password}',
          'database.server.id':str((6476 if target else 6276 if auth else 6376)+index),
          'database.ssl.mode':'required','database.include.list':'yeodam_v1_source',
          'table.include.list':','.join('yeodam_v1_source.'+t for t in (('login_sessions','csrf_tokens') if target else ('login_sessions','csrf_tokens','migration_cutover_barrier') if auth else ('trips','trip_regions','migration_cutover_barrier'))),
          'topic.prefix':a+('.target' if target else '.source') if auth else p,
          'snapshot.mode':'initial' if kind=='auth-source' else 'no_data','snapshot.locking.mode':'none' if target else 'minimal',
          'schema.history.internal.kafka.bootstrap.servers':'kafka.staging.yeodam.internal:9092',
          'schema.history.internal.kafka.topic':a+('.target-history' if target else '.source-history') if auth else p+'.schema-history',
          'schema.history.internal.store.only.captured.tables.ddl':'true','include.schema.changes':'false','tombstones.on.delete':'false',
          'errors.tolerance':'none','producer.override.acks':'all','producer.override.enable.idempotence':'true',
          'producer.override.max.in.flight.requests.per.connection':'1','heartbeat.interval.ms':'1000'}
    if auth:
        data.update({'key.converter':'org.apache.kafka.connect.storage.StringConverter','value.converter':'org.apache.kafka.connect.storage.StringConverter',
          'transforms':'encrypt','transforms.encrypt.type':'com.yeodam.cdc.EncryptAuthRecord',
          'transforms.encrypt.key.file':'/run/yeodam-cdc/auth368-aes.hex','transforms.encrypt.destination.topic':a+('-target-encrypted' if target else '-encrypted'),
          'predicates':'isAuth','predicates.isAuth.type':'org.apache.kafka.connect.transforms.predicates.TopicNameMatches',
          'predicates.isAuth.pattern':r'.*\.(login_sessions|csrf_tokens)$','transforms.encrypt.predicate':'isAuth'})
    else:
        data.update({'key.converter':'org.apache.kafka.connect.json.JsonConverter','key.converter.schemas.enable':'true',
          'value.converter':'org.apache.kafka.connect.json.JsonConverter','value.converter.schemas.enable':'true',
          'transforms':'route,partition','transforms.route.type':'io.debezium.transforms.ByLogicalTableRouter',
          'transforms.route.topic.regex':r'^'+p.replace('.',r'\.')+r'\.yeodam_v1_source\.(trips|trip_regions)$',
          'transforms.route.topic.replacement':p+'.trips','transforms.route.key.enforce.uniqueness':'false',
          'transforms.partition.type':'io.debezium.transforms.partitions.PartitionRouting',
          'transforms.partition.partition.payload.fields':'change.trip_id','transforms.partition.partition.topic.num':'8',
          'transforms.partition.partition.hash.function':'murmur',
          'predicates':'isTrip','predicates.isTrip.type':'org.apache.kafka.connect.transforms.predicates.TopicNameMatches',
          'predicates.isTrip.pattern':r'.*\.(trips|trip_regions)$',
          'transforms.route.predicate':'isTrip','transforms.partition.predicate':'isTrip'})
    return data

def sink(run):
    n,p,a=names(run)
    return {'name':n+'-sink','connector.class':'io.debezium.connector.jdbc.JdbcSinkConnector','tasks.max':'8',
      'topics':p+'.trips','connection.url':'jdbc:mysql://10.30.10.28:3306/yeodam_v1_source?sslMode=REQUIRED',
      'connection.username':'yeodam_cdc_sink','connection.password':'${file:/run/yeodam-cdc/b1-sink.properties:password}',
      'connection.pool.min_size':'1','connection.pool.max_size':'2','connection.pool.acquire_increment':'1',
      'insert.mode':'upsert','delete.enabled':'true','primary.key.mode':'record_key','schema.evolution':'none',
      'collection.name.format':'${source.table}','batch.size':'1','consumer.override.max.poll.records':'1',
      'consumer.override.auto.offset.reset':'earliest','use.reduction.buffer':'false','errors.tolerance':'none',
      'key.converter':'org.apache.kafka.connect.json.JsonConverter','key.converter.schemas.enable':'true',
      'value.converter':'org.apache.kafka.connect.json.JsonConverter','value.converter.schemas.enable':'true'}

def register(data):
    if data['name'] in api('/connectors'):raise RuntimeError('Connector already exists; preserve its offset')
    validation=api('/connector-plugins/'+data['connector.class']+'/config/validate',data,'PUT')
    if validation['error_count']:
        fields=[c['definition']['name'] for c in validation['configs'] if c.get('value',{}).get('errors')]
        raise RuntimeError('Invalid connector fields: '+','.join(fields))
    api('/connectors/'+data['name']+'/config',data,'PUT')
    print(json.dumps({'registered':data['name']}))

def status(run):
    n,p,a=names(run);existing=api('/connectors')
    for name in ['yeodam-staging-outbox',*(n+'-'+k for k in ('trips','auth-source','sink','auth-target'))]:
        if name not in existing:continue
        v=api('/connectors/'+name+'/status')
        print(json.dumps({'name':name,'state':v['connector']['state'],'tasks':[t['state'] for t in v['tasks']]}))
        # Never print task error traces: auth values may appear in driver errors.
        if name.endswith(('-trips','-auth-source','-auth-target')):
            print(json.dumps({'name':name,'offsets':api('/connectors/'+name+'/offsets').get('offsets',[])}))
    for topic in [p+'.trips',p+'.requests',p+'.auth-requests',p+'.auth-requests.results',a+'-encrypted']:
        print(kafka('kafka-get-offsets','--topic',topic,'--time','-1'))
    for group in ['connect-'+n+'-sink','yeodam-b386-'+run,'yeodam-b386-auth-'+run,'yeodam-b386-auth-result-'+run,'yeodam-auth368-b386-'+run]:
        print(kafka('kafka-consumer-groups','--describe','--group',group))

def end_offsets(topic):
    rows=kafka('kafka-get-offsets','--topic',topic,'--time','-1').splitlines()
    return {int(row.split(':')[-2]):int(row.split(':')[-1]) for row in rows}

def group_current(group,topic):
    rows=kafka('kafka-consumer-groups','--describe','--group',group).splitlines()
    values={}
    for row in rows:
        fields=row.split()
        if len(fields)>=6 and fields[0]==group and fields[1]==topic and fields[3].isdigit():values[int(fields[2])]=int(fields[3])
    return values

def ready(run,kind):
    n,_,_=names(run);name=n+'-'+kind
    end=time.monotonic()+90
    while time.monotonic()<end:
        s=api('/connectors/'+name+'/status')
        if s['connector']['state']=='FAILED' or any(t['state']=='FAILED' for t in s['tasks']):raise RuntimeError('Connector failed: '+name)
        offsets=api('/connectors/'+name+'/offsets').get('offsets',[])
        if s['connector']['state']=='RUNNING' and s['tasks'] and all(t['state']=='RUNNING' for t in s['tasks']) and offsets and offsets[0]['offset'].get('snapshot') not in (True,'true'):
            print(json.dumps({'ready':name,'offsets':offsets}),flush=True);return
        time.sleep(2)
    raise RuntimeError('Connector did not reach a committed streaming offset: '+name)

def drain(run,source_file,source_position,target=False,absolute_deadline=None):
    n,p,a=names(run)
    deadline=time.monotonic()+max(0,(absolute_deadline-time.time()) if absolute_deadline else 60)
    stopped=False
    while time.monotonic()<deadline:
        if not stopped:
            kinds=('auth-target',) if target else ('trips','auth-source')
            caught=True
            for kind in kinds:
                offsets=api('/connectors/'+n+'-'+kind+'/offsets').get('offsets',[])
                offset=offsets[0]['offset'] if len(offsets)==1 else {}
                file=offset.get('file','');pos=offset.get('pos',0)
                prefix,_,number=file.rpartition('.')
                expected_prefix,_,expected_number=source_file.rpartition('.')
                caught=caught and prefix==expected_prefix and number.isdigit() and expected_number.isdigit() and (int(number),int(pos)) >= (int(expected_number),source_position)
            if caught:
                for kind in kinds:api('/connectors/'+n+'-'+kind+'/stop',method='PUT')
                stopped=True
        groups=[('yeodam-auth368-b386-'+run+('-target' if target else ''),a+('-target-encrypted' if target else '-encrypted'))]
        if not target:groups.append(('connect-'+n+'-sink',p+'.trips'))
        equal=stopped
        details=[]
        for group,topic in groups:
            last=end_offsets(topic);current=group_current(group,topic)
            matched=current==last
            equal=equal and matched;details.append({'group':group,'end':last,'current':current,'equal':matched})
        if equal:
            if not target:api('/connectors/'+n+'-sink/stop',method='PUT')
            print(json.dumps({'runId':run,'equal':True,'observedAt':time.time(),'groups':details}),flush=True);return
        time.sleep(2)
    print(json.dumps({'runId':run,'equal':False,'observedAt':time.time(),'groups':details}),flush=True)
    raise RuntimeError('Fixed Run CDC boundary deadline exceeded; preserve failure')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('topics','trips','auth-source','sink','auth-target','status','stop-trips','stop-auth-source','stop-auth-target','stop-sink','ready-trips','ready-auth-source','ready-auth-target','drain','drain-target'))
    p.add_argument('--run-id',required=True);p.add_argument('--index',type=int,default=0);p.add_argument('--source-file');p.add_argument('--source-position',type=int);p.add_argument('--deadline',type=float);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id or a.index not in range(4):raise ValueError('Invalid run contract')
    n,_,_=names(a.run_id)
    if a.action=='topics':topics(a.run_id)
    elif a.action=='status':status(a.run_id)
    elif a.action.startswith('ready-'):ready(a.run_id,a.action[6:])
    elif a.action in ('drain','drain-target'):
        if a.action=='drain' and (not a.source_file or not a.source_position):raise ValueError('Frozen source position is required')
        drain(a.run_id,a.source_file,a.source_position,a.action=='drain-target',a.deadline)
    elif a.action.startswith('stop-'):
        api('/connectors/'+n+'-'+a.action[5:]+'/stop',method='PUT');print('STOP_REQUESTED '+n+'-'+a.action[5:])
    else:register(sink(a.run_id) if a.action=='sink' else source(a.run_id,a.action,a.index))
