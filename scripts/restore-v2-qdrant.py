#!/usr/bin/env python3
"""Restore only into a fresh, labelled local Docker runtime; never connects to V3."""
import argparse
import http.client
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid

import qdrant_migration as q
CLIENT_IMAGE='python:3.12-alpine@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7'
CLIENT_SCRIPT=Path(__file__).resolve().parent/'migration/qdrant-http-client.py'


def docker(*args,check=True,timeout=180):
    result=subprocess.run(['docker',*map(str,args)],capture_output=True,text=True,timeout=timeout)
    if check:q.require(result.returncode==0,'Isolated Qdrant Docker operation failed; private logs suppressed')
    return result


class DockerClient(q.Client):
    def __init__(self,runtime,source=False,authenticated=True):
        super().__init__(6333,source=source)
        self.runtime=runtime;self.authenticated=authenticated
    def args(self,name):
        return ['docker','run','--rm','--platform','linux/amd64','--name',name,
            '--label','yeodam.issue=599','--label','yeodam.qdrant.role=client',
            '--network',self.runtime.network,'--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--user','65532:65532',
            '--memory','64m','--cpus','.25','-i',
            '-v',f'{self.runtime.directory}/config:/config:ro','-v',f'{CLIENT_SCRIPT}:/client.py:ro',
            CLIENT_IMAGE,'python','/client.py']
    def request(self,method,path,body=None):
        self.allowed(method,path)
        name=self.runtime.name+'-client-'+uuid.uuid4().hex[:8]
        try:
            result=subprocess.run(self.args(name),input=q.canonical({'method':method,'path':path,
                'body':body,'authenticated':self.authenticated}),capture_output=True,timeout=210)
            q.require(result.returncode==0,'Isolated HTTP client failed; private output suppressed')
            response=json.loads(result.stdout);q.require(response['httpStatus']==200,'Qdrant request rejected')
            value=response['value'];q.require(value.get('status')=='ok' or (path=='/' and value.get('version')),'Qdrant operation failed')
            return value if path=='/' else value.get('result')
        finally:docker('rm','-f',name,check=False)
    def download(self,snapshot,path,expected_size,expected_sha):
        endpoint=q.BASE+'/snapshots/'+q.snapshot_name(snapshot);self.allowed('GET',endpoint)
        q.require(type(expected_size) is int and 0<expected_size<=q.MAX_SNAPSHOT,'Snapshot size exceeds bound')
        q.require(isinstance(expected_sha,str) and len(expected_sha)==64,'Server checksum missing')
        name=self.runtime.name+'-client-'+uuid.uuid4().hex[:8];process=None
        try:
            with tempfile.TemporaryFile() as error:
                process=subprocess.Popen(self.args(name),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=error)
                process.stdin.write(q.canonical({'method':'GET','path':endpoint,'download':True,'authenticated':True}));process.stdin.close()
                total=0;checksum=hashlib.sha256()
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as output:
                    for chunk in iter(lambda:process.stdout.read(1024*1024),b''):
                        total+=len(chunk);q.require(total<=expected_size,'Oversized snapshot');checksum.update(chunk);output.write(chunk)
                q.require(process.wait(timeout=210)==0 and total==expected_size and checksum.hexdigest()==expected_sha,'Snapshot integrity mismatch')
        finally:
            if process and process.poll() is None:process.kill();process.wait()
            docker('rm','-f',name,check=False)


