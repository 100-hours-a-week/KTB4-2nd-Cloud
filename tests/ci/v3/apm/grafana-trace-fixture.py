"""Synthetic workload only. Never read app images, user photos, credentials or AWS."""
import importlib.util,json,os,sys,time,urllib.error,urllib.request
from pathlib import Path
sys.path.insert(0,'/evidence')
spec=importlib.util.spec_from_file_location('capacity','/fixture/capacity-fixture.py');capacity=importlib.util.module_from_spec(spec);spec.loader.exec_module(capacity)
capacity.ALLOY='http://v3-tempo.observability.svc:4318/v1/traces'
STATE=Path('/state/workflow.json')
BASE='http://v3-grafana:3000'
def read(url,data=None,timeout=45):
    req=urllib.request.Request(url,data=data,headers={'Content-Type':'application/json','Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as response:
        raw=response.read(16*1024*1024+1)
    assert len(raw)<=16*1024*1024,'Response budget exceeded'
    return json.loads(raw)
def wait(url):
    deadline=time.monotonic()+100
    while time.monotonic()<deadline:
        try:read(url,timeout=2);return
        except Exception:time.sleep(1)
    raise AssertionError('Synthetic endpoint not ready')
def seed():
    # One workflow slightly larger than the observed 6,059-span E2E trace.
    deadline=time.monotonic()+80
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen(capacity.TEMPO+'/ready',timeout=2) as response:assert response.status==200
            break
        except Exception:time.sleep(1)
    else:raise AssertionError('Tempo not ready')
    workflow=capacity.workflow(count=6144,rich=False)
    capacity.send_workflow(workflow);proof=capacity.await_trace(workflow)
    assert proof['spanCount']==6144 and proof['tracePathComplete']
    STATE.write_text(json.dumps({'traceId':workflow['trace'],'spanCount':6144}))
    print(json.dumps({'seeded':6144,'businessParentPath':True,'syntheticOnly':True}),flush=True)
def query():
    wait(BASE+'/api/health');state=json.loads(STATE.read_text())
    sources=read(BASE+'/api/datasources')
    assert {(x['uid'],x['type'])for x in sources}=={('v3-prometheus','prometheus'),('v3-tempo','tempo'),('v3-cloudwatch-logs','cloudwatch')},'Registered datasource missing'
    plugins=read(BASE+'/api/plugins');ids={x['id']for x in plugins}
    disabled=set(os.environ.get('EXPECTED_DISABLED','').split(','))-{''}
    assert not disabled.intersection(ids),'Disabled backend still registered'
    expected={f'{i:016x}'for i in range(1,state['spanCount']+1)};durations=[];bytes_seen=[]
    for attempt in range(3):
        payload={'from':str(int((time.time()-120)*1000)),'to':str(int((time.time()+5)*1000)),
                 'queries':[{'refId':'A','datasource':{'type':'tempo','uid':'v3-tempo'},'queryType':'traceql','query':state['traceId'],'maxDataPoints':1000,'intervalMs':1000}]}
        started=time.monotonic();body=read(BASE+'/api/ds/query',json.dumps(payload).encode());durations.append(time.monotonic()-started)
        result=body['results']['A'];assert not result.get('error'),str(result.get('error'))[:200]
        frames=result.get('frames',[]);assert len(frames)==1,'Expected one complete Trace frame'
        frame=frames[0];fields={x['name'].replace('_','').lower():v for x,v in zip(frame['schema']['fields'],frame['data']['values'])}
        assert set(fields['spanid'])==expected,'Truncated or different span IDs'
        assert len(fields['spanid'])==state['spanCount'],'Duplicated frame spans'
        index={sid:i for i,sid in enumerate(fields['spanid'])}
        for number,(service,name) in enumerate(capacity.BOUNDARIES,1):
            row=index[f'{number:016x}'];assert fields['servicename'][row]==service and fields['operationname'][row]==name
            if number>1:assert fields['parentspanid'][row]==f'{number-1:016x}','Business parent boundary severed'
        bytes_seen.append(len(json.dumps(body)))
    # A missing Trace must remain an error or an empty response, never a fabricated complete graph.
    payload['queries'][0]['query']='f'*32
    try:
        missing=read(BASE+'/api/ds/query',json.dumps(payload).encode())['results']['A']
        assert missing.get('error') or not any(column for frame in missing.get('frames',[]) for column in frame.get('data',{}).get('values',[])),'Missing Trace represented as data'
    except urllib.error.HTTPError as error:assert error.code in (400,404)
    try:
        read(BASE+'/api/datasources',b'{}');raise AssertionError('Viewer datasource write accepted')
    except urllib.error.HTTPError as error:assert error.code in (401,403)
    print(json.dumps({'passed':True,'spansPerRead':state['spanCount'],'reads':3,'businessBoundaries':6,
                      'fullParentPath':True,'querySeconds':durations,'responseBytes':bytes_seen,
                      'activeBackendsPreserved':['prometheus','tempo','cloudwatch'],'disabled':sorted(disabled),
                      'viewerWriteDenied':True,'missingTraceNotFabricated':True}),flush=True)
if __name__=='__main__':seed() if sys.argv[1]=='seed' else query()
