#!/usr/bin/env python3
"""Summarize safe completion timing records; no payloads or credentials required."""
import argparse
import json
from pathlib import Path

def stats(values):
    values=sorted(values)
    return {'count':len(values),**({name:values[int((len(values)-1)*q)] for name,q in [('p50',.5),('p95',.95),('p99',.99),('max',1)]} if values else {})}

def summarize(rows,started_at):
    timed=[r for r in rows.values() if 'consumerStartedAtMs' in r]
    def group(selected):
        return {'queueWaitMs':stats([r['consumerStartedAtMs']-r['queuedAtMs'] for r in selected]),
                'applyElapsedMs':stats([r['applyElapsedUs']/1000 for r in selected]),
                'totalObservedMs':stats([r['committedAtMs']-r['queuedAtMs'] for r in selected])}
    by_origin={}
    for origin in ('source','target'):
        selected=[r for r in timed if r['origin']==origin]
        partitions={}
        for partition in range(4):
            records=sorted((r for r in selected if r['partition']==partition),key=lambda r:r['consumerStartedAtMs'])
            partitions[str(partition)]={**group(records),
                # Includes offset acknowledgement, polling and scheduling, not only broker time.
                'betweenRecordsMs':stats([b['consumerStartedAtMs']-a['committedAtMs'] for a,b in zip(records,records[1:])])}
        by_origin[origin]={**group(selected),'partitions':partitions}
    return {'totalRecords':len(rows),'timedRecords':len(timed),'byOrigin':by_origin,
            'byCommandMinute':{str(minute):group([r for r in timed if minute*60000 <= r['queuedAtMs']-started_at*1000 < (minute+1)*60000]) for minute in range(15)}}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('file',type=Path);p.add_argument('--started-at',type=float,required=True);a=p.parse_args()
    print(json.dumps(summarize(json.loads(a.file.read_text()),a.started_at),indent=2))
