#!/usr/bin/env python3
"""Read-only identification on the reviewed shared GPU. No service or data changes."""
import json
import os
from pathlib import Path
import subprocess
import urllib.request

GPU='i-05e1ad9dd96dba4ae'
ACCOUNT='483175530259'
REGION='ap-northeast-2'
IMAGE='qdrant/qdrant:v1.19.1@sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10'
CONTAINER='yeodam-ai-qdrant-production'
COLLECTION='vectors_qwen3-vl-embedding-2b'

def require(value):
    if not value:raise ValueError('Source identity/storage inspection refused')

def command(*args):
    r=subprocess.run(list(args),capture_output=True,text=True,timeout=30)
    require(r.returncode==0);return r.stdout

def inspect():
    require(os.geteuid()==0)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    token=opener.open(urllib.request.Request('http://169.254.169.254/latest/api/token',method='PUT',
        headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=3).read().decode()
    request=urllib.request.Request('http://169.254.169.254/latest/dynamic/instance-identity/document',headers={'X-aws-ec2-metadata-token':token})
    identity=json.loads(opener.open(request,timeout=3).read())
    require(identity.get('accountId')==ACCOUNT and identity.get('region')==REGION and identity.get('instanceId')==GPU)
    detail=json.loads(command('docker','inspect',CONTAINER))[0]
    require(detail['State']['Running'] and detail['Config']['Image']==IMAGE
        and detail['Config']['Labels'].get('com.docker.compose.project')=='yeodam-v2-production-qdrant')
    binding=detail['HostConfig']['PortBindings']
    require(binding=={'6333/tcp':[{'HostIp':'10.30.10.162','HostPort':'6335'}]})
    require(any(m['Source']=='/data/qdrant/production/storage' and m['Destination']=='/qdrant/storage'
        and m['RW'] for m in detail['Mounts']))
    # Existing deployment uses network isolation. If authentication was added, require a new reviewed auth path.
    auth_names={'QDRANT__SERVICE__API_KEY','QDRANT__SERVICE__READ_ONLY_API_KEY','QDRANT__SERVICE__JWT_RBAC'}
    for item in detail['Config'].get('Env',[]):
        key,_,value=item.partition('=');require(key not in auth_names or value in ['', 'false'])
        require(not (key in ['QDRANT__STORAGE__STORAGE_PATH','QDRANT__STORAGE__SNAPSHOTS_PATH'] and value))
    marker=Path('/opt/yeodam-production-qdrant/.initialized-instance').read_text().strip().split()
    require(len(marker)==2 and marker[0]==GPU and marker[1].startswith('vol-'))
    require(subprocess.run(['mountpoint','-q','/data/qdrant'],capture_output=True).returncode==0)
    device=command('findmnt','-n','-o','SOURCE','--target','/data/qdrant').strip()
    serial=command('lsblk','-dn','-o','SERIAL',device).strip().replace('-','').lower()
    require(serial==marker[1].replace('-','').lower())
    size=command('docker','exec',CONTAINER,'du','-sk','/qdrant/storage/collections/'+COLLECTION).split()[0]
    require(size.isdigit())
    # Snapshot path uses this container's actual filesystem, independent of storage-volume location.
    disk=command('docker','exec',CONTAINER,'/bin/sh','-ec',"if [ -d /qdrant/snapshots ]; then df -Pk /qdrant/snapshots; else df -Pk /qdrant; fi").strip().splitlines()[-1].split()
    require(len(disk)>=6 and disk[3].isdigit())
    ids=command('docker','ps','-q').split()
    containers=json.loads(command('docker','inspect',*ids)) if ids else []
    preserved={d['Id']:d['State']['StartedAt'] for d in containers if d['State']['Running']}
    return {'account':ACCOUNT,'region':REGION,'instanceId':GPU,'container':CONTAINER,'image':IMAGE,
        'host':'10.30.10.162','port':6335,'storageVolume':marker[1],
        'sourceContainerId':detail['Id'],'protectedContainers':preserved,
        'snapshotFreeBytes':int(disk[3])*1024,'collectionBytes':int(size)*1024,'readOnlyInspection':True}
if __name__=='__main__':
    try:print(json.dumps(inspect()))
    except Exception:raise SystemExit('Qdrant source proof failed; no environment or private values printed') from None
