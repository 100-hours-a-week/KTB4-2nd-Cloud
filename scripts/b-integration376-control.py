#!/usr/bin/env python3
"""Source-host control for the fixed #376 cutover, without native replication."""
import argparse
import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import subprocess
import time
from uuid import UUID,uuid5

BASE=Path('/var/lib/yeodam/b376')

def database():
    loader=SourceFileLoader('b376db','/usr/local/sbin/yeodam-staging-db-cutover')
    spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
    return m.Database('10.30.10.28')

def save(run,phase,**values):
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True);path=BASE/(run+'.json')
    value=json.loads(path.read_text()) if path.exists() else {'runId':run,'events':[]}
    value.update(phase=phase,**values);value['events'].append({'phase':phase,'at':time.time()})
    path.write_text(json.dumps(value));path.chmod(0o600)
    print(json.dumps({'phase':phase,'runId':run,**values}),flush=True)

def audit(db,run,role,max_id=None):
    prefix='t'+hashlib.sha256(run.encode()).hexdigest()[:5]
    region_pk=db.query(role,"SELECT column_name FROM information_schema.key_column_usage WHERE table_schema='yeodam_v1_source' AND table_name='trip_regions' AND constraint_name='PRIMARY' ORDER BY ordinal_position;").splitlines()
    if len(region_pk)!=1 or not region_pk[0].replace('_','').isalnum():raise RuntimeError('Unexpected region primary key')
    where="t.trip_name LIKE '"+prefix+"%'"+((' AND t.trip_id<='+str(max_id)) if max_id is not None else '')
    data=db.query(role,'SELECT t.*,r.* FROM yeodam_v1_source.trips t JOIN yeodam_v1_source.trip_regions r ON r.trip_id=t.trip_id WHERE '+where+' ORDER BY t.trip_id,r.'+region_pk[0]+';',timeout=90)
    rows=len(data.splitlines()) if data else 0
    projection=db.query(role,'SELECT t.user_id,t.trip_name,t.start_date,t.end_date,t.processing_status,r.region_code FROM yeodam_v1_source.trips t LEFT JOIN yeodam_v1_source.trip_regions r ON r.trip_id=t.trip_id WHERE '+where+' ORDER BY t.trip_id,r.'+region_pk[0]+';',timeout=90)
    h=hashlib.sha256()
    for row in sorted(tuple(line.split('\t')) for line in projection.splitlines()):
        h.update(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode());h.update(b'\n')
    return {'rows':rows,'fullRowsSha256':hashlib.sha256((data+'\n').encode()).hexdigest(),'requestProjectionSha256':h.hexdigest(),
            'missingRegions':sum(line.endswith('\tNULL') for line in projection.splitlines())}

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('preflight','fence','promote','audit-source','audit-target','status','resume-source','recover-unpromoted','park-target-pool'));p.add_argument('--run-id',required=True);p.add_argument('--at',type=float);a=p.parse_args()
    if str(UUID(a.run_id))!=a.run_id or '10.30.0.174' not in subprocess.check_output(['hostname','-I'],text=True).split():raise ValueError('Wrong host or UUID')
    db=database()
    try:
        source,target=db.identity('source'),db.identity('target')
        if source[0]!='1' or target[0]!='2':raise RuntimeError('Unexpected database identity')
        if a.action=='preflight':
            if source[2:]!=['0','0'] or target[2:]!=['1','1']:raise RuntimeError('Expected Source Writer and protected Target')
            if db.query('proxy',"SELECT DISTINCT default_hostgroup FROM runtime_mysql_users WHERE username='yeodam_app';")!='10':raise RuntimeError('ProxySQL is not on Source')
            if db.query('proxy','SELECT COUNT(*) FROM runtime_mysql_query_rules WHERE active=1;')!='0':raise RuntimeError('Unexpected ProxySQL rules')
            if db.query('target',"SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('root','event_scheduler','system user','yeodam_cutover') AND ID!=CONNECTION_ID();")!='0':raise RuntimeError('Previous Target clients remain; do not start a new load')
            s=db.status()
            if s['Replica_IO_Running']!='No' or s['Replica_SQL_Running']!='No':raise RuntimeError('Native replication must be stopped')
            prefix='t'+hashlib.sha256(a.run_id.encode()).hexdigest()[:5]
            if db.query('source',"SELECT COUNT(*) FROM yeodam_v1_source.trips WHERE trip_name LIKE '"+prefix+"%';")!='0':raise RuntimeError('Run trip name prefix already exists')
            save(a.run_id,'preflight',sourceRows=int(db.query('source','SELECT COUNT(*) FROM yeodam_v1_source.trips;')),
                 sourceConnections=int(db.query('source','SELECT @@max_connections;')),targetConnections=int(db.query('target','SELECT @@max_connections;')),
                 flywaySource=db.query('source','SELECT version FROM yeodam_v1_source.flyway_schema_history WHERE success=1 ORDER BY installed_rank DESC LIMIT 1;'))
        elif a.action=='fence':
            if a.at is not None:
                if not 0 <= a.at-time.time() <= 900:raise RuntimeError('Cutover schedule outside this Run')
                while time.time()<a.at:time.sleep(min(1,a.at-time.time()))
            source,target=db.identity('source'),db.identity('target')
            if source[2:]!=['0','0'] or target[2:]!=['1','0']:raise RuntimeError('Unexpected pre-fence roles')
            subprocess.run(['python3','/run/yeodam-b376/b-integration376-app.py','queue','--run-id',a.run_id],check=True)
            # The read_only transition waits for pre-existing writes; no B2 consumer is opened here.
            db.query('source','SET SESSION lock_wait_timeout=5; SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;',timeout=10)
            if db.query('source','SELECT COUNT(*) FROM information_schema.innodb_trx;')!='0':raise RuntimeError('Source transactions remain after fence')
            max_id=int(db.query('source','SELECT COALESCE(MAX(trip_id),0) FROM yeodam_v1_source.trips;'))
            save(a.run_id,'source_fenced',sourcePosition=db.query('source','SHOW BINARY LOG STATUS;'),sourceMaxTripId=max_id,sourceRun=audit(db,a.run_id,'source'))
        elif a.action=='promote':
            state=json.loads((BASE/(a.run_id+'.json')).read_text())
            if source[2:]!=['1','1'] or target[2:]!=['1','0'] or state['phase']!='source_fenced':raise RuntimeError('Unexpected promotion boundary')
            proof=BASE/(a.run_id+'.cdc-drained.json')
            if not proof.exists():raise RuntimeError('Missing frozen-tail CDC drain proof')
            value=json.loads(proof.read_text())
            if value.get('runId')!=a.run_id or value.get('equal') is not True:raise RuntimeError('CDC proof does not match Run')
            if db.query('source','SHOW BINARY LOG STATUS;').split('\t')[:2]!=state['sourcePosition'].split('\t')[:2]:raise RuntimeError('Source changed after the frozen boundary')
            save(a.run_id,'promotion_intent')
            db.query('target','SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
            db.query('proxy',"UPDATE mysql_servers SET status='ONLINE' WHERE hostgroup_id=20; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK; INSERT INTO mysql_query_rules(rule_id,active,username,match_digest,destination_hostgroup,apply) VALUES(2376,1,'yeodam_app','.*',20,1); LOAD MYSQL QUERY RULES TO RUNTIME; SAVE MYSQL QUERY RULES TO DISK; UPDATE mysql_users SET default_hostgroup=20 WHERE username='yeodam_app'; LOAD MYSQL USERS TO RUNTIME; SAVE MYSQL USERS TO DISK;")
            save(a.run_id,'switched',targetFlags=db.identity('target')[2:])
        elif a.action=='park-target-pool':
            if source[2:]!=['1','1']:raise RuntimeError('Source must remain fenced during maintenance')
            db.query('proxy',"UPDATE mysql_servers SET status='OFFLINE_SOFT' WHERE hostgroup_id=20; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
            print('TARGET_PROXY_POOL_PARKED')
        elif a.action in ('resume-source','recover-unpromoted'):
            if source[2:]!=['1','1'] or target[2:] not in (['1','1'],['1','0']):raise RuntimeError('Cannot reopen Source while Target may be an application Writer')
            if a.action=='recover-unpromoted':
                state=json.loads((BASE/(a.run_id+'.json')).read_text())
                if state['phase']!='source_fenced':raise RuntimeError('Promotion may have started; keep both sides protected')
            db.query('target','SET PERSIST read_only=ON; SET PERSIST super_read_only=ON;')
            db.query('proxy',"DELETE FROM mysql_query_rules WHERE rule_id=2376; LOAD MYSQL QUERY RULES TO RUNTIME; SAVE MYSQL QUERY RULES TO DISK; UPDATE mysql_users SET default_hostgroup=10 WHERE username='yeodam_app'; LOAD MYSQL USERS TO RUNTIME; SAVE MYSQL USERS TO DISK; UPDATE mysql_servers SET status='ONLINE' WHERE hostgroup_id=10; UPDATE mysql_servers SET status='OFFLINE_SOFT' WHERE hostgroup_id=20; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
            db.query('source','SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
            save(a.run_id,'source_reopened')
        elif a.action.startswith('audit-'):
            state=json.loads((BASE/(a.run_id+'.json')).read_text());role=a.action[6:]
            report={'runId':a.run_id,'role':role,'all':audit(db,a.run_id,role)}
            if role=='target' and state.get('sourceMaxTripId'):
                report['sourceBoundary']=audit(db,a.run_id,'target',state['sourceMaxTripId'])
                report['sourceBoundaryEqual']=report['sourceBoundary']==state['sourceRun']
                lower=state['events'][0]['at']
                smoke=str(uuid5(UUID(a.run_id),'public-b2-smoke'))
                report['journal']=db.query('target',"SELECT status,COUNT(*) FROM yeodam_v1_source.migration_trip_request_result WHERE created_at>=FROM_UNIXTIME("+str(lower)+") AND request_id!='"+smoke+"' GROUP BY status;")
                report['publicSmokeJournal']=db.query('target',"SELECT status,COUNT(*) FROM yeodam_v1_source.migration_trip_request_result WHERE request_id='"+smoke+"' GROUP BY status;")
            print(json.dumps(report))
        else:print(json.dumps({'source':source[2:],'target':target[2:],'proxy':db.query('proxy',"SELECT DISTINCT default_hostgroup FROM runtime_mysql_users WHERE username='yeodam_app';")}))
    finally:db.close()
if __name__=='__main__':main()
