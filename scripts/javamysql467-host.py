#!/usr/bin/env python3
"""Read-only MySQL checkpoints and bounded, payload-free JFR setup."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import UUID
import xml.etree.ElementTree as ET

BASE=Path('/run/yeodam-javamysql467')
DENIED_EVENTS={'jdk.InitialEnvironmentVariable','jdk.InitialSystemProperty','jdk.JVMInformation',
 'jdk.SystemProcess','jdk.ProcessStart','jdk.JavaExceptionThrow','jdk.JavaErrorThrow',
 'jdk.SecurityProperty','jdk.SecurityPropertyModification'}

def private_jfc(text):
 root=ET.fromstring(text)
 for name in DENIED_EVENTS:
  events=[e for e in root.findall('event') if e.get('name')==name]
  if not events:events=[ET.SubElement(root,'event',{'name':name})]
  for event in events:
   for setting in list(event):event.remove(setting)
   ET.SubElement(event,'setting',{'name':'enabled'}).text='false'
 # Record thread CPU at modest resolution to distinguish compiler/GC/application threads.
 for event in root.findall('event'):
  if event.get('name')=='jdk.ThreadCPULoad':
   for setting in event.findall('setting'):
    if setting.get('name')=='period':setting.text='5 s'
 for event in root.findall('event'):
  if event.get('name') in DENIED_EVENTS:
   assert all(s.text=='false' for s in event.findall("setting[@name='enabled']"))
 return ET.tostring(root,encoding='unicode')

def setup_jfr(run,role):
 directory=BASE/run/role
 directory.mkdir(parents=True,mode=0o700,exist_ok=False);directory.chmod(0o700);os.chown(directory,1001,1001)
 # Same installed JRE configuration as the pinned app; no image or JDK installation.
 p=subprocess.run(['docker','exec','yeodam-app-backend-1','cat','/opt/java/openjdk/lib/jfr/default.jfc'],capture_output=True,text=True,check=True)
 path=directory/'settings.jfc';path.write_text(private_jfc(p.stdout));path.chmod(0o444)
 print(json.dumps({'runId':run,'role':role,'jfrDirectory':str(directory),'secretEventsDisabled':sorted(DENIED_EVENTS),'recordingSizeLimitBytes':64*1024*1024}))

def database():
 spec=importlib.util.spec_from_file_location('reset467','/run/yeodam-b391/b-integration376-reset.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def capture(run,label):
 directory=BASE/run;directory.mkdir(parents=True,mode=0o700,exist_ok=True)
 path=directory/('mysql-'+label+'.json')
 if path.exists():raise RuntimeError('Checkpoint already exists; do not overwrite')
 r=database();q=lambda sql:r.query('source',sql)
 version=q('SELECT @@version;')
 columns=set(q("SELECT column_name FROM information_schema.columns WHERE table_schema='performance_schema' AND table_name='events_statements_summary_by_digest';").splitlines())
 names=['SCHEMA_NAME','DIGEST','DIGEST_TEXT','COUNT_STAR','SUM_TIMER_WAIT','SUM_LOCK_TIME','SUM_ROWS_EXAMINED','SUM_ROWS_SENT']
 if 'SUM_CPU_TIME' in columns:names.append('SUM_CPU_TIME')
 # Normalized DIGEST_TEXT only. Never retrieve query samples, credentials or parameter values.
 sql="SELECT "+','.join('COALESCE('+n+",'')" if n in ['SCHEMA_NAME','DIGEST','DIGEST_TEXT'] else n for n in names)+" FROM performance_schema.events_statements_summary_by_digest WHERE COUNT_STAR>0 AND (SCHEMA_NAME='yeodam_v1_source' OR SCHEMA_NAME IS NULL) ORDER BY DIGEST;"
 def lines(sql):return [line.split('\t') for line in q(sql).splitlines()]
 start=time.time();digests=[dict(zip(names,row)) for row in lines(sql)]
 result={'runId':run,'label':label,'startedAt':start,'serverVersion':version,'digestFields':names,'digests':digests,
 'statementEvents':lines('SELECT EVENT_NAME,COUNT_STAR,SUM_TIMER_WAIT FROM performance_schema.events_statements_summary_global_by_event_name WHERE COUNT_STAR>0 ORDER BY EVENT_NAME;'),
 'tableIo':lines("SELECT OBJECT_NAME,COUNT_FETCH,SUM_TIMER_FETCH,COUNT_INSERT,SUM_TIMER_INSERT,COUNT_UPDATE,SUM_TIMER_UPDATE,COUNT_DELETE,SUM_TIMER_DELETE FROM performance_schema.table_io_waits_summary_by_table WHERE OBJECT_SCHEMA='yeodam_v1_source' ORDER BY OBJECT_NAME;"),
 'waits':lines("SELECT EVENT_NAME,COUNT_STAR,SUM_TIMER_WAIT FROM performance_schema.events_waits_summary_global_by_event_name WHERE COUNT_STAR>0 AND (EVENT_NAME LIKE '%innodb%' OR EVENT_NAME LIKE '%binlog%' OR EVENT_NAME LIKE '%lock%') ORDER BY EVENT_NAME;"),
 'files':lines("SELECT EVENT_NAME,COUNT_READ,SUM_TIMER_READ,COUNT_WRITE,SUM_TIMER_WRITE,COUNT_MISC,SUM_TIMER_MISC FROM performance_schema.file_summary_by_event_name WHERE COUNT_STAR>0 ORDER BY EVENT_NAME;"),
 'status':lines("SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected','Threads_running','Com_commit','Com_rollback','Innodb_log_waits','Innodb_os_log_written','Innodb_os_log_fsyncs','Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests','Innodb_row_lock_waits','Innodb_row_lock_time');"),
 'enabledWaitInstruments':lines("SELECT NAME,ENABLED,TIMED FROM performance_schema.setup_instruments WHERE NAME LIKE '%innodb%log%' OR NAME LIKE '%binlog%' OR NAME LIKE '%cpu%';"),
 'schemaScope':'Source application schema and NULL-schema control statements; server-wide waits/status include observer/system work','counterReset':False,'finishedAt':time.time()}
 path.write_text(json.dumps(result));path.chmod(0o600)
 print(json.dumps({'runId':run,'label':label,'startedAt':start,'finishedAt':result['finishedAt'],'digests':len(digests),'cpuColumnAvailable':'SUM_CPU_TIME' in columns,'path':str(path)}))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('action',choices=['jfr-setup','mysql']);p.add_argument('--run-id',required=True);p.add_argument('--role',choices=['app','consumer']);p.add_argument('--label',choices=['before-input','snapshot-start','snapshot-end','input-end']);a=p.parse_args()
 if str(UUID(a.run_id))!=a.run_id or '10.30.0.245' not in subprocess.check_output(['hostname','-I'],text=True).split():raise RuntimeError('Wrong Source or Run')
 (setup_jfr(a.run_id,a.role) if a.action=='jfr-setup' else capture(a.run_id,a.label))
