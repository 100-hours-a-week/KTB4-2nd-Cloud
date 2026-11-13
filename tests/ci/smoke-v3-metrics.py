#!/usr/bin/env python3
"""Pinned Prometheus/Grafana/exporter runtime; synthetic private network, no AWS."""
import importlib.util
import json
import os
import platform
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid
ROOT=Path(__file__).resolve().parents[2]
CHART=ROOT/'deploy/v3/metrics'
images=json.loads((CHART/'images.lock.json').read_text())
spec=importlib.util.spec_from_file_location('chart',ROOT/'scripts/check-v3-metrics.py');chart=importlib.util.module_from_spec(spec);spec.loader.exec_module(chart)
PLATFORM='linux/arm64' if platform.system()=='Darwin' and platform.machine()=='arm64' else 'linux/amd64'
prefix='metrics-fixture-'+uuid.uuid4().hex[:10];network=prefix+'-net';containers=[]
def run(*args,timeout=90):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('Fixture command failed: '+p.stderr[-1400:])
    return p.stdout

def start(role,image,args,volumes=(),env=(),extra=(),memory='64m',network_mode=None):
    name=prefix+'-'+role;containers.append(name)
    cmd=['docker','run','-d','--name',name,'--platform',PLATFORM,'--network',network_mode or network,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--memory',memory,'--cpus','0.5']
    if network_mode is None:cmd+=['--network-alias','v3-'+role]
    for host,target in volumes:cmd+=['-v',str(host)+':'+target+':ro']
    for value in env:cmd+=['-e',value]
    return run(*cmd,*extra,image,*args).strip()

with tempfile.TemporaryDirectory(prefix='v3-metrics-') as tmp:
    root=Path(tmp);conf=root/'config';conf.mkdir();sa=root/'sa';sa.mkdir()
    docs=chart.render(apm=True,logs=True,outbox=True)
    cm=next(x for x in docs if x['kind']=='ConfigMap' and x['metadata']['name']=='v3-metrics-config')['data']
    for key,value in cm.items():(conf/key).write_text(value)
    import yaml
    grafana_config=next(x for x in docs if x['kind']=='ConfigMap' and x['metadata']['name']=='v3-grafana-config')['data']
    sources=yaml.safe_load(grafana_config['datasource.yaml'])
    next(x for x in sources['datasources'] if x['uid']=='v3-cloudwatch-logs')['jsonData']['endpoint']='http://127.0.0.1:8085'
    (conf/'datasource.yaml').write_text(yaml.safe_dump(sources))

    shutil.copyfile(ROOT/'tests/ci/v3/metrics-alerts.test.yaml',conf/'alerts.test.yaml')
    (conf/'ai-auth').mkdir();(conf/'ai-auth/API_KEY').write_text('synthetic-fixture-only')
    (sa/'token').write_text('synthetic-fixture-only');(sa/'namespace').write_text('observability')
    cafile=ssl.get_default_verify_paths().cafile
    assert cafile,'A public CA bundle is required for the isolated parser check'
    shutil.copyfile(cafile,sa/'ca.crt')
    try:
        # Validate exact deployed config, including auth files and Kubernetes discovery.
        run('docker','run','--rm','--platform',PLATFORM,'--network','none','--read-only','--cap-drop','ALL','-e','KUBERNETES_SERVICE_HOST=127.0.0.1','-e','KUBERNETES_SERVICE_PORT=443','-v',str(conf)+':/etc/prometheus:ro','-v',str(sa)+':/var/run/secrets/kubernetes.io/serviceaccount:ro','--entrypoint','/bin/promtool',images['prometheus'],'check','config','/etc/prometheus/prometheus.yaml')
        run('docker','run','--rm','--platform',PLATFORM,'--network','none','-v',str(conf)+':/etc/prometheus:ro','--entrypoint','/bin/promtool',images['prometheus'],'test','rules','/etc/prometheus/alerts.test.yaml')
        helptext=run('docker','run','--rm','--platform',PLATFORM,'--network','none',images['kube-state-metrics'],'--help')
        assert '--metric-allowlist' in helptext and '--namespaces' in helptext
        run('docker','network','create','--internal',network)
        fake=root/'fake.py';fake.write_text('''import json
from http.server import BaseHTTPRequestHandler,HTTPServer
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  data=json.dumps({'name':'yeodam-v3-outbox','connector':{'state':'RUNNING'},'tasks':[{'id':0,'state':'FAILED','trace':'fixture-not-for-export'}]}).encode()
  self.send_response(200);self.end_headers();self.wfile.write(data)
 def log_message(self,*args):pass
HTTPServer(('0.0.0.0',8083),H).serve_forever()
''')
        start('fake',images['cdc-status'],['python','-B','/fake.py'],[(fake,'/fake.py')],extra=['--user','65534:65534','--network-alias','yeodam-v3-connect-connect-api.kafka.svc'])
        start('cdc-status',images['cdc-status'],['python','-B','/exporter/cdc-status.py'],[(CHART/'files','/exporter')],extra=['--user','65534:65534'])
        start('prometheus',images['prometheus'],['--config.file=/etc/prometheus/prometheus.yaml','--storage.tsdb.path=/prometheus','--storage.tsdb.retention.time=3d','--storage.tsdb.retention.size=4GB'],[(conf,'/etc/prometheus'),(sa,'/var/run/secrets/kubernetes.io/serviceaccount')],['KUBERNETES_SERVICE_HOST=127.0.0.1','KUBERNETES_SERVICE_PORT=443'],extra=['--user','65534:65534','--tmpfs','/prometheus:uid=65534,gid=65534,mode=0700'],memory='512m')
        grafana=next(x for x in docs if x['kind']=='Deployment' and x['metadata']['name']=='v3-grafana')['spec']['template']['spec']['containers'][0]
        start('grafana',images['grafana'],[],[(conf/'datasource.yaml','/etc/grafana/provisioning/datasources/default.yaml'),(sa/'token','/fixture/token'),(CHART/'files/dashboard-provider.yaml','/etc/grafana/provisioning/dashboards/default.yaml'),(CHART/'files/operations.json','/etc/grafana/dashboards/operations.json')],[x['name']+'='+x['value'] for x in grafana['env']]+['AWS_CONTAINER_CREDENTIALS_FULL_URI=http://127.0.0.1:8085/credentials','AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE=/fixture/token'],extra=['--user','472:472','--tmpfs','/var/lib/grafana:uid=472,gid=472,mode=0700','--tmpfs','/tmp:uid=472,gid=472'],memory='256m')
        start('cloudwatch-fixture',images['cdc-status'],['python','-B','/fixture.py'],[(ROOT/'tests/ci/v3/cloudwatch-fixture.py','/fixture.py')],extra=['--user','65534:65534'],network_mode='container:'+prefix+'-grafana')
        probe='''import json,urllib.request,urllib.error
from urllib.parse import quote
base='http://v3-grafana:3000'
r=json.load(urllib.request.urlopen(base+'/api/dashboards/uid/yeodam-v3-operations',timeout=3));assert r['dashboard']['uid']=='yeodam-v3-operations'
assert json.load(urllib.request.urlopen(base+'/api/datasources/uid/v3-prometheus/health',timeout=3))['status']=='OK'
assert json.load(urllib.request.urlopen(base+'/api/frontend/settings',timeout=3))['viewersCanEdit'] is True
for expression in ['up{job="prometheus"}','prometheus_tsdb_head_series']:
 data=json.load(urllib.request.urlopen('http://v3-prometheus:9090/api/v1/query?query='+quote(expression),timeout=3))['data']['result']
 assert data,'Self-scrape or required head-series metric missing'
 if expression.startswith('up'):assert data[0]['value'][1]=='1','Self-scrape rejected by sample limit'
q='http://v3-prometheus:9090/api/v1/query?query='+quote('yeodam_cdc_task_state{state="FAILED"}')
r=json.load(urllib.request.urlopen(q,timeout=3));assert r['data']['result'][0]['value'][1]=='1'
raw=urllib.request.urlopen('http://v3-cdc-status:8080/metrics').read();assert b'fixture-not-for-export' not in raw
try:
 req=urllib.request.Request(base+'/api/dashboards/db',data=b'{}',headers={'Content-Type':'application/json'})
 urllib.request.urlopen(req,timeout=3);raise AssertionError('Viewer write accepted')
except urllib.error.HTTPError as err:assert err.code in (401,403)
# Exercise the shipped CloudWatch plugin through Grafana as an anonymous Viewer.
sources=json.load(urllib.request.urlopen(base+'/api/datasources',timeout=3))
assert any(x['uid']=='v3-cloudwatch-logs' and x['type']=='cloudwatch' for x in sources)
def query(subtype,**values):
 target={'refId':'L','datasource':{'uid':'v3-cloudwatch-logs','type':'cloudwatch'},'type':'logAction','queryMode':'Logs','subtype':subtype,'region':'ap-northeast-2','intervalMs':1000,'maxDataPoints':200,**values}
 req=urllib.request.Request(base+'/api/ds/query',data=json.dumps({'from':'1794354300000','to':'1794355200000','queries':[target]}).encode(),headers={'Content-Type':'application/json'})
 result=json.load(urllib.request.urlopen(req,timeout=10))['results']['L']
 assert not result.get('error'),result.get('error');return result
result=query('StartQuery',queryString='fields @timestamp, app.job_id, app.trace_id | limit 200',logGroupNames=['/yeodam/v3/production/backend'],queryLanguage='CWLI')
assert '00000000-0000-4000-8000-000000000001' in json.dumps(result)
result=query('GetQueryResults',queryId='00000000-0000-4000-8000-000000000001')
assert 'fixture-job' in json.dumps(result) and 'cccccccccccccccccccccccccccccccc' in json.dumps(result)
try:health=json.load(urllib.request.urlopen(base+'/api/datasources/uid/v3-cloudwatch-logs/health',timeout=8))
except urllib.error.HTTPError as error:health=json.load(error)
assert health['status']=='OK',health
proof=json.load(urllib.request.urlopen('http://v3-grafana:8085/proof',timeout=3))
assert all(proof[k]>0 for k in ['credentials','StartQuery','GetQueryResults','ListMetrics'])
try:
 req=urllib.request.Request(base+'/api/datasources',data=b'{}',headers={'Content-Type':'application/json'});urllib.request.urlopen(req,timeout=3);raise AssertionError('Viewer datasource write accepted')
except urllib.error.HTTPError as error:assert error.code in (401,403)
print(json.dumps({'passed':True,'checks':['exact Prometheus config and rule syntax','KSM CLI compatibility','Grafana provisioned dashboard and datasource','Viewer Explore enabled but writes denied','Connector RUNNING with Task FAILED visible','private trace omitted','CloudWatch bundled plugin and container-credential SDK path','Viewer Logs Insights start/result and datasource writes denied'],'aws':False}))
'''
        deadline=time.monotonic()+65
        while time.monotonic()<deadline:
            p=subprocess.run(['docker','exec',prefix+'-cdc-status','python','-c',probe],capture_output=True,text=True,timeout=20)
            if p.returncode==0:
                print(p.stdout.strip());print(json.dumps({'runtimePlatform':PLATFORM}));break
            time.sleep(2)
        else:
            for name in containers:
                state=run('docker','inspect','--format','{{json .State}}',name);print(state)
                diagnostic=subprocess.run(['docker','logs','--tail','12',name],capture_output=True,text=True)
                print(name,diagnostic.stdout[-1800:],diagnostic.stderr[-1800:])
            raise AssertionError('Synthetic metrics stack did not become ready: '+p.stderr[-1500:])
    finally:
        for name in reversed(containers):subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
        subprocess.run(['docker','network','rm',network],capture_output=True,timeout=20)
