#!/usr/bin/env python3
"""Sample Source CPU/process deltas without payloads, SQL, HTTP probes or mutation."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import UUID

NAMES={'java','mysqld','mysqldump','gzip','aws','nginx','proxysql'}

def snapshot():
    cpu=list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
    processes={}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            value=path.read_text();end=value.rindex(')');name=value[value.index('(')+1:end]
            if name not in NAMES:continue
            fields=value[end+2:].split()
            # starttime prevents PID reuse from making an unrelated delta.
            processes[(int(path.parent.name),int(fields[19]))]=(name,int(fields[11])+int(fields[12]))
        except (OSError,ValueError,IndexError):continue
    return cpu,processes

def delta(before,after,seconds,ticks):
    old_cpu,old=before;cpu,current=after
    total=sum(cpu[:8])-sum(old_cpu[:8]);idle=cpu[3]+cpu[4]-old_cpu[3]-old_cpu[4]
    groups={}
    for pid,(name,value) in current.items():
        if pid not in old:continue
        groups[name]=groups.get(name,0)+max(0,value-old[pid][1])/ticks/seconds*100
    return {'hostBusyPercent':100*(total-idle)/total if total else None,
            'hostIowaitPercent':100*(cpu[4]-old_cpu[4])/total if total else None,
            'processCpuOneCorePercent':groups}

def main():
    p=argparse.ArgumentParser();p.add_argument('--run-id',required=True);p.add_argument('--started-at',type=float,required=True);p.add_argument('--summarize',action='store_true');a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id or '10.30.0.174' not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Wrong Source or Run')
    root=Path('/run/yeodam-b391')/a.run_id
    if not root.is_dir():raise RuntimeError('Run not prepared')
    path=root/'resource-samples.jsonl'
    if a.summarize:
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        for minute in range(15):
            selected=[r for r in rows if minute*60 <= r['elapsed'] < (minute+1)*60]
            if not selected:continue
            print(json.dumps({'elapsedFrom':minute*60,'samples':len(selected),
                'hostBusyMean':sum(r['hostBusyPercent'] for r in selected)/len(selected),
                'hostIowaitMean':sum(r['hostIowaitPercent'] for r in selected)/len(selected),
                'processCpuOneCoreMean':{name:sum(r['processCpuOneCorePercent'].get(name,0) for r in selected)/len(selected) for name in NAMES}}))
        return
    with path.open('x') as output:
        path.chmod(0o600);before=snapshot();last=time.monotonic();count=0
        while time.time()<a.started_at+900:
            time.sleep(5);after=snapshot();now=time.monotonic()
            output.write(json.dumps({'at':time.time(),'elapsed':time.time()-a.started_at,**delta(before,after,now-last,os.sysconf('SC_CLK_TCK'))})+'\n');output.flush()
            count+=1;before=after;last=now
    print(json.dumps({'phase':'resource_samples_complete','runId':a.run_id,'count':count,'path':str(path)}))

if __name__=='__main__':main()
