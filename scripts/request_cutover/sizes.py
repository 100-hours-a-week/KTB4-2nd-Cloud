"""Cloud-owned, conditional size backfill from actual S3 HEAD metadata."""
import argparse
import asyncio
import json
from .config import load
from .control import admin, database


def backfill(db, s3, bucket, apply=False):
    with db.cursor() as cursor:
        cursor.execute('''SELECT DISTINCT f.file_id,f.object_key FROM files f
            JOIN trip_attachments a ON a.file_id=f.file_id
            JOIN trips t ON t.trip_id=a.trip_id
            WHERE t.processing_status='COMPLETED' AND t.deleted_at IS NULL
              AND a.deleted_at IS NULL AND f.deleted_at IS NULL
              AND f.original_size_bytes IS NULL''')
        rows = cursor.fetchall()
    updates = 0
    for row in rows:
        size = s3.head_object(Bucket=bucket, Key=row['object_key'])['ContentLength']
        if type(size) is not int or size < 0:
            raise ValueError('invalid S3 ContentLength')
        if apply:
            with db.cursor() as cursor:
                cursor.execute('''UPDATE files SET original_size_bytes=%s
                    WHERE file_id=%s AND object_key=%s AND original_size_bytes IS NULL''',
                    (size, row['file_id'], row['object_key']))
                updates += cursor.rowcount
    return {'candidates': len(rows), 'updated': updates, 'source': 'actual_s3_head'}


def main():
    import boto3
    from botocore.config import Config
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--bucket', required=True)
    p.add_argument('--endpoint', default='http://s3:4566')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--confirm-run')
    args = p.parse_args()
    config = load(args.config)
    # This implementation is restricted to the disposable fixture. Production
    # bucket/IAM allowlisting is deliberately not inferred from a CLI string.
    if args.bucket != 'yeodam-cutover-fixture' or args.endpoint != 'http://s3:4566':
        raise ValueError('only the isolated S3 fixture is supported')
    state = asyncio.run(admin(config))
    if state['phase'] != 'V1' or (args.apply and args.confirm_run != config['run_id']):
        raise ValueError('V1 phase and explicit run confirmation required')
    s3 = boto3.client('s3', region_name='ap-northeast-2', endpoint_url=args.endpoint,
                      aws_access_key_id='isolated-fixture', aws_secret_access_key='isolated-fixture',
                      config=Config(connect_timeout=2, read_timeout=2, retries={'max_attempts': 0}))
    with database(config) as db:
        print(json.dumps(backfill(db, s3, args.bucket, args.apply)))


if __name__ == '__main__':
    main()
