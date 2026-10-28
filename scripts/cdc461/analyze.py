#!/usr/bin/env python3
"""Summarize numeric-only Sink observations without conflating SQL and commits."""
import argparse
import collections
import json
from pathlib import Path
import re


def statement_delta(first,last):
    before={r[0]:(int(r[1]),int(r[2])) for r in first}
    out={}
    for event,count,timer in last:
        old=before.get(event,(0,0));count=int(count)-old[0];timer=int(timer)-old[1]
        out[event]={'calls':count,'totalSeconds':timer/1e12,'meanMs':timer/count/1e9 if count>0 and timer>=0 else None,
                    'counterReset':count<0 or timer<0}
    return out


def category(frames):
    text='\n'.join(frames)
    if 'commit' in text.lower():return 'commit path'
    if any(v in text for v in ['DatabaseMetaData','getTable','getColumn','TableDescriptor','checkAndApplyTable']):return 'schema/metadata path'
    if any(v in text for v in ['executeBatch','executeUpdate','RecordWriter']):return 'SQL/write path'
    if 'org.apache.kafka.clients.consumer' in text and 'poll' in text.lower():return 'Kafka poll path'
    return 'other/unclassified'


def analyze(directory):
    state=json.loads((directory/'state.json').read_text())
    if state['phase']!='completed':raise RuntimeError('Retain interrupted diagnosis; no completion claim')
    begin=state['registeredAt'];end=state['finishedAt']
    def load(name):
        return [json.loads(x) for x in (directory/name).read_text().splitlines() if x.strip()]
    jmx=[x for x in load('kafka-jmx.jsonl') if begin<=x['at']<=end]
    mysql=[x for x in load('target-mysql.jsonl') if x['at']<=end]
    offsets=[x for x in load('kafka-offsets.jsonl') if begin<=x['at']<=end]
    if len(jmx)<2 or len(mysql)<2 or len(offsets)<2:raise RuntimeError('Insufficient aligned observer samples')
    # Counter baseline precedes registration; no global resets are performed.
    baseline=max((x for x in mysql if x['at']<=begin),key=lambda x:x['at'],default=mysql[0])
    final=mysql[-1]
    sql=statement_delta(baseline['globalStatementCounters'],final['globalStatementCounters'])
    commits=sql.get('statement/sql/commit',{})
    trip_counters=[];task_details={};stacks=collections.Counter();reset=False;previous={}
    for row in jmx:
        totals={'read':0,'sent':0,'active':0};seen=False
        for name,metrics in row['beans'].items():
            if 'type=sink-task-metrics' not in name or '-sink-journal' in name:continue
            seen=True;current=metrics.get('sink-record-read-total')
            if isinstance(current,(float,int)):
                if name in previous and current<previous[name]:reset=True
                previous[name]=current
            for target,source in [('read','sink-record-read-total'),('sent','sink-record-send-total'),('active','sink-record-active-count')]:
                totals[target]+=metrics.get(source) or 0
            task_details[name]={k:v for k,v in metrics.items() if any(s in k for s in ['put-batch','sink-record','offset-commit'])}
        if seen:trip_counters.append({'at':row['at'],**totals})
        for thread in row.get('taskThreads',[]):
            if '-sink-journal' not in thread['thread']:stacks[(thread['state'],category(thread['stack']))]+=1
    if not trip_counters:raise RuntimeError('No actual Sink task JMX metrics; do not infer pipeline latency')
    first,last=jmx[0],jmx[-1];span=last['at']-first['at']
    observer_cpu=(last['observerCpuNanos']-first['observerCpuNanos'])/1e9
    worker_cpu=(last['workerCpuNanos']-first['workerCpuNanos'])/1e9
    frame_ms=sum(x['sampleMillis'] for x in jmx)
    expected=35212
    first_read=next((x['at'] for x in trip_counters if x['read']>0),None)
    read_done=next((x['at'] for x in trip_counters if x['read']>=expected),None)
    sent_done=next((x['at'] for x in trip_counters if x['sent']>=expected),None)
    caught=next((x['at'] for x in offsets if all(g['equal'] for g in x['groups'])),None)
    verification=json.loads((directory/'data-verification.json').read_text())[-1]
    result={'run':state['run'],'diagnosticOnly':True,'serviceCutoverTest':False,'secondsRegistrationToDecision':end-begin,
            'allOffsetsAtFrozenEnd':state['allOffsetsAtFrozenEnd'],'dataEqual':verification['complete'],
            'initializationToFirstReadSeconds':first_read-begin if first_read else None,
            'allReadFromRegistrationSeconds':read_done-begin if read_done else None,
            'allSentCounterFromRegistrationSeconds':sent_done-begin if sent_done else None,
            'allCommittedFromRegistrationSeconds':caught-begin if caught else None,
            'tripReadEventsPerSecond':expected/(read_done-first_read) if read_done and first_read and read_done>first_read else None,
            'taskCounterResetObserved':reset,'lastTripCounters':trip_counters[-1],
            'serverStatements':sql,'serverCommit':commits,
            'observerCpuSeconds':observer_cpu,'observerCpuPercentOneCore':observer_cpu/span*100,
            'workerCpuSeconds':worker_cpu,'workerCpuPercentOneCore':worker_cpu/span*100,
            'jmxReadWallFractionPercent':frame_ms/(span*1000)*100,
            'taskMetricSnapshots':task_details,'threadSamples':[{'state':k[0],'path':k[1],'count':v} for k,v in stacks.most_common()],
            'limitations':['Frozen finite-topic replay excludes live Source/B2/auth traffic; throughput is not a50RPS cutover result.',
                           'MySQL commit statistics combine trip and Journal Sinks; timing is server-side, not full JDBC network latency.',
                           'JMX average latency uses its own moving window; do not subtract it directly from whole-run SQL means.',
                           'Thread samples classify observed stacks, not an exact per-record latency attribution.',
                           'Local JMX instrumentation remains an observer; its CPU/wall overhead is reported.']}
    (directory/'analysis.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ['taskMetricSnapshots','limitations']},ensure_ascii=False))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args();analyze(a.directory)
