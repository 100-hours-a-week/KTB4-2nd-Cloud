#!/usr/bin/env python3
"""Staging-only native-replication handoff; no HTTP request replay or DB reset."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from uuid import UUID

DIRECTORY = Path('/var/lib/yeodam/migration-cutover')
RULE = 2050
CHANNEL = 'v1_to_v2'
PREFIX = '/yeodam/v2/staging/migration'
PHASES = {name:n for n,name in enumerate(('prepared','gate_intent','gated','fence_intent','source_fenced','caught_up','replica_stopped','promotion_intent','promoted','switched','aborted','hold_before_promotion','hold_after_promotion'),1)}

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def command(args, sql=None, env=None, timeout=5):
    result = subprocess.run(args, input=sql, text=True, capture_output=True, env=env, timeout=timeout)
    require(result.returncode == 0, 'Control command failed; inspect local state before retrying')
    # Empty trailing status columns are represented by tabs, not missing data.
    return result.stdout.rstrip('\n')

def parameter(name):
    return command(['aws','ssm','get-parameter','--region','ap-northeast-2','--name',name,
                    '--with-decryption','--query','Parameter.Value','--output','text'], timeout=15)

def replica_status(raw):
    rows = raw.splitlines()
    require(len(rows) == 2, 'Expected exactly one replication channel')
    require(len(rows[0].split('\t'))==len(rows[1].split('\t')), 'Truncated replica status')
    return dict(zip(rows[0].split('\t'), rows[1].split('\t')))

def position(raw):
    values = raw.split('\t')
    require(len(values) >= 2 and re.fullmatch(r'binlog\.[0-9]+', values[0]) and values[1].isdigit(), 'Unexpected binary log position')
    return [values[0], int(values[1])]

def caught_up(status, source):
    file = status.get('Relay_Source_Log_File', '')
    require(re.fullmatch(r'binlog\.[0-9]+', file) is not None, 'Invalid applied log file')
    return (int(file.split('.')[1]), int(status['Exec_Source_Log_Pos'])) >= (int(source[0].split('.')[1]), source[1])

class Database:
    def __init__(self, target):
        self.target = target
        self.proxy_password = Path('/etc/yeodam/v2-staging-proxysql/admin-password').read_text().strip()
        self.target_password = parameter(PREFIX+'/MYSQL_TARGET_PASSWORD')
        require(re.fullmatch('[0-9a-f]{64}', self.proxy_password) and re.fullmatch('[0-9a-f]{64}', self.target_password), 'Unexpected credential format')
        self.ca = Path(tempfile.mkdtemp(prefix='yeodam-cutover-', dir='/run'))/'ca.pem'
        self.ca.write_text(parameter(PREFIX+'/TARGET_CA_PEM'))
        self.ca.chmod(0o600)
        self.deadline = None

    def query(self, role, sql, timeout=5, headers=False):
        env = os.environ.copy()
        # Keep batch escaping: Executed_Gtid_Set can contain real newlines.
        # --raw would split one replica row into several output lines.
        args = ['mysql','--batch','--connect-timeout=2']
        if not headers: args += ['--skip-column-names']
        if role == 'source': args += ['--protocol=socket','-uroot']
        elif role == 'target':
            env['MYSQL_PWD'] = self.target_password
            args += ['--protocol=tcp','-h'+self.target,'-uyeodam_cutover','--ssl-mode=VERIFY_CA','--ssl-ca='+str(self.ca)]
        else:
            env['MYSQL_PWD'] = self.proxy_password
            args += ['--protocol=tcp','-h127.0.0.1','-P16032','-uradmin']
        if self.deadline is not None:
            require(time.monotonic() < self.deadline, 'Write transition deadline exceeded')
            timeout = min(timeout, max(0.1, self.deadline-time.monotonic()))
        return command(args, sql, env, timeout)

    def status(self):
        return replica_status(self.query('target', "SHOW REPLICA STATUS FOR CHANNEL 'v1_to_v2';", headers=True))

    def identity(self, role):
        return self.query(role, 'SELECT @@server_id,@@server_uuid,@@read_only,@@super_read_only;').split('\t')

    def own_target_gtid(self, uuid):
        raw = self.query('target','SELECT @@GLOBAL.gtid_executed;').replace('\\n','').replace('\n','')
        return ','.join(v.strip() for v in raw.split(',') if v.strip().startswith(uuid+':'))

    def route(self, group):
        # Rules apply to existing frontend sessions too; changing only the
        # user's default_hostgroup is insufficient for a live Hikari pool.
        self.query('proxy', f"UPDATE mysql_query_rules SET destination_hostgroup={group} WHERE rule_id={RULE}; LOAD MYSQL QUERY RULES TO RUNTIME; SAVE MYSQL QUERY RULES TO DISK;")
        require(self.query('proxy', f'SELECT destination_hostgroup FROM runtime_mysql_query_rules WHERE rule_id={RULE};') == str(group), 'Route rule not loaded')

    def close(self):
        self.ca.unlink(missing_ok=True)
        self.ca.parent.rmdir()

class Handoff:
    def __init__(self, db, path, run_id, gap):
        self.db, self.path, self.run_id, self.gap = db, path, run_id, gap
        self.state = json.loads(path.read_text()) if path.exists() else None

    def save(self, phase, **values):
        self.state.update(values, phase=phase)
        self.state.setdefault('events', []).append({'phase':phase,'at':time.time()})
        fd, name = tempfile.mkstemp(dir=self.path.parent)
        with os.fdopen(fd, 'w') as file:
            json.dump(self.state, file, indent=2); file.flush(); os.fsync(file.fileno())
        os.replace(name, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
        metrics = Path('/var/lib/yeodam/mysql-metrics')
        if metrics.is_dir():
            descriptor, temporary = tempfile.mkstemp(prefix='.cutover-', dir=metrics)
            with os.fdopen(descriptor,'w') as file:
                file.write(f'yeodam_cutover_phase{{run_id="{self.run_id}"}} {PHASES[phase]}\n')
                file.write(f'yeodam_cutover_promotion_possible{{run_id="{self.run_id}"}} {int(self.state["promotionPossible"])}\n')
                file.write(f'yeodam_cutover_event_timestamp_seconds {time.time()}\n')
            os.chmod(temporary,0o644); os.replace(temporary,metrics/'cutover.prom')
        print(json.dumps({'runId':self.run_id,'phase':phase}), flush=True)

    def check(self):
        reset=Path('/var/lib/yeodam/rehearsal-reset/active.json')
        require(not reset.exists() or json.loads(reset.read_text()).get('phase')=='completed','Rehearsal reset is incomplete')
        source, target, status = self.db.identity('source'), self.db.identity('target'), self.db.status()
        require(source[0]=='1' and target[0]=='2' and source[1]!=target[1], 'Wrong source/target identities')
        require(source[2:]==['0','0'], 'V1 must be the active writer before handoff')
        require(status['Source_UUID']==source[1] and status['Source_Host']=='10.30.0.174', 'Unexpected replication source')
        require(status['Replica_IO_Running']=='Yes' and status['Replica_SQL_Running']=='Yes' and status['Last_IO_Errno']=='0' and status['Last_SQL_Errno']=='0', 'Replication is not healthy')
        require(status['Auto_Position']=='0' and status['Replicate_Wild_Do_Table']=='yeodam_v1_source.%', 'Unexpected replication mode/filter')
        require(not self.state or self.state['phase']=='aborted', 'Existing handoff requires inspection')
        require(self.db.query('proxy','SELECT DISTINCT default_hostgroup FROM runtime_mysql_users WHERE username="yeodam_app";')=='10', 'Expected V1 hostgroup 10')
        require(self.db.query('proxy','SELECT COUNT(*) FROM runtime_mysql_users WHERE username="yeodam_app" AND (transaction_persistent!=1 OR fast_forward!=0);')=='0', 'Transaction routing cannot be bypassed')
        require(self.db.query('proxy',f'SELECT COUNT(*) FROM mysql_query_rules WHERE active=1 OR rule_id={RULE};')=='0' and self.db.query('proxy','SELECT COUNT(*) FROM mysql_query_rules_fast_routing;')=='0', 'Unexpected query rules')
        servers = self.db.query('proxy','SELECT hostgroup_id,hostname,port,status FROM runtime_mysql_servers ORDER BY hostgroup_id;')
        require(servers in [f'10\t172.28.0.1\t3306\tONLINE\n20\t{self.db.target}\t3306\t{s}' for s in ('ONLINE','OFFLINE_SOFT')], 'Unexpected backend pool')
        backend = command(['docker','ps','--filter','label=com.docker.compose.project=yeodam-app','--filter','label=com.docker.compose.service=backend','--quiet'])
        require(backend and '\n' not in backend, 'Expected one staging BE')
        require(command(['docker','exec',backend,'sh','-c','printf %s "$MYSQL_URL"'])=='jdbc:mysql://proxysql:6033/yeodam_v1_source', 'Switch BE to ProxySQL BEFORE starting load')
        # ProxySQL can retain an idle connection from an earlier candidate
        # SELECT. Permit only idle app connections accounted for by its free
        # HG20 pool. Active or unrelated clients still block the rehearsal.
        require(self.db.query('target',"SELECT COUNT(*) FROM information_schema.processlist WHERE USER NOT IN ('system user','event_scheduler','yeodam_cutover') AND ID!=CONNECTION_ID() AND NOT(USER='yeodam_app' AND HOST LIKE '10.30.0.174:%' AND COMMAND='Sleep');")=='0', 'Target has another client; isolate the rehearsal first')
        idle = self.db.query('target',"SELECT COUNT(*) FROM information_schema.processlist WHERE USER='yeodam_app' AND HOST LIKE '10.30.0.174:%' AND COMMAND='Sleep';")
        free = self.db.query('proxy','SELECT COALESCE(SUM(ConnFree),0) FROM stats_mysql_connection_pool WHERE hostgroup=20;')
        used = self.db.query('proxy','SELECT COALESCE(SUM(ConnUsed),0) FROM stats_mysql_connection_pool WHERE hostgroup=20;')
        require(idle==free and used=='0' and self.db.query('target','SELECT COUNT(*) FROM information_schema.innodb_trx;')=='0', 'Target sessions do not match the idle proxy pool')
        return {'sourceUuid':source[1],'targetUuid':target[1],'targetFlags':target[2:], 'targetOwnGtid':self.db.own_target_gtid(target[1]), 'sourcePosition':position(self.db.query('source','SHOW BINARY LOG STATUS;'))}

    def drain(self):
        while True:
            proxy = self.db.query('proxy', 'SELECT Variable_Value FROM stats_mysql_global WHERE Variable_Name="Active_Transactions";')
            transactions = self.db.query('source','SELECT COUNT(*) FROM information_schema.innodb_trx;')
            if proxy=='0' and transactions=='0': return
            time.sleep(0.1)

    def cutover(self):
        require(not self.state or self.state['phase']=='aborted', 'Previous handoff needs inspection; no implicit reset')
        snapshot = self.check()
        self.state = dict(snapshot, runId=self.run_id, promotionPossible=False, target=self.db.target)
        self.save('prepared')
        try:
            self.db.query('target','SET SESSION lock_wait_timeout=2; SET PERSIST super_read_only=ON;')
            require(self.db.identity('target')[2:]==['1','1'], 'Target fence failed')
            self.save('gate_intent')
            self.db.deadline = time.monotonic()+self.gap
            # Wait IN the destination hostgroup. Queued queries retain their
            # chosen group and cannot be moved later by reloading query rules.
            # Existing transactions stay in HG10 until commit. This waiting
            # is bounded connection waiting, not a durable request queue.
            self.db.query('proxy', f"UPDATE mysql_servers SET status='OFFLINE_SOFT' WHERE hostgroup_id=20; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK; INSERT INTO mysql_query_rules(rule_id,active,username,match_digest,destination_hostgroup,apply,comment) VALUES({RULE},1,'yeodam_app','.*',20,1,'staging handoff'); LOAD MYSQL QUERY RULES TO RUNTIME; SAVE MYSQL QUERY RULES TO DISK; UPDATE mysql_servers SET status='OFFLINE_SOFT' WHERE hostgroup_id=10; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
            self.save('gated')
            self.drain()
            self.save('fence_intent')
            self.db.query('source','SET SESSION lock_wait_timeout=2; SET PERSIST super_read_only=ON;')
            require(self.db.identity('source')[2:]==['1','1'], 'Source fence failed')
            self.drain()
            final = position(self.db.query('source','SHOW BINARY LOG STATUS;'))
            self.save('source_fenced', finalPosition=final, finalSourceGtid=self.db.query('source','SELECT @@GLOBAL.gtid_executed;'))
            # Snapshot initialization uses file/position and does not import
            # all historical GTIDs. Lag=0 / full GTID subset are not sufficient.
            waited = self.db.query('target', f"SELECT SOURCE_POS_WAIT('{final[0]}',{final[1]},2,'{CHANNEL}');")
            require(waited.isdigit(), 'Final replication position wait failed')
            require(caught_up(self.db.status(),final), 'Replica has not applied the final position')
            self.save('caught_up')
            self.db.query('target', f"STOP REPLICA FOR CHANNEL '{CHANNEL}';")
            require(caught_up(self.db.status(),final), 'Applied position changed after stopping')
            require(position(self.db.query('source','SHOW BINARY LOG STATUS;'))==final, 'Source log advanced after fence')
            self.save('replica_stopped')
            require(self.db.identity('source')[2:]==['1','1'] and self.db.identity('target')[2:]==['1','1'], 'Both fences required')
            require(self.db.own_target_gtid(self.state['targetUuid'])==self.state['targetOwnGtid'], 'Target committed unexpected local writes before promotion')
            # Durable intent precedes the first ambiguous promotion command.
            # A lost response may mean target writes already happened.
            self.save('promotion_intent', promotionPossible=True)
            self.db.query('target','SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
            require(self.db.identity('target')[2:]==['0','0'], 'Target writer activation failed')
            self.save('promoted')
            self.finish()
        except BaseException:
            self.db.deadline = None
            if self.state['promotionPossible']:
                self.save('hold_after_promotion')
                # Preserve whichever target state was reached; NEVER reopen V1.
            else:
                self.abort()
            raise
        finally:
            self.db.deadline = None

    def finish(self):
        """Forward-only recovery when promotion acknowledgement was lost."""
        require(self.state and self.state['runId']==self.run_id and self.state['target']==self.db.target and self.state['promotionPossible'], 'No matching promotion intent')
        require(self.db.identity('source')[1:]==[self.state['sourceUuid'],'1','1'], 'V1 must remain fenced')
        require(self.db.query('source','SELECT @@GLOBAL.gtid_executed;')==self.state['finalSourceGtid'], 'V1 transaction history advanced after the fence')
        require(self.db.identity('target')[1:]==[self.state['targetUuid'],'0','0'], 'V2 writer state must be verified before finishing')
        status = self.db.status()
        require(status['Replica_IO_Running']=='No' and status['Replica_SQL_Running']=='No', 'Replication must be stopped before V2 writes')
        require(caught_up(status,self.state['finalPosition']), 'Final replicated position is missing')
        self.db.query('proxy', "UPDATE mysql_users SET default_hostgroup=20 WHERE username='yeodam_app'; LOAD MYSQL USERS TO RUNTIME; SAVE MYSQL USERS TO DISK; UPDATE mysql_servers SET status='OFFLINE_HARD' WHERE hostgroup_id=10; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
        self.db.route(20)
        self.db.query('proxy', "UPDATE mysql_servers SET status='ONLINE' WHERE hostgroup_id=20; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
        self.save('switched')

    def abort(self):
        require(self.state and self.state['runId']==self.run_id and self.state['target']==self.db.target, 'No matching handoff journal')
        require(not self.state['promotionPossible'], 'V2 may have committed writes: V1 automatic rollback forbidden')
        self.db.deadline = None
        try:
            # Re-fence target before restarting replication / opening V1.
            self.db.query('target','SET SESSION lock_wait_timeout=2; SET PERSIST super_read_only=ON;')
            require(self.db.identity('target')[2:]==['1','1'], 'Cannot verify target fence; hold V1')
            require(self.db.identity('target')[1]==self.state['targetUuid'] and self.db.own_target_gtid(self.state['targetUuid'])==self.state['targetOwnGtid'], 'Target history changed; hold V1 for inspection')
            self.db.query('proxy', "UPDATE mysql_servers SET status='OFFLINE_SOFT' WHERE hostgroup_id=20; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
            self.db.query('target', f"START REPLICA FOR CHANNEL '{CHANNEL}';")
            ready_by = time.monotonic()+3
            while True:
                status = self.db.status()
                if status['Replica_IO_Running']=='Yes' and status['Replica_SQL_Running']=='Yes' and status['Last_IO_Errno']=='0' and status['Last_SQL_Errno']=='0': break
                require(time.monotonic()<ready_by, 'Cannot restore replica; hold V1')
                time.sleep(0.1)
            self.db.query('proxy', "UPDATE mysql_users SET default_hostgroup=10 WHERE username='yeodam_app'; LOAD MYSQL USERS TO RUNTIME; SAVE MYSQL USERS TO DISK; UPDATE mysql_servers SET status='ONLINE' WHERE hostgroup_id=10; LOAD MYSQL SERVERS TO RUNTIME; SAVE MYSQL SERVERS TO DISK;")
            self.db.query('source','SET PERSIST super_read_only=OFF; SET PERSIST read_only=OFF;')
            self.db.query('proxy', f'DELETE FROM mysql_query_rules WHERE rule_id={RULE}; LOAD MYSQL QUERY RULES TO RUNTIME; SAVE MYSQL QUERY RULES TO DISK;')
            self.save('aborted')  # Target intentionally stays fenced.
        except BaseException:
            self.save('hold_before_promotion')
            raise

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check','cutover','abort','finish'))
    parser.add_argument('--target',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--confirm-run')
    parser.add_argument('--gap-seconds',type=int,default=8)
    args = parser.parse_args()
    require(os.geteuid()==0, 'Run as root on V1 staging App')
    require(str(UUID(args.run_id))==args.run_id and re.fullmatch(r'10\.30\.10\.[0-9]+',args.target), 'Expected staging target and canonical Run UUID')
    require(2<=args.gap_seconds<=60, 'Gap deadline must be 2..60 seconds')
    if args.action!='check': require(args.confirm_run==args.run_id, 'Mutation requires matching Run confirmation')
    require(command(['aws','sts','get-caller-identity','--query','Account','--output','text'])=='483175530259', 'Wrong AWS account')
    require(command(['mysql','-uroot','--protocol=socket','--batch','--skip-column-names','-e','SELECT @@server_id;'])=='1', 'Wrong staging source')
    require('10.30.0.174' in command(['hostname','-I']).split(), 'Wrong V1 staging private address')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(RuntimeError('Termination requested')))
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    DIRECTORY.chmod(0o700)
    with (DIRECTORY/'lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db = Database(args.target)
        try:
            handoff = Handoff(db,DIRECTORY/'active.json',args.run_id,args.gap_seconds)
            if args.action=='check': print(json.dumps({'preflight':handoff.check(),'journal':handoff.state}))
            else: getattr(handoff,args.action)()
        finally: db.close()

if __name__=='__main__':
    try: main()
    except (RuntimeError,ValueError,OSError,subprocess.SubprocessError) as error:
        print('ERROR: '+str(error), file=sys.stderr); raise SystemExit(1)
