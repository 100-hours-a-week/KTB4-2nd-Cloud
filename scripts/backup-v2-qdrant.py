#!/usr/bin/env python3
"""Reviewed V2 production Qdrant snapshot via an owned SSM tunnel; no service restarts."""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import subprocess
import time

import qdrant_migration as q

BUCKET='yeodam-v2-production-mysql-backup-483175530259-ap-northeast-2'
RUN=re.compile(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}')
PORT=18599
ROOT=Path(__file__).resolve().parents[1]


class Aws:
    def __init__(self,profile):self.prefix=['aws','--profile',profile,'--region',q.REGION]
    def __call__(self,*args):
        result=subprocess.run(self.prefix+list(map(str,args)),capture_output=True,text=True,timeout=180)
        q.require(result.returncode==0,'AWS operation failed; private details suppressed')
        return json.loads(result.stdout) if result.stdout.strip() else {}
    def preflight(self):
        q.require(self('sts','get-caller-identity')['Account']==q.ACCOUNT,'Wrong AWS account')
        instances=self('ec2','describe-instances','--instance-ids',q.GPU)['Reservations'][0]['Instances']
        q.require(len(instances)==1 and instances[0]['State']['Name']=='running'
            and instances[0]['InstanceType']=='g6.xlarge' and instances[0]['PrivateIpAddress']==q.HOST,
            'Reviewed shared GPU must already be running unchanged')
        rules=self('s3api','get-bucket-encryption','--bucket',BUCKET)['ServerSideEncryptionConfiguration']['Rules']
        q.require(any(r['ApplyServerSideEncryptionByDefault']['SSEAlgorithm']=='AES256' for r in rules),'Backup encryption mismatch')
        q.require(self('s3api','get-bucket-versioning','--bucket',BUCKET).get('Status')=='Enabled','Versioned backup bucket required')
        self.volume_ids={d['Ebs']['VolumeId'] for d in instances[0]['BlockDeviceMappings'] if d.get('Ebs')}
    def proof(self):
        script=(ROOT/'scripts/migration/qdrant-source-proof.py').read_bytes()
        encoded=base64.b64encode(script).decode()
        command="python3 - <<'YEODAM_PROOF'\nimport base64\nexec(compile(base64.b64decode('"+encoded+"'),'qdrant-source-proof.py','exec'))\nYEODAM_PROOF"
        sent=self('ssm','send-command','--document-name','AWS-RunShellScript','--instance-ids',q.GPU,
            '--timeout-seconds','120','--parameters',json.dumps({'commands':[command],'executionTimeout':['120']}),
            '--cloud-watch-output-config','CloudWatchOutputEnabled=false')
        command_id=sent['Command']['CommandId']
        for _ in range(45):
            result=self('ssm','list-command-invocations','--command-id',command_id,'--details')
            items=result['CommandInvocations']
            if not items:time.sleep(2);continue
            item=items[0]
            if item['Status']=='Success':
                output=item['CommandPlugins'][0]['Output']
                proof=json.loads(output)
                q.require(proof['storageVolume'] in self.volume_ids,'Qdrant data volume not attached to reviewed GPU')
                return proof
            q.require(item['Status'] in ['Pending','InProgress','Delayed'],'Read-only source proof failed')
            time.sleep(2)
        raise RuntimeError('Read-only source inspection timeout')
    def upload(self,path,key):
        digest=hashlib.md5()
        with path.open('rb') as data:
            for chunk in iter(lambda:data.read(1024*1024),b''):digest.update(chunk)
        response=self('s3api','put-object','--bucket',BUCKET,'--key',key,'--body',path,
            '--server-side-encryption','AES256','--if-none-match','*',
            '--content-md5',base64.b64encode(digest.digest()).decode(),
            '--metadata','sha256='+q.file_sha(path),'--expected-bucket-owner',q.ACCOUNT)
        q.require(response.get('VersionId'),'Snapshot backup version missing')
        head=self('s3api','head-object','--bucket',BUCKET,'--key',key,'--version-id',response['VersionId'],
            '--expected-bucket-owner',q.ACCOUNT)
        q.require(head['ContentLength']==path.stat().st_size and head.get('ServerSideEncryption')=='AES256'
            and head.get('Metadata',{}).get('sha256')==q.file_sha(path),'Snapshot upload verification failed')
        return response['VersionId']


