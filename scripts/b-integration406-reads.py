#!/usr/bin/env python3
"""Select existing readable trips; never mutate business rows or print credentials."""
import argparse
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from uuid import UUID

def prepare(run):
    pool=json.loads((Path('/run/yeodam-synthetic-sessions')/(run+'.json')).read_text())
    if pool['runId']!=run or len(pool['sessions'])!=140:raise RuntimeError('Read actor pool differs')
    ids=[int(v['userId']) for v in pool['sessions'][120:140]]
    if len(set(ids))!=20:raise RuntimeError('Read users not unique')
    m=SourceFileLoader('read406','/run/yeodam-b391/b-integration376-control.py').load_module();d=m.database()
    try:
        rows=d.query('source',"SELECT user_id,MIN(trip_id) FROM yeodam_v1_source.trips WHERE user_id IN ("+','.join(map(str,ids))+") AND deleted_at IS NULL AND processing_status='COMPLETED' GROUP BY user_id;")
        trips={int(line.split('\t')[0]):int(line.split('\t')[1]) for line in rows.splitlines()}
    finally:d.close()
    if set(trips)!=set(ids):raise RuntimeError('Existing completed read fixture missing; no rows modified')
    value={'runId':run,'fixtures':[{'poolIndex':120+i,'userId':u,'tripId':trips[u]} for i,u in enumerate(ids)]}
    path=Path('/run/yeodam-b391')/run/'read-fixtures.json'
    if path.exists():raise RuntimeError('Read fixture already exists')
    path.write_text(json.dumps(value));path.chmod(0o600)
    print(json.dumps({'readFixturesPrepared':20,'existingTripsReused':True,'businessRowsChanged':0}))

def verify(run):
    root=Path('/run/yeodam-b391')/run
    value=json.loads((root/'read-fixtures.json').read_text())
    pool=json.loads((Path('/run/yeodam-synthetic-sessions')/(run+'.json')).read_text())
    fixture=value['fixtures'][0];session=pool['sessions'][fixture['poolIndex']]
    headers={'Cookie':'accessToken='+session['accessToken']+'; CSRF_CONTEXT='+session['csrfContext']}
    for kind,path in [('list','/api/trips'),('detail','/api/trips/'+str(fixture['tripId']))]:
        try:
            with urlopen(Request('https://v1-staging.yeodam-2gether.com'+path,headers=headers),timeout=15) as r:
                data=json.load(r).get('data');status=r.status
        except HTTPError as error:raise RuntimeError('Read fixture HTTP '+str(error.code)) from None
        valid=isinstance(data,dict) and (isinstance(data.get('items'),list) if kind=='list' else data.get('tripId')==fixture['tripId'])
        if status!=200 or not valid:raise RuntimeError('Read fixture response shape invalid')
        print(json.dumps({'readFixtureVerified':kind,'status':status}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','metadata','verify'));p.add_argument('--run-id',required=True);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id or '10.30.0.174' not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Unexpected Source or Run')
    if a.action=='metadata':print((Path('/run/yeodam-b391')/a.run_id/'read-fixtures.json').read_text())
    else:globals()[a.action](a.run_id)
