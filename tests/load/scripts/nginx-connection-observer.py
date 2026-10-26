#!/usr/bin/env python3
"""Observe only V1 staging and one authenticated read fixture; never export its cookie."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import time
from uuid import UUID

HOST = 'v1-staging.yeodam-2gether.com'
INSTANCE = 'i-05d320e3a87200d5a'
SITE_SHA = '57dcf4d79f853496791c7355d29a4d272fd8871d9231952a23ab046668925096'
ROUTES = ['/api/trips/map', '/api/trips', '/api/trips/2048046']


def parse_stub(text):
    values = re.search(r'Active connections:\s*(\d+).*?\n\s*(\d+)\s+(\d+)\s+(\d+).*?Reading:\s*(\d+)\s+Writing:\s*(\d+)\s+Waiting:\s*(\d+)', text, re.S)
    if not values:
        raise ValueError('Unexpected stub_status format')
    return dict(zip(['active','accepts','handled','requests','reading','writing','waiting'], map(int, values.groups())))


def peak_values(rows):
    # The first CPU interval begins immediately after startup, not one second earlier.
    return {key: max((row.get(key,0) for row in (rows[1:] if key == 'cpuPercent' else rows)), default=0)
            for key in ['active','waiting','httpsEstablished','cpuPercent','diskPercent']}


def request(path, cookie=None, loopback=False, timeout=3):
    started = time.monotonic()
    connection = (http.client.HTTPConnection('127.0.0.1', 8086, timeout=timeout) if loopback else
                  http.client.HTTPSConnection(HOST, timeout=timeout, context=ssl.create_default_context()))
    try:
        headers = {'User-Agent': 'yeodam-nginx422-normal-observer'}
        if cookie:
            headers['Cookie'] = cookie
        connection.request('GET', path, headers=headers)
        response = connection.getresponse()
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError('Response exceeds observer bound')
        return response.status, response.getheader('X-Request-ID'), (time.monotonic()-started)*1000, body
    finally:
        connection.close()


def cpu():
    values = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
    return sum(values[:8]), values[3]+values[4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--seconds', type=int, required=True)
    parser.add_argument('--session-run-id', required=True)
    parser.add_argument('--expected-site-sha256', default=SITE_SHA)
    parser.add_argument('--experiment', choices=['422','424','426'], default='422')
    parser.add_argument('--normal-only', action='store_true')
    args = parser.parse_args()
    UUID(args.run_id);UUID(args.session_run_id)
    if not re.fullmatch(r"[a-f0-9]{64}",args.expected_site_sha256):
        parser.error("Expected explicit SHA256")
    maximum_seconds = 1500 if args.experiment == '426' and args.normal_only else 180
    if (not 10 <= args.seconds <= maximum_seconds or os.geteuid() != 0 or
            Path('/var/lib/cloud/data/instance-id').read_text().strip() != INSTANCE):
        parser.error('Root on exact V1 staging App; bounded duration required')
    if hashlib.sha256(Path('/etc/nginx/sites-enabled/yeodam-v1-staging').read_bytes()).hexdigest() != args.expected_site_sha256:
        parser.error('Read policy changed; inspect before starting')
    pool_path = Path('/run/yeodam-nginx-read-fixture')/(args.session_run_id+'.json')
    if pool_path.stat().st_mode & 0o077:
        parser.error('Private session file required')
    pool = json.loads(pool_path.read_text())
    if (pool.get('runId') != args.session_run_id or len(pool['sessions']) != 1 or
            pool['sessions'][0]['userId'] != 11007 or pool['baseUrl'] != 'https://'+HOST or
            (datetime.fromisoformat(pool['expiresAt'])-datetime.now(timezone.utc)).total_seconds() < args.seconds+60):
        parser.error('Expected current single synthetic fixture session')
    cookie = 'accessToken='+pool['sessions'][0]['accessToken']
    os.umask(0o077)
    root = Path('/var/lib/yeodam/nginx-connections-'+args.experiment)/args.run_id
    root.mkdir(parents=True, exist_ok=False)
    rows=[];normal=[];before=cpu();start=time.monotonic();failure=None
    with (root/'samples.jsonl').open('x') as stream:
        for index in range(args.seconds):
            time.sleep(max(0,start+index-time.monotonic()))
            row={'utc':datetime.now(timezone.utc).isoformat()}
            try:
                if not args.normal_only:
                    status,_,_,body=request('/stub_status',loopback=True)
                    if status != 200:raise ValueError('Stub unavailable')
                    row.update(parse_stub(body.decode()))
                    after=cpu();delta=after[0]-before[0]
                    row['cpuPercent']=round(100*(1-(after[1]-before[1])/delta),2) if delta else 0
                    before=after;disk=shutil.disk_usage('/');row['diskPercent']=round(100*disk.used/disk.total,2)
                    row['httpsEstablished']=len(subprocess.check_output(['ss','-Htn','state','established','sport','=',':443'],text=True,timeout=2).splitlines())
                if index % 2 == 0:
                    path=ROUTES[(index//2)%len(ROUTES)]
                    code,rid,ms,_=request(path,cookie)
                    item={'utc':row['utc'],'route':path,'status':code,'requestId':rid,'durationMs':round(ms,3)}
                    normal.append(item);row['normal']=item
                    if code != 200 or (args.normal_only and ms > 1000):raise ValueError('Normal request failed or exceeded initial one-second guard')
                if row.get('diskPercent',0) >= 80:raise ValueError('Disk stop threshold')
            except Exception as error:
                failure=type(error).__name__;row['failureType']=failure
            rows.append(row);stream.write(json.dumps(row)+'\n');stream.flush()
            if failure or (root/'STOP').exists():break
    complete=failure is None and (len(rows)==args.seconds or (args.normal_only and (root/'STOP').exists()))
    summary={'runId':args.run_id,'complete':complete,'sampleCount':len(rows),'normalRequests':len(normal),
             'normalStatuses':dict(Counter(str(r['status']) for r in normal)),
             'failureType':failure,'siteUnchanged':hashlib.sha256(Path('/etc/nginx/sites-enabled/yeodam-v1-staging').read_bytes()).hexdigest()==args.expected_site_sha256,
             'peaks':None if args.normal_only else peak_values(rows),
             'mode':'normal-only' if args.normal_only else 'observer',
             'endedReason':'requested-stop' if (root/'STOP').exists() else ('error' if failure else 'duration'),
             'normalProbeSource':'V1 App via public HTTPS; distinct from EC2 load generator',
             'normalScope':'Authenticated API probes, not browser UI or uploaded photo/AI validation'}
    (root/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)
    return 0 if complete and summary['siteUnchanged'] else 1


if __name__=='__main__':
    raise SystemExit(main())
