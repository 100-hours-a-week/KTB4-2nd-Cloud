"""Pinned per-role injection expectations. No AWS, credentials or cluster client."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CONTRACT=json.loads((ROOT/'deploy/v3/apps/files/apm.json').read_text())

def java_env(name,config):
    c=CONTRACT
    values={
      'JAVA_TOOL_OPTIONS':config['configMapData']['JAVA_TOOL_OPTIONS']+' -javaagent:/otel/javaagent.jar',
      'OTEL_SERVICE_NAME':name,
      'OTEL_RESOURCE_ATTRIBUTES':f"service.namespace={config['namespace']},deployment.environment.name=production,service.version={config['configMapData']['RELEASE']}",
      'OTEL_EXPORTER_OTLP_ENDPOINT':c['endpoint'],'OTEL_EXPORTER_OTLP_PROTOCOL':'http/protobuf',
      'OTEL_TRACES_SAMPLER':c['sampler'],'OTEL_TRACES_SAMPLER_ARG':c['samplerArg'],
      'OTEL_PROPAGATORS':'tracecontext','OTEL_METRICS_EXPORTER':'none','OTEL_LOGS_EXPORTER':'none',
      'OTEL_BSP_MAX_QUEUE_SIZE':'128','OTEL_BSP_MAX_EXPORT_BATCH_SIZE':'32','OTEL_BSP_SCHEDULE_DELAY':'1000','OTEL_EXPORTER_OTLP_TIMEOUT':'1000',
    }
    return [{'name':k,'value':v} for k,v in values.items()]

def python_env(role,source):
    return [{'name':k,'value':v} for k,v in {
      'YEODAM_APM_ENABLED':'true','PYTHONPATH':'/runtime:/otel:/app',
      'OTEL_SERVICE_NAME':'ai-'+role,'OTEL_SERVICE_VERSION':source,
      'OTEL_EXPORTER_OTLP_ENDPOINT':CONTRACT['endpoint'],'OTEL_METRICS_EXPORTER':'none','OTEL_LOGS_EXPORTER':'none',
    }.items()]

def init(language):
    return {'name':'prepare-apm','image':CONTRACT['javaImage' if language=='java' else 'pythonImage'],
      'command':['cp','/javaagent.jar','/otel/javaagent.jar'] if language=='java' else ['cp','-R','/autoinstrumentation/.','/otel/'],
      'resources':{'requests':{'cpu':'25m','memory':'32Mi'},'limits':{'cpu':'100m','memory':'64Mi'}},
      'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
      'volumeMounts':[{'name':'apm','mountPath':'/otel'}]}
