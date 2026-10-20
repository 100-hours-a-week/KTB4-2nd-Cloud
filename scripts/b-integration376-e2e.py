#!/usr/bin/env python3
"""Prepare ten real refreshed sessions, then record all thirty V2 E2E responses."""
import argparse
from http.cookies import SimpleCookie
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen
from uuid import UUID

def request(base,path,session,method='GET',route=None,csrf=None):
    headers={'Cookie':'accessToken='+session['accessToken']+'; refreshToken='+session['refreshToken']+'; CSRF_CONTEXT='+session['csrfContext'],
             'X-CSRF-TOKEN':csrf or session['csrfToken']}
    if path=='/api/auth/token/refresh':
        headers['Cookie']='refreshToken='+session['refreshToken']+'; CSRF_CONTEXT='+session['csrfContext']
    if route:headers.update({'Host':'v1-staging.yeodam-2gether.com','X-Yeodam-A-Rehearsal':route})
    try:
        with urlopen(Request(base+path,data=b'' if method=='POST' else None,method=method,headers=headers),timeout=10) as r:
            body=r.read();jar=SimpleCookie()
            for c in r.headers.get_all('Set-Cookie') or []:jar.load(c)
            return r.status,body,{k:v.value for k,v in jar.items()}
    except HTTPError as e:return e.code,b'',{}
    except (URLError,TimeoutError,ConnectionError):return 0,b'',{}

def retry_request(*args,**kwargs):
    first=request(*args,**kwargs)
    if first[0] in (0,503):
        time.sleep(1);last=request(*args,**kwargs)
        return last,first[0],True
    return first,first[0],False

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('refresh','v1','e2e'));p.add_argument('--run-id',required=True);p.add_argument('--route-header');p.add_argument('--at',type=float);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id:raise ValueError('Canonical run UUID required')
    if a.at:
        while time.time()<a.at:time.sleep(max(0,min(.2,a.at-time.time())))
    actual_started=time.time()
    directory=Path('/run/yeodam-b383')/a.run_id
    path=directory/'e2e-pool.json'
    if a.action=='v1':
        pool=json.loads((Path('/run/yeodam-synthetic-sessions')/(a.run_id+'.json')).read_text())
        results=[]
        for s in pool['sessions'][-20:-10]:
            csrf=s['csrfToken']
            for operation,endpoint,method in [('refresh','/api/auth/token/refresh','POST'),('users_me','/api/users/me','GET'),('csrf','/api/auth/csrf','GET')]:
                status,body,cookies=request('https://v1-staging.yeodam-2gether.com',endpoint,s,method,csrf=csrf)
                if operation=='refresh' and status==200 and cookies.get('accessToken'):
                    s.update(accessToken=cookies['accessToken'],refreshToken=cookies.get('refreshToken',s['refreshToken']))
                if operation=='csrf' and status==200:
                    try:csrf=json.loads(body)['data']['token']
                    except (ValueError,KeyError,TypeError):pass
                results.append({'userId':s['userId'],'operation':operation,'status':status})
        report={'phase':'v1_e2e','runId':a.run_id,'requests':len(results),'success':sum(r['status']==200 for r in results),'scheduledAt':a.at,'startedAt':actual_started,'finishedAt':time.time(),'results':results}
        (directory/'e2e-v1-result.json').write_text(json.dumps(report));print(json.dumps(report))
    elif a.action=='refresh':
        if path.exists():raise RuntimeError('E2E refreshed pool already exists')
        pool=json.loads((Path('/run/yeodam-synthetic-sessions')/(a.run_id+'.json')).read_text())
        sessions=pool['sessions'][-10:];results=[]
        def prepare(s):
            (status,_,cookies),first_status,retried=retry_request('https://v1-staging.yeodam-2gether.com','/api/auth/token/refresh',s,'POST')
            probe=None
            if s is sessions[0] and status==200 and cookies.get('refreshToken'):
                # Deliberately do not install the first successful cookie, then retry the identical old credential.
                previous=dict(cookies)
                status,_,cookies=request('https://v1-staging.yeodam-2gether.com','/api/auth/token/refresh',s,'POST')
                probe={'status':status,'sameTokens':status==200 and cookies.get('refreshToken')==previous.get('refreshToken') and cookies.get('accessToken')==previous.get('accessToken')}
            if status==200 and cookies.get('accessToken') and cookies.get('refreshToken'):
                s.update(accessToken=cookies['accessToken'],refreshToken=cookies['refreshToken'])
            return {'userId':s['userId'],'status':status,'firstStatus':first_status,'retried':retried,'discardedCookieRecoveryProbe':probe}
        with ThreadPoolExecutor(max_workers=10) as workers:results=list(workers.map(prepare,sessions))
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump({'runId':a.run_id,'sessions':sessions},f)
        print(json.dumps({'phase':'v1_e2e_refresh','startedAt':actual_started,'finishedAt':time.time(),'results':results}))
    else:
        if str(UUID(a.route_header))!=a.route_header or path.stat().st_mode&0o077:raise ValueError('Route marker and private pool required')
        sessions=json.loads(path.read_text())['sessions'];results=[]
        for s in sessions:
            csrf=s['csrfToken']
            for operation,endpoint,method in [('refresh','/api/auth/token/refresh','POST'),('users_me','/api/users/me','GET'),('csrf','/api/auth/csrf','GET')]:
                status,body,cookies=request('https://staging.yeodam-2gether.com',endpoint,s,method,a.route_header,csrf)
                if operation=='refresh' and status==200 and cookies.get('accessToken'):
                    s.update(accessToken=cookies['accessToken'],refreshToken=cookies.get('refreshToken',s['refreshToken']))
                if operation=='csrf' and status==200:
                    try:csrf=json.loads(body)['data']['token']
                    except (ValueError,KeyError,TypeError):pass
                results.append({'userId':s['userId'],'operation':operation,'status':status})
        report={'phase':'v2_e2e','runId':a.run_id,'requests':len(results),'success':sum(r['status']==200 for r in results),'scheduledAt':a.at,'startedAt':actual_started,'finishedAt':time.time(),'results':results}
        (directory/'e2e-result.json').write_text(json.dumps(report));print(json.dumps(report))
if __name__=='__main__':main()
