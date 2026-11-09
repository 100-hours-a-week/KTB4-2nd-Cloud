#!/usr/bin/env python3
"""Extract photo references privately, inspect a bounded plan, then copy only approved keys."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

import v2_v3_mysql_logical as logical
import v2_v3_photo_migration as photos


def load_private(path):
    photos.require(path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o077==0,
                   'Private regular input file (0600) required')
    value=json.loads(path.read_text())
    photos.require(value.get('sensitivity')=='private-user-data','Actual photo operation requires a private backup binding')
    return value


def fresh_output(path):
    photos.require(not path.exists() and not path.is_symlink(),'New output file required')
    photos.require(path.parent.is_dir() and not path.parent.is_symlink()
        and path.parent.stat().st_mode & 0o077==0,'Private output directory (0700) required')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    extract=sub.add_parser('extract')
    extract.add_argument('--package',required=True,type=Path)
    extract.add_argument('--image',required=True)
    extract.add_argument('--runtime',required=True,type=Path)
    extract.add_argument('--output',required=True,type=Path)
    plan=sub.add_parser('plan')
    plan.add_argument('--references',required=True,type=Path)
    plan.add_argument('--output',required=True,type=Path)
    plan.add_argument('--profile',default='yeodam-admin')
    copy=sub.add_parser('copy')
    copy.add_argument('--plan',required=True,type=Path)
    copy.add_argument('--plan-sha256',required=True)
    copy.add_argument('--approved-count',required=True,type=int)
    copy.add_argument('--approved-bytes',required=True,type=int)
    copy.add_argument('--results',required=True,type=Path)
    copy.add_argument('--profile',default='yeodam-admin')
    args=parser.parse_args();os.umask(0o077)
    photos.require(not os.environ.get('GITHUB_ACTIONS'),'Actual photo CLI forbidden in CI')
    if args.command=='extract':
        fresh_output(args.output)
        manifest=logical.verify_package(args.package)
        photos.require(manifest['sensitivity']=='private-user-data'
            and manifest.get('sourceInstance')=='i-02b2db8886a13dc25','V2 private package required')
        spec=importlib.util.spec_from_file_location('v2_restore',photos.ROOT/'scripts/restore-v2-mysql-backup.py')
        restore=importlib.util.module_from_spec(spec);spec.loader.exec_module(restore)
        captured={}
        def inspect(container,source):captured.update(photos.extract(container,source))
        restore.restore_package(args.package,args.image,args.runtime,after_verify=inspect)
        logical.private_json(args.output,captured)
        print(json.dumps({'extractedKeys':len(captured['objects']),
            'excludedReferences':captured['excludedReferences'],'blockedReferences':captured['blockedReferences']}))
    else:
        output=args.output if args.command=='plan' else args.results
        fresh_output(output)
        value=load_private(args.references if args.command=='plan' else args.plan)
        import boto3
        from botocore.config import Config
        class Session:
            def __init__(self):self.session=boto3.Session(profile_name=args.profile,region_name='ap-northeast-2')
            def client(self,name,**kwargs):
                return self.session.client(name,config=Config(retries={'max_attempts':3,'mode':'standard'},
                    connect_timeout=10,read_timeout=60,max_pool_connections=8),**kwargs)
        store=photos.S3Store(Session())
        if args.command=='plan':
            result=photos.make_plan(value,store)
            logical.private_json(output,result)
            print(json.dumps({'planSha256':photos.digest(result),'ready':result['ready'],
                'copyCount':result['copyCount'],'copyBytes':result['copyBytes'],'counts':result['counts'],
                'blockedReferences':result['blockedReferences']}))
        else:
            result=photos.execute_plan(value,store,args.plan_sha256,args.approved_count,args.approved_bytes)
            logical.private_json(output,result)
            print(json.dumps({'completed':result['completed'],'counts':result['counts']}))
            return 0 if result['completed'] else 1
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        # SDK/DB errors can contain object keys or row values. Private plan/results hold only reviewed evidence.
        print('Photo migration stopped; no credentials, keys or rows printed. Inspect private evidence.',file=sys.stderr)
        sys.exit(1)
