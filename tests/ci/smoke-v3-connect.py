#!/usr/bin/env python3
"""Synthetic local Docker-only TLS/SCRAM/RF3 runtime check; never reads AWS/kubeconfig/real Secrets."""
import argparse
import base64
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[2]
LOCK=json.loads((ROOT/'deploy/v3/connect-image/source.lock.json').read_text())
INTERNAL=['prod.connect-configs','prod.connect-offsets','prod.connect-status']


def smoke(image, output):
    prefix='yeodam-connect-check-'+uuid.uuid4().hex[:10]
    net=prefix+'-net';containers=[];output.mkdir(parents=True,exist_ok=True)
    directory=Path(tempfile.mkdtemp(prefix=prefix+'-'));directory.chmod(0o755)
    counter=0
    def command(args,timeout=120,check=True):
        nonlocal counter
        result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
        counter+=1;(output/f'command-{counter}.log').write_text(result.stdout+result.stderr)
        if check and result.returncode:raise RuntimeError('Local check failed; private diagnostic command '+str(counter))
        return result
    def docker(*args,**kw):return command(['docker',*map(str,args)],**kw)
    def wait_api(port):
        for _ in range(90):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/connectors',timeout=2) as r:
                    if json.load(r)==[]:return
            except (urllib.error.URLError,TimeoutError,OSError):pass
            time.sleep(2)
        raise RuntimeError('Connect did not become ready under the minimum budget')
    try:
        tls=directory/'tls';tls.mkdir(mode=0o755)
        command(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(tls/'key.pem'),'-out',str(tls/'ca.crt'),'-days','1','-subj','/CN=local-connect-check','-addext','subjectAltName=DNS:broker1,DNS:broker2,DNS:broker3'])
        (tls/'broker.pem').write_text((tls/'key.pem').read_text()+(tls/'ca.crt').read_text())
        for file in tls.iterdir():file.chmod(0o644) # Synthetic credentials only; removed in finally.
        password=secrets.token_hex(24)
        admin='local-admin';user='v3-connect-outbox'
        client='\n'.join(['security.protocol=SASL_SSL','sasl.mechanism=SCRAM-SHA-512',f'sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required username="{admin}" password="{password}";','ssl.truststore.type=PEM','ssl.truststore.location=/check/tls/ca.crt','ssl.endpoint.identification.algorithm=https','request.timeout.ms=5000','default.api.timeout.ms=10000'])+'\n'
        (directory/'admin.properties').write_text(client);(directory/'admin.properties').chmod(0o644)
        docker('network','create','--label','yeodam.issue=537',net)
        cluster=base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('=')
        for i in range(1,4):
            cfg=directory/f'broker{i}.properties';data=directory/f'data{i}';data.mkdir(mode=0o777);data.chmod(0o777)
            cfg.write_text('\n'.join([f'node.id={i}','process.roles=broker,controller','controller.quorum.voters=1@broker1:9094,2@broker2:9094,3@broker3:9094','listeners=CLIENT://:9093,INTERNAL://:9092,CONTROLLER://:9094',f'advertised.listeners=CLIENT://broker{i}:9093,INTERNAL://broker{i}:9092','listener.security.protocol.map=CLIENT:SASL_SSL,INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT','inter.broker.listener.name=INTERNAL','controller.listener.names=CONTROLLER','listener.name.client.sasl.enabled.mechanisms=SCRAM-SHA-512','listener.name.client.scram-sha-512.sasl.jaas.config=org.apache.kafka.common.security.scram.ScramLoginModule required;','ssl.keystore.type=PEM','ssl.keystore.location=/check/tls/broker.pem','ssl.truststore.type=PEM','ssl.truststore.location=/check/tls/ca.crt','ssl.client.auth=none','authorizer.class.name=org.apache.kafka.metadata.authorizer.StandardAuthorizer','allow.everyone.if.no.acl.found=false',f'super.users=User:{admin};User:ANONYMOUS','log.dirs=/data','auto.create.topics.enable=false','default.replication.factor=3','min.insync.replicas=2','offsets.topic.replication.factor=3','num.partitions=1','log.segment.bytes=1048576','log.cleaner.dedupe.buffer.size=16777216'])+'\n');cfg.chmod(0o644)
            common=['--label','yeodam.run='+prefix,'--platform','linux/amd64','--network',net,'--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','512m','--cpus','1','--tmpfs','/tmp:rw,nosuid,size=64m','-v',f'{directory}:/check:ro','-v',f'{data}:/data','-e','KAFKA_HEAP_OPTS=-Xms64m -Xmx256m','-e','LOG_DIR=/tmp/kafka-logs']
            docker('run','--rm',*common,'--entrypoint','/opt/kafka/bin/kafka-storage.sh',image,'format','--cluster-id='+cluster,'-c',f'/check/broker{i}.properties','--add-scram',f'SCRAM-SHA-512=[name={admin},password={password}]','--add-scram',f'SCRAM-SHA-512=[name={user},password={password}]',timeout=120)
            name=prefix+f'-broker{i}';containers.append(name)
            docker('run','-d','--name',name,'--label','yeodam.issue=537',*common,'--network-alias',f'broker{i}','--entrypoint','/opt/kafka/bin/kafka-server-start.sh',image,f'/check/broker{i}.properties')
        def cli(tool,*args,check=True):
            return docker('run','--rm','--label','yeodam.run='+prefix,'--platform','linux/amd64','--network',net,'--memory','256m','--read-only','--tmpfs','/tmp:rw,size=32m','-v',f'{directory}:/check:ro','-e','KAFKA_HEAP_OPTS=-Xms32m -Xmx96m','--entrypoint','/opt/kafka/bin/'+tool,image,'--bootstrap-server','broker1:9093','--command-config','/check/admin.properties',*args,check=check)
        for _ in range(12):
            states=json.loads(docker('inspect',*containers).stdout)
            if not all(c['State']['Running'] for c in states):raise RuntimeError('Synthetic Broker exited; see saved diagnostics')
            if cli('kafka-topics.sh','--list',check=False).returncode==0:break
            time.sleep(2)
        else:raise RuntimeError('Synthetic Kafka quorum did not start')
        print('Synthetic Kafka quorum ready',flush=True)
        for topic in INTERNAL:
            cli('kafka-topics.sh','--create','--topic',topic,'--partitions','1','--replication-factor','3','--config','cleanup.policy=compact','--config','min.insync.replicas=2')
        topic_args=[arg for t in INTERNAL for arg in ['--topic',t]]
        cli('kafka-acls.sh','--add','--allow-principal','User:'+user,*topic_args,*[arg for op in ['Read','Write','Describe','Create','DescribeConfigs'] for arg in ['--operation',op]])
        cli('kafka-acls.sh','--add','--allow-principal','User:'+user,'--group','connect-v3','--operation','Read')
        cli('kafka-acls.sh','--add','--allow-principal','User:'+user,'--cluster','--operation','Describe')
        # Same public plugin JVM/classes and budgets as the reviewed operand.
        checks=directory/'classes';checks.mkdir(mode=0o755)
        command(['javac','--release','17','-d',str(checks),str(ROOT/'tests/ci/v3/CheckConnectPlugins.java')])
        docker('run','--rm','--label','yeodam.run='+prefix,'--platform','linux/amd64','--memory','768m','--cpus','.5','--read-only','--tmpfs','/tmp:rw,size=64m','-v',f'{checks}:/checks:ro','--entrypoint','java',image,'-Xmx256m','-cp','/opt/kafka/libs/*:/opt/kafka/plugins/debezium-mysql/*:/checks','CheckConnectPlugins')
        worker='\n'.join(['bootstrap.servers=broker1:9093,broker2:9093,broker3:9093','group.id=connect-v3','config.storage.topic=prod.connect-configs','offset.storage.topic=prod.connect-offsets','status.storage.topic=prod.connect-status','config.storage.replication.factor=3','offset.storage.replication.factor=3','status.storage.replication.factor=3','offset.storage.partitions=1','status.storage.partitions=1','key.converter=org.apache.kafka.connect.storage.StringConverter','value.converter=org.apache.kafka.connect.storage.StringConverter','plugin.path=/opt/kafka/plugins','listeners=http://0.0.0.0:8083','rest.advertised.host.name=connect-check','connector.client.config.override.policy=None','producer.acks=all','producer.enable.idempotence=true'])+'\n'
        security=client.replace('username="'+admin+'"','username="'+user+'"')
        worker+=security
        for line in security.splitlines():
            if not line.startswith(('default.api.','request.timeout')):worker+='producer.'+line+'\nconsumer.'+line+'\n'
        (directory/'worker.properties').write_text(worker);(directory/'worker.properties').chmod(0o644)
        name=prefix+'-worker';containers.append(name)
        docker('run','-d','--name',name,'--label','yeodam.issue=537','--label','yeodam.run='+prefix,'--platform','linux/amd64','--network',net,'--network-alias','connect-check','--stop-timeout','90','--memory','768m','--cpus','.5','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/tmp:rw,nosuid,size=128m','-v',f'{directory}:/check:ro','-p','127.0.0.1::8083','-e','KAFKA_HEAP_OPTS=-Xms128m -Xmx256m','-e','LOG_DIR=/tmp/connect-logs','--entrypoint','/opt/kafka/bin/connect-distributed.sh',image,'/check/worker.properties')
        state=json.loads(docker('inspect',name).stdout)[0];port=state['NetworkSettings']['Ports']['8083/tcp'][0]['HostPort']
        wait_api(port)
        print('Minimum-budget Connect REST ready',flush=True)
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/connector-plugins',timeout=5) as response:plugins=json.load(response)
        assert any(p['class']=='io.debezium.connector.mysql.MySqlConnector' and p['version']==LOCK['debeziumVersion'] for p in plugins)
        assert not any(p['class'].startswith('io.debezium.') and p['class']!='io.debezium.connector.mysql.MySqlConnector' for p in plugins)
        docker('restart',name,timeout=120)
        restarted=json.loads(docker('inspect',name).stdout)[0]
        port=restarted['NetworkSettings']['Ports']['8083/tcp'][0]['HostPort']
        wait_api(port)
        state=json.loads(docker('inspect',name).stdout)[0];assert state['State']['Running'] and not state['State']['OOMKilled']
        peak=docker('exec',name,'cat','/sys/fs/cgroup/memory.peak').stdout.strip()
        (output/'result.json').write_text(json.dumps({'tlsScram':True,'internalTopics':3,'replicationFactor':3,'minimumIsr':2,'memoryLimitMi':768,'heapMi':256,'cpuLimit':0.5,'mysqlPluginVersion':LOCK['debeziumVersion'],'connectorCount':0,'restartRecovered':True,'memoryPeakBytes':int(peak)},indent=2))
        print('PASS: 768Mi/0.5CPU Connect, TLS/SCRAM, MySQL+Groovy plugins, RF3 internal topics, zero Connectors and restart recovery',flush=True)
    finally:
        for name in reversed(containers):
            try:
                inspected=docker('inspect',name,check=False)
                if inspected.returncode==0:
                    d=json.loads(inspected.stdout)[0];(output/(name+'-state.json')).write_text(json.dumps({'state':d['State'],'networks':d['NetworkSettings']['Networks']},indent=2))
                logs=docker('logs',name,check=False);(output/(name+'.log')).write_text(logs.stdout+logs.stderr)
                docker('rm','-f',name,check=False)
            except Exception:pass
        remaining=docker('ps','-aq','--filter','label=yeodam.run='+prefix,check=False).stdout.split()
        if remaining:docker('rm','-f',*remaining,check=False)
        docker('network','rm',net,check=False)
        shutil.rmtree(directory)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();smoke(a.image,a.output)