class Runtime:
    def __init__(self,directory,role='target'):
        q.require(role in ['target','synthetic-source'],'Invalid isolated role')
        self.directory=directory;self.role=role;self.name='qdrant-restore-599-'+uuid.uuid4().hex[:10]
        self.network=self.name+'-net';self.network_attempted=False;self.container_attempted=False
    def start(self,snapshot=None):
        q.require(not self.directory.exists() and not self.directory.is_symlink(),'Fresh runtime directory required')
        self.directory.mkdir(mode=0o700)
        config=self.directory/'config';config.mkdir();config.chmod(0o755)
        self.volume=self.directory/'volume';self.volume.mkdir();self.volume.chmod(0o777)
        # CLI recovery runs before the service initializes its collection parent directory.
        for child in ['storage','storage/collections','snapshots','tmp']:
            directory=self.volume/child;directory.mkdir();directory.chmod(0o777)
        key=secrets.token_hex(32);self.key=key
        command=['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost',
            '-keyout',str(config/'tls.key'),'-out',str(config/'tls.crt'),
            '-addext','subjectAltName=IP:127.0.0.1,DNS:localhost,DNS:qdrant-599.internal']
        result=subprocess.run(command,capture_output=True,timeout=30)
        q.require(result.returncode==0,'Fixture certificate creation failed')
        settings=f'''log_level: WARN
telemetry_disabled: true
storage:
  storage_path: /data/storage
  snapshots_path: /data/snapshots
  temp_path: /data/tmp
  on_disk_payload: true
  wal:
    wal_capacity_mb: 8
    wal_segments_ahead: 0
  performance:
    max_search_threads: 1
    optimizer_cpu_budget: 1
  optimizers:
    default_segment_number: 1
    max_optimization_threads: 1
service:
  host: 0.0.0.0
  http_port: 6333
  grpc_port: null
  enable_tls: true
  enable_cors: false
  max_workers: 1
  api_key: {key}
tls:
  cert: /config/tls.crt
  key: /config/tls.key
cluster:
  enabled: false
'''
        (config/'qdrant.yaml').write_text(settings)
        (config/'api-key').write_text(key)
        for file in config.iterdir():file.chmod(0o444) # Fresh fixture key/cert; parent runtime is 0700.
        args=['run','-d','--platform','linux/amd64','--name',self.name,'--label','yeodam.issue=599',
            '--label','yeodam.qdrant.role='+self.role,'--network',self.network,'--network-alias','qdrant-599.internal',
            '--memory','512m','--cpus','.5','--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--user','1000:1000','--tmpfs','/tmp:rw,size=32m',
            '-v',f'{config}:/config:ro','-v',f'{self.volume}:/data']
        if snapshot:
            imported=self.directory/'import';imported.mkdir();imported.chmod(0o755)
            shutil.copyfile(snapshot,imported/'collection.snapshot');(imported/'collection.snapshot').chmod(0o444)
            args+=['-v',f'{imported}:/backup:ro']
        args+=['--entrypoint','/qdrant/qdrant',q.IMAGE,'--config-path','/config/qdrant.yaml']
        if snapshot:args+=['--snapshot','/backup/collection.snapshot:'+q.COLLECTION]
        self.network_attempted=True;docker('network','create','--internal','--label','yeodam.issue=599',self.network)
        self.container_attempted=True;docker(*args)
        detail=json.loads(docker('inspect',self.name).stdout)[0]
        q.require(not detail['HostConfig'].get('PortBindings'),'Published ports forbidden')
        self.client=DockerClient(self)
        for _ in range(90):
            detail=json.loads(docker('inspect',self.name).stdout)[0]
            q.require(detail['State']['Running'],'Isolated Qdrant stopped; data not printed')
            try:
                if self.client.request('GET','/').get('version')==q.VERSION:
                    if not snapshot or self.client.request('GET',q.BASE).get('status')=='green':break
            except (OSError,ValueError,http.client.HTTPException):pass
            time.sleep(2)
        else:raise RuntimeError('Isolated Qdrant startup timed out')
        self.assert_isolated()
        anonymous=DockerClient(self,authenticated=False)
        try:anonymous.request('GET',q.BASE);raise AssertionError('Unauthenticated fixture access accepted')
        except ValueError:pass
        return self.client
    def assert_isolated(self):
        detail=json.loads(docker('inspect',self.name).stdout)[0]
        q.require(detail['Config']['Image']==q.IMAGE and detail['Config']['Labels'].get('yeodam.issue')=='599'
            and detail['Config']['Labels'].get('yeodam.qdrant.role')==self.role
            and detail['HostConfig']['ReadonlyRootfs'] and not detail['HostConfig'].get('Privileged')
            and detail['HostConfig']['Memory']==512*1024*1024
            and detail['HostConfig']['NanoCpus']==500000000
            and not detail['HostConfig'].get('PortBindings')
            and set(detail['NetworkSettings']['Networks'])=={self.network},'Unreviewed runtime identity/resources')
        network=json.loads(docker('network','inspect',self.network).stdout)[0]
        q.require(network['Internal'] and network['Labels'].get('yeodam.issue')=='599','External runtime network forbidden')
        binds={m['Destination']:m for m in detail['Mounts']}
        q.require(binds['/data']['Source']==str(self.volume) and binds['/data']['RW']
            and not binds['/config']['RW'],'Wrong runtime mount')
    def cleanup(self):
        errors=[]
        if self.container_attempted:
            result=docker('rm','-f',self.name,check=False)
            if result.returncode and 'No such container' not in result.stderr:errors.append('container')
        if self.network_attempted:
            result=docker('network','rm',self.network,check=False)
            if result.returncode and 'not found' not in result.stderr.lower() and 'No such network' not in result.stderr:errors.append('network')
        if not errors and getattr(self,'volume',None) and self.volume.exists():
            result=docker('run','--rm','--platform','linux/amd64','--network','none','--read-only',
                '--cap-drop','ALL','--user','1000:1000','--memory','64m','--cpus','.25',
                '-v',f'{self.volume}:/cleanup','--entrypoint','/bin/sh',q.IMAGE,'-ec',
                'rm -rf /cleanup/* /cleanup/.[!.]* /cleanup/..?*',check=False)
            if result.returncode:errors.append('fixture-data')
        q.require(not errors,'Runtime cleanup incomplete; inspect only the named private runtime')
        if self.directory.exists():shutil.rmtree(self.directory)


