#!/usr/bin/env python3
"""Run as root on the staging Kafka host. Never prints credentials or connector config."""
import base64
import hashlib
import shutil
import tempfile
import zipfile
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request
import urllib.error

BASE = 'http://127.0.0.1:8083'
NAME = 'yeodam-staging-outbox'

def api(path, value=None, method=None):
    data = None if value is None else json.dumps(value).encode()
    request = urllib.request.Request(BASE+path, data=data, method=method,
        headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=30) as response:
        return json.load(response)

def prerequisites():
    if not shutil.which('aws'):
        with tempfile.TemporaryDirectory(prefix='cdc-aws-') as temp:
            archive=Path(temp)/'aws.zip'
            urllib.request.urlretrieve('https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip',archive)
            with zipfile.ZipFile(archive) as zipped:zipped.extractall(temp)
            for relative in ['aws/install','aws/dist/aws','aws/dist/aws_completer']:
                (Path(temp)/relative).chmod(0o755)
            subprocess.run([str(Path(temp)/'aws/install'),'--update'],check=True)
    libraries=Path('/opt/yeodam-kafka/cdc-libs');libraries.mkdir(exist_ok=True)
    artifacts={
        'groovy':'d77815c8f31d503bc50129f28892e60b64e339b490f7babf9df70848ce465698',
        'groovy-jsr223':'73b24efc770f471d11fb33afde0105765467b21f62db89336259b593c79de745'}
    changed=False
    for artifact,digest in artifacts.items():
        name=artifact+'-4.0.32.jar';path=libraries/name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            data=urllib.request.urlopen('https://repo.maven.apache.org/maven2/org/apache/groovy/'+artifact+'/4.0.32/'+name,timeout=30).read()
            assert hashlib.sha256(data).hexdigest()==digest,'Groovy checksum mismatch'
            path.write_bytes(data);path.chmod(0o644)
            changed=True
    print('CDC_PREREQUISITES_READY',flush=True)
    return changed

def main():
    assert os.geteuid() == 0
    libraries_changed=prerequisites()
    config=json.loads(base64.b64decode(os.environ['CDC_CONNECTOR_B64']))
    assert config['snapshot.mode']=='no_data' and config['database.hostname']=='mysql-validation.staging.yeodam.internal'
    result=subprocess.run(['aws','ssm','get-parameter','--region','ap-northeast-2','--name',
        '/yeodam/v2/staging/kafka/MYSQL_CDC_PASSWORD','--with-decryption'],capture_output=True,text=True,check=True)
    password=json.loads(result.stdout)['Parameter']['Value']
    assert re.fullmatch('[0-9a-f]{32}',password)
    directory=Path('/etc/yeodam/kafka/cdc');directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    # The pinned Connect image runs as UID/GID 1001. Root can still maintain this host directory.
    os.chown(directory,1001,1001);directory.chmod(0o500)
    credential=directory/'mysql.properties'
    fd=os.open(credential,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o400)
    with os.fdopen(fd,'w') as stream:stream.write('password='+password+'\n')
    os.chown(credential,1001,1001);credential.chmod(0o400)
    compose=Path('/opt/yeodam-kafka/compose.yml')
    compose.write_bytes(base64.b64decode(os.environ['CDC_COMPOSE_B64']))
    command=['docker','compose','--env-file','/etc/yeodam/kafka/runtime.env','-f',str(compose)]
    subprocess.run(command+['config','--quiet'],check=True)
    # Only Connect changes. Never restart or reformat the broker.
    recreate=['--force-recreate'] if libraries_changed else []
    subprocess.run(command+['up','-d','--no-deps']+recreate+['connect'],check=True)
    for attempt in range(60):
        try:api('/connectors');break
        except Exception:time.sleep(2)
    else:raise RuntimeError('Connect REST did not become ready')
    validated=api('/connector-plugins/io.debezium.connector.mysql.MySqlConnector/config/validate',config,'PUT')
    if validated['error_count']:
        for entry in validated.get('configs',[]):
            errors=entry.get('value',{}).get('errors',[])
            if errors:print('INVALID_CONFIG_FIELD '+entry['definition']['name'],flush=True)
        raise RuntimeError('Connector config validation failed')
    # PUT keeps the same source identity and offsets on repeat; never delete offsets here.
    api('/connectors/'+NAME+'/config',config,'PUT')
    for attempt in range(60):
        try:
            status=api('/connectors/'+NAME+'/status')
        except urllib.error.HTTPError as exc:
            if exc.code not in (404,409,503):raise
            time.sleep(2)
            continue
        states=[status['connector']['state']]+[t['state'] for t in status['tasks']]
        if len(states)==2 and all(s=='RUNNING' for s in states):
            print('OUTBOX_CONNECTOR RUNNING task_count=1',flush=True);return
        if 'FAILED' in states:raise RuntimeError('Connector task failed; inspect status without exposing credentials')
        time.sleep(2)
    raise RuntimeError('Connector did not become RUNNING')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('OUTBOX_CONNECTOR_FAILED type='+type(exc).__name__,flush=True)
        raise SystemExit(1)
