#!/usr/bin/env python3
"""Summarize whitelisted read-response metadata; never emit HTTP bodies or credentials."""
import argparse
from collections import Counter
import json
from pathlib import Path

def stats(rows):
    values=sorted(r['elapsedMs'] for r in rows)
    return {'count':len(rows),'failures':sum(not r['ok'] for r in rows),
            'statuses':dict(Counter(str(r['status']) for r in rows)),
            'newTcp':sum(r.get('connectingMs',0)>0 for r in rows),
            'latencyMs':({k:values[int((len(values)-1)*q)] for k,q in [('p50',.5),('p95',.95),('p99',.99),('max',1)]} if values else {})}

def analyze(events,started_at,fence_at=None,enabled_at=None):
    rows=[]
    for e in events:
        if e.get('event')!='read_response':continue
        if e.get('kind') not in ('list','detail') or not isinstance(e.get('elapsedMs'),(int,float)) or e['elapsedMs']<0:raise ValueError('Invalid read observation')
        rows.append({k:e[k] for k in ('kind','at','elapsedMs','status','ok','connectingMs')})
    def window(left,right):
        return {kind:stats([r for r in rows if r['kind']==kind and left*1000<=r['at']<right*1000]) for kind in ('list','detail')}
    result={'all':{kind:stats([r for r in rows if r['kind']==kind]) for kind in ('list','detail')},
            'first360':window(started_at,started_at+360),
            'minutes':[{'fromSeconds':n,**window(started_at+n,started_at+n+60)} for n in range(0,900,60)],
            'scope':'Requests grouped by start time; latency includes response wait and JSON validation'}
    if fence_at is not None:
        result['beforeFence']=window(started_at,fence_at)
        if enabled_at is not None:
            result['duringFence']=window(fence_at,enabled_at)
            result['afterEnable']=window(enabled_at,started_at+900)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--events',type=Path,required=True);p.add_argument('--started-at',type=float,required=True);p.add_argument('--fence-at',type=float);p.add_argument('--enabled-at',type=float);a=p.parse_args()
    values=[]
    for line in a.events.read_text().splitlines():
        try:values.append(json.loads(line))
        except ValueError:pass
    print(json.dumps(analyze(values,a.started_at,a.fence_at,a.enabled_at)))
