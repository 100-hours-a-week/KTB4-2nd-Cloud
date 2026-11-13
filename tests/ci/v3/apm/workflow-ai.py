"""Actual AI Kafka receiver and persistent record serialization; no Kafka/Redis/model service."""
import asyncio,json,os,sys
from types import SimpleNamespace
from fastapi import FastAPI,Request
import httpx,uvicorn
from app.jobs.kafka import RequestReceiver,Incoming
from app.jobs.models import JobRecord
from app.observability import otel_context as ctx

app=FastAPI()
class Store:
    cfg=SimpleNamespace(topics={'synthetic.request':('process','synthetic.result')},JOBS_TRACE_CONTEXT_WRITE=True)
    async def register(self,record):
        self.serialized=record.model_dump_json();return 'created'
    def terminate(self,*args,**kwargs):raise AssertionError('Fixture input unexpectedly invalid')

@app.get('/ready')
async def ready():return {'ready':True}

@app.get('/workflow')
async def workflow(request:Request):
    parent=request.headers.get('traceparent');assert parent
    data={'type':'process','trip_id':1,'execution_id':'synthetic-job','trigger':'TRIP_CREATED','trip_name':'synthetic','period':{'start_date':'2026-01-01','end_date':'2026-01-01'},'regions':[{'latitude':37.0,'longitude':127.0}],'attachments':[{'trip_attachment_id':1,'analyze_storage_key':'synthetic/a.jpg'}]}
    store=Store()
    message=Incoming('synthetic.request',0,0,b'1',json.dumps(data).encode(),(('traceparent',parent.encode()),('tracestate',None)))
    assert await RequestReceiver(store,None).accept(message)=='created'
    record=JobRecord.model_validate_json(store.serialized)
    assert record.trace_context and record.trace_context['traceparent'].split('-')[1]==parent.split('-')[1]
    # A real child process imports sitecustomize and the actual AI context module independently.
    env=dict(os.environ,OTEL_SERVICE_NAME='ai-runner')
    child=await asyncio.create_subprocess_exec(sys.executable,'/fixture/workflow-child.py',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=env)
    stdout,stderr=await asyncio.wait_for(child.communicate(store.serialized.encode()),timeout=10)
    assert child.returncode==0,'Child process failed'
    record=JobRecord.model_validate_json(stdout.decode().strip().splitlines()[-1])
    with ctx.span('ai.publish',parent=record.result_trace_context,kind='producer',operation='process'):
        headers=dict((k,v.decode())for k,v in ctx.kafka_headers(ctx.current_carrier()))
        async with httpx.AsyncClient(timeout=5)as client:
            response=await client.get('http://workflow-worker:8080/result',headers=headers)
            assert response.status_code==200
    return {'completed':True}

uvicorn.run(app,host='0.0.0.0',port=8000,log_level='warning')
