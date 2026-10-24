#!/usr/bin/env python3
"""Reuse three authenticated HTTP/1.1 clients across fixed idle gaps on V1 staging."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import argparse,hashlib,json,os,time,threading
from pathlib import Path
from uuid import UUID
import requests

HOST='https://v1-staging.yeodam-2gether.com'
GAPS=[0,2,8,20,40]
ROUTES=['/api/trips/map','/api/trips','/api/trips/2048046']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id',required=True)
    p.add_argument('--session-run-id',required=True)
    p.add_argument('--expected-site-sha256',required=True)
    a=p.parse_args();UUID(a.run_id);UUID(a.session_run_id);os.umask(0o077)
    if os.geteuid()!=0 or Path('/var/lib/cloud/data/instance-id').read_text().strip()!='i-05d320e3a87200d5a':
        p.error('Root on V1 staging App only')
    if hashlib.sha256(Path('/etc/nginx/sites-enabled/yeodam-v1-staging').read_bytes()).hexdigest()!=a.expected_site_sha256:
        p.error('Site changed')
    pool_path=Path('/run/yeodam-nginx-read-fixture')/(a.session_run_id+'.json')
    if pool_path.stat().st_mode & 0o077:p.error('Private fixture file required')
    pool=json.loads(pool_path.read_text())
    if (pool['runId']!=a.session_run_id or pool['baseUrl']!=HOST or len(pool['sessions'])!=1 or
            pool['sessions'][0]['userId']!=11007 or
            (datetime.fromisoformat(pool['expiresAt'])-datetime.now(timezone.utc)).total_seconds()<180):
        p.error('Expected current single synthetic read fixture session')
    root=Path('/var/lib/yeodam/nginx-keepalive-424/reconnect')/a.run_id
    root.mkdir(parents=True,exist_ok=False)
    cookie='accessToken='+pool['sessions'][0]['accessToken']
    stop=threading.Event()
    def client(index):
        rows=[]
        with requests.Session() as session:
            session.trust_env=False
            session.headers.update({'Cookie':cookie,'User-Agent':'yeodam-nginx424-reconnect'})
            for gap in GAPS:
                if stop.wait(gap):break
                started=time.monotonic();row={'client':index,'gapSeconds':gap,'route':ROUTES[index],'utc':datetime.now(timezone.utc).isoformat()}
                try:
                    with session.get(HOST+ROUTES[index],timeout=(3,5),allow_redirects=False,stream=True) as response:
                        connection=response.raw._connection
                        sock=getattr(connection,'sock',None)
                        row.update(status=response.status_code,requestId=response.headers.get('X-Request-ID'),localPort=sock.getsockname()[1] if sock else None)
                        size=0
                        for chunk in response.iter_content(65536):
                            size+=len(chunk)
                            if size>2_000_000:raise ValueError('Response exceeds bound')
                        row['durationMs']=round((time.monotonic()-started)*1000,3)
                        if response.status_code!=200 or row['durationMs']>1000:
                            raise ValueError('Normal request failed or exceeded initial one-second guard')
                except Exception as error:
                    row['failureType']=type(error).__name__;rows.append(row);stop.set();return rows
                rows.append(row)
        return rows
    started=datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=3) as executor:results=list(executor.map(client,range(3)))
    rows=[r for items in results for r in items];complete=len(rows)==15 and not any('failureType' in r for r in rows)
    result={'runId':a.run_id,'startedAt':started,'endedAt':datetime.now(timezone.utc).isoformat(),'complete':complete,'requestsVersion':requests.__version__,'protocol':'HTTP/1.1','gapsSeconds':GAPS,'rows':rows,'httpApplicationRetries':0,'scope':'Three API clients of the same synthetic account; not browser UI or three independent users'}
    (root/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps({'runId':a.run_id,'complete':complete,'requests':len(rows),'failures':sum('failureType' in r for r in rows)}))
    return 0 if complete else 1


if __name__=='__main__':raise SystemExit(main())
