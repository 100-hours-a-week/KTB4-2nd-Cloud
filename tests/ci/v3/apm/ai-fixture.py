"""Real image libraries and startup hook, synthetic ASGI app only; no models or stores."""
from fastapi import FastAPI
from opentelemetry import trace
from app.core.logging import get_logger, setup_logging
import uvicorn
setup_logging(release='synthetic-only')
app=FastAPI()
log=get_logger('apm-fixture')
@app.get('/fixture')
async def fixture():
    context=trace.get_current_span().get_span_context()
    assert context.is_valid
    log.info('APM fixture',extra={'event':'apm_fixture','request_id':'synthetic-only'})
    return {'trace_id':f'{context.trace_id:032x}','span_id':f'{context.span_id:016x}'}
uvicorn.run(app,host='0.0.0.0',port=8000,log_level='warning')
