#!/usr/bin/env python3
"""Synthetic TLS MySQL only: fixed aggregates, column ACLs, timeout/freshness and cleanup."""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
def module(name,path):
    s=importlib.util.spec_from_file_location(name,ROOT/path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
observer=module('observer','deploy/v3/metrics/files/outbox-metrics.py')
provision=module('provision','scripts/provision-v3-outbox-observer.py')
IMAGE=json.loads((ROOT/'deploy/v3/metrics/images.lock.json').read_text())['outbox-query']
PREFIX='outbox-observer-'+uuid.uuid4().hex[:10]
ROOT_PASSWORD=secrets.token_hex(24);PASSWORD=secrets.token_hex(24)
def run(args,source=None,check=True,timeout=90):
    p=subprocess.run(args,input=source,capture_output=True,text=True,timeout=timeout)
    if check and p.returncode:raise RuntimeError('Synthetic fixture command failed; data and credentials withheld')
    return p

def query(source,user='root',password=ROOT_PASSWORD,tls=False,check=True):
    args=['docker','exec','-i','-e','MYSQL_PWD='+password,PREFIX,'mysql','--batch','--skip-column-names','--silent','-u'+user]
    if tls:args+=['--protocol=TCP','--host='+PREFIX,'--ssl-mode=VERIFY_IDENTITY','--ssl-ca=/tls/cert.pem']
    return run(args,source,check)

with tempfile.TemporaryDirectory(prefix='outbox-observer-fixture-') as tmp:
    d=Path(tmp);os.chmod(d,0o755)
    # Synthetic certificate/key only, never any production material.
    run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-keyout',str(d/'key.pem'),'-out',str(d/'cert.pem'),'-subj','/CN='+PREFIX,'-addext','subjectAltName=DNS:'+PREFIX])
    os.chmod(d/'key.pem',0o644)
    try:
        run(['docker','network','create','--internal',PREFIX])
        run(['docker','run','-d','--name',PREFIX,'--network',PREFIX,'--platform','linux/amd64','--memory','512m','--cpus','1','--read-only','--security-opt','no-new-privileges:true','--tmpfs','/var/lib/mysql:uid=999,gid=999,size=512m','--tmpfs','/var/run/mysqld:uid=999,gid=999','--tmpfs','/tmp:size=64m','-v',str(d)+':/tls:ro','-e','MYSQL_ROOT_PASSWORD='+ROOT_PASSWORD,IMAGE,'--innodb-buffer-pool-size=64M','--innodb-redo-log-capacity=32M','--ssl-cert=/tls/cert.pem','--ssl-key=/tls/key.pem','--ssl-ca=/tls/cert.pem','--require-secure-transport=ON'])
        for _ in range(100):
            if query('SELECT 1;',check=False).returncode==0:break
            time.sleep(1)
        else:raise RuntimeError('Synthetic MySQL readiness timeout')
        ddl='CREATE DATABASE yeodam;\n'
        for table in observer.TABLES:
            ddl+=f'CREATE TABLE yeodam.{table} (status VARCHAR(20), cdc_managed BOOLEAN, created_at DATETIME(6), payload TEXT);\n'
            ddl+=f"INSERT INTO yeodam.{table} VALUES ('PENDING',TRUE,UTC_TIMESTAMP(6)-INTERVAL 60 SECOND,'synthetic'),('PUBLISHING',TRUE,UTC_TIMESTAMP(6)-INTERVAL 30 SECOND,'synthetic'),('PENDING',FALSE,UTC_TIMESTAMP(6)-INTERVAL 120 SECOND,'not-cdc'),('PUBLISHED',TRUE,UTC_TIMESTAMP(6)-INTERVAL 180 SECOND,'completed');\n"
        ddl+="CREATE TABLE yeodam.attachment_conversion_tasks(task_id VARCHAR(40) PRIMARY KEY,status VARCHAR(20),private_value TEXT); ALTER TABLE yeodam.attachment_conversion_outbox ADD task_id VARCHAR(40) DEFAULT 'active'; INSERT INTO yeodam.attachment_conversion_tasks VALUES ('active','WAITING','not-for-observer');"
        query(ddl+provision.sql(PASSWORD))
        result=query((ROOT/'deploy/v3/metrics/files/outbox.sql').read_text(),'cloud_outbox_observer',PASSWORD,True)
        rows=[line.split('\t')for line in result.stdout.splitlines()]
        assert len(rows)==9 and all(int(x[1])==2 and float(x[2])>=60 for x in rows[:4]),'Exact pending/oldest aggregate'
        for sql in ['SELECT payload FROM yeodam.trip_creation_outbox;','UPDATE yeodam.trip_creation_outbox SET status=\'PUBLISHED\';','SELECT * FROM mysql.user;','SELECT private_value FROM yeodam.attachment_conversion_tasks;',"UPDATE yeodam.attachment_conversion_tasks SET status='SUCCEEDED';"]:
            assert query(sql,'cloud_outbox_observer',PASSWORD,True,False).returncode!=0,'Observer privilege exceeded'
        # Actual mysql client runs as the same UID as the deployed sidecar; TLS hostname must match.
        client='[client]\nuser=cloud_outbox_observer\npassword='+PASSWORD+'\nhost='+PREFIX+'\nprotocol=TCP\nssl-mode=VERIFY_IDENTITY\nssl-ca=/tls/cert.pem\nconnect-timeout=3\n'
        client_path=d/'client.cnf';client_path.write_text(client);os.chmod(client_path,0o644)
        actual=run(['docker','exec','--user','65534:65534','-i',PREFIX,'mysql','--defaults-extra-file=/tls/client.cnf','--batch','--skip-column-names','--silent'],(ROOT/'deploy/v3/metrics/files/outbox.sql').read_text())
        assert len(actual.stdout.splitlines())==9
        snap=d/'snapshot';snap.write_text(str(int(time.time()))+'\n'+actual.stdout)
        assert 'yeodam_outbox_probe_success 1' in observer.metrics(snap)
        snap.write_text('1\n'+actual.stdout)
        assert observer.metrics(snap)=='yeodam_outbox_probe_success 0\n'
        # Successful/canceled/failed receipt remnants and missing/invalid task states stay visible.
        for task,state in [('done','SUCCEEDED'),('canceled','CANCELED'),('failed','FAILED'),('unrecognized','UNRECOGNIZED')]:
            query(f"INSERT INTO yeodam.attachment_conversion_tasks VALUES ('{task}','{state}','private');")
        for task in ['done','canceled','failed','unrecognized','missing']:
            query(f"INSERT INTO yeodam.attachment_conversion_outbox(status,cdc_managed,created_at,payload,task_id) VALUES ('PENDING',TRUE,UTC_TIMESTAMP(6)-INTERVAL 86400 SECOND,'synthetic','{task}');")
        before=query('SELECT COUNT(*) FROM yeodam.attachment_conversion_outbox;').stdout
        classified=query((ROOT/'deploy/v3/metrics/files/outbox.sql').read_text(),'cloud_outbox_observer',PASSWORD,True).stdout
        groups={key:int(count) for key,count,age in [line.split('\t') for line in classified.splitlines()[4:]]}
        assert groups=={'conversion_active':2,'conversion_succeeded':1,'conversion_failed':1,'conversion_canceled':1,'conversion_unknown':2}
        assert query('SELECT COUNT(*) FROM yeodam.attachment_conversion_outbox;').stdout==before
        # Actual production-shaped index and query plan: retain bounded, timed read queries.
        query('CREATE INDEX idx_conversion_pending ON yeodam.attachment_conversion_outbox(cdc_managed,status,created_at);')
        # Capped scan returns a labelled lower bound rather than claiming an exact full count.
        query("INSERT INTO yeodam.trip_creation_outbox SELECT 'PENDING',TRUE,UTC_TIMESTAMP(6),'synthetic' FROM JSON_TABLE(CONCAT('[',REPEAT('0,',10000),'0]'),'$[*]' COLUMNS(n INT PATH '$')) x;")
        capped=query((ROOT/'deploy/v3/metrics/files/outbox.sql').read_text(),'cloud_outbox_observer',PASSWORD,True).stdout
        assert int(capped.splitlines()[0].split('\t')[1])==10001
        assert query('SELECT COUNT(*) FROM yeodam.attachment_conversion_outbox;').stdout.strip()=='9','Collector must preserve source rows'
        query("INSERT INTO yeodam.attachment_conversion_outbox(status,cdc_managed,created_at,payload,task_id) SELECT 'PENDING',TRUE,UTC_TIMESTAMP(6),'synthetic','active' FROM JSON_TABLE(CONCAT('[',REPEAT('0,',10000),'0]'),'$[*]' COLUMNS(n INT PATH '$')) x;")
        capped_classes=query((ROOT/'deploy/v3/metrics/files/outbox.sql').read_text(),'cloud_outbox_observer',PASSWORD,True).stdout
        snap.write_text(str(int(time.time()))+'\n'+capped_classes)
        assert 'yeodam_conversion_outbox_classification_capped 1' in observer.metrics(snap)
        assert sum(int(line.split('\t')[1]) for line in capped_classes.splitlines()[4:])==10001
        # Actual SQL semantics for the converter quiet window; old successful receipt remnants are not active work.
        query("DELETE FROM yeodam.attachment_conversion_outbox; DELETE FROM yeodam.attachment_conversion_tasks; INSERT INTO yeodam.attachment_conversion_tasks VALUES ('done','SUCCEEDED','private'); INSERT INTO yeodam.attachment_conversion_outbox(status,cdc_managed,created_at,payload,task_id) VALUES ('PENDING',TRUE,UTC_TIMESTAMP(6),'synthetic','done');")
        drain=(ROOT/'deploy/v3/autoscaling/files/converter-drain.sql').read_text()
        assert json.loads(query(drain).stdout)==dict(waiting=0,running=0,unknown=0,unpublishedActive=0)
        query("INSERT INTO yeodam.attachment_conversion_tasks VALUES ('active','WAITING','private'); INSERT INTO yeodam.attachment_conversion_outbox(status,cdc_managed,created_at,payload,task_id) VALUES ('PENDING',TRUE,UTC_TIMESTAMP(6),'synthetic','active');")
        assert json.loads(query(drain).stdout)==dict(waiting=1,running=0,unknown=0,unpublishedActive=1)
        query("UPDATE yeodam.attachment_conversion_tasks SET status='RUNNING' WHERE task_id='active';")
        assert json.loads(query(drain).stdout)==dict(waiting=0,running=1,unknown=0,unpublishedActive=1)
        query("DELETE FROM yeodam.attachment_conversion_tasks WHERE task_id='active';")
        assert json.loads(query(drain).stdout)['unknown']==1
        print('Synthetic TLS Outbox aggregates, column-only ACL, non-root client, capped count and stale-data failure verified')
    finally:
        run(['docker','rm','-f',PREFIX],check=False)
        run(['docker','network','rm',PREFIX],check=False)
