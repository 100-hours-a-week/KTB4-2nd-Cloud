import json,sys,time,uuid,urllib.request
from v3_workflow_trace import inspect_trace
mode=sys.argv[1]
for base in ['http://workflow-convert:8080','http://workflow-worker:8080','http://workflow-ai:8000']:
 for _ in range(200):
  try:urllib.request.urlopen(base+'/ready',timeout=2);break
  except OSError:time.sleep(.5)
 else:raise RuntimeError('Fixture not ready: '+base)
tid=uuid.uuid4().hex;parent=uuid.uuid4().hex[:16];started=time.monotonic()
if mode=='normal':
 for _ in range(100):
  try:
   query=urllib.request.Request('http://workflow-convert:8080/jdbc',headers={'traceparent':'00-'+tid+'-'+parent+'-01'})
   assert json.load(urllib.request.urlopen(query,timeout=4))['jdbc'];break
  except OSError:time.sleep(.5)
 else:raise RuntimeError('Synthetic JDBC not ready')
req=urllib.request.Request('http://workflow-convert:8080/workflow',headers={'traceparent':'00-'+tid+'-'+parent+'-01'})
assert json.load(urllib.request.urlopen(req,timeout=25))['completed']
elapsed=time.monotonic()-started
if mode=='unavailable':
 assert elapsed<20,'Telemetry blocked synthetic work'
 print(json.dumps({'businessCompleted':True,'traceEvidenceReady':False,'telemetryIntegrity':'collector_unavailable','seconds':round(elapsed,3)}));sys.exit()
last={}
for _ in range(160):
 try:
  req=urllib.request.Request('http://v3-tempo.observability.svc:3200/api/traces/'+tid,headers={'Accept':'application/json'})
  body=json.load(urllib.request.urlopen(req,timeout=5));last=inspect_trace(body,tid)
  scopes=[scope for batch in body.get('batches',[])for scope in batch.get('scopeSpans',[])]
  jdbc=any('jdbc' in scope.get('scope',{}).get('name','')and scope.get('spans')for scope in scopes)
  native=any('mysql' in scope.get('scope',{}).get('name','').lower()for scope in scopes)
  assert not native,'MySQL native duplicate instrumentation remains'
  if last['tracePathComplete']and jdbc:break
 except OSError:pass
 time.sleep(.5)
else:raise RuntimeError('Production boundary graph incomplete: '+json.dumps(last))
print(json.dumps({'businessCompleted':True,'tracePathComplete':True,'boundaries':last,'seconds':round(elapsed,3)}))
