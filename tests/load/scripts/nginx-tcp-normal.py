#!/usr/bin/env python3
"""Nine persistent HTTP/1.1 clients of three synthetic users behind one generator IP."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json,os,time
from pathlib import Path
import requests

HOST='https://v1-staging.yeodam-2gether.com'
ROUTES=['/api/trips/map','/api/trips','/api/users/me']

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--pool',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 a=p.parse_args();os.umask(0o077)
 if Path('/var/lib/cloud/data/instance-id').read_text().strip()!='i-0c53b65eb35dd2b42' or os.geteuid()!=0:p.error('Existing generator root only')
 if a.pool.stat().st_mode&0o077:p.error('Private pool required')
 data=json.loads(a.pool.read_text())
 if data['baseUrl']!=HOST or len(data['sessions'])!=3 or (datetime.fromisoformat(data['expiresAt'])-datetime.now(timezone.utc)).total_seconds()<120:p.error('Three fresh staging synthetic sessions required')
 with a.output.open('x') as out:
  def one(index):
   rows=[]
   with requests.Session() as s:
    s.trust_env=False;s.headers.update({'Cookie':'accessToken='+data['sessions'][index//3]['accessToken'],'User-Agent':'yeodam-tcp436-normal'})
    for round in range(4):
     at=time.monotonic();r={'utc':datetime.now(timezone.utc).isoformat(),'client':index,'round':round,'route':ROUTES[index%3]}
     try:
      with s.get(HOST+ROUTES[index%3],stream=True,timeout=(3,5),allow_redirects=False) as response:
       sock=getattr(response.raw._connection,'sock',None)
       r.update(status=response.status_code,requestId=response.headers.get('X-Request-ID'),localPort=sock.getsockname()[1] if sock else None)
       total=0
       for chunk in response.iter_content(65536):
        total+=len(chunk)
        if total>2_000_000:raise ValueError('Response too large')
       r['durationMs']=(time.monotonic()-at)*1000
     except Exception as e:r['error']=type(e).__name__
     rows.append(r)
     if round<3:time.sleep(8)
   return rows
  start=datetime.now(timezone.utc).isoformat()
  with ThreadPoolExecutor(max_workers=9) as ex:rows=[r for group in ex.map(one,range(9)) for r in group]
  result={'startedAt':start,'endedAt':datetime.now(timezone.utc).isoformat(),'scope':'Three synthetic users, nine API clients, same generator source; not browser UI or a large NAT guarantee','rows':rows,'complete':len(rows)==36 and all(r.get('status')==200 and r.get('durationMs',9999)<1000 for r in rows)}
  json.dump(result,out)
 print(json.dumps({'complete':result['complete'],'requests':len(rows),'statuses':{str(k):sum(r.get('status')==k for r in rows) for k in set(r.get('status') for r in rows)},'maxDurationMs':max((r.get('durationMs',0) for r in rows),default=0)}))

if __name__=='__main__':main()
