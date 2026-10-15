#!/usr/bin/env python3
"""MySQL-side isolated fixture; print manifest, never credentials. Kafka side compares exact payloads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

def sql(query):
    result=subprocess.run(['docker','exec','-i','yeodam-v2-mysql','sh','-ec',
        'export MYSQL_PWD=$(cat /run/secrets/mysql-root-password); exec mysql -uroot yeodam_validation -N -B'],
        input=query,text=True,capture_output=True)
    if result.returncode:raise RuntimeError(result.stderr)
    return result.stdout.strip()

def literal(value):
    return 'CONVERT(0x'+str(value).encode().hex()+' USING utf8mb4)'

def main():
    assert os.geteuid()==0
    action=sys.argv[1];run=sys.argv[2];assert str(uuid.UUID(run))==run
    file=Path('/run/yeodam-cdc-probe-'+run+'.json')
    if action=='create':
        assert not file.exists()
        v={'run':run,'email':'cloud-cdc-'+run+'@example.invalid','topic':'staging.yeodam.cloud-cdc-smoke'}
        for name in ['execution','story_execution','upload','positive','negative','rollback']:v[name]=str(uuid.uuid4())
        v['payloads']={source:json.dumps({'run_id':run,'source':source,'message':'cloud-only-229'}) for source in ['trip_creation_outbox','additional_attachment_upload_outbox','story_generation_outbox','negative','rollback']}
        values={
            'probe_run':run,'probe_email':v['email'],'probe_topic':v['topic'],
            **{'probe_'+k:v[k] for k in ['execution','story_execution','upload','positive','negative','rollback']},
            'probe_trip_payload':v['payloads']['trip_creation_outbox'],
            'probe_update_payload':v['payloads']['additional_attachment_upload_outbox'],
            'probe_story_payload':v['payloads']['story_generation_outbox'],
            'probe_negative_payload':v['payloads']['negative'],'probe_rollback_payload':v['payloads']['rollback']}
        query=''.join('SET @'+k+'='+literal(value)+';\n' for k,value in values.items())
        v.update(json.loads(sql(query+Path('/run/verify-v2-staging-outbox.sql').read_text())))
        file.write_text(json.dumps(v));file.chmod(0o600);print(json.dumps(v))
    elif action=='cleanup':
        v=json.loads(file.read_text());assert v['run']==run and v['email']=='cloud-cdc-'+run+'@example.invalid'
        query='START TRANSACTION;'
        for table in ['trip_creation_outbox','additional_attachment_upload_outbox','story_generation_outbox']:
            query+='DELETE FROM '+table+' WHERE request_id='+literal(run)+';'
        for table,col,key in [('additional_attachment_upload_batches','upload_batch_id','batch'),('story_generation_jobs','generation_id','story'),('trip_creation_executions','execution_id','execution'),('trips','trip_id','trip'),('users','user_id','user')]:
            query+='DELETE FROM '+table+' WHERE '+col+'='+literal(v[key])+';'
        query+='COMMIT; SELECT COUNT(*) FROM users WHERE CAST(email AS BINARY)=CAST('+literal(v['email'])+' AS BINARY);'
        assert sql(query)=='0';print('CDC_PROBE_CLEANED '+run)
    else:raise ValueError('Expected create or cleanup')

if __name__=='__main__':main()
