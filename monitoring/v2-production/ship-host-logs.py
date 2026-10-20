#!/usr/bin/env python3
"""Forward only fixed production host-container logs; never inspect environment or Docker APIs over HTTP."""
import argparse,datetime,json,os,re,subprocess,time,urllib.request
from pathlib import Path
NAMES={'kafka':{'connect':'yeodam-v2-production-kafka-connect-1','kafka':'yeodam-v2-production-kafka-kafka-1'},'mysql':{'mysql':'yeodam-v2-mysql'},'redis':{'ai-redis':'yeodam-ai-redis'},'gpu':{'process-runner':'yeodam-ai-process-runner-production','qdrant':'yeodam-ai-qdrant-production','embedding':'yeodam-ai-embedding'}}
def stamp(v):
 v=v.rstrip('Z');base,_,fraction=v.partition('.');return int(datetime.datetime.fromisoformat(base).replace(tzinfo=datetime.timezone.utc).timestamp())*10**9+int((fraction+'000000000')[:9])
def redact(s):
 s=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+',r'\1[REDACTED]',s)
 s=re.sub(r'(?i)(X-Amz-(?:Signature|Credential|Security-Token)=)[^&\s"\\]+',r'\1[REDACTED]',s)
 return s
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--kind',choices=list(NAMES),required=True);p.add_argument('--instance',required=True);a=p.parse_args()
 path=Path('/var/lib/yeodam/production-monitoring/log-positions.json');path.parent.mkdir(parents=True,exist_ok=True)
 state=json.loads(path.read_text()) if path.exists() else {};streams=[];next_state=dict(state);now=time.time_ns();processed=0
 for service,name in NAMES[a.kind].items():
  result=subprocess.run(['docker','inspect','--format','{{.LogPath}}',name],capture_output=True,text=True,timeout=3)
  log=Path(result.stdout.strip())
  if result.returncode or not log.is_file():continue
  stat=log.stat();old=state.get(name,{});files=[]
  if old.get('inode')!=stat.st_ino and old.get('inode'):
   rotated=Path(str(log)+'.1')
   if rotated.exists() and rotated.stat().st_ino==old['inode']:files.append((rotated,old.get('position',0)))
  files.append((log,old.get('position',0) if old.get('inode')==stat.st_ino and old.get('position',0)<=stat.st_size else 0))
  values=[]
  for file,start in files:
   with file.open('rb') as f:
    f.seek(start);end=start
    for _ in range(10000):
     line=f.readline(262145)
     if not line or not line.endswith(b'\n'):break
     end=f.tell()
     if len(line)>262144:continue
     event=json.loads(line);ts=stamp(event['time'])
     # Bound initial backfill; Docker/CloudWatch remain the source for older history.
     if now-ts<900*10**9:values.append([str(ts),redact(event['log'].rstrip())])
     if end-start>=1024*1024:break
   if file==log:next_state[name]={'inode':stat.st_ino,'position':end}
  if values:
   streams.append({'stream':{'env':'production','service_name':service,'instance_id':a.instance,'resource_scope':'shared' if service=='embedding' else 'production'},'values':values});processed+=len(values)
 if streams:
  request=urllib.request.Request('http://plg.production.yeodam.internal:3100/loki/api/v1/push',data=json.dumps({'streams':streams}).encode(),headers={'Content-Type':'application/json'})
  with urllib.request.urlopen(request,timeout=8) as r:assert r.status==204
 tmp=path.with_suffix('.next');tmp.write_text(json.dumps(next_state));tmp.chmod(0o600);os.replace(tmp,path)
 metric=Path('/var/lib/node_exporter/textfile/host-logs.prom');metric.parent.mkdir(parents=True,exist_ok=True)
 tmp=metric.with_suffix('.next');tmp.write_text(f'yeodam_host_log_ship_timestamp_seconds {time.time()}\n');tmp.chmod(0o644);os.replace(tmp,metric)
 print(json.dumps({'kind':a.kind,'lines':processed,'streams':len(streams)}))
