#!/usr/bin/env python3
"""Preserve both DBs and carry the completed Target data back for the next Run."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import time
from uuid import UUID

BUCKET='yeodam-v2-staging-mysql-backup-483175530259-ap-northeast-2'
SCHEMA='yeodam_v1_source'

def command(role,tool='mysql'):
    if role=='source':return [tool,'--protocol=socket','-uroot']
    return ['docker','exec','-i','yeodam-v2-mysql','sh','-ec','export MYSQL_PWD="$(cat /run/secrets/mysql-root-password)"; exec '+tool+' -uroot "$@"','sh']
def query(role,sql):
    p=subprocess.run(command(role)+['--batch','--skip-column-names'],input=sql,text=True,capture_output=True)
    if p.returncode:raise RuntimeError('Maintenance SQL failed; preserve both DBs')
    return p.stdout.strip()
def transfer(*args):
    p=subprocess.run(['aws','s3','cp',*args,'--region','ap-northeast-2','--only-show-errors'],capture_output=True)
    if p.returncode:raise RuntimeError('Preserved snapshot transfer failed')
def snapshot(role,path):
    with path.open('wb') as file, gzip.GzipFile(fileobj=file,mode='wb',mtime=0) as compressed:
        p=subprocess.Popen(command(role,'mysqldump')+['--single-transaction','--quick','--set-gtid-purged=OFF','--hex-blob','--routines','--events','--triggers','--default-character-set=utf8mb4',SCHEMA],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        for b in iter(lambda:p.stdout.read(65536),b''):compressed.write(b)
        error=p.stderr.read()
        if p.wait():raise RuntimeError('Maintenance dump failed; stderr bytes='+str(len(error)))
    path.chmod(0o600)
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(65536),b''):h.update(b)
    return h.hexdigest()
def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('target-snapshot','source-restore'));p.add_argument('--run-id',required=True);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id:raise ValueError('Canonical Run required')
    role='target' if a.action=='target-snapshot' else 'source'
    address='10.30.10.28' if role=='target' else '10.30.0.174'
    if address not in subprocess.check_output(['hostname','-I'],text=True).split():raise RuntimeError('Wrong maintenance host')
    directory=Path('/var/lib/yeodam/b388')/a.run_id;directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    prefix='migration/rehearsal-reset/'+a.run_id+'/b388-'
    if role=='target':
        if query(role,'SELECT @@server_id;')!='2':raise RuntimeError('Wrong Target ID')
        # Stop acknowledgements can precede MySQL socket teardown.
        end=time.monotonic()+15
        while query(role,"SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('root','event_scheduler','system user') AND ID!=CONNECTION_ID();")!='0':
            if time.monotonic()>=end:raise RuntimeError('Stop rehearsal apps and CDC clients before reset')
            time.sleep(.5)
        query(role,'SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')
        path=directory/'completed-target.sql.gz'
        if path.exists():raise RuntimeError('Target already preserved; inspect checkpoint')
        snapshot(role,path)
        value={'runId':a.run_id,'sha256':sha(path),'key':prefix+'target.sql.gz',
               'rows':int(query(role,'SELECT COUNT(*) FROM '+SCHEMA+'.trips;')),
               'maxTripId':int(query(role,'SELECT MAX(trip_id) FROM '+SCHEMA+'.trips;'))}
        marker=directory/'completed-target.ready';marker.write_text(json.dumps(value));marker.chmod(0o600)
        transfer(str(path),'s3://'+BUCKET+'/'+value['key']);transfer(str(marker),'s3://'+BUCKET+'/'+prefix+'target.ready')
        print(json.dumps({'phase':'target_preserved',**value}))
    else:
        if query(role,'SELECT @@server_id,@@read_only,@@super_read_only;')!='1\t1\t1':raise RuntimeError('Source must remain fenced before maintenance import')
        marker=directory/'completed-target.ready';path=directory/'completed-target.sql.gz'
        transfer('s3://'+BUCKET+'/'+prefix+'target.ready',str(marker));value=json.loads(marker.read_text())
        if value['runId']!=a.run_id or value['key']!=prefix+'target.sql.gz':raise RuntimeError('Wrong target snapshot')
        transfer('s3://'+BUCKET+'/'+value['key'],str(path))
        if sha(path)!=value['sha256']:raise RuntimeError('Target snapshot checksum mismatch')
        original=directory/'original-source.sql.gz';snapshot('source',original);transfer(str(original),'s3://'+BUCKET+'/'+prefix+'original-source.sql.gz')
        query(role,'SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
        try:
            with gzip.open(path,'rb') as content, (directory/'restore.stderr').open('wb') as error:
                proc=subprocess.Popen(command('source')+['--default-character-set=utf8mb4'],stdin=subprocess.PIPE,stderr=error,stdout=subprocess.DEVNULL)
                proc.stdin.write(('SET SESSION sql_log_bin=0; DROP DATABASE `'+SCHEMA+'`; CREATE DATABASE `'+SCHEMA+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; USE `'+SCHEMA+'`;\n').encode())
                for b in iter(lambda:content.read(65536),b''):proc.stdin.write(b)
                proc.stdin.close()
                if proc.wait():raise RuntimeError('Source restore failed; preserve private stderr')
            rows=int(query(role,'SELECT COUNT(*) FROM '+SCHEMA+'.trips;'));maximum=int(query(role,'SELECT MAX(trip_id) FROM '+SCHEMA+'.trips;'))
            if (rows,maximum)!=(value['rows'],value['maxTripId']):raise RuntimeError('Restored Source counts disagree')
        finally:query(role,'SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')
        print(json.dumps({'phase':'source_restored','runId':a.run_id,'rows':rows,'maxTripId':maximum,'sourceRemainsFenced':True}))
if __name__=='__main__':main()
