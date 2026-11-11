#!/usr/bin/env python3
"""Validate the fixed public reference locally; --upload explicitly creates its S3 object once."""
import argparse
import base64
import hashlib
import json
from pathlib import Path

BUCKET = 'yeodam-v1-app-data-483175530259-ap-northeast-2'
KEY = 'ai-reference/tourapi/v2026-11-11/tourapi_all.json'
ACCOUNT = '483175530259'
SIZE = 34351861
SHA = '9a32d53d7e7b55e8440b7dae953e3f344e17c55e2ca9fa9c86258bcf5b0d4229'

def verify(path):
    payload = path.read_bytes()
    if len(payload) != SIZE or hashlib.sha256(payload).hexdigest() != SHA:
        raise ValueError('TourAPI size or SHA-256 mismatch')
    data = json.loads(payload)
    if not isinstance(data, dict) or not any(isinstance(value, list) and value for value in data.values()):
        raise ValueError('TourAPI must contain a nonempty record list')
    return payload

def upload(session, payload):
    # Revalidate the exact bytes which will be sent, not only a prior pathname.
    if len(payload) != SIZE or hashlib.sha256(payload).hexdigest() != SHA:
        raise ValueError('TourAPI payload changed')
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise ValueError('Wrong AWS account')
    s3 = session.client('s3', region_name='ap-northeast-2')
    checksum = base64.b64encode(bytes.fromhex(SHA)).decode()
    # IfNoneMatch is the write guard; a prior HEAD alone would be racy.
    created = s3.put_object(Bucket=BUCKET, Key=KEY, Body=payload, IfNoneMatch='*',
                  ExpectedBucketOwner=ACCOUNT, ContentType='application/json',
                  ChecksumSHA256=checksum, Metadata={'sha256':SHA})
    version = {'VersionId':created['VersionId']} if created.get('VersionId') else {}
    head = s3.head_object(Bucket=BUCKET, Key=KEY, ExpectedBucketOwner=ACCOUNT, ChecksumMode='ENABLED', **version)
    if head['ContentLength'] != SIZE or head.get('ChecksumSHA256') != checksum:
        raise ValueError('Uploaded reference verification failed')
    return {'bytes':SIZE, 'sha256':SHA, 's3Uri':f's3://{BUCKET}/{KEY}',
            'versionId':head.get('VersionId'), 'uploaded':True}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--upload', action='store_true')
    parser.add_argument('--profile', default='yeodam-admin')
    args = parser.parse_args()
    payload = verify(args.file)
    if args.upload:
        import boto3
        result = upload(boto3.Session(profile_name=args.profile), payload)
    else:
        result = {'bytes':SIZE, 'sha256':SHA, 'uploaded':False}
    print(json.dumps(result))

if __name__ == '__main__':
    main()
