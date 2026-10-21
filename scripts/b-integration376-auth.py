#!/usr/bin/env python3
"""Reuse #368 encryption/Redis projection; mirror Source auth into V1-compatible Target.

Target-side auth CDC subsequently updates Redis only. Kafka offsets commit after
both required stores succeed; replayed upserts/deletes remain idempotent.
"""
import argparse
from datetime import datetime,timedelta
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from uuid import UUID

sys.path.insert(0,'/opt/yeodam-cdc-auth368')
from cdc_auth368 import apply_change,decrypt_event,parse_change


def aws(service,*args):
    p=subprocess.run(['aws',service,*args,'--region','ap-northeast-2','--output','json'],capture_output=True,text=True)
    if p.returncode:raise RuntimeError('Private credential lookup failed')
    return json.loads(p.stdout)

def configure(run):
    old=json.loads(Path('/run/yeodam-cdc-auth368/consumer.json').read_text())
    old['run_id']=run;old['redis']['prefix']='yeodam:cutover:'+run+':'
    directory=Path('/run/yeodam-b376')/run;directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=directory/'auth.json'
    if path.exists():raise RuntimeError('Auth config already exists')
    path.write_text(json.dumps(old));path.chmod(0o600)
    print(json.dumps({'authConfig':str(path),'runId':run}))

def mirror(event,connection):
    change=parse_change(event)
    table=change.table
    before,after=change.before,change.after
    pk='login_session_id' if table=='login_sessions' else 'csrf_token_id'
    def value(column,item):
        if column.endswith('_at') and isinstance(item,int):return datetime(1970,1,1)+timedelta(microseconds=item)
        if item is not None and not isinstance(item,(int,str)):raise ValueError('Unsupported auth column type')
        return item
    try:
        connection.ping(reconnect=True)
        with connection.cursor() as cursor:
            if after is None:
                cursor.execute('DELETE FROM `'+table+'` WHERE `'+pk+'`=%s',(before[pk],))
            else:
                columns=list(after)
                if any(not re.fullmatch('[a-z_]+',c) for c in columns):raise ValueError('Unexpected auth column')
                sql='INSERT INTO `'+table+'` ('+','.join('`'+c+'`' for c in columns)+') VALUES ('+','.join('%s' for _ in columns)+') ON DUPLICATE KEY UPDATE '+','.join('`'+c+'`=VALUES(`'+c+'`)' for c in columns if c!=pk)
                cursor.execute(sql,tuple(value(c,after[c]) for c in columns))
    except Exception:
        raise RuntimeError('Auth Target projection failed; Kafka offset remains uncommitted') from None


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('configure','consume','audit'));p.add_argument('--run-id',required=True);p.add_argument('--mysql-role',choices=('source','target'),default='source');a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id:raise ValueError('Canonical UUID required')
    if a.action=='configure':configure(a.run_id);return
    config_path=Path('/run/yeodam-b376')/a.run_id/'auth.json'
    config=json.loads(config_path.read_text())
    # Existing IAM grants only this staging Redis secret to the Source host.
    secret=aws('secretsmanager','get-secret-value','--secret-id','yeodam-v2-staging/redis-auth-token')['SecretString']
    os.environ[config['redis']['password_env']]=secret
    if a.action=='audit':
        executable='/opt/yeodam-cdc-auth368/venv/bin/python3'
        os.execv(executable,[executable,'/opt/yeodam-cdc-auth368/audit-cdc-auth368.py','--config',str(config_path),'--mysql-role',a.mysql_role])
    import redis
    from confluent_kafka import Consumer
    r=config['redis'];client=redis.Redis(host=r['host'],port=r['port'],password=secret,ssl=True,decode_responses=True,socket_timeout=5)
    client.ping()
    topic='staging.yeodam.migration.auth368.b376-'+a.run_id+'-encrypted'
    key=bytes.fromhex(Path('/run/yeodam-cdc-auth368/aes.hex').read_text().strip())
    password=aws('ssm','get-parameter','--name','/yeodam/v2/staging/cdc-test/SINK_PASSWORD','--with-decryption')['Parameter']['Value']
    ca=config_path.parent/'target-ca.pem'
    ca.write_text(aws('ssm','get-parameter','--name','/yeodam/v2/staging/migration/TARGET_CA_PEM')['Parameter']['Value']);ca.chmod(0o600)
    import pymysql,ssl
    context=ssl.create_default_context(cafile=str(ca));context.check_hostname=False
    connection=pymysql.connect(host='10.30.10.28',user='yeodam_cdc_sink',password=password,database='yeodam_v1_source',ssl=context,autocommit=True,charset='utf8mb4',connect_timeout=3,read_timeout=5,write_timeout=5)
    consumer=Consumer({'bootstrap.servers':'10.30.10.95:9092','group.id':'yeodam-auth368-b376-'+a.run_id,'auto.offset.reset':'earliest','enable.auto.commit':False,'enable.auto.offset.store':False})
    consumer.subscribe([topic]);running=True;count=0
    def stop(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print(json.dumps({'phase':'auth_consumer_started','runId':a.run_id}),flush=True)
    try:
        while running:
            msg=consumer.poll(1)
            if msg is None:continue
            if msg.error():raise RuntimeError('Authentication Kafka record error')
            event=decrypt_event(msg.value(),topic,key)
            name=event.get('source',{}).get('name')
            if name not in ('staging.yeodam.migration.auth368.b376-'+a.run_id+'.source','staging.yeodam.migration.auth368.b376-'+a.run_id+'.target'):raise RuntimeError('Unexpected auth Source identity')
            if name.endswith('.source'):mirror(event,connection)
            sec,micro=client.time();apply_change(client,event,r['prefix'],config['source_timezone'],sec*1000+micro//1000)
            consumer.commit(message=msg,asynchronous=False);count+=1
            if count%500==0:print(json.dumps({'applied':count,'offset':msg.offset()}),flush=True)
    finally:consumer.close();connection.close();print(json.dumps({'phase':'auth_consumer_stopped','applied':count}),flush=True)
if __name__=='__main__':main()
