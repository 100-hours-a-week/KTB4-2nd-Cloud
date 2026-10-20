#!/usr/bin/env python3
"""Run and summarize a fixed 900s B integration load; never print credentials."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import UUID

BASE=Path('/opt/yeodam-load/source/tests/load')
DATASET='c0014ce1-0398-4a0c-95de-f502cb2e412e'

def save(path,state):
    temporary=path.with_suffix('.next')
    temporary.write_text(json.dumps(state));temporary.chmod(0o600);os.replace(temporary,path)

def paths(run):
    directory=Path('/run/yeodam-b380')/run;directory.mkdir(parents=True,mode=0o700,exist_ok=True)
    return directory,Path('/opt/yeodam-load/results/b380')/run

def start(run,rate):
    directory,root=paths(run)
    if (directory/'k6.json').exists():raise RuntimeError('Run already started; never rerun its ID')
    if subprocess.run(['pgrep','-x','k6'],capture_output=True).returncode==0:raise RuntimeError('Another k6 process is running')
    root.parent.mkdir(parents=True,exist_ok=True);root.parent.chmod(0o755)
    root.mkdir(parents=True,exist_ok=True);subprocess.run(['chown','-R','ubuntu:ubuntu',str(root.parent)],check=True)
    env={'K6_BASE_URL':'https://v1-staging.yeodam-2gether.com','K6_TRIP_RUN_ID':run,'K6_TRIP_DATASET_ID':DATASET,
         'K6_TRIP_RATE':str(rate),'K6_TRIP_DURATION_SECONDS':'900',
         'K6_SESSION_POOL_FILE':str(BASE/'private'/(run+'.json')),'K6_RESULTS_DIRECTORY':str(root)}
    state={'runId':run,'rate':rate,'startedAt':time.time(),'scenarioSha256':hashlib.sha256((BASE/'scenarios/migration-b-integration.js').read_bytes()).hexdigest()}
    path=directory/'k6.json';save(path,state)
    observer_log=(directory/'observer.log').open('w')
    observer=subprocess.Popen(['python3','/run/yeodam-b380/b-integration376-observe.py','--run-id',run],stdout=observer_log,stderr=subprocess.STDOUT)
    with (directory/'wrapper.log').open('w') as log:
        cmd=['runuser','-u','ubuntu','--','env',*[k+'='+v for k,v in env.items()],'bash','scripts/run-k6.sh','scenarios/migration-b-integration.js']
        p=subprocess.run(cmd,cwd=BASE,stdout=log,stderr=subprocess.STDOUT,env={k:v for k,v in os.environ.items() if k not in ('K6_HTTP_DEBUG','K6_LOG_FORMAT')})
    state.update(finishedAt=time.time(),k6ExitCode=p.returncode)
    save(path,state)
    if not list(root.glob('*/trip-events.jsonl')):observer.terminate()
    try:state['observerExitCode']=observer.wait(timeout=40)
    except subprocess.TimeoutExpired:
        observer.terminate();observer.wait();state['observerExitCode']=-1
    observer_log.close();save(path,state);print(json.dumps(state))

def read(run,do_audit=False):
    directory,root=paths(run);state=json.loads((directory/'k6.json').read_text())
    files=list(root.glob('*/trip-events.jsonl'))
    if len(files)>1:raise RuntimeError('More than one event file for a one-use Run')
    if not files:print(json.dumps(state));return
    file=files[0];state['resultDirectory']=str(file.parent)
    started={};final={};statuses=Counter();first_status={};refreshes=Counter();csrf=Counter();plan=None;first=[];latencies=[]
    event_files=[file]
    if (file.parent/'completion-events.jsonl').exists():event_files.append(file.parent/'completion-events.jsonl')
    lines=(line for event_file in event_files for line in event_file.open())
    for line in lines:
            if '{' not in line:continue
            try:e=json.loads(line[line.index('{'):])
            except ValueError:continue
            if e.get('runId')!=run:continue
            if e['event']=='run_started':plan=e
            elif e['event']=='started':started[e['requestId']]=e
            elif e['event']=='first_response':statuses[e['status']]+=1;first_status[e['requestId']]=e['status'];first.append(e['elapsedMs'])
            elif e['event']=='final':final[e['requestId']]=e;latencies.append(e['elapsedMs'])
            elif e['event']=='auth_refresh':refreshes['success' if e['ok'] else 'failure']+=1
            elif e['event']=='auth_csrf':csrf['success' if e['ok'] else 'failure']+=1
    if plan:state['actualStartedAt']=plan['startedAt']/1000
    if do_audit:
        def digest(rows):
            h=hashlib.sha256()
            for row in sorted(rows):h.update(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode());h.update(b'\n')
            return h.hexdigest()
        projected=[(str(e['userId']),e['tripName'],e['request']['startDate'],e['request']['endDate'],'PROCESSING',str(code)) for e in started.values() for code in e['request']['regionCodes']]
        non202=[e for e in started.values() if first_status.get(e['requestId']) in (0,201)]
        non202_projected=[(str(e['userId']),e['tripName'],e['request']['startDate'],e['request']['endDate'],'PROCESSING',str(code)) for e in non202 for code in e['request']['regionCodes']]
        def stats(values):
            values=sorted(values)
            return {k:values[min(len(values)-1,int((len(values)-1)*q))] for k,q in [('p50',.5),('p95',.95),('p99',.99),('max',1)]} if values else {}
        state.update(completedAfterDeadline=sum(e.get('outcome')=='confirmed' and e.get('withinDeadline') is False for e in final.values()),
                     started=len(started),final=len(final),confirmed=sum(e['outcome']=='confirmed' for e in final.values()),
                     firstStatuses=dict(statuses),refreshes=dict(refreshes),csrf=dict(csrf),missingFinal=len(set(started)-set(final)),
                     requestProjectionSha256=digest(projected),non202Requests=len(non202),non202ProjectionSha256=digest(non202_projected),
                     firstResponseMs=stats(first),finalObservationMs=stats(latencies))
        stats=directory/'observer-stats.json'
        if stats.exists():state['completionObserver']=json.loads(stats.read_text())
        native=file.parent/'native-summary.json'
        custom=file.parent/'summary.json'
        state['summaryArtifacts']={'nativeExists':native.exists(),'customExists':custom.exists()}
        if not native.exists() and custom.exists():native=custom
        if native.exists():
            data=json.loads(native.read_text()).get('metrics',{})
            state['native']={k:data[k].get('values',data[k]) for k in ('dropped_iterations','http_req_failed','b_request_admitted','b_request_completed','b_completion_ms','b_first_response_ms') if k in data}
        else:
            state['summaryLimitation']='Aggregate JSON missing; exact dropped_iterations count unavailable. Retain raw events and threshold output.'
        (directory/'result.json').write_text(json.dumps(state))
    print(json.dumps(state))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('start','read','audit'));p.add_argument('--run-id',required=True);p.add_argument('--rate',type=int,choices=(10,30,50));a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id or '10.30.0.126' not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Wrong generator or UUID')
    if a.action=='start':
        if a.rate is None:raise ValueError('Rate required')
        start(a.run_id,a.rate)
    else:read(a.run_id,a.action=='audit')
