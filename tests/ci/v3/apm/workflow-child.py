"""Actual AI context and JobRecord in a new process; synthetic work, real OTLP exporter."""
import asyncio,json,sys
from app.jobs.models import JobRecord
from app.observability import otel_context as ctx
record=JobRecord.model_validate_json(sys.stdin.read())
with ctx.span('ai.execute',parent=record.trace_context,operation='process'):
    record.result_trace_context=ctx.current_carrier()
    assert record.result_trace_context and record.result_trace_context['traceparent'].endswith('-01')
asyncio.run(ctx.flush())
print(record.model_dump_json())
