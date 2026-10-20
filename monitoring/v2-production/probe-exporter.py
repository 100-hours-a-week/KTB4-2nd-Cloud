#!/usr/bin/env python3
"""Fixed read-only production probes. No request inputs become SQL, URLs, or labels."""
import argparse, json, subprocess, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
TABLES=('trip_creation_outbox','additional_attachment_upload_outbox','story_generation_outbox')
cache={'at':0,'text':''}; lock=Lock()
def collect(kind):
 rows=[f'yeodam_observer_sample_timestamp_seconds{{probe="{kind}"}} {time.time()}']
 try:
  if kind=='mysql':
   password=Path('/etc/yeodam/production-monitoring/pipeline-password').read_text().strip()
   sql='\n'.join(f"SELECT /*+ MAX_EXECUTION_TIME(800) */ 'outbox','{t}',COUNT(*),COALESCE(GREATEST(MAX(TIMESTAMPDIFF(SECOND,created_at,NOW())),0),0) FROM yeodam.{t} WHERE cdc_managed=1 AND status='PENDING';" for t in TABLES)
   sql+="\nSELECT /*+ MAX_EXECUTION_TIME(800) */ 'execution',status,COUNT(*),0 FROM yeodam.trip_creation_executions GROUP BY status;"
   sql+="\nSELECT /*+ MAX_EXECUTION_TIME(800) */ 'apply','pending',COUNT(*),COALESCE(GREATEST(MAX(TIMESTAMPDIFF(SECOND,result_received_at,NOW())),0),0) FROM yeodam.trip_creation_executions WHERE status='RESULT_APPLYING';"
   r=subprocess.run(['docker','exec','-i','yeodam-v2-mysql','sh','-ec','IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec mysql -uyeodam_pipeline_metrics --batch --skip-column-names'],input=password+'\n'+sql,text=True,capture_output=True,timeout=8,check=True)
   lines=r.stdout.splitlines();seen=set()
   for line in lines:
    kind_,name,count,age=line.split('\t')
    if kind_=='outbox':
     assert name in TABLES;seen.add(name)
     rows += [f'yeodam_outbox_pending{{outbox="{name}"}} {int(count)}',f'yeodam_outbox_oldest_pending_seconds{{outbox="{name}"}} {int(age)}']
    elif kind_=='execution':
     assert name in ('UPLOADING','IMAGE_PROCESSING','AI_QUEUED','RESULT_APPLYING','COMPLETED','FAILED','CANCELED')
     rows.append(f'yeodam_trip_pipeline_executions{{state="{name}"}} {int(count)}')
    else:
     assert kind_=='apply'
     rows += [f'yeodam_result_apply_pending {int(count)}',f'yeodam_result_apply_oldest_seconds {int(age)}']
   assert seen==set(TABLES)
  else:
   try:
    with urllib.request.urlopen('http://127.0.0.1:8083/connectors/yeodam-production-outbox/status',timeout=3) as r:s=json.load(r)
    running=int(s['connector']['state']=='RUNNING');tasks=s.get('tasks',[])
   except urllib.error.HTTPError as e:
    if e.code!=404:raise
    running=0;tasks=[]
   rows += [f'yeodam_connect_expected_connector_running {running}',f'yeodam_connect_expected_tasks_running {sum(t["state"]=="RUNNING" for t in tasks)}']
  rows.append(f'yeodam_observer_success{{probe="{kind}"}} 1')
 except Exception:
  rows.append(f'yeodam_observer_success{{probe="{kind}"}} 0')
 return '\n'.join(rows)+'\n'
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path!='/metrics':self.send_error(404);return
  with lock:
   if time.monotonic()-cache['at']>10:
    cache.update(at=time.monotonic(),text=collect(self.server.kind))
   body=cache['text'].encode()
  self.send_response(200);self.send_header('Content-Type','text/plain; version=0.0.4');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--kind',choices=['mysql','connect'],required=True);p.add_argument('--address',required=True);a=p.parse_args()
 server=ThreadingHTTPServer((a.address,9187),Handler);server.kind=a.kind;server.serve_forever()
