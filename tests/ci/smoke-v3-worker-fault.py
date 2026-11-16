#!/usr/bin/env python3
"""External SIGKILL of the real JVM container with real Kafka+MySQL; synthetic persistence adapter/IO.

No published ports, production credentials, kubeconfig, model call or AWS client.
The fixture alone expires its synthetic lease AFTER proving it blocks recovery;
this accelerates a 600-second wait, not a production recovery-time measurement.
"""
import argparse,base64,hashlib,json,os,re,subprocess,sys,tempfile,time,uuid,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))
import v3_worker_fault as w


def smoke(output):
    prefix='w1-fixture-'+uuid.uuid4().hex[:10];net=prefix+'-net';names=[]
    broker=json.loads((ROOT/'deploy/v3/connect-image/source.lock.json').read_text())['baseImage']
    mysql=re.search(r'^image: (.+)$',(ROOT/'deploy/v3/mysql/values.yaml').read_text(),re.M)[1]
    def cmd(args,source=None,timeout=120,check=True):
        try:r=subprocess.run(args,input=source,capture_output=True,text=True,timeout=timeout)
        except subprocess.TimeoutExpired:
            if check:raise RuntimeError('Synthetic command exceeded bounded deadline: '+args[0]) from None
            return subprocess.CompletedProcess(args,124,'','')
        if check and r.returncode:raise RuntimeError('Synthetic '+args[0]+' command failed; raw data withheld')
        return r
    db=prefix+'-db'
    def sql(text):return cmd(['docker','exec','-i',db,'mysql','--protocol=TCP','-h127.0.0.1','-uroot','--batch','--raw','--skip-column-names','--silent'],text).stdout
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        d=Path(tmp);d.chmod(0o755)
        try:
            extract=prefix+'-extract';names.append(extract)
            cmd(['docker','create','--platform','linux/amd64','--name',extract,w.BE_IMAGE]);cmd(['docker','cp',extract+':/app/app.jar',str(d/'app.jar')])
            with zipfile.ZipFile(d/'app.jar') as jar:
                for n in jar.namelist():
                    if n.startswith('BOOT-INF/classes/') or n.startswith('BOOT-INF/lib/') and n.endswith('.jar'):
                        assert '..' not in Path(n).parts;jar.extract(n,d)
            javac=str(Path(os.environ.get('JAVA_HOME','/usr'))/'bin/javac')
            cmd([javac,'--release','25','-cp',str(d/'BOOT-INF/classes')+':'+str(d/'BOOT-INF/lib/*'),'-d',str(d/'classes'),str(ROOT/'tests/ci/v3/worker/WorkerCrashFixture.java')])
            cmd([javac,'--release','17','-cp',str(d/'BOOT-INF/lib/kafka-clients-4.2.1.jar'),'-d',str(d/'classes'),str(ROOT/'tests/ci/v3/worker/ConverterOwnership.java')])
            cmd([javac,'--release','17','-cp',str(d/'classes')+':'+str(d/'BOOT-INF/lib/*'),'-d',str(d/'classes'),str(ROOT/'tests/ci/v3/worker/OwnershipFrameFixture.java')])
            frame=json.loads(cmd([str(Path(javac).with_name('java')),'-cp',str(d/'classes')+':'+str(d/'BOOT-INF/lib/*'),'OwnershipFrameFixture']).stdout)
            assert frame['atomicFrameVerified'] and frame['unavailableReplacedHealthy']
            cmd(['docker','network','create','--internal',net])
            names.append(db)
            cmd(['docker','run','-d','--name',db,'--platform','linux/amd64','--network',net,'--network-alias','db','--memory','512m','--cpus','1','--tmpfs','/var/lib/mysql:size=384m','-e','MYSQL_ALLOW_EMPTY_PASSWORD=yes','-e','MYSQL_DATABASE=fixture','-e','MYSQL_USER=fixture','-e','MYSQL_PASSWORD=fixture-only',mysql,'--skip-log-bin','--innodb-buffer-pool-size=64M','--performance-schema=OFF'])
            for _ in range(80):
                if cmd(['docker','exec',db,'mysql','--protocol=TCP','-h127.0.0.1','-uroot','-Nse','SELECT 1'],check=False).returncode==0:break
                time.sleep(1)
            else:raise RuntimeError('Synthetic MySQL startup failed')
            sql("CREATE TABLE fixture.task(id VARCHAR(36) PRIMARY KEY,token VARCHAR(36),lease_until DATETIME(6),terminal BOOLEAN);CREATE TABLE fixture.item(id BIGINT PRIMARY KEY,saved BOOLEAN,attempt INT,applications INT);INSERT INTO fixture.task VALUES ('00000000-0000-4000-8000-000000007050',NULL,NULL,FALSE);INSERT INTO fixture.item VALUES (1,FALSE,0,0),(2,FALSE,0,0),(3,FALSE,0,0),(4,FALSE,0,0),(5,FALSE,0,0);")
            # Execute the exact production-shaped bounded SELECT template, not a regex-only SQL test.
            sql('''CREATE DATABASE yeodam;
CREATE TABLE yeodam.trip_creation_executions(trip_id BIGINT,execution_id VARCHAR(36),status VARCHAR(24),ended_at DATETIME(6),total_attachment_count INT);
CREATE TABLE yeodam.attachment_conversion_tasks(trip_id BIGINT,task_id VARCHAR(36),initial_batch_id BIGINT,kind VARCHAR(20),lane VARCHAR(20),status VARCHAR(20),total_photos BIGINT,claim_token VARCHAR(36),lease_until DATETIME(6),current_item_id BIGINT,current_attempt_no INT,current_attempt_token VARCHAR(36),latest_delivery_no BIGINT);
CREATE TABLE yeodam.initial_attachment_upload_batches(upload_batch_id BIGINT,trip_id BIGINT,accepted_at DATETIME(6),conversion_route VARCHAR(20));
CREATE TABLE yeodam.initial_attachment_upload_items(upload_item_id BIGINT,upload_batch_id BIGINT,trip_attachment_id BIGINT,conversion_attempts INT,conversion_started_at DATETIME(6),derived_json LONGTEXT,cleanup_json LONGTEXT,private_payload TEXT);
CREATE TABLE yeodam.attachment_conversion_outbox(trip_id BIGINT,task_id VARCHAR(36),delivery_no BIGINT,status VARCHAR(20),topic VARCHAR(100),record_partition INT,record_offset BIGINT,traceparent VARCHAR(55),cdc_managed BOOLEAN,created_at DATETIME(6));
CREATE TABLE yeodam.trip_attachments(trip_id BIGINT,trip_attachment_id BIGINT,file_id BIGINT,deleted_at DATETIME(6));
INSERT INTO yeodam.trip_creation_executions VALUES(705,'execution','PROCESSING',NULL,20);
INSERT INTO yeodam.initial_attachment_upload_batches VALUES(1,705,UTC_TIMESTAMP(6),'KAFKA'),(2,705,UTC_TIMESTAMP(6),'KAFKA');
INSERT INTO yeodam.attachment_conversion_tasks VALUES(705,'task1',1,'INITIAL','LIGHT','RUNNING',10,'PRIVATE_CLAIM_TOKEN',UTC_TIMESTAMP(6)+INTERVAL 10 MINUTE,3,1,'PRIVATE_CLAIM_TOKEN',1),(705,'task2',2,'INITIAL','LIGHT','WAITING',10,NULL,NULL,NULL,NULL,NULL,1);
''')
            for i in range(1,21):sql(f"INSERT INTO yeodam.initial_attachment_upload_items VALUES({i},{1 if i<=10 else 2},{i if i<=2 else 'NULL'},1,UTC_TIMESTAMP(6),{'JSON_OBJECT(\"synthetic\",true)' if i<=2 else 'NULL'},NULL,'PRIVATE_PHOTO_PAYLOAD');")
            raw=sql(w.sql(705));rows=[json.loads(x) for x in raw.splitlines()];w.validate_rows(rows,705,True)
            assert 'PRIVATE_CLAIM_TOKEN' not in raw and 'PRIVATE_PHOTO_PAYLOAD' not in raw
            assert len([r for r in rows if r['section']=='worker_item'])==20
            assert sql('SELECT COUNT(*) FROM yeodam.initial_attachment_upload_items;').strip()=='20'
            cfg='\n'.join(['node.id=1','process.roles=broker,controller','controller.quorum.voters=1@broker:9093','listeners=CLIENT://:9092,CONTROLLER://:9093','advertised.listeners=CLIENT://broker:9092','listener.security.protocol.map=CLIENT:PLAINTEXT,CONTROLLER:PLAINTEXT','inter.broker.listener.name=CLIENT','controller.listener.names=CONTROLLER','log.dirs=/tmp/kafka-data','offsets.topic.replication.factor=1','transaction.state.log.replication.factor=1','transaction.state.log.min.isr=1','group.initial.rebalance.delay.ms=0','auto.create.topics.enable=false'])+'\n';(d/'broker.properties').write_text(cfg)
            name=prefix+'-broker';names.append(name);cluster=base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('=')
            cmd(['docker','run','-d','--name',name,'--platform','linux/amd64','--network',net,'--network-alias','broker','--memory','512m','--cpus','1','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:mode=1777,size=384m','-v',str(d/'broker.properties')+':/fixture.properties:ro','-e','LOG_DIR=/tmp/kafka-logs','-e','KAFKA_HEAP_OPTS=-Xms128m -Xmx192m','--entrypoint','/bin/bash',broker,'-ec','/opt/kafka/bin/kafka-storage.sh format -t '+cluster+' -c /fixture.properties && exec /opt/kafka/bin/kafka-server-start.sh /fixture.properties'])
            for _ in range(6):
                if cmd(['docker','exec',name,'timeout','15','/opt/kafka/bin/kafka-topics.sh','--bootstrap-server','broker:9092','--list'],timeout=20,check=False).returncode==0:break
                time.sleep(1)
            else:raise RuntimeError('Local Kafka startup failed')
            def java(name,mode,detached):
                names.append(name)
                return cmd(['docker','run']+(['-d']if detached else[])+['--name',name,'--platform','linux/amd64','--network',net,'--memory','1g','--cpus','1','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','10001:10001','--tmpfs','/tmp:uid=10001,gid=10001,size=32m','-v',str(d)+':/fixture:ro','--entrypoint','java',w.BE_IMAGE,'-Xmx384m','-cp','/fixture/classes:/fixture/BOOT-INF/classes:/fixture/BOOT-INF/lib/*','com.yeodam.yeodambe.trip.service.WorkerCrashFixture',mode],timeout=90)
            first=prefix+'-first';java(first,'await-kill',True)
            for _ in range(70):
                log=cmd(['docker','logs',first]).stdout
                state=json.loads(cmd(['docker','inspect','--format','{{json .State}}',first]).stdout)
                assert state['Running'],'Worker exited before external kill'
                if 'WORKER_CRASH_READY' in log:break
                time.sleep(1)
            else:raise RuntimeError('No in-flight partial work before SIGKILL')
            # An actual external runtime signal, not a hook/Runtime.halt assertion.
            cmd(['docker','kill','--signal=KILL',first])
            cmd(['docker','wait',first],timeout=15)
            state=json.loads(cmd(['docker','inspect','--format','{{json .State}}',first]).stdout)
            assert state['ExitCode']==137 and not state['OOMKilled'],'External SIGKILL not observed'
            assert sql('SELECT SUM(saved),SUM(applications) FROM fixture.item;').strip()=='2\t2'
            assert int(sql('SELECT TIMESTAMPDIFF(SECOND,UTC_TIMESTAMP(),lease_until) FROM fixture.task;').strip())>=500
            second=prefix+'-second';java(second,'recover',True)
            for _ in range(55):
                log=cmd(['docker','logs',second]).stdout
                if 'WORKER_LEASE_BLOCKED' in log:break
                time.sleep(1)
            else:raise RuntimeError('New consumer did not demonstrate actual lease rejection')
            assert sql('SELECT SUM(saved) FROM fixture.item;').strip()=='2'
            # Local synthetic clock acceleration only. Live tool has NO write SQL path.
            sql('UPDATE fixture.task SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND;')
            for _ in range(40):
                state=json.loads(cmd(['docker','inspect','--format','{{json .State}}',second]).stdout)
                if not state['Running']:break
                time.sleep(1)
            assert state['ExitCode']==0,'Recovery fixture did not finish cleanly'
            assert 'WORKER_RECOVERED' in cmd(['docker','logs',second]).stdout
            assert sql('SELECT SUM(saved),SUM(applications),SUM(attempt) FROM fixture.item;').strip()=='5\t5\t6'
            proof={'schemaVersion':1,'image':w.BE_IMAGE,'sourceSha':w.BE_SOURCE,'atomicOwnershipFrameVerified':frame['atomicFrameVerified'],'actualImageClasses':True,'actualKafkaAndMySQL':True,
                   'abruptJvmHalt':False,'externalContainerSigkill':True,'chaosMeshRpcTested':False,'drainSkipped':True,'noEarlyCommit':True,'partialSaved':2,'finalSaved':5,'applications':5,
                   'oldLeaseRejected':True,'savedItemsReused':True,'interruptedItemRetried':True,'syntheticLeaseExpiredByFixture':True,
                   'productionJpaTested':False,'nativeConversionTested':False,'operatingRecoveryTimeProven':False,
                   'fixtureSha256':hashlib.sha256((ROOT/'tests/ci/v3/worker/WorkerCrashFixture.java').read_bytes()).hexdigest()}
            path=Path(output);assert not path.exists();path.write_text(json.dumps(proof,indent=2)+'\n');print(json.dumps(proof))
        except Exception:
            # Synthetic logs only, no production connectivity or credentials are present.
            for name in names:
                if name.endswith(('-first','-second','-broker')):
                    r=cmd(['docker','logs','--tail','16',name],check=False);print((r.stdout+r.stderr)[-2500:])
            raise
        finally:
            for name in reversed(names):cmd(['docker','rm','-f',name],check=False)
            cmd(['docker','network','rm',net],check=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();smoke(a.output)
