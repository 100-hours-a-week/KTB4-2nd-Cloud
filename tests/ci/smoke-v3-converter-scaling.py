#!/usr/bin/env python3
"""Actual BE image classes + local two-partition Kafka. No AWS, kubeconfig, user data or GPU."""
import argparse,base64,hashlib,json,os,subprocess,tempfile,time,uuid,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]

def smoke(output=None):
    candidate=json.loads((ROOT/'deploy/v3/releases/candidate.json').read_text());be=candidate['components']['backend']
    broker=json.loads((ROOT/'deploy/v3/connect-image/source.lock.json').read_text())['baseImage']
    prefix='v3-converter-'+uuid.uuid4().hex[:10];network=prefix+'-net';names=[]
    def cmd(args,timeout=120,check=True):
        try:r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
        except subprocess.TimeoutExpired:
            if check:raise RuntimeError('Synthetic command timed out: '+args[0])
            return subprocess.CompletedProcess(args,124,'','')
        if check and r.returncode:raise RuntimeError('Synthetic command failed: '+args[0]+'; '+(r.stdout+r.stderr)[-1600:])
        return r
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        d=Path(tmp);d.chmod(0o755)
        try:
            extract=prefix+'-extract';names.append(extract)
            cmd(['docker','create','--platform','linux/amd64','--name',extract,be['image']])
            cmd(['docker','cp',extract+':/app/app.jar',str(d/'app.jar')])
            with zipfile.ZipFile(d/'app.jar') as jar:
                for n in jar.namelist():
                    if n.startswith('BOOT-INF/classes/') or n.startswith('BOOT-INF/lib/')and n.endswith('.jar'):
                        path=Path(n);assert '..' not in path.parts;jar.extract(n,d)
            java=Path(os.environ.get('JAVA_HOME','/usr'))/'bin'
            javac=str(java/'javac') if (java/'javac').exists() else 'javac'
            cp=str(d/'BOOT-INF/classes')+':'+str(d/'BOOT-INF/lib/*')
            cmd([javac,'--release','25','-cp',cp,'-d',str(d/'fixture-classes'),str(ROOT/'tests/ci/v3/converter/ConverterScalingFixture.java')])
            cmd([javac,'--release','17','-cp',cp,'-d',str(d/'fixture-classes'),str(ROOT/'tests/ci/v3/converter/ConverterLag.java')])
            # One local broker is sufficient for consumer ownership; RF/ISR resilience is not claimed.
            cfg='\n'.join(['node.id=1','process.roles=broker,controller','controller.quorum.voters=1@broker:9093','listeners=CLIENT://:9092,CONTROLLER://:9093','advertised.listeners=CLIENT://broker:9092','listener.security.protocol.map=CLIENT:PLAINTEXT,CONTROLLER:PLAINTEXT','inter.broker.listener.name=CLIENT','controller.listener.names=CONTROLLER','log.dirs=/tmp/kafka-data','offsets.topic.replication.factor=1','transaction.state.log.replication.factor=1','transaction.state.log.min.isr=1','group.initial.rebalance.delay.ms=0','auto.create.topics.enable=false'])+'\n'
            (d/'broker.properties').write_text(cfg)
            cmd(['docker','network','create','--internal',network])
            name=prefix+'-broker';names.append(name)
            cluster=base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip('=')
            cmd(['docker','run','-d','--name',name,'--platform','linux/amd64','--network',network,'--network-alias','broker','--memory','512m','--cpus','1','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:rw,mode=1777,size=384m','-v',str(d/'broker.properties')+':/fixture.properties:ro','-e','LOG_DIR=/tmp/kafka-logs','-e','KAFKA_HEAP_OPTS=-Xms128m -Xmx192m','--entrypoint','/bin/bash',broker,'-ec','/opt/kafka/bin/kafka-storage.sh format -t '+cluster+' -c /fixture.properties && exec /opt/kafka/bin/kafka-server-start.sh /fixture.properties'])
            for _ in range(12):
                state=cmd(['docker','inspect','--format','{{.State.Running}}',name]).stdout.strip()
                if state!='true':raise RuntimeError('Synthetic broker exited before readiness')
                r=cmd(['docker','exec',name,'/opt/kafka/bin/kafka-topics.sh','--bootstrap-server','broker:9092','--list'],timeout=20,check=False)
                if r.returncode==0:break
                time.sleep(1)
            else:raise RuntimeError('Local synthetic broker not ready')
            name=prefix+'-be';names.append(name)
            r=cmd(['docker','run','--name',name,'--platform','linux/amd64','--network',network,'--memory','1g','--cpus','1','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--user','10001:10001','--tmpfs','/tmp:rw,uid=10001,gid=10001,size=32m','-v',str(d)+':/fixture:ro','--entrypoint','java',be['image'],'-Xmx384m','-cp','/fixture/fixture-classes:/fixture/BOOT-INF/classes:/fixture/BOOT-INF/lib/*','com.yeodam.yeodambe.trip.service.ConverterScalingFixture','broker:9092'],timeout=180)
            lines=[s.removeprefix('CONVERTER_PROOF ')for s in r.stdout.splitlines()if s.startswith('CONVERTER_PROOF ')]
            assert len(lines)==1,'Missing actual fixture proof'
            result=json.loads(lines[0]);result.update(image=be['image'],sourceSha=be['sourceSha'],fixtureSha256=hashlib.sha256((ROOT/'tests/ci/v3/converter/ConverterScalingFixture.java').read_bytes()).hexdigest())
            if output:
                output=Path(output);assert not output.exists(),'Do not overwrite prior evidence';output.write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps(result));return result
        except Exception:
            for name in names:
                if name.endswith('-broker'):
                    diagnostic=cmd(['docker','logs','--tail','18',name],check=False)
                    print((diagnostic.stdout+diagnostic.stderr)[-4000:])
            raise
        finally:
            for name in reversed(names):cmd(['docker','rm','-f',name],check=False)
            cmd(['docker','network','rm',network],check=False)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output');a=p.parse_args();smoke(a.output)
