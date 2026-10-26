#!/usr/bin/env python3
"""Read three Source socket samples without HTTP probes or configuration changes."""
import argparse,json,subprocess,time
from collections import Counter
from uuid import UUID

def sample():
    counts=Counter()
    for line in subprocess.run(['ss','-Htan'],capture_output=True,text=True,check=True).stdout.splitlines():
        row=line.split()
        if len(row)<5:continue
        if row[3].endswith(':443'):counts['httpsLocal:'+row[0]]+=1
        if row[4].endswith(':8080'):counts['upstream8080:'+row[0]]+=1
    return dict(counts)
def main():
    p=argparse.ArgumentParser();p.add_argument('--run-id',required=True);p.add_argument('--started-at',required=True,type=float);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id or '10.30.0.174' not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Wrong Run or Source host')
    for elapsed in (100,250,720):
        at=a.started_at+elapsed
        while time.time()<at:time.sleep(min(1,at-time.time()))
        print(json.dumps({'runId':a.run_id,'elapsed':elapsed,'at':time.time(),'connections':sample()}),flush=True)
if __name__=='__main__':main()
