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
from urllib.parse import quote
from uuid import UUID

IMAGE='ghcr.io/100-hours-a-week/yeodam-backend@sha256:34ea639f509fcc955bd325e9ba1ce38ab0c38d35f77e8cab49175304c996cab9'
ORIGINAL='yeodam-app-backend-1'
BASE=Path('/var/lib/yeodam/b388')
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
    d=Path('/run/yeodam-b388')/run;d.mkdir(mode=0o755,exist_ok=True)
    d.chmod(0o755)
    p=d/'mode';q=d/'mode.next'
    with q.open('w') as f:f.write(value+'\n');f.flush();os.fsync(f.fileno())
    q.chmod(0o644);os.replace(q,p)
    print(json.dumps({'mode':value,'runId':run}),flush=True)
def create(run,consumer):
    role='consumer' if consumer else 'app';name='yeodam-b388-'+role
    base=inspect(ORIGINAL)
    if 'migration-probe-v1-sha-816d69ba' not in base['Config']['Image']:raise RuntimeError('Original V1 image differs')
    env=dict(s.split('=',1) for s in base['Config']['Env'] if '=' in s)
    env.update({'MIGRATION_KAFKA_BOOTSTRAP_SERVERS':'10.30.10.95:9092',
      'MIGRATION_TRIP_TOPIC':'staging.yeodam.migration.b388.'+run+'.requests',
      'MIGRATION_TRIP_CONSUMER_GROUP':'yeodam-b388-'+run,
      'MIGRATION_TRIP_CONCURRENCY':'4',
      'MIGRATION_AUTH_ENABLED':'true',
      'MIGRATION_AUTH_TOPIC':'staging.yeodam.migration.b388.'+run+'.auth-requests',
      'MIGRATION_AUTH_CONSUMER_ENABLED':'false',
      'MIGRATION_AUTH_RESULTS_ENABLED':'false' if consumer else 'true',
      'MIGRATION_AUTH_CONSUMER_GROUP':'yeodam-b388-auth-'+run,
      'MIGRATION_AUTH_RESULT_GROUP':'yeodam-b388-auth-result-'+run,
      'MIGRATION_AUTH_KEY_FILE':'/run/migration-auth/aes.hex',
      'MIGRATION_AUTH_REDIS_FILE':'/run/migration-auth/redis.json',
      'MIGRATION_TRIP_CONSUMER_ENABLED':'false','SPRING_FLYWAY_ENABLED':'false',
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
    else:
        mode(run,'direct');d='/run/yeodam-b388/'+run
        env['MIGRATION_TRIP_MODE_FILE']=d+'/mode';hc['Binds']=[d+':'+d+':ro']
    hc.setdefault('Binds',[]).append('/run/yeodam-b388/'+run+'/container-auth:/run/migration-auth:ro')
    payload={'Image':IMAGE,'Env':[k+'='+v for k,v in env.items()],'HostConfig':hc,'ExposedPorts':{'8080/tcp':{}},'Labels':{'yeodam.issue':'388','yeodam.run':run}}
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
    config=json.loads((Path('/run/yeodam-b388')/run/'auth.json').read_text())
    result=subprocess.run(['aws','secretsmanager','get-secret-value','--secret-id','yeodam-v2-staging/redis-auth-token','--region','ap-northeast-2','--output','json'],capture_output=True,text=True,check=True)
    redis=dict(config['redis']);redis['password']=json.loads(result.stdout)['SecretString']
    directory=Path('/run/yeodam-b388')/run/'container-auth';directory.mkdir(mode=0o750)
    shutil.copyfile('/run/yeodam-cdc-auth368/aes.hex',directory/'aes.hex')
    (directory/'redis.json').write_text(json.dumps(redis))
    for path in directory.iterdir():path.chmod(0o400);os.chown(path,1001,1001)
    os.chown(directory,1001,1001)
    print(json.dumps({'phase':'auth_private_files_prepared','runId':run}))

def remove(name,run):
    v=inspect(name)
    if v['Config'].get('Labels',{}).get('yeodam.issue')!='388' or v['Config']['Labels'].get('yeodam.run')!=run:raise RuntimeError('Container ownership differs')
    if v['State']['Running']:docker('POST','/containers/'+name+'/stop?t=30')
    docker('DELETE','/containers/'+name)
    print(json.dumps({'removed':name}))
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
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('auth-files','start-app','start-consumer','queue','direct','stop-app','stop-consumer','enable-consumer','restore-original','status','route-status','restore-route'))
    p.add_argument('--run-id',required=True);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id or '10.30.0.174' not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Unexpected host or UUID')
    if a.action=='auth-files':auth_files(a.run_id)
    elif a.action in ('route-status','restore-route'):status_route(a.run_id,a.action=='restore-route')
    elif a.action=='enable-consumer':
        path=Path('/run/yeodam-b388')/a.run_id/'container-auth'/'apply-enabled';path.touch(mode=0o444,exist_ok=False)
        print(json.dumps({'consumerApplyEnabled':True,'runId':a.run_id}))
    elif a.action.startswith('start-'):create(a.run_id,a.action=='start-consumer')
    elif a.action in ('queue','direct'):mode(a.run_id,a.action)
    elif a.action.startswith('stop-'):remove('yeodam-b388-'+a.action[5:],a.run_id)
    elif a.action=='restore-original':docker('POST','/containers/'+ORIGINAL+'/start');print('ORIGINAL_STARTED')
    else:
        for name in (ORIGINAL,'yeodam-b388-app','yeodam-b388-consumer'):
            try:
                s=inspect(name);print(json.dumps({'container':name,'status':s['State']['Status'],'health':s['State'].get('Health',{}).get('Status')}))
            except RuntimeError:print(json.dumps({'container':name,'status':'absent'}))
