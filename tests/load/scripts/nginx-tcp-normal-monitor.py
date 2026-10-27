#!/usr/bin/env python3
"""Independent staging read probe; rotate only its own synthetic session on App."""
import argparse
from datetime import datetime,timezone
import hashlib,importlib.util,json,os,secrets,signal,subprocess,time
from pathlib import Path
from uuid import UUID,uuid4

def module(name,file):
 s=importlib.util.spec_from_file_location(name,Path(__file__).with_name(file));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
pool=module('pool436','synthetic-session-pool.py')
observer=module('observer436','nginx-connection-observer.py')
USER=11007
EMAIL='mig-df3d52be-79e0-4984-8cf5-b7ae1d73ffd8-0000@staging.invalid'

def now():return datetime.now(timezone.utc).isoformat()
def revoke(sid):
 pool.run_mysql('DELETE FROM login_sessions WHERE user_id=11007 AND sid='+pool.sql_literal(sid)+';')
 if pool.run_mysql('SELECT COUNT(*) FROM login_sessions WHERE user_id=11007 AND sid='+pool.sql_literal(sid)+';')!='0':
  raise ValueError('Owned session revocation failed')
def issue(owned):
 if pool.run_mysql('SELECT email FROM users WHERE user_id=11007;')!=EMAIL:raise ValueError('Synthetic owner changed')
 if pool.run_mysql('SELECT @@read_only,@@super_read_only;')!='0\t0':raise ValueError('Source is fenced')
 d=json.loads(subprocess.check_output(['docker','inspect','yeodam-app-backend-1'],text=True,timeout=5))[0]
 env=dict(x.split('=',1) for x in d['Config']['Env'] if '=' in x)
 secret,issuer=env['JWT_SECRET'],env['JWT_ISSUER']
 if len(secret.encode())<32 or not issuer.startswith('https://'):raise ValueError('Unexpected runtime JWT contract')
 sid=str(uuid4());owned.add(sid);at=datetime.now(timezone.utc)
 token=pool.access_token(USER,sid,secret,issuer,at)
 pool.run_mysql('INSERT INTO login_sessions (sid,user_id,refresh_token_hash,expires_at) VALUES ('+
                pool.sql_literal(sid)+',11007,'+pool.sql_literal(hashlib.sha256(secrets.token_bytes(32)).hexdigest())+
                ',DATE_ADD(NOW(6),INTERVAL 30 MINUTE));')
 return sid,token,(at+pool.TOKEN_TTL).isoformat()

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-id',required=True);p.add_argument('--seconds',type=int,required=True)
 p.add_argument('--rotation-test',action='store_true',help='Only a <=60s integration check: rotate after20s instead of1200s')
 a=p.parse_args();UUID(a.run_id);os.umask(0o077)
 if os.geteuid()!=0 or Path('/var/lib/cloud/data/instance-id').read_text().strip()!='i-05d320e3a87200d5a':p.error('Exact staging App root only')
 if not 10<=a.seconds<=14400:p.error('Bounded 10..14400 seconds')
 if a.rotation_test and a.seconds>60:p.error('Short integration check only')
 interval=20 if a.rotation_test else 1200
 root=Path('/var/lib/yeodam/nginx-connections-426')/a.run_id;root.mkdir(parents=True,exist_ok=False)
 stopped=False
 def terminate(*_):
  nonlocal stopped
  stopped=True
 for sig in [signal.SIGINT,signal.SIGTERM]:signal.signal(sig,terminate)
 sid=token=None;owned=set();renew=0;failures=0;rotations=0;count=0;start=time.monotonic();began=now();last=None
 try:
  with (root/'samples.jsonl').open('x') as out:
   while not stopped and time.monotonic()-start<a.seconds and not (root/'STOP').exists():
    row={'utc':now()}
    try:
     if time.monotonic()>=renew:
      renew=time.monotonic()+10
      new_sid,new_token,expires=issue(owned)
      if sid:revoke(sid);owned.discard(sid)
      sid,token=new_sid,new_token;renew=time.monotonic()+interval;rotations+=1
      row['sessionExpiresAt']=expires
     route=observer.ROUTES[count%len(observer.ROUTES)]
     if not token:raise ValueError('No valid synthetic session; issuance retry scheduled')
     code,rid,ms,_=observer.request(route,cookie='accessToken='+token)
     row['normal']={'route':route,'status':code,'requestId':rid,'durationMs':round(ms,3)}
     if code!=200 or ms>1000:raise ValueError('Normal request failed or exceeded initial guard')
    except Exception as error:
     failures+=1;row['failureType']=type(error).__name__
    count+=1;row['failureCount']=failures;out.write(json.dumps(row)+'\n');out.flush()
    checkpoint={'runId':a.run_id,'startedAt':began,'updatedAt':now(),'sampleCount':count,'failureCount':failures,'sessionRotations':rotations,'rotationIntervalSeconds':interval,'last':row,'captureLifecycleIndependent':True}
    temporary=root/'checkpoint.tmp';temporary.write_text(json.dumps(checkpoint));temporary.replace(root/'checkpoint.json');last=checkpoint
    for _ in range(20):
     if stopped or (root/'STOP').exists():break
     time.sleep(.1)
 finally:
  cleanup_errors=[]
  for owned_sid in owned:
   try:revoke(owned_sid)
   except Exception as error:cleanup_errors.append(type(error).__name__)
  if last:
   last.update(endedAt=now(),complete=failures==0 and not cleanup_errors,endedReason='requested-stop' if stopped or (root/'STOP').exists() else 'duration',credentialsRevoked=not cleanup_errors,cleanupErrors=cleanup_errors)
   (root/'summary.json').write_text(json.dumps(last,indent=2))
   print(json.dumps(last))
 return 0 if failures==0 else 1

if __name__=='__main__':raise SystemExit(main())
