#!/usr/bin/env python3
"""Exact candidate classes/AI receiver -> serialized record -> child -> result; synthetic services only."""
import json,os,platform,subprocess,tempfile,uuid,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
lock=json.loads((ROOT/'deploy/v3/apm/images.lock.json').read_text())
components=json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text())['components']
prefix='apm-workflow-'+uuid.uuid4().hex[:8];names=[]
public_platform='linux/arm64'if platform.system()=='Darwin'and platform.machine()=='arm64'else'linux/amd64'
out=Path(os.environ.get('V3_APM_RESULTS','/tmp/'+prefix));out.mkdir(parents=True,exist_ok=True)
def run(*args,timeout=240):
 p=subprocess.run(args,text=True,capture_output=True,timeout=timeout)
 if p.returncode:raise RuntimeError((p.stdout+p.stderr)[-3500:])
 return p.stdout
try:
 with tempfile.TemporaryDirectory(prefix=prefix)as directory:
  d=Path(directory)
  for role,image,source,target in [('java',lock['java'],'/javaagent.jar','javaagent.jar'),('python',lock['python'],'/autoinstrumentation','sdk'),('be',components['backend']['image'],'/app/app.jar','app.jar')]:
   name=prefix+'-extract-'+role
   try:
    run('docker','create','--platform','linux/amd64','--name',name,image);run('docker','cp',name+':'+source,str(d/target))
   finally:subprocess.run(['docker','rm','-f',name],capture_output=True)
  lib=d/'lib';classes=d/'classes';lib.mkdir();classes.mkdir()
  with zipfile.ZipFile(d/'app.jar')as jar:
   for item in jar.namelist():
    if item.startswith('BOOT-INF/lib/')and item.endswith('.jar'):(lib/Path(item).name).write_bytes(jar.read(item))
    if item.startswith('BOOT-INF/classes/')and not item.endswith('/'):
     p=classes/item.removeprefix('BOOT-INF/classes/');p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(jar.read(item))
  run('javac','-cp',str(classes)+os.pathsep+str(lib/'*'),'-d',str(classes),str(ROOT/'tests/ci/v3/apm/WorkflowFixture.java'))
  run('docker','network','create','--internal',prefix)
  for role,cpu,mem,go in [('alloy','.2','128m','80MiB'),('tempo','.5','384m','256MiB')]:
   name=prefix+'-'+role;names.append(name)
   command=['run','--server.http.listen-addr=0.0.0.0:12345','--storage.path=/var/alloy','--disable-reporting','/etc/apm/config.alloy']if role=='alloy'else['-target=all','-config.file=/etc/apm/tempo.yaml']
   run('docker','run','-d','--name',name,'--platform',public_platform,'--network',prefix,'--network-alias','v3-'+role+'.observability.svc','--user','10001:10001','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory',mem,'--cpus',cpu,'-e','GOMEMLIMIT='+go,'-e','GOGC=50','-v',str(ROOT/'deploy/v3/apm/files')+':/etc/apm:ro','--tmpfs',('/var/alloy'if role=='alloy'else'/var/tempo')+':uid=10001,gid=10001',lock[role],*command)
  mysql=prefix+'-mysql';names.append(mysql)
  mysql_image=json.loads((ROOT/'deploy/v3/metrics/images.lock.json').read_text())['outbox-query']
  run('docker','run','-d','--name',mysql,'--platform',public_platform,'--network',prefix,'--network-alias','workflow-mysql','--memory','512m','--cpus','.5','-e','MYSQL_ROOT_PASSWORD=synthetic-only','-e','MYSQL_ROOT_HOST=%',mysql_image,'--skip-log-bin','--mysqlx=0')
  for role,service in [('convert','photo-converter'),('worker','trip-result-worker')]:
   name=prefix+'-'+role;names.append(name)
   env={'JAVA_TOOL_OPTIONS':'-Xmx192m -javaagent:/otel/javaagent.jar','OTEL_SERVICE_NAME':service,'OTEL_EXPORTER_OTLP_ENDPOINT':'http://v3-alloy.observability.svc:4318','OTEL_EXPORTER_OTLP_PROTOCOL':'http/protobuf','OTEL_TRACES_SAMPLER':'parentbased_traceidratio','OTEL_TRACES_SAMPLER_ARG':'.01','OTEL_PROPAGATORS':'tracecontext','OTEL_METRICS_EXPORTER':'none','OTEL_LOGS_EXPORTER':'none','OTEL_BSP_MAX_QUEUE_SIZE':'512','OTEL_BSP_MAX_EXPORT_BATCH_SIZE':'64','OTEL_BSP_SCHEDULE_DELAY':'200','OTEL_EXPORTER_OTLP_TIMEOUT':'2000','OTEL_INSTRUMENTATION_HIBERNATE_ENABLED':'false','OTEL_INSTRUMENTATION_SPRING_DATA_ENABLED':'false','FIXTURE_MYSQL_URL':'jdbc:mysql://workflow-mysql:3306/mysql?sslMode=DISABLED&allowPublicKeyRetrieval=true&openTelemetry=DISABLED'}
   args=['docker','run','-d','--name',name,'--platform','linux/amd64','--network',prefix,'--network-alias','workflow-'+role,'--user','10001:10001','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','512m','--cpus','.5','--tmpfs','/tmp:uid=10001,gid=10001','-v',str(d/'javaagent.jar')+':/otel/javaagent.jar:ro','-v',str(lib)+':/fixture/lib:ro','-v',str(classes)+':/fixture/classes:ro','--entrypoint','java']
   for k,v in env.items():args+=['-e',k+'='+v]
   run(*args,components['backend']['image'],'-cp','/fixture/classes:/fixture/lib/*','WorkflowFixture',role)
  name=prefix+'-ai';names.append(name)
  env={'YEODAM_APM_ENABLED':'true','YEODAM_APM_RELIABILITY_ENABLED':'true','PYTHONPATH':'/runtime:/otel:/app','OTEL_SERVICE_NAME':'ai-api','OTEL_SERVICE_VERSION':components['ai-app']['sourceSha'],'OTEL_EXPORTER_OTLP_ENDPOINT':'http://v3-alloy.observability.svc:4318','OTEL_METRICS_EXPORTER':'none','OTEL_LOGS_EXPORTER':'none','PYTHONDONTWRITEBYTECODE':'1'}
  args=['docker','run','-d','--name',name,'--platform','linux/amd64','--network',prefix,'--network-alias','workflow-ai','--user','10001:10001','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory','512m','--cpus','.5','--tmpfs','/tmp:uid=10001,gid=10001','-v',str(d/'sdk')+':/otel:ro','-v',str(ROOT/'deploy/v3/ai-apps/files/apm')+':/runtime:ro','-v',str(ROOT/'tests/ci/v3/apm')+':/fixture:ro','--entrypoint','python']
  for k,v in env.items():args+=['-e',k+'='+v]
  run(*args,components['ai-app']['image'],'/fixture/workflow-ai.py')
  probe=prefix+'-probe';names.append(probe)
  run('docker','run','-d','--name',probe,'--network',prefix,'--read-only','--cap-drop','ALL','--memory','96m','--cpus','.5','-v',str(ROOT/'scripts')+':/evidence:ro','-v',str(ROOT/'tests/ci/v3/apm')+':/fixture:ro','-e','PYTHONPATH=/evidence','python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b','python','-c','import time;time.sleep(600)')
  result=run('docker','exec',probe,'python','/fixture/workflow-probe.py','normal');(out/'workflow.json').write_text(result);print(result)
  # Collector unavailability must not change the synthetic business result or be called a complete trace.
  run('docker','pause',prefix+'-alloy')
  try:
   result=run('docker','exec',probe,'python','/fixture/workflow-probe.py','unavailable');(out/'unavailable.json').write_text(result);print(result)
  finally:run('docker','unpause',prefix+'-alloy')
  result=run('docker','exec',probe,'python','/fixture/workflow-probe.py','normal');(out/'recovered.json').write_text(result);print(result)
finally:
 for name in names:
  logs=subprocess.run(['docker','logs',name],text=True,capture_output=True);(out/(name+'.log')).write_text(logs.stdout+logs.stderr)
 subprocess.run(['docker','rm','-f',*names],capture_output=True);subprocess.run(['docker','network','rm',prefix],capture_output=True)
 print('Synthetic diagnostics: '+str(out))
