#!/usr/bin/env python3
"""Pinned APM pipeline and real-image libraries; synthetic HTTP only, no AWS/stores."""
import json,os,subprocess,time,tempfile,urllib.request,urllib.error,uuid,platform,zipfile,sys
from pathlib import Path
r=Path(__file__).resolve().parents[2];images=json.loads((r/'deploy/v3/apm/images.lock.json').read_text());prefix='apm-core-'+uuid.uuid4().hex[:8];names=[]
SANITIZER_ONLY = sys.argv[1:] == ['--sanitizer-only']
if sys.argv[1:] and not SANITIZER_ONLY:raise SystemExit('Only --sanitizer-only is supported')
PLATFORM='linux/arm64' if platform.system()=='Darwin' and platform.machine()=='arm64' else 'linux/amd64'
workspace=tempfile.TemporaryDirectory(prefix='v3-apm-fixture-');artifacts=Path(workspace.name)
def run(*a):
 p=subprocess.run(a,capture_output=True,text=True,timeout=240)
 if p.returncode:raise RuntimeError(p.stdout[-1500:]+p.stderr[-1500:])
 return p.stdout
try:
 if not SANITIZER_ONLY:
  # Extract reviewed public instrumentation bundles and compile against the exact BE API jars.
  components=json.loads((r/'deploy/v3/releases/candidate.json').read_text())['components']
  for role,im,source,target in [('java',images['java'],'/javaagent.jar','javaagent.jar'),('python',images['python'],'/autoinstrumentation','python-sdk'),('backend',components['backend']['image'],'/app/app.jar','app.jar')]:
   n=prefix+'-extract-'+role
   try:
    run('docker','create','--platform','linux/amd64','--name',n,im)
    run('docker','cp',n+':'+source,str(artifacts/target))
   finally:subprocess.run(['docker','rm','-f',n],capture_output=True)
  lib=artifacts/'java-lib';lib.mkdir();classes=artifacts/'java-classes';classes.mkdir()
  with zipfile.ZipFile(artifacts/'app.jar') as jar:
   for name in jar.namelist():
    if name.startswith(('BOOT-INF/lib/opentelemetry-api-','BOOT-INF/lib/opentelemetry-context-','BOOT-INF/lib/opentelemetry-common-')):(lib/Path(name).name).write_bytes(jar.read(name))
  assert len(list(lib.glob('*.jar')))==3,'BE OpenTelemetry API/context/common libraries required'
  run('javac','--release','17','-cp',str(lib/'*'),'-d',str(classes),str(r/'tests/ci/v3/apm/ApmFixture.java'))
 run('docker','network','create','--internal',prefix)
 for name,mem,go,ports,cmd in [('tempo','384m','256MiB',['3200'],['-target=all','-config.file=/etc/apm/tempo.yaml']),('alloy','128m','80MiB',['4318','12345'],['run','--server.http.listen-addr=0.0.0.0:12345','--storage.path=/var/alloy','--disable-reporting','/etc/apm/config.alloy'])]:
  n=prefix+'-'+name;names.append(n);a=['docker','run','-d','--name',n,'--platform',PLATFORM,'--network',prefix,'-e','NO_PROXY=*','-e','no_proxy=*','--network-alias','v3-'+name+'.observability.svc','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','10001:10001','--memory',mem,'--cpus','.5','-e','GOMEMLIMIT='+go,'-e','GOGC=50','-v',str(r/'deploy/v3/apm/files')+':/etc/apm:ro','--tmpfs',('/var/tempo' if name=='tempo' else '/var/alloy')+':uid=10001,gid=10001,mode=0700']
  run(*a,images[name],*cmd)
 if not SANITIZER_ONLY:
  components=json.loads((r/'deploy/v3/releases/candidate.json').read_text())['components']
  env={'YEODAM_APM_ENABLED':'true','PYTHONPATH':'/runtime:/otel:/app','OTEL_SERVICE_NAME':'ai-api','OTEL_SERVICE_VERSION':components['ai-app']['sourceSha'],'OTEL_EXPORTER_OTLP_ENDPOINT':'http://v3-alloy.observability.svc:4318','OTEL_METRICS_EXPORTER':'none','OTEL_LOGS_EXPORTER':'none','PYTHONDONTWRITEBYTECODE':'1'}
  n=prefix+'-ai';names.append(n)
  a=['docker','run','-d','--name',n,'--platform','linux/amd64','--network',prefix,'-e','NO_PROXY=*','-e','no_proxy=*','--network-alias','apm-ai','--user','10001:10001','--read-only','--cap-drop','ALL','--memory','512m','--cpus','.5','--tmpfs','/tmp:uid=10001,gid=10001','-v',str(r/'deploy/v3/ai-apps/files/apm')+':/runtime:ro','-v',str(artifacts/'python-sdk')+':/otel:ro','-v',str(r/'tests/ci/v3/apm')+':/fixture:ro','--entrypoint','python']
  for k,v in env.items():a+=['-e',k+'='+v]
  run(*a,components['ai-app']['image'],'/fixture/ai-fixture.py')
  n=prefix+'-be';names.append(n)
  a=['docker','run','-d','--name',n,'--platform','linux/amd64','--network',prefix,'-e','NO_PROXY=*','-e','no_proxy=*','--network-alias','apm-be','--user','10001:10001','--read-only','--cap-drop','ALL','--memory','512m','--cpus','.5','--tmpfs','/tmp:uid=10001,gid=10001','-v',str(artifacts/'javaagent.jar')+':/otel/javaagent.jar:ro','-v',str(artifacts/'java-lib')+':/fixture/lib:ro','-v',str(artifacts/'java-classes')+':/fixture/classes:ro','--entrypoint','java']
  for k,v in {'JAVA_TOOL_OPTIONS':'-XX:MaxRAMPercentage=50.0 -XX:InitialRAMPercentage=12.5 -javaagent:/otel/javaagent.jar','OTEL_SERVICE_NAME':'auth-api','OTEL_EXPORTER_OTLP_ENDPOINT':'http://v3-alloy.observability.svc:4318','OTEL_EXPORTER_OTLP_PROTOCOL':'http/protobuf','OTEL_TRACES_SAMPLER':'parentbased_traceidratio','OTEL_TRACES_SAMPLER_ARG':'.01','OTEL_PROPAGATORS':'tracecontext','OTEL_METRICS_EXPORTER':'none','OTEL_LOGS_EXPORTER':'none','OTEL_BSP_SCHEDULE_DELAY':'1000','OTEL_EXPORTER_OTLP_TIMEOUT':'1000'}.items():a+=['-e',k+'='+v]
  run(*a,components['backend']['image'],'-cp','/fixture/classes:/fixture/lib/*','ApmFixture')
 probe=prefix+'-probe';names.append(probe)
 run('docker','run','-d','--name',probe,'--network',prefix,'-e','NO_PROXY=*','-e','no_proxy=*','--memory','64m','--cpus','.5','--read-only','--cap-drop','ALL','python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b','python','-c','import time;time.sleep(360)')
 code="""import json,time,uuid,urllib.request
base='http://v3-tempo.observability.svc:3200'
for i in range(50):
 try:urllib.request.urlopen(base+'/ready',timeout=1);break
 except Exception:time.sleep(.5)
else:raise RuntimeError('Tempo not ready')
for i in range(40):
 try:urllib.request.urlopen('http://v3-alloy.observability.svc:12345/-/ready',timeout=1);break
 except Exception:time.sleep(.5)
else:raise RuntimeError('Alloy not ready')
tid=uuid.uuid4().hex;sid=uuid.uuid4().hex[:16];now=time.time_ns()
kv=lambda k,v:{'key':k,'value':{'stringValue':v}}
data={'resourceSpans':[{'resource':{'attributes':[kv('service.name','apm-fixture'),kv('secret','private-fixture-value')]},'scopeSpans':[{'scope':{'name':'fixture'},'spans':[{'traceId':tid,'spanId':sid,'name':'private-fixture-value','kind':2,'startTimeUnixNano':str(now),'endTimeUnixNano':str(now+10000000),'attributes':[kv('http.request.method','GET'),kv('http.route','/fixture'),kv('authorization','private-fixture-value'),kv('yeodam.operation','process'),kv('yeodam.stage','clustering'),kv('yeodam.result','success')],'events':[{'timeUnixNano':str(now),'name':'private-fixture-value','attributes':[kv('exception.message','private-fixture-value')]}],'status':{'code':2,'message':'private-fixture-value'}}]}]}]}
bad=json.loads(json.dumps(data['resourceSpans'][0]['scopeSpans'][0]['spans'][0]));bad['spanId']=uuid.uuid4().hex[:16];bad['attributes']=[kv('yeodam.boundary','private-fixture-value'),kv('yeodam.operation','private-fixture-value'),kv('yeodam.stage','private-fixture-value'),kv('yeodam.result','private-fixture-value')];data['resourceSpans'][0]['scopeSpans'][0]['spans'].append(bad)
for boundary in ['photo.convert','trip.process.dispatch','trip.result.apply']:
 span=json.loads(json.dumps(bad));span['spanId']=uuid.uuid4().hex[:16];span['name']=boundary
 data['resourceSpans'][0]['scopeSpans'][0]['spans'].append(span)
req=urllib.request.Request('http://v3-alloy.observability.svc:4318/v1/traces',data=json.dumps(data).encode(),headers={'Content-Type':'application/json'});assert urllib.request.urlopen(req,timeout=5).status==200
for i in range(40):
 try:
  req=urllib.request.Request(base+'/api/traces/'+tid,headers={'Accept':'application/json'});body=urllib.request.urlopen(req,timeout=2).read().decode();assert 'apm-fixture' in body and 'private-fixture-value' not in body and 'ai.stage.clustering' in body and 'yeodam.result' in body and all(x in body for x in ['photo.convert','trip.process.dispatch','trip.result.apply']);print('Trace persisted, fixed business names retained and sensitive fields sanitized');break
 except Exception:time.sleep(.5)
else:raise RuntimeError('No sanitized trace')
"""
 print(run('docker','exec',probe,'python','-c',code))
 if not SANITIZER_ONLY:
  cross="""import json,time,uuid,urllib.request,socket

 trace=uuid.uuid4().hex;parent=uuid.uuid4().hex[:16]
 for i in range(240):
  try:
   req=urllib.request.Request('http://apm-be:8080/fixture',headers={'traceparent':'00-'+trace+'-'+parent+'-01'})
   response=json.load(urllib.request.urlopen(req,timeout=5));assert response['javaTrace']==trace==response['ai']['trace_id'];print(json.dumps({'trace_id':trace,'httpContext':True}));break
  except Exception as error:
   last_error=type(error).__name__+': '+str(error);time.sleep(.5)
 else:raise RuntimeError('Real image HTTP fixture failed: '+last_error)
 for i in range(40):
  try:
   req=urllib.request.Request('http://v3-tempo.observability.svc:3200/api/traces/'+trace,headers={'Accept':'application/json'})
   body=urllib.request.urlopen(req,timeout=3).read().decode();assert 'auth-api' in body and 'ai-api' in body;print('Java and AI spans retrieved together');break
  except Exception:time.sleep(.5)
 else:raise RuntimeError('Cross-language trace not stored')
 """
  result=run('docker','exec',probe,'python','-c',cross);print(result)
  trace_id=json.loads(result.splitlines()[0])['trace_id']
  for role in ['be','ai']:
   logs=run('docker','logs',prefix+'-'+role)
   assert trace_id in logs and 'cloud_apm_initialization_failed' not in logs,role+' log correlation failed'
  print('Same trace ID present in both application log fixtures')
  # A background Python process must load the same hook without starting FastAPI.
  runner=run('docker','exec','-e','OTEL_SERVICE_NAME=ai-runner',prefix+'-ai','python','/fixture/runner-fixture.py')
  runner_trace=json.loads(runner.splitlines()[-1])['runnerTraceId']
  lookup="""import time,urllib.request
 for i in range(40):
  try:
   body=urllib.request.urlopen('http://v3-tempo.observability.svc:3200/api/traces/TRACE',timeout=3).read().decode()
   assert 'ai-runner' in body and 'ai-api' in body
   print('Background HTTPX client and API trace stored; no job-wide tracing claim');break
  except Exception:time.sleep(.5)
 else:raise RuntimeError('Background HTTPX trace missing')
 """.replace('TRACE',runner_trace)
  print(run('docker','exec',probe,'python','-c',lookup))
 if SANITIZER_ONLY:print('Sanitizer-only run: no private application image compatibility claim')
 print(run('docker','stats','--no-stream','--format','{{.Name}} {{.MemUsage}}',*names))
except Exception:
 for n in names:
  d=subprocess.run(['docker','logs','--tail','25',n],capture_output=True,text=True);print(n,d.stdout,d.stderr);print(run('docker','inspect','--format','{{json .State}}',n))
 raise
finally:
 for n in names:subprocess.run(['docker','rm','-f',n],capture_output=True)
 subprocess.run(['docker','network','rm',prefix],capture_output=True)
 workspace.cleanup()
