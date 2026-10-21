#!/usr/bin/env python3
"""Observe accepted requests independently from the constant-arrival k6 injector."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import heapq
import http.client
import json
from pathlib import Path
import ssl
import threading
import time
from uuid import UUID

LOCAL=threading.local()
HOST='v1-staging.yeodam-2gether.com'

def query(request_id,token):
    try:
        if not hasattr(LOCAL,'connection'):LOCAL.connection=http.client.HTTPSConnection(HOST,timeout=3,context=ssl.create_default_context())
        connection=LOCAL.connection
        connection.request('GET','/api/migration/trips/requests/'+request_id,headers={'Cookie':'accessToken='+token})
        response=connection.getresponse();body=response.read();status=response.status
        data=json.loads(body).get('data',{}) if status==200 else {}
        return status,data
    except Exception:
        if hasattr(LOCAL,'connection'):
            LOCAL.connection.close();del LOCAL.connection
        return 0,{}

def main():
    p=argparse.ArgumentParser();p.add_argument('--run-id',required=True);a=p.parse_args();run=a.run_id
    if str(UUID(run))!=run:raise ValueError('Canonical Run UUID required')
    pool_path=Path('/opt/yeodam-load/source/tests/load/private')/(run+'.json')
    if pool_path.stat().st_mode&0o077:raise RuntimeError('Private pool required')
    pool=json.loads(pool_path.read_text());tokens={s['userId']:s['accessToken'] for s in pool['sessions']}
    directory=Path('/run/yeodam-b380')/run
    root=Path('/opt/yeodam-load/results/b380')/run
    state_path=directory/'k6.json'
    pending={};inflight={};queue=[];stream=None;buffer='';output=None;sequence=0;statuses=Counter()
    with ThreadPoolExecutor(max_workers=32) as executor:
        while True:
            files=list(root.glob('*/trip-events.jsonl'))
            if stream is None and len(files)==1:
                stream=files[0].open();output=(files[0].parent/'completion-events.jsonl').open('a')
            if stream is not None:
                buffer+=stream.read();lines=buffer.split('\n');buffer=lines.pop()
                for line in lines:
                    try:event=json.loads(line[line.index('{'):])
                    except (ValueError,KeyError):continue
                    if event.get('runId')!=run or event.get('event')!='first_response' or not event.get('accepted'):continue
                    request_id=event['requestId']
                    if request_id in pending:raise RuntimeError('Duplicate admission event')
                    pending[request_id]=event
                    sequence+=1;heapq.heappush(queue,(time.time()+event['retryAfterSeconds'],sequence,request_id))
            now=time.time()
            for future,request_id in list(inflight.items()):
                if not future.done():continue
                del inflight[future];status,data=future.result();statuses[status]+=1;event=pending[request_id]
                done=status==200 and data.get('status') in ('COMPLETED','FAILED')
                elapsed=int(now*1000-event['startedAt'])
                if done or elapsed>=30000:
                    ok=done and data.get('status')=='COMPLETED' and isinstance(data.get('tripId'),int)
                    result={'source':'migration-b376','event':'final','runId':run,'requestId':request_id,'iteration':event['iteration'],
                            'userId':event['userId'],'tripName':event['tripName'],'status':202,'tripId':data.get('tripId'),
                            'outcome':'confirmed' if ok else 'accepted_pending_or_failed','elapsedMs':elapsed,'statusHttp':status,'withinDeadline':elapsed<=30000}
                    output.write(json.dumps(result)+'\n');output.flush();del pending[request_id]
                else:
                    sequence+=1;heapq.heappush(queue,(now+event['retryAfterSeconds'],sequence,request_id))
            while queue and queue[0][0]<=now and len(inflight)<32:
                _,_,request_id=heapq.heappop(queue)
                if request_id not in pending:continue
                event=pending[request_id]
                future=executor.submit(query,request_id,tokens[event['userId']]);inflight[future]=request_id
            state=json.loads(state_path.read_text())
            if state.get('finishedAt') and not pending and not inflight:break
            if state.get('finishedAt') and now>state['finishedAt']+35:raise RuntimeError('Completion observer exceeded its fixed post-run drain')
            time.sleep(.1)
    if stream:stream.close()
    if output:output.close()
    (directory/'observer-stats.json').write_text(json.dumps({'pollStatuses':dict(statuses),'polls':sum(statuses.values()),'maxWorkers':32,'defaultPollSeconds':5}))
    print(json.dumps({'runId':run,'observer':'completed'}))
if __name__=='__main__':main()
