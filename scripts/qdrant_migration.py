"""Bounded collection snapshots. No source collection writes/deletes or V3 endpoints."""
from datetime import datetime,timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import uuid

ACCOUNT='483175530259'
REGION='ap-northeast-2'
GPU='i-05e1ad9dd96dba4ae'
HOST='10.30.10.162'
CONTAINER='yeodam-ai-qdrant-production'
COLLECTION='vectors_qwen3-vl-embedding-2b'
VERSION='1.19.1'
IMAGE='qdrant/qdrant:v1.19.1@sha256:12364fe851b9f17356fc88189fc06d1b521262e04659ec7345975b00c9246a10'
MAX_SNAPSHOT=512*1024*1024
MAX_POINTS=10000
JSON_LIMIT=16*1024*1024
BASE='/collections/'+COLLECTION
SNAPSHOT=re.compile(r'[A-Za-z0-9_.-]{1,180}\.snapshot')


def require(value,message):
    if not value:raise ValueError(message)


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()

def file_sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def private_json(path,value):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
        json.dump(value,f,indent=2,ensure_ascii=False,allow_nan=False)


def snapshot_name(name):
    require(isinstance(name,str) and SNAPSHOT.fullmatch(name) and '..' not in name,'Unsafe snapshot name')
    return name


class Client:
    def __init__(self,port,*,source=False,key=None,ca=None):
        require(type(port) is int and 1024<=port<=65535,'Invalid local Qdrant port')
        self.port=port;self.source=source;self.key=key;self.context=ssl.create_default_context(cafile=str(ca)) if ca else None
    def connection(self):
        if self.context:return http.client.HTTPSConnection('127.0.0.1',self.port,context=self.context,timeout=180)
        return http.client.HTTPConnection('127.0.0.1',self.port,timeout=180)
    def allowed(self,method,path):
        require(path.startswith('/') and '\r' not in path and '\n' not in path and '..' not in path and '%' not in path,'Unsafe API path')
        if not self.source:return
        reads={'/','/cluster',BASE,BASE+'/aliases'}
        allowed=method=='GET' and (path in reads or (path.startswith(BASE+'/snapshots/') and path==BASE+'/snapshots/'+snapshot_name(path[len(BASE+'/snapshots/'):])))
        allowed=allowed or (method=='POST' and path in [BASE+'/points/count',BASE+'/points/scroll',BASE+'/snapshots?wait=true'])
        require(allowed,'Source collection mutation/other endpoint forbidden')
    def request(self,method,path,body=None):
        self.allowed(method,path);connection=self.connection()
        headers={'Content-Type':'application/json'}
        if self.key:headers['api-key']=self.key
        try:
            connection.request(method,path,body=canonical(body) if body is not None else None,headers=headers)
            response=connection.getresponse();data=response.read(JSON_LIMIT+1)
            require(response.status==200 and len(data)<=JSON_LIMIT,'Qdrant request failed; private response suppressed')
            value=json.loads(data)
            require(value.get('status')=='ok' or (path=='/' and value.get('version')),'Qdrant operation not successful')
            return value if path=='/' else value.get('result')
        finally:connection.close()
    def download(self,name,path,expected_size,expected_sha):
        endpoint=BASE+'/snapshots/'+snapshot_name(name);self.allowed('GET',endpoint)
        require(type(expected_size) is int and 0<expected_size<=MAX_SNAPSHOT,'Snapshot size exceeds reviewed bound')
        require(isinstance(expected_sha,str) and re.fullmatch(r'[a-f0-9]{64}',expected_sha),'Server snapshot SHA256 required')
        connection=self.connection();total=0;h=hashlib.sha256()
        try:
            headers={'api-key':self.key} if self.key else {}
            connection.request('GET',endpoint,headers=headers);response=connection.getresponse()
            require(response.status==200,'Snapshot download failed')
            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as f:
                while chunk:=response.read(1024*1024):
                    total+=len(chunk);require(total<=expected_size,'Snapshot exceeded declared size');h.update(chunk);f.write(chunk)
            require(total==expected_size and h.hexdigest()==expected_sha,'Snapshot checksum/size mismatch')
        finally:connection.close()


def point_id(value):
    if type(value) is int:
        require(0<=value<2**64,'Invalid point integer ID');return 'i:'+str(value)
    require(isinstance(value,str),'Invalid point ID')
    return 'u:'+str(uuid.UUID(value))


