#!/usr/bin/env python3
"""Execute the bounded SELECTs on a disposable, network-isolated MySQL.

Synthetic projections only, not a production schema/CDC/chaos recovery proof.
No ports, volumes, AWS credentials, application images or model requests.
"""
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import v3_cdc_fault as f

image = re.search(r'^image: (.+)$', (ROOT/'deploy/v3/mysql/values.yaml').read_text(), re.M)[1]
name = 'v3-cdc-evidence-' + uuid.uuid4().hex[:10]


def command(args, source=None, timeout=60):
    p = subprocess.run(args, input=source, text=True, capture_output=True, timeout=timeout)
    if p.returncode:
        code = re.search(r'\bERROR (\d+)\b', p.stderr)
        raise RuntimeError('Synthetic MySQL operation failed: exit=' + str(p.returncode) +
                           ', mysqlErrorCode=' + (code[1] if code else 'unknown') + '; raw output withheld')
    return p.stdout


def sql(source):
    return command(['docker','exec','-i',name,'mysql','-uroot','--protocol=TCP','-h127.0.0.1',
                    '--batch','--raw','--skip-column-names','--silent'], source)


try:
    if subprocess.run(['docker','image','inspect',image],capture_output=True).returncode:
        command(['docker','pull',image], timeout=180)
    command(['docker','run','-d','--name',name,'--network','none','--memory','512m','--cpus','1',
             '--tmpfs','/var/lib/mysql:rw,size=384m','-e','MYSQL_ALLOW_EMPTY_PASSWORD=yes',
             image,'--skip-log-bin','--innodb-buffer-pool-size=64M','--performance-schema=OFF'])
    ready = False
    for _ in range(60):
        # The entrypoint's initialization server accepts Unix sockets before it
        # deliberately shuts down. Only the FINAL server accepts TCP loopback.
        p = subprocess.run(['docker','exec',name,'mysql','-uroot','--protocol=TCP','-h127.0.0.1',
                            '-Nse','SELECT 1'],capture_output=True,timeout=10)
        if p.returncode == 0:
            ready = True; break
        time.sleep(1)
    assert ready, 'Synthetic MySQL startup timeout'
    sql('''CREATE DATABASE yeodam;
CREATE TABLE yeodam.attachment_conversion_outbox (
outbox_id VARCHAR(36), task_id VARCHAR(36), delivery_no BIGINT, trip_id BIGINT,
status VARCHAR(24), created_at DATETIME(6), published_at DATETIME(6), record_partition INT,
record_offset BIGINT, broker_timestamp DATETIME(6), cdc_managed BOOLEAN, payload TEXT);
CREATE TABLE yeodam.trip_creation_outbox (
outbox_id VARCHAR(36), execution_id VARCHAR(36), trip_id BIGINT, status VARCHAR(24),
created_at DATETIME(6), published_at DATETIME(6), cdc_managed BOOLEAN, payload TEXT);
CREATE TABLE yeodam.attachment_conversion_tasks (task_id VARCHAR(36),trip_id BIGINT,status VARCHAR(24));
CREATE TABLE yeodam.trip_creation_executions (execution_id VARCHAR(36),trip_id BIGINT,status VARCHAR(24),
status_version BIGINT,display_status VARCHAR(24),total_attachment_count INT);
CREATE TABLE yeodam.trip_attachments (trip_id BIGINT,file_id BIGINT,deleted_at DATETIME);
INSERT INTO yeodam.attachment_conversion_outbox VALUES
('box-trial','task-trial',1,7001,'PUBLISHED',NOW(),NOW(),0,123,NOW(),TRUE,'DO_NOT_EXPORT_PAYLOAD'),
('box-other','task-other',1,7002,'PENDING',NOW(),NULL,NULL,NULL,NULL,TRUE,'DO_NOT_EXPORT_PAYLOAD'),
('not-cdc','task-trial',2,7001,'PENDING',NOW(),NULL,NULL,NULL,NULL,FALSE,'DO_NOT_EXPORT_PAYLOAD');
INSERT INTO yeodam.trip_creation_outbox VALUES ('creation-trial','execution-trial',7001,'PENDING',NOW(),NULL,TRUE,'DO_NOT_EXPORT_PAYLOAD');
INSERT INTO yeodam.attachment_conversion_tasks VALUES ('task-trial',7001,'SUCCEEDED');
INSERT INTO yeodam.trip_creation_executions VALUES ('execution-trial',7001,'PROCESSING',2,'PROCESSING',2);
INSERT INTO yeodam.trip_attachments VALUES (7001,1,NULL),(7001,1,NULL),(7002,2,NULL);
''')
    sql('''ALTER TABLE yeodam.trip_attachments ADD COLUMN trip_attachment_id BIGINT;
UPDATE yeodam.trip_attachments SET trip_attachment_id=file_id;
ALTER TABLE yeodam.attachment_conversion_outbox ADD COLUMN topic VARCHAR(255), ADD COLUMN traceparent VARCHAR(55), ADD COLUMN publication_version BIGINT DEFAULT 0;
CREATE TABLE yeodam.initial_attachment_upload_batches (upload_batch_id BIGINT, trip_id BIGINT);
CREATE TABLE yeodam.initial_attachment_upload_items (upload_item_id BIGINT,upload_batch_id BIGINT,trip_attachment_id BIGINT,conversion_attempts INT);
INSERT INTO yeodam.initial_attachment_upload_batches VALUES (1,7001),(2,7002);
INSERT INTO yeodam.initial_attachment_upload_items VALUES (10,1,1,2),(20,2,2,1);
''')
    detailed=sql(f.followup_evidence_sql([7001])); proof=[json.loads(x)for x in detailed.splitlines()]
    assert all(x['tripId']==7001 for x in proof) and 'DO_NOT_EXPORT_PAYLOAD' not in detailed
    assert [x['itemId']for x in proof if x['section']=='trial_item']==[10]
    assert next(x['attempt']for x in proof if x['section']=='trial_item')==2
    assert next(x['offset']for x in proof if x['section']=='delivery_trace')==123
    assert len([x for x in proof if x['section']=='final_attachment'])==2
    count = sql('SELECT COUNT(*) FROM yeodam.attachment_conversion_outbox;')
    raw = sql(f.evidence_sql([7001])); rows = [json.loads(x) for x in raw.splitlines()]
    assert all(r['tripId'] == 7001 for r in rows) and 'DO_NOT_EXPORT_PAYLOAD' not in raw
    assert {r['outboxId'] for r in rows if 'outboxId' in r} == {'box-trial','creation-trial'}
    assert next(r for r in rows if r['section']=='attachment_counts')['activeRows'] == 2
    evidence = {'tripIds':[7001],'approvedRevision':'a'*40,'coverage':'bounded-trial-only','rows':rows}
    result = f.compare_evidence(evidence,evidence)
    assert result['unconfirmedOutboxIds'] == ['creation-trial']
    assert result['duplicateActiveFileRowsByTrip'] == {'7001':1}
    assert result['rawDeliveryCount'] is None and result['verdict'] == 'not_assessed'
    assert sql('SELECT COUNT(*) FROM yeodam.attachment_conversion_outbox;') == count
    print('Synthetic MySQL SELECTs: trial/CDC filters, receipt positions, missing event, duplicate file rows, data preservation passed')
finally:
    subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)
