"""Loopback-only observation with durable partial evidence and explicit telemetry integrity."""
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request
from v3_workflow_trace import inspect_trace, read_tempo

QUERIES = {
    'up': 'up{job=~"apm-alloy|apm-tempo"}',
    'started': 'process_start_time_seconds{job=~"apm-alloy|apm-tempo"}',
    'age': 'time()-timestamp(up{job=~"apm-alloy|apm-tempo"})',
    'sent': 'sum(otelcol_exporter_sent_spans_total{job="apm-alloy"})',
    'queue': 'sum(otelcol_exporter_queue_size{job="apm-alloy",data_type="traces"})',
    'capacity': 'sum(otelcol_exporter_queue_capacity{job="apm-alloy",data_type="traces"})',
    'rejected': 'sum(otelcol_exporter_enqueue_failed_spans_total{job="apm-alloy"})',
    'sendFailed': 'sum(otelcol_exporter_send_failed_spans_total{job="apm-alloy"})',
    'refused': 'sum(otelcol_receiver_refused_spans_total{job="apm-alloy"})',
    'discarded': 'sum(tempo_discarded_spans_total{job="apm-tempo"})',
}
FAILURES = ('rejected','sendFailed','refused','discarded')


def metrics(base):
    parsed=urllib.parse.urlsplit(base)
    if (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.username
            or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment):
        raise ValueError('Use a credential-free loopback Prometheus URL')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    result={}
    for name,query in QUERIES.items():
        url=base.rstrip('/')+'/api/v1/query?'+urllib.parse.urlencode({'query':query})
        with opener.open(url,timeout=5)as response:raw=response.read(256*1024+1)
        if len(raw)>256*1024:raise ValueError('Metrics response exceeds bound')
        body=json.loads(raw)
        if body.get('status')!='success':raise ValueError('Metrics query failed')
        try:result[name]=[float(x['value'][1])for x in body['data']['result']]
        except (KeyError,TypeError,IndexError)as error:raise ValueError('Invalid metrics response shape')from error
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Redirects are not allowed')


def integrity(before,after):
    if not before or not after:return 'unknown'
    for row in (before,after):
        if len(row.get('up',[]))!=2 or any(x!=1 for x in row['up']):return 'unknown'
        if len(row.get('age',[]))!=2 or any(x>45 or x<0 for x in row['age']):return 'unknown'
    if len(before.get('started',[]))!=2 or sorted(before['started'])!=sorted(after.get('started',[])):
        return 'counter_reset_or_missing'
    for key in FAILURES:
        a=before.get(key,[]);b=after.get(key,[])
        if a and not b:return 'counter_reset_or_missing'
        if a and b and b[0]<a[0]:return 'counter_reset_or_missing'
        # These pinned counters are lazily created: a new nonzero series is evidence of rejection.
        if b and b[0]>(a[0]if a else 0):return 'collection_failure_observed'
    if not after.get('sent')or not after.get('capacity'):return 'unknown'
    if not after.get('queue')or after['queue'][0]!=0:return 'pending_export'
    return 'no_observed_loss'


def checkpoint(path,value):
    path=Path(path)
    if path.is_symlink() or path.parent.is_symlink() or path.parent.stat().st_mode&0o077:
        raise ValueError('Use a private regular evidence path')
    temp=path.with_name(path.name+'.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w')as stream:json.dump(value,stream,indent=2,allow_nan=False)
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)


def observe(tid,tempo,prometheus,output,deadline=300,interval=5,read=read_tempo,probe=metrics,sleep=time.sleep,clock=time.monotonic):
    if not 1<=deadline<=600 or not 1<=interval<=30:raise ValueError('Bounded observation window required')
    if Path(output).exists():raise ValueError('Never overwrite a previous experiment')
    try:before=probe(prometheus)
    except (OSError,ValueError):before=None
    result={'traceId':tid,'tracePathComplete':False,'status':'not_observed'};events=[];end=clock()+deadline
    while True:
        try:result=inspect_trace(read(tempo,tid),tid)
        except (OSError,ValueError)as error:
            events.append({'kind':'trace_read_error','type':type(error).__name__})
        try:after=probe(prometheus)
        except (OSError,ValueError)as error:
            after=None;events.append({'kind':'metrics_read_error','type':type(error).__name__})
        quality=integrity(before,after)
        record={'trace':result,'telemetryIntegrity':quality,'traceEvidenceReady':result.get('tracePathComplete',False)and quality=='no_observed_loss',
                'metricsBefore':before,'metricsAfter':after,'errors':events[-120:],
                'businessVerified':False,'limitation':'Trace evidence only; compare final database outcome separately'}
        checkpoint(output,record)
        if record['traceEvidenceReady']or clock()>=end:return record
        sleep(min(interval,max(0,end-clock())))