def restore(package,directory):
    manifest=q.verify_package(package)
    q.require(shutil.disk_usage(directory.parent).free>=max(128*1024*1024,manifest['snapshotBytes']*3),
        'Insufficient local snapshot/import/restore space')
    runtime=Runtime(directory)
    try:
        client=runtime.start(package/'collection.snapshot')
        q.require(client.request('GET',q.BASE+'/aliases')['aliases']==[],'Fresh restored target unexpectedly has aliases')
        aliases=manifest['fingerprint']['aliases']
        if aliases:
            client.request('POST','/collections/aliases',{'actions':[{'create_alias':a} for a in aliases]})
        actual=q.fingerprint(client)
        q.require(actual==manifest['fingerprint'],'Restored IDs/vectors/payload/config/aliases differ')
        for alias in aliases:
            value=client.request('POST','/collections/'+alias['alias_name']+'/points/count',{'exact':True})
            q.require(value['count']==actual['count'],'Alias does not resolve to restored collection')
        return {'restored':True,'version':q.VERSION,'collection':q.COLLECTION,'pointCount':actual['count'],
            'idsMatch':True,'vectorsMatch':True,'payloadsMatch':True,'configMatch':True,'aliasesMatch':True,
            'tlsAndApiKeyVerified':True,'qaAccessed':False,'memoryMi':512,'cpu':0.5}
    finally:runtime.cleanup()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',required=True,type=Path)
    p.add_argument('--runtime',required=True,type=Path);p.add_argument('--result',required=True,type=Path)
    a=p.parse_args();os.umask(0o077)
    try:
        q.require(not os.environ.get('GITHUB_ACTIONS'),'Actual Qdrant restore CLI forbidden in CI')
        q.require(not a.result.exists() and not a.result.is_symlink(),'New private result file required')
        value=restore(a.package,a.runtime);q.private_json(a.result,{**value,'runtimeCleaned':True})
        print(json.dumps({'restored':True,'pointCount':value['pointCount'],'runtimeCleaned':True}))
    except Exception:raise SystemExit('Qdrant restore failed; private values/logs suppressed') from None
