"""Inside the exact AI image, with synthetic keys and memory exporters only."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

os.environ.update(LANGFUSE_PUBLIC_KEY='pk-lf-synthetic-only',LANGFUSE_SECRET_KEY='sk-lf-synthetic-only',LANGFUSE_BASE_URL='https://cloud.langfuse.com',LANGFUSE_TRACING_ENVIRONMENT='synthetic',YEODAM_APM_ENABLED='false',JOBS_TRACE_CONTEXT_WRITE='false')
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.exporter.otlp.proto.http import trace_exporter
cloud=InMemorySpanExporter();external=InMemorySpanExporter()
# Exercise the real Cloud hook while replacing its only network exporter.
os.environ.update(YEODAM_APM_ENABLED='true',OTEL_SERVICE_NAME='ai-runner',OTEL_SERVICE_VERSION='synthetic',OTEL_EXPORTER_OTLP_ENDPOINT='http://127.0.0.1:1')
with patch.object(trace_exporter,'OTLPSpanExporter',return_value=cloud):
    spec=importlib.util.spec_from_file_location('cloud_hook','/hook/sitecustomize.py');hook=importlib.util.module_from_spec(spec);spec.loader.exec_module(hook)
from app.jobs.config import JobsSettings
from app.jobs.models import JobRecord,TRACE_FIELDS,StrictModel
from app.schemas.task import Progress
from app.observability import otel_context as ctx,langfuse_tracing as lf
from app.core.tracing import stage,start_trace,end_trace
from pydantic import create_model
assert JobsSettings.model_fields['JOBS_TRACE_CONTEXT_WRITE'].default is False
record=JobRecord(trip_id=1,kind='process',execution_id='synthetic',request={},request_topic='synthetic',result_topic='synthetic',created_at=0,progress=Progress(done=0,total=1))
old=create_model('OldReader',__base__=StrictModel,**{k:(f.annotation,f)for k,f in JobRecord.model_fields.items()if k not in TRACE_FIELDS})
raw=record.model_dump_json();assert not set(TRACE_FIELDS)&set(json.loads(raw));old.model_validate_json(raw)
parent='00-'+'a'*32+'-'+'b'*16+'-01'
carrier=ctx.carrier_from_headers([('traceparent',parent.encode()),('baggage',b'private-synthetic')])
assert set(carrier)=={'traceparent'}
with ctx.span('ai.receive',parent=carrier,kind='consumer',operation='process'):
    record.trace_context=ctx.current_carrier()
record=JobRecord.model_validate_json(record.model_dump_json())
try:old.model_validate_json(record.model_dump_json())
except ValueError:pass
else:raise AssertionError('Unsafe old reader accepted new record')

# Use the actual Langfuse SDK with its separate provider, but never a real exporter/client.
import langfuse,httpx
real=langfuse.Langfuse;providers=[]
transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'data':[]}))
def make(**kwargs):
    providers.append(kwargs['tracer_provider'])
    return real(**kwargs,span_exporter=external,httpx_client=httpx.Client(transport=transport))

async def execute():
    with patch.object(langfuse,'Langfuse',side_effect=make):
        with ctx.span('ai.execute',parent=record.trace_context,operation='process'):
            execution=trace.get_current_span().get_span_context()
            root=lf.start_root('process',trip_id=1)
            assert lf.active() and lf.client() is not None
            assert trace.get_current_span().get_span_context()==execution
            _,token=start_trace('process',emit_stage_logs=True)
            with stage('clustering'):
                assert trace.get_current_span().get_span_context()==execution
                from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
                client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'fixture':True})))
                HTTPXClientInstrumentor.instrument_client(client)
                assert client.get('https://synthetic.invalid/fixture').status_code==200
                client.close()
                with trace.get_tracer('synthetic-http').start_as_current_span('synthetic HTTP') as h:
                    assert h.get_span_context().trace_id==execution.trace_id
            end_trace(token)
            root.end(status='success')
            assert trace.get_current_span().get_span_context()==execution
            record.result_trace_context=ctx.current_carrier()
        record2=JobRecord.model_validate_json(record.model_dump_json())
        with ctx.span('ai.publish',parent=record2.result_trace_context,kind='producer',operation='process'):
            headers=ctx.kafka_headers(ctx.current_carrier())
            assert ctx.carrier_from_headers(headers)['traceparent'].split('-')[1]=='a'*32
        await ctx.flush()
        # Finish provider-local queues only; both exporters are in memory.
        providers[0].force_flush(timeout_millis=1500)
asyncio.run(execute())
spans=cloud.get_finished_spans();byname={s.name:s for s in spans}
for name in ['ai.receive','ai.execute','ai.publish','ai.stage.clustering','synthetic HTTP','GET']:
    assert name in byname,(name,list(byname))
    assert format(byname[name].context.trace_id,'032x')=='a'*32
assert byname['ai.execute'].parent.span_id==byname['ai.receive'].context.span_id
assert byname['GET'].parent.span_id==byname['ai.execute'].context.span_id
assert byname['ai.publish'].parent.span_id==byname['ai.execute'].context.span_id
assert external.get_finished_spans(),'Actual Langfuse SDK did not record separate observations'
assert not trace.get_current_span().get_span_context().is_valid
print(json.dumps({'passed':True,'readerOffCompatible':True,'writerRoundTrip':True,'langfuseSeparateProvider':True,'cloudContextPreserved':True,'modelOrGpuCalls':False,'realExternalExport':False}))
