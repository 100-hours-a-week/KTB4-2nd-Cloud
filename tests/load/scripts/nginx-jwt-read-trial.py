#!/usr/bin/env python3
"""Finite, explicitly approved V1 edge trial. Never invoked by CI or preparation."""
import argparse,json,os,time,threading,signal,http.client,ssl,socket,statistics,subprocess,re,math
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
from uuid import uuid4, UUID

HOST='v1-staging.yeodam-2gether.com'
BASE=Path('/var/lib/yeodam/jwt-read468-trials')

def utc():return datetime.now(timezone.utc).isoformat()

def percentile95(values):
 values=sorted(values)
 return values[max(0,math.ceil(len(values)*.95)-1)] if values else None

def main():
 p=argparse.ArgumentParser();p.add_argument('--case',required=True,choices=['baseline','unauth40','forged40','auth20','auth40','sameuser8']);p.add_argument('--role',choices=['app','generator'],required=True);p.add_argument('--start-at',required=True);p.add_argument('--trial',required=True);p.add_argument('--session-run-id',required=True);p.add_argument('--execute',action='store_true');p.add_argument('--approved-run',required=True);a=p.parse_args()
 os.umask(0o077)
 assert a.execute and a.approved_run==a.trial, 'Explicit approved trial required'
 UUID(a.trial);UUID(a.session_run_id)
 assert os.geteuid()==0
 for proc in Path('/proc').iterdir():
  if proc.name.isdigit() and int(proc.name)!=os.getpid():
   try: assert (proc/'comm').read_text().strip() not in ('k6','h2load','wrk','vegeta'),'Another load tool is running'
   except OSError: pass
 expected={'app':'i-05d320e3a87200d5a','generator':'i-0c53b65eb35dd2b42'}
 assert Path('/var/lib/cloud/data/instance-id').read_text().strip()==expected[a.role]
 direct=False
 resolved={x[4][0] for x in socket.getaddrinfo(HOST,443,type=socket.SOCK_STREAM)}
 assert resolved and resolved<={'3.37.13.241','10.30.0.174'}
 pool_path=Path('/run/yeodam-jwt-read468/pool.json');assert not pool_path.is_symlink() and not pool_path.stat().st_mode&0o077 and not pool_path.parent.stat().st_mode&0o077
 pool=json.loads(pool_path.read_text());assert pool['runId']==a.session_run_id and pool['baseUrl']=='https://'+HOST and [x['userId'] for x in pool['sessions']]==[11007,107,107]
 assert (datetime.fromisoformat(pool['expiresAt'])-datetime.now(timezone.utc)).total_seconds()>120
 tokens=[x['accessToken'] for x in pool['sessions']]
 forged=tokens[0].split('.');forged[2]=('A' if forged[2][0]!='A' else 'B')+forged[2][1:];forged='.'.join(forged)
 duration=10 if a.case=='baseline' else 20
 root=BASE/a.trial/a.role;root.mkdir(mode=0o700,parents=True,exist_ok=False)
 start=datetime.fromisoformat(a.start_at.replace('Z','+00:00')).timestamp();assert 1<start-time.time()<45
 cohorts=[]
 if a.role=='app':cohorts=[('control',0 if a.case=='sameuser8' else 2,1,.075)]
 elif a.case=='baseline':cohorts=[('normal',1,1,.025)]
 elif a.case=='sameuser8':cohorts=[('session-a',1,4,.025),('session-b',2,4,.125),('normal',0,1,.075)]
 else:
  rate=20 if a.case=='auth20' else 40
  index=None if a.case=='unauth40' else (3 if a.case=='forged40' else 0)
  cohorts=[('load',index,rate/4,i/rate) for i in range(4)]+[('normal',1,1,.037)]
 stop=threading.Event();lock=threading.Lock();counts=Counter();rows=[];dropped=[];reasons=[]
 for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:(reasons.append('signal'),stop.set()))
 (root/'plan.json').write_text(json.dumps({'case':a.case,'role':a.role,'startAt':a.start_at,'seconds':duration,'directBackend':direct,'route':'/api/trips','automaticRetries':False,'cohorts':[{'name':n,'sessionIndex':idx,'rate':rate,'offset':off} for n,idx,rate,off in cohorts]},indent=2))
 stream=(root/'requests.jsonl').open('x')
 epoch=time.monotonic()+start-time.time()
 def worker(name,idx,rate,offset):
  connection=None
  try:
   for n in range(int(duration*rate)):
    scheduled=offset+n/rate
    if stop.wait(max(0,epoch+scheduled-time.monotonic())):break
    if time.monotonic()-epoch-scheduled>.5:
     with lock:dropped.append({'cohort':name,'sequence':n});reasons.append('schedule-lag')
     stop.set();break
    marker=str(uuid4());at=utc();began=time.monotonic();status=0;rid=scope=edge_scope=message=error=None
    try:
     if connection is None:connection=http.client.HTTPConnection('127.0.0.1',8080,timeout=2) if direct else http.client.HTTPSConnection(HOST,timeout=2,context=ssl.create_default_context())
     headers={'X-Migration-Run-Id':marker,'User-Agent':'yeodam-jwt-read468/'+a.case+'/'+name}
     if idx is not None:headers['Cookie']='accessToken='+(forged if idx==3 else tokens[idx])
     connection.request('GET','/api/trips',headers=headers);r=connection.getresponse();status=r.status;rid=r.getheader('X-Request-ID');scope=r.getheader('X-RateLimit-Scope');edge_scope=r.getheader('X-Edge-RateLimit-Scope');body=r.read(2_000_001)
     assert len(body)<=2_000_000
     if status==429:
      try:message=json.loads(body).get('message')
      except (ValueError,AttributeError):pass
     if r.will_close:connection.close();connection=None
    except Exception as e:
     error=type(e).__name__
     if connection:connection.close();connection=None
    latency=round((time.monotonic()-began)*1000,3)
    invalid_load=name=='load' and a.case in ('unauth40','forged40')
    allowed=(401,429) if invalid_load else ((200,) if name in ('normal','control') else (200,429))
    expected_edge='edge-invalid-read' if invalid_load else 'edge-user-read'
    bad=error or status not in allowed or latency>1000 or edge_scope!=expected_edge or not re.fullmatch('[a-f0-9]{32}',rid or '')
    row={'at':at,'endAt':utc(),'cohort':name,'status':status,'requestId':rid,'probeId':marker,'scope':scope,'edgeScope':edge_scope,'message':message,'durationMs':latency,'errorType':error,'scheduledSecond':round(scheduled,3),'directBackend':direct,'trial':a.trial}
    with lock:
     rows.append(row);counts[name+':'+str(status)]+=1;stream.write(json.dumps(row)+'\n');stream.flush()
     if bad:reasons.append('request-guard');stop.set()
  finally:
   if connection:connection.close()
 threads=[threading.Thread(target=worker,args=x) for x in cohorts]
 for t in threads:t.start()
 high=0;prev=None
 resources=(root/'guard-resources.jsonl').open('x')
 while any(t.is_alive() for t in threads):
  if a.role=='app':
   cpu=[int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
   if prev:
    diff=[x-y for x,y in zip(cpu,prev)];pct=100*(1-(diff[3]+diff[4])/sum(diff)) if sum(diff)>0 else 0
    high=high+1 if pct>=70 and time.monotonic()>=epoch else 0
    resources.write(json.dumps({'at':utc(),'cpuPercent':round(pct,3),'highSamples':high,'inMeasurement':time.monotonic()>=epoch})+'\n');resources.flush()
    if high>=2:
     with lock:reasons.append('cpu-70-for-two-samples');stop.set()
   prev=cpu
  time.sleep(.5)
 for t in threads:t.join()
 resources.close()
 if reasons and a.role=='app' and not direct:
  subprocess.run(['python3','/opt/yeodam/ops/jwt-gate468/jwt-gate/prepare.py','rollback','--confirm-instance','i-05d320e3a87200d5a'],capture_output=True,check=True)
 stream.close()
 ms=sorted(x['durationMs'] for x in rows)
 result={'case':a.case,'role':a.role,'trial':a.trial,'startedAt':a.start_at,'endedAt':utc(),'seconds':duration,'counts':dict(counts),'requests':len(rows),'dropped':len(dropped),'guardReasons':reasons,'expectedRequests':sum(int(duration*x[2]) for x in cohorts),'completed':not reasons and len(rows)==sum(int(duration*x[2]) for x in cohorts),'p95Ms':percentile95(ms),'maxMs':max(ms,default=0),'directBackend':direct,'trial':a.trial}
 (root/'summary.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
 if reasons:raise SystemExit(2)

if __name__=='__main__':main()
