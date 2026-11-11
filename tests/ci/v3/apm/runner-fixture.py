"""HTTPX tracing from a background process; no queue, model or real runner invocation."""
import asyncio
import json
import uuid
import httpx
from opentelemetry import context, propagate, trace

async def main():
    trace_id = uuid.uuid4().hex
    parent = uuid.uuid4().hex[:16]
    token = context.attach(propagate.extract({'traceparent': f'00-{trace_id}-{parent}-01'}))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get('http://apm-ai:8000/fixture')
            response.raise_for_status()
            assert response.json()['trace_id'] == trace_id
    finally:
        context.detach(token)
    assert trace.get_tracer_provider().force_flush(timeout_millis=5000)
    print(json.dumps({'runnerTraceId': trace_id, 'httpxContext': True}))

asyncio.run(main())