def session_id(output):
    match=re.search(rb'Starting session with SessionId: ([A-Za-z0-9_.@+=,-]+)',output)
    return match[1].decode() if match else None


@contextmanager
def tunnel(aws):
    with socket.socket() as probe:probe.bind(('127.0.0.1',PORT)) # Fail before starting if another chat owns the port.
    params=json.dumps({'host':[q.HOST],'portNumber':['6335'],'localPortNumber':[str(PORT)]})
    process=subprocess.Popen(aws.prefix+['ssm','start-session','--target',q.GPU,'--document-name',
        'AWS-StartPortForwardingSessionToRemoteHost','--parameters',params],
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
    selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
    output=b'';owned=None
    try:
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            for key,_ in selector.select(timeout=.2):
                data=os.read(key.fileobj.fileno(),8192)
                if data:output=(output+data)[-32768:];owned=session_id(output) or owned
            q.require(process.poll() is None,'Owned SSM forwarding process exited')
            if owned and ('Port '+str(PORT)+' opened').encode() in output:
                try:
                    with socket.create_connection(('127.0.0.1',PORT),timeout=1):break
                except OSError:pass
        else:raise RuntimeError('Owned SSM tunnel did not become ready')
        yield owned
    finally:
        selector.close()
        try:
            if owned:aws('ssm','terminate-session','--session-id',owned)
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()


def backup(directory,run_id,profile):
    q.require(not os.environ.get('GITHUB_ACTIONS') and RUN.fullmatch(run_id),'Manual approved Qdrant backup only')
    q.require(not directory.exists() and not directory.is_symlink(),'New private operation directory required')
    directory.mkdir(mode=0o700)
    aws=Aws(profile);aws.preflight()
    proof=aws.proof();q.proof_valid(proof,'private-user-data');q.private_json(directory/'source-before.json',proof)
    with tunnel(aws) as owned:
        q.private_json(directory/'owned-session.json',{'sessionId':owned,'targetInstance':q.GPU,'remotePort':6335})
        manifest=q.backup(q.Client(PORT,source=True),directory/'package',proof,'private-user-data')
        after=aws.proof()
        q.require(after['sourceContainerId']==proof['sourceContainerId']
            and after['storageVolume']==proof['storageVolume']
            and all(after['protectedContainers'].get(k)==v for k,v in proof['protectedContainers'].items()),
            'Protected GPU containers changed; no automated recovery or restart')
        q.private_json(directory/'source-after.json',after)
    prefix='migration/v2-v3/qdrant/'+run_id+'/'
    versions={name:aws.upload(directory/'package'/name,prefix+name) for name in ['collection.snapshot','manifest.json']}
    ready={'formatVersion':1,'runId':run_id,'bucket':BUCKET,'prefix':prefix,'sourceInstance':q.GPU,
        'sourcePort':6335,'collection':q.COLLECTION,'files':{name:{'versionId':versions[name],
            'sha256':q.file_sha(directory/'package'/name)} for name in versions}}
    q.private_json(directory/'ready.json',ready);aws.upload(directory/'ready.json',prefix+'ready.json')
    summary={'backedUp':True,'runId':run_id,'snapshotBytes':manifest['snapshotBytes'],
        'pointCount':manifest['fingerprint']['count'],'sourceStableBeforeAfter':True,
        'gpuContainersPreserved':True,'ownedTunnelClosed':True,'sourceSnapshotRetained':True,
        'sourceCollectionModified':False,'qaAccessed':False}
    q.private_json(directory/'backup-result.json',summary)
    return summary

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-snapshot-backup',action='store_true',required=True)
    parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--run-id',required=True)
    parser.add_argument('--profile',default='yeodam-admin');args=parser.parse_args();os.umask(0o077)
    try:print(json.dumps(backup(args.directory,args.run_id,args.profile)))
    except Exception:raise SystemExit('Qdrant backup stopped; private data/keys/session output suppressed') from None
