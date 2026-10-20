#!/usr/bin/env python3
"""Two public ACKs with one UUID; no Source trip write, one eventual Target trip."""
import argparse
import json
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from uuid import UUID,uuid5

def main():
    p=argparse.ArgumentParser();p.add_argument('--run-id',required=True);a=p.parse_args();run=a.run_id
    if str(UUID(run))!=run:raise ValueError('Canonical UUID required')
    pool=json.loads((Path('/run/yeodam-synthetic-sessions')/(run+'.json')).read_text());s=pool['sessions'][0]
    request_id=str(uuid5(UUID(run),'public-b2-smoke'));name='s376'+run[:5]
    headers={'Content-Type':'application/json','X-CSRF-TOKEN':s['csrfToken'],'X-Request-ID':request_id,
             'Cookie':'accessToken='+s['accessToken']+'; CSRF_CONTEXT='+s['csrfContext']}
    payload=json.dumps({'tripName':name,'startDate':'2025-01-01','endDate':'2025-01-03','regionCodes':['50110']}).encode()
    results=[]
    for _ in range(2):
        try:
            with urlopen(Request('https://v1-staging.yeodam-2gether.com/api/trips',data=payload,headers=headers,method='POST'),timeout=10) as r:status=r.status;data=json.load(r).get('data',{})
        except HTTPError as e:status=e.code;data={}
        results.append({'status':status,'requestId':data.get('requestId')})
        if status!=202 or data.get('requestId')!=request_id:raise RuntimeError('Public B2 UUID/ACK contract failed with HTTP '+str(status))
    p=subprocess.run(['mysql','--protocol=socket','-uroot','-N','-B','-e',"SELECT COUNT(*) FROM yeodam_v1_source.trips WHERE trip_name='"+name+"';"],capture_output=True,text=True)
    if p.returncode or p.stdout.strip()!='0':raise RuntimeError('Public queue wrote a Source trip')
    print(json.dumps({'runId':run,'phase':'public_smoke','responses':results,'sourceTripRows':0,'excludedFromRps':True}))
if __name__=='__main__':main()
