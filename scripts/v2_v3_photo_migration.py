"""Photo reference planning and atomic create-only S3 copying. Never deletes objects."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlencode,urlsplit

from v2_v3_mysql_logical import require

ROOT=Path(__file__).resolve().parents[1]
ACCOUNT='483175530259'
SOURCE='yeodam-v1-app-data-483175530259-ap-northeast-2'
TARGET='yeodam-v3-foundation-app-data-483175530259-ap-northeast-2'
SQL_PATH=ROOT/'scripts/migration/photo-references.sql'
SQL_SHA256='f0fb8bf84056d9ec62d3f5f25e6b708615f40e6c47125085b8b16ce19c1bbd10'
PREFIXES=('trip-uploads/','trip-additions/')
EXCLUDED={'deleted','unfinished-upload','historical-upload'}
CATEGORIES=EXCLUDED|{'required','blocked-file-state'}
ORIGINS={'file.original','attachment.original','attachment.analyze','attachment.preview',
         'attachment.display','trip.thumbnail','place.thumbnail','initial-upload.original',
         'additional-upload.original','user.profile'}
MAX_COUNT=10000
MAX_BYTES=30_000_000_000
META_FIELDS=('ContentLength','ContentType','ContentEncoding','ContentLanguage','ContentDisposition',
             'CacheControl','Expires','WebsiteRedirectLocation','Metadata')


def now():return datetime.now(timezone.utc)

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def validate_key(key):
    require(isinstance(key,str) and len(key.encode())<=1024 and key.startswith(PREFIXES)
        and all(part not in ['','.','..'] for part in key.split('/'))
        and not any(ord(c)<32 or ord(c)==127 for c in key) and '\\' not in key and '%' not in key
        and '?' not in key and '#' not in key,'Unsupported photo key; explicit review required')
    return key


def reference_manifest(rows,backup_sha,sensitivity):
    require(re.fullmatch(r'[a-f0-9]{64}',backup_sha) and sensitivity in ['synthetic','private-user-data'],'Invalid backup binding')
    grouped={};excluded=Counter();blocked=Counter()
    for row in rows:
        require(set(row)=={'key','origin','category'} and row['origin'] in ORIGINS
                and row['category'] in CATEGORIES and isinstance(row['key'],str),'Unreviewed reference row')
        key=row['key'];category=row['category']
        if category in EXCLUDED:excluded[category]+=1;continue
        if key.startswith(('https://','http://')):
            parsed=urlsplit(key)
            source_url=bool(parsed.hostname and (parsed.hostname.startswith(SOURCE+'.') or (parsed.hostname.startswith('s3.') and parsed.path.startswith('/'+SOURCE+'/'))))
            if row['origin']=='user.profile' and parsed.scheme=='https' and parsed.hostname not in ['localhost','127.0.0.1','::1'] and not source_url:
                excluded['external-profile']+=1;continue
            blocked['absolute-url-requires-review']+=1;continue
        if category=='blocked-file-state':blocked[category]+=1;continue
        try:validate_key(key)
        except ValueError:blocked['unsupported-key']+=1;continue
        grouped.setdefault(key,set()).add(row['origin'])
    require(len(grouped)<=MAX_COUNT,'Photo count exceeds reviewed bound')
    return {'formatVersion':1,'backupSha256':backup_sha,'sqlSha256':SQL_SHA256,'sensitivity':sensitivity,
        'sourceBucket':SOURCE,'targetBucket':TARGET,'createdAt':now().isoformat(),
        'objects':[{'key':key,'origins':sorted(origins)} for key,origins in sorted(grouped.items())],
        'excludedReferences':dict(excluded),'blockedReferences':dict(blocked)}


def extract(container,manifest):
    import v2_v3_mysql_logical as logical
    logical.isolated(container,'target')
    require(logical.sql(container,'SELECT @@global.read_only AND @@global.super_read_only;')=='1','Read-only target required')
    query=SQL_PATH.read_bytes();require(hashlib.sha256(query).hexdigest()==SQL_SHA256,'Photo SQL changed')
    shell='export MYSQL_PWD="$(cat /credentials/app-password)"; exec mysql --no-defaults --protocol=SOCKET --socket=/tmp/mysql.sock -uyeodam --batch --raw --skip-column-names'
    raw=logical.docker('exec','-i',container,'/bin/bash','-ec',shell,source=query.decode())
    return reference_manifest([json.loads(line) for line in raw.splitlines()],manifest['sha256'],manifest['sensitivity'])


def plain(value):
    if isinstance(value,datetime):return value.isoformat()
    if isinstance(value,dict):return {k:plain(v) for k,v in value.items()}
    if isinstance(value,list):return [plain(v) for v in value]
    return value


def metadata(head):return plain({key:head.get(key) for key in META_FIELDS})


def integrity(head):
    require(head.get('ServerSideEncryption')=='AES256','Only source SSE-S3 objects supported')
    for name in ['CRC64NVME','SHA256','SHA1','CRC32C','CRC32']:
        if head.get('Checksum'+name) and (head.get('ChecksumType')=='FULL_OBJECT' or name=='CRC64NVME'):
            return {'method':'checksum-'+name,'value':head['Checksum'+name]}
    tag=head.get('ETag','').strip('"')
    require(re.fullmatch(r'[a-f0-9]{32}',tag),'No comparable full-object checksum; multipart ETag is not a digest')
    return {'method':'etag-md5-sse-s3-single-part','value':tag}


def equal(source,target):
    if metadata(source['head'])!=metadata(target['head']) or source['tags']!=target['tags']:return False
    try:return integrity(source['head'])==integrity(target['head'])
    except ValueError:return False


def supported(source):
    head=source['head']
    require(isinstance(head.get('VersionId'),str) and head['VersionId'],'Source version ID required')
    require(type(head.get('ContentLength')) is int and 0<head['ContentLength']<=5_000_000_000,'Unsupported photo size')
    require(head.get('StorageClass','STANDARD')=='STANDARD','Archive/nonstandard class needs review')
    require(not head.get('WebsiteRedirectLocation'),'Redirect object needs review')
    require(source['tags'].get('status')=='retained','Active photo is not retained; do not copy temporary lifecycle tags')
    integrity(head)


def validate_refs(refs):
    require(refs.get('formatVersion')==1 and refs.get('sourceBucket')==SOURCE
        and refs.get('targetBucket')==TARGET and refs.get('sqlSha256')==SQL_SHA256
        and refs.get('sensitivity') in ['synthetic','private-user-data']
        and re.fullmatch(r'[a-f0-9]{64}',refs.get('backupSha256','')),'Wrong photo reference binding')
    require(len(refs['objects'])<=MAX_COUNT,'Too many photo objects')
    keys=[validate_key(obj['key']) for obj in refs['objects']]
    require(len(keys)==len(set(keys)),'Duplicate photo keys')


def make_plan(refs,store):
    validate_refs(refs);store.assert_buckets()
    def inspect(obj):
        key=obj['key'];result={'key':key,'origins':obj['origins']}
        try:
            source=store.observe(SOURCE,key)
            if source is None:return {**result,'status':'missing-source'}
            supported(source)
            target=store.observe(TARGET,key)
            if target is not None:
                return {**result,'source':source,'target':target,
                    'status':'reuse' if equal(source,target) else 'target-conflict'}
            return {**result,'source':source,'status':'copy'}
        except ValueError as error:
            return {**result,'status':'inspection-failed','reason':str(error)}
        except Exception:
            return {**result,'status':'inspection-failed','reason':'S3 metadata unavailable'} # Keys/reasons retained privately, errors never printed.
    with ThreadPoolExecutor(max_workers=4) as executor:objects=list(executor.map(inspect,refs['objects']))
    counts=Counter(obj['status'] for obj in objects)
    total=sum(obj['source']['head']['ContentLength'] for obj in objects if obj['status']=='copy')
    ready=not refs['blockedReferences'] and not any(obj['status'] not in ['copy','reuse'] for obj in objects) and total<=MAX_BYTES
    return {'formatVersion':1,'createdAt':now().isoformat(),'sourceBucket':SOURCE,'targetBucket':TARGET,
        'backupSha256':refs['backupSha256'],'referenceSha256':digest(refs),'sqlSha256':SQL_SHA256,
        'sensitivity':refs['sensitivity'],'ready':ready,'copyCount':counts['copy'],'copyBytes':total,
        'counts':dict(counts),'excludedReferences':refs['excludedReferences'],
        'blockedReferences':refs['blockedReferences'],'objects':objects}


def execute_plan(plan,store,expected_sha,approved_count,approved_bytes):
    require(digest(plan)==expected_sha and plan['formatVersion']==1 and plan['ready'] is True,'Plan hash or readiness mismatch')
    require(plan['sourceBucket']==SOURCE and plan['targetBucket']==TARGET and plan['sqlSha256']==SQL_SHA256,'Wrong copy bindings')
    age=(now()-datetime.fromisoformat(plan['createdAt'])).total_seconds()
    require(0<=age<=3600,'Photo plan expired; inspect again')
    require(type(approved_count) is int and type(approved_bytes) is int
        and approved_count==plan['copyCount'] and approved_bytes==plan['copyBytes']
        and approved_count<=MAX_COUNT and approved_bytes<=MAX_BYTES,'Approved scope differs from plan')
    objects=plan['objects'];keys=[validate_key(obj['key']) for obj in objects]
    require(len(keys)==len(set(keys)) and len(keys)<=MAX_COUNT,'Duplicate/excess copy objects')
    require(all(obj['status'] in ['copy','reuse'] for obj in objects)
        and sum(obj['status']=='copy' for obj in objects)==approved_count
        and sum(obj['source']['head']['ContentLength'] for obj in objects if obj['status']=='copy')==approved_bytes,'Inconsistent copy totals')
    store.assert_buckets()
    # Recheck every source and target before any write. Native IfNoneMatch closes the race after this scan.
    def preflight(obj):
        supported(obj['source'])
        observed=store.observe(SOURCE,obj['key'],obj['source']['head']['VersionId'])
        require(observed==obj['source'],'Pinned source changed or disappeared')
        target=store.observe(TARGET,obj['key'])
        require(target is None or equal(obj['source'],target),'QA object conflict; no overwrite allowed')
        require(obj['status']=='copy' or target is not None,'Previously reused target disappeared; new plan required')
        return target
    with ThreadPoolExecutor(max_workers=4) as executor:current=list(executor.map(preflight,objects))
    def transfer(item):
        obj,target=item;key=obj['key']
        if target is not None:return {'key':key,'status':'reused','versionId':target['head']['VersionId']}
        try:
            response=store.copy_new(key,obj['source'])
            require(response.get('CopySourceVersionId')==obj['source']['head']['VersionId']
                and response.get('VersionId'),'Copy response has no pinned versions')
            version=response['VersionId'];copied=store.observe(TARGET,key,version)
            require(copied is not None and equal(obj['source'],copied),'Copy verification mismatch')
            latest=store.observe(TARGET,key)
            require(latest is not None and latest['head']['VersionId']==version,'QA changed target during verification')
            return {'key':key,'status':'copied','versionId':version}
        except Exception:
            return {'key':key,'status':'failed'} # No deletion or overwrite rollback; a new plan can reuse verified copies.
    with ThreadPoolExecutor(max_workers=4) as executor:results=list(executor.map(transfer,zip(objects,current)))
    counts=Counter(item['status'] for item in results)
    return {'formatVersion':1,'planSha256':expected_sha,'completed':counts['failed']==0,
        'counts':dict(counts),'results':results,'qaOverwritesAllowed':False,'sourceDeletesAllowed':False}


class S3Store:
    """SDK only: no shell keys, object downloads, IAM writes, deletes, sync or GPU/EC2 API."""
    def __init__(self,session):
        self.session=session
        self.client=session.client('s3',region_name='ap-northeast-2')
        require('IfNoneMatch' in self.client.meta.service_model.operation_model('CopyObject').input_shape.members,'SDK lacks destination conditional copy')
    def assert_buckets(self):
        require(self.session.client('sts').get_caller_identity()['Account']==ACCOUNT,'Wrong AWS account')
        for bucket in [SOURCE,TARGET]:
            require(self.client.get_bucket_location(Bucket=bucket,ExpectedBucketOwner=ACCOUNT)['LocationConstraint']=='ap-northeast-2','Wrong bucket Region')
            require(self.client.get_bucket_versioning(Bucket=bucket,ExpectedBucketOwner=ACCOUNT).get('Status')=='Enabled','Versioned buckets required')
    def observe(self,bucket,key,version=None):
        require(bucket in [SOURCE,TARGET],'Unexpected bucket');validate_key(key)
        args={'Bucket':bucket,'Key':key,'ExpectedBucketOwner':ACCOUNT}
        if version is not None:args['VersionId']=version
        try:head=self.client.head_object(**args,ChecksumMode='ENABLED')
        except Exception as error:
            code=getattr(error,'response',{}).get('Error',{}).get('Code')
            if code in ['404','NoSuchKey','NotFound']:return None
            raise RuntimeError('S3 metadata read failed; private details suppressed') from None
        require(head.get('VersionId'),'Object version ID missing')
        tags=self.client.get_object_tagging(Bucket=bucket,Key=key,VersionId=head['VersionId'],ExpectedBucketOwner=ACCOUNT)['TagSet']
        fields=set(META_FIELDS)|{'VersionId','ETag','ServerSideEncryption','StorageClass','ChecksumType',
            'ChecksumCRC64NVME','ChecksumSHA256','ChecksumSHA1','ChecksumCRC32C','ChecksumCRC32'}
        return {'head':plain({k:v for k,v in head.items() if k in fields}),'tags':{t['Key']:t['Value'] for t in tags}}
    def copy_new(self,key,source):
        validate_key(key);supported(source)
        copy_source={'Bucket':SOURCE,'Key':key,'VersionId':source['head']['VersionId']}
        return self.client.copy_object(Bucket=TARGET,Key=key,CopySource=copy_source,
            CopySourceIfMatch=source['head']['ETag'],IfNoneMatch='*',MetadataDirective='COPY',
            TaggingDirective='REPLACE',Tagging=urlencode(sorted(source['tags'].items())),
            StorageClass='STANDARD',ServerSideEncryption='AES256',
            ExpectedBucketOwner=ACCOUNT,ExpectedSourceBucketOwner=ACCOUNT)