def fingerprint(client,allow_empty=False):
    require(client.request('GET','/').get('version')==VERSION,'Qdrant version differs from fixed snapshot engine')
    require(client.request('GET','/cluster').get('status')=='disabled','Single-node source/target required')
    info=client.request('GET',BASE);config=info['config']
    vectors=config['params']['vectors']
    fields=[vectors] if 'size' in vectors else list(vectors.values())
    require(len(fields)==1 and fields[0].get('size')==2048 and fields[0].get('distance')=='Cosine','Expected one 2048/Cosine vector configuration')
    aliases=client.request('GET',BASE+'/aliases')['aliases']
    for alias in aliases:
        require(set(alias)=={'alias_name','collection_name'} and alias['collection_name']==COLLECTION
            and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',alias['alias_name']) and alias['alias_name']!=COLLECTION,'Invalid alias binding')
    aliases=sorted(aliases,key=lambda a:a['alias_name'])
    count=client.request('POST',BASE+'/points/count',{'exact':True})['count']
    require(type(count) is int and (0 if allow_empty else 1)<=count<=MAX_POINTS,'Unexpected point count')
    rows={};offset=None;seen=set()
    for _ in range((MAX_POINTS//64)+2):
        args={'limit':64,'with_payload':True,'with_vector':True}
        if offset is not None:args['offset']=offset
        page=client.request('POST',BASE+'/points/scroll',args)
        for point in page['points']:
            identifier=point_id(point['id']);require(identifier not in rows,'Duplicate point ID')
            rows[identifier]={'payload':digest(point.get('payload')),'vector':digest(point.get('vector'))}
        require(len(rows)<=MAX_POINTS,'Too many points')
        offset=page.get('next_page_offset')
        if offset is None:break
        token=point_id(offset);require(token not in seen,'Repeated scroll offset');seen.add(token)
    else:raise ValueError('Qdrant scroll did not terminate')
    require(len(rows)==count,'Point count changed during scan')
    return {'count':count,'idsSha256':digest(sorted(rows)),
        'vectorsSha256':digest({k:v['vector'] for k,v in sorted(rows.items())}),
        'payloadsSha256':digest({k:v['payload'] for k,v in sorted(rows.items())}),
        'config':config,'payloadSchema':info.get('payload_schema',{}),'aliases':aliases}


def proof_valid(proof,sensitivity):
    require(proof['account']==ACCOUNT and proof['region']==REGION and proof['instanceId']==GPU
        and proof['container']==CONTAINER and proof['image']==IMAGE and proof['host']==HOST
        and proof['port']==6335 and proof['readOnlyInspection'] is True,'Wrong production Qdrant source')
    require(proof['snapshotFreeBytes']>=max(128*1024*1024,proof['collectionBytes']*3),'Insufficient source snapshot disk headroom')
    require(sensitivity in ['synthetic','private-user-data'],'Unexpected package sensitivity')
    require(not os.environ.get('GITHUB_ACTIONS') or sensitivity=='synthetic','Private Qdrant data forbidden in CI')


def backup(client,directory,proof,sensitivity):
    proof_valid(proof,sensitivity)
    require(client.source,'Restricted source client required')
    require(not directory.exists() and not directory.is_symlink(),'New private package directory required')
    before=fingerprint(client)
    directory.mkdir(mode=0o700)
    snapshot=client.request('POST',BASE+'/snapshots?wait=true')
    name=snapshot_name(snapshot['name']);size=snapshot['size'];sha=snapshot.get('checksum')
    client.download(name,directory/'collection.snapshot',size,sha)
    after=fingerprint(client)
    require(before==after,'Source changed during snapshot; preserve files and retry at a reviewed time')
    manifest={'formatVersion':1,'sensitivity':sensitivity,'source':proof,'collection':COLLECTION,
        'version':VERSION,'image':IMAGE,'createdAt':datetime.now(timezone.utc).isoformat(),
        'snapshotName':name,'snapshotBytes':size,'snapshotSha256':sha,'fingerprint':before,
        'sourceStableBeforeAfter':True,'aliasesRecordedSeparately':True}
    private_json(directory/'manifest.json',manifest)
    verify_package(directory)
    return manifest


def verify_package(directory):
    require(directory.is_dir() and not directory.is_symlink(),'Invalid package path')
    require({p.name for p in directory.iterdir()}=={'manifest.json','collection.snapshot'},'Exact snapshot package files required')
    require(all(p.is_file() and not p.is_symlink() for p in directory.iterdir()),'Package symlinks rejected')
    require((directory/'manifest.json').stat().st_size<=1024*1024,'Oversized snapshot manifest')
    manifest=json.loads((directory/'manifest.json').read_text())
    require(manifest['formatVersion']==1 and manifest['collection']==COLLECTION and manifest['version']==VERSION
        and manifest['image']==IMAGE and manifest['sourceStableBeforeAfter'] is True
        and manifest['aliasesRecordedSeparately'] is True,'Unreviewed snapshot package binding')
    proof_valid(manifest['source'],manifest['sensitivity']);snapshot_name(manifest['snapshotName'])
    require(type(manifest['snapshotBytes']) is int and 0<manifest['snapshotBytes']<=MAX_SNAPSHOT
        and (directory/'collection.snapshot').stat().st_size==manifest['snapshotBytes']
        and file_sha(directory/'collection.snapshot')==manifest['snapshotSha256'],'Snapshot integrity mismatch')
    require(0<manifest['fingerprint']['count']<=MAX_POINTS,'Invalid package point count')
    return manifest
