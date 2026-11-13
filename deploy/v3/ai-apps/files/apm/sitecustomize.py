"""Bounded HTTP tracing only; no model import, payload logging or command replacement."""
import os
import sys

# Management probes and maintenance CLI must keep their existing lightweight behavior.
_skip = any(x.endswith(("health.py", "runpod_control.py", "prepare-trust.py")) for x in getattr(sys, "orig_argv", sys.argv))
if os.environ.get("YEODAM_APM_ENABLED") == "true" and not _skip:
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        provider = TracerProvider(resource=Resource.create({
            "service.name": os.environ["OTEL_SERVICE_NAME"], "service.namespace": "ai",
            "service.version": os.environ["OTEL_SERVICE_VERSION"], "deployment.environment.name": "production",
        }), sampler=ParentBased(TraceIdRatioBased(0.01)))
        reliable = os.environ.get('YEODAM_APM_RELIABILITY_ENABLED') == 'true'
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]+"/v1/traces", timeout=1),
            max_queue_size=256 if reliable else 128, max_export_batch_size=64 if reliable else 32, schedule_delay_millis=200 if reliable else 1000, export_timeout_millis=1000))
        trace.set_tracer_provider(provider)
        set_global_textmap(TraceContextTextMapPropagator())
        FastAPIInstrumentor().instrument(excluded_urls=r".*/(health|metrics)(?:\?.*)?$")
        HTTPXClientInstrumentor().instrument()
    except Exception as error:
        # Telemetry setup must not prevent the original app from starting. CI rejects this condition.
        print('{"event":"cloud_apm_initialization_failed","error_type":"'+type(error).__name__+'"}', file=sys.stderr, flush=True)
