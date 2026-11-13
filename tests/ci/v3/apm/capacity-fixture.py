"""Synthetic binary OTLP, late business spans and two concurrent bounded workflows."""
import base64
import copy
import concurrent.futures
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from google.protobuf.json_format import ParseDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
sys.path.insert(0,'/evidence')
from v3_workflow_trace import inspect_trace

ALLOY='http://v3-alloy.observability.svc:4318/v1/traces'
TEMPO='http://v3-tempo.observability.svc:3200'
BOUNDARIES=[('photo-converter','photo.convert'),('trip-result-worker','trip.process.dispatch'),('ai-api','ai.receive'),('ai-runner','ai.execute'),('ai-api','ai.publish'),('trip-result-worker','trip.result.apply')]

def get(url):
    return urllib.request.urlopen(url,timeout=15)


def metrics():
    result={}
    for component,url in [('alloy','http://v3-alloy.observability.svc:12345/metrics'),('tempo',TEMPO+'/metrics')]:
        with get(url) as response:text=response.read().decode()
        for line in text.splitlines():
            if line.startswith('#'):continue
            match=re.match(r'([a-zA-Z_:][a-zA-Z_0-9:]*)(\{.*\})?\s+([0-9.eE+\-]+)(?:\s+.*)?$',line)
            if not match:continue
            name=match[1]
            if name.startswith('otelcol_exporter_queue_')and 'data_type="traces"'not in (match[2]or''):continue
            if name.startswith('otelcol_') and any(x in name for x in ['spans','queue_']):
                result[component+'/'+name]=result.get(component+'/'+name,0)+float(match[3])
            elif any(x in name for x in ['discarded_spans','discarded_bytes','traces_created','received_spans']):
                result[component+'/'+name]=result.get(component+'/'+name,0)+float(match[3])
    return result


def wire(service,spans):
    spans=copy.deepcopy(spans)
    for span in spans:
        for key in ['traceId','spanId','parentSpanId']:
            if key in span:span[key]=base64.b64encode(bytes.fromhex(span[key])).decode()
    body={'resourceSpans':[{'resource':{'attributes':[{'key':'service.name','value':{'stringValue':service}}]},'scopeSpans':[{'scope':{'name':'synthetic-capacity'},'spans':spans}]}]}
    request=ParseDict(body,ExportTraceServiceRequest())
    return request.SerializeToString()


def post(service,spans):
    data=wire(service,spans)
    req=urllib.request.Request(ALLOY,data=data,headers={'Content-Type':'application/x-protobuf'})
    with get(req) as response:assert response.status==200


def workflow(count=2048,rich=True):
    tid=secrets.token_hex(16);now=time.time_ns();rows=[]
    for i in range(7,count+1):
        attrs=[{'key':'db.system','value':{'stringValue':'mysql'}},{'key':'db.operation.name','value':{'stringValue':'SELECT'}}]
        if rich:attrs.append({'key':'error.type','value':{'stringValue':'synthetic.'+'x'*128}})
        rows.append(('photo-converter',{'traceId':tid,'spanId':f'{i:016x}','parentSpanId':f'{1:016x}','name':'synthetic SQL','kind':3,'startTimeUnixNano':str(now),'endTimeUnixNano':str(now+1000000),'attributes':attrs}))
    # These are intentionally emitted after the SQL burst, just as long operations end later.
    late=[]
    for i,(service,name) in enumerate(BOUNDARIES,1):
        span={'traceId':tid,'spanId':f'{i:016x}','name':name,'kind':1,'startTimeUnixNano':str(now),'endTimeUnixNano':str(now+2000000000)}
        if i>1:span['parentSpanId']=f'{i-1:016x}'
        late.append((service,span))
    expected={f'{i:016x}' for i in range(1,count+1)}
    total=sum(len(wire(service,[span]))for service,span in rows+late)
    return {'trace':tid,'rows':rows,'late':late,'ids':expected,'wireUpperBound':total}


def await_trace(w,timeout=100):
    until=time.monotonic()+timeout;last={}
    while time.monotonic()<until:
        try:
            req=urllib.request.Request(TEMPO+'/api/traces/'+w['trace'],headers={'Accept':'application/json'})
            with get(req) as response:body=json.load(response)
            last=inspect_trace(body,w['trace'])
            if {x['id']for x in last.get('spans',[])}==w['ids'] and last['tracePathComplete']:return last
        except (urllib.error.URLError,TimeoutError):pass
        time.sleep(1)
    raise AssertionError('Incomplete stored graph: '+json.dumps({k:last.get(k)for k in ['spanCount','missingBoundaries','tracePathComplete']}))


def send_workflow(w):
    groups=defaultdict(list)
    for service,span in w['rows']:groups[service].append(span)
    for service,spans in groups.items():
        for start in range(0,len(spans),64):post(service,spans[start:start+64])
    time.sleep(1)
    for service,span in w['late']:post(service,[span])


def assert_no_loss(before,after):
    for name,value in after.items():
        if any(k in name for k in ['failed_spans','refused_spans','discarded_spans','discarded_bytes']):
            assert value==before.get(name,0),(name,before.get(name),value)
    for name in ['alloy/otelcol_exporter_queue_size','alloy/otelcol_exporter_queue_capacity']:
        assert name in after,'Pinned queue metric missing: '+name
    assert after['alloy/otelcol_exporter_queue_size']==0,after
    assert after['alloy/otelcol_exporter_queue_capacity']==2097152,after


def main():
    for i in range(60):
        try:
            with get(TEMPO+'/ready') as response:assert response.status==200
            with get('http://v3-alloy.observability.svc:12345/-/ready') as response:assert response.status==200
            break
        except Exception:time.sleep(.5)
    else:raise AssertionError('APM not ready')
    budget=json.load(open('/budget.json'))
    before=metrics();work=[workflow(budget['spansPerWorkflow'])for _ in range(budget['workflows'])]
    assert all(w['wireUpperBound']<=budget['maxWireBytesPerWorkflow']for w in work),[w['wireUpperBound']for w in work]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2)as pool:list(pool.map(send_workflow,work))
    reports=[await_trace(w)for w in work]
    time.sleep(2);after=metrics();assert_no_loss(before,after)
    print(json.dumps({'passed':True,'workflows':len(work),'spansEach':budget['spansPerWorkflow'],'wireUpperBounds':[w['wireUpperBound']for w in work],'fullParentGraphs':[x['tracePathComplete']for x in reports],'lateBusinessSpansStored':True,'metricsBefore':before,'metricsAfter':after}),flush=True)


if __name__=='__main__':main()
