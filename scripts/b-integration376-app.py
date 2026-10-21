#!/usr/bin/env python3
"""Install the pinned #333 rehearsal BE without changing the original container."""
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import hashlib
import re
import secrets
from urllib.request import Request,urlopen
from urllib.parse import quote
from uuid import UUID

IMAGE='ghcr.io/100-hours-a-week/yeodam-backend@sha256:db08f44e07474f336041f9653cccdd20d15f2d4d2cf1607bf986b7b12c622ef3'
ORIGINAL='yeodam-app-backend-1'
BASE=Path('/var/lib/yeodam/b391')
class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.connect('/var/run/docker.sock')
def docker(method,path,data=None):
    c=Connection('localhost',timeout=40);c.request(method,path,body=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json'})
    r=c.getresponse();b=r.read();c.close()
    if r.status not in (200,201,204):raise RuntimeError('Docker action HTTP '+str(r.status))
    return json.loads(b) if b else {}
def inspect(name):return docker('GET','/containers/'+quote(name,safe='')+'/json')
def mode(run,value):
    d=Path('/run/yeodam-b391')/run;d.mkdir(mode=0o755,exist_ok=True)
    d.chmod(0o755)
    p=d/'mode';q=d/'mode.next'
    with q.open('w') as f:f.write(value+'\n');f.flush();os.fsync(f.fileno())
    q.chmod(0o644);os.replace(q,p)
    print(json.dumps({'mode':value,'runId':run}),flush=True)
def create(run,consumer):
    role='consumer' if consumer else 'app';name='yeodam-b391-'+role
    base=inspect(ORIGINAL)
    if 'migration-probe-v1-sha-816d69ba' not in base['Config']['Image']:raise RuntimeError('Original V1 image differs')
    env=dict(s.split('=',1) for s in base['Config']['Env'] if '=' in s)
    env.update({'MIGRATION_KAFKA_BOOTSTRAP_SERVERS':'10.30.10.95:9092',
      'MIGRATION_TRIP_TOPIC':'staging.yeodam.migration.b391.'+run+'.requests',
      'MIGRATION_TRIP_CONSUMER_GROUP':'yeodam-b391-'+run,
      'MIGRATION_TRIP_CONCURRENCY':'8' if consumer else '4',
      'MIGRATION_TRIP_PARTITIONS':'8',
      # The public service owns housekeeping; the dedicated request executor must
      # not run a second copy or reopen Target connections before Bulk import.
      'MIGRATION_SERVICE_SCHEDULERS_ENABLED':'false' if consumer else 'true',
      'MIGRATION_AUTH_ENABLED':'true',
      'MIGRATION_AUTH_TOPIC':'staging.yeodam.migration.b391.'+run+'.auth-requests',
      'MIGRATION_AUTH_CONSUMER_ENABLED':'false',
      'MIGRATION_AUTH_RESULTS_ENABLED':'false' if consumer else 'true',
      'MIGRATION_AUTH_CONSUMER_GROUP':'yeodam-b391-auth-'+run,
      'MIGRATION_AUTH_RESULT_GROUP':'yeodam-b391-auth-result-'+run,
      'MIGRATION_AUTH_KEY_FILE':'/run/migration-auth/aes.hex',
      'MIGRATION_AUTH_REDIS_FILE':'/run/migration-auth/redis.json',
      'MIGRATION_TRIP_CONSUMER_ENABLED':'false' if consumer else 'true','MIGRATION_TRIP_APPLY_ROLE':'target' if consumer else 'source','SPRING_FLYWAY_ENABLED':'false',
      # A synchronous Kafka reply wait must not retain a request-scoped JPA connection.
      'SPRING_JPA_OPEN_IN_VIEW':'false',
      # Nginx auth read timeout is 60s; reserve 5s for returning the response.
      'MIGRATION_AUTH_RESPONSE_TIMEOUT_MS':'55000',
      'JAVA_TOOL_OPTIONS':'-Xms128m -Xmx512m -XX:ActiveProcessorCount=2'})
    hc={'NetworkMode':'yeodam-app-network','RestartPolicy':{'Name':'no'},
        'PortBindings':{'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'18081' if consumer else '8080'}]}}
    if consumer:
        env['MYSQL_URL']='jdbc:mysql://10.30.10.28:3306/yeodam_v1_source';env['MIGRATION_TRIP_MODE_FILE']=''
        # Release idle Target connections before live Snapshot/Bulk import.
        env['SPRING_DATASOURCE_HIKARI_MINIMUM_IDLE']='0'
        env['SPRING_DATASOURCE_HIKARI_IDLE_TIMEOUT']='10000'
        env['MIGRATION_CONSUMER_ENABLE_FILE']='/run/migration-auth/apply-enabled'
        env['MIGRATION_CONSUMER_DEFER_TRIP']='true'
    else:
        mode(run,'source-queue');d='/run/yeodam-b391/'+run
        env['MIGRATION_TRIP_MODE_FILE']=d+'/mode';hc['Binds']=[d+':'+d+':ro']
        env.update(MIGRATION_SOURCE_CONTROL_PORT='8091',MIGRATION_SOURCE_CONTROL_RUN_ID=run,MIGRATION_SOURCE_CONTROL_TOKEN_FILE='/run/migration-auth/control.token')
        hc['PortBindings']['8091/tcp']=[{'HostIp':'127.0.0.1','HostPort':'18082'}]
    hc.setdefault('Binds',[]).append('/run/yeodam-b391/'+run+'/container-auth:/run/migration-auth:ro')
    payload={'Image':IMAGE,'Env':[k+'='+v for k,v in env.items()],'HostConfig':hc,'ExposedPorts':{'8080/tcp':{}},'Labels':{'yeodam.issue':'391','yeodam.run':run}}
    if not consumer:
        if not base['State']['Running']:raise RuntimeError('Original V1 BE must be running before replacement')
        BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
        record=BASE/(run+'.original.json')
        if record.exists():raise RuntimeError('Run already has an app checkpoint')
        record.write_text(json.dumps({'original':ORIGINAL,'runId':run,'startedAt':time.time()}));record.chmod(0o600)
        docker('POST','/containers/'+ORIGINAL+'/stop?t=30')
    try:
        docker('POST','/containers/create?name='+name,payload);docker('POST','/containers/'+name+'/start')
    except Exception:
        if not consumer:docker('POST','/containers/'+ORIGINAL+'/start')
        raise
    print(json.dumps({'started':name,'runId':run,'consumerEnabled':consumer}),flush=True)
def auth_files(run):
    import shutil
    config=json.loads((Path('/run/yeodam-b391')/run/'auth.json').read_text())
    result=subprocess.run(['aws','secretsmanager','get-secret-value','--secret-id','yeodam-v2-staging/redis-auth-token','--region','ap-northeast-2','--output','json'],capture_output=True,text=True,check=True)
    redis=dict(config['redis']);redis['password']=json.loads(result.stdout)['SecretString']
    directory=Path('/run/yeodam-b391')/run/'container-auth';directory.mkdir(mode=0o750)
    shutil.copyfile('/run/yeodam-cdc-auth368/aes.hex',directory/'aes.hex')
    (directory/'redis.json').write_text(json.dumps(redis))
    (directory/'control.token').write_text(secrets.token_hex(32))
    for path in directory.iterdir():path.chmod(0o400);os.chown(path,1001,1001)
    os.chown(directory,1001,1001)
    print(json.dumps({'phase':'auth_private_files_prepared','runId':run}))

def source_control(run,fence=False):
    token=(Path('/run/yeodam-b391')/run/'container-auth'/'control.token').read_text().strip()
    request=Request('http://127.0.0.1:18082/'+('fence' if fence else 'state'),method='POST' if fence else 'GET',headers={'X-Migration-Control-Token':token,'X-Migration-Run':run})
    with urlopen(request,timeout=3) as response:return json.load(response)

def source_ready(run):
    end=time.monotonic()+40
    while time.monotonic()<end:
        try:state=source_control(run)
        except OSError:
            time.sleep(.5);continue
        if state['runId']==run and not state['fencing'] and state['sourceConsumerRunning'] and state['sourceAssignedPartitions']==8:
            print(json.dumps(state));return
        time.sleep(.5)
    raise RuntimeError('Source Consumer not assigned before input')

def collect_completions(name,run):
    # Docker's stdout multiplexing frames are binary; decode only matching safe records.
    c=Connection('localhost',timeout=30);c.request('GET','/containers/'+name+'/logs?stdout=1&stderr=1');r=c.getresponse();raw=r.read();c.close()
    if r.status!=200:raise RuntimeError('Completion log read failed')
    chunks=[]
    while len(raw)>=8:
        length=int.from_bytes(raw[4:8],'big');chunks.append(raw[8:8+length]);raw=raw[8+length:]
    pattern=re.compile(r'migration_trip_completed request_id=([0-9a-f-]{36}) origin=(source|target) queued_at_ms=(\d+) committed_at_ms=(\d+)')
    timing_pattern=re.compile(r'consumer_started_at_ms=(\d+) apply_elapsed_us=(\d+) partition=(\d+)')
    path=Path('/run/yeodam-b391')/run/'completions.json'
    rows=json.loads(path.read_text()) if path.exists() else {}
    from uuid import uuid5
    smoke=str(uuid5(UUID(run),'public-b2-smoke'))
    text=b''.join(chunks).decode(errors='replace')
    for match in pattern.finditer(text):
        rid,origin,queued,committed=match.groups()
        if rid!=smoke and int(queued)>0:
            row={'origin':origin,'queuedAtMs':int(queued),'committedAtMs':int(committed)}
            timing=timing_pattern.match(text,match.end()+1)
            if timing:
                received,elapsed,partition=map(int,timing.groups())
                row.update(consumerStartedAtMs=received,applyElapsedUs=elapsed,partition=partition)
            rows.setdefault(rid,row)
    path.write_text(json.dumps(rows));path.chmod(0o600)
    def percentiles(values):
        values=sorted(values)
        return {k:values[int((len(values)-1)*q)] for k,q in [('p50',.5),('p95',.95),('p99',.99),('max',1)]} if values else {}
    timed=[v for v in rows.values() if 'consumerStartedAtMs' in v]
    detail={}
    for origin in ('source','target'):
        selected=[v for v in timed if v['origin']==origin]
        detail[origin]={'count':len(selected),'queueWaitMs':percentiles([v['consumerStartedAtMs']-v['queuedAtMs'] for v in selected]),'applyElapsedUs':percentiles([v['applyElapsedUs'] for v in selected]),'partitionCounts':{str(p):sum(v['partition']==p for v in selected) for p in range(8)}}
    return {'timingCount':len(timed),'timingByOrigin':detail,'count':len(rows),'origins':{origin:sum(v['origin']==origin for v in rows.values()) for origin in ('source','target')},'commandCreatedToCommitObservedMs':percentiles([v['committedAtMs']-v['queuedAtMs'] for v in rows.values()])}

def remove(name,run):
    v=inspect(name)
    if v['Config'].get('Labels',{}).get('yeodam.issue')!='391' or v['Config']['Labels'].get('yeodam.run')!=run:raise RuntimeError('Container ownership differs')
    if v['State']['Running']:docker('POST','/containers/'+name+'/stop?t=30')
    metrics=collect_completions(name,run)
    docker('DELETE','/containers/'+name)
    print(json.dumps({'removed':name,'completionMetrics':metrics}))
def status_route(run,restore=False):
    directory=BASE/run;directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    record=directory/'nginx-route.json';backup=directory/'nginx.before'
    if restore:
        state=json.loads(record.read_text());path=Path(state['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest()!=state['candidateSha256']:raise RuntimeError('Nginx changed outside this Run')
        path.write_bytes(backup.read_bytes())
    else:
        if record.exists():raise RuntimeError('Run already owns a status route')
        dump=subprocess.run(['nginx','-T'],capture_output=True,text=True,check=True).stdout
        files=re.split(r'# configuration file ([^\n]+):\n',dump)
        matches=[]
        for index in range(1,len(files),2):
            path=Path(files[index]);body=files[index+1]
            if 'v1-staging.yeodam-2gether.com' in body and re.search(r'location\s+/api/\s*\{',body):matches.append(path)
        if len(matches)!=1:raise RuntimeError('Expected exactly one V1 API vhost')
        path=matches[0];body=path.read_text();backup.write_text(body);backup.chmod(0o600)
        marker='map "$request_method:$uri:$http_x_request_id" $yeodam_staging_request_id {'
        if body.count(marker)!=1:raise RuntimeError('Expected the original staging request-ID map')
        body=body.replace(marker,marker+'\n    "~^POST:/api/trips:(?<yeodam_valid_b376_request_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$" $yeodam_valid_b376_request_id;')
        insertion='''location ^~ /api/migration/trips/requests/ {
            proxy_pass http://127.0.0.1:18081;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_intercept_errors on;
            error_page 502 =503 @b376_pending;
        }
        location @b376_pending { return 503; }
        '''
        body,count=re.subn(r'(?=location\s+/api/\s*\{)',lambda _:insertion,body)
        if count!=1:raise RuntimeError('Expected one API location')
        path.write_text(body)
        record.write_text(json.dumps({'path':str(path),'candidateSha256':hashlib.sha256(body.encode()).hexdigest()}));record.chmod(0o600)
    checked=subprocess.run(['nginx','-t'],capture_output=True)
    if checked.returncode:
        if not restore:path.write_bytes(backup.read_bytes())
        raise RuntimeError('Nginx candidate invalid; original retained')
    subprocess.run(['systemctl','reload','nginx'],check=True,capture_output=True)
    print(json.dumps({'statusRoute':'restored' if restore else 'installed','runId':run}))

def restore_original():
    docker('POST','/containers/'+ORIGINAL+'/start')
    end=time.monotonic()+100
    while time.monotonic()<end:
        try:
            with urlopen('http://127.0.0.1:8080/api/actuator/health',timeout=2) as response:
                if response.status==200:
                    print('ORIGINAL_STARTED_HEALTH_200');return
        except OSError:pass
        time.sleep(1)
    raise RuntimeError('Original V1 startup did not become healthy; do not start next Run')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('auth-files','start-app','start-consumer','queue','source-queue','source-ready','direct','stop-app','stop-consumer','enable-consumer','restore-original','status','route-status','restore-route'))
    p.add_argument('--run-id',required=True);p.add_argument('--deadline',type=float);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id or '10.30.0.174' not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Unexpected host or UUID')
    if a.action=='auth-files':auth_files(a.run_id)
    elif a.action in ('route-status','restore-route'):status_route(a.run_id,a.action=='restore-route')
    elif a.action=='enable-consumer':
        path=Path('/run/yeodam-b391')/a.run_id/'container-auth'/'apply-enabled';path.touch(mode=0o444,exist_ok=False)
        end=a.deadline if a.deadline is not None else time.time()+30
        while time.time()<end:
            try:
                with urlopen('http://127.0.0.1:18081/api/actuator/health',timeout=1) as response:
                    if response.status==200:break
            except Exception:pass
            time.sleep(.05)
        else:raise RuntimeError('Target listener activation exceeded fixed budget')
        print(json.dumps({'consumerApplyEnabled':True,'runId':a.run_id,'activatedAt':time.time()}))
    elif a.action.startswith('start-'):create(a.run_id,a.action=='start-consumer')
    elif a.action=='source-ready':source_ready(a.run_id)
    elif a.action in ('queue','source-queue','direct'):mode(a.run_id,a.action)
    elif a.action.startswith('stop-'):remove('yeodam-b391-'+a.action[5:],a.run_id)
    elif a.action=='restore-original':restore_original()
    else:
        for name in (ORIGINAL,'yeodam-b391-app','yeodam-b391-consumer'):
            try:
                s=inspect(name);print(json.dumps({'container':name,'status':s['State']['Status'],'health':s['State'].get('Health',{}).get('Status')}))
            except RuntimeError:print(json.dumps({'container':name,'status':'absent'}))
